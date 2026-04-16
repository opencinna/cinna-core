"""
A2A Request Handler - handles A2A JSON-RPC requests.

This module bridges A2A protocol requests to internal services,
handling message send/stream, task get/cancel operations.

All data access is done through the service layer (SessionService, MessageService)
rather than direct database queries.

Authentication context:
- When using A2A access tokens, the handler enforces scope restrictions:
  - LIMITED scope: Can only access sessions created by this token
  - GENERAL scope: Can access all sessions for the agent
"""
import asyncio
import json
import logging
from typing import AsyncIterator, Any, Callable, Optional
from uuid import UUID
from datetime import UTC, datetime

from sqlmodel import Session as DbSession

from a2a.types import (
    Task,
    TaskState,
    TaskStatus,
    Message,
    Part,
    TextPart,
)

from app.models import Agent, Session as ChatSession, A2ATokenPayload
from app.models.environments.environment import AgentEnvironment
from app.services.sessions.session_service import SessionService
from app.services.a2a.a2a_event_mapper import A2AEventMapper
from app.services.a2a.a2a_task_store import DatabaseTaskStore
from app.services.sessions.active_streaming_manager import active_streaming_manager
from app.services.a2a.access_token_service import AccessTokenService

logger = logging.getLogger(__name__)


class A2ARequestHandler:
    """
    Handles A2A JSON-RPC requests by delegating to internal services.
    """

    def __init__(
        self,
        agent: Agent,
        environment: AgentEnvironment,
        user_id: UUID,
        get_db_session: Callable[[], DbSession],
        a2a_token_payload: Optional[A2ATokenPayload] = None,
        access_token_id: Optional[UUID] = None,
        backend_base_url: str = "",
    ):
        """
        Initialize the request handler.

        Args:
            agent: The Agent model instance
            environment: The agent's active environment
            user_id: The authenticated user's ID
            get_db_session: Callable that returns a fresh database session
            a2a_token_payload: Optional A2A token payload (if using access token auth)
            access_token_id: Optional access token ID (if using access token auth)
            backend_base_url: Backend base URL for generating public links
        """
        self.agent = agent
        self.environment = environment
        self.user_id = user_id
        self.get_db_session = get_db_session
        self.task_store = DatabaseTaskStore(get_db_session)
        self.a2a_token_payload = a2a_token_payload
        self.access_token_id = access_token_id
        self.backend_base_url = backend_base_url

    async def handle_message_send(
        self,
        params: dict[str, Any],
    ) -> Task:
        """
        Handle message/send request (non-streaming).

        Creates session if needed, sends message, waits for completion,
        and returns the task.

        Args:
            params: MessageSendParams dict with 'message' and optional 'configuration'

        Returns:
            A2A Task with final status
        """
        message_data = params.get("message", {})
        config = params.get("configuration", {})

        # Extract message content from parts
        content = self._extract_text_from_parts(message_data.get("parts", []))

        # Parse task_id to session_id if provided
        task_id = message_data.get("taskId") or message_data.get("task_id")
        session_id = self._parse_and_validate_session_id(task_id)

        # For existing sessions, ensure environment is ready before sending
        # (For new sessions, we check after creation)
        if session_id is not None:
            try:
                await SessionService.ensure_environment_ready_for_streaming(
                    session_id=session_id,
                    get_fresh_db_session=self.get_db_session,
                    timeout_seconds=120
                )
                logger.info(f"Environment is ready for A2A message/send (existing session)")
            except (ValueError, RuntimeError) as e:
                logger.error(f"Environment not ready for message/send: {e}")
                raise ValueError(f"Environment error: {str(e)}")

        # Send message using SessionService (creates session if session_id is None)
        result = await SessionService.send_session_message(
            session_id=session_id,
            user_id=self.user_id,
            content=content,
            file_ids=None,
            answers_to_message_id=None,
            get_fresh_db_session=self.get_db_session,
            agent_id=self.agent.id if session_id is None else None,
            access_token_id=self.access_token_id,
            backend_base_url=self.backend_base_url,
        )

        if result.get("action") == "error":
            raise ValueError(result.get("message", "Unknown error"))

        # Handle command results - return immediately with completed task
        if result.get("action") == "command_executed":
            cmd_session_id = result.get("session_id", session_id)
            return Task(
                id=str(cmd_session_id),
                contextId=str(cmd_session_id),
                status=TaskStatus(
                    state=TaskState.completed,
                    timestamp=datetime.now(UTC).isoformat() + "Z",
                    message=Message(
                        messageId="cmd_response",
                        role="agent",
                        parts=[Part(root=TextPart(text=result.get("message", "")))],
                    ),
                ),
            )

        # Get the session_id from result (may be newly created)
        session_id = result.get("session_id", session_id)

        # For new sessions, ensure environment is ready now
        # (session was just created, so we need to check environment status)
        if result.get("action") in ["pending", "message_created"]:
            try:
                await SessionService.ensure_environment_ready_for_streaming(
                    session_id=session_id,
                    get_fresh_db_session=self.get_db_session,
                    timeout_seconds=120
                )
                logger.info(f"Environment is ready for A2A message/send (new session)")

                # Re-initiate streaming now that environment is ready
                result = await SessionService.initiate_stream(
                    session_id=session_id,
                    get_fresh_db_session=self.get_db_session
                )
            except (ValueError, RuntimeError) as e:
                logger.error(f"Environment not ready for message/send: {e}")
                raise ValueError(f"Environment error: {str(e)}")

        # Wait for completion if streaming started
        if result.get("action") == "streaming":
            # Poll for completion
            max_wait = 300  # 5 minutes
            poll_interval = 1
            elapsed = 0

            while elapsed < max_wait:
                task = self.task_store.get(str(session_id))
                if task and task.status.state in [
                    TaskState.completed,
                    TaskState.failed,
                    TaskState.canceled,
                    TaskState.input_required,
                ]:
                    return task
                await asyncio.sleep(poll_interval)
                elapsed += poll_interval

        # Return final task state
        history_length = config.get("historyLength", config.get("history_length", 10))
        task = self.task_store.get_task_with_limited_history(str(session_id), history_length)
        if not task:
            # Create minimal task response
            task = Task(
                id=str(session_id),
                contextId=str(session_id),
                status=TaskStatus(
                    state=TaskState.completed,
                    timestamp=datetime.now(UTC).isoformat() + "Z",
                ),
            )
        return task

    async def handle_message_stream(
        self,
        params: dict[str, Any],
        request_id: Any,
    ) -> AsyncIterator[str]:
        """
        Handle message/stream request (SSE streaming).

        Creates session if needed, sends message, and yields A2A-formatted
        SSE events.  Delegates the core streaming pipeline to
        ``SessionStreamProcessor`` with an ``A2AStreamEventHandler``.

        Args:
            params: MessageSendParams dict with 'message' and optional 'configuration'
            request_id: JSON-RPC request ID for response correlation

        Yields:
            SSE event strings (data: {...}\n\n)
        """
        from app.services.sessions.stream_processor import SessionStreamProcessor
        from app.services.sessions.stream_event_handlers import A2AStreamEventHandler

        message_data = params.get("message", {})

        # Extract message content from parts
        content = self._extract_text_from_parts(message_data.get("parts", []))

        # Parse task_id to session_id if provided
        task_id = message_data.get("taskId") or message_data.get("task_id")
        session_id = self._parse_and_validate_session_id(task_id)

        # Use SessionService to create message (without initiating background streaming)
        result = await SessionService.send_session_message(
            session_id=session_id,
            user_id=self.user_id,
            content=content,
            file_ids=None,
            answers_to_message_id=None,
            get_fresh_db_session=self.get_db_session,
            initiate_streaming=False,
            agent_id=self.agent.id if session_id is None else None,
            access_token_id=self.access_token_id,
            backend_base_url=self.backend_base_url,
        )

        if result["action"] == "error":
            yield self._format_sse_error(request_id, -32001, result["message"])
            return

        # Handle command results - yield completed status and return
        if result["action"] == "command_executed":
            cmd_session_id = result.get("session_id", session_id)
            task_id_str = str(cmd_session_id)
            completed_event = A2AEventMapper._create_status_update(
                task_id=task_id_str,
                context_id=task_id_str,
                state=TaskState.completed,
                final=True,
                message=result.get("message", ""),
            )
            yield self._format_sse_event(request_id, completed_event)
            return

        # Get the session_id from result (may be newly created)
        session_id = result.get("session_id", session_id)

        task_id_str = str(session_id)
        context_id_str = str(session_id)

        # Yield initial working status
        initial_event = A2AEventMapper._create_status_update(
            task_id=task_id_str,
            context_id=context_id_str,
            state=TaskState.working,
            final=False,
        )
        yield self._format_sse_event(request_id, initial_event)

        # Notify client if environment needs activation
        env_status = self.environment.status
        if env_status in ["suspended", "activating", "starting"]:
            logger.info(f"Environment {self.environment.id} status is '{env_status}', notifying client...")
            activation_event = A2AEventMapper._create_status_update(
                task_id=task_id_str,
                context_id=context_id_str,
                state=TaskState.working,
                final=False,
                message="Starting up the agent environment, this may take a moment...",
            )
            yield self._format_sse_event(request_id, activation_event)

        # Delegate streaming to the unified processor
        handler = A2AStreamEventHandler(
            task_id=task_id_str,
            context_id=context_id_str,
            request_id=request_id,
            format_sse_event=self._format_sse_event,
        )

        processor = SessionStreamProcessor(
            session_id=session_id,
            get_fresh_db_session=self.get_db_session,
            event_handler=handler,
            use_session_lock=False,
            ensure_env_ready=True,
            env_timeout_seconds=120,
            log_prefix="[A2A]",
        )

        try:
            await processor.process()
        except (ValueError, RuntimeError) as e:
            logger.error(f"Environment not ready for streaming: {e}")
            error_event = A2AEventMapper._create_status_update(
                task_id=task_id_str,
                context_id=context_id_str,
                state=TaskState.failed,
                final=True,
                message=f"Environment error: {str(e)}",
            )
            yield self._format_sse_event(request_id, error_event)
            return
        except Exception as e:
            logger.error(f"Error streaming message: {e}")
            error_event = A2AEventMapper._create_status_update(
                task_id=task_id_str,
                context_id=context_id_str,
                state=TaskState.failed,
                final=True,
                message=f"Error: {str(e)}",
            )
            yield self._format_sse_event(request_id, error_event)
            return

        # Yield all collected A2A events
        for sse_event in handler.events:
            yield sse_event

    async def handle_tasks_get(self, params: dict[str, Any]) -> Task | None:
        """
        Handle tasks/get request.

        Args:
            params: TaskQueryParams dict with 'id' and optional 'historyLength'

        Returns:
            A2A Task or None if not found

        Raises:
            ValueError: If scope restrictions deny access
        """
        task_id = params.get("id")
        if not task_id:
            return None

        # If using A2A token, check scope restrictions
        if self.a2a_token_payload:
            try:
                session_id = UUID(task_id)
                with self.get_db_session() as db:
                    session = SessionService.get_session(db, session_id)
                    if session:
                        if not AccessTokenService.can_access_session(
                            self.a2a_token_payload, session.access_token_id
                        ):
                            raise ValueError(
                                "Access token scope does not allow access to this task"
                            )
            except ValueError as e:
                if "scope" in str(e).lower():
                    raise
                return None  # Invalid UUID

        history_length = params.get("historyLength", params.get("history_length", 10))
        return self.task_store.get_task_with_limited_history(task_id, history_length)

    async def handle_tasks_list(self, params: dict[str, Any]) -> list[Task]:
        """
        Handle tasks/list request.

        Lists tasks (sessions) for the agent, optionally filtered.

        Args:
            params: Optional parameters:
                - limit: Max number of tasks to return (default 20, max 100)
                - offset: Offset for pagination (default 0)

        Returns:
            List of A2A Task objects

        Note: This is a custom extension to the A2A protocol.
        """
        limit = min(params.get("limit", 20), 100)
        offset = params.get("offset", 0)

        tasks: list[Task] = []

        with self.get_db_session() as db:
            # Determine access token filter for LIMITED scope
            access_token_filter = None
            if self.a2a_token_payload and self.access_token_id:
                if not AccessTokenService.has_general_scope(self.a2a_token_payload):
                    access_token_filter = self.access_token_id

            # Use SessionService to list sessions
            sessions = SessionService.list_environment_sessions(
                db_session=db,
                environment_id=self.environment.id,
                limit=limit,
                offset=offset,
                access_token_id=access_token_filter,
            )

            for session in sessions:
                task = self.task_store.get_task_with_limited_history(str(session.id), 0)
                if task:
                    tasks.append(task)

        return tasks

    async def handle_tasks_cancel(self, params: dict[str, Any]) -> dict:
        """
        Handle tasks/cancel request.

        Args:
            params: TaskIdParams dict with 'id'

        Returns:
            Empty dict on success

        Raises:
            ValueError: If task not found, doesn't belong to agent, or scope restriction denies access
        """
        task_id = params.get("id")
        if not task_id:
            raise ValueError("Task ID is required")

        session_id = UUID(task_id)

        # Check if session exists and belongs to agent via service layer
        with self.get_db_session() as db:
            session = SessionService.get_session(db, session_id)
            if not session:
                raise ValueError("Task not found")
            if session.environment_id != self.environment.id:
                raise ValueError("Task does not belong to this agent")

            # If using A2A token, check scope restrictions
            if self.a2a_token_payload:
                if not AccessTokenService.can_access_session(
                    self.a2a_token_payload, session.access_token_id
                ):
                    raise ValueError(
                        "Access token scope does not allow access to this task"
                    )

        # Request interrupt via active streaming manager
        await active_streaming_manager.request_interrupt(session_id)

        return {}

    def _parse_and_validate_session_id(self, task_id: str | None) -> UUID | None:
        """
        Parse task_id string to UUID and validate A2A scope if applicable.

        Args:
            task_id: Optional task/session ID string

        Returns:
            Session UUID if valid task_id provided, None otherwise (for new session creation)

        Raises:
            ValueError: If scope restrictions deny access to existing session
        """
        if not task_id:
            return None

        try:
            session_id = UUID(task_id)
            # If using A2A token, check scope restrictions
            if self.a2a_token_payload:
                with self.get_db_session() as db:
                    session = SessionService.get_session(db, session_id)
                    if session:
                        if not AccessTokenService.can_access_session(
                            self.a2a_token_payload, session.access_token_id
                        ):
                            raise ValueError(
                                "Access token scope does not allow access to this session"
                            )
            return session_id
        except ValueError as e:
            if "scope" in str(e).lower():
                raise  # Re-raise scope errors
            return None  # Invalid UUID, will create new session

    def _extract_text_from_parts(self, parts: list[dict]) -> str:
        """
        Extract text content from A2A message parts.

        Args:
            parts: List of Part dicts

        Returns:
            Concatenated text content
        """
        text_parts = []
        for part in parts:
            # Handle both direct 'text' and nested 'root.text' structures
            if "text" in part:
                text_parts.append(part["text"])
            elif "kind" in part and part["kind"] == "text" and "text" in part:
                text_parts.append(part["text"])
            elif "root" in part and isinstance(part["root"], dict):
                root = part["root"]
                if "text" in root:
                    text_parts.append(root["text"])
        return "\n".join(text_parts)

    def _format_sse_event(self, request_id: Any, result: dict) -> str:
        """Format JSON-RPC result as SSE event."""
        response = {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": result,
        }
        return f"data: {json.dumps(response)}\n\n"

    def _format_sse_error(self, request_id: Any, code: int, message: str) -> str:
        """Format JSON-RPC error as SSE event."""
        response = {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        }
        return f"data: {json.dumps(response)}\n\n"
