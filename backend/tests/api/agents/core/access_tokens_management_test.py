"""
Integration test: access token CRUD lifecycle.

Tests the full user story for managing A2A access tokens:
  1. User creates an agent
  2. User creates access tokens with different modes/scopes
  3. User lists tokens and verifies they appear
  4. User gets individual token details
  5. User renames a token
  6. User revokes a token, verifies it shows as revoked
  7. User restores the revoked token
  8. User deletes a token, verifies it's gone from the list
  9. Another user cannot see or manage tokens they don't own

Only environment adapter is stubbed (via conftest autouse fixtures).
"""
import uuid

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.agent import create_agent_via_api
from tests.utils.background_tasks import drain_tasks
from tests.utils.user import create_random_user, user_authentication_headers


API = settings.API_V1_STR


def _tokens_url(agent_id: str) -> str:
    return f"{API}/agents/{agent_id}/access-tokens/"


def _token_url(agent_id: str, token_id: str) -> str:
    return f"{API}/agents/{agent_id}/access-tokens/{token_id}"


def _create_token(
    client: TestClient,
    headers: dict[str, str],
    agent_id: str,
    name: str,
    mode: str = "conversation",
    scope: str = "limited",
) -> dict:
    """Create a token and return the full response (includes one-time ``token`` field)."""
    r = client.post(
        _tokens_url(agent_id),
        headers=headers,
        json={
            "agent_id": agent_id,
            "name": name,
            "mode": mode,
            "scope": scope,
        },
    )
    assert r.status_code == 200, f"Create token failed: {r.text}"
    return r.json()


def _list_tokens(
    client: TestClient,
    headers: dict[str, str],
    agent_id: str,
) -> list[dict]:
    """List tokens and return the ``data`` array."""
    r = client.get(_tokens_url(agent_id), headers=headers)
    assert r.status_code == 200, f"List tokens failed: {r.text}"
    body = r.json()
    assert "data" in body
    assert "count" in body
    assert body["count"] == len(body["data"])
    return body["data"]


def _get_token(
    client: TestClient,
    headers: dict[str, str],
    agent_id: str,
    token_id: str,
) -> dict:
    """Get a single token by ID."""
    r = client.get(_token_url(agent_id, token_id), headers=headers)
    assert r.status_code == 200, f"Get token failed: {r.text}"
    return r.json()


def _update_token(
    client: TestClient,
    headers: dict[str, str],
    agent_id: str,
    token_id: str,
    **fields,
) -> dict:
    """Update a token (name and/or is_revoked)."""
    r = client.put(
        _token_url(agent_id, token_id),
        headers=headers,
        json=fields,
    )
    assert r.status_code == 200, f"Update token failed: {r.text}"
    return r.json()


def _delete_token(
    client: TestClient,
    headers: dict[str, str],
    agent_id: str,
    token_id: str,
) -> dict:
    """Delete a token."""
    r = client.delete(_token_url(agent_id, token_id), headers=headers)
    assert r.status_code == 200, f"Delete token failed: {r.text}"
    return r.json()


# ── Tests ────────────────────────────────────────────────────────────────


