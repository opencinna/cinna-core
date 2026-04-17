"""Desktop Auth Cleanup Scheduler.

Periodically removes expired authorization codes and old revoked/expired
refresh tokens.  Follows the same pattern as cli_setup_token_scheduler.py.
"""
import logging

from apscheduler.schedulers.background import BackgroundScheduler
from sqlmodel import Session

from app.core.db import engine

logger = logging.getLogger(__name__)

scheduler = BackgroundScheduler()


def run_cleanup() -> None:
    """Run desktop auth cleanup for expired codes and refresh tokens."""
    try:
        from app.services.desktop_auth.desktop_auth_service import DesktopAuthService

        with Session(engine) as session:
            count = DesktopAuthService.cleanup_expired(session)
            logger.info("Desktop auth cleanup complete: %d records removed", count)
    except Exception as e:
        logger.error("Desktop auth cleanup failed: %s", e)


def start_scheduler() -> None:
    """Start background scheduler (call on app startup)."""
    scheduler.add_job(run_cleanup, "interval", minutes=15, id="desktop_auth_cleanup")
    scheduler.start()
    logger.info("Desktop auth cleanup scheduler started (runs every 15 minutes)")


def shutdown_scheduler() -> None:
    """Stop background scheduler (call on app shutdown)."""
    scheduler.shutdown()
    logger.info("Desktop auth cleanup scheduler stopped")
