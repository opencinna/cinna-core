"""HTTP helpers for the managed AI credential admin surface.

``/admin/llm-providers`` is the superuser CRUD over ``ManagedAICredential``
parent records — the thing an admin configures once and every covered account
then receives a child credential from. Zero-touch onboarding phase 2 turned it
into a *cross-domain* surface: ``tests/api/users/`` now needs to create a
parent record in order to observe what a brand-new account is handed, and
``tests/api/ai_credentials/`` needs the same calls for the uniqueness
scenarios.

There is no ``apply_to_existing`` wrapper here. ``POST
/admin/llm-providers/{id}/apply-to-existing`` was removed as redundant (§5.7,
ai-credential-providers plan): for a provider-owned record it duplicated
``POST /admin/ai-providers/{id}/apply-to-existing``, and for a manual record
it always answered ``candidate_count: 0``. Tests exercising that reconcile
call the surviving provider route instead.

``tests/api/ai_credentials/test_admin_ai_credentials.py`` predates this module
and keeps its own private ``_create_managed`` / ``_update_managed`` copies.
They are deliberately left alone: that file is about the endpoint's own shape
(it asserts specific status codes and error bodies inline), and rewriting 1400
lines of a passing suite to import a helper is churn, not coverage. New files
use these.

Plain API wrappers — no Rule-1 exemptions needed anywhere in here.
"""
from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.utils import random_lower_string

ADMIN_BASE = f"{settings.API_V1_STR}/admin/llm-providers"

# Any syntactically plausible Anthropic key. The parent stores it encrypted and
# never calls the provider with it (that is one of the invariants phase 2 is
# named for), so its only requirement is that it round-trips.
DEFAULT_API_KEY = "sk-ant-test-managed-parent"


def create_managed_credential(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    *,
    name: str | None = None,
    credential_type: str = "anthropic",
    api_key: str = DEFAULT_API_KEY,
    target_user_ids: list[str] | None = None,
    set_as_default: bool = False,
    set_user_sdk_defaults: bool = False,
    sdk_default_modes: list[str] | None = None,
    model_override_conversation: str | None = None,
    model_override_building: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
    expected_status: int = 200,
) -> dict[str, Any]:
    """``POST /admin/llm-providers/`` → ``ManagedAICredentialReconcileResult``.

    ``target_user_ids`` defaults to ``[]`` — an auto-provision-only record
    legitimately starts with no members, which is the shape every
    origin-coverage test uses.

    On a non-200 ``expected_status`` the raw body is returned so the caller can
    assert on the error envelope (the 409 conflict payload, say).

    There is no ``auto_provision_roles`` parameter, and its absence is the
    point: the rule for who automatically receives a key lives on
    ``ai_provider`` now, and ``ManagedAICredentialCreate`` sets
    ``extra="forbid"``, so sending the field here is a 422 rather than a
    silently ignored key. A test that needs auto-provisioning needs a
    provider — ``tests/utils/ai_provider_admin.create_provider_credential``.
    """
    payload: dict[str, Any] = {
        "name": name or f"Managed {random_lower_string()[:8]}",
        "type": credential_type,
        "api_key": api_key,
        "target_user_ids": target_user_ids if target_user_ids is not None else [],
        "set_as_default": set_as_default,
        "set_user_sdk_defaults": set_user_sdk_defaults,
    }
    if sdk_default_modes is not None:
        payload["sdk_default_modes"] = sdk_default_modes
    if model_override_conversation is not None:
        payload["model_override_conversation"] = model_override_conversation
    if model_override_building is not None:
        payload["model_override_building"] = model_override_building
    if base_url is not None:
        payload["base_url"] = base_url
    if model is not None:
        payload["model"] = model

    response = client.post(
        f"{ADMIN_BASE}/", headers=superuser_token_headers, json=payload
    )
    assert response.status_code == expected_status, (
        f"create_managed_credential expected {expected_status}, "
        f"got {response.status_code}: {response.text}"
    )
    return response.json()


def update_managed_credential(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    managed_credential_id: str,
    *,
    force: bool = False,
    expected_status: int = 200,
    **fields: Any,
) -> dict[str, Any]:
    """``PATCH /admin/llm-providers/{id}`` → reconcile result (or error body)."""
    response = client.patch(
        f"{ADMIN_BASE}/{managed_credential_id}",
        headers=superuser_token_headers,
        params={"force": force},
        json=fields,
    )
    assert response.status_code == expected_status, (
        f"update_managed_credential expected {expected_status}, "
        f"got {response.status_code}: {response.text}"
    )
    return response.json()


