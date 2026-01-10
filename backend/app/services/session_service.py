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
        db_session: DBSession, user_id: UUID, data: SessionCreate
    ) -> Session | None:
        """Create session using agent's active environment"""
        # Get agent to find active environment
        agent = db_session.get(Agent, data.agent_id)
        if not agent or not agent.active_environment_id:
            return None

        session = Session(
            environment_id=agent.active_environment_id,
            user_id=user_id,
            user_workspace_id=agent.user_workspace_id,
            title=data.title,
            mode=data.mode,
            agent_sdk=data.agent_sdk,
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
        external_session_id: str,
        sdk_type: str | None = None
    ) -> Session:
        """
        Set external SDK session ID in metadata.
        Called after first message to SDK to store the session ID for resumption.

        Args:
            db: Database session
            session: Session to update
            external_session_id: External SDK session ID
            sdk_type: SDK type (if None, uses session.agent_sdk)
        """
        session.session_metadata["external_session_id"] = external_session_id
        session.session_metadata["sdk_type"] = sdk_type or session.agent_sdk

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

                session.interaction_status = "running"
                session.status = "active"
                session.updated_at = datetime.utcnow()

                db.add(session)
                db.commit()

                logger.info(f"Session {session_id} status updated to 'active' with interaction_status 'running' (STREAM_STARTED)")

        except Exception as e:
            logger.error(f"Error handling STREAM_STARTED event: {e}", exc_info=True)

    @staticmethod
    async def handle_stream_completed(event_data: dict[str, Any]) -> None:
        """
        React to STREAM_COMPLETED events and update session status.

        When streaming completes:
        - Clear interaction_status (set to empty string)
        - Set status based on interruption:
          - If interrupted: "active" (user can continue)
          - If not interrupted: "completed"

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
                session.status = "active" if was_interrupted else "completed"
                session.updated_at = datetime.utcnow()

                db.add(session)
                db.commit()

                status_msg = f"'active' (interrupted)" if was_interrupted else "'completed'"
                logger.info(f"Session {session_id} status updated to {status_msg} with interaction_status cleared (STREAM_COMPLETED)")

        except Exception as e:
            logger.error(f"Error handling STREAM_COMPLETED event: {e}", exc_info=True)

    @staticmethod
    async def handle_stream_error(event_data: dict[str, Any]) -> None:
        """
        React to STREAM_ERROR events and update session status.

        When streaming encounters an error:
        - Clear interaction_status
        - Set status to "error"

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
                session.status = "error"
                session.updated_at = datetime.utcnow()

                db.add(session)
                db.commit()

                logger.info(f"Session {session_id} status updated to 'error' (STREAM_ERROR: {error_type})")

        except Exception as e:
            logger.error(f"Error handling STREAM_ERROR event: {e}", exc_info=True)

    @staticmethod
    async def handle_stream_interrupted(event_data: dict[str, Any]) -> None:
        """
        React to STREAM_INTERRUPTED events and update session status.

        When streaming is interrupted:
        - Clear interaction_status
        - Set status to "active" (user can continue)

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
                session.status = "active"
                session.updated_at = datetime.utcnow()

                db.add(session)
                db.commit()

                logger.info(f"Session {session_id} status updated to 'active' with interaction_status cleared (STREAM_INTERRUPTED)")

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

                # Update session with generated title
                logger.info(f"[DEBUG] Updating session {session_id} with title: {title}")
                with get_fresh_db_session() as db:
                    updated_session = SessionService.update_session(
                        db_session=db,
                        session_id=session_id,
                        data=SessionUpdate(title=title)
                    )
                    logger.info(f"[DEBUG] Session updated successfully. New title: {updated_session.title}")
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
                    logger.info(f"[DEBUG] Session updated with fallback title. New title: {updated_session.title}")
                logger.info(f"Set fallback session title (no LLM): {fallback_title}")

        except Exception as e:
            logger.error(f"[DEBUG] Exception in auto_generate_session_title: {e}", exc_info=True)
            logger.warning(f"Failed to generate session title asynchronously: {e}")
            # Fallback to truncated message if LLM fails
            fallback_title = first_message_content[:100]
            if len(first_message_content) > 100:
                fallback_title += "..."

            logger.info(f"[DEBUG] Using error fallback title: {fallback_title}")
            try:
                with get_fresh_db_session() as db:
                    updated_session = SessionService.update_session(
                        db_session=db,
                        session_id=session_id,
                        data=SessionUpdate(title=fallback_title)
                    )
                    logger.info(f"[DEBUG] Session updated with error fallback. New title: {updated_session.title}")
                logger.info(f"Set fallback session title after error: {fallback_title}")
            except Exception as fallback_error:
                logger.error(f"[DEBUG] Failed to set fallback title: {fallback_error}", exc_info=True)
                logger.error(f"Failed to set fallback title: {fallback_error}", exc_info=True)

    @staticmethod
    async def send_session_message(
        session_id: UUID,
        user_id: UUID,
        content: str,
        file_ids: list[UUID] | None = None,
        answers_to_message_id: UUID | None = None,
        get_fresh_db_session: callable = None
    ) -> dict[str, Any]:
        """
        Send a message to a session and initiate streaming.

        This method encapsulates the full flow of sending a message:
        1. Validates session ownership and existence
        2. Handles file attachments if present
        3. Creates user message with sent_to_agent_status='pending'
        4. Delegates to initiate_stream() for processing

        This is the centralized entry point for sending messages to sessions,
        used by both API endpoints and internal services (like handover).

        Args:
            session_id: Session UUID
            user_id: User ID (for ownership validation)
            content: Message content
            file_ids: Optional list of file IDs to attach
            answers_to_message_id: Optional message ID being answered
            get_fresh_db_session: Callable that returns a fresh DB session (context manager)
                                 If None, uses default engine session

        Returns:
            dict with status information:
            {
                "action": "streaming" | "pending" | "no_pending_messages" | "error",
                "message": str,
                "pending_count": int,
                "files_attached": int (if files present)
            }
        """
        # Default get_fresh_db_session if not provided
        if get_fresh_db_session is None:
            get_fresh_db_session = lambda: DBSession(engine)

        with get_fresh_db_session() as db:
            from app.models import Session as ChatSession, AgentEnvironment
            from app.services.message_service import MessageService

            # Get session
            chat_session = db.get(ChatSession, session_id)
            if not chat_session:
                return {"action": "error", "message": "Session not found"}

            # Validate ownership
            if chat_session.user_id != user_id:
                return {"action": "error", "message": "Not enough permissions"}

            # Get environment
            environment = db.get(AgentEnvironment, chat_session.environment_id)
            if not environment:
                return {"action": "error", "message": "Environment not found"}

            # Handle file attachments if present
            has_files = bool(file_ids)

            if has_files:
                try:
                    # Prepare user message with files
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

        # Delegate to initiate_stream to decide when to stream
        # Note: We exit the DB session context before calling initiate_stream
        # because it will create its own fresh sessions
        result = await SessionService.initiate_stream(
            session_id=session_id,
            get_fresh_db_session=get_fresh_db_session
        )

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
            from app.models import Session as ChatSession, AgentEnvironment, Agent
            from app.services.message_service import MessageService

            # Get session
            session = db.get(ChatSession, session_id)
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
            if environment.status == "suspended":
                logger.info(f"Environment {environment.id} is suspended, initiating activation...")

                # Store IDs for background task (avoid passing detached ORM objects)
                environment_id_for_activation = environment.id
                agent_id_for_activation = agent.id

                # Start activation in background
                from app.services.environment_lifecycle import EnvironmentLifecycleManager

                async def _activate_with_fresh_session():
                    """Wrapper to fetch objects with a fresh DB session"""
                    with get_fresh_db_session() as fresh_db:
                        fresh_env = fresh_db.get(AgentEnvironment, environment_id_for_activation)
                        fresh_agent = fresh_db.get(Agent, agent_id_for_activation)
                        if not fresh_env or not fresh_agent:
                            logger.error(f"Could not fetch environment or agent for activation")
                            return False

                        lifecycle_manager = EnvironmentLifecycleManager()
                        return await lifecycle_manager.activate_suspended_environment(
                            db_session=fresh_db,
                            environment=fresh_env,
                            agent=fresh_agent,
                            emit_events=True
                        )

                create_task_with_error_logging(
                    _activate_with_fresh_session(),
                    task_name=f"activate_environment_{environment_id_for_activation}"
                )

                # Mark session as pending stream
                session.interaction_status = "pending_stream"
                session.pending_messages_count = len(pending_messages)
                db.add(session)
                db.commit()

                logger.info(f"Environment activation started, session {session_id} marked as pending_stream")
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
