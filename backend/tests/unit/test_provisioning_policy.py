"""The one read path for a managed AI credential's wiring policy.

Pure unit tests — no database, no TestClient. Everything here exercises the two
constructors and the branch between them, which is the whole of the resolver's
decision; ``resolve_policy``'s session round-trip (a provider edit being visible
immediately, with no second copy left on the credential) is asserted through the
API in ``tests/api/ai_credentials/ai_providers_service_test.py::
test_a_policy_edit_writes_through_to_every_existing_member``.

Paired-file pointer, per this directory's cross-reference convention:
``tests/api/ai_credentials/ai_providers_service_test.py``.

WHY THIS MATTERS MORE THAN IT LOOKS
-----------------------------------
``managed_ai_credential`` still *has* every policy column. On a provider-owned
record they are shadowed — present, not read — and they hold whatever they held
before the provider took ownership. A resolver that fell back to them for a
provider-owned record would present stale values as the active policy, which is
worse than presenting none, and no type error anywhere would say so.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from app.models.credentials.ai_credential import AICredentialType
from app.models.credentials.managed_ai_credential import (
    ManagedAICredential,
    ProvisioningMode,
)
from app.models.credentials.provider_admin_credential import (
    AIProvider,
    AIProviderKind,
)
from app.services.credentials.provisioning_policy import (
    _policy_for,
    policy_from_manual,
    policy_from_provider,
)

EXPIRY = datetime(2030, 1, 1, tzinfo=timezone.utc)


def _provider(**overrides) -> AIProvider:
    fields = dict(
        id=uuid.uuid4(),
        name="Company OpenAI",
        kind=AIProviderKind.FIXED_KEY.value,
        provider_type=AICredentialType.ANTHROPIC,
        encrypted_secret="envelope",
        config={},
        auto_provision_roles=["agent-user"],
        set_as_default=True,
        set_user_sdk_defaults=True,
        sdk_default_modes=["conversation"],
        default_model="provider-model",
        available_models=["provider-model"],
        model_override_conversation="provider-override",
        model_override_building=None,
        expiry_notification_date=EXPIRY,
    )
    fields.update(overrides)
    return AIProvider(**fields)


def _credential(**overrides) -> ManagedAICredential:
    """A record whose own columns say something *different* from any provider.

    Deliberately different in every field: a test whose two sides agree cannot
    tell which one was read.
    """
    fields = dict(
        id=uuid.uuid4(),
        name="Managed",
        type=AICredentialType.ANTHROPIC,
        set_as_default=False,
        set_user_sdk_defaults=False,
        sdk_default_modes=["building"],
        default_model="credential-model",
        available_models=["credential-model"],
        model_override_conversation="credential-override",
        model_override_building="credential-override-building",
        expiry_notification_date=None,
    )
    fields.update(overrides)
    return ManagedAICredential(**fields)


# ── The two constructors ───────────────────────────────────────────────


def test_a_provider_owned_records_policy_is_entirely_the_providers() -> None:
    provider = _provider()
    credential = _credential(provider_id=provider.id)

    policy = _policy_for(credential, provider)

    assert policy.source == "provider"
    assert policy.is_provider_owned is True
    assert policy.provider_id == provider.id
    assert policy.provider_name == "Company OpenAI"
    assert policy.auto_provision_roles == ["agent-user"]
    assert policy.set_as_default is True
    assert policy.set_user_sdk_defaults is True
    assert policy.sdk_default_modes == ["conversation"]
    assert policy.default_model == "provider-model"
    assert policy.available_models == ["provider-model"]
    assert policy.model_override_conversation == "provider-override"
    assert policy.model_override_building is None
    assert policy.expiry_notification_date == EXPIRY


def test_a_manual_records_policy_is_entirely_its_own_columns() -> None:
    credential = _credential(provider_id=None)

    policy = _policy_for(credential, None)

    assert policy.source == "manual"
    assert policy.is_provider_owned is False
    assert policy.provider_id is None
    assert policy.provider_name is None
    assert policy.set_as_default is False
    assert policy.sdk_default_modes == ["building"]
    assert policy.default_model == "credential-model"
    assert policy.available_models == ["credential-model"]
    assert policy.model_override_conversation == "credential-override"
    assert policy.model_override_building == "credential-override-building"
    assert policy.expiry_notification_date is None


def test_a_manual_record_auto_provisions_to_nobody() -> None:
    """The rule is a property of a provider.

    A record with no provider is one an admin hands out by hand, so the resolver
    answers with an empty list rather than with anything that could be mistaken
    for a rule the record no longer has a column for.
    """
    assert policy_from_manual(_credential()).auto_provision_roles == []


def test_provisioning_mode_is_derived_from_the_providers_kind() -> None:
    """Never stored, so a record and its provider cannot disagree about it."""
    minted = _provider(kind=AIProviderKind.MINTED.value)
    fixed = _provider(kind=AIProviderKind.FIXED_KEY.value)

    assert policy_from_provider(minted).provisioning_mode is ProvisioningMode.MINTED
    assert policy_from_provider(minted).is_minted is True
    assert policy_from_provider(fixed).provisioning_mode is ProvisioningMode.SHARED
    assert policy_from_provider(fixed).is_minted is False
    assert policy_from_manual(_credential()).is_minted is False


def test_hand_edited_roles_that_are_not_a_list_read_as_empty() -> None:
    """``auto_provision_roles`` is a ``json`` column, not a typed array.

    ``in`` against a non-container raises, and this value is read on the signup
    path — where an exception is an account that failed to be created.
    """
    assert policy_from_provider(
        _provider(auto_provision_roles="agent-user")
    ).auto_provision_roles == []


# ── The unresolved provider ────────────────────────────────────────────


def test_a_provider_that_does_not_resolve_wires_nothing() -> None:
    """Not a fallback to the record's own columns, and that is the point.

    Those columns hold whatever they held before the provider took ownership. A
    policy built from them would be a stale configuration presented as the
    active one — the failure the shadowing exists to prevent — and it could
    hand out a company key to a role nobody currently intends. So the answer is
    a provider-owned policy that grants nothing and wires nothing.

    Unreachable through any service path: the FK is ``ON DELETE RESTRICT``. The
    branch exists so an admin list renders instead of raising.
    """
    orphan_id = uuid.uuid4()
    credential = _credential(provider_id=orphan_id)

    policy = _policy_for(credential, None)

    assert policy.source == "provider"
    assert policy.provider_id == orphan_id
    assert policy.provider_name is None
    assert policy.auto_provision_roles == []
    assert policy.set_as_default is False
    assert policy.set_user_sdk_defaults is False
    assert policy.sdk_default_modes == []
    assert policy.default_model is None
    assert policy.available_models is None
    assert policy.model_override_conversation is None
    assert policy.model_override_building is None
    assert policy.expiry_notification_date is None
    assert policy.provisioning_mode is ProvisioningMode.SHARED


# ── Derived answers ────────────────────────────────────────────────────


def test_model_override_for_answers_per_mode() -> None:
    policy = policy_from_provider(
        _provider(
            model_override_conversation="c-model",
            model_override_building="b-model",
        )
    )
    assert policy.model_override_for("conversation") == "c-model"
    assert policy.model_override_for("building") == "b-model"


def test_claimed_slots_needs_all_three_of_roles_defaults_and_a_real_mode() -> None:
    """The write-time conflict rule and the run-time grant read one definition.

    A provider that auto-provisions without wiring SDK defaults grants a
    credential and touches no default, so any number of those may coexist. A
    mode outside the two real ones wires nothing, so it cannot be fought over —
    ``sdk_default_modes`` is not validated at the edge, and counting a typo as a
    claim would make two providers collide over a slot neither of them has.
    """
    claiming = policy_from_provider(
        _provider(
            auto_provision_roles=["agent-user", "admin"],
            set_user_sdk_defaults=True,
            sdk_default_modes=["conversation", "building"],
        )
    )
    assert claiming.claimed_slots() == {
        ("agent-user", "conversation"),
        ("agent-user", "building"),
        ("admin", "conversation"),
        ("admin", "building"),
    }

    assert policy_from_provider(
        _provider(set_user_sdk_defaults=False)
    ).claimed_slots() == set()
    assert policy_from_provider(
        _provider(auto_provision_roles=[])
    ).claimed_slots() == set()
    assert policy_from_provider(
        _provider(sdk_default_modes=["typo"])
    ).claimed_slots() == set()
    # A manual record has no roles, so it can never claim a slot — which is why
    # the rule is provider-versus-provider and nothing else.
    assert policy_from_manual(
        _credential(set_user_sdk_defaults=True)
    ).claimed_slots() == set()


# ── The two definitions of a claim must agree ──────────────────────────


def test_the_services_prospective_claim_matches_the_resolved_one() -> None:
    """``AIProvidersService._claimed_slots`` and ``ProvisioningPolicy.claimed_slots``.

    Two implementations of one rule, and they exist for a real reason: a
    *create* has no stored row to resolve a policy from, so the write-time check
    needs a form that takes loose values. The docstrings say they are the same
    definition, and until this test nothing made that true — a mode filter
    tightened on one side and not the other would let two providers both claim a
    slot, or refuse a pair that does not collide, with no symptom until a user's
    default started depending on row order.
    """
    from app.services.credentials.ai_providers_service import (
        ai_providers_service,
    )

    cases = [
        (["agent-user", "admin"], ["conversation", "building"], True),
        (["agent-user"], ["conversation"], True),
        (["agent-user"], ["conversation"], False),
        ([], ["conversation"], True),
        (["agent-user"], [], True),
        (["agent-user"], ["typo", "conversation"], True),
        (["agent-user", "agent-user"], ["building", "building"], True),
    ]
    for roles, modes, wires_defaults in cases:
        loose = ai_providers_service._claimed_slots(roles, modes, wires_defaults)
        resolved = policy_from_provider(
            _provider(
                auto_provision_roles=roles,
                sdk_default_modes=modes,
                set_user_sdk_defaults=wires_defaults,
            )
        ).claimed_slots()
        assert loose == resolved, (
            f"The write-time check and the resolved policy disagree for "
            f"roles={roles} modes={modes} sdk={wires_defaults}: "
            f"{loose} != {resolved}"
        )
