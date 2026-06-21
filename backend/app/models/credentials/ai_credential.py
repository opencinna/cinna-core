import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlmodel import Field, Relationship, SQLModel, Column, Text, Index

if TYPE_CHECKING:
    from app.models.users.user import User


class AICredentialType(str, Enum):
    """Type of AI credential/SDK provider"""
    # Existing types (backward compat)
    ANTHROPIC = "anthropic"
    MINIMAX = "minimax"
    OPENAI_COMPATIBLE = "openai_compatible"
    # New types added for OpenCode and expanded SDK support
    OPENAI = "openai"
    GOOGLE = "google"


# Shared properties
class AICredentialBase(SQLModel):
    """Base properties for AI credentials"""
    name: str = Field(min_length=1, max_length=255)
    type: AICredentialType = Field(..., sa_type=sa.String(50))
    expiry_notification_date: datetime | None = Field(default=None)


# Properties to receive on creation
class AICredentialCreate(AICredentialBase):
    """Create AI credential with sensitive data"""
    api_key: str = Field(min_length=1)
    # Optional fields — used depending on credential type
    # openai_compatible, google: base_url (optional endpoint override)
    base_url: str | None = Field(default=None, max_length=500)
    # openai_compatible: required default model name
    model: str | None = Field(default=None, max_length=255)


# Properties to receive on update
class AICredentialUpdate(SQLModel):
    """Update AI credential (partial update)"""
    name: str | None = Field(default=None, min_length=1, max_length=255)
    api_key: str | None = Field(default=None, min_length=1)
    # Optional fields for various types
    base_url: str | None = Field(default=None, max_length=500)
    model: str | None = Field(default=None, max_length=255)
    expiry_notification_date: datetime | None = Field(default=None)


