"""Gemini Live transcription adapter.

An asyncio translation of the desktop engine ``openGeminiStream``
(``cinna-desktop/src/main/speech/engines/geminiLive.ts``), measured against
``gemini-3.5-transcribe-live``:

- ``interim_input_transcription.text`` is cumulative within the current
  utterance (replace, may shrink); ``input_transcription.text`` is one
  utterance's final only.
- After ``audio_stream_end`` the socket stays open. A connection is settled
  once it has been quiet (no pending interim, no open voice activity) for
  ``quiet_grace`` (``burst_grace`` when its audio went out as one burst at
  setup); every message re-arms that. Hard stops: ``stop_timeout`` with no
  message, ``stop_cap`` after the end in any case. A forced settle turns the
  last interim into a final.
- A connection lives about 10 minutes: at ``rotate_after`` or on ``go_away``
  a new one opens, audio queues on it until ready, the old one is ended, and
  the new one's events are held until the old one's last final is out.
- Input audio is never paced: a replay burst passes straight through.

Nothing provider-specific reaches the client: failure messages are fixed
sentences, and provider reasons go to the log only, scrubbed of the key and
clipped to 300 characters. Transcript text and audio are never logged.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import AbstractAsyncContextManager, aclosing, asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.models.credentials.ai_credential import AICredentialType
from app.services.voice_transcription.constants import PCM_BYTES_PER_SECOND
from app.services.voice_transcription.providers.base import (
    Closed,
    Failed,
    Final,
    Partial,
    ProbeOutcome,
    Ready,
    SpeechEvent,
    SpeechFailureCause,
    SpeechFailureCode,
)

logger = logging.getLogger(__name__)

GEMINI_TRANSCRIBE_MODEL = "gemini-3.5-transcribe-live"
GEMINI_API_VERSION = "v1beta"
AUDIO_MIME = "audio/pcm;rate=16000"
SAMPLE_RATE = 16_000

# Close code Gemini uses for a bad API key ("API key not valid ..."), before setup.
CLOSE_CODE_INVALID_KEY = 1007

AUTH_REASON = re.compile(
    r"api[ _-]?key|unauthenticated|unauthori[sz]ed|permission[ _-]?denied|"
    r"invalid authentication|credential",
    re.IGNORECASE,
)
QUOTA_REASON = re.compile(
    r"quota|resource[ _-]?exhausted|\b429\b|rate[ _-]?limit|too many requests",
    re.IGNORECASE,
)

MSG_AUTH = (
    "Voice transcription is not available: the server's speech provider "
    "rejected its credentials. Contact your administrator."
)
MSG_QUOTA = (
    "Voice transcription is temporarily unavailable: the server's speech "
    "quota is used up. Contact your administrator."
)
MSG_UNREACHABLE = "Couldn't reach the speech service. Try again later."
MSG_UNEXPECTED = "Voice transcription stopped unexpectedly."

SCRUB_MAX_CHARS = 300
# Probe deadline = connect_timeout + stop_cap + this margin (the worst valid path).
PROBE_TIMEOUT_MARGIN_SECONDS = 1.0


class LiveSessionLike(Protocol):
    """The part of ``google.genai.live.AsyncSession`` this adapter uses."""

    async def send_realtime_input(
        self, *, audio: Any = None, audio_stream_end: bool | None = None
    ) -> None: ...

    def receive(self) -> AsyncIterator[Any]: ...


# Opens one Live connection; entering it returns once setup is complete
# (the SDK awaits the setup reply inside ``connect``).
LiveConnect = Callable[[str, str], AbstractAsyncContextManager[LiveSessionLike]]


@dataclass(frozen=True)
class GeminiTimings:
    """Seconds; defaults are the desktop engine's ``DEFAULT_DEPS``."""
    stop_timeout: float = 3.0
    stop_cap: float = 10.0
    quiet_grace: float = 0.3
    burst_grace: float = 0.6
    connect_timeout: float = 10.0
    rotate_after: float = 570.0


