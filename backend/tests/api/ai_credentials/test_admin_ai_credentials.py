"""
Tests for Admin Managed AI Credentials feature.

Covers ``/admin/llm-providers/*`` (superuser-gated CRUD over ManagedAICredential
parent records) plus cross-cutting behaviors:
  - Provisioning creates per-user AICredential children linked to the parent.
  - Reconcile on PATCH adds/removes/updates members.
  - set_as_default, set_user_sdk_defaults, sdk_default_modes wiring.
  - Idempotency: identical PATCH → empty diff.
  - Key-less PATCH still provisions new members from stored parent key.
  - SecurityEvent emission without key material.
  - Auth guard: non-superuser gets 403 on every route.
  - User-facing read-only guard: target user cannot PATCH/DELETE admin-managed row.
  - User CAN call set-default on an admin-managed credential (OQ-8).
  - ManagedAICredentialPublic has no encrypted_data/api_key.
  - Per-type validation: openai_compatible without base_url/model → 400.
  - Unknown/inactive targets land in result.skipped (not a hard failure).

Pure CRUD suite — no agents or environments needed.
"""
import uuid

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.ai_credential import (
    create_random_ai_credential,
    get_ai_credential,
    list_ai_credentials,
    set_ai_credential_default,
)
from tests.utils.user import create_random_user, user_authentication_headers

# No agent stubs or default credentials needed for these tests.
NEEDS_AGENT_STUBS = False
NEEDS_DEFAULT_CREDENTIALS = False

_ADMIN_BASE = f"{settings.API_V1_STR}/admin/llm-providers"
_CRED_BASE = f"{settings.API_V1_STR}/ai-credentials"
_SEC_EVENTS_BASE = f"{settings.API_V1_STR}/security-events"


# ── Helpers ───────────────────────────────────────────────────────────────────


def _create_managed(
    client: TestClient,
    headers: dict,
    target_user_ids: list[str],
    credential_type: str = "anthropic",
    name: str | None = None,
    api_key: str = "sk-ant-test-admin-provision",
    set_as_default: bool = False,
    set_user_sdk_defaults: bool = False,
    sdk_default_modes: list[str] | None = None,
    base_url: str | None = None,
    model: str | None = None,
    expected_status: int = 200,
) -> dict:
    """POST /admin/llm-providers/ and return the JSON response (ManagedAICredentialReconcileResult)."""
    payload: dict = {
        "name": name or f"Admin Cred {credential_type}",
        "type": credential_type,
        "api_key": api_key,
        "target_user_ids": target_user_ids,
        "set_as_default": set_as_default,
        "set_user_sdk_defaults": set_user_sdk_defaults,
    }
    if sdk_default_modes is not None:
        payload["sdk_default_modes"] = sdk_default_modes
    if base_url is not None:
        payload["base_url"] = base_url
    if model is not None:
        payload["model"] = model
    r = client.post(_ADMIN_BASE + "/", headers=headers, json=payload)
    assert r.status_code == expected_status, (
        f"_create_managed returned {r.status_code}: {r.text}"
    )
    return r.json()


def _list_managed(
    client: TestClient,
    headers: dict,
    managed_by_id: str | None = None,
    target_user_id: str | None = None,
) -> list[dict]:
    """GET /admin/llm-providers/ → list[ManagedAICredentialPublic]."""
    params: dict = {}
    if managed_by_id is not None:
        params["managed_by_id"] = managed_by_id
    if target_user_id is not None:
        params["target_user_id"] = target_user_id
    r = client.get(_ADMIN_BASE + "/", headers=headers, params=params)
    assert r.status_code == 200, f"_list_managed failed: {r.text}"
    return r.json()


def _get_managed(
    client: TestClient,
    headers: dict,
    managed_credential_id: str,
    expected_status: int = 200,
) -> dict:
    """GET /admin/llm-providers/{id} → ManagedAICredentialPublic."""
    r = client.get(f"{_ADMIN_BASE}/{managed_credential_id}", headers=headers)
    assert r.status_code == expected_status, (
        f"_get_managed({managed_credential_id}) returned {r.status_code}: {r.text}"
    )
    return r.json()


def _update_managed(
    client: TestClient,
    headers: dict,
    managed_credential_id: str,
    force: bool = False,
    expected_status: int = 200,
    **fields,
) -> dict:
    """PATCH /admin/llm-providers/{id} → ManagedAICredentialReconcileResult."""
    r = client.patch(
        f"{_ADMIN_BASE}/{managed_credential_id}",
        headers=headers,
        params={"force": force},
        json=fields,
    )
    assert r.status_code == expected_status, (
        f"_update_managed returned {r.status_code}: {r.text}"
    )
    return r.json()


def _delete_managed(
    client: TestClient,
    headers: dict,
    managed_credential_id: str,
    force: bool = False,
    expected_status: int = 200,
) -> dict:
    """DELETE /admin/llm-providers/{id}."""
    r = client.delete(
        f"{_ADMIN_BASE}/{managed_credential_id}",
        headers=headers,
        params={"force": force},
    )
    assert r.status_code == expected_status, (
        f"_delete_managed returned {r.status_code}: {r.text}"
    )
    return r.json()


def _set_managed_default(
    client: TestClient,
    headers: dict,
    managed_credential_id: str,
    expected_status: int = 200,
) -> dict:
    """POST /admin/llm-providers/{id}/set-default → ManagedAICredentialPublic."""
    r = client.post(
        f"{_ADMIN_BASE}/{managed_credential_id}/set-default", headers=headers
    )
    assert r.status_code == expected_status, (
        f"_set_managed_default returned {r.status_code}: {r.text}"
    )
    return r.json()


