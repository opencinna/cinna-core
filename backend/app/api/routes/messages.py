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
    SessionMessage,
    ActivityCreate,
)
from app.services.message_service import MessageService
from app.services.session_service import SessionService
from app.services.ai_functions_service import AIFunctionsService
from app.services.active_streaming_manager import active_streaming_manager
from app.services.activity_service import ActivityService

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
        """
        Generate SSE events for frontend with true decoupling.

        ARCHITECTURE: Uses a queue-based approach to decouple frontend streaming
        from backend-to-agent-env streaming:

        1. Background task consumes from MessageService.stream_message_with_events()
        2. Events are pushed to an asyncio.Queue
        3. Frontend-facing generator reads from queue
        4. If frontend disconnects, background task continues independently

        This ensures data integrity even when the client closes the connection.
        """
        # Create queue for event passing (unbounded to avoid blocking)
        event_queue = asyncio.Queue()

        # Flag to track if frontend is still connected
        frontend_connected = True

        async def _create_error_activity(session_id: uuid.UUID, error_message: str | None):
            """
            Create activity for background stream error.
            Called when stream fails while user was disconnected.
            """
            try:
                def _create_activity():
                    with DBSession(engine) as db:
                        # Get session to find user_id and environment_id
                        chat_session = db.get(Session, session_id)
                        if not chat_session:
                            logger.warning(f"Cannot create error activity: session {session_id} not found")
                            return

                        # Get environment to find agent_id
                        environment = db.get(AgentEnvironment, chat_session.environment_id)
                        if not environment:
                            logger.warning(f"Cannot create error activity: environment not found for session {session_id}")
                            return

                        agent_id = environment.agent_id
                        user_id = chat_session.user_id

                        # Create descriptive text for the error activity
                        activity_text = "Session encountered an error"
                        if error_message:
                            # Truncate error message if too long
                            if len(error_message) > 100:
                                activity_text = f"Error: {error_message[:97]}..."
                            else:
                                activity_text = f"Error: {error_message}"

                        # Create "error_occurred" activity
                        ActivityService.create_activity(
                            db_session=db,
                            user_id=user_id,
                            data=ActivityCreate(
                                session_id=session_id,
                                agent_id=agent_id,
                                activity_type="error_occurred",
                                text=activity_text,
                                action_required="",
                                is_read=False
                            )
                        )
                        logger.info(f"Created 'error_occurred' activity for session {session_id}: {activity_text}")

                await asyncio.to_thread(_create_activity)
            except Exception as e:
                logger.error(f"Failed to create error activity for session {session_id}: {e}", exc_info=True)

        async def _create_unread_completion_activities(session_id: uuid.UUID):
            """
            Create activities for unread session completion.
            Called when stream completes while user was disconnected.
            """
            try:
                def _create_activities():
                    with DBSession(engine) as db:
                        # Get session to find user_id and environment_id
                        chat_session = db.get(Session, session_id)
                        if not chat_session:
                            logger.warning(f"Cannot create activities: session {session_id} not found")
                            return

                        # Get environment to find agent_id
                        environment = db.get(AgentEnvironment, chat_session.environment_id)
                        if not environment:
                            logger.warning(f"Cannot create activities: environment not found for session {session_id}")
                            return

                        agent_id = environment.agent_id
                        user_id = chat_session.user_id

                        # Only create activities if session was actually completed (not interrupted)
                        # Interrupted sessions have status "active", completed sessions have status "completed"
                        if chat_session.status != "completed":
                            logger.info(f"Skipping activities for session {session_id} with status '{chat_session.status}' (not completed)")
                            return

                        # Get the latest agent message to check if it has questions
                        from sqlmodel import select, desc
                        latest_message_stmt = (
                            select(SessionMessage)
                            .where(SessionMessage.session_id == session_id)
                            .where(SessionMessage.role == "agent")
                            .order_by(desc(SessionMessage.sequence_number))
                            .limit(1)
                        )
                        latest_message = db.exec(latest_message_stmt).first()

                        # Create "session_completed" activity
                        ActivityService.create_activity(
                            db_session=db,
                            user_id=user_id,
                            data=ActivityCreate(
                                session_id=session_id,
                                agent_id=agent_id,
                                activity_type="session_completed",
                                text="Session completed",
                                action_required="",
                                is_read=False
                            )
                        )
                        logger.info(f"Created 'session_completed' activity for session {session_id}")

                        # If the latest message has unanswered questions, create additional activity
                        if latest_message and latest_message.tool_questions_status == "unanswered":
                            ActivityService.create_activity(
                                db_session=db,
                                user_id=user_id,
                                data=ActivityCreate(
                                    session_id=session_id,
                                    agent_id=agent_id,
                                    activity_type="questions_asked",
                                    text="Agent asked questions that need answers",
                                    action_required="answers_required",
                                    is_read=False
                                )
                            )
                            logger.info(f"Created 'questions_asked' activity for session {session_id}")

                await asyncio.to_thread(_create_activities)
            except Exception as e:
                logger.error(f"Failed to create unread completion activities for session {session_id}: {e}", exc_info=True)

        async def _create_session_running_activity():
            """
            Create 'session_running' activity when stream starts.
            This activity will be shown if user is not watching the session.
            """
            try:
                def _create_activity():
                    with DBSession(engine) as db:
                        # Get session to find user_id and environment_id
                        chat_session = db.get(Session, session_id)
                        if not chat_session:
                            logger.warning(f"Cannot create running activity: session {session_id} not found")
                            return

                        # Get environment to find agent_id
                        environment = db.get(AgentEnvironment, chat_session.environment_id)
                        if not environment:
                            logger.warning(f"Cannot create running activity: environment not found for session {session_id}")
                            return

                        agent_id = environment.agent_id
                        user_id = chat_session.user_id

                        # Create "session_running" activity (unread by default)
                        ActivityService.create_activity(
                            db_session=db,
                            user_id=user_id,
                            data=ActivityCreate(
                                session_id=session_id,
                                agent_id=agent_id,
                                activity_type="session_running",
                                text="Session is running",
                                action_required="",
                                is_read=False
                            )
                        )
                        logger.info(f"Created 'session_running' activity for session {session_id}")

                await asyncio.to_thread(_create_activity)
            except Exception as e:
                logger.error(f"Failed to create session_running activity for session {session_id}: {e}", exc_info=True)

        async def _delete_session_running_activity():
            """
            Delete 'session_running' activity when stream completes with frontend watching.
            """
            try:
                def _delete_activity():
                    with DBSession(engine) as db:
                        ActivityService.delete_activity_by_session_and_type(
                            db_session=db,
                            session_id=session_id,
                            activity_type="session_running"
                        )
                        logger.info(f"Deleted 'session_running' activity for session {session_id} (frontend was watching)")

                await asyncio.to_thread(_delete_activity)
            except Exception as e:
                logger.error(f"Failed to delete session_running activity for session {session_id}: {e}", exc_info=True)

        async def _update_running_activity_to_completion():
            """
            Update 'session_running' activity to completion status.
            Called when stream completes while user was disconnected.
            """
            try:
                def _update_or_replace():
                    with DBSession(engine) as db:
                        # Find the session_running activity
                        running_activity = ActivityService.find_activity_by_session_and_type(
                            db_session=db,
                            session_id=session_id,
                            activity_type="session_running"
                        )

                        if running_activity:
                            # Delete the running activity
                            db.delete(running_activity)
                            db.commit()
                            logger.info(f"Deleted 'session_running' activity for session {session_id}")

                        # Create completion activities (existing logic)
                        _create_unread_completion_activities_sync(db)

                def _create_unread_completion_activities_sync(db: DBSession):
                    """Sync version of _create_unread_completion_activities for inline use"""
                    # Get session to find user_id and environment_id
                    chat_session = db.get(Session, session_id)
                    if not chat_session:
                        logger.warning(f"Cannot create activities: session {session_id} not found")
                        return

                    # Get environment to find agent_id
                    environment = db.get(AgentEnvironment, chat_session.environment_id)
                    if not environment:
                        logger.warning(f"Cannot create activities: environment not found for session {session_id}")
                        return

                    agent_id = environment.agent_id
                    user_id = chat_session.user_id

                    # Only create activities if session was actually completed (not interrupted)
                    if chat_session.status != "completed":
                        logger.info(f"Skipping activities for session {session_id} with status '{chat_session.status}' (not completed)")
                        return

                    # Get the latest agent message to check if it has questions
                    from sqlmodel import select, desc
                    latest_message_stmt = (
                        select(SessionMessage)
                        .where(SessionMessage.session_id == session_id)
                        .where(SessionMessage.role == "agent")
                        .order_by(desc(SessionMessage.sequence_number))
                        .limit(1)
                    )
                    latest_message = db.exec(latest_message_stmt).first()

                    # Create "session_completed" activity
                    ActivityService.create_activity(
                        db_session=db,
                        user_id=user_id,
                        data=ActivityCreate(
                            session_id=session_id,
                            agent_id=agent_id,
                            activity_type="session_completed",
                            text="Session completed",
                            action_required="",
                            is_read=False
                        )
                    )
                    logger.info(f"Created 'session_completed' activity for session {session_id}")

                    # If the latest message has unanswered questions, create additional activity
                    if latest_message and latest_message.tool_questions_status == "unanswered":
                        ActivityService.create_activity(
                            db_session=db,
                            user_id=user_id,
                            data=ActivityCreate(
                                session_id=session_id,
                                agent_id=agent_id,
                                activity_type="questions_asked",
                                text="Agent asked questions that need answers",
                                action_required="answers_required",
                                is_read=False
                            )
                        )
                        logger.info(f"Created 'questions_asked' activity for session {session_id}")

                await asyncio.to_thread(_update_or_replace)
            except Exception as e:
                logger.error(f"Failed to update running activity to completion for session {session_id}: {e}", exc_info=True)

        async def _update_running_activity_to_error(error_message: str | None):
            """
            Update 'session_running' activity to error status.
            Called when stream encounters error while user was disconnected.
            """
            try:
                def _update_or_replace():
                    with DBSession(engine) as db:
                        # Find the session_running activity
                        running_activity = ActivityService.find_activity_by_session_and_type(
                            db_session=db,
                            session_id=session_id,
                            activity_type="session_running"
                        )

                        if running_activity:
                            # Delete the running activity
                            db.delete(running_activity)
                            db.commit()
                            logger.info(f"Deleted 'session_running' activity for session {session_id}")

                        # Create error activity
                        _create_error_activity_sync(db, error_message)

                def _create_error_activity_sync(db: DBSession, error_msg: str | None):
                    """Sync version of _create_error_activity for inline use"""
                    # Get session to find user_id and environment_id
                    chat_session = db.get(Session, session_id)
                    if not chat_session:
                        logger.warning(f"Cannot create error activity: session {session_id} not found")
                        return

                    # Get environment to find agent_id
                    environment = db.get(AgentEnvironment, chat_session.environment_id)
                    if not environment:
                        logger.warning(f"Cannot create error activity: environment not found for session {session_id}")
                        return

                    agent_id = environment.agent_id
                    user_id = chat_session.user_id

                    # Create descriptive text for the error activity
                    activity_text = "Session encountered an error"
                    if error_msg:
                        # Truncate error message if too long
                        if len(error_msg) > 100:
                            activity_text = f"Error: {error_msg[:97]}..."
                        else:
                            activity_text = f"Error: {error_msg}"

                    # Create "error_occurred" activity
                    ActivityService.create_activity(
                        db_session=db,
                        user_id=user_id,
                        data=ActivityCreate(
                            session_id=session_id,
                            agent_id=agent_id,
                            activity_type="error_occurred",
                            text=activity_text,
                            action_required="",
                            is_read=False
                        )
                    )
                    logger.info(f"Created 'error_occurred' activity for session {session_id}: {activity_text}")

                await asyncio.to_thread(_update_or_replace)
            except Exception as e:
                logger.error(f"Failed to update running activity to error for session {session_id}: {e}", exc_info=True)

        async def stream_consumer_task():
            """
            Background task that consumes events from agent env.
            Runs independently of frontend connection state.
            """
            try:
                event_count = 0
                error_occurred = False
                error_message = None

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
                    event_count += 1
                    # Always put event in queue (even if frontend disconnected)
                    await event_queue.put(event)

                    # Track if an error occurred
                    if event.get("type") == "error":
                        error_occurred = True
                        error_message = event.get("content", "Unknown error occurred")
                        logger.warning(f"Error detected in stream for session {session_id}: {error_message}")

                # Signal completion
                await event_queue.put(None)

                if frontend_connected:
                    logger.info(
                        f"Stream consumer completed for session {session_id} "
                        f"({event_count} events, frontend still connected, error={error_occurred})"
                    )
                    # Frontend was watching - delete the "session_running" activity
                    await _delete_session_running_activity()

                    # But if error occurred, still create error activity (errors are important to track)
                    if error_occurred:
                        await _create_error_activity(session_id, error_message)
                else:
                    logger.info(
                        f"Stream consumer completed for session {session_id} "
                        f"({event_count} events, frontend disconnected earlier, error={error_occurred})"
                    )
                    # Update or replace the "session_running" activity with final result
                    if error_occurred:
                        await _update_running_activity_to_error(error_message)
                    else:
                        await _update_running_activity_to_completion()
            except Exception as e:
                logger.error(
                    f"Error in stream consumer for session {session_id}: {e}",
                    exc_info=True
                )
                # Put error marker in queue
                await event_queue.put({"type": "error", "content": str(e)})
                await event_queue.put(None)

                # Update running activity to error if frontend was disconnected
                if not frontend_connected:
                    await _update_running_activity_to_error(str(e))

        # Create "session_running" activity before stream starts
        await _create_session_running_activity()

        # Start background consumer task BEFORE we start yielding to frontend
        # This is the key to decoupling - the task is independent
        consumer_task = asyncio.create_task(stream_consumer_task())

        try:
            # Yield events to frontend from queue
            while True:
                event = await event_queue.get()

                # None signals completion
                if event is None:
                    break

                # Format event as SSE and forward to frontend
                event_json = json.dumps(event)
                yield f"data: {event_json}\n\n"

        except (asyncio.CancelledError, GeneratorExit) as e:
            # Frontend disconnected
            frontend_connected = False
            logger.warning(
                f"Frontend disconnected from session {session_id} (error: {type(e).__name__}). "
                f"Backend-to-agent-env stream continues independently."
            )
            # Don't cancel consumer_task - let it continue!
            # The background task will keep consuming and saving data
            raise
        except Exception as e:
            # Unexpected error
            frontend_connected = False
            logger.error(
                f"Unexpected error while streaming to frontend for session {session_id}: {e}",
                exc_info=True
            )
            # Don't cancel consumer_task in case it can still save data
            raise

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
