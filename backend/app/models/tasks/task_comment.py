"""
Task Comment models for the task collaboration system.

Task comments are the primary collaboration surface — agents and users
post structured comments on tasks to share findings, report progress,
and deliver results.
"""
import uuid
from datetime import datetime, UTC
from sqlmodel import Field, SQLModel, Column, Index
from sqlalchemy import JSON


class TaskCommentBase(SQLModel):
    content: str = Field(min_length=1, max_length=10000)
    comment_type: str = Field(default="message", max_length=30)


class TaskComment(TaskCommentBase, table=True):
    __tablename__ = "task_comment"
    __table_args__ = (
        Index("ix_task_comment_task_id", "task_id"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    task_id: uuid.UUID = Field(
        foreign_key="input_task.id", nullable=False, ondelete="CASCADE"
    )
    # Author tracking — one of these will be set, or none for system comments
    author_node_id: uuid.UUID | None = Field(
        default=None, foreign_key="agentic_team_node.id", ondelete="SET NULL"
    )
    author_agent_id: uuid.UUID | None = Field(
        default=None, foreign_key="agent.id", ondelete="SET NULL"
    )
    author_user_id: uuid.UUID | None = Field(
        default=None, foreign_key="user.id", ondelete="SET NULL"
    )
    # Optional extra data (e.g. for status_change: {from_status, to_status})
    comment_meta: dict | None = Field(default=None, sa_column=Column(JSON, name="metadata"))
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


# Create schema for user-initiated comments
class TaskCommentCreate(SQLModel):
    content: str = Field(min_length=1, max_length=10000)
    comment_type: str = Field(default="message", max_length=30)


# Create schema for agent-initiated comments (allows file paths for attachment)
class AgentTaskCommentCreate(SQLModel):
    content: str = Field(min_length=1, max_length=10000)
    comment_type: str = Field(default="message", max_length=30)
    # Workspace file paths to attach (agent-env resolves these to actual files)
    file_paths: list[str] | None = None
    source_session_id: uuid.UUID | None = None  # Calling session UUID (set by MCP tool)


# API response schema
class TaskCommentPublic(SQLModel):
    id: uuid.UUID
    task_id: uuid.UUID
    content: str
    comment_type: str
    author_node_id: uuid.UUID | None
    author_agent_id: uuid.UUID | None
    author_user_id: uuid.UUID | None
    comment_meta: dict | None
    created_at: datetime
    # Resolved display fields
    author_name: str | None = None       # Human-readable author name
    author_role: str | None = None       # Node name if team context (e.g., "HR Lead")
    # Inline attachments linked to this comment
    inline_attachments: list = Field(default_factory=list)


class TaskCommentsPublic(SQLModel):
    data: list[TaskCommentPublic]
    count: int
