"""
Agent Scheduler Service - handles all scheduler-related business logic.

This service:
- Calculates next execution time from CRON strings
- Creates, reads, updates, and deletes AgentSchedule records
- Manages multi-schedule CRUD operations per agent
- Enforces agent ownership and schedule access control
"""
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
import uuid
import pytz
from croniter import croniter
from sqlmodel import Session, select

from app.models import Agent, AgentSchedule

logger = logging.getLogger(__name__)


# ==================== Manual Execution Result ====================


@dataclass
class ManualRunResult:
    """
    Outcome of a manual ``execute_now`` invocation.

    ``action="executed"`` — the schedule ran synchronously inside the request.
    ``action="env_starting"`` — the agent's environment was not running and
    activation has been kicked off in the background; the schedule will run
    automatically once the environment becomes ready.

    The route layer maps the action to a user-facing message — copy stays out
    of the service.
    """

    action: Literal["executed", "env_starting"]


# ==================== Domain Exceptions ====================


class ScheduleError(Exception):
    """Base exception for agent schedule service errors."""

    def __init__(self, message: str, status_code: int = 400):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class ScheduleNotFoundError(ScheduleError):
    """Schedule not found."""

    def __init__(self, message: str = "Schedule not found for this agent"):
        super().__init__(message, status_code=404)


class AgentNotFoundError(ScheduleError):
    """Agent not found."""

    def __init__(self, message: str = "Agent not found"):
        super().__init__(message, status_code=404)


class PermissionDeniedError(ScheduleError):
    """Permission denied."""

    def __init__(self, message: str = "Not enough permissions"):
        super().__init__(message, status_code=400)


class InvalidCronError(ScheduleError):
    """Invalid CRON expression or timezone."""

    def __init__(self, detail: str):
        super().__init__(f"Invalid CRON string or timezone: {detail}", status_code=400)


# ==================== Manual-Run Helpers (module-level) ====================
#
# Extracted from ``AgentSchedulerService.execute_now`` so the synchronous
# fast path and the deferred (post-activation) background path share the
# same schedule-type dispatch, and so the deferred path can be tested /
# reasoned about without dragging the route-scoped DB session along.


async def _dispatch_schedule(
    db_session: Session,
    schedule: AgentSchedule,
    agent: Agent,
) -> None:
    """
    Run a schedule once on a live environment — the post-precheck core
    shared between the synchronous (fast) and deferred (background) paths
    of ``execute_now``.

    Branches on ``schedule.schedule_type`` and delegates to the same
    private ``_execute_static_prompt`` / ``_execute_script_trigger``
    helpers used by the background cron scheduler so behaviour stays in
    lockstep.
    """
    from app.services.sessions.session_service import SessionService
    from app.services.environments.agent_env_connector import agent_env_connector
    from app.services.events.activity_service import ActivityService
    from app.services.agents.agent_schedule_scheduler import (
        _execute_static_prompt,
        _execute_script_trigger,
    )

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
        await _execute_static_prompt(
            schedule=schedule,
            agent=agent,
            db_session=db_session,
            session_service=SessionService,
        )


