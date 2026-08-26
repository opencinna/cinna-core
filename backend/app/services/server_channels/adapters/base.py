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
restates the same promise for messages nobody pushed; an **authenticated**
transport holds it nowhere in this file at all, because its caller was already
an authenticated platform user before the platform heard from them (App MCP
presents a bearer token this platform minted). The mode is a **declared
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

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar, Literal

from fastapi import Request
from sqlmodel import Session as DBSession

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
class ChannelReplaceResult:
    """What :meth:`ChannelAdapter.replace_message` actually did.

    ``replaced`` is the load-bearing half, and the reason this is a result
    object rather than a bare id. ``replace_message`` degrades to an ordinary
    post when the message it was told to reuse cannot be patched — deleted by
    hand, an id from before a redeploy, a scope that has gone away — and the
    caller has to be able to see the difference, because the two outcomes
    imply *opposite* things about the status notice id it is holding:

    * **replaced** — the notice now holds the answer. The id must be released,
      or the next turn's "working on your message…" is patched straight over
      the reply.
    * **not replaced** — the notice is still standing, still saying "working
      on your message…", with the answer posted underneath it. The id must be
      **kept**: releasing it orphans that message, and it cannot be tidied
      away by deleting it either, because Chat leaves a "Message deleted by
      its author" tombstone — the exact thing reusing the slot exists to
      avoid. Keeping it lets the next turn patch that same message back to
      "working…" and self-heal, so a transport whose patches are permanently
      broken leaves one stale notice on the thread rather than one per turn.

    Without the flag the fallback is invisible: the caller sees a successful
    delivery, releases the id, and files a debug summary saying the reply went
    into the notice when it did not.

    ``message_id`` is the id of the last message written, whichever path was
    taken — the same value the old bare return carried.
    """

    message_id: str | None
    replaced: bool


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
    #: Whether a message this app posted can be removed again. NOT part of
    #: :attr:`supports_status_notice` — the notice is reused as the reply's
    #: slot rather than deleted, precisely because Chat leaves a "Message
    #: deleted by its author" tombstone. This gates only the two edges that
    #: end a turn with nothing to say; see ``ChannelOutboundService``.
    supports_message_delete: bool = False
    supports_markdown: bool = False
    max_message_chars: int | None = None
    supports_sync_reply: bool = False

    # --- Transport shape -------------------------------------------------
    #
    # The defaults below describe a freely-instantiable push webhook with its
    # own outbound credential — i.e. exactly what every adapter was before the
    # transport split — so an adapter that says nothing keeps its previous
    # behaviour byte for byte.

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
    #: Whether at most **one** ``ServerChannel`` row of this type may exist.
    #:
    #: True for a transport that is not an instance of anything: App MCP is a
    #: single endpoint this deployment either offers or does not, so a second
    #: row would be two policies over one door with nothing to say which wins.
    #: False — the default, and the shape every transport had before this
    #: existed — for a transport an admin can legitimately run several of
    #: (two Google Chat apps, three polled mailboxes).
    #:
    #: Declared here rather than spelled ``channel_type == "app_mcp"`` in the
    #: service, for the same reason every other transport fact is: the rule
    #: belongs to the transport, and a hardcoded type check is a rule that has
    #: to be found and edited again for the next such transport.
    is_singleton: bool = False

    @property
    def supports_status_notice(self) -> bool:
        """Whether this transport can run a single, mutating progress notice.

        The pipeline narrates slow work — routing, installing, ready — and the
        naive way to do that is one message per state, which leaves a thread
        littered with three notices nobody wants to read again once the answer
        arrives. A transport that can *edit* its own posts keeps one message
        instead, rewrites it as the work advances, and finally rewrites it one
        last time **with the agent's own reply** — so the narration does not
        merely disappear, it becomes the answer.

        Editing is the whole requirement, and deliberately so. An earlier
        version derived this from edit **and** delete, because the notice used
        to be deleted once the real reply had been posted underneath it. Google
        Chat renders a deleted message as a "Message deleted by its author"
        tombstone, so every single answer arrived under one. Reusing the notice
        as the reply's slot removes the deletion from the common path
        entirely; ``supports_message_delete`` still gates the two edges that
        genuinely have nothing to say (see ``clear_status``), and it is checked
        there rather than folded in here, where it would bar a transport that
        can do everything that actually matters.

        Transports that answer False fall back to posting each notice
        separately — exactly what every transport did before this existed.
        """
        return self.supports_progress_updates and self.supports_message_edit


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

    def validate_config_references(
        self, db: DBSession, config: dict[str, Any]
    ) -> None:
        """Validate config values that point at *other rows*. Default: no-op.

        Split from :meth:`validate_config` rather than folded into it because
        the two answer different questions with different inputs.
        ``validate_config`` is a pure shape check — every fact it needs is in
        the dict, so it can run anywhere, including where no session exists.
        This one asks whether the things the config *names* actually exist and
        are of the right kind, which is a database question.

        The session is a parameter, never opened here, and that is the whole
        point of the method existing at all: an adapter that opened its own
        connection would validate against a different snapshot from the one
        the caller is about to persist into — and under test, against a
        different transaction entirely, so a row the caller just created would
        read as missing.

        Raises ``ChannelConfigError`` with an admin-readable message, exactly
        like ``validate_config``. A transport whose config references nothing
        (Google Chat's is a bare project number) inherits the no-op.
        """
        return None

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

    def has_outbound_credentials(self, channel: ServerChannel) -> bool:
        """Whether an outbound credential has been configured for ``channel``.

        The derivation behind the admin projection's field of the same name,
        and it lives here because only the transport knows *where* its
        credential is kept. ``needs_outbound_credentials`` declares that fact;
        this method acts on it.

        The default is the reading of ``needs_outbound_credentials=True``, which
        is what every adapter was before the transport split: the credential is
        the ``encrypted_secrets`` blob, so its presence is the whole answer. A
        transport that declares ``False`` keeps its credential somewhere else
        and **must** override this — the registry refuses to import otherwise,
        because the inherited answer for such a transport is a confident
        ``False`` about a channel that may be perfectly operational.

        **Must not raise, and must not need a session.** It is called per row
        inside a list comprehension in the admin list route, where an exception
        blanks the entire Channels tab — including the offending channel the
        admin would need in order to fix it.
        """
        return bool(channel.encrypted_secrets)

    def record_routing_outcome(
        self,
        db: DBSession,
        channel: ServerChannel,
        *,
        thread_key: str,
        agent_id: uuid.UUID,
        session_id: uuid.UUID,
    ) -> None:
        """Attach a routing outcome to whatever durable record this transport
        kept of the inbound message. Default: no-op.

        The second half of a two-step store, and the *only* reason the pipeline
        knows this method exists. A transport that persists arrivals cannot
        persist them with an agent on them: the agent is what classification
        produces, and classification happens after arrival — while the whole
        value of the durable row is that it exists for the messages that never
        reach classification at all (a sender denied by the whitelist, by the
        channel policy, or by user resolution). So the row is written on
        arrival with nothing routed on it, and this hook stamps it once the
        pipeline has an answer.

        Same shape and the same reasoning as
        :meth:`validate_config_references`: the question is transport-specific,
        the answer needs a database, and the session is a **parameter** rather
        than something opened here — the caller is mid-transaction and an
        adapter that opened its own connection would be writing against a
        different snapshot from the one the caller is about to commit (and,
        under test, a different transaction entirely).

        Keyed on ``thread_key`` rather than on a message id because that is
        what the pipeline still holds at the point it can answer, and because
        thread-wide is the more useful grain: it heals any earlier arrival on
        the same thread that was stored before the thread had an agent.

        **Must not raise, and must not be load-bearing.** The caller wraps it,
        but the contract is stated here too: this is an audit stamp riding
        along on a successful ingest. A transport that lets a bookkeeping
        failure escape would turn a delivered message into a failed binding.
        """
        return None

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

    async def replace_message(
        self,
        channel: ServerChannel,
        thread_key: str,
        external_message_id: str,
        text: str,
    ) -> ChannelReplaceResult:
        """Deliver ``text``, **reusing** ``external_message_id`` as its first
        message rather than posting below it.

        This is how the agent's reply lands in the status notice's slot: the
        message that said "working on your message…" becomes the answer, in
        place, with no second message and — the reason this exists rather than
        a delete-then-send — no deletion tombstone above every reply.

        Unlike :meth:`update_message` this is a *delivery*, so it carries
        delivery's obligations: oversized text is chunked, with the first chunk
        replacing the named message and the remainder posted after it.

        Returns a :class:`ChannelReplaceResult`, **not** a bare id: an
        implementation that falls back to posting when the patch fails has to
        say so, or the caller releases a status notice id whose message is
        still standing. Read that class before writing an override.

        The default ignores the id and posts normally — the right answer for a
        transport that cannot edit, which never had a notice to reuse, since
        ``set_status`` hands one back only when ``supports_status_notice``
        holds. It reports ``replaced=False`` for the same reason: nothing was
        taken over, so there is nothing for the caller to let go of.
        """
        return ChannelReplaceResult(
            message_id=await self.send_message(channel, thread_key, text),
            replaced=False,
        )

    async def delete_message(
        self,
        channel: ServerChannel,
        thread_key: str,
        external_message_id: str,
    ) -> None:
        """Remove a message this app posted. No-op unless overridden.

        Only called when ``capabilities.supports_message_delete`` is True.
        Deleting something the app did not post is not in this contract — no
        transport grants it, and the pipeline only ever passes back an id it
        was handed by :meth:`send_message`.
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

    def build_sync_response(
        self, text: str | None, thread_key: str | None = None
    ) -> dict[str, Any]:
        """Payload for the webhook's own HTTP response.

        Channels that render the webhook response as a message in-thread
        (Google Chat does) can answer without any outbound credential — which
        is what makes denial replies possible before setup is complete.
        ``None`` means "acknowledge silently".

        ``thread_key`` is the thread the triggering message arrived on, and it
        is **not** optional information for a transport whose sync reply has to
        be addressed. Google Chat posts an unthreaded response as a new
        top-level message in the space, so a denial or a "working on it" that
        omits it lands somewhere other than the conversation it answers — the
        one place the sender is looking. It defaults to ``None`` for the one
        caller that genuinely has no thread yet (the ``added_to_space``
        welcome) and for transports that ignore it entirely.

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


