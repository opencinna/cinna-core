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
    6. binding / routing    session work, off the request path

Steps 1–5 are cheap and run inline. Everything from 6 on can be slow (an LLM
routing call, an install, an environment build), so it runs as a background
task and the webhook answers immediately with a short static acknowledgement.
The real reply arrives asynchronously through ``ChannelOutboundService``.

Trust model: the sender's email comes from a payload the adapter verified was
signed by the platform. That is the same trust tier the email integration
extends to IMAP, and it is what lets a whitelisted address be treated as an
identity. Sessions are always owned by the sender's own platform user, so the
blast radius of a whitelist mistake is one empty auto-created account.
"""
from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import Request
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import Session as DBSession, select

from app.models import (
    Agent,
    AgentBundle,
    AgentBundleRevision,
    AgentEnvironment,
    CHANNEL_BINDING_ACTIVE,
    CHANNEL_BINDING_FAILED,
    CHANNEL_BINDING_PENDING_INSTALL,
    ChannelAccessPolicy,
    ChannelThreadBinding,
    SecurityEventCreate,
    ServerAutoInstallBundle,
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
from app.services.server_channels.server_channel_service import ServerChannelService
from app.services.users.user_service import UserService

logger = logging.getLogger(__name__)

_LOG_PREFIX = "[ChannelInbound]"

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

        # ---- 6. Binding dispatch ----
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

        # ---- 7-9. New thread: route (and possibly install) off-request ----
        ChannelInboundService._schedule(
            ChannelInboundService._route_new_thread(
                channel_id=channel.id,
                user_id=user.id,
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
        *, binding_id: uuid.UUID, text: str, external_message_id: str | None = None
    ) -> None:
        """Feed a message into the session an active binding already owns."""
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
                    db=db, channel=channel, binding=binding, agent=agent, user=user
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
    # Steps 7-9 — routing for a brand-new thread
    # ==================================================================

    @staticmethod
    async def _route_new_thread(
        *,
        channel_id: uuid.UUID,
        user_id: uuid.UUID,
        thread_key: str,
        text: str,
        external_message_id: str | None,
        external_user_id: str | None,
    ) -> None:
        """Pass 1 (installed agents) then Pass 2 (auto-install catalog)."""
        from app.core.db import create_session

        with create_session() as db:
            channel = db.get(ServerChannel, channel_id)
            user = db.get(User, user_id)
            if channel is None or user is None:
                return

            try:
                # ---- Pass 1: the sender's OWN installed agents ----
                # Offloaded: routing ends in a blocking LLM HTTP call, and this
                # coroutine runs on the main event loop. Left inline it would
                # stall every other request and stream in the process for the
                # duration of the provider cascade.
                # Release the outer connection for the duration of the LLM
                # cascade. The worker opens its own, and the pool is 15 wide
                # against a 40-thread limiter — holding one idle-in-transaction
                # here would let a burst of new threads stall unrelated
                # requests behind QueuePool timeouts. Nothing below depends on
                # outer-transaction continuity across the offload.
                db.commit()
                agent_id = await ChannelInboundService._in_thread(
                    ChannelInboundService._route_installed_in_thread, user.id, text
                )
                agent = db.get(Agent, agent_id) if agent_id else None
                if agent is not None:
                    ChannelDebugBuffer.record(
                        channel_id=channel.id,
                        direction="inbound",
                        kind=DEBUG_ROUTED,
                        summary=f"Routed to installed agent '{agent.name}'",
                        sender_email=user.email,
                        thread_key=thread_key,
                        detail={"pass": "1", "agent_id": str(agent.id)},
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
                    )
                    return

                # ---- Pass 2: server-wide auto-install catalog ----
                # Same offload rationale as Pass 1.
                db.commit()  # same rationale as Pass 1
                bundle_uuid = await ChannelInboundService._in_thread(
                    ChannelInboundService._route_catalog_in_thread, user.id, text
                )
                bundle = db.get(AgentBundle, bundle_uuid) if bundle_uuid else None
                if bundle is None:
                    ChannelDebugBuffer.record(
                        channel_id=channel.id,
                        direction="inbound",
                        kind=DEBUG_NO_MATCH,
                        summary=(
                            "No installed agent and no auto-install catalog "
                            "candidate matched this message"
                        ),
                        sender_email=user.email,
                        thread_key=thread_key,
                        detail={"pass": "2"},
                    )
                    await ChannelInboundService._reply(db, channel, thread_key, REPLY_NO_MATCH)
                    return

                ChannelDebugBuffer.record(
                    channel_id=channel.id,
                    direction="inbound",
                    kind=DEBUG_INSTALLING,
                    summary=f"Auto-installing '{bundle.display_name}' — message parked",
                    sender_email=user.email,
                    thread_key=thread_key,
                    detail={"pass": "2", "bundle_uuid": str(bundle.id)},
                )

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
    async def _in_thread(fn: Any, *args: Any) -> Any:
        """Run a blocking callable off the event loop.

        Both routing passes end in a synchronous LLM HTTP call (and the
        provider manager cascades through providers sequentially). This
        coroutine runs on the main loop, so calling them inline would block
        the whole process — externally triggerable, since any whitelisted
        sender can open a new thread.

        ``fn`` must NOT close over the caller's DB session: ``run_sync`` cannot
        interrupt a running thread, so a cancelled task would close the session
        out from under a mid-query worker. The thread targets below each open
        their own session and return plain ids (the pattern at
        ``message_service.py`` ``_run_in_thread``).

        Capacity note: anyio's default thread limiter is 40. With a webhook
        rate limit of 120/min per token, a burst of new threads queues here
        rather than growing unbounded — queueing is the intended degradation,
        and it is bounded by the limiter rather than by memory.
        """
        import functools

        import anyio.to_thread

        return await anyio.to_thread.run_sync(functools.partial(fn, *args))

    @staticmethod
    def _route_installed_in_thread(user_id: uuid.UUID, text: str) -> uuid.UUID | None:
        """Thread target for Pass 1. Owns its session; returns an agent id."""
        from app.core.db import create_session

        with create_session() as db:
            user = db.get(User, user_id)
            if user is None:
                return None
            agent = ChannelInboundService._route_installed(db, user, text)
            return agent.id if agent is not None else None

    @staticmethod
    def _route_catalog_in_thread(user_id: uuid.UUID, text: str) -> uuid.UUID | None:
        """Thread target for Pass 2. Owns its session; returns a bundle id."""
        from app.core.db import create_session

        with create_session() as db:
            user = db.get(User, user_id)
            if user is None:
                return None
            bundle = ChannelInboundService._route_catalog(db, user, text)
            return bundle.id if bundle is not None else None

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
    ) -> None:
        """Ingest, leaving the binding in a coherent state on every outcome.

        Without this, a Pass 1 ingest failure would leave an ``active`` binding
        with no session and no error — a state the self-heal path (which only
        triggers on ``failed``) never cleans up.
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
    def _route_installed(db: DBSession, user: User, text: str) -> Agent | None:
        """Pass 1 — route over the sender's installed agents.

        **Ownership filter.** ``AppMCPRoutingService.route_message`` answers with
        every route *effective for* the user, which is a broader set than the
        agents they own: identity routes deliberately resolve to another user's
        agent, and an admin-created route can point anywhere. Handing an
        external caller one of those would put their session inside somebody
        else's workspace, so two filters apply:

          1. reject ``is_identity`` outright — by construction someone else's
             agent, and the identity flow's own consent model doesn't cover an
             anonymous external sender;
          2. require ``agent.owner_id == user.id`` — the authoritative check,
             which also catches admin routes pointing at a foreign agent.

        This is the same invariant ``ChannelIngestionService.assert_access``
        asserts for ``channel_caller``; enforcing it here means the pipeline
        declines cleanly (falls through to Pass 2) instead of raising.
        """
        from app.services.app_mcp.app_mcp_routing_service import AppMCPRoutingService

        # DEFERRED (known gap): this router logs the message text at INFO
        # (`app_mcp_routing_service` "[Stage1] Routing message ..." and
        # `app_agent_router.py`). Plan §4 requires message text never be logged
        # at info level. Not changed here because the logger is shared with the
        # live App MCP feature — but note the content class has changed: it is
        # now EXTERNAL, non-platform users' text, not the internal traffic
        # those lines were written for. Downgrade to debug when that log can be
        # touched safely.
        try:
            result = AppMCPRoutingService.route_message(db, user.id, text)
        except Exception:  # noqa: BLE001 — router outage must not 500 the webhook
            logger.exception("%s Pass 1 routing failed", _LOG_PREFIX)
            return None

        if result is None:
            return None

        if result.is_identity:
            logger.info(
                "%s Pass 1 matched identity route for user %s — not eligible for "
                "channel routing (agent is not the sender's own install)",
                _LOG_PREFIX,
                user.id,
            )
            return None

        agent = db.get(Agent, result.agent_id)
        if agent is None:
            return None
        if agent.owner_id != user.id:
            logger.warning(
                "%s Pass 1 matched agent %s owned by %s for sender %s — rejected "
                "(channel sessions must run on the sender's own install)",
                _LOG_PREFIX,
                agent.id,
                agent.owner_id,
                user.id,
            )
            return None

        logger.info(
            "%s Pass 1 matched own install %s for user %s", _LOG_PREFIX, agent.id, user.id
        )
        return agent

    @staticmethod
    def _route_catalog(db: DBSession, user: User, text: str) -> AgentBundle | None:
        """Pass 2 — classify against the server-wide auto-install list.

        Candidates must satisfy all three: not already installed by this user,
        installable *by this user* per catalog visibility (list membership is
        not a grant), and carrying a router trigger prompt to classify on.
        """
        from app.services.ai_functions.ai_functions_service import AIFunctionsService
        from app.services.bundles.catalog_service import CatalogService

        entries = db.exec(select(ServerAutoInstallBundle)).all()
        if not entries:
            return None

        candidates: list[dict[str, Any]] = []
        by_id: dict[str, AgentBundle] = {}

        for entry in entries:
            bundle = db.get(AgentBundle, entry.bundle_uuid)
            if bundle is None or bundle.latest_revision_id is None:
                continue

            # Already installed → Pass 1 would have handled it if it matched.
            # Publisher installs count as installed: a publisher whose own
            # bundle is on the list should not get a second consumer copy
            # provisioned behind their back by a chat message.
            already = db.exec(
                select(Agent.id).where(
                    Agent.bundle_uuid == bundle.id,
                    Agent.owner_id == user.id,
                )
            ).first()
            if already is not None:
                continue

            # Visibility gate — the auto-install list never bypasses it.
            if not CatalogService.user_can_install(db, bundle, user):
                logger.debug(
                    "%s Bundle %s not installable by user %s — skipping",
                    _LOG_PREFIX,
                    bundle.id,
                    user.id,
                )
                continue

            revision = db.get(AgentBundleRevision, bundle.latest_revision_id)
            trigger = (revision.router_trigger_prompt or "").strip() if revision else ""
            if not trigger:
                continue

            candidates.append(
                {
                    "id": str(bundle.id),
                    "name": bundle.display_name,
                    "trigger_prompt": trigger,
                }
            )
            by_id[str(bundle.id)] = bundle

        if not candidates:
            return None

        result = AIFunctionsService.route_to_agent(text, candidates)
        if result is None or not result.agent_id:
            return None
        return by_id.get(str(result.agent_id))

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
            db=db, channel=channel, binding=binding, agent=agent, user=user
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
        sender_external_id: str | None = None,
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
        """
        from app.core.db import create_session
        from app.models import Session as ChatSession

        sender = SessionSender.from_channel(
            channel_type=channel.channel_type,
            external_user_id=sender_external_id or str(user.id),
            platform_user_id=user.id,
            display_name=None,
        )

        # Resume only if the session still exists. The FK nulls `session_id` on
        # delete, but a delete racing this read would leave a stale pointer that
        # `resolve_or_create_session` would reject with "Session not found".
        thread_key: uuid.UUID | None = binding.session_id
        if thread_key is not None and db.get(ChatSession, thread_key) is None:
            thread_key = None

        extra_session_kwargs: dict[str, Any] | None = None
        if thread_key is None:
            extra_session_kwargs = {
                "session_metadata_extra": {
                    "server_channel_id": str(channel.id),
                    "thread_key": binding.thread_key,
                    "sender_external_id": sender.external_id,
                }
            }

        result = await ChannelIngestionService.ingest_inbound_message(
            db=db,
            agent=agent,
            sender=sender,
            thread_key=thread_key,
            content=text,
            integration_type=f"channel_{channel.channel_type}",
            access_policy=ChannelAccessPolicy(expected_owner_id=user.id),
            get_fresh_db_session=create_session,
            extra_session_kwargs=extra_session_kwargs,
        )

        if binding.session_id != result.session.id:
            binding.session_id = result.session.id
            binding.updated_at = datetime.now(UTC)
            db.add(binding)
            db.commit()

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
        try:
            adapter = get_adapter(channel.channel_type)
            await adapter.send_message(channel, thread_key, text)
            ChannelDebugBuffer.record(
                channel_id=channel.id,
                direction="outbound",
                kind=DEBUG_REPLIED,
                summary="Pipeline notice delivered",
                thread_key=thread_key,
                text=text,
            )
        except Exception as exc:  # noqa: BLE001 — best effort
            ChannelDebugBuffer.record(
                channel_id=channel.id,
                direction="outbound",
                kind=DEBUG_SEND_FAILED,
                summary=f"Notice delivery failed: {exc}",
                thread_key=thread_key,
                text=text,
            )
            logger.warning(
                "%s Could not deliver notice to channel=%s thread=%s: %s",
                _LOG_PREFIX,
                channel.id,
                thread_key,
                exc,
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
