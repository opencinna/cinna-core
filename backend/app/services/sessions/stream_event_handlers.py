"""
Concrete StreamEventHandler implementations for each integration path.

- ``WebSocketEventHandler`` — UI path: emits events to frontend via Socket.IO
- ``MCPEventHandler`` — MCP / App MCP path: sends MCP progress notifications,
  accumulates response text
- ``A2AStreamEventHandler`` — A2A streaming path: maps events to A2A SSE format
  and pushes them into an asyncio.Queue for the SSE generator
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, AsyncIterator
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
        # Agent `attachment` events are emitted to the Socket.IO room explicitly
        # at finalize (inside MessageService._process_attachments) so session
        # watchers see the file regardless of which client drives the stream.
        # They are *also* yielded into the stream generator so streaming A2A
        # clients receive the FilePart live. Skip them here so the web client
        # (when it is the driver) doesn't render the same attachment twice.
        if event.get("type") == "attachment":
            return
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
    """Maps streaming events to A2A SSE format and exposes them as an async iterator.

    The handler owns three concerns:

    1. **Protocol mapping** — each agent-env event is mapped to an A2A
       SSE payload via ``A2AEventMapper``.
    2. **Producer/consumer plumbing** — events are pushed into an
       ``asyncio.Queue`` as they arrive from the processor; the SSE
       generator drains the queue and yields them to the client. This
       gives the client true incremental streaming (chunked ``assistant``/
       ``tool``/``thinking`` events) rather than a single burst at the end.
    3. **Background-task lifecycle** — ``stream(processor)`` runs
       ``processor.process()`` as a detached task, handles errors and
       cancellation, and guarantees the consumer always unblocks via a
       ``None`` sentinel (posted by a done-callback on the task).

    Callers should consume events with::

        async for sse_event in handler.stream(processor):
            yield sse_event

    The ``on_event`` / ``on_error`` / ``on_*`` hooks continue to satisfy the
    ``StreamEventHandler`` protocol so the handler can be passed to
    ``SessionStreamProcessor`` unchanged.
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
        self.queue: asyncio.Queue[str | None] = asyncio.Queue()
        self.error_enqueued: bool = False

    @property
    def event_mapper(self):
        if self._event_mapper is None:
            from app.services.a2a.a2a_event_mapper import A2AEventMapper
            self._event_mapper = A2AEventMapper
        return self._event_mapper

    # ------------------------------------------------------------------
    # StreamEventHandler protocol
    # ------------------------------------------------------------------

    async def on_stream_starting(self, pending_count: int) -> None:
        pass  # Initial "working" status is sent by the A2A handler before process()

    async def on_event(self, event: dict) -> None:
        a2a_event = self.event_mapper.map_stream_event(
            event, self.task_id, self.context_id
        )
        if a2a_event:
            await self.queue.put(self.format_sse_event(self.request_id, a2a_event))

    async def on_error(self, error: Exception) -> None:
        await self._enqueue_error_once(f"Error: {error}")

    async def on_complete(self, response_text: str) -> None:
        pass  # Final status is handled by the A2A event mapper's "done" event handling

    # ------------------------------------------------------------------
    # Producer / consumer
    # ------------------------------------------------------------------

    async def stream(self, processor: Any) -> AsyncIterator[str]:
        """Run ``processor.process()`` as a background task and yield SSE events as they arrive.

        The consumer is unblocked on every exit path:

        - Normal completion → producer returns, done-callback posts sentinel.
        - Error in processor (streaming or pre-streaming) → error event is
          enqueued once (either by ``on_error`` from inside the processor
          or by ``_run_processor``'s own except branch), then the sentinel.
        - Client disconnect (``GeneratorExit`` inside this async generator)
          → the ``finally`` block cancels the producer task; the
          done-callback still posts the sentinel.
        - Producer task killed before its ``finally`` runs (theoretical) →
          the done-callback posts the sentinel anyway.
        """
        producer = asyncio.create_task(
            self._run_processor(processor),
            name=f"a2a-stream-producer-{self.task_id}",
        )
        # Defense-in-depth: guarantee the consumer unblocks even if the
        # producer dies without running its own finally.
        producer.add_done_callback(lambda _t: self.queue.put_nowait(None))

        try:
            while True:
                item = await self.queue.get()
                if item is None:
                    break
                yield item
        finally:
            if not producer.done():
                producer.cancel()
                try:
                    await producer
                except asyncio.CancelledError:
                    pass
                except Exception as exc:  # noqa: BLE001 - log only, don't mask original
                    logger.warning(
                        "A2A producer task raised during cancel: %s", exc,
                        exc_info=True,
                    )

    async def _run_processor(self, processor: Any) -> None:
        try:
            await processor.process()
        except asyncio.CancelledError:
            # Client disconnected or caller cancelled us — don't enqueue a
            # "failed" event for a cancel we initiated ourselves.
            raise
        except (ValueError, RuntimeError) as exc:
            logger.error(
                "A2A streaming: environment not ready: %s", exc,
            )
            await self._enqueue_error_once(f"Environment error: {exc}")
        except Exception as exc:  # noqa: BLE001 - surface as error event
            logger.error(
                "A2A streaming: error during processing",
                exc_info=True,
            )
            await self._enqueue_error_once(f"Error: {exc}")

    async def _enqueue_error_once(self, message: str) -> None:
        """Enqueue a final ``failed`` status event, at most once.

        The invariant ``error_enqueued == True  ⇒  error event is on the
        queue`` is upheld by enqueueing *before* flipping the flag, so a
        caller that sees the flag can safely skip its own enqueue.
        """
        if self.error_enqueued:
            return
        from a2a.types import TaskState

        error_event = self.event_mapper.create_status_update(
            task_id=self.task_id,
            context_id=self.context_id,
            state=TaskState.failed,
            final=True,
            message=message,
        )
        try:
            await self.queue.put(self.format_sse_event(self.request_id, error_event))
        except Exception as exc:  # noqa: BLE001 - logging-only; don't mask original
            logger.warning(
                "A2A streaming: failed to enqueue error event: %s", exc,
                exc_info=True,
            )
            return
        self.error_enqueued = True
