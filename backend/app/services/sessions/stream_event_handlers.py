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

# Strong references to detached A2A producer tasks. The event loop only keeps
# weak references to tasks, so a producer that outlives its SSE consumer must be
# held here until it finishes (see ``A2AStreamEventHandler.stream``).
_DETACHED_A2A_TURNS: set[asyncio.Task] = set()


async def wait_for_detached_a2a_turns(timeout: float) -> None:
    """Wait (up to ``timeout`` seconds) for the detached A2A producers running now.

    Takes a snapshot of the set, so producers started while waiting are not
    awaited. Never raises on timeout; unfinished producers keep running.
    """
    pending = list(_DETACHED_A2A_TURNS)
    if not pending:
        return
    await asyncio.wait(pending, timeout=timeout)


class A2AStreamEventHandler:
    """Maps streaming events to A2A SSE format and exposes them as an async iterator.

    The handler owns three concerns:

    1. **Protocol mapping** — each agent-env event is mapped to an A2A
       SSE payload via ``A2AEventMapper``.
    2. **Producer/consumer plumbing** — events are pushed into an
       unbounded ``asyncio.Queue`` as they arrive from the processor; the
       SSE generator drains the queue and yields them to the client. This
       gives the client true incremental streaming (chunked ``assistant``/
       ``tool``/``thinking`` events) rather than a single burst at the end.
    3. **Background-task lifecycle** — ``stream(processor)`` runs
       ``processor.process()`` as a *detached* task. The agent turn is not
       tied to the SSE connection: if the client disconnects, the producer
       keeps running to completion (held in ``_DETACHED_A2A_TURNS``) and its
       events are dropped instead of queued. The consumer always unblocks via
       a ``None`` sentinel (posted by a done-callback on the task).

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
        # Unbounded on purpose: ``put_nowait`` never raises and never blocks,
        # so the producer can never stall on a slow or absent consumer.
        self.queue: asyncio.Queue[str | None] = asyncio.Queue()
        self.error_enqueued: bool = False
        self.final_emitted: bool = False
        self._consumer_attached: bool = True

    @property
    def event_mapper(self):
        if self._event_mapper is None:
            from app.services.a2a.a2a_event_mapper import A2AEventMapper
            self._event_mapper = A2AEventMapper
        return self._event_mapper

    def _emit(self, item: str | None) -> None:
        """Queue ``item`` for the consumer; drop it once the consumer is gone."""
        if not self._consumer_attached:
            return
        self.queue.put_nowait(item)

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
            if a2a_event.get("kind") == "status-update" and a2a_event.get("final"):
                self.final_emitted = True
            self._emit(self.format_sse_event(self.request_id, a2a_event))

    async def on_error(self, error: Exception) -> None:
        await self._enqueue_error_once(f"Error: {error}")

    async def on_complete(self, response_text: str) -> None:
        pass  # Final status is handled by the A2A event mapper's "done" event handling

    async def emit_final_state(self, state: Any) -> None:
        """Emit a closing status event unless a final event was already emitted.

        Used when a turn ends without streaming anything of its own (e.g. its
        message was already answered by an earlier batched turn). Terminal
        states and ``input_required`` are sent with ``final=True``.
        """
        if self.final_emitted:
            return
        from app.services.a2a.a2a_event_mapper import is_final_task_state

        event = self.event_mapper.create_status_update(
            task_id=self.task_id,
            context_id=self.context_id,
            state=state,
            final=is_final_task_state(state),
        )
        self._emit(self.format_sse_event(self.request_id, event))
        self.final_emitted = True

    # ------------------------------------------------------------------
    # Producer / consumer
    # ------------------------------------------------------------------

    async def stream(self, processor: Any) -> AsyncIterator[str]:
        """Run ``processor.process()`` as a detached task and yield SSE events as they arrive.

        The consumer is unblocked on every exit path:

        - Normal completion → producer returns, done-callback posts sentinel.
        - Error in processor (streaming or pre-streaming) → error event is
          enqueued once (either by ``on_error`` from inside the processor
          or by ``_run_processor``'s own except branch), then the sentinel.
        - Producer task killed before its ``finally`` runs (theoretical) →
          the done-callback posts the sentinel anyway.

        Client disconnect (``GeneratorExit`` inside this async generator) does
        **not** cancel the producer: the turn runs to completion. The
        ``finally`` block detaches the consumer and drains the queue, so later
        events are dropped rather than accumulated.
        """
        producer = asyncio.create_task(
            self._run_processor(processor),
            name=f"a2a-stream-producer-{self.task_id}",
        )
        _DETACHED_A2A_TURNS.add(producer)

        def _on_producer_done(task: asyncio.Task) -> None:
            _DETACHED_A2A_TURNS.discard(task)
            # Defense-in-depth: guarantee the consumer unblocks even if the
            # producer dies without running its own finally.
            self._emit(None)

        producer.add_done_callback(_on_producer_done)

        try:
            while True:
                item = await self.queue.get()
                if item is None:
                    break
                yield item
        finally:
            self._consumer_attached = False
            while True:
                try:
                    self.queue.get_nowait()
                except asyncio.QueueEmpty:
                    break

    async def _run_processor(self, processor: Any) -> None:
        try:
            await processor.process()
        except asyncio.CancelledError:
            # Only process shutdown cancels a detached producer; don't
            # enqueue a "failed" event for it.
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
        """Emit a final ``failed`` status event, at most once."""
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
        self._emit(self.format_sse_event(self.request_id, error_event))
        self.error_enqueued = True
        self.final_emitted = True


# ---------------------------------------------------------------------------
# Composite handler
# ---------------------------------------------------------------------------

class CompositeStreamEventHandler:
    """Fans every ``StreamEventHandler`` call out to a primary and passengers.

    Exists so a session can be watched by more than one consumer without any
    of them knowing about the others: the channel streaming relay tees off the
    UI path's ``WebSocketEventHandler`` this way (see
    ``server_channels/channel_stream_relay.py``), and the processor keeps
    talking to one handler.

    **The children are not peers, and the asymmetry is the point.** The
    ``primary`` is the handler the stream would have had anyway; its contract
    with ``SessionStreamProcessor`` is unchanged, exceptions and all. The
    ``passengers`` are the ones that composed themselves in, and *their*
    failures are isolated — a relay that cannot reach Google Chat must not
    stop Socket.IO emission, and the web client watching the same session must
    not freeze mid-answer because a channel went down.

    Isolating the primary too was the obvious first shape and it is wrong:
    ``WebSocketEventHandler.on_complete`` is what writes
    ``pending_messages_count`` and ``interaction_status`` back, and swallowing
    its failure leaves a web client watching a channel session on a stale
    "streaming" state until a poll re-derives it — a regression visible only on
    the composed sessions, i.e. exactly the ones this class was added for. So
    a primary failure propagates, and the processor does what it does for an
    uncomposed handler: ``on_error``, then re-raise.

    A primary failure does **not** cost the passengers their event. It is
    remembered, the fan-out finishes, and only then is it re-raised — a
    passenger that missed an ``assistant`` chunk (or a ``stop``) because
    somebody else's Socket.IO emit failed would lose text it is the only holder
    of.

    A child that does not implement one of the protocol's four methods is
    skipped rather than being an error, so a narrow handler (one that only
    cares about ``on_event``) can be composed in without stubs.

    Order is preserved: the primary is called first, then the passengers in
    order, so the caller can rely on the primary's side effects being in place.
    It is NOT a guarantee of completion order across children — each call is
    awaited to completion before the next child is entered.
    """

    def __init__(self, primary: Any, passengers: list[Any] | None = None) -> None:
        self.primary = primary
        self.passengers = [p for p in (passengers or []) if p is not None]

    @property
    def handlers(self) -> list[Any]:
        """Every child, primary first. For introspection and tests."""
        return ([self.primary] if self.primary is not None else []) + self.passengers

    async def on_stream_starting(self, pending_count: int) -> None:
        await self._fan_out("on_stream_starting", pending_count)

    async def on_event(self, event: dict[str, Any]) -> None:
        await self._fan_out("on_event", event)

    async def on_error(self, error: Exception) -> None:
        await self._fan_out("on_error", error)

    async def on_complete(self, response_text: str) -> None:
        await self._fan_out("on_complete", response_text)

    async def _fan_out(self, method_name: str, *args: Any) -> None:
        primary_error: Exception | None = None
        for handler in self.handlers:
            method = getattr(handler, method_name, None)
            if method is None:
                continue
            try:
                await method(*args)
            except asyncio.CancelledError:
                # Cancellation is the caller's, not this child's failure —
                # swallowing it here would keep a cancelled stream running
                # through the remaining children.
                raise
            except Exception as exc:
                if handler is self.primary:
                    # Held, not swallowed: the passengers still get this call,
                    # and the caller still gets the exception (see the
                    # docstring).
                    primary_error = exc
                    continue
                logger.warning(
                    "Composite stream handler: %s.%s failed (isolated)",
                    type(handler).__name__,
                    method_name,
                    exc_info=True,
                )
        if primary_error is not None:
            raise primary_error
