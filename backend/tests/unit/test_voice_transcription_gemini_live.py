"""Unit tests for the Gemini Live speech adapter (voice transcription relay, P2).

Pure logic against a fake live session: no network, no DB. The adapter's
``connect`` factory and ``GeminiTimings`` are injected, so timers are scaled
down (quiet 0.1 s / burst 0.4 s / stop 0.3 s / cap 0.6 s instead of
0.3 / 0.6 / 3 / 10) and keep the same ratios as the desktop engine.

Mirrors the scenarios of cinna-desktop ``geminiLive.test.ts``. The WS / REST
path through the session service is covered in ``tests/api`` (P3).
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest

from app.services.voice_transcription.providers.base import (
    Closed,
    Failed,
    Final,
    Partial,
    Ready,
)
from app.services.voice_transcription.providers.gemini_live import (
    MSG_AUTH,
    MSG_QUOTA,
    MSG_UNEXPECTED,
    MSG_UNREACHABLE,
    GeminiLiveAdapter,
    GeminiTimings,
)

KEY = "AIzaSy-SECRET-KEY-123"
QUIET = 0.1
BURST = 0.4
FAST = GeminiTimings(
    stop_timeout=0.3,
    stop_cap=0.6,
    quiet_grace=QUIET,
    burst_grace=BURST,
    connect_timeout=2.0,
    rotate_after=60.0,
)
CHUNK = bytes(3200)


# ── fakes ─────────────────────────────────────────────────────────────


class CloseError(Exception):
    """A websocket-style close carrying a numeric code."""

    def __init__(self, code: int | None, reason: str) -> None:
        super().__init__(reason)
        self.code = code


class FakeSession:
    def __init__(self) -> None:
        self.sent: list[tuple[str, Any]] = []
        self.inbox: asyncio.Queue[Any] = asyncio.Queue()

    async def send_realtime_input(self, *, audio: Any = None, audio_stream_end: bool | None = None) -> None:
        if audio is not None:
            self.sent.append(("audio", audio.data))
        if audio_stream_end:
            self.sent.append(("end", None))

    async def receive(self):
        while True:
            item = await self.inbox.get()
            if isinstance(item, BaseException):
                raise item
            yield item

    # server -> client scripting
    def say(self, message: Any) -> None:
        self.inbox.put_nowait(message)

    def interim(self, text: str) -> None:
        self.say(SimpleNamespace(server_content=SimpleNamespace(interim_input_transcription=SimpleNamespace(text=text))))

    def final(self, text: str) -> None:
        self.say(SimpleNamespace(server_content=SimpleNamespace(input_transcription=SimpleNamespace(text=text))))

    def activity(self, kind: str) -> None:
        self.say(SimpleNamespace(voice_activity=SimpleNamespace(voice_activity_type=kind)))

    def go_away(self) -> None:
        self.say(SimpleNamespace(go_away=SimpleNamespace(time_left="5s")))

    def drop(self, code: int | None, reason: str) -> None:
        self.say(CloseError(code, reason))

    @property
    def audio(self) -> list[bytes]:
        return [d for kind, d in self.sent if kind == "audio"]

    @property
    def ended(self) -> bool:
        return ("end", None) in self.sent


class FakeConnect:
    """``LiveConnect`` stand-in. ``gates[n]`` delays setup of connection n+1;
    ``enter_errors[n]`` makes connection n+1 fail during setup."""

    def __init__(self) -> None:
        self.sessions: list[FakeSession] = []
        self.calls: list[tuple[str, str]] = []
        self.gates: dict[int, asyncio.Event] = {}
        self.enter_errors: dict[int, BaseException] = {}

    def __call__(self, api_key: str, locale: str):
        index = len(self.calls)
        self.calls.append((api_key, locale))

        @asynccontextmanager
        async def ctx():
            gate = self.gates.get(index)
            if gate is not None:
                await gate.wait()
            error = self.enter_errors.get(index)
            if error is not None:
                raise error
            session = FakeSession()
            self.sessions.append(session)
            yield session

        return ctx()


class Run:
    """An open stream plus a background collector of its events."""

    def __init__(self, stream: Any) -> None:
        self.stream = stream
        self.events: list[Any] = []
        self.times: list[float] = []
        self.task = asyncio.create_task(self._collect())

    async def _collect(self) -> None:
        async for event in self.stream.events():
            self.events.append(event)
            self.times.append(time.monotonic())

    async def until(self, cond: Callable[[], bool], timeout: float = 3.0) -> None:
        deadline = time.monotonic() + timeout
        while not cond():
            if time.monotonic() > deadline:
                raise AssertionError(f"condition not met; events={self.events!r}")
            await asyncio.sleep(0.005)

    async def terminal(self, timeout: float = 3.0) -> None:
        await self.until(lambda: self.task.done(), timeout)

    @property
    def texts(self) -> list[tuple[str, str]]:
        return [(type(e).__name__, e.text) for e in self.events if isinstance(e, (Partial, Final))]


async def open_stream(connect: FakeConnect, timings: GeminiTimings = FAST, *, wait_ready: bool = True):
    adapter = GeminiLiveAdapter(connect=connect, timings=timings)
    stream = await adapter.open(api_key=KEY, locale="en-US", sample_rate=16_000)
    run = Run(stream)
    if wait_ready:
        await run.until(lambda: any(isinstance(e, Ready) for e in run.events))
    return run


def run_async(coro: Awaitable[Any]) -> Any:
    return asyncio.run(coro)


# ── transcription flow ────────────────────────────────────────────────


def test_interim_replaces_and_finals_relay_in_order_then_closed() -> None:
    async def scenario() -> None:
        connect = FakeConnect()
        run = await open_stream(connect)
        assert connect.calls == [(KEY, "en-US")]
        s = connect.sessions[0]
        await run.stream.push(CHUNK)
        s.interim("he")
        s.interim("hello")
        s.interim("hell")  # may shrink: replace, not append
        s.final("hello world")
        s.interim("second")
        s.final("second one")
        await run.until(lambda: len(run.texts) == 6)
        await run.stream.end_audio()
        await run.terminal()
        assert run.texts == [
            ("Partial", "he"), ("Partial", "hello"), ("Partial", "hell"),
            ("Final", "hello world"), ("Partial", "second"), ("Final", "second one"),
        ]
        assert isinstance(run.events[0], Ready)
        assert isinstance(run.events[-1], Closed)
        assert sum(isinstance(e, Closed) for e in run.events) == 1
        assert s.audio == [CHUNK] and s.ended

    run_async(scenario())


def test_audio_pushed_before_setup_is_queued_then_flushed_in_order() -> None:
    async def scenario() -> None:
        connect = FakeConnect()
        connect.gates[0] = asyncio.Event()
        run = await open_stream(connect, wait_ready=False)
        for i in range(3):
            await run.stream.push(bytes([i]) * 4)
        assert connect.sessions == []
        connect.gates[0].set()
        await run.until(lambda: connect.sessions and len(connect.sessions[0].audio) == 3)
        assert connect.sessions[0].audio == [bytes([i]) * 4 for i in range(3)]
        await run.stream.cancel()

    run_async(scenario())


def test_burst_of_audio_is_sent_without_pacing() -> None:
    async def scenario() -> None:
        connect = FakeConnect()
        run = await open_stream(connect)
        s = connect.sessions[0]
        chunk = bytes(32_000)  # one second of audio each
        started = time.monotonic()
        for _ in range(30):  # 30 s of audio
            await run.stream.push(chunk)
        assert time.monotonic() - started < 1.0
        assert len(s.audio) == 30
        assert all(a == chunk for a in s.audio)
        await run.stream.cancel()

    run_async(scenario())


# ── settle rule ───────────────────────────────────────────────────────


def test_streamed_audio_settles_after_quiet_grace() -> None:
    async def scenario() -> None:
        connect = FakeConnect()
        run = await open_stream(connect)
        await run.stream.push(CHUNK)
        started = time.monotonic()
        await run.stream.end_audio()
        await run.terminal()
        elapsed = run.times[-1] - started
        assert isinstance(run.events[-1], Closed)
        assert QUIET - 0.02 <= elapsed < BURST - 0.05
        assert connect.sessions[0].ended

    run_async(scenario())


def test_burst_audio_waits_the_longer_grace() -> None:
    async def scenario() -> None:
        connect = FakeConnect()
        connect.gates[0] = asyncio.Event()
        # stop_timeout must outlast burst_grace here (3 s vs 0.6 s in production).
        slow_stop = GeminiTimings(
            stop_timeout=1.5, stop_cap=3.0, quiet_grace=QUIET, burst_grace=BURST, connect_timeout=2.0
        )
        run = await open_stream(connect, slow_stop, wait_ready=False)
        await run.stream.push(CHUNK)  # queued: the whole audio goes out as one burst
        await run.stream.end_audio()
        connect.gates[0].set()
        await run.until(lambda: connect.sessions and connect.sessions[0].ended)
        ended_at = time.monotonic()
        await run.terminal()
        assert isinstance(run.events[-1], Closed)
        assert run.times[-1] - ended_at >= BURST - 0.05

    run_async(scenario())


def test_every_message_after_end_rearms_the_grace() -> None:
    async def scenario() -> None:
        connect = FakeConnect()
        run = await open_stream(connect)
        s = connect.sessions[0]
        await run.stream.push(CHUNK)
        started = time.monotonic()
        await run.stream.end_audio()
        await asyncio.sleep(QUIET * 0.7)
        s.final("late words")  # re-arms: must not close at started + QUIET
        await run.terminal()
        elapsed = run.times[-1] - started
        assert run.texts == [("Final", "late words")]
        assert elapsed >= QUIET * 0.7 + QUIET - 0.02

    run_async(scenario())


def test_first_final_after_end_does_not_stop_while_server_keeps_talking() -> None:
    async def scenario() -> None:
        connect = FakeConnect()
        run = await open_stream(connect)
        s = connect.sessions[0]
        await run.stream.push(CHUNK)
        await run.stream.end_audio()
        s.interim("one")
        s.final("one")
        s.interim("two")  # a second utterance follows the first final
        await asyncio.sleep(QUIET * 0.5)
        assert not any(isinstance(e, Closed) for e in run.events)
        s.final("two")
        await run.terminal()
        assert run.texts == [("Partial", "one"), ("Final", "one"), ("Partial", "two"), ("Final", "two")]
        assert isinstance(run.events[-1], Closed)

    run_async(scenario())


def test_open_voice_activity_blocks_quiet_settle_until_it_ends() -> None:
    async def scenario() -> None:
        connect = FakeConnect()
        run = await open_stream(connect)
        s = connect.sessions[0]
        await run.stream.push(CHUNK)
        await run.stream.end_audio()
        s.activity("ACTIVITY_START")
        await asyncio.sleep(QUIET * 2)
        assert not any(isinstance(e, Closed) for e in run.events)
        s.activity("ACTIVITY_END")
        await run.terminal()
        assert isinstance(run.events[-1], Closed)

    run_async(scenario())


def test_no_message_stop_forces_last_interim_into_a_final() -> None:
    async def scenario() -> None:
        connect = FakeConnect()
        run = await open_stream(connect)
        s = connect.sessions[0]
        await run.stream.push(CHUNK)
        s.interim("unconfirmed tail")
        await run.until(lambda: run.texts)
        started = time.monotonic()
        await run.stream.end_audio()
        await run.terminal()
        # Pending interim never goes quiet: only stop_timeout ends it.
        assert run.times[-1] - started >= FAST.stop_timeout - 0.05
        assert run.texts == [("Partial", "unconfirmed tail"), ("Final", "unconfirmed tail")]
        assert isinstance(run.events[-1], Closed)

    run_async(scenario())


def test_stop_cap_ends_a_server_that_never_stops_talking() -> None:
    async def scenario() -> None:
        connect = FakeConnect()
        run = await open_stream(connect)
        s = connect.sessions[0]
        await run.stream.push(CHUNK)
        started = time.monotonic()
        await run.stream.end_audio()

        async def chatter() -> None:
            n = 0
            while True:
                n += 1
                s.interim(f"word {n}")
                await asyncio.sleep(0.05)  # faster than stop_timeout: idle never fires

        talker = asyncio.create_task(chatter())
        try:
            await run.terminal()
        finally:
            talker.cancel()
        assert FAST.stop_cap - 0.05 <= run.times[-1] - started < FAST.stop_cap + 0.5
        assert isinstance(run.events[-2], Final)  # forced settle
        assert isinstance(run.events[-1], Closed)

    run_async(scenario())


# ── rotation ──────────────────────────────────────────────────────────


def test_rotation_at_lifetime_limit_queues_audio_and_delivers_old_final_first() -> None:
    async def scenario() -> None:
        connect = FakeConnect()
        connect.gates[1] = asyncio.Event()
        timings = GeminiTimings(
            stop_timeout=0.3, stop_cap=0.6, quiet_grace=QUIET, burst_grace=BURST,
            connect_timeout=2.0, rotate_after=0.15,
        )
        run = await open_stream(connect, timings)
        old = connect.sessions[0]
        await run.stream.push(CHUNK)
        # Rotation fires: new connection is opening (gated), old is told to end.
        await run.until(lambda: len(connect.calls) == 2 and old.ended)
        # Audio now goes to the new connection and queues until it is ready.
        await run.stream.push(b"\x01" * 8)
        old.interim("old tail")
        connect.gates[1].set()
        await run.until(lambda: len(connect.sessions) == 2)
        new = connect.sessions[1]
        await run.until(lambda: new.audio == [b"\x01" * 8])
        # The new connection speaks first; its text must wait behind the old final.
        new.interim("new words")
        new.final("new words")
        await asyncio.sleep(0.02)
        assert ("Partial", "new words") not in run.texts
        old.final("old tail")
        await run.until(lambda: ("Final", "new words") in run.texts)
        finals = [t for t in run.texts if t[0] == "Final"]
        assert finals == [("Final", "old tail"), ("Final", "new words")]
        assert run.texts.index(("Final", "old tail")) < run.texts.index(("Partial", "new words"))
        assert run.stream.rotations == 1
        assert sum(isinstance(e, Ready) for e in run.events) == 1
        await run.stream.end_audio()
        await run.terminal()
        assert isinstance(run.events[-1], Closed)
        assert new.ended

    run_async(scenario())


def test_go_away_rotates_to_a_new_connection() -> None:
    async def scenario() -> None:
        connect = FakeConnect()
        run = await open_stream(connect)
        old = connect.sessions[0]
        await run.stream.push(CHUNK)
        old.go_away()
        await run.until(lambda: len(connect.sessions) == 2 and old.ended)
        await run.stream.push(b"\x02" * 4)
        await run.until(lambda: connect.sessions[1].audio == [b"\x02" * 4])
        assert run.stream.rotations == 1
        assert old.audio == [CHUNK]
        await run.stream.cancel()

    run_async(scenario())


# ── failures ──────────────────────────────────────────────────────────


def _failed(run: Run) -> Failed:
    assert isinstance(run.events[-1], Failed)
    return run.events[-1]


def test_close_1007_before_setup_is_failed_bad_key() -> None:
    async def scenario() -> None:
        connect = FakeConnect()
        connect.enter_errors[0] = CloseError(1007, "API key not valid. Please pass a valid API key.")
        run = await open_stream(connect, wait_ready=False)
        await run.terminal()
        failure = _failed(run)
        assert (failure.code, failure.cause, failure.message) == ("failed", "auth", MSG_AUTH)
        assert not any(isinstance(e, Ready) for e in run.events)

    run_async(scenario())


@pytest.mark.parametrize(
    "code,reason",
    [
        (None, "RESOURCE_EXHAUSTED: You exceeded your current quota, project secret-project-42"),
        (429, "too many things"),
        (1011, "quota exceeded for model"),
    ],
)
def test_quota_closes_are_unavailable_without_raw_provider_text(code: int | None, reason: str) -> None:
    async def scenario() -> None:
        connect = FakeConnect()
        connect.enter_errors[0] = CloseError(code, reason)
        run = await open_stream(connect, wait_ready=False)
        await run.terminal()
        failure = _failed(run)
        assert (failure.code, failure.cause, failure.message) == ("unavailable", "quota", MSG_QUOTA)
        assert "secret-project" not in failure.message and "exceeded" not in failure.message

    run_async(scenario())


def test_unclassified_close_before_ready_is_unavailable_unreachable() -> None:
    async def scenario() -> None:
        connect = FakeConnect()
        connect.enter_errors[0] = CloseError(1006, "connection reset by peer 10.1.2.3")
        run = await open_stream(connect, wait_ready=False)
        await run.terminal()
        failure = _failed(run)
        assert (failure.code, failure.cause, failure.message) == ("unavailable", "unreachable", MSG_UNREACHABLE)
        assert "10.1.2.3" not in failure.message

    run_async(scenario())


def test_unexpected_close_mid_stream_is_failed_other() -> None:
    async def scenario() -> None:
        connect = FakeConnect()
        run = await open_stream(connect)
        await run.stream.push(CHUNK)
        connect.sessions[0].drop(1011, "internal error: stack trace foo.py:12")
        await run.terminal()
        failure = _failed(run)
        assert (failure.code, failure.cause, failure.message) == ("failed", "other", MSG_UNEXPECTED)
        assert "foo.py" not in failure.message

    run_async(scenario())


def test_close_after_audio_end_keeps_what_was_said_and_closes() -> None:
    async def scenario() -> None:
        connect = FakeConnect()
        run = await open_stream(connect)
        s = connect.sessions[0]
        await run.stream.push(CHUNK)
        await run.stream.end_audio()
        s.interim("partial words")
        s.drop(1000, "normal closure")
        await run.terminal()
        assert run.texts == [("Partial", "partial words"), ("Final", "partial words")]
        assert isinstance(run.events[-1], Closed)

    run_async(scenario())


# ── cancel ────────────────────────────────────────────────────────────


def test_cancel_emits_nothing_more() -> None:
    async def scenario() -> None:
        connect = FakeConnect()
        run = await open_stream(connect)
        s = connect.sessions[0]
        await run.stream.push(CHUNK)
        s.interim("before cancel")
        await run.until(lambda: run.texts)
        await run.stream.cancel()
        await run.terminal()
        seen = list(run.events)
        s.interim("after cancel")
        s.final("after cancel")
        await run.stream.push(CHUNK)
        await run.stream.end_audio()
        await run.stream.cancel()  # idempotent
        await asyncio.sleep(QUIET * 2)
        assert run.events == seen
        assert not any(isinstance(e, (Closed, Failed)) for e in run.events)
        assert s.audio == [CHUNK]  # nothing sent after cancel

    run_async(scenario())


def test_cancel_before_setup_completes_emits_nothing() -> None:
    async def scenario() -> None:
        connect = FakeConnect()
        connect.gates[0] = asyncio.Event()
        run = await open_stream(connect, wait_ready=False)
        await run.stream.cancel()
        connect.gates[0].set()
        await run.terminal()
        await asyncio.sleep(0.05)
        assert run.events == []

    run_async(scenario())


# ── probe ─────────────────────────────────────────────────────────────


def test_probe_outcomes() -> None:
    async def scenario() -> None:
        ok = FakeConnect()
        assert await GeminiLiveAdapter(connect=ok, timings=FAST).probe(api_key=KEY) == "ok"

        bad = FakeConnect()
        bad.enter_errors[0] = CloseError(1007, "API key not valid")
        assert await GeminiLiveAdapter(connect=bad, timings=FAST).probe(api_key=KEY) == "invalid_key"

        quota = FakeConnect()
        quota.enter_errors[0] = CloseError(429, "RESOURCE_EXHAUSTED")
        assert await GeminiLiveAdapter(connect=quota, timings=FAST).probe(api_key=KEY) == "quota"

    run_async(scenario())


def test_open_rejects_non_16khz_audio() -> None:
    async def scenario() -> None:
        with pytest.raises(ValueError):
            await GeminiLiveAdapter(connect=FakeConnect(), timings=FAST).open(
                api_key=KEY, locale="en-US", sample_rate=48_000
            )

    run_async(scenario())


# ── secrecy ───────────────────────────────────────────────────────────


def test_api_key_never_appears_in_logs_or_failure_messages() -> None:
    # Handler attached straight to the adapter's logger: caplog depends on
    # propagation, which the app's logging config may turn off (run-order trap).
    records: list[logging.LogRecord] = []

    class Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    adapter_logger = logging.getLogger("app.services.voice_transcription.providers.gemini_live")
    handler = Collect(level=logging.DEBUG)
    old_level, old_disabled = adapter_logger.level, adapter_logger.disabled
    adapter_logger.setLevel(logging.DEBUG)
    adapter_logger.disabled = False
    adapter_logger.addHandler(handler)

    async def scenario() -> None:
        # Provider text that quotes the key (it rides in the socket URL).
        connect = FakeConnect()
        connect.enter_errors[0] = CloseError(
            1008, f"policy violation wss://x/ws?key={KEY} while handling request"
        )
        run = await open_stream(connect, wait_ready=False)
        await run.terminal()
        assert KEY not in repr(run.events)

        # A mid-stream close that quotes the key, and a failing send.
        connect2 = FakeConnect()
        run2 = await open_stream(connect2)
        await run2.stream.push(CHUNK)
        connect2.sessions[0].drop(1011, f"boom key={KEY}")
        await run2.terminal()
        assert KEY not in repr(run2.events)

        # A clean run, including the stream-finished log line.
        connect3 = FakeConnect()
        run3 = await open_stream(connect3)
        await run3.stream.push(CHUNK)
        await run3.stream.end_audio()
        await run3.terminal()

    try:
        run_async(scenario())
    finally:
        adapter_logger.removeHandler(handler)
        adapter_logger.setLevel(old_level)
        adapter_logger.disabled = old_disabled
    text = "\n".join(r.getMessage() for r in records)
    assert records, "expected the adapter to log something"
    assert KEY not in text
    assert "SECRET-KEY" not in text


# ── task hygiene ──────────────────────────────────────────────────────


def _leftover_tasks(*ignore: asyncio.Task[Any]) -> list[asyncio.Task[Any]]:
    skip = {asyncio.current_task(), *ignore}
    # Only the adapter's own tasks; the loop's async-generator finalizers are not its.
    return [
        t for t in asyncio.all_tasks()
        if t not in skip and not t.done() and "gemini_live" in repr(t.get_coro())
    ]


def test_cancel_after_natural_end_and_probe_leave_no_pending_tasks() -> None:
    async def scenario() -> None:
        # Natural Closed, then cancel(): connection + audio_stream_end tasks are gone.
        connect = FakeConnect()
        run = await open_stream(connect)
        await run.stream.push(CHUNK)
        await run.stream.end_audio()
        await run.terminal()
        assert isinstance(run.events[-1], Closed)
        await run.stream.cancel()
        await asyncio.sleep(0)
        assert _leftover_tasks() == []

        # Natural Failed, then cancel().
        connect2 = FakeConnect()
        run2 = await open_stream(connect2)
        await run2.stream.push(CHUNK)
        connect2.sessions[0].drop(1011, "boom")
        await run2.terminal()
        assert isinstance(run2.events[-1], Failed)
        await run2.stream.cancel()
        await asyncio.sleep(0)
        assert _leftover_tasks() == []

        # probe() on success and on failure.
        for errors in ({}, {0: CloseError(1007, "API key not valid")}):
            probe_connect = FakeConnect()
            probe_connect.enter_errors.update(errors)
            await GeminiLiveAdapter(connect=probe_connect, timings=FAST).probe(api_key=KEY)
            await asyncio.sleep(0)
            assert _leftover_tasks() == []

    run_async(scenario())
