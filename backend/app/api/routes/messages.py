import uuid
from typing import Any
import json
import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from sqlmodel import Session as DBSession

from app.api.deps import CurrentUser, SessionDep
from app.core.db import engine
from app.models import (
    Session,
    AgentEnvironment,
    Agent,
    MessageCreate,
    MessagePublic,
    MessagesPublic,
    SessionUpdate,
)
from app.services.message_service import MessageService
from app.services.session_service import SessionService

logger = logging.getLogger(__name__)

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


@router.post("/{session_id}/messages/stream")
async def send_message_stream(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    session_id: uuid.UUID,
    message_in: MessageCreate,
) -> Any:
    """
    Send message to agent environment and stream response.

    Flow:
    1. Verify session ownership
    2. Get external SDK session ID (if exists)
    3. Stream message to environment container
    4. Capture session_created event and store external_session_id
    5. Save user message and agent response to database
    6. Stream events to frontend
    """
    # Verify session exists and user owns it
    chat_session = session.get(Session, session_id)
    if not chat_session:
        raise HTTPException(status_code=404, detail="Session not found")

    if not current_user.is_superuser and (chat_session.user_id != current_user.id):
        raise HTTPException(status_code=400, detail="Not enough permissions")

    # Get environment
    environment = session.get(AgentEnvironment, chat_session.environment_id)
    if not environment:
        raise HTTPException(status_code=404, detail="Environment not found")

    # Check environment is running
    if environment.status != "running":
        raise HTTPException(
            status_code=400,
            detail=f"Environment is not running (status: {environment.status})"
        )

    # Get external SDK session ID (if exists)
    external_session_id = SessionService.get_external_session_id(chat_session)

    logger.info(
        f"Streaming message to session {session_id} "
        f"(mode={chat_session.mode}, external_session_id={external_session_id})"
    )

    # Store user message
    user_message_obj = MessageService.create_message(
        session=session,
        session_id=session_id,
        role="user",
        content=message_in.content,
    )

    # Auto-set session title from first message if no title exists
    if not chat_session.title or chat_session.title.strip() == "":
        # Truncate message content to reasonable length for title
        title = message_in.content[:100]
        if len(message_in.content) > 100:
            title += "..."

        SessionService.update_session(
            db_session=session,
            session_id=session_id,
            data=SessionUpdate(title=title)
        )
        logger.info(f"Auto-set session title from first message: {title}")

    # Set session status to "active" before streaming starts
    SessionService.update_session_status(
        db_session=session,
        session_id=session_id,
        status="active"
    )

    # Extract data from ORM objects BEFORE async generator (to avoid detached instance errors)
    session_mode = chat_session.mode
    user_message_content = message_in.content
    environment_id = environment.id
    base_url = MessageService.get_environment_url(environment)
    auth_headers = MessageService.get_auth_headers(environment)

    async def event_stream():
        """Generate SSE events for frontend"""
        # Stream events from MessageService (all business logic is in the service)
        async for event in MessageService.stream_message_with_events(
            session_id=session_id,
            environment_id=environment_id,
            base_url=base_url,
            auth_headers=auth_headers,
            user_message_content=user_message_content,
            session_mode=session_mode,
            external_session_id=external_session_id,
            get_fresh_db_session=lambda: DBSession(engine)
        ):
            # Format event as SSE and forward to frontend
            event_json = json.dumps(event)
            yield f"data: {event_json}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        }
    )
