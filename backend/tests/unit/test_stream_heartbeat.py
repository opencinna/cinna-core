"""
Unit tests for the per-turn stream heartbeat (plan D10,
``app/services/sessions/stream_heartbeat.py``).

No database: a small in-memory fake stands in for ``get_fresh_db_session``.
``get(..., populate_existing=True)`` returns a fresh object built from the
last *committed* state, and ``commit`` writes the added objects back — enough
to show that a beat re-reads the row and sets only its own key, so a flush
committed between two beats is never clobbered.

The orphan pass that reads the heartbeat is covered by
``tests/api/agents/sessions/agents_orphaned_stream_repair_test.py``.
"""
from __future__ import annotations

import asyncio
import copy
import threading
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch

import pytest

from app.models.sessions.session import Session as ChatSession, SessionMessage
from app.services.sessions import stream_heartbeat as hb_module
from app.services.sessions.stream_heartbeat import (
    ORPHAN_CLEARED_KEY,
    STREAM_HEARTBEAT_KEY,
    StreamHeartbeat,
    build_orphan_cleared_marker,
    parse_heartbeat,
)

_FIELDS = {
    ChatSession: ("session_metadata", "interaction_status", "streaming_started_at"),
    SessionMessage: ("message_metadata", "status", "status_message"),
}


class _FakeStore:
    def __init__(self) -> None:
        self.rows: dict[tuple[type, uuid.UUID], dict[str, Any]] = {}
        self.commits: list[tuple[type, uuid.UUID]] = []
        self.lock = threading.Lock()
        self.fail = False

    def put(self, model: type, row_id: uuid.UUID, **state: Any) -> None:
        with self.lock:
            self.rows[(model, row_id)] = copy.deepcopy(state)

    def state(self, model: type, row_id: uuid.UUID) -> dict[str, Any]:
        with self.lock:
            return copy.deepcopy(self.rows[(model, row_id)])

    def commits_for(self, model: type) -> int:
        with self.lock:
            return sum(1 for m, _ in self.commits if m is model)

    def factory(self) -> "_FakeDB":
        return _FakeDB(self)


class _FakeDB:
    def __init__(self, store: _FakeStore) -> None:
        self._store = store
        self._added: list[Any] = []

    def __enter__(self) -> "_FakeDB":
        return self

    def __exit__(self, *exc: object) -> None:
        self._added.clear()

    def get(self, model: type, row_id: uuid.UUID, **_: Any) -> Any:
        if self._store.fail:
            raise RuntimeError("db down")
        with self._store.lock:
            state = self._store.rows.get((model, row_id))
            if state is None:
                return None
            return model(id=row_id, **copy.deepcopy(state))

    def exec(self, statement: Any) -> Any:
        # Only the turn-aborted marker lookup queries; the fake has no user
        # messages (covered against real rows in agents_stream_heartbeat_test).
        class _NoRows:
            def first(self) -> None:
                return None

        return _NoRows()

    def add(self, obj: Any) -> None:
        self._added.append(obj)

    def commit(self) -> None:
        with self._store.lock:
            for obj in self._added:
                model = type(obj)
                self._store.rows[(model, obj.id)] = {
                    f: copy.deepcopy(getattr(obj, f)) for f in _FIELDS[model]
                }
                self._store.commits.append((model, obj.id))
        self._added.clear()

    def rollback(self) -> None:
        self._added.clear()


@pytest.fixture
def store() -> _FakeStore:
    return _FakeStore()


@pytest.fixture(autouse=True)
def _fast_enabled_heartbeat():
    with patch.object(hb_module, "HEARTBEAT_INTERVAL_SECONDS", 0.02), \
            patch.object(hb_module, "HEARTBEAT_ENABLED", True):
        yield


def _seed(store: _FakeStore, *, message_status: str = "", streaming: bool = True,
          interaction_status: str = "running", session_meta: dict | None = None,
          ) -> tuple[uuid.UUID, uuid.UUID]:
    session_id, message_id = uuid.uuid4(), uuid.uuid4()
    store.put(
        ChatSession, session_id,
        session_metadata=session_meta or {"other": "kept"},
        interaction_status=interaction_status,
        streaming_started_at=None,
    )
    store.put(
        SessionMessage, message_id,
        message_metadata={"streaming_in_progress": streaming, "mode": "conversation"},
        status=message_status,
        status_message=None,
    )
    return session_id, message_id


