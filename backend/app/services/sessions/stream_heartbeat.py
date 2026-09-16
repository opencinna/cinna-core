"""
Per-turn DB heartbeat for live agent streams (plan D10).

While a turn streams, ``StreamHeartbeat`` writes ``stream_heartbeat_at`` (UTC
ISO) roughly every ``HEARTBEAT_INTERVAL_SECONDS``:

* into ``session.session_metadata`` from turn start (no agent row may exist
  yet), and
* into the in-progress agent row's ``message_metadata`` once it exists.

The status-repair orphan pass (``services/system/status_repair_sessions.py``)
treats a row or session as orphaned only when this stamp is stale. The stamp is
DB state, so the verdict holds across backend workers; in-process memory (the
stream registry, the session lock) only covers the worker that reads it.

It is a separate task because the 2 s event flush only runs when events
arrive, and a long tool call emits none.

Write discipline (both columns are JSON rewritten whole by other writers):
every beat locks the row (``with_for_update`` + ``populate_existing``),
re-reads it, sets only its own key and commits. A stop flag is checked
**under the row lock**, and ``stop()`` sets it before the caller's final write
or seal, so a beat that is still in its thread can never land after the seal.

Self-heal: a beat that finds its agent row ``aborted`` (the orphan pass
misjudged a stalled heartbeat) reopens it. A beat that finds its session
cleared by the orphan pass for *this* turn (``ORPHAN_CLEARED_KEY`` not older
than the turn's first beat) restores ``running``.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy.orm.attributes import flag_modified

from app.core.config import settings
from app.models.sessions.session import Session as ChatSession, SessionMessage

logger = logging.getLogger(__name__)

STREAM_HEARTBEAT_KEY = "stream_heartbeat_at"
# Written into ``session_metadata`` by the orphan pass when it clears a session
# whose heartbeat was stale: ``{"heartbeat": <judged stamp>,
# "streaming_started_at": <the cleared claim's start>}``.
ORPHAN_CLEARED_KEY = "stream_orphan_cleared_heartbeat"

# Beat period. The orphan pass waits
# ``STATUS_REPAIR_ORPHAN_STREAM_MIN_AGE_MINUTES`` (2 min = 4 missed beats).
# Read at ``start()`` so tests can patch it.
HEARTBEAT_INTERVAL_SECONDS = 30.0

# Off under pytest: the API test harness hands every caller ONE shared DB
# session (``tests/utils/fixtures.py``), which a concurrent heartbeat thread
# would race. Only the ticker is gated; the inline stamps written at agent-row
# creation and on each flush still run. ``tests/unit/test_stream_heartbeat.py``
# covers the ticker with this patched to ``True``. ``None`` = decide at
# ``start()`` from ``settings.TESTING``.
HEARTBEAT_ENABLED: bool | None = None

_LIVE_HEARTBEATS: set[asyncio.Task] = set()


def heartbeat_now() -> str:
    return datetime.now(UTC).isoformat()


def build_orphan_cleared_marker(
    heartbeat: Any, streaming_started_at: datetime | None,
) -> dict[str, Any]:
    return {
        "heartbeat": heartbeat,
        "streaming_started_at": (
            streaming_started_at.isoformat() if streaming_started_at else None
        ),
    }


def _parse_orphan_cleared_marker(value: Any) -> tuple[datetime | None, datetime | None]:
    """``(judged heartbeat, original streaming_started_at)``; tolerant of junk."""
    if not isinstance(value, dict):
        return None, None
    return (
        parse_heartbeat(value.get("heartbeat")),
        parse_heartbeat(value.get("streaming_started_at")),
    )


def parse_heartbeat(value: Any) -> datetime | None:
    """Parse a stored heartbeat; ``None`` when missing or malformed."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


