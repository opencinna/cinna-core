"""
Tests for Admin-Provisioned AI Credentials feature.

Covers ``/admin/llm-providers/*`` (superuser-gated CRUD + provisioning) and
the read-only guard on the user-facing ``/ai-credentials/*`` routes.

Scenarios:
  1. Single-user provisioning — row owned by target, is_admin_managed=True,
     managed_by_id=admin, appears in target's own credential list.
  2. N-user provisioning — one row per valid target; invalid targets land in
     ``skipped`` (unknown id, inactive user); call still succeeds for valid targets.
  3. set_as_default provisioning — target's is_default is set; never auto-sets
     two defaults for the same type.
  4. set_user_sdk_defaults provisioning — profile fields wired on the target user.
  5. Admin list_managed fleet-wide and with ?target_user_id= filter.
  6. Admin get/update/delete on a managed row; delete on non-managed row → 404.
  7. Admin set-default on a managed row → succeeds.
  8. Non-superuser hitting any /admin/llm-providers/* route → 403.
  9. User-facing guard: target PATCH/DELETE on admin-managed row → 403.
 10. User CAN set an admin-managed credential as their own default (OQ-8).
 11. User GET sees is_admin_managed=True on the row; managed_by_id NOT in response.
 12. SecurityEvent rows for provision contain ids/counts but NO api_key material.
 13. managed_by_id appears in admin responses (AdminAICredentialPublic) but NOT
     in user-facing AICredentialPublic.
 14. Admin provisioning for openai_compatible without base_url/model → 400.

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


def _provision(
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
    """POST /admin/llm-providers/ and return the JSON response."""
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
        f"Provision returned {r.status_code}: {r.text}"
    )
    return r.json()


def _list_managed(
    client: TestClient,
    headers: dict,
    target_user_id: str | None = None,
) -> list[dict]:
    """GET /admin/llm-providers/ → list of admin-managed credentials."""
    params = {}
    if target_user_id is not None:
        params["target_user_id"] = target_user_id
    r = client.get(_ADMIN_BASE + "/", headers=headers, params=params)
    assert r.status_code == 200, f"list_managed failed: {r.text}"
    return r.json()


def _get_managed(
    client: TestClient, headers: dict, credential_id: str, expected_status: int = 200
) -> dict:
    """GET /admin/llm-providers/{id}."""
    r = client.get(f"{_ADMIN_BASE}/{credential_id}", headers=headers)
    assert r.status_code == expected_status, (
        f"get_managed({credential_id}) returned {r.status_code}: {r.text}"
    )
    return r.json()


def _update_managed(
    client: TestClient,
    headers: dict,
    credential_id: str,
    expected_status: int = 200,
    **fields,
) -> dict:
    """PATCH /admin/llm-providers/{id}."""
    r = client.patch(
        f"{_ADMIN_BASE}/{credential_id}", headers=headers, json=fields
    )
    assert r.status_code == expected_status, (
        f"update_managed returned {r.status_code}: {r.text}"
    )
    return r.json()


def _delete_managed(
    client: TestClient,
    headers: dict,
    credential_id: str,
    force: bool = False,
    expected_status: int = 200,
) -> dict:
    """DELETE /admin/llm-providers/{id}."""
    r = client.delete(
        f"{_ADMIN_BASE}/{credential_id}",
        headers=headers,
        params={"force": force},
    )
    assert r.status_code == expected_status, (
        f"delete_managed returned {r.status_code}: {r.text}"
    )
    return r.json()


def _set_managed_default(
    client: TestClient,
    headers: dict,
    credential_id: str,
    expected_status: int = 200,
) -> dict:
    """POST /admin/llm-providers/{id}/set-default."""
    r = client.post(
        f"{_ADMIN_BASE}/{credential_id}/set-default", headers=headers
    )
    assert r.status_code == expected_status, (
        f"set_managed_default returned {r.status_code}: {r.text}"
    )
    return r.json()


def _get_user_me(client: TestClient, headers: dict) -> dict:
    """GET /users/me to inspect default_sdk_* profile fields."""
    r = client.get(f"{settings.API_V1_STR}/users/me", headers=headers)
    assert r.status_code == 200
    return r.json()


def _list_security_events(
    client: TestClient,
    headers: dict,
    event_type: str | None = None,
) -> list[dict]:
    """GET /security-events/ → list of events (optionally filtered)."""
    params = {}
    if event_type is not None:
        params["event_type"] = event_type
    r = client.get(_SEC_EVENTS_BASE + "/", headers=headers, params=params)
    assert r.status_code == 200, f"list_security_events failed: {r.text}"
    return r.json().get("data", [])


# ── Scenario 1: Single-user provisioning ──────────────────────────────────────


def test_provision_single_user_creates_owned_row(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Single-user provisioning scenario:
      1. Admin provisions an anthropic credential for a target user.
      2. The result includes one created row with is_admin_managed=True.
      3. owner_id == target user id.
      4. managed_by_id is present in the admin response.
      5. Target user can list the credential via /ai-credentials/.
      6. User-facing GET shows is_admin_managed=True.
      7. managed_by_id is NOT in the user-facing AICredentialPublic.
    """
    target = create_random_user(client)
    target_id = target["id"]
    target_headers = user_authentication_headers(
        client=client, email=target["email"], password=target["_password"]
    )

    # ── Phase 1: Admin provisions ─────────────────────────────────────────────
    result = _provision(
        client, superuser_token_headers,
        target_user_ids=[target_id],
        credential_type="anthropic",
        name="Company Anthropic Key",
    )

    # ── Phase 2: Validate provision result ────────────────────────────────────
    assert len(result["created"]) == 1
    assert result["skipped"] == []

    created = result["created"][0]
    cred_id = created["id"]
    assert created["is_admin_managed"] is True
    assert created["owner_id"] == target_id
    assert created["managed_by_id"] is not None  # admin's id, present in admin model
    assert created["type"] == "anthropic"
    assert created["name"] == "Company Anthropic Key"
    # Sensitive data must NOT leak from the admin surface either
    assert "api_key" not in created
    assert "encrypted_data" not in created

    # ── Phase 3: Admin can fetch via get_managed ──────────────────────────────
    fetched_admin = _get_managed(client, superuser_token_headers, cred_id)
    assert fetched_admin["id"] == cred_id
    assert fetched_admin["owner_id"] == target_id
    assert fetched_admin["managed_by_id"] is not None

    # ── Phase 4: Target user sees it in /ai-credentials/ listing ─────────────
    user_list = list_ai_credentials(client, target_headers)
    assert user_list["count"] >= 1
    ids_in_list = [c["id"] for c in user_list["data"]]
    assert cred_id in ids_in_list

    # ── Phase 5: User-facing GET shows is_admin_managed=True ──────────────────
    user_cred = get_ai_credential(client, target_headers, cred_id)
    assert user_cred["is_admin_managed"] is True

    # ── Phase 6: managed_by_id is NOT in user-facing response (OQ-4) ──────────
    assert "managed_by_id" not in user_cred


