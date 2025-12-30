"""Event models for WebSocket-based real-time communication."""

from datetime import datetime
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

    # Streaming events
    STREAM_STARTED = "stream_started"
    STREAM_COMPLETED = "stream_completed"
    STREAM_ERROR = "stream_error"

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

    timestamp: datetime = Field(default_factory=datetime.utcnow, description="When the event was created")
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
    connected_at: datetime = Field(default_factory=datetime.utcnow)
    rooms: list[str] = Field(default_factory=list, description="Rooms the connection is subscribed to")
