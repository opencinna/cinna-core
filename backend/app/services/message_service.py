from uuid import UUID
from datetime import datetime
from typing import AsyncIterator
import json
import httpx
import logging
import asyncio
from sqlmodel import Session, select, func
from app.models import SessionMessage, Session as ChatSession, AgentEnvironment, Agent, SessionUpdate
from app.services.active_streaming_manager import active_streaming_manager
from app.services.agent_service import AgentService

logger = logging.getLogger(__name__)

# Pre-allowed tools that never require user approval
# These match the default tools in agent-env's sdk_manager.py
PRE_ALLOWED_TOOLS = frozenset([
    "Read", "Edit", "Glob", "Grep", "Bash", "Write", "WebFetch", "WebSearch", "TodoWrite",
    "Task", "Skill", "AskUserQuestion", "EnterPlanMode", "ExitPlanMode", "NotebookEdit",
    "KillShell", "TaskOutput",
    # Additional built-in tools
    "mcp__knowledge__query_integration_knowledge", "mcp__task__create_agent_task"
])


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
        sent_to_agent_status: str = "pending",
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
            sent_to_agent_status=sent_to_agent_status,
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
    async def prepare_user_message_with_files(
        session: Session,
        session_id: UUID,
        message_content: str,
        file_ids: list[UUID],
        environment_id: UUID,
        user_id: UUID,
        answers_to_message_id: UUID | None = None
    ) -> tuple[SessionMessage, str]:
        """
        Prepare user message with file attachments.

        This method:
        1. Validates files (ownership, status)
        2. Uploads files to agent-env
        3. Creates user message with file associations
        4. Updates message_files with agent_env_paths
        5. Marks files as attached

        Args:
            session: Database session
            session_id: Session UUID
            message_content: Original user message content (without file paths)
            file_ids: List of file IDs to attach
            environment_id: Environment UUID for file upload
            user_id: User UUID for ownership validation
            answers_to_message_id: Optional message ID being answered

        Returns:
            tuple: (user_message, message_content_for_agent)
                - user_message: Created SessionMessage with files
                - message_content_for_agent: Message content with file paths prepended

        Raises:
            HTTPException: If validation fails or upload errors
        """
        from app.models.file_upload import FileUpload, MessageFile
        from app.services.file_service import FileService
        from sqlmodel import select
        from fastapi import HTTPException

        # Validate files exist
        statement = select(FileUpload).where(FileUpload.id.in_(file_ids))
        files = session.exec(statement).all()

        if len(files) != len(file_ids):
            raise HTTPException(status_code=400, detail="Some files not found")

        # Check ownership and status
        for file in files:
            if file.user_id != user_id:
                raise HTTPException(
                    status_code=403,
                    detail=f"Not authorized for file: {file.filename}"
                )
            if file.status != "temporary":
                raise HTTPException(
                    status_code=400,
                    detail=f"File already attached: {file.filename}"
                )

        # Upload files to agent-env
        try:
            agent_file_paths = await FileService.upload_files_to_agent_env(
                session=session,
                file_ids=file_ids,
                environment_id=environment_id,
            )
        except HTTPException:
            raise  # Re-raise HTTP exceptions as-is
        except Exception as e:
            raise HTTPException(
                status_code=500,
                detail=f"Failed to upload files to agent environment: {str(e)}"
            )

        # Compose message content with file paths for agent
        file_list = "\n".join(f"- {path}" for path in agent_file_paths.values())
        message_content_for_agent = f"Uploaded files:\n{file_list}\n---\n\n{message_content}"

        # Create user message with file associations
        user_message = MessageService.create_message(
            session=session,
            session_id=session_id,
            role="user",
            content=message_content,  # Store original content without file paths
            answers_to_message_id=answers_to_message_id,
            file_ids=file_ids,
        )

        # Update message_files with agent_env_paths
        statement = select(MessageFile).where(MessageFile.message_id == user_message.id)
        message_files = session.exec(statement).all()

        for message_file in message_files:
            if message_file.file_id in agent_file_paths:
                message_file.agent_env_path = agent_file_paths[message_file.file_id]

        session.commit()

        # Mark files as attached
        FileService.mark_files_as_attached(
            session=session,
            file_ids=list(agent_file_paths.keys()),
        )

        logger.info(f"Prepared user message with {len(file_ids)} files for session {session_id}")

        return user_message, message_content_for_agent

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
                sent_to_agent_status=msg.sent_to_agent_status,
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
    def get_last_message(
        session: Session, session_id: UUID
    ) -> SessionMessage | None:
        """Get the last message in a session by sequence number"""
        statement = (
            select(SessionMessage)
            .where(SessionMessage.session_id == session_id)
            .order_by(SessionMessage.sequence_number.desc())
            .limit(1)
        )
        return session.exec(statement).first()

    @staticmethod
    def collect_pending_messages(session: Session, session_id: UUID) -> tuple[str | None, list[SessionMessage]]:
        """
        Collect all pending user messages (sent_to_agent_status='pending') for a session.

        If messages have attached files, reconstructs the content with file paths prepended.

        Returns:
            tuple: (concatenated_content, list of pending_messages)
                - concatenated_content: All pending messages concatenated with formatting, or None if no pending messages
                - pending_messages: List of SessionMessage objects that are pending
        """
        from app.models.file_upload import MessageFile

        # Get all pending user messages
        statement = (
            select(SessionMessage)
            .where(
                SessionMessage.session_id == session_id,
                SessionMessage.role == "user",
                SessionMessage.sent_to_agent_status == "pending"
            )
            .order_by(SessionMessage.sequence_number)
        )
        pending_messages = list(session.exec(statement).all())

        if not pending_messages:
            return None, []

        # Helper function to reconstruct message content with files
        def get_message_content_with_files(message: SessionMessage) -> str:
            # Check if message has attached files
            file_statement = select(MessageFile).where(MessageFile.message_id == message.id)
            message_files = list(session.exec(file_statement).all())

            if not message_files:
                # No files, return original content
                return message.content

            # Reconstruct content with file paths (same format as prepare_user_message_with_files)
            file_paths = [mf.agent_env_path for mf in message_files if mf.agent_env_path]

            if not file_paths:
                # Files exist but no agent_env_path, return original content
                return message.content

            file_list = "\n".join(f"- {path}" for path in file_paths)
            return f"Uploaded files:\n{file_list}\n---\n\n{message.content}"

        # Concatenate messages with formatting
        if len(pending_messages) == 1:
            # Single message - no need for formatting
            concatenated_content = get_message_content_with_files(pending_messages[0])
        else:
            # Multiple messages - format with separators
            message_contents = []
            for i, msg in enumerate(pending_messages):
                msg_content = get_message_content_with_files(msg)
                message_contents.append(f"[Message {i+1}]:\n{msg_content}")
            concatenated_content = "\n\n".join(message_contents)

        logger.info(f"Collected {len(pending_messages)} pending messages for session {session_id}")
        return concatenated_content, pending_messages

    @staticmethod
    def mark_messages_as_sent(session: Session, message_ids: list[UUID]) -> None:
        """
        Mark messages as sent to agent-env.

        Args:
            session: Database session
            message_ids: List of message IDs to mark as sent
        """
        for message_id in message_ids:
            message = session.get(SessionMessage, message_id)
            if message and message.role == "user":
                message.sent_to_agent_status = "sent"
                session.add(message)

        session.commit()
        logger.info(f"Marked {len(message_ids)} messages as sent to agent")

    @staticmethod
    async def process_pending_messages(session_id: UUID, get_fresh_db_session: callable) -> None:
        """
        Process all pending messages for a session by streaming them to the agent.

        This is the core method that:
        1. Collects pending messages
        2. Gets environment and agent details
        3. Emits stream_started event
        4. Calls streaming logic
        5. Marks messages as sent after successful stream
        6. Updates session state

        Args:
            session_id: Session UUID
            get_fresh_db_session: Callable that returns a fresh DB session (context manager)
        """
        logger.info(f"process_pending_messages called for session {session_id}")

        try:
            from app.services.event_service import event_service

            # Get session info, pending messages, and prepare for streaming
            with get_fresh_db_session() as db:
                # Get session
                chat_session = db.get(ChatSession, session_id)
                if not chat_session:
                    logger.error(f"Session {session_id} not found")
                    return

                # Collect pending messages
                concatenated_content, pending_messages = MessageService.collect_pending_messages(db, session_id)

                if not concatenated_content or not pending_messages:
                    logger.info(f"No pending messages found for session {session_id}")
                    # Still reset session state
                    chat_session.pending_messages_count = 0
                    chat_session.interaction_status = ""
                    db.add(chat_session)
                    db.commit()
                    return

                # Get environment and agent
                environment = db.get(AgentEnvironment, chat_session.environment_id)
                if not environment:
                    logger.error(f"Environment {chat_session.environment_id} not found")
                    return

                agent = db.get(Agent, environment.agent_id)
                if not agent:
                    logger.error(f"Agent {environment.agent_id} not found")
                    return

                # Prepare streaming parameters
                base_url = MessageService.get_environment_url(environment)
                auth_headers = MessageService.get_auth_headers(environment)
                external_session_id = chat_session.session_metadata.get("external_session_id")
                session_mode = chat_session.mode or "conversation"
                environment_id = environment.id

                # Store message IDs for marking as sent later
                message_ids = [msg.id for msg in pending_messages]

            # Emit stream_started event to frontend
            await event_service.emit_stream_event(
                session_id=session_id,
                event_type="stream_started",
                event_data={
                    "message": f"Processing {len(pending_messages)} pending message(s)...",
                    "pending_count": len(pending_messages)
                }
            )

            logger.info(f"Starting stream for session {session_id} with {len(pending_messages)} pending message(s)")

            # Stream the concatenated messages and emit each event via WebSocket
            async for event in MessageService.stream_message_with_events(
                session_id=session_id,
                environment_id=environment_id,
                base_url=base_url,
                auth_headers=auth_headers,
                user_message_content=concatenated_content,
                session_mode=session_mode,
                external_session_id=external_session_id,
                get_fresh_db_session=get_fresh_db_session
            ):
                # Emit each streaming event via WebSocket to frontend
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

            # After successful stream, mark messages as sent and update session
            with get_fresh_db_session() as db:
                MessageService.mark_messages_as_sent(db, message_ids)

                # Update session state
                chat_session = db.get(ChatSession, session_id)
                if chat_session:
                    chat_session.pending_messages_count = 0
                    chat_session.interaction_status = ""
                    db.add(chat_session)
                    db.commit()

            logger.info(f"Successfully processed {len(message_ids)} pending messages for session {session_id}")

        except Exception as e:
            logger.error(f"Error in process_pending_messages for session {session_id}: {e}", exc_info=True)
            # Emit error event
            try:
                await event_service.emit_stream_event(
                    session_id=session_id,
                    event_type="error",
                    event_data={
                        "type": "error",
                        "content": str(e),
                        "error_type": type(e).__name__
                    }
                )
            except Exception as emit_error:
                logger.error(f"Failed to emit error event: {emit_error}", exc_info=True)
            raise

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
            external_session_id: Optional external SDK session ID for resumption

        Yields:
            dict: SSE event chunks from environment
        """
        headers = {**auth_headers, "Content-Type": "application/json"}

        # Prepare request payload
        payload = {
            "message": user_message,
            "mode": mode,
            "session_id": external_session_id,
            "backend_session_id": backend_session_id,
        }

        logger.info(
            f"Sending message to {base_url}/chat/stream "
            f"(mode={mode}, external_session_id={external_session_id})"
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
            # For streaming responses, we need to read the content first
            try:
                await e.response.aread()
                error_text = e.response.text
            except Exception:
                error_text = "(unable to read response)"
            logger.error(f"HTTP error from environment: {e.response.status_code} - {error_text}")
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

        # Get user_id and agent's allowed_tools for event emission and tool approval checking
        def _get_session_context():
            with get_fresh_db_session() as db:
                session_db = db.get(ChatSession, session_id)
                user_id = session_db.user_id if session_db else None
                allowed_tools = set()
                if session_db:
                    env = db.get(AgentEnvironment, environment_id)
                    if env and env.agent_id:
                        agent = db.get(Agent, env.agent_id)
                        if agent and agent.agent_sdk_config:
                            allowed_tools = set(agent.agent_sdk_config.get("allowed_tools", []))
                return user_id, allowed_tools
        user_id, agent_allowed_tools = await asyncio.to_thread(_get_session_context)

        # Emit STREAM_STARTED event for activity tracking
        try:
            from app.services.event_service import event_service
            from app.models.event import EventType
            await event_service.emit_event(
                event_type=EventType.STREAM_STARTED,
                model_id=session_id,
                meta={
                    "session_id": str(session_id),
                    "environment_id": str(environment_id),
                    "session_mode": session_mode,
                },
                user_id=user_id
            )
            logger.info(f"Emitted STREAM_STARTED event for session {session_id}")
        except Exception as e:
            logger.error(f"Failed to emit STREAM_STARTED event: {e}", exc_info=True)

        # Variables to collect agent response
        agent_response_parts = []
        streaming_events = []  # Store raw streaming events for visualization
        new_external_session_id = external_session_id
        was_interrupted = False  # Track if message was interrupted
        agent_message_id = None  # Track agent message ID for early creation
        tools_needing_approval = set()  # Track all tools that need approval in this message
        response_metadata = {
            "external_session_id": external_session_id,
            "mode": session_mode,
        }

        try:
            # Stream from environment
            async for event in MessageService.send_message_to_environment_stream(
                base_url=base_url,
                auth_headers=auth_headers,
                user_message=user_message_content,
                mode=session_mode,
                external_session_id=external_session_id,
                backend_session_id=str(session_id)
            ):
                # Handle interrupted events
                if event.get("type") == "interrupted":
                    was_interrupted = True
                    logger.info("Message was interrupted by user")

                    # Emit STREAM_INTERRUPTED event for activity tracking
                    try:
                        from app.services.event_service import event_service as evt_service
                        from app.models.event import EventType
                        await evt_service.emit_event(
                            event_type=EventType.STREAM_INTERRUPTED,
                            model_id=session_id,
                            meta={
                                "session_id": str(session_id),
                                "environment_id": str(environment_id),
                                "session_mode": session_mode
                            },
                            user_id=user_id
                        )
                        logger.info(f"Emitted STREAM_INTERRUPTED event for session {session_id}")
                    except Exception as e:
                        logger.error(f"Failed to emit STREAM_INTERRUPTED event: {e}", exc_info=True)

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
                        await asyncio.to_thread(_clear_corrupted_session)

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

                    # Emit STREAM_ERROR event for activity tracking
                    try:
                        from app.services.event_service import event_service as evt_service
                        from app.models.event import EventType
                        await evt_service.emit_event(
                            event_type=EventType.STREAM_ERROR,
                            model_id=session_id,
                            meta={
                                "session_id": str(session_id),
                                "environment_id": str(environment_id),
                                "error_type": error_type,
                                "error_message": error_content,
                                "session_mode": session_mode
                            },
                            user_id=user_id
                        )
                        logger.info(f"Emitted STREAM_ERROR event for session {session_id}")
                    except Exception as e:
                        logger.error(f"Failed to emit STREAM_ERROR event: {e}", exc_info=True)

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
                                        # sdk_type is optional
                                    )
                        await asyncio.to_thread(_store_session_id)

                # Handle tools_init event - update agent's sdk_tools
                if event.get("type") == "system" and event.get("subtype") == "tools_init":
                    tools_list = event.get("data", {}).get("tools", [])
                    if tools_list:
                        def _update_sdk_tools():
                            with get_fresh_db_session() as db:
                                # Get agent_id from environment
                                env = db.get(AgentEnvironment, environment_id)
                                if env and env.agent_id:
                                    try:
                                        AgentService.update_sdk_tools(
                                            session=db,
                                            agent_id=env.agent_id,
                                            tools=tools_list
                                        )
                                        logger.info(f"Updated sdk_tools for agent {env.agent_id} with {len(tools_list)} tools")
                                    except Exception as e:
                                        logger.error(f"Failed to update sdk_tools: {e}")
                        await asyncio.to_thread(_update_sdk_tools)
                    # Don't forward this event to frontend - it's internal
                    continue

                # Check tool events for approval status
                if event.get("type") == "tool" and event.get("tool_name"):
                    tool_name = event["tool_name"]
                    # Check if tool needs approval (not in pre-allowed or agent's allowed_tools)
                    needs_approval = (
                        tool_name not in PRE_ALLOWED_TOOLS and
                        tool_name not in agent_allowed_tools
                    )
                    if needs_approval:
                        # Add metadata to flag this tool needs approval
                        if "metadata" not in event:
                            event["metadata"] = {}
                        event["metadata"]["needs_approval"] = True
                        event["metadata"]["tool_name"] = tool_name
                        # Track for summary in final message metadata
                        tools_needing_approval.add(tool_name)
                        logger.info(f"Tool '{tool_name}' flagged as needing approval")

                    # Detect TodoWrite tool calls for progress tracking
                    if tool_name.lower() == "todowrite":
                        tool_input = event.get("metadata", {}).get("tool_input", {})
                        todos = tool_input.get("todos", [])
                        if todos:
                            # Update session's todo_progress and emit event
                            def _update_todo_progress():
                                with get_fresh_db_session() as db:
                                    session_db = db.get(ChatSession, session_id)
                                    if session_db:
                                        session_db.todo_progress = todos
                                        db.add(session_db)
                                        db.commit()
                                        logger.info(f"Updated todo_progress for session {session_id} with {len(todos)} items")
                            await asyncio.to_thread(_update_todo_progress)

                            # Emit TODO_LIST_UPDATED event
                            try:
                                await event_service.emit_event(
                                    event_type=EventType.TODO_LIST_UPDATED,
                                    model_id=session_id,
                                    user_id=user_id,
                                    meta={
                                        "session_id": str(session_id),
                                        "todos": todos
                                    }
                                )
                                logger.info(f"Emitted TODO_LIST_UPDATED event for session {session_id}")
                            except Exception as e:
                                logger.error(f"Failed to emit TODO_LIST_UPDATED event: {e}", exc_info=True)

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
                            if k in ["tool_id", "tool_input", "model", "needs_approval", "tool_name"]
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

                # Add tools needing approval summary if any
                if tools_needing_approval:
                    response_metadata["tools_needing_approval"] = list(tools_needing_approval)
                    logger.info(f"Message has {len(tools_needing_approval)} tools needing approval: {tools_needing_approval}")
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

            # Emit stream_completed event for event-driven post-processing
            # This allows services (like EnvironmentService and SessionService) to react to stream completion
            # SessionService will update session status based on was_interrupted flag
            if not was_interrupted:
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

            # Emit STREAM_ERROR event for activity tracking
            # SessionService will update session status to "error" via event handler
            try:
                from app.services.event_service import event_service as evt_service
                from app.models.event import EventType
                await evt_service.emit_event(
                    event_type=EventType.STREAM_ERROR,
                    model_id=session_id,
                    meta={
                        "session_id": str(session_id),
                        "environment_id": str(environment_id),
                        "error_type": type(e).__name__,
                        "error_message": str(e),
                        "session_mode": session_mode
                    },
                    user_id=user_id
                )
                logger.info(f"Emitted STREAM_ERROR event for session {session_id}")
            except Exception as evt_error:
                logger.error(f"Failed to emit STREAM_ERROR event: {evt_error}", exc_info=True)

            yield {
                "type": "error",
                "content": str(e),
                "error_type": type(e).__name__
            }
        finally:
            # Always unregister stream when done (success, error, or interruption)
            # Note: SessionService handles interaction_status updates via event handlers
            await active_streaming_manager.unregister_stream(session_id)
            logger.info(f"Stream unregistered for session {session_id}")

    @staticmethod
    async def create_user_message_and_emit_event(
        db_session: Session,
        session_id: UUID,
        message_content: str,
        answers_to_message_id: UUID | None
    ) -> SessionMessage:
        """
        Create user message and emit event to frontend.

        This is a helper method to avoid code duplication.

        Args:
            db_session: Database session
            session_id: Session UUID
            message_content: User message content
            answers_to_message_id: Optional message ID being answered

        Returns:
            Created SessionMessage
        """
        from app.services.event_service import event_service

        # Create user message
        user_message = MessageService.create_message(
            session=db_session,
            session_id=session_id,
            role="user",
            content=message_content,
            answers_to_message_id=answers_to_message_id
        )

        # Emit user message created event
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

        return user_message
