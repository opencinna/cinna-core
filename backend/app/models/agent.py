import uuid
from datetime import datetime
from typing import List, Optional
from sqlmodel import Field, Relationship, SQLModel

from app.models.user import User
from app.models.link_models import AgentCredentialLink


# Shared properties
class AgentBase(SQLModel):
    name: str = Field(min_length=1, max_length=255)
    workflow_prompt: str | None = Field(default=None)
    entrypoint_prompt: str | None = Field(default=None)


# Properties to receive on agent creation
class AgentCreate(AgentBase):
    description: str | None = None
    user_workspace_id: uuid.UUID | None = None


# Properties to receive on agent update
class AgentUpdate(SQLModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    workflow_prompt: str | None = None
    entrypoint_prompt: str | None = None
    is_active: bool | None = None
    ui_color_preset: str | None = None
    show_on_dashboard: bool | None = None


# Database model, database table inferred from class name
class Agent(AgentBase, table=True):
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    owner_id: uuid.UUID = Field(
        foreign_key="user.id", nullable=False, ondelete="CASCADE"
    )
    user_workspace_id: uuid.UUID | None = Field(
        default=None, foreign_key="user_workspace.id", ondelete="CASCADE"
    )
    # NEW FIELDS for agent sessions
    description: str | None = None
    is_active: bool = Field(default=True)
    active_environment_id: uuid.UUID | None = Field(default=None, foreign_key="agent_environment.id")
    ui_color_preset: str | None = Field(default="slate")
    show_on_dashboard: bool = Field(default=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    owner: User | None = Relationship(back_populates="agents")
    credentials: List["app.models.credential.Credential"] = Relationship(
        back_populates="agents", link_model=AgentCredentialLink
    )
    schedules: List["app.models.agent_schedule.AgentSchedule"] = Relationship(
        back_populates="agent",
        cascade_delete=True
    )
    handover_configs: List["app.models.agent_handover.AgentHandoverConfig"] = Relationship(
        sa_relationship_kwargs={
            "foreign_keys": "[AgentHandoverConfig.source_agent_id]",
            "cascade": "all, delete-orphan"
        }
    )


# Properties to return via API, id is always required
class AgentPublic(SQLModel):
    id: uuid.UUID
    name: str
    description: str | None
    workflow_prompt: str | None
    entrypoint_prompt: str | None
    is_active: bool
    active_environment_id: uuid.UUID | None
    ui_color_preset: str | None
    show_on_dashboard: bool
    created_at: datetime
    updated_at: datetime
    owner_id: uuid.UUID
    user_workspace_id: uuid.UUID | None


class AgentsPublic(SQLModel):
    data: list[AgentPublic]
    count: int


# Properties to return agent with credentials
class AgentWithCredentials(AgentPublic):
    credentials: list["CredentialPublic"]


# Request to link credential to agent
class AgentCredentialLinkRequest(SQLModel):
    credential_id: uuid.UUID


# Request to create agent with full flow (agent + environment + session)
class AgentCreateFlowRequest(SQLModel):
    description: str = Field(min_length=1, max_length=2000)
    mode: str = Field(default="building")  # "building" or "conversation"
    auto_create_session: bool = Field(default=False)  # If False, stop after environment is ready
    user_workspace_id: uuid.UUID | None = None


# Response for agent creation flow initiation
class AgentCreateFlowResponse(SQLModel):
    agent_id: uuid.UUID
    message: str
