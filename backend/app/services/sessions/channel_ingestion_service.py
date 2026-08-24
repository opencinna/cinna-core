"""
ChannelIngestionService — single orchestration point for inbound session
work across A2A, App MCP, web-UI, and internal triggers.

Glue over existing primitives (`SessionService`, `AccessTokenService`,
`AppMCPRoutingService`); never re-implements message creation, stream
initiation, or DB inserts. See
`docs/drafts/channel-ingestion-service_plan.md`.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any
from uuid import UUID

from sqlmodel import Session as DBSession

from app.models import (
    Agent,
    ChannelAccessPolicy,
    IdentityGrant,
    IngestionResult,
    Session,
    SessionCreate,
    SessionSender,
)
from app.services.sessions.session_service import SessionService

logger = logging.getLogger(__name__)

_LOG_PREFIX = "[ChannelIngestion]"

#: Sender kinds ``create_identity_session`` will build a session for. An
#: allowlist rather than a comment, because the failure mode of the wrong kind
#: is a *committed* session owned by the wrong user — see that method's
#: docstring. Grows in **Phase 3** of
#: ``docs/plans/channels_identity_unification/`` — the same change that teaches
#: ``ChannelIngestionService._select_session_owner_id`` how a
#: ``channel_caller`` sender owns an identity session, never ahead of it.
_IDENTITY_SESSION_SENDER_KINDS: frozenset[str] = frozenset({"mcp_caller"})


class NoActiveEnvironmentError(RuntimeError):
    """Raised by resolve_or_create_session when the agent has no active environment.

    A dedicated subclass (rather than bare RuntimeError) so callers can
    catch this specific failure mode without swallowing unrelated runtime errors.
    """


class ChannelIngestionService:
    """Stateless orchestration over `SessionService` for inbound channels."""

    @staticmethod
    async def ingest_inbound_message(
        *,
        db: DBSession,
        agent: Agent,
        sender: SessionSender,
        thread_key: UUID | None,
        content: str,
        integration_type: str,
        access_policy: ChannelAccessPolicy,
        get_fresh_db_session: Callable[[], Any],
        file_ids: list[UUID] | None = None,
        backend_base_url: str | None = None,
        answers_to_message_id: UUID | None = None,
        extra_session_kwargs: dict[str, Any] | None = None,
    ) -> IngestionResult:
        """Run the full inbound ingestion flow (plan §4.1).

        Orchestration only — every step delegates:
          1. `assert_access` — raises on denial.
          2. `resolve_or_create_session` — `(session, is_new_session)`.
          3. `SessionService.send_session_message` — message + stream kick.
          4. Map the dict return into an `IngestionResult`.

        ``extra_session_kwargs`` is forwarded to ``resolve_or_create_session``
        so callers can stamp create-time columns whitelisted by that helper
        (``access_token_id``, ``source_task_id``, ``email_thread_id``,
        ``sender_email``) and post-create columns / metadata listed in
        ``_STAMPABLE_COLUMNS``. Ignored on resume.
        """
        # Step 1: access gating.
        ChannelIngestionService.assert_access(
            db=db, agent=agent, sender=sender, policy=access_policy
        )

        # Step 2: resolve or create the session.
        session, is_new_session = ChannelIngestionService.resolve_or_create_session(
            db=db,
            agent=agent,
            sender=sender,
            thread_key=thread_key,
            integration_type=integration_type,
            extra_session_kwargs=extra_session_kwargs,
        )

        # Step 3: delegate message creation + stream initiation. For A2A
        # scoped tokens carry the access_token_id forward so slash commands
        # (e.g. /files) can render token-signed links.
        access_token_id: UUID | None = None
        if sender.kind == "a2a_caller" and access_policy.require_access_token_scope:
            try:
                access_token_id = UUID(access_policy.require_access_token_scope.sub)
            except (TypeError, ValueError):
                access_token_id = None

        result = await SessionService.send_session_message(
            session_id=session.id,
            user_id=session.user_id,
            content=content,
            file_ids=file_ids,
            answers_to_message_id=answers_to_message_id,
            get_fresh_db_session=get_fresh_db_session,
            initiate_streaming=True,
            agent_id=None,  # session is already resolved
            access_token_id=access_token_id,
            backend_base_url=backend_base_url,
            integration_type=integration_type if is_new_session else None,
        )

        # Step 4: map the dict return into `IngestionResult`. `message` is a
        # passthrough of `send_session_message`'s `"message"` key — populated
        # for both error and command_executed paths so callers don't need to
        # re-query the session to recover command response text.
        action = result.get("action")
        message_id = result.get("message_id")
        message = result.get("message")
        streaming_initiated = action in ("streaming", "pending")

        return IngestionResult(
            session=session,
            message_id=message_id,
            is_new_session=is_new_session,
            streaming_initiated=streaming_initiated,
            action=action,
            message=message,
        )

    # ------------------------------------------------------------------
    # Session resolution / creation.
    # ------------------------------------------------------------------

    @staticmethod
    def resolve_or_create_session(
        *,
        db: DBSession,
        agent: Agent,
        sender: SessionSender,
        thread_key: UUID | None,
        integration_type: str | None,
        extra_session_kwargs: dict[str, Any] | None = None,
    ) -> tuple[Session, bool]:
        """Resolve an existing session or create a new one (plan §4.2).

        Returns `(session, is_new_session)`. On resume, verifies the
        sender matches the existing session per the §4.2 sender-kind table.
        On create, picks the owner and stamps post-create extras (caller_id,
        identity_caller_id, session_metadata) per the same table.
        """
        extra_kwargs = dict(extra_session_kwargs or {})

        if thread_key is not None:
            existing = SessionService.get_session(db, thread_key)
            if existing is None:
                raise ValueError(f"Session not found: {thread_key}")
            ChannelIngestionService._verify_resume_sender(existing, sender)
            return existing, False

        session_owner_id = ChannelIngestionService._select_session_owner_id(
            agent=agent, sender=sender, extra_session_kwargs=extra_kwargs
        )

        session_data = SessionCreate(
            agent_id=agent.id,
            title=extra_kwargs.pop("title", None),
            mode=extra_kwargs.pop("mode", "conversation"),
            guest_share_id=extra_kwargs.pop("guest_share_id", None),
            webapp_share_id=extra_kwargs.pop("webapp_share_id", None),
            dashboard_block_id=extra_kwargs.pop("dashboard_block_id", None),
        )

        # Named args supported by SessionService.create_session.
        create_kwargs: dict[str, Any] = {
            key: extra_kwargs.pop(key)
            for key in (
                "access_token_id",
                "source_task_id",
                "email_thread_id",
                "sender_email",
            )
            if key in extra_kwargs
        }

        # Everything else is a post-create stamp (see `_stamp_new_session`).
        post_create_stamps = extra_kwargs

        session = SessionService.create_session(
            db_session=db,
            user_id=session_owner_id,
            data=session_data,
            integration_type=integration_type,
            **create_kwargs,
        )
        if session is None:
            raise NoActiveEnvironmentError(
                "Failed to create session — agent has no active environment"
            )

        ChannelIngestionService._stamp_new_session(
            db=db,
            session=session,
            post_create_stamps=post_create_stamps,
        )

        logger.info(
            "%s created new session %s for agent %s (sender.kind=%s, integration_type=%s)",
            _LOG_PREFIX,
            session.id,
            agent.id,
            sender.kind,
            integration_type,
        )
        return session, True

    @staticmethod
    def create_identity_session(
        *,
        db: DBSession,
        agent: Agent,
        sender: SessionSender,
        grant: IdentityGrant,
        integration_type: str,
        mode: str = "conversation",
        session_metadata_extra: dict[str, Any] | None = None,
    ) -> Session:
        """Create a session in the identity **owner's** space for a foreign caller.

        The one place identity sessions are built. It used to live inline in
        ``AppMCPRequestHandler._create_identity_session``, which made "a caller
        may hold a session inside somebody else's workspace" an App-MCP-only
        capability; any surface that wanted it would have had to rebuild the
        ownership and stamping rules, and a surface that rebuilt them slightly
        differently is the failure this consolidates away.

        Ownership is the non-obvious part and does not change here:
        ``session.user_id`` is the **identity owner** (the agent runs in their
        space, on their credentials) while ``identity_caller_id`` records who
        is talking. That inversion is why the grant exists and why
        ``assert_access`` re-reads it.

        ``sender`` is a parameter rather than built here because it is the one
        thing that genuinely differs per surface: App MCP passes
        ``SessionSender.from_app_mcp(caller, identity_caller_user_id=caller)``,
        and a channel passes ``SessionSender.from_channel(...)``.
        ``integration_type`` is likewise the caller's to choose — App MCP
        identity sessions stay ``"identity_mcp"``.

        **Where the grant is actually verified.** ``assert_access`` consults
        ``policy.identity_grant`` on its ``channel_caller`` arm only. The one
        sender kind that reaches here today is ``mcp_caller``, whose arm does
        **not** consult it — App MCP re-verifies identity per message on resume
        instead, which is what it has always done, and this refactor does not
        add a second gate on the create path. So on today's only live caller
        the grant is carried and stamped but not re-read; it becomes a live
        check the moment a ``channel_caller`` sender uses this method.

        Raises ``PermissionError`` when the sender kind is unsupported or a
        consulted grant does not re-verify, and ``NoActiveEnvironmentError``
        when the agent has no live environment.

        **Only ``mcp_caller`` is supported, and the check is up front.**
        ``_select_session_owner_id`` consumes ``identity_owner_id`` on the
        ``mcp_caller`` arm only; its ``channel_caller`` arm deliberately honors
        no owner override — "a channel session owned by anyone but the sender
        would let one external caller reach another user's installs". A channel
        sender reaching the body would therefore create and **commit** a
        session owned by the external caller and only then trip
        ``_stamp_new_session``'s unknown-key ``ValueError``: a wrong session
        that already exists, plus an unhandled error. Rejecting the kind before
        any write turns that tripwire into a guard. Widening the
        ``channel_caller`` owner arm is a security decision that belongs in
        **Phase 3** of ``docs/plans/channels_identity_unification/``, the change
        which turns identity candidates on for channels — not here. Lifting
        ``_IDENTITY_SESSION_SENDER_KINDS`` must happen in that *same* change,
        the one that teaches ``_select_session_owner_id`` how a
        ``channel_caller`` sender owns an identity session. Either half alone is
        the bug: the allowlist without the owner arm rejects a legitimate
        sender, the owner arm without the allowlist is unreachable, and lifting
        the allowlist first re-opens the committed-wrong-owner path this guard
        closes.
        """
        if sender.kind not in _IDENTITY_SESSION_SENDER_KINDS:
            raise PermissionError(
                f"identity session not supported for sender kind {sender.kind!r} "
                f"(supported: {sorted(_IDENTITY_SESSION_SENDER_KINDS)})"
            )

        caller_id = sender.platform_user_id
        if caller_id is None:
            raise PermissionError(
                "identity session requires a sender with a platform_user_id"
            )

        ChannelIngestionService.assert_access(
            db=db,
            agent=agent,
            sender=sender,
            policy=ChannelAccessPolicy(
                expected_owner_id=agent.owner_id,
                require_caller_in_route=True,
                identity_grant=grant,
            ),
        )
        session, _ = ChannelIngestionService.resolve_or_create_session(
            db=db,
            agent=agent,
            sender=sender,
            thread_key=None,
            integration_type=integration_type,
            extra_session_kwargs={
                "mode": mode,
                # Owner of the session is the identity owner, not the agent
                # owner (consumed by ``_select_session_owner_id``).
                "identity_owner_id": grant.owner_id,
                # Post-create stamping of identity-specific columns; all three
                # are already in ``_STAMPABLE_COLUMNS``.
                "identity_caller_id": caller_id,
                "identity_binding_id": grant.binding_id,
                "identity_binding_assignment_id": grant.assignment_id,
                "session_metadata_extra": session_metadata_extra or {},
            },
        )
        return session

    @staticmethod
    def assert_access(
        *,
        db: DBSession,
        agent: Agent,
        sender: SessionSender,
        policy: ChannelAccessPolicy,
    ) -> None:
        """Per-kind access check (plan §4.3). Raises on denial.

        ``db`` is required rather than opened here. The ``channel_caller`` arm
        re-reads the identity rows behind ``policy.identity_grant``, and an
        access check that quietly opened its own connection would be deciding
        on a different snapshot from the one the caller is about to create the
        session in — the honest signature is the one that says it reads.
        """
        kind = sender.kind

        if kind == "webui_user":
            if policy.require_owner_match:
                if policy.expected_owner_id is None:
                    raise PermissionError(
                        "webui_user requires expected_owner_id when require_owner_match=True"
                    )
                if not (
                    agent.owner_id
                    == policy.expected_owner_id
                    == sender.platform_user_id
                ):
                    raise PermissionError(
                        "webui_user owner mismatch: "
                        f"agent.owner_id={agent.owner_id}, "
                        f"expected_owner_id={policy.expected_owner_id}, "
                        f"sender.platform_user_id={sender.platform_user_id}"
                    )
            return

        if kind == "task_executor":
            # Real check, not a fast-path — replicates session_service.py:1250.
            if policy.expected_owner_id is None:
                raise PermissionError("task_executor requires policy.expected_owner_id")
            if policy.expected_owner_id != sender.platform_user_id:
                raise PermissionError(
                    "task_executor sender does not match expected owner: "
                    f"expected_owner_id={policy.expected_owner_id}, "
                    f"sender.platform_user_id={sender.platform_user_id}"
                )
            return

        if kind == "channel_caller":
            # Same trust shape as `task_executor`: the channel pipeline resolved
            # the sender to their own platform user, and the session must be
            # created/resumed under exactly that user. Never a fast-path — a
            # channel that mis-resolves the sender is denied here.
            if policy.expected_owner_id is None:
                raise PermissionError(
                    "channel_caller requires policy.expected_owner_id"
                )
            # Three-way structural invariant, deliberately stricter than
            # `task_executor`: the agent must be the SENDER'S OWN install.
            # Channel routing resolves agents two ways — the caller's installed
            # agents, or a fresh auto-install for the caller — so a legitimate
            # channel agent is always owned by the sender. Asserting it here
            # (rather than trusting the routing layer) means a router that can
            # return someone else's agent can never hand an external caller a
            # session inside another user's workspace *by accident*.
            if (
                agent.owner_id
                == policy.expected_owner_id
                == sender.platform_user_id
            ):
                return

            # The one deliberate exception: an identity grant. Reaching another
            # person's agent is the whole point of identity, so the invariant
            # above cannot be the only door — but the grant does not *weaken*
            # it, because every fact behind the grant is re-read here. The
            # routing decision that produced it and this call are separated by
            # a worker-thread hop and possibly an auto-install wait; the owner
            # may have revoked in between.
            #
            # ``policy.expected_owner_id`` is not compared separately on this
            # arm, and does not need to be: condition 6 below pins
            # ``binding.owner_id == agent.owner_id == grant.owner_id``, and the
            # arm above already established ``expected_owner_id`` is not None.
            # A seventh check tying the two together would be free — it is left
            # out only because the six are the agreed contract, and an
            # authorization check whose condition list drifts from its spec is
            # worse than one that is merely minimal.
            grant = policy.identity_grant
            if grant is None:
                raise PermissionError(
                    "channel_caller invariant violated: "
                    f"agent.owner_id={agent.owner_id}, "
                    f"expected_owner_id={policy.expected_owner_id}, "
                    f"sender.platform_user_id={sender.platform_user_id}"
                )

            from app.services.identity.identity_service import IdentityService

            denial = IdentityService.verify_identity_access(
                db,
                owner_id=grant.owner_id,
                binding_id=grant.binding_id,
                assignment_id=grant.assignment_id,
                caller_user_id=sender.platform_user_id,
                agent_id=agent.id,
            )
            if denial:
                raise PermissionError(
                    f"channel_caller identity grant rejected: {denial}"
                )
            return

        if kind == "a2a_caller":
            # Scope checks for resume run in `_verify_resume_sender`; new-session
            # token-to-agent allowance is enforced by the routing layer before
            # reaching the service (plan §4.3, §7.4).
            return

        if kind == "mcp_caller":
            # Routing has already gated this caller against the agent (§7.4).
            # An `identity_grant` on the policy is carried but not consulted
            # here. App MCP re-verifies identity per message on resume, in
            # `_check_identity_session_validity` — which checks *liveness*
            # (binding active, assignment active and enabled), not the grant
            # arm's four linkage conditions. Those linkage facts are not
            # re-derived on this path because App MCP never derived them here
            # either: the routing decision that produced the ids ran in this
            # same transaction moments ago. Keeping this arm as it was is what
            # makes the extraction a refactor; tightening it is a behaviour
            # change and needs its own reasoning.
            if policy.require_caller_in_route and sender.platform_user_id is None:
                raise PermissionError(
                    "mcp_caller missing platform_user_id "
                    "(routing layer should have rejected this)"
                )
            return

        if kind == "system_trigger":
            # Fastpath only after asserting the structural invariant — not a skip.
            if not policy.allow_system_trigger_fastpath:
                raise PermissionError(
                    "system_trigger requires policy.allow_system_trigger_fastpath=True"
                )
            if not (
                policy.expected_owner_id
                == agent.owner_id
                == sender.platform_user_id
            ):
                raise PermissionError(
                    "system_trigger invariant violated: "
                    f"policy.expected_owner_id={policy.expected_owner_id}, "
                    f"agent.owner_id={agent.owner_id}, "
                    f"sender.platform_user_id={sender.platform_user_id}"
                )
            return

        if kind == "platform_user":
            # Reader-fallback / test kind. Opportunistic owner check.
            if (
                policy.require_owner_match
                and policy.expected_owner_id is not None
                and policy.expected_owner_id != sender.platform_user_id
            ):
                raise PermissionError(
                    "platform_user owner mismatch: "
                    f"expected_owner_id={policy.expected_owner_id}, "
                    f"sender.platform_user_id={sender.platform_user_id}"
                )
            return

        # `anonymous` is reserved for future use; not supported in Phase 1.
        raise PermissionError(f"sender kind not supported in Phase 1: {kind!r}")

    # ------------------------------------------------------------------
    # Internal helpers.
    # ------------------------------------------------------------------

    @staticmethod
    def _select_session_owner_id(
        *,
        agent: Agent,
        sender: SessionSender,
        extra_session_kwargs: dict[str, Any],
    ) -> UUID:
        """Pick `Session.user_id` per the §4.2 sender-kind table."""
        kind = sender.kind

        if kind == "webui_user":
            # Routes may override (e.g. guest-share owner != session caller).
            override = extra_session_kwargs.pop("session_owner_id", None)
            return override or agent.owner_id

        if kind == "task_executor":
            # The executing human — matches today's input_task_service.py:803.
            if sender.platform_user_id is None:
                raise ValueError("task_executor sender must carry platform_user_id")
            return sender.platform_user_id

        if kind == "channel_caller":
            # The external sender's own account. No override is honored — a
            # channel session owned by anyone but the sender would let one
            # external caller reach another user's installs.
            if sender.platform_user_id is None:
                raise ValueError("channel_caller sender must carry platform_user_id")
            return sender.platform_user_id

        if kind == "a2a_caller":
            # External A2A target types (e.g. identity_mcp, external) own
            # the session under a non-owner user; honor an explicit override
            # when supplied. Core A2A passes nothing and falls back to the
            # agent owner (caller is stamped via access_token_id, not user_id).
            override = extra_session_kwargs.pop("session_owner_id", None)
            return override or agent.owner_id

        if kind == "system_trigger":
            # Cron-fired / handover paths run in the agent owner's space.
            return agent.owner_id

        if kind == "mcp_caller":
            # identity_mcp: session is owned by the identity owner, not the caller.
            identity_owner_id = extra_session_kwargs.pop("identity_owner_id", None)
            return identity_owner_id or agent.owner_id

        if kind == "platform_user":
            return sender.platform_user_id or agent.owner_id

        if kind == "anonymous":
            # Guest-share anonymous caller. The route is the only path that
            # builds this kind today; it always supplies
            # `session_owner_id=agent.owner_id` as the override so the session
            # is created in the owner's space.
            override = extra_session_kwargs.pop("session_owner_id", None)
            return override or agent.owner_id

        raise PermissionError(f"sender kind not supported in Phase 1: {kind!r}")

    @staticmethod
    def _verify_resume_sender(
        existing: Session, sender: SessionSender
    ) -> None:
        """Verify a resumed session matches the sender (plan §4.2 table).

        Raises `PermissionError` on mismatch.
        """
        kind = sender.kind

        # `channel_caller` resumes like `task_executor`: the bound session must
        # belong to the sender's own user. The channel's thread binding already
        # pins the session id, so this is the second gate on the same fact.
        if kind in ("webui_user", "task_executor", "platform_user", "channel_caller"):
            if existing.user_id != sender.platform_user_id:
                raise PermissionError(
                    f"session.user_id={existing.user_id} does not match "
                    f"sender.platform_user_id={sender.platform_user_id} "
                    f"(kind={kind})"
                )
            return

        if kind == "mcp_caller":
            # MCP-style resume is handled at the channel edge — App MCP's
            # `_try_resume_session` performs a strict (integration_type,
            # caller-column) match before any service call, and no
            # production path reaches the service with thread_key != None
            # for an mcp_caller sender. If a new MCP channel ever needs
            # service-level resume verification, reintroduce the
            # caller_id / identity_caller_id branch here.
            raise PermissionError(
                "mcp_caller resume is not handled by ChannelIngestionService; "
                "channels must perform their own resume verification "
                "(see app_mcp_request_handler._try_resume_session)"
            )

        if kind == "a2a_caller":
            # AccessTokenService.can_access_session runs at the caller's edge
            # (A2A handler's _parse_session_scope). Best-effort lineage check
            # here: external_id should match either the session's
            # access_token_id or the owner's user_id.
            if existing.access_token_id is not None:
                token_id_str = str(existing.access_token_id)
                if (
                    sender.external_id != token_id_str
                    and sender.external_id != str(existing.user_id)
                ):
                    raise PermissionError(
                        "a2a_caller resume mismatch: "
                        f"session.access_token_id={existing.access_token_id}, "
                        f"sender.external_id={sender.external_id}"
                    )
            return

        if kind == "system_trigger":
            # System triggers do not resume — every cron fire is a fresh
            # session. If a `thread_key` reaches the service for this kind
            # it is a caller bug.
            raise PermissionError(
                "system_trigger does not support thread_key (resume); "
                "every trigger fires a fresh session"
            )

        raise PermissionError(f"sender kind not supported in Phase 1: {kind!r}")

    # Columns that callers may stamp directly via `extra_session_kwargs`.
    # Mirrors what A2A's `_stamp_new_session` overrides and App MCP's
    # `_resolve_session` / `_create_identity_session` do today.
    _STAMPABLE_COLUMNS: tuple[str, ...] = (
        "caller_id",
        "identity_caller_id",
        "identity_binding_id",
        "identity_binding_assignment_id",
    )

    @staticmethod
    def _stamp_new_session(
        *,
        db: DBSession,
        session: Session,
        post_create_stamps: dict[str, Any],
    ) -> None:
        """Consolidated post-create stamping (plan §4.2)."""
        if not post_create_stamps:
            return

        changed = False
        for column in ChannelIngestionService._STAMPABLE_COLUMNS:
            value = post_create_stamps.pop(column, None)
            if value is not None:
                setattr(session, column, value)
                changed = True

        metadata_extra = post_create_stamps.pop("session_metadata_extra", None)
        if metadata_extra:
            session.session_metadata = {
                **(session.session_metadata or {}),
                **metadata_extra,
            }
            changed = True

        if post_create_stamps:
            raise ValueError(
                f"Unknown post-create stamping keys: {sorted(post_create_stamps)}"
            )

        if changed:
            db.add(session)
            db.commit()
            db.refresh(session)


__all__ = ["ChannelIngestionService", "NoActiveEnvironmentError"]
