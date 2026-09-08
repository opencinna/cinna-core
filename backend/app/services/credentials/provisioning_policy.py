"""The one read path for a managed AI credential's wiring policy.

WHY THIS MODULE EXISTS
----------------------
``ManagedAICredential`` used to be both a credential and its own factory: the
key it hands out *and* the rule for who receives one *and* every flag that says
how the receiver's profile is wired. The provider/credential split moved the
rule and the wiring policy onto ``ai_provider`` and left the same-named columns
standing on ``managed_ai_credential``, where they remain the real values for a
**manual** record and are **shadowed** — present, no longer read — on a
provider-owned one.

Two sources for one question is how a stale copy gets displayed as an active
policy. So the columns are deliberately not mirrored down from the provider, and
the branch that chooses between them is written **once, here**:

    ``resolve_policy(session, parent)`` is the only legal way to read any of the
    facts on :class:`ProvisioningPolicy` about a managed credential.

That property is not a convention. ``tests/architecture/
managed_credential_shadowed_fields_test.py`` walks ``app/`` and fails on a
direct read of a shadowed field outside this module — but it is an AST
inference, not a type checker, and **it under-approximates on purpose**: it sees
receivers bound by a parameter annotation, an annotated assignment or a
``session.get(ManagedAICredential, …)``, and a read off a receiver reached any
other way is invisible to it. That file states the same limit in its own
docstring. Green there means no *statically visible* second reader, which is a
weaker claim than "no second reader" and is the one to rely on.

WHAT IS AND IS NOT ON THE POLICY
--------------------------------
Everything a *grant* needs to decide how to wire the receiver, plus the two
facts that identify where those values came from (``source`` /
``provider_name``) so a refusal can name the provider an admin has to go and
edit instead.

**Not the key.** A policy object is copied, logged and compared; a secret has no
business in one. ``fixed_key`` key material is fetched separately, by the one
service allowed to read the provider table, at the moment it is needed.

**Not ``name`` / ``base_url`` / ``model``.** Those stay the managed
credential's own columns for provider-owned records too — they are the shape of
the child credentials it writes, not the policy for wiring them.

WHY IT DOES NOT QUERY ``ai_provider`` ITSELF
--------------------------------------------
``app/services/credentials/ai_providers_service.py`` and
``key_provisioning_service.py`` are the only two modules in ``app/`` allowed to
query :class:`AIProvider` (``tests/architecture/ai_provider_isolation_test.py``
enforces it, and §4.1 of the ai-credential-providers plan is the argument). This
module resolves providers *through* the first of them, so the allowlist stays at
two files rather than growing one for the resolver. The import is function-local
because ``ai_providers_service`` imports ``managed_ai_credentials_service``,
which imports this module.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Iterable, Literal

from sqlmodel import Session

from app.models.credentials.managed_ai_credential import (
    ManagedAICredential,
    ProvisioningMode,
)
from app.models.credentials.provider_admin_credential import (
    AIProvider,
    AIProviderKind,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    pass

logger = logging.getLogger(__name__)

#: The two profile modes a policy can wire. A mode outside this set wires
#: nothing, so it cannot be claimed and cannot be fought over.
POLICY_MODES = ("conversation", "building")


@dataclass(frozen=True)
class ProvisioningPolicy:
    """How one managed AI credential wires the people it is granted to.

    Frozen because it is an *answer*, not a handle: a caller that could mutate
    it would be editing a policy nothing persists, which reads as a write and
    is not one. Write to the provider (or, for a manual record, to the
    credential) and resolve again.
    """

    #: ``"provider"`` when an ``ai_provider`` row owns this credential and every
    #: value below came off it; ``"manual"`` when the credential is its own
    #: source of truth.
    source: Literal["provider", "manual"]
    provider_id: uuid.UUID | None
    #: The provider's display name, so a refusal can say *which* provider an
    #: admin has to go and edit. ``None`` for a manual record.
    provider_name: str | None
    #: Roles whose newly created accounts receive this credential. Always empty
    #: for a manual record: the rule is a property of a provider, and a record
    #: with no provider is one an admin hands out by hand.
    auto_provision_roles: list[str] = field(default_factory=list)
    set_as_default: bool = False
    set_user_sdk_defaults: bool = False
    sdk_default_modes: list[str] = field(default_factory=list)
    default_model: str | None = None
    available_models: list[str] | None = None
    model_override_conversation: str | None = None
    model_override_building: str | None = None
    expiry_notification_date: datetime | None = None
    #: Derived from the owning provider's ``kind``; ``shared`` for a manual
    #: record and for a ``fixed_key`` provider, ``minted`` only for a ``minted``
    #: one. Never stored, so a record and its provider cannot disagree.
    provisioning_mode: ProvisioningMode = ProvisioningMode.SHARED

    @property
    def is_provider_owned(self) -> bool:
        return self.source == "provider"

    @property
    def is_minted(self) -> bool:
        return self.provisioning_mode == ProvisioningMode.MINTED

    def model_override_for(self, mode: str) -> str | None:
        """This policy's model override for ``mode`` (``None`` when unset)."""
        if mode == "conversation":
            return self.model_override_conversation
        return self.model_override_building

    def claimed_slots(self) -> set[tuple[str, str]]:
        """The ``(role, mode)`` default slots this policy lays claim to.

        Empty unless it does all three things: auto-provision to a role, wire
        SDK defaults, and claim a real mode. A policy that auto-provisions
        without ``set_user_sdk_defaults`` grants a credential and touches no
        default, so any number of those may coexist — a supported
        configuration, not an oversight.

        Modes outside :data:`POLICY_MODES` are filtered out because a mode that
        wires nothing cannot be fought over. ``sdk_default_modes`` is not
        validated at the edge, so a typo saves successfully and then wires
        nothing; counting it as a claim would make two records collide over a
        slot neither of them has.
        """
        if not self.set_user_sdk_defaults or not self.auto_provision_roles:
            return set()
        modes = [mode for mode in self.sdk_default_modes if mode in POLICY_MODES]
        return {
            (role, mode) for role in self.auto_provision_roles for mode in modes
        }


