"""Scheduler: repair rows abandoned in a transitional status.

**The failure it exists for.** Long lifecycle operations (activate an
environment, build an image, drain a session) set a transitional status, run
under ``create_task_with_error_logging`` — fire-and-forget, never awaited by the
request — and clear the status on completion. When the backend process goes away
mid-operation (dev ``--reload``, deploy SIGTERM) that task is cancelled with
``asyncio.CancelledError``, which is a ``BaseException``: every ``except
Exception`` fallback that would have written ``status="error"`` is skipped, and
the row keeps its transitional status *forever*. Observed live 2026-09-01, an
environment stuck at ``activating`` / "Starting container..." while its container
was up and healthy.

Nothing else in the platform can see those rows. The existing environment status
and suspension schedulers both filter on ``status == "running"``, so a row stuck
at ``activating`` is invisible to them, and the stuck status in turn blocks the
user's own way out (rebuild is refused while the env is transitional).

**Golden rule: verify, then repair.** A threshold only decides when a row is
worth *looking at*. What the repair writes is decided by live evidence — is the
container actually running, is the environment actually up — because a row that
pattern-matches "stuck" may be a genuinely slow operation. No pass may flip a
status on age alone when the truth is checkable.

**Self-safety.** Every repair is a single-row read → verify → write in its own
transaction, and re-checks the status under the write, so it is idempotent by
construction: a repair that runs twice is a no-op the second time. That is why
no pass needs the stamp-before-work bookkeeping the bundle auto-update sweep
uses.

**Isolation between passes.** A pass that raises is logged and skipped; the
remaining passes still run. One broken domain must never stop the others from
being reconciled.
"""
import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import text
from sqlmodel import Session

from app.core.config import settings
from app.core.db import create_session, engine
from app.services.system.status_repair_channels import repair_channel_deliveries
from app.services.system.status_repair_context import RepairContext
from app.services.system.status_repair_environments import repair_environments
from app.services.system.status_repair_sessions import repair_sessions
from app.services.system.status_repair_tasks import repair_input_tasks

logger = logging.getLogger(__name__)

scheduler = BackgroundScheduler()

# Stable, arbitrary 64-bit key for the Postgres advisory lock that makes the
# repair sweep single-leader. Two workers reconciling the same stuck row would
# both probe its container and both write a terminal status — and Pass A's
# repair emits ``ENVIRONMENT_ACTIVATED``, which drains a session's parked
# messages, so a duplicated repair means duplicated message delivery.
STATUS_REPAIR_LOCK_KEY = 0x53544154524550  # "STATREP"

# How long the APScheduler worker thread blocks waiting for a sweep it handed
# to the main loop. A CONSTANT, deliberately not derived from the tick interval:
# shutdown waits for that worker thread, so a configurable wait would let
# ``STATUS_REPAIR_INTERVAL_MINUTES=60`` turn a deploy into an hour-long hang.
SWEEP_WAIT_TIMEOUT_SECONDS = 120

# The application's main event loop, captured at startup by
# ``start_scheduler``. See ``run_status_repair`` for why the sweep has to run
# there rather than on the APScheduler worker thread.
_main_loop: asyncio.AbstractEventLoop | None = None


@contextmanager
def repair_leader_session() -> Iterator[Session | None]:
    """Yield a session holding the repair sweep's leader lock, or ``None``.

    ``None`` means another process already holds the lock and this tick should
    skip.

    Copied from ``install_service.sweep_leader_session``, including the reason
    it looks like this: ``pg_try_advisory_lock`` is *connection*-scoped, while a
    ``Session`` bound to an **engine** returns its connection to the pool at
    every ``commit()``. The sweep commits once per repaired row, so an
    engine-bound session would strand the lock on a pooled connection — the
    matching ``pg_advisory_unlock`` then runs on a different connection and
    returns false, the lock is never released, and every later run is locked out
    permanently. Binding the ``Session`` to an explicit ``engine.connect()``
    pins it for the lock's whole life.

    Do **not** re-derive this from ``model_discovery_scheduler``: that one takes
    the lock on a pooled session and leaks it exactly as described above (a live
    bug, already cited as "the pattern" by two other schedulers — this is not
    the fourth).
    """
    if settings.TESTING:
        # Under test there is no cross-process concurrency to guard against, and
        # the harness patches ``create_session`` to hand back the rolled-back
        # test transaction. Checking out a real pooled connection here would
        # escape that isolation and write to the live database.
        with create_session() as session:
            yield session
        return

    with engine.connect() as connection:
        acquired = connection.execute(
            text("SELECT pg_try_advisory_lock(:k)"),
            {"k": STATUS_REPAIR_LOCK_KEY},
        ).scalar_one()
        connection.commit()
        if not acquired:
            yield None
            return
        try:
            with Session(bind=connection) as session:
                yield session
        finally:
            connection.execute(
                text("SELECT pg_advisory_unlock(:k)"),
                {"k": STATUS_REPAIR_LOCK_KEY},
            )
            connection.commit()