# Database model
class AICredential(AICredentialBase, table=True):
    """AI credential database model with encrypted storage"""
    __tablename__ = "ai_credential"
    __table_args__ = (
        Index("ix_ai_credential_owner_type", "owner_id", "type"),
        Index("ix_ai_credential_owner_default", "owner_id", "is_default"),
        Index("ix_ai_credential_managed_credential", "managed_credential_id"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    owner_id: uuid.UUID = Field(
        foreign_key="user.id", nullable=False, ondelete="CASCADE"
    )
    # Encrypted JSON: {api_key, base_url?, model?}
    encrypted_data: str = Field(sa_column=Column(Text, nullable=False))
    is_default: bool = Field(default=False)

    # Admin provisioning (see admin_ai_credential_provisioning).
    # - is_admin_managed: the single behavioral discriminator. When True the row
    #   was provisioned by an admin FOR this owner and is READ-ONLY through the
    #   user-facing AI-credentials CRUD (the owner may use it / set it default,
    #   but cannot edit/delete/re-key it).
    # - managed_by_id: audit-only FK to the admin who provisioned it. NEVER used
    #   in an access decision. SET NULL on admin deletion so the user keeps their
    #   credential when the provisioning admin account is removed.
    is_admin_managed: bool = Field(
        default=False,
        sa_column=Column(
            sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    managed_by_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(
            sa.Uuid(),
            sa.ForeignKey("user.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
    )
    # Structural link to the parent ManagedAICredential record (admin-managed
    # parent/child model). NULL = a normal self-created credential. SET NULL on
    # parent deletion is a safety net only — parent deletion is expected to route
    # through the reconcile/delete path so each child gets proper profile
    # un-wiring + Tier-2 blast-radius gating. If a parent is ever deleted
    # out-of-band, children degrade to plain is_admin_managed orphans rather than
    # silently vanishing.
    managed_credential_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(
            sa.Uuid(),
            sa.ForeignKey("managed_ai_credential.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    expiry_notification_date: datetime | None = Field(default=None)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    # Per-credential model discovery cache (populated by the model discovery
    # cron). Different API keys can see different models, so the available
    # model list is cached per credential. These are non-secret.
    # - discovered_models: concrete provider model IDs this key can see.
    #   None = never discovered.
    # - models_discovered_at: timestamp of last SUCCESSFUL discovery.
    # - models_discovery_error: coarse reason code for the last failure
    #   (e.g. "oauth_token_unsupported"), not a raw API error body.
    discovered_models: list[str] | None = Field(
        default=None, sa_column=Column(sa.JSON, nullable=True)
    )
    models_discovered_at: datetime | None = Field(
        default=None, sa_type=sa.DateTime(timezone=True)
    )
    models_discovery_error: str | None = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )

    # Admin-curated model metadata (see admin_curated_model_list). Written ONLY
    # through the managed (admin) reconcile — never through the user-facing CRUD
    # (these fields are absent from AICredentialCreate/AICredentialUpdate). For
    # self-created credentials they stay NULL and every consumer falls back to
    # today's behavior (catalog default + discovered list). Both are non-secret.
    # - default_model: admin's preferred default model (bare concrete id). NULL =
    #   no curated default; the catalog tier default applies.
    # - available_models: admin-curated list of selectable model ids. NULL/empty
    #   = fall back to per-credential auto-discovery (discovered_models).
    default_model: str | None = Field(default=None, max_length=255)
    available_models: list[str] | None = Field(
        default=None, sa_column=Column(sa.JSON, nullable=True)
    )

    # Relationships
    owner: "User" = Relationship(
        sa_relationship_kwargs={"foreign_keys": "[AICredential.owner_id]"}
    )


# Properties to return via API
class AICredentialPublic(AICredentialBase):
    """Public AI credential (no sensitive data)"""
    id: uuid.UUID
    is_default: bool
    # Read-only marker for the owner's own UI. Whether the row was provisioned
    # by an admin. ``managed_by_id`` (which admin) is deliberately NOT exposed
    # here — it is admin-only (see AdminAICredentialPublic).
    is_admin_managed: bool = False
    has_api_key: bool = True  # Always true for existing credentials
    is_oauth_token: bool = False  # True if this is an OAuth token (sk-ant-oat*)
    # Safe to expose (no secret data)
    base_url: str | None = None     # For openai_compatible, google
    model: str | None = None        # For openai_compatible
    expiry_notification_date: datetime | None = None
    # Per-credential model discovery cache (non-secret). Lets the UI show the
    # models this key can access and surface "couldn't verify" states.
    discovered_models: list[str] | None = None
    models_discovered_at: datetime | None = None
    models_discovery_error: str | None = None
    # Admin-curated model metadata (read-only for the owner — see
    # admin_curated_model_list). Projected here so the SDK + native paths and the
    # owner UI can read them; the owner cannot mutate them (no corresponding
    # fields on AICredentialCreate/AICredentialUpdate). NULL for self-created
    # credentials.
    default_model: str | None = None
    available_models: list[str] | None = None
    created_at: datetime
    updated_at: datetime


class AICredentialsPublic(SQLModel):
    """List of AI credentials"""
    data: list[AICredentialPublic]
    count: int


# ============= Admin provisioning DTOs =============


class AdminAICredentialPublic(AICredentialPublic):
    """Admin-facing projection of an AI credential.

    Extends the shared :class:`AICredentialPublic` with the owner and
    provisioning-admin identity that are intentionally hidden from the
    owner-facing surface (see OQ-4). Used by the ``/admin/llm-providers/*``
    routes only.
    """
    owner_id: uuid.UUID
    managed_by_id: uuid.UUID | None = None


class AdminAICredentialCreate(SQLModel):
    """Admin request to provision an AI credential for one or more users.

    One :class:`AICredential` row is created per ``target_user_id`` with
    ``owner_id = target.id``, ``is_admin_managed=True`` and
    ``managed_by_id = admin.id``. The provided key bytes are shared across the
    created rows but each row is an independent credential owned by its user.
    """
    name: str = Field(min_length=1, max_length=255)
    type: AICredentialType
    api_key: str = Field(min_length=1)
    base_url: str | None = Field(default=None, max_length=500)
    model: str | None = Field(default=None, max_length=255)
    expiry_notification_date: datetime | None = None
    # Provisioning targets + behavior
    target_user_ids: list[uuid.UUID] = Field(min_length=1)
    set_as_default: bool = False
    set_user_sdk_defaults: bool = False
    # Which mode preferences to wire when set_user_sdk_defaults=True.
    sdk_default_modes: list[str] = Field(
        default_factory=lambda: ["conversation", "building"]
    )


class AdminProvisionSkip(SQLModel):
    """A target user that was skipped during provisioning (e.g. unknown or
    inactive). The whole call does not fail for an individual bad target."""
    user_id: uuid.UUID
    reason: str


class AdminAICredentialProvisionResult(SQLModel):
    """Result of an admin provision call — one created row per valid target,
    plus any skipped targets."""
    created: list[AdminAICredentialPublic]
    skipped: list[AdminProvisionSkip] = []


# Internal data structure for decrypted credential data
class AICredentialData(SQLModel):
    """Decrypted AI credential data (internal use only)"""
    api_key: str
    base_url: str | None = None
    model: str | None = None


# Test-connection request/response (used by POST /ai-credentials/test-connection)
class AICredentialTestRequest(SQLModel):
    """Request to validate an AI credential and refresh its model list.

    The key may come from the form (``api_key``) for the Add case, or be
    resolved from a stored credential (``credential_id``) for the Edit case.
    """
    type: AICredentialType
    api_key: str | None = None
    base_url: str | None = None
    credential_id: uuid.UUID | None = None


class AICredentialTestResult(SQLModel):
    """Result of a Test Connection probe.

    The ``error`` and ``skip_reason`` fields are mutually exclusive and keyed
    off ``success`` for an unambiguous contract:

    - ``success=True`` + non-empty ``models``: the key works and a model list
      was retrieved (``error`` and ``skip_reason`` both ``None``).
    - ``success=True`` + ``skip_reason`` set
      (``oauth_token_unsupported`` / ``no_list_endpoint`` / ``no_base_url``):
      the connection is considered valid but model listing isn't supported for
      this credential type/token — the UI shows an informative note.
    - ``success=False`` + ``error`` set (e.g. ``invalid_key``): the provider
      rejected the key (HTTP 401/403) or another hard failure occurred.
    """
    success: bool
    models: list[str] = []
    model_count: int = 0
    # Populated ONLY on success=False (real failure).
    error: str | None = None
    # Populated ONLY on success=True with a benign skip.
    skip_reason: str | None = None


# Affected environments query response models
class AffectedEnvironmentPublic(SQLModel):
    """Information about an environment affected by credential change"""
    environment_id: uuid.UUID
    agent_id: uuid.UUID
    agent_name: str
    environment_name: str
    status: str
    usage: str  # "conversation", "building", or "conversation & building"
    owner_id: uuid.UUID
    owner_email: str


class SharedUserPublic(SQLModel):
    """User who has access to this credential via share"""
    user_id: uuid.UUID
    email: str
    shared_at: datetime


class AffectedEnvironmentsPublic(SQLModel):
    """Response for affected environments query"""
    credential_id: uuid.UUID
    credential_name: str
    environments: list[AffectedEnvironmentPublic]
    shared_with_users: list[SharedUserPublic]
    count: int


class AICredentialBundleUsage(SQLModel):
    """One bundle that references this AI credential as a publisher-provided
    conversation and/or building credential.

    ``publisher_install_id`` is the publisher install's ``Agent.id`` so the
    frontend can deep-link into the agent's Bundle tab (the platform has no
    standalone ``/bundles/{uuid}`` route).
    """

    bundle_uuid: uuid.UUID
    bundle_id: str
    display_name: str
    publisher_install_id: uuid.UUID | None = None
    used_for_conversation: bool = False
    used_for_building: bool = False


class AICredentialDeletionImpact(SQLModel):
    """Blast-radius classification for deleting an AI credential.

    Deleting a publisher AI credential nulls the bundle's
    ``publisher_ai_credential_*_id`` FK via ``ON DELETE SET NULL``, silently
    degrading affected bundles to "user provides". ``tier`` grades the impact:

    - ``0`` — no published bundle references this credential; deletion is safe.
    - ``2`` — one or more bundles reference it as a publisher-provided AI
      credential; deletion is blocked (HTTP 409) unless ``force=true``.

    (There is no Tier 1 for AI credentials: direct AI-credential shares are
    materialised from bundle PBP wiring, so the bundle reference is the
    meaningful blast radius.)
    """

    tier: int
    bundle_usages: list[AICredentialBundleUsage] = []
