"""Per-target endpoint run budget and page state, in ``dashboard_tile_runtime``.

A tile may run its endpoints at most 30 times a minute. The budget is shared
by everything that changes what the tile computes: the page's ``cinna.call``,
its ``setParams``, the owner's filter changes and the agent's
``dashboards_update_tile`` params. Opening, re-showing, refreshing and a
highlight alone are not counted. Callers decide what counts; this module only
keeps the count.

The count lives in Postgres and is read and updated under
``SELECT … FOR UPDATE``, so it holds across workers. Targets are
``b:<block_id>`` for a dashboard block and
``p:<user>:<agent>:<skill|->:<component>:<tile_key|preview>`` for an
agent-scoped run.

The same row carries the owner's open page's highlight readiness per
document (``page_doc``, ``highlight_ready``), so a tool call on any worker can
tell whether a highlight will be drawn.
"""
import logging
import math
import time
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert
from sqlmodel import Session, select

from app.models.dashboards.dashboard_component import DashboardTileRuntime

logger = logging.getLogger(__name__)

WINDOW_S = 60.0
MAX_RUNS_PER_WINDOW = 30
IDLE_PRUNE_AFTER = timedelta(days=1)
PRUNE_EVERY_S = 3600.0
PREVIEW_TILE_KEY = "preview"
# Highlight readiness older than this counts as not ready.
PAGE_STATE_TTL = timedelta(minutes=15)

# Per worker: when idle rows were last pruned (monotonic seconds).
_last_prune_at: float | None = None


def block_target(block_id: uuid.UUID) -> str:
    return f"b:{block_id}"


def preview_target(
    user_id: uuid.UUID,
    agent_id: uuid.UUID,
    skill: str | None,
    component: str,
    tile_key: str | None,
) -> str:
    return f"p:{user_id}:{agent_id}:{skill or '-'}:{component}:{tile_key or PREVIEW_TILE_KEY}"


def _lock_row(db: Session, target_key: str) -> DashboardTileRuntime:
    """Upsert the target's row and lock it.

    ``INSERT … ON CONFLICT DO UPDATE`` takes the row lock atomically (a new
    row or the existing one), so another worker's idle prune cannot delete it
    before the read.
    """
    now = datetime.now(UTC)
    db.exec(  # type: ignore[call-overload]
        insert(DashboardTileRuntime)
        .values(target_key=target_key, run_times=[], highlight_ready=False, updated_at=now)
        .on_conflict_do_update(
            index_elements=["target_key"],
            set_={"updated_at": insert(DashboardTileRuntime).excluded.updated_at},
        )
    )
    # The upsert holds the row lock until commit, so this read cannot miss it.
    return db.exec(
        select(DashboardTileRuntime)
        .where(DashboardTileRuntime.target_key == target_key)
        .execution_options(populate_existing=True)
    ).one()


def _maybe_prune_idle(db: Session) -> None:
    """Delete rows idle for more than a day, at most once an hour per worker."""
    global _last_prune_at
    now = time.monotonic()
    if _last_prune_at is not None and now - _last_prune_at < PRUNE_EVERY_S:
        return
    _last_prune_at = now
    cutoff = datetime.now(UTC) - IDLE_PRUNE_AFTER
    db.exec(  # type: ignore[call-overload]
        delete(DashboardTileRuntime).where(DashboardTileRuntime.updated_at < cutoff)
    )


def check_and_record(db: Session, target_key: str) -> int | None:
    """Count one run against ``target_key``.

    Returns None when the run is allowed (and recorded), or the seconds until
    the budget allows another run when it is not (nothing is recorded).
    Commits the session.
    """
    _maybe_prune_idle(db)
    row = _lock_row(db, target_key)
    now = time.time()
    recent = [t for t in (row.run_times or []) if now - float(t) < WINDOW_S]
    retry_after: int | None = None
    if len(recent) >= MAX_RUNS_PER_WINDOW:
        oldest = min(recent)
        retry_after = max(1, math.ceil(oldest + WINDOW_S - now))
    else:
        recent.append(now)
    row.run_times = recent[-MAX_RUNS_PER_WINDOW:]
    row.updated_at = datetime.now(UTC)
    db.add(row)
    db.commit()
    return retry_after


def set_page_state(
    db: Session,
    target_key: str,
    page_doc: str | None,
    highlight_ready: bool,
    seq: int | None = None,
    if_page_doc: str | None = None,
) -> bool:
    """Record what the owner's open page reported for its current document. Commits.

    A write whose ``seq`` is lower than the stored one arrived late and is
    ignored (returns False). A write without ``seq`` always applies. With
    ``if_page_doc`` the write applies only while the stored ``page_doc`` is
    that one (otherwise ignored, ``page_seq`` untouched).
    """
    _maybe_prune_idle(db)
    row = _lock_row(db, target_key)
    stale = seq is not None and row.page_seq is not None and seq < row.page_seq
    if stale or (if_page_doc is not None and row.page_doc != if_page_doc):
        db.commit()  # release the row lock
        return False
    now = datetime.now(UTC)
    row.page_doc = page_doc
    row.highlight_ready = highlight_ready
    if seq is not None:
        row.page_seq = seq
    row.page_state_at = now
    row.updated_at = now
    db.add(row)
    db.commit()
    return True


def get_page_state(db: Session, target_key: str) -> tuple[str | None, bool]:
    """``(page_doc, highlight_ready)``; ``(None, False)`` when the page never reported
    or its last report is older than ``PAGE_STATE_TTL`` (the page went away)."""
    row = db.get(DashboardTileRuntime, target_key)
    if row is None:
        return None, False
    reported_at = row.page_state_at
    if reported_at is not None and reported_at.tzinfo is None:
        reported_at = reported_at.replace(tzinfo=UTC)
    if reported_at is None or datetime.now(UTC) - reported_at > PAGE_STATE_TTL:
        return None, False
    return row.page_doc, row.highlight_ready
