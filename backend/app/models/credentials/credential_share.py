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


#: ``CredentialShare.source`` of a share ``provision-local`` creates for a
#: skill installed on a Desktop.
SHARE_SOURCE_SKILL_LOCAL_INSTALL = "skill_local_install"


class CredentialShareBase(SQLModel):
    """Base model for credential shares."""
    access_level: str = Field(default="read", max_length=20)  # Currently only 'read' is supported
    # Provenance of the share, stamped at creation time. "direct" = a user→user
    # share created via the credential detail sharing UI; "bundle_install" = an
    # auto-created share from installing a bundle whose publisher provides the
    # credential (PBP); "skill_install" = the same for a catalog skill whose
    # publisher provides a credential slot; "skill_local_install" = the same
    # for a skill installed on a Desktop (no agent, no link), the one automatic
    # source Desktop may copy locally. NULL = legacy row (pre-feature), read as
    # "direct" everywhere. First writer wins, with one exception:
    # provision-local promotes "skill_install" to "skill_local_install" (both
    # "automatic"). Never inferred after the fact, never accepted from client
    # input.
    source: str | None = Field(default=None, max_length=20)


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


class CredentialShareRevocation(SQLModel, table=True):
    """The credential's owner removed one person's share of it.

    Written when the owner deletes a share (``DELETE
    /credentials/{id}/shares/{share_id}``), whatever the share's source. While
    the row exists no automatic path shares the credential with that person
    again: provision-local answers ``not_shareable`` and a cloud bundle or
    skill install treats the publisher credential as unusable. Only the owner
    sharing it with that person again (the direct share route) removes it.
    Turning sharing off and on leaves the rows alone; turning it off writes
    none.
    """

    __tablename__ = "credential_share_revocation"
    __table_args__ = (
        UniqueConstraint(
            "credential_id",
            "user_id",
            name="uq_credential_share_revocation_credential_user",
        ),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    credential_id: uuid.UUID = Field(
        foreign_key="credential.id", ondelete="CASCADE", nullable=False
    )
    #: The person the share was removed from.
    user_id: uuid.UUID = Field(foreign_key="user.id", ondelete="CASCADE", nullable=False)
    revoked_by_user_id: uuid.UUID = Field(
        foreign_key="user.id", ondelete="CASCADE", nullable=False
    )
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class CredentialShareAgentUsage(SQLModel):
    """A recipient's cloud agent that links the shared credential through at
    least one of the owner's catalog skills."""

    id: uuid.UUID
    name: str
    #: Display names of the owner's catalog skills installed on this agent
    #: that provide the credential.
    skill_display_names: list[str] = []


class CredentialShareLocalSkillUsage(SQLModel):
    """A skill of the owner, providing the credential, that the recipient
    installed into a local agent on Cinna Desktop."""

    package_uuid: uuid.UUID
    package_id: str
    display_name: str
    revision_number: int
    #: The local agent's catalog name, only when that agent is shared through
    #: core by the recipient themselves.
    host_agent_name: str | None = None


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
    source: str | None = None  # "direct" | "bundle_install" | "skill_install" | "skill_local_install"; NULL = legacy (direct)
    # What removing this share breaks for the recipient, so the owner can be
    # warned first. Filled by the owner's share list only. Agents that use the
    # credential without any of the owner's skills are counted, not named.
    agent_usages: list[CredentialShareAgentUsage] = []
    direct_agent_use_count: int = 0
    local_skill_usages: list[CredentialShareLocalSkillUsage] = []


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
    # Tab discriminator computed via CredentialsService.classify_credential_category.
    # For shared rows this is "bundle" (share.source == "bundle_install"),
    # "automatic" (share.source in "skill_install"/"skill_local_install") or "mine".
    category: str = "mine"
    # Raw provenance marker (debug / forward-compat).
    # "direct" | "bundle_install" | "skill_install" | "skill_local_install" | None.
    source: str | None = None
    # Agents the recipient has linked this shared credential to (recipient-scoped).
    agent_usage_count: int = 0
    # Always False for shared rows in MVP (bundle badge is an owner concept);
    # kept for shape symmetry with CredentialPublic.
    used_in_bundle: bool = False


class SharedCredentialsPublic(SQLModel):
    """Response model for list of credentials shared with current user."""
    data: list[SharedCredentialPublic]
    count: int
