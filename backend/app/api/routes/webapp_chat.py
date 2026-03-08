"""
Webapp Chat API Routes.

Public chat endpoints for webapp viewers. Uses webapp-viewer JWT for auth.
All endpoints validate that chat is enabled (chat_mode is not null).
"""
import uuid
from typing import Any
import logging

from fastapi import APIRouter, HTTPException

from app.api.deps import CurrentWebappChatUser, SessionDep
from app.models import (
    Session,
    SessionPublic,
    AgentEnvironment,
    MessageCreate,
    MessagesPublic,
)
from app.services.webapp_chat_service import (
    WebappChatService,
    WebappChatError,
)
from app.services.message_service import MessageService
from app.services.webapp_service import WebappService
from app.services.session_service import SessionService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webapp/{token}/chat", tags=["webapp-chat"])

# Maximum characters allowed in the page_context field before truncation.
# Prevents excessively large context payloads from bloating stored metadata.
_PAGE_CONTEXT_MAX_CHARS = 10_000


def _handle_chat_error(e: WebappChatError) -> None:
    """Convert service exceptions to HTTP exceptions."""
    raise HTTPException(status_code=e.status_code, detail=e.message)


def _update_env_activity(session: Any, chat_session: Session) -> None:
    """Update environment last_activity_at for keep-alive."""
    environment = session.get(AgentEnvironment, chat_session.environment_id)
    if environment:
        WebappService.update_last_activity(session, environment)


@router.post("/sessions", response_model=SessionPublic)
def create_or_get_chat_session(
    token: str,
    session: SessionDep,
    caller: CurrentWebappChatUser,
) -> Any:
    """
    Create a chat session for this webapp share, or return existing one.

    Idempotent — returns the active session if one already exists.
    Session mode is determined by the agent's chat_mode configuration.
    """
    try:
        chat_mode = WebappChatService.validate_chat_enabled(
            session, caller.agent_id
        )
        chat_session = WebappChatService.get_or_create_session(
            db_session=session,
            webapp_share_id=caller.webapp_share_id,
            agent_id=caller.agent_id,
            owner_id=caller.owner_id,
            chat_mode=chat_mode,
        )
        _update_env_activity(session, chat_session)
        return chat_session
    except WebappChatError as e:
        _handle_chat_error(e)


@router.get("/sessions", response_model=SessionPublic | None)
def get_active_chat_session(
    token: str,
    session: SessionDep,
    caller: CurrentWebappChatUser,
) -> Any:
    """
    Get the active chat session for this webapp share.

    Returns null if no active session exists.
    """
    try:
        WebappChatService.validate_chat_enabled(session, caller.agent_id)
    except WebappChatError as e:
        _handle_chat_error(e)

    chat_session = WebappChatService.get_active_session(
        session, caller.webapp_share_id
    )
    if chat_session:
        _update_env_activity(session, chat_session)
    return chat_session


@router.get("/sessions/{session_id}", response_model=SessionPublic)
def get_chat_session(
    token: str,
    session_id: uuid.UUID,
    session: SessionDep,
    caller: CurrentWebappChatUser,
) -> Any:
    """Get chat session details. Verifies webapp_share_id match."""
    try:
        chat_session = WebappChatService.verify_session_access(
            session, session_id, caller.webapp_share_id
        )
        _update_env_activity(session, chat_session)
        return chat_session
    except WebappChatError as e:
        _handle_chat_error(e)


@router.get("/sessions/{session_id}/messages", response_model=MessagesPublic)
async def get_chat_messages(
    token: str,
    session_id: uuid.UUID,
    session: SessionDep,
    caller: CurrentWebappChatUser,
    limit: int = 100,
    offset: int = 0,
) -> Any:
    """Get message history for a chat session."""
    try:
        WebappChatService.verify_session_access(
            session, session_id, caller.webapp_share_id
        )
    except WebappChatError as e:
        _handle_chat_error(e)

    messages = MessageService.get_session_messages(
        session=session, session_id=session_id, limit=limit, offset=offset
    )

    messages = await MessageService.enrich_messages_with_streaming(
        messages, session_id
    )

    return MessagesPublic(data=messages, count=len(messages))


@router.post("/sessions/{session_id}/messages/stream")
async def send_chat_message_stream(
    token: str,
    session_id: uuid.UUID,
    session: SessionDep,
    caller: CurrentWebappChatUser,
    message_in: MessageCreate,
) -> Any:
    """
    Send a chat message and stream response via WebSocket.

    Streaming events are emitted via WebSocket to room: session_{session_id}_stream.
    """
    try:
        chat_session = WebappChatService.verify_session_access(
            session, session_id, caller.webapp_share_id
        )
    except WebappChatError as e:
        _handle_chat_error(e)

    _update_env_activity(session, chat_session)

    # Truncate page_context to the allowed limit before passing it along.
    # The context is stored in message_metadata (not in message content) so it
    # remains invisible in the chat UI but is still available when building the
    # agent prompt in collect_pending_messages.
    safe_page_context: str | None = None
    if message_in.page_context:
        safe_page_context = message_in.page_context[:_PAGE_CONTEXT_MAX_CHARS]

    # Send message using centralized service (user_id = owner_id)
    result = await SessionService.send_session_message(
        session_id=session_id,
        user_id=caller.owner_id,
        content=message_in.content,
        file_ids=message_in.file_ids,
        answers_to_message_id=message_in.answers_to_message_id,
        page_context=safe_page_context,
    )

    if result["action"] == "error":
        raise HTTPException(status_code=500, detail=result["message"])

    return MessageService.build_stream_response(session_id, result)


@router.post("/sessions/{session_id}/messages/interrupt")
async def interrupt_chat_message(
    token: str,
    session_id: uuid.UUID,
    session: SessionDep,
    caller: CurrentWebappChatUser,
) -> Any:
    """Interrupt an active streaming message in a chat session."""
    try:
        chat_session = WebappChatService.verify_session_access(
            session, session_id, caller.webapp_share_id
        )
    except WebappChatError as e:
        _handle_chat_error(e)

    try:
        return await MessageService.interrupt_stream(
            db_session=session,
            session_id=session_id,
            environment_id=chat_session.environment_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
