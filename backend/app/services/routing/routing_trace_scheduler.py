"""Scheduler: expire routing decisions past the retention window.

Routing traces are disposable diagnostics, not an audit trail — ``SecurityEvent``
remains the durable record. They are also the only place in the platform that
holds external senders' message text at rest, so the retention window is a
privacy commitment, not just disk hygiene: without this loop,
``ROUTING_TRACE_STORE_MESSAGE_TEXT`` would mean "forever".

``ROUTING_TRACE_RETENTION_FOREVER`` (``-1``) is the only value that switches
expiry off, and the startup line says so in words when it is set — see
``config.py``. Every other value below ``1`` is rejected at settings validation.

Both of those exist for one reason, and it is the rule to apply to anything
added here: **the dangerous state must not be able to look routine.** Unbounded
retention of external senders' message text is the worst configuration this
feature has, so it must not be reachable by typing ``0``, and it must not appear
in startup output as "retention -1 days" among the ordinary numbers. A new
expiry path or a new log line inherits that obligation.

Started from the app lifespan alongside the other schedulers and gated there on
``settings.TESTING`` per project convention — tests call
``RoutingTraceService.purge`` directly rather than racing a background thread.

Single-process assumption: no leader election. A duplicated purge is harmless
(the second one deletes nothing), which is why this does *not* take the
model-discovery advisory lock and its documented connection-pool caveat.
"""
import logging

from apscheduler.schedulers.background import BackgroundScheduler
from sqlmodel import Session

from app.core.config import ROUTING_TRACE_RETENTION_FOREVER, settings
from app.core.db import engine
from app.services.routing.routing_trace_service import RoutingTraceService

logger = logging.getLogger(__name__)

scheduler = BackgroundScheduler()

PURGE_INTERVAL_HOURS = 1


def _retention_description() -> str:
    """The retention window in words, for the startup line.

    ``-1`` is a legitimate but consequential setting — every routing trace,
    message text included, kept indefinitely. Rendering it as "retention -1
    days" would bury that; naming it makes the one unbounded configuration
    visible in the log an operator actually reads.
    """
    days = settings.ROUTING_TRACE_RETENTION_DAYS
    if days == ROUTING_TRACE_RETENTION_FOREVER:
        return "retention DISABLED (keeping routing traces forever)"
    return f"retention {days} days"


def run_purge() -> None:
    """Delete decisions older than ``ROUTING_TRACE_RETENTION_DAYS``."""
    try:
        with Session(engine) as session:
            deleted = RoutingTraceService.purge(session)
            if deleted:
                logger.info(
                    "Routing trace purge: %d decision(s) removed (%s)",
                    deleted,
                    _retention_description(),
                )
    except Exception as e:
        logger.error(f"Routing trace purge job failed: {e}", exc_info=True)


def start_scheduler() -> None:
    """Start the purge loop (call on app startup)."""
    scheduler.add_job(
        run_purge,
        "interval",
        hours=PURGE_INTERVAL_HOURS,
        id="routing_trace_purge",
        max_instances=1,
    )
    scheduler.start()
    logger.info(
        "Routing trace scheduler started (runs every %dh, %s)",
        PURGE_INTERVAL_HOURS,
        _retention_description(),
    )


def shutdown_scheduler() -> None:
    """Stop the purge loop (call on app shutdown)."""
    scheduler.shutdown()
    logger.info("Routing trace scheduler stopped")
