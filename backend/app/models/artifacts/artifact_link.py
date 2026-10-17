"""ArtifactLink — a public link to an artifact.

Only the SHA-256 of the token is stored (plus its first 8 characters for the
list UI); the token itself is shown once, at creation. The optional 4-digit
code is Fernet-encrypted so the owner can read it back, like guest shares.
``failed_attempts >= ARTIFACT_LINK_MAX_CODE_ATTEMPTS`` means blocked (derived,
no column).

``pinned_revision_id`` has no ``ON DELETE`` action (Postgres ``NO ACTION``):
deleting a pinned revision directly fails, while deleting the whole artifact
cascades to links and revisions in the same statement without tripping it
(``RESTRICT`` would be checked mid-cascade). The service answers 409 before the
database gets the chance (D17).
"""
import uuid
from datetime import UTC, datetime

from sqlalchemy import Column, DateTime, Index, Text
from sqlmodel import Field, SQLModel


def _utc_now() -> datetime:
    return datetime.now(UTC)


class ArtifactLink(SQLModel, table=True):
    """Database model for a public artifact link."""

    __tablename__ = "artifact_link"
    __table_args__ = (
        Index("ix_artifact_link_artifact", "artifact_id"),
        Index("ix_artifact_link_pinned_revision", "pinned_revision_id"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    artifact_id: uuid.UUID = Field(
        foreign_key="artifact.id", nullable=False, ondelete="CASCADE"
    )
    token_hash: str = Field(max_length=64, nullable=False, unique=True)
    token_prefix: str = Field(max_length=8, nullable=False)
    label: str | None = Field(default=None, max_length=200)
    expires_at: datetime | None = Field(
        default=None, sa_type=DateTime(timezone=True), nullable=True
    )
    code_encrypted: str | None = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )
    failed_attempts: int = Field(
        default=0, nullable=False, sa_column_kwargs={"server_default": "0"}
    )
    pinned_revision_id: uuid.UUID | None = Field(
        default=None, foreign_key="artifact_revision.id", nullable=True
    )
    created_by: uuid.UUID | None = Field(
        default=None, foreign_key="user.id", nullable=True, ondelete="SET NULL"
    )
    created_at: datetime = Field(
        default_factory=_utc_now, sa_type=DateTime(timezone=True), nullable=False
    )
    revoked_at: datetime | None = Field(
        default=None, sa_type=DateTime(timezone=True), nullable=True
    )


# ── API schemas ─────────────────────────────────────────────────────


class ArtifactLinkCreate(SQLModel):
    label: str | None = Field(default=None, max_length=200)
    expires_at: datetime | None = None
    with_code: bool = False
    pinned_revision_id: uuid.UUID | None = None


class ArtifactLinkPublic(SQLModel):
    id: uuid.UUID
    token_prefix: str
    label: str | None = None
    created_at: datetime
    expires_at: datetime | None = None
    revoked_at: datetime | None = None
    pinned_revision_id: uuid.UUID | None = None
    pinned_revision_number: int | None = None
    needs_code: bool = False
    # Owner-only: the decrypted 4-digit code.
    code: str | None = None
    failed_attempts: int = 0
    expired: bool = False
    blocked: bool = False


class ArtifactLinkCreated(ArtifactLinkPublic):
    """Creation response: the only time the plaintext token is returned."""

    token: str
    url: str


class ArtifactLinkInfo(SQLModel):
    """``GET /artifact-links/{token}/info`` — no auth."""

    title: str
    description: str | None = None
    owner_name: str | None = None
    # ``core_agent`` = published by a cloud agent; ``desktop`` / ``cli`` /
    # ``upload`` = published by a person (or their local tooling).
    source_kind: str
    needs_code: bool
    expired: bool
    blocked: bool


class ArtifactLinkOpen(SQLModel):
    code: str | None = Field(default=None, max_length=16)


class ArtifactLinkCodeRefused(SQLModel):
    """``detail`` of a 403 from ``/artifact-links/{token}/open``."""

    message: str
    attempts_left: int
    blocked: bool

