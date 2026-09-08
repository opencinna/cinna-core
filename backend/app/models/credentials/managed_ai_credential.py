import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING

import sqlalchemy as sa
from pydantic import ConfigDict
from sqlmodel import Field, Relationship, SQLModel, Column, Text, Index
from sqlalchemy.dialects.postgresql import JSON as PG_JSON

from app.models.credentials.ai_credential import AICredentialType
from app.models.credentials.managed_ai_credential_membership import (
    MembershipProvisioningStatus,
)
from app.models.users.user import VALID_USER_ROLES, AIKeyOnboardingState


class ProvisioningMode(str, Enum):
    """How a parent record gets each member their key.

    - ``shared`` — one key, copied onto every member's child row. A manual
      record (no provider) is always ``shared``; so is a provider-owned record
      whose provider is ``fixed_key``.
    - ``minted`` — each member gets their **own** key, created at the provider
      through an :class:`app.models.credentials.provider_admin_credential.AIProvider`
      whose ``kind`` is ``minted``. The parent holds no key of its own, which is
      why ``encrypted_data`` is nullable.

    **No longer a stored column.** It is derived from the owning provider's
    ``kind`` and stays on :class:`ManagedAICredentialPublic` as a computed
    field, so a record and its provider cannot disagree about it.
    """

    SHARED = "shared"
    MINTED = "minted"

if TYPE_CHECKING:
    from app.models.users.user import User


