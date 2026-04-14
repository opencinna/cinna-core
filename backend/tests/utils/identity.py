"""Helper functions for identity API tests."""
import uuid

from fastapi.testclient import TestClient

from app.core.config import settings

_BASE = f"{settings.API_V1_STR}/identity"
_CONTACTS_BASE = f"{settings.API_V1_STR}/users/me/identity-contacts"


# ---------------------------------------------------------------------------
# Binding helpers
# ---------------------------------------------------------------------------


def create_identity_binding(
    client: TestClient,
    token_headers: dict[str, str],
    agent_id: str,
    trigger_prompt: str = "Route to this agent when asked about support.",
    message_patterns: str | None = None,
    session_mode: str = "conversation",
    assigned_user_ids: list[str] | None = None,
    auto_enable: bool = False,
) -> dict:
    """Create an identity binding via POST /identity/bindings/."""
    payload: dict = {
        "agent_id": agent_id,
        "trigger_prompt": trigger_prompt,
        "session_mode": session_mode,
        "assigned_user_ids": assigned_user_ids or [],
        "auto_enable": auto_enable,
    }
    if message_patterns is not None:
        payload["message_patterns"] = message_patterns

    r = client.post(f"{_BASE}/bindings/", headers=token_headers, json=payload)
    assert r.status_code == 200, f"Create binding failed: {r.text}"
    return r.json()


def list_identity_bindings(
    client: TestClient,
    token_headers: dict[str, str],
) -> list[dict]:
    """List identity bindings via GET /identity/bindings/."""
    r = client.get(f"{_BASE}/bindings/", headers=token_headers)
    assert r.status_code == 200, f"List bindings failed: {r.text}"
    return r.json()


def update_identity_binding(
    client: TestClient,
    token_headers: dict[str, str],
    binding_id: str,
    **fields,
) -> dict:
    """Update a binding via PUT /identity/bindings/{id}."""
    r = client.put(f"{_BASE}/bindings/{binding_id}", headers=token_headers, json=fields)
    assert r.status_code == 200, f"Update binding failed: {r.text}"
    return r.json()


def delete_identity_binding(
    client: TestClient,
    token_headers: dict[str, str],
    binding_id: str,
) -> dict:
    """Delete a binding via DELETE /identity/bindings/{id}."""
    r = client.delete(f"{_BASE}/bindings/{binding_id}", headers=token_headers)
    assert r.status_code == 200, f"Delete binding failed: {r.text}"
    return r.json()


# ---------------------------------------------------------------------------
# Assignment helpers
# ---------------------------------------------------------------------------


def assign_users_to_binding(
    client: TestClient,
    token_headers: dict[str, str],
    binding_id: str,
    user_ids: list[str],
) -> list[dict]:
    """Assign users to a binding via POST /identity/bindings/{id}/assignments."""
    r = client.post(
        f"{_BASE}/bindings/{binding_id}/assignments",
        headers=token_headers,
        json=user_ids,
    )
    assert r.status_code == 200, f"Assign users failed: {r.text}"
    return r.json()


def remove_user_from_binding(
    client: TestClient,
    token_headers: dict[str, str],
    binding_id: str,
    user_id: str,
) -> dict:
    """Remove a user assignment via DELETE /identity/bindings/{id}/assignments/{user_id}."""
    r = client.delete(
        f"{_BASE}/bindings/{binding_id}/assignments/{user_id}",
        headers=token_headers,
    )
    assert r.status_code == 200, f"Remove assignment failed: {r.text}"
    return r.json()


# ---------------------------------------------------------------------------
# Summary helper
# ---------------------------------------------------------------------------


def get_identity_summary(
    client: TestClient,
    token_headers: dict[str, str],
) -> list[dict]:
    """Get identity summary via GET /identity/summary/."""
    r = client.get(f"{_BASE}/summary/", headers=token_headers)
    assert r.status_code == 200, f"Get summary failed: {r.text}"
    return r.json()


# ---------------------------------------------------------------------------
# Identity contacts helpers
# ---------------------------------------------------------------------------


def list_identity_contacts(
    client: TestClient,
    token_headers: dict[str, str],
) -> list[dict]:
    """List identity contacts via GET /users/me/identity-contacts/."""
    r = client.get(_CONTACTS_BASE + "/", headers=token_headers)
    assert r.status_code == 200, f"List contacts failed: {r.text}"
    return r.json()


def toggle_identity_contact(
    client: TestClient,
    token_headers: dict[str, str],
    owner_id: str,
    is_enabled: bool,
) -> dict:
    """Toggle identity contact via PATCH /users/me/identity-contacts/{owner_id}."""
    r = client.patch(
        f"{_CONTACTS_BASE}/{owner_id}",
        headers=token_headers,
        json={"is_enabled": is_enabled},
    )
    assert r.status_code == 200, f"Toggle contact failed: {r.text}"
    return r.json()
