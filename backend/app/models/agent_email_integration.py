import uuid
from datetime import datetime
from enum import Enum

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel, Column, Text


class EmailAccessMode(str, Enum):
    OPEN = "open"
    RESTRICTED = "restricted"


class EmailCloneShareMode(str, Enum):
    USER = "user"
    BUILDER = "builder"


class AgentSessionMode(str, Enum):
    CLONE = "clone"   # Each sender gets their own isolated clone (default)
    OWNER = "owner"   # Sessions created on the original agent (owner's space)


# Shared properties (used in create/update/public)
class AgentEmailIntegrationBase(SQLModel):
    enabled: bool = False
    access_mode: EmailAccessMode = EmailAccessMode.RESTRICTED
    auto_approve_email_pattern: str | None = Field(default=None, max_length=1024)
    allowed_domains: str | None = Field(default=None, max_length=1024)
    max_clones: int = Field(default=50, ge=1, le=1000)
    clone_share_mode: EmailCloneShareMode = EmailCloneShareMode.USER
    agent_session_mode: AgentSessionMode = AgentSessionMode.CLONE
    incoming_server_id: uuid.UUID | None = Field(default=None)
    incoming_mailbox: str | None = Field(default=None, max_length=255)
    outgoing_server_id: uuid.UUID | None = Field(default=None)
    outgoing_from_address: str | None = Field(default=None, max_length=255)


# Properties to receive on creation
class AgentEmailIntegrationCreate(AgentEmailIntegrationBase):
    pass


# Properties to receive on update (all optional)
class AgentEmailIntegrationUpdate(SQLModel):
    enabled: bool | None = None
    access_mode: EmailAccessMode | None = None
    auto_approve_email_pattern: str | None = Field(default=None, max_length=1024)
    allowed_domains: str | None = Field(default=None, max_length=1024)
    max_clones: int | None = Field(default=None, ge=1, le=1000)
    clone_share_mode: EmailCloneShareMode | None = None
    agent_session_mode: AgentSessionMode | None = None
    incoming_server_id: uuid.UUID | None = None
    incoming_mailbox: str | None = Field(default=None, max_length=255)
    outgoing_server_id: uuid.UUID | None = None
    outgoing_from_address: str | None = Field(default=None, max_length=255)


# Database model
class AgentEmailIntegration(AgentEmailIntegrationBase, table=True):
    __tablename__ = "agent_email_integration"
    __table_args__ = (
        UniqueConstraint("agent_id", name="uq_agent_email_integration_agent_id"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    agent_id: uuid.UUID = Field(
        foreign_key="agent.id", nullable=False, ondelete="CASCADE"
    )
    # Override FK fields to include foreign_key constraints
    incoming_server_id: uuid.UUID | None = Field(
        default=None, foreign_key="mail_server_config.id", ondelete="SET NULL"
    )
    outgoing_server_id: uuid.UUID | None = Field(
        default=None, foreign_key="mail_server_config.id", ondelete="SET NULL"
    )
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


# Properties to return via API
class AgentEmailIntegrationPublic(AgentEmailIntegrationBase):
    id: uuid.UUID
    agent_id: uuid.UUID
    email_clone_count: int = 0
    created_at: datetime
    updated_at: datetime


# Result of manual process-emails action
class ProcessEmailsResult(SQLModel):
    polled: int = 0
    processed: int = 0
    pending: int = 0
    errors: int = 0
    message: str = ""
