"""Superuser CRUD over AI providers — key sources, and the rule for who gets one.

A **provider** is one place keys come from plus the policy applied to whoever
receives one. It owns exactly one ``managed_ai_credential``, which owns the
member list; this service writes the pair and keeps them in step, and
``ManagedAICredentialsService`` keeps its unchanged role as the owner of the
credential and its members.

THE SECRET MEANS TWO THINGS, AND THIS SERVICE SPEAKS FOR BOTH
--------------------------------------------------------------
``AIProvider.encrypted_secret`` holds a **model API key** for a ``fixed_key``
provider and an **organisation administration secret** for a ``minted`` one.
Everything that touches it here branches on ``kind`` first:

- a ``fixed_key`` secret is stored in the *same envelope* a credential uses
  (``{api_key, base_url, model}``, Fernet-encrypted), because a member's child
  credential is written from it verbatim;
- a ``minted`` secret is stored bare, is never handed to a model call, and is
  the only means of revoking the keys minted through it.

That is why :meth:`rotate_key` refuses a ``minted`` provider and
:meth:`fixed_key_envelope` answers ``None`` for one. Both refusals are
behaviour, not defensiveness: silently accepting a rotation that rotates nothing
is how an admin comes to believe they rolled a key they did not, and handing an
administration secret back as a model key would put it on a member's credential
row and from there into the desktop client.

THE ISOLATION THIS MODULE IS ONE HALF OF
-----------------------------------------
``ai_provider`` is deliberately not reachable from the plumbing that reads
``ai_credential`` — model discovery, ``/external/account-config``, sharing,
environment linking, bundle wiring. This service and
``key_provisioning_service`` are the two modules that are *allowed* to query the
table; callers that need a fact off a provider ask
:func:`app.services.credentials.provisioning_policy.resolve_policy` (for policy)
or this service (for anything else).

**Nothing else queries it.** ``tests/architecture/ai_provider_isolation_test.py``
asserts that with no exception list at all: its ``PENDING`` table and the
``xfail(strict=True)`` twin that held the exception-free end state are both gone,
deleted with the last entry they excused. Three modules came off that table in
turn — ``account_provisioning_service`` in Phase 3, which asks
:meth:`auto_provision_targets` now, and ``provider_admin_credentials_service``
and ``admin_provider_credentials`` in Phase 4, deleted with the superseded
``/admin/provider-admin-credentials`` route. A fourth module that learns to query
the table fails that test on the commit that teaches it.
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from fastapi import HTTPException
from sqlmodel import Session, col, func, select

from app.core.security import decrypt_field, encrypt_field
from app.models.credentials.ai_credential import (
    AICredentialData,
    AICredentialType,
)
from app.models.credentials.managed_ai_credential import (
    ManagedAICredential,
    ManagedAICredentialApplyResult,
    ManagedAICredentialReconcileResult,
    VALID_AUTO_PROVISION_ROLES,
)
from app.models.credentials.managed_ai_credential_membership import (
    ManagedAICredentialMembership,
    holds_provider_key,
)
from app.models.credentials.provider_admin_credential import (
    AIProvider,
    AIProviderCreate,
    AIProviderDeleteImpact,
    AIProviderDeleteMember,
    AIProviderKind,
    AIProviderPublic,
    AIProviderUpdate,
    AIProviderVerifyResult,
    ProviderAdminCredentialConfig,
)
from app.models.users.user import User
from app.services.ai_providers import registry
from app.services.ai_providers.base import KeyProvisioner, ProviderAdminError
from app.services.credentials.ai_credentials_service import (
    ai_credentials_service,
)
from app.services.credentials.key_provisioning_types import RevocationRequest
from app.services.credentials.managed_ai_credentials_service import (
    ManagedCredentialConflictError,
    managed_ai_credentials_service,
)
from app.services.credentials.provisioning_policy import (
    POLICY_MODES,
    policy_from_provider,
)

logger = logging.getLogger(__name__)


class AIProviderConflictError(ManagedCredentialConflictError):
    """Two providers would both own the same ``(role, mode)`` default slot.

    A subclass rather than a new type: the payload a route needs — the other
    side's name and id, the role and the mode — is identical, and the surface
    that renders it has not changed. What changed is *whose* configuration
    collides: the rule is provider-vs-provider now, because a managed credential
    no longer carries auto-provision roles of its own.
    """


class AIProviderInUseError(Exception):
    """Deleting this provider would take a key away from people who hold one.

    Carries the full impact rather than a count, because "3 users lose a key" is
    not something an administrator can check before pressing.
    """

    def __init__(self, impact: AIProviderDeleteImpact) -> None:
        self.impact = impact
        super().__init__(
            f"{impact.member_count} member(s) hold a credential from "
            f"'{impact.provider_name}'."
        )


@dataclass(frozen=True)
class ProviderGrantTarget:
    """A provider to grant, paired with the credential it grants through.

    The rule lives on the provider and the members live on the credential, so a
    grant needs both and a caller that is not allowed to query ``ai_provider``
    cannot pair them itself. This is what
    :meth:`AIProvidersService.auto_provision_targets` and
    :meth:`AIProvidersService.grant_targets` hand to
    ``AccountProvisioningService``.

    Only a provider that owns a credential produces one. A provider with none
    has nothing to add a member to, so it is absent from the result rather than
    present with a ``None`` — the caller reports the id it asked for and did not
    get back, and there is no third state to forget to handle.
    """

    provider_id: uuid.UUID
    credential: ManagedAICredential


class AIProvidersService:
    """Superuser-only CRUD, verification and lifecycle for AI providers."""

    # ------------------------------------------------------------------ #
    # Row access — the only queries against ``ai_provider`` in this module
    # ------------------------------------------------------------------ #

    def load(
        self, session: Session, provider_id: uuid.UUID | None
    ) -> AIProvider | None:
        """One provider by id, or ``None``.

        Public because it is the door other modules come through: the resolver
        and ``ManagedAICredentialsService`` need provider rows and are not
        allowed to query for them.
        """
        if provider_id is None:
            return None
        return session.get(AIProvider, provider_id)

    def load_many(
        self, session: Session, provider_ids: Iterable[uuid.UUID]
    ) -> dict[uuid.UUID, AIProvider]:
        """Providers by id, in one query. The batch resolver's fetch."""
        wanted = list(dict.fromkeys(provider_ids))
        if not wanted:
            return {}
        return {
            row.id: row
            for row in session.exec(
                select(AIProvider).where(col(AIProvider.id).in_(wanted))
            ).all()
        }

    def _get_or_404(
        self, session: Session, provider_id: uuid.UUID
    ) -> AIProvider:
        record = self.load(session, provider_id)
        if record is None:
            raise HTTPException(status_code=404, detail="AI provider not found")
        return record

    def owned_credential(
        self, session: Session, provider: AIProvider
    ) -> ManagedAICredential | None:
        """The one managed credential this provider owns.

        ``.first()`` rather than ``.one()``: the 1:1 shape is established by
        :meth:`create` and by migration ``c23d6b59a8f5``, not by a schema
        constraint, and a provider row left behind by the deleted
        ``/admin/provider-admin-credentials`` route can still have none.
        """
        return session.exec(
            select(ManagedAICredential)
            .where(ManagedAICredential.provider_id == provider.id)
            .order_by(col(ManagedAICredential.created_at).asc())
        ).first()

    def _owned_credential_or_404(
        self, session: Session, provider: AIProvider
    ) -> ManagedAICredential:
        parent = self.owned_credential(session, provider)
        if parent is None:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"AI provider '{provider.name}' has no managed credential "
                    "to act on."
                ),
            )
        return parent

    def _owned_credentials(
        self, session: Session, provider_ids: Iterable[uuid.UUID]
    ) -> dict[uuid.UUID, ManagedAICredential]:
        """The owned credential of each provider, in one query.

        The batch form of :meth:`owned_credential`, ordering on the same column
        in the same direction and taking the first row, so that a provider which
        somehow owns two credentials is resolved to the same one either way.
        Both sides order on ``created_at`` **alone**, so two rows written in the
        same transaction can tie and neither is then deterministic; nothing in
        this codebase creates that shape — :meth:`create` writes one credential
        per provider, and no request can point a *new* credential at an existing
        one because ``ManagedAICredentialCreate`` has no ``provider_id`` field
        and forbids unknown keys. That is why it is stated rather than defended
        against.
        """
        wanted = list(dict.fromkeys(provider_ids))
        if not wanted:
            return {}
        rows = session.exec(
            select(ManagedAICredential)
            .where(col(ManagedAICredential.provider_id).in_(wanted))
            .order_by(col(ManagedAICredential.created_at).asc())
        ).all()
        out: dict[uuid.UUID, ManagedAICredential] = {}
        for row in rows:
            # Type narrowing, not a filter: the ``WHERE`` above already
            # excludes NULL, and ``provider_id`` is declared optional.
            if row.provider_id is not None:
                out.setdefault(row.provider_id, row)
        return out

    def _grant_targets(
        self, session: Session, providers: list[AIProvider]
    ) -> list[ProviderGrantTarget]:
        credentials = self._owned_credentials(
            session, [provider.id for provider in providers]
        )
        return [
            ProviderGrantTarget(
                provider_id=provider.id, credential=credentials[provider.id]
            )
            for provider in providers
            if provider.id in credentials
        ]

    # ------------------------------------------------------------------ #
    # Grant targets — the account-creation path's read of this table
    # ------------------------------------------------------------------ #

    def auto_provision_targets(
        self, session: Session, role: str
    ) -> list[ProviderGrantTarget]:
        """Every provider whose ``auto_provision_roles`` covers ``role``.

        The scan behind ``AccountProvisioningService.on_account_created``. It
        lives here because that module is not allowed to query ``ai_provider``
        (``tests/architecture/ai_provider_isolation_test.py``), and it reads
        every row and filters in Python as §3.1 of the ai-credential-providers
        plan specifies: the table holds a handful of rows per instance, and a
        portable JSON containment predicate over a ``json`` (not ``jsonb``)
        column is more machinery than the saving is worth.

        The ``isinstance`` guard has no test behind it and is not claimed to:
        ``in`` against a non-list raises, ``auto_provision_roles`` is a JSON
        column, and nothing this service exposes can write a non-list into it.
        It is there because the statement it guards runs inline on the signup
        request, where the cost of being wrong is a failed signup rather than a
        failed admin write.

        The ``order_by`` makes the scan read in insertion order instead of
        whatever order the table returns. Nothing asserts that order and no
        behaviour depends on it — grants are independent — so this is
        determinism for a reader of the logs, not a contract.
        """
        providers = [
            provider
            for provider in session.exec(
                select(AIProvider).order_by(col(AIProvider.created_at).asc())
            ).all()
            if isinstance(provider.auto_provision_roles, list)
            and role in provider.auto_provision_roles
        ]
        return self._grant_targets(session, providers)

    def grant_targets(
        self, session: Session, provider_ids: Iterable[uuid.UUID]
    ) -> list[ProviderGrantTarget]:
        """The named providers, de-duplicated, in the order they were asked for.

        The invitation wizard's explicit list. An id that names no provider — or
        one whose provider owns no credential to grant through — is simply not
        in the result; the caller diffs what it asked for against what it got
        and reports the difference, rather than being handed a placeholder it
        has to test for. Both shapes come back to an administrator as a
        ``provider_not_found`` skip, and both are covered in
        ``tests/api/users/users_invitation_lifecycle_test.py``.
        """
        wanted = list(dict.fromkeys(provider_ids))
        if not wanted:
            return []
        by_id = self.load_many(session, wanted)
        return self._grant_targets(
            session,
            [
                by_id[provider_id]
                for provider_id in wanted
                if provider_id in by_id
            ],
        )

    # ------------------------------------------------------------------ #
    # Secret access
    # ------------------------------------------------------------------ #

    def decrypt_secret(self, record: AIProvider) -> str:
        """The provider's secret, in plaintext.

        The only door out of this column. Not a projection field and not
        reachable from a route: every caller is a provider call here or in
        ``KeyProvisioningService``.
        """
        return decrypt_field(record.encrypted_secret)

    def fixed_key_envelope(
        self, session: Session, provider_id: uuid.UUID
    ) -> str | None:
        """The stored key envelope of a ``fixed_key`` provider, still encrypted.

        Handed to ``ManagedAICredentialsService._decrypt_parent``, which owns
        every decrypt of a member-facing key. **``None`` for a ``minted``
        provider**, whose secret is an administration key: returning it would
        put it on a member's child credential and from there into every model
        call and the desktop client. Covered by
        ``tests/unit/test_managed_credential_key_source.py``.
        """
        record = self.load(session, provider_id)
        if record is None or record.kind != AIProviderKind.FIXED_KEY.value:
            return None
        return record.encrypted_secret

    # ------------------------------------------------------------------ #
    # Validation
    # ------------------------------------------------------------------ #

    def _adapter_or_400(self, provider_type: AICredentialType):
        adapter = registry.find_adapter(provider_type)
        if adapter is None:
            raise HTTPException(
                status_code=400,
                detail=f"No adapter serves provider type '{provider_type}'.",
            )
        return adapter

    def _provisioner(self, record: AIProvider) -> KeyProvisioner:
        adapter = self._adapter_or_400(record.provider_type)
        provisioner = adapter.key_provisioner
        if provisioner is None:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"{adapter.label} cannot create API keys through its "
                    "administration API."
                ),
            )
        return provisioner

    def _validate_shape(
        self,
        *,
        kind: AIProviderKind,
        provider_type: AICredentialType,
        secret: str,
        config: ProviderAdminCredentialConfig,
        base_url: str | None,
        model: str | None,
    ) -> None:
        """Refuse a provider that cannot do what its ``kind`` promises.

        Four rules, each with its own message, because "invalid configuration"
        sends an admin hunting:

        1. ``minted`` for a type whose adapter cannot create keys. Anthropic's
           administration API lists and updates keys but cannot create them, so
           for it — and for every other non-OpenAI type — ``fixed_key`` is the
           normal path, not a degraded one.
        2. ``minted`` with no ``project_id``: there is no project to mint into
           and therefore no spend limit to verify.
        3. ``fixed_key`` missing a field the adapter requires. Delegated to
           ``ai_credentials_service._validate_credential_data`` so the provider
           and the per-user pipeline share one rule rather than two that drift.
        4. A type with no adapter at all.
        """
        adapter = self._adapter_or_400(provider_type)

        if kind == AIProviderKind.MINTED:
            if not adapter.supports_minting:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"{adapter.label} cannot create API keys through its "
                        "administration API, so per-user keys are not "
                        "available for it. Use a fixed key instead."
                    ),
                )
            if not (config.project_id or "").strip():
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "A per-user-keys provider needs the project id it mints "
                        "into; the project's spend limit is what bounds every "
                        "key it creates."
                    ),
                )
            return

        ai_credentials_service._validate_credential_data(
            provider_type, secret, base_url, model
        )

    @staticmethod
    def _normalize_roles(value: list[str] | None) -> list[str] | None:
        """Validate + canonicalise ``auto_provision_roles``.

        ``None`` passes through as "no change". Anything else is trimmed,
        de-duplicated order-preservingly, and checked against the role enum — an
        unknown role is a 400, not a silent drop, because an admin who mistypes
        a role would otherwise save successfully and watch nothing happen at the
        next signup with no clue why.
        """
        if value is None:
            return None
        seen: set[str] = set()
        out: list[str] = []
        for raw in value:
            entry = (raw or "").strip()
            if not entry or entry in seen:
                continue
            if entry not in VALID_AUTO_PROVISION_ROLES:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Unknown role '{entry}' in auto_provision_roles. "
                        f"Valid roles: {', '.join(VALID_AUTO_PROVISION_ROLES)}."
                    ),
                )
            seen.add(entry)
            out.append(entry)
        return out

    @staticmethod
    def _claimed_slots(
        roles: list[str] | None,
        modes: list[str] | None,
        set_user_sdk_defaults: bool,
    ) -> set[tuple[str, str]]:
        """The ``(role, mode)`` slots a *prospective* configuration claims.

        The same definition :meth:`ProvisioningPolicy.claimed_slots` applies to a
        stored one — this overload exists because a create has no stored row to
        resolve a policy from yet. Both filter modes to :data:`POLICY_MODES`, so
        a typo in ``sdk_default_modes`` wires nothing and collides with nothing.
        """
        if not set_user_sdk_defaults or not roles or not modes:
            return set()
        wanted = [mode for mode in modes if mode in POLICY_MODES]
        return {(role, mode) for role in roles for mode in wanted}

    def _validate_auto_provision_uniqueness(
        self,
        session: Session,
        provider_id: uuid.UUID | None,
        roles: list[str],
        modes: list[str],
        set_user_sdk_defaults: bool,
        *,
        previously_claimed: set[tuple[str, str]] | None = None,
    ) -> None:
        """Refuse a second provider owning a ``(role, mode)`` default slot.

        ``User.default_ai_credential_<mode>_id`` holds exactly one credential,
        so two providers that would both wire it for the same role fight over it
        silently, per user, in row order. The second configuration is refused at
        write time instead.

        **The rule is scoped to the transition, not to the state**, and that is
        preserved verbatim from where it lived before: only slots this request
        *newly* claims — ``claimed - previously_claimed`` — can raise. A provider
        already sitting in a conflicting configuration is not made this request's
        problem by being touched: renaming it, rotating its key or editing an
        unrelated field must not 409 on a collision the admin did not introduce.

        The alternative — validating the effective end state — reads as safer and
        is not. It makes a stale absolute payload indistinguishable from a
        deliberate edit, so a client rebuilding its request from an open-time
        snapshot gets refused for someone else's change.

        ``provider_id`` is the provider being written (excluded from the search),
        or ``None`` on create, where every slot is new.
        """
        claimed = self._claimed_slots(roles, modes, set_user_sdk_defaults)
        newly_claimed = claimed - (previously_claimed or set())
        if not newly_claimed:
            return

        for other in session.exec(select(AIProvider)).all():
            if provider_id is not None and other.id == provider_id:
                continue
            overlap = newly_claimed & policy_from_provider(other).claimed_slots()
            if not overlap:
                continue
            role, mode = sorted(overlap)[0]
            raise AIProviderConflictError(
                conflicting_name=other.name,
                conflicting_id=other.id,
                role=role,
                mode=mode,
            )

    # ------------------------------------------------------------------ #
    # Policy writes reached from the credential side
    # ------------------------------------------------------------------ #

    def write_policy_fields(
        self,
        session: Session,
        provider_id: uuid.UUID,
        fields: dict[str, Any],
    ) -> AIProvider:
        """Write wiring-policy columns onto a provider, and commit.

        Exists because a path on the credential side can hold a policy value that
        belongs to the provider. Writing such a value onto the credential's
        shadowed columns would save successfully and change nothing, which is the
        failure the split exists to remove.

        **One caller today**: ``ManagedAICredentialsService.set_default_all``
        (``grep -rn write_policy_fields app/``). It had a second — the minted
        create on ``/admin/llm-providers``, which submitted a policy alongside
        the credential — and Phase 4 removed that shape from the request model
        entirely, so it is gone rather than merely unused.

        Deliberately narrow: it takes already-normalised values and does no
        validation of its own, because its caller has run the same normalisation
        the provider surface runs. It also does **not** re-apply the policy to
        existing members — a caller that needs that must reconcile the owned
        credential itself. ``set_default_all`` does not, and does not need to: it
        applies the flag to each member directly first, which leaves nothing for
        a reconcile to do *for that one flag*. That is not a licence for the next
        caller to skip it.
        """
        record = self._get_or_404(session, provider_id)
        for name, value in fields.items():
            setattr(record, name, value)
        record.updated_at = datetime.now(timezone.utc)
        session.add(record)
        session.commit()
        session.refresh(record)
        return record

    # ------------------------------------------------------------------ #
    # CRUD
    # ------------------------------------------------------------------ #

    def create(
        self,
        session: Session,
        data: AIProviderCreate,
        actor: User,
    ) -> tuple[AIProvider, ManagedAICredentialReconcileResult]:
        """Create a provider **and its one managed credential**, together.

        The 1:1 shape is not a schema constraint (the FK allows many), so it is
        held here: the pair is written in one call and nothing else creates a
        provider that is meant to hand keys out. The reconcile that follows adds
        the initial members, which is the same path every later grant takes.

        The conflict check runs **before the row exists**: a refused create must
        not leave a half-configured provider behind for the admin to clean up.
        """
        kind = AIProviderKind(data.kind)
        self._validate_shape(
            kind=kind,
            provider_type=data.type,
            secret=data.secret,
            config=data.config,
            base_url=data.base_url,
            model=data.model,
        )
        roles = self._normalize_roles(data.auto_provision_roles) or []
        self._validate_auto_provision_uniqueness(
            session,
            None,
            roles,
            data.sdk_default_modes,
            data.set_user_sdk_defaults,
        )

        now = datetime.now(timezone.utc)
        provider = AIProvider(
            name=data.name,
            kind=kind.value,
            provider_type=data.type,
            encrypted_secret=self._encrypt_secret(
                kind, data.type, data.secret, data.base_url, data.model
            ),
            config=data.config.model_dump(),
            base_url=data.base_url,
            model=data.model,
            auto_provision_roles=roles,
            set_as_default=data.set_as_default,
            set_user_sdk_defaults=data.set_user_sdk_defaults,
            sdk_default_modes=data.sdk_default_modes,
            default_model=managed_ai_credentials_service._normalize_default_model(
                data.default_model
            ),
            available_models=(
                managed_ai_credentials_service._normalize_available_models(
                    data.available_models
                )
            ),
            model_override_conversation=(
                managed_ai_credentials_service._normalize_default_model(
                    data.model_override_conversation
                )
            ),
            model_override_building=(
                managed_ai_credentials_service._normalize_default_model(
                    data.model_override_building
                )
            ),
            expiry_notification_date=data.expiry_notification_date,
            created_by_id=actor.id,
            created_at=now,
            updated_at=now,
        )
        parent = ManagedAICredential(
            name=data.name,
            type=data.type,
            # The key lives on the provider, which is the only source of truth
            # for it. NULL here for both kinds.
            encrypted_data=None,
            provider_id=provider.id,
            base_url=data.base_url,
            model=data.model,
            managed_by_id=actor.id,
            created_at=now,
            updated_at=now,
        )
        # **One transaction for the pair.** Two commits would make the 1:1
        # invariant this method exists to hold breakable by this method: a
        # failure between them leaves a lone provider row, which is the very
        # state the rest of the service carries special-case handling for.
        #
        # The ``flush`` is not a stylistic choice. Nothing declares an ORM
        # relationship between the two, so the unit of work has no dependency to
        # order them by and will happily emit the child's INSERT first — which
        # trips ``fk_managed_ai_credential_provider`` immediately. Flushing the
        # provider orders the two statements *inside* the same transaction, so
        # the pair still commits or fails together.
        session.add(provider)
        session.flush()
        session.add(parent)
        session.commit()
        session.refresh(provider)
        session.refresh(parent)

        result = managed_ai_credentials_service.reconcile(
            session,
            actor,
            parent,
            list(data.target_user_ids),
            apply_fields=False,
            force=False,
            key_rotated=False,
        )
        return provider, result

    def update(
        self,
        session: Session,
        provider_id: uuid.UUID,
        data: AIProviderUpdate,
        actor: User,
    ) -> tuple[AIProvider, ManagedAICredentialReconcileResult | None]:
        """Partial update — and a policy edit re-applies to existing members.

        That is the whole reason this returns a reconcile result. The machinery
        already existed on the credential side; what this does is reach it from
        the provider, with ``cleared_overrides`` carried down so a cleared
        model override actually **retracts** members' pins instead of being
        cosmetic (the record would show blank while every member stayed pinned,
        with no admin action left that could remove it).

        ``model_override_*`` keeps the three-state contract verbatim: omitted
        leaves the stored value alone, ``""`` clears it, a model id sets it.

        The reconcile result is ``None`` for a **lone** provider row, which has
        no credential to re-apply to — see
        :attr:`AIProviderPublic.owned_credential_id` for how one exists.
        """
        provider = self._get_or_404(session, provider_id)
        parent = self.owned_credential(session, provider)

        previous_policy = policy_from_provider(provider)
        previously_claimed = previous_policy.claimed_slots()
        previous_overrides = {
            mode: previous_policy.model_override_for(mode)
            for mode in POLICY_MODES
        }

        roles = self._normalize_roles(data.auto_provision_roles)
        effective_roles = (
            roles if roles is not None else previous_policy.auto_provision_roles
        )
        effective_modes = (
            data.sdk_default_modes
            if data.sdk_default_modes is not None
            else previous_policy.sdk_default_modes
        )
        effective_sdk_defaults = (
            data.set_user_sdk_defaults
            if data.set_user_sdk_defaults is not None
            else previous_policy.set_user_sdk_defaults
        )
        # Before a single field is written, so a PATCH that introduces a
        # conflict cannot half-apply.
        self._validate_auto_provision_uniqueness(
            session,
            provider.id,
            effective_roles or [],
            effective_modes or [],
            effective_sdk_defaults,
            previously_claimed=previously_claimed,
        )

        if data.name is not None:
            provider.name = data.name
        if data.config is not None:
            config_changed = data.config.model_dump() != provider.config
            provider.config = data.config.model_dump()
            if config_changed:
                # A verification is of a (secret, project) pair, not of a
                # secret: re-pointing at another project means nobody has
                # checked that project's spend limit, and a surviving stamp
                # would report "verified, capped" about it.
                provider.last_verified_at = None
                provider.last_verify_error = None
        if data.base_url is not None:
            provider.base_url = data.base_url
        if data.model is not None:
            provider.model = data.model
        if roles is not None:
            provider.auto_provision_roles = roles
        if data.set_as_default is not None:
            provider.set_as_default = data.set_as_default
        if data.set_user_sdk_defaults is not None:
            provider.set_user_sdk_defaults = data.set_user_sdk_defaults
        if data.sdk_default_modes is not None:
            provider.sdk_default_modes = data.sdk_default_modes
        if data.default_model is not None:
            provider.default_model = (
                managed_ai_credentials_service._normalize_default_model(
                    data.default_model
                )
            )
        if data.available_models is not None:
            provider.available_models = (
                managed_ai_credentials_service._normalize_available_models(
                    data.available_models
                )
            )
        if data.model_override_conversation is not None:
            provider.model_override_conversation = (
                managed_ai_credentials_service._normalize_default_model(
                    data.model_override_conversation
                )
            )
        if data.model_override_building is not None:
            provider.model_override_building = (
                managed_ai_credentials_service._normalize_default_model(
                    data.model_override_building
                )
            )
        if data.expiry_notification_date is not None:
            provider.expiry_notification_date = data.expiry_notification_date

        # The ``fixed_key`` envelope carries ``base_url``/``model`` alongside
        # the key, and ``_add_child`` builds a new member's credential straight
        # out of it. Leaving it alone after an edit would hand members added
        # before the edit one shape and members added after it another, with
        # nothing anywhere reporting the divergence.
        if (
            provider.kind == AIProviderKind.FIXED_KEY.value
            and (data.base_url is not None or data.model is not None)
        ):
            provider.encrypted_secret = self._encrypt_secret(
                AIProviderKind.FIXED_KEY,
                provider.provider_type,
                self._decrypt_fixed_key(provider).api_key or "",
                provider.base_url,
                provider.model,
            )

        provider.updated_at = datetime.now(timezone.utc)
        session.add(provider)
        session.commit()
        session.refresh(provider)

        if parent is None:
            return provider, None

        # The name is the child credentials' name, and it lives on the managed
        # credential rather than being shadowed — so it is mirrored down here
        # and reaches every member through the same reconcile as the policy.
        if data.name is not None or data.base_url is not None or data.model is not None:
            if data.name is not None:
                parent.name = data.name
            if data.base_url is not None:
                parent.base_url = data.base_url
            if data.model is not None:
                parent.model = data.model
            parent.updated_at = provider.updated_at
            session.add(parent)
            session.commit()
            session.refresh(parent)

        updated_policy = policy_from_provider(provider)
        cleared_overrides = {
            mode: previous
            for mode, previous in previous_overrides.items()
            if previous is not None
            and updated_policy.model_override_for(mode) is None
        }
        result = self._reapply_to_members(
            session, actor, parent, cleared_overrides=cleared_overrides
        )
        return provider, result

    def _reapply_to_members(
        self,
        session: Session,
        actor: User,
        parent: ManagedAICredential,
        *,
        key_rotated: bool = False,
        cleared_overrides: dict[str, str] | None = None,
    ) -> ManagedAICredentialReconcileResult:
        """Push the current policy (and optionally a new key) onto every member.

        Membership is unchanged — the desired set is the current one — so this
        is the Update pass of reconcile and nothing else.
        """
        desired = list(
            managed_ai_credentials_service._current_members(
                session, parent
            ).keys()
        )
        return managed_ai_credentials_service.reconcile(
            session,
            actor,
            parent,
            desired,
            apply_fields=True,
            force=False,
            key_rotated=key_rotated,
            cleared_overrides=cleared_overrides,
        )

    def rotate_key(
        self,
        session: Session,
        provider_id: uuid.UUID,
        api_key: str,
        actor: User,
    ) -> tuple[AIProvider, ManagedAICredentialReconcileResult]:
        """Replace a ``fixed_key`` provider's key and re-key every member.

        **Refused on a ``minted`` provider with a 400.** There is nothing here to
        rotate: that provider's secret creates keys, it is not one of them, and
        rotating a minted member's key is a per-member re-mint. Accepting the
        request would store a key nothing reads and leave the admin believing
        they had rolled one.
        """
        provider = self._get_or_404(session, provider_id)
        if provider.kind != AIProviderKind.FIXED_KEY.value:
            raise HTTPException(
                status_code=400,
                detail=(
                    "This provider creates a separate key for each member and "
                    "holds no key of its own, so there is nothing to rotate. "
                    "Re-create an individual member's key instead."
                ),
            )
        parent = self._owned_credential_or_404(session, provider)

        provider.encrypted_secret = self._encrypt_secret(
            AIProviderKind.FIXED_KEY,
            provider.provider_type,
            api_key,
            provider.base_url,
            provider.model,
        )
        # The stamp described the key that was just replaced.
        provider.last_verified_at = None
        provider.last_verify_error = None
        provider.updated_at = datetime.now(timezone.utc)
        session.add(provider)
        session.commit()
        session.refresh(provider)

        result = self._reapply_to_members(
            session, actor, parent, key_rotated=True
        )
        return provider, result

    def _encrypt_secret(
        self,
        kind: AIProviderKind,
        provider_type: AICredentialType,
        secret: str,
        base_url: str | None,
        model: str | None,
    ) -> str:
        """Fernet-encrypt the secret in the envelope its ``kind`` calls for.

        ``fixed_key`` uses the credential envelope (``{api_key, base_url,
        model}``) because a member's child credential is written from it
        verbatim; ``minted`` stores the administration secret bare, because
        nothing ever reads it as a model key.
        """
        if kind == AIProviderKind.FIXED_KEY:
            return managed_ai_credentials_service._encrypt_key(
                provider_type, secret, base_url, model
            )
        return encrypt_field(secret)

    # ------------------------------------------------------------------ #
    # Verification
    # ------------------------------------------------------------------ #

    async def verify(
        self, session: Session, provider_id: uuid.UUID
    ) -> AIProviderVerifyResult:
        """Ask the provider whether this configuration still works.

        Two different questions by ``kind``, and the result says which was asked:

        * ``minted`` — does the administration secret authenticate, and does the
          project carry a hard spend limit? Both, because they fail
          independently and an admin who fixes one wants to see the other. The
          cap is only ever *read*; it belongs to the provider's console.
        * ``fixed_key`` — does the key work, asked with the same probe the Test
          Connection button uses. There is no project here, so
          ``checked_spend_limit`` is False and the spend fields are not answers.
        """
        record = self._get_or_404(session, provider_id)
        if record.kind == AIProviderKind.MINTED.value:
            return await self._verify_minted(session, record)
        return await self._verify_fixed_key(session, record)

    async def _verify_minted(
        self, session: Session, record: AIProvider
    ) -> AIProviderVerifyResult:
        provisioner = self._provisioner(record)
        secret = self.decrypt_secret(record)
        try:
            account_ref = await provisioner.verify_admin_access(
                secret, record.config
            )
            limit = await provisioner.verify_spend_limit(secret, record.config)
        except ProviderAdminError as exc:
            self._stamp_verification(session, record, error=exc.code)
            return AIProviderVerifyResult(
                ok=False, checked_spend_limit=True, error=exc.code
            )

        if not limit.is_capped:
            self._stamp_verification(
                session, record, error="project_not_capped"
            )
            return AIProviderVerifyResult(
                ok=False,
                account_ref=account_ref,
                checked_spend_limit=True,
                spend_limit_enforcing=False,
                spend_limit_cents=limit.threshold_cents,
                error="project_not_capped",
            )

        self._stamp_verification(session, record, error=None)
        return AIProviderVerifyResult(
            ok=True,
            account_ref=account_ref,
            checked_spend_limit=True,
            spend_limit_enforcing=True,
            spend_limit_cents=limit.threshold_cents,
        )

    async def _verify_fixed_key(
        self, session: Session, record: AIProvider
    ) -> AIProviderVerifyResult:
        from app.services.credentials.model_discovery_service import (
            probe_models,
        )

        data = self._decrypt_fixed_key(record)
        probe = await probe_models(
            record.provider_type, data.api_key or "", data.base_url
        )
        # A benign skip — a provider with no model-list endpoint, an OAuth
        # token that cannot call one — is not a failed key. ``ProbeResult.ok``
        # is already that distinction; re-deriving it from the reason code here
        # would be a second answer to a question the probe has answered.
        error = None if probe.ok else (probe.reason or "invalid_key")
        self._stamp_verification(session, record, error=error)
        return AIProviderVerifyResult(ok=probe.ok, error=error)

    def _decrypt_fixed_key(self, record: AIProvider) -> AICredentialData:
        """The ``fixed_key`` envelope, decrypted. Never called for ``minted``."""
        return AICredentialData(
            **json.loads(decrypt_field(record.encrypted_secret))
        )

    def _stamp_verification(
        self, session: Session, record: AIProvider, *, error: str | None
    ) -> None:
        record.last_verify_error = error
        if error is None:
            record.last_verified_at = datetime.now(timezone.utc)
        record.updated_at = datetime.now(timezone.utc)
        session.add(record)
        session.commit()
        session.refresh(record)

    # ------------------------------------------------------------------ #
    # Grants
    # ------------------------------------------------------------------ #

    def apply_to_existing(
        self,
        session: Session,
        provider_id: uuid.UUID,
        actor: User,
        *,
        dry_run: bool = False,
    ) -> ManagedAICredentialApplyResult:
        """Grant this provider to every existing account its roles cover.

        ``auto_provision_roles`` fires at account *creation*; turning it on does
        nothing for the people already on the instance, and re-running
        provisioning on a role change was rejected as a design. This is the
        explicit action that closes that gap, and being explicit is why it is
        one of the two acts that **do** overwrite a default somebody already
        holds — automatic provisioning never does.

        Delegates to the owned credential, which is where membership lives.
        """
        provider = self._get_or_404(session, provider_id)
        parent = self._owned_credential_or_404(session, provider)
        return managed_ai_credentials_service.apply_to_existing(
            session, actor, parent.id, dry_run=dry_run
        )

    # ------------------------------------------------------------------ #
    # Deletion
    # ------------------------------------------------------------------ #

    def delete_impact(
        self, session: Session, provider: AIProvider
    ) -> AIProviderDeleteImpact:
        """What deleting this provider costs, as the 409 body would state it."""
        parent = self.owned_credential(session, provider)
        if parent is None:
            # A lone provider row. Nothing holds a key from it, so the impact is
            # genuinely empty and the delete needs no confirmation.
            return AIProviderDeleteImpact(
                provider_id=provider.id, provider_name=provider.name
            )
        memberships = session.exec(
            select(ManagedAICredentialMembership).where(
                ManagedAICredentialMembership.managed_credential_id == parent.id
            )
        ).all()
        owners = {
            row.id: row
            for row in session.exec(
                select(User).where(
                    col(User.id).in_([m.user_id for m in memberships])
                )
            ).all()
        } if memberships else {}
        members = [
            AIProviderDeleteMember(
                user_id=m.user_id,
                email=owners[m.user_id].email if m.user_id in owners else "",
                full_name=(
                    owners[m.user_id].full_name if m.user_id in owners else None
                ),
                holds_provider_key=holds_provider_key(m),
            )
            for m in memberships
        ]
        return AIProviderDeleteImpact(
            provider_id=provider.id,
            provider_name=provider.name,
            owned_credential_id=parent.id,
            member_count=len(members),
            minted_key_count=sum(1 for m in members if m.holds_provider_key),
            members=members,
        )

    async def delete(
        self,
        session: Session,
        provider_id: uuid.UUID,
        actor: User,
        *,
        force: bool = False,
    ) -> AIProviderDeleteImpact:
        """Delete the provider, refusing while anybody still holds a key from it.

        Unforced and the owned credential has **any** membership row → raises
        :class:`AIProviderInUseError` carrying the impact, which the route turns
        into a 409. A provider whose credential has no members deletes without a
        confirm — there is nobody to warn.

        Forced, the order is fixed and is the point (§5.4 of the
        ai-credential-providers plan):

        1. **revoke every minted key at the provider**, awaited here rather than
           handed to the background loop;
        2. delete the managed credential with ``force=True`` — that path deletes
           the child ``AICredential`` rows, the membership rows and the
           credential itself;
        3. delete the provider.

        **Step 1 is awaited, and this is the one place that inverts the
        service's usual delete-then-revoke rule.** Everywhere else the revoke
        comes second, because the delete can be refused by the blast-radius gate
        and destroying a key for a removal the database then declines is not
        recoverable. Here the thing being deleted *is the secret the revoke
        needs*: a scheduled revocation would run after ``session.delete(provider)``
        had committed, find no administration secret, and record every key as
        ``revoke_failed`` — live at the provider forever with only an audit line
        naming it. So the keys are destroyed while the secret still exists, and
        the handles are cleared from the rows afterwards so the credential delete
        cannot schedule a second, doomed attempt at the same key.

        The FK is ``ON DELETE RESTRICT``, so getting the row order wrong surfaces
        as a database error rather than as an orphaned credential that looks
        manual, is fully editable, and has a NULL key.

        **The residual, stated rather than left to be discovered.** Revoking
        first means a forced delete that then fails to remove a member — a mint
        in flight, a lock timeout — leaves that member holding a key that no
        longer works at the vendor. Whether the call then refuses depends on
        whether the *credential row* survived, not on whether members were
        blocked: ``force`` deletes the parent even while reporting blocked
        members, and refusing on the report alone deleted the credential,
        answered 409 and left a lone provider still auto-provisioning — the
        silent no-op this gate exists to prevent, produced by the gate. So the
        409 is raised only when the credential is genuinely still there, where
        RESTRICT would otherwise trip; the admin retries and the retry completes
        the removal. Pinned by
        ``minted_ai_credentials_test.py::test_a_member_cannot_be_removed_while_their_mint_is_in_flight``.

        Returns the impact as it was at the moment of deletion, so the audit
        entry says what state the provider was in rather than merely that it
        went.
        """
        provider = self._get_or_404(session, provider_id)
        impact = self.delete_impact(session, provider)
        if impact.member_count and not force:
            raise AIProviderInUseError(impact)

        parent = self.owned_credential(session, provider)
        if parent is not None:
            await self._revoke_member_keys(session, provider, parent)
            result = managed_ai_credentials_service.delete(
                session,
                actor,
                parent.id,
                force=True,
                # The one sanctioned way past that method's provider-owned
                # refusal: it exists to stop the credential being deleted out
                # from under a live provider, and here the provider is going
                # too, immediately below.
                allow_provider_owned=True,
            )
            # ``force`` gets past the blast-radius gate but not past a lock
            # timeout or a constraint trip. **Whether that leaves the credential
            # behind is the question, and ``result.blocked`` does not answer
            # it**: ``ManagedAICredentialsService.delete(force=True)`` reports
            # blocked members *and deletes the parent anyway*, sweeping their
            # stranded keys. Refusing on ``blocked`` alone therefore deleted the
            # credential, answered 409, and left the provider standing — a lone
            # provider row still carrying its ``auto_provision_roles``, which is
            # the silent-no-op state this service refuses everywhere else.
            #
            # So the predicate is the row: if the credential is genuinely still
            # there, deleting the provider would hit RESTRICT and this says why
            # instead of raising an IntegrityError the admin cannot act on. If it
            # is gone, there is nothing left to protect and the provider follows
            # it, blocked members or not.
            if self.owned_credential(session, provider) is not None:
                raise HTTPException(
                    status_code=409,
                    detail={
                        # Structured, and carrying a ``code``, because this is
                        # the *second* 409 shape ``DELETE /admin/ai-providers/{id}``
                        # can answer. A client that reads ``detail.code`` on the
                        # first one (``ai_provider_in_use``) would get
                        # ``undefined`` here and render nothing at all — one
                        # endpoint, one contract.
                        "code": "ai_provider_members_not_removed",
                        "message": (
                            f"{len(result.blocked)} member(s) of "
                            f"'{provider.name}' could not be removed, so the "
                            "provider was left in place. Try again."
                        ),
                        "blocked": [
                            block.model_dump(mode="json")
                            for block in result.blocked
                        ],
                    },
                )

        session.delete(provider)
        session.commit()
        return impact

    async def _revoke_member_keys(
        self,
        session: Session,
        provider: AIProvider,
        parent: ManagedAICredential,
    ) -> None:
        """Destroy every key this provider minted, then forget the handles.

        Awaited, and before any row goes — see :meth:`delete`. ``revoke_now``
        records its own outcome for each key, success or failure, so the handles
        are cleared either way: a failure already has a durable event carrying
        its external ref, and leaving the ref on a row that is about to be
        deleted would only produce a second, identical record from the
        credential delete's own scheduled pass.
        """
        from app.services.credentials.key_provisioning_service import (
            key_provisioning_service,
        )

        memberships = [
            row
            for row in session.exec(
                select(ManagedAICredentialMembership).where(
                    ManagedAICredentialMembership.managed_credential_id
                    == parent.id
                )
            ).all()
            if holds_provider_key(row)
        ]
        if not memberships:
            return
        provider_type = (
            parent.type.value
            if isinstance(parent.type, AICredentialType)
            else str(parent.type)
        )
        await key_provisioning_service.revoke_now(
            session,
            [
                RevocationRequest(
                    user_id=row.user_id,
                    parent_id=parent.id,
                    provider_admin_credential_id=provider.id,
                    provider_type=provider_type,
                    external_key_ref=dict(row.external_key_ref or {}),
                    audit_user_id=row.user_id,
                )
                for row in memberships
            ],
        )
        for row in memberships:
            row.external_key_ref = None
            session.add(row)
        session.commit()

    # ------------------------------------------------------------------ #
    # Listing / projection
    # ------------------------------------------------------------------ #

    def list(self, session: Session) -> list[AIProviderPublic]:
        records = session.exec(
            select(AIProvider).order_by(col(AIProvider.created_at).desc())
        ).all()
        return [self.to_public(session, record) for record in records]

    def get(
        self, session: Session, provider_id: uuid.UUID
    ) -> AIProviderPublic:
        return self.to_public(session, self._get_or_404(session, provider_id))

    def to_public(
        self, session: Session, record: AIProvider
    ) -> AIProviderPublic:
        """Project one provider. **Never raises for a lone row.**

        A provider with no managed credential is describable — as a provider
        with no members — and it has to be, because the alternative is one
        legacy row taking the whole admin listing down with a 404. The actions
        that genuinely need a credential (``rotate_key``, ``apply_to_existing``)
        are the ones that refuse.
        """
        parent = self.owned_credential(session, record)
        summary: dict[str, int] = {}
        if parent is not None:
            for status, count in session.exec(
                select(
                    ManagedAICredentialMembership.status, func.count()
                )
                .where(
                    ManagedAICredentialMembership.managed_credential_id
                    == parent.id
                )
                .group_by(col(ManagedAICredentialMembership.status))
            ).all():
                summary[status] = int(count)
        policy = policy_from_provider(record)
        return AIProviderPublic(
            id=record.id,
            name=record.name,
            kind=record.kind,
            type=record.provider_type,
            config=ProviderAdminCredentialConfig(**(record.config or {})),
            has_secret=bool(record.encrypted_secret),
            base_url=record.base_url,
            model=record.model,
            auto_provision_roles=policy.auto_provision_roles,
            set_as_default=policy.set_as_default,
            set_user_sdk_defaults=policy.set_user_sdk_defaults,
            sdk_default_modes=policy.sdk_default_modes,
            default_model=policy.default_model,
            available_models=policy.available_models,
            model_override_conversation=policy.model_override_conversation,
            model_override_building=policy.model_override_building,
            expiry_notification_date=policy.expiry_notification_date,
            last_verified_at=record.last_verified_at,
            last_verify_error=record.last_verify_error,
            created_by_id=record.created_by_id,
            owned_credential_id=parent.id if parent is not None else None,
            member_count=sum(summary.values()),
            key_state_summary=summary,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )


ai_providers_service = AIProvidersService()

__all__ = [
    "AIProviderConflictError",
    "AIProviderInUseError",
    "AIProvidersService",
    "ai_providers_service",
]
