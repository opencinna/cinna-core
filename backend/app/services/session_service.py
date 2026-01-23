from uuid import UUID
from datetime import datetime
import logging
import asyncio
from typing import Any
from sqlmodel import Session as DBSession, select
from sqlalchemy.orm.attributes import flag_modified
from app.models import Session, SessionCreate, SessionUpdate, Agent, AgentEnvironment
from app.core.db import engine
from app.utils import create_task_with_error_logging

logger = logging.getLogger(__name__)


class SessionService:
    @staticmethod
    def create_session(
        db_session: DBSession,
        user_id: UUID,
        data: SessionCreate,
        access_token_id: UUID | None = None,
        source_task_id: UUID | None = None,
    ) -> Session | None:
        """
        Create session using agent's active environment.

        Args:
            db_session: Database session
            user_id: User ID creating the session
            data: Session creation data
            access_token_id: Optional access token ID (for A2A token-created sessions)
            source_task_id: Optional task ID that spawned this session (for task management)
        """
        # Get agent to find active environment
        agent = db_session.get(Agent, data.agent_id)
        if not agent or not agent.active_environment_id:
            return None

        session = Session(
            environment_id=agent.active_environment_id,
            user_id=user_id,
            user_workspace_id=agent.user_workspace_id,
            access_token_id=access_token_id,
            source_task_id=source_task_id,
            title=data.title,
            mode=data.mode,
        )
        db_session.add(session)
        db_session.commit()
        db_session.refresh(session)
        return session

    @staticmethod
    def get_session(db_session: DBSession, session_id: UUID) -> Session | None:
        """Get session by ID"""
        return db_session.get(Session, session_id)

    @staticmethod
    def update_session(
        db_session: DBSession, session_id: UUID, data: SessionUpdate
    ) -> Session | None:
        """Update session (title, status, mode)"""
        session = db_session.get(Session, session_id)
        if not session:
            return None

        update_dict = data.model_dump(exclude_unset=True)
        session.sqlmodel_update(update_dict)
        session.updated_at = datetime.utcnow()

        db_session.add(session)
        db_session.commit()
        db_session.refresh(session)
        return session

    @staticmethod
    def update_session_status(
        db_session: DBSession, session_id: UUID, status: str
    ) -> Session | None:
        """
        Update session status.

        Valid statuses:
        - "active": Streaming is currently happening
        - "completed": All messages received successfully, nothing to process
        - "error": Server-side error occurred (SDK response failed, HTTP errors, etc.)
        - "paused": Not implemented yet
        """
        session = db_session.get(Session, session_id)
        if not session:
            return None

        session.status = status
        session.updated_at = datetime.utcnow()

        db_session.add(session)
        db_session.commit()
        db_session.refresh(session)
        return session

    @staticmethod
    def update_interaction_status(
        db_session: DBSession,
        session_id: UUID,
        interaction_status: str,
        pending_messages_count: int | None = None
    ) -> Session | None:
        """
        Update session interaction status and optionally pending messages count.

        Valid interaction_status values:
        - "": No interaction in progress
        - "running": Streaming is currently happening
        - "pending_stream": Messages waiting to be processed

        Args:
            db_session: Database session
            session_id: Session UUID
            interaction_status: New interaction status
            pending_messages_count: Optional pending messages count (if None, not updated)
        """
        session = db_session.get(Session, session_id)
        if not session:
            return None

        session.interaction_status = interaction_status
        if pending_messages_count is not None:
            session.pending_messages_count = pending_messages_count
        session.updated_at = datetime.utcnow()

        db_session.add(session)
        db_session.commit()
        db_session.refresh(session)
        return session

    @staticmethod
    def switch_mode(db_session: DBSession, session_id: UUID, new_mode: str) -> Session | None:
        """Switch session mode (building <-> conversation)"""
        session = db_session.get(Session, session_id)
        if not session:
            return None

        session.mode = new_mode
        session.updated_at = datetime.utcnow()

        db_session.add(session)
        db_session.commit()
        db_session.refresh(session)
        return session

    @staticmethod
    def list_user_sessions(db_session: DBSession, user_id: UUID) -> list[Session]:
        """List all sessions for user"""
        statement = select(Session).where(Session.user_id == user_id)
        return list(db_session.exec(statement).all())

    @staticmethod
    def list_agent_sessions(db_session: DBSession, agent_id: UUID) -> list[Session]:
        """List all sessions for agent (across all environments)"""
        # Get all environments for this agent
        env_statement = select(AgentEnvironment).where(AgentEnvironment.agent_id == agent_id)
        environments = db_session.exec(env_statement).all()
        env_ids = [env.id for env in environments]

        if not env_ids:
            return []

        # Get all sessions for these environments
        statement = select(Session).where(Session.environment_id.in_(env_ids))
        return list(db_session.exec(statement).all())

    @staticmethod
    def list_environment_sessions(
        db_session: DBSession,
        environment_id: UUID,
        limit: int = 20,
        offset: int = 0,
        access_token_id: UUID | None = None,
    ) -> list[Session]:
        """
        List sessions for an environment with pagination and optional filtering.

        Args:
            db_session: Database session
            environment_id: Environment UUID to filter by
            limit: Max number of sessions to return
            offset: Offset for pagination
            access_token_id: If provided, only return sessions created by this access token

        Returns:
            List of Session objects ordered by created_at descending
        """
        from sqlmodel import desc

        query = (
            select(Session)
            .where(Session.environment_id == environment_id)
            .order_by(desc(Session.created_at))
            .offset(offset)
            .limit(limit)
        )

        # Filter by access token if provided
        if access_token_id is not None:
            query = query.where(Session.access_token_id == access_token_id)

        return list(db_session.exec(query).all())

    @staticmethod
    def list_task_sessions(
        db_session: DBSession,
        task_id: UUID,
        limit: int = 20,
        offset: int = 0,
    ) -> list[Session]:
        """
        List sessions spawned by a specific task.

        Args:
            db_session: Database session
            task_id: Task UUID to filter by
            limit: Max number of sessions to return
            offset: Offset for pagination

        Returns:
            List of Session objects ordered by created_at descending
        """
        from sqlmodel import desc

        query = (
            select(Session)
            .where(Session.source_task_id == task_id)
            .order_by(desc(Session.created_at))
            .offset(offset)
            .limit(limit)
        )

        return list(db_session.exec(query).all())

    @staticmethod
    def get_task_sessions_info(
        db_session: DBSession,
        task_id: UUID,
    ) -> tuple[int, UUID | None]:
        """
        Get sessions count and latest session ID for a task.

        Args:
            db_session: Database session
            task_id: Task UUID to filter by

        Returns:
            Tuple of (count, latest_session_id)
        """
        from sqlmodel import desc, func

        # Get count
        count_query = (
            select(func.count(Session.id))
            .where(Session.source_task_id == task_id)
        )
        count = db_session.exec(count_query).one() or 0

        # Get latest session ID
        latest_query = (
            select(Session.id)
            .where(Session.source_task_id == task_id)
            .order_by(desc(Session.created_at))
            .limit(1)
        )
        latest_session_id = db_session.exec(latest_query).first()

        return count, latest_session_id

    @staticmethod
    def delete_session(db_session: DBSession, session_id: UUID) -> bool:
        """Delete session"""
        session = db_session.get(Session, session_id)
        if not session:
            return False

        db_session.delete(session)
        db_session.commit()
        return True

    # External SDK session management methods

    @staticmethod
    def get_external_session_id(session: Session) -> str | None:
        """Get external SDK session ID from metadata"""
        return session.session_metadata.get("external_session_id")

    @staticmethod
    def set_external_session_id(
        db: DBSession,
        session: Session,
        external_session_id: str | None,
        sdk_type: str | None = None
    ) -> Session:
        """
        Set external SDK session ID in metadata.
        Called after first message to SDK to store the session ID for resumption.

        Args:
            db: Database session
            session: Session to update
            external_session_id: External SDK session ID (None to clear)
            sdk_type: SDK type (optional)
        """
        if external_session_id is not None:
            session.session_metadata["external_session_id"] = external_session_id
            if sdk_type:
                session.session_metadata["sdk_type"] = sdk_type
        else:
            # Clear external session ID
            session.session_metadata.pop("external_session_id", None)
            session.session_metadata.pop("sdk_type", None)

        # Mark metadata as modified for SQLAlchemy
        flag_modified(session, "session_metadata")

        session.updated_at = datetime.utcnow()
        db.add(session)
        db.commit()
        db.refresh(session)

        return session

    @staticmethod
    def get_sdk_type(session: Session) -> str | None:
        """Get SDK type from metadata"""
        return session.session_metadata.get("sdk_type")

    @staticmethod
    def should_create_new_sdk_session(session: Session) -> bool:
        """
        Determine if we should create a new SDK session or resume existing.

        Returns True if:
        - No external session ID exists
        - Session mode was just switched (different SDK type)
        """
        return not session.session_metadata.get("external_session_id")

    @staticmethod
    def clear_external_session(db: DBSession, session: Session) -> Session:
        """
        Clear external session metadata.
        Used when switching modes or restarting a session.
        """
        session.session_metadata.pop("external_session_id", None)
        session.session_metadata.pop("sdk_type", None)
        session.session_metadata.pop("last_sdk_message_id", None)

        flag_modified(session, "session_metadata")

        session.updated_at = datetime.utcnow()
        db.add(session)
        db.commit()
        db.refresh(session)

        return session

    @staticmethod
    def update_session_mode(
        db: DBSession,
        session: Session,
        new_mode: str,
        clear_external_session: bool = False
    ) -> Session:
        """
        Update session mode.

        Args:
            db: Database session
            session: Session to update
            new_mode: New mode ("building" | "conversation")
            clear_external_session: If True, clear external session ID
                                    (useful when switching between SDKs)
        """
        old_mode = session.mode
        session.mode = new_mode
        session.updated_at = datetime.utcnow()

        # If switching modes and flag is set, clear external session
        if clear_external_session and old_mode != new_mode:
            session.session_metadata.pop("external_session_id", None)
            session.session_metadata.pop("sdk_type", None)
            session.session_metadata.pop("last_sdk_message_id", None)

            flag_modified(session, "session_metadata")

        db.add(session)
        db.commit()
        db.refresh(session)

        return session

    # Event handlers for event bus integration
    # These handlers react to streaming events and update session status accordingly

    @staticmethod
    async def handle_stream_started(event_data: dict[str, Any]) -> None:
        """
        React to STREAM_STARTED events and update session status.

        When streaming starts:
        - Set interaction_status to "running"
        - Set status to "active"
        - Set streaming_started_at to now
        - Emit session_interaction_status_changed WS event

        Args:
            event_data: Full event dict with type, model_id, meta, user_id, timestamp
        """
        try:
            meta = event_data.get("meta", {})
            session_id = meta.get("session_id")

            if not session_id:
                logger.error(f"Invalid STREAM_STARTED event: missing session_id: {meta}")
                return

            # Use fresh database session to avoid conflicts
            with DBSession(engine) as db:
                session = db.get(Session, UUID(session_id))
                if not session:
                    logger.warning(f"Session {session_id} not found for STREAM_STARTED event")
                    return

                now = datetime.utcnow()
                session.interaction_status = "running"
                session.status = "active"
                session.streaming_started_at = now
                session.updated_at = now
                user_id = session.user_id

                db.add(session)
                db.commit()

                logger.info(f"Session {session_id} status updated to 'active' with interaction_status 'running' (STREAM_STARTED)")

            # Emit session_interaction_status_changed WS event to user room
            try:
                from app.services.event_service import event_service
                await event_service.emit_event(
                    event_type="session_interaction_status_changed",
                    model_id=UUID(session_id),
                    meta={
                        "session_id": session_id,
                        "interaction_status": "running",
                        "streaming_started_at": now.isoformat(),
                    },
                    user_id=user_id,
                )
            except Exception as ws_err:
                logger.error(f"Failed to emit session_interaction_status_changed: {ws_err}", exc_info=True)

        except Exception as e:
            logger.error(f"Error handling STREAM_STARTED event: {e}", exc_info=True)

    @staticmethod
    async def handle_stream_completed(event_data: dict[str, Any]) -> None:
        """
        React to STREAM_COMPLETED events and update session status.

        When streaming completes:
        - Clear interaction_status (set to empty string)
        - Clear streaming_started_at
        - Set status based on interruption:
          - If interrupted: "active" (user can continue)
          - If not interrupted: "completed"
        - Emit session_interaction_status_changed WS event

        Args:
            event_data: Full event dict with type, model_id, meta, user_id, timestamp
        """
        try:
            meta = event_data.get("meta", {})
            session_id = meta.get("session_id")
            was_interrupted = meta.get("was_interrupted", False)

            if not session_id:
                logger.error(f"Invalid STREAM_COMPLETED event: missing session_id: {meta}")
                return

            # Use fresh database session to avoid conflicts
            with DBSession(engine) as db:
                session = db.get(Session, UUID(session_id))
                if not session:
                    logger.warning(f"Session {session_id} not found for STREAM_COMPLETED event")
                    return

                session.interaction_status = ""
                session.streaming_started_at = None
                session.status = "active" if was_interrupted else "completed"
                session.updated_at = datetime.utcnow()
                user_id = session.user_id

                db.add(session)
                db.commit()

                status_msg = f"'active' (interrupted)" if was_interrupted else "'completed'"
                logger.info(f"Session {session_id} status updated to {status_msg} with interaction_status cleared (STREAM_COMPLETED)")

            # Emit session_interaction_status_changed WS event to user room
            try:
                from app.services.event_service import event_service
                await event_service.emit_event(
                    event_type="session_interaction_status_changed",
                    model_id=UUID(session_id),
                    meta={
                        "session_id": session_id,
                        "interaction_status": "",
                    },
                    user_id=user_id,
                )
            except Exception as ws_err:
                logger.error(f"Failed to emit session_interaction_status_changed: {ws_err}", exc_info=True)

        except Exception as e:
            logger.error(f"Error handling STREAM_COMPLETED event: {e}", exc_info=True)

    @staticmethod
    async def handle_stream_error(event_data: dict[str, Any]) -> None:
        """
        React to STREAM_ERROR events and update session status.

        When streaming encounters an error:
        - Clear interaction_status
        - Clear streaming_started_at
        - Set status to "error"
        - Emit session_interaction_status_changed WS event

        Args:
            event_data: Full event dict with type, model_id, meta, user_id, timestamp
        """
        try:
            meta = event_data.get("meta", {})
            session_id = meta.get("session_id")
            error_type = meta.get("error_type", "Unknown")

            if not session_id:
                logger.error(f"Invalid STREAM_ERROR event: missing session_id: {meta}")
                return

            # Use fresh database session to avoid conflicts
            with DBSession(engine) as db:
                session = db.get(Session, UUID(session_id))
                if not session:
                    logger.warning(f"Session {session_id} not found for STREAM_ERROR event")
                    return

                session.interaction_status = ""
                session.streaming_started_at = None
                session.status = "error"
                session.updated_at = datetime.utcnow()
                user_id = session.user_id

                db.add(session)
                db.commit()

                logger.info(f"Session {session_id} status updated to 'error' (STREAM_ERROR: {error_type})")

            # Emit session_interaction_status_changed WS event to user room
            try:
                from app.services.event_service import event_service
                await event_service.emit_event(
                    event_type="session_interaction_status_changed",
                    model_id=UUID(session_id),
                    meta={
                        "session_id": session_id,
                        "interaction_status": "",
                    },
                    user_id=user_id,
                )
            except Exception as ws_err:
                logger.error(f"Failed to emit session_interaction_status_changed: {ws_err}", exc_info=True)

        except Exception as e:
            logger.error(f"Error handling STREAM_ERROR event: {e}", exc_info=True)

    @staticmethod
    async def handle_stream_interrupted(event_data: dict[str, Any]) -> None:
        """
        React to STREAM_INTERRUPTED events and update session status.

        When streaming is interrupted:
        - Clear interaction_status
        - Clear streaming_started_at
        - Set status to "active" (user can continue)
        - Emit session_interaction_status_changed WS event

        Args:
            event_data: Full event dict with type, model_id, meta, user_id, timestamp
        """
        try:
            meta = event_data.get("meta", {})
            session_id = meta.get("session_id")

            if not session_id:
                logger.error(f"Invalid STREAM_INTERRUPTED event: missing session_id: {meta}")
                return

            # Use fresh database session to avoid conflicts
            with DBSession(engine) as db:
                session = db.get(Session, UUID(session_id))
                if not session:
                    logger.warning(f"Session {session_id} not found for STREAM_INTERRUPTED event")
                    return

                session.interaction_status = ""
                session.streaming_started_at = None
                session.status = "active"
                session.updated_at = datetime.utcnow()
                user_id = session.user_id

                db.add(session)
                db.commit()

                logger.info(f"Session {session_id} status updated to 'active' with interaction_status cleared (STREAM_INTERRUPTED)")

            # Emit session_interaction_status_changed WS event to user room
            try:
                from app.services.event_service import event_service
                await event_service.emit_event(
                    event_type="session_interaction_status_changed",
                    model_id=UUID(session_id),
                    meta={
                        "session_id": session_id,
                        "interaction_status": "",
                    },
                    user_id=user_id,
                )
            except Exception as ws_err:
                logger.error(f"Failed to emit session_interaction_status_changed: {ws_err}", exc_info=True)

        except Exception as e:
            logger.error(f"Error handling STREAM_INTERRUPTED event: {e}", exc_info=True)

    @staticmethod
    async def auto_generate_session_title(
        session_id: UUID,
        first_message_content: str,
        get_fresh_db_session: callable
    ) -> None:
        """
        Auto-generate session title from first message if no title exists.
        Runs asynchronously in the background.

        Args:
            session_id: Session UUID
            first_message_content: Content of the first message
            get_fresh_db_session: Callable that returns a fresh DB session (context manager)
        """
        logger.info(f"[DEBUG] Starting auto_generate_session_title for session {session_id}")
        logger.info(f"[DEBUG] First message content (first 100 chars): {first_message_content[:100]}")

        try:
            from app.services.ai_functions_service import AIFunctionsService

            # Check if AIFunctionsService is available
            is_available = AIFunctionsService.is_available()
            logger.info(f"[DEBUG] AIFunctionsService.is_available() = {is_available}")

            if is_available:
                # Generate title using LLM
                logger.info(f"[DEBUG] Calling AIFunctionsService.generate_session_title")
                title = await asyncio.to_thread(
                    AIFunctionsService.generate_session_title,
                    first_message_content
                )
                logger.info(f"[DEBUG] Generated title: {title}")

                # Only update if we got a valid title
                if not title:
                    logger.warning(f"[DEBUG] Generated title is empty, skipping update")
                    return

                # Update session with generated title
                logger.info(f"[DEBUG] Updating session {session_id} with title: {title}")
                with get_fresh_db_session() as db:
                    updated_session = SessionService.update_session(
                        db_session=db,
                        session_id=session_id,
                        data=SessionUpdate(title=title)
                    )
                    if updated_session:
                        logger.info(f"[DEBUG] Session updated successfully. New title: {updated_session.title}")
                    else:
                        logger.warning(f"[DEBUG] Session {session_id} not found when updating title")
                logger.info(f"Generated session title asynchronously: {title}")
            else:
                # If no LLM available, set truncated message immediately
                logger.info(f"[DEBUG] No LLM available, using fallback title")
                fallback_title = first_message_content[:100]
                if len(first_message_content) > 100:
                    fallback_title += "..."

                logger.info(f"[DEBUG] Fallback title: {fallback_title}")
                with get_fresh_db_session() as db:
                    updated_session = SessionService.update_session(
                        db_session=db,
                        session_id=session_id,
                        data=SessionUpdate(title=fallback_title)
                    )
                    if updated_session:
                        logger.info(f"[DEBUG] Session updated with fallback title. New title: {updated_session.title}")
                    else:
                        logger.warning(f"[DEBUG] Session {session_id} not found when updating fallback title")
                logger.info(f"Set fallback session title (no LLM): {fallback_title}")

        except Exception as e:
            # If title generation fails, don't update the title at all - just log and return
            logger.error(f"[DEBUG] Exception in auto_generate_session_title: {e}", exc_info=True)
            logger.warning(f"Failed to generate session title asynchronously, leaving title unchanged: {e}")

    @staticmethod
    async def send_session_message(
        session_id: UUID | None,
        user_id: UUID,
        content: str,
        file_ids: list[UUID] | None = None,
        answers_to_message_id: UUID | None = None,
        get_fresh_db_session: callable = None,
        initiate_streaming: bool = True,
        agent_id: UUID | None = None,
        access_token_id: UUID | None = None,
    ) -> dict[str, Any]:
        """
        Send a message to a session and optionally initiate streaming.

        This method encapsulates the full flow of sending a message:
        1. Gets existing session or creates new one (if agent_id provided)
        2. Validates session ownership and existence
        3. If files attached and env not running, waits for environment activation
        4. Handles file attachments if present
        5. Creates user message with sent_to_agent_status='pending'
        6. Delegates to initiate_stream() for processing (if initiate_streaming=True)

        This is the centralized entry point for sending messages to sessions,
        used by both API endpoints and internal services (like handover, A2A).

        Args:
            session_id: Session UUID (can be None if agent_id is provided to create new session)
            user_id: User ID (for ownership validation)
            content: Message content
            file_ids: Optional list of file IDs to attach
            answers_to_message_id: Optional message ID being answered
            get_fresh_db_session: Callable that returns a fresh DB session (context manager)
                                 If None, uses default engine session
            initiate_streaming: If True (default), calls initiate_stream() to start
                               background streaming via WebSocket. If False, just creates
                               the message and returns session info for manual streaming
                               (used by A2A handler for SSE streaming).
            agent_id: Optional agent ID for creating a new session (required if session_id is None)
            access_token_id: Optional access token ID (for A2A token-created sessions)

        Returns:
            dict with status information:
            {
                "action": "streaming" | "pending" | "no_pending_messages" | "error" | "message_created",
                "message": str,
                "pending_count": int,
                "files_attached": int (if files present),
                "session_id": UUID (always included),
                "environment_id": UUID (if initiate_streaming=False),
                "external_session_id": str | None (if initiate_streaming=False)
            }
        """
        # Default get_fresh_db_session if not provided
        if get_fresh_db_session is None:
            get_fresh_db_session = lambda: DBSession(engine)

        has_files = bool(file_ids)
        is_new_session = False
        environment_id: UUID | None = None
        environment_status: str | None = None

        # Phase 1: Get/create session and validate (collect IDs for later use)
        with get_fresh_db_session() as db:
            from app.models import AgentEnvironment

            chat_session: Session | None = None

            # Get existing session or create new one
            if session_id:
                chat_session = SessionService.get_session(db, session_id)
                if not chat_session:
                    return {"action": "error", "message": "Session not found"}
            elif agent_id:
                # Create new session for the agent
                session_data = SessionCreate(
                    agent_id=agent_id,
                    mode="conversation",
                )
                chat_session = SessionService.create_session(
                    db_session=db,
                    user_id=user_id,
                    data=session_data,
                    access_token_id=access_token_id,
                )
                if not chat_session:
                    return {"action": "error", "message": "Failed to create session"}
                session_id = chat_session.id
                is_new_session = True
                logger.info(f"Created new session {session_id} for agent {agent_id}")
            else:
                return {"action": "error", "message": "Either session_id or agent_id must be provided"}

            # Validate ownership
            if chat_session.user_id != user_id:
                return {"action": "error", "message": "Not enough permissions"}

            # Get environment info
            environment = db.get(AgentEnvironment, chat_session.environment_id)
            if not environment:
                return {"action": "error", "message": "Environment not found"}

            environment_id = environment.id
            environment_status = environment.status

        # Phase 2: If files attached and environment not running, wait for activation
        # This must be done BEFORE file upload because upload_files_to_agent_env
        # requires the environment to be running.
        if has_files and environment_status != "running":
            logger.info(
                f"Files attached but environment {environment_id} is {environment_status}, "
                "waiting for environment to be ready before file upload..."
            )
            try:
                await SessionService.ensure_environment_ready_for_streaming(
                    session_id=session_id,
                    get_fresh_db_session=get_fresh_db_session,
                    timeout_seconds=120
                )
                logger.info(f"Environment {environment_id} ready for file upload")
            except Exception as e:
                logger.error(f"Failed to activate environment for file upload: {e}", exc_info=True)
                return {"action": "error", "message": f"Failed to activate environment: {str(e)}"}

        # Phase 3: Create message (with file upload if needed)
        with get_fresh_db_session() as db:
            from app.services.message_service import MessageService

            # Re-fetch session in fresh context
            chat_session = SessionService.get_session(db, session_id)
            if not chat_session:
                return {"action": "error", "message": "Session not found"}

            if has_files:
                try:
                    # Prepare user message with files (uploads to agent-env)
                    user_message, message_content_for_agent = await MessageService.prepare_user_message_with_files(
                        session=db,
                        session_id=session_id,
                        message_content=content,
                        file_ids=file_ids,
                        environment_id=chat_session.environment_id,
                        user_id=user_id,
                        answers_to_message_id=answers_to_message_id
                    )
                    logger.info(f"Prepared message with {len(file_ids)} files for session {session_id}")
                except Exception as e:
                    logger.error(f"Failed to prepare message with files: {e}", exc_info=True)
                    return {"action": "error", "message": f"Failed to prepare message with files: {str(e)}"}
            else:
                # Create user message without files
                user_message = MessageService.create_message(
                    session=db,
                    session_id=session_id,
                    role="user",
                    content=content,
                    answers_to_message_id=answers_to_message_id
                )
                logger.info(f"Created user message for session {session_id}")

            # If not initiating streaming, return session info for manual streaming
            if not initiate_streaming:
                # Auto-generate session title for new sessions
                # This handles cases where initiate_stream won't be called (e.g., A2A SSE streaming)
                # When initiate_streaming=True, title generation happens in initiate_stream instead
                if is_new_session and (not chat_session.title or chat_session.title.strip() == ""):
                    logger.info(f"[DEBUG] New session {session_id} has no title. Creating title generation task...")
                    create_task_with_error_logging(
                        SessionService.auto_generate_session_title(
                            session_id=session_id,
                            first_message_content=content,
                            get_fresh_db_session=get_fresh_db_session
                        ),
                        task_name=f"auto_generate_title_session_{session_id}"
                    )
                external_session_id = chat_session.session_metadata.get("external_session_id") if chat_session.session_metadata else None
                result = {
                    "action": "message_created",
                    "message": "Message created, ready for manual streaming",
                    "pending_count": 1,
                    "session_id": session_id,
                    "environment_id": chat_session.environment_id,
                    "external_session_id": external_session_id,
                }
                if has_files:
                    result["files_attached"] = len(file_ids)
                return result

        # Phase 4: Delegate to initiate_stream to decide when to stream
        # Note: We exit the DB session context before calling initiate_stream
        # because it will create its own fresh sessions
        result = await SessionService.initiate_stream(
            session_id=session_id,
            get_fresh_db_session=get_fresh_db_session
        )

        # Always include session_id in result
        result["session_id"] = session_id

        # Add files info to result if files were attached
        if has_files:
            result["files_attached"] = len(file_ids)

        return result

    @staticmethod
    async def initiate_stream(
        session_id: UUID,
        get_fresh_db_session: callable
    ) -> dict[str, Any]:
        """
        Initiate streaming for a session's pending messages.

        This method checks if the environment is ready and either:
        - Starts streaming immediately if environment is running
        - Marks session as pending_stream if environment is not ready
        - Starts environment activation if environment is suspended

        This is the central orchestration point for deciding when to stream.

        Args:
            session_id: Session UUID
            get_fresh_db_session: Callable that returns a fresh DB session (context manager)

        Returns:
            dict with status information:
            {
                "action": "streaming" | "pending" | "no_pending_messages",
                "message": str,
                "pending_count": int
            }
        """
        with get_fresh_db_session() as db:
            from app.services.message_service import MessageService

            # Get session via service
            session = SessionService.get_session(db, session_id)
            if not session:
                return {"action": "error", "message": "Session not found"}

            # Get environment
            environment = db.get(AgentEnvironment, session.environment_id)
            if not environment:
                return {"action": "error", "message": "Environment not found"}

            # Get agent
            agent = db.get(Agent, environment.agent_id)
            if not agent:
                return {"action": "error", "message": "Agent not found"}

            # Check if there are pending messages
            concatenated_content, pending_messages = MessageService.collect_pending_messages(db, session_id)

            if not pending_messages:
                logger.info(f"No pending messages for session {session_id}")
                return {
                    "action": "no_pending_messages",
                    "message": "No pending messages to process",
                    "pending_count": 0
                }

            # Auto-generate session title from first message if no title exists
            if not session.title or session.title.strip() == "":
                # Start background task to generate title with error logging
                logger.info(f"[DEBUG] Session {session_id} has no title (title='{session.title}'). Creating title generation task...")
                create_task_with_error_logging(
                    SessionService.auto_generate_session_title(
                        session_id=session_id,
                        first_message_content=concatenated_content,
                        get_fresh_db_session=get_fresh_db_session
                    ),
                    task_name=f"auto_generate_title_session_{session_id}"
                )
                logger.info(f"[DEBUG] Title generation task created for session {session_id}")
            else:
                logger.info(f"[DEBUG] Session {session_id} already has title: '{session.title}'. Skipping title generation.")

            # Check environment status
            if environment.status in ("suspended", "stopped"):
                logger.info(f"Environment {environment.id} is {environment.status}, initiating start...")

                # Store IDs for background task (avoid passing detached ORM objects)
                environment_id_for_activation = environment.id
                agent_id_for_activation = agent.id
                env_was_suspended = environment.status == "suspended"

                # Start activation in background
                from app.services.environment_lifecycle import EnvironmentLifecycleManager

                async def _start_with_fresh_session():
                    """Wrapper to fetch objects with a fresh DB session"""
                    with get_fresh_db_session() as fresh_db:
                        fresh_env = fresh_db.get(AgentEnvironment, environment_id_for_activation)
                        fresh_agent = fresh_db.get(Agent, agent_id_for_activation)
                        if not fresh_env or not fresh_agent:
                            logger.error(f"Could not fetch environment or agent for activation")
                            return False

                        lifecycle_manager = EnvironmentLifecycleManager()
                        if env_was_suspended:
                            # Use optimized activation for suspended environments
                            return await lifecycle_manager.activate_suspended_environment(
                                db_session=fresh_db,
                                environment=fresh_env,
                                agent=fresh_agent,
                                emit_events=True
                            )
                        else:
                            # Use full start for stopped environments
                            return await lifecycle_manager.start_environment(
                                db_session=fresh_db,
                                environment=fresh_env,
                                agent=fresh_agent
                            )

                create_task_with_error_logging(
                    _start_with_fresh_session(),
                    task_name=f"start_environment_{environment_id_for_activation}"
                )

                # Mark session as pending stream
                session.interaction_status = "pending_stream"
                session.pending_messages_count = len(pending_messages)
                db.add(session)
                db.commit()

                logger.info(f"Environment start initiated, session {session_id} marked as pending_stream")
                return {
                    "action": "pending",
                    "message": "Environment is starting, messages will be processed once ready",
                    "pending_count": len(pending_messages)
                }

            elif environment.status == "running":
                logger.info(f"Environment {environment.id} is running, starting stream for session {session_id}")

                # Refresh expiring OAuth credentials before streaming
                # This ensures the agent has valid tokens for the expected stream duration
                from app.services.credentials_service import CredentialsService

                try:
                    credentials_refreshed = await CredentialsService.refresh_expiring_credentials_for_agent(
                        session=db,
                        agent_id=agent.id
                    )

                    if credentials_refreshed:
                        logger.info(
                            f"Credentials were refreshed for agent {agent.id}, "
                            f"syncing to environment {environment.id}"
                        )
                        # Sync refreshed credentials to the agent environment
                        await CredentialsService.sync_credentials_to_agent_environments(
                            session=db,
                            agent_id=agent.id
                        )
                        logger.info(f"Credentials synced to environment {environment.id}")
                except Exception as e:
                    # Log error but don't block streaming - credentials might still work
                    logger.error(
                        f"Error refreshing/syncing credentials for agent {agent.id}: {e}",
                        exc_info=True
                    )

                # Environment is ready - start streaming in background
                # The actual streaming will be done by process_pending_messages
                create_task_with_error_logging(
                    MessageService.process_pending_messages(
                        session_id=session_id,
                        get_fresh_db_session=get_fresh_db_session
                    ),
                    task_name=f"process_pending_messages_{session_id}"
                )

                return {
                    "action": "streaming",
                    "message": f"Processing {len(pending_messages)} pending message(s)",
                    "pending_count": len(pending_messages)
                }

            else:
                # Environment in other state (activating, building, error, etc.)
                logger.info(f"Environment {environment.id} status is {environment.status}, marking session as pending_stream")

                session.interaction_status = "pending_stream"
                session.pending_messages_count = len(pending_messages)
                db.add(session)
                db.commit()

                return {
                    "action": "pending",
                    "message": f"Environment is {environment.status}, messages will be processed once ready",
                    "pending_count": len(pending_messages)
                }

    @staticmethod
    async def _activate_environment_and_wait(
        environment_id: UUID,
        agent_id: UUID,
        get_fresh_db_session: callable,
        timeout_seconds: int = 120
    ) -> tuple[AgentEnvironment, Agent]:
        """
        Activate a suspended environment and wait for it to become ready.

        This is a shared helper used by both UI and A2A flows when synchronous
        activation is needed.

        Args:
            environment_id: Environment UUID
            agent_id: Agent UUID
            get_fresh_db_session: Callable that returns a fresh DB session (context manager)
            timeout_seconds: Maximum time to wait for activation (default 120s)

        Returns:
            tuple: (environment, agent) when ready

        Raises:
            RuntimeError: If activation fails or times out
        """
        import time
        from app.services.environment_lifecycle import EnvironmentLifecycleManager

        logger.info(f"Activating environment {environment_id} synchronously...")

        with get_fresh_db_session() as db:
            environment = db.get(AgentEnvironment, environment_id)
            agent = db.get(Agent, agent_id)

            if not environment or not agent:
                raise RuntimeError("Environment or agent not found")

            lifecycle_manager = EnvironmentLifecycleManager()

            # Activate synchronously and wait for it
            await lifecycle_manager.activate_suspended_environment(
                db_session=db,
                environment=environment,
                agent=agent,
                emit_events=True
            )

        # Refresh and verify
        with get_fresh_db_session() as db:
            environment = db.get(AgentEnvironment, environment_id)
            agent = db.get(Agent, agent_id)

            if environment.status != "running":
                raise RuntimeError(f"Environment activation failed, status: {environment.status}")

            return environment, agent

    @staticmethod
    async def _wait_for_environment_ready(
        environment_id: UUID,
        agent_id: UUID,
        get_fresh_db_session: callable,
        timeout_seconds: int = 120
    ) -> tuple[AgentEnvironment, Agent]:
        """
        Wait for an environment that's already starting/activating to become ready.

        Handles both:
        - 'activating' status (suspended environments being reactivated)
        - 'starting' status (stopped environments being started)

        Args:
            environment_id: Environment UUID
            agent_id: Agent UUID
            get_fresh_db_session: Callable that returns a fresh DB session (context manager)
            timeout_seconds: Maximum time to wait (default 120s)

        Returns:
            tuple: (environment, agent) when ready

        Raises:
            RuntimeError: If start/activation fails or times out
        """
        import time

        logger.info(f"Waiting for environment {environment_id} to become ready...")
        start_time = time.time()

        while time.time() - start_time < timeout_seconds:
            with get_fresh_db_session() as db:
                environment = db.get(AgentEnvironment, environment_id)

                if environment.status == "running":
                    agent = db.get(Agent, agent_id)
                    logger.info(f"Environment {environment_id} is now running")
                    return environment, agent

                if environment.status == "error":
                    raise RuntimeError(f"Environment activation failed: {environment.status_message}")

                if environment.status not in ["activating", "starting"]:
                    raise RuntimeError(f"Environment in unexpected status: {environment.status}")

            await asyncio.sleep(2)

        raise RuntimeError(f"Timeout waiting for environment activation ({timeout_seconds}s)")

    @staticmethod
    async def _start_stopped_environment_and_wait(
        environment_id: UUID,
        agent_id: UUID,
        get_fresh_db_session: callable,
        timeout_seconds: int = 120
    ) -> tuple[AgentEnvironment, Agent]:
        """
        Start a stopped environment and wait for it to become ready.

        This is similar to _activate_environment_and_wait but uses start_environment()
        instead of activate_suspended_environment() since stopped environments may
        need full container setup.

        Args:
            environment_id: Environment UUID
            agent_id: Agent UUID
            get_fresh_db_session: Callable that returns a fresh DB session (context manager)
            timeout_seconds: Maximum time to wait for start (default 120s)

        Returns:
            tuple: (environment, agent) when ready

        Raises:
            RuntimeError: If start fails or times out
        """
        from app.services.environment_lifecycle import EnvironmentLifecycleManager

        logger.info(f"Starting stopped environment {environment_id} synchronously...")

        with get_fresh_db_session() as db:
            environment = db.get(AgentEnvironment, environment_id)
            agent = db.get(Agent, agent_id)

            if not environment or not agent:
                raise RuntimeError("Environment or agent not found")

            lifecycle_manager = EnvironmentLifecycleManager()

            # Start the environment (this handles both new and existing containers)
            await lifecycle_manager.start_environment(
                db_session=db,
                environment=environment,
                agent=agent
            )

        # Refresh and verify
        with get_fresh_db_session() as db:
            environment = db.get(AgentEnvironment, environment_id)
            agent = db.get(Agent, agent_id)

            if environment.status != "running":
                raise RuntimeError(f"Environment start failed, status: {environment.status}")

            return environment, agent

    @staticmethod
    async def ensure_environment_ready_for_streaming(
        session_id: UUID,
        get_fresh_db_session: callable,
        timeout_seconds: int = 120
    ) -> tuple[AgentEnvironment, Agent]:
        """
        Ensure environment is ready for streaming (blocking).

        This method checks if the environment is ready to accept streaming requests.
        If the environment is suspended or stopped, it starts it and waits for it to become ready.

        This is a BLOCKING method suitable for A2A streaming where we need
        the environment to be running before we can stream (unlike UI flow which uses
        WebSocket events for async notification).

        Handles:
        - 'suspended' environments: Uses optimized activation (skips container setup)
        - 'stopped' environments: Uses full start (may need container setup)
        - 'activating'/'starting' environments: Waits for them to complete

        Args:
            session_id: Session UUID
            get_fresh_db_session: Callable that returns a fresh DB session (context manager)
            timeout_seconds: Maximum time to wait for start/activation (default 120s)

        Returns:
            tuple: (environment, agent) when ready

        Raises:
            ValueError: If session/environment/agent not found
            RuntimeError: If environment is in an error state or timeout waiting for start/activation
        """
        with get_fresh_db_session() as db:
            session = SessionService.get_session(db, session_id)
            if not session:
                raise ValueError("Session not found")

            environment = db.get(AgentEnvironment, session.environment_id)
            if not environment:
                raise ValueError("Environment not found")

            agent = db.get(Agent, environment.agent_id)
            if not agent:
                raise ValueError("Agent not found")

            initial_status = environment.status
            environment_id = environment.id
            agent_id = agent.id

        # If already running, return immediately
        if initial_status == "running":
            with get_fresh_db_session() as db:
                environment = db.get(AgentEnvironment, environment_id)
                agent = db.get(Agent, agent_id)
                return environment, agent

        # If suspended, activate it synchronously using shared helper
        if initial_status == "suspended":
            return await SessionService._activate_environment_and_wait(
                environment_id=environment_id,
                agent_id=agent_id,
                get_fresh_db_session=get_fresh_db_session,
                timeout_seconds=timeout_seconds
            )

        # If stopped, start it synchronously using shared helper
        if initial_status == "stopped":
            return await SessionService._start_stopped_environment_and_wait(
                environment_id=environment_id,
                agent_id=agent_id,
                get_fresh_db_session=get_fresh_db_session,
                timeout_seconds=timeout_seconds
            )

        # If activating or starting, wait for it to complete using shared helper
        if initial_status in ("activating", "starting"):
            return await SessionService._wait_for_environment_ready(
                environment_id=environment_id,
                agent_id=agent_id,
                get_fresh_db_session=get_fresh_db_session,
                timeout_seconds=timeout_seconds
            )

        # Other statuses (error, building, etc.) - cannot proceed
        raise RuntimeError(f"Environment is not ready for streaming, status: {initial_status}")

    @staticmethod
    async def handle_environment_activated(event_data: dict[str, Any]) -> None:
        """
        React to ENVIRONMENT_ACTIVATED events and process pending messages.

        When an environment is activated, this method:
        - Finds all sessions with pending_stream status for this environment
        - Calls initiate_stream() for each session to process pending messages

        Args:
            event_data: Full event dict with type, model_id, meta, user_id, timestamp
        """
        try:
            meta = event_data.get("meta", {})
            environment_id = meta.get("environment_id")

            if not environment_id:
                logger.error(f"Invalid ENVIRONMENT_ACTIVATED event: missing environment_id: {meta}")
                return

            logger.info(f"Processing ENVIRONMENT_ACTIVATED event for environment {environment_id}")

            # Find all sessions with pending messages for this environment
            with DBSession(engine) as db:
                sessions_with_pending = db.exec(
                    select(Session).where(
                        Session.environment_id == UUID(environment_id),
                        Session.interaction_status == "pending_stream"
                    )
                ).all()

                session_ids = [session.id for session in sessions_with_pending]

            logger.info(f"Found {len(session_ids)} sessions with pending_stream for environment {environment_id}")

            # Process each session by calling initiate_stream
            # NOTE: We call initiate_stream as a coroutine but don't await it directly
            # because it creates background tasks. Awaiting would cause task cancellation.
            for session_id in session_ids:
                try:
                    # Create a task for initiate_stream to prevent parent context cancellation
                    create_task_with_error_logging(
                        SessionService.initiate_stream(
                            session_id=session_id,
                            get_fresh_db_session=lambda: DBSession(engine)
                        ),
                        task_name=f"initiate_stream_{session_id}"
                    )
                    logger.info(f"Initiated stream for session {session_id}")
                except Exception as e:
                    logger.error(f"Error initiating stream for session {session_id}: {e}", exc_info=True)

        except Exception as e:
            logger.error(f"Error handling ENVIRONMENT_ACTIVATED event: {e}", exc_info=True)