# ── Scenario 2: N-user provisioning with skipped targets ──────────────────────


def test_provision_n_users_with_skips(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    N-user provisioning scenario:
      1. Admin provisions for 2 valid users + 1 unknown uuid.
      2. 2 rows are created, 1 target is skipped with reason='user_not_found'.
      3. Each created row is owned by its respective target.
    """
    user_a = create_random_user(client)
    user_b = create_random_user(client)
    ghost_id = str(uuid.uuid4())  # does not exist

    result = _provision(
        client, superuser_token_headers,
        target_user_ids=[user_a["id"], user_b["id"], ghost_id],
        credential_type="openai",
        name="Company OpenAI Key",
        api_key="sk-openai-test-batch",
    )

    # ── Validate created rows ─────────────────────────────────────────────────
    assert len(result["created"]) == 2
    created_owner_ids = {c["owner_id"] for c in result["created"]}
    assert user_a["id"] in created_owner_ids
    assert user_b["id"] in created_owner_ids

    for created in result["created"]:
        assert created["is_admin_managed"] is True

    # ── Validate skipped ──────────────────────────────────────────────────────
    assert len(result["skipped"]) == 1
    skip = result["skipped"][0]
    assert skip["user_id"] == ghost_id
    assert skip["reason"] == "user_not_found"


# ── Scenario 3: set_as_default provisioning ───────────────────────────────────


def test_provision_set_as_default(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    set_as_default provisioning:
      1. Admin provisions with set_as_default=True.
      2. Created row has is_default=True for the target.
      3. Provisioning again for same type does NOT auto-default the second one.
    """
    target = create_random_user(client)
    target_id = target["id"]
    target_headers = user_authentication_headers(
        client=client, email=target["email"], password=target["_password"]
    )

    # ── Phase 1: Provision with set_as_default=True ───────────────────────────
    result = _provision(
        client, superuser_token_headers,
        target_user_ids=[target_id],
        credential_type="minimax",
        name="Company MiniMax Key",
        api_key="mm-test-admin-key",
        set_as_default=True,
    )
    assert len(result["created"]) == 1
    cred_1_id = result["created"][0]["id"]

    # ── Phase 2: Row is default for target ────────────────────────────────────
    user_cred = get_ai_credential(client, target_headers, cred_1_id)
    assert user_cred["is_default"] is True

    # ── Phase 3: Provision second credential for same type — no auto-default ──
    result2 = _provision(
        client, superuser_token_headers,
        target_user_ids=[target_id],
        credential_type="minimax",
        name="Company MiniMax Key 2",
        api_key="mm-test-admin-key-2",
        set_as_default=False,
    )
    cred_2_id = result2["created"][0]["id"]

    # First credential is still default; second is not
    assert get_ai_credential(client, target_headers, cred_1_id)["is_default"] is True
    assert get_ai_credential(client, target_headers, cred_2_id)["is_default"] is False


# ── Scenario 4: set_user_sdk_defaults ────────────────────────────────────────


def test_provision_set_user_sdk_defaults(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    set_user_sdk_defaults=True wires the target's default_sdk_*
    and default_ai_credential_*_id profile fields.
    We verify indirectly via GET /users/me (user must read their own profile).
    """
    target = create_random_user(client)
    target_id = target["id"]
    target_headers = user_authentication_headers(
        client=client, email=target["email"], password=target["_password"]
    )

    result = _provision(
        client, superuser_token_headers,
        target_user_ids=[target_id],
        credential_type="anthropic",
        name="SDK Default Anthropic Key",
        api_key="sk-ant-sdk-defaults-test",
        set_as_default=True,
        set_user_sdk_defaults=True,
        sdk_default_modes=["conversation", "building"],
    )

    cred_id = result["created"][0]["id"]

    # Target user's profile should reflect the SDK defaults
    me = _get_user_me(client, target_headers)
    # The sdk defaults must be set to claude-code/anthropic (the engine for anthropic type)
    assert me["default_sdk_conversation"] == "claude-code/anthropic"
    assert me["default_sdk_building"] == "claude-code/anthropic"
    # And the credential ids must point to the provisioned credential
    assert me["default_ai_credential_conversation_id"] == cred_id
    assert me["default_ai_credential_building_id"] == cred_id


# ── Scenario 5: Admin list_managed fleet-wide + filter ────────────────────────


def test_admin_list_managed_fleet_wide_and_filtered(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    list_managed scenario:
      1. Admin provisions for 2 different target users.
      2. GET /admin/llm-providers/ returns both rows (fleet-wide).
      3. GET /admin/llm-providers/?target_user_id=<user_a> returns only user_a's row.
    """
    user_a = create_random_user(client)
    user_b = create_random_user(client)

    res_a = _provision(
        client, superuser_token_headers,
        target_user_ids=[user_a["id"]],
        credential_type="anthropic",
        name="Fleet-wide A",
        api_key="sk-ant-fleet-a",
    )
    res_b = _provision(
        client, superuser_token_headers,
        target_user_ids=[user_b["id"]],
        credential_type="openai",
        name="Fleet-wide B",
        api_key="sk-openai-fleet-b",
    )

    cred_a_id = res_a["created"][0]["id"]
    cred_b_id = res_b["created"][0]["id"]

    # ── Fleet-wide list includes both ─────────────────────────────────────────
    all_managed = _list_managed(client, superuser_token_headers)
    all_ids = [c["id"] for c in all_managed]
    assert cred_a_id in all_ids
    assert cred_b_id in all_ids

    # ── Filtered by target_user_id shows only user_a's row ────────────────────
    filtered = _list_managed(
        client, superuser_token_headers, target_user_id=user_a["id"]
    )
    filtered_ids = [c["id"] for c in filtered]
    assert cred_a_id in filtered_ids
    assert cred_b_id not in filtered_ids


# ── Scenario 6: Admin CRUD on managed rows ────────────────────────────────────


def test_admin_update_and_delete_managed_credential(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Admin update/delete on a managed row:
      1. Admin provisions a credential.
      2. Admin updates its name via PATCH → succeeds.
      3. Admin deletes via DELETE → succeeds.
      4. Admin delete on a NON-admin-managed credential → 404.
      5. Admin update on a NON-admin-managed credential → 404.
    """
    target = create_random_user(client)
    target_id = target["id"]
    target_headers = user_authentication_headers(
        client=client, email=target["email"], password=target["_password"]
    )

    # ── Phase 1: Provision ────────────────────────────────────────────────────
    result = _provision(
        client, superuser_token_headers,
        target_user_ids=[target_id],
        credential_type="anthropic",
        name="Original Name",
        api_key="sk-ant-admin-crud-test",
    )
    cred_id = result["created"][0]["id"]

    # ── Phase 2: Admin updates name ───────────────────────────────────────────
    updated = _update_managed(
        client, superuser_token_headers, cred_id, name="Updated by Admin"
    )
    assert updated["name"] == "Updated by Admin"
    assert updated["id"] == cred_id

    # ── Phase 3: Admin deletes the credential ─────────────────────────────────
    _delete_managed(client, superuser_token_headers, cred_id)

    # Confirm gone from admin listing
    all_managed = _list_managed(client, superuser_token_headers)
    all_ids = [c["id"] for c in all_managed]
    assert cred_id not in all_ids

    # ── Phase 4: Self-created (non-admin-managed) credential → 404 for admin ──
    # Target creates their own credential
    self_cred = create_random_ai_credential(client, target_headers)
    self_cred_id = self_cred["id"]

    _update_managed(
        client, superuser_token_headers, self_cred_id, expected_status=404, name="Hack"
    )
    _delete_managed(
        client, superuser_token_headers, self_cred_id, expected_status=404
    )


# ── Scenario 7: Admin set-default on a managed row ───────────────────────────


def test_admin_set_managed_default(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Admin set-default on a managed row:
      1. Admin provisions WITHOUT set_as_default.
      2. Admin calls POST /admin/llm-providers/{id}/set-default → succeeds.
      3. Target user sees is_default=True on that credential.
    """
    target = create_random_user(client)
    target_id = target["id"]
    target_headers = user_authentication_headers(
        client=client, email=target["email"], password=target["_password"]
    )

    result = _provision(
        client, superuser_token_headers,
        target_user_ids=[target_id],
        credential_type="anthropic",
        name="Set Default Later",
        api_key="sk-ant-set-default-test",
        set_as_default=False,
    )
    cred_id = result["created"][0]["id"]

    # Not default yet
    assert get_ai_credential(client, target_headers, cred_id)["is_default"] is False

    # Admin calls set-default
    set_result = _set_managed_default(client, superuser_token_headers, cred_id)
    assert set_result["is_default"] is True

    # Target user sees it as default
    assert get_ai_credential(client, target_headers, cred_id)["is_default"] is True


# ── Scenario 8: Non-superuser access → 403 ────────────────────────────────────


def test_non_superuser_cannot_access_admin_routes(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Non-superuser hitting any /admin/llm-providers/* route → 403.
    """
    regular = create_random_user(client)
    regular_headers = user_authentication_headers(
        client=client, email=regular["email"], password=regular["_password"]
    )

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

    # GET /admin/llm-providers/{id} (ghost id — gate fires before DB lookup)
    ghost = str(uuid.uuid4())
    assert client.get(f"{_ADMIN_BASE}/{ghost}", headers=regular_headers).status_code == 403

    # PATCH /admin/llm-providers/{id}
    r = client.patch(
        f"{_ADMIN_BASE}/{ghost}", headers=regular_headers, json={"name": "x"}
    )
    assert r.status_code == 403

    # DELETE /admin/llm-providers/{id}
    assert client.delete(f"{_ADMIN_BASE}/{ghost}", headers=regular_headers).status_code == 403

    # POST set-default
    assert (
        client.post(f"{_ADMIN_BASE}/{ghost}/set-default", headers=regular_headers).status_code
        == 403
    )


# ── Scenario 9: User-facing read-only guard ───────────────────────────────────


def test_user_cannot_patch_or_delete_admin_managed_credential(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Read-only guard:
      1. Admin provisions a credential for a target user.
      2. Target user tries PATCH → 403 with admin-managed message.
      3. Target user tries DELETE → 403.
    """
    target = create_random_user(client)
    target_id = target["id"]
    target_headers = user_authentication_headers(
        client=client, email=target["email"], password=target["_password"]
    )

    result = _provision(
        client, superuser_token_headers,
        target_user_ids=[target_id],
        credential_type="anthropic",
        name="Immutable Key",
        api_key="sk-ant-immutable",
    )
    cred_id = result["created"][0]["id"]

    # ── Target PATCH → 403 ────────────────────────────────────────────────────
    r = client.patch(
        f"{_CRED_BASE}/{cred_id}",
        headers=target_headers,
        json={"name": "Attempted Override"},
    )
    assert r.status_code == 403

    # ── Target DELETE → 403 ───────────────────────────────────────────────────
    r = client.delete(f"{_CRED_BASE}/{cred_id}", headers=target_headers)
    assert r.status_code == 403

    # Credential still exists and is unchanged
    still_there = get_ai_credential(client, target_headers, cred_id)
    assert still_there["name"] == "Immutable Key"


# ── Scenario 10: User CAN set an admin-managed credential as their own default ─


def test_user_can_set_admin_managed_credential_as_default(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    OQ-8: Setting an admin-managed credential as the user's own default is
    allowed (it's a read-only use of the credential, not a mutation).
    """
    target = create_random_user(client)
    target_id = target["id"]
    target_headers = user_authentication_headers(
        client=client, email=target["email"], password=target["_password"]
    )

    result = _provision(
        client, superuser_token_headers,
        target_user_ids=[target_id],
        credential_type="anthropic",
        name="User Can Default Me",
        api_key="sk-ant-user-default-test",
        set_as_default=False,
    )
    cred_id = result["created"][0]["id"]

    # Not default yet
    assert get_ai_credential(client, target_headers, cred_id)["is_default"] is False

    # User sets it as default via the user-facing endpoint → must succeed
    set_result = set_ai_credential_default(client, target_headers, cred_id)
    assert set_result["is_default"] is True
    assert set_result["is_admin_managed"] is True


# ── Scenario 11: managed_by_id projection boundary ────────────────────────────


def test_managed_by_id_absent_from_user_facing_response(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    OQ-4: ``managed_by_id`` must appear in AdminAICredentialPublic (admin GET)
    but must NOT appear in AICredentialPublic (user-facing GET/LIST).
    """
    target = create_random_user(client)
    target_id = target["id"]
    target_headers = user_authentication_headers(
        client=client, email=target["email"], password=target["_password"]
    )

    result = _provision(
        client, superuser_token_headers,
        target_user_ids=[target_id],
        credential_type="openai",
        name="Projection Test Key",
        api_key="sk-openai-proj-test",
    )
    cred_id = result["created"][0]["id"]

    # Admin GET → managed_by_id present
    admin_resp = _get_managed(client, superuser_token_headers, cred_id)
    assert "managed_by_id" in admin_resp
    assert admin_resp["managed_by_id"] is not None

    # User GET → managed_by_id absent
    user_resp = get_ai_credential(client, target_headers, cred_id)
    assert "managed_by_id" not in user_resp

    # User LIST → none of the items have managed_by_id
    user_list = list_ai_credentials(client, target_headers)
    for item in user_list["data"]:
        assert "managed_by_id" not in item


# ── Scenario 12: SecurityEvent rows contain ids but NO key material ───────────


def test_provision_security_events_contain_no_key_material(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    SecurityEvent rows written during provisioning must contain credential ids /
    counts but NOT the raw api_key bytes.

    Per-row provision events (event_type="admin.ai_credential.provision") are
    scoped to the TARGET user's id in the security_events table (the route
    emits them with user_id=created.owner_id). The batch summary event
    (event_type="admin.ai_credential.provision_batch") is scoped to the ADMIN.

    We verify the batch event is written for the superuser, and the per-row
    event appears for the target user — both without key material.
    """
    target = create_random_user(client)
    target_headers = user_authentication_headers(
        client=client, email=target["email"], password=target["_password"]
    )
    sentinel_key = "sk-ant-api03-sentinel-key-must-not-leak"

    _provision(
        client, superuser_token_headers,
        target_user_ids=[target["id"]],
        credential_type="anthropic",
        name="Audit Test Key",
        api_key=sentinel_key,
    )

    # The batch summary event is scoped to the admin (superuser).
    batch_events = _list_security_events(
        client, superuser_token_headers, event_type="admin.ai_credential.provision_batch"
    )
    assert len(batch_events) >= 1, (
        "Expected at least one admin.ai_credential.provision_batch event for the admin"
    )

    # Per-row provision event is scoped to the target user (owner_id).
    per_row_events = _list_security_events(
        client, target_headers, event_type="admin.ai_credential.provision"
    )
    assert len(per_row_events) >= 1, (
        "Expected at least one admin.ai_credential.provision event for the target user"
    )

    # Assert no event details contain the raw api_key
    for event in batch_events + per_row_events:
        details_str = str(event.get("details", ""))
        assert sentinel_key not in details_str, (
            f"Raw api_key leaked in security event details: {details_str}"
        )
        assert "api_key" not in details_str


# ── Scenario 13: openai_compatible validation ──────────────────────────────────


def test_provision_openai_compatible_missing_base_url_fails(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """openai_compatible requires base_url and model — omitting either → 400."""
    target = create_random_user(client)

    # Missing base_url
    _provision(
        client, superuser_token_headers,
        target_user_ids=[target["id"]],
        credential_type="openai_compatible",
        name="Incomplete OAI Compat",
        api_key="sk-compat-key",
        model="gpt-4",
        expected_status=400,
    )

    # Missing model
    _provision(
        client, superuser_token_headers,
        target_user_ids=[target["id"]],
        credential_type="openai_compatible",
        name="Incomplete OAI Compat 2",
        api_key="sk-compat-key",
        base_url="https://api.example.com/v1",
        expected_status=400,
    )


def test_provision_openai_compatible_valid_succeeds(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """openai_compatible with both base_url and model → 200."""
    target = create_random_user(client)
    target_id = target["id"]
    target_headers = user_authentication_headers(
        client=client, email=target["email"], password=target["_password"]
    )

    result = _provision(
        client, superuser_token_headers,
        target_user_ids=[target_id],
        credential_type="openai_compatible",
        name="Valid OAI Compat Key",
        api_key="sk-compat-valid-key",
        base_url="https://api.example.com/v1",
        model="gpt-4o",
    )
    assert len(result["created"]) == 1
    created = result["created"][0]
    assert created["type"] == "openai_compatible"
    assert created["base_url"] == "https://api.example.com/v1"
    assert created["model"] == "gpt-4o"


# ── Scenario 14: Inactive user is skipped ─────────────────────────────────────


def test_provision_inactive_user_is_skipped(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Provisioning for an inactive target user → lands in skipped list.
    """
    inactive_user = create_random_user(client)
    inactive_id = inactive_user["id"]

    # Deactivate via admin endpoint
    r = client.patch(
        f"{settings.API_V1_STR}/users/{inactive_id}",
        headers=superuser_token_headers,
        json={"is_active": False},
    )
    assert r.status_code == 200, f"Deactivate user failed: {r.text}"

    valid_user = create_random_user(client)

    result = _provision(
        client, superuser_token_headers,
        target_user_ids=[inactive_id, valid_user["id"]],
        credential_type="google",
        name="Inactive Skip Test",
        api_key="aig-test-key-inactive",
    )

    created_owner_ids = [c["owner_id"] for c in result["created"]]
    skipped_user_ids = [s["user_id"] for s in result["skipped"]]

    assert inactive_id in skipped_user_ids
    assert valid_user["id"] in created_owner_ids

    inactive_skip = next(s for s in result["skipped"] if s["user_id"] == inactive_id)
    assert inactive_skip["reason"] == "user_inactive"


# ── Scenario 15: Admin get managed returns 404 for non-managed rows ───────────


def test_admin_get_managed_404_for_self_created(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Admin GET /admin/llm-providers/{id} on a self-created (non-admin-managed)
    credential → 404 (the admin surface is limited to is_admin_managed rows).
    """
    # Superuser creates their own credential (self-created, not via admin route)
    self_cred = create_random_ai_credential(client, superuser_token_headers)

    _get_managed(
        client, superuser_token_headers, self_cred["id"], expected_status=404
    )
