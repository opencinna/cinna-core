"""
A2A Event Mapper - transforms internal streaming events to A2A format.

This module provides utilities for mapping internal streaming events
(from MessageService) to A2A protocol event format for SSE streaming.
Each emitted TextPart carries vendor-namespaced metadata identifying
its content kind — agent-generated (``text``, ``thinking``, ``tool``,
``tool_result``) or platform-generated (``notice``, ``command_result``).

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
    FilePart,
    FileWithUri,
)

from app.core.config import settings
from app.models import SessionMessage
from app.services.environments.agent_workspace_token_service import (
    AgentWorkspaceTokenService,
)

logger = logging.getLogger(__name__)


# Vendor-namespaced keys placed on each TextPart's metadata so A2A clients
# can distinguish the agent's final text, chain-of-thought, tool-call
# events, tool-result chunks, and platform-emitted parts (notices and
# synchronous slash-command output) — all of which otherwise look like
# generic "agent" text.
CONTENT_KIND_KEY = "cinna.content_kind"
# Per-message state of an agent row in history: complete | streaming |
# aborted | canceled.
MESSAGE_STATE_KEY = "cinna.message_state"
# The caller-supplied A2A ``messageId`` echoed on the stored user row.
CLIENT_MESSAGE_ID_KEY = "cinna.client_message_id"
TOOL_NAME_KEY = "cinna.tool_name"
# Structured tool arguments (object) — surfaced on each tool TextPart so
# clients can render the call without parsing the narration text.
TOOL_INPUT_KEY = "cinna.tool_input"
# Opaque tool-call identifier — pairs each tool-call TextPart with its later
# tool-result event and cross-references the persisted streaming-event trace.
TOOL_ID_KEY = "cinna.tool_id"
# Per-chunk stream discriminator for tool_result content (stdout vs stderr).
TOOL_STREAM_KEY = "cinna.tool_stream"
# Verbatim slash-command invocation string (e.g. "/files", "/run:check").
# Present on TextParts produced by platform-emitted command flows — both the
# synchronous backend-only command terminal status part and the synthesized
# tool / tool_result parts that wrap a /run:* execution in agent-env. The
# field's *absence* is the signal that the part originated from an LLM tool
# call, mirroring the existing TOOL_ID_KEY pattern.
COMMAND_INVOCATION_KEY = "cinna.command_invocation"

CONTENT_KIND_TEXT = "text"
CONTENT_KIND_THINKING = "thinking"
CONTENT_KIND_TOOL = "tool"
CONTENT_KIND_TOOL_RESULT = "tool_result"
CONTENT_KIND_NOTICE = "notice"
CONTENT_KIND_COMMAND_RESULT = "command_result"
# Agent-authored file attachment delivered as a native A2A FilePart.
CONTENT_KIND_FILE = "file"

# Metadata keys carried on an attachment FilePart (vendor-namespaced), so A2A
# clients can render the file without parsing the surrounding narration.
FILE_ID_KEY = "cinna.file_id"
FILE_NAME_KEY = "cinna.file_name"
FILE_MIME_KEY = "cinna.file_mime"
FILE_SIZE_KEY = "cinna.file_size"

# Contents the stream stores for an agent row with no assistant text: the
# finalize placeholder, and the in-progress one a crashed or canceled row keeps
# (``MessageService.stream_message_with_events``).
AGENT_RESPONSE_PLACEHOLDER = "Agent response"
_NO_TEXT_PLACEHOLDERS = frozenset({AGENT_RESPONSE_PLACEHOLDER, "Agent is responding..."})

# Allowed values for cinna.tool_stream
TOOL_STREAM_STDOUT = "stdout"
TOOL_STREAM_STDERR = "stderr"

_STREAM_EVENT_TO_CONTENT_KIND = {
    "assistant": CONTENT_KIND_TEXT,
    "thinking": CONTENT_KIND_THINKING,
    "tool": CONTENT_KIND_TOOL,
    "tool_result_delta": CONTENT_KIND_TOOL_RESULT,
    "attachment": CONTENT_KIND_FILE,
}


def _build_file_download_uri(file_id: str, session_id: str) -> str:
    """Build an absolute, signed download URL for an attachment FilePart.

    Embeds a short-lived ``file_download`` token so native A2A clients can fetch
    the file without a regular session JWT. The base host is the public frontend
    host (same approach as the /files command link builder).
    """
    try:
        token = AgentWorkspaceTokenService.create_file_download_token(
            file_id=UUID(file_id),
            session_id=UUID(session_id),
        )
    except (ValueError, TypeError):
        token = ""
    # Backend origin, not FRONTEND_HOST: this link is sent to a remote agent
    # and must resolve to the API host, which is a different origin whenever
    # the SPA and API are served separately.
    base = settings.backend_base_url
    suffix = f"?token={token}" if token else ""
    return f"{base}{settings.API_V1_STR}/files/{file_id}/download{suffix}"


def _build_attachment_file_part(
    event_meta: dict,
    session_id: str,
) -> Part | None:
    """Build an A2A FilePart for a stored/streamed ``attachment`` event.

    Returns None when the event has no usable file_id. Content-kind metadata is
    placed on the part (never on the Message), consistent with the TextPart rule.
    """
    file_id = event_meta.get("file_id")
    if not file_id:
        return None
    filename = event_meta.get("filename")
    mime_type = event_meta.get("mime_type")
    size = event_meta.get("size")

    part_metadata: dict[str, Any] = {
        CONTENT_KIND_KEY: CONTENT_KIND_FILE,
        FILE_ID_KEY: file_id,
    }
    if filename:
        part_metadata[FILE_NAME_KEY] = filename
    if mime_type:
        part_metadata[FILE_MIME_KEY] = mime_type
    if isinstance(size, int):
        part_metadata[FILE_SIZE_KEY] = size

    file_with_uri = FileWithUri(
        uri=_build_file_download_uri(file_id, session_id),
        name=filename,
        mime_type=mime_type,
    )
    return Part(root=FilePart(file=file_with_uri, metadata=part_metadata))


TERMINAL_TASK_STATES: frozenset[TaskState] = frozenset({
    TaskState.completed,
    TaskState.failed,
    TaskState.canceled,
    TaskState.rejected,
})


def _parts_from_stream_events(streaming_events: list[dict], session_id: str) -> list[Part]:
    """One Part per assistant/thinking/tool/tool-result/attachment event, with content-kind metadata."""
    parts: list[Part] = []
    for evt in streaming_events:
        evt_type = evt.get("type")
        content_kind = _STREAM_EVENT_TO_CONTENT_KIND.get(evt_type)
        if content_kind is None:
            continue

        # Agent attachment → native FilePart (no text content required).
        if content_kind == CONTENT_KIND_FILE:
            file_part = _build_attachment_file_part(
                evt.get("metadata") or {}, session_id
            )
            if file_part is not None:
                parts.append(file_part)
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
            evt_command_invocation = evt_meta.get("command_invocation")
            if isinstance(evt_command_invocation, str) and evt_command_invocation:
                part_metadata[COMMAND_INVOCATION_KEY] = evt_command_invocation
        elif content_kind == CONTENT_KIND_TOOL_RESULT:
            evt_meta = evt.get("metadata") or {}
            evt_tool_id = evt_meta.get("tool_id")
            if isinstance(evt_tool_id, str) and evt_tool_id:
                part_metadata[TOOL_ID_KEY] = evt_tool_id
            evt_stream = evt_meta.get("stream")
            if evt_stream not in (TOOL_STREAM_STDOUT, TOOL_STREAM_STDERR):
                evt_stream = TOOL_STREAM_STDOUT
            part_metadata[TOOL_STREAM_KEY] = evt_stream
            evt_command_invocation = evt_meta.get("command_invocation")
            if isinstance(evt_command_invocation, str) and evt_command_invocation:
                part_metadata[COMMAND_INVOCATION_KEY] = evt_command_invocation

        parts.append(Part(root=TextPart(text=content, metadata=part_metadata)))

    return parts


def _question_events(streaming_events: list[dict]) -> list[dict]:
    """The ask-user tool calls of a turn (see ``MessageService.detect_ask_user_question_tool``)."""
    from app.services.sessions.message_service import MessageService

    return [
        evt for evt in streaming_events
        if MessageService.detect_ask_user_question_tool([evt])
    ]


def _message_state(status: str | None, streaming_in_progress: bool) -> str:
    """The ``cinna.message_state`` value of an agent row."""
    if status == "aborted":
        return "aborted"
    if status == "user_interrupted":
        return "canceled"
    if streaming_in_progress:
        return "streaming"
    return "complete"


def is_terminal_task_state(state: TaskState) -> bool:
    """True when the task can make no further progress."""
    return state in TERMINAL_TASK_STATES


def is_final_task_state(state: TaskState) -> bool:
    """True when a stream ends on ``state``: terminal, or waiting on the caller."""
    return is_terminal_task_state(state) or state == TaskState.input_required


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
            return A2AEventMapper.create_status_update(
                task_id=task_id,
                context_id=context_id,
                state=TaskState.working,
                final=False,
            )

        elif event_type == "assistant":
            content = event.get("content", "")
            if content:
                return A2AEventMapper.create_status_update(
                    task_id=task_id,
                    context_id=context_id,
                    state=TaskState.working,
                    final=False,
                    message=content,
                    part_metadata={CONTENT_KIND_KEY: CONTENT_KIND_TEXT},
                )
            return None

        elif event_type == "stream_completed":
            return A2AEventMapper.create_status_update(
                task_id=task_id,
                context_id=context_id,
                state=TaskState.completed,
                final=True,
            )

        elif event_type == "error":
            error_message = event.get("content", "An error occurred")
            return A2AEventMapper.create_status_update(
                task_id=task_id,
                context_id=context_id,
                state=TaskState.failed,
                final=True,
                message=error_message,
            )

        elif event_type == "interrupted":
            return A2AEventMapper.create_status_update(
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
            command_invocation = event_metadata.get("command_invocation")
            if tool_name or content:
                metadata: dict[str, Any] = {CONTENT_KIND_KEY: CONTENT_KIND_TOOL}
                if tool_name:
                    metadata[TOOL_NAME_KEY] = tool_name
                if isinstance(tool_input, dict):
                    metadata[TOOL_INPUT_KEY] = tool_input
                if isinstance(tool_id, str) and tool_id:
                    metadata[TOOL_ID_KEY] = tool_id
                if isinstance(command_invocation, str) and command_invocation:
                    metadata[COMMAND_INVOCATION_KEY] = command_invocation
                return A2AEventMapper.create_status_update(
                    task_id=task_id,
                    context_id=context_id,
                    state=TaskState.working,
                    final=False,
                    message=content,
                    part_metadata=metadata,
                )
            return None

        elif event_type == "tool_result_delta":
            content = event.get("content", "")
            if not content:
                return None
            event_metadata = event.get("metadata") or {}
            tool_id = event_metadata.get("tool_id")
            tool_stream = event_metadata.get("stream")
            command_invocation = event_metadata.get("command_invocation")
            # Defensive: clients may switch on this value; coerce unknowns.
            if tool_stream not in (TOOL_STREAM_STDOUT, TOOL_STREAM_STDERR):
                tool_stream = TOOL_STREAM_STDOUT
            metadata: dict[str, Any] = {
                CONTENT_KIND_KEY: CONTENT_KIND_TOOL_RESULT,
                TOOL_STREAM_KEY: tool_stream,
            }
            if isinstance(tool_id, str) and tool_id:
                metadata[TOOL_ID_KEY] = tool_id
            if isinstance(command_invocation, str) and command_invocation:
                metadata[COMMAND_INVOCATION_KEY] = command_invocation
            return A2AEventMapper.create_status_update(
                task_id=task_id,
                context_id=context_id,
                state=TaskState.working,
                final=False,
                message=content,
                part_metadata=metadata,
            )

        elif event_type == "thinking":
            content = event.get("content", "")
            if content:
                return A2AEventMapper.create_status_update(
                    task_id=task_id,
                    context_id=context_id,
                    state=TaskState.working,
                    final=False,
                    message=content,
                    part_metadata={CONTENT_KIND_KEY: CONTENT_KIND_THINKING},
                )
            return None

        elif event_type == "attachment":
            # Live agent attachment — deliver as a FilePart on a working status
            # update so streaming A2A clients see the file as it is finalised.
            event_meta = event.get("metadata") or {}
            file_part = _build_attachment_file_part(event_meta, task_id)
            if file_part is None:
                return None
            return A2AEventMapper.create_file_status_update(
                task_id=task_id,
                context_id=context_id,
                state=TaskState.working,
                final=False,
                file_part=file_part,
            )

        elif event_type == "done":
            # Final event from MessageService - map to completed or canceled based on metadata
            metadata = event.get("metadata") or {}
            was_interrupted = metadata.get("interrupted", False)
            if was_interrupted:
                return A2AEventMapper.create_status_update(
                    task_id=task_id,
                    context_id=context_id,
                    state=TaskState.canceled,
                    final=True,
                )
            # A turn that asked through the ask-user tool waits on the caller,
            # as ``tasks/get`` reports it; the final event carries the question.
            question_events = _question_events(metadata.get("streaming_events") or [])
            if question_events:
                return A2AEventMapper.create_parts_status_update(
                    task_id=task_id,
                    context_id=context_id,
                    state=TaskState.input_required,
                    final=True,
                    parts=_parts_from_stream_events(question_events, task_id),
                )
            return A2AEventMapper.create_status_update(
                task_id=task_id,
                context_id=context_id,
                state=TaskState.completed,
                final=True,
            )

        # Skip other event types (result, session_created, etc.)
        return None

    @staticmethod
    def create_notice_event(
        task_id: str,
        context_id: str,
        message: str,
    ) -> dict:
        """Build a non-final ``working`` status update carrying a
        platform-emitted notice (``cinna.content_kind = "notice"``).

        Used for ephemeral, informational hints surfaced by the platform
        itself (not by the agent) — e.g. the environment-activation
        warm-up message yielded before the agent stream begins.
        """
        return A2AEventMapper.create_status_update(
            task_id=task_id,
            context_id=context_id,
            state=TaskState.working,
            final=False,
            message=message,
            part_metadata={CONTENT_KIND_KEY: CONTENT_KIND_NOTICE},
        )

    @staticmethod
    def create_command_result_event(
        task_id: str,
        context_id: str,
        message: str,
        command_invocation: str | None = None,
    ) -> dict:
        """Build the terminal ``completed`` status update for a
        synchronous platform slash command (``cinna.content_kind =
        "command_result"``).

        Used on the A2A ``command_executed`` branch — the agent stream
        does not run in this case; the command's output is delivered as
        the final status event's message.

        When ``command_invocation`` is a non-empty string, the verbatim
        slash-command invocation (e.g. ``"/files"``) is forwarded on the
        part metadata under ``COMMAND_INVOCATION_KEY`` so clients can use
        a single uniform key across the synchronous and ``/run:*``
        command flows.
        """
        part_metadata: dict[str, Any] = {CONTENT_KIND_KEY: CONTENT_KIND_COMMAND_RESULT}
        if isinstance(command_invocation, str) and command_invocation:
            part_metadata[COMMAND_INVOCATION_KEY] = command_invocation
        return A2AEventMapper.create_status_update(
            task_id=task_id,
            context_id=context_id,
            state=TaskState.completed,
            final=True,
            message=message,
            part_metadata=part_metadata,
        )

    @staticmethod
    def create_status_update(
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
    def create_parts_status_update(
        task_id: str,
        context_id: str,
        state: TaskState,
        final: bool,
        parts: list[Part],
    ) -> dict:
        """Create a TaskStatusUpdateEvent whose message carries ``parts`` (none → no message)."""
        status = TaskStatus(
            state=state,
            timestamp=datetime.now(UTC).isoformat() + "Z",
        )
        if parts:
            status.message = Message(
                messageId=uuid4().hex,
                role="agent",
                parts=parts,
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
    def create_file_status_update(
        task_id: str,
        context_id: str,
        state: TaskState,
        final: bool,
        file_part: Part,
    ) -> dict:
        """Create a TaskStatusUpdateEvent whose message carries a single FilePart.

        Used to deliver a live agent attachment to streaming A2A clients. The
        content-kind metadata lives on the part (set by ``_build_attachment_file_part``),
        never on the Message.
        """
        status = TaskStatus(
            state=state,
            timestamp=datetime.now(UTC).isoformat() + "Z",
        )
        status.message = Message(
            messageId=uuid4().hex,
            role="agent",
            parts=[file_part],
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
        *,
        turn_in_flight: bool = False,
        has_pending: bool = False,
        last_agent_status: str | None = None,
    ) -> TaskState:
        """
        Map internal session state to A2A TaskState.

        Rules are evaluated in order; the first match wins.

        Args:
            status: Session status (active, completed, error, paused)
            interaction_status: Session interaction status (running, pending_stream, "")
            tool_questions_status: Last message tool_questions_status (unanswered, answered, null)
            turn_in_flight: A turn of this session is running in this process
                (session lock held or a stream registered)
            has_pending: Undelivered user messages exist
            last_agent_status: ``status`` of the agent message of the current
                turn (``None`` when the turn has no agent message yet)

        Returns:
            A2A TaskState enum value
        """
        if tool_questions_status == "unanswered":
            return TaskState.input_required

        if interaction_status == "running":
            return TaskState.working
        if interaction_status == "pending_stream":
            return TaskState.submitted

        if turn_in_flight or has_pending:
            return TaskState.working

        if status == "error":
            return TaskState.failed

        if last_agent_status == "user_interrupted":
            return TaskState.canceled
        if last_agent_status == "aborted":
            return TaskState.failed

        if status in ("active", "completed"):
            return TaskState.completed

        # Unknown statuses keep the conservative answer.
        return TaskState.working

    @staticmethod
    def convert_session_messages_to_a2a(
        messages: list[SessionMessage],
        session_id: UUID,
        live_stream: dict | None = None,
    ) -> list[Message]:
        """
        Convert a list of SessionMessage objects to A2A Message format.

        For agent messages with a persisted ``streaming_events`` trace in
        ``message_metadata``, the events are expanded into one TextPart per
        event, each carrying ``cinna.content_kind`` on its metadata so A2A
        clients can distinguish final answer text from chain-of-thought and
        tool-call narration. Messages without a trace fall back to a single
        TextPart built from ``msg.content``.

        Agent rows carry ``cinna.message_state`` in their metadata; user rows
        that were sent with a client ``messageId`` echo it as
        ``cinna.client_message_id``.

        Args:
            messages: List of SessionMessage objects
            session_id: The session UUID (used as taskId and contextId)
            live_stream: In-memory buffer of the session's active stream
                (``active_streaming_manager.get_stream_events``). Its events
                are merged into the in-progress agent row, on copies.

        Returns:
            List of A2A Message objects
        """
        from app.services.sessions.message_service import merge_live_events

        live_events = (live_stream or {}).get("streaming_events") or []
        history: list[Message] = []
        for msg in messages:
            # Map role: user -> user, agent/system -> agent
            role = "user" if msg.role == "user" else "agent"
            stored_metadata = msg.message_metadata or {}
            in_progress = bool(stored_metadata.get("streaming_in_progress"))

            events_override = None
            if msg.role == "agent" and in_progress and live_events:
                # Copies only: the ORM row must never be mutated here, the
                # caller's DB session would flush it.
                events_override = merge_live_events(
                    list(stored_metadata.get("streaming_events") or []),
                    list(live_events),
                )

            parts = A2AEventMapper._build_parts_for_session_message(
                msg, role, events_override=events_override,
            )

            message_metadata: dict[str, Any] = {}
            if msg.role == "agent":
                message_metadata[MESSAGE_STATE_KEY] = _message_state(msg.status, in_progress)
            elif msg.role == "user":
                client_message_id = stored_metadata.get("client_message_id")
                if client_message_id:
                    message_metadata[CLIENT_MESSAGE_ID_KEY] = client_message_id

            a2a_message = Message(
                messageId=str(msg.id),
                role=role,
                parts=parts,
                taskId=str(session_id),
                contextId=str(session_id),
                metadata=message_metadata or None,
            )
            history.append(a2a_message)

        return history

    @staticmethod
    def _build_parts_for_session_message(
        msg: SessionMessage,
        role: str,
        events_override: list[dict] | None = None,
    ) -> list[Part]:
        """Build A2A Parts for a stored SessionMessage.

        Agent messages with a recorded streaming-event trace become multiple
        TextParts (one per assistant/thinking/tool event) carrying content-kind
        metadata. All other cases produce a single TextPart from ``msg.content``,
        except that a canceled / aborted agent row never shows a no-text
        placeholder: its fallback part is then empty.
        """
        text = msg.content or ""
        if (
            role == "agent"
            and msg.status in ("user_interrupted", "aborted")
            and text in _NO_TEXT_PLACEHOLDERS
        ):
            text = ""
        fallback = [Part(root=TextPart(text=text))]

        if role != "agent":
            return fallback

        metadata = msg.message_metadata or {}
        if events_override is not None:
            streaming_events = events_override
        else:
            streaming_events = metadata.get("streaming_events") or []
        if not streaming_events:
            return fallback

        parts = _parts_from_stream_events(streaming_events, str(msg.session_id))
        return parts or fallback
