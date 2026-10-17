"""Artifact — a published site or file set a person (or their agent) shares.

An artifact is a stable identity (title, visibility, source) over a sequence
of immutable revisions (:mod:`.artifact_revision`). ``current_revision_id`` is
a plain indexed UUID, deliberately without a foreign key: a circular
``artifact`` ⇄ ``artifact_revision`` FK with cascades is the known DEFERRABLE
trap (D16). The artifact services keep it consistent.

Not to be confused with ``TaskArtifact`` / ``DelegationArtifact`` (A13).
"""
import uuid
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import DateTime, Index, text
from sqlmodel import Field, SQLModel

from app.models.artifacts.artifact_grant import ArtifactGrantPublic
from app.models.artifacts.artifact_link import ArtifactLinkPublic
from app.models.artifacts.artifact_revision import (
    ArtifactFileEntry,
    ArtifactRevisionPublic,
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


class ArtifactKind:
    SITE = "site"
    FILES = "files"

    ALL = (SITE, FILES)


class ArtifactVisibility:
    PRIVATE = "private"
    INSTANCE = "instance"

    ALL = (PRIVATE, INSTANCE)


class ArtifactSourceKind:
    DESKTOP = "desktop"
    CORE_AGENT = "core_agent"
    CLI = "cli"
    UPLOAD = "upload"

    ALL = (DESKTOP, CORE_AGENT, CLI, UPLOAD)


ArtifactKindLiteral = Literal["site", "files"]
ArtifactVisibilityLiteral = Literal["private", "instance"]
ArtifactSourceKindLiteral = Literal["desktop", "core_agent", "cli", "upload"]


class Artifact(SQLModel, table=True):
    """Database model for an artifact."""

    __tablename__ = "artifact"
    __table_args__ = (
        Index("ix_artifact_owner_updated", "owner_id", "updated_at"),
        # A12: a core agent republishing the same workspace path updates the
        # same artifact.
        Index(
            "uq_artifact_owner_source_key",
            "owner_id",
            "source_key",
            unique=True,
            postgresql_where=text("source_key IS NOT NULL"),
        ),
        Index("ix_artifact_visibility", "visibility"),
        Index("ix_artifact_current_revision", "current_revision_id"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    owner_id: uuid.UUID = Field(
        foreign_key="user.id", nullable=False, ondelete="CASCADE"
    )
    user_workspace_id: uuid.UUID | None = Field(
        default=None, foreign_key="user_workspace.id", nullable=True, ondelete="SET NULL"
    )
    title: str = Field(max_length=200, nullable=False)
    description: str | None = Field(default=None, max_length=2000)
    kind: str = Field(default=ArtifactKind.FILES, max_length=16, nullable=False)
    # Relative path of the HTML entry; set for ``site``.
    entry_path: str | None = Field(default=None, max_length=1024)
    # No FK on purpose (D16).
    current_revision_id: uuid.UUID | None = Field(default=None, nullable=True)
    visibility: str = Field(
        default=ArtifactVisibility.PRIVATE,
        max_length=16,
        nullable=False,
        sa_column_kwargs={"server_default": ArtifactVisibility.PRIVATE},
    )
    source_kind: str = Field(max_length=16, nullable=False)
    source_agent_id: uuid.UUID | None = Field(
        default=None, foreign_key="agent.id", nullable=True, ondelete="SET NULL"
    )
    source_session_id: uuid.UUID | None = Field(
        default=None, foreign_key="session.id", nullable=True, ondelete="SET NULL"
    )
    # Free text, e.g. a desktop agent's name and folder.
    source_label: str | None = Field(default=None, max_length=200)
    # Core agents only: ``<env_id>:<path>`` for A12 update matching.
    source_key: str | None = Field(default=None, max_length=1024)
    created_at: datetime = Field(
        default_factory=_utc_now, sa_type=DateTime(timezone=True), nullable=False
    )
    updated_at: datetime = Field(
        default_factory=_utc_now, sa_type=DateTime(timezone=True), nullable=False
    )


# ── API schemas ─────────────────────────────────────────────────────


class ArtifactPublic(SQLModel):
    id: uuid.UUID
    title: str
    description: str | None = None
    kind: ArtifactKindLiteral
    entry_path: str | None = None
    visibility: ArtifactVisibilityLiteral
    source_kind: ArtifactSourceKindLiteral
    source_label: str | None = None
    source_agent_id: uuid.UUID | None = None
    # Owner only: the conversation that published it (core agents).
    source_session_id: uuid.UUID | None = None
    user_workspace_id: uuid.UUID | None = None
    current_revision_id: uuid.UUID | None = None
    current_revision_number: int | None = None
    revision_count: int = 0
    # Files in the current revision.
    file_count: int = 0
    # Sum over every revision (what counts against the owner's quota).
    size_bytes: int = 0
    owner_id: uuid.UUID
    owner_name: str | None = None
    is_owner: bool = False
    # Sharing summary; 0 for viewers who are not the owner.
    grant_count: int = 0
    # Links neither revoked nor expired.
    live_link_count: int = 0
    # The core page for this artifact.
    url: str
    created_at: datetime
    updated_at: datetime


class ArtifactPublished(ArtifactPublic):
    """Create / republish response: the artifact plus what this publish did."""

    revision_number: int
    # Paths the ingest dropped (A10) from this revision.
    excluded: list[str] = []


class ArtifactsPublic(SQLModel):
    data: list[ArtifactPublic]
    count: int


class ArtifactDetailPublic(ArtifactPublic):
    revisions: list[ArtifactRevisionPublic] = []
    # Owner only; None for everyone else.
    grants: list[ArtifactGrantPublic] | None = None
    links: list[ArtifactLinkPublic] | None = None


class ArtifactUpdate(SQLModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    entry_path: str | None = Field(default=None, max_length=1024)
    visibility: ArtifactVisibilityLiteral | None = None


class ArtifactViewPublic(SQLModel):
    """A view capability: where the revision's files are served from."""

    # ``<content origin>/a/<cap>/`` — every file URL is this + its path.
    content_base: str
    entry: str | None = None
    files: list[ArtifactFileEntry]
    revision_number: int
    expires_at: datetime
    # Zip of the revision, authorised by the same capability.
    download_url: str


class ArtifactsLimits(SQLModel):
    max_revision_bytes: int
    max_revision_files: int
    max_file_bytes: int
    user_quota_bytes: int


class ArtifactsCapabilities(SQLModel):
    version: int = 1
    limits: ArtifactsLimits
