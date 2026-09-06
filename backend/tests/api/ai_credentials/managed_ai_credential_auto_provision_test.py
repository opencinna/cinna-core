"""Configuring auto-provisioning: the slot conflict, the backfill, the overrides.

The admin side of zero-touch onboarding phase 2. Three things live on
``ManagedAICredential`` now that did not before, and each carries a rule that
is not obvious from the field name:

**``auto_provision_roles`` + the ``(role, mode)`` uniqueness rule.**
``User.default_ai_credential_<mode>_id`` holds exactly one credential. Two
managed records that both auto-provision to the same role *and* both wire that
role's SDK default for the same mode would fight over it — resolved silently at
grant time, per user, in whatever order the rows came back. So the second
configuration is refused at write time with a 409 that names the other record,
and the frontend highlights the offending cell from the structured body rather
than parsing a sentence.

**"Apply to existing users".** Auto-provisioning fires at account *creation*.
Turning the flag on does nothing for the people already on the instance, and
re-running provisioning on a role change was rejected as a design (a promotion
should not hand out a company key as a side effect). This action closes that
gap explicitly, with a dry run so the admin sees the cost before paying it —
including ``defaults_overwrite_count``, because "12 users will receive this
credential" is only half the story when 9 of them lose the default they chose.
That number has to count **both** axes a grant writes, because
``set_as_default`` and ``set_user_sdk_defaults`` are independent flags applied
independently, and a preview built around one of them understates the other to
zero.

**Per-mode model overrides, and the asymmetry between claiming and holding.**
The spec had one rule; the implementation has two, because the single rule
destroyed user data. Claiming a slot (a member is added, the pointer moves onto
this credential) *resets* the override — whatever was there described a
different credential and may name a model this provider does not serve. Holding
a slot (any later edit of the record) only pushes an *actual opinion* — a
``None`` on the parent means "no opinion", not "clear theirs", or an admin
renaming a record would silently erase the model every member had picked.

Clearing an override is its own case again: an omitted field means "leave it
alone", a submitted ``""`` retracts it — and retracting it unpins the members
who still carry the value being dropped, so the clear is not merely cosmetic.

One deliberate backend gap is pinned here as behaviour rather than tested as a
bug: the uniqueness rule does not consider ``set_as_default``. That is about
the *409*, and is not the same thing as the preview counting that flag — two
records may still both claim it, and the admin is simply told what the second
one costs.

The account-creation side — which origins get provisioned, and what happens
when provisioning fails — is in ``tests/api/users/users_auto_provision_*``.
"""
from __future__ import annotations

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.core.config import settings
from tests.utils.ai_credential import (
    create_random_ai_credential,
    list_ai_credentials,
)
from tests.utils.account_provisioning import (
    CHILD_CREDENTIAL_INSERT,
    failing_sql_statement,
    session_is_usable,
)
from tests.utils.managed_ai_credential import (
    ADMIN_BASE,
    apply_to_existing,
    create_managed_credential,
    get_managed_credential,
    list_managed_credentials,
    member_for,
    member_user_ids,
    update_managed_credential,
)
from tests.utils.user import (
    create_random_user_with_headers,
    promote_to_developer,
)
from tests.utils.utils import random_lower_string

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


def _create_conflicting(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    **kwargs,
) -> dict:
    """POST expecting the 409 envelope; returns the ``detail`` object."""
    body = create_managed_credential(
        client, superuser_token_headers, expected_status=409, **kwargs
    )
    return body["detail"]


# ── Scenario 1: the (role, mode) slot may have only one owner ──────────


