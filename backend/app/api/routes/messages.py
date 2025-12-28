import uuid
from typing import Any
import json
import logging
import asyncio

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
from app.services.ai_functions_service import AIFunctionsService
from app.services.active_streaming_manager import active_streaming_manager

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
        answers_to_message_id=message_in.answers_to_message_id,
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
        f"(mode={chat_session.mode}, agent_sdk={chat_session.agent_sdk}, external_session_id={external_session_id})"
    )

    # Store user message
    user_message_obj = MessageService.create_message(
        session=session,
        session_id=session_id,
        role="user",
        content=message_in.content,
        answers_to_message_id=message_in.answers_to_message_id,
    )

    # Auto-generate session title from first message if no title exists
    if not chat_session.title or chat_session.title.strip() == "":
        # Generate title asynchronously (non-blocking) using LLM
        if AIFunctionsService.is_available():
            async def generate_title_async():
                """Background task to generate and update session title"""
                try:
                    # Generate title using LLM
                    title = await asyncio.to_thread(
                        AIFunctionsService.generate_session_title,
                        message_in.content
                    )

                    # Update session with generated title
                    with DBSession(engine) as db:
                        SessionService.update_session(
                            db_session=db,
                            session_id=session_id,
                            data=SessionUpdate(title=title)
                        )
                    logger.info(f"Generated session title asynchronously: {title}")
                except Exception as e:
                    logger.warning(f"Failed to generate session title asynchronously: {e}")
                    # Fallback to truncated message if LLM fails
                    fallback_title = message_in.content[:100]
                    if len(message_in.content) > 100:
                        fallback_title += "..."
                    with DBSession(engine) as db:
                        SessionService.update_session(
                            db_session=db,
                            session_id=session_id,
                            data=SessionUpdate(title=fallback_title)
                        )
                    logger.info(f"Set fallback session title: {fallback_title}")

            # Start background task (fire and forget)
            asyncio.create_task(generate_title_async())
        else:
            # If no LLM available, set truncated message immediately
            fallback_title = message_in.content[:100]
            if len(message_in.content) > 100:
                fallback_title += "..."
            SessionService.update_session(
                db_session=session,
                session_id=session_id,
                data=SessionUpdate(title=fallback_title)
            )
            logger.info(f"Set fallback session title (no LLM): {fallback_title}")

    # Set session status to "active" before streaming starts
    SessionService.update_session_status(
        db_session=session,
        session_id=session_id,
        status="active"
    )

    # Extract data from ORM objects BEFORE async generator (to avoid detached instance errors)
    session_mode = chat_session.mode
    agent_sdk = chat_session.agent_sdk
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
            agent_sdk=agent_sdk,
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


@router.post("/{session_id}/messages/interrupt")
async def interrupt_message(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    session_id: uuid.UUID,
) -> Any:
    """
    Interrupt an active streaming message.

    Flow:
    1. Verify session ownership
    2. Request interrupt via active_streaming_manager
    3. If external_session_id available, forward to agent env immediately
    4. If not available yet, queue interrupt (will be sent when session_id arrives)
    5. Return status
    """
    # Verify session exists and user owns it
    chat_session = session.get(Session, session_id)
    if not chat_session:
        raise HTTPException(status_code=404, detail="Session not found")

    if not current_user.is_superuser and (chat_session.user_id != current_user.id):
        raise HTTPException(status_code=400, detail="Not enough permissions")

    # Request interrupt via active_streaming_manager
    interrupt_info = await active_streaming_manager.request_interrupt(session_id)

    if not interrupt_info["found"]:
        raise HTTPException(
            status_code=400,
            detail="No active stream to interrupt (message may have already completed)"
        )

    # If interrupt is pending (external_session_id not yet available)
    if interrupt_info["pending"]:
        logger.info(f"Interrupt queued for session {session_id} (waiting for external_session_id)")
        return {
            "status": "ok",
            "message": "Interrupt queued (session starting)",
            "session_id": str(session_id),
            "queued": True
        }

    # External session ID is available - forward to agent env
    external_session_id = interrupt_info["external_session_id"]

    # Get environment
    environment = session.get(AgentEnvironment, chat_session.environment_id)
    if not environment:
        raise HTTPException(status_code=404, detail="Environment not found")

    # Call environment interrupt endpoint
    import httpx

    base_url = MessageService.get_environment_url(environment)
    auth_headers = MessageService.get_auth_headers(environment)

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.post(
                f"{base_url}/chat/interrupt/{external_session_id}",
                headers=auth_headers
            )

            if response.status_code == 200:
                data = response.json()
                logger.info(f"Interrupt request sent successfully: {data}")
                return {
                    "status": "ok",
                    "message": "Interrupt request sent",
                    "session_id": str(session_id),
                    "external_session_id": external_session_id,
                    "queued": False
                }
            else:
                logger.warning(f"Environment returned {response.status_code}: {response.text}")
                return {
                    "status": "warning",
                    "message": "Interrupt request sent but session may have completed",
                    "session_id": str(session_id),
                    "queued": False
                }

    except httpx.RequestError as e:
        logger.error(f"Failed to send interrupt to environment: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to communicate with environment: {str(e)}"
        )


@router.get("/{session_id}/messages/streaming-status")
async def get_streaming_status(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    session_id: uuid.UUID,
) -> Any:
    """
    Check if a session is currently streaming.

    This allows frontend to:
    - Detect ongoing streams after page refresh
    - Reconnect to active streams
    - Show appropriate UI state

    Returns:
        {
            "is_streaming": bool,
            "stream_info": dict | None  # Only if streaming
        }
    """
    # Verify session exists and user owns it
    chat_session = session.get(Session, session_id)
    if not chat_session:
        raise HTTPException(status_code=404, detail="Session not found")

    if not current_user.is_superuser and (chat_session.user_id != current_user.id):
        raise HTTPException(status_code=400, detail="Not enough permissions")

    # Check if session is actively streaming
    is_streaming = await active_streaming_manager.is_streaming(session_id)

    if is_streaming:
        stream_info = await active_streaming_manager.get_stream_info(session_id)
        return {
            "is_streaming": True,
            "stream_info": stream_info
        }
    else:
        return {
            "is_streaming": False,
            "stream_info": None
        }
