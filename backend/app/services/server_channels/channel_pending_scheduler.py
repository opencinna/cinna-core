"""Scheduler: flush channel bindings whose environment has become ready.

A Pass-2 auto-install parks the caller's first message and returns a binding in
``pending_install`` — the environment still has to build, which takes minutes.
This loop is what eventually delivers that message.

Started from the app lifespan alongside the email schedulers, and gated on
``settings.TESTING`` there per project convention: tests call
``ChannelInboundService.flush_pending_bindings`` directly rather than racing a
background thread.

Like the other schedulers here, APScheduler runs the job on a worker thread, so
the async flush is submitted to the main event loop rather than run on an
ephemeral one — fire-and-forget tasks spawned during ingestion (streaming, title
generation) must outlive the job.

Single-process assumption: no leader election. If this deployment ever runs
multiple backend workers, mirror the model-discovery leader pattern *including*
its documented advisory-lock/connection-pool caveat — duplicated flushes would
otherwise deliver a parked message more than once.
"""
import asyncio
import logging

from apscheduler.schedulers.background import BackgroundScheduler
from sqlmodel import Session

from app.core.db import engine
from app.services.server_channels.channel_inbound_service import ChannelInboundService

logger = logging.getLogger(__name__)

scheduler = BackgroundScheduler()

FLUSH_INTERVAL_SECONDS = 45

_main_loop: asyncio.AbstractEventLoop | None = None


async def _flush() -> None:
    with Session(engine) as session:
        advanced = await ChannelInboundService.flush_pending_bindings(session)
        if advanced:
            logger.info("Channel pending flush: %d binding(s) activated", advanced)


def run_pending_flush() -> None:
    """Submit the flush coroutine to the main application event loop."""
    if _main_loop is None or _main_loop.is_closed():
        logger.error("Main event loop not available — skipping channel flush")
        return
    try:
        future = asyncio.run_coroutine_threadsafe(_flush(), _main_loop)
        # Wait for the flush itself, not the streams it kicks off.
        future.result(timeout=120)
    except Exception as e:
        logger.error(f"Channel pending flush job failed: {e}", exc_info=True)


def start_scheduler() -> None:
    """Start the flush loop (call on app startup)."""
    global _main_loop
    _main_loop = asyncio.get_running_loop()

    scheduler.add_job(
        run_pending_flush,
        "interval",
        seconds=FLUSH_INTERVAL_SECONDS,
        id="channel_pending_flush",
        max_instances=1,
    )
    scheduler.start()
    logger.info(
        f"Channel pending scheduler started (runs every {FLUSH_INTERVAL_SECONDS}s)"
    )


def shutdown_scheduler() -> None:
    """Stop the flush loop (call on app shutdown)."""
    scheduler.shutdown()
    logger.info("Channel pending scheduler stopped")
