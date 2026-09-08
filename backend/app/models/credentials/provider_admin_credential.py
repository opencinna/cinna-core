"""AI providers — where keys come from, and the rule for who gets one.

WHAT THIS IS
------------
One row per key source an administrator has connected. A provider holds a
secret, the vendor type it belongs to, the roles whose new accounts receive a
key from it, and the wiring policy applied when that happens. It owns exactly
one ``managed_ai_credential``, which owns the member list.

THE SECRET MEANS TWO DIFFERENT THINGS, AND ``kind`` SAYS WHICH
---------------------------------------------------------------
``encrypted_secret`` is Fernet-encrypted either way, but what it grants depends
entirely on :class:`AIProviderKind`:

- ``fixed_key`` — an ordinary **model API key**. The admin pastes one key and
  every member's child ``ai_credential`` row holds a copy of it. Its blast
  radius is model access billed to the account the key belongs to.
- ``minted`` — an **administration** secret (an OpenAI Admin API key). It does
  not call a model; it creates and destroys keys for a whole provider
  organisation, and it is the only means of revoking every key minted through
  it.

This column used to hold only the second kind, and this docstring used to say
so. It does not any more. A reader who trusts the old sentence concludes that a
row here can never be used against a model, which is false for every
``fixed_key`` row, and would treat the two as interchangeable in a projection.

WHY IT IS ITS OWN TABLE
-----------------------
Unchanged by the ``fixed_key`` addition, and the argument is about the *table*,
not about which of the two secrets a given row holds: ``ai_credential`` is
*plumbing*, and all of that plumbing would reach any secret the day it lived
there. Two lines make it concretely:

- ``model_discovery_service`` selects **every** row in ``ai_credential`` on a
  cron and calls the provider with each key.
- ``external_account_config_service`` hands **every** row a user owns,
  decrypted, to the desktop client.

Neither has a bug. They are correct for the table they read. The isolation is
structural rather than intentional: sharing
(``ai_credential_share.ai_credential_id``), environment linking
(``agent_environment.{conversation,building}_ai_credential_id``), bundle
publisher wiring and the blast-radius counts are all foreign keys pinned to
``ai_credential.id``, and the environment credential bag is a fixed slot dict
with no slot to pour into. A table that is not ``ai_credential`` is not
reachable through any of those.

The precedent to read is :class:`app.models.email.mail_server_config.MailServerConfig`:
server-scoped with no ``owner_id`` at all, an encrypted secret column, a public
projection that exposes ``has_secret: bool`` instead of the value, and a
superuser-or-nothing router. This copies that shape deliberately.

THE ONE GENERIC PATH A SEPARATE TABLE DOES NOT ESCAPE
-----------------------------------------------------
User deletion is a bare ``session.delete(user)`` and relies entirely on
database-level ``ON DELETE``. ``created_by_id`` is therefore **SET NULL**, never
CASCADE: deleting the superuser who happened to paste the key must not destroy
the instance's ability to mint and — worse — to *revoke* every key it has ever
minted.

WHAT A MINTED KEY ACTUALLY CARRIES
-----------------------------------
A minted key carries write access to the project's API resources. Its blast
radius is bounded by the project's monthly spend limit, which Cinna verifies is
in place before minting into a project, and by revocation. No narrower claim is
available to us: the provider documents default service-account permissions as
read and write of the project's API resources, and we do not request key scopes
in this phase.
"""
import uuid
from datetime import datetime, timezone
from enum import Enum

import sqlalchemy as sa
from pydantic import ConfigDict
from sqlmodel import Column, Field, Index, SQLModel, Text
from sqlalchemy.dialects.postgresql import JSON as PG_JSON

from app.models.credentials.ai_credential import AICredentialType


class AIProviderKind(str, Enum):
    """What a provider *is*, and therefore what its secret means.

    - ``fixed_key`` — the admin pastes one model API key and every member's
      child credential holds a copy of it. Available for every type.
    - ``minted`` — each member gets their own key, created at the provider
      through an administration secret. Available only for types whose adapter
      sets ``supports_minting``; the service, not the schema, enforces that.
    """

    FIXED_KEY = "fixed_key"
    MINTED = "minted"


def _default_sdk_modes() -> list[str]:
    """Default SDK modes wired when ``set_user_sdk_defaults`` is enabled."""
    return ["conversation", "building"]


def _no_auto_provision_roles() -> list[str]:
    """Default for ``auto_provision_roles``: nobody, until an admin says so.

    Empty is the only safe default. A provider that auto-granted itself to
    every new account the moment it was created would hand a company API key to
    the next person who signed up, with no admin action anywhere in the story.
    """
    return []


