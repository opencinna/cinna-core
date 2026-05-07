"""Daily orphan-volume reporter for ``AppDataVolume`` rows.

Phase 1 deliberately does NOT auto-delete orphaned volumes — deletion is
user-driven via Settings → App Data. This scheduler exists only to surface
long-orphaned volumes in logs (and, downstream, in monitoring) so operators
can prompt users to clean up.

Runs once per day. Threshold is 90 days, matching the plan.
"""
import logging

from apscheduler.schedulers.background import BackgroundScheduler

from app.core.db import create_session
from app.services.bundles.app_data_service import AppDataService

logger = logging.getLogger(__name__)

# 90 days mirrors the plan; tweakable via env config in a future iteration.
ORPHAN_REPORT_AGE_DAYS = 90

scheduler = BackgroundScheduler()


def run_orphan_report() -> None:
    """Walk orphaned volumes older than the threshold and log the result."""
    try:
        with create_session() as session:
            stale = AppDataService.find_orphans_older_than(
                session, days=ORPHAN_REPORT_AGE_DAYS
            )
            if not stale:
                logger.info(
                    "AppData orphan report: no volumes orphaned > %d days",
                    ORPHAN_REPORT_AGE_DAYS,
                )
                return

            total_bytes = sum(v.size_bytes for v in stale)
            logger.warning(
                "AppData orphan report: %d volume(s) orphaned > %d days "
                "(total %d bytes)",
                len(stale), ORPHAN_REPORT_AGE_DAYS, total_bytes,
            )
            for v in stale:
                logger.warning(
                    "  orphan id=%s user=%s bundle=%s size=%d updated=%s",
                    v.id, v.user_id, v.bundle_id, v.size_bytes, v.updated_at,
                )
    except Exception as e:
        logger.error("AppData orphan report failed: %s", e, exc_info=True)


def start_scheduler() -> None:
    """Register the daily report job (call on app startup)."""
    scheduler.add_job(
        run_orphan_report,
        "interval",
        days=1,
        id="app_data_orphan_report",
    )
    scheduler.start()
    logger.info("AppData orphan-report scheduler started (runs every 24h)")


def shutdown_scheduler() -> None:
    """Stop the scheduler (call on app shutdown)."""
    scheduler.shutdown()
    logger.info("AppData orphan-report scheduler stopped")
