"""The provider surface: slot conflicts, rotation, policy write-through, delete.

An **AI provider** is where keys come from plus the rule for who automatically
gets one. It owns exactly one managed credential, which owns the member list.
This file covers the half of that split that lives on the provider; the member
list and the model-override behaviour it drives are in
``managed_ai_credential_auto_provision_test.py``.

WHY THESE GO THROUGH ``tests/utils/ai_provider_admin`` RATHER THAN HTTP
------------------------------------------------------------------------
``/admin/ai-providers`` exists as of Phase 4, and its own surface — status
codes, error envelopes, projections, the secret never leaving — is asserted over
HTTP in ``tests/api/ai_credentials/admin_ai_providers_test.py``. This file is
about the *service* underneath it: what a policy edit does to existing members,
which slot a grant claims, what a forced delete removes and in what order. Those
are values the route reports only in summary, so the calls come through the one
named, documented Rule-1 exemption module and keep their resolution. Everything
else here — accounts, membership reads, ``apply-to-existing`` — still goes over
the API. The exemption is a standing decision now rather than a phase boundary;
``tests/utils/ai_provider_admin`` says the same thing.

THE FOUR RULES THIS FILE PINS
-----------------------------
1. **Two providers may not own the same ``(role, mode)`` default slot**, because
   ``User.default_ai_credential_<mode>_id`` holds exactly one credential and the
   winner would otherwise be row order. Scoped to the *transition*: only slots a
   request newly claims can be refused.
2. **Rotation is a ``fixed_key`` thing.** On a ``minted`` provider it is a 400 —
   there is no key here to roll, and silently accepting the request is how an
   admin comes to believe they rolled one.
3. **A policy edit re-applies to existing members**, cleared overrides included.
   A clear that only blanked the provider would be cosmetic: every member would
   stay pinned to the dropped value with no admin action able to remove it.
4. **The incumbent wins at grant time.** Automatic provisioning never takes a
   default somebody already holds; ``apply_to_existing`` deliberately does.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.core.config import settings
from tests.utils.account_provisioning import get_user_row, provision_account
from tests.utils.ai_credential import create_random_ai_credential
from tests.utils.ai_provider import stub_minting_providers
from tests.utils.key_provisioning import converge_keys
from tests.utils.ai_provider_admin import (
    AIProviderConflictError,
    AIProviderInUseError,
    apply_provider_to_existing,
    create_provider_credential,
    delete_provider,
    get_provider,
    orphan_provider,
    grant_members_automatically,
    rotate_provider_key,
    update_provider,
)
from tests.utils.managed_ai_credential import (
    get_managed_credential,
    member_for,
    member_user_ids,
)
from tests.utils.user import create_random_user_with_headers

# Pure admin CRUD — no agents, no environments, no seeded default credential.
NEEDS_AGENT_STUBS = False
NEEDS_DEFAULT_CREDENTIALS = False

API = settings.API_V1_STR
ANTHROPIC_ENGINE = "claude-code/anthropic"


# ── Local helpers ──────────────────────────────────────────────────────


def _me(client: TestClient, headers: dict[str, str]) -> dict:
    response = client.get(f"{API}/users/me", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def _patch_me(client: TestClient, headers: dict[str, str], **fields) -> dict:
    response = client.patch(f"{API}/users/me", headers=headers, json=fields)
    assert response.status_code == 200, response.text
    return response.json()


def _credentials(client: TestClient, headers: dict[str, str]) -> list[dict]:
    response = client.get(f"{API}/ai-credentials/", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["data"]


# ── Rule 1: one owner per (role, mode) default slot ────────────────────


def test_two_providers_cannot_claim_the_same_role_and_mode_default(
    client: TestClient, db: Session, superuser_token_headers: dict[str, str]
) -> None:
    """
    The uniqueness rule, now between providers:
      1. "Company OpenAI" auto-provisions to developers and wires both modes
      2. "Company Claude" tries to wire ``building`` for developers → refused,
         naming the other provider and the exact cell
      3. Nothing was written — a refused create must not leave a
         half-configured provider behind for the admin to clean up
      4. The same configuration for a *different role* is accepted
      5. The same role and mode, but not wiring SDK defaults, is accepted —
         granting a credential without touching a default is a supported
         configuration, not an oversight
      6. The same role and a mode the first provider has released is accepted
    """
    # ── Phase 1 ────────────────────────────────────────────────────────
    first = create_provider_credential(
        db,
        name="Company OpenAI",
        auto_provision_roles=["agent-developer"],
        set_user_sdk_defaults=True,
        sdk_default_modes=["conversation", "building"],
    )

    # ── Phase 2 ────────────────────────────────────────────────────────
    with pytest.raises(AIProviderConflictError) as conflict:
        create_provider_credential(
            db,
            name="Company Claude",
            auto_provision_roles=["agent-developer"],
            set_user_sdk_defaults=True,
            sdk_default_modes=["building"],
        )
    # The error carries what a 409 body needs to highlight the offending cell
    # and link to the other provider — a dialog can do neither from a sentence.
    assert conflict.value.conflicting_id == first["provider_id"] or str(
        conflict.value.conflicting_id
    ) == first["provider_id"]
    assert conflict.value.conflicting_name == "Company OpenAI"
    assert conflict.value.role == "agent-developer"
    assert conflict.value.mode == "building"
    assert "Company OpenAI" in str(conflict.value)

    # ── Phase 3: nothing half-written ──────────────────────────────────
    names = {
        record["name"]
        for record in client.get(
            f"{API}/admin/llm-providers/", headers=superuser_token_headers
        ).json()
    }
    assert "Company Claude" not in names, names

    # ── Phase 4: a different role does not collide ─────────────────────
    other_role = create_provider_credential(
        db,
        name="Company Claude (admins)",
        auto_provision_roles=["admin"],
        set_user_sdk_defaults=True,
        sdk_default_modes=["building"],
    )
    assert get_provider(db, other_role["provider_id"])[
        "auto_provision_roles"
    ] == ["admin"]

    # ── Phase 5: same cell, but no SDK defaults → allowed ──────────────
    no_defaults = create_provider_credential(
        db,
        name="Company Claude (grant only)",
        auto_provision_roles=["agent-developer"],
        set_user_sdk_defaults=False,
        sdk_default_modes=["building"],
    )
    assert no_defaults["record"]["set_user_sdk_defaults"] is False
    assert get_provider(db, no_defaults["provider_id"])[
        "auto_provision_roles"
    ] == ["agent-developer"]

    # ── Phase 6: a released mode is free again ─────────────────────────
    narrowed = update_provider(
        db, first["provider_id"], sdk_default_modes=["conversation"]
    )["provider"]
    assert narrowed["sdk_default_modes"] == ["conversation"]

    freed = create_provider_credential(
        db,
        name="Company Claude (building)",
        auto_provision_roles=["agent-developer"],
        set_user_sdk_defaults=True,
        sdk_default_modes=["building"],
    )
    assert freed["record"]["sdk_default_modes"] == ["building"]


def test_an_update_that_would_introduce_a_conflict_is_refused_and_applies_nothing(
    client: TestClient, db: Session, superuser_token_headers: dict[str, str]
) -> None:
    """
    The same rule on an edit, where "applies nothing" is the harder half:
      1. Two providers coexist because they cover different roles
      2. Widening the second one's roles onto the first's would collide → refused
      3. The second provider is completely unchanged — roles *and* the name that
         rode along in the same request
      4. An edit that touches only the name is not refused for a conflict it did
         not introduce
      5. Nor is one that renames *and* resubmits the provider's own
         auto-provision fields unchanged — the rule is scoped to the slots a
         request newly claims, not to the state it happens to describe
      6. The same widening with SDK defaults turned off is accepted
    """
    # ── Phase 1 ────────────────────────────────────────────────────────
    incumbent = create_provider_credential(
        db,
        name="Incumbent",
        auto_provision_roles=["agent-user"],
        set_user_sdk_defaults=True,
        sdk_default_modes=["conversation"],
    )
    challenger = create_provider_credential(
        db,
        name="Challenger",
        auto_provision_roles=["agent-developer"],
        set_user_sdk_defaults=True,
        sdk_default_modes=["conversation"],
    )

    # ── Phase 2 ────────────────────────────────────────────────────────
    with pytest.raises(AIProviderConflictError) as conflict:
        update_provider(
            db,
            challenger["provider_id"],
            name="Challenger renamed",
            auto_provision_roles=["agent-user", "agent-developer"],
        )
    assert str(conflict.value.conflicting_id) == incumbent["provider_id"]
    assert conflict.value.conflicting_name == "Incumbent"
    assert conflict.value.role == "agent-user"
    assert conflict.value.mode == "conversation"

    # ── Phase 3: not even the name landed ──────────────────────────────
    unchanged = get_provider(db, challenger["provider_id"])
    assert unchanged["name"] == "Challenger"
    assert unchanged["auto_provision_roles"] == ["agent-developer"]

    # ── Phase 4: an unrelated edit still goes through ──────────────────
    renamed = update_provider(
        db, challenger["provider_id"], name="Challenger v2"
    )["provider"]
    assert renamed["name"] == "Challenger v2"
    assert renamed["auto_provision_roles"] == ["agent-developer"]

    # ── Phase 5: a stale absolute payload claims nothing new ───────────
    # What a dialog sends on a rename when it rebuilds every field from the
    # snapshot it opened with. Nothing here is a new claim, so nothing here may
    # be refused — which is why the validator compares transitions rather than
    # end states.
    resubmitted = update_provider(
        db,
        challenger["provider_id"],
        name="Challenger v3",
        set_user_sdk_defaults=True,
        sdk_default_modes=["conversation"],
        auto_provision_roles=["agent-developer"],
    )["provider"]
    assert resubmitted["name"] == "Challenger v3"
    assert resubmitted["auto_provision_roles"] == ["agent-developer"]

    # ── Phase 6: no SDK defaults, no fight ─────────────────────────────
    widened = update_provider(
        db,
        challenger["provider_id"],
        set_user_sdk_defaults=False,
        auto_provision_roles=["agent-user", "agent-developer"],
    )["provider"]
    assert widened["set_user_sdk_defaults"] is False
    assert set(widened["auto_provision_roles"]) == {
        "agent-user",
        "agent-developer",
    }


def test_set_as_default_is_not_covered_by_the_uniqueness_rule(
    client: TestClient, db: Session, superuser_token_headers: dict[str, str]
) -> None:
    """Pinned as known behaviour, not asserted as correct.

    The uniqueness rule guards ``default_ai_credential_<mode>_id`` only. Two
    auto-provisioning providers that both carry ``set_as_default`` — the child
    credential's own "is default for its type" flag — are accepted.

    Recorded here so the gap is a decision with a test next to it rather than
    something a reader has to infer from the absence of one. If ``set_as_default``
    is ever brought under the rule, this test is the one that fails and the one
    to rewrite.
    """
    create_provider_credential(
        db,
        name="Default A",
        auto_provision_roles=["agent-user"],
        set_as_default=True,
        set_user_sdk_defaults=False,
    )
    accepted = create_provider_credential(
        db,
        name="Default B",
        auto_provision_roles=["agent-user"],
        set_as_default=True,
        set_user_sdk_defaults=False,
    )
    assert accepted["record"]["set_as_default"] is True
    assert get_provider(db, accepted["provider_id"])[
        "auto_provision_roles"
    ] == ["agent-user"]


def test_an_unknown_role_on_a_provider_is_rejected(
    client: TestClient, db: Session, superuser_token_headers: dict[str, str]
) -> None:
    """A mistyped role is a 400, not a silent drop.

    Silently dropping it would let an admin save successfully and then watch
    nothing happen at the next signup, with no clue why. Duplicates and
    whitespace, on the other hand, are canonicalised rather than refused —
    those are not mistakes worth an error.
    """
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as refused:
        create_provider_credential(
            db, name="Typo", auto_provision_roles=["agent-develper"]
        )
    assert refused.value.status_code == 400
    assert "agent-develper" in refused.value.detail

    canonicalised = create_provider_credential(
        db, name="Tidy", auto_provision_roles=[" agent-user ", "agent-user", ""]
    )
    assert get_provider(db, canonicalised["provider_id"])[
        "auto_provision_roles"
    ] == ["agent-user"]


# ── Rule 2: rotation is a fixed-key thing ──────────────────────────────


def test_rotating_a_fixed_key_provider_re_keys_every_member(
    client: TestClient, db: Session, superuser_token_headers: dict[str, str]
) -> None:
    """One paste, and everybody holding a copy gets the new key.

    A ``fixed_key`` provider's members each hold a *copy* of the one key, so a
    rotation that stopped at the provider would leave every member using the
    key that was just retired — working today, dead the moment the old one is
    revoked at the vendor, and with nothing in the product saying so.

    The stored key is never projected, so what is asserted is the observable
    consequence: the member's credential row was rewritten by the rotation, and
    the provider's verification stamp — which described the key that has just
    been replaced — is cleared.
    """
    user, headers = create_random_user_with_headers(client)
    created = create_provider_credential(
        db,
        name="Company Fixed",
        secret="sk-ant-original-key",
        target_user_ids=[user["id"]],
    )
    assert member_user_ids(created) == {user["id"]}
    before = _credentials(client, headers)
    assert len(before) == 1

    rotated = rotate_provider_key(
        db, created["provider_id"], "sk-ant-rotated-key"
    )
    assert rotated["provider"]["last_verified_at"] is None
    # The member's child row was rewritten — the update pass reports exactly the
    # members it touched, and a rotation touches all of them.
    assert [m["user_id"] for m in rotated["result"]["updated"]] == [user["id"]]

    after = _credentials(client, headers)
    assert len(after) == 1
    assert after[0]["id"] == before[0]["id"]
    assert after[0]["updated_at"] != before[0]["updated_at"]


def test_rotating_a_minted_provider_is_refused_rather_than_accepted(
    client: TestClient, db: Session, superuser_token_headers: dict[str, str]
) -> None:
    """There is nothing here to roll, and saying so is the whole point.

    A ``minted`` provider's secret creates keys; it is not one of them. Storing
    a pasted key against it would leave an administrator believing they had
    rolled a key that is still live at the vendor — the failure mode the
    equivalent refusal on a minted managed credential already exists for.
    """
    from fastapi import HTTPException

    created = create_provider_credential(
        db, name="Company Minted", kind="minted", credential_type="openai"
    )
    with pytest.raises(HTTPException) as refused:
        rotate_provider_key(db, created["provider_id"], "sk-whatever")
    assert refused.value.status_code == 400
    assert "nothing to rotate" in refused.value.detail


# ── Rule 3: a policy edit re-applies to existing members ───────────────


def test_a_policy_edit_writes_through_to_every_existing_member(
    client: TestClient, db: Session, superuser_token_headers: dict[str, str]
) -> None:
    """Editing the provider is how a member's wiring changes.

      1. A provider with two members, wiring the conversation default
      2. Curated models are edited on the provider → both members' rows follow
      3. The model override is edited → both owners' profiles follow
      4. The provider's own columns are the only copy: the managed credential's
         shadowed columns were never written, so there is nothing to go stale
    """
    first, first_headers = create_random_user_with_headers(client)
    second, second_headers = create_random_user_with_headers(client)
    created = create_provider_credential(
        db,
        name="Company Policy",
        target_user_ids=[first["id"], second["id"]],
        set_user_sdk_defaults=True,
        sdk_default_modes=["conversation"],
        model_override_conversation="model-one",
        default_model="model-one",
    )
    credential_id = created["record"]["id"]
    assert member_user_ids(created) == {first["id"], second["id"]}
    assert _me(client, first_headers)[
        "default_model_override_conversation"
    ] == "model-one"

    # ── Phase 2: curated models write through ──────────────────────────
    update_provider(
        db,
        created["provider_id"],
        default_model="model-two",
        available_models=["model-two", "model-three"],
    )
    for headers in (first_headers, second_headers):
        child = _credentials(client, headers)[0]
        assert child["default_model"] == "model-two", child
        assert child["available_models"] == ["model-two", "model-three"], child

    # ── Phase 3: the override writes through ───────────────────────────
    update_provider(
        db, created["provider_id"], model_override_conversation="model-two"
    )
    for headers in (first_headers, second_headers):
        assert _me(client, headers)[
            "default_model_override_conversation"
        ] == "model-two"

    # ── Phase 4: one copy, on the provider ─────────────────────────────
    record = get_managed_credential(
        client, superuser_token_headers, credential_id
    )
    assert record["default_model"] == "model-two"
    assert record["model_override_conversation"] == "model-two"
    provider = get_provider(db, created["provider_id"])
    assert provider["default_model"] == "model-two"
    assert provider["model_override_conversation"] == "model-two"


def test_clearing_a_providers_override_retracts_the_pins_it_wrote(
    client: TestClient, db: Session, superuser_token_headers: dict[str, str]
) -> None:
    """A clear that only blanked the provider would be cosmetic.

    The member would stay pinned to the dropped value, the provider would show
    blank, and no admin action could ever remove it — worse than not offering
    the clear at all. So the *transition* is carried down: the pin is retracted
    for the member who still carries the value this provider wrote, and left
    alone for the member who chose their own.
    """
    follower, follower_headers = create_random_user_with_headers(client)
    dissenter, dissenter_headers = create_random_user_with_headers(client)
    created = create_provider_credential(
        db,
        name="Company Clearing",
        target_user_ids=[follower["id"], dissenter["id"]],
        set_user_sdk_defaults=True,
        sdk_default_modes=["conversation"],
        model_override_conversation="admin-model",
    )
    assert _me(client, follower_headers)[
        "default_model_override_conversation"
    ] == "admin-model"

    # The dissenter picks their own model against the same credential.
    _patch_me(
        client,
        dissenter_headers,
        default_model_override_conversation="their-own-model",
    )

    # ``""`` is the clear — omitting the field would mean "leave it alone".
    update_provider(db, created["provider_id"], model_override_conversation="")

    assert (
        _me(client, follower_headers)["default_model_override_conversation"]
        is None
    ), "the pin this provider wrote survived the clear"
    assert (
        _me(client, dissenter_headers)["default_model_override_conversation"]
        == "their-own-model"
    ), "a member's own pick was retracted along with the admin's"


# ── Rule 4: the incumbent wins, except where an admin says otherwise ───


def test_automatic_provisioning_does_not_take_a_default_somebody_holds(
    client: TestClient, db: Session, superuser_token_headers: dict[str, str]
) -> None:
    """The grant happens; the default does not move.

    A person pastes their own key and picks it as their conversation default;
    an administrator later points a provider at their role. Both are normal, and
    the collision is resolved rather than prevented: they become a member and
    get the company credential, and the default they chose stays theirs. The
    fact is reported as a slot skip on the reconcile result — not as a skipped
    member, which would tell every reader the grant failed.
    """
    holder, holder_headers = create_random_user_with_headers(client)
    own = create_random_ai_credential(client, holder_headers)
    _patch_me(
        client,
        holder_headers,
        default_ai_credential_conversation_id=own["id"],
        default_model_override_conversation="a-model-they-picked",
    )

    created = create_provider_credential(
        db,
        name="Company Incumbent",
        auto_provision_roles=["agent-user"],
        set_user_sdk_defaults=True,
        sdk_default_modes=["conversation"],
        model_override_conversation="an-admin-model",
    )
    credential_id = created["record"]["id"]

    report = provision_account(db, get_user_row(db, holder["id"]))
    assert [str(entry.managed_credential_id) for entry in report.added] == [
        credential_id
    ], report

    record = get_managed_credential(
        client, superuser_token_headers, credential_id
    )
    assert holder["id"] in member_user_ids(record), "the grant did not happen"

    profile = _me(client, holder_headers)
    assert profile["default_ai_credential_conversation_id"] == own["id"]
    assert (
        profile["default_model_override_conversation"] == "a-model-they-picked"
    )


def test_automatic_provisioning_still_claims_an_empty_slot(
    client: TestClient, db: Session, superuser_token_headers: dict[str, str]
) -> None:
    """The control for the test above.

    Without this, "the default did not move" would pass just as happily against
    an implementation that stopped wiring defaults altogether.
    """
    arriving, arriving_headers = create_random_user_with_headers(client)
    assert (
        _me(client, arriving_headers)["default_ai_credential_conversation_id"]
        is None
    )

    created = create_provider_credential(
        db,
        name="Company Empty Slot",
        auto_provision_roles=["agent-user"],
        set_user_sdk_defaults=True,
        sdk_default_modes=["conversation"],
        model_override_conversation="an-admin-model",
    )
    provision_account(db, get_user_row(db, arriving["id"]))

    record = get_managed_credential(
        client, superuser_token_headers, created["record"]["id"]
    )
    member = member_for(record, arriving["id"])
    assert member is not None
    profile = _me(client, arriving_headers)
    assert (
        profile["default_ai_credential_conversation_id"]
        == member["child_credential_id"]
    )
    assert profile["default_sdk_conversation"] == ANTHROPIC_ENGINE
    assert profile["default_model_override_conversation"] == "an-admin-model"


def test_a_declined_slot_is_reported_as_a_slot_skip_not_a_skipped_member(
    client: TestClient, db: Session, superuser_token_headers: dict[str, str]
) -> None:
    """The grant succeeded; only the default did not move — and it says so.

    ``ManagedDefaultSlotSkip`` is a separate shape from ``ManagedReconcileSkip``
    for exactly this reason: folding the two together would have every reader of
    ``skipped`` report a successful grant as a failure.

    Asserted through ``add_members`` directly, because that is currently the only
    place the value can be seen: ``AccountProvisioningService`` takes the same
    incumbent-wins default but its ``ProvisioningReport`` has no field for the
    skips, and giving it one belongs to Phase 3 with the rest of that service's
    rewrite.
    """
    holder, holder_headers = create_random_user_with_headers(client)
    own = create_random_ai_credential(client, holder_headers)
    _patch_me(
        client,
        holder_headers,
        default_ai_credential_conversation_id=own["id"],
    )

    created = create_provider_credential(
        db,
        name="Company Slot Skip",
        set_user_sdk_defaults=True,
        sdk_default_modes=["conversation", "building"],
    )
    outcome = grant_members_automatically(
        db, created["record"]["id"], [holder["id"]]
    )

    assert [m["user_id"] for m in outcome["added"]] == [holder["id"]]
    assert outcome["skipped"] == [], (
        "A member who received the credential was reported as skipped."
    )
    assert outcome["default_slot_skips"] == [
        {
            "user_id": holder["id"],
            "mode": "conversation",
            "held_by_credential_id": own["id"],
        }
    ], outcome
    # ``building`` was empty, so it was claimed — the skip is per slot, not per
    # member, and a rule that gave up on the whole grant would show here.
    profile = _me(client, holder_headers)
    assert profile["default_ai_credential_conversation_id"] == own["id"]
    assert profile["default_ai_credential_building_id"] == (
        outcome["added"][0]["child_credential_id"]
    )


def test_apply_to_existing_is_the_escape_hatch_and_does_overwrite(
    client: TestClient, db: Session, superuser_token_headers: dict[str, str]
) -> None:
    """The deliberate admin act, contrasted with the automatic one above.

    "Grant this to everyone who already matches" is a decision somebody made on
    purpose, with a preview in front of them saying how many defaults it costs.
    That is why it overwrites where automatic provisioning declines.
    """
    holder, holder_headers = create_random_user_with_headers(client)
    own = create_random_ai_credential(client, holder_headers)
    _patch_me(
        client,
        holder_headers,
        default_ai_credential_conversation_id=own["id"],
        default_model_override_conversation="a-model-they-picked",
    )

    created = create_provider_credential(
        db,
        name="Company Escape Hatch",
        auto_provision_roles=["agent-user"],
        set_user_sdk_defaults=True,
        sdk_default_modes=["conversation"],
        model_override_conversation="an-admin-model",
    )

    preview = apply_provider_to_existing(
        db, created["provider_id"], dry_run=True
    )
    assert preview["dry_run"] is True
    assert preview["defaults_overwrite_count"] >= 1, preview

    apply_provider_to_existing(db, created["provider_id"])

    record = get_managed_credential(
        client, superuser_token_headers, created["record"]["id"]
    )
    member = member_for(record, holder["id"])
    assert member is not None
    profile = _me(client, holder_headers)
    assert (
        profile["default_ai_credential_conversation_id"]
        == member["child_credential_id"]
    )
    assert profile["default_model_override_conversation"] == "an-admin-model"


def test_a_provider_owned_credential_refuses_a_key_of_its_own(
    client: TestClient, db: Session, superuser_token_headers: dict[str, str]
) -> None:
    """The credential surface must not be able to take the key back.

    ``api_key`` is not one of the shadowed *policy* columns — it is the key —
    but on a provider-owned record it is refused for a sharper reason than
    those are. Accepting it writes ``managed_ai_credential.encrypted_data``,
    and the next ``rotate_key`` would then re-key every member out of that
    stale copy while reporting that the rotation succeeded: the "believing they
    rolled a key they did not" failure, reached through the other door.

    Two halves, and the second is the one that would have caught it: the write
    is refused, **and** a rotation afterwards actually reaches the members.
    """
    user, headers = create_random_user_with_headers(client)
    created = create_provider_credential(
        db,
        name="Company Keyed",
        secret="sk-ant-provider-owns-this",
        target_user_ids=[user["id"]],
    )
    credential_id = created["record"]["id"]

    refused = client.patch(
        f"{API}/admin/llm-providers/{credential_id}",
        headers=superuser_token_headers,
        json={"api_key": "sk-ant-smuggled-in"},
    )
    assert refused.status_code == 400, refused.text
    assert "Company Keyed" in refused.json()["detail"]

    # And the provider is still the only source of truth, so a rotation writes
    # through rather than re-applying whatever the record kept.
    rotated = rotate_provider_key(
        db, created["provider_id"], "sk-ant-actually-rotated"
    )
    assert [m["user_id"] for m in rotated["result"]["updated"]] == [user["id"]]


def test_a_manual_credential_cannot_be_created_against_a_provider(
    client: TestClient, db: Session, superuser_token_headers: dict[str, str]
) -> None:
    """§5.2's refusal, now structural rather than a service branch.

    Two shapes the split exists to outlaw, both of which used to be 400s from
    ``_validate_provisioning_shape``:

      1. A credential that holds its own key *and* points at a provider: the
         policy resolver would report the provider's mode while the row carried
         a key, which for a minted provider is the shape the Phase 1 migration
         aborts on — created through a live route.
      2. A *second* credential on a provider that already owns one. Not an error
         the admin would ever see: ``owned_credential`` resolves the older row,
         so the new record is invisible to rotation, apply-to-existing and the
         delete gate while still handing its members keys — and creating it would
         rewrite the provider's wiring under the members it already has.

    Phase 4 removed ``provider_admin_credential_id`` from
    ``ManagedAICredentialCreate`` outright, so neither shape is expressible any
    more. **The refusal is asserted rather than assumed to follow**, because
    removing a field from a Pydantic model does not refuse it — it ignores it,
    and an ignored pointer would create a *manual* credential under a name the
    admin believed was attached to the provider. ``extra="forbid"`` is what makes
    it a 422 that names the field; this is the test that says so from the wire.
    """
    created = create_provider_credential(db, name="Company Owner")
    provider_id = created["provider_id"]

    shared_against_provider = client.post(
        f"{API}/admin/llm-providers/",
        headers=superuser_token_headers,
        json={
            "name": "Smuggled manual",
            "type": "anthropic",
            "api_key": "sk-ant-manual",
            "provider_admin_credential_id": provider_id,
            "target_user_ids": [],
        },
    )
    assert shared_against_provider.status_code == 422, (
        shared_against_provider.text
    )
    assert any(
        "provider_admin_credential_id" in str(error.get("loc", ""))
        for error in shared_against_provider.json()["detail"]
    ), shared_against_provider.text

    second_on_same_provider = client.post(
        f"{API}/admin/llm-providers/",
        headers=superuser_token_headers,
        json={
            "name": "Second credential",
            "type": "openai",
            "provisioning_mode": "minted",
            "provider_admin_credential_id": provider_id,
            "target_user_ids": [],
        },
    )
    assert second_on_same_provider.status_code == 422, (
        second_on_same_provider.text
    )

    # The provider still owns exactly the one credential it was created with —
    # a refusal that left a stray row behind would be no refusal at all.
    assert get_provider(db, provider_id)["owned_credential_id"] == (
        created["record"]["id"]
    )


def test_the_key_shape_of_a_provider_owned_credential_is_refused_too(
    client: TestClient, db: Session, superuser_token_headers: dict[str, str]
) -> None:
    """``base_url`` and ``model`` are the provider's, and the failure is silent.

    They are not in §3.2's shadowed set — that list is the wiring *policy* — but
    for a ``fixed_key`` provider they live in the provider's encrypted envelope,
    which is what ``_add_child`` builds each new member's credential out of. So
    accepting an edit here wrote through to the members who already existed and
    was reverted for every member added afterwards, and by the next
    ``rotate_key``: two members of one credential holding different base URLs,
    with nothing reporting the divergence.

    That is why the refusal is asserted rather than the write-through being
    "good enough". The control is that the provider's own PATCH does reach both
    — pinned by ``test_editing_a_providers_base_url_reaches_members_added_afterwards``
    below — so this refuses the wrong door, not the act.
    """
    created = create_provider_credential(
        db,
        name="Company Compat",
        credential_type="openai_compatible",
        secret="sk-compat-key",
        base_url="https://one.example.com/v1",
        model="some-model",
    )
    credential_id = created["record"]["id"]

    for field, value in (
        ("base_url", "https://smuggled.example.com/v1"),
        ("model", "smuggled-model"),
    ):
        refused = client.patch(
            f"{API}/admin/llm-providers/{credential_id}",
            headers=superuser_token_headers,
            json={field: value},
        )
        assert refused.status_code == 400, (field, refused.text)
        detail = refused.json()["detail"]
        assert field in detail and "Company Compat" in detail, detail

    # Nothing was written by either attempt.
    record = client.get(
        f"{API}/admin/llm-providers/{credential_id}",
        headers=superuser_token_headers,
    ).json()
    assert record["base_url"] == "https://one.example.com/v1"
    assert record["model"] == "some-model"


def test_editing_a_providers_base_url_reaches_members_added_afterwards(
    client: TestClient, db: Session, superuser_token_headers: dict[str, str]
) -> None:
    """The stored key envelope carries the shape, so an edit has to re-encrypt it.

    ``_add_child`` builds a new member's credential straight out of the
    provider's envelope, while the write-through to existing members diffs the
    managed credential's columns. If the edit updated only the latter, members
    added *before* it would hold the new base URL and members added *after* it
    the old one — a divergence nothing reports.
    """
    early, early_headers = create_random_user_with_headers(client)
    late, late_headers = create_random_user_with_headers(client)
    created = create_provider_credential(
        db,
        name="Company Compatible",
        credential_type="openai_compatible",
        secret="sk-compat-key",
        base_url="https://one.example.com/v1",
        model="some-model",
        target_user_ids=[early["id"]],
    )

    update_provider(
        db, created["provider_id"], base_url="https://two.example.com/v1"
    )

    added = client.patch(
        f"{API}/admin/llm-providers/{created['record']['id']}",
        headers=superuser_token_headers,
        json={"target_user_ids": [early["id"], late["id"]]},
    )
    assert added.status_code == 200, added.text

    for headers in (early_headers, late_headers):
        child = _credentials(client, headers)[0]
        assert child["base_url"] == "https://two.example.com/v1", child


# ── The minted escape hatch ────────────────────────────────────────────


def test_the_escape_hatch_overwrites_for_a_minted_provider_too(
    client: TestClient, db: Session, superuser_token_headers: dict[str, str]
) -> None:
    """The intent has to survive the gap between the grant and the key.

    A minted member's default wiring does not happen during the grant — the key
    is created later, by a converge pass that has no idea whether a superuser
    pressed "apply to existing users" or an account simply signed up. Deciding
    it there is wrong either way: always claiming lets automatic provisioning
    steal a default, never claiming silently breaks both escape hatches for
    exactly the provider kind per-user spend tracking uses. So the grant records
    the answer on the membership row and the converge pass reads it back.

    Both directions, in one test, because either alone passes against an
    implementation that hardcodes the other.
    """
    hatched, hatched_headers = create_random_user_with_headers(client)
    hatched_own = create_random_ai_credential(client, hatched_headers)["id"]
    _patch_me(
        client,
        hatched_headers,
        default_ai_credential_conversation_id=hatched_own,
    )

    created = create_provider_credential(
        db,
        name="Company Minted Hatch",
        kind="minted",
        credential_type="openai",
        auto_provision_roles=["agent-user"],
        set_user_sdk_defaults=True,
        sdk_default_modes=["conversation"],
    )

    # The deliberate act, over the population that exists right now.
    apply_provider_to_existing(db, created["provider_id"])

    # The automatic one arrives afterwards, so apply-to-existing cannot have
    # claimed them — the two grants stay cleanly separated.
    automatic, automatic_headers = create_random_user_with_headers(client)
    automatic_own = create_random_ai_credential(client, automatic_headers)["id"]
    _patch_me(
        client,
        automatic_headers,
        default_ai_credential_conversation_id=automatic_own,
    )
    provision_account(db, get_user_row(db, automatic["id"]))

    with stub_minting_providers():
        converge_keys(db)

    record = get_managed_credential(
        client, superuser_token_headers, created["record"]["id"]
    )
    hatched_member = member_for(record, hatched["id"])
    assert hatched_member is not None
    assert hatched_member["child_credential_id"] is not None, hatched_member

    assert _me(client, hatched_headers)[
        "default_ai_credential_conversation_id"
    ] == hatched_member["child_credential_id"], (
        "apply-to-existing is one of the two acts that overwrite, and for a "
        "minted provider it did not."
    )
    automatic_member = member_for(record, automatic["id"])
    assert automatic_member is not None
    assert automatic_member["child_credential_id"] is not None, (
        "The automatic grant did not produce a key, so the assertion below "
        "would pass for the wrong reason."
    )
    assert _me(client, automatic_headers)[
        "default_ai_credential_conversation_id"
    ] == automatic_own, (
        "Automatic provisioning took a default somebody already held."
    )
    assert hatched_own != hatched_member["child_credential_id"]


# ── The delete gate ────────────────────────────────────────────────────


def test_deleting_a_provider_with_members_is_refused_then_forced(
    client: TestClient, db: Session, superuser_token_headers: dict[str, str]
) -> None:
    """A refusal that names the people, then an ordered force.

      1. A ``fixed_key`` provider with one member
      2. Deleting it is refused, and the refusal names who loses a key
      3. Forcing it removes all four rows — the child credential, the
         membership, the managed credential and the provider

    End state only. Nothing here observes the *order* those rows go in; the
    backstop under it is the RESTRICT constraint, asserted directly by
    ``tests/migrations/ai_provider_split_test.py::
    test_a_provider_cannot_be_deleted_out_from_under_its_credential``.
    """
    user, headers = create_random_user_with_headers(client)
    created = create_provider_credential(
        db, name="Company Doomed", target_user_ids=[user["id"]]
    )
    credential_id = created["record"]["id"]
    assert len(_credentials(client, headers)) == 1

    with pytest.raises(AIProviderInUseError) as blocked:
        delete_provider(db, created["provider_id"])
    impact = blocked.value.impact
    assert impact.member_count == 1
    assert [m.user_id for m in impact.members] == [
        __import__("uuid").UUID(user["id"])
    ]
    assert impact.members[0].email == user["email"]
    # A fixed key is a copy, not a key minted at the vendor, so nothing is
    # revoked there.
    assert impact.minted_key_count == 0

    forced = delete_provider(db, created["provider_id"], force=True)
    assert forced["member_count"] == 1

    assert _credentials(client, headers) == []
    gone = client.get(
        f"{API}/admin/llm-providers/{credential_id}",
        headers=superuser_token_headers,
    )
    assert gone.status_code == 404, gone.text


def test_a_provider_nobody_holds_deletes_without_a_confirmation(
    client: TestClient, db: Session, superuser_token_headers: dict[str, str]
) -> None:
    """There is nobody to warn, so there is nothing to confirm."""
    created = create_provider_credential(db, name="Company Unused")
    credential_id = created["record"]["id"]

    impact = delete_provider(db, created["provider_id"])
    assert impact["member_count"] == 0

    gone = client.get(
        f"{API}/admin/llm-providers/{credential_id}",
        headers=superuser_token_headers,
    )
    assert gone.status_code == 404, gone.text


def test_a_provider_with_no_credential_still_projects_and_still_deletes(
    client: TestClient, db: Session, superuser_token_headers: dict[str, str]
) -> None:
    """A lone provider row is describable, and has to be.

    Such a row was written by ``POST /admin/provider-admin-credentials``, which
    Phase 4 deleted; migration ``c23d6b59a8f5`` gave every pre-existing row a
    credential and ``AIProvidersService.create`` always writes the pair, so no
    live route creates one any more. Rows that predate the deletion still exist,
    and a projection that raised for one would take the whole admin listing down
    with it — so the state is still reproduced here, through a **test seam**
    rather than a route. Deleting the credential and leaving the provider
    standing was the last route into it, and it is now refused (see the test
    below): a provider with no credential auto-provisions to nobody, silently,
    while the admin surface still shows the rule as active.

    Nothing holds a key from it, so its impact is genuinely empty and it deletes
    without a confirmation.
    """
    created = create_provider_credential(
        db, name="Lone Org", kind="minted", credential_type="openai"
    )
    provider_id = created["provider_id"]
    credential_id = created["record"]["id"]

    orphan_provider(db, credential_id)

    projected = get_provider(db, provider_id)
    assert projected["owned_credential_id"] is None
    assert projected["member_count"] == 0
    assert projected["key_state_summary"] == {}
    assert projected["has_secret"] is True

    impact = delete_provider(db, provider_id)
    assert impact["member_count"] == 0
    assert impact["owned_credential_id"] is None

    gone = client.get(
        f"{API}/admin/ai-providers/{provider_id}",
        headers=superuser_token_headers,
    )
    assert gone.status_code == 404, gone.text


def test_a_provider_owned_credential_cannot_be_deleted_out_from_under_it(
    client: TestClient, db: Session, superuser_token_headers: dict[str, str]
) -> None:
    """400 naming the provider — because the alternative fails *silently*.

    ``ON DELETE RESTRICT`` protects one direction: the provider cannot go while
    its credential stands. Nothing protected the other, and the resulting state
    is worse than either delete being refused. A provider with no credential
    keeps its ``auto_provision_roles``, still lists as active on
    ``/admin/ai-providers``, and is dropped by ``auto_provision_targets`` with no
    skip, no log and no security event — so every subsequent signup for those
    roles quietly gets nothing, and there is no record anywhere that it did.

    The control matters: the provider's own delete still works and takes the
    credential with it, so this is a refusal of the *wrong door*, not of the act.
    """
    created = create_provider_credential(db, name="Company Undeletable")
    credential_id = created["record"]["id"]
    provider_id = created["provider_id"]

    refused = client.delete(
        f"{API}/admin/llm-providers/{credential_id}",
        headers=superuser_token_headers,
    )
    assert refused.status_code == 400, refused.text
    assert "Company Undeletable" in refused.json()["detail"], refused.text

    # Nothing was removed, and the provider still owns exactly what it did.
    assert get_provider(db, provider_id)["owned_credential_id"] == credential_id
    assert (
        client.get(
            f"{API}/admin/llm-providers/{credential_id}",
            headers=superuser_token_headers,
        ).status_code
        == 200
    )

    # The right door: deleting the provider removes the credential with it.
    delete_provider(db, provider_id)
    assert (
        client.get(
            f"{API}/admin/llm-providers/{credential_id}",
            headers=superuser_token_headers,
        ).status_code
        == 404
    )

# ── The shadowed-field refusal ─────────────────────────────────────────


def test_a_provider_owned_credential_refuses_edits_to_its_wiring_policy(
    client: TestClient, db: Session, superuser_token_headers: dict[str, str]
) -> None:
    """400, naming the provider — not a save that changes nothing.

    Those columns are not read for a provider-owned record, so accepting the
    write would be the exact "saved successfully, nothing happened" failure the
    provider/credential split exists to remove. Membership and the record's name
    are still editable here; the policy is edited on the provider.
    """
    created = create_provider_credential(db, name="Company Shadowed")
    credential_id = created["record"]["id"]

    refused = client.patch(
        f"{API}/admin/llm-providers/{credential_id}",
        headers=superuser_token_headers,
        json={"set_as_default": True, "default_model": "something"},
    )
    assert refused.status_code == 400, refused.text
    detail = refused.json()["detail"]
    assert "Company Shadowed" in detail, detail
    # Every offending field, not just the first: an admin who sent three should
    # not discover them one round trip at a time.
    assert "set_as_default" in detail and "default_model" in detail, detail

    allowed = client.patch(
        f"{API}/admin/llm-providers/{credential_id}",
        headers=superuser_token_headers,
        json={"name": "Renamed by hand"},
    )
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["record"]["name"] == "Renamed by hand"
