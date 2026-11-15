"""Web session cleanup scheduler.

Every 15 minutes, deletes browser sessions (and, by cascade, their refresh
tokens) that expired or were revoked more than a day ago. Same pattern as
``desktop_auth_scheduler``.
"""
import logging

from apscheduler.schedulers.background import BackgroundScheduler
from sqlmodel import Session

from app.core.db import engine

logger = logging.getLogger(__name__)

scheduler = BackgroundScheduler()


def run_cleanup() -> None:
    """Delete ended web sessions."""
    try:
        from app.services.users.web_session_service import WebSessionService

        with Session(engine) as session:
            count = WebSessionService.cleanup_expired(session)
            logger.info("Web session cleanup complete: %d sessions removed", count)
    except Exception as e:
        logger.error("Web session cleanup failed: %s", e)


def start_scheduler() -> None:
    """Start background scheduler (call on app startup)."""
    scheduler.add_job(run_cleanup, "interval", minutes=15, id="web_session_cleanup")
    scheduler.start()
    logger.info("Web session cleanup scheduler started (runs every 15 minutes)")


def shutdown_scheduler() -> None:
    """Stop background scheduler (call on app shutdown)."""
    scheduler.shutdown()
    logger.info("Web session cleanup scheduler stopped")
