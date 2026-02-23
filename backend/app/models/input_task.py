"""
Input Task models for task management workflow.

Input tasks allow users to receive, refine, and execute incoming tasks through
an AI-assisted preparation workflow.
"""
import uuid
from datetime import datetime, UTC
from sqlmodel import Field, SQLModel, Column
from sqlalchemy import JSON, Index
from sqlalchemy.dialects.postgresql import JSONB

from app.models.file_upload import FileUploadPublic


class InputTaskStatus:
    """Status values for input tasks"""
    NEW = "new"  # Just created, needs refinement
    REFINING = "refining"  # Currently being refined with AI
    RUNNING = "running"  # Session created and running
    PENDING_INPUT = "pending_input"  # Session waiting for user input
    COMPLETED = "completed"  # Session completed successfully
    ERROR = "error"  # Session ended with error
    ARCHIVED = "archived"  # Archived by user


class RefinementHistoryItem(SQLModel):
    """Single item in refinement history"""
    role: str  # "user" | "ai"
    content: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))


# Shared properties
class InputTaskBase(SQLModel):
    original_message: str = Field(min_length=1, max_length=10000)
    current_description: str = Field(min_length=1, max_length=10000)


# Database model
class InputTask(InputTaskBase, table=True):
    __tablename__ = "input_task"
    __table_args__ = (
        # Compound index for efficient lookup by owner and status
        Index("ix_input_task_owner_status", "owner_id", "status"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    owner_id: uuid.UUID = Field(
        foreign_key="user.id", nullable=False, ondelete="CASCADE"
    )
    selected_agent_id: uuid.UUID | None = Field(
        default=None, foreign_key="agent.id", ondelete="SET NULL"
    )
    session_id: uuid.UUID | None = Field(
        default=None, foreign_key="session.id", ondelete="SET NULL"
    )
    user_workspace_id: uuid.UUID | None = Field(
        default=None, foreign_key="user_workspace.id", ondelete="CASCADE"
    )
    # Agent-initiated task fields (for handover through task creation)
    agent_initiated: bool = Field(default=False)
    auto_execute: bool = Field(default=False)
    source_session_id: uuid.UUID | None = Field(
        default=None, foreign_key="session.id", ondelete="SET NULL"
    )
    # Email source tracking (for email-originated tasks)
    source_email_message_id: uuid.UUID | None = Field(
        default=None, foreign_key="email_message.id", ondelete="SET NULL"
    )
    source_agent_id: uuid.UUID | None = Field(
        default=None, foreign_key="agent.id", ondelete="SET NULL"
    )
    # Auto-feedback: whether to auto-trigger source agent on session state change
    auto_feedback: bool = Field(default=True)
    feedback_delivered: bool = Field(default=False)
    status: str = Field(default=InputTaskStatus.NEW)
    refinement_history: list = Field(default_factory=list, sa_column=Column(JSON))
    # To-do progress tracking from TodoWrite tool (list of TodoItem dicts)
    todo_progress: list | None = Field(default=None, sa_column=Column(JSONB))
    error_message: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    executed_at: datetime | None = None
    completed_at: datetime | None = None
    archived_at: datetime | None = None


# Create schema
class InputTaskCreate(SQLModel):
    original_message: str = Field(min_length=1, max_length=10000)
    selected_agent_id: uuid.UUID | None = None
    user_workspace_id: uuid.UUID | None = None
    # Agent-initiated task fields
    agent_initiated: bool = False
    auto_execute: bool = False
    source_session_id: uuid.UUID | None = None
    # File attachments
    file_ids: list[uuid.UUID] | None = None


# Update schema
class InputTaskUpdate(SQLModel):
    current_description: str | None = Field(default=None, min_length=1, max_length=10000)
    selected_agent_id: uuid.UUID | None = None


# API response schemas
class InputTaskPublic(SQLModel):
    id: uuid.UUID
    owner_id: uuid.UUID
    original_message: str
    current_description: str
    status: str
    selected_agent_id: uuid.UUID | None
    session_id: uuid.UUID | None
    user_workspace_id: uuid.UUID | None
    # Agent-initiated task fields
    agent_initiated: bool
    auto_execute: bool
    source_session_id: uuid.UUID | None
    # Email source tracking
    source_email_message_id: uuid.UUID | None = None
    source_agent_id: uuid.UUID | None = None
    auto_feedback: bool
    error_message: str | None
    created_at: datetime
    updated_at: datetime
    executed_at: datetime | None
    completed_at: datetime | None
    archived_at: datetime | None


class InputTaskPublicExtended(InputTaskPublic):
    """Extended response with agent name and sessions count"""
    agent_name: str | None = None
    refinement_history: list = Field(default_factory=list)
    todo_progress: list | None = None
    sessions_count: int = 0
    latest_session_id: uuid.UUID | None = None
    attached_files: list[FileUploadPublic] = Field(default_factory=list)
    # Session result state (joined from linked session)
    result_state: str | None = None
    result_summary: str | None = None


class InputTasksPublic(SQLModel):
    data: list[InputTaskPublic]
    count: int


class InputTasksPublicExtended(SQLModel):
    data: list[InputTaskPublicExtended]
    count: int


# Action request/response schemas
class RefineTaskRequest(SQLModel):
    """Request to refine a task with AI assistance"""
    user_comment: str = Field(min_length=1, max_length=8000)
    user_selected_text: str | None = Field(default=None, max_length=5000)


class RefineTaskResponse(SQLModel):
    """Response from task refinement"""
    success: bool
    refined_description: str | None = None
    feedback_message: str | None = None
    error: str | None = None


class ExecuteTaskRequest(SQLModel):
    """Request to execute a task (create session)"""
    mode: str = Field(default="conversation")  # "building" | "conversation"


class ExecuteTaskResponse(SQLModel):
    """Response from task execution"""
    success: bool
    session_id: uuid.UUID | None = None
    error: str | None = None
    file_ids: list[str] | None = None


class SendAnswerRequest(SQLModel):
    """Request to send an email reply for an email-originated task"""
    custom_message: str | None = Field(default=None, max_length=10000)


class SendAnswerResponse(SQLModel):
    """Response from sending an email reply"""
    success: bool
    queue_entry_id: uuid.UUID | None = None
    generated_reply: str | None = None
    error: str | None = None
