"""Scheduler: pull inbound messages from every enabled polled channel.

The timer half of ``ChannelPollService``. Nothing but an interval and a
session — all the work, and everything worth testing, is in the service.

Started from the app lifespan and gated on ``settings.TESTING`` there, per
project convention. That gate is load-bearing rather than tidy: a poller
running during a test would open real IMAP connections from an arbitrary
worker thread and push messages into the pipeline underneath unrelated
suites, which shows up as flakiness in domains that never mentioned email.
Tests call ``ChannelPollService.poll_enabled_channels`` directly.

Like the other schedulers here, APScheduler runs the job on a worker thread,
so the async poll is submitted to the main event loop rather than run on an
ephemeral one — the fire-and-forget tasks the inbound pipeline spawns
(routing, ingestion, streaming) must outlive the job that started them.

Single-process assumption: no leader election. Two backend processes would
both poll, and the IMAP ``\\Seen`` flag plus the pipeline's dedup on
``external_message_id`` is what keeps that from double-answering — a race, not
a guarantee. This is a documented limitation, the same one
``channel_pending_scheduler`` carries. **Do not** fix it by copying the
advisory-lock leader pattern from ``model_discovery_scheduler``: it leaks
connections on a pooled connection (master plan §7).
"""
import asyncio
import logging

from apscheduler.schedulers.background import BackgroundScheduler
from sqlmodel import Session

from app.core.db import engine
from app.services.server_channels.channel_poll_service import ChannelPollService

logger = logging.getLogger(__name__)

scheduler = BackgroundScheduler()

#: How long a person waits, worst case, before their message is even seen.
#: Short enough to feel like a chat surface; long enough that a mailbox is not
#: hammered.
POLL_INTERVAL_SECONDS = 60

_main_loop: asyncio.AbstractEventLoop | None = None


async def _poll() -> None:
    with Session(engine) as session:
        processed = await ChannelPollService.poll_enabled_channels(session)
        if processed:
            logger.info("Channel poll: %d inbound message(s) processed", processed)


def run_channel_poll() -> None:
    """Submit the poll coroutine to the main application event loop."""
    if _main_loop is None or _main_loop.is_closed():
        logger.error("Main event loop not available — skipping channel poll")
        return
    try:
        future = asyncio.run_coroutine_threadsafe(_poll(), _main_loop)
        # Wait for the fetch and the pipeline hand-off, not for the agent runs
        # those hand-offs start.
        future.result(timeout=300)
    except Exception as e:
        logger.error(f"Channel poll job failed: {e}", exc_info=True)


def start_scheduler() -> None:
    """Start the poll loop (call on app startup)."""
    global _main_loop
    _main_loop = asyncio.get_running_loop()

    scheduler.add_job(
        run_channel_poll,
        "interval",
        seconds=POLL_INTERVAL_SECONDS,
        id="channel_poll",
        max_instances=1,
    )
    scheduler.start()
    logger.info(
        f"Channel poll scheduler started (runs every {POLL_INTERVAL_SECONDS}s)"
    )


def shutdown_scheduler() -> None:
    """Stop the poll loop (call on app shutdown)."""
    scheduler.shutdown()
    logger.info("Channel poll scheduler stopped")
