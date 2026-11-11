"""Fake speech adapter for the voice transcription relay tests.

Implements the ``SpeechAdapter`` / ``SpeechStream`` contract from
``app.services.voice_transcription.providers.base`` with no provider and no
network. Events are scripted by how many PCM bytes have been pushed, and the
stream records what the relay did to it (pushes with timestamps, ends,
cancels) so tests can assert on the relay's behaviour, not on a provider's.
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator

from app.services.voice_transcription.providers.base import (
    Closed,
    Failed,
    Ready,
    SpeechEvent,
)


class FakeSpeechStream:
    rotations = 0

    def __init__(self, adapter: "FakeSpeechAdapter") -> None:
        self._adapter = adapter
        self._queue: asyncio.Queue[SpeechEvent | None] = asyncio.Queue()
        self._fired: set[int] = set()
        self.pushes: list[tuple[int, float]] = []
        self.total_bytes = 0
        self.ends = 0
        self.cancels = 0
        if adapter.emit_ready:
            self._queue.put_nowait(Ready())

    async def push(self, pcm: bytes) -> None:
        self.pushes.append((len(pcm), time.monotonic()))
        self.total_bytes += len(pcm)
        for index, (threshold, event) in enumerate(self._adapter.script):
            if index not in self._fired and self.total_bytes >= threshold:
                self._fired.add(index)
                self._queue.put_nowait(event)

    async def end_audio(self) -> None:
        self.ends += 1
        for event in self._adapter.on_end:
            self._queue.put_nowait(event)
        if self._adapter.close_on_end:
            self._queue.put_nowait(Closed())

    async def cancel(self) -> None:
        self.cancels += 1
        self._queue.put_nowait(None)

    async def events(self) -> AsyncIterator[SpeechEvent]:
        while True:
            event = await self._queue.get()
            if event is None:
                return
            yield event
            if isinstance(event, (Closed, Failed)):
                return


class FakeSpeechAdapter:
    provider_type = "google"
    model = "fake-live-1"

    def __init__(
        self,
        *,
        script: list[tuple[int, SpeechEvent]] | None = None,
        on_end: list[SpeechEvent] | None = None,
        close_on_end: bool = True,
        emit_ready: bool = True,
        open_error: Exception | None = None,
        probe_result: str = "ok",
        probe_error: Exception | None = None,
    ) -> None:
        # ``(cumulative pushed bytes, event)``: fired once when reached.
        self.script = script or []
        self.on_end = on_end or []
        self.close_on_end = close_on_end
        self.emit_ready = emit_ready
        self.open_error = open_error
        self.probe_result = probe_result
        self.probe_error = probe_error
        self.streams: list[FakeSpeechStream] = []
        self.open_locales: list[str] = []
        self.open_keys: list[str] = []
        self.probe_keys: list[str] = []

    @property
    def opens(self) -> int:
        return len(self.open_locales)

    @property
    def stream(self) -> FakeSpeechStream:
        return self.streams[-1]

    async def open(self, *, api_key: str, locale: str, sample_rate: int) -> FakeSpeechStream:
        self.open_locales.append(locale)
        self.open_keys.append(api_key)
        if self.open_error is not None:
            raise self.open_error
        stream = FakeSpeechStream(self)
        self.streams.append(stream)
        return stream

    async def probe(self, *, api_key: str) -> str:
        self.probe_keys.append(api_key)
        if self.probe_error is not None:
            raise self.probe_error
        return self.probe_result
