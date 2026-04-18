"""
A2A Request Handler - handles A2A JSON-RPC requests.

This module bridges A2A protocol requests to internal services,
handling message send/stream, task get/cancel operations.

All data access is done through the service layer (SessionService, MessageService)
rather than direct database queries.

Hook protocol
-------------

The handler exposes hook methods that subclasses override to customize
access-control and session-stamping behavior. The shared body of each
dispatch method (``handle_message_send``, ``handle_message_stream``,
``handle_tasks_get``, ``handle_tasks_list``, ``handle_tasks_cancel``) lives
here and calls into those hooks:

  * ``_parse_session_scope(task_id)`` — parse + scope-check an existing
    session id, or return ``None`` for new sessions.
  * ``_authorize_existing_session(session)`` — guard tasks/get &
    tasks/cancel against out-of-scope sessions.
  * ``_stamp_new_session(session_id)`` — post-create hook (no-op by default).
  * ``_integration_type_for_new_session()`` — passed to
    ``SessionService.send_session_message`` (``None`` by default).
  * ``_session_access_token_id()`` — A2A-token id to thread through
    ``SessionService`` (powers both new-session lineage and command context).
  * ``_task_list_access_token_filter()`` — DB-level filter for tasks/list.
  * ``_task_list_filter(session)`` — in-memory filter for tasks/list.
  * ``_wrap_env_error(exc)`` — shape env-readiness errors for the caller.
  * ``_stream_scope_error(exc, request_id)`` — optionally surface scope
    violations as inline SSE errors (default: ``None`` → propagate).

The default hook implementations enforce A2A-access-token scope — the
core ``/api/v1/a2a/...`` surface instantiates this class directly.
``ExternalA2AContextHandler`` overrides them for the three-target-type
``/api/v1/external/a2a/...`` surface.
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

    Subclass this handler to customize access-control and session-stamping
    via the hook methods listed at the top of this module.
    """

    log_prefix: str = "[A2A]"

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

    # ------------------------------------------------------------------
    # Hook methods — subclasses override to customize access control
    # ------------------------------------------------------------------

    def _parse_session_scope(self, task_id: str | None) -> UUID | None:
        """Parse a task_id string to a session UUID, enforcing scope rules.

        Default behavior (A2A-token scope):
        - Returns ``None`` for falsy or non-UUID task ids (new session will
          be created).
        - When an A2A token is in use, raises ``ValueError`` with "scope" in
          the message if the session's ``access_token_id`` is outside the
          token's scope.

        Subclasses override to raise domain-specific exceptions for
        scope-violation cases (e.g. ``TaskScopeViolationError``).
        """
        if not task_id:
            return None
        try:
            session_id = UUID(task_id)
        except (ValueError, TypeError):
            return None

        if self.a2a_token_payload:
            with self.get_db_session() as db:
                session = SessionService.get_session(db, session_id)
                if session is not None and not AccessTokenService.can_access_session(
                    self.a2a_token_payload, session.access_token_id
                ):
                    raise ValueError(
                        "Access token scope does not allow access to this session"
                    )
        return session_id

    def _authorize_existing_session(self, session: ChatSession) -> None:
        """Guard tasks/get & tasks/cancel against out-of-scope sessions.

        Default: enforce A2A-token scope. Raises ``ValueError`` on denial.
        """
        if self.a2a_token_payload:
            if not AccessTokenService.can_access_session(
                self.a2a_token_payload, session.access_token_id
            ):
                raise ValueError(
                    "Access token scope does not allow access to this task"
                )

    def _stamp_new_session(self, session_id: UUID) -> None:
        """Post-create hook for a newly created session.

        Default: no-op. Subclasses override to write ``caller_id``,
        ``session_metadata``, and other bookkeeping fields.
        """
        return None

    def _integration_type_for_new_session(self) -> str | None:
        """``integration_type`` to pass to ``SessionService.send_session_message``.

        Default: ``None`` — core A2A sessions inherit the default.
        """
        return None

    def _session_access_token_id(self) -> Optional[UUID]:
        """``access_token_id`` to pass into ``send_session_message``.

        Used for two things by SessionService: (a) stamp on newly created
        sessions (A2A-token lineage) and (b) populate ``CommandContext`` so
        slash commands like ``/files`` can render A2A-token-signed links.

        Default returns the A2A access token id for the core A2A surface.
        The external surface returns ``None`` (no A2A tokens involved).
        """
        return self.access_token_id

    def _task_list_access_token_filter(self) -> Optional[UUID]:
        """DB-level ``access_token_id`` filter for ``list_environment_sessions``.

        Default: when an A2A token is in use without general scope, filter
        by this token's id so LIMITED scope tokens only see their own
        sessions. Returns ``None`` otherwise (no DB filter).
        """
        if self.a2a_token_payload and self.access_token_id:
            if not AccessTokenService.has_general_scope(self.a2a_token_payload):
                return self.access_token_id
        return None

    def _task_list_filter(self, session: ChatSession) -> bool:
        """In-memory filter for tasks/list results. Default: include all."""
        return True

    def _wrap_env_error(self, exc: Exception) -> Exception:
        """Convert env-readiness errors to the caller's preferred exception type.

        Default: ``ValueError("Environment error: ...")``. Subclasses can
        swap in domain exceptions (e.g. ``NoActiveEnvironmentError``).
        """
        return ValueError(f"Environment error: {str(exc)}")

    def _stream_scope_error(
        self, exc: Exception, request_id: Any
    ) -> Optional[str]:
        """Optionally render a scope-violation exception as an inline SSE error.

        Default: returns ``None`` — the exception propagates out of the
        async generator (current core A2A behavior). Subclasses can return
        a formatted SSE string to keep the stream well-formed instead.
        """
        return None

    # ------------------------------------------------------------------
    # Shared dispatch methods
    # ------------------------------------------------------------------

    async def handle_message_send(
        self,
        params: dict[str, Any],
    ) -> Task:
        """
        Handle message/send request (non-streaming).

        Creates session if needed, sends message, waits for completion,
        and returns the task.
        """
        message_data = params.get("message", {})
        config = params.get("configuration", {})

        content = self._extract_text_from_parts(message_data.get("parts", []))

        task_id = message_data.get("taskId") or message_data.get("task_id")
        session_id = self._parse_session_scope(task_id)
        is_new_session = session_id is None

        # For existing sessions, ensure environment is ready before sending
        # (For new sessions, we check after creation)
        if session_id is not None:
            try:
                await SessionService.ensure_environment_ready_for_streaming(
                    session_id=session_id,
                    get_fresh_db_session=self.get_db_session,
                    timeout_seconds=120,
                )
                logger.info(
                    "%s environment is ready for message/send (existing session)",
                    self.log_prefix,
                )
            except (ValueError, RuntimeError) as e:
                logger.error(
                    "%s environment not ready for message/send: %s",
                    self.log_prefix, e,
                )
                raise self._wrap_env_error(e)

        # Send message using SessionService (creates session if session_id is None).
        # ``access_token_id`` is passed regardless of new/existing because
        # slash commands (e.g. /files) use it to decide whether to render
        # A2A-token-signed workspace links.
        result = await SessionService.send_session_message(
            session_id=session_id,
            user_id=self.user_id,
            content=content,
            file_ids=None,
            answers_to_message_id=None,
            get_fresh_db_session=self.get_db_session,
            agent_id=self.agent.id if is_new_session else None,
            access_token_id=self._session_access_token_id(),
            integration_type=self._integration_type_for_new_session() if is_new_session else None,
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

        # Stamp a newly created session (no-op by default)
        if is_new_session and session_id is not None:
            self._stamp_new_session(session_id)

        # For new sessions, ensure environment is ready now
        # (session was just created, so we need to check environment status)
        if result.get("action") in ["pending", "message_created"]:
            try:
                await SessionService.ensure_environment_ready_for_streaming(
                    session_id=session_id,
                    get_fresh_db_session=self.get_db_session,
                    timeout_seconds=120,
                )
                logger.info(
                    "%s environment is ready for message/send (new session)",
                    self.log_prefix,
                )

                # Re-initiate streaming now that environment is ready
                result = await SessionService.initiate_stream(
                    session_id=session_id,
                    get_fresh_db_session=self.get_db_session,
                )
            except (ValueError, RuntimeError) as e:
                logger.error(
                    "%s environment not ready for message/send: %s",
                    self.log_prefix, e,
                )
                raise self._wrap_env_error(e)

        # Wait for completion if streaming started
        if result.get("action") == "streaming":
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
        """
        from app.services.sessions.stream_processor import SessionStreamProcessor
        from app.services.sessions.stream_event_handlers import A2AStreamEventHandler

        message_data = params.get("message", {})
        content = self._extract_text_from_parts(message_data.get("parts", []))

        task_id = message_data.get("taskId") or message_data.get("task_id")
        try:
            session_id = self._parse_session_scope(task_id)
        except Exception as e:
            sse = self._stream_scope_error(e, request_id)
            if sse is not None:
                yield sse
                return
            raise
        is_new_session = session_id is None

        # Use SessionService to create message (without initiating background streaming).
        # ``access_token_id`` is passed regardless of new/existing — see
        # ``handle_message_send`` for the rationale.
        result = await SessionService.send_session_message(
            session_id=session_id,
            user_id=self.user_id,
            content=content,
            file_ids=None,
            answers_to_message_id=None,
            get_fresh_db_session=self.get_db_session,
            initiate_streaming=False,
            agent_id=self.agent.id if is_new_session else None,
            access_token_id=self._session_access_token_id(),
            integration_type=self._integration_type_for_new_session() if is_new_session else None,
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

        if is_new_session and session_id is not None:
            self._stamp_new_session(session_id)

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
            logger.info(
                "%s environment %s status is '%s', notifying client...",
                self.log_prefix, self.environment.id, env_status,
            )
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
            log_prefix=self.log_prefix,
        )

        try:
            await processor.process()
        except (ValueError, RuntimeError) as e:
            logger.error("%s environment not ready for streaming: %s", self.log_prefix, e)
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
            logger.error("%s error streaming message: %s", self.log_prefix, e)
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

        Returns the task or ``None`` if not found. Scope enforcement goes
        through ``_authorize_existing_session`` (which raises on denial).
        """
        task_id = params.get("id")
        if not task_id:
            return None

        try:
            session_uuid = UUID(task_id)
        except (ValueError, TypeError):
            return None

        with self.get_db_session() as db:
            session = SessionService.get_session(db, session_uuid)
            if session is None:
                return None
            self._authorize_existing_session(session)

        history_length = params.get("historyLength", params.get("history_length", 10))
        return self.task_store.get_task_with_limited_history(task_id, history_length)

    async def handle_tasks_list(self, params: dict[str, Any]) -> list[Task]:
        """
        Handle tasks/list request.

        Lists tasks (sessions) for the agent, optionally filtered via
        ``_task_list_access_token_filter`` (DB-level) and ``_task_list_filter``
        (in-memory).

        Note: This is a custom extension to the A2A protocol.
        """
        limit = min(params.get("limit", 20), 100)
        offset = params.get("offset", 0)

        tasks: list[Task] = []

        with self.get_db_session() as db:
            access_token_filter = self._task_list_access_token_filter()
            sessions = SessionService.list_environment_sessions(
                db_session=db,
                environment_id=self.environment.id,
                limit=limit,
                offset=offset,
                access_token_id=access_token_filter,
            )

            for session in sessions:
                if not self._task_list_filter(session):
                    continue
                task = self.task_store.get_task_with_limited_history(str(session.id), 0)
                if task:
                    tasks.append(task)

        return tasks

    async def handle_tasks_cancel(self, params: dict[str, Any]) -> dict:
        """
        Handle tasks/cancel request.

        Raises:
            ValueError: If task not found, doesn't belong to agent, or
                scope denies access (see ``_authorize_existing_session``).
        """
        task_id = params.get("id")
        if not task_id:
            raise ValueError("Task ID is required")

        session_id = UUID(task_id)

        with self.get_db_session() as db:
            session = SessionService.get_session(db, session_id)
            if not session:
                raise ValueError("Task not found")
            if session.environment_id != self.environment.id:
                raise ValueError("Task does not belong to this agent")

            self._authorize_existing_session(session)

        await active_streaming_manager.request_interrupt(session_id)

        return {}

    # ------------------------------------------------------------------
    # Shared helpers (used by subclasses via inheritance)
    # ------------------------------------------------------------------

    def _extract_text_from_parts(self, parts: list[dict]) -> str:
        """
        Extract text content from A2A message parts.
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
