import uuid
from typing import Any
import logging

from fastapi import APIRouter, HTTPException, BackgroundTasks
from fastapi.responses import StreamingResponse
from sqlmodel import Session as DBSession

from app.api.deps import CurrentUser, SessionDep
from app.core.db import engine
from app.models import (
    Session,
    AgentEnvironment,
    MessageCreate,
    MessagePublic,
    MessagesPublic,
)
from app.services.message_service import MessageService
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


@router.post("/{session_id}/messages/stream")
async def send_message_stream(
    session: SessionDep,
    current_user: CurrentUser,
    session_id: uuid.UUID,
    message_in: MessageCreate,
) -> Any:
    """
    Send message to agent environment and stream response via WebSocket.

    This endpoint:
    1. Validates session ownership
    2. Handles file attachments (if present) via MessageService
    3. Creates user message with sent_to_agent_status='pending'
    4. Delegates to SessionService to initiate streaming
    5. Returns immediately with status

    Streaming events are emitted via WebSocket to room: session_{session_id}_stream
    Frontend should subscribe to this room before calling this endpoint.
    """
    # Verify session exists and user owns it
    chat_session = session.get(Session, session_id)
    if not chat_session:
        raise HTTPException(status_code=404, detail="Session not found")

    if not current_user.is_superuser and (chat_session.user_id != current_user.id):
        raise HTTPException(status_code=400, detail="Not enough permissions")

    # Handle file attachments if present
    has_files = bool(message_in.file_ids)

    if has_files:
        # Prepare user message with files using service layer
        # This creates the message with sent_to_agent_status='pending' by default
        try:
            user_message, message_content_for_agent = await MessageService.prepare_user_message_with_files(
                session=session,
                session_id=session_id,
                message_content=message_in.content,
                file_ids=message_in.file_ids,
                environment_id=chat_session.environment_id,
                user_id=current_user.id,
                answers_to_message_id=message_in.answers_to_message_id
            )
            logger.info(f"Prepared message with {len(message_in.file_ids)} files for session {session_id}")
        except HTTPException:
            raise  # Re-raise HTTP exceptions from service
        except Exception as e:
            logger.error(f"Failed to prepare message with files: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Failed to prepare message with files: {str(e)}")
    else:
        # Create user message without files
        # sent_to_agent_status='pending' by default
        user_message = MessageService.create_message(
            session=session,
            session_id=session_id,
            role="user",
            content=message_in.content,
            answers_to_message_id=message_in.answers_to_message_id
        )
        logger.info(f"Created user message for session {session_id}")

    # Delegate to SessionService to decide when to stream
    from app.services.session_service import SessionService

    result = await SessionService.initiate_stream(
        session_id=session_id,
        get_fresh_db_session=lambda: DBSession(engine)
    )

    # Build response based on result
    response = {
        "status": "ok",
        "session_id": str(session_id),
        "stream_room": f"session_{session_id}_stream"
    }

    if result["action"] == "streaming":
        response["message"] = result["message"]
        response["streaming"] = True
    elif result["action"] == "pending":
        response["message"] = result["message"]
        response["pending"] = True
    elif result["action"] == "error":
        raise HTTPException(status_code=500, detail=result["message"])
    else:
        response["message"] = result.get("message", "Message received")

    if has_files:
        response["files_attached"] = len(message_in.file_ids)

    return response


@router.post("/{session_id}/messages/interrupt")
async def interrupt_message(
    session: SessionDep,
    current_user: CurrentUser,
    session_id: uuid.UUID,
) -> Any:
    """
    Interrupt an active streaming message.

    Flow:
    1. Verify session ownership
    2. Request interrupt via active_streaming_manager
    3. Forward interrupt to agent environment if external_session_id available
    4. Return status

    Note: Interrupt only stops the current stream. Pending messages remain pending
    and will be processed when the user sends the next message.
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

    # Forward interrupt to environment using MessageService
    base_url = MessageService.get_environment_url(environment)
    auth_headers = MessageService.get_auth_headers(environment)

    try:
        result = await MessageService.forward_interrupt_to_environment(
            base_url=base_url,
            auth_headers=auth_headers,
            external_session_id=external_session_id
        )

        return {
            **result,
            "session_id": str(session_id),
            "queued": False
        }

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=str(e)
        )


@router.get("/{session_id}/messages/streaming-status")
async def get_streaming_status(
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