class AuthenticatedChannelTransport(ChannelAdapter):
    """A transport with **no inbound driver at all**.

    The third shape, and the odd one out: there is nothing to push to and
    nothing to pull from, because the caller is already an authenticated
    platform user by the time the platform hears from them. App MCP is the
    first — a user's MCP client presents a bearer token minted by this
    platform's own OAuth flow, and the door it comes through has been
    authenticating that token since long before channels existed.

    So what does a ``ServerChannel`` row buy such a surface? Everything above
    the transport: an admin kill switch, visibility plus a grant allowlist,
    per-user enablement and agent scope. Rule 1 of the module docstring —
    "``verify_inbound`` is the single authentication chokepoint" — is not
    weakened here so much as satisfied *elsewhere and earlier*: the identity
    is proven by the platform's own token verification, which is a stronger
    tier than either a signed webhook or a ``From:`` header, and this class
    exists to make sure nobody can accidentally route traffic around that
    proof by pointing a webhook at the row.

    Subclasses **must** declare ``inbound_mode="authenticated"`` in
    ``capabilities``. As with :class:`PolledChannelTransport`, subclassing is
    how a transport inherits the refusals below; it is never a substitute for
    the declaration, because nothing dispatches on the class. The registry
    checks both directions and refuses to import on a disagreement.
    """

    async def verify_inbound(
        self, request: Request, channel: ServerChannel, body: bytes
    ) -> ChannelInboundMessage:
        """Refuse: this transport has no webhook, so there is nothing to verify.

        Verbatim the reasoning of :meth:`PolledChannelTransport.verify_inbound`,
        and deliberately the same exception. Reaching here means a request
        arrived at ``/channels/{token}/inbound`` for a channel whose callers are
        supposed to arrive already authenticated — the route cannot prove
        anything about it, and acking or parsing the body would put an
        unverified payload into a pipeline whose ordering rests on step 2 having
        really run.

        ``ChannelTransportMisuseError`` is a ``ChannelVerificationError``, so
        the caller gets the standard detail-free 403 and the attempt is audited
        like any other verification failure. In practice this is unreachable:
        the webhook route resolves a channel *by token*, and a transport
        declaring ``needs_webhook_token=False`` never has one. It is written
        anyway, because "unreachable because of a rule in another module" is
        exactly the guarantee that quietly stops holding.
        """
        raise ChannelTransportMisuseError(
            f"Channel type {self.channel_type!r} is an authenticated transport "
            "and has no webhook; its callers arrive already authenticated by "
            "the platform."
        )

    async def send_message(
        self, channel: ServerChannel, thread_key: str, text: str
    ) -> str | None:
        """Refuse: this transport has no outbound path to deliver into.

        The answer to an authenticated caller rides the synchronous response
        of the request they made — there is no thread to post into later and
        no credential with which to do it.

        ``ChannelSendError`` rather than ``ChannelTransportMisuseError``,
        because the two callers are outbound ones and already handle it: the
        admin test-send route renders a ``ChannelError`` as an admin-readable
        ``success=false``, and ``ChannelOutboundService`` treats any exception
        from a send as a failed best-effort delivery. Reusing the *verification*
        error here would put an authentication-shaped exception on a path that
        never authenticated anything.

        Also unreachable in practice, and worth stating where the next reader
        will look: ``ChannelOutboundService`` only resolves sessions whose
        ``integration_type`` starts with ``channel_``, and this transport's
        sessions are stamped by their own surface (App MCP writes ``app_mcp``),
        so no stream event ever reaches a send on this adapter.
        """
        raise ChannelSendError(
            f"Channel type {self.channel_type!r} has no outbound transport; "
            "replies are returned in the caller's own synchronous response."
        )


__all__ = [
    "ChannelAdapter",
    "PolledChannelTransport",
    "AuthenticatedChannelTransport",
    "ChannelCapabilities",
    "ChannelReplaceResult",
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
