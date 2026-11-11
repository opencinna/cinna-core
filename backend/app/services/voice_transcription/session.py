"""One voice transcription relay session over one WebSocket (protocol v1).

Lifecycle: accept, read and check the ``start`` frame, refuse early (rate,
config, one-per-user, monthly cap), open the provider stream, then run three
tasks until exactly one terminal event has gone out:

* **reader** — client frames: PCM is pushed to the adapter immediately, never
  paced (a replay burst passes straight through); ``stop`` ends the audio;
  ``cancel`` drops everything; anything else is ``bad_request``;
* **pump** — adapter events to the client (``start``, ``partial``, ``final``,
  then ``stop`` / ``error``);
* **watchdog** — idle timeout (ends like ``stop``), periodic revocation
  re-check (``unauthorized``) and a ready backstop.

The session cap and the monthly cap both end the audio like a client ``stop``
so the finals already spoken are delivered; the cap then reports
``quota_exceeded`` instead of ``stop``. A ledger row is written for every
session that sent a valid ``start``, whatever the outcome.

Privacy: nothing here logs or stores transcript text, audio, the provider key,
the access token or the URL. One metadata log line per session.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from fastapi import WebSocket

from app.core import db as core_db
from app.core.config import settings
from app.models.voice_transcription.usage import (
    VOICE_CLIENT_KIND_DESKTOP,
    VOICE_CLIENT_KIND_MOBILE,
    VOICE_CLIENT_KIND_WEB,
    VoiceTranscriptionUsage,
)
from app.services.voice_transcription import tracker
from app.services.voice_transcription.config_service import (
    VoiceTranscriptionConfigService,
)
from app.services.voice_transcription.constants import (
    BACKEND_NAME,
    MAX_FRAME_BYTES,
    PCM_BYTES_PER_SECOND,
    PROTOCOL_VERSION,
)
from app.services.voice_transcription.providers.base import (
    Closed,
    Failed,
    Final,
    Partial,
    Ready,
    SpeechStream,
)
from app.services.voice_transcription.providers.registry import get_adapter
from app.services.voice_transcription.usage_service import (
    VoiceTranscriptionUsageService,
    cap_seconds,
)

logger = logging.getLogger(__name__)

WS_CLOSE_NORMAL = 1000
# Consecutive erroring revocation re-checks that end the session as ``unauthorized``.
MAX_RECHECK_FAILURES = 2
REQUIRED_SAMPLE_RATE = 16_000
MAX_LOCALE_CHARS = 35
_LOCALE_RE = re.compile(r"^[A-Za-z]{2,8}(-[A-Za-z0-9]{1,8})*$")

# User-facing sentences per error code (``message`` of the ``error`` event).
MSG_DISABLED = "Voice transcription isn't available on this server."
MSG_QUOTA = "You've used all of your voice transcription minutes for this month."
MSG_BUSY = "You already have a voice transcription session running."
MSG_RATE_LIMITED = (
    "Too many voice transcription sessions were started in the last minute. "
    "Try again shortly."
)
MSG_UNAVAILABLE = "Couldn't reach the speech service. Try again later."
MSG_FAILED = "Voice transcription stopped unexpectedly."
MSG_UNAUTHORIZED = "This device was signed out. Sign in again to keep dictating."
MSG_BAD_REQUEST = "The voice transcription session received an unexpected message."


@dataclass(frozen=True)
class StartFrame:
    locale: str
    sample_rate: int
    replay_seconds: float


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def parse_start(text: str | None) -> StartFrame | None:
    """The client's ``start`` frame, or ``None`` when it is off-contract."""
    if text is None:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("type") != "start":
        return None
    version = data.get("version")
    locale = data.get("locale")
    sample_rate = data.get("sample_rate")
    replay = data.get("replay_seconds")
    if not _is_number(version) or version != PROTOCOL_VERSION:
        return None
    if (
        not isinstance(locale, str)
        or len(locale) > MAX_LOCALE_CHARS
        or not _LOCALE_RE.match(locale)
    ):
        return None
    if not _is_number(sample_rate) or sample_rate != REQUIRED_SAMPLE_RATE:
        return None
    if not _is_number(replay) or not math.isfinite(replay) or replay < 0:
        return None
    return StartFrame(
        locale=locale, sample_rate=REQUIRED_SAMPLE_RATE, replay_seconds=float(replay)
    )


def _error(code: str, message: str) -> dict[str, Any]:
    return {"type": "error", "code": code, "message": message, "recoverable": False}


def _ledger_client_kind(client_kind: str | None) -> str:
    if client_kind == VOICE_CLIENT_KIND_DESKTOP:
        return VOICE_CLIENT_KIND_DESKTOP
    if client_kind == VOICE_CLIENT_KIND_MOBILE:
        return VOICE_CLIENT_KIND_MOBILE
    return VOICE_CLIENT_KIND_WEB


