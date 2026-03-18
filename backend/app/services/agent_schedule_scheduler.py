"""
Agent Schedule Scheduler - polls for due agent schedules and triggers execution.

Uses the main application event loop (captured at startup) so that
fire-and-forget tasks spawned by send_session_message (title generation,
process_pending_messages / streaming) survive beyond the poll cycle.
"""
import asyncio
import logging
from datetime import UTC, datetime

from apscheduler.schedulers.background import BackgroundScheduler
from sqlmodel import Session as DBSession, select

from app.core.db import engine
from app.models import AgentSchedule, Agent
from app.services.agent_scheduler_service import AgentSchedulerService

logger = logging.getLogger(__name__)

scheduler = BackgroundScheduler()

# Captured at start_scheduler() time — the uvicorn / FastAPI event loop.
_main_loop: asyncio.AbstractEventLoop | None = None


async def _poll_due_schedules() -> None:
    """Poll for due agent schedules and trigger execution."""
    from app.services.session_service import SessionService

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

                # Determine the message to send (schedule prompt → agent entrypoint → fallback)
                message = schedule.prompt or agent.entrypoint_prompt or "Start scheduled execution."

                # Create session and send message
                result = await SessionService.send_session_message(
                    session_id=None,
                    agent_id=agent.id,
                    user_id=agent.owner_id,
                    content=message,
                    initiate_streaming=True,
                    get_fresh_db_session=lambda: DBSession(engine),
                )

                action = result.get("action")
                session_id = result.get("session_id")

                if action == "error":
                    logger.error(
                        f"Schedule {schedule.id}: failed to execute agent {agent.id}: "
                        f"{result.get('message')}"
                    )
                    continue  # Don't advance schedule on failure

                logger.info(
                    f"Schedule {schedule.id}: fired agent {agent.id}, "
                    f"session={session_id}, action={action}"
                )

                # Update execution times only on success
                AgentSchedulerService.update_execution_time(
                    session=db_session,
                    schedule_id=schedule.id,
                    last_execution=datetime.now(UTC),
                )

            except Exception as e:
                logger.error(
                    f"Error executing schedule {schedule.id}: {e}", exc_info=True
                )


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
