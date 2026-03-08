from uuid import UUID
from datetime import datetime, UTC
from typing import AsyncIterator, NamedTuple
import json
import time
import httpx
import logging
import asyncio
from sqlmodel import Session, select, func
from app.models import SessionMessage, Session as ChatSession, AgentEnvironment, Agent, SessionUpdate, MessagePublic
from app.services.active_streaming_manager import active_streaming_manager
from app.services.agent_env_connector import agent_env_connector
from app.services.agent_service import AgentService
from app.models.mcp_session_meta import MCPSessionMeta
from app.services.session_context_signer import sign_session_context

logger = logging.getLogger(__name__)

# Pre-allowed tools that never require user approval
# These match the default tools in agent-env's sdk_manager.py
PRE_ALLOWED_TOOLS = frozenset([
    "Read", "Edit", "Glob", "Grep", "Bash", "Write", "WebFetch", "WebSearch", "TodoWrite",
    "Task", "Skill", "AskUserQuestion", "EnterPlanMode", "ExitPlanMode", "NotebookEdit",
    "KillShell", "TaskOutput",
    # Additional built-in tools
    "mcp__knowledge__query_integration_knowledge", "mcp__task__create_agent_task",
    "mcp__task__update_session_state", "mcp__task__respond_to_task"
])

# Metadata keys to forward from streaming events to response_metadata
_FORWARDED_METADATA_KEYS = {"model", "total_cost_usd", "claude_code_version", "duration_ms", "num_turns"}

# Keys to keep when copying event metadata for storage
_STORED_EVENT_METADATA_KEYS = {"tool_id", "tool_input", "model", "needs_approval", "tool_name"}


class MessageServiceError(Exception):
    """Domain exception for message service operations."""
    def __init__(self, message: str, status_code: int = 400):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class StreamContext(NamedTuple):
    """Pre-resolved context for streaming a message to agent environment."""
    user_id: UUID | None
    allowed_tools: set
    previous_result_state: str | None
    session_context: dict | None
    base_url: str
    auth_headers: dict
    env_auth_token: str | None


async def _emit_activity_event(
    event_type,
    session_id: UUID,
    environment_id: UUID,
    session_mode: str,
    user_id: UUID | None,
    **extra_meta,
) -> None:
    """Fire-and-forget activity event with standard error handling."""
    try:
        from app.services.event_service import event_service
        await event_service.emit_event(
            event_type=event_type,
            model_id=session_id,
            meta={
                "session_id": str(session_id),
                "environment_id": str(environment_id),
                "session_mode": session_mode,
                **extra_meta,
            },
            user_id=user_id,
        )
        logger.info(f"Emitted {event_type.value if hasattr(event_type, 'value') else event_type} event for session {session_id}")
    except Exception as e:
        logger.error(f"Failed to emit {event_type} event: {e}", exc_info=True)


