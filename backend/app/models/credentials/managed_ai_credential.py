import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlmodel import Field, Relationship, SQLModel, Column, Text, Index
from sqlalchemy.dialects.postgresql import JSON as PG_JSON

from app.models.credentials.ai_credential import AICredentialType
from app.models.credentials.managed_ai_credential_membership import (
    MembershipProvisioningStatus,
)
from app.models.users.user import VALID_USER_ROLES, AIKeyOnboardingState


class ProvisioningMode(str, Enum):
    """How a parent record gets each member their key.

    - ``shared`` — the administrator pastes one key and every member's child row
      holds a copy of it. The historical behaviour and the default; the only mode
      available for a provider whose API cannot create keys.
    - ``minted`` — each member gets their **own** key, created at the provider
      through a :class:`ProviderAdminCredential`. The parent holds no key of its
      own, which is why ``encrypted_data`` is nullable.
    """

    SHARED = "shared"
    MINTED = "minted"

if TYPE_CHECKING:
    from app.models.users.user import User


def _default_sdk_modes() -> list[str]:
    """Default SDK modes wired when ``set_user_sdk_defaults`` is enabled."""
    return ["conversation", "building"]


def _no_auto_provision_roles() -> list[str]:
    """Default for ``auto_provision_roles``: nobody, until an admin says so.

    Empty is the only safe default. A managed credential that auto-granted
    itself to every new account the moment the column shipped would hand a
    company API key to the next person who signed up, with no admin action
    anywhere in the story.
    """
    return []


# Roles that may appear in ``auto_provision_roles``. Deliberately the full
# ``UserRole`` set including ``admin``: an instance where every employee is an
# administrator is a normal small-team shape, and excluding it would make the
# feature silently useless there.
VALID_AUTO_PROVISION_ROLES = tuple(VALID_USER_ROLES)


# ============= Parent table =============


