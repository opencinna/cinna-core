"""AgentBundleRevision — immutable snapshot of a bundle's content.

Each ``Publish`` action creates one row; revisions are append-only.

A revision captures:
- Bundle folders on disk under ``<BUNDLE_STORAGE_DIR>/<bundle_id>/<revision>/``
  (scripts/, docs/, knowledge/, files/, workspace_requirements.txt,
  workspace_system_packages.txt).
- Prompts (workflow / entrypoint / refiner) at publish time.
- SDK selections + model overrides at publish time.
- ``required_credential_specs`` — names + types only; never secret values.
- A SHA-256 ``content_hash`` over the canonical snapshot tree for cache
  busting / dedup.

Old revisions stay around as long as the parent bundle exists; deletion is
cascade with the bundle. GC of unused revisions is out of scope for this
phase.
"""
import uuid
from datetime import datetime, UTC

from sqlmodel import Field, SQLModel, Column
from sqlalchemy import JSON, Index, UniqueConstraint, Text


class AgentBundleRevision(SQLModel, table=True):
    """Database model for an immutable bundle revision."""

    __tablename__ = "agent_bundle_revision"
    __table_args__ = (
        Index("ix_revision_bundle", "bundle_id"),
        UniqueConstraint(
            "bundle_id",
            "revision_number",
            name="uq_revision_bundle_number",
        ),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    bundle_id: uuid.UUID = Field(
        foreign_key="agent_bundle.id", nullable=False, ondelete="CASCADE"
    )

    # Monotonically increasing per bundle. Allocated by ``PublishService``.
    revision_number: int = Field(nullable=False)

    # Human-friendly version label entered by the publisher at publish
    # time (e.g. "1.0", "1.1", "2.0"). Independent from ``revision_number``,
    # which stays as the internal monotonic identifier used for snapshot
    # paths and ordering. Optional for backward compatibility with
    # revisions created before the field was introduced.
    version: str | None = Field(default=None, max_length=64)

    # Manifest mirrors what's written to ``manifest.json`` on disk so the
    # API can answer "what's in this revision" without reading the file.
    manifest: dict = Field(default_factory=dict, sa_column=Column(JSON))

    # Prompts copied from the publisher install at publish time.
    workflow_prompt: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    entrypoint_prompt: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    refiner_prompt: str | None = Field(default=None, sa_column=Column(Text, nullable=True))

    # SDK selections at publish time.
    agent_sdk_building: str | None = Field(default=None, max_length=128)
    agent_sdk_conversation: str | None = Field(default=None, max_length=128)
    model_override_building: str | None = Field(default=None, max_length=128)
    model_override_conversation: str | None = Field(default=None, max_length=128)

    # List of {name, type, allow_sharing, description?} for the install wizard.
    required_credential_specs: list = Field(default_factory=list, sa_column=Column(JSON))

    # Filesystem location of the snapshot under ``BUNDLE_STORAGE_DIR``.
    snapshot_path: str = Field(max_length=1024, nullable=False)

    # SHA-256 hex digest over canonical snapshot content.
    content_hash: str = Field(max_length=64, nullable=False)

    # Author of this revision (kept even if user deleted via SET NULL).
    published_by_user_id: uuid.UUID | None = Field(
        default=None, foreign_key="user.id", ondelete="SET NULL"
    )
    published_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    # Optional changelog — surfaced in update banners / catalog detail.
    release_notes: str | None = Field(default=None, sa_column=Column(Text, nullable=True))


# ── Public schemas ──────────────────────────────────────────────────


class AgentBundleRevisionPublic(SQLModel):
    """Response schema for revision listings + detail."""

    id: uuid.UUID
    bundle_id: uuid.UUID
    revision_number: int
    version: str | None = None
    manifest: dict | None = None
    content_hash: str
    workflow_prompt: str | None = None
    entrypoint_prompt: str | None = None
    refiner_prompt: str | None = None
    agent_sdk_building: str | None = None
    agent_sdk_conversation: str | None = None
    model_override_building: str | None = None
    model_override_conversation: str | None = None
    required_credential_specs: list = []
    published_by_user_id: uuid.UUID | None
    published_at: datetime
    release_notes: str | None = None
    install_count: int = 0


class AgentBundleRevisionsPublic(SQLModel):
    data: list[AgentBundleRevisionPublic]
    count: int


class PublishRequest(SQLModel):
    """Body of ``POST /agents/{agent_id}/publish``.

    ``bundle_id`` is only honoured on the first publish (the moment the
    bundle is defined). For subsequent publishes it is ignored — the
    bundle ID is locked once the bundle row exists.

    ``version`` is the human-friendly version label entered by the
    publisher (e.g. "1.0"). The frontend defaults it to ``"1.0"`` on the
    first publish and suggests a minor bump from the previous revision
    afterwards.
    """
    release_notes: str | None = None
    display_name: str | None = None
    description: str | None = None
    bundle_id: str | None = None
    version: str | None = None
