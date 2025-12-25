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

    # Extract data from ORM objects BEFORE async generator
    # (to avoid detached instance errors)
    base_url = MessageService.get_environment_url(environment)
    auth_headers = MessageService.get_auth_headers(environment)
    session_mode = chat_session.mode
    user_message_content = message_in.content

    # Variables to collect agent response
    agent_response_parts = []
    streaming_events = []  # Store raw streaming events for visualization
    new_external_session_id = external_session_id
    response_metadata = {
        "external_session_id": external_session_id,
        "mode": session_mode
    }

    async def event_stream():
        """Generate SSE events for frontend"""
        nonlocal new_external_session_id, response_metadata

        try:
            # Stream from environment
            async for event in MessageService.send_message_to_environment_stream(
                base_url=base_url,
                auth_headers=auth_headers,
                user_message=user_message_content,
                mode=session_mode,
                external_session_id=external_session_id
            ):
                # Handle error events from message service
                if event.get("type") == "error":
                    # Update session status to "error" on SDK/HTTP errors
                    with DBSession(engine) as db:
                        SessionService.update_session_status(
                            db_session=db,
                            session_id=session_id,
                            status="error"
                        )
                    # Forward error event and continue (will exit loop after this)
                    event_json = json.dumps(event)
                    yield f"data: {event_json}\n\n"
                    return  # Exit early on error

                # Capture external session ID from done event (contains actual session_id from ResultMessage)
                if event.get("type") == "done" and not external_session_id:
                    # Get session_id from metadata (set by SDK manager from ResultMessage)
                    event_session_id = event.get("session_id") or event.get("metadata", {}).get("session_id")
                    if event_session_id:
                        new_external_session_id = event_session_id
                        logger.info(f"External session ID captured from ResultMessage: {new_external_session_id}")

                        # Store external session ID (use new DB session)
                        with DBSession(engine) as db:
                            chat_session_db = db.get(Session, session_id)
                            if chat_session_db:
                                SessionService.set_external_session_id(
                                    db=db,
                                    session=chat_session_db,
                                    external_session_id=new_external_session_id,
                                    sdk_type="claude_code"
                                )

                # Store raw event for visualization (exclude done/error events from storage)
                if event.get("type") not in ["done", "error", "session_created"]:
                    # Create a clean copy of event for storage
                    event_copy = {
                        "type": event.get("type"),
                        "content": event.get("content", ""),
                    }
                    if event.get("tool_name"):
                        event_copy["tool_name"] = event["tool_name"]
                    if event.get("metadata"):
                        # Store relevant metadata fields
                        event_copy["metadata"] = {
                            k: v for k, v in event["metadata"].items()
                            if k in ["tool_id", "tool_input", "model"]
                        }
                    streaming_events.append(event_copy)

                # Collect agent response content
                if event.get("content"):
                    agent_response_parts.append(event["content"])

                # Collect metadata from events
                event_metadata = event.get("metadata", {})
                if event_metadata:
                    # Update response metadata with any new info
                    if "model" in event_metadata:
                        response_metadata["model"] = event_metadata["model"]
                    if "total_cost_usd" in event_metadata:
                        response_metadata["total_cost_usd"] = event_metadata["total_cost_usd"]
                    if "claude_code_version" in event_metadata:
                        response_metadata["claude_code_version"] = event_metadata["claude_code_version"]
                    if "duration_ms" in event_metadata:
                        response_metadata["duration_ms"] = event_metadata["duration_ms"]
                    if "num_turns" in event_metadata:
                        response_metadata["num_turns"] = event_metadata["num_turns"]

                # Forward event to frontend
                event_json = json.dumps(event)
                yield f"data: {event_json}\n\n"

            # After stream completes, save agent response to database (use new DB session)
            if streaming_events:
                # Create summary content from text events only
                text_parts = [e["content"] for e in streaming_events if e["type"] == "assistant" and e.get("content")]
                agent_content = "\n\n".join(text_parts) if text_parts else "Agent response"

                # Store structured events in metadata
                response_metadata["external_session_id"] = new_external_session_id
                response_metadata["streaming_events"] = streaming_events

                with DBSession(engine) as db:
                    MessageService.create_message(
                        session=db,
                        session_id=session_id,
                        role="agent",
                        content=agent_content,
                        message_metadata=response_metadata
                    )
                logger.info(f"Agent response saved ({len(streaming_events)} events, model={response_metadata.get('model')})")

            # Update session status to "completed" after successful streaming
            with DBSession(engine) as db:
                SessionService.update_session_status(
                    db_session=db,
                    session_id=session_id,
                    status="completed"
                )

            # Send final done event to frontend to trigger UI refresh
            done_event = json.dumps({
                "type": "done",
                "content": "",
                "metadata": response_metadata
            })
            yield f"data: {done_event}\n\n"

        except Exception as e:
            logger.error(f"Error in message stream: {e}", exc_info=True)

            # Update session status to "error" when streaming fails
            with DBSession(engine) as db:
                SessionService.update_session_status(
                    db_session=db,
                    session_id=session_id,
                    status="error"
                )

            error_event = json.dumps({
                "type": "error",
                "content": str(e),
                "error_type": type(e).__name__
            })
            yield f"data: {error_event}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        }
    )
