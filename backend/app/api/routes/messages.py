import uuid
from typing import Any
import logging

from fastapi import APIRouter, HTTPException
from sqlmodel import select

from app.api.deps import CurrentUser, CurrentUserOrGuest, GuestShareContext, SessionDep
from app.models import (
    Session,
    AgentEnvironment,
    MessageCreate,
    MessagesPublic,
    SessionCommandPublic,
    SessionCommandsPublic,
    User,
)
from app.services.sessions.message_service import MessageService
from app.services.sessions.active_streaming_manager import active_streaming_manager
from app.services.sharing.agent_guest_share_service import AgentGuestShareService
from app.services.agents.command_service import CommandService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/sessions", tags=["messages"])


def _verify_message_access(
    caller: User | GuestShareContext,
    chat_session: Session,
    db_session: Any,
) -> None:
    """
    Verify that the caller has access to the session's messages.

    For anonymous guests: session must belong to their guest_share_id.
    For authenticated users: session must belong to them, OR they must
    have a grant for the session's guest_share_id, OR they are a superuser.

    Raises HTTPException if access is denied.
    """
    if isinstance(caller, GuestShareContext):
        if chat_session.guest_share_id != caller.guest_share_id:
            raise HTTPException(status_code=403, detail="Not enough permissions")
    else:
        current_user: User = caller
        if current_user.is_superuser:
            return
        if chat_session.user_id == current_user.id:
            return
        if chat_session.guest_share_id:
            has_grant = AgentGuestShareService.check_grant(
                db_session, current_user.id, chat_session.guest_share_id
            )
            if has_grant:
                return
        raise HTTPException(status_code=400, detail="Not enough permissions")


@router.get("/{session_id}/messages", response_model=MessagesPublic)
async def get_messages(
    session: SessionDep,
    caller: CurrentUserOrGuest,
    session_id: uuid.UUID,
    limit: int = 100,
    offset: int = 0,
) -> Any:
    """
    Get session messages.

    Supports both authenticated users and guest share callers.

    For messages with tools_needing_approval metadata, filters out tools
    that have already been approved in the agent's allowed_tools config.
    This prevents showing approval buttons for already-approved tools on page reload.

    For sessions with active streams, merges in-memory streaming events into the
    response so the API always returns the most current data (DB + in-memory buffer).
    """
    from app.models import Agent

    # Verify session exists and caller has access
    chat_session = session.get(Session, session_id)
    if not chat_session:
        raise HTTPException(status_code=404, detail="Session not found")

    _verify_message_access(caller, chat_session, session)

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
    messages = await MessageService.enrich_messages_with_streaming(messages, session_id)

    return MessagesPublic(data=messages, count=len(messages))


@router.post("/{session_id}/messages/stream")
async def send_message_stream(
    session: SessionDep,
    caller: CurrentUserOrGuest,
    session_id: uuid.UUID,
    message_in: MessageCreate,
) -> Any:
    """
    Send message to agent environment and stream response via WebSocket.

    Supports both authenticated users and guest share callers.

    This endpoint delegates to SessionService.send_session_message() which:
    1. Validates session ownership
    2. Handles file attachments (if present)
    3. Creates user message with sent_to_agent_status='pending'
    4. Initiates streaming

    Streaming events are emitted via WebSocket to room: session_{session_id}_stream
    Frontend should subscribe to this room before calling this endpoint.
    """
    from app.services.sessions.session_service import SessionService

    # Verify session access for guest callers
    chat_session = session.get(Session, session_id)
    if not chat_session:
        raise HTTPException(status_code=404, detail="Session not found")

    _verify_message_access(caller, chat_session, session)

    # Determine user_id for the message
    if isinstance(caller, GuestShareContext):
        user_id = caller.owner_id
    else:
        user_id = caller.id

    # Truncate page_context to protect against oversized payloads.
    # Mirrors the same limit applied in webapp_chat.py.
    _PAGE_CONTEXT_MAX_CHARS = 10_000
    safe_page_context: str | None = None
    if message_in.page_context:
        safe_page_context = message_in.page_context[:_PAGE_CONTEXT_MAX_CHARS]

    # Send message using centralized service method
    result = await SessionService.send_session_message(
        session_id=session_id,
        user_id=user_id,
        content=message_in.content,
        file_ids=message_in.file_ids,
        answers_to_message_id=message_in.answers_to_message_id,
        page_context=safe_page_context,
    )

    # Handle error results
    if result["action"] == "error":
        raise HTTPException(status_code=500, detail=result["message"])

    return MessageService.build_stream_response(session_id, result)


