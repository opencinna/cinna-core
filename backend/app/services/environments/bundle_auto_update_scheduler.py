"""Periodic convergence sweep for automatic-mode bundle installs.

The suspension scheduler applies pending bundle updates as a *running →
suspended* transition hook, which only covers installs whose environment was
live when the revision was published. An install whose environment was already
suspended (or stopped, or absent) at publish time is never revisited by that
hook, so "automatic" silently means "never" for owners who are not actively
using the install.

This scheduler closes that gap: every ``BUNDLE_AUTO_UPDATE_INTERVAL_MINUTES``
it runs :meth:`InstallService.sweep_automatic_updates` fleet-wide, which selects
on revision mismatch (not ``pending_update``, so it is self-healing) and only
touches installs whose environment is idle.

The suspension-time hook stays exactly as-is — it is still the best moment to
update a running environment, since it avoids an extra stop/start cycle.
"""
import asyncio
import logging

from apscheduler.schedulers.background import BackgroundScheduler

from app.core.config import settings
from app.services.bundles.install_service import (
    InstallService,
    sweep_leader_session,
)

logger = logging.getLogger(__name__)

scheduler = BackgroundScheduler()


def run_bundle_auto_update():
    """Converge automatic-mode installs onto their bundle's latest revision."""
    try:
        asyncio.run(_sweep_automatic_updates())
    except Exception as e:
        logger.error(f"Bundle auto-update job failed: {e}", exc_info=True)


async def _sweep_automatic_updates():
    """Async implementation of the auto-update sweep.

    Single-leader across processes via a Postgres advisory lock held by
    ``sweep_leader_session`` — under N gunicorn/uvicorn workers each runs its
    own BackgroundScheduler, and without the guard every worker would run the
    same batch concurrently and race on the same installs (two workers copying
    the same bundle snapshot into the same env directory). Only the worker that
    wins the lock runs; the rest skip. The publish-time fast path takes the same
    lock, so the two entry points are mutually exclusive too.
    """
    with sweep_leader_session() as session:
        if session is None:
            logger.info(
                "Bundle auto-update sweep skipped: another worker holds the "
                "leader lock"
            )
            return
        await InstallService.sweep_automatic_updates(
            session,
            limit=settings.BUNDLE_AUTO_UPDATE_BATCH_LIMIT,
        )


def start_scheduler():
    """Start background scheduler for bundle auto-update convergence
    (call on app startup)."""
    if not settings.BUNDLE_AUTO_UPDATE_ENABLED:
        logger.info(
            "Bundle auto-update scheduler disabled "
            "(BUNDLE_AUTO_UPDATE_ENABLED=False)"
        )
        return

    interval_minutes = settings.BUNDLE_AUTO_UPDATE_INTERVAL_MINUTES
    scheduler.add_job(
        run_bundle_auto_update,
        "interval",
        minutes=interval_minutes,
        id="bundle_auto_update",
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    logger.info(
        f"Bundle auto-update scheduler started (runs every {interval_minutes} minutes)"
    )


def shutdown_scheduler():
    """Stop background scheduler (call on app shutdown)."""
    if scheduler.running:
        scheduler.shutdown()
        logger.info("Bundle auto-update scheduler stopped")
