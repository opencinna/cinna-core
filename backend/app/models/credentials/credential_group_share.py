"""CredentialGroupShare — an owner's share of one credential with one user group.

Kept apart from :class:`~app.models.credentials.credential_share.CredentialShare`
on purpose (D5): that table has a non-null recipient, per-person provenance
(``source``), revocation records and a unique (credential, user) key that many
call sites rely on. A call site that does not know about this table therefore
**fails closed**: group members lack access rather than gain it by accident.

A group share is always the owner's explicit, "direct"-like share (D7). Removing
it writes no ``credential_share_revocation`` rows.
"""
import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, Index, UniqueConstraint
from sqlmodel import Field, SQLModel


class CredentialGroupShare(SQLModel, table=True):
    """Database model for one credential shared with one user group."""

    __tablename__ = "credential_group_share"
    __table_args__ = (
        UniqueConstraint("credential_id", "group_id", name="uq_credential_group_share"),
        Index("ix_credential_group_share_group", "group_id"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    credential_id: uuid.UUID = Field(
        foreign_key="credential.id", nullable=False, ondelete="CASCADE"
    )
    group_id: uuid.UUID = Field(
        foreign_key="user_group.id", nullable=False, ondelete="CASCADE"
    )
    # Always the credential's owner, so CASCADE shares the credential's fate.
    shared_by_user_id: uuid.UUID = Field(
        foreign_key="user.id", nullable=False, ondelete="CASCADE"
    )
    shared_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_type=DateTime(timezone=True),
        nullable=False,
    )


# ── API schemas ──────────────────────────────────────────────────────────────


class CredentialGroupShareCreate(SQLModel):
    """Request body for ``POST /credentials/{id}/group-shares``."""

    group_id: uuid.UUID


class CredentialGroupSharePublic(SQLModel):
    """One group share of a credential, as its owner sees it.

    ``links_at_risk`` counts the agent links revoking this share would drop:
    links held by members who have no other path to the credential (owner,
    direct share or another group share).
    """

    group_id: uuid.UUID
    group_name: str
    member_count: int = 0
    shared_by_user_id: uuid.UUID
    shared_at: datetime
    links_at_risk: int = 0


class CredentialGroupSharesPublic(SQLModel):
    data: list[CredentialGroupSharePublic]
    count: int
