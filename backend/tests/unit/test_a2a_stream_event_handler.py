"""
Unit tests for ``A2AStreamEventHandler``.

These tests lock in the **incremental streaming guarantee** that the
handler is built around: events from the agent-env must reach the SSE
client as they arrive, not in a burst at the end of the stream. They
also cover the lifecycle contract (sentinel on every exit path, the
producer keeps running after a client disconnect).

The end-to-end HTTP tests in ``tests/api/a2a_integration/`` collect the
full SSE response before asserting, so they cannot distinguish
"streamed incrementally" from "buffered and flushed at the end". These
unit tests deliberately work at the handler level to verify cadence.

Run: cd backend && python -m pytest tests/unit/test_a2a_stream_event_handler.py -v
"""
from __future__ import annotations

import asyncio
from typing import Any

from app.services.sessions.stream_event_handlers import (
    A2AStreamEventHandler,
    wait_for_detached_a2a_turns,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_handler() -> A2AStreamEventHandler:
    """Build a handler with a simple format function that tags events by kind."""

    def fake_format(request_id: Any, event: dict) -> str:
        # Encode enough info to let tests assert on state + message.
        state = event.get("status", {}).get("state", "?")
        msg_parts = event.get("status", {}).get("message", {}).get("parts") or []
        text = ""
        if msg_parts:
            root = msg_parts[0].get("root") or msg_parts[0]
            text = root.get("text", "") if isinstance(root, dict) else ""
        return f"{request_id}|{state}|{text}"

    return A2AStreamEventHandler(
        task_id="task-1",
        context_id="ctx-1",
        request_id="req-1",
        format_sse_event=fake_format,
    )


class _ScriptedProcessor:
    """Fake processor that yields events through the handler with delays.

    Mirrors what ``SessionStreamProcessor`` does from the handler's
    point of view — calls ``handler.on_event`` as events arrive.
    """

    def __init__(
        self,
        handler: A2AStreamEventHandler,
        events: list[dict],
        delay_s: float = 0.0,
        raise_after: int | None = None,
        raise_exc: Exception | None = None,
    ) -> None:
        self.handler = handler
        self.events = events
        self.delay_s = delay_s
        self.raise_after = raise_after
        self.raise_exc = raise_exc
        self.cancelled = False
        self.completed = False

    async def process(self) -> None:
        try:
            for i, event in enumerate(self.events):
                if self.delay_s:
                    await asyncio.sleep(self.delay_s)
                await self.handler.on_event(event)
                if self.raise_after is not None and i + 1 == self.raise_after:
                    exc = self.raise_exc or RuntimeError("boom")
                    # Mirror SessionStreamProcessor._process_inner: call on_error
                    # then re-raise.
                    await self.handler.on_error(exc)
                    raise exc
            self.completed = True
        except asyncio.CancelledError:
            self.cancelled = True
            raise


# ---------------------------------------------------------------------------
# Cadence: events reach the consumer incrementally
# ---------------------------------------------------------------------------

def test_stream_yields_events_incrementally_not_buffered() -> None:
    """Each agent event must be delivered to the consumer before the next one is produced.

    Regression guard against the earlier bug where the handler collected
    events into a list and yielded them all after ``processor.process()``
    returned.
    """

    async def run() -> tuple[list[float], list[str], bool]:
        handler = _make_handler()
        processor = _ScriptedProcessor(
            handler,
            events=[
                {"type": "assistant", "content": "chunk-0"},
                {"type": "assistant", "content": "chunk-1"},
                {"type": "assistant", "content": "chunk-2"},
            ],
            delay_s=0.05,  # 50ms between events
        )

        loop = asyncio.get_running_loop()
        start = loop.time()
        times: list[float] = []
        texts: list[str] = []

        async for event in handler.stream(processor):
            times.append(loop.time() - start)
            parts = event.split("|", 2)
            if len(parts) == 3:
                texts.append(parts[2])

        return times, texts, processor.completed

    receive_times, received_texts, completed = asyncio.run(run())

    assert received_texts == ["chunk-0", "chunk-1", "chunk-2"], (
        f"expected all three chunks in order, got {received_texts}"
    )
    # If the handler had buffered until the end, all receive_times would
    # be clustered at ~3 * delay_s (~0.15s) instead of ~0.05s apart.
    assert receive_times[0] >= 0.04, (
        f"first event arrived too early ({receive_times[0]:.3f}s); "
        "suggests no real delay was observed"
    )
    gap_1 = receive_times[1] - receive_times[0]
    gap_2 = receive_times[2] - receive_times[1]
    assert gap_1 >= 0.04, (
        f"events 0→1 were not delivered incrementally (gap={gap_1:.3f}s); "
        "handler may be buffering events"
    )
    assert gap_2 >= 0.04, (
        f"events 1→2 were not delivered incrementally (gap={gap_2:.3f}s); "
        "handler may be buffering events"
    )
    assert completed is True


# ---------------------------------------------------------------------------
# Lifecycle: sentinel on every exit path
# ---------------------------------------------------------------------------

def test_stream_terminates_on_normal_completion() -> None:
    """Normal completion: sentinel is posted by the done-callback, consumer unblocks."""

    async def run() -> tuple[list[str], bool, bool]:
        handler = _make_handler()
        processor = _ScriptedProcessor(
            handler,
            events=[{"type": "assistant", "content": "only-chunk"}],
        )
        collected = [event async for event in handler.stream(processor)]
        return collected, processor.completed, handler.queue.empty()

    collected, completed, queue_empty = asyncio.run(run())

    assert len(collected) == 1
    assert "only-chunk" in collected[0]
    assert completed is True
    assert queue_empty is True


def test_stream_surfaces_mid_stream_error_and_dedupes() -> None:
    """If the processor raises mid-stream after calling ``on_error``, the error
    event surfaces exactly once and the consumer still terminates.

    Exercises the ``_enqueue_error_once`` guard: ``on_error`` enqueues an
    error, the re-raised exception hits ``_run_processor``'s except
    branch, and the guard prevents a duplicate.
    """

    async def run() -> tuple[list[str], bool]:
        handler = _make_handler()
        processor = _ScriptedProcessor(
            handler,
            events=[
                {"type": "assistant", "content": "chunk-0"},
                {"type": "assistant", "content": "chunk-1"},
            ],
            raise_after=1,
            raise_exc=RuntimeError("mid-stream boom"),
        )
        collected = [event async for event in handler.stream(processor)]
        return collected, handler.error_enqueued

    collected, error_enqueued = asyncio.run(run())

    # Expect: chunk-0, then exactly one failed event. chunk-1 never gets sent.
    assert len(collected) == 2, f"expected 2 events (chunk + error), got {collected!r}"
    assert "chunk-0" in collected[0]
    assert "failed" in collected[1]
    assert "mid-stream boom" in collected[1]
    assert error_enqueued is True


def test_stream_surfaces_pre_stream_error_without_on_error() -> None:
    """Env-not-ready errors bypass ``on_error`` (raised before the processor's
    internal try/except wraps anything). The handler's own except branch
    must still produce a failed event.
    """

    class EnvNotReadyProcessor:
        async def process(self) -> None:
            raise ValueError("environment suspended and failed to activate")

    async def run() -> tuple[list[str], bool]:
        handler = _make_handler()
        collected = [event async for event in handler.stream(EnvNotReadyProcessor())]
        return collected, handler.error_enqueued

    collected, error_enqueued = asyncio.run(run())

    assert len(collected) == 1
    assert "failed" in collected[0]
    assert "Environment error" in collected[0]
    assert "environment suspended" in collected[0]
    assert error_enqueued is True


# ---------------------------------------------------------------------------
# Lifecycle: client disconnect does not cancel the producer
# ---------------------------------------------------------------------------

def test_stream_producer_survives_client_disconnect() -> None:
    """When the consumer closes the async generator early (SSE client
    disconnected), the detached producer keeps running to completion and
    its later events are dropped instead of queued.
    """

    async def run() -> tuple[str, str, bool, bool, int]:
        handler = _make_handler()
        processor = _ScriptedProcessor(
            handler,
            events=[{"type": "assistant", "content": f"chunk-{i}"} for i in range(10)],
            delay_s=0.01,
        )

        agen = handler.stream(processor)
        first = await agen.__anext__()
        second = await agen.__anext__()
        await agen.aclose()
        await wait_for_detached_a2a_turns(timeout=2.0)
        return (
            first, second, processor.cancelled, processor.completed,
            handler.queue.qsize(),
        )

    first, second, cancelled, completed, queued = asyncio.run(run())

    assert "chunk-0" in first
    assert "chunk-1" in second
    assert cancelled is False
    assert completed is True, "producer should run to completion after disconnect"
    assert queued == 0, "events after disconnect must not accumulate in the queue"


def test_stream_unblocks_consumer_if_producer_dies_without_sentinel() -> None:
    """Defense-in-depth: if the producer task exits without enqueuing a
    sentinel (e.g. killed abruptly), the done-callback posts one so the
    consumer never hangs on ``queue.get()``.
    """

    class ImmediateExitProcessor:
        async def process(self) -> None:
            return  # no events, no on_complete, no on_error

    async def run() -> list[str]:
        handler = _make_handler()
        # If the sentinel guarantee is broken, this hangs and wait_for fails.
        return await asyncio.wait_for(
            _collect(handler.stream(ImmediateExitProcessor())),
            timeout=1.0,
        )

    async def _collect(agen) -> list[str]:
        return [event async for event in agen]

    collected = asyncio.run(run())
    assert collected == []


def test_on_error_is_idempotent_across_multiple_calls() -> None:
    """Even if ``on_error`` is invoked twice for the same stream, only one
    error event reaches the queue. Guards against double-signalling if
    future wiring changes introduce a second error path.
    """

    async def run() -> tuple[list[str], bool]:
        handler = _make_handler()
        await handler.on_error(RuntimeError("first"))
        await handler.on_error(RuntimeError("second"))

        events: list[str] = []
        while not handler.queue.empty():
            events.append(handler.queue.get_nowait())
        return events, handler.error_enqueued

    events, error_enqueued = asyncio.run(run())

    assert len(events) == 1
    assert "first" in events[0]
    assert "second" not in events[0]
    assert error_enqueued is True