def get_managed_credential(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    managed_credential_id: str,
) -> dict[str, Any]:
    """``GET /admin/llm-providers/{id}`` → ``ManagedAICredentialPublic``."""
    response = client.get(
        f"{ADMIN_BASE}/{managed_credential_id}", headers=superuser_token_headers
    )
    assert response.status_code == 200, response.text
    return response.json()


def list_managed_credentials(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> list[dict[str, Any]]:
    """``GET /admin/llm-providers/`` → list of parent projections."""
    response = client.get(f"{ADMIN_BASE}/", headers=superuser_token_headers)
    assert response.status_code == 200, response.text
    return response.json()


def member_user_ids(record: dict[str, Any]) -> set[str]:
    """The set of user ids holding a child of this parent record.

    Accepts either a ``ManagedAICredentialPublic`` or a reconcile result
    (which nests the projection under ``record``), because both shapes come
    back from this surface and callers should not have to remember which.
    """
    projection = record.get("record", record)
    return {member["user_id"] for member in projection.get("members", [])}


def member_for(record: dict[str, Any], user_id: str) -> dict[str, Any] | None:
    """One member entry of ``record`` by user id, or ``None``."""
    projection = record.get("record", record)
    for member in projection.get("members", []):
        if member["user_id"] == user_id:
            return member
    return None


PROVIDER_BASE = f"{settings.API_V1_STR}/admin/ai-providers"


def connect_minted_provider(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    *,
    name: str | None = None,
    project_id: str = "proj_test",
) -> str:
    """Connect a ``minted`` provider organisation; return the provider id.

    ``POST /admin/ai-providers/`` writes the provider **and its one managed
    credential** in a single transaction, so there is no second request
    pointing a credential at it — see :func:`configure_minted_record` for the
    other half.

    The secret is never sent anywhere: every test that mints replaces the
    provider's HTTP through the registry override
    (``tests/stubs/key_provisioner_stub.py``), so this only has to round-trip.
    """
    response = client.post(
        f"{PROVIDER_BASE}/",
        headers=superuser_token_headers,
        json={
            "name": name or f"Org {random_lower_string()[:8]}",
            "kind": "minted",
            "type": "openai",
            "secret": "sk-admin-not-a-real-secret",
            "config": {"project_id": project_id},
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["id"]


def configure_minted_record(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    *,
    provider_id: str,
    target_user_ids: list[str] | None = None,
    auto_provision_roles: list[str] | None = None,
    set_as_default: bool = False,
    expected_status: int = 200,
) -> dict[str, Any]:
    """Set the provider's policy, then grant its managed credential's members.

    Two requests instead of one create, because the record already exists:

    * the **policy** (``set_as_default``, ``auto_provision_roles``) belongs to
      the provider — a managed credential is not a factory, and both fields are
      shadowed and read-only on a provider-owned record;
    * the **membership** still belongs to the credential, so
      ``target_user_ids`` goes to ``/admin/llm-providers``.

    The policy is written *before* the members are added, so the wiring a member
    receives is the wiring this call asked for rather than a write-through
    applied a request later.

    Returns the reconcile result — ``{"record": …, "added": …, …}``.
    """
    provider = client.get(
        f"{PROVIDER_BASE}/{provider_id}", headers=superuser_token_headers
    )
    assert provider.status_code == 200, provider.text
    parent_id = provider.json()["owned_credential_id"]
    assert parent_id is not None, (
        "the provider owns no managed credential; POST /admin/ai-providers/ "
        "is supposed to write the pair together"
    )

    policy: dict[str, Any] = {"set_as_default": set_as_default}
    if auto_provision_roles is not None:
        policy["auto_provision_roles"] = auto_provision_roles
    patched = client.patch(
        f"{PROVIDER_BASE}/{provider_id}",
        headers=superuser_token_headers,
        json=policy,
    )
    assert patched.status_code == 200, patched.text

    response = client.patch(
        f"{ADMIN_BASE}/{parent_id}",
        headers=superuser_token_headers,
        json={"target_user_ids": target_user_ids or []},
    )
    assert response.status_code == expected_status, response.text
    return response.json()
