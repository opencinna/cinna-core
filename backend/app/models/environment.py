import uuid
from datetime import datetime
from sqlmodel import SQLModel, Field, Column
from sqlalchemy import JSON


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
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    last_health_check: datetime | None = None
    last_activity_at: datetime | None = None  # Last time environment was actively used (message sent, session opened, etc.)


# Pydantic Schemas
class AgentEnvironmentCreate(SQLModel):
    env_name: str
    env_version: str = "1.0.0"
    instance_name: str = "Instance"
    type: str = "docker"  # "docker" | "remote_ssh" | "remote_http"
    config: dict = {}


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


class AgentEnvironmentsPublic(SQLModel):
    data: list[AgentEnvironmentPublic]
    count: int
