"""Event models for WebSocket-based real-time communication."""

from datetime import datetime, UTC
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field
from sqlmodel import SQLModel


# Event types - can be extended as needed
class EventType:
    """Available event types for the event bus."""

    # Session events
    SESSION_CREATED = "session_created"
    SESSION_UPDATED = "session_updated"
    SESSION_DELETED = "session_deleted"

    # Message events
    MESSAGE_CREATED = "message_created"
    MESSAGE_UPDATED = "message_updated"
    MESSAGE_DELETED = "message_deleted"

    # Activity events
    ACTIVITY_CREATED = "activity_created"
    ACTIVITY_UPDATED = "activity_updated"
    ACTIVITY_DELETED = "activity_deleted"

    # Agent events
    AGENT_CREATED = "agent_created"
    AGENT_UPDATED = "agent_updated"
    AGENT_DELETED = "agent_deleted"

    # Environment events
    ENVIRONMENT_ACTIVATING = "environment_activating"
    ENVIRONMENT_ACTIVATED = "environment_activated"
    ENVIRONMENT_ACTIVATION_FAILED = "environment_activation_failed"
    ENVIRONMENT_SUSPENDED = "environment_suspended"
    ENVIRONMENT_STATUS_CHANGED = "environment_status_changed"

    # Streaming events
    STREAM_STARTED = "stream_started"
    STREAM_COMPLETED = "stream_completed"
    STREAM_ERROR = "stream_error"
    STREAM_INTERRUPTED = "stream_interrupted"
    SESSION_INTERACTION_STATUS_CHANGED = "session_interaction_status_changed"

    # Session state events
    SESSION_STATE_UPDATED = "session_state_updated"  # Agent declared session outcome

    # To-do progress events (from TodoWrite tool)
    TODO_LIST_UPDATED = "todo_list_updated"      # Session-level to-do update
    TASK_TODO_UPDATED = "task_todo_updated"      # Task-level to-do update (propagated from session)

    # Task lifecycle events
    TASK_CREATED = "task_created"
    TASK_STATUS_UPDATED = "task_status_changed"

    # Task collaboration events (new)
    TASK_COMMENT_ADDED = "task_comment_added"
    TASK_STATUS_CHANGED = "task_status_changed"   # alias for TASK_STATUS_UPDATED
    TASK_ATTACHMENT_ADDED = "task_attachment_added"
    SUBTASK_COMPLETED = "subtask_completed"
    TASK_SUBTASK_CREATED = "task_subtask_created"

    # Agent status events
    AGENT_STATUS_UPDATED = "agent_status_updated"

    # Generic notification
    NOTIFICATION = "notification"


class EventBase(SQLModel):
    """Base event model with common fields."""

    type: str = Field(description="Event type (e.g., 'session_updated', 'message_created')")
    model_id: UUID | None = Field(default=None, description="ID of the related model (session_id, message_id, etc.)")
    text_content: str | None = Field(default=None, description="Optional notification text for the user")
    meta: dict[str, Any] | None = Field(default=None, description="Additional metadata (e.g., agent_id, session_id, etc.)")


class EventPublic(EventBase):
    """Public event model sent to clients."""

    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC), description="When the event was created")
    user_id: UUID | None = Field(default=None, description="User ID for targeted events (None for broadcast)")


class EventBroadcast(BaseModel):
    """Event broadcast request model."""

    type: str = Field(description="Event type")
    model_id: UUID | None = Field(default=None, description="ID of the related model")
    text_content: str | None = Field(default=None, description="Optional notification text")
    meta: dict[str, Any] | None = Field(default=None, description="Additional metadata")
    user_id: UUID | None = Field(default=None, description="Target user ID (None for broadcast)")
    room: str | None = Field(default=None, description="Room name for targeted broadcast (e.g., 'user_{user_id}')")


class ConnectionInfo(BaseModel):
    """WebSocket connection information."""

    sid: str = Field(description="Socket.IO session ID")
    user_id: UUID = Field(description="Authenticated user ID")
    connected_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    rooms: list[str] = Field(default_factory=list, description="Rooms the connection is subscribed to")
