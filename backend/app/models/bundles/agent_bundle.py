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

from sqlmodel import Field, SQLModel
from sqlalchemy import Index, text


# Default install/update mode constants. Mirror ``UpdateMode`` on Agent for
# clarity at call sites that operate on bundles directly.
class BundleVisibility:
    """Catalog visibility levels."""
    PRIVATE = "private"   # Publisher only
    USERS = "users"        # Allowlist via BundleAccessGrant
    PUBLIC = "public"      # All authenticated users on this instance


class BundleInstallMode:
    """Default install mode for new installs of a bundle."""
    MANUAL = "manual"
    AUTOMATIC = "automatic"


class AgentBundleBase(SQLModel):
    """Shared fields for AgentBundle CRUD + responses."""
    display_name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    is_listed: bool = False
    visibility: str = Field(default=BundleVisibility.PRIVATE, max_length=32)
    default_install_mode: str = Field(default=BundleInstallMode.MANUAL, max_length=32)


class AgentBundle(AgentBundleBase, table=True):
    """Database model for canonical bundle metadata."""

    __tablename__ = "agent_bundle"
    __table_args__ = (
        Index("ix_agent_bundle_publisher", "publisher_user_id"),
        Index(
            "ix_agent_bundle_listed_visibility",
            "is_listed",
            "visibility",
            postgresql_where=text("is_listed = true"),
        ),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)

    # Reverse-DNS bundle identifier — globally unique on this instance.
    # Stable across re-publishes; immutable once any revision exists.
    bundle_id: str = Field(max_length=255, nullable=False, unique=True, index=True)

    # Publisher cannot be deleted while their bundles exist (RESTRICT).
    publisher_user_id: uuid.UUID = Field(
        foreign_key="user.id", nullable=False, ondelete="RESTRICT"
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

    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


# ── Public schemas ──────────────────────────────────────────────────


class AgentBundlePublic(SQLModel):
    """Response schema for ``GET /bundles/...``."""

    id: uuid.UUID
    bundle_id: str
    display_name: str
    description: str | None
    publisher_user_id: uuid.UUID
    publisher_handle: str | None = None  # e.g. truncated name; never email
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
