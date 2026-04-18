"""
ExternalA2AContextHandler — A2ARequestHandler subclass for the external surface.

Inherits the shared JSON-RPC dispatch and SSE plumbing from A2ARequestHandler
and overrides hook methods to enforce caller-scope, stamp session metadata,
and raise domain exceptions defined in ``app.services.external.errors``.

This module also defines ``TargetContext``, the pre-resolved target descriptor
constructed by ``ExternalA2ARequestHandler`` and passed into the handler via
its constructor.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Optional
from uuid import UUID

from sqlmodel import Session as DbSession

from app.models import Session as ChatSession
from app.services.a2a.a2a_request_handler import A2ARequestHandler
from app.services.external.errors import (
    IdentityBindingRevokedError,
    NoActiveEnvironmentError,
    TaskScopeViolationError,
)
from app.services.sessions.session_service import SessionService

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# TargetContext — pre-resolved target descriptor
# ---------------------------------------------------------------------------


@dataclass
class TargetContext:
    """Pre-resolved target context for external A2A dispatch.

    Used by ExternalA2ARequestHandler to pass ownership and integration
    hints into ExternalA2AContextHandler without re-deriving them from
    agent_id alone.
    """

    agent: Any  # Agent
    environment: Any  # AgentEnvironment
    integration_type: str  # "external", "app_mcp", "identity_mcp"
    session_owner_id: UUID  # user_id for the session
    caller_id: Optional[UUID] = None  # for app_mcp: the calling user's ID
    identity_caller_id: Optional[UUID] = None  # for identity_mcp
    match_method: Optional[str] = None  # for app_mcp: "external_direct"
    route_id: Optional[UUID] = None  # for app_mcp: the AppAgentRoute.id
    route_source: Optional[str] = None  # for app_mcp: "admin" or "user"
    # Identity-specific fields (only set when integration_type == "identity_mcp")
    identity_binding_id: Optional[UUID] = None
    identity_binding_assignment_id: Optional[UUID] = None
    identity_stage2_match_method: Optional[str] = None  # "only_one" | "pattern" | "ai"
    identity_owner_name: Optional[str] = None
    identity_caller_name: Optional[str] = None
    # Client attribution — populated from JWT claims when the request originates
    # from a desktop/mobile native client.  Written into session_metadata by
    # _stamp_new_session for all integration types.
    client_kind: Optional[str] = None        # e.g. "desktop", "mobile"
    external_client_id: Optional[str] = None  # DesktopOAuthClient.id (str UUID)


# ---------------------------------------------------------------------------
# ExternalA2AContextHandler
# ---------------------------------------------------------------------------


class ExternalA2AContextHandler(A2ARequestHandler):
    """A2ARequestHandler subclass for the External Agent Access surface.

    Overrides the base hooks to enforce caller-scope per integration_type
    and stamp session metadata (caller_id, identity bindings, client
    attribution) into newly created sessions.
    """

    log_prefix = "[ExternalA2A]"

    def __init__(
        self,
        context: TargetContext,
        get_db_session: Callable[[], DbSession],
        backend_base_url: str = "",
    ) -> None:
        super().__init__(
            agent=context.agent,
            environment=context.environment,
            user_id=context.session_owner_id,
            get_db_session=get_db_session,
            a2a_token_payload=None,
            access_token_id=None,
            backend_base_url=backend_base_url,
        )
        self.context = context

    # ------------------------------------------------------------------
    # Hook overrides
    # ------------------------------------------------------------------

    def _parse_session_scope(self, task_id: str | None) -> UUID | None:
        """Parse task_id and enforce caller-scope per context.integration_type.

        - "external":     session.user_id must equal context.session_owner_id
        - "app_mcp":      session.caller_id must equal context.caller_id
        - "identity_mcp": session.identity_caller_id must equal
                          context.identity_caller_id; binding validity re-checked

        Returns None for falsy or non-UUID task ids (new session will be
        created).

        Raises:
            TaskScopeViolationError: Session exists but belongs to a different caller.
            IdentityBindingRevokedError: Identity session's binding/assignment
                was disabled mid-conversation.
        """
        if not task_id:
            return None
        try:
            session_id = UUID(task_id)
        except (ValueError, TypeError):
            return None

        with self.get_db_session() as db:
            session = SessionService.get_session(db, session_id)
            if session is None:
                return None

            if self.context.integration_type == "app_mcp":
                if session.caller_id != self.context.caller_id:
                    raise TaskScopeViolationError()
            elif self.context.integration_type == "identity_mcp":
                if session.identity_caller_id != self.context.identity_caller_id:
                    raise TaskScopeViolationError()
                if session.user_id != self.context.session_owner_id:
                    raise TaskScopeViolationError()
                # Re-check binding validity so mid-conversation revocations surface.
                from app.services.identity.identity_service import IdentityService
                validity_error = IdentityService.check_session_validity(db, session)
                if validity_error:
                    raise IdentityBindingRevokedError(validity_error)
            else:  # "external" and any other owner-scoped type
                if session.user_id != self.context.session_owner_id:
                    raise TaskScopeViolationError()
        return session_id

    def _authorize_existing_session(self, session: ChatSession) -> None:
        """Caller-scope check for tasks/get, tasks/cancel."""
        if not self._session_matches_context(session):
            raise TaskScopeViolationError()

    def _stamp_new_session(self, session_id: UUID) -> None:
        """Write caller_id / metadata for a newly created context-scoped session.

        Also stamps client attribution claims (client_kind / external_client_id)
        into session_metadata for all integration types when present.
        """
        context = self.context
        with self.get_db_session() as db:
            session = SessionService.get_session(db, session_id)
            if session is None:
                return

            if context.integration_type == "external":
                # Owner-only sessions: no caller_id / identity stamping needed.
                # Only write client attribution if present.
                if context.client_kind is not None:
                    meta: dict[str, Any] = dict(session.session_metadata or {})
                    meta["client_kind"] = context.client_kind
                    if context.external_client_id is not None:
                        meta["external_client_id"] = context.external_client_id
                    session.session_metadata = meta
                    db.add(session)
                    db.commit()
                return

            if context.integration_type == "app_mcp":
                session.caller_id = context.caller_id
                meta = dict(session.session_metadata or {})
                if context.route_id is not None:
                    meta["app_mcp_route_id"] = str(context.route_id)
                if context.route_source is not None:
                    meta["app_mcp_route_type"] = context.route_source
                if context.match_method is not None:
                    meta["app_mcp_match_method"] = context.match_method
                meta.setdefault("app_mcp_agent_name", context.agent.name)
                if context.client_kind is not None:
                    meta["client_kind"] = context.client_kind
                    if context.external_client_id is not None:
                        meta["external_client_id"] = context.external_client_id
                session.session_metadata = meta
            elif context.integration_type == "identity_mcp":
                session.identity_caller_id = context.identity_caller_id
                if context.identity_binding_id is not None:
                    session.identity_binding_id = context.identity_binding_id
                if context.identity_binding_assignment_id is not None:
                    session.identity_binding_assignment_id = (
                        context.identity_binding_assignment_id
                    )
                meta = dict(session.session_metadata or {})
                if context.identity_owner_name is not None:
                    meta["identity_owner_name"] = context.identity_owner_name
                if context.identity_caller_name is not None:
                    meta["identity_caller_name"] = context.identity_caller_name
                if context.identity_stage2_match_method is not None:
                    meta["identity_match_method"] = (
                        context.identity_stage2_match_method
                    )
                if context.match_method is not None:
                    meta["app_mcp_match_method"] = context.match_method
                meta.setdefault("app_mcp_route_type", "identity")
                if context.client_kind is not None:
                    meta["client_kind"] = context.client_kind
                    if context.external_client_id is not None:
                        meta["external_client_id"] = context.external_client_id
                session.session_metadata = meta

            db.add(session)
            db.commit()

    def _integration_type_for_new_session(self) -> str | None:
        return self.context.integration_type

    def _session_access_token_id(self) -> Optional[UUID]:
        # External surface does not use A2A access tokens.
        return None

    def _task_list_access_token_filter(self) -> Optional[UUID]:
        # External surface does not use A2A access tokens; no DB-level filter.
        return None

    def _task_list_filter(self, session: ChatSession) -> bool:
        return self._session_matches_context(session)

    def _wrap_env_error(self, exc: Exception) -> Exception:
        return NoActiveEnvironmentError(f"Environment error: {str(exc)}")

    def _stream_scope_error(self, exc: Exception, request_id: Any) -> Optional[str]:
        """Render caller-scope violations as inline SSE errors."""
        if isinstance(exc, (TaskScopeViolationError, IdentityBindingRevokedError)):
            return self._format_sse_error(request_id, exc.jsonrpc_code, exc.message)
        return None

    # ------------------------------------------------------------------
    # Session-matching helper used by _authorize_existing_session + _task_list_filter
    # ------------------------------------------------------------------

    def _session_matches_context(self, session: ChatSession) -> bool:
        """Caller-scope check: compare session fields to ``self.context``.

        - app_mcp:      session.caller_id == context.caller_id
        - identity_mcp: session.identity_caller_id == context.identity_caller_id
        - external:     session.user_id == context.session_owner_id
        """
        context = self.context
        if context.integration_type == "app_mcp":
            return session.caller_id == context.caller_id
        if context.integration_type == "identity_mcp":
            return (
                session.identity_caller_id == context.identity_caller_id
                and session.user_id == context.session_owner_id
            )
        return session.user_id == context.session_owner_id

    # ------------------------------------------------------------------
    # tasks/cancel override — external uses domain exceptions, not ValueError
    # ------------------------------------------------------------------

    async def handle_tasks_cancel(self, params: dict[str, Any]) -> dict:
        """External-surface override: translate ``ValueError`` to domain exceptions.

        The shared body validates task_id, session existence, and
        environment membership, raising ``ValueError`` for each. The
        external dispatcher needs domain exceptions (``TargetNotAccessibleError``
        etc.) so translation happens here.
        """
        from app.services.external.errors import (
            InvalidExternalParamsError,
            TargetNotAccessibleError,
        )

        task_id = params.get("id")
        if not task_id:
            raise InvalidExternalParamsError("Task ID is required")
        try:
            UUID(task_id)
        except (ValueError, TypeError):
            raise InvalidExternalParamsError("Task ID must be a UUID")

        try:
            return await super().handle_tasks_cancel(params)
        except TaskScopeViolationError:
            raise
        except ValueError as e:
            message = str(e)
            if message == "Task not found":
                raise TargetNotAccessibleError("Task not found")
            if message == "Task does not belong to this agent":
                raise TargetNotAccessibleError("Task does not belong to this agent")
            raise
