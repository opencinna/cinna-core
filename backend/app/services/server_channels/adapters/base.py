"""Channel adapter contract — the seam between transport and pipeline.

An *adapter* owns everything platform-specific about one inbound transport
(Google Chat, Discord, Telegram, …): how to prove a request really came from
that platform, how to read a message out of its payload shape, and how to
send a reply back. Everything above it — whitelisting, user resolution,
routing, session binding — is transport-agnostic and lives in
``channel_inbound_service``.

Adding a channel is therefore a new module plus one registry entry. No
migration, no pipeline change.

Two rules the pipeline depends on:

1. ``verify_inbound`` is the *single* authentication chokepoint. It runs
   first, before any other field of the payload is trusted, and it raises
   rather than returning a partial result. Nothing downstream re-checks the
   signature.
2. Every value on ``ChannelInboundMessage`` is attacker-influenced except
   those the adapter took from *verified* claims. Adapters must document
   which is which; the pipeline treats ``sender_email`` as transitively
   trusted only because the adapter verified the platform's signature over
   it.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar, Literal

from fastapi import Request

from app.models import ServerChannel


class ChannelError(Exception):
    """Base for channel-domain errors. Routes translate to HTTPException."""


class ChannelVerificationError(ChannelError):
    """Inbound request failed transport-layer authentication.

    The route maps this to 403 and never processes the payload. The message
    is for logs only — it is not returned to the caller, since a detailed
    reason is a probing oracle.
    """


class UnknownChannelTypeError(ChannelError):
    """``channel_type`` has no adapter in the registry."""


class ChannelConfigError(ChannelError):
    """Adapter config failed ``validate_config``."""


class ChannelSendError(ChannelError):
    """Outbound delivery failed after retries."""


# What kind of event the adapter found in the payload. The pipeline branches
# on this rather than on any platform-specific event name.
ChannelEventKind = Literal["message", "added_to_space", "ignored"]


@dataclass(frozen=True)
class ChannelInboundMessage:
    """One normalized inbound event.

    ``event_kind`` decides how far the pipeline goes:
    - ``message``        — the full routing / ingestion path.
    - ``added_to_space`` — static welcome reply, nothing persisted.
    - ``ignored``        — acked and dropped (the bot's own messages,
      membership changes, empty text). Never an error: a channel that
      returns non-2xx for uninteresting events gets retried forever.
    """

    event_kind: ChannelEventKind
    # Verified sender identity. `sender_email` is the whitelist + user
    # resolution key and MUST come from a signature-verified payload.
    sender_email: str | None = None
    sender_display_name: str | None = None
    # Platform-native user id, namespaced into SessionSender.external_id.
    external_user_id: str | None = None
    # Platform-native thread identity — the binding key.
    thread_key: str | None = None
    text: str = ""
    # Platform message id, used for redelivery dedup.
    external_message_id: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ChannelCapabilities:
    """What a transport can do, so the pipeline can degrade instead of fail.

    Progress notifications and message editing are nice-to-have; a channel
    without them still works, it is just quieter. ``supports_sync_reply``
    is the important one: when False the pipeline has no way to answer a
    denial inline and must stay silent rather than leak that the token is
    valid through a side channel.
    """

    supports_progress_updates: bool = False
    supports_message_edit: bool = False
    supports_markdown: bool = False
    max_message_chars: int | None = None
    supports_sync_reply: bool = False


class ChannelAdapter(ABC):
    """One transport's implementation of the channel contract."""

    #: Registry key and the ``channel_<type>`` session integration_type stem.
    channel_type: ClassVar[str]
    #: Human label for admin UI / setup instructions.
    display_name: ClassVar[str] = ""

    @property
    @abstractmethod
    def capabilities(self) -> ChannelCapabilities:
        """Static capability declaration for this transport."""

    @abstractmethod
    def validate_config(self, config: dict[str, Any]) -> None:
        """Validate the admin-supplied non-secret config.

        Raises ``ChannelConfigError`` with an admin-readable message. Called
        on create and update, before anything is persisted.
        """

    @abstractmethod
    async def verify_inbound(
        self, request: Request, channel: ServerChannel, body: bytes
    ) -> ChannelInboundMessage:
        """Authenticate the request, then parse it. Fails closed.

        ``body`` is the already-read (and size-capped) raw request body — the
        route reads it once so the size cap applies before any parsing, and
        so adapters that need the exact bytes for a signature check get them
        unmodified.

        Raises ``ChannelVerificationError`` when the request cannot be proven
        to come from this platform, or when the payload lacks a verified
        sender identity. Returning an ``ignored`` message is for *authentic*
        events the pipeline doesn't act on — never for auth failures.
        """

    @abstractmethod
    async def send_message(
        self, channel: ServerChannel, thread_key: str, text: str
    ) -> str | None:
        """Deliver ``text`` into ``thread_key``. Returns the platform message id.

        Splits oversized text per ``capabilities.max_message_chars``. Raises
        ``ChannelSendError`` after exhausting retries.
        """

    async def update_message(
        self,
        channel: ServerChannel,
        thread_key: str,
        external_message_id: str,
        text: str,
    ) -> None:
        """Edit a previously sent message. No-op unless the adapter overrides.

        Only called when ``capabilities.supports_message_edit`` is True.
        """
        return None

    @abstractmethod
    def get_setup_instructions(
        self, channel: ServerChannel, webhook_url: str
    ) -> tuple[dict[str, str], list[str]]:
        """Return ``(details, steps)`` for the admin setup panel.

        ``details`` is a flat label→value map (audience, bot scopes, …);
        ``steps`` is an ordered list of human-readable instructions.
        """

    def build_sync_response(self, text: str | None) -> dict[str, Any]:
        """Payload for the webhook's own HTTP response.

        Channels that render the webhook response as a message in-thread
        (Google Chat does) can answer without any outbound credential — which
        is what makes denial replies possible before setup is complete.
        ``None`` means "acknowledge silently".
        """
        return {}


__all__ = [
    "ChannelAdapter",
    "ChannelCapabilities",
    "ChannelInboundMessage",
    "ChannelEventKind",
    "ChannelError",
    "ChannelVerificationError",
    "UnknownChannelTypeError",
    "ChannelConfigError",
    "ChannelSendError",
]