class StreamHeartbeat:
    """Heartbeat ticker for one turn. Never raises into the turn."""

    def __init__(
        self,
        session_id: UUID,
        get_fresh_db_session: Callable[[], Any],
    ) -> None:
        self._session_id = session_id
        self._get_db = get_fresh_db_session
        self._message_id: UUID | None = None
        self._stopped = threading.Event()
        self._wake = asyncio.Event()
        self._started_at: datetime | None = None
        # The last session-side stamp this ticker committed. The turn-end
        # clear deletes the key only while it still holds this value.
        self._last_session_stamp: str | None = None
        self._task: asyncio.Task | None = None

    @property
    def message_id(self) -> UUID | None:
        return self._message_id

    def start(self) -> None:
        enabled = (
            HEARTBEAT_ENABLED if HEARTBEAT_ENABLED is not None
            else not settings.TESTING
        )
        if self._task is not None or not enabled:
            return
        interval = HEARTBEAT_INTERVAL_SECONDS
        self._task = asyncio.create_task(
            self._run(interval), name=f"stream-heartbeat-{self._session_id}"
        )
        _LIVE_HEARTBEATS.add(self._task)
        self._task.add_done_callback(_LIVE_HEARTBEATS.discard)

    def attach_message(self, message_id: UUID) -> None:
        """Include the in-progress agent row in subsequent beats."""
        self._message_id = message_id

    async def stop(self) -> None:
        """Stop beating, let an in-flight beat settle, then drop the session stamp.

        The stop flag is set synchronously first, so even if this await is
        cancelled no later beat can write. A cancel of the caller is always
        re-raised, after the ticker has settled.

        Removing ``stream_heartbeat_at`` from the session at the normal turn
        end keeps a finished turn's stamp from going stale on the session, so
        a later turn by a worker that does not beat (a pre-D10 worker during a
        rolling deploy) is not judged orphaned by it. The agent row needs no
        such step: the final write replaces its metadata without the key, and
        a sealed row is no longer a candidate.
        """
        self._stopped.set()
        self._wake.set()
        task = self._task
        if task is None:
            # Never started (ticker disabled): nothing of ours to clear,
            # unless a beat was run directly.
            if self._last_session_stamp is not None:
                await self._clear_session_stamp_safely()
            return
        if not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                caller_cancelled = not task.done() or not task.cancelled()
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    caller_cancelled = True
                if caller_cancelled:
                    try:
                        if not task.done():
                            await asyncio.wait({task})
                        clear = asyncio.create_task(self._clear_session_stamp_safely())
                        _LIVE_HEARTBEATS.add(clear)
                        clear.add_done_callback(_LIVE_HEARTBEATS.discard)
                        await asyncio.shield(clear)
                    finally:
                        raise
        await self._clear_session_stamp_safely()

    async def _clear_session_stamp_safely(self) -> None:
        try:
            await asyncio.to_thread(self._clear_session_stamp)
        except Exception as exc:  # noqa: BLE001 - never raise into the turn
            logger.warning(
                "Failed to clear stream heartbeat on session %s: %s",
                self._session_id, exc,
            )

    async def _run(self, interval: float) -> None:
        while not self._stopped.is_set():
            try:
                await asyncio.to_thread(self._beat)
            except Exception as exc:  # noqa: BLE001 - never raise into the turn
                logger.warning(
                    "Stream heartbeat failed for session %s: %s",
                    self._session_id, exc,
                )
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=interval)
            except TimeoutError:
                pass

    # ── thread side ─────────────────────────────────────────────────────

    def _beat(self) -> None:
        now = datetime.now(UTC)
        if self._started_at is None:
            self._started_at = now
        with self._get_db() as db:
            self._beat_session(db, now)
            if self._message_id is not None:
                self._beat_message(db, now)

    def _clear_session_stamp(self) -> None:
        with self._get_db() as db:
            row = db.get(
                ChatSession, self._session_id,
                with_for_update=True, populate_existing=True,
            )
            metadata = dict((row.session_metadata if row else None) or {})
            if (
                row is None
                or self._last_session_stamp is None
                or metadata.get(STREAM_HEARTBEAT_KEY) != self._last_session_stamp
            ):
                # Absent, or another turn (another worker) beats here now.
                db.rollback()
                return
            metadata.pop(STREAM_HEARTBEAT_KEY)
            row.session_metadata = metadata
            flag_modified(row, "session_metadata")
            db.add(row)
            db.commit()

    def _beat_session(self, db: Any, now: datetime) -> None:
        row = db.get(
            ChatSession, self._session_id,
            with_for_update=True, populate_existing=True,
        )
        if row is None or self._stopped.is_set():
            db.rollback()
            return
        metadata = dict(row.session_metadata or {})
        stamp = now.isoformat()
        metadata[STREAM_HEARTBEAT_KEY] = stamp
        cleared, original_start = _parse_orphan_cleared_marker(
            metadata.pop(ORPHAN_CLEARED_KEY, None)
        )
        if (
            cleared is not None
            and self._started_at is not None
            and cleared >= self._started_at
            and not row.interaction_status
        ):
            # The orphan pass cleared this very turn: it is alive after all.
            # Restore the original start so the hard-cap clock keeps running.
            row.interaction_status = "running"
            row.streaming_started_at = original_start or self._started_at
            logger.warning(
                "Stream heartbeat restored 'running' on session %s "
                "(orphan repair misjudged a live turn)", self._session_id,
            )
        row.session_metadata = metadata
        flag_modified(row, "session_metadata")
        db.add(row)
        db.commit()
        self._last_session_stamp = stamp

    def _beat_message(self, db: Any, now: datetime) -> None:
        row = db.get(
            SessionMessage, self._message_id,
            with_for_update=True, populate_existing=True,
        )
        if row is None or self._stopped.is_set():
            db.rollback()
            return
        metadata = dict(row.message_metadata or {})
        aborted = row.status == "aborted"
        if not metadata.get("streaming_in_progress") and not aborted:
            # Finalized (completed, interrupted): never reopen it.
            db.rollback()
            return
        metadata[STREAM_HEARTBEAT_KEY] = now.isoformat()
        if aborted:
            reopen_aborted(row, metadata)
            logger.warning(
                "Stream heartbeat reopened aborted agent message %s "
                "(orphan repair misjudged a live turn)", self._message_id,
            )
        row.message_metadata = metadata
        flag_modified(row, "message_metadata")
        db.add(row)
        db.commit()


def reopen_aborted(row: SessionMessage, metadata: dict) -> None:
    """Undo an orphan-pass seal on a row a live writer still owns."""
    metadata["streaming_in_progress"] = True
    row.status = ""
    row.status_message = None
