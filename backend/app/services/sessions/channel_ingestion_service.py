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
    IngestionResult,
    Session,
    SessionCreate,
    SessionSender,
)
from app.services.sessions.session_service import SessionService

logger = logging.getLogger(__name__)

_LOG_PREFIX = "[ChannelIngestion]"


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
            agent=agent, sender=sender, policy=access_policy
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
    def assert_access(
        *,
        agent: Agent,
        sender: SessionSender,
        policy: ChannelAccessPolicy,
    ) -> None:
        """Per-kind access check (plan §4.3). Raises on denial."""
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

        if kind == "a2a_caller":
            # Scope checks for resume run in `_verify_resume_sender`; new-session
            # token-to-agent allowance is enforced by the routing layer before
            # reaching the service (plan §4.3, §7.4).
            return

        if kind == "mcp_caller":
            # Routing has already gated this caller against the agent (§7.4).
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

        if kind in ("webui_user", "task_executor", "platform_user"):
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
