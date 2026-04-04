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

from app.models import Session as ChatSession
from app.services.sessions.session_service import SessionService
from app.services.sessions.message_service import MessageService
from app.services.a2a.a2a_event_mapper import A2AEventMapper

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

    def _map_status_to_state(self, session: ChatSession, db: DbSession) -> TaskState:
        """
        Map internal session status to A2A TaskState.

        Delegates to A2AEventMapper for the actual mapping logic.

        Args:
            session: Internal Session model
            db: Database session for querying last message

        Returns:
            A2A TaskState enum value
        """
        # Get last message to check for unanswered tool questions
        last_message = MessageService.get_last_message(db, session.id)
        tool_questions_status = last_message.tool_questions_status if last_message else None

        # Delegate to A2AEventMapper for consistent status mapping
        return A2AEventMapper.map_session_status_to_task_state(
            status=session.status or "",
            interaction_status=session.interaction_status or "",
            tool_questions_status=tool_questions_status,
        )

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
    ) -> Task | None:
        """
        Get task with limited message history.

        Args:
            task_id: The task/session UUID as string
            history_length: Maximum number of messages to include

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
                history = A2AEventMapper.convert_session_messages_to_a2a(messages, session.id)

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
