"""
Agent Schedule Scheduler - polls for due agent schedules and triggers execution.

Uses the main application event loop (captured at startup) so that
fire-and-forget tasks spawned by send_session_message (title generation,
process_pending_messages / streaming) survive beyond the poll cycle.

Supports two schedule types:
- static_prompt: Creates a session with a prompt (original behavior)
- script_trigger: Executes a shell command in the agent environment; if output is
  exactly "OK" → logs success activity; if anything else → starts session with context

Every schedule execution emits one of three lifecycle events
(CRON_COMPLETED_OK / CRON_TRIGGER_SESSION / CRON_ERROR) so other services
(status puller, dashboards, activity feed) can react without polling.
"""
import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from sqlmodel import Session as DBSession, select

from app.core.db import engine
from app.models import (
    AgentSchedule,
    Agent,
    ChannelAccessPolicy,
    SessionSender,
)
from app.models.events.event import EventType
from app.services.agents.agent_scheduler_service import AgentSchedulerService
from app.services.sessions.channel_ingestion_service import (
    ChannelIngestionService,
    NoActiveEnvironmentError,
)

logger = logging.getLogger(__name__)


async def _emit_cron_event(
    event_type: str,
    schedule: AgentSchedule,
    agent: Agent,
    environment_id: Any | None = None,
    **extra_meta: Any,
) -> None:
    """
    Emit a CRON_* lifecycle event (best-effort, never raises).

    `environment_id` is included in meta so handlers like
    AgentStatusService.handle_post_action_event can pull STATUS.md without
    extra DB lookups.
    """
    try:
        from app.services.events.event_service import event_service
        meta: dict[str, Any] = {
            "schedule_id": str(schedule.id),
            "schedule_type": schedule.schedule_type,
            "schedule_name": schedule.name,
            "agent_id": str(agent.id),
        }
        if environment_id is not None:
            meta["environment_id"] = str(environment_id)
        meta.update({k: v for k, v in extra_meta.items() if v is not None})
        await event_service.emit_event(
            event_type=event_type,
            model_id=agent.id,
            user_id=agent.owner_id,
            meta=meta,
        )
    except Exception as exc:
        logger.warning("Failed to emit %s event: %s", event_type, exc)

scheduler = BackgroundScheduler()

# Captured at start_scheduler() time — the uvicorn / FastAPI event loop.
_main_loop: asyncio.AbstractEventLoop | None = None


async def _poll_due_schedules() -> None:
    """Poll for due agent schedules and trigger execution."""
    from app.services.sessions.session_service import SessionService
    from app.services.environments.agent_env_connector import agent_env_connector
    from app.services.events.activity_service import ActivityService

    now = datetime.now(UTC)

    with DBSession(engine) as db_session:
        statement = select(AgentSchedule).where(
            AgentSchedule.enabled == True,  # noqa: E712
            AgentSchedule.next_execution <= now,
        )
        due_schedules = list(db_session.exec(statement).all())

        if not due_schedules:
            return

        logger.info(f"Found {len(due_schedules)} agent schedules due for execution")

        for schedule in due_schedules:
            try:
                agent = db_session.get(Agent, schedule.agent_id)
                if not agent:
                    logger.error(
                        f"Schedule {schedule.id}: agent {schedule.agent_id} not found"
                    )
                    continue

                if not agent.is_active:
                    logger.warning(
                        f"Schedule {schedule.id}: agent {schedule.agent_id} is inactive, skipping"
                    )
                    continue

                # CRON gating: a critical (running-but-degraded) env receives no
                # scheduled runs. Record the skip, notify (throttled), advance
                # next_execution to avoid a per-minute skip loop, and continue.
                # Only CRON is gated — sessions/chat/terminals are untouched.
                environment = AgentSchedulerService.get_active_environment(
                    db_session, agent.id
                )
                if environment is not None and environment.critical_state:
                    await _skip_schedule_for_critical_env(
                        schedule=schedule,
                        agent=agent,
                        environment=environment,
                        db_session=db_session,
                    )
                    continue

                if schedule.schedule_type == "script_trigger":
                    await _execute_script_trigger(
                        schedule=schedule,
                        agent=agent,
                        db_session=db_session,
                        session_service=SessionService,
                        activity_service=ActivityService,
                        env_connector=agent_env_connector,
                    )
                else:
                    # static_prompt (default behavior)
                    await _execute_static_prompt(
                        schedule=schedule,
                        agent=agent,
                        db_session=db_session,
                        session_service=SessionService,
                    )

            except Exception as e:
                logger.error(
                    f"Error executing schedule {schedule.id}: {e}", exc_info=True
                )


