"""AgentBundle — canonical bundle metadata, owned by a publisher.

Phase 2 of the Agent Bundles & Installs plan: a ``Bundle`` is the
publisher-owned, versioned definition of an agent. Other users **install**
the bundle, which produces an ``Agent`` (Install) row plus its own per-user
``AppDataVolume``.

One ``AgentBundle`` row corresponds to a unique ``bundle_id`` (reverse-DNS
identifier) on this Cinna instance. The publisher's working install (the
``Agent`` row with ``is_publisher_install=True`` and matching ``bundle_uuid``)
is the source of truth for the next snapshot. Each publish action writes a
new immutable ``AgentBundleRevision`` snapshot.
"""
import uuid
from datetime import datetime, UTC

from sqlmodel import Field, SQLModel, Column
from sqlalchemy import CheckConstraint, Index, String, UniqueConstraint, text, Text


# Default install/update mode constants. Mirror ``UpdateMode`` on Agent for
# clarity at call sites that operate on bundles directly.
class BundleVisibility:
    """Catalog visibility levels."""
    PRIVATE = "private"   # Publisher only
    USERS = "users"        # Allowlist via BundleAccessGrant
    PUBLIC = "public"      # All authenticated users on this instance


class BundleRunsOn:
    """Where an installed copy of the bundle runs.

    ``cloud`` — installed into Cinna (snapshot revisions, ``/catalog`` installs).
    ``desktop`` — a desktop agent: content lives in a ``git_repo``, people get
    it by git clone in Cinna Desktop; every cloud path refuses it
    (``desktop_only``).
    """
    CLOUD = "cloud"
    DESKTOP = "desktop"

    ALL = (CLOUD, DESKTOP)


class BundleInstallMode:
    """Default install mode for new installs of a bundle."""
    MANUAL = "manual"
    AUTOMATIC = "automatic"


class AgentBundleBase(SQLModel):
    """Shared fields for AgentBundle CRUD + responses."""
    display_name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    is_listed: bool = False
    visibility: str = Field(default=BundleVisibility.PRIVATE, max_length=32)
    default_install_mode: str = Field(default=BundleInstallMode.MANUAL, max_length=32)


class AgentBundle(AgentBundleBase, table=True):
    """Database model for canonical bundle metadata."""

    __tablename__ = "agent_bundle"
    __table_args__ = (
        UniqueConstraint("bundle_id", name="uq_agent_bundle_bundle_id"),
        Index("ix_agent_bundle_publisher", "publisher_user_id"),
        Index(
            "ix_agent_bundle_listed_visibility",
            "is_listed",
            "visibility",
            postgresql_where=text("is_listed = true"),
        ),
        Index(
            "uq_agent_bundle_desktop_manifest_id",
            "manifest_id",
            unique=True,
            postgresql_where=text("runs_on = 'desktop'"),
        ),
        Index(
            "uq_agent_bundle_desktop_slug",
            "slug",
            unique=True,
            postgresql_where=text("runs_on = 'desktop'"),
        ),
        CheckConstraint(
            "runs_on = 'cloud' OR (git_repo_id IS NOT NULL AND manifest_id IS NOT NULL "
            "AND slug IS NOT NULL)",
            name="ck_agent_bundle_desktop_fields",
        ),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)

    # Reverse-DNS bundle identifier — globally unique on this instance.
    # Stable across re-publishes; immutable once any revision exists.
    bundle_id: str = Field(max_length=255, nullable=False)

    # Publisher cannot be deleted while their bundles exist (RESTRICT).
    # NULL = ownerless / shared row. A catalog publish always sets this to the
    # publisher; a git-sourced import (the checking-out user is NOT the
    # publisher) creates the row ownerless so ONE shared bundle row backs every
    # user's checkout of the same repo — exactly how catalog installs share one
    # bundle across many consumers. An ownerless row stays private/unlisted and
    # never surfaces in the public catalog.
    publisher_user_id: uuid.UUID | None = Field(
        default=None, foreign_key="user.id", nullable=True, ondelete="RESTRICT"
    )

    # Pointer to the latest published revision (NULL until first publish).
    latest_revision_id: uuid.UUID | None = Field(
        default=None,
        foreign_key="agent_bundle_revision.id",
        ondelete="SET NULL",
    )

    # Optional publisher-provided AI credentials. NULL = user provides AI at
    # install time (current default). When set, foreign installs link to the
    # publisher's row by reference (via ``AICredentialShare``) instead of
    # snapshotting the API key. The mode lives on the bundle (not on the
    # revision) so a publisher can flip "I now provide AI" without
    # re-publishing.
    publisher_ai_credential_conversation_id: uuid.UUID | None = Field(
        default=None,
        foreign_key="ai_credential.id",
        ondelete="SET NULL",
    )
    publisher_ai_credential_building_id: uuid.UUID | None = Field(
        default=None,
        foreign_key="ai_credential.id",
        ondelete="SET NULL",
    )

    # ``cloud`` / ``desktop`` (:class:`BundleRunsOn`); immutable.
    runs_on: str = Field(
        default=BundleRunsOn.CLOUD,
        sa_column=Column(
            String(16), nullable=False, server_default=text(f"'{BundleRunsOn.CLOUD}'")
        ),
    )
    # Desktop agents only: the bare repo holding the content (1:1).
    git_repo_id: uuid.UUID | None = Field(
        default=None,
        foreign_key="git_repo.id",
        ondelete="RESTRICT",
        nullable=True,
        unique=True,
    )
    # Desktop agents only: the ``cinna-agent.json`` ``id`` bound at create; a
    # push whose manifest carries another id is refused.
    manifest_id: uuid.UUID | None = Field(default=None, nullable=True)
    # Desktop agents only: the kit slug (``Local/<slug>/`` on the desktop).
    slug: str | None = Field(default=None, max_length=64, nullable=True)

    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


# ── Public schemas ──────────────────────────────────────────────────


class AgentBundlePublic(SQLModel):
    """Response schema for ``GET /bundles/...``."""

    id: uuid.UUID
    bundle_id: str
    display_name: str
    description: str | None
    publisher_user_id: uuid.UUID | None = None  # NULL for ownerless git imports
    publisher_handle: str | None = None  # e.g. truncated name; never email
    publisher_name: str | None = None
    publisher_email: str | None = None
    publisher_email_confirmed: bool = False
    latest_revision_id: uuid.UUID | None
    latest_revision_number: int | None = None
    is_listed: bool
    visibility: str
    default_install_mode: str
    install_count: int = 0
    publisher_ai_credential_conversation_id: uuid.UUID | None = None
    publisher_ai_credential_building_id: uuid.UUID | None = None
    created_at: datetime
    updated_at: datetime


class AgentBundlesPublic(SQLModel):
    data: list[AgentBundlePublic]
    count: int


class AgentBundleUpdate(SQLModel):
    """Editable fields on an existing bundle (publisher only)."""
    display_name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    is_listed: bool | None = None
    visibility: str | None = None
    default_install_mode: str | None = None
    # Optional publisher-provided AI credentials (Phase 1 of the install
    # redesign). Setting either to NULL clears the publisher-provides
    # state for that mode and reverts to "user provides at install time".
    publisher_ai_credential_conversation_id: uuid.UUID | None = None
    publisher_ai_credential_building_id: uuid.UUID | None = None