class VoiceSession:
    """One relay session. Construct per socket and ``await run()``."""

    def __init__(
        self,
        websocket: WebSocket,
        *,
        user_id: uuid.UUID,
        client_kind: str | None,
        recheck: Callable[[], bool],
    ) -> None:
        self._ws = websocket
        self._user_id = user_id
        self._client_kind = client_kind
        self._recheck = recheck
        self.session_id = uuid.uuid4()
        self.outcome: str | None = None

        self._ended = False
        self._done = asyncio.Event()
        self._send_lock = asyncio.Lock()
        self._stream: SpeechStream | None = None
        self._acquired = False

        # Ledger / log metadata.
        self._started_at: datetime | None = None
        self._replay_declared = 0.0
        self._provider_id: uuid.UUID | None = None
        self._provider_type: str | None = None
        self._model: str | None = None
        self._bytes = 0
        self._partials = 0
        self._finals: list[str] = []
        self._first_partial_ms: int | None = None

        # Runtime state.
        self._ready = False
        self._remaining: float | None = None
        self._quota_bytes: int | None = None
        self._max_bytes = int(settings.VOICE_TRANSCRIPTION_MAX_SESSION_SECONDS * PCM_BYTES_PER_SECOND)
        self._audio_closed = False
        # What a provider ``Closed`` turns into: ``stop`` or ``quota_exceeded``.
        self._end_reason = "stop"
        self._opened_at = time.monotonic()
        self._last_audio = self._opened_at
        # Consecutive re-checks that errored (not "revoked"); see _still_authorized.
        self._recheck_failures = 0

    # ── entry ──────────────────────────────────────────────────────────

    async def run(self) -> None:
        await self._ws.accept()
        start = await self._read_start()
        if start is None:
            return
        self._started_at = datetime.now(UTC)
        self._replay_declared = start.replay_seconds
        try:
            await self._serve(start)
        except Exception as exc:  # noqa: BLE001 — the client still gets one terminal event
            logger.error(
                "Voice transcription session %s crashed (%s)",
                self.session_id,
                type(exc).__name__,
            )
            await self._finish("failed", _error("failed", MSG_FAILED))
        finally:
            await self._teardown()

    async def _read_start(self) -> StartFrame | None:
        """Read the first frame; ``bad_request`` and close when it is not a
        valid ``start`` (no ledger row: the session never started)."""
        try:
            message = await asyncio.wait_for(
                self._ws.receive(),
                timeout=settings.VOICE_TRANSCRIPTION_START_FRAME_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            message = None
        if message is not None and message["type"] == "websocket.disconnect":
            self._ended = True
            return None
        start = parse_start(message.get("text")) if message is not None else None
        if start is None:
            await self._finish("bad_request", _error("bad_request", MSG_BAD_REQUEST))
        return start

    async def _serve(self, start: StartFrame) -> None:
        user_id = self._user_id
        if (
            tracker.open_rate_limiter.check(
                f"speech:{user_id}", settings.VOICE_TRANSCRIPTION_OPEN_RATE_LIMIT_PER_MIN
            )
            is not None
        ):
            await self._finish("busy", _error("busy", MSG_RATE_LIMITED))
            return

        api_key: str | None = None
        with core_db.create_session() as db:
            resolved = VoiceTranscriptionConfigService.resolve(db)
            if resolved is not None:
                api_key = VoiceTranscriptionConfigService.load_api_key(db, resolved.provider_id)
                used = VoiceTranscriptionUsageService.month_seconds(db, user_id)
        adapter = get_adapter(resolved.provider_type) if resolved is not None else None
        if resolved is None or adapter is None or api_key is None:
            await self._finish("disabled", _error("disabled", MSG_DISABLED))
            return
        self._provider_id = resolved.provider_id
        self._provider_type = resolved.provider_type
        self._model = adapter.model

        if not tracker.try_acquire(
            user_id, self.session_id, settings.VOICE_TRANSCRIPTION_MAX_SESSIONS_PER_USER
        ):
            await self._finish("busy", _error("busy", MSG_BUSY))
            return
        self._acquired = True

        cap = cap_seconds(resolved.cap_minutes)
        if cap is not None:
            self._remaining = cap - used
            if self._remaining <= 0:
                await self._finish("quota_exceeded", _error("quota_exceeded", MSG_QUOTA))
                return
            self._quota_bytes = int(self._remaining * PCM_BYTES_PER_SECOND)

        self._opened_at = time.monotonic()
        self._last_audio = self._opened_at
        try:
            self._stream = await adapter.open(
                api_key=api_key, locale=start.locale, sample_rate=start.sample_rate
            )
        except Exception as exc:  # noqa: BLE001 — never echo provider text
            logger.warning(
                "Voice transcription session %s: provider open failed (%s)",
                self.session_id,
                type(exc).__name__,
            )
            await self._finish("unavailable", _error("unavailable", MSG_UNAVAILABLE))
            return
        finally:
            # The key lives only for the adapter call.
            api_key = None

        tasks = [
            asyncio.create_task(self._guard(self._read_client())),
            asyncio.create_task(self._guard(self._pump())),
            asyncio.create_task(self._guard(self._watch())),
        ]
        try:
            await self._done.wait()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _guard(self, body: Any) -> None:
        """Run one session task; an unexpected error still ends the session."""
        try:
            await body
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Voice transcription session %s task failed (%s)",
                self.session_id,
                type(exc).__name__,
            )
            await self._finish("failed", _error("failed", MSG_FAILED))

    # ── client → server ────────────────────────────────────────────────

    async def _read_client(self) -> None:
        while not self._ended:
            message = await self._ws.receive()
            if message["type"] == "websocket.disconnect":
                # Gone without a word: same as ``cancel``.
                await self._finish("cancelled", None)
                return
            data = message.get("bytes")
            text = message.get("text")
            if data is not None:
                await self._on_audio(data)
            elif text is not None:
                await self._on_text(text)

    async def _on_audio(self, data: bytes) -> None:
        if len(data) > MAX_FRAME_BYTES:
            await self._finish("bad_request", _error("bad_request", MSG_BAD_REQUEST))
            return
        if self._audio_closed or self._stream is None:
            # Audio still in flight after a stop / cap: dropped, not an error.
            return
        self._last_audio = time.monotonic()
        room = self._max_bytes - self._bytes
        if self._quota_bytes is not None:
            room = min(room, self._quota_bytes - self._bytes)
        chunk = data[: max(room, 0)]
        if chunk:
            self._bytes += len(chunk)
            await self._stream.push(chunk)
        if self._quota_bytes is not None and self._bytes >= self._quota_bytes:
            await self._end_audio("quota_exceeded")
        elif self._bytes >= self._max_bytes:
            await self._end_audio("stop")

    async def _on_text(self, text: str) -> None:
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        kind = data.get("type") if isinstance(data, dict) else None
        if kind == "stop":
            await self._end_audio("stop")
        elif kind == "cancel":
            await self._finish("cancelled", None)
        else:
            await self._finish("bad_request", _error("bad_request", MSG_BAD_REQUEST))

    async def _end_audio(self, reason: str) -> None:
        """No more audio; the pump turns the adapter's ``Closed`` into ``reason``."""
        if self._audio_closed or self._stream is None:
            return
        self._audio_closed = True
        self._end_reason = reason
        await self._stream.end_audio()

    # ── provider → client ──────────────────────────────────────────────

    async def _pump(self) -> None:
        assert self._stream is not None
        async for event in self._stream.events():
            if self._ended:
                return
            if isinstance(event, Ready):
                await self._ensure_started()
            elif isinstance(event, Partial):
                await self._ensure_started()
                if self._first_partial_ms is None:
                    self._first_partial_ms = int((time.monotonic() - self._opened_at) * 1000)
                self._partials += 1
                await self._send({"type": "partial", "text": event.text})
            elif isinstance(event, Final):
                await self._ensure_started()
                self._finals.append(event.text)
                await self._send({"type": "final", "text": event.text})
            elif isinstance(event, Closed):
                await self._ensure_started()
                if self._end_reason == "quota_exceeded":
                    await self._finish("quota_exceeded", _error("quota_exceeded", MSG_QUOTA))
                else:
                    full_text = " ".join(t for t in self._finals if t)
                    await self._finish("stop", {"type": "stop", "full_text": full_text})
                return
            elif isinstance(event, Failed):
                code = event.code if event.code in ("failed", "unavailable") else "failed"
                await self._finish(code, _error(code, event.message or MSG_FAILED))
                return
        if not self._ended:
            await self._finish("failed", _error("failed", MSG_FAILED))

    async def _ensure_started(self) -> None:
        """Send the server ``start`` once, before any transcript event."""
        if self._ready:
            return
        self._ready = True
        remaining = None if self._remaining is None else int(self._remaining)
        await self._send(
            {
                "type": "start",
                "backend": BACKEND_NAME,
                "session_id": str(self.session_id),
                "remaining_seconds": remaining,
            }
        )

    # ── timers ─────────────────────────────────────────────────────────

    async def _watch(self) -> None:
        idle = settings.VOICE_TRANSCRIPTION_IDLE_TIMEOUT_SECONDS
        recheck_every = settings.VOICE_TRANSCRIPTION_REVOCATION_CHECK_SECONDS
        ready_timeout = settings.VOICE_TRANSCRIPTION_READY_TIMEOUT_SECONDS
        step = max(0.01, min(1.0, idle / 4, recheck_every / 4, ready_timeout / 4))
        last_check = time.monotonic()
        while not self._ended:
            await asyncio.sleep(step)
            now = time.monotonic()
            if not self._ready and now - self._opened_at >= ready_timeout:
                await self._finish("unavailable", _error("unavailable", MSG_UNAVAILABLE))
                return
            if not self._audio_closed and now - self._last_audio >= idle:
                await self._end_audio("stop")
            if now - last_check >= recheck_every:
                last_check = now
                if not self._still_authorized():
                    await self._finish("unauthorized", _error("unauthorized", MSG_UNAUTHORIZED))
                    return

    def _still_authorized(self) -> bool:
        """One failed re-check (a DB blip) is not a revocation; two in a row
        end the session, so revocation cannot be dodged by a broken check."""
        try:
            allowed = self._recheck()
        except Exception as exc:  # noqa: BLE001
            self._recheck_failures += 1
            logger.warning(
                "Voice transcription session %s: revocation re-check failed (%s, %d in a row)",
                self.session_id,
                type(exc).__name__,
                self._recheck_failures,
            )
            return self._recheck_failures < MAX_RECHECK_FAILURES
        self._recheck_failures = 0
        return allowed

    # ── ending ─────────────────────────────────────────────────────────

    async def _send(self, payload: dict[str, Any]) -> None:
        """A non-terminal event; dropped once the session has ended."""
        if self._ended:
            return
        try:
            async with self._send_lock:
                await self._ws.send_text(json.dumps(payload))
        except Exception:  # noqa: BLE001 — the client went away mid-send
            await self._finish("cancelled", None)

    async def _finish(self, outcome: str, terminal: dict[str, Any] | None) -> None:
        """Exactly once: stop the provider, send ``terminal`` (if any), close 1000."""
        if self._ended:
            return
        self._ended = True
        self.outcome = outcome
        await self._cancel_stream()
        try:
            async with self._send_lock:
                if terminal is not None:
                    await self._ws.send_text(json.dumps(terminal))
                await self._ws.close(code=WS_CLOSE_NORMAL)
        except Exception:  # noqa: BLE001 — the client may already be gone
            pass
        self._done.set()

    async def _cancel_stream(self) -> None:
        if self._stream is None:
            return
        try:
            await self._stream.cancel()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Voice transcription session %s: provider cancel failed (%s)",
                self.session_id,
                type(exc).__name__,
            )

    async def _teardown(self) -> None:
        await self._cancel_stream()
        if self._acquired:
            tracker.release(self._user_id, self.session_id)
            self._acquired = False
        outcome = self.outcome or "failed"
        audio_seconds = self._bytes / PCM_BYTES_PER_SECOND
        replay_seconds = min(max(self._replay_declared, 0.0), audio_seconds)
        rotations = int(getattr(self._stream, "rotations", 0) or 0)
        self._write_ledger(outcome, audio_seconds, replay_seconds)
        logger.info(
            "voice transcription session finished user_id=%s session_id=%s "
            "provider_type=%s model=%s audio_seconds=%.2f replay_seconds=%.2f "
            "first_partial_ms=%s partials=%d finals=%d rotations=%d outcome=%s",
            self._user_id,
            self.session_id,
            self._provider_type,
            self._model,
            audio_seconds,
            replay_seconds,
            self._first_partial_ms,
            self._partials,
            len(self._finals),
            rotations,
            outcome,
        )
        self._finals = []

    def _write_ledger(self, outcome: str, audio_seconds: float, replay_seconds: float) -> None:
        if self._started_at is None:
            return
        row = VoiceTranscriptionUsage(
            user_id=self._user_id,
            session_id=self.session_id,
            started_at=self._started_at,
            ended_at=datetime.now(UTC),
            audio_seconds=audio_seconds,
            replay_seconds=replay_seconds,
            provider_id=self._provider_id,
            provider_type=self._provider_type,
            model=self._model,
            client_kind=_ledger_client_kind(self._client_kind),
            outcome=outcome,
        )
        try:
            with core_db.create_session() as db:
                VoiceTranscriptionUsageService.record(db, row)
        except Exception as exc:  # noqa: BLE001 — the session is over either way
            logger.error(
                "Voice transcription session %s: ledger write failed (%s)",
                self.session_id,
                type(exc).__name__,
            )