async def _activate_env_and_run_schedule(
    agent_id: uuid.UUID,
    schedule_id: uuid.UUID,
) -> None:
    """
    Deferred manual-run worker.

    Opens a fresh DB session (the route-scoped one is gone by now), waits
    for the agent's environment to come up via
    ``environment_resolver.ensure_environment_running``, then dispatches
    the schedule via ``_dispatch_schedule``.

    On either failure path (activation timeout / env entered error /
    disappeared / dispatch raised), surfaces the failure via an
    ``AgentScheduleLog`` row plus a ``CRON_ERROR`` event so the UI logs
    panel and activity feed show the same failure shape a cron-poll
    failure would — without this the user only sees the "Environment is
    starting…" toast and has no signal that anything went wrong.
    """
    from app.core.db import engine
    from sqlmodel import Session as DBSession
    from app.services.agents.environment_resolver import (
        ensure_environment_running,
        get_active_environment,
    )

    with DBSession(engine) as fresh_db:
        fresh_agent = fresh_db.get(Agent, agent_id)
        fresh_schedule = fresh_db.get(AgentSchedule, schedule_id)
        fresh_env = get_active_environment(fresh_db, agent_id)

        if not fresh_agent or not fresh_schedule or not fresh_env:
            logger.error(
                "Deferred manual run aborted — missing agent/schedule/env "
                f"(agent={agent_id}, schedule={schedule_id})"
            )
            # Best-effort surface: only if we still have agent + schedule.
            if fresh_agent and fresh_schedule:
                await _log_and_emit_manual_run_error(
                    db_session=fresh_db,
                    schedule=fresh_schedule,
                    agent=fresh_agent,
                    environment_id=None,
                    error_message="Agent environment not found after activation request",
                )
            return

        env_id = fresh_env.id

        try:
            await ensure_environment_running(
                fresh_env,
                get_fresh_db_session=lambda: DBSession(engine),
            )
        except RuntimeError as exc:
            logger.error(
                f"Deferred manual run for schedule {schedule_id}: "
                f"environment activation failed: {exc}"
            )
            await _log_and_emit_manual_run_error(
                db_session=fresh_db,
                schedule=fresh_schedule,
                agent=fresh_agent,
                environment_id=env_id,
                error_message=f"Environment activation failed: {exc}",
            )
            return

        try:
            await _dispatch_schedule(fresh_db, fresh_schedule, fresh_agent)
        except Exception as exc:
            logger.error(
                f"Deferred manual run for schedule {schedule_id} "
                f"failed after env activation: {exc}",
                exc_info=True,
            )
            await _log_and_emit_manual_run_error(
                db_session=fresh_db,
                schedule=fresh_schedule,
                agent=fresh_agent,
                environment_id=env_id,
                error_message=f"Schedule execution failed after env activation: {exc}",
            )


async def _log_and_emit_manual_run_error(
    *,
    db_session: Session,
    schedule: AgentSchedule,
    agent: Agent,
    environment_id: "uuid.UUID | None",
    error_message: str,
) -> None:
    """
    Mirror the cron-error surface for a deferred manual-run failure.

    Writes an ``AgentScheduleLog`` (status=error, schedule_type + populated
    ``command_executed``/``prompt_used`` matching the type), then emits
    ``EventType.CRON_ERROR`` via the existing helper. Both are best-effort:
    any exception is swallowed and logged so a failure surfacing the error
    doesn't itself raise.
    """
    from app.models.events.event import EventType
    from app.services.agents.agent_schedule_scheduler import _emit_cron_event

    schedule_type = schedule.schedule_type
    prompt_used: str | None = None
    command_executed: str | None = None
    if schedule_type == "script_trigger":
        command_executed = schedule.command
    else:
        # Match the cron static_prompt log shape — `_execute_static_prompt`
        # records the resolved message (schedule.prompt or agent
        # entrypoint), so we do the same here.
        prompt_used = (
            schedule.prompt
            or agent.entrypoint_prompt
            or "Start scheduled execution."
        )

    try:
        AgentSchedulerService.create_log(
            db_session,
            schedule_id=schedule.id,
            agent_id=agent.id,
            schedule_type=schedule_type,
            status="error",
            prompt_used=prompt_used,
            command_executed=command_executed,
            error_message=error_message,
        )
    except Exception as log_exc:
        logger.error(
            f"Deferred manual run: failed to write error log for "
            f"schedule {schedule.id}: {log_exc}",
            exc_info=True,
        )

    # ``_emit_cron_event`` already swallows its own errors and never raises,
    # but wrap defensively so any future change in its contract can't break
    # the surrounding background task.
    try:
        await _emit_cron_event(
            EventType.CRON_ERROR,
            schedule=schedule,
            agent=agent,
            environment_id=environment_id,
            error_message=error_message,
        )
    except Exception as ev_exc:
        logger.error(
            f"Deferred manual run: failed to emit CRON_ERROR event for "
            f"schedule {schedule.id}: {ev_exc}",
            exc_info=True,
        )


# ==================== Service ====================