@asynccontextmanager
async def _connect_gemini_live(api_key: str, locale: str) -> AsyncIterator[LiveSessionLike]:
    from google import genai
    from google.genai import types

    client = genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(api_version=GEMINI_API_VERSION),
    )
    config = types.LiveConnectConfig(
        response_modalities=[types.Modality.TEXT],
        input_audio_transcription=types.AudioTranscriptionConfig(language_codes=[locale]),
    )
    try:
        async with client.aio.live.connect(model=GEMINI_TRANSCRIBE_MODEL, config=config) as session:
            yield session
    finally:
        try:
            await client.aio.aclose()
        except Exception:  # noqa: BLE001 - best-effort cleanup
            pass


def _audio_blob(pcm: bytes) -> Any:
    from google.genai import types

    return types.Blob(data=pcm, mime_type=AUDIO_MIME)


def _close_details(err: BaseException) -> tuple[int | None, str]:
    """(close code, reason) from whatever ended a connection."""
    code = getattr(err, "code", None)
    if not isinstance(code, int):
        rcvd = getattr(err, "rcvd", None)  # websockets.ConnectionClosed
        code = getattr(rcvd, "code", None)
    return (code if isinstance(code, int) else None), str(err)


def _value(raw: Any) -> Any:
    """Enum value or the raw value (fake sessions may pass plain strings)."""
    return getattr(raw, "value", raw)


@dataclass(eq=False)
class _Conn:
    """One Live connection (``newConnection`` in the TS engine)."""
    n: int
    session: LiveSessionLike | None = None
    # Setup complete: audio may be sent.
    ready: bool = False
    # Audio pushed before ``ready``.
    queue: list[bytes] = field(default_factory=list)
    # Asked to end (stop or rotation); ``audio_stream_end`` goes out once ready.
    ending: bool = False
    end_sent: bool = False
    # Audio was streaming live when the end was asked: "nothing pending" can be trusted.
    streamed_before_end: bool = False
    # Said everything it will say; its socket is closing / closed.
    done: bool = False
    closed_by_us: bool = False
    # The current utterance's interim, since the last final.
    pending: str = ""
    # Between ACTIVITY_START and ACTIVITY_END.
    speaking: bool = False
    timed_out: bool = False
    timers: list[asyncio.TimerHandle] = field(default_factory=list)
    grace: asyncio.TimerHandle | None = None
    idle: asyncio.TimerHandle | None = None
    task: asyncio.Task[None] | None = None
    # Serialises sends so queued audio, live audio and the end keep their order.
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


_END = object()


