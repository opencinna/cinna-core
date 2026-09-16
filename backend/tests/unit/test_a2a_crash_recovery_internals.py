"""
Unit tests for the C1 crash-recovery internals (plan: a2a_crash_recovery_plan.md §1).

Covers what ``tests/unit/test_a2a_stream_event_handler.py`` does not
(that file already carries the cadence/lifecycle/disconnect-survival tests):

- ``A2AStreamEventHandler``: a detached producer emitting far more events
  than the queue ever saw (drop-not-block, at scale), removal from
  ``_DETACHED_A2A_TURNS`` once done, and ``emit_final_state``'s no-op guard
  (idempotent, and superseded by an already-mapped final event).
- ``LockedTurnRunner``: teardown order (interaction-status clear, then
  ``after_teardown``, both still inside the per-session lock), skipping
  ``after_teardown`` when the turn raises, and that a cancelled turn still
  tears down and releases the lock.
- ``is_session_lock_held``: read-only, never creates a ``_session_locks`` entry.

No DB, no client — pure asyncio + the module-level lock/task registries.
"""
from __future__ import annotations

import asyncio
import uuid
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from a2a.types import TaskState

from app.services.sessions.session_service import SessionService
from app.services.sessions.stream_event_handlers import (
    _DETACHED_A2A_TURNS,
    A2AStreamEventHandler,
    wait_for_detached_a2a_turns,
)
from app.services.sessions.stream_processor import (
    LockedTurnRunner,
    _session_locks,
    is_session_lock_held,
)


# ---------------------------------------------------------------------------
# Helpers (mirrors tests/unit/test_a2a_stream_event_handler.py's private helpers,
# duplicated locally so this file has no cross-file test coupling)
# ---------------------------------------------------------------------------

def _make_handler(task_id: str = "task-1") -> A2AStreamEventHandler:
    def fake_format(request_id: Any, event: dict) -> str:
        state = event.get("status", {}).get("state", "?")
        msg_parts = event.get("status", {}).get("message", {}).get("parts") or []
        text = ""
        if msg_parts:
            root = msg_parts[0].get("root") or msg_parts[0]
            text = root.get("text", "") if isinstance(root, dict) else ""
        return f"{request_id}|{state}|{text}"

    return A2AStreamEventHandler(
        task_id=task_id,
        context_id="ctx-1",
        request_id="req-1",
        format_sse_event=fake_format,
    )


class _ScriptedProcessor:
    """Fake processor that calls ``handler.on_event`` for each scripted event."""

    def __init__(self, handler: A2AStreamEventHandler, events: list[dict]) -> None:
        self.handler = handler
        self.events = events
        self.completed = False

    async def process(self) -> None:
        for event in self.events:
            await self.handler.on_event(event)
        self.completed = True


# ---------------------------------------------------------------------------
# A2AStreamEventHandler — detached producer at scale
# ---------------------------------------------------------------------------

def test_detached_producer_drops_events_without_blocking_at_scale() -> None:
    """After the consumer detaches, the producer must run 10k+ events to
    completion without blocking, and none of those events accumulate in the
    queue (``_emit`` drops them once ``_consumer_attached`` is False).
    """

    async def run() -> tuple[str, bool, int]:
        handler = _make_handler()
        n = 10_001
        processor = _ScriptedProcessor(
            handler,
            events=[{"type": "assistant", "content": f"chunk-{i}"} for i in range(n)],
        )

        agen = handler.stream(processor)
        first = await agen.__anext__()
        await agen.aclose()  # simulate disconnect right after the first item
        await wait_for_detached_a2a_turns(timeout=10.0)
        return first, processor.completed, handler.queue.qsize()

    first, completed, qsize = asyncio.run(run())

    assert "chunk-0" in first
    assert completed is True, "producer must run all 10k+ events to completion after disconnect"
    assert qsize == 0, "dropped events must never accumulate in the queue"


def test_task_removed_from_detached_set_when_done() -> None:
    """The producer task is discarded from ``_DETACHED_A2A_TURNS`` by its
    own done-callback — it must not be found there once the stream ends.
    """

    async def run() -> bool:
        handler = _make_handler(task_id="task-removal-check")
        processor = _ScriptedProcessor(handler, events=[{"type": "assistant", "content": "x"}])
        collected = [event async for event in handler.stream(processor)]
        assert collected  # sanity: stream actually ran
        return any(
            t.get_name() == "a2a-stream-producer-task-removal-check"
            for t in _DETACHED_A2A_TURNS
        )

    still_present = asyncio.run(run())
    assert still_present is False


# ---------------------------------------------------------------------------
# A2AStreamEventHandler — emit_final_state
# ---------------------------------------------------------------------------

def test_emit_final_state_emits_once_and_is_idempotent() -> None:
    async def run() -> tuple[int, int, bool]:
        handler = _make_handler()
        await handler.emit_final_state(TaskState.completed)
        first_size = handler.queue.qsize()
        await handler.emit_final_state(TaskState.completed)
        second_size = handler.queue.qsize()
        return first_size, second_size, handler.final_emitted

    first_size, second_size, final_emitted = asyncio.run(run())

    assert first_size == 1, "first call must emit exactly one closing status event"
    assert second_size == 1, "second call must be a no-op (already final_emitted)"
    assert final_emitted is True