async def _skip_schedule_for_critical_env(
    *,
    schedule: AgentSchedule,
    agent: Agent,
    environment: Any,
    db_session: DBSession,
) -> None:
    """Record a CRON skip for a critical environment and notify the owner.

    Writes a ``skipped`` AgentScheduleLog + a ``cron_skipped`` env action-log,
    notifies the owner (throttled by the environment_id dedup scope), and
    advances ``next_execution`` so the schedule is re-evaluated next cycle
    rather than re-firing the same due time every minute. All side effects are
    best-effort — a surfacing failure must never break the poll, and the
    next_execution advance always runs so the skip loop is broken even if a
    log/notify step raised.
    """
    reason = (
        "Skipped: environment is in a critical state and is not eligible for "
        "scheduled execution. Resolve the environment issue and the schedule "
        "will resume."
    )

    # 1) Schedule-side record (skipped) — surfaces in the execution-logs modal.
    try:
        AgentSchedulerService.create_log(
            db_session,
            schedule_id=schedule.id,
            agent_id=agent.id,
            schedule_type=schedule.schedule_type,
            status="skipped",
            prompt_used=schedule.prompt if schedule.schedule_type != "script_trigger" else None,
            command_executed=schedule.command if schedule.schedule_type == "script_trigger" else None,
            error_message=reason,
        )
    except Exception as exc:
        logger.warning(
            f"Schedule {schedule.id}: failed to write skipped schedule-log: {exc}"
        )

    # 2) Env-side action-log record (cron_skipped).
    try:
        from app.services.environments.agent_env_action_log_service import (
            AgentEnvActionLogService,
        )

        AgentEnvActionLogService.record(
            db_session,
            environment_id=environment.id,
            agent_id=agent.id,
            action="cron_skipped",
            status="skipped",
            cause=environment.critical_cause,
            summary="Scheduled run skipped — environment critical",
            detail=(
                f"Schedule '{schedule.name}' skipped because the environment is "
                f"critical (cause: {environment.critical_cause or 'unknown'})."
            ),
        )
    except Exception as exc:
        logger.warning(
            f"Schedule {schedule.id}: failed to write cron_skipped action-log: {exc}"
        )

    # 3) Owner notification (throttled by environment_id dedup).
    try:
        from app.core.config import settings
        from app.services.notifications.notification_catalog import NotificationType
        from app.services.notifications.notification_service import (
            SystemNotificationService,
        )

        await SystemNotificationService.notify(
            db_session,
            user_id=agent.owner_id,
            notification_type=NotificationType.ENVIRONMENT_CRITICAL,
            context={
                "project_name": settings.PROJECT_NAME,
                "agent_name": agent.name or "your agent",
                "instance_name": environment.instance_name or str(environment.id),
                "environment_id": str(environment.id),
                "reason": "a scheduled run was skipped",
                "detail": "a scheduled run was skipped",
                "link": f"{settings.FRONTEND_HOST}/agents/{agent.id}",
            },
        )
    except Exception as exc:
        logger.debug(
            f"Schedule {schedule.id}: failed to dispatch cron-skip notification: {exc}"
        )

    # 4) Advance next_execution to break the per-minute skip loop. This must
    #    always run (do NOT advance last_execution — that would imply success).
    try:
        AgentSchedulerService.update_execution_time(
            session=db_session,
            schedule_id=schedule.id,
            last_execution=schedule.last_execution,
        )
    except Exception as exc:
        logger.warning(
            f"Schedule {schedule.id}: failed to advance next_execution after "
            f"critical-env skip: {exc}"
        )

    logger.info(
        f"Schedule {schedule.id}: skipped — environment {environment.id} is critical"
    )


