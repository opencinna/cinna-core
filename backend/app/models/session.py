import uuid
from datetime import datetime, UTC
from sqlmodel import SQLModel, Field, Column
from sqlalchemy import JSON, UniqueConstraint

from app.models.file_upload import FileUploadPublic


class Session(SQLModel, table=True):
    __tablename__ = "session"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    environment_id: uuid.UUID = Field(foreign_key="agent_environment.id", ondelete="CASCADE")
    user_id: uuid.UUID = Field(foreign_key="user.id", ondelete="CASCADE")
    user_workspace_id: uuid.UUID | None = Field(
        default=None, foreign_key="user_workspace.id", ondelete="CASCADE"
    )
    # Track which access token created this session (for A2A scope enforcement)
    access_token_id: uuid.UUID | None = Field(
        default=None, foreign_key="agent_access_tokens.id", ondelete="SET NULL"
    )
    # Track which input task spawned this session (for task management)
    source_task_id: uuid.UUID | None = Field(
        default=None, foreign_key="input_task.id", ondelete="SET NULL"
    )
    # Track which guest share created this session (for guest access)
    guest_share_id: uuid.UUID | None = Field(
        default=None, foreign_key="agent_guest_share.id", ondelete="SET NULL"
    )
    # Track which webapp share created this session (for webapp chat)
    webapp_share_id: uuid.UUID | None = Field(
        default=None, foreign_key="agent_webapp_share.id", ondelete="SET NULL"
    )
    title: str | None = None
    mode: str = "conversation"  # "building" | "conversation"
    status: str = "active"  # "active" | "paused" | "completed" | "error"
    interaction_status: str = ""  # "" (default/nothing happens) | "running" (active stream with agent-env) | "pending_stream" (waiting for env to activate or user to send next message)
    pending_messages_count: int = 0  # Number of user messages with sent_to_agent_status='pending'
    session_metadata: dict = Field(default_factory=dict, sa_column=Column(JSON))
    # To-do progress tracking from TodoWrite tool (list of TodoItem dicts)
    todo_progress: list | None = Field(default=None, sa_column=Column(JSON))
    # Agent-declared session outcome (set via update_session_state tool)
    result_state: str | None = None  # "completed" | "needs_input" | "error"
    result_summary: str | None = None  # Agent's summary/question/error description
    # Email integration fields
    email_thread_id: str | None = None  # Email Message-ID for threading
    integration_type: str | None = None  # "email" | "a2a" | null
    sender_email: str | None = None  # Original sender email (owner mode only)
    streaming_started_at: datetime | None = None
    # MCP integration fields
    mcp_connector_id: uuid.UUID | None = Field(
        default=None, foreign_key="mcp_connector.id", ondelete="SET NULL"
    )
    mcp_session_id: str | None = Field(default=None)
    # Track which dashboard block triggered this session (for prompt action session reuse)
    dashboard_block_id: uuid.UUID | None = Field(
        default=None, foreign_key="user_dashboard_block.id", ondelete="SET NULL"
    )
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    last_message_at: datetime | None = None


class SessionMessage(SQLModel, table=True):
    __tablename__ = "message"
    __table_args__ = (UniqueConstraint("session_id", "sequence_number"),)

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    session_id: uuid.UUID = Field(foreign_key="session.id", ondelete="CASCADE")
    role: str  # "user" | "agent" | "system"
    content: str
    sequence_number: int
    message_metadata: dict = Field(default_factory=dict, sa_column=Column(JSON))
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    tool_questions_status: str | None = None  # null | "unanswered" | "answered"
    answers_to_message_id: uuid.UUID | None = Field(default=None, foreign_key="message.id")
    status: str = ""  # "" | "user_interrupted" | "error"
    status_message: str | None = None  # Error details or interrupt reason
    sent_to_agent_status: str = "pending"  # "pending" | "sent" - tracks if user message was sent to agent-env

    # Note: 'files' attribute is populated at runtime by service layer
    # (Not declared as Field to avoid SQLModel table column creation)


# Pydantic Schemas
class SessionCreate(SQLModel):
    agent_id: uuid.UUID  # Will use active environment
    title: str | None = None
    mode: str = "conversation"  # "building" | "conversation"
    guest_share_id: uuid.UUID | None = None  # Optional guest share link
    webapp_share_id: uuid.UUID | None = None  # Optional webapp share link
    dashboard_block_id: uuid.UUID | None = None  # Optional dashboard block that triggered this session


class SessionUpdate(SQLModel):
    title: str | None = None
    status: str | None = None
    interaction_status: str | None = None
    mode: str | None = None


class SessionPublic(SQLModel):
    id: uuid.UUID
    environment_id: uuid.UUID
    user_id: uuid.UUID
    user_workspace_id: uuid.UUID | None
    access_token_id: uuid.UUID | None
    source_task_id: uuid.UUID | None
    guest_share_id: uuid.UUID | None = None
    webapp_share_id: uuid.UUID | None = None
    title: str | None
    mode: str
    status: str
    interaction_status: str
    pending_messages_count: int
    result_state: str | None = None
    result_summary: str | None = None
    todo_progress: list | None = None
    email_thread_id: str | None = None
    integration_type: str | None = None
    sender_email: str | None = None
    streaming_started_at: datetime | None = None
    mcp_connector_id: uuid.UUID | None = None
    mcp_session_id: str | None = None
    dashboard_block_id: uuid.UUID | None = None
    created_at: datetime
    updated_at: datetime
    last_message_at: datetime | None


class SessionPublicExtended(SessionPublic):
    """Session with external session metadata"""
    external_session_id: str | None = None
    sdk_type: str | None = None
    agent_id: uuid.UUID | None = None
    agent_name: str | None = None
    agent_ui_color_preset: str | None = None
    message_count: int | None = None
    last_message_content: str | None = None


class SessionsPublic(SQLModel):
    data: list[SessionPublic]
    count: int


class SessionsPublicExtended(SQLModel):
    data: list[SessionPublicExtended]
    count: int


class MessageCreate(SQLModel):
    content: str
    answers_to_message_id: uuid.UUID | None = None
    file_ids: list[uuid.UUID] = Field(default_factory=list)
    page_context: str | None = None


class MessagePublic(SQLModel):
    id: uuid.UUID
    session_id: uuid.UUID
    role: str
    content: str
    sequence_number: int
    timestamp: datetime
    message_metadata: dict
    tool_questions_status: str | None
    answers_to_message_id: uuid.UUID | None
    status: str
    status_message: str | None
    sent_to_agent_status: str
    files: list[FileUploadPublic] = Field(default_factory=list)


class MessagesPublic(SQLModel):
    data: list[MessagePublic]
    count: int


class SessionCommandPublic(SQLModel):
    name: str
    description: str
    is_available: bool


class SessionCommandsPublic(SQLModel):
    commands: list[SessionCommandPublic]
