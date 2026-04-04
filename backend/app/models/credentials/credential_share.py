"""
Credential Share Model - Allows users to share credentials with other users.

This enables credential owners to grant read-only access to their credentials
to other users, who can then use them in their own agents.
"""
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlmodel import Field, Relationship, SQLModel
from sqlalchemy import Index, UniqueConstraint, ForeignKeyConstraint

if TYPE_CHECKING:
    from app.models.credentials.credential import Credential
    from app.models.users.user import User


class CredentialShareBase(SQLModel):
    """Base model for credential shares."""
    access_level: str = Field(default="read", max_length=20)  # Currently only 'read' is supported


class CredentialShare(CredentialShareBase, table=True):
    """Database model for credential shares."""
    __tablename__ = "credential_shares"
    __table_args__ = (
        # Indexes for efficient querying
        Index("ix_credential_shares_credential_id", "credential_id"),
        Index("ix_credential_shares_shared_with_user_id", "shared_with_user_id"),
        # Unique constraint: one share per credential+user pair
        UniqueConstraint(
            "credential_id",
            "shared_with_user_id",
            name="uq_credential_shares_credential_user",
        ),
        # Named foreign keys with ondelete
        ForeignKeyConstraint(
            ["credential_id"],
            ["credential.id"],
            name="credential_shares_credential_id_fkey",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["shared_with_user_id"],
            ["user.id"],
            name="credential_shares_shared_with_user_id_fkey",
            ondelete="CASCADE",
        ),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    credential_id: uuid.UUID = Field(nullable=False)  # FK in __table_args__
    shared_with_user_id: uuid.UUID = Field(nullable=False)  # FK in __table_args__
    shared_by_user_id: uuid.UUID = Field(foreign_key="user.id", nullable=False)
    shared_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    # Relationships
    credential: "Credential" = Relationship(
        sa_relationship_kwargs={"foreign_keys": "[CredentialShare.credential_id]"}
    )
    shared_with_user: "User" = Relationship(
        sa_relationship_kwargs={"foreign_keys": "[CredentialShare.shared_with_user_id]"}
    )
    shared_by_user: "User" = Relationship(
        sa_relationship_kwargs={"foreign_keys": "[CredentialShare.shared_by_user_id]"}
    )


class CredentialSharePublic(SQLModel):
    """Public response model for credential shares with resolved user info."""
    id: uuid.UUID
    credential_id: uuid.UUID
    credential_name: str
    credential_type: str
    shared_with_user_id: uuid.UUID
    shared_with_email: str
    shared_by_user_id: uuid.UUID
    shared_by_email: str
    shared_at: datetime
    access_level: str


class CredentialShareCreate(SQLModel):
    """Request model for creating a credential share."""
    shared_with_email: str  # We find user by email


class CredentialSharesPublic(SQLModel):
    """Response model for list of credential shares."""
    data: list[CredentialSharePublic]
    count: int


class SharedCredentialPublic(SQLModel):
    """Response model for credentials shared with the current user."""
    id: uuid.UUID
    name: str
    type: str
    notes: str | None
    owner_id: uuid.UUID
    owner_email: str
    shared_at: datetime
    access_level: str


class SharedCredentialsPublic(SQLModel):
    """Response model for list of credentials shared with current user."""
    data: list[SharedCredentialPublic]
    count: int