class AgentSchedulerService:
    """Service for managing agent schedules."""

    # ==================== Frequency Constraints ====================
    #
    # Minimum gap (in minutes) the platform allows between two consecutive
    # executions, keyed by schedule type. ``static_prompt`` schedules always
    # spin up a session and spend tokens, so they keep a floor. ``script_trigger``
    # schedules usually no-op (the command returns "OK" without creating a
    # session or spending tokens), so they have NO floor and may run as
    # frequently as the user needs.
    #
    # Enforcement is deterministic (computed from the CRON string below), NOT
    # delegated to the LLM — the AI generator only translates natural language
    # to a CRON expression; this service is the source of truth for whether the
    # resulting cadence is allowed.
    MINIMUM_INTERVAL_MINUTES: dict[str, int] = {
        "static_prompt": 10,
        "script_trigger": 0,
    }

    @staticmethod
    def _minimum_interval_minutes(cron_string: str) -> float:
        """
        Smallest gap (in minutes) between any two consecutive fire times of a
        CRON expression.

        Samples a fixed window of consecutive executions from a fixed base
        time (deterministic, timezone-invariant for minute/hour cadence) and
        returns the tightest gap observed — this captures the real cadence of
        step expressions like ``*/40`` (which fire at :00 and :40, a true 20
        minute minimum gap), not just the nominal "40 minutes".
        """
        base = datetime(2024, 1, 1, tzinfo=pytz.utc)
        itr = croniter(cron_string, base)
        prev = itr.get_next(datetime)
        smallest = float("inf")
        # 500 steps covers well over a full day even for per-minute schedules,
        # which is enough to surface any within-day minimum gap.
        for _ in range(500):
            nxt = itr.get_next(datetime)
            gap = (nxt - prev).total_seconds() / 60.0
            if gap < smallest:
                smallest = gap
            prev = nxt
        return smallest

    @staticmethod
    def validate_frequency(cron_string: str, schedule_type: str) -> None:
        """
        Reject a schedule whose cadence is tighter than the per-type minimum.

        Args:
            cron_string: CRON expression (local or UTC — minute/hour gaps are
                timezone-invariant)
            schedule_type: "static_prompt" or "script_trigger"

        Raises:
            ScheduleError: If the schedule would run more frequently than the
                minimum allowed for its type.
            InvalidCronError: If the CRON string cannot be parsed.
        """
        minimum = AgentSchedulerService.MINIMUM_INTERVAL_MINUTES.get(
            schedule_type, 10
        )
        if minimum <= 0:
            return  # No floor for this type (e.g. script_trigger).

        try:
            interval = AgentSchedulerService._minimum_interval_minutes(cron_string)
        except Exception as e:
            raise InvalidCronError(str(e))

        if interval < minimum:
            raise ScheduleError(
                f"Execution frequency too high: minimum interval for "
                f"{schedule_type.replace('_', ' ')} schedules is {minimum} "
                f"minutes, but this schedule would run every "
                f"{int(interval)} minutes."
            )

    # ==================== Access Control Helpers ====================

    @staticmethod
    def verify_agent_access(
        session: Session,
        agent_id: uuid.UUID,
        user_id: uuid.UUID,
        *,
        is_superuser: bool = False,
    ) -> Agent:
        """
        Verify agent exists and user has access.

        Args:
            session: Database session
            agent_id: Agent UUID to verify
            user_id: User ID requesting access
            is_superuser: Whether the user is a superuser (bypasses ownership check)

        Returns:
            Agent instance if valid

        Raises:
            AgentNotFoundError: If agent doesn't exist
            PermissionDeniedError: If user doesn't own the agent
        """
        agent = session.get(Agent, agent_id)
        if not agent:
            raise AgentNotFoundError()
        if not is_superuser and agent.owner_id != user_id:
            raise PermissionDeniedError()
        return agent

    @staticmethod
    def get_schedule_for_agent(
        session: Session,
        agent_id: uuid.UUID,
        schedule_id: uuid.UUID,
    ) -> AgentSchedule:
        """
        Get a schedule and verify it belongs to the given agent.

        Args:
            session: Database session
            agent_id: Agent UUID the schedule should belong to
            schedule_id: Schedule UUID to fetch

        Returns:
            AgentSchedule instance

        Raises:
            ScheduleNotFoundError: If schedule doesn't exist or doesn't belong to the agent
        """
        schedule = session.get(AgentSchedule, schedule_id)
        if not schedule or schedule.agent_id != agent_id:
            raise ScheduleNotFoundError()
        return schedule

    # ==================== CRON Utilities ====================

    @staticmethod
    def convert_local_cron_to_utc(cron_string: str, timezone: str) -> str:
        """
        Convert CRON expression from local time to UTC.

        Args:
            cron_string: CRON expression in local time
            timezone: User's IANA timezone

        Returns:
            CRON expression in UTC

        Raises:
            InvalidCronError: If CRON string or timezone is invalid
        """
        try:
            user_tz = pytz.timezone(timezone)

            # Parse the cron string
            parts = cron_string.split()
            if len(parts) != 5:
                raise ValueError("Invalid CRON format")

            minute, hour, day, month, day_of_week = parts

            # If hour is *, don't convert (hourly schedules)
            if hour == '*' or '/' in hour:
                return cron_string

            # Create a naive datetime (we'll use tomorrow to avoid edge cases with current time)
            # Must be naive (no tzinfo) so that pytz.localize() can attach the user timezone.
            naive_dt = datetime.now(UTC).replace(
                hour=0, minute=0, second=0, microsecond=0, tzinfo=None
            ) + timedelta(days=1)

            # Handle hour ranges (e.g., "9-17")
            if '-' in hour:
                start, end = hour.split('-')
                start_hour = int(start)
                end_hour = int(end)

                # Create localized datetime in user timezone
                local_dt_start = user_tz.localize(naive_dt.replace(hour=start_hour))
                local_dt_end = user_tz.localize(naive_dt.replace(hour=end_hour))

                # Convert to UTC
                utc_dt_start = local_dt_start.astimezone(pytz.utc)
                utc_dt_end = local_dt_end.astimezone(pytz.utc)

                utc_hour = f"{utc_dt_start.hour}-{utc_dt_end.hour}"
            else:
                # Single hour or comma-separated hours
                if ',' in hour:
                    hours = [int(h) for h in hour.split(',')]
                else:
                    hours = [int(hour)]

                utc_hours = []
                for h in hours:
                    # Create localized datetime in user timezone
                    local_dt = user_tz.localize(naive_dt.replace(hour=h))

                    # Convert to UTC
                    utc_dt = local_dt.astimezone(pytz.utc)
                    utc_hours.append(utc_dt.hour)

                utc_hour = ','.join(str(h) for h in utc_hours) if len(utc_hours) > 1 else str(utc_hours[0])

            result = f"{minute} {utc_hour} {day} {month} {day_of_week}"
            return result
        except InvalidCronError:
            raise
        except Exception as e:
            logger.error(f"Failed to convert CRON to UTC: {e}", exc_info=True)
            raise InvalidCronError(str(e))

    @staticmethod
    def calculate_next_execution(cron_string: str) -> datetime:
        """
        Calculate next execution time from CRON string.

        Args:
            cron_string: CRON expression in UTC

        Returns:
            Next execution datetime in UTC

        Raises:
            InvalidCronError: If CRON string is invalid
        """
        try:
            # Create croniter with current UTC time
            now_utc = datetime.now(pytz.utc)
            cron = croniter(cron_string, now_utc)

            # Get next run as datetime
            next_run = cron.get_next(datetime)

            # Check if it's naive or aware
            if next_run.tzinfo is None:
                # Naive datetime - assume it's UTC and localize
                next_run_utc = pytz.utc.localize(next_run)
            else:
                # Already aware - ensure it's in UTC
                next_run_utc = next_run.astimezone(pytz.utc)
            return next_run_utc
        except InvalidCronError:
            raise
        except Exception as e:
            logger.error(f"Failed to calculate next execution: {e}", exc_info=True)
            raise InvalidCronError(str(e))

    # ==================== Generate Preview ====================

    @staticmethod
    def generate_schedule_preview(
        natural_language: str,
        timezone: str,
        schedule_type: str = "static_prompt",
        user: "User | None" = None,
        db: "Session | None" = None,
    ) -> dict:
        """
        Generate a CRON schedule from natural language and calculate next execution.

        Orchestrates the AI call, CRON conversion, type-aware frequency
        validation, and next_execution calculation.

        Args:
            natural_language: User's schedule description
            timezone: User's IANA timezone
            schedule_type: "static_prompt" or "script_trigger" — selects which
                minimum-interval floor to enforce on the generated cadence

        Returns:
            Dict with success, cron_string, description, next_execution, or error
        """
        from app.services.ai_functions.ai_functions_service import AIFunctionsService

        ai_result = AIFunctionsService.generate_schedule(
            natural_language=natural_language,
            timezone=timezone,
            user=user,
            db=db,
        )

        if ai_result.get("success"):
            try:
                cron_utc = AgentSchedulerService.convert_local_cron_to_utc(
                    ai_result["cron_string"], timezone
                )
                AgentSchedulerService.validate_frequency(cron_utc, schedule_type)
                next_exec = AgentSchedulerService.calculate_next_execution(cron_utc)
                ai_result["next_execution"] = next_exec.isoformat()
            except ScheduleError as e:
                # Type-aware frequency / cron errors carry a user-facing message.
                return {"success": False, "error": e.message}
            except Exception as e:
                return {
                    "success": False,
                    "error": f"Failed to calculate next execution: {str(e)}",
                }

        return ai_result

    # ==================== CRUD Operations ====================

    @staticmethod
    def create_schedule(
        *,
        session: Session,
        agent_id: uuid.UUID,
        name: str,
        cron_string: str,
        timezone: str,
        description: str,
        prompt: str | None = None,
        enabled: bool = True,
        schedule_type: str = "static_prompt",
        command: str | None = None,
    ) -> AgentSchedule:
        """
        Create a new agent schedule.

        Args:
            session: Database session
            agent_id: Agent UUID
            name: User-friendly label
            cron_string: CRON expression in local time
            timezone: User's IANA timezone (used transiently for conversion, not stored)
            description: Human-readable description
            prompt: Schedule-specific prompt (None = use agent's entrypoint_prompt)
            enabled: Whether schedule is enabled
            schedule_type: "static_prompt" (default) or "script_trigger"
            command: Shell command for script_trigger type (None for static_prompt)

        Returns:
            Created AgentSchedule

        Raises:
            InvalidCronError: If CRON string or timezone is invalid
        """
        # Convert CRON from local time to UTC
        cron_utc = AgentSchedulerService.convert_local_cron_to_utc(
            cron_string, timezone
        )

        # Enforce the per-type minimum execution interval (server-side source
        # of truth — a direct cron_string can bypass the AI preview check).
        AgentSchedulerService.validate_frequency(cron_utc, schedule_type)

        # Calculate next execution
        next_exec = AgentSchedulerService.calculate_next_execution(cron_utc)

        schedule = AgentSchedule(
            agent_id=agent_id,
            name=name,
            cron_string=cron_utc,
            description=description,
            prompt=prompt,
            enabled=enabled,
            next_execution=next_exec,
            schedule_type=schedule_type,
            command=command,
        )
        session.add(schedule)
        session.commit()
        session.refresh(schedule)
        logger.info(f"Created schedule '{name}' (type={schedule_type}) for agent {agent_id}: {description}")
        return schedule

    @staticmethod
    def get_agent_schedules(
        session: Session,
        agent_id: uuid.UUID,
    ) -> list[AgentSchedule]:
        """
        Get all schedules for an agent, ordered by created_at.

        Args:
            session: Database session
            agent_id: Agent UUID

        Returns:
            List of AgentSchedule records
        """
        statement = (
            select(AgentSchedule)
            .where(AgentSchedule.agent_id == agent_id)
            .order_by(AgentSchedule.created_at)
        )
        return list(session.exec(statement).all())

    @staticmethod
    def update_schedule(
        session: Session,
        schedule: AgentSchedule,
        **fields,
    ) -> AgentSchedule:
        """
        Partial update of an agent schedule.

        If cron_string changes, timezone must be provided for UTC conversion
        and next_execution is recalculated.

        Args:
            session: Database session
            schedule: AgentSchedule instance (already verified)
            **fields: Fields to update (name, cron_string, timezone, description, prompt, enabled)

        Returns:
            Updated AgentSchedule

        Raises:
            InvalidCronError: If CRON string or timezone is invalid
            ScheduleError: If timezone missing when updating cron_string
        """
        # Handle cron_string change (requires timezone for conversion)
        if "cron_string" in fields and fields["cron_string"] is not None:
            timezone = fields.pop("timezone", None)
            if not timezone:
                raise ScheduleError("timezone is required when updating cron_string")

            cron_utc = AgentSchedulerService.convert_local_cron_to_utc(
                fields["cron_string"], timezone
            )
            # Enforce the per-type minimum interval using the schedule's own
            # (immutable) type.
            AgentSchedulerService.validate_frequency(
                cron_utc, schedule.schedule_type
            )
            schedule.cron_string = cron_utc
            schedule.next_execution = AgentSchedulerService.calculate_next_execution(cron_utc)
            del fields["cron_string"]
        else:
            # Remove timezone if present but cron_string not changing
            fields.pop("timezone", None)

        # Apply remaining field updates
        for key, value in fields.items():
            if value is not None or key == "prompt":  # Allow setting prompt to None
                setattr(schedule, key, value)

        schedule.updated_at = datetime.now(UTC)
        session.add(schedule)
        session.commit()
        session.refresh(schedule)
        logger.info(f"Updated schedule {schedule.id}")
        return schedule

    @staticmethod
    def delete_schedule(
        session: Session,
        schedule: AgentSchedule,
    ) -> None:
        """
        Delete a schedule.

        Args:
            session: Database session
            schedule: AgentSchedule instance (already verified)
        """
        schedule_id = schedule.id
        session.delete(schedule)
        session.commit()
        logger.info(f"Deleted schedule {schedule_id}")

    @staticmethod
    def get_all_enabled_schedules(session: Session) -> list[AgentSchedule]:
        """
        Get all enabled schedules.

        Args:
            session: Database session

        Returns:
            List of enabled AgentSchedule records
        """
        statement = select(AgentSchedule).where(AgentSchedule.enabled == True)  # noqa: E712
        return list(session.exec(statement).all())

    @staticmethod
    def update_execution_time(
        session: Session,
        schedule_id: uuid.UUID,
        last_execution: datetime,
    ) -> None:
        """
        Update schedule execution times after running an agent.

        Args:
            session: Database session
            schedule_id: Schedule UUID
            last_execution: When the agent was executed
        """
        schedule = session.get(AgentSchedule, schedule_id)
        if schedule:
            schedule.last_execution = last_execution
            schedule.next_execution = AgentSchedulerService.calculate_next_execution(
                schedule.cron_string
            )
            schedule.updated_at = datetime.now(UTC)
            session.commit()
            logger.info(
                f"Updated execution time for schedule {schedule_id}. "
                f"Next run: {schedule.next_execution}"
            )

    # ==================== Schedule Log Operations ====================

    @staticmethod
    def create_log(
        session: Session,
        *,
        schedule_id: uuid.UUID,
        agent_id: uuid.UUID,
        schedule_type: str,
        status: str,
        prompt_used: str | None = None,
        command_executed: str | None = None,
        command_output: str | None = None,
        command_exit_code: int | None = None,
        session_id: uuid.UUID | None = None,
        error_message: str | None = None,
    ) -> "AgentScheduleLog":
        """
        Create an immutable execution log entry for a schedule run.

        Args:
            session: Database session
            schedule_id: Schedule that was executed
            agent_id: Agent the schedule belongs to
            schedule_type: Type snapshot at execution time
            status: "success", "session_triggered", or "error"
            prompt_used: Prompt sent (static_prompt only)
            command_executed: Command that ran (script_trigger only)
            command_output: stdout from command (script_trigger only)
            command_exit_code: Exit code from command (script_trigger only)
            session_id: Session created (if any)
            error_message: Error details if status is "error"

        Returns:
            Created AgentScheduleLog
        """
        from app.models import AgentScheduleLog
        log = AgentScheduleLog(
            schedule_id=schedule_id,
            agent_id=agent_id,
            schedule_type=schedule_type,
            status=status,
            prompt_used=prompt_used,
            command_executed=command_executed,
            command_output=command_output,
            command_exit_code=command_exit_code,
            session_id=session_id,
            error_message=error_message,
            executed_at=datetime.now(UTC),
        )
        session.add(log)
        session.commit()
        session.refresh(log)
        logger.debug(
            f"Created schedule log for schedule {schedule_id}: "
            f"type={schedule_type}, status={status}"
        )
        return log

    @staticmethod
    def get_schedule_logs(
        session: Session,
        schedule_id: uuid.UUID,
        limit: int = 50,
    ) -> list["AgentScheduleLog"]:
        """
        Get recent execution logs for a schedule, ordered by executed_at DESC.

        Args:
            session: Database session
            schedule_id: Schedule UUID to query
            limit: Maximum number of logs to return (default 50)

        Returns:
            List of AgentScheduleLog records, newest first
        """
        from app.models import AgentScheduleLog
        from sqlmodel import desc
        statement = (
            select(AgentScheduleLog)
            .where(AgentScheduleLog.schedule_id == schedule_id)
            .order_by(desc(AgentScheduleLog.executed_at))
            .limit(limit)
        )
        return list(session.exec(statement).all())

    # ==================== Manual Execution ====================

    @staticmethod
    async def execute_now(
        session: Session,
        agent_id: uuid.UUID,
        schedule_id: uuid.UUID,
    ) -> ManualRunResult:
        """
        Execute a schedule immediately, identical to cron execution.

        Behavior depends on the agent's active environment status:

        - ``running`` → execute synchronously and return ``executed``.
        - ``suspended`` / ``stopped`` / ``activating`` / ``starting`` → kick off
          the environment in the background and return ``env_starting``
          immediately. The schedule will execute automatically once the
          environment is ready.
        - ``error`` → raise ``ScheduleError`` (400).
        - missing active environment → raise ``ScheduleError`` (400).

        Args:
            session: Database session
            agent_id: Agent UUID
            schedule_id: Schedule UUID (must belong to agent)

        Returns:
            ManualRunResult describing the outcome. The route layer maps
            ``action`` to a user-facing toast message.

        Raises:
            ScheduleError: If the agent is inactive, has no active environment,
                its environment is in an error state, the schedule is missing
                or belongs to another agent, or synchronous execution fails.
        """
        from app.services.agents.environment_resolver import get_active_environment
        from app.utils import create_task_with_error_logging

        agent = session.get(Agent, agent_id)
        if not agent or not agent.is_active:
            raise ScheduleError("Agent is not active", status_code=400)

        # Defensive: enforce our own preconditions so future callers (and
        # background callers) don't have to rely on the route's
        # ``get_schedule_for_agent`` pre-check.
        schedule = session.get(AgentSchedule, schedule_id)
        if not schedule or schedule.agent_id != agent_id:
            raise ScheduleNotFoundError()

        # Resolve the active environment up front so we can decide whether to
        # run synchronously or defer activation to a background task.
        environment = get_active_environment(session, agent_id)
        if not environment:
            raise ScheduleError(
                "Agent has no active environment", status_code=400
            )

        env_status = environment.status

        if env_status == "error":
            raise ScheduleError(
                "Agent environment is in an error state and cannot be started",
                status_code=400,
            )

        # Fast path: env already running — execute synchronously, exactly as
        # the cron scheduler would.
        if env_status == "running":
            try:
                await _dispatch_schedule(session, schedule, agent)
            except Exception as e:
                logger.error(
                    f"Manual schedule execution failed for {schedule_id}: {e}",
                    exc_info=True,
                )
                raise ScheduleError(
                    f"Schedule execution failed: {e}", status_code=500
                )
            return ManualRunResult(action="executed")

        # Deferred path: env is suspended / stopped / activating / starting.
        # We must NOT block the HTTP request — the background task opens its
        # own DB session (the route-scoped one will be closed by the time
        # activation finishes).
        if env_status not in ("suspended", "stopped", "activating", "starting"):
            # Defensive: any unexpected status is surfaced as a 400 rather
            # than triggering an unbounded background activation attempt.
            raise ScheduleError(
                f"Agent environment is in an unexpected state '{env_status}'",
                status_code=400,
            )

        create_task_with_error_logging(
            _activate_env_and_run_schedule(agent_id, schedule_id),
            task_name=f"manual_run_after_activation_{schedule_id}",
        )

        return ManualRunResult(action="env_starting")

    # ==================== Environment Helpers ====================
    #
    # Thin wrappers kept for backward compatibility — the actual
    # implementations live in ``app.services.agents.environment_resolver``
    # so they can be shared with other features (e.g. agent webhooks).

    @staticmethod
    def get_active_environment(
        session: Session,
        agent_id: uuid.UUID,
    ) -> "AgentEnvironment | None":
        """Return the agent's active environment, or None if not configured."""
        from app.services.agents.environment_resolver import get_active_environment
        return get_active_environment(session, agent_id)

    @staticmethod
    async def ensure_environment_running(
        environment: "AgentEnvironment",
        get_fresh_db_session: "callable",
    ) -> "AgentEnvironment":
        """Activate environment if suspended/stopped; return running env or raise."""
        from app.services.agents.environment_resolver import ensure_environment_running
        return await ensure_environment_running(environment, get_fresh_db_session)
