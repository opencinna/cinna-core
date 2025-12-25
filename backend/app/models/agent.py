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


# Properties to receive on agent update
class AgentUpdate(SQLModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    workflow_prompt: str | None = None
    entrypoint_prompt: str | None = None
    is_active: bool | None = None


# Database model, database table inferred from class name
class Agent(AgentBase, table=True):
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    owner_id: uuid.UUID = Field(
        foreign_key="user.id", nullable=False, ondelete="CASCADE"
    )
    # NEW FIELDS for agent sessions
    description: str | None = None
    is_active: bool = Field(default=True)
    active_environment_id: uuid.UUID | None = Field(default=None, foreign_key="agent_environment.id")
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    owner: User | None = Relationship(back_populates="agents")
    credentials: List["app.models.credential.Credential"] = Relationship(
        back_populates="agents", link_model=AgentCredentialLink
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
    created_at: datetime
    updated_at: datetime
    owner_id: uuid.UUID


class AgentsPublic(SQLModel):
    data: list[AgentPublic]
    count: int


# Properties to return agent with credentials
class AgentWithCredentials(AgentPublic):
    credentials: list["CredentialPublic"]


# Request to link credential to agent
class AgentCredentialLinkRequest(SQLModel):
    credential_id: uuid.UUID
