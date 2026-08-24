"""The inbound pipeline: webhook request → agent session.

This module is the security boundary of the feature. The webhook it serves is
platform-unauthenticated by design — anyone on the internet can POST to it —
so the ordering below is load-bearing, not stylistic:

    1. resolve channel      unknown/disabled token → 404, no detail, no oracle
    2. VERIFY (fail closed) adapter proves the platform sent this; nothing
                            above this line has parsed or trusted the payload
    3. dedup                webhook redelivery must not double-send
    4. whitelist            NULL/empty denies everyone; "*" is the only
                            blanket allow
    5. resolve user         auto-register only if the channel opted in
    6. channel policy       admin kill switch AND access grant AND the user's
                            own toggle; resolved once, here, and carried into
                            routing as plain data
    7. binding / routing    session work, off the request path

Steps 1–6 are cheap and run inline. Everything from 7 on can be slow (an LLM
routing call, an install, an environment build), so it runs as a background
task and the webhook answers immediately with a short static acknowledgement.
The real reply arrives asynchronously through ``ChannelOutboundService``.

**Step 6 gates every path below it, not only new threads.** A sender whose
access was revoked — the admin disabled the channel, withdrew their grant, or
they switched the channel off themselves — is declined on their *existing*
thread too. An already-bound conversation that keeps answering after access is
taken away is a security surprise, and ``ServerChannel.enabled`` is documented
as an absolute kill switch, which it would not be if the threads it had already
opened went on working. The decline uses the whitelist miss's reply verbatim;
see ``REPLY_DENIED``.

Trust model: the sender's email comes from a payload the adapter verified was
signed by the platform. That is the same trust tier the email integration
extends to IMAP, and it is what lets a whitelisted address be treated as an
identity. Sessions are owned by the sender's own platform user, so the blast
radius of a whitelist mistake is one empty auto-created account.

**One deliberate exception to that last sentence, since Phase 3 of
``docs/plans/channels_identity_unification/``: identity routing.** When a
sender has switched ``allow_identity_routing`` on and addresses a person who
shared an identity with them, routing may select *that person's* agent, and the
session is then owned by the identity owner rather than by the sender — it is
their agent, their space, their credentials, and their session list. It is
opt-in on both sides (the owner shares the identity and assigns it; the sender
turns identity routing on for the channel and keeps the per-person toggle), and
every message re-verifies the grant, so revocation bites immediately rather than
at the next thread. What does **not** change is the thread binding: it stays the
sender's, because "this thread belongs to this person" is what stops one member
of a group space from posting into another's conversation.
"""
from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from fastapi import Request
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import Session as DBSession, select

from app.models import (
    Agent,
    AgentBundle,
    AgentEnvironment,
    CHANNEL_BINDING_ACTIVE,
    CHANNEL_BINDING_FAILED,
    CHANNEL_BINDING_PENDING_INSTALL,
    ChannelAccessPolicy,
    ChannelThreadBinding,
    IdentityGrant,
    SecurityEventCreate,
    ServerChannel,
    SessionSender,
    User,
)
from app.models.events import security_event as security_event_constants
from app.services.common.email_patterns import match_email_pattern
from app.services.server_channels.channel_debug_buffer import (
    DEBUG_INSTALLING,
    DEBUG_NO_MATCH,
    DEBUG_RECEIVED,
    DEBUG_REJECTED,
    DEBUG_REPLIED,
    DEBUG_ROUTED,
    DEBUG_SEND_FAILED,
    ChannelDebugBuffer,
)
from app.services.routing import routing_trace
from app.services.sessions.channel_ingestion_service import (
    ChannelIngestionService,
    NoActiveEnvironmentError,
)
from app.services.server_channels.adapters.base import (
    ChannelInboundMessage,
    ChannelVerificationError,
)
from app.services.server_channels.adapters.registry import get_adapter
from app.services.server_channels.channel_outbound_service import (
    ChannelOutboundService,
)
from app.services.server_channels.channel_policy_service import (
    ChannelPolicyService,
    ResolvedChannelPolicy,
)
from app.services.server_channels.channel_routing_service import (
    ChannelRoutingService,
)
from app.services.server_channels.server_channel_service import ServerChannelService
from app.services.users.user_service import UserService

if TYPE_CHECKING:  # pragma: no cover — typing only
    from app.models import Session as ChatSession

logger = logging.getLogger(__name__)

_LOG_PREFIX = "[ChannelInbound]"


def _decision_detail(decision_id: uuid.UUID | None) -> dict[str, str]:
    """``{"trace_id": ...}`` — but only when a durable row was actually written.

    The live debug feed publishes this so an admin can jump from "no match" to
    the full routing trace. Emitting the id unconditionally would advertise a
    link that ``GET /admin/routing/traces/{id}`` 404s on whenever tracing is
    switched off, the origin is filtered out, or a persist was swallowed — a
    dead link in a diagnostic panel is worse than no link.
    """
    return {"trace_id": str(decision_id)} if decision_id else {}


def _debug_channel_key(channel: ServerChannel) -> str | None:
    """``channel``'s debug-buffer key, or ``None``. **Total by construction.**

    §11a Rule 2 in its narrowest useful form. ``channel.id`` looks like a field
    read and is not: every caller that reaches ``_reply`` does so after a
    ``db.commit()``, which expires the instance, so the read is a lazy reload
    and a reload of a concurrently deleted row raises ``ObjectDeletedError``.

    ``_route_and_bind`` solves the same problem by reading the id *before* the
    commit, while the instance is fresh. That is the better fix and it is not
    available here: the commit lives in the caller — six different callers,
    several frames up. So the read is made total instead, which is the same
    contract :func:`app.services.routing.routing_trace.clamp` and
    :func:`~app.services.routing.routing_trace.describe_exception` hold, for
    the same reason: it is evaluated as a bare argument expression, and
    ``ChannelDebugBuffer.record``'s own never-raises guard cannot reach back
    out to cover it.

    Returning ``None`` (rather than a placeholder key) is deliberate. An event
    filed under a key no channel has is unreachable through
    ``list_events(channel_id)``: it would look recorded while being invisible,
    and it would let a hostile or churning workload accrete buffers nobody can
    read. The caller drops the event instead — and this logs, so the drop is
    itself observable rather than silent.
    """
    try:
        return str(channel.id)
    except Exception:  # noqa: BLE001 — a debug aid must never break delivery
        logger.warning(
            "%s Could not identify a channel for the debug buffer (instance "
            "expired and its row is gone?) — dropping the event rather than "
            "filing it under a key no panel reads",
            _LOG_PREFIX,
            exc_info=True,
        )
        return None


def _log_detail(exc: BaseException) -> str:
    """``exc``'s full text for the application **log**. Total by construction.

    Deliberately not :func:`~app.services.routing.routing_trace.describe_exception`,
    and the split is about audience. The debug buffer is a superuser *read*
    surface, so what goes there is de-tainted down to type and status; the
    application log is an operator surface where the adapter's actual complaint
    ("permission denied for space X") is the whole diagnosis. Dropping it in
    both places would leave nobody able to answer why a notice failed.

    Nor is it :func:`~app.services.routing.routing_trace.clamp`, which reaches
    ``if not text`` before its own guard and so is not total against a raising
    ``__bool__`` — one of the shapes §11a Rule 2 names.

    Why pre-format at all, when ``logging`` interpolates lazily and swallows
    its own formatting errors: pytest's ``LogCaptureHandler`` overrides
    ``handleError`` to *re-raise*. Passing a raw ``exc`` therefore behaves one
    way in production and another under test — and the way it behaves under
    test is "destroys the exception this handler was recording". A guard whose
    correctness depends on which logging handler is installed is not a guard.
    Found by firing the poison object, not by reading this code.
    """
    try:
        return str(exc)[:2000]
    except Exception:  # noqa: BLE001 — the point of the helper
        try:
            return f"<unprintable {type(exc).__name__}>"
        except Exception:  # noqa: BLE001 — poisoned metaclass; still total
            return "<unprintable>"


# Environment statuses that mean "ready to take a message now".
_ENV_READY = {"running"}
# Terminal failures: the install will not become usable on its own. Both are
# genuinely terminal, unlike `critical_state` (see the note in `_flush_one`) —
# waiting out the age cap on them would just delay the inevitable.
_ENV_FAILED = {"error", "deprecated"}
# A binding that never reaches `running` is failed after this long rather than
# retrying forever. Bounds the flush loop without needing an attempt column:
# at a 45s tick this is ~40 attempts.
_PENDING_MAX_AGE_SECONDS = 30 * 60

# Static, deliberately uninformative replies. None of these tell an
# unauthenticated caller anything they didn't already know.
REPLY_DENIED = (
    "Sorry, you don't have access to this assistant. "
    "Please contact your administrator."
)
REPLY_WELCOME = (
    "Hi! Send me a message describing what you need and I'll find the right "
    "assistant for you."
)
REPLY_WORKING = "Got it — finding the right assistant for you…"
REPLY_STILL_SETTING_UP = (
    "Still setting up your assistant — I'll answer here as soon as it's ready."
)
REPLY_NO_MATCH = (
    "I couldn't find an assistant that can help with that. "
    "Please contact your administrator."
)
REPLY_INSTALLING = (
    "Setting up **{agent_name}** for you — first-time setup takes a few "
    "minutes. I'll reply here when it's ready."
)
REPLY_READY = "Your assistant is ready. Working on your message now…"
REPLY_SETUP_FAILED = (
    "Sorry — setting up your assistant failed. Please contact your "
    "administrator."
)
REPLY_TOO_MANY_QUEUED = (
    "I've got a lot queued up for you already — I'll work through those first, "
    "then you can send this again."
)
REPLY_THREAD_OWNED = (
    "This conversation belongs to someone else. Please start a new thread and "
    "I'll set you up with your own assistant."
)

