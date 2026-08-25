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

Both rules survive the transport split; what changes is *which method* holds
rule 1 for a given transport. A push transport authenticates in
``verify_inbound``; a pull transport authenticates inside ``poll``, which
restates the same promise for messages nobody pushed. The mode is a **declared
capability** (``ChannelCapabilities.inbound_mode``), never inferred from which
ABC an adapter happens to subclass — the declaration is what the registry, the
channel service and the pollers dispatch on.

Rule 2 gains a corollary the split makes unavoidable: *how strong* "verified"
is now differs per transport. Google Chat's ``sender_email`` comes out of a
Google-signed JWT; email's comes out of a ``From:`` header and is spoofable.
Both feed the same pipeline, so each transport must say which tier it offers
where an admin can read it.
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


class ChannelTransportMisuseError(ChannelVerificationError):
    """A channel was driven through an entry point its transport does not have.

    Concretely: something POSTed at the webhook route for a channel whose
    transport is ``polled`` or ``authenticated``. That is a deployment or
    configuration bug rather than an attack — but it arrives on the *public*
    webhook, so it deliberately subclasses ``ChannelVerificationError``: the
    caller gets the same detail-free 403 every other verification failure
    gets, the attempt lands on the same throttled audit bucket, and the route
    needs no new branch. Fail-closed by inheritance.

    A distinct type all the same, so a log reader can tell "someone forged a
    signature" apart from "this channel has no webhook to forge one at". That
    distinction survives in exactly one place: the ``logger.warning`` in
    ``ChannelInboundService.handle_inbound``'s ``except`` block, which renders
    this exception. Not in the 403 (detail-free by design) and not in the admin
    debug feed, whose summary on that path is hardcoded — so the log line is
    load-bearing for this type meaning anything at all.
    """


# What kind of event the adapter found in the payload. The pipeline branches
# on this rather than on any platform-specific event name.
ChannelEventKind = Literal["message", "added_to_space", "ignored"]

# How a transport's inbound events reach the pipeline, and therefore where its
# authentication chokepoint lives:
#
#   "webhook"       the platform pushes an HTTP request; ``verify_inbound``
#                   proves it came from there. The original and the default.
#   "polled"        the platform is pulled on a timer; ``poll`` authenticates
#                   what it fetched. No ``Request`` exists.
#   "authenticated" no inbound driver at all — the caller is already an
#                   authenticated platform user when the pipeline is entered.
#
# Declared, not inferred. Every dispatch on transport shape reads this value.
ChannelInboundMode = Literal["webhook", "polled", "authenticated"]


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

    # --- Transport shape -------------------------------------------------
    #
    # The three defaults below describe a push webhook with its own outbound
    # credential — i.e. exactly what every adapter was before the transport
    # split — so an adapter that says nothing keeps its previous behaviour
    # byte for byte.

    #: Where this transport's authentication chokepoint lives. See
    #: ``ChannelInboundMode``.
    inbound_mode: ChannelInboundMode = "webhook"
    #: Whether the channel is reachable at ``/channels/{token}/inbound`` and
    #: therefore needs an unguessable token minted for it. False for a
    #: transport with no webhook: a token nothing can be reached through is
    #: dead weight, and publishing a URL that answers nothing misleads the
    #: admin who pastes it somewhere.
    needs_webhook_token: bool = True
    #: Whether this transport's outbound credential lives in
    #: ``ServerChannel.encrypted_secrets``. False for a transport whose
    #: credential lives elsewhere (email references a server-scoped SMTP
    #: config), which is why ``has_outbound_credentials`` on the admin
    #: projection is *derived* rather than read straight off that column.
    needs_outbound_credentials: bool = True


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
        self, channel: ServerChannel, webhook_url: str | None
    ) -> tuple[dict[str, str], list[str]]:
        """Return ``(details, steps)`` for the admin setup panel.

        ``details`` is a flat label→value map (audience, bot scopes, …);
        ``steps`` is an ordered list of human-readable instructions.

        ``webhook_url`` is ``None`` for a transport that is not reached by a
        webhook. An adapter that weaves the URL into its steps must say "this
        channel has no inbound URL" rather than render the ``None`` — the panel
        is copy-paste material for an admin.
        """

    def build_sync_response(self, text: str | None) -> dict[str, Any]:
        """Payload for the webhook's own HTTP response.

        Channels that render the webhook response as a message in-thread
        (Google Chat does) can answer without any outbound credential — which
        is what makes denial replies possible before setup is complete.
        ``None`` means "acknowledge silently".

        The base returns ``{}`` for everything, which is the right answer for a
        transport with no sync-reply surface and is what a polled transport
        inherits: its denials reach the sender as nothing at all. Deliberate —
        pushing declines back down a pull channel is a probing oracle and a
        spam amplifier — and never invisible to the operator, since every
        denial branch in ``process_inbound`` records to the debug buffer first.
        """
        return {}


