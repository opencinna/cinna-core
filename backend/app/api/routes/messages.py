import uuid
from typing import Any

from fastapi import APIRouter, HTTPException

from app.api.deps import CurrentUser, SessionDep
from app.models import (
    Session,
    MessageCreate,
    MessagePublic,
    MessagesPublic,
)
from app.services.message_service import MessageService

router = APIRouter(prefix="/sessions", tags=["messages"])


@router.get("/{session_id}/messages", response_model=MessagesPublic)
def get_messages(
    session: SessionDep,
    current_user: CurrentUser,
    session_id: uuid.UUID,
    limit: int = 100,
    offset: int = 0,
) -> Any:
    """
    Get session messages.
    """
    # Verify session exists and user owns it
    chat_session = session.get(Session, session_id)
    if not chat_session:
        raise HTTPException(status_code=404, detail="Session not found")

    if not current_user.is_superuser and (chat_session.user_id != current_user.id):
        raise HTTPException(status_code=400, detail="Not enough permissions")

    messages = MessageService.get_session_messages(
        session=session, session_id=session_id, limit=limit, offset=offset
    )
    return MessagesPublic(data=messages, count=len(messages))


@router.post("/{session_id}/messages", response_model=MessagePublic)
def send_message(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    session_id: uuid.UUID,
    message_in: MessageCreate,
) -> Any:
    """
    Send message to agent (stub - no actual agent communication yet).

    For Step 1: Just store user message, return mock agent response.
    """
    # Verify session exists and user owns it
    chat_session = session.get(Session, session_id)
    if not chat_session:
        raise HTTPException(status_code=404, detail="Session not found")

    if not current_user.is_superuser and (chat_session.user_id != current_user.id):
        raise HTTPException(status_code=400, detail="Not enough permissions")

    # Store user message
    user_message = MessageService.create_message(
        session=session,
        session_id=session_id,
        role="user",
        content=message_in.content,
    )

    # TODO: In future steps, actually send to agent and get real response
    # For now, create a mock agent response
    agent_response = MessageService.create_message(
        session=session,
        session_id=session_id,
        role="agent",
        content=f"[Mock response] I received your message: '{message_in.content[:50]}...' (Step 1 stub)",
        message_metadata={"mock": True, "step": 1},
    )

    return agent_response
