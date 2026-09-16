"""
Unified session stream processor.

Provides a single streaming pipeline used by all message-processing paths:
UI (WebSocket), MCP (per-connector & App MCP), and A2A (SSE streaming).

Each path supplies a ``StreamEventHandler`` that customises event delivery
and error handling while the core lifecycle is shared:

    collect pending → inject context → mark sent → stream → finalize

The processor replaces duplicated logic that previously lived in:
- ``MessageService.process_pending_messages`` (UI path)
- ``message_streaming.stream_and_collect_response`` (MCP/App MCP path)
- ``A2ARequestHandler.handle_message_stream`` inline loop (A2A path)
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Protocol, runtime_checkable
from uuid import UUID

from sqlmodel import Session as DbSession

from app.models import Agent, Session as ChatSession
from app.models.environments.environment import AgentEnvironment
from app.services.sessions.message_service import MessageService
from app.services.sessions.session_service import SessionService

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Per-session locks — shared singleton across all handler types
# ---------------------------------------------------------------------------
_session_locks: dict[str, asyncio.Lock] = {}
_MAX_SESSION_LOCKS = 1000


def get_session_lock(session_id: str) -> asyncio.Lock:
    """Return (or create) a per-session asyncio.Lock.

    Bounded: evicts unlocked entries when the dict exceeds
    ``_MAX_SESSION_LOCKS`` to prevent unbounded memory growth.
    """
    if session_id not in _session_locks:
        if len(_session_locks) >= _MAX_SESSION_LOCKS:
            to_remove = [
                sid for sid, lock in _session_locks.items()
                if not lock.locked()
            ]
            for sid in to_remove:
                del _session_locks[sid]
        _session_locks[session_id] = asyncio.Lock()
    return _session_locks[session_id]


def is_session_lock_held(session_id: str) -> bool:
    """True when this process holds the per-session lock.

    Read-only: never creates a lock entry, so probing unknown sessions does
    not grow ``_session_locks``.
    """
    lock = _session_locks.get(session_id)
    return lock is not None and lock.locked()


# ---------------------------------------------------------------------------
# Event handler protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class StreamEventHandler(Protocol):
    """Strategy interface for path-specific event delivery.

    Each integration path (UI, MCP, A2A) implements this to customise
    how streaming events are forwarded to the caller.
    """

    async def on_stream_starting(self, pending_count: int) -> None:
        """Called after messages are collected, before streaming begins."""
        ...

    async def on_event(self, event: dict) -> None:
        """Called for each streaming event from the agent environment."""
        ...

    async def on_error(self, error: Exception) -> None:
        """Called when a fatal error occurs during streaming."""
        ...

    async def on_complete(self, response_text: str) -> None:
        """Called after streaming finishes successfully."""
        ...


# ---------------------------------------------------------------------------
# Processor
# ---------------------------------------------------------------------------

class SessionStreamProcessor:
    """Unified pipeline: collect → mark sent → stream → finalize.

    Usage::

        processor = SessionStreamProcessor(
            session_id=session_id,
            get_fresh_db_session=get_db,
            event_handler=MyEventHandler(...),
        )
        response_text = await processor.process()
    """

    def __init__(
        self,
        *,
        session_id: UUID,
        get_fresh_db_session: Callable[[], DbSession],
        event_handler: StreamEventHandler,
        use_session_lock: bool = False,
        ensure_env_ready: bool = True,
        env_timeout_seconds: int = 120,
        inject_recovery_context: bool = False,
        inject_webapp_context: bool = False,
        log_prefix: str = "[Stream]",
    ) -> None:
        self.session_id = session_id
        self.get_fresh_db_session = get_fresh_db_session
        self.event_handler = event_handler
        self.use_session_lock = use_session_lock
        self.ensure_env_ready = ensure_env_ready
        self.env_timeout_seconds = env_timeout_seconds
        self.inject_recovery_context = inject_recovery_context
        self.inject_webapp_context = inject_webapp_context
        self.log_prefix = log_prefix

        # Populated during processing
        self.environment: AgentEnvironment | None = None
        self.agent: Agent | None = None

    async def process(self) -> str:
        """Run the full streaming pipeline.

        Returns:
            The agent's collected response text (may be empty).

        Raises:
            EnvironmentNotReadyError: if environment cannot be activated.
            SessionLockBusyError: if per-session lock is already held.
        """
        # Phase 1: Ensure environment is ready
        if self.ensure_env_ready:
            self.environment, self.agent = await SessionService.ensure_environment_ready_for_streaming(
                session_id=self.session_id,
                get_fresh_db_session=self.get_fresh_db_session,
                timeout_seconds=self.env_timeout_seconds,
            )

        # Phase 2: Optionally acquire per-session lock
        if self.use_session_lock:
            return await self._process_with_lock()
        else:
            return await self._process_inner()

    async def _process_with_lock(self) -> str:
        """Acquire per-session lock and run inner pipeline."""
        lock = get_session_lock(str(self.session_id))
        if lock.locked():
            raise SessionLockBusyError(self.session_id)

        async with lock:
            return await self._process_inner()

    async def _process_inner(self) -> str:
        """Core pipeline: collect → context → mark sent → stream → return.

        Supports mixed batches of LLM messages and command stream messages.
        LLM batches are routed to stream_message_with_events (unchanged).
        Command batches are routed to stream_command_via_agent_env (new).
        """
        # Step 1: Collect and partition pending messages into routing batches
        with self.get_fresh_db_session() as db:
            batches = MessageService.collect_pending_batches(db, self.session_id)
            if not batches:
                logger.info(
                    "%s No pending messages for session %s",
                    self.log_prefix, self.session_id,
                )
                return ""

            # Read session metadata (needed for LLM batches)
            chat_session = db.get(ChatSession, self.session_id)
            session_mode = (chat_session.mode or "conversation") if chat_session else "conversation"
            external_session_id = (
                (chat_session.session_metadata or {}).get("external_session_id")
                if chat_session else None
            )

            all_message_ids = [msg.id for b in batches for msg in b["messages"]]

            # Extract prior-command IDs attached by collect_pending_batches / build_non_llm_prefix
            included_command_ids: list[UUID] = batches[0].get("_included_command_ids", []) if batches else []

            # Step 2: Inject recovery context for the first LLM batch (UI path only)
            # Find the first LLM batch and prepend recovery context to its content
            if self.inject_recovery_context and chat_session:
                if chat_session.session_metadata.get("recovery_pending"):
                    recovery_context = MessageService.build_recovery_context(db, self.session_id)
                    if recovery_context:
                        for batch in batches:
                            if batch["routing"] is None:
                                batch["content"] = f"{recovery_context}\n\n{batch['content']}"
                                break
                    # Clear recovery_pending flag
                    chat_session.session_metadata.pop("recovery_pending", None)
                    from sqlalchemy.orm.attributes import flag_modified
                    flag_modified(chat_session, "session_metadata")
                    db.add(chat_session)
                    db.commit()
                    db.refresh(chat_session)

            # Step 3: Determine webapp context injection (UI path only)
            # Applies to the first LLM batch that has page_context messages
            include_extra_instructions: str | None = None
            extra_instructions_prepend: str | None = None
            if self.inject_webapp_context and chat_session:
                all_pending_messages = [msg for b in batches for msg in b["messages"]]
                has_page_context = any(
                    (msg.message_metadata or {}).get("page_context")
                    for msg in all_pending_messages
                )
                if has_page_context:
                    should_inject = SessionService.activate_webapp_context(db, self.session_id)
                    if should_inject:
                        extra_instructions_prepend = (
                            "This session is connected to a webapp that the user is viewing.\n"
                            "- The user's current page state is included as <page_context> or "
                            "<context_update> blocks in their messages.\n"
                            "- You can interact with the webapp UI by embedding <webapp_action> "
                            "tags in your responses (see webapp actions documentation for syntax and available actions)."
                        )
                        if session_mode == "conversation":
                            include_extra_instructions = "/app/workspace/webapp/WEB_APP_ACTIONS.md"
                        logger.info(
                            "%s Including one-time webapp context for session %s",
                            self.log_prefix, self.session_id,
                        )

            # Resolve environment if not already done (non-env-ready paths)
            if self.environment is None:
                environment = db.get(AgentEnvironment, chat_session.environment_id) if chat_session else None
                if not environment:
                    logger.error("%s Environment not found for session %s", self.log_prefix, self.session_id)
                    return ""
                self.environment = environment

        # Step 4: Notify handler that streaming is about to start
        await self.event_handler.on_stream_starting(len(all_message_ids))

        # Step 5: Mark all pending messages as sent before any batch starts
        with self.get_fresh_db_session() as db:
            MessageService.mark_messages_as_sent(db, all_message_ids)

        # Step 5b: Mark prior-command messages as forwarded to the LLM
        # Must happen before streaming begins (Step 6) to prevent concurrent
        # LLM turns from re-including the same command output.
        if included_command_ids:
            with self.get_fresh_db_session() as db:
                MessageService.mark_command_messages_as_forwarded(db, included_command_ids)

        logger.info(
            "%s Processing %d batch(es) / %d message(s) for session %s",
            self.log_prefix, len(batches), len(all_message_ids), self.session_id,
        )

        # Step 6: Process each batch in order
        response_parts: list[str] = []
        # webapp context only injected into the first LLM batch
        webapp_context_injected = False

        for batch_idx, batch in enumerate(batches):
            with self.get_fresh_db_session() as db:
                MessageService.activate_channel_reply_target(db, self.session_id, batch["messages"])
            routing = batch["routing"]

            if routing == "command_stream":
                # --- Command streaming batch ---
                logger.info(
                    "%s Batch %d/%d: command stream (%s) for session %s",
                    self.log_prefix, batch_idx + 1, len(batches),
                    batch["command_name"], self.session_id,
                )
                try:
                    await MessageService.stream_command_via_agent_env(
                        session_id=self.session_id,
                        environment_id=self.environment.id,
                        resolved_command=batch["resolved_command"],
                        command_name=batch["command_name"],
                        user_message_id=batch["messages"][0].id,
                        get_fresh_db_session=self.get_fresh_db_session,
                        event_handler=self.event_handler,
                    )
                except Exception as e:
                    logger.error(
                        "%s Error in command batch %d for session %s: %s",
                        self.log_prefix, batch_idx + 1, self.session_id, e,
                        exc_info=True,
                    )
                    await self.event_handler.on_error(e)
                    # Stop processing remaining batches on command error
                    break

            else:
                # --- LLM streaming batch ---
                batch_content = batch["content"]

                # Apply webapp context only to the first LLM batch
                batch_extra_instructions = None
                batch_extra_prepend = None
                if not webapp_context_injected:
                    batch_extra_instructions = include_extra_instructions
                    batch_extra_prepend = extra_instructions_prepend
                    webapp_context_injected = True

                logger.info(
                    "%s Batch %d/%d: LLM stream for session %s",
                    self.log_prefix, batch_idx + 1, len(batches), self.session_id,
                )

                try:
                    async for event in MessageService.stream_message_with_events(
                        session_id=self.session_id,
                        environment_id=self.environment.id,
                        user_message_content=batch_content,
                        session_mode=session_mode,
                        external_session_id=external_session_id,
                        get_fresh_db_session=self.get_fresh_db_session,
                        include_extra_instructions=batch_extra_instructions,
                        extra_instructions_prepend=batch_extra_prepend,
                    ):
                        event_type = event.get("type", "")

                        # Accumulate assistant response text
                        if event_type == "assistant":
                            event_content = event.get("content", "")
                            if event_content:
                                response_parts.append(event_content)

                        # Forward every event to the handler
                        await self.event_handler.on_event(event)

                        # Early exit on error events
                        if event_type == "error":
                            break

                except Exception as e:
                    logger.error(
                        "%s Error in LLM batch %d for session %s: %s",
                        self.log_prefix, batch_idx + 1, self.session_id, e,
                        exc_info=True,
                    )
                    await self.event_handler.on_error(e)
                    raise

        full_response = "".join(response_parts)

        # Step 7: Notify handler of completion (once, after all batches)
        await self.event_handler.on_complete(full_response)

        logger.info(
            "%s All batches complete | session=%s | batches=%d | response_length=%d",
            self.log_prefix, self.session_id, len(batches), len(full_response),
        )
        return full_response


# ---------------------------------------------------------------------------
# Locked turn runner
# ---------------------------------------------------------------------------

class LockedTurnRunner:
    """Run one processor turn under the per-session lock, teardown included.

    Takes the shared per-session lock in WAIT mode (a queued turn waits, it
    is not rejected) and holds it around ``processor.process()`` and the
    teardown: a shielded ``clear_interaction_status`` followed by the
    optional ``after_teardown`` callback, which runs only when the turn
    returned normally. ``after_failure`` runs (also inside the lock, after
    teardown) when the turn raised an ``Exception``; its own errors are
    logged, and the turn's exception is re-raised. The reasoning, including the accepted shield
    residual, is the comment block in
    ``MessageService.process_pending_messages`` (web path), which keeps its
    own hand-written copy of this sequence.

    The wrapped processor must be built with ``use_session_lock=False`` so
    the lock is not taken twice.
    """

    def __init__(
        self,
        *,
        processor: Any,
        session_id: UUID,
        teardown_reason: str,
        after_teardown: Callable[[], Awaitable[None]] | None = None,
        after_failure: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.processor = processor
        self.session_id = session_id
        self.teardown_reason = teardown_reason
        self.after_teardown = after_teardown
        self.after_failure = after_failure

    async def process(self) -> str:
        async with get_session_lock(str(self.session_id)):
            try:
                result = await self.processor.process()
            except Exception:
                await self._teardown()
                if self.after_failure is not None:
                    try:
                        await self.after_failure()
                    except Exception as exc:  # noqa: BLE001 - never mask the turn error
                        logger.warning(
                            "after_failure hook failed | session=%s | error=%s",
                            self.session_id, exc,
                        )
                raise
            except BaseException:
                await self._teardown()
                raise
            await self._teardown()
            if self.after_teardown is not None:
                await self.after_teardown()
            return result

    async def _teardown(self) -> None:
        try:
            await asyncio.shield(
                SessionService.clear_interaction_status(
                    self.session_id, reason=self.teardown_reason,
                )
            )
        except Exception as exc:  # noqa: BLE001 - teardown is best-effort
            logger.warning(
                "Teardown clear failed | session=%s | reason=%s | error=%s",
                self.session_id, self.teardown_reason, exc,
            )


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class SessionLockBusyError(Exception):
    """Raised when a per-session lock is already held."""

    def __init__(self, session_id: UUID) -> None:
        self.session_id = session_id
        super().__init__(f"Another message is being processed for session {session_id}. Please wait.")


class EnvironmentNotReadyError(Exception):
    """Raised when the environment cannot be activated in time."""

    def __init__(self, session_id: UUID, reason: str) -> None:
        self.session_id = session_id
        super().__init__(f"Environment not ready for session {session_id}: {reason}")
