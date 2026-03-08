"""
Webapp Chat Service - Business logic for webapp chat widget sessions.
"""
import uuid
import logging

from sqlmodel import Session as DBSession, select

from app.models import (
    Session,
    SessionCreate,
)
from app.services.agent_webapp_interface_config_service import (
    AgentWebappInterfaceConfigService,
)
from app.services.session_service import SessionService

logger = logging.getLogger(__name__)


class WebappChatError(Exception):
    """Base exception for webapp chat service errors."""

    def __init__(self, message: str, status_code: int = 400):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class WebappChatDisabledError(WebappChatError):
    def __init__(self):
        super().__init__("Chat is not enabled for this webapp", status_code=403)


class WebappChatSessionNotFoundError(WebappChatError):
    def __init__(self):
        super().__init__("Chat session not found", status_code=404)


class WebappChatAccessDeniedError(WebappChatError):
    def __init__(self):
        super().__init__("Access denied to this chat session", status_code=403)


class WebappChatService:

    @staticmethod
    def validate_chat_enabled(
        db_session: DBSession, agent_id: uuid.UUID
    ) -> str:
        """
        Check that chat is enabled for the agent.

        Returns the chat_mode string if enabled.
        Raises WebappChatDisabledError if chat_mode is None.
        """
        config = AgentWebappInterfaceConfigService.get_by_agent_id(
            db_session, agent_id
        )
        if not config.chat_mode:
            raise WebappChatDisabledError()
        return config.chat_mode

    @staticmethod
    def get_or_create_session(
        db_session: DBSession,
        webapp_share_id: uuid.UUID,
        agent_id: uuid.UUID,
        owner_id: uuid.UUID,
        chat_mode: str,
    ) -> Session:
        """
        Get existing active session for this webapp share, or create one.

        Sessions are scoped by webapp_share_id — one active session per share.
        """
        existing = WebappChatService.get_active_session(db_session, webapp_share_id)
        if existing:
            return existing

        # Create new session
        data = SessionCreate(
            agent_id=agent_id,
            mode=chat_mode,
        )
        new_session = SessionService.create_session(
            db_session=db_session,
            user_id=owner_id,
            data=data,
            webapp_share_id=webapp_share_id,
        )
        if not new_session:
            raise WebappChatError(
                "Failed to create chat session. Agent may not have an active environment.",
                status_code=400,
            )
        return new_session

    @staticmethod
    def get_active_session(
        db_session: DBSession, webapp_share_id: uuid.UUID
    ) -> Session | None:
        """Get the most recent active session for this webapp share."""
        return db_session.exec(
            select(Session).where(
                Session.webapp_share_id == webapp_share_id,
                Session.status == "active",
            ).order_by(Session.created_at.desc())
        ).first()

    @staticmethod
    def verify_session_access(
        db_session: DBSession,
        session_id: uuid.UUID,
        webapp_share_id: uuid.UUID,
    ) -> Session:
        """
        Verify the session exists and belongs to the given webapp share.

        Raises appropriate errors if not found or access denied.
        """
        chat_session = db_session.get(Session, session_id)
        if not chat_session:
            raise WebappChatSessionNotFoundError()
        if chat_session.webapp_share_id != webapp_share_id:
            raise WebappChatAccessDeniedError()
        return chat_session
