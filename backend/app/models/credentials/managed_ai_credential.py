import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlmodel import Field, Relationship, SQLModel, Column, Text, Index
from sqlalchemy.dialects.postgresql import JSON as PG_JSON

from app.models.credentials.ai_credential import AICredentialType

if TYPE_CHECKING:
    from app.models.users.user import User


def _default_sdk_modes() -> list[str]:
    """Default SDK modes wired when ``set_user_sdk_defaults`` is enabled."""
    return ["conversation", "building"]


# ============= Parent table =============


class ManagedAICredential(SQLModel, table=True):
    """Admin-managed parent record for AI credentials.

    A single parent record that a superuser manages once. Its canonical config
    (name/type/key/base_url/model/default flags) and target user set are the
    source of truth, reconciled into per-user ``AICredential`` child rows on
    every change. Membership is **derived** from the children (those whose
    ``managed_credential_id`` points at this parent) — there is deliberately no
    ``target_user_ids`` column here, to avoid drift between intended and actual
    members.

    The parent holds its own Fernet-encrypted copy of the key (same shape/codec
    as ``ai_credential.encrypted_data``) so new children can be created and
    existing children re-keyed without the admin re-typing the secret.
    """

    __tablename__ = "managed_ai_credential"
    __table_args__ = (
        Index("ix_managed_ai_credential_managed_by", "managed_by_id"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    name: str = Field(min_length=1, max_length=255, nullable=False)
    type: AICredentialType = Field(..., sa_type=sa.String(50))

    # Fernet-encrypted JSON {api_key, base_url?, model?} — the canonical key.
    encrypted_data: str = Field(sa_column=Column(Text, nullable=False))

    # Non-secret mirrors for projection/UI (openai_compatible/google).
    base_url: str | None = Field(default=None, max_length=500)
    model: str | None = Field(default=None, max_length=255)

    # Admin-curated model metadata (see admin_curated_model_list). Reconciled
    # onto each child AICredential row. Both non-secret.
    # - default_model: admin's preferred default model (bare concrete id).
    # - available_models: curated list of selectable model ids (NULL/empty =
    #   offer the per-credential auto-discovered list).
    default_model: str | None = Field(default=None, max_length=255)
    available_models: list[str] | None = Field(
        default=None, sa_column=Column(PG_JSON, nullable=True)
    )

    # Desired: set each child as its owner's default for the type.
    set_as_default: bool = Field(
        default=False,
        sa_column=Column(
            sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    # Desired: wire each owner's default_sdk_* to their child.
    set_user_sdk_defaults: bool = Field(
        default=False,
        sa_column=Column(
            sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    # Modes wired when set_user_sdk_defaults=True.
    sdk_default_modes: list[str] = Field(
        default_factory=_default_sdk_modes,
        sa_column=Column(
            PG_JSON,
            nullable=False,
            server_default=sa.text("""'["conversation", "building"]'::json"""),
        ),
    )

    expiry_notification_date: datetime | None = Field(default=None)

    # Admin who owns/manages the record (audit + "who provisioned"). SET NULL on
    # admin deletion — record stays fleet-manageable by any superuser.
    managed_by_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(
            sa.Uuid(),
            sa.ForeignKey("user.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    managed_by: "User" = Relationship(
        sa_relationship_kwargs={
            "foreign_keys": "[ManagedAICredential.managed_by_id]"
        }
    )


# ============= Projection DTOs =============


class ManagedAICredentialMember(SQLModel):
    """One member of a managed AI credential record — i.e. one child credential
    and the user who owns it."""

    user_id: uuid.UUID
    email: str
    full_name: str | None = None
    child_credential_id: uuid.UUID
    is_default: bool = False


class ManagedAICredentialPublic(SQLModel):
    """Admin-facing projection of a managed AI credential parent record.

    Never includes ``encrypted_data`` or any key material.
    """

    id: uuid.UUID
    name: str
    type: AICredentialType
    base_url: str | None = None
    model: str | None = None
    default_model: str | None = None
    available_models: list[str] | None = None
    set_as_default: bool = False
    set_user_sdk_defaults: bool = False
    sdk_default_modes: list[str] = Field(default_factory=_default_sdk_modes)
    expiry_notification_date: datetime | None = None
    managed_by_id: uuid.UUID | None = None
    has_api_key: bool = True  # Always true — a parent always holds a key.
    is_oauth_token: bool = False  # Derived from type/key prefix (as today).
    members: list[ManagedAICredentialMember] = Field(default_factory=list)
    member_count: int = 0
    created_at: datetime
    updated_at: datetime


class ManagedAICredentialCreate(SQLModel):
    """Admin request to create a managed AI credential record.

    Creates the parent row + reconciles to create one ``AICredential`` child per
    valid target user.
    """

    name: str = Field(min_length=1, max_length=255)
    type: AICredentialType
    api_key: str = Field(min_length=1)
    base_url: str | None = Field(default=None, max_length=500)
    model: str | None = Field(default=None, max_length=255)
    # Admin-curated model metadata (normalized + prefix-stripped server-side).
    default_model: str | None = Field(default=None, max_length=255)
    available_models: list[str] | None = None
    expiry_notification_date: datetime | None = None
    target_user_ids: list[uuid.UUID] = Field(min_length=1)
    set_as_default: bool = False
    set_user_sdk_defaults: bool = False
    sdk_default_modes: list[str] = Field(default_factory=_default_sdk_modes)


class ManagedAICredentialUpdate(SQLModel):
    """Admin request to update a managed AI credential record (partial update).

    Omitting ``api_key`` keeps the stored key. Omitting ``target_user_ids``
    leaves membership unchanged.
    """

    name: str | None = Field(default=None, min_length=1, max_length=255)
    api_key: str | None = Field(default=None, min_length=1)
    base_url: str | None = Field(default=None, max_length=500)
    model: str | None = Field(default=None, max_length=255)
    # Admin-curated model metadata (partial update). For ``available_models``:
    # ``None`` = leave unchanged; ``[]`` = clear curation (fall back to
    # discovered). ``default_model``: ``None`` leaves unchanged (use the
    # explicit ``""`` -> normalized to clear if ever needed; today blank stays).
    default_model: str | None = Field(default=None, max_length=255)
    available_models: list[str] | None = None
    expiry_notification_date: datetime | None = None
    target_user_ids: list[uuid.UUID] | None = None
    set_as_default: bool | None = None
    set_user_sdk_defaults: bool | None = None
    sdk_default_modes: list[str] | None = None


class ManagedReconcileSkip(SQLModel):
    """A target user skipped during reconcile (unknown/inactive)."""

    user_id: uuid.UUID
    reason: str


class ManagedReconcileBlock(SQLModel):
    """A member that could not be removed because a child is in use (Tier-2
    blast radius). ``impact`` carries the deletion-impact payload."""

    user_id: uuid.UUID
    reason: str
    impact: dict | None = None


class ManagedAICredentialReconcileResult(SQLModel):
    """Result of a create/update reconcile call."""

    record: ManagedAICredentialPublic
    added: list[ManagedAICredentialMember] = Field(default_factory=list)
    removed: list[uuid.UUID] = Field(default_factory=list)
    # Members whose child row was actually mutated this reconcile (field/key
    # write-through or default change). Empty on a no-op. ``updated_count`` is
    # kept as the convenience scalar (== len(updated)).
    updated: list[ManagedAICredentialMember] = Field(default_factory=list)
    updated_count: int = 0
    skipped: list[ManagedReconcileSkip] = Field(default_factory=list)
    blocked: list[ManagedReconcileBlock] = Field(default_factory=list)
