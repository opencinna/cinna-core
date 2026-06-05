import logging
import asyncio
from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import text

from app.core.config import settings
from app.core.db import create_session
from app.services.credentials.model_discovery_service import (
    dispatch_model_deprecation_notifications,
    refresh_all_credentials,
)

logger = logging.getLogger(__name__)

scheduler = BackgroundScheduler()

# Stable, arbitrary 64-bit key for the session-level Postgres advisory lock that
# makes the discovery batch single-leader across processes. Under N gunicorn/
# uvicorn workers each runs its own BackgroundScheduler, so without this guard
# every worker would run the batch and emit up to N deprecation emails per
# transition (the 30-min notification dedup TTL is shorter than the daily cron,
# so it gives no cross-run protection). Only the worker that wins the lock runs;
# the rest skip. This also makes the in-memory ``_warned_env_ids`` transition
# tracker authoritative (only the leader mutates it).
_MODEL_DISCOVERY_LOCK_KEY = 0x4D4F44454C4453  # "MODELDS"


def run_model_discovery():
    """Refresh the per-credential available-model cache for all AI credentials.

    Polls each provider's native /models endpoint using the owning user's key
    and caches the result on the credential. Failure-isolated per credential.
    """
    try:
        asyncio.run(_refresh_all_credentials())
    except Exception as e:
        logger.error(f"Model discovery job failed: {e}", exc_info=True)


async def _refresh_all_credentials():
    """Async implementation of the model discovery batch.

    Single-leader across workers via a session-level Postgres advisory lock: if
    another worker already holds it, this run skips. The lock is acquired on the
    same session/connection used for the batch and released in ``finally``
    (``pg_advisory_unlock`` is session-scoped, so it must run on that
    connection; closing the session would also drop it).

    When leader: refreshes the per-credential discovered-model cache, then
    evaluates env model health and emails owners whose environments newly
    transitioned into a deprecated-model warning state.
    """
    with create_session() as session:
        acquired = session.execute(
            text("SELECT pg_try_advisory_lock(:k)"),
            {"k": _MODEL_DISCOVERY_LOCK_KEY},
        ).scalar_one()
        if not acquired:
            logger.info(
                "Model discovery skipped: another worker holds the leader lock"
            )
            return
        try:
            await refresh_all_credentials(session)
            await dispatch_model_deprecation_notifications(session)
        finally:
            session.execute(
                text("SELECT pg_advisory_unlock(:k)"),
                {"k": _MODEL_DISCOVERY_LOCK_KEY},
            )


def start_scheduler():
    """Start background scheduler for per-credential model discovery
    (call on app startup)."""
    if not settings.MODEL_DISCOVERY_ENABLED:
        logger.info("Model discovery scheduler disabled (MODEL_DISCOVERY_ENABLED=False)")
        return

    interval_hours = settings.MODEL_DISCOVERY_INTERVAL_HOURS
    scheduler.add_job(
        run_model_discovery,
        "interval",
        hours=interval_hours,
        id="model_discovery",
        max_instances=1,
        coalesce=True,
        jitter=600,  # spread up to 10 minutes to avoid thundering herd
    )
    scheduler.start()
    logger.info(
        f"Model discovery scheduler started (runs every {interval_hours} hours)"
    )


def shutdown_scheduler():
    """Stop background scheduler (call on app shutdown)."""
    if scheduler.running:
        scheduler.shutdown()
        logger.info("Model discovery scheduler stopped")
