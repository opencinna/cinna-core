"""
Agent Schedule Scheduler - polls for due agent schedules and triggers execution.

Follows the pattern of task_trigger_scheduler.py.
"""
import asyncio
import logging
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from sqlmodel import Session as DBSession, select

from app.core.db import engine
from app.models import AgentSchedule, Agent
from app.services.agent_scheduler_service import AgentSchedulerService

logger = logging.getLogger(__name__)

scheduler = BackgroundScheduler()


async def _poll_due_schedules() -> None:
    """Poll for due agent schedules and trigger execution."""
    from app.services.session_service import SessionService

    now = datetime.utcnow()

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

                # Determine the message to send
                message = agent.entrypoint_prompt or "Start scheduled execution."

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
                else:
                    logger.info(
                        f"Schedule {schedule.id}: fired agent {agent.id}, "
                        f"session={session_id}, action={action}"
                    )

                # Update execution times
                AgentSchedulerService.update_execution_time(
                    session=db_session,
                    schedule_id=schedule.id,
                    last_execution=datetime.utcnow(),
                )

            except Exception as e:
                logger.error(
                    f"Error executing schedule {schedule.id}: {e}", exc_info=True
                )


def run_schedule_poll():
    """Synchronous wrapper for async _poll_due_schedules."""
    try:
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(_poll_due_schedules())
        finally:
            loop.close()
    except Exception as e:
        logger.error(f"Agent schedule poll job failed: {e}", exc_info=True)


def start_scheduler():
    """Start background scheduler (call on app startup)."""
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
    scheduler.shutdown()
    logger.info("Agent schedule scheduler stopped")