def _default_sdk_modes() -> list[str]:
    """Default SDK modes wired when ``set_user_sdk_defaults`` is enabled."""
    return ["conversation", "building"]


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

    A **manual** record (``provider_id IS NULL``) holds its own Fernet-encrypted
    copy of the key (same shape/codec as ``ai_credential.encrypted_data``) so new
    children can be created and existing children re-keyed without the admin
    re-typing the secret, and every policy column below is the real value.

    A **provider-owned** record (``provider_id`` set) is the member list and
    nothing else. Its ``encrypted_data`` is NULL — the key lives on the provider,
    which is the only source of truth for it — and every policy column below is
    *shadowed*: still present, no longer read. The values are deliberately not
    mirrored down from the provider, because a second copy of a number the
    provider owns goes stale the moment the provider is edited, and a stale copy
    displayed as the active policy is worse than no copy.
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

    # The provider that owns this record: the source of its key and of every
    # wiring flag below. NULL means a **manual** record — an admin pasted a key
    # here and this row is the source of truth for it.
    #
    # **ON DELETE RESTRICT**, and deliberately unlike every other FK in this
    # domain. Elsewhere a lost pointer should degrade a capability rather than
    # delete data, so the FK is SET NULL with a service-level gate. Here SET NULL
    # would silently turn a provider-owned record into a *manual* one: fully
    # editable, its shadowed columns suddenly load-bearing while holding stale
    # values, and its ``encrypted_data`` NULL. A refused delete is the better
    # state, so the ordering is fixed the other way round: the credential is
    # deleted first — that path revokes its members' keys — and the provider
    # second. RESTRICT is the backstop under that ordering, so an ordering bug
    # surfaces as a database error rather than as an orphan. It is also what
    # turned the superseded ``ProviderAdminCredentialsService.delete`` from a
    # forced disconnect into a refusal; ``AIProvidersService.delete`` (Phase 2
    # of the ai-credential-providers plan) is where the ordered force lands.
    provider_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(
            sa.Uuid(),
            sa.ForeignKey("ai_provider.id", ondelete="RESTRICT"),
            nullable=True,
        ),
    )

    # ── Everything from here to ``managed_by_id`` is SHADOWED on a
    # provider-owned record: the real values live on the provider and are read
    # through the policy resolver, never off these columns. They remain the real
    # values for a manual record, which keeps every control it has today.

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

    # Model the owner's ``default_model_override_<mode>`` is set to when this
    # parent wires that mode's SDK defaults. Separate from ``default_model``
    # (which is the credential's own preferred model, written onto the child
    # row): these two write to the *user profile*, and only for the modes in
    # ``sdk_default_modes``. Shadowed on a provider-owned record, like every
    # column above it in this block: the provider's ``base_url``/``model``/
    # policy are the values that are read there, and these are the manual
    # record's.
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

    ``auto_provision_roles`` is **not** published here. §3.2 dropped the column
    from ``managed_ai_credential``; the rule is a property of a provider and is
    published on :class:`AIProviderPublic`. Restating it on this projection
    would put a second answer on the wire for a question that has one owner.
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
    model_override_conversation: str | None = None
    model_override_building: str | None = None
    expiry_notification_date: datetime | None = None
    managed_by_id: uuid.UUID | None = None
    #: Required for the same reason as ``provisioning_status`` above: an absent
    #: mode rendered through a client-side ``!== "minted"`` fallback is the
    #: browser deciding a record holds one shared key because a field did not
    #: arrive. **Computed**, not stored — see :class:`ProvisioningMode`.
    provisioning_mode: ProvisioningMode
    #: The AI provider that owns this record, or ``None`` for a manual one.
    #: Renamed from ``provider_admin_credential_id`` when the wire moved to the
    #: provider vocabulary of §2.1 — the entity is a **Provider**, and the
    #: administration secret is one of the two things a provider's secret can
    #: be, not the name of the relationship.
    provider_id: uuid.UUID | None = None
    #: The owning provider's name, resolved once by the policy resolver so the
    #: managed-credentials table can render a "Source" column without a second
    #: request per row. ``None`` for a manual record, and also for the
    #: unreachable "points at a provider row that is gone" policy.
    provider_name: str | None = None
    #: The **answer** to "is every wiring control read-only here", rather than
    #: ``provider_id !== null`` re-derived in the browser. The two agree today;
    #: only one of them keeps agreeing the day the rule changes.
    is_provider_owned: bool = False
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
    """Admin request to create a **manual** managed AI credential record.

    Creates the parent row + reconciles to create one ``AICredential`` child per
    valid target user.

    THIS ROUTE ONLY CREATES MANUAL RECORDS
    --------------------------------------
    A record that belongs to a provider is created *by creating the provider*
    (``POST /admin/ai-providers``), which writes the pair in one transaction.
    So three fields this model used to carry are gone rather than optional:

    * ``provider_admin_credential_id`` — pointing a new credential at a provider
      is §5.2's refused shape. It produced either a record holding its own key
      *and* a provider (the shape the Phase 1 migration aborts on) or a silent
      second credential on a provider that already owns one, invisible to
      rotation, apply-to-existing and the delete gate while still handing out
      keys. The refusal used to be a service 400; with the field gone it is
      structural, and ``extra="forbid"`` below is what keeps it from becoming a
      silent accept.
    * ``provisioning_mode`` — derived, never stated. A manual record is always
      ``shared``; ``minted`` is a property of the provider's ``kind``, and there
      is no provider to name here any more.
    * ``auto_provision_roles`` — the rule lives on ``ai_provider``. A managed
      credential is not a factory.

    ``extra="forbid"``, AND WHY IT IS NOT DECORATION
    -------------------------------------------------
    Removing a field from a Pydantic model does not refuse it; by default it
    *ignores* it. A client still sending ``auto_provision_roles`` would get a
    200 and nothing would happen at the next signup — the exact "saved
    successfully, changed nothing" failure the provider/credential split exists
    to delete, arriving through the door the split just closed. Phase 1 made a
    non-empty ``auto_provision_roles`` a 400 for that reason and left a narrow
    gap at ``[]`` on provider-owned records; forbidding unknown keys closes the
    gap and strengthens the refusal rather than trading it away, and it names
    the offending field in the 422. Pinned by
    ``tests/api/ai_credentials/admin_ai_providers_test.py::
    test_the_retired_managed_credential_fields_are_refused_not_ignored``.
    """

    model_config = ConfigDict(extra="forbid")  # type: ignore[assignment]

    name: str = Field(min_length=1, max_length=255)
    type: AICredentialType
    # Required — a manual record is always ``shared`` and a shared record must
    # hold a key. Nullable rather than required-with-a-sentinel so the omission
    # is representable in the request model itself; the service raises the 400,
    # because that rule is a policy and policies live in one place, not in a
    # validator on every write site.
    api_key: str | None = Field(default=None, min_length=1)
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

    ``auto_provision_roles`` is **gone**, not optional, and ``extra="forbid"``
    is what makes that a refusal instead of a silent accept — see
    :class:`ManagedAICredentialCreate` for the argument. The ``[]`` gap this
    closes was specific to this model: on a provider-owned record an empty list
    was accepted, wrote nothing anywhere, and left an administrator believing
    they had stopped that provider auto-provisioning.
    """

    model_config = ConfigDict(extra="forbid")  # type: ignore[assignment]

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


class ManagedDefaultSlotSkip(SQLModel):
    """A default slot an automatic grant declined to take, because it was held.

    **Not a** :class:`ManagedReconcileSkip`. That one means "this person did not
    receive the credential"; this one means the opposite — they received it, and
    the one thing that did *not* happen is the SDK default being repointed at
    it. Folding the two together would have every reader of ``skipped`` report a
    successful grant as a failure.

    Produced by ``_apply_sdk_defaults`` when the owner's
    ``default_ai_credential_<mode>_id`` already names a credential, and carried
    on :class:`~app.services.credentials.managed_ai_credentials_service.MemberAddition`
    — the add-only shape the automatic path returns. The two deliberate grant
    paths (``reconcile``'s explicit member list and ``apply_to_existing``) claim
    the slot instead and produce none of these.

    ``set-default-all`` is not in that list and does not belong in it: it never
    calls ``_apply_sdk_defaults`` at all. It sets each member's child as the
    per-type default and flips the flag, which is a different axis from the
    per-mode SDK slot this class is about.

    **It reaches a client through the invite response.**
    ``AccountProvisioningService`` takes the incumbent-wins default and carries
    the skips up as ``ProvisioningReport.default_slot_skips``, which
    ``InvitationService`` renders as
    :class:`~app.models.users.user_invitation.InviteProvisioningDefaultSlotSkip`
    on ``InviteProvisioningSummary``. Phase 3 declined to add the field on the
    grounds that it would be empty on virtually every response; Phase 4 added it
    once the ordinary producer was identified — a re-invite of an account that
    already exists runs explicit provisioning against somebody who may already
    hold a default, and one ticked provider is enough.

    Two things it is **not**, both load-bearing: it is not a failure (it never
    sets ``provisioning_failed``), and it is not carried on
    :class:`ManagedAICredentialReconcileResult`, whose every caller claims held
    slots and would therefore publish a field that is always empty.

    The automatic account-creation path produces these too, and
    ``on_account_created``'s ``ProvisioningReport`` has no HTTP surface — signup
    and the OAuth callback do not return one — so the invite response is the
    only place the value is currently rendered. That is a statement about
    today's callers rather than a rule; what is asserted is the positive half,
    end to end through the route, by
    ``tests/api/users/users_invitation_lifecycle_test.py::
    test_a_provider_whose_wiring_a_reinvited_account_already_holds_is_disclosed_not_failed``,
    which also pins both negatives — ``provisioning_failed`` false and
    ``skipped`` empty.
    """

    user_id: uuid.UUID
    #: ``conversation`` or ``building``.
    mode: str
    #: The credential already sitting in the slot. **Required, no default**: the
    #: skip exists *because* something is there, so a null here would describe a
    #: state that cannot produce this row.
    held_by_credential_id: uuid.UUID


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
    # No ``default_slot_skips`` here, deliberately. Every path into
    # ``reconcile`` is a superuser naming members by hand, and those claim the
    # slot; a field on this shape would be empty on every response it ever
    # appeared in, which is a worse lie than not offering it. The skips live on
    # ``MemberAddition``, which is what the automatic path returns.


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


# ============= The keys list =============
#
# ONE ROW = ONE REAL API KEY.
#
# The admin surface used to list *records*, which meant a minted provider showed
# one row holding one key per member — the unit stored and the unit administered
# had come apart for exactly the configuration the feature was built for. These
# two DTOs are the projection that puts them back together, and the rule they
# implement is arithmetic on the model rather than a presentation choice:
#
# * a **minted** parent has one real key per membership, each with its own
#   ``external_key_ref`` at the vendor and its own child credential → one row
#   per membership;
# * a **shared** parent (manual, or owned by a ``fixed_key`` provider) has
#   exactly one key: ``_resolve_key`` decrypts the parent's and ``_add_child``
#   copies *that same secret* onto every member's child row → one row for the
#   record, whatever its member count.
#
# So the N children under a shared record are copies, not keys, and the row
# count follows the number of secrets that exist at the provider.


class AdminAIKeyKind(str, Enum):
    """Which of the two things a row on the keys list is.

    Not derivable from ``provisioning_mode`` in the client even though the two
    agree today: the mode describes the *record*, this describes the **row**,
    and the day a third kind of row appears (a key held by nobody, an orphan
    found at the vendor) the mode still says ``minted`` for it.
    """

    #: One person's own key. ``membership_id`` is the resource every per-key
    #: verb addresses.
    PER_USER = "per_user"
    #: One shared key, held as a copy by ``member_count`` people.
    SHARED = "shared"


class AdminAIKeyRow(SQLModel):
    """One real API key — or one that is on its way.

    A row with no key yet (``pending`` / ``minting`` / ``failed`` /
    ``suspended``) is still a row. It is the only place an administrator can see
    that one person's key is stuck, which is half the reason this list exists;
    hiding it until a key materialises would make the surface silent exactly
    when something needs doing.

    **Two id fields rather than one polymorphic ``id``.** A single ``id`` whose
    meaning depends on ``kind`` is a field whose type every consumer has to
    re-derive, and the first one to get it wrong addresses a verb at the wrong
    table. ``managed_credential_id`` is always the record; ``membership_id`` is
    the per-key resource and is ``None`` for exactly ``kind="shared"``.
    """

    #: **Required, no default** — the discriminator every other optional field
    #: on this model is read against.
    kind: AdminAIKeyKind
    managed_credential_id: uuid.UUID
    #: The verb target: revoke, rotate, set-default and retry all address this.
    #: ``None`` for a shared row, whose verbs act on the record instead.
    membership_id: uuid.UUID | None = None
    #: What the administrator called the record this key came from. The row's
    #: own title when ``kind="shared"``; the second line when ``per_user``.
    credential_name: str
    type: AICredentialType
    provider_id: uuid.UUID | None = None
    #: ``None`` for a manual record **and** for a record whose provider row has
    #: gone — the surface renders those two differently and reads
    #: ``provider_id`` to tell them apart, exactly as the record list does.
    provider_name: str | None = None

    # ---- per_user only ----
    holder_user_id: uuid.UUID | None = None
    holder_email: str | None = None
    holder_full_name: str | None = None
    #: ``None`` for exactly ``kind="shared"``, and that is the one place this
    #: feature relaxes its "a status is always explicit" rule. The alternative
    #: is worse: a synthetic ``not_applicable`` on a shared row would be a
    #: provisioning status invented by the server for a row with no membership
    #: behind it, and a client filtering on it would silently include records.
    provisioning_status: MembershipProvisioningStatus | None = None
    provision_error: str | None = None
    provision_attempts: int = 0
    child_credential_id: uuid.UUID | None = None
    #: Whether this key is its holder's default for its type. Read off the
    #: child row, never inferred from the record's ``set_as_default`` — that
    #: flag is what a *grant* does, and the person may have changed it since.
    is_default: bool = False
    api_key_onboarding_state: AIKeyOnboardingState | None = None
    #: The vendor's own handles for this key, formatted for reading:
    #: ``"proj_… · user-…"``. Never the secret, and never the half of an
    #: ``api_key_id`` that could be replayed. It is here so an administrator can
    #: match a row against the provider's console — the same job the service
    #: account's ``email (membership id)`` name does from the other side.
    key_reference: str | None = None

    # ---- shared only ----
    #: How many people hold a copy of this one key. ``None`` for a per-user row,
    #: where the answer is always one and printing it would suggest otherwise.
    member_count: int | None = None

    created_at: datetime


class AdminAIKeysPublic(SQLModel):
    """One page of the keys list.

    ``count`` is the **total** number of matching rows, not the length of
    ``data`` — the house shape (:class:`~app.models.users.user.UsersPublic`),
    and the only one a pager can be built on.
    """

    data: list[AdminAIKeyRow] = Field(default_factory=list)
    count: int = 0