async def _execute_static_prompt(
    schedule: AgentSchedule,
    agent: Agent,
    db_session: DBSession,
    session_service,
) -> None:
    """Execute a static_prompt schedule — original behavior with added logging."""
    del session_service  # kept for signature compatibility; routed through ChannelIngestionService
    message = schedule.prompt or agent.entrypoint_prompt or "Start scheduled execution."

    sender = SessionSender.from_system_trigger(
        owner_user_id=agent.owner_id,
        trigger_kind="schedule",
        trigger_id=schedule.id,
        display_name=schedule.name,
    )
    try:
        ingestion = await ChannelIngestionService.ingest_inbound_message(
            db=db_session,
            agent=agent,
            sender=sender,
            thread_key=None,
            content=message,
            integration_type="schedule",
            access_policy=ChannelAccessPolicy(
                expected_owner_id=agent.owner_id,
                allow_system_trigger_fastpath=True,
            ),
            get_fresh_db_session=lambda: DBSession(engine),
            extra_session_kwargs={
                "session_metadata_extra": {"schedule_id": str(schedule.id)},
            },
        )
        action = ingestion.action
        session_id = ingestion.session.id if ingestion.session is not None else None
        error_msg = ingestion.message if action == "error" else None
    except NoActiveEnvironmentError as exc:
        # Mirror the old "Failed to create session" error path that returned
        # when send_session_message(session_id=None, ...) hit an agent with no
        # active environment.
        action = "error"
        session_id = None
        error_msg = str(exc)

    if action == "error":
        logger.error(
            f"Schedule {schedule.id}: failed to execute agent {agent.id}: {error_msg}"
        )
        AgentSchedulerService.create_log(
            db_session,
            schedule_id=schedule.id,
            agent_id=agent.id,
            schedule_type="static_prompt",
            status="error",
            prompt_used=message,
            error_message=error_msg,
        )
        await _emit_cron_event(
            EventType.CRON_ERROR,
            schedule=schedule,
            agent=agent,
            environment_id=agent.active_environment_id,
            error_message=error_msg,
        )
        return  # Do NOT advance schedule on failure

    logger.info(
        f"Schedule {schedule.id}: fired agent {agent.id} (static_prompt), "
        f"session={session_id}, action={action}"
    )
    AgentSchedulerService.create_log(
        db_session,
        schedule_id=schedule.id,
        agent_id=agent.id,
        schedule_type="static_prompt",
        status="success",
        prompt_used=message,
        session_id=session_id,
    )
    AgentSchedulerService.update_execution_time(
        session=db_session,
        schedule_id=schedule.id,
        last_execution=datetime.now(UTC),
    )
    await _emit_cron_event(
        EventType.CRON_TRIGGER_SESSION,
        schedule=schedule,
        agent=agent,
        environment_id=agent.active_environment_id,
        session_id=session_id,
    )


