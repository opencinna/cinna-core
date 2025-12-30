import uuid
from datetime import datetime
from sqlmodel import Field, Relationship, SQLModel


class AgentSchedule(SQLModel, table=True):
    """
    Agent execution schedule configuration.

    Relationship: Many AgentSchedule → One Agent
    (An agent can have multiple schedules, though initially only one will be used)
    """
    __tablename__ = "agent_schedule"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    agent_id: uuid.UUID = Field(foreign_key="agent.id", ondelete="CASCADE")

    # Schedule configuration
    cron_string: str  # CRON expression in UTC (e.g., "0 6 * * 1-5")
    timezone: str  # User's IANA timezone (e.g., "Europe/Berlin")
    description: str  # Human-readable description from AI
    enabled: bool = Field(default=True)  # Allow disabling without deleting

    # Execution tracking
    last_execution: datetime | None = Field(default=None)  # Last run timestamp
    next_execution: datetime  # Calculated next run timestamp

    # Timestamps
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    # Relationship
    agent: "Agent" = Relationship(back_populates="schedules")


class ScheduleRequest(SQLModel):
    """Request to generate schedule from natural language."""
    natural_language: str
    timezone: str


class ScheduleResponse(SQLModel):
    """Response from AI schedule generation."""
    success: bool
    description: str | None = None  # Human-readable explanation
    cron_string: str | None = None  # CRON expression in UTC
    next_execution: str | None = None  # ISO 8601 timestamp (calculated by backend)
    error: str | None = None  # Error message if failed


class SaveScheduleRequest(SQLModel):
    """Request to save schedule configuration."""
    cron_string: str
    timezone: str
    description: str
    enabled: bool = True


class AgentSchedulePublic(SQLModel):
    """Public response model for AgentSchedule."""
    id: uuid.UUID
    agent_id: uuid.UUID
    cron_string: str
    timezone: str
    description: str
    enabled: bool
    last_execution: datetime | None
    next_execution: datetime
    created_at: datetime
    updated_at: datetime