class ManagedAICredential(SQLModel, table=True):
    """Admin-managed parent record for AI credentials.

    A single parent record that a superuser manages once. Its canonical config
    (name/type/key/base_url/model/default flags) and target user set are the
    source of truth, reconciled into per-user ``AICredential`` child rows on
    every change.

    **Membership is a row**, one per (parent, user), in
    ``managed_ai_credential_membership``. It used to be derived from the children
    and is not any more — see that model's docstring for why, and note that the
    two do not coexist: the derived notion is gone, and a member is a membership
    row whatever its status.

    In ``shared`` mode the parent holds its own Fernet-encrypted copy of the key
    (same shape/codec as ``ai_credential.encrypted_data``) so new children can be
    created and existing children re-keyed without the admin re-typing the
    secret. In ``minted`` mode there is no such key: ``encrypted_data`` is NULL,
    each member's key is created at the provider, and a rotation request is
    refused because there is nothing here to rotate.
    """

    __tablename__ = "managed_ai_credential"
    __table_args__ = (
        Index("ix_managed_ai_credential_managed_by", "managed_by_id"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    name: str = Field(min_length=1, max_length=255, nullable=False)
    type: AICredentialType = Field(..., sa_type=sa.String(50))

    # Fernet-encrypted JSON {api_key, base_url?, model?} — the canonical key.
    # NULL in ``minted`` mode, where the parent holds no key at all. Every reader
    # goes through ``ManagedAICredentialsService._decrypt_parent``, which refuses
    # a minted parent rather than dereferencing the NULL.
    encrypted_data: str | None = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )

    # How members get their key. See :class:`ProvisioningMode`.
    provisioning_mode: str = Field(
        default=ProvisioningMode.SHARED.value,
        sa_column=Column(
            sa.String(24), nullable=False, server_default="shared"
        ),
    )
    # The admin secret used to mint (and later revoke) this record's per-user
    # keys. Required in ``minted`` mode, NULL in ``shared`` mode. SET NULL rather
    # than CASCADE for the same reason as ``managed_by_id``: losing the pointer
    # must degrade minting, not delete the record and its members' keys. Deleting
    # a provider admin credential that any parent still points at is refused by
    # the service, so this SET NULL is a safety net rather than a normal path.
    provider_admin_credential_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(
            sa.Uuid(),
            sa.ForeignKey("provider_admin_credential.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )

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

    # ── Auto-provisioning (zero-touch onboarding) ────────────────────
    # Roles whose *newly created* accounts receive this credential, granted by
    # ``AccountProvisioningService.on_account_created`` at the creation
    # chokepoint. Empty (the default) = never granted automatically; the admin
    # picks members by hand. Deliberately creation-time only: a role change on
    # an existing account does not re-run provisioning, because a promotion
    # should not silently hand out a company key. "Apply to existing users" is
    # the explicit path for that.
    auto_provision_roles: list[str] = Field(
        default_factory=_no_auto_provision_roles,
        sa_column=Column(
            PG_JSON,
            nullable=False,
            server_default=sa.text("""'[]'::json"""),
        ),
    )
    # Model the owner's ``default_model_override_<mode>`` is set to when this
    # parent wires that mode's SDK defaults. Separate from ``default_model``
    # (which is the credential's own preferred model, written onto the child
    # row): these two write to the *user profile*, and only for the modes in
    # ``sdk_default_modes``.
    #
    # NULL means "this record has no opinion about the model", and what that
    # produces depends on whether the slot is being *claimed* or merely
    # *held* — the asymmetry is deliberate, see ``_apply_sdk_defaults`` and
    # ``_sync_model_overrides``:
    #   * claiming it (a member is added, the credential pointer moves here)
    #     clears the owner's override, because the override that was there
    #     described a different credential and may name a model this provider
    #     does not serve;
    #   * holding it (an unrelated field of this record is edited) leaves the
    #     owner's override alone, because a user who picked their own model
    #     for this credential should not lose it to an admin renaming the
    #     record.
    # A stored NULL therefore cannot say whether it was never set or has just
    # been retracted, so the *request* that retracts one carries that fact
    # down instead (``ManagedAICredentialUpdate`` accepts ``""`` to clear, and
    # ``update()`` passes the dropped value to ``_sync_model_overrides``).
    model_override_conversation: str | None = Field(default=None, max_length=255)
    model_override_building: str | None = Field(default=None, max_length=255)

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
    """One member of a managed AI credential record.

    A member is a **membership row**, not a credential: on a minted record a
    person is a member from the moment the admin adds them, and their key exists
    a little later or not at all. ``provisioning_status`` says which, and it is
    always populated — the client reads that one field and never reconstructs the
    state from which other fields happen to be null.

    ``child_credential_id`` is therefore optional. It is ``None`` for exactly the
    statuses that mean "no key exists right now" (``pending``, ``minting``,
    ``failed``, ``suspended``), and set for the two that mean one does
    (``not_applicable``, ``provisioned``).
    """

    user_id: uuid.UUID
    email: str
    full_name: str | None = None
    child_credential_id: uuid.UUID | None = None
    is_default: bool = False
    #: One of :class:`MembershipProvisioningStatus`. **Required, no default.**
    #: A default here would be optional on the wire, and a client reading an
    #: absent status through a fallback is a client asserting a provisioning
    #: policy it inferred from a missing field. The one construction site
    #: already states it; nothing is left to infer.
    provisioning_status: MembershipProvisioningStatus
    #: Coarse failure reason for a ``failed``/retrying member. Never a provider
    #: response body and never key material.
    provision_error: str | None = None
    provision_attempts: int = 0
    #: **The owner's account-wide onboarding state** — the same field, computed
    #: by the same function, that ``/users/me/ai-credentials/status`` returns to
    #: that person's own dashboard. Not a property of this membership.
    #:
    #: It is here so an admin surface can say what is true of the person it just
    #: acted on. Creating a credential for somebody and announcing "they now
    #: have their own AI credential" is a claim about their account, and it was
    #: being made from the fact that a row had been written — while
    #: ``set_as_default`` defaults to false, so the person could be looking at
    #: the paste-a-key wall with that exact credential listed in their settings.
    #: The admin now reads the same answer the wall reads.
    api_key_onboarding_state: AIKeyOnboardingState


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
    auto_provision_roles: list[str] = Field(
        default_factory=_no_auto_provision_roles
    )
    model_override_conversation: str | None = None
    model_override_building: str | None = None
    expiry_notification_date: datetime | None = None
    managed_by_id: uuid.UUID | None = None
    #: Required for the same reason as ``provisioning_status`` above: an absent
    #: mode rendered through a client-side ``!== "minted"`` fallback is the
    #: browser deciding a record holds one shared key because a field did not
    #: arrive.
    provisioning_mode: ProvisioningMode
    provider_admin_credential_id: uuid.UUID | None = None
    # Whether this parent holds a key of its own. True for every shared record;
    # **false for every minted one**, which is why it is computed rather than the
    # constant it used to be. A reader who trusts the old "always true" comment
    # concludes a minted parent's key is missing rather than absent by design.
    has_api_key: bool = True
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
    # Required in ``shared`` mode, refused in ``minted`` mode — a minted parent
    # holds no key. Nullable rather than required-with-a-sentinel so the omission
    # is representable in the request model itself; the service raises the 400
    # that ties it to ``provisioning_mode``, because that rule is a policy and
    # policies live in one place, not in a validator on every write site.
    api_key: str | None = Field(default=None, min_length=1)
    provisioning_mode: ProvisioningMode = ProvisioningMode.SHARED
    # Required in ``minted`` mode: the admin secret the keys are minted with.
    provider_admin_credential_id: uuid.UUID | None = None
    base_url: str | None = Field(default=None, max_length=500)
    model: str | None = Field(default=None, max_length=255)
    # Admin-curated model metadata (normalized + prefix-stripped server-side).
    default_model: str | None = Field(default=None, max_length=255)
    available_models: list[str] | None = None
    expiry_notification_date: datetime | None = None
    # May be empty: an auto-provision-only credential legitimately starts with
    # no members and fills up as accounts are created.
    target_user_ids: list[uuid.UUID] = Field(default_factory=list)
    set_as_default: bool = False
    set_user_sdk_defaults: bool = False
    sdk_default_modes: list[str] = Field(default_factory=_default_sdk_modes)
    auto_provision_roles: list[str] = Field(
        default_factory=_no_auto_provision_roles
    )
    model_override_conversation: str | None = Field(default=None, max_length=255)
    model_override_building: str | None = Field(default=None, max_length=255)


class ManagedAICredentialUpdate(SQLModel):
    """Admin request to update a managed AI credential record (partial update).

    Omitting ``api_key`` keeps the stored key. Omitting ``target_user_ids``
    leaves membership unchanged.

    ``api_key`` on a **minted** record is refused with a 400 rather than ignored:
    there is no stored key to replace, and silently accepting a rotation that
    rotates nothing is how an admin comes to believe they have rolled a key they
    have not. Rotating a minted member's key is a per-member mint, not a parent
    edit.
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
    # ``None`` = leave unchanged; ``[]`` = stop auto-provisioning (existing
    # members keep their credential — this is not a revoke).
    auto_provision_roles: list[str] | None = None
    # Three distinct requests, and the distinction is the contract:
    #   * **omitted / ``null``** — leave the stored override alone. The common
    #     case: a PATCH that renames the record says nothing about models.
    #   * **``""``** — clear the override back to NULL. ``_normalize_default_model``
    #     turns a blank into ``None``, and ``update()`` carries the transition
    #     down so existing members pinned to the dropped value are unpinned
    #     too (a member who chose their own model keeps it). Without that a
    #     clear would be cosmetic — see ``_sync_model_overrides``.
    #   * **a model id** — set it, and write it through to every member who
    #     still has this credential in that slot.
    # This is why the field is not ``str | None`` with ``min_length=1``: the
    # empty string is a meaningful value here, not a malformed one.
    model_override_conversation: str | None = Field(default=None, max_length=255)
    model_override_building: str | None = Field(default=None, max_length=255)


class ManagedReconcileSkip(SQLModel):
    """A target user skipped during reconcile (unknown/inactive)."""

    user_id: uuid.UUID
    reason: str


#: The reasons a member removal can be refused, each with the one sentence that
#: explains it. **Declared here, once**, because a reason code with no stated
#: meaning is a reason code every consumer invents a meaning for — which is what
#: happened: the delete route's 409, the member-dialog toast and the force-delete
#: confirmation each said "in use by a published bundle" for all three reasons.
#: An administrator blocked by a mint that is still in flight was therefore told
#: it was a bundle conflict, whose obvious remedy is ``force=true``.
#:
#: A fourth reason cannot be introduced without a sentence to go with it — but
#: **this table is not what stops it**, and the difference is worth stating
#: because it was previously claimed the other way round. :meth:`
#: ManagedReconcileBlock.of` looks the reason up with a ``.get`` and falls back
#: to a generic sentence, so an unknown reason is accepted silently at runtime;
#: that fallback is deliberate (a block an admin cannot read is worse than a
#: vague one) and is therefore *not* enforcement. The enforcement is a test —
#: ``test_every_block_reason_the_service_can_produce_has_a_sentence`` — which
#: parses the services for ``ManagedReconcileBlock.of(reason=...)`` literals and
#: fails in both directions: a reason with no sentence, and a sentence for a
#: reason nothing produces.
MANAGED_RECONCILE_BLOCK_MESSAGES: dict[str, str] = {
    "in_use_bundle": (
        "Their credential is in use by a published bundle. Removing them "
        "anyway degrades that bundle back to \"user provides\"."
    ),
    "mint_in_flight": (
        "A key is being created for them right now. Try again in a moment — "
        "forcing it through does not help and is not needed."
    ),
    "remove_failed": (
        "Removing their credential failed unexpectedly. It has been logged; "
        "try again."
    ),
}


class ManagedReconcileBlock(SQLModel):
    """A member that could not be removed, and why.

    ``reason`` is the machine-readable code and ``message`` is the sentence for
    a person — **stated by the server, rendered by every client**. The message
    travels with the block rather than being looked up per consumer for the
    reason the table above gives: three consumers previously each substituted a
    constant of their own, and all three named the wrong cause for two of the
    three reasons.

    ``impact`` carries the deletion-impact payload, and only ``in_use_bundle``
    has one.
    """

    user_id: uuid.UUID
    reason: str
    #: Human-readable, server-authored. **Required, no default**, which is what
    #: makes :meth:`of` the only practical constructor: a default would let a
    #: new block site ship an empty sentence, and an empty sentence is exactly
    #: the vacuum the three clients filled with a constant of their own.
    message: str
    impact: dict | None = None

    @classmethod
    def of(
        cls, *, user_id: uuid.UUID, reason: str, impact: dict | None = None
    ) -> "ManagedReconcileBlock":
        """The only sanctioned constructor — it is what fills ``message``."""
        return cls(
            user_id=user_id,
            reason=reason,
            message=MANAGED_RECONCILE_BLOCK_MESSAGES.get(
                reason, "This member could not be removed."
            ),
            impact=impact,
        )


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


class ManagedAICredentialApplyCandidate(SQLModel):
    """A user who *would* receive this credential on "apply to existing".

    Not a :class:`ManagedAICredentialMember`: a member is identified by the
    child credential it owns, and on a dry run no child exists. Inventing a
    placeholder id for one would be a lie the frontend could not distinguish
    from a real member, so the preview gets its own shape.
    """

    user_id: uuid.UUID
    email: str
    full_name: str | None = None
    role: str


class ManagedAICredentialApplyResult(ManagedAICredentialReconcileResult):
    """Result of ``POST /{id}/apply-to-existing``.

    A reconcile result plus the preview fields. On a real run ``dry_run`` is
    False, ``added`` carries the new members and ``candidates`` is empty; on a
    dry run nothing is written, ``added`` is empty and ``candidates`` lists who
    would be added. ``candidate_count`` is populated in both cases so the
    confirm dialog and the result toast quote the same number.
    """

    dry_run: bool = False
    candidate_count: int = 0
    candidates: list[ManagedAICredentialApplyCandidate] = Field(
        default_factory=list
    )
    # How many candidates already hold a default this grant would take away,
    # counting each person once. Covers both axes a grant writes: the per-mode
    # SDK default (``set_user_sdk_defaults`` — pointer *and* model override,
    # which is reset even when the pointer was NULL) and the per-type default
    # credential (``set_as_default``, which demotes the key they chose). Zero
    # only when the record wires no defaults at all. Surfaced so the confirm
    # dialog can say what the action costs, not only what it gives — "12 users
    # will receive this credential" is only half the story when 9 of them lose
    # the default they chose, and a dialog that says nothing because it looked
    # at one flag is worse than one that says nothing at all.
    defaults_overwrite_count: int = 0
