"""ArtifactRevision — one immutable published state of an artifact.

The files live on disk under ``<ARTIFACT_STORAGE_DIR>/<artifact_id>/<revision_id>/``;
the row carries the manifest (``files``) so the content route can answer
"is this path part of the revision" without touching the filesystem, and the
paths the ingest dropped (``excluded``, A10) so the UI can show them.
"""
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Column,
    DateTime,
    Index,
    Text,
    UniqueConstraint,
    text,
)
from sqlmodel import Field, SQLModel


def _utc_now() -> datetime:
    return datetime.now(UTC)


class ArtifactRevision(SQLModel, table=True):
    """Database model for an immutable artifact revision."""

    __tablename__ = "artifact_revision"
    __table_args__ = (
        UniqueConstraint("artifact_id", "number", name="uq_artifact_revision_number"),
        Index("ix_artifact_revision_artifact", "artifact_id"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    artifact_id: uuid.UUID = Field(
        foreign_key="artifact.id", nullable=False, ondelete="CASCADE"
    )
    # 1, 2, 3 … per artifact (the folder on disk is keyed by ``id``).
    number: int = Field(nullable=False)
    size_bytes: int = Field(default=0, sa_type=BigInteger, nullable=False)
    file_count: int = Field(default=0, nullable=False)
    # [{path, size, sha256, mime}, …] sorted by path.
    files: list = Field(
        default_factory=list,
        sa_column=Column(JSON, nullable=False, server_default=text("'[]'::json")),
    )
    # Relative paths dropped by the ingest (A10 patterns), sorted.
    excluded: list = Field(
        default_factory=list,
        sa_column=Column(JSON, nullable=False, server_default=text("'[]'::json")),
    )
    created_by: uuid.UUID | None = Field(
        default=None, foreign_key="user.id", nullable=True, ondelete="SET NULL"
    )
    note: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    created_at: datetime = Field(
        default_factory=_utc_now, sa_type=DateTime(timezone=True), nullable=False
    )


# ── API schemas ─────────────────────────────────────────────────────


class ArtifactFileEntry(SQLModel):
    """One file of a revision manifest."""

    path: str
    size: int
    sha256: str
    mime: str


class ArtifactRevisionPublic(SQLModel):
    id: uuid.UUID
    number: int
    size_bytes: int
    file_count: int
    files: list[ArtifactFileEntry]
    excluded: list[str]
    note: str | None = None
    created_by: uuid.UUID | None = None
    # Display name (full name or email) of ``created_by``.
    created_by_name: str | None = None
    created_at: datetime
    is_current: bool = False
    # Owner only: a live (not revoked, not expired) link pins this revision,
    # so it cannot be deleted.
    pinned: bool = False
