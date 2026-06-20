"""
Device-Login Cleanup Scheduler.

Periodically hard-deletes expired ``cli_device_login_request`` rows. Lazy-on-read
expiry keeps the flow correct without this; this is housekeeping only. Follows
the same pattern as ``cli_setup_token_scheduler.py`` / ``desktop_auth_scheduler.py``.
"""
import logging

from apscheduler.schedulers.background import BackgroundScheduler
from sqlmodel import Session

from app.core.db import engine

logger = logging.getLogger(__name__)

scheduler = BackgroundScheduler()


def run_cleanup() -> None:
    """Run device-login request cleanup."""
    try:
        from app.services.cli.device_login_service import DeviceLoginService

        with Session(engine) as session:
            count = DeviceLoginService.cleanup_expired(session)
            logger.info("Device-login cleanup complete: %d requests removed", count)
    except Exception as e:
        logger.error("Device-login cleanup failed: %s", e)


def start_scheduler() -> None:
    """Start background scheduler (call on app startup)."""
    scheduler.add_job(run_cleanup, "interval", minutes=15, id="device_login_cleanup")
    scheduler.start()
    logger.info("Device-login cleanup scheduler started (runs every 15 minutes)")


def shutdown_scheduler() -> None:
    """Stop background scheduler (call on app shutdown)."""
    scheduler.shutdown()
    logger.info("Device-login cleanup scheduler stopped")
