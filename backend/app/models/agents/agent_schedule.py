import uuid
from datetime import datetime, UTC
from sqlalchemy import Text
from sqlmodel import Field, Relationship, SQLModel


class AgentSchedule(SQLModel, table=True):
    """
    Agent execution schedule configuration.

    Relationship: Many AgentSchedule → One Agent
    (An agent can have multiple schedules with different timings and prompts)
    """
    __tablename__ = "agent_schedule"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    agent_id: uuid.UUID = Field(foreign_key="agent.id", ondelete="CASCADE")

    # Schedule identity
    name: str  # User-friendly label (e.g., "Daily data collection")

    # Schedule configuration
    cron_string: str  # CRON expression in UTC (e.g., "0 6 * * 1-5")
    description: str  # Human-readable description from AI
    enabled: bool = Field(default=True)  # Allow disabling without deleting

    # Schedule-specific prompt (null = use agent's entrypoint_prompt at execution time)
    prompt: str | None = Field(default=None, sa_type=Text)

    # Execution tracking
    last_execution: datetime | None = Field(default=None)  # Last run timestamp
    next_execution: datetime  # Calculated next run timestamp

    # Timestamps
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

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


class CreateScheduleRequest(SQLModel):
    """Request to create a new schedule."""
    name: str
    cron_string: str
    timezone: str  # Needed for CRON conversion, not stored
    description: str
    prompt: str | None = None
    enabled: bool = True


class UpdateScheduleRequest(SQLModel):
    """Request to update an existing schedule. All fields optional."""
    name: str | None = None
    cron_string: str | None = None
    timezone: str | None = None  # Required when cron_string changes, for conversion
    description: str | None = None
    prompt: str | None = None
    enabled: bool | None = None


class AgentSchedulePublic(SQLModel):
    """Public response model for AgentSchedule."""
    id: uuid.UUID
    agent_id: uuid.UUID
    name: str
    cron_string: str
    description: str
    enabled: bool
    prompt: str | None
    last_execution: datetime | None
    next_execution: datetime
    created_at: datetime
    updated_at: datetime


class AgentSchedulesPublic(SQLModel):
    """List response model for AgentSchedule."""
    data: list[AgentSchedulePublic]
    count: int
