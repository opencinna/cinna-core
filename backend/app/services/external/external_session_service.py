"""
External Agent Access API — Session Metadata Service (Phase 5).

Read-only metadata surface over existing session data. Allows native clients
(Cinna Desktop, Cinna Mobile, etc.) to restore their thread list at launch
without opening a full A2A connection per thread.

Three methods are exposed:
  list_sessions_for_external  — union of owner / caller / identity_caller sessions
  get_session_for_external    — single session lookup with visibility check
  list_messages_for_external  — message history after visibility check
"""
from __future__ import annotations

import uuid

from sqlalchemy import or_
from sqlmodel import Session, select

from app.models.agents.agent import Agent
from app.models.sessions.session import Session as ChatSession
from app.models.external.external_agents import ExternalSessionPublic
from app.models.users.user import User
from app.services.external.errors import SessionNotVisibleError
from app.services.sessions.message_service import MessageService
from app.services.sessions.session_service import SessionService

_MAX_LIMIT = 200


class ExternalSessionService:
    """Read-only session metadata for external (native client) consumers."""

    @staticmethod
    def list_sessions_for_external(
        db: Session,
        user: User,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ExternalSessionPublic]:
        """Return sessions where the user is owner, caller, or identity_caller.

        Sessions are ordered by last_message_at DESC (most recent first).
        The limit is capped at _MAX_LIMIT (200) inside the service.
        """
        limit = min(limit, _MAX_LIMIT)
        stmt = (
            select(ChatSession)
            .where(
                or_(
                    ChatSession.user_id == user.id,
                    ChatSession.caller_id == user.id,
                    ChatSession.identity_caller_id == user.id,
                )
            )
            .order_by(ChatSession.last_message_at.desc())
            .limit(limit)
            .offset(offset)
        )
        sessions = db.exec(stmt).all()
        # Filter out sessions the caller has soft-hidden.
        sessions = [
            s for s in sessions
            if not (s.session_metadata or {}).get("hidden_for_callers")
        ]
        return [ExternalSessionService._to_public(db, s) for s in sessions]

    @staticmethod
    def get_session_for_external(
        db: Session,
        user: User,
        session_id: uuid.UUID,
    ) -> ExternalSessionPublic:
        """Return a single session's metadata.

        Raises SessionNotVisibleError (HTTP 404) if the session does not exist or
        the user is not the owner, caller, or identity_caller.
        """
        session = ExternalSessionService._get_visible_session(db, user, session_id)
        return ExternalSessionService._to_public(db, session)

    @staticmethod
    def hide_session_for_external(
        db: Session,
        user: User,
        session_id: uuid.UUID,
    ) -> None:
        """Soft-hide a session for the caller.

        Sets ``session_metadata["hidden_for_callers"] = True`` on the session.
        The session is not deleted — it remains in the database and is visible
        to the agent owner for audit purposes.

        Raises SessionNotVisibleError (HTTP 404) if the session is not visible
        to the user.
        """
        session = ExternalSessionService._get_visible_session(db, user, session_id)
        SessionService.set_session_metadata_flag(db, session.id, "hidden_for_callers", True)

    @staticmethod
    def list_messages_for_external(
        db: Session,
        user: User,
        session_id: uuid.UUID,
    ) -> list:
        """Return message history for a visible session.

        Raises SessionNotVisibleError (HTTP 404) if the session is not visible.
        """
        session = ExternalSessionService._get_visible_session(db, user, session_id)
        return MessageService.get_session_messages(session=db, session_id=session.id)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _get_visible_session(
        db: Session,
        user: User,
        session_id: uuid.UUID,
    ) -> ChatSession:
        """Fetch a session and verify the user is owner / caller / identity_caller.

        Raises SessionNotVisibleError when the session is not found or not visible.
        """
        stmt = (
            select(ChatSession)
            .where(
                ChatSession.id == session_id,
                or_(
                    ChatSession.user_id == user.id,
                    ChatSession.caller_id == user.id,
                    ChatSession.identity_caller_id == user.id,
                ),
            )
        )
        session = db.exec(stmt).first()
        if session is None:
            raise SessionNotVisibleError("Session not found")
        return session

    @staticmethod
    def _resolve_agent_name(db: Session, session: ChatSession) -> str | None:
        """Resolve the agent display name for a session."""
        if session.integration_type == "identity_mcp":
            metadata = session.session_metadata or {}
            owner_name = metadata.get("identity_owner_name")
            if owner_name:
                return str(owner_name)

        if session.agent_id is None:
            return None

        agent = db.get(Agent, session.agent_id)
        return agent.name if agent else None

    @staticmethod
    def _derive_target(
        session: ChatSession,
    ) -> tuple[str | None, uuid.UUID | None]:
        """Derive (target_type, target_id) from the session's integration_type."""
        itype = session.integration_type

        if itype in ("external", "app_mcp"):
            # App MCP routes over the caller's own agents, so its addressable
            # target is the agent itself — the same card the owner-scoped
            # "external" arm points at. There is no separate route target any
            # more: the ``AppAgentRoute`` family (and the ``app_mcp_route_id``
            # metadata key this used to read) went with the route target kind.
            return "agent", session.agent_id

        if itype == "identity_mcp":
            return "identity", session.user_id

        return None, None

    @staticmethod
    def _to_public(db: Session, session: ChatSession) -> ExternalSessionPublic:
        """Convert a Session ORM row to ExternalSessionPublic."""
        metadata = session.session_metadata or {}
        target_type, target_id = ExternalSessionService._derive_target(session)

        return ExternalSessionPublic(
            id=session.id,
            title=session.title,
            integration_type=session.integration_type,
            status=session.status,
            interaction_status=session.interaction_status,
            result_state=session.result_state,
            result_summary=session.result_summary,
            last_message_at=session.last_message_at,
            created_at=session.created_at,
            agent_id=session.agent_id,
            agent_name=ExternalSessionService._resolve_agent_name(db, session),
            caller_id=session.caller_id,
            identity_caller_id=session.identity_caller_id,
            client_kind=metadata.get("client_kind"),
            external_client_id=metadata.get("external_client_id"),
            target_type=target_type,
            target_id=target_id,
        )
