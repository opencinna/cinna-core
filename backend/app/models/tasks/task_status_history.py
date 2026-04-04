"""
Task Status History models for the task collaboration system.

Immutable audit trail of every status transition on an input task.
"""
import uuid
from datetime import datetime, UTC
from sqlmodel import Field, SQLModel, Index


class TaskStatusHistory(SQLModel, table=True):
    __tablename__ = "task_status_history"
    __table_args__ = (
        Index("ix_task_status_history_task_id", "task_id"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    task_id: uuid.UUID = Field(
        foreign_key="input_task.id", nullable=False, ondelete="CASCADE"
    )
    from_status: str = Field(max_length=30)
    to_status: str = Field(max_length=30)
    # Who caused the change
    changed_by_agent_id: uuid.UUID | None = Field(
        default=None, foreign_key="agent.id", ondelete="SET NULL"
    )
    changed_by_user_id: uuid.UUID | None = Field(
        default=None, foreign_key="user.id", ondelete="SET NULL"
    )
    # Optional explanation for the transition
    reason: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


# API response schema
class TaskStatusHistoryPublic(SQLModel):
    id: uuid.UUID
    task_id: uuid.UUID
    from_status: str
    to_status: str
    changed_by_agent_id: uuid.UUID | None
    changed_by_user_id: uuid.UUID | None
    reason: str | None
    created_at: datetime
    # Resolved display field
    changed_by_name: str | None = None   # Human-readable changer name


class TaskStatusHistoriesPublic(SQLModel):
    data: list[TaskStatusHistoryPublic]
    count: int
