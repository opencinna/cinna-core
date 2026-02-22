import uuid
from typing import Any
import logging

from fastapi import APIRouter, HTTPException, BackgroundTasks
from fastapi.responses import StreamingResponse

from app.api.deps import CurrentUser, SessionDep
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
async def get_messages(
    session: SessionDep,
    current_user: CurrentUser,
    session_id: uuid.UUID,
    limit: int = 100,
    offset: int = 0,
) -> Any:
    """
    Get session messages.

    For messages with tools_needing_approval metadata, filters out tools
    that have already been approved in the agent's allowed_tools config.
    This prevents showing approval buttons for already-approved tools on page reload.

    For sessions with active streams, merges in-memory streaming events into the
    response so the API always returns the most current data (DB + in-memory buffer).
    """
    from app.models import Agent

    # Verify session exists and user owns it
    chat_session = session.get(Session, session_id)
    if not chat_session:
        raise HTTPException(status_code=404, detail="Session not found")

    if not current_user.is_superuser and (chat_session.user_id != current_user.id):
        raise HTTPException(status_code=400, detail="Not enough permissions")

    messages = MessageService.get_session_messages(
        session=session, session_id=session_id, limit=limit, offset=offset
    )

    # Get agent's allowed_tools to filter out already-approved tools from messages
    allowed_tools: set[str] = set()
    if chat_session.environment_id:
        environment = session.get(AgentEnvironment, chat_session.environment_id)
        if environment and environment.agent_id:
            agent = session.get(Agent, environment.agent_id)
            if agent and agent.agent_sdk_config:
                allowed_tools = set(agent.agent_sdk_config.get("allowed_tools", []))

    # Filter tools_needing_approval for each message
    if allowed_tools:
        for msg in messages:
            if msg.message_metadata and "tools_needing_approval" in msg.message_metadata:
                original_tools = msg.message_metadata.get("tools_needing_approval", [])
                # Filter out tools that are now approved
                filtered_tools = [t for t in original_tools if t not in allowed_tools]
                # Update the metadata (this doesn't persist to DB, just affects this response)
                msg.message_metadata["tools_needing_approval"] = filtered_tools

    # Merge in-memory streaming events if session has an active stream
    if await active_streaming_manager.is_streaming(session_id):
        stream_data = await active_streaming_manager.get_stream_events(session_id)
        if stream_data and stream_data["streaming_events"]:
            # Find the in-progress message in the response
            in_progress_msg = None
            for msg in messages:
                if msg.message_metadata and msg.message_metadata.get("streaming_in_progress"):
                    in_progress_msg = msg
                    break

            if in_progress_msg:
                # Get DB events and in-memory events
                db_events = in_progress_msg.message_metadata.get("streaming_events", [])
                db_max_seq = max((e.get("event_seq", 0) for e in db_events), default=0)
                # Append in-memory events beyond last flush
                new_events = [
                    e for e in stream_data["streaming_events"]
                    if e.get("event_seq", 0) > db_max_seq
                ]
                if new_events:
                    in_progress_msg.message_metadata["streaming_events"] = db_events + new_events
                # Update content with full accumulated text
                if stream_data["accumulated_content"]:
                    in_progress_msg.content = stream_data["accumulated_content"]

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

    This endpoint delegates to SessionService.send_session_message() which:
    1. Validates session ownership
    2. Handles file attachments (if present)
    3. Creates user message with sent_to_agent_status='pending'
    4. Initiates streaming

    Streaming events are emitted via WebSocket to room: session_{session_id}_stream
    Frontend should subscribe to this room before calling this endpoint.
    """
    from app.services.session_service import SessionService

    # Send message using centralized service method
    result = await SessionService.send_session_message(
        session_id=session_id,
        user_id=current_user.id,
        content=message_in.content,
        file_ids=message_in.file_ids,
        answers_to_message_id=message_in.answers_to_message_id,
    )

    # Handle error results
    if result["action"] == "error":
        raise HTTPException(status_code=500, detail=result["message"])

    # Handle command results (already delivered via WebSocket)
    if result["action"] == "command_executed":
        return {
            "status": "ok",
            "session_id": str(session_id),
            "stream_room": f"session_{session_id}_stream",
            "message": "Command executed",
            "command_executed": True,
        }

    # Build response
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
    else:
        response["message"] = result.get("message", "Message received")

    # Add files info if present
    if result.get("files_attached"):
        response["files_attached"] = result["files_attached"]

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

    Uses DB interaction_status as the primary source of truth, with
    ActiveStreamingManager providing supplementary info (duration, external_session_id).

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

    # Use DB interaction_status as primary source of truth
    is_streaming = chat_session.interaction_status == "running"

    if is_streaming:
        # Get supplementary info from ActiveStreamingManager if available
        stream_info = await active_streaming_manager.get_stream_info(session_id)
        if not stream_info:
            # Fallback: construct from DB fields
            stream_info = {
                "session_id": str(session_id),
                "started_at": chat_session.streaming_started_at.isoformat() if chat_session.streaming_started_at else None,
            }
        return {
            "is_streaming": True,
            "stream_info": stream_info
        }
    else:
        return {
            "is_streaming": False,
            "stream_info": None
        }
