"""Provider-neutral speech adapter contract.

An adapter opens a :class:`SpeechStream` for one dictation: the caller pushes
16 kHz mono PCM, ends the audio, and reads :class:`SpeechEvent` values until a
terminal :class:`Closed` or :class:`Failed`. Event messages are user-facing
sentences that never name the provider nor quote its raw text.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Literal, Protocol

SpeechFailureCode = Literal["failed", "unavailable"]
# Why a stream failed, for the server's own use (``probe``, logs); never sent
# to the client, which only sees ``code`` and ``message``.
SpeechFailureCause = Literal["auth", "quota", "unreachable", "other"]
ProbeOutcome = Literal["ok", "invalid_key", "unreachable", "quota"]


@dataclass(frozen=True)
class Ready:
    """The provider accepted the session; audio flows without queueing."""


@dataclass(frozen=True)
class Partial:
    """The current utterance so far. Replaces the previous partial; may shrink."""
    text: str


@dataclass(frozen=True)
class Final:
    """One utterance's final text (not cumulative)."""
    text: str


@dataclass(frozen=True)
class Closed:
    """Terminal: everything said after ``end_audio`` has been delivered."""


@dataclass(frozen=True)
class Failed:
    """Terminal: the stream ended on an error."""
    code: SpeechFailureCode
    message: str
    cause: SpeechFailureCause = "other"


SpeechEvent = Ready | Partial | Final | Closed | Failed


class SpeechStream(Protocol):
    """One dictation against a provider, possibly over several connections."""

    rotations: int

    async def push(self, pcm: bytes) -> None:
        """Send PCM now, or queue it until the provider is ready. Never paces."""
        ...

    async def end_audio(self) -> None:
        """No more audio: flush, then ``Closed`` once the provider settles."""
        ...

    async def cancel(self) -> None:
        """Drop everything; no further events are produced."""
        ...

    def events(self) -> AsyncIterator[SpeechEvent]:
        """The stream's events, ending after the terminal one (or on cancel)."""
        ...


class SpeechAdapter(Protocol):
    provider_type: str
    model: str

    async def open(self, *, api_key: str, locale: str, sample_rate: int) -> SpeechStream:
        """Start a stream; ``Ready`` (or ``Failed``) arrives as an event."""
        ...

    async def probe(self, *, api_key: str) -> ProbeOutcome:
        """Check the key with a short real session (admin "Test" action)."""
        ...