async def _wait_for(predicate, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


def test_writes_session_immediately_and_message_periodically(store: _FakeStore) -> None:
    session_id, message_id = _seed(store)

    async def scenario() -> None:
        hb = StreamHeartbeat(session_id, store.factory)
        hb.start()
        await _wait_for(lambda: store.commits_for(ChatSession) >= 1)
        assert store.commits_for(SessionMessage) == 0, "no agent row attached yet"
        first = store.state(ChatSession, session_id)["session_metadata"][STREAM_HEARTBEAT_KEY]

        hb.attach_message(message_id)
        await _wait_for(lambda: store.commits_for(SessionMessage) >= 3)

        session_meta = store.state(ChatSession, session_id)["session_metadata"]
        assert session_meta["other"] == "kept"
        assert parse_heartbeat(session_meta[STREAM_HEARTBEAT_KEY]) > parse_heartbeat(first)
        message_meta = store.state(SessionMessage, message_id)["message_metadata"]
        assert parse_heartbeat(message_meta[STREAM_HEARTBEAT_KEY]) is not None
        assert message_meta["streaming_in_progress"] is True

        await hb.stop()
        # The normal turn end drops the session stamp (rolling-deploy safety).
        session_meta = store.state(ChatSession, session_id)["session_metadata"]
        assert STREAM_HEARTBEAT_KEY not in session_meta
        assert session_meta["other"] == "kept"

    asyncio.run(scenario())


def test_beat_does_not_clobber_a_concurrent_flush_key(store: _FakeStore) -> None:
    session_id, message_id = _seed(store)

    async def scenario() -> None:
        hb = StreamHeartbeat(session_id, store.factory)
        hb.attach_message(message_id)
        hb.start()
        await _wait_for(lambda: store.commits_for(SessionMessage) >= 1)

        # A flush commits between two beats, rewriting the JSON column whole.
        flushed = store.state(SessionMessage, message_id)
        flushed["message_metadata"]["streaming_events"] = [{"type": "assistant", "event_seq": 7}]
        store.put(SessionMessage, message_id, **flushed)
        session_side = store.state(ChatSession, session_id)
        session_side["session_metadata"]["title_generated"] = True
        store.put(ChatSession, session_id, **session_side)

        seen = store.commits_for(SessionMessage)
        await _wait_for(lambda: store.commits_for(SessionMessage) >= seen + 2)
        await hb.stop()

        message_meta = store.state(SessionMessage, message_id)["message_metadata"]
        assert message_meta["streaming_events"] == [{"type": "assistant", "event_seq": 7}]
        assert STREAM_HEARTBEAT_KEY in message_meta
        assert store.state(ChatSession, session_id)["session_metadata"]["title_generated"] is True

    asyncio.run(scenario())


def test_stop_ends_the_ticker_and_no_beat_lands_after_it(store: _FakeStore) -> None:
    session_id, message_id = _seed(store)

    async def scenario() -> None:
        hb = StreamHeartbeat(session_id, store.factory)
        hb.attach_message(message_id)
        hb.start()
        await _wait_for(lambda: store.commits_for(SessionMessage) >= 1)
        await hb.stop()
        assert hb._task is not None and hb._task.done()
        assert hb._task not in hb_module._LIVE_HEARTBEATS

        commits = len(store.commits)
        await asyncio.sleep(0.1)
        assert len(store.commits) == commits

        # A beat already in its thread when stop() is called writes nothing:
        # the flag is checked under the row lock.
        hb._beat()
        assert len(store.commits) == commits

    asyncio.run(scenario())


def test_finalized_row_is_never_reopened(store: _FakeStore) -> None:
    session_id, message_id = _seed(store, streaming=False, message_status="user_interrupted")

    async def scenario() -> None:
        hb = StreamHeartbeat(session_id, store.factory)
        hb.attach_message(message_id)
        hb.start()
        await _wait_for(lambda: store.commits_for(ChatSession) >= 2)
        await hb.stop()

    asyncio.run(scenario())
    state = store.state(SessionMessage, message_id)
    assert store.commits_for(SessionMessage) == 0
    assert state["status"] == "user_interrupted"
    assert state["message_metadata"]["streaming_in_progress"] is False


def test_aborted_row_is_reopened_by_a_live_beat(store: _FakeStore) -> None:
    session_id, message_id = _seed(store, streaming=False, message_status="aborted")

    async def scenario() -> None:
        hb = StreamHeartbeat(session_id, store.factory)
        hb.attach_message(message_id)
        hb.start()
        await _wait_for(lambda: store.commits_for(SessionMessage) >= 1)
        await hb.stop()

    asyncio.run(scenario())
    state = store.state(SessionMessage, message_id)
    assert state["status"] == ""
    assert state["message_metadata"]["streaming_in_progress"] is True


def test_session_cleared_for_this_turn_is_restored_to_running(store: _FakeStore) -> None:
    session_id, _ = _seed(store, interaction_status="running")
    original_start = datetime.now(UTC) - timedelta(hours=3)

    async def scenario() -> None:
        hb = StreamHeartbeat(session_id, store.factory)
        hb.start()
        await _wait_for(lambda: store.commits_for(ChatSession) >= 1)

        # The orphan pass clears the session, judging this turn's heartbeat.
        state = store.state(ChatSession, session_id)
        judged = state["session_metadata"][STREAM_HEARTBEAT_KEY]
        state["interaction_status"] = ""
        state["session_metadata"][ORPHAN_CLEARED_KEY] = build_orphan_cleared_marker(
            judged, original_start,
        )
        store.put(ChatSession, session_id, **state)

        await _wait_for(
            lambda: store.state(ChatSession, session_id)["interaction_status"] == "running"
        )
        await hb.stop()

    asyncio.run(scenario())
    state = store.state(ChatSession, session_id)
    assert ORPHAN_CLEARED_KEY not in state["session_metadata"]
    # The original start is restored, so the hard-cap clock is not reset.
    assert state["streaming_started_at"] == original_start


def test_marker_from_an_earlier_turn_does_not_restore(store: _FakeStore) -> None:
    earlier = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    session_id, _ = _seed(
        store, interaction_status="",
        session_meta={ORPHAN_CLEARED_KEY: build_orphan_cleared_marker(earlier, None)},
    )

    async def scenario() -> None:
        hb = StreamHeartbeat(session_id, store.factory)
        hb.start()
        await _wait_for(lambda: store.commits_for(ChatSession) >= 2)
        await hb.stop()

    asyncio.run(scenario())
    state = store.state(ChatSession, session_id)
    assert state["interaction_status"] == ""
    assert ORPHAN_CLEARED_KEY not in state["session_metadata"]


def test_db_errors_never_reach_the_turn(store: _FakeStore) -> None:
    session_id, _ = _seed(store)
    store.fail = True

    async def scenario() -> None:
        hb = StreamHeartbeat(session_id, store.factory)
        hb.start()
        await asyncio.sleep(0.08)
        assert hb._task is not None and not hb._task.done()
        store.fail = False
        await _wait_for(lambda: store.commits_for(ChatSession) >= 1)
        await hb.stop()
        assert hb._task.exception() is None

    asyncio.run(scenario())


def test_disabled_heartbeat_starts_no_task(store: _FakeStore) -> None:
    session_id, _ = _seed(store)

    async def scenario() -> None:
        with patch.object(hb_module, "HEARTBEAT_ENABLED", False):
            hb = StreamHeartbeat(session_id, store.factory)
            hb.start()
            assert hb._task is None
            await hb.stop()

    asyncio.run(scenario())
    assert store.commits == []


def test_stop_re_raises_the_callers_cancel_after_the_ticker_settles(store: _FakeStore) -> None:
    session_id, _ = _seed(store)
    beat_started = threading.Event()
    release = threading.Event()
    original_beat = StreamHeartbeat._beat

    def slow_beat(self: StreamHeartbeat) -> None:
        beat_started.set()
        release.wait(2)
        original_beat(self)

    async def scenario() -> None:
        with patch.object(StreamHeartbeat, "_beat", slow_beat):
            hb = StreamHeartbeat(session_id, store.factory)
            hb.start()
            await asyncio.to_thread(beat_started.wait, 2)

            stopper = asyncio.create_task(hb.stop())
            await asyncio.sleep(0.02)
            stopper.cancel()
            await asyncio.sleep(0.02)
            assert not stopper.done(), "must wait for the in-flight beat"
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await stopper
            assert hb._task.done()

    asyncio.run(scenario())
    # The in-flight beat saw the stop flag under the lock and wrote nothing.
    assert store.commits == []


def test_turn_end_leaves_another_turns_session_stamp_alone(store: _FakeStore) -> None:
    session_id, _ = _seed(store)

    async def scenario() -> None:
        hb = StreamHeartbeat(session_id, store.factory)
        hb.start()
        await _wait_for(lambda: store.commits_for(ChatSession) >= 1)
        # Another worker's overlapping turn beats on the same session.
        state = store.state(ChatSession, session_id)
        foreign = (datetime.now(UTC) + timedelta(seconds=5)).isoformat()
        hb._stopped.set()  # freeze our ticker before the foreign write
        hb._wake.set()
        await _wait_for(lambda: hb._task.done())
        state["session_metadata"][STREAM_HEARTBEAT_KEY] = foreign
        store.put(ChatSession, session_id, **state)

        await hb.stop()
        meta = store.state(ChatSession, session_id)["session_metadata"]
        assert meta[STREAM_HEARTBEAT_KEY] == foreign

    asyncio.run(scenario())


def test_cancelled_stop_still_clears_its_own_session_stamp(store: _FakeStore) -> None:
    session_id, _ = _seed(store)
    release = threading.Event()
    beats = {"n": 0}
    original_beat = StreamHeartbeat._beat

    def gated_beat(self: StreamHeartbeat) -> None:
        beats["n"] += 1
        if beats["n"] == 2:
            release.wait(2)
        original_beat(self)

    async def scenario() -> None:
        with patch.object(StreamHeartbeat, "_beat", gated_beat):
            hb = StreamHeartbeat(session_id, store.factory)
            hb.start()
            await _wait_for(lambda: beats["n"] >= 2)
            stopper = asyncio.create_task(hb.stop())
            await asyncio.sleep(0.02)
            stopper.cancel()
            await asyncio.sleep(0.02)
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await stopper

    asyncio.run(scenario())
    meta = store.state(ChatSession, session_id)["session_metadata"]
    assert STREAM_HEARTBEAT_KEY not in meta, "cancel path still clears its own stamp"
