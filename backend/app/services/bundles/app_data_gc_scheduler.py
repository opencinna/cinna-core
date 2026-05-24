"""Garbage collector for orphaned app-data directories on disk.

When a user (or a single install) is deleted, the ``app_data_volume`` rows
disappear via FK cascade, but the on-disk tree under
``APP_DATA_STORAGE_DIR/<user_id>/...`` is left behind — the cascade never
touches the filesystem. Removing those trees inline during account deletion
could block the request for a long time on large workspaces, so instead we
reclaim them out-of-band: this scheduler periodically diffs the directory tree
against the DB (:meth:`AppDataService.purge_orphan_dirs`) and rmtrees any
directory with no remaining DB representation.

Distinct from ``app_data_orphan_scheduler`` — that one *reports* DB rows
flagged ``is_orphaned`` but still present; this one *deletes* on-disk dirs that
have no row at all.

Runs every 6 hours.
"""
import logging

from apscheduler.schedulers.background import BackgroundScheduler

from app.core.db import create_session
from app.services.bundles.app_data_service import AppDataService

logger = logging.getLogger(__name__)

# Frequent enough to keep disk usage bounded, cheap enough to be unnoticeable:
# the scan is a shallow directory diff, not a full tree walk.
GC_INTERVAL_HOURS = 6

scheduler = BackgroundScheduler()


def run_app_data_gc() -> None:
    """Reclaim orphaned app-data directories. Never raises."""
    try:
        with create_session() as session:
            removed, failed = AppDataService.purge_orphan_dirs(session)
        if removed or failed:
            logger.info(
                "AppData GC: removed %d orphaned dir(s), %d failure(s)",
                removed, failed,
            )
        else:
            logger.info("AppData GC: no orphaned dirs found")
    except Exception as e:
        logger.error("AppData GC run failed: %s", e, exc_info=True)


def start_scheduler() -> None:
    """Register the GC job (call on app startup)."""
    scheduler.add_job(
        run_app_data_gc,
        "interval",
        hours=GC_INTERVAL_HOURS,
        id="app_data_gc",
    )
    scheduler.start()
    logger.info(
        "AppData GC scheduler started (runs every %dh)", GC_INTERVAL_HOURS
    )


def shutdown_scheduler() -> None:
    """Stop the scheduler (call on app shutdown)."""
    scheduler.shutdown()
    logger.info("AppData GC scheduler stopped")
