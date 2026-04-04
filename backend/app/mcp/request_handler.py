"""
MCP Request Handler - handles MCP tool requests.

This module bridges MCP protocol requests to internal services,
handling message send operations through the service layer.

All data access is done through the service layer (SessionService, MessageService)
rather than direct database queries (following the same pattern as A2ARequestHandler).
"""
import asyncio
import json
import logging
import time
from typing import Callable
from uuid import UUID

from sqlmodel import Session as DbSession

from app.models import Agent
from app.models.environments.environment import AgentEnvironment
from app.models.mcp.mcp_connector import MCPConnector
from app.services.sessions.session_service import SessionService
from app.services.sessions.message_service import MessageService
from app.utils import create_task_with_error_logging

logger = logging.getLogger(__name__)

# Per-session locks for sequential message processing.
# Bounded: evict unlocked entries when the dict exceeds _MAX_SESSION_LOCKS
# to prevent unbounded memory growth from orphaned sessions.
_session_locks: dict[str, asyncio.Lock] = {}
_MAX_SESSION_LOCKS = 1000


def _get_session_lock(session_id: str) -> asyncio.Lock:
    if session_id not in _session_locks:
        # Evict unlocked (idle) entries if we've exceeded the limit
        if len(_session_locks) >= _MAX_SESSION_LOCKS:
            to_remove = [
                sid for sid, lock in _session_locks.items()
                if not lock.locked()
            ]
            for sid in to_remove:
                del _session_locks[sid]
        _session_locks[session_id] = asyncio.Lock()
    return _session_locks[session_id]


