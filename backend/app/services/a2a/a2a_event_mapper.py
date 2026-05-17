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


# Vendor-namespaced keys placed on each TextPart's metadata so A2A clients
# can distinguish the agent's final text, chain-of-thought, and tool-call
# events (all of which otherwise look like generic "agent" text).
CONTENT_KIND_KEY = "cinna.content_kind"
TOOL_NAME_KEY = "cinna.tool_name"
# Structured tool arguments (object) — surfaced on each tool TextPart so
# clients can render the call without parsing the narration text.
TOOL_INPUT_KEY = "cinna.tool_input"
# Opaque tool-call identifier — pairs each tool-call TextPart with its later
# tool-result event and cross-references the persisted streaming-event trace.
TOOL_ID_KEY = "cinna.tool_id"

CONTENT_KIND_TEXT = "text"
CONTENT_KIND_THINKING = "thinking"
CONTENT_KIND_TOOL = "tool"

_STREAM_EVENT_TO_CONTENT_KIND = {
    "assistant": CONTENT_KIND_TEXT,
    "thinking": CONTENT_KIND_THINKING,
    "tool": CONTENT_KIND_TOOL,
}


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
            content = event.get("content", "")
            if content:
                return A2AEventMapper._create_status_update(
                    task_id=task_id,
                    context_id=context_id,
                    state=TaskState.working,
                    final=False,
                    message=content,
                    part_metadata={CONTENT_KIND_KEY: CONTENT_KIND_TEXT},
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
            tool_name = event.get("tool_name", "")
            content = event.get("content", "")
            event_metadata = event.get("metadata") or {}
            tool_input = event_metadata.get("tool_input")
            tool_id = event_metadata.get("tool_id")
            if tool_name or content:
                metadata: dict[str, Any] = {CONTENT_KIND_KEY: CONTENT_KIND_TOOL}
                if tool_name:
                    metadata[TOOL_NAME_KEY] = tool_name
                if isinstance(tool_input, dict):
                    metadata[TOOL_INPUT_KEY] = tool_input
                if isinstance(tool_id, str) and tool_id:
                    metadata[TOOL_ID_KEY] = tool_id
                return A2AEventMapper._create_status_update(
                    task_id=task_id,
                    context_id=context_id,
                    state=TaskState.working,
                    final=False,
                    message=content,
                    part_metadata=metadata,
                )
            return None

        elif event_type == "thinking":
            content = event.get("content", "")
            if content:
                return A2AEventMapper._create_status_update(
                    task_id=task_id,
                    context_id=context_id,
                    state=TaskState.working,
                    final=False,
                    message=content,
                    part_metadata={CONTENT_KIND_KEY: CONTENT_KIND_THINKING},
                )
            return None

        elif event_type == "done":
            # Final event from MessageService - map to completed or canceled based on metadata
            metadata = event.get("metadata") or {}
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
        part_metadata: dict[str, Any] | None = None,
    ) -> dict:
        """Create a TaskStatusUpdateEvent dict.

        ``part_metadata``, when provided, is attached to the embedded
        TextPart. This is how streaming events signal content kind
        (answer/thinking/tool) to A2A clients.
        """
        status = TaskStatus(
            state=state,
            timestamp=datetime.now(UTC).isoformat() + "Z",
        )
        if message:
            text_part = TextPart(text=message, metadata=part_metadata) if part_metadata else TextPart(text=message)
            status.message = Message(
                messageId=uuid4().hex,
                role="agent",
                parts=[Part(root=text_part)],
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

        For agent messages with a persisted ``streaming_events`` trace in
        ``message_metadata``, the events are expanded into one TextPart per
        event, each carrying ``cinna.content_kind`` on its metadata so A2A
        clients can distinguish final answer text from chain-of-thought and
        tool-call narration. Messages without a trace fall back to a single
        TextPart built from ``msg.content``.

        Args:
            messages: List of SessionMessage objects
            session_id: The session UUID (used as taskId and contextId)

        Returns:
            List of A2A Message objects
        """
        history: list[Message] = []
        for msg in messages:
            # Map role: user -> user, agent/system -> agent
            role = "user" if msg.role == "user" else "agent"

            parts = A2AEventMapper._build_parts_for_session_message(msg, role)

            a2a_message = Message(
                messageId=str(msg.id),
                role=role,
                parts=parts,
                taskId=str(session_id),
                contextId=str(session_id),
            )
            history.append(a2a_message)

        return history

    @staticmethod
    def _build_parts_for_session_message(
        msg: SessionMessage,
        role: str,
    ) -> list[Part]:
        """Build A2A Parts for a stored SessionMessage.

        Agent messages with a recorded streaming-event trace become multiple
        TextParts (one per assistant/thinking/tool event) carrying content-kind
        metadata. All other cases produce a single TextPart from ``msg.content``.
        """
        fallback = [Part(root=TextPart(text=msg.content or ""))]

        if role != "agent":
            return fallback

        metadata = msg.message_metadata or {}
        streaming_events = metadata.get("streaming_events") or []
        if not streaming_events:
            return fallback

        parts: list[Part] = []
        for evt in streaming_events:
            evt_type = evt.get("type")
            content_kind = _STREAM_EVENT_TO_CONTENT_KIND.get(evt_type)
            if content_kind is None:
                continue
            content = evt.get("content") or ""
            if not content:
                continue

            part_metadata: dict[str, Any] = {CONTENT_KIND_KEY: content_kind}
            if content_kind == CONTENT_KIND_TOOL:
                tool_name = evt.get("tool_name")
                if tool_name:
                    part_metadata[TOOL_NAME_KEY] = tool_name
                evt_meta = evt.get("metadata") or {}
                evt_tool_input = evt_meta.get("tool_input")
                if isinstance(evt_tool_input, dict):
                    part_metadata[TOOL_INPUT_KEY] = evt_tool_input
                evt_tool_id = evt_meta.get("tool_id")
                if isinstance(evt_tool_id, str) and evt_tool_id:
                    part_metadata[TOOL_ID_KEY] = evt_tool_id

            parts.append(Part(root=TextPart(text=content, metadata=part_metadata)))

        return parts or fallback