class PolledChannelTransport(ChannelAdapter):
    """A transport the platform is *pulled* from on a timer, not pushed to.

    Email is the first of these: there is no request to verify because there
    is no request — a scheduler asks the mail server what arrived. Everything
    below the transport is unchanged. ``poll`` returns the same
    ``ChannelInboundMessage`` values ``verify_inbound`` would have produced,
    and the pipeline is entered at its post-verification step.

    Subclasses **must** declare ``inbound_mode="polled"`` in ``capabilities``.
    Subclassing this ABC is how a transport inherits the webhook refusal
    below; it is never a substitute for the declaration, because nothing
    dispatches on the class — the registry, ``ServerChannelService`` and the
    poller all read the declared capability.
    """

    async def verify_inbound(
        self, request: Request, channel: ServerChannel, body: bytes
    ) -> ChannelInboundMessage:
        """Refuse: this transport has no webhook, so there is nothing to verify.

        Not a ``NotImplementedError`` stub and not a silent ``ignored``
        message. Reaching here means a request arrived at
        ``/channels/{token}/inbound`` for a channel that is polled — the route
        has no way to authenticate it, and *pretending* to (by acking, or by
        parsing the body) would put an unverified payload into a pipeline
        whose whole ordering rests on step 2 having really run.

        ``ChannelTransportMisuseError`` is a ``ChannelVerificationError``, so
        the caller gets the standard detail-free 403 and the attempt is
        audited like any other verification failure. Fail closed, loudly, in
        the logs only.
        """
        raise ChannelTransportMisuseError(
            f"Channel type {self.channel_type!r} is a polled transport and has "
            "no webhook; inbound messages arrive through poll()."
        )

    @abstractmethod
    async def poll(self, channel: ServerChannel) -> list[ChannelInboundMessage]:
        """Fetch and authenticate everything new on ``channel``, oldest first.

        This is rule 1 of the module docstring restated for a pull transport.
        ``verify_inbound`` promises that nothing downstream re-checks the
        sender; **so does this method**. Every ``sender_email`` returned here
        must come from a source this transport considers authenticated,
        because the whitelist, user resolution, auto-registration and identity
        routing all treat that address as the sender's identity, and there is
        no second gate anywhere below.

        A pull transport must additionally document **how strong** that
        guarantee is, because — unlike a signed webhook — it is not the same
        answer for every transport. For **email the source is the ``From:``
        header, and it is spoofable**: anyone who can get a message into the
        polled mailbox can claim any address in it. That is a materially
        weaker trust tier than Google Chat's Google-signed JWT, and both now
        feed the same pipeline, so the difference has to be stated wherever an
        admin picks a whitelist — in the transport's own docstring, in the
        feature docs, and in the admin UI.

        Returning an empty list is the ordinary "nothing new" answer. A
        transient fetch failure (the mail server is down) is the transport's
        own problem to raise or swallow for its scheduler to retry; it is not
        an inbound event and must never be returned as one.

        Marking a message consumed — IMAP ``\\Seen``, an ack, a stored cursor —
        is the transport's job too. The pipeline does dedup on
        ``external_message_id``, but that is a safety net for redelivery, not
        the mechanism that stops the same mail being answered on every tick.
        """


__all__ = [
    "ChannelAdapter",
    "PolledChannelTransport",
    "ChannelCapabilities",
    "ChannelInboundMessage",
    "ChannelEventKind",
    "ChannelInboundMode",
    "ChannelError",
    "ChannelVerificationError",
    "ChannelTransportMisuseError",
    "UnknownChannelTypeError",
    "ChannelConfigError",
    "ChannelSendError",
]
