"""Scheduler: converge pending per-user key mints.

Adding a member to a minted credential records an intent and returns; this is
what turns that intent into a key. It exists as a sweep rather than as work on
the request path for two reasons that are really the same one — the provider is
somebody else's service. A mint on the signup path would make a provider timeout
into a failed login, and a mint on the admin's PATCH would make an admin's page
as slow as the slowest provider call in the batch.

The leader lock comes from :func:`app.core.db.leader_session`. It is not
re-derived here, and deliberately: a session bound to the *engine* returns its
connection to the pool at every ``commit()``, and this sweep commits per member,
so an inline copy would strand the advisory lock on a pooled connection and lock
every later tick out permanently. Three schedulers had that code, one of them had
that bug, and the helper is now the only implementation.

``converge`` takes a session and is awaited directly by the tests. The
``TESTING`` gate in ``app/main.py`` means this scheduler never runs under pytest,
so the pass function is the only test surface — which is why it takes a session
rather than opening one.
"""
import asyncio
import logging

from apscheduler.schedulers.background import BackgroundScheduler

from app.core.db import leader_session
from app.services.credentials.key_provisioning_service import (
    key_provisioning_service,
)

logger = logging.getLogger(__name__)

scheduler = BackgroundScheduler()

# Stable, arbitrary 64-bit key for this sweep's advisory lock. Its own key, not
# shared: two workers minting for the same membership would create two provider
# keys and remember one.
KEY_PROVISIONING_LOCK_KEY = 0x4B45594D494E54  # "KEYMINT"

# How long the APScheduler worker thread waits for a sweep it handed to the main
# loop. A constant, not derived from the tick interval: shutdown waits on this
# thread, so a configurable wait would let a long interval turn a deploy into a
# long hang.
SWEEP_WAIT_TIMEOUT_SECONDS = 180

_main_loop: asyncio.AbstractEventLoop | None = None


async def _run_tick() -> None:
    """One sweep: take the leader lock, converge what is due, log what changed."""
    with leader_session(KEY_PROVISIONING_LOCK_KEY) as session:
        if session is None:
            logger.debug("Key provisioning: another worker holds the leader lock")
            return
        report = await key_provisioning_service.converge(session)

    if report.attempted:
        logger.info(
            "Key provisioning: %d attempted, %d provisioned, %d failed, "
            "%d deferred",
            report.attempted,
            report.provisioned,
            report.failed,
            report.deferred,
        )


def run_key_provisioning() -> None:
    """APScheduler entry point — submit one sweep to the main event loop.

    Same idiom, same reason, as the status-repair sweep: APScheduler runs jobs on
    a worker thread, and creating a child credential emits events whose handlers
    are ``asyncio`` tasks on the *currently running* loop. Under ``asyncio.run()``
    those would land on a throwaway loop that closes the moment the sweep returns.
    """
    if _main_loop is None or _main_loop.is_closed():
        logger.error("Main event loop not available — skipping key provisioning")
        return
    try:
        future = asyncio.run_coroutine_threadsafe(_run_tick(), _main_loop)
        future.result(timeout=SWEEP_WAIT_TIMEOUT_SECONDS)
    except TimeoutError:
        logger.warning(
            "Key provisioning sweep still running after %ds; the next tick will "
            "be refused the leader lock until it finishes",
            SWEEP_WAIT_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        logger.error(f"Key provisioning job failed: {exc}", exc_info=True)


def start_scheduler() -> None:
    """Start the converge loop (call on app startup)."""
    global _main_loop

    _main_loop = asyncio.get_running_loop()

    scheduler.add_job(
        run_key_provisioning,
        "interval",
        minutes=1,
        id="key_provisioning_converge",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    logger.info("Key provisioning scheduler started (every 1 minute)")


def shutdown_scheduler() -> None:
    """Stop the converge loop (call on app shutdown)."""
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("Key provisioning scheduler stopped")