# Cap on parked messages per binding: a thread that keeps talking while an
# environment builds should not grow an unbounded JSON blob.
_MAX_PARKED_MESSAGES = 20

# Throttle window for repeated denial / verification-failure security events
# from the same source, so a noisy prober can't flood the audit log.
_SECURITY_EVENT_THROTTLE_SECONDS = 300.0
_SECURITY_EVENT_MAX_KEYS = 5_000
_recent_security_events: dict[str, float] = {}

# In-process dedup for messages that arrive BEFORE a binding exists. The
# binding's `last_external_message_id` cannot cover first contact, and a
# redelivery there is expensive: two routing calls, possibly two installs.
# Bounded and TTL-swept — this key is derived from attacker-supplied input.
_RECENT_MESSAGE_TTL_SECONDS = 600.0
_RECENT_MESSAGE_MAX_KEYS = 5_000
_recent_message_ids: dict[str, float] = {}


class ChannelNotFound(Exception):
    """Unknown or disabled webhook token. Route maps to 404."""


class ChannelInboundService:
    """Webhook → routing → session. Stateless; all state is in the binding."""

    # ==================================================================
    # Entry point
    # ==================================================================

    @staticmethod
    async def handle_inbound(
        *,
        db: DBSession,
        webhook_token: str,
        request: Request,
        body: bytes,
    ) -> dict[str, Any]:
        """Run the inbound pipeline. Returns the webhook's sync response body.

        Raises ``ChannelNotFound`` (404) and ``ChannelVerificationError`` (403);
        every other outcome is a 200 with a static body, because a channel that
        gets a non-2xx retries the event forever.
        """
        # ---- 1. Channel resolution (enabled-only; 404 covers both cases) ----
        channel = ServerChannelService.get_by_webhook_token(db, webhook_token)
        if channel is None:
            raise ChannelNotFound()

        adapter = get_adapter(channel.channel_type)

        # ---- 2. VERIFICATION — the first thing that touches the payload ----
        # Fails closed. Nothing below this line runs unless the platform's
        # signature over this exact body checked out.
        try:
            inbound = await adapter.verify_inbound(request, channel, body)
        except ChannelVerificationError:
            await ChannelInboundService._audit_throttled(
                db=db,
                key=f"verify:{channel.id}",
                user_id=channel.created_by,
                event_type=security_event_constants.SERVER_CHANNEL_VERIFICATION_FAILED,
                severity="high",
                details={"server_channel_id": str(channel.id)},
            )
            ChannelDebugBuffer.record(
                channel_id=channel.id,
                direction="inbound",
                kind=DEBUG_REJECTED,
                summary="Signature verification failed — request rejected (403)",
                detail={"stage": "verify"},
            )
            raise

        # ---- Non-message events: ack cheaply, never error ----
        if inbound.event_kind == "ignored":
            ChannelDebugBuffer.record(
                channel_id=channel.id,
                direction="inbound",
                kind=DEBUG_REJECTED,
                summary="Authentic event the pipeline does not act on — acked",
                thread_key=inbound.thread_key,
                detail={"stage": "event_kind", "event_kind": "ignored"},
            )
            return {}
        if inbound.event_kind == "added_to_space":
            ChannelDebugBuffer.record(
                channel_id=channel.id,
                direction="inbound",
                kind=DEBUG_RECEIVED,
                summary="App added to a space — welcome reply sent",
                thread_key=inbound.thread_key,
                detail={"stage": "added_to_space"},
            )
            return adapter.build_sync_response(REPLY_WELCOME)

        ChannelDebugBuffer.record(
            channel_id=channel.id,
            direction="inbound",
            kind=DEBUG_RECEIVED,
            summary="Message received and signature verified",
            sender_email=inbound.sender_email,
            sender_display_name=inbound.sender_display_name,
            thread_key=inbound.thread_key,
            text=inbound.text,
            detail={
                "external_message_id": inbound.external_message_id or "",
                "external_user_id": inbound.external_user_id or "",
            },
        )

        if not inbound.sender_email:
            # Authentic event, but the platform gave us no address to identify
            # the sender by (a consumer-Gmail user on Google Chat). There is
            # nothing to whitelist against, so it is a denial — answered 200
            # with the standard text, never a 403, which the channel would
            # retry forever.
            ChannelDebugBuffer.record(
                channel_id=channel.id,
                direction="inbound",
                kind=DEBUG_REJECTED,
                summary="Sender has no verified email address — denied",
                sender_display_name=inbound.sender_display_name,
                thread_key=inbound.thread_key,
                detail={"stage": "sender_identity"},
            )
            return adapter.build_sync_response(REPLY_DENIED)
        if not inbound.thread_key:
            # No binding key means nothing can be bound; ack and drop.
            return {}

        # ---- 3. Redelivery dedup ----
        binding = ChannelInboundService._get_binding(
            db, channel.id, inbound.thread_key
        )
        if (
            binding is not None
            and inbound.external_message_id
            and binding.last_external_message_id == inbound.external_message_id
        ):
            logger.info(
                "%s Duplicate delivery of message %s on channel %s — acking",
                _LOG_PREFIX,
                inbound.external_message_id,
                channel.id,
            )
            return {}

        # First contact has no binding to dedup against, and a redelivery there
        # is the expensive one: two routing calls and possibly two installs.
        #
        # Deliberately gated on `binding is None`. Once a binding exists the
        # durable stamp above is the only dedup, and it is stamped ONLY after a
        # successful ingest — so a redelivery of a message we failed to process
        # is a recovery opportunity. Running this in-process check there would
        # ack and drop that redelivery (it records the key at webhook time,
        # before processing), silently defeating the recovery path
        # `_continue_thread` and `_park_message` are written to preserve.
        if (
            binding is None
            and inbound.external_message_id
            and ChannelInboundService._seen_recently(
                f"{channel.id}:{inbound.external_message_id}"
            )
        ):
            logger.info(
                "%s Duplicate pre-binding delivery of %s on channel %s — acking",
                _LOG_PREFIX,
                inbound.external_message_id,
                channel.id,
            )
            return {}

        # ---- 4. Whitelist — fails closed ----
        # `match_email_pattern` returns False for a NULL/empty pattern string,
        # so an unconfigured channel denies everyone. "*" is the only way to
        # allow all verified senders.
        if not match_email_pattern(inbound.sender_email, channel.email_whitelist):
            await ChannelInboundService._audit_throttled(
                db=db,
                key=f"denied:{channel.id}:{inbound.sender_email}",
                user_id=channel.created_by,
                event_type=security_event_constants.SERVER_CHANNEL_SENDER_DENIED,
                severity="medium",
                details={
                    "server_channel_id": str(channel.id),
                    "sender_email": inbound.sender_email,
                    "reason": "not_whitelisted",
                },
            )
            ChannelDebugBuffer.record(
                channel_id=channel.id,
                direction="inbound",
                kind=DEBUG_REJECTED,
                summary=(
                    "Sender is not allowed by the email whitelist — denied. "
                    "An empty whitelist denies everyone."
                ),
                sender_email=inbound.sender_email,
                sender_display_name=inbound.sender_display_name,
                thread_key=inbound.thread_key,
                detail={
                    "stage": "whitelist",
                    "whitelist": channel.email_whitelist or "(empty)",
                },
            )
            return adapter.build_sync_response(REPLY_DENIED)

        # ---- 5. User resolution (auto-register only if opted in) ----
        user = await ChannelInboundService._resolve_user(
            db=db, channel=channel, inbound=inbound
        )
        if user is None:
            ChannelDebugBuffer.record(
                channel_id=channel.id,
                direction="inbound",
                kind=DEBUG_REJECTED,
                summary=(
                    "No platform account for this sender and auto-registration "
                    "is off — denied"
                ),
                sender_email=inbound.sender_email,
                sender_display_name=inbound.sender_display_name,
                thread_key=inbound.thread_key,
                detail={"stage": "user_resolution"},
            )
            return adapter.build_sync_response(REPLY_DENIED)

        # ---- 6. Channel policy — resolved once, for every path below ----
        #
        # ``describe`` rather than ``resolve``: it is the same work (``resolve``
        # delegates to it) and it hands back *which* of the three terms failed,
        # which the debug entry below is allowed to say and the sender's reply
        # is not. Routing takes ``.policy`` and nothing else, per that view's
        # own docstring.
        #
        # Necessarily **after** user resolution, which means a restricted
        # channel can auto-register an account and then decline it: a grant is
        # keyed by user id, so there is no user to ask about until one exists.
        # The blast radius is unchanged — one empty passwordless account for a
        # sender who was already through the whitelist.
        policy_view = ChannelPolicyService.describe(db, channel, user.id)
        policy = policy_view.policy
        if not policy.is_available:
            # Audited on the same throttled bucket as the whitelist miss, and
            # under the same event type: from the outside these are one event —
            # "a verified sender was turned away" — and splitting them would
            # mean an admin hunting a denial had two feeds to search. The
            # ``reason`` field is what tells them apart.
            await ChannelInboundService._audit_throttled(
                db=db,
                key=f"unavailable:{channel.id}:{user.id}",
                user_id=channel.created_by,
                event_type=security_event_constants.SERVER_CHANNEL_SENDER_DENIED,
                severity="medium",
                details={
                    "server_channel_id": str(channel.id),
                    "sender_email": inbound.sender_email,
                    "reason": "channel_unavailable",
                },
            )
            ChannelDebugBuffer.record(
                channel_id=channel.id,
                direction="inbound",
                kind=DEBUG_REJECTED,
                summary=(
                    "This sender may not use this channel right now — denied. "
                    "The detail says which of the three terms failed: the "
                    "channel's own switch, their access to it, or their "
                    "per-user toggle."
                ),
                sender_email=inbound.sender_email,
                sender_display_name=inbound.sender_display_name,
                thread_key=inbound.thread_key,
                # Every value here is a plain bool off a frozen dataclass, so
                # nothing in this argument list can reach the database (§11a
                # Rule 2). The feed is superuser-only, which is why it may be
                # specific where the reply must not be.
                detail={
                    "stage": "channel_policy",
                    "channel_enabled": str(policy_view.channel_enabled),
                    "is_granted": str(policy_view.is_granted),
                    "is_enabled_for_user": str(policy_view.is_enabled_for_user),
                    "is_enabled_inherited": str(policy_view.is_enabled_inherited),
                },
            )
            # The whitelist miss's reply, verbatim and deliberately. "You are
            # not granted this channel", "you are not whitelisted" and "this
            # channel is switched off" must be one answer to an unauthenticated
            # sender, or the reply becomes an oracle that enumerates a server's
            # channel configuration one probe at a time.
            return adapter.build_sync_response(REPLY_DENIED)

        # ---- 7. Binding dispatch ----
        if binding is not None:
            # A thread belongs to exactly one person. In a group space another
            # whitelisted member can post into a thread already bound to
            # someone else's session — injecting their text into a stranger's
            # agent session, whose reply would then be posted where they can
            # read it. Multi-user rooms need a participant model and an
            # owner-approval flow (a listed future enhancement); until then the
            # correct behaviour is to decline, not to silently route.
            if binding.user_id != user.id:
                logger.warning(
                    "%s User %s posted into thread %s bound to user %s — declining",
                    _LOG_PREFIX,
                    user.id,
                    binding.thread_key,
                    binding.user_id,
                )
                return adapter.build_sync_response(REPLY_THREAD_OWNED)

            if binding.status == CHANNEL_BINDING_ACTIVE:
                ChannelInboundService._schedule(
                    ChannelInboundService._continue_thread(
                        binding_id=binding.id,
                        text=inbound.text,
                        external_message_id=inbound.external_message_id,
                        # Carried, not re-resolved — the same reasoning as the
                        # new-thread hop below. An existing identity thread is
                        # re-checked against ``allow_identity_routing`` on every
                        # message (see ``_ingest``), and it has to be checked
                        # against *this* message's reading of the sender's
                        # settings, which the decline gate above already made.
                        policy=policy,
                    ),
                    "channel_continue_thread",
                )
                # Silent ack: the agent's real reply arrives via the outbound
                # path, and a "working on it" here would double every turn.
                return {}

            if binding.status == CHANNEL_BINDING_PENDING_INSTALL:
                accepted = ChannelInboundService._park_message(db, binding, inbound)
                # Tell the truth when the queue is full: "I'll answer shortly"
                # would be a promise about a message we just dropped.
                return adapter.build_sync_response(
                    REPLY_STILL_SETTING_UP if accepted else REPLY_TOO_MANY_QUEUED
                )

            # `failed` — self-heal: drop the binding and route again from
            # scratch. A transient build failure must not wedge the thread.
            logger.info(
                "%s Clearing failed binding %s — re-routing", _LOG_PREFIX, binding.id
            )
            db.delete(binding)
            db.commit()

        # ---- 8-10. New thread: route (and possibly install) off-request ----
        ChannelInboundService._schedule(
            ChannelInboundService._route_new_thread(
                channel_id=channel.id,
                user_id=user.id,
                # Carried, not re-resolved. The background task opens its own
                # session and could resolve again, and must not: the decline
                # gate above and the routing below have to be answering from
                # one reading of this person's settings, or a message declined
                # by one and routed by the other becomes a state nobody can
                # reproduce. Plain frozen data, so it survives the hop.
                policy=policy,
                thread_key=inbound.thread_key,
                text=inbound.text,
                external_message_id=inbound.external_message_id,
                # Bare platform id — `SessionSender.from_channel` adds the
                # `channel_type:` prefix, so namespacing here would double it.
                external_user_id=inbound.external_user_id,
            ),
            "channel_route_new_thread",
        )
        return adapter.build_sync_response(REPLY_WORKING)

    # ==================================================================
    # Step 5 — user resolution
    # ==================================================================

    @staticmethod
    async def _resolve_user(
        *,
        db: DBSession,
        channel: ServerChannel,
        inbound: ChannelInboundMessage,
    ) -> User | None:
        """Find (or auto-create) the sender's platform account.

        Returns None for every denial case — an inactive account, or an unknown
        sender on a channel that has auto-registration off. The caller sends the
        same reply either way so the difference is not observable.
        """
        email = (inbound.sender_email or "").strip().lower()
        user = UserService.get_user_by_email(session=db, email=email)

        if user is not None:
            if not user.is_active:
                logger.info(
                    "%s Denying inactive user %s on channel %s",
                    _LOG_PREFIX,
                    user.id,
                    channel.id,
                )
                return None
            return user

        if not channel.auto_register_users:
            logger.info(
                "%s Unknown sender on channel %s and auto-register is off",
                _LOG_PREFIX,
                channel.id,
            )
            return None

        # The transport verified this address (Google signed the payload), so
        # the account is created confirmed and passwordless — same justification
        # as Google OAuth signup. The channel whitelist is the sole registration
        # gate; AUTH_WHITELIST_USER_DOMAINS is deliberately not re-checked.
        try:
            user = UserService.create_external_user(
                session=db,
                email=email,
                confirmed=True,
                provenance=f"server_channel:{channel.id}",
                passwordless=True,
            )
        except ValueError:
            logger.warning(
                "%s Rejected malformed sender address on channel %s",
                _LOG_PREFIX,
                channel.id,
            )
            return None

        await ChannelInboundService._audit(
            db=db,
            user_id=user.id,
            event_type=security_event_constants.SERVER_CHANNEL_USER_AUTO_REGISTERED,
            severity="medium",
            details={
                "server_channel_id": str(channel.id),
                "channel_type": channel.channel_type,
                "email": email,
            },
        )
        logger.info(
            "%s Auto-registered user %s from channel %s",
            _LOG_PREFIX,
            user.id,
            channel.id,
        )
        return user

    # ==================================================================
    # Step 6 — continue an existing thread
    # ==================================================================

    @staticmethod
    async def _continue_thread(
        *,
        binding_id: uuid.UUID,
        text: str,
        policy: ResolvedChannelPolicy,
        external_message_id: str | None = None,
    ) -> None:
        """Feed a message into the session an active binding already owns.

        ``policy`` is ``handle_inbound``'s resolution, carried in rather than
        re-read here. **One reading per message, and on this path the carried
        one is the only one** — the decline gate that let the message through
        and the identity-consent check in ``_ingest`` are answering from the
        same frozen value, so they cannot disagree about a channel edited
        mid-flight. (The scheduler's drain path has no live resolution to
        carry, so ``_flush_one`` makes its own, and that one is likewise the
        only reading for the messages it delivers.)
        """
        from app.core.db import create_session

        with create_session() as db:
            binding = db.get(ChannelThreadBinding, binding_id)
            if binding is None:
                return
            channel = db.get(ServerChannel, binding.server_channel_id)
            agent = db.get(Agent, binding.agent_id)
            user = db.get(User, binding.user_id)
            if channel is None or agent is None or user is None:
                return

            # Any messages left parked by an interrupted drain go first, so the
            # conversation stays in order.
            if binding.pending_messages:
                await ChannelInboundService._drain_parked(
                    db=db,
                    channel=channel,
                    binding=binding,
                    agent=agent,
                    user=user,
                    policy=policy,
                )
                # `_drain_parked` stops on the first failure and leaves the rest
                # parked. Ingesting the new message now would let it overtake
                # older ones the person is still waiting on — the exact
                # out-of-order delivery the drain is written to prevent. Park it
                # at the back of the queue instead; the next inbound message (or
                # a later drain) retries the whole queue in order.
                db.refresh(binding)
                if binding.pending_messages:
                    accepted = ChannelInboundService._append_parked(
                        db, binding, text, external_message_id, None
                    )
                    binding.updated_at = datetime.now(UTC)
                    db.add(binding)
                    db.commit()
                    # Same invariant `_park_message` holds: at the cap the
                    # message is gone, so say so. The failed drain already sent
                    # a generic setup-failed notice, but that does not tell the
                    # sender THIS message was dropped — and a silent drop here
                    # leaves them waiting for an answer that can never come.
                    if not accepted:
                        await ChannelInboundService._reply(
                            db, channel, binding.thread_key, REPLY_TOO_MANY_QUEUED
                        )
                    return

            await ChannelInboundService._ingest_or_fail(
                db=db,
                channel=channel,
                binding=binding,
                agent=agent,
                user=user,
                text=text,
                external_message_id=external_message_id,
                external_user_id=None,
                policy=policy,
            )

            # Record the delivered id only now. Stamping it at webhook time
            # would dedup a redelivery of a message we then failed to process,
            # losing it silently.
            if (
                external_message_id
                and binding.status == CHANNEL_BINDING_ACTIVE
                and binding.last_external_message_id != external_message_id
            ):
                binding.last_external_message_id = external_message_id
                db.add(binding)
                db.commit()

    # ==================================================================
    # Steps 8-10 — routing for a brand-new thread
    # ==================================================================

    @staticmethod
    async def _route_new_thread(
        *,
        channel_id: uuid.UUID,
        user_id: uuid.UUID,
        policy: ResolvedChannelPolicy,
        thread_key: str,
        text: str,
        external_message_id: str | None,
        external_user_id: str | None,
    ) -> None:
        """``decide()`` → bind → ingest.

        The decision itself — Pass 1 over the sender's installed agents, then
        Pass 2 over the auto-install catalog — lives in
        :meth:`ChannelRoutingService.decide` and has no effects of its own. What
        remains here is everything that *does* have effects: the thread binding,
        the session ingest, the install-and-park, the outbound reply, the debug
        feed entry, and the durable trace write.

        That split is what makes ``POST /admin/routing/simulate`` safe by
        construction: simulate calls ``decide`` and stops, so there is no
        ``simulate`` flag in this method to get a branch wrong about.

        ``policy`` is the resolution ``handle_inbound`` already made — the same
        one its decline gate consulted — carried in rather than resolved again
        here. It is a frozen dataclass of scalars, so it crosses into this
        background task the way ids and text do, and this method's
        freshly-opened session is never asked to re-derive an inherit rule.
        """
        from app.core.db import create_session

        with create_session() as db:
            channel = db.get(ServerChannel, channel_id)
            user = db.get(User, user_id)
            if channel is None or user is None:
                return

            # Every scalar the diagnostic records below need, read HERE — while
            # both instances are freshly loaded, and before the first
            # ``db.commit()``. §11a Rule 2: the test for an instrumentation
            # point is not "is the recorder guarded" but "can anything in the
            # caller's argument list raise".
            #
            # Hoisting these to just above their ``ChannelDebugBuffer.record``
            # call would not have been enough. That commit expires every
            # instance in this session, so ``channel.id`` / ``user.email`` are
            # lazy reloads wherever they are read afterwards, and a reload of a
            # row deleted concurrently raises ``ObjectDeletedError`` into the
            # broad ``except`` at the bottom of this method — REPLY_SETUP_FAILED
            # to a sender whose message routed perfectly. Moving the read a few
            # lines up its own block only relocates that raise inside the same
            # handler. Read before the commit and there is no reload at all:
            # from here on these are plain Python values that cannot touch the
            # database, whatever happens to the session or to the rows.
            debug_channel_id = channel.id
            sender_email = user.email

            try:
                # ---- Decide ----
                # Release the outer connection for the whole decision. The
                # worker threads open their own, and the pool is 15 wide
                # against a 40-thread limiter — holding one idle-in-transaction
                # here would let a burst of new threads stall unrelated
                # requests behind QueuePool timeouts. Nothing below depends on
                # outer-transaction continuity across the offload.
                #
                # One commit now covers both passes, where there used to be one
                # per pass: ``decide`` holds no session of ours, so between Pass
                # 1 and Pass 2 this session touches nothing and its connection
                # stays back in the pool for the whole cascade rather than being
                # re-taken for the interleaved ``db.get``.
                db.commit()
                # ``user_id`` / ``channel_id``, not ``user.id`` / ``channel.id``:
                # same values, but this method already holds them as plain
                # parameters, so reading them off the just-expired instances was
                # a database round trip that could raise — for nothing.
                decision = await ChannelRoutingService.decide(
                    user_id=user_id,
                    text=text,
                    policy=policy,
                    channel_id=channel_id,
                    thread_key=thread_key,
                )
                pass1_trace = decision.pass1_trace
                pass2_trace = decision.pass2_trace
                agent = db.get(Agent, decision.agent_id) if decision.agent_id else None
                if agent is not None:
                    # ``agent`` was just loaded by the ``db.get`` above, so
                    # these two are in-memory reads rather than the lazy
                    # reloads ``channel``/``user`` would be — but they are
                    # hoisted anyway, because §11a Rule 2 is a rule about the
                    # *shape* of an argument list, not about which reads happen
                    # to be safe today. An earlier version hoisted ``agent.name``
                    # alone, with a comment explaining exactly this hazard, and
                    # left ``channel.id`` / ``user.email`` / ``agent.id`` inline
                    # — which is why the rule is written as "sweep the pattern",
                    # not "guard the one that bit".
                    agent_name = agent.name
                    agent_ref = str(agent.id)
                    # Pass 1 was terminal, so this trace is the whole decision —
                    # ``persist_args`` says so rather than this call site
                    # deciding it a second time (simulate persists the same
                    # decision through the same rule; two call sites applying it
                    # by hand is how the admin list ends up comparing rows of
                    # different shapes).
                    #
                    # Offloaded, like the routing passes above. ``persist`` is
                    # synchronous and does an INSERT+COMMIT; run inline it does
                    # that ON THE EVENT LOOP, stalling every other request and
                    # stream in the process for the duration of the round trip.
                    # (It takes a second pooled connection either way — a worker
                    # thread needs one too. What the offload buys is that the
                    # loop is not the thing waiting on it.)
                    decision_id = await ChannelRoutingService.run_in_thread(
                        decision.persist_call()
                    )
                    ChannelDebugBuffer.record(
                        channel_id=debug_channel_id,
                        direction="inbound",
                        kind=DEBUG_ROUTED,
                        # Two sentences, because "installed agent" is false on
                        # the identity branch — that agent is someone else's,
                        # and the single most useful thing this line can say
                        # about such a message is that it left the sender's own
                        # workspace. Both operands are plain reads (a local
                        # string, a frozen-dataclass attribute), so this
                        # argument list still cannot raise (§11a Rule 2).
                        summary=(
                            f"Routed via identity to '{agent_name}' in another "
                            "user's workspace"
                            if decision.identity_grant is not None
                            else f"Routed to installed agent '{agent_name}'"
                        ),
                        sender_email=sender_email,
                        thread_key=thread_key,
                        detail={
                            "pass": "1",
                            "agent_id": agent_ref,
                            **_decision_detail(decision_id),
                        },
                    )
                    binding, created = ChannelInboundService._upsert_binding(
                        db=db,
                        channel=channel,
                        user=user,
                        agent=agent,
                        thread_key=thread_key,
                        status=CHANNEL_BINDING_ACTIVE,
                        external_message_id=external_message_id,
                    )
                    if not created:
                        # Another delivery for this brand-new thread won the
                        # race and already picked an agent. Defer to its
                        # binding — continuing with our own would create a
                        # session on a different agent than the binding names.
                        await ChannelInboundService._handle_lost_race(
                            db=db,
                            channel=channel,
                            binding=binding,
                            sender_user_id=user.id,
                            thread_key=thread_key,
                            text=text,
                            external_message_id=external_message_id,
                            external_user_id=external_user_id,
                            policy=policy,
                        )
                        return
                    await ChannelInboundService._ingest_or_fail(
                        db=db,
                        channel=channel,
                        binding=binding,
                        agent=agent,
                        user=user,
                        text=text,
                        external_message_id=external_message_id,
                        external_user_id=external_user_id,
                        # Set only when Stage 1 chose a *person* and Stage 2
                        # picked one of their agents (plan §2.3). It is what
                        # permits a session on an agent this sender does not
                        # own — and it is a claim, re-read in full by
                        # ``assert_access`` before anything is created.
                        # ``None`` on every other branch, which leaves the
                        # channel invariant exactly as strict as it was.
                        identity_grant=decision.identity_grant,
                        policy=policy,
                    )
                    return

                # ---- Pass 2 outcome: server-wide auto-install catalog ----
                # Already run inside ``decide`` above; from here on this method
                # only turns its verdict into effects.
                bundle_uuid = decision.bundle_uuid
                bundle = db.get(AgentBundle, bundle_uuid) if bundle_uuid else None
                bundle_display_name = bundle.display_name if bundle is not None else ""
                bundle_id = bundle.id if bundle is not None else None
                # ``debug_channel_id`` / ``sender_email`` come from the top of
                # this method, read before the first commit. Found by the Rule 2
                # sweep: this block hoisted its *bundle* reads but left
                # ``channel.id`` and ``user.email`` inline in both ``record``
                # calls below, exactly as Pass 1 did — two adjacent blocks
                # contradicting each other on the same question.
                #
                # **Persisted AFTER the effect it describes, in every branch.**
                # The single ``persist`` used to sit here, above both branches —
                # so a trace already settled as ``parked_install`` (Pass 2's
                # classifier settles it the moment it picks a bundle) became a
                # durable row claiming an install that had not been attempted
                # yet, and might never be: the bundle can vanish between the
                # classifier's pick and the ``db.get`` above, and
                # ``_install_and_park`` can fail outright. Either way the row
                # read ``outcome=parked_install, error=NULL`` — invisible to
                # ``?outcome=error``, the one filter that exists to find it.
                # Same defect class as the omitted Pass-2 stage: the trace
                # asserting something about execution that did not happen.
                #
                # Deliberately NOT solved by writing a provisional row and
                # correcting it — a corrected row is worse than a late one, and
                # the correction is the write most likely to be the one that
                # fails. Each branch persists once, after it knows what really
                # happened, and the ``ChannelDebugBuffer`` record follows the
                # persist because it is the ``decision_id`` link that wanted the
                # early write, not the buffer entry.
                if bundle is None:
                    if decision.agent_id is not None and pass1_trace is not None:
                        # Pass 1 picked an agent that vanished before we could
                        # bind it — the mirror of the bundle case below, and the
                        # ONLY way control reaches here with an agent selected.
                        #
                        # Without this the row is the worst shape this table can
                        # hold: ``outcome=routed, selected_agent_id=<deleted>,
                        # error=NULL`` — invisible to ``?outcome=error`` AND to
                        # ``?outcome=no_match``, while the sender was told no
                        # match and the live feed logged one. A durable record
                        # contradicting both the reply and the feed, and looking
                        # like the most ordinary success the table has. §11a
                        # Rule 1, on the write path.
                        #
                        # It arrived with the ``decide()`` split: Pass 2 is now
                        # gated on Pass 1 returning no agent, so the vanished
                        # agent no longer falls through to a Pass 2 whose own
                        # trace would have supplied the verdict. ``record_error``
                        # settles the trace as ``error`` and clears the stale
                        # selection through the same settler the bundle branch
                        # uses. ``decision.agent_id`` is a plain frozen-dataclass
                        # attribute, so this argument list cannot raise.
                        pass1_trace.record_error(
                            f"selected agent {decision.agent_id} no longer exists"
                        )
                    if bundle_uuid is not None and pass2_trace is not None:
                        # The classifier picked a bundle that no longer exists —
                        # a data-integrity failure between the pick and this
                        # read, not "nothing matched". Recorded so the row
                        # settles as ``error`` instead of keeping a
                        # ``parked_install`` verdict for an install nobody
                        # attempted. ``bundle_uuid`` is a plain value returned by
                        # the thread target, so this argument cannot raise.
                        pass2_trace.record_error(
                            f"selected bundle {bundle_uuid} no longer exists"
                        )
                    # ONE row for the whole decision: Pass 1's stages are folded
                    # in rather than written as their own ``no_match`` row, which
                    # would otherwise appear on the ``?outcome=no_match`` filter
                    # for every message Pass 2 went on to handle.
                    decision_id = await ChannelRoutingService.run_in_thread(
                        decision.persist_call()
                    )
                    # "Nothing matched" is the least actionable line the panel
                    # can show. `summarize` turns both passes into one sentence
                    # naming the candidates and why each was dropped; it never
                    # raises and returns "" when it has nothing to add.
                    diagnosis = routing_trace.summarize(pass1_trace, pass2_trace)
                    # Both hoisted to plain locals before the record call, and
                    # both describe what actually happened rather than the
                    # common case. ``pass`` used to be the literal "2" here,
                    # which since the ``decide()`` split can be a pass that
                    # never ran; and the vanished-agent branch above reaches
                    # this record having selected something, so "nothing
                    # matched" would be flatly untrue.
                    reached_pass = "2" if decision.catalog_ran else "1"
                    if decision.agent_id is not None:
                        headline = (
                            "Routing picked an agent that no longer exists — "
                            "nothing to bind"
                        )
                    else:
                        headline = (
                            "No installed agent and no auto-install catalog "
                            "candidate matched this message"
                        )
                    ChannelDebugBuffer.record(
                        channel_id=debug_channel_id,
                        direction="inbound",
                        kind=DEBUG_NO_MATCH,
                        summary=headline + (f" — {diagnosis}" if diagnosis else ""),
                        sender_email=sender_email,
                        thread_key=thread_key,
                        detail={
                            "pass": reached_pass,
                            **_decision_detail(decision_id),
                        },
                    )
                    await ChannelInboundService._reply(db, channel, thread_key, REPLY_NO_MATCH)
                    return

                try:
                    await ChannelInboundService._install_and_park(
                        db=db,
                        channel=channel,
                        user=user,
                        bundle=bundle,
                        thread_key=thread_key,
                        text=text,
                        external_message_id=external_message_id,
                        external_user_id=external_user_id,
                    )
                except Exception as exc:
                    # The park is what ``parked_install`` asserts, so a failed
                    # park must not persist as one. Recorded and written here
                    # rather than left to the handler below, which knows nothing
                    # about traces; then re-raised unchanged so the sender still
                    # gets REPLY_SETUP_FAILED exactly as before.
                    if pass2_trace is not None:
                        pass2_trace.record_error(exc)
                    await ChannelRoutingService.run_in_thread(decision.persist_call())
                    raise

                decision_id = await ChannelRoutingService.run_in_thread(
                    decision.persist_call()
                )
                ChannelDebugBuffer.record(
                    channel_id=debug_channel_id,
                    direction="inbound",
                    kind=DEBUG_INSTALLING,
                    summary=f"Auto-installing '{bundle_display_name}' — message parked",
                    sender_email=sender_email,
                    thread_key=thread_key,
                    detail={
                        "pass": "2",
                        "bundle_uuid": str(bundle_id),
                        **_decision_detail(decision_id),
                    },
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "%s Routing failed for channel %s thread %s",
                    _LOG_PREFIX,
                    channel_id,
                    thread_key,
                )
                await ChannelInboundService._reply(
                    db, channel, thread_key, REPLY_SETUP_FAILED
                )

    @staticmethod
    async def _handle_lost_race(
        *,
        db: DBSession,
        channel: ServerChannel,
        binding: ChannelThreadBinding,
        sender_user_id: uuid.UUID,
        thread_key: str,
        text: str,
        external_message_id: str | None,
        external_user_id: str | None,
        policy: ResolvedChannelPolicy,
    ) -> None:
        """Deliver this message via the binding that won the creation race.

        Never proceeds with the loser's own routing result: the winner's
        binding already names an agent, and creating a session on a different
        one would leave the binding permanently lying about who is answering.

        The same one-person-per-thread rule the synchronous path enforces
        applies here, and it must be re-checked rather than assumed. Two
        members of a group space can @-mention the bot in the same new thread;
        both see no binding, both run a full routing pass — the window is LLM
        latency, seconds — and both try to create it. Without this check the
        loser's message would be ingested into (or parked onto) the *winner's*
        session, putting one external user's text in another's history. Since
        this runs in a background task, the refusal is delivered as a message
        rather than a sync reply.
        """
        if binding.user_id != sender_user_id:
            logger.warning(
                "%s User %s lost the binding race for thread %s to user %s — "
                "declining rather than delivering into their session",
                _LOG_PREFIX,
                sender_user_id,
                thread_key,
                binding.user_id,
            )
            await ChannelInboundService._reply(
                db, channel, thread_key, REPLY_THREAD_OWNED
            )
            return

        if binding.status == CHANNEL_BINDING_PENDING_INSTALL:
            accepted = ChannelInboundService._append_parked(
                db, binding, text, external_message_id, external_user_id
            )
            db.add(binding)
            db.commit()
            # Never drop in silence: at the cap the message is gone, so say so
            # rather than leaving the sender waiting for an answer to it.
            await ChannelInboundService._reply(
                db,
                channel,
                thread_key,
                REPLY_STILL_SETTING_UP if accepted else REPLY_TOO_MANY_QUEUED,
            )
            return

        agent = db.get(Agent, binding.agent_id)
        user = db.get(User, binding.user_id)
        if agent is None or user is None:
            return
        await ChannelInboundService._ingest_or_fail(
            db=db,
            channel=channel,
            binding=binding,
            agent=agent,
            user=user,
            text=text,
            external_message_id=external_message_id,
            external_user_id=external_user_id,
            # The loser's own reading, which is the reading for this message —
            # the winner's binding decides which agent answers, never whose
            # consent applies.
            policy=policy,
        )

    @staticmethod
    async def _ingest_or_fail(
        *,
        db: DBSession,
        channel: ServerChannel,
        binding: ChannelThreadBinding,
        agent: Agent,
        user: User,
        text: str,
        external_message_id: str | None,
        external_user_id: str | None,
        policy: ResolvedChannelPolicy,
        identity_grant: IdentityGrant | None = None,
    ) -> None:
        """Ingest, leaving the binding in a coherent state on every outcome.

        Without this, a Pass 1 ingest failure would leave an ``active`` binding
        with no session and no error — a state the self-heal path (which only
        triggers on ``failed``) never cleans up.

        ``identity_grant`` is forwarded verbatim and defaults to ``None``: only
        the first message of a freshly-routed thread has a routing decision
        behind it. Every later message reconstructs its own grant from the
        session row (see :meth:`_ingest`), because a stale claim carried across
        turns is exactly what re-verification exists to prevent.

        ``policy`` is required, not defaulted: it is the sender's reading for
        *this* message and there is no safe value to invent for a caller that
        forgot it — a permissive default would silently re-open identity
        routing for a person who has switched it off.
        """
        try:
            await ChannelInboundService._ingest(
                db=db,
                channel=channel,
                binding=binding,
                agent=agent,
                user=user,
                text=text,
                sender_external_id=external_user_id,
                identity_grant=identity_grant,
                policy=policy,
            )
        except NoActiveEnvironmentError:
            # Terminal, not transient: `SessionService.create_session` returns
            # None only when the agent has NO `active_environment_id` at all
            # (a suspended or still-building environment succeeds and is woken
            # by `initiate_stream`). Nothing will fix that on its own, so fail
            # rather than parking a message behind a wait that never ends. The
            # binding self-heals — the next inbound message deletes it and
            # re-routes.
            logger.info(
                "%s Agent %s has no active environment — failing binding %s",
                _LOG_PREFIX,
                agent.id,
                binding.id,
            )
            ChannelInboundService._fail_binding(
                db, binding, "Agent has no active environment"
            )
            await ChannelOutboundService.notify_progress(
                db=db, channel=channel, binding=binding, text=REPLY_SETUP_FAILED
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "%s Ingest failed for binding %s", _LOG_PREFIX, binding.id
            )
            # The failing ingest may have left the transaction poisoned; without
            # this rollback the status write below would itself raise, out of
            # the handler.
            db.rollback()
            ChannelInboundService._fail_binding(db, binding, str(exc))
            await ChannelOutboundService.notify_progress(
                db=db, channel=channel, binding=binding, text=REPLY_SETUP_FAILED
            )

    @staticmethod
    async def _install_and_park(
        *,
        db: DBSession,
        channel: ServerChannel,
        user: User,
        bundle: AgentBundle,
        thread_key: str,
        text: str,
        external_message_id: str | None,
        external_user_id: str | None,
    ) -> None:
        """Install the matched bundle and park the message until the env is up."""
        from app.services.bundles.install_service import InstallService

        agent = await InstallService.install_bundle(db, user, bundle)

        await ChannelInboundService._audit(
            db=db,
            user_id=user.id,
            event_type=security_event_constants.SERVER_CHANNEL_AUTO_INSTALL,
            severity="low",
            details={
                "server_channel_id": str(channel.id),
                "bundle_uuid": str(bundle.id),
                "bundle_id": bundle.bundle_id,
                "agent_id": str(agent.id),
            },
        )

        binding, created = ChannelInboundService._upsert_binding(
            db=db,
            channel=channel,
            user=user,
            agent=agent,
            thread_key=thread_key,
            status=CHANNEL_BINDING_PENDING_INSTALL,
            external_message_id=external_message_id,
        )
        if not created:
            # Raced. Defer to the winner — parking onto an already-`active`
            # binding would strand the message, since the flush loop only ever
            # selects `pending_install`.
            await ChannelInboundService._handle_lost_race(
                db=db,
                channel=channel,
                binding=binding,
                sender_user_id=user.id,
                thread_key=thread_key,
                text=text,
                external_message_id=external_message_id,
                external_user_id=external_user_id,
            )
            return

        ChannelInboundService._append_parked(
            db, binding, text, external_message_id, external_user_id
        )
        db.add(binding)
        db.commit()

        # Announced only now: before the binding is confirmed ours, a lost race
        # would have told the sender "setting up X for you" and then declined
        # them in the next breath.
        await ChannelInboundService._reply(
            db,
            channel,
            thread_key,
            REPLY_INSTALLING.format(agent_name=bundle.display_name),
        )

    # ==================================================================
    # Pending flush (scheduler entry point)
    # ==================================================================

    @staticmethod
    async def flush_pending_bindings(db: DBSession) -> int:
        """Deliver parked messages for bindings whose environment is now ready.

        Called by the scheduler, and directly by tests. Each binding is handled
        in its own try/except so one bad row never starves the rest.

        Returns the number of bindings advanced to ``active``.
        """
        bindings = db.exec(
            select(ChannelThreadBinding).where(
                ChannelThreadBinding.status == CHANNEL_BINDING_PENDING_INSTALL
            )
        ).all()

        advanced = 0
        for binding in bindings:
            try:
                if await ChannelInboundService._flush_one(db, binding):
                    advanced += 1
            except Exception:  # noqa: BLE001
                logger.exception(
                    "%s Failed to flush binding %s", _LOG_PREFIX, binding.id
                )
                # Every binding in this tick shares one session. A failure that
                # left the transaction in a failed state would make each
                # remaining binding raise PendingRollbackError on its first
                # query — one bad row starving the rest, which is precisely
                # what the per-binding try/except exists to prevent. Reset the
                # session so the next iteration starts clean.
                try:
                    db.rollback()
                except Exception:  # noqa: BLE001 — never mask the real error
                    logger.exception(
                        "%s Could not roll back after binding %s",
                        _LOG_PREFIX,
                        binding.id,
                    )
        return advanced

    @staticmethod
    async def _flush_one(db: DBSession, binding: ChannelThreadBinding) -> bool:
        channel = db.get(ServerChannel, binding.server_channel_id)
        agent = db.get(Agent, binding.agent_id)
        user = db.get(User, binding.user_id)
        if channel is None or agent is None or user is None:
            return False

        # The scheduler reaches here with no live resolution to carry — this
        # tick is not downstream of any webhook — so it legitimately makes its
        # own. **One reading per message still holds**: this fresh value is the
        # only reading for every parked message drained below, exactly as the
        # webhook's carried value is the only reading on ``_continue_thread``.
        # Neither path ever holds two, so there is nothing for a channel edited
        # mid-flight to make disagree; the edit simply lands on the next
        # message, or the next tick.
        policy = ChannelPolicyService.resolve(db, channel, user.id)

        env = ChannelInboundService._agent_environment(db, agent)
        status = env.status if env else None
        failure: str | None = None

        if env is None or agent.active_environment_id is None:
            # No environment to wait for. This does not self-heal, so retrying
            # forever would wedge the thread silently.
            failure = "Agent has no active environment"
        elif status in _ENV_FAILED:
            failure = f"Environment build failed (status={status})"
        # NOTE: `critical_state` is deliberately NOT a failure here, diverging
        # from plan §8 ("env error/critical -> failed"). That wording predates
        # the current semantics: `critical_state` coexists with
        # status="running" — a degraded-but-up container whose sessions, chat
        # and terminals all keep working. It would have answered the user, so
        # telling an external person "setup failed" and burning the binding
        # would be wrong. Only `status == "error"` is terminal.
        elif status not in _ENV_READY:
            # Still building/starting/suspended. Bounded: a binding that never
            # becomes ready fails rather than retrying indefinitely.
            if ChannelInboundService._binding_age_seconds(binding) > _PENDING_MAX_AGE_SECONDS:
                failure = (
                    f"Environment did not become ready in time (status={status})"
                )
            else:
                return False

        if failure is not None:
            ChannelInboundService._fail_binding(db, binding, failure)
            await ChannelOutboundService.notify_progress(
                db=db, channel=channel, binding=binding, text=REPLY_SETUP_FAILED
            )
            return False

        # Environment is ready. Flip to `active` and announce it BEFORE
        # draining: the flush query only selects `pending_install`, so this is
        # what stops a mid-drain failure from re-announcing "ready" and
        # re-delivering messages on every 45-second tick.
        if binding.pending_messages:
            await ChannelOutboundService.notify_progress(
                db=db, channel=channel, binding=binding, text=REPLY_READY
            )
        binding.status = CHANNEL_BINDING_ACTIVE
        binding.last_error = None
        binding.updated_at = datetime.now(UTC)
        db.add(binding)
        db.commit()

        await ChannelInboundService._drain_parked(
            db=db,
            channel=channel,
            binding=binding,
            agent=agent,
            user=user,
            policy=policy,
        )
        return True

    @staticmethod
    def _binding_age_seconds(binding: ChannelThreadBinding) -> float:
        created = binding.created_at
        if created is None:
            return 0.0
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        return (datetime.now(UTC) - created).total_seconds()

    @staticmethod
    async def _drain_parked(
        *,
        db: DBSession,
        channel: ServerChannel,
        binding: ChannelThreadBinding,
        agent: Agent,
        user: User,
        policy: ResolvedChannelPolicy,
    ) -> None:
        """Deliver parked messages in arrival order, exactly once each.

        Each entry is removed only AFTER its ingest succeeds, and the removal
        is committed immediately — so a crash mid-drain re-delivers at most the
        one in flight, and a failure leaves the rest parked rather than losing
        them. Stops on the first failure: the messages are a conversation, and
        replaying later ones out of order past a gap would be worse than
        waiting.
        """
        while True:
            parked = list(binding.pending_messages or [])
            if not parked:
                return
            entry = parked[0] or {}
            text = entry.get("text") or ""

            if text:
                try:
                    await ChannelInboundService._ingest(
                        db=db,
                        channel=channel,
                        binding=binding,
                        agent=agent,
                        user=user,
                        text=text,
                        sender_external_id=entry.get("external_user_id"),
                        policy=policy,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.exception(
                        "%s Failed to deliver parked message for binding %s",
                        _LOG_PREFIX,
                        binding.id,
                    )
                    db.rollback()
                    binding.last_error = str(exc)[:2000]
                    db.add(binding)
                    db.commit()
                    # Never strand silently: the remaining messages stay parked
                    # and will be retried on the next inbound message, but the
                    # person is owed an answer now.
                    await ChannelOutboundService.notify_progress(
                        db=db,
                        channel=channel,
                        binding=binding,
                        text=REPLY_SETUP_FAILED,
                    )
                    return

            binding.pending_messages = parked[1:]
            flag_modified(binding, "pending_messages")
            binding.last_error = None  # healed; don't leave a stale diagnosis
            binding.updated_at = datetime.now(UTC)
            db.add(binding)
            db.commit()

    # ==================================================================
    # Ingestion
    # ==================================================================

    @staticmethod
    async def _ingest(
        *,
        db: DBSession,
        channel: ServerChannel,
        binding: ChannelThreadBinding,
        agent: Agent,
        user: User,
        text: str,
        policy: ResolvedChannelPolicy,
        sender_external_id: str | None = None,
        identity_grant: IdentityGrant | None = None,
    ) -> None:
        """Hand the message to the canonical ingestion service.

        The session is resumed by id when the binding already has one, and
        created otherwise. ``session_id`` is NULL both before the first message
        and after the session was deleted (the SET NULL cascade), so one branch
        covers first contact and recovery alike.

        ``sender_external_id`` is the platform-native id captured at the webhook
        (``google_chat:users/123``). It is metadata only — the authoritative
        identity is ``user.id`` — so when it is unavailable (a resume, or a
        parked message from before this field was recorded) the constructor
        falls back to the platform user id.

        **``user`` is always the binding's user — and this method now enforces
        that rather than merely asserting it.** Every caller already satisfies
        it (``_route_new_thread`` passes the sender it just bound;
        ``_continue_thread``, ``_handle_lost_race`` and the drain paths all
        load ``db.get(User, binding.user_id)``), but "every caller today" is
        not something a downstream guard can rest on, and Phase 4 adds a second
        poller into these helpers. The check at the top of the body makes
        ``sender.platform_user_id == binding.user_id`` true by construction.

        What that buys, precisely: it protects the **unchanged, non-identity**
        arm of ``ChannelIngestionService._verify_resume_sender``
        (``existing.user_id == sender.platform_user_id``). The binding pins
        ``session_id`` and the sender is the binding's user, so an ordinary
        channel session can only ever be resumed by the person the thread
        belongs to. It is **not** what makes that method's identity exception
        safe: that arm compares ``existing.identity_caller_id`` against the
        sender on the session row itself, and refuses a deliberately
        mismatched ``(binding, user)`` pair without any help from here. And
        neither arm is the authorization — ``assert_access`` runs first, on
        every message, and re-reads the whole grant.

        **Identity: the session may be owned by someone other than the
        sender.** Two sources, never mixed:

        - *First message of a routed thread* — ``identity_grant`` is the claim
          Stage 2 produced (plan §2.3). The identity ids are stamped onto the
          new session, and ``session.user_id`` becomes the identity owner.
        - *Every later message* — the claim is rebuilt from the session row
          itself, which carries all three ids. Rebuilding rather than caching
          is the point: ``assert_access`` re-reads and re-verifies all six
          conditions on **every** message, so an owner who revokes mid-thread
          is honored on the next turn rather than at the next thread. That
          extends Phase 2's decided semantic — a revoked sender is declined on
          an existing thread exactly as on a new one — and, like it, the
          decline is detail-free: the ``PermissionError`` is caught by
          ``_ingest_or_fail``, which sends the same generic reply every other
          failure gets.

        **The sender's own consent is re-read on every message too**, from
        ``policy`` — which is this message's single reading of their channel
        settings, carried in from the webhook or made fresh by the scheduler
        drain, never both. Turning ``allow_identity_routing`` off (or resetting
        the channel, which drops the row and returns the column to its ``false``
        default) therefore stops the *existing* identity thread on its next
        message, not only the next new one. A consent switch that could not be
        withdrawn on the very conversation it authorized would be no consent at
        all; and this is the same semantic already decided for a revoked grant,
        applied to the other side of the same permission. An ordinary channel
        thread has no grant, so it never consults the flag.

        ``integration_type`` stays ``channel_<type>`` on the identity path.
        ``ChannelOutboundService._resolve_channel_session`` gates on that
        prefix, so a session stamped ``identity_mcp`` here would route
        correctly, run correctly, and never deliver a reply.
        """
        from app.core.db import create_session
        from app.models import Session as ChatSession

        # The invariant this method's contract is written on, enforced at the
        # only place all of its callers meet. A mismatched pair here would put
        # one person's text into another's thread; refusing it costs one
        # comparison and cannot be forgotten by a future caller the way a
        # docstring can.
        if user.id != binding.user_id:
            raise PermissionError(
                "channel ingest sender does not own the binding: "
                f"user.id={user.id}, binding.user_id={binding.user_id}"
            )

        sender = SessionSender.from_channel(
            channel_type=channel.channel_type,
            external_user_id=sender_external_id or str(user.id),
            platform_user_id=user.id,
            display_name=None,
        )

        # Resume only if the session still exists. The FK nulls `session_id` on
        # delete, but a delete racing this read would leave a stale pointer that
        # `resolve_or_create_session` would reject with "Session not found".
        existing: ChatSession | None = None
        thread_key: uuid.UUID | None = binding.session_id
        if thread_key is not None:
            existing = db.get(ChatSession, thread_key)
            if existing is None:
                thread_key = None

        if existing is not None:
            # Resume: the row is the only admissible source. A grant handed in
            # by a caller would be describing a different message.
            grant = ChannelInboundService._resume_identity_grant(existing)
        else:
            # Create. On the recovery branch — the binding's session was
            # deleted, so `thread_key` was just cleared — a continue/drain call
            # arrives with no grant, and if the bound agent is a foreign one
            # `assert_access` refuses. That is deliberate rather than repaired
            # here: nothing in this call is evidence the identity is still
            # shared, and re-deriving one from the binding would be inventing
            # an authorization the routing layer never issued. The refusal
            # fails the binding, which self-heals — the next message deletes it
            # and re-routes, producing a fresh, freshly-verified grant.
            grant = identity_grant

        # Consent, re-read per message from THIS message's single reading (see
        # the docstring). Short-circuited on `grant is None`, so an ordinary
        # channel thread never reaches the flag and is unaffected.
        #
        # Raised bare and detail-free on purpose: `_ingest_or_fail` turns it
        # into the one generic reply every other refusal gets, so this decline
        # is indistinguishable from a revoked grant, a vanished binding, or a
        # failed environment. A reply that named it would be an oracle telling
        # an external sender which gate closed.
        if grant is not None and not policy.allow_identity_routing:
            raise PermissionError(
                "identity routing is switched off for this sender on this "
                "channel"
            )

        extra_session_kwargs: dict[str, Any] | None = None
        if thread_key is None:
            session_metadata_extra: dict[str, Any] = {
                "server_channel_id": str(channel.id),
                "thread_key": binding.thread_key,
                "sender_external_id": sender.external_id,
            }
            extra_session_kwargs = {
                "session_metadata_extra": session_metadata_extra
            }
            if grant is not None:
                # Attribution for the person who did not start this
                # conversation. The session opens in the identity OWNER's
                # space, so it appears in their list containing a stranger's
                # message; without a name the only identification is a raw uuid
                # in a column nothing renders. Same key App MCP's identity path
                # stamps (`external_a2a_context_handler`), so the session view
                # needs one branch for both, not two.
                #
                # `identity_owner_name` — the other half of that pair — is
                # deliberately NOT stamped: on this path the owner is the
                # session's own user, so it would be telling the reader their
                # own name.
                session_metadata_extra["identity_caller_name"] = (
                    (user.full_name or "").strip() or user.email
                )
                extra_session_kwargs.update(
                    {
                        # Consumed by `_select_session_owner_id`: the session is
                        # the identity owner's, not the sender's.
                        "identity_owner_id": grant.owner_id,
                        # Post-create stamps; all three are already in
                        # `ChannelIngestionService._STAMPABLE_COLUMNS`, and all
                        # three are what the resume path above reads back.
                        "identity_caller_id": user.id,
                        "identity_binding_id": grant.binding_id,
                        "identity_binding_assignment_id": grant.assignment_id,
                    }
                )

        result = await ChannelIngestionService.ingest_inbound_message(
            db=db,
            agent=agent,
            sender=sender,
            thread_key=thread_key,
            content=text,
            integration_type=f"channel_{channel.channel_type}",
            access_policy=ChannelAccessPolicy(
                # The owner the three-way invariant is checked against. On the
                # identity path that is the identity owner (which is also
                # `agent.owner_id`), not the sender — the sender is the
                # `identity_caller_id`, and the grant beside it is what makes
                # the mismatch legitimate.
                expected_owner_id=grant.owner_id if grant is not None else user.id,
                identity_grant=grant,
            ),
            get_fresh_db_session=create_session,
            extra_session_kwargs=extra_session_kwargs,
        )

        if binding.session_id != result.session.id:
            binding.session_id = result.session.id
            binding.updated_at = datetime.now(UTC)
            db.add(binding)
            db.commit()

    @staticmethod
    def _resume_identity_grant(session: "ChatSession") -> IdentityGrant | None:
        """Rebuild the identity claim for a session being resumed.

        ``None`` for an ordinary channel session — every column below is NULL
        there, and the caller then uses the sender's own id as the expected
        owner exactly as before.

        The three ids were stamped when the session was created and are re-read
        rather than remembered, which is the whole point: this produces a
        *claim*, and ``ChannelIngestionService.assert_access`` re-verifies all
        six conditions behind it against the database on this message. A row
        whose ids no longer describe a live, enabled, correctly-linked binding
        is refused, so revocation takes effect on the very next message.

        ``owner_id`` comes from ``session.user_id`` because that is what
        identity ownership *means* here (the session lives in the owner's
        space). It is not trusted: condition 6 pins
        ``binding.owner_id == agent.owner_id == owner_id``, so a row whose
        ``user_id`` disagrees with its own binding is rejected rather than
        honored.

        All three columns are required. A partially-stamped row cannot be
        completed by guessing — the missing piece is precisely the linkage the
        six conditions exist to check — so it degrades to "no grant" and is
        refused by the ordinary invariant.
        """
        if (
            session.identity_caller_id is None
            or session.identity_binding_id is None
            or session.identity_binding_assignment_id is None
        ):
            return None
        return IdentityGrant(
            owner_id=session.user_id,
            binding_id=session.identity_binding_id,
            assignment_id=session.identity_binding_assignment_id,
        )

    # ==================================================================
    # Binding helpers
    # ==================================================================

    @staticmethod
    def _get_binding(
        db: DBSession, channel_id: uuid.UUID, thread_key: str
    ) -> ChannelThreadBinding | None:
        return db.exec(
            select(ChannelThreadBinding).where(
                ChannelThreadBinding.server_channel_id == channel_id,
                ChannelThreadBinding.thread_key == thread_key,
            )
        ).first()

    @staticmethod
    def _upsert_binding(
        *,
        db: DBSession,
        channel: ServerChannel,
        user: User,
        agent: Agent,
        thread_key: str,
        status: str,
        external_message_id: str | None,
    ) -> tuple[ChannelThreadBinding, bool]:
        """Create the binding, tolerating a concurrent first-message race.

        Returns ``(binding, created)``. Two messages arriving simultaneously in
        one brand-new thread both reach routing; the unique constraint on
        (server_channel_id, thread_key) lets exactly one insert win, and the
        loser catches IntegrityError and re-reads the winner's row (the
        ``ServerConfigService.get_or_create`` idiom).

        ``created`` matters: the loser must defer to the winner's agent rather
        than proceeding with its own routing result, or the binding ends up
        naming one agent while its session belongs to another.
        """
        binding = ChannelThreadBinding(
            server_channel_id=channel.id,
            thread_key=thread_key,
            user_id=user.id,
            agent_id=agent.id,
            status=status,
            last_external_message_id=external_message_id,
        )
        db.add(binding)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            existing = ChannelInboundService._get_binding(db, channel.id, thread_key)
            if existing is None:
                raise
            logger.info(
                "%s Lost binding race for thread %s — using existing binding %s",
                _LOG_PREFIX,
                thread_key,
                existing.id,
            )
            return existing, False
        db.refresh(binding)
        return binding, True

    @staticmethod
    def _park_message(
        db: DBSession,
        binding: ChannelThreadBinding,
        inbound: ChannelInboundMessage,
    ) -> bool:
        """Park an inbound message. Returns False when the cap refused it."""
        accepted = ChannelInboundService._append_parked(
            db,
            binding,
            inbound.text,
            inbound.external_message_id,
            inbound.external_user_id,
        )
        # Only claim delivery for a message we actually kept. Stamping a
        # message the cap refused would mark it deduped, so a redelivery would
        # be dropped too and the user would lose it silently.
        if accepted and inbound.external_message_id:
            binding.last_external_message_id = inbound.external_message_id
        binding.updated_at = datetime.now(UTC)
        db.add(binding)
        db.commit()
        return accepted

    @staticmethod
    def _append_parked(
        db: DBSession,
        binding: ChannelThreadBinding,
        text: str,
        external_message_id: str | None,
        external_user_id: str | None = None,
    ) -> bool:
        """Append to the parked queue. Returns False when the cap refused it.

        Reassigns the list rather than mutating in place, and flags the
        attribute: a plain ``.append()`` on a JSON column is not dirty-tracked
        and the commit would silently drop the message.
        """
        parked = list(binding.pending_messages or [])
        if len(parked) >= _MAX_PARKED_MESSAGES:
            # Refuse the newest rather than evicting the oldest: the first
            # parked message is the one routing chose the agent from, and
            # dropping it would leave the session opening on a follow-up with
            # no context.
            logger.warning(
                "%s Binding %s is at the parked-message cap — dropping newest",
                _LOG_PREFIX,
                binding.id,
            )
            return False
        parked.append(
            {
                "text": text,
                "external_message_id": external_message_id,
                "external_user_id": external_user_id,
                "received_at": datetime.now(UTC).isoformat(),
            }
        )
        binding.pending_messages = parked
        flag_modified(binding, "pending_messages")
        return True

    @staticmethod
    def _fail_binding(
        db: DBSession, binding: ChannelThreadBinding, error: str
    ) -> None:
        binding.status = CHANNEL_BINDING_FAILED
        binding.last_error = (error or "")[:2000]
        binding.updated_at = datetime.now(UTC)
        db.add(binding)
        db.commit()

    @staticmethod
    def _agent_environment(db: DBSession, agent: Agent) -> AgentEnvironment | None:
        if agent.active_environment_id:
            env = db.get(AgentEnvironment, agent.active_environment_id)
            if env is not None:
                return env
        return db.exec(
            select(AgentEnvironment).where(AgentEnvironment.agent_id == agent.id)
        ).first()

    # ==================================================================
    # Misc helpers
    # ==================================================================

    @staticmethod
    def _seen_recently(key: str) -> bool:
        """True if ``key`` was seen inside the TTL. Records it either way.

        Bounded and swept, because the key embeds attacker-supplied input: an
        unbounded process dict on a public endpoint is a memory-exhaustion
        vector in its own right.
        """
        import time

        now = time.monotonic()
        cutoff = now - _RECENT_MESSAGE_TTL_SECONDS
        if len(_recent_message_ids) > _RECENT_MESSAGE_MAX_KEYS:
            for stale in [k for k, t in _recent_message_ids.items() if t < cutoff]:
                _recent_message_ids.pop(stale, None)
            if len(_recent_message_ids) > _RECENT_MESSAGE_MAX_KEYS:
                _recent_message_ids.clear()

        last = _recent_message_ids.get(key)
        _recent_message_ids[key] = now
        return last is not None and last >= cutoff

    @staticmethod
    def _schedule(coro: Any, name: str) -> None:
        from app.utils import create_task_with_error_logging

        create_task_with_error_logging(coro, name)

    @staticmethod
    async def _reply(
        db: DBSession, channel: ServerChannel, thread_key: str, text: str
    ) -> None:
        """Post a standalone message into a thread that may have no binding."""
        # §11a Rule 2, at the one instrumentation point in this file that sits
        # INSIDE an ``except`` — which is why it is fixed ahead of its known
        # siblings rather than with them. Everywhere else an unguarded argument
        # expression becomes a 500 that the platform retries; here it would
        # *replace* the exception it was recording. The delivery failure this
        # panel exists to show would vanish, and the caller would be handed a
        # database error in its place. Losing the diagnosis is categorically
        # worse than failing loudly, so this one does not wait.
        #
        # Two expressions were exposed, not one: ``channel.id`` and the
        # ``f"...{exc}"`` summary. Python evaluates both *before* entering
        # ``ChannelDebugBuffer.record``, so the buffer's never-raises guard
        # covered neither. The test is not "is the recorder guarded" but "can
        # anything in the caller's argument list raise".
        #
        # ``channel.id`` is resolved once, above the ``try``, through a total
        # helper: hoisting it a few lines up inside the same handler would only
        # relocate the raise, and reading it unguarded above the ``try`` would
        # trade a swallowed diagnosis for a notice that is never even attempted.
        debug_channel_id = _debug_channel_key(channel)
        try:
            adapter = get_adapter(channel.channel_type)
            await adapter.send_message(channel, thread_key, text)
            if debug_channel_id is not None:
                ChannelDebugBuffer.record(
                    channel_id=debug_channel_id,
                    direction="outbound",
                    kind=DEBUG_REPLIED,
                    summary="Pipeline notice delivered",
                    thread_key=thread_key,
                    text=text,
                )
        except Exception as exc:  # noqa: BLE001 — best effort
            # ``describe_exception`` rather than ``f"{exc}"``. It is total by
            # construction — an exception with a raising ``__str__`` comes back
            # as ``"unavailable"`` instead of detonating in this handler — and
            # it is already this file's answer to "stringify an exception
            # inside an argument list", so the fix reuses the established tool
            # rather than growing a second local one.
            #
            # It drops the exception's message body, and that is a gain here
            # rather than a cost: an adapter's HTTP error routinely echoes the
            # request it just made, and the request this method makes carries
            # the channel's service-account credentials into a buffer that is a
            # read surface. What diagnoses a delivery failure — the exception
            # type and the HTTP status — is exactly what survives. The full
            # message is not lost either: the ``logger.warning`` below still
            # carries it, where formatting is lazy and inside ``logging``'s own
            # error handling.
            failure = routing_trace.describe_exception(exc)
            if debug_channel_id is not None:
                ChannelDebugBuffer.record(
                    channel_id=debug_channel_id,
                    direction="outbound",
                    kind=DEBUG_SEND_FAILED,
                    summary=f"Notice delivery failed: {failure}",
                    thread_key=thread_key,
                    text=text,
                )
            logger.warning(
                "%s Could not deliver notice to channel=%s thread=%s: %s",
                _LOG_PREFIX,
                # The same hoisted value: this argument list is evaluated
                # eagerly too, so an inline ``channel.id`` here would destroy
                # the original exception just as surely as the one above.
                debug_channel_id or "unknown",
                thread_key,
                # ``_log_detail(exc)``, not ``exc``: see that helper. The
                # interpolation ``logging`` would do later is not covered by
                # anything this handler controls.
                _log_detail(exc),
            )

    @staticmethod
    async def _audit(
        *,
        db: DBSession,
        user_id: uuid.UUID | None,
        event_type: str,
        severity: str,
        details: dict[str, Any],
    ) -> None:
        """Write a SecurityEvent, never letting audit failure break the flow."""
        if user_id is None:
            # DEFERRED (known gap): rejection events are attributed to the
            # channel's creator, and `ServerChannel.created_by` is ON DELETE
            # SET NULL. Deleting the superuser who created a channel therefore
            # silently stops verification-failure and denial auditing for it
            # from then on. The event still reaches the application log.
            logger.info(
                "%s %s (no user to attribute): %s", _LOG_PREFIX, event_type, details
            )
            return
        try:
            from app.services.events.security_event_service import SecurityEventService

            await SecurityEventService.create_event(
                session=db,
                user_id=user_id,
                data=SecurityEventCreate(
                    event_type=event_type, severity=severity, details=details
                ),
            )
        except Exception:  # noqa: BLE001
            logger.exception("%s Failed to write security event %s", _LOG_PREFIX, event_type)

    @staticmethod
    async def _audit_throttled(
        *,
        db: DBSession,
        key: str,
        user_id: uuid.UUID | None,
        event_type: str,
        severity: str,
        details: dict[str, Any],
    ) -> None:
        """Rate-limited audit for attacker-triggerable events.

        A prober hitting a valid token with bad JWTs would otherwise write one
        row per request. Process-local, like every other in-memory throttle
        here — a multi-worker deployment gets one row per worker per window,
        which is still bounded.
        """
        import time

        now = time.monotonic()
        last = _recent_security_events.get(key)
        if last is not None and now - last < _SECURITY_EVENT_THROTTLE_SECONDS:
            return
        # Bounded like its neighbours: the key embeds a sender address, so an
        # attacker varying it must not be able to grow this dict without limit.
        if len(_recent_security_events) > _SECURITY_EVENT_MAX_KEYS:
            cutoff = now - _SECURITY_EVENT_THROTTLE_SECONDS
            for stale in [k for k, t in _recent_security_events.items() if t < cutoff]:
                _recent_security_events.pop(stale, None)
            if len(_recent_security_events) > _SECURITY_EVENT_MAX_KEYS:
                _recent_security_events.clear()
        _recent_security_events[key] = now
        await ChannelInboundService._audit(
            db=db,
            user_id=user_id,
            event_type=event_type,
            severity=severity,
            details=details,
        )


__all__ = ["ChannelInboundService", "ChannelNotFound"]
