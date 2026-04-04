"""
A2A Event Mapper - transforms internal streaming events to A2A format.

This module provides utilities for mapping internal streaming events
(from MessageService) to A2A protocol event format for SSE streaming.

All A2A protocol mapping logic is centralized here.
"""
import logging
from typing import Any
from uuid import UUID, uuid4
from datetime import UTC, datetime

from a2a.types import (
    TaskState,
    TaskStatus,
    TaskStatusUpdateEvent,
    Message,
    Part,
    TextPart,
)

from app.models import SessionMessage

logger = logging.getLogger(__name__)


class A2AEventMapper:
    """Maps internal streaming events to A2A protocol events."""

    @staticmethod
    def map_stream_event(
        event: dict[str, Any],
        task_id: str,
        context_id: str,
    ) -> dict | None:
        """
        Map an internal streaming event to A2A event format.

        Args:
            event: Internal event dict with 'type', 'content', etc.
            task_id: The A2A task ID (session ID)
            context_id: The A2A context ID (same as task_id for Phase 1)

        Returns:
            A2A event dict or None if event should be skipped
        """
        event_type = event.get("type")

        if event_type == "stream_started":
            return A2AEventMapper._create_status_update(
                task_id=task_id,
                context_id=context_id,
                state=TaskState.working,
                final=False,
            )

        elif event_type == "assistant":
            # Send assistant content as status update with embedded message
            # (A2A streaming doesn't support standalone Message events)
            content = event.get("content", "")
            if content:
                return A2AEventMapper._create_status_update(
                    task_id=task_id,
                    context_id=context_id,
                    state=TaskState.working,
                    final=False,
                    message=content,
                )
            return None

        elif event_type == "stream_completed":
            return A2AEventMapper._create_status_update(
                task_id=task_id,
                context_id=context_id,
                state=TaskState.completed,
                final=True,
            )

        elif event_type == "error":
            error_message = event.get("content", "An error occurred")
            return A2AEventMapper._create_status_update(
                task_id=task_id,
                context_id=context_id,
                state=TaskState.failed,
                final=True,
                message=error_message,
            )

        elif event_type == "interrupted":
            return A2AEventMapper._create_status_update(
                task_id=task_id,
                context_id=context_id,
                state=TaskState.canceled,
                final=True,
            )

        elif event_type == "tool":
            # Tool events as status updates (A2A streaming doesn't support standalone Message events)
            tool_name = event.get("tool_name", "")
            content = event.get("content", "")
            if tool_name or content:
                tool_content = f"[Tool: {tool_name}] {content}" if tool_name else content
                return A2AEventMapper._create_status_update(
                    task_id=task_id,
                    context_id=context_id,
                    state=TaskState.working,
                    final=False,
                    message=tool_content,
                )
            return None

        elif event_type == "thinking":
            # Thinking events as status updates (A2A streaming doesn't support standalone Message events)
            content = event.get("content", "")
            if content:
                return A2AEventMapper._create_status_update(
                    task_id=task_id,
                    context_id=context_id,
                    state=TaskState.working,
                    final=False,
                    message=content,
                )
            return None

        elif event_type == "done":
            # Final event from MessageService - map to completed or canceled based on metadata
            metadata = event.get("metadata", {})
            was_interrupted = metadata.get("interrupted", False)
            if was_interrupted:
                return A2AEventMapper._create_status_update(
                    task_id=task_id,
                    context_id=context_id,
                    state=TaskState.canceled,
                    final=True,
                )
            return A2AEventMapper._create_status_update(
                task_id=task_id,
                context_id=context_id,
                state=TaskState.completed,
                final=True,
            )

        # Skip other event types (result, session_created, etc.)
        return None

    @staticmethod
    def _create_status_update(
        task_id: str,
        context_id: str,
        state: TaskState,
        final: bool,
        message: str | None = None,
    ) -> dict:
        """Create a TaskStatusUpdateEvent dict."""
        status = TaskStatus(
            state=state,
            timestamp=datetime.now(UTC).isoformat() + "Z",
        )
        if message:
            status.message = Message(
                messageId=uuid4().hex,
                role="agent",
                parts=[Part(root=TextPart(text=message))],
            )

        event = TaskStatusUpdateEvent(
            taskId=task_id,
            contextId=context_id,
            status=status,
            final=final,
        )
        return {
            "kind": "status-update",
            **event.model_dump(by_alias=True, exclude_none=True),
        }

    @staticmethod
    def _create_message_event(
        role: str,
        content: str,
        task_id: str,
        context_id: str,
        metadata: dict | None = None,
    ) -> dict:
        """Create a Message event dict."""
        message = Message(
            messageId=uuid4().hex,
            role=role,
            parts=[Part(root=TextPart(text=content))],
            taskId=task_id,
            contextId=context_id,
            metadata=metadata,
        )
        return {
            "kind": "message",
            **message.model_dump(by_alias=True, exclude_none=True),
        }

    @staticmethod
    def map_session_status_to_task_state(
        status: str,
        interaction_status: str,
        tool_questions_status: str | None = None,
    ) -> TaskState:
        """
        Map internal session status to A2A TaskState.

        Args:
            status: Session status (active, completed, error, paused)
            interaction_status: Session interaction status (running, pending_stream, "")
            tool_questions_status: Last message tool_questions_status (unanswered, answered, null)

        Returns:
            A2A TaskState enum value
        """
        # Check for input required (tool questions)
        if tool_questions_status == "unanswered":
            return TaskState.input_required

        # Map interaction_status
        if interaction_status == "running":
            return TaskState.working
        elif interaction_status == "pending_stream":
            return TaskState.submitted

        # Map session status
        if status == "completed":
            return TaskState.completed
        elif status == "error":
            return TaskState.failed

        # Default to working for active sessions
        return TaskState.working

    @staticmethod
    def convert_session_messages_to_a2a(
        messages: list[SessionMessage],
        session_id: UUID,
    ) -> list[Message]:
        """
        Convert a list of SessionMessage objects to A2A Message format.

        Args:
            messages: List of SessionMessage objects
            session_id: The session UUID (used as taskId and contextId)

        Returns:
            List of A2A Message objects
        """
        history = []
        for msg in messages:
            # Map role: user -> user, agent/system -> agent
            role = "user" if msg.role == "user" else "agent"

            # Create A2A Message
            a2a_message = Message(
                messageId=str(msg.id),
                role=role,
                parts=[Part(root=TextPart(text=msg.content or ""))],
                taskId=str(session_id),
                contextId=str(session_id),
            )
            history.append(a2a_message)

        return history