def _get_user_me(client: TestClient, headers: dict) -> dict:
    """GET /users/me."""
    r = client.get(f"{settings.API_V1_STR}/users/me", headers=headers)
    assert r.status_code == 200
    return r.json()


def _list_security_events(
    client: TestClient,
    headers: dict,
    event_type: str | None = None,
) -> list[dict]:
    """GET /security-events/ → list of events (optionally filtered by type)."""
    params: dict = {}
    if event_type is not None:
        params["event_type"] = event_type
    r = client.get(_SEC_EVENTS_BASE + "/", headers=headers, params=params)
    assert r.status_code == 200, f"_list_security_events failed: {r.text}"
    return r.json().get("data", [])


def _assert_no_key_material(obj: dict, sentinel: str) -> None:
    """Assert that *sentinel* (raw API key) does not appear anywhere in obj."""
    text = str(obj)
    assert sentinel not in text, f"Raw api_key leaked in response: {text}"
    assert "encrypted_data" not in obj
    assert "api_key" not in obj


# ── Scenario 1: Single-user provisioning (happy path) ─────────────────────────


def test_create_managed_single_user(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Single-user provisioning:
      1. POST /admin/llm-providers/ creates the parent + one child.
      2. result.record is the parent (ManagedAICredentialPublic).
      3. result.added has exactly one member; skipped/removed/updated are empty.
      4. member.user_id == target user id; child exists under that user.
      5. Child is owned by target, is_admin_managed=True in user-facing listing.
      6. Parent record has managed_by_id set (admin's id).
      7. No key material in response.
    """
    target = create_random_user(client)
    target_id = target["id"]
    target_headers = user_authentication_headers(
        client=client, email=target["email"], password=target["_password"]
    )

    # ── Phase 1: Create ───────────────────────────────────────────────────────
    result = _create_managed(
        client,
        superuser_token_headers,
        target_user_ids=[target_id],
        credential_type="anthropic",
        name="Company Anthropic Key",
        api_key="sk-ant-test-admin-single",
    )

    # ── Phase 2: Validate reconcile result shape ──────────────────────────────
    assert "record" in result
    assert "added" in result
    assert "removed" in result
    assert "updated" in result
    assert "skipped" in result
    assert "blocked" in result
    assert "updated_count" in result

    assert len(result["added"]) == 1
    assert result["removed"] == []
    assert result["skipped"] == []
    assert result["blocked"] == []
    assert result["updated_count"] == 0

    # ── Phase 3: Validate parent record ──────────────────────────────────────
    record = result["record"]
    parent_id = record["id"]
    assert record["name"] == "Company Anthropic Key"
    assert record["type"] == "anthropic"
    assert record["managed_by_id"] is not None
    assert record["member_count"] == 1
    assert len(record["members"]) == 1
    assert record["members"][0]["user_id"] == target_id
    # No key material
    assert "encrypted_data" not in record
    assert "api_key" not in record
    assert "has_api_key" in record and record["has_api_key"] is True

    # ── Phase 4: Validate the added member ───────────────────────────────────
    member = result["added"][0]
    assert member["user_id"] == target_id
    child_cred_id = member["child_credential_id"]

    # ── Phase 5: Admin can GET the parent ────────────────────────────────────
    fetched = _get_managed(client, superuser_token_headers, parent_id)
    assert fetched["id"] == parent_id
    assert fetched["managed_by_id"] is not None
    assert fetched["member_count"] == 1

    # ── Phase 6: Target user sees child in /ai-credentials/ ──────────────────
    user_list = list_ai_credentials(client, target_headers)
    child_ids = [c["id"] for c in user_list["data"]]
    assert child_cred_id in child_ids

    # ── Phase 7: User-facing GET: is_admin_managed=True, no managed_by_id ────
    user_cred = get_ai_credential(client, target_headers, child_cred_id)
    assert user_cred["is_admin_managed"] is True
    assert "managed_by_id" not in user_cred
    assert "encrypted_data" not in user_cred
    assert "api_key" not in user_cred


# ── Scenario 2: Multi-user provisioning with skipped targets ──────────────────


def test_create_managed_multi_user_with_skips(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    N-user provisioning with invalid targets:
      1. 2 valid users + 1 ghost UUID → 2 members added, 1 skipped.
      2. Each added member has the correct user_id.
      3. Skipped target has reason='user_not_found'.
      4. Call still returns 200 (partial success is not a hard error).
    """
    user_a = create_random_user(client)
    user_b = create_random_user(client)
    ghost_id = str(uuid.uuid4())

    result = _create_managed(
        client,
        superuser_token_headers,
        target_user_ids=[user_a["id"], user_b["id"], ghost_id],
        credential_type="openai",
        name="Multi-User OpenAI Key",
        api_key="sk-openai-test-multi",
    )

    assert len(result["added"]) == 2
    added_user_ids = {m["user_id"] for m in result["added"]}
    assert user_a["id"] in added_user_ids
    assert user_b["id"] in added_user_ids

    assert len(result["skipped"]) == 1
    skip = result["skipped"][0]
    assert skip["user_id"] == ghost_id
    assert skip["reason"] == "user_not_found"

    # Parent reflects only the successfully provisioned members
    record = result["record"]
    assert record["member_count"] == 2


# ── Scenario 3: set_as_default provisioning ───────────────────────────────────


def test_create_managed_set_as_default(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    set_as_default=True wires the child as the owner's default for the type.
      1. Provision with set_as_default=True → child is_default=True.
      2. Provision a second parent for same type without set_as_default → first
         stays default (the 'one default per type' rule is enforced per-user,
         the second row is not auto-defaulted).
    """
    target = create_random_user(client)
    target_id = target["id"]
    target_headers = user_authentication_headers(
        client=client, email=target["email"], password=target["_password"]
    )

    # ── Phase 1: Provision with set_as_default=True ───────────────────────────
    result = _create_managed(
        client,
        superuser_token_headers,
        target_user_ids=[target_id],
        credential_type="minimax",
        name="Minimax Default Key",
        api_key="mm-test-default",
        set_as_default=True,
    )
    child_1_id = result["added"][0]["child_credential_id"]

    # Child is default for the target user
    assert get_ai_credential(client, target_headers, child_1_id)["is_default"] is True

    # ── Phase 2: Second parent for same type, no set_as_default ──────────────
    result2 = _create_managed(
        client,
        superuser_token_headers,
        target_user_ids=[target_id],
        credential_type="minimax",
        name="Minimax Key 2",
        api_key="mm-test-no-default",
        set_as_default=False,
    )
    child_2_id = result2["added"][0]["child_credential_id"]

    # First is still default; second is not
    assert get_ai_credential(client, target_headers, child_1_id)["is_default"] is True
    assert get_ai_credential(client, target_headers, child_2_id)["is_default"] is False


# ── Scenario 4: set_user_sdk_defaults provisioning ───────────────────────────


def test_create_managed_set_user_sdk_defaults(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    set_user_sdk_defaults=True wires the target's default_sdk_* profile fields.
    Verified indirectly via GET /users/me.
    """
    target = create_random_user(client)
    target_id = target["id"]
    target_headers = user_authentication_headers(
        client=client, email=target["email"], password=target["_password"]
    )

    result = _create_managed(
        client,
        superuser_token_headers,
        target_user_ids=[target_id],
        credential_type="anthropic",
        name="SDK Defaults Anthropic Key",
        api_key="sk-ant-sdk-defaults",
        set_as_default=True,
        set_user_sdk_defaults=True,
        sdk_default_modes=["conversation", "building"],
    )
    child_id = result["added"][0]["child_credential_id"]

    me = _get_user_me(client, target_headers)
    assert me["default_sdk_conversation"] == "claude-code/anthropic"
    assert me["default_sdk_building"] == "claude-code/anthropic"
    assert me["default_ai_credential_conversation_id"] == child_id
    assert me["default_ai_credential_building_id"] == child_id


# ── Scenario 5: List fleet-wide and with filters ──────────────────────────────


def test_list_managed_fleet_wide_and_filtered(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    List managed credential parent records:
      1. Create 2 parent records targeting different users.
      2. GET /admin/llm-providers/ returns both (fleet-wide).
      3. GET /admin/llm-providers/?target_user_id=<user_a> returns only user_a's parent.
    """
    user_a = create_random_user(client)
    user_b = create_random_user(client)

    res_a = _create_managed(
        client,
        superuser_token_headers,
        target_user_ids=[user_a["id"]],
        credential_type="anthropic",
        name="Fleet A",
        api_key="sk-ant-fleet-a",
    )
    res_b = _create_managed(
        client,
        superuser_token_headers,
        target_user_ids=[user_b["id"]],
        credential_type="openai",
        name="Fleet B",
        api_key="sk-openai-fleet-b",
    )

    parent_a_id = res_a["record"]["id"]
    parent_b_id = res_b["record"]["id"]

    # ── Fleet-wide list contains both ─────────────────────────────────────────
    all_parents = _list_managed(client, superuser_token_headers)
    all_ids = [p["id"] for p in all_parents]
    assert parent_a_id in all_ids
    assert parent_b_id in all_ids

    # ── Filtered by target_user_id → only user_a's parent ────────────────────
    filtered = _list_managed(
        client, superuser_token_headers, target_user_id=user_a["id"]
    )
    filtered_ids = [p["id"] for p in filtered]
    assert parent_a_id in filtered_ids
    assert parent_b_id not in filtered_ids


# ── Scenario 6: Reconcile on PATCH — add/remove/update members ───────────────


def test_patch_managed_reconcile_add_and_remove(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Reconcile on PATCH:
      1. Create parent for user_a only.
      2. PATCH with target_user_ids=[user_a, user_b] → added=[user_b], removed=[].
      3. PATCH with target_user_ids=[user_b] → removed=[user_a's id], added=[].
      4. user_a's child is gone from their AI-credentials listing.
      5. user_b's child still exists.
    """
    user_a = create_random_user(client)
    user_b = create_random_user(client)
    user_a_headers = user_authentication_headers(
        client=client, email=user_a["email"], password=user_a["_password"]
    )
    user_b_headers = user_authentication_headers(
        client=client, email=user_b["email"], password=user_b["_password"]
    )

    # ── Phase 1: Create for user_a ────────────────────────────────────────────
    result = _create_managed(
        client,
        superuser_token_headers,
        target_user_ids=[user_a["id"]],
        credential_type="anthropic",
        name="Reconcile Test Key",
        api_key="sk-ant-reconcile-test",
    )
    parent_id = result["record"]["id"]
    child_a_id = result["added"][0]["child_credential_id"]

    # ── Phase 2: Add user_b ───────────────────────────────────────────────────
    patch_result = _update_managed(
        client,
        superuser_token_headers,
        parent_id,
        target_user_ids=[user_a["id"], user_b["id"]],
    )
    assert len(patch_result["added"]) == 1
    assert patch_result["added"][0]["user_id"] == user_b["id"]
    assert patch_result["removed"] == []
    child_b_id = patch_result["added"][0]["child_credential_id"]

    # Parent now reflects 2 members
    parent = _get_managed(client, superuser_token_headers, parent_id)
    assert parent["member_count"] == 2
    member_user_ids = {m["user_id"] for m in parent["members"]}
    assert user_a["id"] in member_user_ids
    assert user_b["id"] in member_user_ids

    # ── Phase 3: Remove user_a ────────────────────────────────────────────────
    patch_result2 = _update_managed(
        client,
        superuser_token_headers,
        parent_id,
        target_user_ids=[user_b["id"]],
    )
    assert len(patch_result2["removed"]) == 1
    assert patch_result2["removed"][0] == user_a["id"]
    assert patch_result2["added"] == []

    # ── Phase 4: user_a's child is gone ──────────────────────────────────────
    user_a_creds = list_ai_credentials(client, user_a_headers)
    user_a_ids = [c["id"] for c in user_a_creds["data"]]
    assert child_a_id not in user_a_ids

    # ── Phase 5: user_b's child still exists ─────────────────────────────────
    user_b_cred = get_ai_credential(client, user_b_headers, child_b_id)
    assert user_b_cred["is_admin_managed"] is True


# ── Scenario 7: PATCH with no api_key still adds new members ─────────────────


def test_patch_add_member_without_retyping_key(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Adding a member via PATCH without specifying api_key:
      - The stored parent key is used to provision the new child.
      - The child appears in the new member's AI-credentials listing.
    """
    user_a = create_random_user(client)
    user_b = create_random_user(client)
    user_b_headers = user_authentication_headers(
        client=client, email=user_b["email"], password=user_b["_password"]
    )

    # Create parent for user_a with a stored key
    result = _create_managed(
        client,
        superuser_token_headers,
        target_user_ids=[user_a["id"]],
        credential_type="anthropic",
        name="Key-Less Add Test",
        api_key="sk-ant-stored-key",
    )
    parent_id = result["record"]["id"]

    # PATCH: add user_b WITHOUT providing api_key
    patch_result = _update_managed(
        client,
        superuser_token_headers,
        parent_id,
        target_user_ids=[user_a["id"], user_b["id"]],
        # api_key intentionally omitted
    )
    assert len(patch_result["added"]) == 1
    assert patch_result["added"][0]["user_id"] == user_b["id"]
    child_b_id = patch_result["added"][0]["child_credential_id"]

    # user_b can see and access their newly provisioned credential
    user_b_cred = get_ai_credential(client, user_b_headers, child_b_id)
    assert user_b_cred["is_admin_managed"] is True
    assert user_b_cred["has_api_key"] is True


# ── Scenario 8: Idempotent PATCH ─────────────────────────────────────────────


def test_patch_managed_idempotent(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Idempotency: PATCH with identical target_user_ids and no changed fields
    → added=[], removed=[], updated=[], updated_count=0.
    """
    target = create_random_user(client)

    result = _create_managed(
        client,
        superuser_token_headers,
        target_user_ids=[target["id"]],
        credential_type="anthropic",
        name="Idempotency Test",
        api_key="sk-ant-idempotent",
    )
    parent_id = result["record"]["id"]

    # PATCH with same target set, no field changes
    patch_result = _update_managed(
        client,
        superuser_token_headers,
        parent_id,
        target_user_ids=[target["id"]],
    )
    assert patch_result["added"] == []
    assert patch_result["removed"] == []
    assert patch_result["updated"] == []
    assert patch_result["updated_count"] == 0


# ── Scenario 9: Key rotation writes through to existing children ──────────────


def test_patch_key_rotation(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    PATCH with api_key rotates the stored parent key and write-through to all
    existing children. We verify indirectly: the updated list is non-empty and
    the child credential still exists (i.e. the rotation did not delete it).
    The raw key is never readable from the API, so we assert absence of key
    material and that the credential remains functional.
    """
    target = create_random_user(client)
    target_headers = user_authentication_headers(
        client=client, email=target["email"], password=target["_password"]
    )

    result = _create_managed(
        client,
        superuser_token_headers,
        target_user_ids=[target["id"]],
        credential_type="anthropic",
        name="Key Rotation Test",
        api_key="sk-ant-original-key",
    )
    parent_id = result["record"]["id"]
    child_id = result["added"][0]["child_credential_id"]

    # Rotate the key
    patch_result = _update_managed(
        client,
        superuser_token_headers,
        parent_id,
        api_key="sk-ant-rotated-key",
    )
    # The existing member was updated (key write-through)
    assert patch_result["updated_count"] >= 1

    # Child still accessible to the user
    child_cred = get_ai_credential(client, target_headers, child_id)
    assert child_cred["id"] == child_id
    assert child_cred["has_api_key"] is True
    assert "api_key" not in child_cred
    assert "encrypted_data" not in child_cred


# ── Scenario 10: Admin set-default endpoint ───────────────────────────────────


def test_admin_set_managed_default_endpoint(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    POST /admin/llm-providers/{id}/set-default:
      1. Provision without set_as_default.
      2. Call set-default → parent.set_as_default=True; every child becomes default.
      3. Target user sees the child as their default.
    """
    target = create_random_user(client)
    target_id = target["id"]
    target_headers = user_authentication_headers(
        client=client, email=target["email"], password=target["_password"]
    )

    result = _create_managed(
        client,
        superuser_token_headers,
        target_user_ids=[target_id],
        credential_type="anthropic",
        name="Set-Default Later",
        api_key="sk-ant-set-default-later",
        set_as_default=False,
    )
    parent_id = result["record"]["id"]
    child_id = result["added"][0]["child_credential_id"]

    # Not default yet
    assert get_ai_credential(client, target_headers, child_id)["is_default"] is False

    # Admin triggers set-default
    set_result = _set_managed_default(client, superuser_token_headers, parent_id)
    assert set_result["id"] == parent_id
    assert set_result["set_as_default"] is True

    # Target user's child is now default
    assert get_ai_credential(client, target_headers, child_id)["is_default"] is True


# ── Scenario 11: Delete parent removes all children ──────────────────────────


def test_delete_managed_removes_children(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    DELETE /admin/llm-providers/{id}:
      1. Create parent with 2 target users.
      2. Delete the parent → 200 with removal result.
      3. Both children are gone from their respective user listings.
      4. Admin GET on the deleted parent → 404.
    """
    user_a = create_random_user(client)
    user_b = create_random_user(client)
    user_a_headers = user_authentication_headers(
        client=client, email=user_a["email"], password=user_a["_password"]
    )
    user_b_headers = user_authentication_headers(
        client=client, email=user_b["email"], password=user_b["_password"]
    )

    result = _create_managed(
        client,
        superuser_token_headers,
        target_user_ids=[user_a["id"], user_b["id"]],
        credential_type="anthropic",
        name="Delete Test Key",
        api_key="sk-ant-delete-test",
    )
    parent_id = result["record"]["id"]
    child_a_id = next(
        m["child_credential_id"]
        for m in result["added"]
        if m["user_id"] == user_a["id"]
    )
    child_b_id = next(
        m["child_credential_id"]
        for m in result["added"]
        if m["user_id"] == user_b["id"]
    )

    # ── Delete ────────────────────────────────────────────────────────────────
    del_response = _delete_managed(client, superuser_token_headers, parent_id)
    assert "message" in del_response

    # ── Parent gone from admin list ───────────────────────────────────────────
    _get_managed(client, superuser_token_headers, parent_id, expected_status=404)

    # ── Children gone from user listings ─────────────────────────────────────
    user_a_creds = list_ai_credentials(client, user_a_headers)
    assert child_a_id not in [c["id"] for c in user_a_creds["data"]]

    user_b_creds = list_ai_credentials(client, user_b_headers)
    assert child_b_id not in [c["id"] for c in user_b_creds["data"]]


# ── Scenario 12: Auth guard — non-superuser gets 403 everywhere ───────────────


def test_non_superuser_gets_403_on_all_routes(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Non-superuser hitting any /admin/llm-providers/* route → 403.
    Auth gate fires before DB lookup so we can use a ghost id for path params.
    """
    regular = create_random_user(client)
    regular_headers = user_authentication_headers(
        client=client, email=regular["email"], password=regular["_password"]
    )
    ghost = str(uuid.uuid4())

    # POST /admin/llm-providers/
    r = client.post(
        _ADMIN_BASE + "/",
        headers=regular_headers,
        json={
            "name": "Forbidden",
            "type": "anthropic",
            "api_key": "sk-ant-forbidden",
            "target_user_ids": [regular["id"]],
        },
    )
    assert r.status_code == 403

    # GET /admin/llm-providers/
    assert client.get(_ADMIN_BASE + "/", headers=regular_headers).status_code == 403

    # GET /admin/llm-providers/{id}
    assert (
        client.get(f"{_ADMIN_BASE}/{ghost}", headers=regular_headers).status_code == 403
    )

    # PATCH /admin/llm-providers/{id}
    assert (
        client.patch(
            f"{_ADMIN_BASE}/{ghost}",
            headers=regular_headers,
            json={"name": "x"},
        ).status_code
        == 403
    )

    # DELETE /admin/llm-providers/{id}
    assert (
        client.delete(f"{_ADMIN_BASE}/{ghost}", headers=regular_headers).status_code == 403
    )

    # POST /admin/llm-providers/{id}/set-default
    assert (
        client.post(
            f"{_ADMIN_BASE}/{ghost}/set-default", headers=regular_headers
        ).status_code
        == 403
    )

    # POST /admin/llm-providers/test-connection
    assert (
        client.post(
            f"{_ADMIN_BASE}/test-connection",
            headers=regular_headers,
            json={"type": "anthropic", "api_key": "sk-ant-test"},
        ).status_code
        == 403
    )


# ── Scenario 13: User-facing read-only guard ──────────────────────────────────


def test_user_cannot_patch_or_delete_admin_managed_child(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Read-only guard on user-facing /ai-credentials/* routes:
      1. Admin provisions a child for the target user.
      2. Target PATCH /ai-credentials/{child_id} → 403.
      3. Target DELETE /ai-credentials/{child_id} → 403.
      4. Credential is still there, unchanged.
    """
    target = create_random_user(client)
    target_id = target["id"]
    target_headers = user_authentication_headers(
        client=client, email=target["email"], password=target["_password"]
    )

    result = _create_managed(
        client,
        superuser_token_headers,
        target_user_ids=[target_id],
        credential_type="anthropic",
        name="Immutable Child",
        api_key="sk-ant-immutable-child",
    )
    child_id = result["added"][0]["child_credential_id"]

    # PATCH → 403
    r = client.patch(
        f"{_CRED_BASE}/{child_id}",
        headers=target_headers,
        json={"name": "Override Attempt"},
    )
    assert r.status_code == 403

    # DELETE → 403
    r = client.delete(f"{_CRED_BASE}/{child_id}", headers=target_headers)
    assert r.status_code == 403

    # Credential unchanged
    still_there = get_ai_credential(client, target_headers, child_id)
    assert still_there["name"] == "Immutable Child"
    assert still_there["is_admin_managed"] is True


# ── Scenario 14: User CAN set an admin-managed child as default ───────────────


def test_user_can_set_admin_managed_child_as_default(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    OQ-8: Users are allowed to set an admin-managed credential as their default
    (read-only use of the credential, not a mutation).
    """
    target = create_random_user(client)
    target_id = target["id"]
    target_headers = user_authentication_headers(
        client=client, email=target["email"], password=target["_password"]
    )

    result = _create_managed(
        client,
        superuser_token_headers,
        target_user_ids=[target_id],
        credential_type="anthropic",
        name="User Can Default Me",
        api_key="sk-ant-user-default",
        set_as_default=False,
    )
    child_id = result["added"][0]["child_credential_id"]

    # Not default yet
    assert get_ai_credential(client, target_headers, child_id)["is_default"] is False

    # User sets it as default
    set_result = set_ai_credential_default(client, target_headers, child_id)
    assert set_result["is_default"] is True
    assert set_result["is_admin_managed"] is True


# ── Scenario 15: managed_by_id projection boundary ────────────────────────────


def test_managed_by_id_in_admin_response_but_not_user_response(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    managed_by_id is present in admin responses (GET /admin/llm-providers/{id}
    and the reconcile result.record) but MUST NOT appear in user-facing
    /ai-credentials/ GET or LIST responses.
    """
    target = create_random_user(client)
    target_id = target["id"]
    target_headers = user_authentication_headers(
        client=client, email=target["email"], password=target["_password"]
    )

    result = _create_managed(
        client,
        superuser_token_headers,
        target_user_ids=[target_id],
        credential_type="openai",
        name="Projection Test",
        api_key="sk-openai-projection",
    )
    parent_id = result["record"]["id"]
    child_id = result["added"][0]["child_credential_id"]

    # Admin reconcile result.record has managed_by_id
    assert result["record"]["managed_by_id"] is not None

    # Admin GET /admin/llm-providers/{id} has managed_by_id
    admin_get = _get_managed(client, superuser_token_headers, parent_id)
    assert "managed_by_id" in admin_get
    assert admin_get["managed_by_id"] is not None

    # User GET /ai-credentials/{id} has NO managed_by_id
    user_get = get_ai_credential(client, target_headers, child_id)
    assert "managed_by_id" not in user_get

    # User LIST /ai-credentials/ items have NO managed_by_id
    user_list = list_ai_credentials(client, target_headers)
    for item in user_list["data"]:
        assert "managed_by_id" not in item


# ── Scenario 16: No key material in any response ─────────────────────────────


def test_no_key_material_in_any_response(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    ManagedAICredentialPublic, ManagedAICredentialReconcileResult, and
    child AICredential responses must never expose encrypted_data or api_key.
    """
    target = create_random_user(client)
    sentinel = "sk-ant-api03-DO-NOT-LEAK-THIS-KEY"

    result = _create_managed(
        client,
        superuser_token_headers,
        target_user_ids=[target["id"]],
        credential_type="anthropic",
        name="No Key Leak Test",
        api_key=sentinel,
    )

    # Reconcile result must not contain sentinel anywhere
    _assert_no_key_material(result, sentinel)

    # Parent record
    _assert_no_key_material(result["record"], sentinel)

    # Admin GET of parent
    parent_get = _get_managed(
        client, superuser_token_headers, result["record"]["id"]
    )
    _assert_no_key_material(parent_get, sentinel)

    # Admin LIST
    all_parents = _list_managed(client, superuser_token_headers)
    for p in all_parents:
        _assert_no_key_material(p, sentinel)


# ── Scenario 17: SecurityEvent rows contain no key material ──────────────────


def test_security_events_contain_no_key_material(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    SecurityEvent rows emitted during provisioning must contain ids/counts but
    NOT raw api_key bytes.

    Per-child events (admin.ai_credential.provision) are scoped to the target
    user; the parent batch event (admin.managed_ai_credential.create) is scoped
    to the admin.
    """
    target = create_random_user(client)
    target_headers = user_authentication_headers(
        client=client, email=target["email"], password=target["_password"]
    )
    sentinel = "sk-ant-api03-sentinel-DO-NOT-LOG"

    _create_managed(
        client,
        superuser_token_headers,
        target_user_ids=[target["id"]],
        credential_type="anthropic",
        name="Audit Key",
        api_key=sentinel,
    )

    # Admin batch event: admin.managed_ai_credential.create
    batch_events = _list_security_events(
        client,
        superuser_token_headers,
        event_type="admin.managed_ai_credential.create",
    )
    assert len(batch_events) >= 1

    # Per-child provision event: admin.ai_credential.provision (target user's events)
    per_row_events = _list_security_events(
        client,
        target_headers,
        event_type="admin.ai_credential.provision",
    )
    assert len(per_row_events) >= 1

    for event in batch_events + per_row_events:
        details_text = str(event.get("details", ""))
        assert sentinel not in details_text, (
            f"Raw api_key leaked in security event: {details_text}"
        )
        assert "api_key" not in details_text
        assert "encrypted_data" not in details_text


# ── Scenario 18: Inactive user skipped ───────────────────────────────────────


def test_inactive_user_is_skipped(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Provisioning for an inactive target lands in result.skipped with
    reason='user_inactive'. Valid targets still get provisioned.
    """
    inactive_user = create_random_user(client)
    inactive_id = inactive_user["id"]

    # Deactivate via admin route
    r = client.patch(
        f"{settings.API_V1_STR}/users/{inactive_id}",
        headers=superuser_token_headers,
        json={"is_active": False},
    )
    assert r.status_code == 200, f"Deactivate failed: {r.text}"

    valid_user = create_random_user(client)

    result = _create_managed(
        client,
        superuser_token_headers,
        target_user_ids=[inactive_id, valid_user["id"]],
        credential_type="google",
        name="Inactive Skip Test",
        api_key="aig-test-inactive-skip",
    )

    added_user_ids = {m["user_id"] for m in result["added"]}
    skipped_user_ids = {s["user_id"] for s in result["skipped"]}

    assert valid_user["id"] in added_user_ids
    assert inactive_id in skipped_user_ids

    inactive_skip = next(
        s for s in result["skipped"] if s["user_id"] == inactive_id
    )
    assert inactive_skip["reason"] == "user_inactive"


# ── Scenario 19: Per-type validation — openai_compatible ─────────────────────


def test_openai_compatible_missing_base_url_fails(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """openai_compatible without base_url → 400."""
    target = create_random_user(client)
    _create_managed(
        client,
        superuser_token_headers,
        target_user_ids=[target["id"]],
        credential_type="openai_compatible",
        name="Missing base_url",
        api_key="sk-compat-no-base",
        model="gpt-4o",
        expected_status=400,
    )


def test_openai_compatible_missing_model_fails(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """openai_compatible without model → 400."""
    target = create_random_user(client)
    _create_managed(
        client,
        superuser_token_headers,
        target_user_ids=[target["id"]],
        credential_type="openai_compatible",
        name="Missing model",
        api_key="sk-compat-no-model",
        base_url="https://api.example.com/v1",
        expected_status=400,
    )


def test_openai_compatible_valid_succeeds(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """openai_compatible with both base_url and model → 200."""
    target = create_random_user(client)
    target_id = target["id"]
    target_headers = user_authentication_headers(
        client=client, email=target["email"], password=target["_password"]
    )

    result = _create_managed(
        client,
        superuser_token_headers,
        target_user_ids=[target_id],
        credential_type="openai_compatible",
        name="Valid OAI Compat",
        api_key="sk-compat-valid",
        base_url="https://api.example.com/v1",
        model="gpt-4o",
    )
    assert len(result["added"]) == 1
    record = result["record"]
    assert record["type"] == "openai_compatible"
    assert record["base_url"] == "https://api.example.com/v1"
    assert record["model"] == "gpt-4o"

    # Child also reflects base_url and model
    child_id = result["added"][0]["child_credential_id"]
    child = get_ai_credential(client, target_headers, child_id)
    assert child["base_url"] == "https://api.example.com/v1"
    assert child["model"] == "gpt-4o"


# ── Scenario 20: 404 on non-managed / ghost IDs ──────────────────────────────


def test_get_update_delete_404_for_unknown_or_non_managed(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Admin routes return 404 for:
      - Ghost UUIDs that don't exist.
      - A self-created (non-admin-managed) credential id (the admin surface
        only exposes parent records, not per-user child rows).
    """
    # Ghost id
    ghost = str(uuid.uuid4())
    _get_managed(client, superuser_token_headers, ghost, expected_status=404)
    _update_managed(
        client, superuser_token_headers, ghost, expected_status=404, name="x"
    )
    _delete_managed(client, superuser_token_headers, ghost, expected_status=404)
    _set_managed_default(client, superuser_token_headers, ghost, expected_status=404)

    # Self-created (non-admin-managed) credential id — these are AICredential rows,
    # not ManagedAICredential parent rows, so the admin surface can't find them.
    self_cred = create_random_ai_credential(client, superuser_token_headers)
    _get_managed(
        client, superuser_token_headers, self_cred["id"], expected_status=404
    )
    _update_managed(
        client,
        superuser_token_headers,
        self_cred["id"],
        expected_status=404,
        name="hack",
    )
    _delete_managed(
        client, superuser_token_headers, self_cred["id"], expected_status=404
    )


# ── Scenario 21: PATCH name update propagates to members ─────────────────────


def test_patch_name_update_propagates_to_children(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    PATCH with a new name:
      1. Parent name updates.
      2. Existing child names are updated via write-through.
      3. result.updated contains the affected member.
    """
    target = create_random_user(client)
    target_id = target["id"]
    target_headers = user_authentication_headers(
        client=client, email=target["email"], password=target["_password"]
    )

    result = _create_managed(
        client,
        superuser_token_headers,
        target_user_ids=[target_id],
        credential_type="anthropic",
        name="Original Name",
        api_key="sk-ant-name-update-test",
    )
    parent_id = result["record"]["id"]
    child_id = result["added"][0]["child_credential_id"]

    # Rename via PATCH
    patch_result = _update_managed(
        client,
        superuser_token_headers,
        parent_id,
        name="Updated Name",
    )
    assert patch_result["record"]["name"] == "Updated Name"
    # The existing member was updated (name write-through)
    assert patch_result["updated_count"] >= 1
    updated_child_ids = [m["child_credential_id"] for m in patch_result["updated"]]
    assert child_id in updated_child_ids

    # Child reflects the new name
    child_cred = get_ai_credential(client, target_headers, child_id)
    assert child_cred["name"] == "Updated Name"


# ── Scenario 12: Admin-curated model list (admin_curated_model_list) ──────────


def _create_managed_with_curation(
    client: TestClient,
    headers: dict,
    target_user_ids: list[str],
    name: str,
    api_key: str,
    default_model: str | None = None,
    available_models: list[str] | None = None,
) -> dict:
    """POST /admin/llm-providers/ carrying the curated-model fields."""
    payload: dict = {
        "name": name,
        "type": "anthropic",
        "api_key": api_key,
        "target_user_ids": target_user_ids,
    }
    if default_model is not None:
        payload["default_model"] = default_model
    if available_models is not None:
        payload["available_models"] = available_models
    r = client.post(_ADMIN_BASE + "/", headers=headers, json=payload)
    assert r.status_code == 200, f"create with curation failed: {r.text}"
    return r.json()


def test_curation_create_writes_through_to_child(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Create with default_model + available_models reconciles both onto the
    child and projects them read-only on the child's AICredentialPublic."""
    target = create_random_user(client)
    target_headers = user_authentication_headers(
        client=client, email=target["email"], password=target["_password"]
    )

    result = _create_managed_with_curation(
        client,
        superuser_token_headers,
        target_user_ids=[target["id"]],
        name="Curated Create",
        api_key="sk-ant-curated-create",
        default_model="claude-sonnet-4-6",
        available_models=["claude-sonnet-4-6", "claude-haiku-4-5"],
    )
    # Parent projection carries the curated fields.
    assert result["record"]["default_model"] == "claude-sonnet-4-6"
    assert result["record"]["available_models"] == [
        "claude-sonnet-4-6",
        "claude-haiku-4-5",
    ]
    child_id = result["added"][0]["child_credential_id"]

    # Child (owner-facing) reflects the curated fields read-only.
    child = get_ai_credential(client, target_headers, child_id)
    assert child["default_model"] == "claude-sonnet-4-6"
    assert child["available_models"] == ["claude-sonnet-4-6", "claude-haiku-4-5"]


def test_curation_input_is_normalized(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """provider/ prefixes are stripped, blanks dropped, duplicates removed,
    entries trimmed — on the way in."""
    target = create_random_user(client)

    result = _create_managed_with_curation(
        client,
        superuser_token_headers,
        target_user_ids=[target["id"]],
        name="Curated Normalize",
        api_key="sk-ant-curated-normalize",
        default_model="anthropic/claude-sonnet-4-6",
        available_models=[
            "anthropic/claude-sonnet-4-6",
            "  claude-haiku-4-5  ",
            "claude-sonnet-4-6",  # dup after strip
            "",  # blank dropped
        ],
    )
    assert result["record"]["default_model"] == "claude-sonnet-4-6"
    assert result["record"]["available_models"] == [
        "claude-sonnet-4-6",
        "claude-haiku-4-5",
    ]


def test_curation_patch_idempotent_on_equal_values(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Re-sending identical curated values does NOT flag the child as updated."""
    target = create_random_user(client)

    result = _create_managed_with_curation(
        client,
        superuser_token_headers,
        target_user_ids=[target["id"]],
        name="Curated Idempotent",
        api_key="sk-ant-curated-idem",
        default_model="claude-sonnet-4-6",
        available_models=["claude-sonnet-4-6"],
    )
    parent_id = result["record"]["id"]

    patch_result = _update_managed(
        client,
        superuser_token_headers,
        parent_id,
        target_user_ids=[target["id"]],
        default_model="claude-sonnet-4-6",
        available_models=["claude-sonnet-4-6"],
    )
    assert patch_result["updated"] == []
    assert patch_result["updated_count"] == 0


def test_curation_patch_empty_list_clears_none_leaves_unchanged(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """available_models: [] clears curation; omitting it (None) leaves it
    unchanged. default_model omitted (None) is left unchanged."""
    target = create_random_user(client)
    target_headers = user_authentication_headers(
        client=client, email=target["email"], password=target["_password"]
    )

    result = _create_managed_with_curation(
        client,
        superuser_token_headers,
        target_user_ids=[target["id"]],
        name="Curated Clear",
        api_key="sk-ant-curated-clear",
        default_model="claude-sonnet-4-6",
        available_models=["claude-sonnet-4-6", "claude-haiku-4-5"],
    )
    parent_id = result["record"]["id"]
    child_id = result["added"][0]["child_credential_id"]

    # PATCH omitting both curated fields → unchanged (no curated diff). The
    # member must NOT be flagged updated by the curated write-through.
    patch_unchanged = _update_managed(
        client,
        superuser_token_headers,
        parent_id,
        target_user_ids=[target["id"]],
    )
    assert patch_unchanged["updated"] == []
    child = get_ai_credential(client, target_headers, child_id)
    assert child["available_models"] == ["claude-sonnet-4-6", "claude-haiku-4-5"]
    assert child["default_model"] == "claude-sonnet-4-6"

    # PATCH available_models=[] → explicit clear (child becomes empty list).
    patch_clear = _update_managed(
        client,
        superuser_token_headers,
        parent_id,
        target_user_ids=[target["id"]],
        available_models=[],
    )
    cleared_child_ids = [m["child_credential_id"] for m in patch_clear["updated"]]
    assert child_id in cleared_child_ids
    child = get_ai_credential(client, target_headers, child_id)
    assert child["available_models"] == []
    # default_model left untouched (was not in the PATCH body).
    assert child["default_model"] == "claude-sonnet-4-6"


def test_curation_self_created_credentials_stay_null(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """A normal self-created credential cannot carry curated fields (absent from
    the user input schema) and projects them as null."""
    target = create_random_user(client)
    target_headers = user_authentication_headers(
        client=client, email=target["email"], password=target["_password"]
    )

    # Self-created credential with an attempted curated payload — fields are
    # silently ignored (not in AICredentialCreate).
    r = client.post(
        _CRED_BASE + "/",
        headers=target_headers,
        json={
            "name": "Self Created",
            "type": "anthropic",
            "api_key": "sk-ant-self-created",
            "default_model": "claude-sonnet-4-6",
            "available_models": ["claude-sonnet-4-6"],
        },
    )
    assert r.status_code == 200, r.text
    cred = r.json()
    assert cred.get("default_model") is None
    assert cred.get("available_models") is None
