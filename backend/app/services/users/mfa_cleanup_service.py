"""MFA Cleanup Scheduler.

Periodically removes stale :class:`UserMfaChallenge` rows.  Mirrors the
pattern used by :mod:`app.services.desktop_auth.desktop_auth_scheduler`
and :mod:`app.services.cli.cli_setup_token_scheduler` — a
``BackgroundScheduler`` with a single ``interval`` job.

Idempotent — a failure on one run is harmless because the next run will
pick up any rows the previous one missed (the DELETE filter is purely
time-based).  No retry / recovery needed.
"""
import logging
from datetime import datetime, timedelta, UTC

from apscheduler.schedulers.background import BackgroundScheduler
from sqlmodel import Session, delete

from app.core.db import engine
from app.models import UserMfaChallenge

logger = logging.getLogger(__name__)

scheduler = BackgroundScheduler()

# Rows older than this are unambiguously dead — even consumed challenges
# kept for a short audit window get pruned eventually.  Aggressive enough
# to keep the table small in production.
_RETENTION = timedelta(hours=24)


def run_cleanup() -> None:
    """Delete every :class:`UserMfaChallenge` older than 24h."""
    cutoff = datetime.now(UTC) - _RETENTION
    try:
        with Session(engine) as session:
            result = session.exec(
                delete(UserMfaChallenge).where(
                    UserMfaChallenge.created_at < cutoff
                )
            )
            session.commit()
            count = getattr(result, "rowcount", None)
            logger.info(
                "MFA challenge cleanup complete: %s rows removed (cutoff=%s)",
                count if count is not None else "?",
                cutoff.isoformat(),
            )
    except Exception as exc:  # noqa: BLE001 — log and swallow, will retry next run
        logger.error("MFA challenge cleanup failed: %s", exc)


def start_scheduler() -> None:
    """Start the background scheduler (call on app startup)."""
    scheduler.add_job(run_cleanup, "interval", hours=1, id="mfa_challenge_cleanup")
    scheduler.start()
    logger.info("MFA challenge cleanup scheduler started (runs every hour)")


def shutdown_scheduler() -> None:
    """Stop the background scheduler (call on app shutdown)."""
    scheduler.shutdown()
    logger.info("MFA challenge cleanup scheduler stopped")
