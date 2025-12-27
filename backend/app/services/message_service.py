from uuid import UUID
from datetime import datetime
from typing import AsyncIterator
import json
import httpx
import logging
import asyncio
from sqlmodel import Session, select, func
from sqlalchemy.orm import Session as AlchemySession
from app.models import SessionMessage, Session as ChatSession, AgentEnvironment, Agent

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
    ) -> SessionMessage:
        """Create message in session with auto-incremented sequence"""
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

        session.commit()
        session.refresh(message)
        return message

    @staticmethod
    def get_session_messages(
        session: Session, session_id: UUID, limit: int = 100, offset: int = 0
    ) -> list[SessionMessage]:
        """Get messages for session ordered by sequence"""
        statement = (
            select(SessionMessage)
            .where(SessionMessage.session_id == session_id)
            .order_by(SessionMessage.sequence_number)
            .offset(offset)
            .limit(limit)
        )
        return list(session.exec(statement).all())

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
    async def send_message_to_environment_stream(
        base_url: str,
        auth_headers: dict,
        user_message: str,
        mode: str,
        agent_sdk: str = "claude",
        external_session_id: str | None = None
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
    async def sync_agent_prompts_from_environment(
        session: Session,
        environment: AgentEnvironment,
        agent: Agent
    ) -> bool:
        """
        Sync agent prompts from environment docs files back to agent model.

        This should be called after a building mode session completes to capture
        any updates the agent made to WORKFLOW_PROMPT.md and ENTRYPOINT_PROMPT.md.

        Args:
            session: Database session
            environment: Agent environment instance
            agent: Agent instance to update

        Returns:
            True if sync successful
        """
        try:
            from app.services.environment_lifecycle import EnvironmentLifecycleManager

            lifecycle_manager = EnvironmentLifecycleManager()
            adapter = lifecycle_manager.get_adapter(environment)

            # Fetch current prompts from environment
            prompts = await adapter.get_agent_prompts()

            workflow_prompt = prompts.get("workflow_prompt")
            entrypoint_prompt = prompts.get("entrypoint_prompt")

            # Update agent if prompts have changed
            updated = False
            if workflow_prompt and workflow_prompt != agent.workflow_prompt:
                agent.workflow_prompt = workflow_prompt
                updated = True
                logger.info(f"Updated agent {agent.id} workflow_prompt from environment ({len(workflow_prompt)} chars)")

            if entrypoint_prompt and entrypoint_prompt != agent.entrypoint_prompt:
                agent.entrypoint_prompt = entrypoint_prompt
                updated = True
                logger.info(f"Updated agent {agent.id} entrypoint_prompt from environment ({len(entrypoint_prompt)} chars)")

            if updated:
                agent.updated_at = datetime.utcnow()
                session.add(agent)
                session.commit()
                session.refresh(agent)
                logger.info(f"Synced agent prompts from environment {environment.id} to agent {agent.id}")

            return True

        except Exception as e:
            logger.error(f"Failed to sync agent prompts from environment: {e}", exc_info=True)
            return False

    @staticmethod
    async def sync_agent_prompts_to_environment(
        environment: AgentEnvironment,
        workflow_prompt: str | None = None,
        entrypoint_prompt: str | None = None
    ) -> bool:
        """
        Sync agent prompts from backend to environment docs files.

        This should be called when user manually edits prompts in the backend UI
        to ensure the environment has the latest versions.

        Args:
            environment: Agent environment instance
            workflow_prompt: Updated workflow prompt content (None to skip)
            entrypoint_prompt: Updated entrypoint prompt content (None to skip)

        Returns:
            True if sync successful
        """
        try:
            from app.services.environment_lifecycle import EnvironmentLifecycleManager

            lifecycle_manager = EnvironmentLifecycleManager()
            adapter = lifecycle_manager.get_adapter(environment)

            # Push prompts to environment
            await adapter.set_agent_prompts(
                workflow_prompt=workflow_prompt,
                entrypoint_prompt=entrypoint_prompt
            )

            logger.info(f"Synced agent prompts to environment {environment.id}")
            return True

        except Exception as e:
            logger.error(f"Failed to sync agent prompts to environment: {e}", exc_info=True)
            return False

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

        # Variables to collect agent response
        agent_response_parts = []
        streaming_events = []  # Store raw streaming events for visualization
        new_external_session_id = external_session_id
        was_interrupted = False  # Track if message was interrupted
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
                external_session_id=external_session_id
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

                # Capture external session ID from done event
                if event.get("type") == "done" and not external_session_id:
                    event_session_id = event.get("session_id") or event.get("metadata", {}).get("session_id")
                    if event_session_id:
                        new_external_session_id = event_session_id
                        logger.info(f"External session ID captured from ResultMessage: {new_external_session_id}")

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

                def _save_agent_message():
                    with get_fresh_db_session() as db:
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
                await asyncio.to_thread(_save_agent_message)
                logger.info(f"Agent response saved ({len(streaming_events)} events, model={response_metadata.get('model')}, has_questions={has_questions}, interrupted={was_interrupted})")

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

            # Sync agent prompts from environment if in building mode (non-blocking)
            if session_mode == "building":
                logger.info(f"Syncing agent prompts from environment after building session")
                try:
                    def _get_env_and_agent():
                        with get_fresh_db_session() as db:
                            env_db = db.get(AgentEnvironment, environment_id)
                            agent_db = db.get(Agent, env_db.agent_id) if env_db else None
                            return env_db, agent_db

                    env_db, agent_db = await asyncio.to_thread(_get_env_and_agent)

                    if env_db and agent_db:
                        # sync_agent_prompts_from_environment is already async
                        def _sync_prompts():
                            with get_fresh_db_session() as db:
                                # Re-fetch to ensure we have attached instances
                                env_attached = db.get(AgentEnvironment, environment_id)
                                agent_attached = db.get(Agent, env_db.agent_id)
                                if env_attached and agent_attached:
                                    import asyncio
                                    loop = asyncio.new_event_loop()
                                    asyncio.set_event_loop(loop)
                                    try:
                                        loop.run_until_complete(
                                            MessageService.sync_agent_prompts_from_environment(
                                                session=db,
                                                environment=env_attached,
                                                agent=agent_attached
                                            )
                                        )
                                    finally:
                                        loop.close()

                        await asyncio.to_thread(_sync_prompts)
                        logger.info("Agent prompts synced successfully from environment")
                    else:
                        logger.warning("Could not sync prompts: environment or agent not found")
                except Exception as sync_error:
                    logger.error(f"Failed to sync agent prompts: {sync_error}", exc_info=True)
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