class MCPRequestHandler:
    """
    Handles MCP tool requests by delegating to internal services.

    Follows the same isolation pattern as A2ARequestHandler:
    - Receives pre-resolved entities (agent, environment, connector)
    - All data access through SessionService and MessageService
    - No direct database queries in this handler
    """

    def __init__(
        self,
        agent: Agent,
        environment: AgentEnvironment,
        connector: MCPConnector,
        get_db_session: Callable[[], DbSession],
        authenticated_user_id: UUID | None = None,
    ):
        """
        Initialize the request handler.

        Args:
            agent: The Agent model instance
            environment: The agent's active environment
            connector: The MCP connector instance
            get_db_session: Callable that returns a fresh database session
            authenticated_user_id: OAuth-authenticated user ID (may differ from connector owner)
        """
        self.agent = agent
        self.environment = environment
        self.connector = connector
        self.get_db_session = get_db_session
        self.authenticated_user_id = authenticated_user_id

    @staticmethod
    async def _send_mcp_progress(mcp_ctx, event: dict, progress: int, last_info_time: float) -> tuple[int, float]:
        """
        Send MCP progress and log notifications for a streaming event.

        Returns updated (progress, last_info_time) tuple.
        Failures are silently caught — notifications must never crash the tool.
        """
        if mcp_ctx is None:
            return progress, last_info_time

        event_type = event.get("type", "")
        now = time.monotonic()

        try:
            # Send progress notification for phase labels (capped at 100)
            if event_type == "assistant" and progress < 100:
                progress = min(progress + 10, 100)
                await mcp_ctx.report_progress(progress, 100, "Processing...")
            elif event_type == "tool" and progress < 100:
                tool_name = event.get("name", "tool")
                progress = min(progress + 10, 100)
                await mcp_ctx.report_progress(progress, 100, f"Using tool: {tool_name}")
            elif event_type == "thinking" and progress < 100:
                progress = min(progress + 10, 100)
                await mcp_ctx.report_progress(progress, 100, "Thinking...")
        except Exception:
            logger.debug("[MCP] Failed to send progress notification (non-fatal)", exc_info=True)

        try:
            # Stream partial content via log notifications (throttled to 0.5s)
            if event_type == "assistant":
                content = event.get("content", "")
                if content and (now - last_info_time) >= 0.5:
                    await mcp_ctx.info(content)
                    last_info_time = now
        except Exception:
            logger.debug("[MCP] Failed to send log notification (non-fatal)", exc_info=True)

        return progress, last_info_time

    async def handle_send_message(
        self,
        message: str,
        mcp_session_id: str | None = None,
        context_id: str | None = None,
        mcp_ctx=None,
    ) -> str:
        """
        Handle send_message tool call.

        Uses the same SessionService/MessageService pipeline as A2A integration:
        1. Get/create platform session for this MCP connector
        2. Create user message
        3. Ensure environment is ready for streaming
        4. Stream response from agent environment
        5. Return JSON with response text and context_id

        Args:
            message: User message content
            mcp_session_id: Optional MCP transport session ID (from client header)
            context_id: Optional context_id for per-chat session isolation

        Returns:
            JSON string with "response" and "context_id" fields
        """
        # Phase 1: Get/create session and create user message
        with self.get_db_session() as db:
            try:
                platform_session, is_new_session = SessionService.get_or_create_mcp_session(
                    db_session=db,
                    connector=self.connector,
                    mcp_session_id=mcp_session_id,
                    context_id=context_id,
                    authenticated_user_id=self.authenticated_user_id,
                )
            except ValueError as e:
                return json.dumps({"error": str(e), "context_id": ""})

            session_id = platform_session.id
            result_context_id = str(session_id)
            external_session_id = (platform_session.session_metadata or {}).get(
                "external_session_id"
            )

            # Create user message (same as email/A2A pipeline)
            MessageService.create_message(
                session=db,
                session_id=session_id,
                role="user",
                content=message,
            )

        # Trigger title generation for new sessions (background task)
        if is_new_session:
            create_task_with_error_logging(
                SessionService.auto_generate_session_title(
                    session_id=session_id,
                    first_message_content=message,
                    get_fresh_db_session=self.get_db_session,
                ),
                task_name=f"auto_generate_title_session_{session_id}",
            )

        # Phase 2: Ensure environment is ready for streaming
        # (activates suspended environments, same as A2A handler)
        if mcp_ctx is not None:
            try:
                await mcp_ctx.report_progress(0, 100, "Preparing agent environment...")
            except Exception:
                logger.debug("[MCP] Failed to send initial progress notification (non-fatal)", exc_info=True)

        try:
            environment, _agent = await SessionService.ensure_environment_ready_for_streaming(
                session_id=session_id,
                get_fresh_db_session=self.get_db_session,
                timeout_seconds=120,
            )
        except (ValueError, RuntimeError) as e:
            logger.error("[MCP] Environment not ready for streaming: %s", e)
            return json.dumps({"error": f"Environment not ready: {e}", "context_id": result_context_id})

        # Phase 3: Stream response with per-session locking
        session_id_str = str(session_id)
        lock = _get_session_lock(session_id_str)
        if lock.locked():
            return json.dumps({"error": "Another message is being processed. Please wait.", "context_id": result_context_id})

        response_parts: list[str] = []
        mcp_progress = 0
        mcp_last_info_time = 0.0
        async with lock:
            try:
                async for event in MessageService.stream_message_with_events(
                    session_id=session_id,
                    environment_id=environment.id,
                    user_message_content=message,
                    session_mode=self.connector.mode,
                    external_session_id=external_session_id,
                    get_fresh_db_session=self.get_db_session,
                ):
                    event_type = event.get("type", "")

                    # Send MCP progress/log notifications for partial content streaming
                    mcp_progress, mcp_last_info_time = await self._send_mcp_progress(
                        mcp_ctx, event, mcp_progress, mcp_last_info_time,
                    )

                    if event_type == "assistant":
                        content = event.get("content", "")
                        if content:
                            response_parts.append(content)

                    elif event_type == "error":
                        error_content = event.get("content", "Unknown error")
                        logger.error("[MCP] Error event from agent: %s", error_content)
                        return json.dumps({"error": f"Error from agent: {error_content}", "context_id": result_context_id})

            except Exception as e:
                logger.error("[MCP] Error streaming from agent environment: %s", e)
                return json.dumps({"error": f"Failed to communicate with agent environment: {e}", "context_id": result_context_id})

        full_response = "\n\n".join(response_parts)
        logger.info(
            "[MCP] Response complete | session=%s | response_parts=%d | length=%d",
            session_id, len(response_parts), len(full_response),
        )
        return json.dumps({
            "response": full_response if full_response else "No response from agent",
            "context_id": result_context_id,
        })