class ProviderAdminCredentialConfig(SQLModel):
    """Non-secret configuration for one provider organisation.

    **The spend limit is not configured here, and deliberately cannot be.** It
    is set on the provider's own console and is read back — never written — by
    :meth:`verify_spend_limit`; the project is still verified to carry a hard
    limit before the first key is minted. Cinna held an editable
    ``spend_limit_cents`` and an "Apply spend limit" action until it was removed:
    a locally stored threshold is a second copy of a number the provider owns,
    it silently goes stale the moment anybody edits the real one in the console
    or in any other tool pointed at the same organisation, and a stale copy
    displayed as the project's cap is worse than no copy at all. There is no
    fallback value and no local default.
    """

    organization_id: str | None = Field(default=None, max_length=255)
    project_id: str | None = Field(default=None, max_length=255)


class AIProvider(SQLModel, table=True):
    """A source of AI keys, plus the rule for who automatically gets one.

    Server-scoped, not user-scoped: there is no ``owner_id``. Only superusers
    may see or edit these, and the secret is never returned by any endpoint.

    A provider owns exactly one ``managed_ai_credential`` (that record's
    ``provider_id``), which owns the member list. That 1:1 shape is not a schema
    constraint — the FK allows many — so it is established by migration
    ``c23d6b59a8f5``, which asserts it as a post-condition and has
    ``tests/migrations/ai_provider_split_test.py`` asserting it back.

    **Holding it afterwards is the service's.** ``AIProvidersService.create``
    writes the pair in one transaction (one ``flush`` to order the inserts, one
    ``commit``), and it is the only writer of the pair. A *second* credential on
    one provider is no longer refusable-in-service because it is no longer
    expressible: ``ManagedAICredentialCreate`` carries no ``provider_id`` field
    and forbids unknown keys, so no request can point a new credential at an
    existing provider.

    A **lone** provider row — one with no credential at all — was creatable by
    ``POST /admin/provider-admin-credentials``, which Phase 4 deleted. The other
    route to it is closed too: ``ManagedAICredentialsService.delete`` refuses a
    provider-owned credential, because deleting it would leave this row
    auto-provisioning with nothing to grant through and the scan drops such a
    provider silently. ``AIProviderPublic.owned_credential_id`` stays nullable
    for rows that predate the deletion, because a projection that could not
    describe one would take the whole admin listing down with it.

    The policy columns below are the real values for a provider-owned
    credential; the same-named columns on ``managed_ai_credential`` are
    shadowed there and stay the real values only for manual records with no
    provider.
    """

    __tablename__ = "ai_provider"
    __table_args__ = (
        Index("ix_ai_provider_type", "provider_type"),
        # On ``kind``, not on the JSON roles column. The auto-provision scan
        # reads every provider row and filters roles in Python — there are tens
        # of these, not thousands — so what an index can usefully narrow here is
        # the kind, not the membership test.
        Index("ix_ai_provider_auto_provision", "kind"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    name: str = Field(min_length=1, max_length=255, nullable=False)

    #: :class:`AIProviderKind`. **No Python-side default**: a provider whose
    #: kind defaulted would let a caller create one without saying what its
    #: secret is, and the two secrets are not interchangeable.
    #:
    #: The database column *does* carry ``DEFAULT 'minted'``, left in place by
    #: migration ``c23d6b59a8f5`` because every row that predates it is an
    #: organisation administration secret. That default is reachable only by a
    #: raw INSERT that omits the column — SQLModel always writes it — so it
    #: backfills history without softening the rule above.
    kind: str = Field(sa_column=Column(sa.String(16), nullable=False))

    #: The vendor. Named ``provider_type`` in the database since this table was
    #: ``provider_admin_credential``; the DTO exposes it as ``type``.
    provider_type: AICredentialType = Field(..., sa_type=sa.String(50))

    #: Fernet-encrypted secret. **Its meaning depends on ``kind``** — a model
    #: API key for ``fixed_key``, an organisation administration secret for
    #: ``minted``. See the module docstring. Never projected, never linked to an
    #: environment, never in ``/external/account-config``.
    encrypted_secret: str = Field(sa_column=Column(Text, nullable=False))

    #: :class:`ProviderAdminCredentialConfig` as stored JSON. Populated for
    #: ``minted`` providers; empty for ``fixed_key`` ones, which have no
    #: organisation or project to name.
    config: dict = Field(
        default_factory=dict,
        sa_column=Column(
            PG_JSON, nullable=False, server_default=sa.text("'{}'::json")
        ),
    )

    #: Non-secret mirrors of the key's shape, for ``fixed_key`` providers whose
    #: adapter requires them (``openai_compatible``/``google``). NULL on a
    #: ``minted`` provider, whose members' keys carry their own.
    base_url: str | None = Field(default=None, max_length=500)
    model: str | None = Field(default=None, max_length=255)

    # ── The rule ─────────────────────────────────────────────────────
    #: Roles whose *newly created* accounts receive a key from this provider,
    #: granted by ``AccountProvisioningService.on_account_created`` at the
    #: creation chokepoint. Empty (the default) = never granted automatically.
    #: Deliberately creation-time only: a role change on an existing account
    #: does not re-run provisioning, because a promotion should not silently
    #: hand out a company key. "Apply to existing users" is the explicit path.
    auto_provision_roles: list[str] = Field(
        default_factory=_no_auto_provision_roles,
        sa_column=Column(
            PG_JSON, nullable=False, server_default=sa.text("'[]'::json")
        ),
    )

    # ── The wiring policy ────────────────────────────────────────────
    #: Set each member's child credential as its owner's default for the type.
    set_as_default: bool = Field(
        default=False,
        sa_column=Column(
            sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    #: Wire each owner's ``default_sdk_*`` to their child credential.
    set_user_sdk_defaults: bool = Field(
        default=False,
        sa_column=Column(
            sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    #: Modes wired when ``set_user_sdk_defaults`` is true.
    sdk_default_modes: list[str] = Field(
        default_factory=_default_sdk_modes,
        sa_column=Column(
            PG_JSON,
            nullable=False,
            server_default=sa.text("""'["conversation", "building"]'::json"""),
        ),
    )
    #: Admin's preferred default model (bare concrete id), written onto each
    #: child row, and the curated list of selectable ids (NULL/empty = offer the
    #: per-credential auto-discovered list). Both non-secret.
    default_model: str | None = Field(default=None, max_length=255)
    available_models: list[str] | None = Field(
        default=None, sa_column=Column(PG_JSON, nullable=True)
    )
    #: Model each owner's ``default_model_override_<mode>`` is set to when this
    #: provider wires that mode's SDK defaults. Separate from ``default_model``:
    #: these two write to the *user profile*, and only for the modes in
    #: ``sdk_default_modes``. A stored NULL cannot say whether it was never set
    #: or has just been retracted, so the *request* that retracts one carries
    #: that fact down instead — the three-state ``""``-clears contract of
    #: ``ManagedAICredentialUpdate`` applies here unchanged.
    model_override_conversation: str | None = Field(default=None, max_length=255)
    model_override_building: str | None = Field(default=None, max_length=255)
    #: When the key behind this provider expires. It describes the key, and the
    #: provider owns the key.
    expiry_notification_date: datetime | None = Field(
        default=None, sa_type=sa.DateTime(timezone=True)
    )

    #: Last successful verification against the provider, and the coarse reason
    #: code for the last failed one. Both are non-secret and both are shown to
    #: the admin, because "the key works" and "the project is capped" are the two
    #: questions a Verify press exists to answer.
    last_verified_at: datetime | None = Field(
        default=None, sa_type=sa.DateTime(timezone=True)
    )
    last_verify_error: str | None = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )

    #: Audit only — which superuser connected this. SET NULL on deletion (see the
    #: module docstring): the record stays usable by any superuser, and deleting
    #: an admin account must never destroy the org secret.
    created_by_id: uuid.UUID | None = Field(
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


# ============= Projection DTOs =============


# ============= Retired DTOs =============
#
# ``ProviderAdminCredentialPublic`` / ``Create`` / ``Update`` / ``VerifyResult``
# were the wire shapes of ``/admin/provider-admin-credentials``, deleted in
# Phase 4 of the ai-credential-providers plan along with that route. Their
# replacements are the ``AIProvider*`` DTOs below, which describe the same table
# under the vocabulary of §2.1 and cover both kinds of secret it now holds.
# ``ProviderAdminCredentialConfig`` is **not** retired: it is the stored config
# shape, aliased as ``AIProviderConfig``.


# ============= Provider DTOs (ai-credential-providers, phase 2) =============
#
# The wire vocabulary of §2.1: the entity is a **Provider**, and ``type`` is the
# vendor. The stored column is still ``provider_type`` — renaming it is a
# migration these DTOs do not need — so the projection does the translation once
# here rather than in every caller.


class AIProviderConfigInput(ProviderAdminCredentialConfig):
    """The request-side config shape: same fields, unknown keys refused.

    A subclass rather than ``extra="forbid"`` on the parent, because the parent
    is also the **response** shape (:attr:`AIProviderPublic.config`, built as
    ``ProviderAdminCredentialConfig(**(record.config or {}))``). Forbidding
    extras there would make a stored config carrying a stray key raise on *read*,
    taking the admin listing down over a row somebody has to look at to fix.
    Strict going in, tolerant coming out.

    The reason it is strict going in is specific rather than tidiness.
    ``organization_id`` is spelled the American way here and this codebase's own
    prose spells it the British way everywhere else — "provider organisation",
    "organisation administration secret" — so ``organisation_id`` is the likeliest
    typo on the surface. Ignored, it lands on one of the two fields that select
    *which OpenAI project keys are minted into*: a provider created with a
    dropped ``project_id`` verifies as uncapped and refuses to mint, with nothing
    anywhere saying the field was thrown away. Pinned by
    ``tests/api/ai_credentials/admin_ai_providers_test.py::
    test_a_misspelled_config_field_is_refused_not_dropped``.
    """

    model_config = ConfigDict(extra="forbid")  # type: ignore[assignment]


#: ``AIProviderConfig`` is the name §3.1 of the plan gives this shape. It is the
#: same class the table has always used; the alias exists so new code can spell
#: it the way the entity is now called without a second declaration to keep in
#: step.
AIProviderConfig = ProviderAdminCredentialConfig


class AIProviderPublic(SQLModel):
    """Admin-facing projection of one provider. **Never** carries the secret.

    ``has_secret`` rather than a nullable secret field, copying
    ``MailServerConfigPublic``: a projection that carries the value's *slot* is
    one refactor away from carrying the value. There is no reveal endpoint.
    """

    id: uuid.UUID
    name: str
    #: :class:`AIProviderKind`.
    kind: str
    #: The vendor. Named ``type`` on the wire, ``provider_type`` in the column.
    type: AICredentialType
    config: ProviderAdminCredentialConfig
    has_secret: bool = True
    base_url: str | None = None
    model: str | None = None

    auto_provision_roles: list[str] = Field(
        default_factory=_no_auto_provision_roles
    )
    set_as_default: bool = False
    set_user_sdk_defaults: bool = False
    sdk_default_modes: list[str] = Field(default_factory=_default_sdk_modes)
    default_model: str | None = None
    available_models: list[str] | None = None
    model_override_conversation: str | None = None
    model_override_building: str | None = None
    expiry_notification_date: datetime | None = None

    last_verified_at: datetime | None = None
    last_verify_error: str | None = None
    created_by_id: uuid.UUID | None = None

    #: The one managed credential this provider owns.
    #:
    #: ``None`` only for a **lone** provider row — one written by the
    #: ``POST /admin/provider-admin-credentials`` route Phase 4 deleted, which
    #: created a provider and no credential. ``AIProvidersService.create`` always
    #: writes the pair and migration ``c23d6b59a8f5`` gave every pre-existing row
    #: one, so no live path produces this state any more. It is nullable rather
    #: than absent because a projection that could not describe such a row would
    #: take the whole admin listing down with it, and rows created before the
    #: deletion still exist.
    owned_credential_id: uuid.UUID | None = None
    member_count: int = 0
    #: ``membership status -> count`` for the owned credential's members. The
    #: admin list's "is anything stuck" answer, computed server-side so two
    #: surfaces cannot disagree about which statuses mean "has a key".
    key_state_summary: dict[str, int] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class AIProviderCreate(SQLModel):
    """Connect a key source and say who automatically gets one.

    ``kind`` has no default here for the same reason the column has none in
    Python: the secret means two categorically different things depending on it,
    and a caller that did not say which one it is pasting has not been asked the
    question.

    ``extra="forbid"`` because there is exactly **one** secret field and for a
    ``minted`` provider it is the administration key. A body that also carried
    ``api_key`` — the spelling every other credential surface uses, and the
    obvious thing to reach for — would otherwise be accepted, ignored, and leave
    an admin believing they had given the provider a member key. Refusing it
    names the field instead. Pinned by
    ``tests/api/ai_credentials/admin_ai_providers_test.py::
    test_a_stray_member_key_on_a_provider_create_is_refused_not_ignored``.
    """

    model_config = ConfigDict(extra="forbid")  # type: ignore[assignment]

    name: str = Field(min_length=1, max_length=255)
    kind: AIProviderKind
    type: AICredentialType
    secret: str = Field(min_length=1)
    config: AIProviderConfigInput = Field(
        default_factory=AIProviderConfigInput
    )
    base_url: str | None = Field(default=None, max_length=500)
    model: str | None = Field(default=None, max_length=255)

    auto_provision_roles: list[str] = Field(
        default_factory=_no_auto_provision_roles
    )
    set_as_default: bool = False
    set_user_sdk_defaults: bool = False
    sdk_default_modes: list[str] = Field(default_factory=_default_sdk_modes)
    default_model: str | None = Field(default=None, max_length=255)
    available_models: list[str] | None = None
    model_override_conversation: str | None = Field(default=None, max_length=255)
    model_override_building: str | None = Field(default=None, max_length=255)
    expiry_notification_date: datetime | None = None
    #: Members to grant immediately. May be empty: an auto-provision-only
    #: provider legitimately starts with nobody and fills up as accounts are
    #: created.
    target_user_ids: list[uuid.UUID] = Field(default_factory=list)


class AIProviderUpdate(SQLModel):
    """Partial update. Every field's omission means "leave it alone".

    ``model_override_*`` keeps the three-state contract of
    ``ManagedAICredentialUpdate`` **verbatim**, because the same members are on
    the other end of it:

      * omitted / ``null`` — leave the stored override alone;
      * ``""`` — clear it, and unpin every member still carrying the dropped
        value (without that the clear is cosmetic);
      * a model id — set it and write it through.

    ``kind``, ``type`` and ``target_user_ids`` are absent on purpose. The first
    two would re-point an existing member's key at a different vendor or change
    what the stored secret means; membership is edited on the managed credential
    the provider owns, which is the record that has always held it.

    ``extra="forbid"`` for the same reason as :class:`AIProviderCreate`, and one
    more: ``secret`` is absent here on purpose — replacing a ``fixed_key``
    provider's key is ``POST /{id}/rotate-key``, which re-keys every member, and
    a ``secret`` quietly ignored by this endpoint would look exactly like the
    rotation that endpoint exists to perform.
    """

    model_config = ConfigDict(extra="forbid")  # type: ignore[assignment]

    name: str | None = Field(default=None, min_length=1, max_length=255)
    config: AIProviderConfigInput | None = None
    base_url: str | None = Field(default=None, max_length=500)
    model: str | None = Field(default=None, max_length=255)

    #: ``None`` = leave unchanged; ``[]`` = stop auto-provisioning (existing
    #: members keep their credential — this is not a revoke).
    auto_provision_roles: list[str] | None = None
    set_as_default: bool | None = None
    set_user_sdk_defaults: bool | None = None
    sdk_default_modes: list[str] | None = None
    default_model: str | None = Field(default=None, max_length=255)
    available_models: list[str] | None = None
    model_override_conversation: str | None = Field(default=None, max_length=255)
    model_override_building: str | None = Field(default=None, max_length=255)
    expiry_notification_date: datetime | None = None


class AIProviderRotateKey(SQLModel):
    """Replace a ``fixed_key`` provider's key. Refused on ``minted``."""

    api_key: str = Field(min_length=1)


class AIProviderVerifyResult(SQLModel):
    """Outcome of a Verify press, for either kind.

    ``spend_limit_*`` are answered only for a ``minted`` provider — a
    ``fixed_key`` provider has no project whose cap could be read, and reporting
    ``False`` there would read as "no cap configured" rather than "the question
    does not apply". ``checked_spend_limit`` says which of the two it is.
    """

    ok: bool
    #: Provider-side identity the secret resolved to (an organisation or project
    #: id) for a ``minted`` provider. Never key material.
    account_ref: str | None = None
    checked_spend_limit: bool = False
    spend_limit_enforcing: bool = False
    spend_limit_cents: int | None = None
    #: Coarse reason code when ``ok`` is False.
    error: str | None = None


class AIProviderDeleteMember(SQLModel):
    """One person who loses a key if a provider is force-deleted."""

    user_id: uuid.UUID
    email: str
    full_name: str | None = None
    #: True when a key minted at the provider exists for this member and will be
    #: revoked there. False for a ``fixed_key`` member, whose child credential
    #: is simply deleted.
    holds_provider_key: bool = False


class AIProviderDeleteImpact(SQLModel):
    """What deleting this provider costs — the 409 body of an unforced delete.

    Named people rather than a bare count, because "3 users will lose a key" is
    not something an administrator can check before pressing.
    """

    provider_id: uuid.UUID
    provider_name: str
    #: ``None`` for a lone provider row — see :attr:`AIProviderPublic.owned_credential_id`.
    #: Such a row has no members and deletes without a confirmation.
    owned_credential_id: uuid.UUID | None = None
    member_count: int = 0
    #: How many of those members hold a key that will be revoked *at the
    #: provider*. Zero for a ``fixed_key`` provider.
    minted_key_count: int = 0
    members: list[AIProviderDeleteMember] = Field(default_factory=list)