class _GeminiStream:
    """One dictation over one or more Live connections."""

    def __init__(
        self,
        *,
        api_key: str,
        locale: str,
        connect: LiveConnect,
        timings: GeminiTimings,
    ) -> None:
        self._api_key: str | None = api_key
        # Kept for scrubbing only: late log lines (an in-flight send failing
        # after ``_finish`` cleared ``_api_key``) must still have the key removed.
        self._scrub_key = api_key
        self._locale = locale
        self._connect_fn = connect
        self._t = timings
        self._loop = asyncio.get_running_loop()
        self._queue: asyncio.Queue[Any] = asyncio.Queue()
        self._held: list[SpeechEvent] = []
        self._tasks: set[asyncio.Task[Any]] = set()
        self._count = 0
        self._opened = time.monotonic()
        self._ended = False
        self._stopping = False
        self._bytes = 0
        self._partials = 0
        self._finals = 0
        self.rotations = 0
        self._draining: _Conn | None = None
        self._current = self._new_connection()

    # ── public (SpeechStream) ──────────────────────────────────────────

    def start(self) -> None:
        self._connect(self._current)

    async def push(self, pcm: bytes) -> None:
        if self._ended or self._stopping or not pcm:
            return
        self._bytes += len(pcm)
        c = self._current
        if c.ready and c.session is not None and not c.done:
            await self._send(c, audio=_audio_blob(pcm))
        else:
            c.queue.append(bytes(pcm))

    async def end_audio(self) -> None:
        if self._ended or self._stopping:
            return
        self._stopping = True
        self._end_connection(self._current)

    async def cancel(self) -> None:
        """Stop producing events, then wait for every connection and send task
        to wind down (sockets closed, clients closed), also after a natural end."""
        if not self._ended:
            self._finish("cancelled", None)
        current = asyncio.current_task()
        tasks = [t for t in self._tasks if not t.done() and t is not current]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def events(self) -> AsyncIterator[SpeechEvent]:
        while True:
            item = await self._queue.get()
            if item is _END:
                return
            yield item

    # ── connections ────────────────────────────────────────────────────

    def _new_connection(self) -> _Conn:
        self._count += 1
        return _Conn(n=self._count)

    def _spawn(self, coro: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
        task = self._loop.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _later(self, c: _Conn, delay: float, run: Callable[[], None]) -> None:
        c.timers.append(self._loop.call_later(delay, run))

    def _connect(self, c: _Conn) -> None:
        if self._api_key is None:
            return

        def setup_timed_out() -> None:
            if c.ready or c.done or self._ended:
                return
            logger.warning(
                "gemini live did not set up in time (connection=%s timeout_s=%s)",
                c.n, self._t.connect_timeout,
            )
            self._fail("unavailable", MSG_UNREACHABLE, "unreachable")

        def age_out() -> None:
            if c is self._current:
                self._rotate("age")

        self._later(c, self._t.connect_timeout, setup_timed_out)
        self._later(c, self._t.rotate_after, age_out)
        c.task = self._spawn(self._run_connection(c, self._api_key))

    async def _run_connection(self, c: _Conn, api_key: str) -> None:
        try:
            async with self._connect_fn(api_key, self._locale) as session:
                if c.done or self._ended:
                    return
                c.session = session
                c.ready = True
                if c.n == 1:
                    self._emit(Ready())
                await self._flush(c)
                # ``receive()`` stops after each interaction completes; re-enter it.
                while not c.done:
                    async for message in session.receive():
                        self._on_message(c, message)
                        if c.done:
                            break
                    await asyncio.sleep(0)
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001 - every close is classified below
            code, reason = _close_details(err)
            self._on_close(c, code, reason)

    async def _send(self, c: _Conn, **payload: Any) -> None:
        async with c.send_lock:
            session = c.session
            if session is None or c.done:
                return
            try:
                await session.send_realtime_input(**payload)
            except Exception as err:  # noqa: BLE001
                # The socket is going away; the receive loop decides what that means.
                logger.debug(
                    "gemini live send failed (connection=%s): %s", c.n, self._scrub(str(err))
                )

    async def _flush(self, c: _Conn) -> None:
        if not c.ready or c.session is None or c.done:
            return
        async with c.send_lock:
            while c.queue and not c.done and c.session is not None:
                pcm = c.queue.pop(0)
                try:
                    await c.session.send_realtime_input(audio=_audio_blob(pcm))
                except Exception as err:  # noqa: BLE001
                    logger.debug(
                        "gemini live send failed (connection=%s): %s", c.n, self._scrub(str(err))
                    )
        self._send_end(c)

    def _send_end(self, c: _Conn) -> None:
        if not c.ending or c.end_sent or not c.ready or c.session is None or c.done:
            return
        c.end_sent = True
        self._spawn(self._send(c, audio_stream_end=True))
        self._later(
            c,
            self._t.stop_cap,
            lambda: self._time_out(c, "gemini live did not finish after the audio ended", self._t.stop_cap),
        )
        self._rearm(c)

    def _end_connection(self, c: _Conn) -> None:
        if c.ending or c.done:
            return
        c.ending = True
        c.streamed_before_end = c.ready and c.session is not None and not c.queue
        self._send_end(c)

    def _rotate(self, why: str) -> None:
        if self._ended or self._stopping or self._draining is not None or not self._current.ready:
            return
        old = self._current
        self._current = self._new_connection()
        self._draining = old
        self.rotations += 1
        logger.info(
            "gemini live connection rotating (why=%s from=%s to=%s)", why, old.n, self._current.n
        )
        self._connect(self._current)
        self._end_connection(old)

    def _shut(self, c: _Conn) -> None:
        c.done = True
        for handle in c.timers:
            handle.cancel()
        c.timers.clear()
        for pending in (c.grace, c.idle):
            if pending is not None:
                pending.cancel()
        c.grace = c.idle = None
        c.queue.clear()
        if not c.closed_by_us:
            c.closed_by_us = True
            # Leaving the connection task's ``async with`` closes the socket.
            # From inside that task the receive loop sees ``done`` and exits.
            if c.task is not None and c.task is not asyncio.current_task() and not c.task.done():
                c.task.cancel()

    # ── settling ───────────────────────────────────────────────────────

    @staticmethod
    def _quiet(c: _Conn) -> bool:
        return not c.pending and not c.speaking

    def _rearm(self, c: _Conn) -> None:
        """After the end, any message means the server is still talking."""
        if not c.end_sent or c.done:
            return
        for timer in (c.grace, c.idle):
            if timer is not None:
                timer.cancel()
        grace = self._t.quiet_grace if c.streamed_before_end else self._t.burst_grace

        def grace_over() -> None:
            c.grace = None
            if self._quiet(c):
                self._settle(c)

        c.grace = self._loop.call_later(grace, grace_over)
        c.idle = self._loop.call_later(
            self._t.stop_timeout,
            lambda: self._time_out(c, "gemini live went silent after the audio ended", self._t.stop_timeout),
        )

    def _time_out(self, c: _Conn, why: str, timeout: float) -> None:
        if c.done:
            return
        logger.warning("%s (connection=%s timeout_s=%s)", why, c.n, timeout)
        c.timed_out = True
        self._settle(c)

    def _settle(self, c: _Conn) -> None:
        """The connection has said all it will: its unconfirmed text becomes its last final."""
        if c.done:
            return
        tail = c.pending.strip()
        c.pending = ""
        if tail:
            self._route(c, Final(tail))
        self._shut(c)
        if c is self._draining:
            self._draining = None
            held, self._held = self._held, []
            for event in held:
                self._emit(event)
        if self._stopping and self._draining is None and self._current.done:
            self._finish("stop-timeout" if self._current.timed_out else "stop", Closed())

    # ── events ─────────────────────────────────────────────────────────

    def _emit(self, event: SpeechEvent) -> None:
        if self._ended:
            return
        if isinstance(event, Partial):
            self._partials += 1
        elif isinstance(event, Final):
            self._finals += 1
        self._queue.put_nowait(event)

    def _route(self, c: _Conn, event: SpeechEvent) -> None:
        """The old connection speaks first; the new one's words wait behind it."""
        if self._ended or c.done:
            return
        if c is self._draining:
            self._emit(event)
        elif c is self._current:
            if self._draining is not None:
                self._held.append(event)
            else:
                self._emit(event)

    def _on_message(self, c: _Conn, message: Any) -> None:
        if self._ended or c.done:
            return
        content = getattr(message, "server_content", None)
        interim_obj = getattr(content, "interim_input_transcription", None)
        interim = getattr(interim_obj, "text", None)
        if isinstance(interim, str):
            c.pending = interim
            self._route(c, Partial(interim))
        final_obj = getattr(content, "input_transcription", None)
        final = getattr(final_obj, "text", None)
        if isinstance(final, str):
            c.pending = ""
            text = final.strip()
            if text:
                self._route(c, Final(text))
        activity_obj = getattr(message, "voice_activity", None)
        activity = _value(getattr(activity_obj, "voice_activity_type", None))
        if activity == "ACTIVITY_START":
            c.speaking = True
        elif activity == "ACTIVITY_END":
            c.speaking = False
        if isinstance(interim, str) or isinstance(final, str) or activity:
            self._rearm(c)
        if getattr(message, "go_away", None) is not None and c is self._current:
            self._rotate("go_away")

    def _on_close(self, c: _Conn, code: int | None, raw_reason: str) -> None:
        if c.closed_by_us or c.done or self._ended:
            return
        c.session = None
        # Closed after it was told to end, or while handing over: keep what it said.
        if c.end_sent or c is self._draining:
            self._settle(c)
            return
        reason = self._scrub(raw_reason).strip()
        logger.warning(
            "gemini live connection closed (connection=%s code=%s ready=%s): %s",
            c.n, code, c.ready, reason,
        )
        if code == CLOSE_CODE_INVALID_KEY or AUTH_REASON.search(reason):
            self._fail("failed", MSG_AUTH, "auth")
        elif code == 429 or QUOTA_REASON.search(reason):
            self._fail("unavailable", MSG_QUOTA, "quota")
        elif not c.ready:
            self._fail("unavailable", MSG_UNREACHABLE, "unreachable")
        else:
            self._fail("failed", MSG_UNEXPECTED, "other")

    def _fail(self, code: SpeechFailureCode, message: str, cause: SpeechFailureCause) -> None:
        self._finish(f"error:{code}:{cause}", Failed(code=code, message=message, cause=cause))

    def _finish(self, outcome: str, terminal: SpeechEvent | None) -> None:
        if self._ended:
            return
        self._ended = True
        self._shut(self._current)
        if self._draining is not None:
            self._shut(self._draining)
        self._held.clear()
        self._api_key = None
        logger.info(
            "gemini live stream finished (outcome=%s seconds=%.2f duration_s=%.2f "
            "partials=%s finals=%s rotations=%s)",
            outcome,
            self._bytes / PCM_BYTES_PER_SECOND,
            time.monotonic() - self._opened,
            self._partials,
            self._finals,
            self.rotations,
        )
        if terminal is not None:
            self._queue.put_nowait(terminal)
        self._queue.put_nowait(_END)

    def _scrub(self, text: str) -> str:
        """Provider text may quote the key; strip it before the text goes anywhere."""
        scrubbed = text.replace(self._scrub_key, "[key]") if self._scrub_key else text
        return scrubbed[:SCRUB_MAX_CHARS]


class GeminiLiveAdapter:
    """Speech adapter for Google AI providers over Gemini Live."""

    provider_type: str = AICredentialType.GOOGLE.value
    model: str = GEMINI_TRANSCRIBE_MODEL

    def __init__(
        self,
        *,
        connect: LiveConnect | None = None,
        timings: GeminiTimings | None = None,
    ) -> None:
        self._connect = connect or _connect_gemini_live
        self._timings = timings or GeminiTimings()

    async def open(self, *, api_key: str, locale: str, sample_rate: int) -> _GeminiStream:
        if sample_rate != SAMPLE_RATE:
            raise ValueError(f"Gemini Live transcription needs {SAMPLE_RATE} Hz PCM")
        stream = _GeminiStream(
            api_key=api_key, locale=locale, connect=self._connect, timings=self._timings
        )
        stream.start()
        return stream

    async def probe(self, *, api_key: str) -> ProbeOutcome:
        """One second of silence through a real session, then classify the end."""
        try:
            stream = await self.open(api_key=api_key, locale="en-US", sample_rate=SAMPLE_RATE)
        except Exception:  # noqa: BLE001
            return "unreachable"
        try:
            deadline = (
                self._timings.connect_timeout
                + self._timings.stop_cap
                + PROBE_TIMEOUT_MARGIN_SECONDS
            )
            async with asyncio.timeout(deadline):
                await stream.push(bytes(PCM_BYTES_PER_SECOND))
                await stream.end_audio()
                # ``aclosing``: returning mid-iteration must not leave the
                # generator to a loop finalizer task.
                async with aclosing(stream.events()) as events:
                    async for event in events:
                        if isinstance(event, Closed):
                            return "ok"
                        if isinstance(event, Failed):
                            if event.cause == "auth":
                                return "invalid_key"
                            if event.cause == "quota":
                                return "quota"
                            return "unreachable"
        except TimeoutError:
            pass
        finally:
            await stream.cancel()
        return "unreachable"
