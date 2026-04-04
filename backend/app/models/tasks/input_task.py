"""
Input Task models for task management workflow.

Input tasks allow users to receive, refine, and execute incoming tasks through
an AI-assisted preparation workflow. Extended for the task-based collaboration
system: short-code IDs, hierarchy, team assignment, priority.
"""
import uuid
from datetime import datetime, UTC
from sqlmodel import Field, SQLModel, Column
from sqlalchemy import JSON, Index
from sqlalchemy.dialects.postgresql import JSONB

from app.models.files.file_upload import FileUploadPublic


class InputTaskStatus:
    """Status values for input tasks (clean set — running/pending_input removed)"""
    NEW = "new"              # Created, awaiting refinement or assignment
    REFINING = "refining"   # User actively refining with AI
    OPEN = "open"            # Refined and assigned, ready for agent execution
    IN_PROGRESS = "in_progress"  # Agent actively working
    BLOCKED = "blocked"     # Agent blocked, waiting for external input or dependency
    COMPLETED = "completed"  # Task finished successfully
    ERROR = "error"          # Task failed
    CANCELLED = "cancelled"  # Task cancelled by user or agent
    ARCHIVED = "archived"   # Archived by user

    # Legacy aliases — kept for backward compatibility during migration
    RUNNING = "in_progress"      # Old: running → in_progress
    PENDING_INPUT = "blocked"    # Old: pending_input → blocked

    # Valid status values (for validation)
    ALL_STATUSES = {
        "new", "refining", "open", "in_progress", "blocked",
        "completed", "error", "cancelled", "archived",
    }

    # Valid transitions: {from_status: {allowed_to_statuses}}
    VALID_TRANSITIONS: dict[str, set[str]] = {
        "new": {"refining", "open", "in_progress", "cancelled", "archived"},
        "refining": {"new", "open", "in_progress", "archived"},
        "open": {"in_progress", "cancelled", "archived"},
        "in_progress": {"completed", "blocked", "cancelled", "error", "archived"},
        "blocked": {"in_progress", "cancelled", "archived"},
        "completed": {"archived"},
        "error": {"new", "in_progress", "archived"},
        "cancelled": {"archived"},
        "archived": set(),
    }


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
        # New indexes for collaboration features
        Index("ix_input_task_parent_task_id", "parent_task_id"),
        Index("ix_input_task_team_id", "team_id"),
        Index("ix_input_task_assigned_node_id", "assigned_node_id"),
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

    # ── New collaboration columns ──────────────────────────────────────────────
    # Short code: globally unique per owner (e.g., "TASK-1", "HR-42")
    short_code: str | None = Field(default=None, max_length=20, index=True)
    # Monotonic sequence number from user.task_sequence_counter
    sequence_number: int | None = Field(default=None)
    # Human-readable title (derived from original_message on creation, editable)
    title: str | None = Field(default=None, max_length=500)
    # Priority: low / normal / high / urgent
    priority: str = Field(default="normal", max_length=20)
    # Subtask hierarchy: parent task (SET NULL on parent delete)
    parent_task_id: uuid.UUID | None = Field(
        default=None, foreign_key="input_task.id", ondelete="SET NULL"
    )
    # Team association (optional — team-scoped tasks only)
    team_id: uuid.UUID | None = Field(
        default=None, foreign_key="agentic_team.id", ondelete="SET NULL"
    )
    # The team node assigned to this task (role context for team tasks)
    assigned_node_id: uuid.UUID | None = Field(
        default=None, foreign_key="agentic_team_node.id", ondelete="SET NULL"
    )
    # Which team node created this task (for agent-initiated subtasks; NULL for user-created)
    created_by_node_id: uuid.UUID | None = Field(
        default=None, foreign_key="agentic_team_node.id", ondelete="SET NULL"
    )


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
    # Collaboration fields
    title: str | None = Field(default=None, max_length=500)
    priority: str = "normal"
    team_id: uuid.UUID | None = None
    assigned_node_id: uuid.UUID | None = None
    parent_task_id: uuid.UUID | None = None


# Update schema
class InputTaskUpdate(SQLModel):
    current_description: str | None = Field(default=None, min_length=1, max_length=10000)
    selected_agent_id: uuid.UUID | None = None
    # Collaboration fields
    title: str | None = Field(default=None, max_length=500)
    priority: str | None = None
    team_id: uuid.UUID | None = None
    assigned_node_id: uuid.UUID | None = None


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
    # Collaboration fields
    short_code: str | None = None
    sequence_number: int | None = None
    title: str | None = None
    priority: str = "normal"
    parent_task_id: uuid.UUID | None = None
    team_id: uuid.UUID | None = None
    assigned_node_id: uuid.UUID | None = None
    created_by_node_id: uuid.UUID | None = None
    # Computed counts (populated by service layer)
    subtask_count: int = 0
    subtask_completed_count: int = 0


class InputTaskPublicExtended(InputTaskPublic):
    """Extended response with agent name, sessions count, and collaboration data"""
    agent_name: str | None = None
    refinement_history: list = Field(default_factory=list)
    todo_progress: list | None = None
    sessions_count: int = 0
    latest_session_id: uuid.UUID | None = None
    attached_files: list[FileUploadPublic] = Field(default_factory=list)
    # Team/node display names (resolved by service layer)
    assigned_node_name: str | None = None
    team_name: str | None = None


class InputTaskDetailPublic(InputTaskPublicExtended):
    """Full task detail including comments, attachments, subtasks, and status history"""
    comments: list = Field(default_factory=list)       # list[TaskCommentPublic]
    attachments: list = Field(default_factory=list)    # list[TaskAttachmentPublic]
    subtasks: list = Field(default_factory=list)       # list[InputTaskPublic]
    status_history: list = Field(default_factory=list) # list[TaskStatusHistoryPublic]


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


# Agent-facing request/response models (for MCP tools and internal agent API)

class AgentTaskStatusUpdate(SQLModel):
    """Agent request to explicitly update task status (edge cases only)"""
    status: str  # blocked / completed / cancelled
    reason: str | None = None
    task: str | None = None  # short_code; defaults to agent's current task
    source_session_id: uuid.UUID | None = None  # Calling session UUID (set by MCP tool)


class AgentSubtaskCreate(SQLModel):
    """Agent request to create a subtask (team context required)"""
    title: str = Field(min_length=1, max_length=500)
    description: str | None = Field(default=None, max_length=10000)
    assigned_to: str | None = None  # Agent name or team node name to assign to
    priority: str = "normal"
    task: str | None = None  # parent short_code; defaults to agent's current task
    source_session_id: uuid.UUID | None = None  # Calling session UUID (set by MCP tool)


class AgentTaskCreate(SQLModel):
    """Agent request to create a new task"""
    title: str = Field(min_length=1, max_length=500)
    description: str | None = Field(default=None, max_length=10000)
    assigned_to: str | None = None  # Agent name to assign to
    priority: str = "normal"
    source_session_id: uuid.UUID | None = None  # Calling session UUID (set by MCP tool)


class AgentTaskOperationResponse(SQLModel):
    """Generic response for agent task operations"""
    success: bool
    task: str | None = None          # short_code of the task
    parent_task: str | None = None   # parent short_code (for subtasks)
    assigned_to: str | None = None   # resolved agent/node name
    message: str | None = None
    error: str | None = None