async def _execute_script_trigger(
    schedule: AgentSchedule,
    agent: Agent,
    db_session: DBSession,
    session_service,
    activity_service,
    env_connector,
) -> None:
    """Execute a script_trigger schedule — run command, check output, act accordingly."""
    del session_service  # kept for signature compatibility; routed through ChannelIngestionService
    # Resolve active environment
    environment = AgentSchedulerService.get_active_environment(db_session, agent.id)
    if not environment:
        logger.error(
            f"Schedule {schedule.id}: no active environment for agent {agent.id}"
        )
        AgentSchedulerService.create_log(
            db_session,
            schedule_id=schedule.id,
            agent_id=agent.id,
            schedule_type="script_trigger",
            status="error",
            command_executed=schedule.command,
            error_message="No active environment found for agent",
        )
        await _emit_cron_event(
            EventType.CRON_ERROR,
            schedule=schedule,
            agent=agent,
            environment_id=None,
            error_message="No active environment found for agent",
        )
        return

    # Ensure environment is running (auto-activate if suspended/stopped)
    try:
        environment = await AgentSchedulerService.ensure_environment_running(
            environment,
            get_fresh_db_session=lambda: DBSession(engine),
        )
    except RuntimeError as e:
        logger.error(
            f"Schedule {schedule.id}: environment activation failed: {e}"
        )
        AgentSchedulerService.create_log(
            db_session,
            schedule_id=schedule.id,
            agent_id=agent.id,
            schedule_type="script_trigger",
            status="error",
            command_executed=schedule.command,
            error_message=str(e),
        )
        await _emit_cron_event(
            EventType.CRON_ERROR,
            schedule=schedule,
            agent=agent,
            environment_id=environment.id,
            error_message=str(e),
        )
        return

    # Build environment URL and auth token using the same helpers as MessageService
    from app.services.sessions.message_service import MessageService

    base_url = MessageService.get_environment_url(environment)
    auth_headers = MessageService.get_auth_headers(environment)
    auth_token = (environment.config or {}).get("auth_token", "")

    # Execute the command
    executed_at = datetime.now(UTC)
    try:
        exec_result = await env_connector.exec_command(
            base_url=base_url,
            auth_token=auth_token,
            command=schedule.command,
        )
    except RuntimeError as e:
        logger.error(f"Schedule {schedule.id}: command execution failed: {e}")
        AgentSchedulerService.create_log(
            db_session,
            schedule_id=schedule.id,
            agent_id=agent.id,
            schedule_type="script_trigger",
            status="error",
            command_executed=schedule.command,
            error_message=str(e),
        )
        await _emit_cron_event(
            EventType.CRON_ERROR,
            schedule=schedule,
            agent=agent,
            environment_id=environment.id,
            error_message=str(e),
        )
        return

    exit_code = exec_result.get("exit_code", -1)
    stdout = exec_result.get("stdout", "")
    stderr = exec_result.get("stderr", "")

    # Check if output is exactly "OK" (case-sensitive, whitespace-trimmed)
    if exit_code == 0 and stdout.strip() == "OK":
        # Success: log activity and advance schedule (no session created)
        try:
            from app.models import ActivityCreate
            activity_service.create_activity(
                db_session=db_session,
                user_id=agent.owner_id,
                data=ActivityCreate(
                    agent_id=agent.id,
                    session_id=None,
                    activity_type="schedule_executed",
                    text=f"Schedule '{schedule.name}' executed successfully",
                    action_required="",
                ),
            )
        except Exception as ae:
            logger.warning(
                f"Schedule {schedule.id}: failed to create activity: {ae}"
            )

        AgentSchedulerService.create_log(
            db_session,
            schedule_id=schedule.id,
            agent_id=agent.id,
            schedule_type="script_trigger",
            status="success",
            command_executed=schedule.command,
            command_output=stdout,
            command_exit_code=exit_code,
        )
        AgentSchedulerService.update_execution_time(
            session=db_session,
            schedule_id=schedule.id,
            last_execution=executed_at,
        )
        logger.info(
            f"Schedule {schedule.id}: script returned OK — no session created"
        )
        await _emit_cron_event(
            EventType.CRON_COMPLETED_OK,
            schedule=schedule,
            agent=agent,
            environment_id=environment.id,
            command_exit_code=exit_code,
        )

    else:
        # Non-OK output: start session with execution context
        context_message = _build_script_context_message(
            schedule_name=schedule.name,
            command=schedule.command,
            executed_at=executed_at,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
        )

        sender = SessionSender.from_system_trigger(
            owner_user_id=agent.owner_id,
            trigger_kind="schedule",
            trigger_id=schedule.id,
            display_name=schedule.name,
        )
        try:
            ingestion = await ChannelIngestionService.ingest_inbound_message(
                db=db_session,
                agent=agent,
                sender=sender,
                thread_key=None,
                content=context_message,
                integration_type="schedule",
                access_policy=ChannelAccessPolicy(
                    expected_owner_id=agent.owner_id,
                    allow_system_trigger_fastpath=True,
                ),
                get_fresh_db_session=lambda: DBSession(engine),
                extra_session_kwargs={
                    "session_metadata_extra": {"schedule_id": str(schedule.id)},
                },
            )
            action = ingestion.action
            session_id = ingestion.session.id if ingestion.session is not None else None
            error_msg = ingestion.message if action == "error" else None
        except NoActiveEnvironmentError as exc:
            action = "error"
            session_id = None
            error_msg = str(exc)

        if action == "error":
            logger.error(
                f"Schedule {schedule.id}: failed to create session for non-OK output: "
                f"{error_msg}"
            )
            AgentSchedulerService.create_log(
                db_session,
                schedule_id=schedule.id,
                agent_id=agent.id,
                schedule_type="script_trigger",
                status="error",
                command_executed=schedule.command,
                command_output=stdout,
                command_exit_code=exit_code,
                error_message=error_msg,
            )
            await _emit_cron_event(
                EventType.CRON_ERROR,
                schedule=schedule,
                agent=agent,
                environment_id=environment.id,
                command_exit_code=exit_code,
                error_message=error_msg,
            )
            return

        AgentSchedulerService.create_log(
            db_session,
            schedule_id=schedule.id,
            agent_id=agent.id,
            schedule_type="script_trigger",
            status="session_triggered",
            command_executed=schedule.command,
            command_output=stdout,
            command_exit_code=exit_code,
            session_id=session_id,
        )
        AgentSchedulerService.update_execution_time(
            session=db_session,
            schedule_id=schedule.id,
            last_execution=executed_at,
        )
        logger.info(
            f"Schedule {schedule.id}: script non-OK (exit={exit_code}), "
            f"session={session_id} created"
        )
        await _emit_cron_event(
            EventType.CRON_TRIGGER_SESSION,
            schedule=schedule,
            agent=agent,
            environment_id=environment.id,
            session_id=session_id,
            command_exit_code=exit_code,
        )