@router.post("/{session_id}/messages/interrupt")
async def interrupt_message(
    session: SessionDep,
    caller: CurrentUserOrGuest,
    session_id: uuid.UUID,
) -> Any:
    """
    Interrupt an active streaming message.

    Supports both authenticated users and guest share callers.

    Flow:
    1. Verify session access
    2. Request interrupt via active_streaming_manager
    3. Forward interrupt to agent environment if external_session_id available
    4. Return status

    Note: Interrupt only stops the current stream. Pending messages remain pending
    and will be processed when the user sends the next message.
    """
    # Verify session exists and caller has access
    chat_session = session.get(Session, session_id)
    if not chat_session:
        raise HTTPException(status_code=404, detail="Session not found")

    _verify_message_access(caller, chat_session, session)

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


@router.get("/{session_id}/messages/streaming-status")
async def get_streaming_status(
    session: SessionDep,
    caller: CurrentUserOrGuest,
    session_id: uuid.UUID,
) -> Any:
    """
    Check if a session is currently streaming.

    Supports both authenticated users and guest share callers.

    Uses DB interaction_status as the primary source of truth, with
    ActiveStreamingManager providing supplementary info (duration, external_session_id).

    Returns:
        {
            "is_streaming": bool,
            "stream_info": dict | None  # Only if streaming
        }
    """
    # Verify session exists and caller has access
    chat_session = session.get(Session, session_id)
    if not chat_session:
        raise HTTPException(status_code=404, detail="Session not found")

    _verify_message_access(caller, chat_session, session)

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


@router.get("/{session_id}/commands", response_model=SessionCommandsPublic)
async def list_session_commands(
    session: SessionDep,
    current_user: CurrentUser,
    session_id: uuid.UUID,
) -> Any:
    """
    List available slash commands for a session.

    Returns all registered slash commands with name, description, and availability status.
    The /rebuild-env command is marked unavailable if any session on the same environment
    is actively streaming.

    Authenticated users only; no guest access (command autocomplete is a UX aid for
    the main chat session page which requires authentication).
    """
    # Ensure command handlers are registered (lazy import mirrors session_service.py pattern)
    import app.services.agents.commands  # noqa: F401

    chat_session = session.get(Session, session_id)
    if not chat_session:
        raise HTTPException(status_code=404, detail="Session not found")

    # Verify ownership
    if not current_user.is_superuser and chat_session.user_id != current_user.id:
        raise HTTPException(status_code=400, detail="Not enough permissions")

    # Determine /rebuild-env availability: unavailable if any session on the same
    # environment is actively streaming (mirrors the check in RebuildEnvCommandHandler)
    is_rebuild_env_available = True
    if chat_session.environment_id:
        try:
            session_ids = set(
                session.exec(
                    select(Session.id).where(
                        Session.environment_id == chat_session.environment_id
                    )
                ).all()
            )
            if session_ids:
                is_rebuild_env_available = not await active_streaming_manager.is_any_session_streaming(
                    session_ids
                )
        except Exception:
            logger.warning(
                "Failed to check streaming status for /rebuild-env availability",
                exc_info=True,
            )
            is_rebuild_env_available = True  # Default to available on error

    commands = []
    for handler in CommandService.list_handlers():
        if handler.name == "/rebuild-env":
            is_available = is_rebuild_env_available
        else:
            is_available = True
        commands.append(
            SessionCommandPublic(
                name=handler.name,
                description=handler.description,
                is_available=is_available,
            )
        )

    return SessionCommandsPublic(commands=commands)
