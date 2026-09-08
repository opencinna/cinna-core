"""SkillPackageRevision — an immutable snapshot of one published skill.

Append-only, exactly like :class:`~app.models.bundles.agent_bundle_revision.AgentBundleRevision`:
each publish writes a new row plus a directory snapshot under
``<SKILL_STORAGE_DIR>/<package uuid>/<revision_number>/skills/<name>/``. Rows
are never edited — an install pins to a revision id, and a pinned artifact that
can change underneath the container is not an artifact.
"""
import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, Index, Text, UniqueConstraint, text
from sqlmodel import Column, Field, SQLModel


class SkillPackageRevision(SQLModel, table=True):
    """Database model for one immutable skill package revision."""

    __tablename__ = "skill_package_revision"
    __table_args__ = (
        UniqueConstraint(
            "package_id",
            "revision_number",
            name="uq_skill_package_revision_number",
        ),
        Index("ix_skill_package_revision_package", "package_id"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)

    # CASCADE: deleting the package deletes its revisions. Links that pointed at
    # them survive with ``skill_package_revision_id = NULL`` and render as
    # "source unavailable" (§9), which is the state the container reports as
    # ``catalog_revision_missing``.
    package_id: uuid.UUID = Field(
        foreign_key="skill_package.id", nullable=False, ondelete="CASCADE"
    )

    # Monotonic per package, starting at 1. Part of the snapshot path, so it is
    # allocated under the publish lock and never reused.
    revision_number: int = Field(nullable=False)

    # Publisher-entered label ("1.0", "2026.09"). Independent of
    # ``revision_number``, which is the internal identity.
    version: str | None = Field(default=None, max_length=64)

    # The parsed ``SKILL.md`` frontmatter as published — name, description and
    # every optional key the standard allows, passed through untouched. This is
    # what the catalog card and the synthesised ``plugin.json`` read, so the
    # backend never has to re-open the snapshot to answer "what is this skill".
    frontmatter: dict = Field(
        default_factory=dict,
        sa_column=Column(JSON, nullable=False, server_default=text("'{}'::json")),
    )

    # Absolute path of the snapshot's ``skills/<name>/`` directory. Stored
    # rather than recomputed so a future storage-root move is a data migration
    # instead of a silent 404 on every old revision.
    snapshot_path: str = Field(max_length=1024, nullable=False)

    # SHA-256 over the snapshot tree (``PublishService.hash_workspace_tree``):
    # path + bytes, no timestamps. Identity of the *content*.
    content_hash: str = Field(max_length=64, nullable=False)

    # SHA-256 of the *archive* the container downloads — a different digest
    # over different bytes (the gzipped tarball), which is why both columns
    # exist. Stored rather than computed on demand because the plugin manifest
    # carries it, and that manifest is built on the asyncio event loop from
    # environment activation and every allowed-tools sync: reading or gzipping
    # a snapshot there would stall session streaming. Computed once at publish,
    # in the same worker thread that writes the snapshot.
    #
    # Nullable as a guard, not as a live state: every publish path writes this
    # or fails, so no row reaches NULL today. It stays nullable so a future
    # backfill or restore degrades to "the container reports
    # ``catalog_revision_missing``" instead of breaking a manifest build.
    archive_sha256: str | None = Field(default=None, max_length=64)

    size_bytes: int = Field(default=0, nullable=False)

    release_notes: str | None = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )

    published_by_user_id: uuid.UUID | None = Field(
        default=None, foreign_key="user.id", nullable=True, ondelete="SET NULL"
    )
    published_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
