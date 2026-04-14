"""
Integration tests for the Identity MCP Server feature.

Tests are organized as scenario-based journeys covering:
  1. Binding full lifecycle — CRUD, field assertions, ownership guards
  2. Assignment management — assign, remove, self-exclusion, duplicate skipping
  3. Identity contacts — listing, per-person toggle (enable/disable)
  4. Summary endpoint — returns same data as bindings list
  5. auto_enable restriction — only superusers may use it
  6. Cross-user isolation — other user cannot read or mutate foreign bindings
  7. Error cases — agent not found, binding not found, agent ownership mismatch
  8. Cascade behavior — delete binding removes all its assignments
"""

import uuid

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.agent import create_agent_via_api
from tests.utils.identity import (
    assign_users_to_binding,
    create_identity_binding,
    delete_identity_binding,
    get_identity_summary,
    list_identity_bindings,
    list_identity_contacts,
    remove_user_from_binding,
    toggle_identity_contact,
    update_identity_binding,
)
from tests.utils.ai_credential import create_random_ai_credential
from tests.utils.user import (
    create_random_user,
    create_random_user_with_headers,
)

_BINDINGS = f"{settings.API_V1_STR}/identity/bindings"
_CONTACTS = f"{settings.API_V1_STR}/users/me/identity-contacts"


# ---------------------------------------------------------------------------
# Scenario 1: Binding full lifecycle
# ---------------------------------------------------------------------------


