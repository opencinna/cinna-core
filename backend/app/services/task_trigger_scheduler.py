"""
Task Trigger Scheduler - polls for due schedule and exact-date triggers.

Follows the pattern of file_cleanup_scheduler.py and environment_suspension_scheduler.py.
"""
import asyncio
import logging

from apscheduler.schedulers.background import BackgroundScheduler

from app.services.task_trigger_service import TaskTriggerService

logger = logging.getLogger(__name__)

scheduler = BackgroundScheduler()


def run_trigger_poll():
    """Synchronous wrapper for async poll_due_triggers."""
    try:
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(TaskTriggerService.poll_due_triggers())
        finally:
            loop.close()
    except Exception as e:
        logger.error(f"Trigger poll job failed: {e}", exc_info=True)


def start_scheduler():
    """Start background scheduler (call on app startup)."""
    scheduler.add_job(
        run_trigger_poll,
        "interval",
        minutes=1,
        id="task_trigger_poll",
        max_instances=1,
    )
    scheduler.start()
    logger.info("Task trigger scheduler started (polls every 1 minute)")


def shutdown_scheduler():
    """Stop background scheduler (call on app shutdown)."""
    scheduler.shutdown()
    logger.info("Task trigger scheduler stopped")
