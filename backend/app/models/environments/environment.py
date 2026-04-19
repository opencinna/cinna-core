import uuid
from datetime import datetime, UTC
from sqlmodel import SQLModel, Field, Column
from sqlalchemy import JSON, Text, DateTime
import sqlalchemy as sa


class AgentEnvironment(SQLModel, table=True):
    __tablename__ = "agent_environment"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    agent_id: uuid.UUID = Field(foreign_key="agent.id", ondelete="CASCADE")
    env_name: str  # e.g., "python-env-basic"
    env_version: str = "1.0.0"  # e.g., "1.0.0"
    instance_name: str = "Instance"  # e.g., "Production", "Testing"
    type: str = "docker"  # "docker" | "remote_ssh" | "remote_http" | "kubernetes"
    status: str = "stopped"  # "stopped" | "creating" | "building" | "initializing" | "starting" | "running" | "rebuilding" | "suspended" | "activating" | "error" | "deprecated"
    is_active: bool = Field(default=False)
    status_message: str | None = None  # Detailed status message for UI (e.g., "Building Docker image...")
    config: dict = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    last_health_check: datetime | None = None
    last_activity_at: datetime | None = None  # Last time environment was actively used (message sent, session opened, etc.)
    # SDK selection for agent (immutable after creation)
    agent_sdk_conversation: str | None = None  # "claude-code/anthropic" | "claude-code/minimax" | "opencode/anthropic"
    agent_sdk_building: str | None = None  # "claude-code/anthropic" | "claude-code/minimax" | "opencode/anthropic"
    # Model override per mode (optional; if None, adapter uses its own default)
    model_override_conversation: str | None = None  # e.g., "gpt-4o-mini", "claude-haiku-4-5"
    model_override_building: str | None = None  # e.g., "claude-opus-4", "gpt-4o"
    # AI credential linking (if False, use explicitly linked credentials)
    use_default_ai_credentials: bool = Field(default=True)
    conversation_ai_credential_id: uuid.UUID | None = Field(
        default=None, foreign_key="ai_credential.id", ondelete="SET NULL"
    )
    building_ai_credential_id: uuid.UUID | None = Field(
        default=None, foreign_key="ai_credential.id", ondelete="SET NULL"
    )
    # Agent self-reported status snapshot (from STATUS.md in workspace root)
    status_file_raw: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    status_file_severity: str | None = Field(default=None, sa_column=Column(sa.String(16), nullable=True))
    status_file_summary: str | None = Field(default=None, sa_column=Column(sa.String(512), nullable=True))
    status_file_reported_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True), nullable=True))
    status_file_reported_at_source: str | None = Field(default=None, sa_column=Column(sa.String(16), nullable=True))
    status_file_fetched_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True), nullable=True))
    status_file_prev_severity: str | None = Field(default=None, sa_column=Column(sa.String(16), nullable=True))
    status_file_severity_changed_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True), nullable=True))


# Pydantic Schemas
class AgentEnvironmentCreate(SQLModel):
    env_name: str
    env_version: str = "1.0.0"
    instance_name: str = "Instance"
    type: str = "docker"  # "docker" | "remote_ssh" | "remote_http"
    config: dict = {}
    agent_sdk_conversation: str | None = None  # "claude-code/anthropic" | "claude-code/minimax" | "opencode/anthropic"
    agent_sdk_building: str | None = None  # "claude-code/anthropic" | "claude-code/minimax" | "opencode/anthropic"
    # Model override per mode (optional)
    model_override_conversation: str | None = None
    model_override_building: str | None = None
    # AI credential linking
    use_default_ai_credentials: bool = True
    conversation_ai_credential_id: uuid.UUID | None = None
    building_ai_credential_id: uuid.UUID | None = None


class AgentEnvironmentUpdate(SQLModel):
    instance_name: str | None = None
    config: dict | None = None


class AgentEnvironmentPublic(SQLModel):
    id: uuid.UUID
    agent_id: uuid.UUID
    env_name: str
    env_version: str
    instance_name: str
    type: str
    status: str
    status_message: str | None
    is_active: bool
    created_at: datetime
    updated_at: datetime
    last_health_check: datetime | None
    last_activity_at: datetime | None
    agent_sdk_conversation: str | None
    agent_sdk_building: str | None
    # Model override per mode (optional)
    model_override_conversation: str | None
    model_override_building: str | None
    # AI credential linking
    use_default_ai_credentials: bool
    conversation_ai_credential_id: uuid.UUID | None
    building_ai_credential_id: uuid.UUID | None


class AgentEnvironmentsPublic(SQLModel):
    data: list[AgentEnvironmentPublic]
    count: int
