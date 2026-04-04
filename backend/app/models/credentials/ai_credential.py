import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlmodel import Field, Relationship, SQLModel, Column, Text, Index

if TYPE_CHECKING:
    from app.models.users.user import User


class AICredentialType(str, Enum):
    """Type of AI credential/SDK provider"""
    # Existing types (backward compat)
    ANTHROPIC = "anthropic"
    MINIMAX = "minimax"
    OPENAI_COMPATIBLE = "openai_compatible"
    # New types added for OpenCode and expanded SDK support
    OPENAI = "openai"
    GOOGLE = "google"


# Shared properties
class AICredentialBase(SQLModel):
    """Base properties for AI credentials"""
    name: str = Field(min_length=1, max_length=255)
    type: AICredentialType = Field(..., sa_type=sa.String(50))
    expiry_notification_date: datetime | None = Field(default=None)


# Properties to receive on creation
class AICredentialCreate(AICredentialBase):
    """Create AI credential with sensitive data"""
    api_key: str = Field(min_length=1)
    # Optional fields — used depending on credential type
    # openai_compatible, google: base_url (optional endpoint override)
    base_url: str | None = Field(default=None, max_length=500)
    # openai_compatible: required default model name
    model: str | None = Field(default=None, max_length=255)


# Properties to receive on update
class AICredentialUpdate(SQLModel):
    """Update AI credential (partial update)"""
    name: str | None = Field(default=None, min_length=1, max_length=255)
    api_key: str | None = Field(default=None, min_length=1)
    # Optional fields for various types
    base_url: str | None = Field(default=None, max_length=500)
    model: str | None = Field(default=None, max_length=255)
    expiry_notification_date: datetime | None = Field(default=None)


# Database model
class AICredential(AICredentialBase, table=True):
    """AI credential database model with encrypted storage"""
    __tablename__ = "ai_credential"
    __table_args__ = (
        Index("ix_ai_credential_owner_type", "owner_id", "type"),
        Index("ix_ai_credential_owner_default", "owner_id", "is_default"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    owner_id: uuid.UUID = Field(
        foreign_key="user.id", nullable=False, ondelete="CASCADE"
    )
    # Encrypted JSON: {api_key, base_url?, model?}
    encrypted_data: str = Field(sa_column=Column(Text, nullable=False))
    is_default: bool = Field(default=False)
    expiry_notification_date: datetime | None = Field(default=None)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    # Relationships
    owner: "User" = Relationship(
        sa_relationship_kwargs={"foreign_keys": "[AICredential.owner_id]"}
    )


# Properties to return via API
class AICredentialPublic(AICredentialBase):
    """Public AI credential (no sensitive data)"""
    id: uuid.UUID
    is_default: bool
    has_api_key: bool = True  # Always true for existing credentials
    is_oauth_token: bool = False  # True if this is an OAuth token (sk-ant-oat*)
    # Safe to expose (no secret data)
    base_url: str | None = None     # For openai_compatible, google
    model: str | None = None        # For openai_compatible
    expiry_notification_date: datetime | None = None
    created_at: datetime
    updated_at: datetime


class AICredentialsPublic(SQLModel):
    """List of AI credentials"""
    data: list[AICredentialPublic]
    count: int


# Internal data structure for decrypted credential data
class AICredentialData(SQLModel):
    """Decrypted AI credential data (internal use only)"""
    api_key: str
    base_url: str | None = None
    model: str | None = None


# Affected environments query response models
class AffectedEnvironmentPublic(SQLModel):
    """Information about an environment affected by credential change"""
    environment_id: uuid.UUID
    agent_id: uuid.UUID
    agent_name: str
    environment_name: str
    status: str
    usage: str  # "conversation", "building", or "conversation & building"
    owner_id: uuid.UUID
    owner_email: str


class SharedUserPublic(SQLModel):
    """User who has access to this credential via share"""
    user_id: uuid.UUID
    email: str
    shared_at: datetime


class AffectedEnvironmentsPublic(SQLModel):
    """Response for affected environments query"""
    credential_id: uuid.UUID
    credential_name: str
    environments: list[AffectedEnvironmentPublic]
    shared_with_users: list[SharedUserPublic]
    count: int