# ── Repair passes ──────────────────────────────────────────────────────
#
# A pass takes the tick's ``RepairContext`` — the leader session plus what the
# tick has already done — and returns the number of rows it repaired. Each one
# is an ordinary coroutine function so a test can await it directly against a
# context built over the test session (``RepairContext(session=db)``); the
# ``TESTING`` gate means the scheduler itself never runs under pytest, so the
# pass functions are the only test surface.
#
# Register them HERE, as an explicit literal, by importing each pass function
# into this module — never by having the pass module append to this list on
# import. Ordering that depends on import order is ordering nobody can read, and
# a pass whose module simply never gets imported would leave the scheduler
# ticking silently forever (``start_scheduler`` warns once when the list is
# empty, which is the only guard against that).
#
# Tenants (see docs/plans/system_status_repair_plan.md):
#   A  environments        — transitional env status, probe container, repair
#   B  sessions            — stale "running" / "pending_stream" interaction status
#   C  input tasks         — re-derive "in_progress" from repaired sessions
#   D  channel deliveries  — unsealed "draft" turn-delivery rows
#
# **Ordering is load-bearing, but only C actually consumes a committed result.**
# The other two dependencies are the opposite of a hand-off: they exist so a
# later pass can be told what an earlier one *started* and has not finished, and
# they run through ``RepairContext`` rather than through the database, because
# the database does not yet show it. See that module for the two races.
RepairPass = Callable[[RepairContext], Awaitable[int]]

REPAIR_PASSES: list[tuple[str, RepairPass]] = [
    # A first, and NOT because its repairs are visible to B by the time B runs —
    # they are not. Repairing an environment to "running" emits
    # ENVIRONMENT_ACTIVATED, whose handler runs as a detached task and reaches
    # the session row only several hops later, so B would still see the parked
    # claim and re-drain (or clear) a session that is already moving. A runs
    # first so it can record those environments in the context and B can skip
    # their sessions outright.
    ("environments", repair_environments),
    # B before C is the one true hand-off in this list: the input-task pass
    # re-derives task status from the session rows B has just *committed*, so
    # running it first would faithfully derive from the stale rows and conclude
    # nothing had changed.
    ("sessions", repair_sessions),
    ("input_tasks", repair_input_tasks),
    # D after B, for evidence rather than for output — and the evidence runs
    # through the context, not the column. D reads
    # ``interaction_status != "running"`` as proof that a channel turn is over,
    # and B writes exactly that value for a long stream it clears *without
    # cancelling it*. So D must know which sessions B touched this tick and
    # leave them alone; otherwise B manufactures the evidence D trusts.
    ("channel_deliveries", repair_channel_deliveries),
]


async def run_repair_passes(ctx: RepairContext) -> dict[str, int]:
    """Run every registered pass once, returning per-pass repair counts.

    A pass that raises is logged and skipped — a broken domain must not stop the
    others from being reconciled. What the failed pass had already recorded in
    ``ctx`` stays there on purpose: those notes describe work that really was
    started, and a later pass must still steer clear of it.
    """
    results: dict[str, int] = {}
    for name, repair_pass in REPAIR_PASSES:
        try:
            results[name] = await repair_pass(ctx)
        except Exception as exc:
            # Roll back before the next pass. All passes share one session on
            # one pinned connection, and a Postgres error aborts the whole
            # transaction — without this, the first pass to hit a DB error makes
            # every later pass fail with "current transaction is aborted",
            # inverting the isolation this loop exists to provide.
            ctx.session.rollback()
            logger.error(
                "Status repair pass %r failed: %s", name, exc, exc_info=True
            )
    return results


