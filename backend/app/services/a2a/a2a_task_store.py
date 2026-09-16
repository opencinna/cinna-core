"""
A2A Task Store Adapter - wraps Session model for A2A TaskStore interface.

This module provides an adapter that implements the A2A TaskStore pattern
using our internal Session model for persistence.

All data access is done through the service layer (SessionService, MessageService)
rather than direct database queries.
"""
import logging
from typing import Callable
from uuid import UUID
from datetime import UTC, datetime

from sqlmodel import Session as DbSession

from a2a.types import (
    Task,
    TaskState,
    TaskStatus,
    Message,
)

from app.models import Session as ChatSession, SessionMessage
from app.services.sessions.session_service import SessionService
from app.services.sessions.message_service import MessageService
from app.services.a2a.a2a_event_mapper import A2AEventMapper
from app.services.sessions.active_streaming_manager import active_streaming_manager
from app.services.sessions.stream_processor import is_session_lock_held

logger = logging.getLogger(__name__)


class DatabaseTaskStore:
    """
    TaskStore implementation backed by Session database model.

    Maps A2A Task operations to internal Session/SessionMessage queries
    via the service layer.
    """

    def __init__(self, get_db_session: Callable[[], DbSession]):
        """
        Initialize the task store.

        Args:
            get_db_session: Callable that returns a fresh database session
        """
        self.get_db_session = get_db_session

    def get(self, task_id: str) -> Task | None:
        """
        Get task (session) by ID.

        Args:
            task_id: The task/session UUID as string

        Returns:
            A2A Task object or None if not found
        """
        try:
            with self.get_db_session() as db:
                session = SessionService.get_session(db, UUID(task_id))
                if not session:
                    return None
                return self._session_to_task(session, db)
        except Exception as e:
            logger.error(f"Error getting task {task_id}: {e}")
            return None

    def _session_to_task(self, session: ChatSession, db: DbSession) -> Task:
        """
        Convert Session to A2A Task.

        Args:
            session: Internal Session model
            db: Database session for querying messages

        Returns:
            A2A Task object
        """
        # Map session status to TaskState
        state = self._map_status_to_state(session, db)

        # Get message history via service layer
        history = self._get_message_history(session.id, db)

        # Build Task
        return Task(
            id=str(session.id),
            contextId=str(session.id),  # task = context for Phase 1
            status=TaskStatus(
                state=state,
                timestamp=session.updated_at.isoformat() + "Z" if session.updated_at else datetime.now(UTC).isoformat() + "Z",
            ),
            history=history if history else None,
        )

    def _map_status_to_state(
        self,
        session: ChatSession,
        db: DbSession,
        *,
        ignore_turn_in_flight: bool = False,
    ) -> TaskState:
        """
        Map internal session status to A2A TaskState.

        Delegates to A2AEventMapper for the actual mapping logic. Inputs the
        mapper would not reach are not queried: one query for the last
        message, then at most one each for pending rows and the turn's agent
        message.

        Args:
            session: Internal Session model
            db: Database session for querying last message
            ignore_turn_in_flight: Skip the in-flight signals (lock, local
                stream, pending rows). For callers that hold the session lock
                themselves after the turn's teardown, where those signals
                describe the caller, not the task.

        Returns:
            A2A TaskState enum value
        """
        status = session.status or ""
        interaction_status = session.interaction_status or ""

        # Get last message to check for unanswered tool questions
        last_message = MessageService.get_last_message(db, session.id)
        tool_questions_status = last_message.tool_questions_status if last_message else None

        decided_early = (
            tool_questions_status == "unanswered"
            or interaction_status in ("running", "pending_stream")
        )

        turn_in_flight = False
        has_pending = False
        if not decided_early and not ignore_turn_in_flight:
            turn_in_flight = self.is_turn_in_flight(session.id)
            if not turn_in_flight:
                has_pending = MessageService.has_pending_user_messages(db, session.id)

        last_agent_status = None
        if not (decided_early or turn_in_flight or has_pending or status == "error"):
            last_agent_status = self._current_turn_agent_status(db, session.id, last_message)

        # Delegate to A2AEventMapper for consistent status mapping
        return A2AEventMapper.map_session_status_to_task_state(
            status=status,
            interaction_status=interaction_status,
            tool_questions_status=tool_questions_status,
            turn_in_flight=turn_in_flight,
            has_pending=has_pending,
            last_agent_status=last_agent_status,
        )

    @staticmethod
    def _current_turn_agent_status(
        db: DbSession, session_id: UUID, last_message: SessionMessage | None
    ) -> str | None:
        """Status of the current turn's agent message, reusing the last message."""
        if last_message is None or last_message.role == "user":
            return None
        if last_message.role == "agent":
            return last_message.status
        agent_message = MessageService.get_last_agent_message_of_current_turn(db, session_id)
        return agent_message.status if agent_message else None

    def clear_delivery_failed(self, message_id: UUID) -> None:
        """Make a re-driven message count as pending again (see MessageService)."""
        with self.get_db_session() as db:
            MessageService.clear_delivery_failed(db, message_id)

    def get_max_user_sequence(self, session_id: UUID) -> int:
        """Newest user message sequence of the session (0 if none)."""
        with self.get_db_session() as db:
            return MessageService.get_max_user_sequence(db, session_id)

    def flag_pending_delivery_failed(self, session_id: UUID, up_to_sequence: int) -> None:
        """Flag pending rows (up to ``up_to_sequence``) left behind by a failed turn."""
        try:
            with self.get_db_session() as db:
                MessageService.flag_pending_delivery_failed(db, session_id, up_to_sequence)
        except Exception as e:  # noqa: BLE001 - best-effort; never mask the turn error
            logger.warning(f"Failed to flag undelivered messages for {session_id}: {e}")

    @staticmethod
    def is_turn_in_flight(session_id: UUID) -> bool:
        """True when this process runs a turn of the session (lock or stream)."""
        return (
            is_session_lock_held(str(session_id))
            or active_streaming_manager.is_streaming_nowait(session_id)
        )

    def get_turn_state(
        self, session_id: UUID, user_message_id: UUID | None
    ) -> TaskState | None:
        """
        State of the turn opened by ``user_message_id``.

        While no newer user message exists this is the session-level state.
        Once one does, the turn is over and its state comes from the last agent
        message of the turn: ``user_interrupted`` → canceled, ``aborted`` →
        failed, anything else (or no agent message) → completed.
        """
        if user_message_id is None:
            return self.get_state(str(session_id))
        try:
            with self.get_db_session() as db:
                user_message = db.get(SessionMessage, user_message_id)
                if user_message is None or user_message.session_id != session_id:
                    closed, agent_message = False, None
                else:
                    closed, agent_message = MessageService.get_turn_closing_agent_message(
                        db, session_id, user_message
                    )
                agent_status = agent_message.status if agent_message else None
        except Exception as e:
            logger.error(f"Error getting turn state {session_id}/{user_message_id}: {e}")
            return None

        if not closed:
            return self.get_state(str(session_id))
        if agent_status == "user_interrupted":
            return TaskState.canceled
        if agent_status == "aborted":
            return TaskState.failed
        return TaskState.completed

    def get_state(
        self, task_id: str, *, ignore_turn_in_flight: bool = False
    ) -> TaskState | None:
        """
        Get only the task state, without reading message history.

        Args:
            task_id: The task/session UUID as string
            ignore_turn_in_flight: See ``_map_status_to_state``

        Returns:
            A2A TaskState, or None if the task is not found
        """
        try:
            with self.get_db_session() as db:
                session = SessionService.get_session(db, UUID(task_id))
                if not session:
                    return None
                return self._map_status_to_state(
                    session, db, ignore_turn_in_flight=ignore_turn_in_flight,
                )
        except Exception as e:
            logger.error(f"Error getting task state {task_id}: {e}")
            return None

    def _get_message_history(self, session_id: UUID, db: DbSession) -> list[Message]:
        """
        Get message history for a session as A2A Messages.

        Uses MessageService.get_last_n_messages with a high limit to get all messages.

        Args:
            session_id: The session UUID
            db: Database session

        Returns:
            List of A2A Message objects
        """
        # Use service layer to get messages (get_last_n_messages returns in chronological order)
        messages = MessageService.get_last_n_messages(db, session_id, n=1000)

        # Delegate to A2AEventMapper for conversion
        return A2AEventMapper.convert_session_messages_to_a2a(messages, session_id)

    def get_task_with_limited_history(
        self,
        task_id: str,
        history_length: int = 10,
        live_stream: dict | None = None,
    ) -> Task | None:
        """
        Get task with limited message history.

        Args:
            task_id: The task/session UUID as string
            history_length: Maximum number of messages to include
            live_stream: In-memory buffer of the session's active stream,
                merged into the in-progress agent row

        Returns:
            A2A Task object or None if not found
        """
        try:
            with self.get_db_session() as db:
                session = SessionService.get_session(db, UUID(task_id))
                if not session:
                    return None

                # Get limited history via service layer
                # get_last_n_messages returns messages in chronological order
                messages = MessageService.get_last_n_messages(db, session.id, n=history_length)

                # Convert to A2A messages via mapper
                history = A2AEventMapper.convert_session_messages_to_a2a(
                    messages, session.id, live_stream=live_stream,
                )

                # Get state via service layer
                state = self._map_status_to_state(session, db)

                return Task(
                    id=str(session.id),
                    contextId=str(session.id),
                    status=TaskStatus(
                        state=state,
                        timestamp=session.updated_at.isoformat() + "Z" if session.updated_at else datetime.now(UTC).isoformat() + "Z",
                    ),
                    history=history if history else None,
                )
        except Exception as e:
            logger.error(f"Error getting task {task_id}: {e}")
            return None
