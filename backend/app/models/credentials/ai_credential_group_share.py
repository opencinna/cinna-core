"""AICredentialGroupShare — an owner's share of one AI credential with one user group.

Same shape and rationale as
:class:`~app.models.credentials.credential_group_share.CredentialGroupShare`:
a separate table so a call site that does not know about group shares fails
closed. Admin-managed AI credentials are never group-shared (``is_shareable``).
"""
import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, Index, UniqueConstraint
from sqlmodel import Field, SQLModel


class AICredentialGroupShare(SQLModel, table=True):
    """Database model for one AI credential shared with one user group."""

    __tablename__ = "ai_credential_group_share"
    __table_args__ = (
        UniqueConstraint(
            "ai_credential_id", "group_id", name="uq_ai_credential_group_share"
        ),
        Index("ix_ai_credential_group_share_group", "group_id"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    ai_credential_id: uuid.UUID = Field(
        foreign_key="ai_credential.id", nullable=False, ondelete="CASCADE"
    )
    group_id: uuid.UUID = Field(
        foreign_key="user_group.id", nullable=False, ondelete="CASCADE"
    )
    # Always the AI credential's owner, so CASCADE shares its fate.
    shared_by_user_id: uuid.UUID = Field(
        foreign_key="user.id", nullable=False, ondelete="CASCADE"
    )
    shared_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_type=DateTime(timezone=True),
        nullable=False,
    )


# ── API schemas ──────────────────────────────────────────────────────────────


class AICredentialGroupShareCreate(SQLModel):
    """Request body for ``POST /ai-credentials/{id}/group-shares``."""

    group_id: uuid.UUID


class AICredentialEnvironmentsAtRisk(SQLModel):
    """Environments that lose this AI credential if the group share goes (D10).

    ``total`` environments of members with no other path pin the credential;
    ``running`` of them are restarted so their ``.env`` drops the key.
    """

    total: int = 0
    running: int = 0


class AICredentialGroupSharePublic(SQLModel):
    """One group share of an AI credential, as its owner sees it."""

    group_id: uuid.UUID
    group_name: str
    member_count: int = 0
    shared_by_user_id: uuid.UUID
    shared_at: datetime
    environments_at_risk: AICredentialEnvironmentsAtRisk = Field(
        default_factory=AICredentialEnvironmentsAtRisk
    )


class AICredentialGroupSharesPublic(SQLModel):
    data: list[AICredentialGroupSharePublic]
    count: int