def _roles_of(provider: AIProvider) -> list[str]:
    """``auto_provision_roles`` off a provider row, guarded.

    The column is ``json``, not a typed array, so a hand-edited row can hold
    something that is not a list. ``in`` against a non-container raises, and
    this value is read on the signup path.
    """
    roles = provider.auto_provision_roles
    return list(roles) if isinstance(roles, list) else []


def policy_from_provider(provider: AIProvider) -> ProvisioningPolicy:
    """The policy of a provider-owned credential: entirely the provider's."""
    return ProvisioningPolicy(
        source="provider",
        provider_id=provider.id,
        provider_name=provider.name,
        auto_provision_roles=_roles_of(provider),
        set_as_default=provider.set_as_default,
        set_user_sdk_defaults=provider.set_user_sdk_defaults,
        sdk_default_modes=list(provider.sdk_default_modes or []),
        default_model=provider.default_model,
        available_models=provider.available_models,
        model_override_conversation=provider.model_override_conversation,
        model_override_building=provider.model_override_building,
        expiry_notification_date=provider.expiry_notification_date,
        provisioning_mode=(
            ProvisioningMode.MINTED
            if provider.kind == AIProviderKind.MINTED.value
            else ProvisioningMode.SHARED
        ),
    )


def policy_from_manual(parent: ManagedAICredential) -> ProvisioningPolicy:
    """The policy of a manual record: entirely its own columns.

    This function and :func:`_policy_for`'s unresolved branch are the only
    places in ``app/`` that read those columns off a ``ManagedAICredential``.
    """
    return ProvisioningPolicy(
        source="manual",
        provider_id=None,
        provider_name=None,
        auto_provision_roles=[],
        set_as_default=parent.set_as_default,
        set_user_sdk_defaults=parent.set_user_sdk_defaults,
        sdk_default_modes=list(parent.sdk_default_modes or []),
        default_model=parent.default_model,
        available_models=parent.available_models,
        model_override_conversation=parent.model_override_conversation,
        model_override_building=parent.model_override_building,
        expiry_notification_date=parent.expiry_notification_date,
        provisioning_mode=ProvisioningMode.SHARED,
    )


def _policy_for(
    parent: ManagedAICredential, provider: AIProvider | None
) -> ProvisioningPolicy:
    """Pick the constructor. The whole branch, in one place.

    The third case — ``parent.provider_id`` is set and no provider came back —
    resolves to a provider-owned policy that wires **nothing** and
    auto-provisions to nobody. It deliberately does not fall back to the
    record's own shadowed columns: those hold whatever they held before the
    provider took ownership, and presenting stale values as the active policy
    is the failure this module exists to prevent. The FK is ``ON DELETE
    RESTRICT``, so no service path can produce this row; the branch is here so
    a list page renders instead of raising, and it is covered by
    ``tests/unit/test_provisioning_policy.py``.
    """
    if parent.provider_id is None:
        return policy_from_manual(parent)
    if provider is None:
        logger.warning(
            "Managed AI credential %s names provider %s, which does not "
            "exist; resolving to an empty provider policy.",
            parent.id,
            parent.provider_id,
        )
        return ProvisioningPolicy(
            source="provider",
            provider_id=parent.provider_id,
            provider_name=None,
        )
    return policy_from_provider(provider)


def resolve_policy(
    session: Session, parent: ManagedAICredential
) -> ProvisioningPolicy:
    """The wiring policy for one managed credential."""
    provider = None
    if parent.provider_id is not None:
        from app.services.credentials.ai_providers_service import (
            ai_providers_service,
        )

        provider = ai_providers_service.load(session, parent.provider_id)
    return _policy_for(parent, provider)


def resolve_policies(
    session: Session, parents: Iterable[ManagedAICredential]
) -> dict[uuid.UUID, ProvisioningPolicy]:
    """The batch form, keyed by managed credential id.

    One provider fetch for the whole list rather than one per record, which is
    what a fleet-wide admin listing needs: the page's cost grows with the
    number of records, not with how many of them share a provider.
    """
    rows = list(parents)
    provider_ids = {
        parent.provider_id for parent in rows if parent.provider_id is not None
    }
    providers: dict[uuid.UUID, AIProvider] = {}
    if provider_ids:
        from app.services.credentials.ai_providers_service import (
            ai_providers_service,
        )

        providers = ai_providers_service.load_many(session, provider_ids)
    return {
        parent.id: _policy_for(parent, providers.get(parent.provider_id))
        for parent in rows
    }


__all__ = [
    "POLICY_MODES",
    "ProvisioningPolicy",
    "policy_from_manual",
    "policy_from_provider",
    "resolve_policies",
    "resolve_policy",
]