def test_two_records_cannot_claim_the_same_role_and_mode_default(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    The uniqueness rule, from the admin's side:
      1. "Company OpenAI" auto-provisions to developers and wires both modes
      2. "Company Claude" tries to wire ``building`` for developers → 409,
         with a structured body naming the other record and the exact cell
      3. Nothing was written — a refused create must not leave a
         half-configured parent behind for the admin to clean up
      4. The same record for a *different role* is accepted
      5. The same role and mode, but not wiring SDK defaults, is accepted —
         granting a credential without touching a default is a supported
         configuration, not an oversight
      6. The same role, a mode the first record does not claim, is accepted
    """
    # ── Phase 1 ────────────────────────────────────────────────────────
    first = create_managed_credential(
        client,
        superuser_token_headers,
        name="Company OpenAI",
        auto_provision_roles=["agent-developer"],
        set_user_sdk_defaults=True,
        sdk_default_modes=["conversation", "building"],
    )["record"]

    # ── Phase 2: the collision ─────────────────────────────────────────
    detail = _create_conflicting(
        client,
        superuser_token_headers,
        name="Company Claude",
        auto_provision_roles=["agent-developer"],
        set_user_sdk_defaults=True,
        sdk_default_modes=["building"],
    )
    # The exact key set, not just the ones this test reads: the dialog
    # highlights the offending role/mode cell and links to the other record,
    # and it can do neither from a prose string. A field quietly dropped from
    # the envelope is a frontend regression with no backend symptom.
    assert set(detail) == {
        "code",
        "message",
        "conflicting_credential_id",
        "conflicting_credential_name",
        "role",
        "mode",
    }, detail
    assert detail["code"] == "auto_provision_conflict"
    assert detail["conflicting_credential_id"] == first["id"]
    assert detail["conflicting_credential_name"] == "Company OpenAI"
    assert detail["role"] == "agent-developer"
    assert detail["mode"] == "building"
    # The message names the other record, so the dialog has something to say
    # even before it reads the structured fields.
    assert "Company OpenAI" in detail["message"]

    # ── Phase 3: nothing half-written ──────────────────────────────────
    names = {record["name"] for record in list_managed_credentials(
        client, superuser_token_headers
    )}
    assert names == {"Company OpenAI"}, names

    # ── Phase 4: a different role does not collide ─────────────────────
    other_role = create_managed_credential(
        client,
        superuser_token_headers,
        name="Company Claude (admins)",
        auto_provision_roles=["admin"],
        set_user_sdk_defaults=True,
        sdk_default_modes=["building"],
    )["record"]
    assert other_role["auto_provision_roles"] == ["admin"]

    # ── Phase 5: same cell, but no SDK defaults → allowed ──────────────
    no_defaults = create_managed_credential(
        client,
        superuser_token_headers,
        name="Company Claude (grant only)",
        auto_provision_roles=["agent-developer"],
        set_user_sdk_defaults=False,
        sdk_default_modes=["building"],
    )["record"]
    assert no_defaults["set_user_sdk_defaults"] is False
    assert no_defaults["auto_provision_roles"] == ["agent-developer"]

    # ── Phase 6: an unclaimed mode → allowed ───────────────────────────
    narrowed = update_managed_credential(
        client,
        superuser_token_headers,
        first["id"],
        sdk_default_modes=["conversation"],
    )["record"]
    assert narrowed["sdk_default_modes"] == ["conversation"]

    freed = create_managed_credential(
        client,
        superuser_token_headers,
        name="Company Claude (building)",
        auto_provision_roles=["agent-developer"],
        set_user_sdk_defaults=True,
        sdk_default_modes=["building"],
    )["record"]
    assert freed["sdk_default_modes"] == ["building"]


def test_an_update_that_would_introduce_a_conflict_is_refused_and_applies_nothing(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    The same rule on PATCH, where "applies nothing" is the harder half:
      1. Two records coexist because they cover different roles
      2. Widening the second one's roles onto the first's would collide → 409
      3. The second record is completely unchanged — roles *and* the name that
         rode along in the same request. The validation runs against the
         *effective* values before anything is written, so a PATCH cannot
         half-apply
      4. A PATCH that touches only the name is not refused for a conflict it
         did not introduce
      5. Nor is one that renames *and* resubmits the record's own
         auto-provision fields unchanged — the rule is scoped to the slots a
         request newly claims, not to the state it happens to describe
      6. The same widening with SDK defaults turned off is accepted
    """
    # ── Phase 1 ────────────────────────────────────────────────────────
    incumbent = create_managed_credential(
        client,
        superuser_token_headers,
        name="Incumbent",
        auto_provision_roles=["agent-user"],
        set_user_sdk_defaults=True,
        sdk_default_modes=["conversation"],
    )["record"]
    challenger = create_managed_credential(
        client,
        superuser_token_headers,
        name="Challenger",
        auto_provision_roles=["agent-developer"],
        set_user_sdk_defaults=True,
        sdk_default_modes=["conversation"],
    )["record"]

    # ── Phase 2 ────────────────────────────────────────────────────────
    body = update_managed_credential(
        client,
        superuser_token_headers,
        challenger["id"],
        expected_status=409,
        name="Challenger renamed",
        auto_provision_roles=["agent-user", "agent-developer"],
    )
    detail = body["detail"]
    assert set(detail) == {
        "code",
        "message",
        "conflicting_credential_id",
        "conflicting_credential_name",
        "role",
        "mode",
    }, detail
    assert detail["code"] == "auto_provision_conflict"
    assert detail["conflicting_credential_id"] == incumbent["id"]
    assert detail["conflicting_credential_name"] == "Incumbent"
    assert detail["role"] == "agent-user"
    assert detail["mode"] == "conversation"

    # ── Phase 3: not even the name landed ──────────────────────────────
    unchanged = get_managed_credential(
        client, superuser_token_headers, challenger["id"]
    )
    assert unchanged["name"] == "Challenger"
    assert unchanged["auto_provision_roles"] == ["agent-developer"]

    # ── Phase 4: an unrelated edit still goes through ──────────────────
    renamed = update_managed_credential(
        client, superuser_token_headers, challenger["id"], name="Challenger v2"
    )["record"]
    assert renamed["name"] == "Challenger v2"
    assert renamed["auto_provision_roles"] == ["agent-developer"]

    # ── Phase 5: a stale absolute payload claims nothing new ───────────
    # What the dialog used to send on a rename: every auto-provision field,
    # rebuilt from the snapshot taken when it opened. Nothing here is a new
    # claim, so nothing here may be refused. The dialog now sends only what
    # the admin touched; this is the backstop under that, and the reason the
    # validator compares transitions rather than end states.
    resubmitted = update_managed_credential(
        client,
        superuser_token_headers,
        challenger["id"],
        name="Challenger v3",
        set_user_sdk_defaults=True,
        sdk_default_modes=["conversation"],
        auto_provision_roles=["agent-developer"],
    )["record"]
    assert resubmitted["name"] == "Challenger v3"
    assert resubmitted["auto_provision_roles"] == ["agent-developer"]

    # ── Phase 6: no SDK defaults, no fight ─────────────────────────────
    widened = update_managed_credential(
        client,
        superuser_token_headers,
        challenger["id"],
        set_user_sdk_defaults=False,
        auto_provision_roles=["agent-user", "agent-developer"],
    )["record"]
    assert widened["set_user_sdk_defaults"] is False
    assert set(widened["auto_provision_roles"]) == {"agent-user", "agent-developer"}


def test_an_unknown_role_in_auto_provision_roles_is_rejected(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """A mistyped role is a 400, not a silent drop.

    Silently dropping it would let an admin save successfully and then watch
    nothing happen at the next signup, with no clue why. Duplicates and
    whitespace, on the other hand, are canonicalised rather than refused —
    those are not mistakes worth an error.
    """
    body = create_managed_credential(
        client,
        superuser_token_headers,
        name="Typo",
        auto_provision_roles=["agent-develper"],
        expected_status=400,
    )
    assert "agent-develper" in body["detail"]

    canonicalised = create_managed_credential(
        client,
        superuser_token_headers,
        name="Tidy",
        auto_provision_roles=[" agent-user ", "agent-user", ""],
    )["record"]
    assert canonicalised["auto_provision_roles"] == ["agent-user"]


# ── Scenario 2: known gap — set_as_default is outside the rule ─────────


def test_set_as_default_is_not_covered_by_the_uniqueness_rule(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Pinned as known behaviour, not asserted as correct.

    The uniqueness rule guards ``default_ai_credential_<mode>_id`` only. Two
    auto-provisioning records that both carry ``set_as_default`` — the child
    credential's own "is default for its type" flag — are accepted, and a new
    account covered by both ends up with whichever was provisioned second.

    Recorded here so the gap is a decision with a test next to it rather than
    something a reader has to infer from the absence of one. If ``set_as_default``
    is ever brought under the rule, this test is the one that fails and the one
    to rewrite.
    """
    create_managed_credential(
        client,
        superuser_token_headers,
        name="Default A",
        auto_provision_roles=["agent-user"],
        set_as_default=True,
        set_user_sdk_defaults=False,
    )
    accepted = create_managed_credential(
        client,
        superuser_token_headers,
        name="Default B",
        auto_provision_roles=["agent-user"],
        set_as_default=True,
        set_user_sdk_defaults=False,
    )["record"]
    assert accepted["set_as_default"] is True
    assert accepted["auto_provision_roles"] == ["agent-user"]


# ── Scenario 3: apply to existing users ────────────────────────────────


def test_apply_to_existing_previews_then_backfills_then_becomes_a_no_op(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Bringing the people who were already here into a new company credential:
      1. Three ``agent-user`` accounts and one ``agent-developer`` exist
      2. One of them has already chosen their own conversation default
      3. The admin creates the record — nobody is provisioned, because
         auto-provisioning is creation-time only
      4. Dry run: three candidates, named; one of them would have a default
         replaced; nothing written
      5. Real run: those three become members, and ``candidates`` is empty
         because they are ``added`` now
      6. The developer account, whose role the record does not cover, is not
         a member
      7. Running it again adds nobody — idempotent
      8. The user who had chosen their own default now points at the child
    """
    # ── Phase 1 ────────────────────────────────────────────────────────
    users = [create_random_user_with_headers(client) for _ in range(3)]
    developer, developer_headers = create_random_user_with_headers(client)
    promote_to_developer(client, superuser_token_headers, developer["id"])

    # ── Phase 2: one of them already has a conversation default ────────
    opinionated, opinionated_headers = users[0]
    own_credential = create_random_ai_credential(client, opinionated_headers)
    _patch_me(
        client,
        opinionated_headers,
        default_ai_credential_conversation_id=own_credential["id"],
        default_model_override_conversation="a-model-they-picked",
    )

    # ── Phase 3: the record is created; nothing happens to anyone ──────
    parent = create_managed_credential(
        client,
        superuser_token_headers,
        name="Company Backfill",
        auto_provision_roles=["agent-user"],
        set_user_sdk_defaults=True,
        sdk_default_modes=["conversation"],
        model_override_conversation="an-admin-model",
    )["record"]
    assert parent["members"] == []

    # ── Phase 4: dry run ───────────────────────────────────────────────
    preview = apply_to_existing(
        client, superuser_token_headers, parent["id"], dry_run=True
    )
    assert preview["dry_run"] is True
    assert preview["added"] == []
    preview_ids = {candidate["user_id"] for candidate in preview["candidates"]}
    expected_ids = {user["id"] for user, _ in users}
    assert expected_ids <= preview_ids
    assert developer["id"] not in preview_ids
    assert preview["candidate_count"] == len(preview["candidates"])
    assert preview["defaults_overwrite_count"] >= 1, preview
    # A question writes nothing.
    assert get_managed_credential(
        client, superuser_token_headers, parent["id"]
    )["members"] == []

    # ── Phase 5: the real run ──────────────────────────────────────────
    applied = apply_to_existing(client, superuser_token_headers, parent["id"])
    assert applied["dry_run"] is False
    assert applied["candidates"] == []
    added_ids = {member["user_id"] for member in applied["added"]}
    assert expected_ids <= added_ids
    assert applied["candidate_count"] == preview["candidate_count"]

    record = get_managed_credential(client, superuser_token_headers, parent["id"])
    assert expected_ids <= member_user_ids(record)

    # ── Phase 6: an uncovered role is left alone ───────────────────────
    assert developer["id"] not in member_user_ids(record)
    assert _me(client, developer_headers)[
        "default_ai_credential_conversation_id"
    ] is None

    # ── Phase 7: idempotent ────────────────────────────────────────────
    again = apply_to_existing(client, superuser_token_headers, parent["id"])
    assert again["added"] == []
    assert again["candidate_count"] == 0
    assert again["defaults_overwrite_count"] == 0
    assert member_user_ids(
        get_managed_credential(client, superuser_token_headers, parent["id"])
    ) == member_user_ids(record)

    # ── Phase 8: the replaced default really was replaced ──────────────
    child_id = member_for(record, opinionated["id"])["child_credential_id"]
    profile = _me(client, opinionated_headers)
    assert profile["default_ai_credential_conversation_id"] == child_id
    assert profile["default_sdk_conversation"] == ANTHROPIC_ENGINE
    assert profile["default_model_override_conversation"] == "an-admin-model"


def test_the_preview_counts_the_default_credential_a_grant_would_demote(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """``set_as_default`` costs a candidate their own default, and must say so.

    ``set_as_default`` and ``set_user_sdk_defaults`` are **independent** flags,
    and ``_add_child`` applies them independently: the first calls
    ``ai_credentials_service.set_default``, which unsets whatever the owner had
    marked default for this type and rewrites their legacy profile blob. A
    counter that looked only at the SDK-defaults axis therefore answered
    ``defaults_overwrite_count = 0`` for a record that was about to demote
    every candidate's own key — and the confirm dialog, reading the same single
    flag, said nothing about defaults at all. The admin confirmed a destructive
    action that had been previewed as free.

    Measured as a **delta**, not an absolute: the candidate set is every active
    account with the role, and this test does not own the instance.

      1. An ``agent-user`` account that holds nothing yet
      2. A record that only ``set_as_default`` — no SDK defaults anywhere
      3. Preview it and remember both numbers
      4. That account chooses its own default of the record's type
      5. The candidate set is unchanged; the overwrite count is one higher
      6. A twin record *without* the flag counts the same person at zero, so
         the number is measuring this axis and not the population
      7. The real run does what the preview promised

    The account is created **before** the record on purpose: an account
    created after it would be auto-provisioned on the spot and stop being a
    candidate at all.
    """
    # ── Phase 1 + 2 ────────────────────────────────────────────────────
    opinionated, opinionated_headers = create_random_user_with_headers(client)
    parent = create_managed_credential(
        client,
        superuser_token_headers,
        name="Company Type Default",
        auto_provision_roles=["agent-user"],
        set_as_default=True,
        set_user_sdk_defaults=False,
    )["record"]

    # ── Phase 3 ────────────────────────────────────────────────────────
    before = apply_to_existing(
        client, superuser_token_headers, parent["id"], dry_run=True
    )

    # ── Phase 4 ────────────────────────────────────────────────────────
    own_credential = create_random_ai_credential(
        client, opinionated_headers, set_default=True
    )

    # ── Phase 5 ────────────────────────────────────────────────────────
    after = apply_to_existing(
        client, superuser_token_headers, parent["id"], dry_run=True
    )
    assert after["candidate_count"] == before["candidate_count"], (
        "Nobody joined or left; only one candidate's credentials changed."
    )
    assert (
        after["defaults_overwrite_count"]
        == before["defaults_overwrite_count"] + 1
    ), (
        "The preview did not count the default this grant takes away. "
        f"before={before['defaults_overwrite_count']} "
        f"after={after['defaults_overwrite_count']}"
    )

    # ── Phase 6: the control ───────────────────────────────────────────
    # Same candidates, same type, only the flag differs. Two records may both
    # carry ``set_as_default`` for one role — that gap is pinned in scenario 2
    # — so this second record is accepted.
    passive = create_managed_credential(
        client,
        superuser_token_headers,
        name="Company Passive",
        auto_provision_roles=["agent-user"],
        set_as_default=False,
        set_user_sdk_defaults=False,
    )["record"]
    passive_preview = apply_to_existing(
        client, superuser_token_headers, passive["id"], dry_run=True
    )
    assert passive_preview["candidate_count"] == after["candidate_count"]
    assert passive_preview["defaults_overwrite_count"] == 0, (
        "A record that writes no defaults cannot cost anyone theirs."
    )

    # ── Phase 7: the real run ──────────────────────────────────────────
    apply_to_existing(client, superuser_token_headers, parent["id"])
    record = get_managed_credential(
        client, superuser_token_headers, parent["id"]
    )
    member = member_for(record, opinionated["id"])
    assert member is not None
    assert member["is_default"] is True

    theirs = {
        credential["id"]: credential
        for credential in list_ai_credentials(client, opinionated_headers)[
            "data"
        ]
    }
    assert theirs[own_credential["id"]]["is_default"] is False, (
        "Their own key kept the default flag — the two credentials disagree "
        "about who is default."
    )


def test_the_preview_counts_a_model_override_that_no_pointer_guards(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """A chosen model with no credential behind it is still a thing to lose.

    ``_apply_sdk_defaults`` writes the parent's override for every mode it
    claims **unconditionally**, ``None`` included, because the slot is moving
    onto a different credential and the value sitting in it described the old
    one (its docstring argues the case). So a user who never pointed a
    credential at ``conversation`` but did pick a model for it loses that pick
    — and the first version of the counter, which asked only whether the
    *pointer* was set, walked straight past them.

    Delta-measured against the same candidate set, so nothing but the override
    differs between the two previews:

      1. An ``agent-user`` candidate who holds neither pointer nor override
      2. A record claiming the conversation slot, with no override of its own
      3. They pick a model — and only a model
      4. The candidate count is unchanged; the overwrite count is one higher
      5. The real run clears the pick, exactly as previewed
    """
    # ── Phase 1 + 2 ────────────────────────────────────────────────────
    # The account first: one created after the record would be
    # auto-provisioned on the spot and never appear as a candidate.
    picky, picky_headers = create_random_user_with_headers(client)
    parent = create_managed_credential(
        client,
        superuser_token_headers,
        name="Company Slot Claim",
        auto_provision_roles=["agent-user"],
        set_user_sdk_defaults=True,
        sdk_default_modes=["conversation"],
    )["record"]
    before = apply_to_existing(
        client, superuser_token_headers, parent["id"], dry_run=True
    )
    profile = _me(client, picky_headers)
    assert profile["default_ai_credential_conversation_id"] is None
    assert profile["default_model_override_conversation"] is None

    # ── Phase 3 ────────────────────────────────────────────────────────
    _patch_me(
        client,
        picky_headers,
        default_model_override_conversation="a-model-they-picked",
    )

    # ── Phase 4 ────────────────────────────────────────────────────────
    after = apply_to_existing(
        client, superuser_token_headers, parent["id"], dry_run=True
    )
    assert after["candidate_count"] == before["candidate_count"], (
        "Nobody joined or left; only one candidate's profile changed."
    )
    assert (
        after["defaults_overwrite_count"]
        == before["defaults_overwrite_count"] + 1
    ), (
        "A model override with a NULL pointer beside it was reset uncounted. "
        f"before={before['defaults_overwrite_count']} "
        f"after={after['defaults_overwrite_count']}"
    )

    # ── Phase 5 ────────────────────────────────────────────────────────
    apply_to_existing(client, superuser_token_headers, parent["id"])
    record = get_managed_credential(
        client, superuser_token_headers, parent["id"]
    )
    member = member_for(record, picky["id"])
    assert member is not None
    reloaded = _me(client, picky_headers)
    assert (
        reloaded["default_ai_credential_conversation_id"]
        == member["child_credential_id"]
    )
    assert reloaded["default_model_override_conversation"] is None, (
        "The claim was supposed to reset the slot's model override."
    )


def test_a_failing_child_insert_leaves_apply_to_existing_a_usable_session(
    client: TestClient, db: Session, superuser_token_headers: dict[str, str]
) -> None:
    """``add_members`` repairs the session itself, and it has to.

    A per-owner failure can be *statement*-level — a lock timeout, a
    serialization failure, a constraint the insert trips — and Postgres leaves
    the whole transaction aborted after one of those, not just the statement.
    ``add_members`` recording a skip and returning normally on an aborted
    transaction is a normal-looking return whose session detonates in whatever
    runs next.

    This route is the shortest proof that it must be ``add_members``' own job.
    Nothing here writes an audit row: ``apply_to_existing`` goes straight from
    ``add_members`` to ``_to_public``, which queries. For a while the *account
    creation* path survived only because ``AccountProvisioningService`` wrote a
    security event immediately afterwards, whose ``commit`` failed and rolled
    the session back as a side effect — a guarantee that would have evaporated
    the day somebody batched those events or made them lazy. There is no such
    accident on this path, so this test fails against that arrangement and
    passes only when the recovery is where it belongs.

      1. Two ``agent-user`` accounts are missing the record
      2. The second child insert aborts the transaction for real
      3. The route still answers 200 — not a 500 out of ``_to_public``
      4. One grant landed, one is reported skipped
      5. The session the request ran on is usable afterwards
    """
    create_random_user_with_headers(client)
    create_random_user_with_headers(client)
    parent = create_managed_credential(
        client,
        superuser_token_headers,
        name="Company Half Broken",
        auto_provision_roles=["agent-user"],
    )["record"]
    expected = apply_to_existing(
        client, superuser_token_headers, parent["id"], dry_run=True
    )["candidate_count"]
    assert expected >= 2, (
        f"Need at least two candidates to abort the second insert; got {expected}"
    )

    with failing_sql_statement(
        db, when_statement_contains=CHILD_CREDENTIAL_INSERT, occurrence=2
    ) as injection:
        response = client.post(
            f"{ADMIN_BASE}/{parent['id']}/apply-to-existing",
            headers=superuser_token_headers,
            params={"dry_run": False},
        )

    assert injection["fired"], (
        "The injected statement never matched — no failure was reproduced and "
        "this test is asserting nothing."
    )
    assert response.status_code == 200, (
        "The route died after a per-member failure. `add_members` returned on "
        f"an aborted transaction and `_to_public` ran into it: {response.text}"
    )
    result = response.json()
    assert len(result["added"]) == expected - 1, result
    assert [skip["reason"] for skip in result["skipped"]] == [
        "provision_failed"
    ], result
    assert session_is_usable(db)


def test_apply_to_existing_on_a_record_with_no_auto_provision_roles_does_nothing(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """No roles means no candidate set — the action is defined, and empty.

    Also the control for ``defaults_overwrite_count``: it must be 0 here, so
    the ``>= 1`` above is measuring something rather than reporting a constant.
    """
    create_random_user_with_headers(client)
    parent = create_managed_credential(
        client,
        superuser_token_headers,
        name="Manual only",
        set_user_sdk_defaults=True,
        sdk_default_modes=["conversation"],
    )["record"]
    assert parent["auto_provision_roles"] == []

    preview = apply_to_existing(
        client, superuser_token_headers, parent["id"], dry_run=True
    )
    assert preview["candidates"] == []
    assert preview["candidate_count"] == 0
    assert preview["defaults_overwrite_count"] == 0

    applied = apply_to_existing(client, superuser_token_headers, parent["id"])
    assert applied["added"] == []
    assert applied["candidate_count"] == 0


def test_apply_to_existing_is_superuser_only(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """It hands out a company API key, so it is not a route a member may call."""
    _, member_headers = create_random_user_with_headers(client)
    parent = create_managed_credential(
        client,
        superuser_token_headers,
        name="Guarded",
        auto_provision_roles=["agent-user"],
    )["record"]

    response = client.post(
        f"{ADMIN_BASE}/{parent['id']}/apply-to-existing", headers=member_headers
    )
    assert response.status_code in (401, 403), response.text
    assert client.post(f"{ADMIN_BASE}/{parent['id']}/apply-to-existing").status_code in (
        401,
        403,
    )


# ── Scenario 4: model overrides — landing, updating, clearing ──────────


def test_model_overrides_land_on_the_owner_and_clear_when_membership_ends(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    The whole life of a per-mode model override:
      1. A member is added to a record carrying overrides → both modes land on
         the owner's profile, alongside the credential pointer and engine
      2. Changing an override on the record writes through to the member who
         still points at this child
      3. An edit that says nothing about the overrides leaves them alone
      4. An omitted (``null``) override still means "unchanged"; a submitted
         ``""`` is the clear, and it reaches the member who was pinned to the
         value being dropped
      5. Removing the member un-points the owner's per-mode credential — and
         takes the override with it (asserted in full in the next test)
    """
    member, member_headers = create_random_user_with_headers(client)

    # ── Phase 1 ────────────────────────────────────────────────────────
    created = create_managed_credential(
        client,
        superuser_token_headers,
        name="Overrides",
        target_user_ids=[member["id"]],
        set_user_sdk_defaults=True,
        sdk_default_modes=["conversation", "building"],
        model_override_conversation="model-for-chat",
        model_override_building="model-for-building",
    )
    parent = created["record"]
    child_id = created["added"][0]["child_credential_id"]

    profile = _me(client, member_headers)
    assert profile["default_ai_credential_conversation_id"] == child_id
    assert profile["default_ai_credential_building_id"] == child_id
    assert profile["default_sdk_conversation"] == ANTHROPIC_ENGINE
    assert profile["default_model_override_conversation"] == "model-for-chat"
    assert profile["default_model_override_building"] == "model-for-building"

    # ── Phase 2: an override change reaches an existing member ─────────
    update_managed_credential(
        client,
        superuser_token_headers,
        parent["id"],
        model_override_conversation="model-for-chat-v2",
    )
    profile = _me(client, member_headers)
    assert profile["default_model_override_conversation"] == "model-for-chat-v2"
    assert profile["default_model_override_building"] == "model-for-building"

    # ── Phase 3: an unrelated edit changes nothing ─────────────────────
    update_managed_credential(
        client, superuser_token_headers, parent["id"], name="Overrides renamed"
    )
    profile = _me(client, member_headers)
    assert profile["default_model_override_conversation"] == "model-for-chat-v2"
    assert profile["default_model_override_building"] == "model-for-building"

    # ── Phase 4a: an explicit null is still "leave unchanged" ──────────
    update_managed_credential(
        client,
        superuser_token_headers,
        parent["id"],
        model_override_conversation=None,
    )
    still_set = get_managed_credential(
        client, superuser_token_headers, parent["id"]
    )
    assert still_set["model_override_conversation"] == "model-for-chat-v2", (
        "PATCH with an explicit null is 'leave unchanged' for this field, "
        "matching base_url/model — an edit that says nothing about models "
        "must not erase one."
    )
    assert _me(client, member_headers)[
        "default_model_override_conversation"
    ] == "model-for-chat-v2"

    # ── Phase 4b: an empty string is the clear, and it propagates ──────
    update_managed_credential(
        client,
        superuser_token_headers,
        parent["id"],
        model_override_conversation="",
    )
    cleared = get_managed_credential(
        client, superuser_token_headers, parent["id"]
    )
    assert cleared["model_override_conversation"] is None, (
        "An empty string clears the override to NULL. This is the documented "
        "contract on ManagedAICredentialUpdate, not an accident of "
        "_normalize_default_model."
    )
    assert cleared["model_override_building"] == "model-for-building", (
        "Clearing one mode must not touch the other."
    )
    profile = _me(client, member_headers)
    assert profile["default_model_override_conversation"] is None, (
        "A clear that leaves every existing member pinned to the dropped "
        "value is cosmetic: the record reads blank and no admin action can "
        "ever remove the pin."
    )
    assert profile["default_model_override_building"] == "model-for-building"

    # ── Phase 5: removal un-points the owner ───────────────────────────
    update_managed_credential(
        client, superuser_token_headers, parent["id"], target_user_ids=[]
    )
    profile = _me(client, member_headers)
    assert profile["default_ai_credential_conversation_id"] is None
    assert profile["default_ai_credential_building_id"] is None
    assert member_user_ids(
        get_managed_credential(client, superuser_token_headers, parent["id"])
    ) == set()


def test_removing_a_member_tears_down_the_model_override_with_the_pointer(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """The other half of step 5, stated on its own because it was missing.

    ``_clear_child_default`` clears ``default_model_override_<mode>`` alongside
    the pointer, and its comment gives the reason: *"leaving the override
    behind would pin the user's next default credential to a model chosen for
    the one just removed."* That is exactly right — and that method is not on
    the removal path. It is reached only from the *update* pass, when
    ``set_as_default`` flips from true to false. Reconcile's Remove pass calls
    ``ai_credentials_service.delete_credential``, which touches none of the
    profile wiring; the pointer only goes ``NULL`` because
    ``user.default_ai_credential_<mode>_id`` carries ``ondelete="SET NULL"`` at
    the database — a database fact that knows nothing about the override
    beside it.

    So the Remove pass now reads which slots the child holds *before* the
    delete (afterwards the pointer is already NULL and the answer is gone) and
    releases those overrides after it succeeds. ``DELETE
    /admin/llm-providers/{id}`` reconciles to an empty membership by the same
    route and is fixed with it.

    ``default_sdk_<mode>`` is left dangling still, and is deliberately not
    asserted here — but not because it is harmless. Nothing re-derives it:
    ``EnvironmentService`` and ``ExternalAccountConfigService`` read
    ``user.default_sdk_<mode>`` as-is, so a removed member's next environment
    is still composed on the engine of a credential they no longer hold. It is
    out of this test's scope because it predates the phase and wants its own
    coverage, not because it does nothing.
    """
    member, member_headers = create_random_user_with_headers(client)
    parent = create_managed_credential(
        client,
        superuser_token_headers,
        name="Teardown",
        target_user_ids=[member["id"]],
        set_user_sdk_defaults=True,
        sdk_default_modes=["conversation", "building"],
        model_override_conversation="model-for-chat",
        model_override_building="model-for-building",
    )["record"]

    before = _me(client, member_headers)
    assert before["default_model_override_conversation"] == "model-for-chat"
    assert before["default_model_override_building"] == "model-for-building"

    update_managed_credential(
        client, superuser_token_headers, parent["id"], target_user_ids=[]
    )

    after = _me(client, member_headers)
    assert after["default_ai_credential_conversation_id"] is None
    assert after["default_model_override_conversation"] is None
    assert after["default_model_override_building"] is None


def test_claiming_a_slot_resets_the_override_but_holding_one_does_not_wipe_it(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    The asymmetry the spec did not have, and the reason it was added.

    The spec's single rule — "write the parent's override through" — destroyed
    user data on the update path: an admin renaming a record would silently
    erase the model every member had chosen for it, and since ``None`` means
    "unchanged" on the request model, the admin could not even have expressed
    that wipe on purpose. So the implementation splits it:

      1. A user picks their own conversation model against their own credential
      2. They are added to a record that wires ``conversation`` and has **no**
         override of its own → the slot is *claimed*, so their override is
         reset. Their old value described a different credential and may name
         a model this provider does not serve
      3. They pick a model again, now against the managed child
      4. The admin edits the record — a rename, nothing about models → the slot
         is merely *held*, the record has no opinion, and their choice survives
      5. The admin then states an opinion → that one does write through, so
         "no opinion" is genuinely about the absence of a value and not about
         the update path being inert
    """
    member, member_headers = create_random_user_with_headers(client)

    # ── Phase 1: their own credential, their own model ─────────────────
    own = create_random_ai_credential(client, member_headers)
    _patch_me(
        client,
        member_headers,
        default_ai_credential_conversation_id=own["id"],
        default_model_override_conversation="their-own-model",
    )
    assert _me(client, member_headers)[
        "default_model_override_conversation"
    ] == "their-own-model"

    # ── Phase 2: claiming resets ───────────────────────────────────────
    created = create_managed_credential(
        client,
        superuser_token_headers,
        name=f"Opinionless {random_lower_string()[:6]}",
        target_user_ids=[member["id"]],
        set_user_sdk_defaults=True,
        sdk_default_modes=["conversation"],
    )
    parent = created["record"]
    child_id = created["added"][0]["child_credential_id"]

    profile = _me(client, member_headers)
    assert profile["default_ai_credential_conversation_id"] == child_id
    assert profile["default_model_override_conversation"] is None, (
        "Claiming the slot must reset the override: the value that was there "
        "described a different credential."
    )

    # ── Phase 3: they choose again, against the managed child ──────────
    _patch_me(
        client, member_headers, default_model_override_conversation="chosen-again"
    )

    # ── Phase 4: holding does not wipe ─────────────────────────────────
    update_managed_credential(
        client, superuser_token_headers, parent["id"], name="Renamed by admin"
    )
    assert _me(client, member_headers)[
        "default_model_override_conversation"
    ] == "chosen-again", (
        "An admin renaming the record erased a model the user had chosen. "
        "On the update path a parent with no override has no opinion — it "
        "must not write its None through."
    )

    # ── Phase 5: an actual opinion does write through ──────────────────
    update_managed_credential(
        client,
        superuser_token_headers,
        parent["id"],
        model_override_conversation="admin-says-this-one",
    )
    assert _me(client, member_headers)[
        "default_model_override_conversation"
    ] == "admin-says-this-one"


def test_clearing_an_override_retracts_only_the_value_this_record_wrote(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """The guard that makes the clear safe, on the branch where it holds.

    Clearing an override to NULL unpins existing members — otherwise the record
    reads blank while every member stays pinned, and no admin action can ever
    remove it. But "unpin" must mean *retract this record's own value*, not
    *wipe the slot*: a member who picked their own model against the managed
    credential did not ask the admin's opinion and must not lose their choice
    when the admin withdraws it.

    Both halves need a member of their own, because the equality guard is
    invisible with only the first: delete ``!= retracted`` from
    ``_sync_model_overrides`` and a one-member version of this test still
    passes.

      1. Two members receive a record that pins ``admin-model``
      2. One of them replaces it with their own choice
      3. The admin clears the override on the record
      4. The one still carrying ``admin-model`` is unpinned
      5. The one who chose keeps their choice — and neither loses the
         credential pointer, which the clear has no business touching
    """
    kept, kept_headers = create_random_user_with_headers(client)
    chooser, chooser_headers = create_random_user_with_headers(client)

    # ── Phase 1 ────────────────────────────────────────────────────────
    parent = create_managed_credential(
        client,
        superuser_token_headers,
        name=f"Retract {random_lower_string()[:6]}",
        target_user_ids=[kept["id"], chooser["id"]],
        set_user_sdk_defaults=True,
        sdk_default_modes=["conversation"],
        model_override_conversation="admin-model",
    )["record"]

    assert _me(client, kept_headers)[
        "default_model_override_conversation"
    ] == "admin-model"
    assert _me(client, chooser_headers)[
        "default_model_override_conversation"
    ] == "admin-model"

    # ── Phase 2 ────────────────────────────────────────────────────────
    _patch_me(
        client,
        chooser_headers,
        default_model_override_conversation="chooser-picked-this",
    )

    # ── Phase 3 ────────────────────────────────────────────────────────
    update_managed_credential(
        client,
        superuser_token_headers,
        parent["id"],
        model_override_conversation="",
    )

    # ── Phase 4 + 5 ────────────────────────────────────────────────────
    kept_profile = _me(client, kept_headers)
    chooser_profile = _me(client, chooser_headers)

    assert kept_profile["default_model_override_conversation"] is None, (
        "A member still carrying the admin's value must be unpinned by the "
        "clear, or the clear is cosmetic."
    )
    assert chooser_profile[
        "default_model_override_conversation"
    ] == "chooser-picked-this", (
        "The clear retracts the admin's own value, not the slot. A member who "
        "chose their own model keeps it."
    )

    member_ids = member_user_ids(
        get_managed_credential(client, superuser_token_headers, parent["id"])
    )
    assert member_ids == {kept["id"], chooser["id"]}
    for profile in (kept_profile, chooser_profile):
        assert profile["default_ai_credential_conversation_id"] is not None, (
            "Clearing a model override must not un-wire the credential."
        )