def test_emit_final_state_is_noop_after_a_mapped_final_event() -> None:
    """A real final event from the stream (e.g. ``stream_completed``) already
    sets ``final_emitted``; a subsequent ``emit_final_state`` call must not
    enqueue a second closing event.
    """

    async def run() -> tuple[int, bool]:
        handler = _make_handler()
        await handler.on_event({"type": "stream_completed"})
        size_after_mapped_final = handler.queue.qsize()
        await handler.emit_final_state(TaskState.completed)
        size_after_emit_final_state = handler.queue.qsize()
        return size_after_mapped_final, size_after_emit_final_state == size_after_mapped_final

    size_after_mapped, unchanged = asyncio.run(run())

    assert size_after_mapped == 1
    assert unchanged is True, "emit_final_state must not add a second event on top of a mapped final"


def test_emit_final_state_drops_silently_once_consumer_has_detached() -> None:
    """Calling ``emit_final_state`` after the consumer detached must not grow
    the queue — ``_emit`` drops it, same as any other post-detach event.
    """

    async def run() -> int:
        handler = _make_handler()
        processor = _ScriptedProcessor(handler, events=[{"type": "assistant", "content": "x"}])
        agen = handler.stream(processor)
        await agen.__anext__()
        await agen.aclose()  # detaches the consumer; queue is drained empty
        await handler.emit_final_state(TaskState.completed)
        return handler.queue.qsize()

    qsize = asyncio.run(run())
    assert qsize == 0


# ---------------------------------------------------------------------------
# LockedTurnRunner — teardown order
# ---------------------------------------------------------------------------

def test_locked_turn_runner_runs_teardown_then_after_teardown_inside_the_lock() -> None:
    async def run() -> tuple[list[str], Any, bool]:
        session_id = uuid.uuid4()
        order: list[str] = []

        class OkProcessor:
            async def process(self) -> str:
                order.append("process")
                return "result"

        async def after_teardown() -> None:
            # Must observe the lock as still held at this point.
            assert is_session_lock_held(str(session_id)) is True
            order.append("after_teardown")

        clear_mock = AsyncMock(side_effect=lambda *a, **k: order.append("clear"))
        with patch.object(SessionService, "clear_interaction_status", clear_mock):
            runner = LockedTurnRunner(
                processor=OkProcessor(),
                session_id=session_id,
                teardown_reason="unit-test",
                after_teardown=after_teardown,
            )
            result = await runner.process()

        lock_released = not is_session_lock_held(str(session_id))
        return order, result, lock_released

    order, result, lock_released = asyncio.run(run())

    assert order == ["process", "clear", "after_teardown"]
    assert result == "result"
    assert lock_released is True


def test_locked_turn_runner_skips_after_teardown_when_turn_raises() -> None:
    async def run() -> tuple[list[str], bool, bool]:
        session_id = uuid.uuid4()
        order: list[str] = []

        class RaisingProcessor:
            async def process(self) -> None:
                raise RuntimeError("boom")

        after_teardown_mock = AsyncMock()
        clear_mock = AsyncMock(side_effect=lambda *a, **k: order.append("clear"))
        with patch.object(SessionService, "clear_interaction_status", clear_mock):
            runner = LockedTurnRunner(
                processor=RaisingProcessor(),
                session_id=session_id,
                teardown_reason="unit-test",
                after_teardown=after_teardown_mock,
            )
            with pytest.raises(RuntimeError, match="boom"):
                await runner.process()

        lock_released = not is_session_lock_held(str(session_id))
        return order, after_teardown_mock.called, lock_released

    order, after_teardown_called, lock_released = asyncio.run(run())

    assert order == ["clear"], "teardown clear still runs (best-effort) even though the turn raised"
    assert after_teardown_called is False, "after_teardown must be skipped when the turn did not succeed"
    assert lock_released is True


def test_locked_turn_runner_cancel_still_tears_down_and_releases_lock() -> None:
    async def run() -> tuple[list[str], bool, bool]:
        session_id = uuid.uuid4()
        order: list[str] = []
        started = asyncio.Event()
        blocked = asyncio.Event()

        class SlowProcessor:
            async def process(self) -> str:
                started.set()
                await blocked.wait()  # only unblocked by cancellation
                return "unreachable"

        after_teardown_mock = AsyncMock()
        clear_mock = AsyncMock(side_effect=lambda *a, **k: order.append("clear"))
        with patch.object(SessionService, "clear_interaction_status", clear_mock):
            runner = LockedTurnRunner(
                processor=SlowProcessor(),
                session_id=session_id,
                teardown_reason="unit-test",
                after_teardown=after_teardown_mock,
            )
            task = asyncio.create_task(runner.process())
            await started.wait()
            assert is_session_lock_held(str(session_id)) is True

            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        lock_released = not is_session_lock_held(str(session_id))
        return order, after_teardown_mock.called, lock_released

    order, after_teardown_called, lock_released = asyncio.run(run())

    assert order == ["clear"], "the shielded teardown clear must still run on cancellation"
    assert after_teardown_called is False
    assert lock_released is True, "the per-session lock must be released even when the turn is cancelled"


# ---------------------------------------------------------------------------
# is_session_lock_held — read-only
# ---------------------------------------------------------------------------

def test_is_session_lock_held_does_not_create_a_lock_entry() -> None:
    session_id = str(uuid.uuid4())
    assert session_id not in _session_locks

    assert is_session_lock_held(session_id) is False
    assert session_id not in _session_locks, (
        "probing an unknown session must not grow _session_locks"
    )
