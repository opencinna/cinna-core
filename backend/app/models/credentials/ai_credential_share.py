"""
AI Credential Share Model - Allows users to share AI credentials with other users.

This enables AI credential owners to grant access to their AI API keys
to other users, typically through agent sharing.
"""
import uuid
from datetime import datetime, UTC
from typing import TYPE_CHECKING

from sqlmodel import Field, Relationship, SQLModel
from sqlalchemy import Index, UniqueConstraint

if TYPE_CHECKING:
    from app.models.credentials.ai_credential import AICredential
    from app.models.users.user import User


class AICredentialShareBase(SQLModel):
    """Base model for AI credential shares."""
    pass


class AICredentialShare(AICredentialShareBase, table=True):
    """Database model for AI credential shares."""
    __tablename__ = "ai_credential_shares"
    __table_args__ = (
        # Indexes for efficient querying
        Index("ix_ai_credential_shares_ai_credential_id", "ai_credential_id"),
        Index("ix_ai_credential_shares_shared_with_user_id", "shared_with_user_id"),
        # Unique constraint: one share per credential+user pair
        UniqueConstraint(
            "ai_credential_id",
            "shared_with_user_id",
            name="uq_ai_credential_shares_credential_user",
        ),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    ai_credential_id: uuid.UUID = Field(
        foreign_key="ai_credential.id", nullable=False, ondelete="CASCADE"
    )
    shared_with_user_id: uuid.UUID = Field(
        foreign_key="user.id", nullable=False, ondelete="CASCADE"
    )
    shared_by_user_id: uuid.UUID = Field(foreign_key="user.id", nullable=False)
    shared_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    # Relationships
    ai_credential: "AICredential" = Relationship(
        sa_relationship_kwargs={"foreign_keys": "[AICredentialShare.ai_credential_id]"}
    )
    shared_with_user: "User" = Relationship(
        sa_relationship_kwargs={"foreign_keys": "[AICredentialShare.shared_with_user_id]"}
    )
    shared_by_user: "User" = Relationship(
        sa_relationship_kwargs={"foreign_keys": "[AICredentialShare.shared_by_user_id]"}
    )


class AICredentialSharePublic(SQLModel):
    """Public response model for AI credential shares."""
    id: uuid.UUID
    ai_credential_id: uuid.UUID
    ai_credential_name: str
    ai_credential_type: str
    shared_with_user_id: uuid.UUID
    shared_with_email: str
    shared_by_user_id: uuid.UUID
    shared_by_email: str
    shared_at: datetime


class AICredentialShareCreate(SQLModel):
    """Request model for creating an AI credential share."""
    shared_with_user_id: uuid.UUID


class AICredentialSharesPublic(SQLModel):
    """Response model for list of AI credential shares."""
    data: list[AICredentialSharePublic]
    count: int


class SharedAICredentialPublic(SQLModel):
    """Response model for AI credentials shared with the current user."""
    id: uuid.UUID
    name: str
    type: str
    owner_id: uuid.UUID
    owner_email: str
    shared_at: datetime


class SharedAICredentialsPublic(SQLModel):
    """Response model for list of AI credentials shared with current user."""
    data: list[SharedAICredentialPublic]
    count: int