def _build_session_context(
    db: Session,
    session_db: ChatSession,
    env: AgentEnvironment | None,
    agent: Agent | None,
) -> dict:
    """Build session_context dict for agent-env, including email/MCP enrichment."""
    context = {
        "integration_type": session_db.integration_type,
        "agent_id": str(env.agent_id) if env and env.agent_id else None,
        "is_clone": agent.is_clone if agent else False,
        "parent_agent_id": str(agent.parent_agent_id) if agent and agent.parent_agent_id else None,
        "sender_email": session_db.sender_email,
        "email_thread_id": session_db.email_thread_id,
        "backend_session_id": str(session_db.id),
    }

    # Fetch email subject from the initiating EmailMessage (avoid data duplication)
    if session_db.integration_type == "email":
        from app.models.email_message import EmailMessage
        initiating_email = db.exec(
            select(EmailMessage)
            .where(EmailMessage.session_id == session_db.id)
            .order_by(EmailMessage.received_at.asc())
            .limit(1)
        ).first()
        if initiating_email:
            context["email_subject"] = initiating_email.subject

    # Fetch authenticated MCP user email (may differ from session owner)
    if session_db.integration_type == "mcp":
        mcp_meta = db.exec(
            select(MCPSessionMeta).where(
                MCPSessionMeta.session_id == session_db.id
            )
        ).first()
        if mcp_meta:
            context["mcp_user_email"] = mcp_meta.authenticated_user_email

    return context


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
            chat_session.last_message_at = datetime.now(UTC)
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

        Raises:
            MessageServiceError: If validation fails or upload errors
        """
        from app.models.file_upload import FileUpload, MessageFile
        from app.services.file_service import FileService
        from sqlmodel import select

        # Validate files exist
        statement = select(FileUpload).where(FileUpload.id.in_(file_ids))
        files = session.exec(statement).all()

        if len(files) != len(file_ids):
            raise MessageServiceError("Some files not found", status_code=400)

        # Check ownership and status
        for file in files:
            if file.user_id != user_id:
                raise MessageServiceError(
                    f"Not authorized for file: {file.filename}",
                    status_code=403,
                )
            if file.status != "temporary":
                raise MessageServiceError(
                    f"File already attached: {file.filename}",
                    status_code=400,
                )

        # Upload files to agent-env
        try:
            agent_file_paths = await FileService.upload_files_to_agent_env(
                session=session,
                file_ids=file_ids,
                environment_id=environment_id,
            )
        except Exception as e:
            raise MessageServiceError(
                f"Failed to upload files to agent environment: {str(e)}",
                status_code=500,
            ) from e

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
    def build_recovery_context(db, session_id: UUID, max_messages: int = 20) -> str:
        """Build history string for session recovery."""
        statement = (
            select(SessionMessage)
            .where(SessionMessage.session_id == session_id)
            .order_by(SessionMessage.sequence_number.desc())
            .limit(max_messages)
        )
        messages = list(reversed(list(db.exec(statement).all())))

        # Filter to user + agent messages only (skip system)
        filtered = [m for m in messages if m.role in ("user", "agent")]
        if not filtered:
            return ""

        lines = []
        for msg in filtered:
            role_label = "User" if msg.role == "user" else "Assistant"
            lines.append(f"{role_label}: {msg.content or ''}")

        return (
            "[SESSION RECOVERY]\n"
            "Previous conversation history:\n"
            + "\n".join(lines)
            + "\n[END SESSION RECOVERY]\n"
            "Please continue the conversation. The user's new message follows:"
        )

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

                # Check for session recovery
                if chat_session.session_metadata.get("recovery_pending"):
                    recovery_context = MessageService.build_recovery_context(db, session_id)
                    if recovery_context:
                        concatenated_content = f"{recovery_context}\n\n{concatenated_content}"
                    # Clear recovery_pending flag
                    chat_session.session_metadata.pop("recovery_pending", None)
                    from sqlalchemy.orm.attributes import flag_modified
                    flag_modified(chat_session, "session_metadata")
                    db.add(chat_session)
                    db.commit()
                    db.refresh(chat_session)

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
                    chat_session.streaming_started_at = None
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
        """Get environment base URL from config."""
        container_name = environment.config.get("container_name", f"agent-{environment.id}")
        port = environment.config.get("port", 8000)
        return f"http://{container_name}:{port}"

    @staticmethod
    def get_auth_headers(environment: AgentEnvironment) -> dict:
        """Get authentication headers for environment API calls."""
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

        Returns:
            dict with status information

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
        backend_session_id: str | None = None,
        session_state: dict | None = None
    ) -> AsyncIterator[dict]:
        """
        Send message to environment and stream response.

        Delegates HTTP/SSE logic to agent_env_connector (injectable for testing).
        """
        payload = {
            "message": user_message,
            "mode": mode,
            "session_id": external_session_id,
            "backend_session_id": backend_session_id,
        }
        if session_state:
            payload["session_state"] = session_state

        async for event in agent_env_connector.stream_chat(
            base_url=base_url,
            auth_headers=auth_headers,
            payload=payload,
        ):
            yield event

    @staticmethod
    async def enrich_messages_with_streaming(
        messages: list[MessagePublic],
        session_id: UUID,
    ) -> list[MessagePublic]:
        """
        Merge in-memory streaming events into message list.

        If the session has an active stream, patches the in-progress message
        with new streaming events and accumulated content from the stream manager.
        """
        if not await active_streaming_manager.is_streaming(session_id):
            return messages

        stream_data = await active_streaming_manager.get_stream_events(session_id)
        if not stream_data or not stream_data["streaming_events"]:
            return messages

        # Find the in-progress message
        in_progress_msg = None
        for msg in messages:
            if msg.message_metadata and msg.message_metadata.get("streaming_in_progress"):
                in_progress_msg = msg
                break

        if in_progress_msg:
            db_events = in_progress_msg.message_metadata.get("streaming_events", [])
            db_max_seq = max((e.get("event_seq", 0) for e in db_events), default=0)
            new_events = [
                e for e in stream_data["streaming_events"]
                if e.get("event_seq", 0) > db_max_seq
            ]
            if new_events:
                in_progress_msg.message_metadata["streaming_events"] = db_events + new_events
            if stream_data["accumulated_content"]:
                in_progress_msg.content = stream_data["accumulated_content"]

        return messages

    @staticmethod
    async def interrupt_stream(
        db_session: Session,
        session_id: UUID,
        environment_id: UUID,
    ) -> dict:
        """
        Interrupt an active streaming message.

        Handles the full flow: request interrupt via active_streaming_manager,
        check for pending state, and forward to agent environment if needed.

        Returns:
            dict with status, message, session_id, and queued flag.

        Raises:
            ValueError: If no active stream to interrupt.
            Exception: If environment not found or communication fails.
        """
        interrupt_info = await active_streaming_manager.request_interrupt(session_id)

        if not interrupt_info["found"]:
            raise ValueError("No active stream to interrupt")

        if interrupt_info["pending"]:
            return {
                "status": "ok",
                "message": "Interrupt queued",
                "session_id": str(session_id),
                "queued": True,
            }

        external_session_id = interrupt_info["external_session_id"]
        environment = db_session.get(AgentEnvironment, environment_id)
        if not environment:
            raise ValueError("Environment not found")

        base_url = MessageService.get_environment_url(environment)
        auth_headers = MessageService.get_auth_headers(environment)

        result = await MessageService.forward_interrupt_to_environment(
            base_url=base_url,
            auth_headers=auth_headers,
            external_session_id=external_session_id,
        )
        return {**result, "session_id": str(session_id), "queued": False}

    @staticmethod
    def build_stream_response(session_id: UUID, result: dict) -> dict:
        """
        Build a standardized response dict from SessionService.send_session_message() result.

        Used by both regular message and webapp chat routes.
        """
        if result["action"] == "command_executed":
            return {
                "status": "ok",
                "session_id": str(session_id),
                "stream_room": f"session_{session_id}_stream",
                "message": "Command executed",
                "command_executed": True,
            }

        response = {
            "status": "ok",
            "session_id": str(session_id),
            "stream_room": f"session_{session_id}_stream",
        }

        if result["action"] == "streaming":
            response["message"] = result["message"]
            response["streaming"] = True
        elif result["action"] == "pending":
            response["message"] = result["message"]
            response["pending"] = True
        else:
            response["message"] = result.get("message", "Message received")

        if result.get("files_attached"):
            response["files_attached"] = result["files_attached"]

        return response

    @staticmethod
    def _resolve_stream_context(
        db: Session,
        session_id: UUID,
        environment_id: UUID,
    ) -> StreamContext:
        """
        Resolve all context needed before starting a stream.

        Loads session, environment, agent; builds session_context;
        resets result_state if needed. Returns a StreamContext NamedTuple.
        """
        session_db = db.get(ChatSession, session_id)
        user_id = session_db.user_id if session_db else None
        allowed_tools = set()
        previous_result_state = None
        session_context = None
        env_base_url = ""
        env_auth_headers = {}
        env_auth_token = None

        if session_db:
            env = db.get(AgentEnvironment, environment_id)
            agent = None
            if env:
                env_base_url = MessageService.get_environment_url(env)
                env_auth_headers = MessageService.get_auth_headers(env)
                env_auth_token = env.config.get("auth_token") if env.config else None

                if env.agent_id:
                    agent = db.get(Agent, env.agent_id)
                    if agent and agent.agent_sdk_config:
                        allowed_tools = set(agent.agent_sdk_config.get("allowed_tools", []))

            session_context = _build_session_context(db, session_db, env, agent)

            # Reset result_state when user sends a new message
            if session_db.result_state is not None:
                previous_result_state = session_db.result_state
                session_db.result_state = None
                session_db.result_summary = None
                db.add(session_db)
                db.commit()
                logger.info(
                    f"Reset result_state '{previous_result_state}' for session {session_id}"
                )

                # Sync task status if session is linked to a task
                if session_db.source_task_id:
                    from app.services.input_task_service import InputTaskService
                    InputTaskService.sync_task_status_from_sessions(
                        db_session=db, task_id=session_db.source_task_id
                    )

        return StreamContext(
            user_id=user_id,
            allowed_tools=allowed_tools,
            previous_result_state=previous_result_state,
            session_context=session_context,
            base_url=env_base_url,
            auth_headers=env_auth_headers,
            env_auth_token=env_auth_token,
        )

    @staticmethod
    async def _handle_session_id_capture(
        session_id: UUID,
        new_external_session_id: str,
        base_url: str,
        auth_headers: dict,
        get_fresh_db_session: callable,
    ) -> None:
        """
        Handle first capture of external_session_id during streaming.

        Updates ActiveStreamingManager, forwards any pending interrupt,
        and persists the ID to the database.
        """
        # Update active streaming manager — returns True if there's a pending interrupt
        interrupt_pending = await active_streaming_manager.update_external_session_id(
            session_id=session_id,
            external_session_id=new_external_session_id,
        )

        # If interrupt was requested before external_session_id was available, forward it now
        if interrupt_pending:
            logger.info(f"Forwarding pending interrupt to agent env for session {session_id}")
            try:
                await MessageService.forward_interrupt_to_environment(
                    base_url=base_url,
                    auth_headers=auth_headers,
                    external_session_id=new_external_session_id,
                )
            except Exception as e:
                logger.error(f"Error forwarding pending interrupt: {e}")

        # Store external session ID in DB
        def _store_session_id():
            with get_fresh_db_session() as db:
                from app.services.session_service import SessionService
                chat_session_db = db.get(ChatSession, session_id)
                if chat_session_db:
                    SessionService.set_external_session_id(
                        db=db,
                        session=chat_session_db,
                        external_session_id=new_external_session_id,
                    )
        await asyncio.to_thread(_store_session_id)

    @staticmethod
    async def _handle_tools_init_event(
        event: dict,
        environment_id: UUID,
        get_fresh_db_session: callable,
    ) -> None:
        """Update agent's sdk_tools from a tools_init system event."""
        tools_list = event.get("data", {}).get("tools", [])
        if not tools_list:
            return

        def _update_sdk_tools():
            with get_fresh_db_session() as db:
                env = db.get(AgentEnvironment, environment_id)
                if env and env.agent_id:
                    try:
                        AgentService.update_sdk_tools(
                            session=db,
                            agent_id=env.agent_id,
                            tools=tools_list,
                        )
                        logger.info(f"Updated sdk_tools for agent {env.agent_id} with {len(tools_list)} tools")
                    except Exception as e:
                        logger.error(f"Failed to update sdk_tools: {e}")
        await asyncio.to_thread(_update_sdk_tools)

    @staticmethod
    async def _handle_tool_event(
        event: dict,
        agent_allowed_tools: set,
        tools_needing_approval: set,
        session_id: UUID,
        user_id: UUID | None,
        get_fresh_db_session: callable,
    ) -> None:
        """
        Process tool events: approval flagging, TodoWrite progress tracking.

        Mutates event dict (adds metadata flags) and tools_needing_approval set.
        """
        from app.models.event import EventType

        tool_name = event["tool_name"]

        # Check if tool needs approval (not in pre-allowed or agent's allowed_tools)
        needs_approval = (
            tool_name not in PRE_ALLOWED_TOOLS and
            tool_name not in agent_allowed_tools
        )
        if needs_approval:
            if "metadata" not in event:
                event["metadata"] = {}
            event["metadata"]["needs_approval"] = True
            event["metadata"]["tool_name"] = tool_name
            tools_needing_approval.add(tool_name)
            logger.info(f"Tool '{tool_name}' flagged as needing approval")

        # Detect TodoWrite tool calls for progress tracking
        if tool_name.lower() == "todowrite":
            tool_input = event.get("metadata", {}).get("tool_input", {})
            todos = tool_input.get("todos", [])
            if todos:
                def _update_todo_progress():
                    with get_fresh_db_session() as db:
                        session_db = db.get(ChatSession, session_id)
                        if session_db:
                            session_db.todo_progress = todos
                            db.add(session_db)
                            db.commit()
                            logger.info(f"Updated todo_progress for session {session_id} with {len(todos)} items")
                await asyncio.to_thread(_update_todo_progress)

                await _emit_activity_event(
                    event_type=EventType.TODO_LIST_UPDATED,
                    session_id=session_id,
                    environment_id=UUID(int=0),  # not relevant for this event
                    session_mode="",
                    user_id=user_id,
                    todos=todos,
                )

    @staticmethod
    async def _flush_streaming_to_db(
        agent_message_id: UUID,
        streaming_events: list[dict],
        response_metadata: dict,
        get_fresh_db_session: callable,
        session_id: UUID,
    ) -> None:
        """Periodic flush of streaming content to DB for crash recovery."""
        flush_events = list(streaming_events)
        flush_content = "\n\n".join(
            e["content"] for e in flush_events
            if e["type"] == "assistant" and e.get("content")
        ) or "Agent is responding..."

        flush_metadata = dict(response_metadata)

        def _flush(
            msg_id=agent_message_id,
            content=flush_content,
            events=flush_events,
            metadata=flush_metadata,
        ):
            from sqlalchemy.orm.attributes import flag_modified
            with get_fresh_db_session() as db:
                agent_msg = db.get(SessionMessage, msg_id)
                if agent_msg:
                    agent_msg.content = content
                    metadata["streaming_in_progress"] = True
                    metadata["streaming_events"] = events
                    agent_msg.message_metadata = metadata
                    flag_modified(agent_msg, "message_metadata")
                    db.add(agent_msg)
                    db.commit()

        await asyncio.to_thread(_flush)
        flush_seq = streaming_events[-1].get("event_seq", 0) if streaming_events else 0
        await active_streaming_manager.update_last_flushed_seq(session_id, flush_seq)
        logger.debug(f"Flushed streaming content to DB (seq={flush_seq}) for session {session_id}")

    @staticmethod
    async def _finalize_agent_message(
        agent_message_id: UUID | None,
        session_id: UUID,
        agent_content: str,
        response_metadata: dict,
        tool_questions_status: str | None,
        was_interrupted: bool,
        get_fresh_db_session: callable,
    ) -> None:
        """Save or update the final agent message after stream completes."""
        def _save_or_update():
            from sqlalchemy.orm.attributes import flag_modified
            response_metadata["streaming_in_progress"] = False
            status = "user_interrupted" if was_interrupted else ""
            status_msg = "Interrupted by user" if was_interrupted else None
            with get_fresh_db_session() as db:
                if agent_message_id:
                    agent_message = db.get(SessionMessage, agent_message_id)
                    if agent_message:
                        agent_message.content = agent_content
                        agent_message.message_metadata = response_metadata
                        flag_modified(agent_message, "message_metadata")
                        agent_message.tool_questions_status = tool_questions_status
                        agent_message.status = status
                        agent_message.status_message = status_msg
                        db.add(agent_message)
                        db.commit()
                        logger.info(f"Updated agent message {agent_message_id} with final content")
                        return
                    else:
                        logger.error(f"Agent message {agent_message_id} not found for update, creating new one")

                # Fallback: create new message
                MessageService.create_message(
                    session=db,
                    session_id=session_id,
                    role="agent",
                    content=agent_content,
                    message_metadata=response_metadata,
                    tool_questions_status=tool_questions_status,
                    status=status,
                    status_message=status_msg,
                )
        await asyncio.to_thread(_save_or_update)

    @staticmethod
    async def stream_message_with_events(
        session_id: UUID,
        environment_id: UUID,
        user_message_content: str,
        session_mode: str,
        external_session_id: str | None,
        get_fresh_db_session: callable
    ) -> AsyncIterator[dict]:
        """
        Stream message to environment and handle all business logic.

        This method:
        - Resolves environment URL and auth headers from environment_id
        - Streams message to environment
        - Handles session ID capture
        - Saves messages to database
        - Updates session status
        - Syncs agent prompts (for building mode)
        - Yields SSE events for frontend
        """
        from app.models.event import EventType

        # Register this as an active stream BEFORE starting
        await active_streaming_manager.register_stream(
            session_id=session_id,
            external_session_id=external_session_id
        )

        # Resolve all context needed for streaming
        def _resolve_context():
            with get_fresh_db_session() as db:
                return MessageService._resolve_stream_context(db, session_id, environment_id)

        ctx = await asyncio.to_thread(_resolve_context)

        # Emit STREAM_STARTED event for activity tracking
        await _emit_activity_event(
            EventType.STREAM_STARTED, session_id, environment_id, session_mode, ctx.user_id
        )

        # Variables to collect agent response
        streaming_events = []
        new_external_session_id = external_session_id
        was_interrupted = False
        agent_message_id = None
        tools_needing_approval = set()
        event_seq_counter = 0
        last_flush_time = time.time()
        FLUSH_INTERVAL = 2.0
        response_metadata = {
            "external_session_id": external_session_id,
            "mode": session_mode,
        }

        # Build session_state for agent-env
        session_state = None
        if ctx.previous_result_state or ctx.session_context:
            session_state = {}
            if ctx.previous_result_state:
                session_state["previous_result_state"] = ctx.previous_result_state
            if ctx.session_context:
                session_state["session_context"] = ctx.session_context
                # HMAC-sign context so agent-env can verify authenticity
                if ctx.env_auth_token:
                    session_state["session_context_signature"] = sign_session_context(
                        ctx.session_context, ctx.env_auth_token
                    )

        try:
            # Stream from environment
            async for event in MessageService.send_message_to_environment_stream(
                base_url=ctx.base_url,
                auth_headers=ctx.auth_headers,
                user_message=user_message_content,
                mode=session_mode,
                external_session_id=external_session_id,
                backend_session_id=str(session_id),
                session_state=session_state
            ):
                # Handle interrupted events
                if event.get("type") == "interrupted":
                    was_interrupted = True
                    logger.info("Message was interrupted by user")
                    await _emit_activity_event(
                        EventType.STREAM_INTERRUPTED, session_id, environment_id,
                        session_mode, ctx.user_id,
                    )
                    yield event
                    break

                # Handle error events from message service
                if event.get("type") == "error":
                    error_content = event.get("content", "Unknown error occurred")
                    error_type = event.get("error_type", "Error")

                    # Check if this is a corrupted session error
                    if event.get("session_corrupted"):
                        logger.warning(f"Session {session_id} has corrupted external_session_id, clearing it")
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

                    await _emit_activity_event(
                        EventType.STREAM_ERROR, session_id, environment_id,
                        session_mode, ctx.user_id,
                        error_type=error_type, error_message=error_content,
                    )

                    yield event
                    return

                # Capture external session ID from first event that has it
                if not new_external_session_id:
                    event_session_id = event.get("session_id") or event.get("metadata", {}).get("session_id")
                    logger.debug(f"Event type={event.get('type')}, session_id={event.get('session_id')}, metadata.session_id={event.get('metadata', {}).get('session_id')}")
                    if event_session_id:
                        new_external_session_id = event_session_id
                        logger.info(f"External session ID captured early from event type={event.get('type')}: {new_external_session_id}")

                        await MessageService._handle_session_id_capture(
                            session_id=session_id,
                            new_external_session_id=new_external_session_id,
                            base_url=ctx.base_url,
                            auth_headers=ctx.auth_headers,
                            get_fresh_db_session=get_fresh_db_session,
                        )

                # Handle tools_init event - update agent's sdk_tools
                if event.get("type") == "system" and event.get("subtype") == "tools_init":
                    await MessageService._handle_tools_init_event(
                        event, environment_id, get_fresh_db_session,
                    )
                    # Don't forward this event to frontend - it's internal
                    continue

                # Check tool events for approval status and TodoWrite progress
                if event.get("type") == "tool" and event.get("tool_name"):
                    await MessageService._handle_tool_event(
                        event=event,
                        agent_allowed_tools=ctx.allowed_tools,
                        tools_needing_approval=tools_needing_approval,
                        session_id=session_id,
                        user_id=ctx.user_id,
                        get_fresh_db_session=get_fresh_db_session,
                    )

                # Store raw event for visualization (exclude done/error events from storage)
                if event.get("type") not in ["done", "error", "session_created"]:
                    event_seq_counter += 1
                    event_copy = {
                        "type": event.get("type"),
                        "content": event.get("content", ""),
                        "event_seq": event_seq_counter,
                    }
                    if event.get("tool_name"):
                        event_copy["tool_name"] = event["tool_name"]
                    if event.get("metadata"):
                        event_copy["metadata"] = {
                            k: v for k, v in event["metadata"].items()
                            if k in _STORED_EVENT_METADATA_KEYS
                        }
                    streaming_events.append(event_copy)

                    # Append to ActiveStreamingManager buffer for API access
                    await active_streaming_manager.append_streaming_event(session_id, event_copy)

                    # Include event_seq in the event forwarded to frontend via WS
                    event["event_seq"] = event_seq_counter

                    # Periodic flush to DB (non-blocking)
                    if agent_message_id and (time.time() - last_flush_time >= FLUSH_INTERVAL):
                        await MessageService._flush_streaming_to_db(
                            agent_message_id, streaming_events, response_metadata,
                            get_fresh_db_session, session_id,
                        )
                        last_flush_time = time.time()

                # Create agent message in DB as soon as we receive the first "assistant" event
                # This ensures the message exists BEFORE any tool calls execute (like handover)
                if event.get("type") == "assistant" and agent_message_id is None:
                    def _create_initial_agent_message():
                        with get_fresh_db_session() as db:
                            initial_content = event.get("content", "Agent is responding...")
                            initial_metadata = {
                                "external_session_id": new_external_session_id,
                                "mode": session_mode,
                                "streaming_in_progress": True
                            }
                            message = MessageService.create_message(
                                session=db,
                                session_id=session_id,
                                role="agent",
                                content=initial_content,
                                message_metadata=initial_metadata,
                            )
                            return message.id

                    agent_message_id = await asyncio.to_thread(_create_initial_agent_message)
                    logger.info(f"Created initial agent message {agent_message_id} on first assistant event")

                # Collect metadata from events
                event_metadata = event.get("metadata", {})
                if event_metadata:
                    response_metadata.update({
                        k: v for k, v in event_metadata.items()
                        if k in _FORWARDED_METADATA_KEYS
                    })

                # Forward event to frontend (except 'done' - we'll send our own after saving message)
                if event.get("type") != "done":
                    yield event

            # After stream completes, save agent response to database
            if streaming_events:
                text_parts = [e["content"] for e in streaming_events if e["type"] == "assistant" and e.get("content")]
                agent_content = "\n\n".join(text_parts) if text_parts else "Agent response"

                response_metadata["external_session_id"] = new_external_session_id
                if tools_needing_approval:
                    response_metadata["tools_needing_approval"] = list(tools_needing_approval)
                    logger.info(f"Message has {len(tools_needing_approval)} tools needing approval: {tools_needing_approval}")
                response_metadata["streaming_events"] = streaming_events

                has_questions = MessageService.detect_ask_user_question_tool(streaming_events)
                tool_questions_status = "unanswered" if has_questions else None

                await MessageService._finalize_agent_message(
                    agent_message_id=agent_message_id,
                    session_id=session_id,
                    agent_content=agent_content,
                    response_metadata=response_metadata,
                    tool_questions_status=tool_questions_status,
                    was_interrupted=was_interrupted,
                    get_fresh_db_session=get_fresh_db_session,
                )
                logger.info(f"Agent response finalized ({len(streaming_events)} events, model={response_metadata.get('model')}, has_questions={has_questions}, interrupted={was_interrupted})")

            # Emit stream_completed event for event-driven post-processing
            if not was_interrupted:
                try:
                    def _get_session_info():
                        with get_fresh_db_session() as db:
                            session_db = db.get(ChatSession, session_id)
                            if session_db:
                                env_db = db.get(AgentEnvironment, environment_id)
                                return session_db.user_id, env_db.agent_id if env_db else None
                            return None, None

                    user_id, agent_id = await asyncio.to_thread(_get_session_info)

                    await _emit_activity_event(
                        EventType.STREAM_COMPLETED, session_id, environment_id,
                        session_mode, user_id,
                        agent_id=str(agent_id) if agent_id else None,
                        was_interrupted=was_interrupted,
                    )
                except Exception as event_error:
                    logger.error(f"Failed to emit stream_completed event: {event_error}", exc_info=True)

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

            await _emit_activity_event(
                EventType.STREAM_ERROR, session_id, environment_id,
                session_mode, ctx.user_id,
                error_type=type(e).__name__, error_message=str(e),
            )

            yield {
                "type": "error",
                "content": str(e),
                "error_type": type(e).__name__
            }
        finally:
            # Always unregister stream when done (success, error, or interruption)
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
