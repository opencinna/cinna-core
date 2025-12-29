import uuid
from datetime import datetime
from sqlmodel import SQLModel, Field


class Activity(SQLModel, table=True):
    """
    Activity/Notification log - tracks system events and agent actions.

    Examples:
    - Session completed
    - Agent asked questions (action_required = "answers_required")
    - File created
    - Agent notification
    """
    __tablename__ = "activity"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: uuid.UUID = Field(foreign_key="user.id", ondelete="CASCADE")
    session_id: uuid.UUID | None = Field(default=None, foreign_key="session.id", ondelete="CASCADE")
    agent_id: uuid.UUID | None = Field(default=None, foreign_key="agent.id", ondelete="SET NULL")

    # Activity type: "session_completed", "file_created", "agent_notification", "questions_asked", etc.
    activity_type: str

    # Human-readable activity text
    text: str

    # Action required: "" (empty) or "answers_required"
    action_required: str = Field(default="")

    # Read status
    is_read: bool = Field(default=False)

    created_at: datetime = Field(default_factory=datetime.utcnow)


# Pydantic Schemas
class ActivityCreate(SQLModel):
    session_id: uuid.UUID | None = None
    agent_id: uuid.UUID | None = None
    activity_type: str
    text: str
    action_required: str = ""
    is_read: bool = False


class ActivityUpdate(SQLModel):
    is_read: bool | None = None


class ActivityPublic(SQLModel):
    id: uuid.UUID
    user_id: uuid.UUID
    session_id: uuid.UUID | None
    agent_id: uuid.UUID | None
    activity_type: str
    text: str
    action_required: str
    is_read: bool
    created_at: datetime


class ActivityPublicExtended(ActivityPublic):
    """Activity with extended data (agent name, session title, etc.)"""
    agent_name: str | None = None
    agent_ui_color_preset: str | None = None
    session_title: str | None = None


class ActivitiesPublic(SQLModel):
    data: list[ActivityPublic]
    count: int


class ActivitiesPublicExtended(SQLModel):
    data: list[ActivityPublicExtended]
    count: int


class ActivityStats(SQLModel):
    """Statistics about activities (unread count, action required count)"""
    unread_count: int
    action_required_count: int
