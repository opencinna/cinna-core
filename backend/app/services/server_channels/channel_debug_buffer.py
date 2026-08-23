"""In-memory capture of recent channel traffic, for the admin debug panel.

Configuring a channel against a real provider is a blind exercise: the webhook
either fires or it does not, and when it does the pipeline's decision — denied
by the whitelist, routed to an agent, parked behind an install — leaves no
trace an admin can see. Every diagnostic signal the feature emits deliberately
avoids logging message text, so "did my message arrive, and what happened to
it" has no answer short of reading the server log.

This buffer is that answer. It is a *debugging aid*, not an audit trail:

- **In memory, never persisted.** The buffer dies with the process. Inbound
  message text at rest in the database is exactly what the rest of the feature
  is careful to avoid, and a debug convenience is not a good reason to start.
  ``SecurityEvent`` remains the durable, auditable record of denials and
  verification failures; this is the live view beside it.
- **Bounded twice** — per channel (ring buffer) and per entry (text clamp) —
  so a busy or hostile channel cannot grow it without limit.
- **Superuser-only on read.** Text held here is no wider an exposure than the
  admin surface already grants: a superuser can read the resulting session's
  messages through the platform anyway. It is not therefore *free*, which is
  why the bound is small and the lifetime is the process.

Recording must never break the pipeline. Every entry point swallows its own
errors — a debug panel that fails an inbound webhook would be worse than no
debug panel.

That guard protects the *recording*, not the caller's argument expressions,
which Python evaluates first: an f-string like ``f"...{bundle.name}"`` at a
call site raises before ``record`` is entered, and in the inbound pipeline
that lands in a broad ``except`` that abandons the install. Keep summary
arguments to attributes you are certain exist.
"""
from __future__ import annotations

import logging
import threading
import uuid
from collections import deque
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Literal

from app.core.config import settings

logger = logging.getLogger(__name__)

ChannelDebugDirection = Literal["inbound", "outbound"]

# When this process started capturing. Surfaced to the panel so an empty feed
# reads as "nothing since 14:03" rather than an ambiguous blank — a restart
# drops the buffer, and that is the single most confusing thing about it.
CAPTURING_SINCE = datetime.now(UTC)

# Event kinds. Plain strings (not an Enum) to match the feature's status-string
# convention; the frontend maps them to badges and tolerates unknown values.
DEBUG_RECEIVED = "received"
DEBUG_REJECTED = "rejected"
DEBUG_ROUTED = "routed"
DEBUG_INSTALLING = "installing"
DEBUG_NO_MATCH = "no_match"
DEBUG_REPLIED = "replied"
DEBUG_SEND_FAILED = "send_failed"
DEBUG_TEST_SEND = "test_send"


@dataclass(frozen=True)
class ChannelDebugEvent:
    """One line in the debug feed."""

    id: str
    at: datetime
    direction: ChannelDebugDirection
    kind: str
    # Short human-readable line — what the pipeline decided, in words.
    summary: str
    sender_email: str | None = None
    sender_display_name: str | None = None
    # The channel-native thread identity, and the whole point of the panel:
    # it is what a "reply here" action sends to.
    thread_key: str | None = None
    text: str | None = None
    detail: dict[str, str] = field(default_factory=dict)
    #: How many times this identical event repeated in a row. ``at`` is the
    #: MOST RECENT occurrence when this is above 1.
    repeat: int = 1

    def same_as(self, other: "ChannelDebugEvent") -> bool:
        """Whether two events differ only in identity/timestamp.

        Used to collapse a run of identical events into one row with a count.
        """
        return (
            self.direction == other.direction
            and self.kind == other.kind
            and self.summary == other.summary
            and self.sender_email == other.sender_email
            and self.thread_key == other.thread_key
            and self.text == other.text
            and self.detail == other.detail
        )


class ChannelDebugBuffer:
    """Per-channel ring buffer of recent events.

    Process-local by construction. The backend runs a single worker, so for the
    local-testing job this serves that is the whole picture; behind multiple
    workers a given panel would show only the events its own worker handled,
    which is a limitation of a debugging aid, not a correctness problem.
    """

    _lock = threading.Lock()
    _buffers: dict[str, deque[ChannelDebugEvent]] = {}

    @classmethod
    def record(
        cls,
        *,
        channel_id: uuid.UUID | str,
        direction: ChannelDebugDirection,
        kind: str,
        summary: str,
        sender_email: str | None = None,
        sender_display_name: str | None = None,
        thread_key: str | None = None,
        text: str | None = None,
        detail: dict[str, str] | None = None,
    ) -> None:
        """Append one event. Never raises — callers are on the request path."""
        try:
            event = ChannelDebugEvent(
                id=str(uuid.uuid4()),
                at=datetime.now(UTC),
                direction=direction,
                kind=kind,
                summary=summary,
                sender_email=sender_email,
                sender_display_name=sender_display_name,
                thread_key=thread_key,
                text=cls._clamp(text),
                detail=detail or {},
            )
            key = str(channel_id)
            with cls._lock:
                buffer = cls._buffers.get(key)
                if buffer is None:
                    buffer = deque(maxlen=settings.SERVER_CHANNEL_DEBUG_BUFFER_SIZE)
                    cls._buffers[key] = buffer
                # Collapse a run of identical events into one row with a count.
                # Two reasons, one of them a defence: a retry storm or a
                # redelivery loop is far easier to read as "x40" than as forty
                # rows, AND anyone holding the webhook token could otherwise
                # push the events an admin is trying to read straight out of a
                # bounded ring simply by repeating a request.
                if buffer and buffer[-1].same_as(event):
                    buffer[-1] = replace(
                        buffer[-1], at=event.at, repeat=buffer[-1].repeat + 1
                    )
                else:
                    buffer.append(event)
        except Exception:  # noqa: BLE001 — a debug aid must never break delivery
            logger.debug("Channel debug capture failed", exc_info=True)

    @classmethod
    def list_events(cls, channel_id: uuid.UUID | str) -> list[ChannelDebugEvent]:
        """Newest-first snapshot for one channel."""
        with cls._lock:
            buffer = cls._buffers.get(str(channel_id))
            if not buffer:
                return []
            return list(reversed(buffer))

    @classmethod
    def clear(cls, channel_id: uuid.UUID | str) -> None:
        with cls._lock:
            cls._buffers.pop(str(channel_id), None)

    @classmethod
    def reset(cls) -> None:
        """Drop every buffer. Test hygiene — the class state is process-global."""
        with cls._lock:
            cls._buffers.clear()

    @staticmethod
    def _clamp(text: str | None) -> str | None:
        if not text:
            return None
        limit = settings.SERVER_CHANNEL_DEBUG_TEXT_MAX_CHARS
        if len(text) <= limit:
            return text
        return f"{text[:limit]}… (truncated)"


__all__ = [
    "CAPTURING_SINCE",
    "ChannelDebugBuffer",
    "ChannelDebugEvent",
    "DEBUG_RECEIVED",
    "DEBUG_REJECTED",
    "DEBUG_ROUTED",
    "DEBUG_INSTALLING",
    "DEBUG_NO_MATCH",
    "DEBUG_REPLIED",
    "DEBUG_SEND_FAILED",
    "DEBUG_TEST_SEND",
]
