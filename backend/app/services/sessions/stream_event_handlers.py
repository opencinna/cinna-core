"""
Concrete StreamEventHandler implementations for each integration path.

- ``WebSocketEventHandler`` — UI path: emits events to frontend via Socket.IO
- ``MCPEventHandler`` — MCP / App MCP path: sends MCP progress notifications,
  accumulates response text
- ``A2AStreamEventHandler`` — A2A streaming path: maps events to A2A SSE format
  and pushes them into an asyncio.Queue for the SSE generator
"""

from __future__ import annotations

import logging
import time
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# UI (WebSocket) handler
# ---------------------------------------------------------------------------

class WebSocketEventHandler:
    """Emits streaming events to the frontend via Socket.IO.

    Also manages session state updates (pending_messages_count, interaction_status)
    after streaming completes.
    """

    def __init__(self, session_id: UUID, get_fresh_db_session) -> None:
        self.session_id = session_id
        self.get_fresh_db_session = get_fresh_db_session
        self._event_service = None

    @property
    def event_service(self):
        if self._event_service is None:
            from app.services.events.event_service import event_service
            self._event_service = event_service
        return self._event_service

    async def on_stream_starting(self, pending_count: int) -> None:
        await self.event_service.emit_stream_event(
            session_id=self.session_id,
            event_type="stream_started",
            event_data={
                "message": f"Processing {pending_count} pending message(s)...",
                "pending_count": pending_count,
            },
        )

    async def on_event(self, event: dict) -> None:
        await self.event_service.emit_stream_event(
            session_id=self.session_id,
            event_type=event.get("type"),
            event_data=event,
        )

    async def on_error(self, error: Exception) -> None:
        try:
            await self.event_service.emit_stream_event(
                session_id=self.session_id,
                event_type="error",
                event_data={
                    "type": "error",
                    "content": str(error),
                    "error_type": type(error).__name__,
                },
            )
        except Exception as emit_error:
            logger.error("Failed to emit error event: %s", emit_error, exc_info=True)

    async def on_complete(self, response_text: str) -> None:
        # Emit stream completed event
        await self.event_service.emit_stream_event(
            session_id=self.session_id,
            event_type="stream_completed",
            event_data={
                "status": "completed",
                "session_id": str(self.session_id),
            },
        )

        # Update session state
        from app.models import Session as ChatSession
        with self.get_fresh_db_session() as db:
            chat_session = db.get(ChatSession, self.session_id)
            if chat_session:
                chat_session.pending_messages_count = 0
                chat_session.interaction_status = ""
                chat_session.streaming_started_at = None
                db.add(chat_session)
                db.commit()


# ---------------------------------------------------------------------------
# MCP handler (shared by per-connector MCP and App MCP)
# ---------------------------------------------------------------------------

class MCPEventHandler:
    """Sends MCP progress/log notifications during streaming.

    The response text is accumulated by the ``SessionStreamProcessor``
    itself — this handler only deals with MCP-specific notifications.
    """

    def __init__(self, mcp_ctx: Any | None = None, log_prefix: str = "[MCP]") -> None:
        self.mcp_ctx = mcp_ctx
        self.log_prefix = log_prefix
        self._progress: int = 0
        self._last_info_time: float = 0.0
        self._has_error: bool = False
        self.error_content: str | None = None

    async def on_stream_starting(self, pending_count: int) -> None:
        if self.mcp_ctx is not None:
            try:
                await self.mcp_ctx.report_progress(0, 100, "Preparing agent environment...")
            except Exception:
                logger.debug(
                    "%s Failed to send initial progress notification (non-fatal)",
                    self.log_prefix, exc_info=True,
                )

    async def on_event(self, event: dict) -> None:
        event_type = event.get("type", "")

        # Track errors for the caller
        if event_type == "error":
            self._has_error = True
            self.error_content = event.get("content", "Unknown error")

        if self.mcp_ctx is None:
            return

        now = time.monotonic()

        # Progress bar
        try:
            if event_type == "assistant" and self._progress < 100:
                self._progress = min(self._progress + 10, 100)
                await self.mcp_ctx.report_progress(self._progress, 100, "Processing...")
            elif event_type == "tool" and self._progress < 100:
                tool_name = event.get("name", "tool")
                self._progress = min(self._progress + 10, 100)
                await self.mcp_ctx.report_progress(
                    self._progress, 100, f"Using tool: {tool_name}"
                )
            elif event_type == "thinking" and self._progress < 100:
                self._progress = min(self._progress + 10, 100)
                await self.mcp_ctx.report_progress(self._progress, 100, "Thinking...")
        except Exception:
            logger.debug(
                "%s Failed to send progress notification (non-fatal)",
                self.log_prefix, exc_info=True,
            )

        # Periodic content log
        try:
            if event_type == "assistant":
                content = event.get("content", "")
                if content and (now - self._last_info_time) >= 0.5:
                    await self.mcp_ctx.info(content)
                    self._last_info_time = now
        except Exception:
            logger.debug(
                "%s Failed to send log notification (non-fatal)",
                self.log_prefix, exc_info=True,
            )

    async def on_error(self, error: Exception) -> None:
        self._has_error = True
        self.error_content = str(error)

    async def on_complete(self, response_text: str) -> None:
        pass  # MCP path returns text synchronously; nothing to do here


# ---------------------------------------------------------------------------
# A2A streaming handler
# ---------------------------------------------------------------------------

class A2AStreamEventHandler:
    """Maps streaming events to A2A SSE format and pushes into an async queue.

    The A2A SSE generator reads from the queue to yield events to the client.
    """

    def __init__(
        self,
        task_id: str,
        context_id: str,
        request_id: Any,
        format_sse_event,
    ) -> None:
        self.task_id = task_id
        self.context_id = context_id
        self.request_id = request_id
        self.format_sse_event = format_sse_event
        self._event_mapper = None
        self.events: list[str] = []

    @property
    def event_mapper(self):
        if self._event_mapper is None:
            from app.services.a2a.a2a_event_mapper import A2AEventMapper
            self._event_mapper = A2AEventMapper
        return self._event_mapper

    async def on_stream_starting(self, pending_count: int) -> None:
        pass  # Initial "working" status is sent by the A2A handler before process()

    async def on_event(self, event: dict) -> None:
        a2a_event = self.event_mapper.map_stream_event(
            event, self.task_id, self.context_id
        )
        if a2a_event:
            self.events.append(self.format_sse_event(self.request_id, a2a_event))

    async def on_error(self, error: Exception) -> None:
        from a2a.types import TaskState
        error_event = self.event_mapper._create_status_update(
            task_id=self.task_id,
            context_id=self.context_id,
            state=TaskState.failed,
            final=True,
            message=f"Error: {error}",
        )
        self.events.append(self.format_sse_event(self.request_id, error_event))

    async def on_complete(self, response_text: str) -> None:
        pass  # Final status is handled by the A2A event mapper's "done" event handling