async def _run_repair_tick() -> None:
    """One full sweep: take the leader lock, run the passes, log what changed."""
    with repair_leader_session() as session:
        if session is None:
            logger.debug("Status repair: another worker holds the leader lock")
            return
        if not REPAIR_PASSES:
            logger.debug("Status repair: no repair passes registered")
            return
        # A fresh context per tick: what one sweep started is only ever a reason
        # for *that* sweep's later passes to stand back. Carrying it across ticks
        # would turn a one-tick "leave this alone" into a permanent one.
        results = await run_repair_passes(RepairContext(session=session))

    repaired = sum(results.values())
    if repaired:
        logger.info(
            "Status repair: %d row(s) reconciled (%s)",
            repaired,
            ", ".join(f"{name}={count}" for name, count in results.items() if count),
        )


def run_status_repair() -> None:
    """APScheduler entry point — submit one sweep to the main event loop.

    APScheduler runs jobs on a worker thread, but the sweep must execute on the
    application's main loop: repairing an environment emits
    ``ENVIRONMENT_ACTIVATED``, and ``event_service`` dispatches handlers as
    ``asyncio`` tasks on the *currently running* loop. Under ``asyncio.run()``
    those tasks would be created on a throwaway loop that closes the moment the
    sweep returns, killing the session drain the repair exists to trigger. Same
    idiom, same reason, as ``channel_pending_scheduler``.
    """
    if _main_loop is None or _main_loop.is_closed():
        logger.error("Main event loop not available — skipping status repair")
        return
    try:
        future = asyncio.run_coroutine_threadsafe(_run_repair_tick(), _main_loop)
        # Wait for the sweep itself, not for the work it hands off (a drained
        # session's stream must outlive this tick).
        #
        # ``max_instances=1`` does NOT cover the timeout case: once this
        # function returns, APScheduler considers the job finished and will
        # start the next tick while the timed-out sweep still runs on the main
        # loop. What actually prevents two overlapping sweeps from repairing the
        # same row is the advisory lock — the second sweep asks for it on a
        # different connection and is refused. The warning below is the signal
        # that this is happening.
        future.result(timeout=SWEEP_WAIT_TIMEOUT_SECONDS)
    except TimeoutError:
        logger.warning(
            "Status repair sweep still running after %ds; the next tick will "
            "be refused the leader lock until it finishes",
            SWEEP_WAIT_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        logger.error(f"Status repair job failed: {exc}", exc_info=True)


def start_scheduler() -> None:
    """Start the repair loop (call on app startup)."""
    global _main_loop

    if not settings.STATUS_REPAIR_ENABLED:
        logger.info("Status repair scheduler disabled (STATUS_REPAIR_ENABLED=False)")
        return

    _main_loop = asyncio.get_running_loop()

    scheduler.add_job(
        run_status_repair,
        "interval",
        minutes=settings.STATUS_REPAIR_INTERVAL_MINUTES,
        id="system_status_repair",
        max_instances=1,
    )
    scheduler.start()
    logger.info(
        "Status repair scheduler started (runs every %d minute(s))",
        settings.STATUS_REPAIR_INTERVAL_MINUTES,
    )
    if not REPAIR_PASSES:
        # Said once, at startup, where an operator reads it — a scheduler that
        # ticks forever and repairs nothing must not be able to look healthy.
        # Deliberately not per-tick: a warning every two minutes is how warnings
        # stop being read.
        logger.warning(
            "Status repair scheduler has NO repair passes registered — it will "
            "tick and do nothing"
        )


def shutdown_scheduler() -> None:
    """Stop the repair loop (call on app shutdown)."""
    if not scheduler.running:
        return
    # ``wait=False``: a worker thread parked in ``future.result`` can only be
    # released by its coroutine finishing on the main loop — the same loop this
    # runs on — so waiting for it would deadlock until the timeout above.
    # In-flight repairs are safe to abandon: each commits per row and re-checks
    # the status under the write, so the next process re-runs them idempotently.
    scheduler.shutdown(wait=False)
    logger.info("Status repair scheduler stopped")
