from uuid import UUID
from datetime import datetime
from typing import AsyncIterator
import json
import httpx
import logging
import asyncio
from sqlmodel import Session, select, func
from sqlalchemy.orm import Session as AlchemySession
from app.models import SessionMessage, Session as ChatSession, AgentEnvironment, Agent, SessionUpdate
from app.services.active_streaming_manager import active_streaming_manager
from app.core.db import engine as db_engine

logger = logging.getLogger(__name__)


class MessageService:
    @staticmethod
    def create_message(
        session: Session,
        session_id: UUID,
        role: str,
        content: str,
        message_metadata: dict | None = None,
        answers_to_message_id: UUID | None = None,
        tool_questions_status: str | None = None,
        status: str = "",
        status_message: str | None = None,
        file_ids: list[UUID] | None = None,
    ) -> SessionMessage:
        """Create message in session with auto-incremented sequence.

        If file_ids provided, creates message_files junction records.
        """
        # Get the next sequence number for this session
        statement = select(func.max(SessionMessage.sequence_number)).where(
            SessionMessage.session_id == session_id
        )
        max_sequence = session.exec(statement).first()
        next_sequence = (max_sequence or 0) + 1

        message = SessionMessage(
            session_id=session_id,
            role=role,
            content=content,
            sequence_number=next_sequence,
            message_metadata=message_metadata or {},
            answers_to_message_id=answers_to_message_id,
            tool_questions_status=tool_questions_status,
            status=status,
            status_message=status_message,
        )
        session.add(message)

        # If this message is answering another message's questions, update that message's status
        if answers_to_message_id:
            referenced_message = session.get(SessionMessage, answers_to_message_id)
            if referenced_message:
                referenced_message.tool_questions_status = "answered"
                session.add(referenced_message)

        # Update session's last_message_at
        chat_session = session.get(ChatSession, session_id)
        if chat_session:
            chat_session.last_message_at = datetime.utcnow()
            session.add(chat_session)

        # Flush to ensure message exists in DB before creating message_files
        # This prevents foreign key constraint violations
        session.flush()

        # Create message_files records if files attached
        if file_ids:
            from app.models.file_upload import MessageFile
            for file_id in file_ids:
                message_file = MessageFile(
                    message_id=message.id,
                    file_id=file_id,
                    # agent_env_path set later when files uploaded to agent-env
                )
                session.add(message_file)

        session.commit()
        session.refresh(message)
        return message

    @staticmethod
    def get_session_messages(
        session: Session, session_id: UUID, limit: int = 100, offset: int = 0
    ) -> list["MessagePublic"]:
        """Get messages for session ordered by sequence with files populated"""
        from app.services.file_service import FileService
        from app.models.session import MessagePublic

        statement = (
            select(SessionMessage)
            .where(SessionMessage.session_id == session_id)
            .order_by(SessionMessage.sequence_number)
            .offset(offset)
            .limit(limit)
        )
        db_messages = list(session.exec(statement).all())

        # Convert to MessagePublic and populate files
        messages = []
        for msg in db_messages:
            files = FileService.get_message_files(
                session=session,
                message_id=msg.id
            )
            message_public = MessagePublic(
                id=msg.id,
                session_id=msg.session_id,
                role=msg.role,
                content=msg.content,
                sequence_number=msg.sequence_number,
                timestamp=msg.timestamp,
                message_metadata=msg.message_metadata,
                tool_questions_status=msg.tool_questions_status,
                answers_to_message_id=msg.answers_to_message_id,
                status=msg.status,
                status_message=msg.status_message,
                files=files
            )
            messages.append(message_public)

        return messages

    @staticmethod
    def get_last_n_messages(
        session: Session, session_id: UUID, n: int = 20
    ) -> list[SessionMessage]:
        """Get last N messages for context window"""
        statement = (
            select(SessionMessage)
            .where(SessionMessage.session_id == session_id)
            .order_by(SessionMessage.sequence_number.desc())
            .limit(n)
        )
        messages = list(session.exec(statement).all())
        # Reverse to get chronological order
        return list(reversed(messages))

    @staticmethod
    def detect_ask_user_question_tool(streaming_events: list[dict]) -> bool:
        """Check if AskUserQuestion tool was called in streaming events"""
        for event in streaming_events:
            if event.get("type") == "tool" and event.get("tool_name") == "AskUserQuestion":
                return True
        return False

    @staticmethod
    def get_environment_url(environment: AgentEnvironment) -> str:
        """
        Get environment base URL from config.

        Args:
            environment: AgentEnvironment instance

        Returns:
            Base URL for the environment (e.g., "http://agent-{env_id}:8000")
        """
        container_name = environment.config.get("container_name", f"agent-{environment.id}")
        port = environment.config.get("port", 8000)
        return f"http://{container_name}:{port}"

    @staticmethod
    def get_auth_headers(environment: AgentEnvironment) -> dict:
        """
        Get authentication headers for environment API calls.

        Args:
            environment: AgentEnvironment instance

        Returns:
            Headers dict with Authorization bearer token
        """
        auth_token = environment.config.get("auth_token")
        if auth_token:
            return {"Authorization": f"Bearer {auth_token}"}
        return {}

    @staticmethod
    async def forward_interrupt_to_environment(
        base_url: str,
        auth_headers: dict,
        external_session_id: str
    ) -> dict:
        """
        Forward interrupt request to the agent environment.

        Args:
            base_url: Environment base URL
            auth_headers: Authentication headers
            external_session_id: External SDK session ID

        Returns:
            dict with status information:
            {
                "status": "ok" | "warning" | "error",
                "message": str,
                "external_session_id": str
            }

        Raises:
            Exception: If communication with environment fails
        """
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
                        "external_session_id": external_session_id
                    }
                else:
                    logger.warning(f"Environment returned {response.status_code}: {response.text}")
                    return {
                        "status": "warning",
                        "message": "Interrupt request sent but session may have completed",
                        "external_session_id": external_session_id
                    }

        except httpx.RequestError as e:
            logger.error(f"Failed to send interrupt to environment: {e}")
            raise Exception(f"Failed to communicate with environment: {str(e)}")

    @staticmethod
    async def send_message_to_environment_stream(
        base_url: str,
        auth_headers: dict,
        user_message: str,
        mode: str,
        agent_sdk: str = "claude",
        external_session_id: str | None = None,
        backend_session_id: str | None = None
    ) -> AsyncIterator[dict]:
        """
        Send message to environment and stream response.

        Yields SSE events from the environment server in the format:
        {
            "type": "session_created" | "assistant" | "tool" | "result" | "error" | "done",
            "content": str,
            "session_id": str (external SDK session ID),
            "metadata": dict
        }

        Args:
            base_url: Environment base URL
            auth_headers: Authentication headers
            user_message: User's message content
            mode: Session mode ("building" or "conversation")
            agent_sdk: SDK to use ("claude" is currently the only option)
            external_session_id: Optional external SDK session ID for resumption

        Yields:
            dict: SSE event chunks from environment
        """
        headers = {**auth_headers, "Content-Type": "application/json"}

        # Prepare request payload
        payload = {
            "message": user_message,
            "mode": mode,
            "agent_sdk": agent_sdk,
            "session_id": external_session_id,
            "backend_session_id": backend_session_id,
        }

        logger.info(
            f"Sending message to {base_url}/chat/stream "
            f"(mode={mode}, agent_sdk={agent_sdk}, external_session_id={external_session_id})"
        )

        try:
            async with httpx.AsyncClient(timeout=300.0) as client:
                async with client.stream(
                    "POST",
                    f"{base_url}/chat/stream",
                    json=payload,
                    headers=headers
                ) as response:
                    response.raise_for_status()

                    # Parse SSE stream
                    async for line in response.aiter_lines():
                        if line.startswith("data: "):
                            data_str = line[6:]  # Remove "data: " prefix
                            try:
                                event_data = json.loads(data_str)
                                yield event_data
                            except json.JSONDecodeError as e:
                                logger.error(f"Failed to parse SSE event: {data_str}, error: {e}")
                                continue

        except httpx.HTTPStatusError as e:
            logger.error(f"HTTP error from environment: {e.response.status_code} - {e.response.text}")
            yield {
                "type": "error",
                "content": f"Environment returned error: {e.response.status_code}",
                "error_type": "HTTPError"
            }
        except httpx.RequestError as e:
            logger.error(f"Request error to environment: {e}")
            yield {
                "type": "error",
                "content": f"Failed to connect to environment: {str(e)}",
                "error_type": "ConnectionError"
            }
        except Exception as e:
            logger.error(f"Unexpected error streaming from environment: {e}", exc_info=True)
            yield {
                "type": "error",
                "content": f"Unexpected error: {str(e)}",
                "error_type": type(e).__name__
            }

    @staticmethod
    async def stream_message_with_events(
        session_id: UUID,
        environment_id: UUID,
        base_url: str,
        auth_headers: dict,
        user_message_content: str,
        session_mode: str,
        agent_sdk: str,
        external_session_id: str | None,
        get_fresh_db_session: callable
    ) -> AsyncIterator[dict]:
        """
        Stream message to environment and handle all business logic.

        This method:
        - Streams message to environment
        - Handles session ID capture
        - Saves messages to database
        - Updates session status
        - Syncs agent prompts (for building mode)
        - Yields SSE events for frontend

        Args:
            session_id: Session UUID
            environment_id: Environment UUID (for refetching in fresh sessions)
            base_url: Environment base URL
            auth_headers: Environment auth headers
            user_message_content: User's message
            session_mode: "building" or "conversation"
            agent_sdk: SDK to use ("claude" is currently the only option)
            external_session_id: External SDK session ID (None for new)
            get_fresh_db_session: Callable that returns a fresh DB session (context manager)

        Yields:
            dict: SSE event dictionaries
        """

        # Register this as an active stream BEFORE starting
        # This allows frontend to reconnect if it disconnects during streaming
        await active_streaming_manager.register_stream(
            session_id=session_id,
            external_session_id=external_session_id
        )

        # Set interaction_status to 'running'
        def _set_running_status():
            with get_fresh_db_session() as db:
                from app.services.session_service import SessionService
                from app.models import SessionUpdate
                SessionService.update_session(
                    db_session=db,
                    session_id=session_id,
                    data=SessionUpdate(interaction_status="running")
                )
        await asyncio.to_thread(_set_running_status)

        # Variables to collect agent response
        agent_response_parts = []
        streaming_events = []  # Store raw streaming events for visualization
        new_external_session_id = external_session_id
        was_interrupted = False  # Track if message was interrupted
        agent_message_id = None  # Track agent message ID for early creation
        response_metadata = {
            "external_session_id": external_session_id,
            "mode": session_mode,
            "agent_sdk": agent_sdk
        }

        try:
            # Stream from environment
            async for event in MessageService.send_message_to_environment_stream(
                base_url=base_url,
                auth_headers=auth_headers,
                user_message=user_message_content,
                mode=session_mode,
                agent_sdk=agent_sdk,
                external_session_id=external_session_id,
                backend_session_id=str(session_id)
            ):
                # Handle interrupted events
                if event.get("type") == "interrupted":
                    was_interrupted = True
                    logger.info("Message was interrupted by user")
                    # Forward event to frontend
                    yield event
                    # Don't return - let cleanup happen below
                    break

                # Handle error events from message service
                if event.get("type") == "error":
                    error_content = event.get("content", "Unknown error occurred")
                    error_type = event.get("error_type", "Error")

                    # Check if this is a corrupted session error
                    if event.get("session_corrupted"):
                        logger.warning(f"Session {session_id} has corrupted external_session_id, clearing it")
                        # Clear the corrupted external_session_id so next message starts fresh
                        def _clear_corrupted_session():
                            with get_fresh_db_session() as db:
                                from app.services.session_service import SessionService
                                chat_session_db = db.get(ChatSession, session_id)
                                if chat_session_db:
                                    SessionService.set_external_session_id(
                                        db=db,
                                        session=chat_session_db,
                                        external_session_id=None
                                    )
                                # Also update session status to "idle"
                                SessionService.update_session_status(
                                    db_session=db,
                                    session_id=session_id,
                                    status="idle"
                                )
                        await asyncio.to_thread(_clear_corrupted_session)
                    else:
                        # Update session status to "idle" (not "error" - we're showing error in chat)
                        def _update_idle_status():
                            with get_fresh_db_session() as db:
                                from app.services.session_service import SessionService
                                SessionService.update_session_status(
                                    db_session=db,
                                    session_id=session_id,
                                    status="idle"
                                )
                        await asyncio.to_thread(_update_idle_status)

                    # Save error as a system message in the chat
                    def _save_error_message():
                        with get_fresh_db_session() as db:
                            MessageService.create_message(
                                session=db,
                                session_id=session_id,
                                role="system",
                                content=f"⚠️ {error_content}",
                                message_metadata={"error_type": error_type},
                                status="error",
                            )
                    await asyncio.to_thread(_save_error_message)

                    # Forward error event and exit
                    yield event
                    return

                # Capture external session ID from first event that has it (not just "done")
                # This allows us to forward pending interrupts early in the stream
                if not new_external_session_id:
                    event_session_id = event.get("session_id") or event.get("metadata", {}).get("session_id")
                    logger.debug(f"🔍 Event type={event.get('type')}, session_id={event.get('session_id')}, metadata.session_id={event.get('metadata', {}).get('session_id')}")
                    if event_session_id:
                        new_external_session_id = event_session_id
                        logger.info(f"✅ External session ID captured early from event type={event.get('type')}: {new_external_session_id}")

                        # Update active streaming manager with external session ID
                        # This returns True if there's a pending interrupt
                        interrupt_pending = await active_streaming_manager.update_external_session_id(
                            session_id=session_id,
                            external_session_id=new_external_session_id
                        )

                        # If interrupt was requested before external_session_id was available, forward it now
                        if interrupt_pending:
                            logger.info(f"Forwarding pending interrupt to agent env for session {session_id}")
                            try:
                                import httpx
                                async with httpx.AsyncClient(timeout=5.0) as client:
                                    response = await client.post(
                                        f"{base_url}/chat/interrupt/{new_external_session_id}",
                                        headers=auth_headers
                                    )
                                    if response.status_code == 200:
                                        logger.info(f"Pending interrupt forwarded successfully")
                                    else:
                                        logger.warning(f"Failed to forward pending interrupt: {response.status_code}")
                            except Exception as e:
                                logger.error(f"Error forwarding pending interrupt: {e}")

                        # Store external session ID (non-blocking)
                        def _store_session_id():
                            with get_fresh_db_session() as db:
                                from app.services.session_service import SessionService
                                chat_session_db = db.get(ChatSession, session_id)
                                if chat_session_db:
                                    SessionService.set_external_session_id(
                                        db=db,
                                        session=chat_session_db,
                                        external_session_id=new_external_session_id
                                        # sdk_type will default to chat_session_db.agent_sdk
                                    )
                        await asyncio.to_thread(_store_session_id)

                # Store raw event for visualization (exclude done/error events from storage)
                if event.get("type") not in ["done", "error", "session_created"]:
                    event_copy = {
                        "type": event.get("type"),
                        "content": event.get("content", ""),
                    }
                    if event.get("tool_name"):
                        event_copy["tool_name"] = event["tool_name"]
                    if event.get("metadata"):
                        event_copy["metadata"] = {
                            k: v for k, v in event["metadata"].items()
                            if k in ["tool_id", "tool_input", "model"]
                        }
                    streaming_events.append(event_copy)

                # Create agent message in DB as soon as we receive the first "assistant" event
                # This ensures the message exists BEFORE any tool calls execute (like handover)
                # so the sequence number is correctly calculated
                if event.get("type") == "assistant" and agent_message_id is None:
                    def _create_initial_agent_message():
                        with get_fresh_db_session() as db:
                            # Create placeholder message with initial content
                            initial_content = event.get("content", "Agent is responding...")
                            initial_metadata = {
                                "external_session_id": new_external_session_id,
                                "mode": session_mode,
                                "agent_sdk": agent_sdk,
                                "streaming_in_progress": True  # Mark as incomplete
                            }
                            message = MessageService.create_message(
                                session=db,
                                session_id=session_id,
                                role="agent",
                                content=initial_content,
                                message_metadata=initial_metadata,
                            )
                            return message.id

                    # Create message in background thread and capture ID
                    agent_message_id = await asyncio.to_thread(_create_initial_agent_message)
                    logger.info(f"Created initial agent message {agent_message_id} on first assistant event")

                # Collect agent response content
                if event.get("content"):
                    agent_response_parts.append(event["content"])

                # Collect metadata from events
                event_metadata = event.get("metadata", {})
                if event_metadata:
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
                yield event

            # After stream completes, save agent response to database (non-blocking)
            if streaming_events:
                # Create summary content from text events only
                text_parts = [e["content"] for e in streaming_events if e["type"] == "assistant" and e.get("content")]
                agent_content = "\n\n".join(text_parts) if text_parts else "Agent response"

                # Store structured events in metadata
                response_metadata["external_session_id"] = new_external_session_id
                response_metadata["streaming_events"] = streaming_events

                # Detect if AskUserQuestion tool was used
                has_questions = MessageService.detect_ask_user_question_tool(streaming_events)
                tool_questions_status = "unanswered" if has_questions else None

                def _save_or_update_agent_message():
                    with get_fresh_db_session() as db:
                        if agent_message_id:
                            # Update existing message that was created on first assistant event
                            agent_message = db.get(SessionMessage, agent_message_id)
                            if agent_message:
                                agent_message.content = agent_content
                                agent_message.message_metadata = response_metadata
                                agent_message.tool_questions_status = tool_questions_status
                                agent_message.status = "user_interrupted" if was_interrupted else ""
                                agent_message.status_message = "Interrupted by user" if was_interrupted else None
                                db.add(agent_message)
                                db.commit()
                                logger.info(f"Updated agent message {agent_message_id} with final content")
                            else:
                                logger.error(f"Agent message {agent_message_id} not found for update, creating new one")
                                # Fallback: create new message if the initial one was lost
                                MessageService.create_message(
                                    session=db,
                                    session_id=session_id,
                                    role="agent",
                                    content=agent_content,
                                    message_metadata=response_metadata,
                                    tool_questions_status=tool_questions_status,
                                    status="user_interrupted" if was_interrupted else "",
                                    status_message="Interrupted by user" if was_interrupted else None
                                )
                        else:
                            # No assistant events received (edge case), create message normally
                            MessageService.create_message(
                                session=db,
                                session_id=session_id,
                                role="agent",
                                content=agent_content,
                                message_metadata=response_metadata,
                                tool_questions_status=tool_questions_status,
                                status="user_interrupted" if was_interrupted else "",
                                status_message="Interrupted by user" if was_interrupted else None
                            )
                await asyncio.to_thread(_save_or_update_agent_message)
                logger.info(f"Agent response finalized ({len(streaming_events)} events, model={response_metadata.get('model')}, has_questions={has_questions}, interrupted={was_interrupted})")

            # Update session status after streaming
            # Keep as "active" when interrupted so user can continue
            # Mark as "completed" only when finished normally
            if was_interrupted:
                def _update_active_status():
                    with get_fresh_db_session() as db:
                        from app.services.session_service import SessionService
                        SessionService.update_session_status(
                            db_session=db,
                            session_id=session_id,
                            status="active"
                        )
                await asyncio.to_thread(_update_active_status)
                logger.info("Session kept as 'active' after interruption")
            else:
                def _update_completed_status():
                    with get_fresh_db_session() as db:
                        from app.services.session_service import SessionService
                        SessionService.update_session_status(
                            db_session=db,
                            session_id=session_id,
                            status="completed"
                        )
                await asyncio.to_thread(_update_completed_status)

                # Emit stream_completed event for event-driven post-processing
                # This allows services (like EnvironmentService) to react to stream completion
                try:
                    from app.models.event import EventType

                    # Get user_id and agent_id for event targeting
                    def _get_session_info():
                        with get_fresh_db_session() as db:
                            session_db = db.get(ChatSession, session_id)
                            if session_db:
                                env_db = db.get(AgentEnvironment, environment_id)
                                return session_db.user_id, env_db.agent_id if env_db else None
                            return None, None

                    user_id, agent_id = await asyncio.to_thread(_get_session_info)

                    await event_service.emit_event(
                        event_type=EventType.STREAM_COMPLETED,
                        model_id=session_id,
                        meta={
                            "session_id": str(session_id),
                            "environment_id": str(environment_id),
                            "agent_id": str(agent_id) if agent_id else None,
                            "session_mode": session_mode,
                            "was_interrupted": was_interrupted
                        },
                        user_id=user_id
                    )
                    logger.info(f"Emitted stream_completed event for session {session_id} (mode: {session_mode})")
                except Exception as event_error:
                    logger.error(f"Failed to emit stream_completed event: {event_error}", exc_info=True)
                    # Don't fail the request, just log the error

            # Send final done event to frontend
            yield {
                "type": "done",
                "content": "",
                "metadata": {
                    **response_metadata,
                    "interrupted": was_interrupted
                }
            }

        except Exception as e:
            logger.error(f"Error in message stream: {e}", exc_info=True)

            # Update session status to "error" when streaming fails (non-blocking)
            def _update_error_status():
                with get_fresh_db_session() as db:
                    from app.services.session_service import SessionService
                    SessionService.update_session_status(
                        db_session=db,
                        session_id=session_id,
                        status="error"
                    )
            await asyncio.to_thread(_update_error_status)

            yield {
                "type": "error",
                "content": str(e),
                "error_type": type(e).__name__
            }
        finally:
            # Clear interaction_status
            def _clear_running_status():
                with get_fresh_db_session() as db:
                    from app.services.session_service import SessionService
                    from app.models import SessionUpdate
                    SessionService.update_session(
                        db_session=db,
                        session_id=session_id,
                        data=SessionUpdate(interaction_status="")
                    )
            await asyncio.to_thread(_clear_running_status)

            # Always unregister stream when done (success, error, or interruption)
            await active_streaming_manager.unregister_stream(session_id)
            logger.info(f"Stream unregistered for session {session_id}")

    @staticmethod
    async def stream_message_with_activity_tracking(
        session_id: UUID,
        environment_id: UUID,
        base_url: str,
        auth_headers: dict,
        user_message_content: str,
        session_mode: str,
        agent_sdk: str,
        external_session_id: str | None,
        get_fresh_db_session: callable
    ) -> AsyncIterator[dict]:
        """
        Stream message with integrated activity tracking for frontend disconnection handling.

        This method orchestrates the entire streaming process with activity management:
        - Creates "session_running" activity at start
        - Streams events to frontend via queue
        - Handles frontend disconnection gracefully
        - Updates activities based on completion status

        Args:
            session_id: Session UUID
            environment_id: Environment UUID
            base_url: Environment base URL
            auth_headers: Environment auth headers
            user_message_content: User's message
            session_mode: "building" or "conversation"
            agent_sdk: SDK to use
            external_session_id: External SDK session ID (None for new)
            get_fresh_db_session: Callable that returns a fresh DB session

        Yields:
            dict: SSE event dictionaries for frontend
        """
        from app.services.activity_service import ActivityService

        # Create queue for event passing (unbounded to avoid blocking)
        event_queue = asyncio.Queue()

        # Flag to track if frontend is still connected
        frontend_connected = True

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
                    get_fresh_db_session=get_fresh_db_session
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
                    def _delete_running_activity():
                        with get_fresh_db_session() as db:
                            asyncio.run(ActivityService.delete_session_running_activity(db, session_id))
                    await asyncio.to_thread(_delete_running_activity)

                    # But if error occurred, still create error activity (errors are important to track)
                    if error_occurred:
                        def _create_error():
                            with get_fresh_db_session() as db:
                                asyncio.run(ActivityService.create_error_activity(db, session_id, error_message))
                        await asyncio.to_thread(_create_error)
                else:
                    logger.info(
                        f"Stream consumer completed for session {session_id} "
                        f"({event_count} events, frontend disconnected earlier, error={error_occurred})"
                    )
                    # Update or replace the "session_running" activity with final result
                    if error_occurred:
                        def _transition_to_error():
                            with get_fresh_db_session() as db:
                                asyncio.run(ActivityService.transition_running_to_error(db, session_id, error_message))
                        await asyncio.to_thread(_transition_to_error)
                    else:
                        def _transition_to_completion():
                            with get_fresh_db_session() as db:
                                asyncio.run(ActivityService.transition_running_to_completion(db, session_id))
                        await asyncio.to_thread(_transition_to_completion)
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
                    def _transition_to_error_on_exception():
                        with get_fresh_db_session() as db:
                            asyncio.run(ActivityService.transition_running_to_error(db, session_id, str(e)))
                    await asyncio.to_thread(_transition_to_error_on_exception)

        # Create "session_running" activity before stream starts
        def _create_running_activity():
            with get_fresh_db_session() as db:
                asyncio.run(ActivityService.create_session_running_activity(db, session_id))
        await asyncio.to_thread(_create_running_activity)

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

    @staticmethod
    async def handle_stream_message(
        session_id: UUID,
        message_content: str,
        answers_to_message_id: UUID | None,
        db_session: Session,
        get_fresh_db_session: callable
    ) -> AsyncIterator[str]:
        """
        Comprehensive handler for streaming message requests.

        This method orchestrates the entire streaming flow:
        1. Validates environment status
        2. Stores user message
        3. Generates session title (if needed)
        4. Sets session status
        5. Streams response with activity tracking

        Args:
            session_id: Session UUID
            message_content: User's message content
            answers_to_message_id: Optional message ID being answered
            db_session: Database session for initial operations
            get_fresh_db_session: Callable that returns fresh DB sessions for background tasks

        Yields:
            str: SSE-formatted event strings for streaming response

        Raises:
            ValueError: If environment is not found or not running
        """
        from app.services.session_service import SessionService

        # Get session
        chat_session = db_session.get(ChatSession, session_id)
        if not chat_session:
            raise ValueError("Session not found")

        # Get environment
        environment = db_session.get(AgentEnvironment, chat_session.environment_id)
        if not environment:
            raise ValueError("Environment not found")

        # Get agent
        agent = db_session.get(Agent, environment.agent_id)
        if not agent:
            raise ValueError("Agent not found")

        # Check if environment is suspended and activate if needed
        if environment.status == "suspended":
            logger.info(f"Environment {environment.id} is suspended, activating...")

            # Import lifecycle manager
            from app.services.environment_lifecycle import EnvironmentLifecycleManager
            lifecycle_manager = EnvironmentLifecycleManager()

            # Activate the environment (this emits activation events)
            await lifecycle_manager.activate_suspended_environment(
                db_session=db_session,
                environment=environment,
                agent=agent,
                emit_events=True
            )

            # Refresh environment status
            db_session.refresh(environment)
            logger.info(f"Environment {environment.id} activated, status: {environment.status}")

        # Validate environment is running
        if environment.status != "running":
            raise ValueError(f"Environment is not running (status: {environment.status})")

        # Get external SDK session ID (if exists)
        external_session_id = SessionService.get_external_session_id(chat_session)

        logger.info(
            f"Streaming message to session {session_id} "
            f"(mode={chat_session.mode}, agent_sdk={chat_session.agent_sdk}, external_session_id={external_session_id})"
        )

        # Store user message
        MessageService.create_message(
            session=db_session,
            session_id=session_id,
            role="user",
            content=message_content,
            answers_to_message_id=answers_to_message_id,
        )

        # Auto-generate session title from first message if no title exists
        if not chat_session.title or chat_session.title.strip() == "":
            # Start background task to generate title (fire and forget)
            asyncio.create_task(
                SessionService.auto_generate_session_title(
                    session_id=session_id,
                    first_message_content=message_content,
                    get_fresh_db_session=get_fresh_db_session
                )
            )

        # Set session status to "active" before streaming starts
        SessionService.update_session_status(
            db_session=db_session,
            session_id=session_id,
            status="active"
        )

        # Extract data from ORM objects BEFORE async generator (to avoid detached instance errors)
        session_mode = chat_session.mode
        agent_sdk = chat_session.agent_sdk
        environment_id = environment.id
        base_url = MessageService.get_environment_url(environment)
        auth_headers = MessageService.get_auth_headers(environment)

        # Stream response using MessageService with activity tracking
        async for event in MessageService.stream_message_with_activity_tracking(
            session_id=session_id,
            environment_id=environment_id,
            base_url=base_url,
            auth_headers=auth_headers,
            user_message_content=message_content,
            session_mode=session_mode,
            agent_sdk=agent_sdk,
            external_session_id=external_session_id,
            get_fresh_db_session=get_fresh_db_session
        ):
            yield event

    @staticmethod
    async def handle_stream_message_websocket(
        session_id: UUID,
        message_content: str,
        answers_to_message_id: UUID | None,
        db_session: Session,
        get_fresh_db_session: callable
    ) -> None:
        """
        Handle message streaming via WebSocket instead of SSE.

        This method:
        - Emits stream start event
        - Creates user message
        - Streams to agent-env (SSE)
        - Emits each event to WebSocket
        - Saves agent response
        - Emits stream completion event

        Args:
            session_id: Session UUID
            message_content: User's message
            answers_to_message_id: Optional ID of message being answered
            db_session: Database session for initial queries
            get_fresh_db_session: Callable for fresh DB sessions
        """
        from app.services.event_service import event_service
        from app.services.session_service import SessionService

        # Emit stream started event
        await event_service.emit_stream_event(
            session_id=session_id,
            event_type="stream_started",
            event_data={
                "status": "started",
                "message": "Processing your message..."
            }
        )

        try:
            # Get session and environment
            chat_session = db_session.get(ChatSession, session_id)
            if not chat_session:
                raise ValueError("Session not found")

            environment = db_session.get(AgentEnvironment, chat_session.environment_id)
            if not environment:
                raise ValueError("Environment not found")

            # Get agent
            agent = db_session.get(Agent, environment.agent_id)
            if not agent:
                raise ValueError("Agent not found")

            # Check if environment is suspended and activate if needed
            if environment.status == "suspended":
                logger.info(f"Environment {environment.id} is suspended, activating...")

                # Import lifecycle manager
                from app.services.environment_lifecycle import EnvironmentLifecycleManager
                lifecycle_manager = EnvironmentLifecycleManager()

                # Activate the environment (this emits activation events)
                await lifecycle_manager.activate_suspended_environment(
                    db_session=db_session,
                    environment=environment,
                    agent=agent,
                    emit_events=True
                )

                # Refresh environment status
                db_session.refresh(environment)
                logger.info(f"Environment {environment.id} activated, status: {environment.status}")

            # Validate environment is running
            if environment.status != "running":
                raise ValueError(f"Environment is not running (status: {environment.status})")

            # Update last_activity_at
            environment.last_activity_at = datetime.utcnow()
            db_session.add(environment)
            db_session.commit()

            # Create user message
            user_message = MessageService.create_message(
                session=db_session,
                session_id=session_id,
                role="user",
                content=message_content,
                answers_to_message_id=answers_to_message_id
            )

            # Emit user message created event (for immediate UI update)
            await event_service.emit_stream_event(
                session_id=session_id,
                event_type="user_message_created",
                event_data={
                    "id": str(user_message.id),
                    "role": "user",
                    "content": message_content,
                    "sequence_number": user_message.sequence_number,
                    "timestamp": user_message.timestamp.isoformat()
                }
            )

            # Auto-generate session title from first message if no title exists
            if not chat_session.title or chat_session.title.strip() == "":
                # Start background task to generate title (fire and forget)
                asyncio.create_task(
                    SessionService.auto_generate_session_title(
                        session_id=session_id,
                        first_message_content=message_content,
                        get_fresh_db_session=get_fresh_db_session
                    )
                )

            # Set session status to "active" before streaming starts
            SessionService.update_session_status(
                db_session=db_session,
                session_id=session_id,
                status="active"
            )

            # Get environment details
            base_url = MessageService.get_environment_url(environment)
            auth_headers = MessageService.get_auth_headers(environment)
            session_mode = chat_session.mode
            agent_sdk = chat_session.agent_sdk
            external_session_id = SessionService.get_external_session_id(chat_session)
            environment_id = environment.id

            # Stream from environment and emit each event via WebSocket
            async for event in MessageService.stream_message_with_events(
                session_id=session_id,
                environment_id=environment_id,
                base_url=base_url,
                auth_headers=auth_headers,
                user_message_content=message_content,
                session_mode=session_mode,
                agent_sdk=agent_sdk,
                external_session_id=external_session_id,
                get_fresh_db_session=get_fresh_db_session
            ):
                # Emit each streaming event via WebSocket
                await event_service.emit_stream_event(
                    session_id=session_id,
                    event_type=event.get("type"),
                    event_data=event
                )

            # Emit stream completed event
            await event_service.emit_stream_event(
                session_id=session_id,
                event_type="stream_completed",
                event_data={
                    "status": "completed",
                    "session_id": str(session_id)
                }
            )

        except Exception as e:
            logger.error(f"Error in WebSocket message streaming: {e}", exc_info=True)

            # Emit error event
            await event_service.emit_stream_event(
                session_id=session_id,
                event_type="error",
                event_data={
                    "type": "error",
                    "content": str(e),
                    "error_type": type(e).__name__
                }
            )