def test_identity_binding_lifecycle(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Full binding lifecycle from creation to deletion:
      1.  Unauthenticated requests rejected
      2.  Create an agent (prerequisite for binding)
      3.  Create a binding — verify initial fields
      4.  Binding appears in list
      5.  Summary returns the same binding
      6.  Update trigger_prompt and message_patterns — changes persist
      7.  Update session_mode and is_active — changes persist
      8.  Update binding owned by another user returns 404
      9.  Non-existent binding ID returns 404 on PUT
      10. Delete binding — 200 message returned
      11. Verify binding is gone from list
    """
    # ── Phase 1: No auth ───────────────────────────────────────────────────
    assert client.get(f"{_BINDINGS}/").status_code in (401, 403)
    assert client.post(f"{_BINDINGS}/", json={}).status_code in (401, 403)

    # ── Phase 2: Create agent ──────────────────────────────────────────────
    agent = create_agent_via_api(client, superuser_token_headers)
    agent_id = agent["id"]

    # ── Phase 3: Create binding ────────────────────────────────────────────
    prompt = "Route to this agent for billing questions."
    patterns = "billing|invoice|payment"
    binding = create_identity_binding(
        client,
        superuser_token_headers,
        agent_id=agent_id,
        trigger_prompt=prompt,
        message_patterns=patterns,
        session_mode="conversation",
    )
    binding_id = binding["id"]

    assert binding["agent_id"] == agent_id
    assert binding["agent_name"] == agent["name"]
    assert binding["trigger_prompt"] == prompt
    assert binding["message_patterns"] == patterns
    assert binding["session_mode"] == "conversation"
    assert binding["is_active"] is True
    assert "created_at" in binding
    assert "updated_at" in binding
    assert binding["assignments"] == []

    # ── Phase 4: Binding appears in list ──────────────────────────────────
    bindings = list_identity_bindings(client, superuser_token_headers)
    assert any(b["id"] == binding_id for b in bindings)

    # ── Phase 5: Summary returns same binding ─────────────────────────────
    summary = get_identity_summary(client, superuser_token_headers)
    assert any(b["id"] == binding_id for b in summary)
    # Summary and list must return identical content
    summary_ids = {b["id"] for b in summary}
    list_ids = {b["id"] for b in bindings}
    assert summary_ids == list_ids

    # ── Phase 6: Update trigger_prompt and message_patterns ───────────────
    new_prompt = "Route to this agent for technical support."
    new_patterns = "error|crash|bug"
    updated = update_identity_binding(
        client,
        superuser_token_headers,
        binding_id,
        trigger_prompt=new_prompt,
        message_patterns=new_patterns,
    )
    assert updated["trigger_prompt"] == new_prompt
    assert updated["message_patterns"] == new_patterns
    assert updated["session_mode"] == "conversation"  # unchanged
    assert updated["is_active"] is True  # unchanged

    # Verify the update persisted via list
    refreshed = list_identity_bindings(client, superuser_token_headers)
    refreshed_binding = next(b for b in refreshed if b["id"] == binding_id)
    assert refreshed_binding["trigger_prompt"] == new_prompt

    # ── Phase 7: Update session_mode and deactivate ────────────────────────
    toggled = update_identity_binding(
        client,
        superuser_token_headers,
        binding_id,
        session_mode="single_turn",
        is_active=False,
    )
    assert toggled["session_mode"] == "single_turn"
    assert toggled["is_active"] is False

    # Re-activate for remaining phases
    update_identity_binding(
        client, superuser_token_headers, binding_id, is_active=True
    )

    # ── Phase 8: Other user cannot update this binding ────────────────────
    other_user, other_headers = create_random_user_with_headers(client)
    r = client.put(
        f"{_BINDINGS}/{binding_id}",
        headers=other_headers,
        json={"trigger_prompt": "hijacked"},
    )
    assert r.status_code == 404

    # Binding is still intact
    still_there = list_identity_bindings(client, superuser_token_headers)
    still_binding = next(b for b in still_there if b["id"] == binding_id)
    assert still_binding["trigger_prompt"] == new_prompt  # not hijacked

    # ── Phase 9: Non-existent binding ID returns 404 ──────────────────────
    ghost_id = str(uuid.uuid4())
    assert client.put(
        f"{_BINDINGS}/{ghost_id}",
        headers=superuser_token_headers,
        json={"trigger_prompt": "x"},
    ).status_code == 404

    # ── Phase 10: Delete binding ───────────────────────────────────────────
    result = delete_identity_binding(client, superuser_token_headers, binding_id)
    assert "message" in result
    assert "deleted" in result["message"].lower()

    # ── Phase 11: Verify gone ─────────────────────────────────────────────
    final_bindings = list_identity_bindings(client, superuser_token_headers)
    assert not any(b["id"] == binding_id for b in final_bindings)


# ---------------------------------------------------------------------------
# Scenario 2: Assignment management
# ---------------------------------------------------------------------------


def test_assignment_management(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Assignment lifecycle and edge cases:
      1. Create agent + binding
      2. Assign two users — assignments appear in binding response
      3. Assignment fields are populated (user email, is_active, is_enabled)
      4. Assign the same users again — duplicates are silently skipped
      5. Assign the owner's own ID — self-assignment silently skipped
      6. Remove one assignment — remaining still present
      7. Remove non-existent assignment returns 404
      8. Other user cannot add assignments to a foreign binding
      9. Delete non-existent binding returns 404
    """
    agent = create_agent_via_api(client, superuser_token_headers)
    agent_id = agent["id"]
    binding = create_identity_binding(
        client, superuser_token_headers, agent_id=agent_id
    )
    binding_id = binding["id"]

    # Get the superuser's own ID to test self-exclusion
    me_r = client.get(f"{settings.API_V1_STR}/users/me", headers=superuser_token_headers)
    assert me_r.status_code == 200
    owner_id = me_r.json()["id"]

    # Create two target users
    user_a, headers_a = create_random_user_with_headers(client)
    user_b = create_random_user(client)
    user_a_id = user_a["id"]
    user_b_id = user_b["id"]

    # ── Phase 2: Assign two users ──────────────────────────────────────────
    assignments = assign_users_to_binding(
        client, superuser_token_headers, binding_id, [user_a_id, user_b_id]
    )
    assert len(assignments) == 2
    assigned_user_ids = {a["target_user_id"] for a in assignments}
    assert user_a_id in assigned_user_ids
    assert user_b_id in assigned_user_ids

    # ── Phase 3: Assignment fields are populated ───────────────────────────
    for assignment in assignments:
        assert "id" in assignment
        assert "binding_id" in assignment
        assert assignment["binding_id"] == binding_id
        assert "target_user_id" in assignment
        assert "target_user_email" in assignment
        assert assignment["target_user_email"] != ""
        assert isinstance(assignment["is_active"], bool)
        assert isinstance(assignment["is_enabled"], bool)
        assert "created_at" in assignment

    # Assignments also appear in the binding's nested list
    bindings = list_identity_bindings(client, superuser_token_headers)
    nested = next(b for b in bindings if b["id"] == binding_id)
    nested_ids = {a["target_user_id"] for a in nested["assignments"]}
    assert user_a_id in nested_ids
    assert user_b_id in nested_ids

    # ── Phase 4: Duplicate assignment silently skipped ────────────────────
    assignments_again = assign_users_to_binding(
        client, superuser_token_headers, binding_id, [user_a_id]
    )
    # Count must be unchanged — still 2
    assert len(assignments_again) == 2

    # ── Phase 5: Self-assignment silently skipped ──────────────────────────
    assignments_with_self = assign_users_to_binding(
        client, superuser_token_headers, binding_id, [owner_id]
    )
    # Still 2 — owner not added
    assert len(assignments_with_self) == 2
    assert not any(a["target_user_id"] == owner_id for a in assignments_with_self)

    # ── Phase 6: Remove one assignment ────────────────────────────────────
    result = remove_user_from_binding(
        client, superuser_token_headers, binding_id, user_a_id
    )
    assert "message" in result

    # User A should be gone; user B still present
    after_remove = list_identity_bindings(client, superuser_token_headers)
    after_nested = next(b for b in after_remove if b["id"] == binding_id)
    remaining_ids = {a["target_user_id"] for a in after_nested["assignments"]}
    assert user_a_id not in remaining_ids
    assert user_b_id in remaining_ids

    # ── Phase 7: Remove non-existent assignment returns 404 ───────────────
    ghost_user_id = str(uuid.uuid4())
    r = client.delete(
        f"{_BINDINGS}/{binding_id}/assignments/{ghost_user_id}",
        headers=superuser_token_headers,
    )
    assert r.status_code == 404

    # ── Phase 8: Other user cannot add assignments to foreign binding ──────
    r = client.post(
        f"{_BINDINGS}/{binding_id}/assignments",
        headers=headers_a,
        json=[str(uuid.uuid4())],
    )
    assert r.status_code in (403, 404)

    # ── Phase 9: Delete non-existent binding returns 404 ──────────────────
    ghost_binding_id = str(uuid.uuid4())
    r = client.delete(
        f"{_BINDINGS}/{ghost_binding_id}",
        headers=superuser_token_headers,
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Scenario 3: Identity contacts — listing and per-person toggle
# ---------------------------------------------------------------------------


def test_identity_contacts_listing_and_toggle(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Identity contacts from the target user's perspective:
      1. Owner creates two bindings and assigns a target user to both
      2. Target user lists contacts — sees one contact (the owner)
      3. Contact has correct owner info, agent_count=2, is_enabled starts False
      4. Target user enables the contact — is_enabled becomes True
      5. Target user disables the contact — is_enabled becomes False
      6. Toggle non-existent owner returns 404
      7. Unauthenticated contacts request is rejected
    """
    # ── Phase 1: Owner creates two bindings and assigns target user ────────
    owner_me = client.get(
        f"{settings.API_V1_STR}/users/me", headers=superuser_token_headers
    ).json()
    owner_id = owner_me["id"]

    agent_1 = create_agent_via_api(client, superuser_token_headers)
    agent_2 = create_agent_via_api(client, superuser_token_headers)

    target_user, target_headers = create_random_user_with_headers(client)
    target_user_id = target_user["id"]

    create_identity_binding(
        client,
        superuser_token_headers,
        agent_id=agent_1["id"],
        trigger_prompt="Support agent.",
        assigned_user_ids=[target_user_id],
    )
    create_identity_binding(
        client,
        superuser_token_headers,
        agent_id=agent_2["id"],
        trigger_prompt="Sales agent.",
        assigned_user_ids=[target_user_id],
    )

    # ── Phase 2: Target user lists contacts ───────────────────────────────
    contacts = list_identity_contacts(client, target_headers)
    owner_contact = next(
        (c for c in contacts if c["owner_id"] == owner_id), None
    )
    assert owner_contact is not None, "Owner must appear in target's contacts"

    # ── Phase 3: Contact fields ────────────────────────────────────────────
    assert owner_contact["owner_email"] == owner_me["email"]
    assert owner_contact["agent_count"] == 2
    assert isinstance(owner_contact["assignment_ids"], list)
    assert len(owner_contact["assignment_ids"]) == 2
    # Default is_enabled is False (auto_enable not set)
    assert owner_contact["is_enabled"] is False

    # ── Phase 4: Enable all assignments from owner ────────────────────────
    toggle_result = toggle_identity_contact(
        client, target_headers, owner_id, is_enabled=True
    )
    assert "message" in toggle_result

    contacts_after_enable = list_identity_contacts(client, target_headers)
    enabled_contact = next(
        c for c in contacts_after_enable if c["owner_id"] == owner_id
    )
    assert enabled_contact["is_enabled"] is True

    # ── Phase 5: Disable all assignments from owner ────────────────────────
    toggle_identity_contact(client, target_headers, owner_id, is_enabled=False)

    contacts_after_disable = list_identity_contacts(client, target_headers)
    disabled_contact = next(
        c for c in contacts_after_disable if c["owner_id"] == owner_id
    )
    assert disabled_contact["is_enabled"] is False

    # ── Phase 6: Toggle non-existent owner returns 404 ────────────────────
    ghost_owner = str(uuid.uuid4())
    r = client.patch(
        f"{_CONTACTS}/{ghost_owner}",
        headers=target_headers,
        json={"is_enabled": True},
    )
    assert r.status_code == 404

    # ── Phase 7: Unauthenticated contacts request rejected ────────────────
    assert client.get(f"{_CONTACTS}/").status_code in (401, 403)


# ---------------------------------------------------------------------------
# Scenario 4: auto_enable — superuser restriction
# ---------------------------------------------------------------------------


def test_auto_enable_superuser_restriction(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    auto_enable=True:
      1. Regular user gets 403 when trying to create binding with auto_enable=True
      2. Superuser can create a binding with auto_enable=True
      3. Assigned users have is_enabled=True from the start
      4. Superuser without auto_enable: assigned users have is_enabled=False
    """
    # ── Phase 1: Regular user rejected for auto_enable ────────────────────
    regular_user, regular_headers = create_random_user_with_headers(client)
    # Regular user needs an AI credential to be able to create an agent
    create_random_ai_credential(
        client, regular_headers,
        credential_type="anthropic",
        api_key="sk-ant-api03-test-regular-user-key",
        set_default=True,
    )
    regular_agent = create_agent_via_api(client, regular_headers)

    r = client.post(
        f"{_BINDINGS}/",
        headers=regular_headers,
        json={
            "agent_id": regular_agent["id"],
            "trigger_prompt": "Some agent.",
            "assigned_user_ids": [],
            "auto_enable": True,
        },
    )
    assert r.status_code == 403
    assert "administrator" in r.json()["detail"].lower() or "superuser" in r.json()["detail"].lower() or "admin" in r.json()["detail"].lower()

    # ── Phase 2: Superuser can use auto_enable ────────────────────────────
    su_agent = create_agent_via_api(client, superuser_token_headers)
    target_user, target_headers = create_random_user_with_headers(client)
    target_id = target_user["id"]

    auto_binding = create_identity_binding(
        client,
        superuser_token_headers,
        agent_id=su_agent["id"],
        trigger_prompt="Auto-enabled agent.",
        assigned_user_ids=[target_id],
        auto_enable=True,
    )

    # ── Phase 3: Assigned users have is_enabled=True immediately ──────────
    assignments = auto_binding["assignments"]
    assert len(assignments) == 1
    assert assignments[0]["is_enabled"] is True

    # Target user's contact shows is_enabled=True
    contacts = list_identity_contacts(client, target_headers)
    su_me = client.get(f"{settings.API_V1_STR}/users/me", headers=superuser_token_headers).json()
    contact = next((c for c in contacts if c["owner_id"] == su_me["id"]), None)
    assert contact is not None
    assert contact["is_enabled"] is True

    # ── Phase 4: Without auto_enable, assigned user starts with is_enabled=False
    su_agent_2 = create_agent_via_api(client, superuser_token_headers)
    create_identity_binding(
        client,
        superuser_token_headers,
        agent_id=su_agent_2["id"],
        trigger_prompt="Normal agent.",
        assigned_user_ids=[target_id],
        auto_enable=False,
    )
    # The new assignment should have is_enabled=False
    # Check via contacts — there will be 2 agents from this owner now
    contacts_2 = list_identity_contacts(client, target_headers)
    contact_2 = next(c for c in contacts_2 if c["owner_id"] == su_me["id"])
    # agent_count is now 2; the first was auto-enabled, second was not.
    # is_enabled is True if ANY is enabled (as per service logic)
    assert contact_2["agent_count"] == 2
    assert contact_2["is_enabled"] is True  # because one is still enabled


# ---------------------------------------------------------------------------
# Scenario 5: Agent ownership validation
# ---------------------------------------------------------------------------


def test_agent_ownership_validation(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    A user cannot bind an agent they do not own:
      1. Superuser creates an agent
      2. Regular user tries to bind that agent → 403
      3. Agent that doesn't exist → 404
      4. Regular user's own agent → 200 (allowed)
    """
    su_agent = create_agent_via_api(client, superuser_token_headers)
    su_agent_id = su_agent["id"]

    regular_user, regular_headers = create_random_user_with_headers(client)

    # ── Phase 2: Regular user cannot bind superuser's agent ───────────────
    # (No AI credential needed — agent ownership check happens before env creation)
    r = client.post(
        f"{_BINDINGS}/",
        headers=regular_headers,
        json={
            "agent_id": su_agent_id,
            "trigger_prompt": "Steal this agent.",
            "assigned_user_ids": [],
        },
    )
    assert r.status_code == 403
    assert "own" in r.json()["detail"].lower() or "permission" in r.json()["detail"].lower() or "only" in r.json()["detail"].lower()

    # ── Phase 3: Non-existent agent → 404 ────────────────────────────────
    ghost_agent = str(uuid.uuid4())
    r = client.post(
        f"{_BINDINGS}/",
        headers=regular_headers,
        json={
            "agent_id": ghost_agent,
            "trigger_prompt": "Missing agent.",
            "assigned_user_ids": [],
        },
    )
    assert r.status_code == 404

    # ── Phase 4: Regular user can bind their own agent ────────────────────
    # Create an AI credential for the regular user first (required for agent creation)
    create_random_ai_credential(
        client, regular_headers,
        credential_type="anthropic",
        api_key="sk-ant-api03-test-regular-key",
        set_default=True,
    )
    own_agent = create_agent_via_api(client, regular_headers)
    own_binding = create_identity_binding(
        client,
        regular_headers,
        agent_id=own_agent["id"],
        trigger_prompt="My own agent.",
    )
    assert own_binding["agent_id"] == own_agent["id"]

    # Regular user cannot see superuser's bindings (isolation)
    own_bindings = list_identity_bindings(client, regular_headers)
    su_bindings = list_identity_bindings(client, superuser_token_headers)
    own_ids = {b["id"] for b in own_bindings}
    su_ids = {b["id"] for b in su_bindings}
    assert own_ids.isdisjoint(su_ids), "Regular user and superuser bindings must not overlap"


# ---------------------------------------------------------------------------
# Scenario 6: Duplicate binding constraint (same agent twice)
# ---------------------------------------------------------------------------


def test_duplicate_binding_rejected(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Creating a binding with the same (owner, agent) pair twice returns 409:
      1. Create binding for agent A
      2. Create same binding again — 409 Conflict
    """
    agent_a = create_agent_via_api(client, superuser_token_headers)

    # ── Phase 1: First binding succeeds ───────────────────────────────────
    create_identity_binding(
        client, superuser_token_headers, agent_id=agent_a["id"]
    )

    # ── Phase 2: Duplicate rejected ───────────────────────────────────────
    # Note: The unique constraint violation invalidates the test transaction
    # savepoint, so this test ends here. See test_two_agents_can_be_bound for
    # the complementary case that verifies a different agent CAN be added.
    r = client.post(
        f"{_BINDINGS}/",
        headers=superuser_token_headers,
        json={
            "agent_id": agent_a["id"],
            "trigger_prompt": "Duplicate.",
            "assigned_user_ids": [],
        },
    )
    assert r.status_code == 409
    assert "already" in r.json()["detail"].lower()


def test_two_agents_can_be_bound_independently(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Two different agents can each have their own binding under the same owner:
      1. Create two agents
      2. Bind agent A — succeeds
      3. Bind agent B — also succeeds (different agent, not a duplicate)
      4. Both bindings appear in the list
    """
    agent_a = create_agent_via_api(client, superuser_token_headers)
    agent_b = create_agent_via_api(client, superuser_token_headers)

    binding_a = create_identity_binding(
        client, superuser_token_headers, agent_id=agent_a["id"],
        trigger_prompt="Agent A binding.",
    )
    binding_b = create_identity_binding(
        client, superuser_token_headers, agent_id=agent_b["id"],
        trigger_prompt="Agent B binding.",
    )

    assert binding_a["agent_id"] == agent_a["id"]
    assert binding_b["agent_id"] == agent_b["id"]

    bindings = list_identity_bindings(client, superuser_token_headers)
    binding_ids = {b["id"] for b in bindings}
    assert binding_a["id"] in binding_ids
    assert binding_b["id"] in binding_ids


# ---------------------------------------------------------------------------
# Scenario 7: Cascade — deleting a binding removes its assignments
# ---------------------------------------------------------------------------


def test_binding_cascade_deletes_assignments(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Deleting a binding cascades to its assignments:
      1. Create binding with two assigned users
      2. Both users see the owner in their contacts
      3. Delete binding
      4. Both users no longer see the owner in contacts
    """
    agent = create_agent_via_api(client, superuser_token_headers)
    user_1, headers_1 = create_random_user_with_headers(client)
    user_2, headers_2 = create_random_user_with_headers(client)

    owner_me = client.get(
        f"{settings.API_V1_STR}/users/me", headers=superuser_token_headers
    ).json()
    owner_id = owner_me["id"]

    binding = create_identity_binding(
        client,
        superuser_token_headers,
        agent_id=agent["id"],
        trigger_prompt="Cascade test agent.",
        assigned_user_ids=[user_1["id"], user_2["id"]],
    )
    binding_id = binding["id"]

    # ── Phase 2: Both users see the contact ───────────────────────────────
    contacts_1 = list_identity_contacts(client, headers_1)
    assert any(c["owner_id"] == owner_id for c in contacts_1)

    contacts_2 = list_identity_contacts(client, headers_2)
    assert any(c["owner_id"] == owner_id for c in contacts_2)

    # ── Phase 3: Delete binding ────────────────────────────────────────────
    delete_identity_binding(client, superuser_token_headers, binding_id)

    # ── Phase 4: Assignments gone — contact no longer visible ─────────────
    contacts_1_after = list_identity_contacts(client, headers_1)
    assert not any(c["owner_id"] == owner_id for c in contacts_1_after)

    contacts_2_after = list_identity_contacts(client, headers_2)
    assert not any(c["owner_id"] == owner_id for c in contacts_2_after)

    # Second delete should return 404
    r = client.delete(f"{_BINDINGS}/{binding_id}", headers=superuser_token_headers)
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Scenario 8: create_binding with assigned_user_ids inline
# ---------------------------------------------------------------------------


def test_create_binding_with_inline_assignments(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Bindings can include assigned_user_ids at creation time:
      1. Create agent
      2. Create binding with two users assigned and one being the owner (self)
      3. Binding response contains assignments for the two non-owner users
      4. Owner not present in assignments (self-exclusion at creation)
    """
    agent = create_agent_via_api(client, superuser_token_headers)

    owner_me = client.get(
        f"{settings.API_V1_STR}/users/me", headers=superuser_token_headers
    ).json()
    owner_id = owner_me["id"]

    user_a, _ = create_random_user_with_headers(client)
    user_b = create_random_user(client)

    binding = create_identity_binding(
        client,
        superuser_token_headers,
        agent_id=agent["id"],
        trigger_prompt="Pre-assigned binding.",
        assigned_user_ids=[user_a["id"], user_b["id"], owner_id],
    )

    # ── Phase 3: Two non-owner users have assignments ──────────────────────
    assert len(binding["assignments"]) == 2
    assigned_ids = {a["target_user_id"] for a in binding["assignments"]}
    assert user_a["id"] in assigned_ids
    assert user_b["id"] in assigned_ids

    # ── Phase 4: Owner not in assignments ─────────────────────────────────
    assert owner_id not in assigned_ids