def test_access_token_full_lifecycle(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Full CRUD lifecycle for a single access token:
      1. Create agent
      2. Create token → verify one-time JWT is returned
      3. List tokens → verify it appears
      4. Get token by ID → verify fields
      5. Rename token
      6. Revoke token
      7. Restore (un-revoke) token
      8. Delete token
      9. List tokens → verify it's gone
    """
    # ── Phase 1: Create agent ─────────────────────────────────────────────

    agent = create_agent_via_api(client, superuser_token_headers, name="Token Lifecycle Agent")
    drain_tasks()
    agent_id = agent["id"]

    # ── Phase 2: Create token ─────────────────────────────────────────────

    created = _create_token(
        client, superuser_token_headers, agent_id,
        name="my-first-token",
        mode="conversation",
        scope="limited",
    )

    token_id = created["id"]
    assert "token" in created, "JWT token must be returned on creation"
    assert created["token"].startswith("eyJ"), "Token should be a JWT"
    assert created["name"] == "my-first-token"
    assert created["mode"] == "conversation"
    assert created["scope"] == "limited"
    assert created["is_revoked"] is False
    assert created["token_prefix"] is not None
    assert len(created["token_prefix"]) == 8

    # ── Phase 3: List tokens → token is present ───────────────────────────

    tokens = _list_tokens(client, superuser_token_headers, agent_id)
    assert len(tokens) == 1
    assert tokens[0]["id"] == token_id
    assert tokens[0]["name"] == "my-first-token"
    # List endpoint should NOT expose the raw JWT
    assert "token" not in tokens[0] or tokens[0].get("token") is None

    # ── Phase 4: Get token by ID ──────────────────────────────────────────

    fetched = _get_token(client, superuser_token_headers, agent_id, token_id)
    assert fetched["id"] == token_id
    assert fetched["name"] == "my-first-token"
    assert fetched["mode"] == "conversation"
    assert fetched["scope"] == "limited"
    assert fetched["is_revoked"] is False
    assert fetched["agent_id"] == agent_id

    # ── Phase 5: Rename token ─────────────────────────────────────────────

    updated = _update_token(
        client, superuser_token_headers, agent_id, token_id,
        name="renamed-token",
    )
    assert updated["name"] == "renamed-token"
    assert updated["is_revoked"] is False  # unchanged

    # Verify via GET
    fetched = _get_token(client, superuser_token_headers, agent_id, token_id)
    assert fetched["name"] == "renamed-token"

    # ── Phase 6: Revoke token ─────────────────────────────────────────────

    updated = _update_token(
        client, superuser_token_headers, agent_id, token_id,
        is_revoked=True,
    )
    assert updated["is_revoked"] is True
    assert updated["name"] == "renamed-token"  # name unchanged

    # ── Phase 7: Restore (un-revoke) token ────────────────────────────────

    updated = _update_token(
        client, superuser_token_headers, agent_id, token_id,
        is_revoked=False,
    )
    assert updated["is_revoked"] is False

    # ── Phase 8: Delete token ─────────────────────────────────────────────

    result = _delete_token(client, superuser_token_headers, agent_id, token_id)
    assert "message" in result

    # ── Phase 9: Verify token is gone ─────────────────────────────────────

    tokens = _list_tokens(client, superuser_token_headers, agent_id)
    assert len(tokens) == 0

    # GET by ID should 404
    r = client.get(
        _token_url(agent_id, token_id),
        headers=superuser_token_headers,
    )
    assert r.status_code == 404


def test_multiple_tokens_with_different_modes_and_scopes(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    User creates multiple tokens with different mode/scope combinations,
    lists them, deletes one, and verifies the remaining set.
    """
    # ── Setup ─────────────────────────────────────────────────────────────

    agent = create_agent_via_api(client, superuser_token_headers, name="Multi-Token Agent")
    drain_tasks()
    agent_id = agent["id"]

    # ── Create tokens with all mode/scope combos ──────────────────────────

    t1 = _create_token(
        client, superuser_token_headers, agent_id,
        name="conv-limited", mode="conversation", scope="limited",
    )
    t2 = _create_token(
        client, superuser_token_headers, agent_id,
        name="conv-general", mode="conversation", scope="general",
    )
    t3 = _create_token(
        client, superuser_token_headers, agent_id,
        name="build-limited", mode="building", scope="limited",
    )
    t4 = _create_token(
        client, superuser_token_headers, agent_id,
        name="build-general", mode="building", scope="general",
    )

    # ── Verify all four appear in the list ────────────────────────────────

    tokens = _list_tokens(client, superuser_token_headers, agent_id)
    assert len(tokens) == 4

    token_map = {t["name"]: t for t in tokens}
    assert token_map["conv-limited"]["mode"] == "conversation"
    assert token_map["conv-limited"]["scope"] == "limited"
    assert token_map["conv-general"]["scope"] == "general"
    assert token_map["build-limited"]["mode"] == "building"
    assert token_map["build-general"]["mode"] == "building"
    assert token_map["build-general"]["scope"] == "general"

    # ── Delete one, verify the rest remain ────────────────────────────────

    _delete_token(client, superuser_token_headers, agent_id, t2["id"])

    tokens = _list_tokens(client, superuser_token_headers, agent_id)
    assert len(tokens) == 3
    remaining_ids = {t["id"] for t in tokens}
    assert t2["id"] not in remaining_ids
    assert t1["id"] in remaining_ids
    assert t3["id"] in remaining_ids
    assert t4["id"] in remaining_ids


def test_token_not_found_for_wrong_agent(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    A token created for agent A cannot be accessed via agent B's URL.
    """
    # ── Create two agents ─────────────────────────────────────────────────

    agent_a = create_agent_via_api(client, superuser_token_headers, name="Agent A")
    agent_b = create_agent_via_api(client, superuser_token_headers, name="Agent B")
    drain_tasks()

    # ── Create token on agent A ───────────────────────────────────────────

    token = _create_token(
        client, superuser_token_headers, agent_a["id"],
        name="agent-a-token",
    )

    # ── Try to access via agent B's URL → 404 ────────────────────────────

    r = client.get(
        _token_url(agent_b["id"], token["id"]),
        headers=superuser_token_headers,
    )
    assert r.status_code == 404

    # ── Agent B's list should be empty ────────────────────────────────────

    tokens_b = _list_tokens(client, superuser_token_headers, agent_b["id"])
    assert len(tokens_b) == 0

    # ── Agent A's list should have the token ──────────────────────────────

    tokens_a = _list_tokens(client, superuser_token_headers, agent_a["id"])
    assert len(tokens_a) == 1
    assert tokens_a[0]["id"] == token["id"]


def test_delete_nonexistent_token_returns_404(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Deleting a token that doesn't exist returns 404.
    """
    agent = create_agent_via_api(client, superuser_token_headers, name="404 Agent")
    drain_tasks()

    fake_token_id = str(uuid.uuid4())
    r = client.delete(
        _token_url(agent["id"], fake_token_id),
        headers=superuser_token_headers,
    )
    assert r.status_code == 404


def _create_second_user(client: TestClient) -> tuple[dict, dict[str, str]]:
    """Create a second user and return (user_data, auth_headers)."""
    user = create_random_user(client)
    headers = user_authentication_headers(
        client=client, email=user["email"], password=user["_password"],
    )
    return user, headers


def test_other_user_cannot_see_or_manage_tokens(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    User B cannot list, get, update, or delete tokens on an agent owned by user A.
      1. User A (superuser) creates agent + token
      2. User B tries to list tokens for that agent → error
      3. User B tries to get the token by ID → 404
      4. User B tries to update the token → 404
      5. User B tries to delete the token → 404
      6. User A can still manage the token normally
    """
    # ── Phase 1: User A creates agent + token ─────────────────────────────

    agent = create_agent_via_api(client, superuser_token_headers, name="Owner Agent")
    drain_tasks()
    agent_id = agent["id"]

    token = _create_token(
        client, superuser_token_headers, agent_id,
        name="owner-only-token",
    )
    token_id = token["id"]

    # ── Phase 2: Create user B ────────────────────────────────────────────

    _, user_b_headers = _create_second_user(client)

    # ── Phase 3: User B cannot list tokens ────────────────────────────────

    r = client.get(_tokens_url(agent_id), headers=user_b_headers)
    # Agent not owned by user B → 404 (agent not found for this user)
    assert r.status_code == 404

    # ── Phase 4: User B cannot get token by ID ────────────────────────────

    r = client.get(_token_url(agent_id, token_id), headers=user_b_headers)
    assert r.status_code == 404

    # ── Phase 5: User B cannot update token ───────────────────────────────

    r = client.put(
        _token_url(agent_id, token_id),
        headers=user_b_headers,
        json={"name": "hacked"},
    )
    assert r.status_code == 404

    # ── Phase 6: User B cannot delete token ───────────────────────────────

    r = client.delete(_token_url(agent_id, token_id), headers=user_b_headers)
    assert r.status_code == 404

    # ── Phase 7: User A still has full access ─────────────────────────────

    tokens = _list_tokens(client, superuser_token_headers, agent_id)
    assert len(tokens) == 1
    assert tokens[0]["id"] == token_id
    assert tokens[0]["name"] == "owner-only-token"