def _build_script_context_message(
    schedule_name: str,
    command: str,
    executed_at: datetime,
    exit_code: int,
    stdout: str,
    stderr: str,
) -> str:
    """Build context message sent to agent when script output is not 'OK'."""
    lines = [
        f'Scheduled script trigger "{schedule_name}" produced output that requires your attention.',
        "",
        f"**Command:** {command}",
        f"**Executed at:** {executed_at.isoformat()}",
        f"**Exit code:** {exit_code}",
        "",
        "**Output:**",
        stdout if stdout.strip() else "(no output)",
    ]
    if stderr:
        lines += [
            "",
            "**Errors:**",
            stderr,
        ]
    lines += [
        "",
        "Please review the output above and take appropriate action.",
    ]
    return "\n".join(lines)


def run_schedule_poll():
    """Submit the poll coroutine to the main application event loop.

    APScheduler runs this in a background thread.  Previously we created
    an ephemeral event loop here, but fire-and-forget tasks spawned by
    send_session_message (title generation, streaming) were silently
    cancelled when that loop closed.  By submitting to the main loop the
    tasks live as long as the application does.
    """
    if _main_loop is None or _main_loop.is_closed():
        logger.error("Main event loop not available — skipping schedule poll")
        return

    try:
        future = asyncio.run_coroutine_threadsafe(_poll_due_schedules(), _main_loop)
        # Wait for the poll itself to finish (not the fire-and-forget tasks it spawns).
        # Timeout generously — the poll should be quick; streaming continues in the background.
        future.result(timeout=120)
    except Exception as e:
        logger.error(f"Agent schedule poll job failed: {e}", exc_info=True)


def start_scheduler():
    """Start background scheduler (call on app startup)."""
    global _main_loop
    _main_loop = asyncio.get_running_loop()

    scheduler.add_job(
        run_schedule_poll,
        "interval",
        minutes=1,
        id="agent_schedule_poll",
        max_instances=1,
    )
    scheduler.start()
    logger.info("Agent schedule scheduler started (polls every 1 minute)")


def shutdown_scheduler():
    """Stop background scheduler (call on app shutdown)."""
    global _main_loop
    scheduler.shutdown()
    _main_loop = None
    logger.info("Agent schedule scheduler stopped")
