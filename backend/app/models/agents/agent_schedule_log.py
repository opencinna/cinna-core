import uuid
from datetime import datetime, UTC
from sqlalchemy import Text, Index
from sqlmodel import Field, SQLModel


class AgentScheduleLog(SQLModel, table=True):
    """
    Execution log for agent schedule runs.

    Immutable append-only records created after every schedule execution attempt.
    Deleted via cascade when parent schedule or agent is deleted.

    Status values:
    - "success": static_prompt session created OK, or script_trigger returned "OK"
    - "session_triggered": script_trigger returned non-OK, session was created with context
    - "error": execution failed (timeout, network error, env not available)
    - "skipped": run skipped because the agent's environment is in a critical state
      (running-but-degraded) and is not eligible for scheduled execution
    """
    __tablename__ = "agent_schedule_log"
    __table_args__ = (
        Index("ix_agent_schedule_log_schedule_id", "schedule_id"),
        Index("ix_agent_schedule_log_agent_id", "agent_id"),
        Index("ix_agent_schedule_log_executed_at", "executed_at"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    schedule_id: uuid.UUID = Field(foreign_key="agent_schedule.id", ondelete="CASCADE")
    agent_id: uuid.UUID = Field(foreign_key="agent.id", ondelete="CASCADE")

    # Snapshot of schedule type at execution time
    schedule_type: str  # "static_prompt" or "script_trigger"

    # Execution outcome
    status: str  # "success", "session_triggered", "error", "skipped"

    # static_prompt fields
    prompt_used: str | None = Field(default=None, sa_type=Text)

    # script_trigger fields
    command_executed: str | None = Field(default=None, sa_type=Text)
    command_output: str | None = Field(default=None, sa_type=Text)  # truncated to 10,000 chars
    command_exit_code: int | None = Field(default=None)

    # Session created (if any) — SET NULL on session delete to preserve log history
    session_id: uuid.UUID | None = Field(
        default=None, foreign_key="session.id", ondelete="SET NULL"
    )

    # Error details if execution failed
    error_message: str | None = Field(default=None, sa_type=Text)

    # When the execution happened (UTC)
    executed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class AgentScheduleLogPublic(SQLModel):
    """Public response model for AgentScheduleLog."""
    id: uuid.UUID
    schedule_id: uuid.UUID
    agent_id: uuid.UUID
    schedule_type: str
    status: str
    prompt_used: str | None
    command_executed: str | None
    command_output: str | None
    command_exit_code: int | None
    session_id: uuid.UUID | None
    error_message: str | None
    executed_at: datetime


class AgentScheduleLogsPublic(SQLModel):
    """List response model for AgentScheduleLog."""
    data: list[AgentScheduleLogPublic]
    count: int
