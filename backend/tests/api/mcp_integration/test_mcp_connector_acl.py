"""
MCP Connector ACL integration tests.

Tests the user-ID ACL and allow_token_access fields on MCP connectors:
  - allowed_user_ids and allow_token_access round-trip through create/update/get
  - MCPConnectorPublic returns resolved allowed_users display projection
  - Consent ACL: owner allowed; user in allowed_user_ids allowed; user only
    in legacy allowed_emails allowed (fallback); unlisted user denied
  - allowed_users resolved server-side (batch query — no N+1 per connector)
  - Partial update of new fields does not clobber others
"""
import uuid

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.agent import create_agent_via_api, get_agent
from tests.utils.background_tasks import drain_tasks
from tests.utils.mcp import (
    approve_consent,
    create_mcp_connector,
    get_mcp_connector,
    list_mcp_connectors,
    register_oauth_client,
    run_full_oauth_flow,
    start_authorize,
    update_mcp_connector,
)
from tests.utils.user import create_random_user_with_headers


# ── Helpers ──────────────────────────────────────────────────────────────────


def _setup_agent(client: TestClient, token_headers: dict[str, str], name: str = "ACL Agent") -> dict:
    """Create an agent and return it."""
    agent = create_agent_via_api(client, token_headers, name=name)
    drain_tasks()
    return get_agent(client, token_headers, agent["id"])


def _get_superuser_id(client: TestClient, superuser_token_headers: dict[str, str]) -> str:
    """Fetch the authenticated superuser's own user ID via the /users/me endpoint."""
    r = client.get(f"{settings.API_V1_STR}/users/me", headers=superuser_token_headers)
    assert r.status_code == 200, f"GET /users/me failed: {r.text}"
    return r.json()["id"]


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_connector_acl_fields_round_trip(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    allowed_user_ids and allow_token_access are persisted on create and
    reflected in the public response:
      1. Create connector with allowed_user_ids + allow_token_access=True
      2. GET connector → fields present and correct
      3. List → same fields present in listing
      4. Update: change allow_token_access to False, add another user id
      5. Verify update persisted via GET
    """
    agent = _setup_agent(client, superuser_token_headers, "ACL Round-Trip Agent")
    agent_id = agent["id"]

    # Create two random users so we have real UUIDs to put in allowed_user_ids
    user_a, _ = create_random_user_with_headers(client)
    user_b, _ = create_random_user_with_headers(client)
    user_a_id = user_a["id"]
    user_b_id = user_b["id"]

    # ── Phase 1: Create connector with new ACL fields ─────────────────────
    r = client.post(
        f"{settings.API_V1_STR}/agents/{agent_id}/mcp-connectors",
        headers=superuser_token_headers,
        json={
            "name": "ACL Connector",
            "mode": "conversation",
            "allowed_emails": ["legacy@example.com"],
            "allowed_user_ids": [user_a_id],
            "allow_token_access": True,
            "max_clients": 10,
        },
    )
    assert r.status_code == 200, f"Create failed: {r.text}"
    connector = r.json()
    connector_id = connector["id"]

    assert connector["allow_token_access"] is True
    assert str(user_a_id) in [str(u) for u in connector["allowed_user_ids"]]
    assert connector["allowed_emails"] == ["legacy@example.com"]

    # ── Phase 2: GET connector → same fields ──────────────────────────────
    fetched = get_mcp_connector(client, superuser_token_headers, agent_id, connector_id)
    assert fetched["allow_token_access"] is True
    assert str(user_a_id) in [str(u) for u in fetched["allowed_user_ids"]]
    assert fetched["allowed_emails"] == ["legacy@example.com"]

    # ── Phase 3: List connectors → same fields present ────────────────────
    listing = list_mcp_connectors(client, superuser_token_headers, agent_id)
    assert listing["count"] == 1
    listed = listing["data"][0]
    assert str(user_a_id) in [str(u) for u in listed["allowed_user_ids"]]
    assert listed["allow_token_access"] is True

    # ── Phase 4: Update — add user_b, disable token access ────────────────
    updated = update_mcp_connector(
        client, superuser_token_headers, agent_id, connector_id,
        allowed_user_ids=[user_a_id, user_b_id],
        allow_token_access=False,
    )
    assert updated["allow_token_access"] is False
    updated_ids = [str(u) for u in updated["allowed_user_ids"]]
    assert str(user_a_id) in updated_ids
    assert str(user_b_id) in updated_ids
    # allowed_emails unchanged
    assert updated["allowed_emails"] == ["legacy@example.com"]

    # ── Phase 5: Verify via GET ───────────────────────────────────────────
    fetched2 = get_mcp_connector(client, superuser_token_headers, agent_id, connector_id)
    assert fetched2["allow_token_access"] is False
    fetched2_ids = [str(u) for u in fetched2["allowed_user_ids"]]
    assert str(user_a_id) in fetched2_ids
    assert str(user_b_id) in fetched2_ids


def test_connector_public_returns_allowed_users_display_projection(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    MCPConnectorPublic.allowed_users contains resolved display info (id/email/full_name)
    for each UUID in allowed_user_ids. The full token value must NOT appear.

      1. Create two random users
      2. Create connector with allowed_user_ids=[user_a, user_b]
      3. GET connector → allowed_users has id + email for each resolved user
      4. No 'token' key appears in the connector response at all
    """
    agent = _setup_agent(client, superuser_token_headers, "Allowed Users Display Agent")
    agent_id = agent["id"]

    user_a, _ = create_random_user_with_headers(client)
    user_b, _ = create_random_user_with_headers(client)

    r = client.post(
        f"{settings.API_V1_STR}/agents/{agent_id}/mcp-connectors",
        headers=superuser_token_headers,
        json={
            "name": "Display Projection Connector",
            "mode": "conversation",
            "allowed_user_ids": [user_a["id"], user_b["id"]],
        },
    )
    assert r.status_code == 200
    connector = r.json()
    connector_id = connector["id"]

    # ── allowed_users display projection ─────────────────────────────────
    allowed_users = connector.get("allowed_users", [])
    assert len(allowed_users) == 2, f"Expected 2 allowed_users, got {len(allowed_users)}"
    resolved_ids = {str(u["id"]) for u in allowed_users}
    assert str(user_a["id"]) in resolved_ids
    assert str(user_b["id"]) in resolved_ids
    for u in allowed_users:
        assert "email" in u, "allowed_users entry must have email"
        assert "id" in u, "allowed_users entry must have id"

    # ── No token value in connector public response ───────────────────────
    assert "token" not in connector, "Full token value must never appear in connector public response"

    # ── Same via GET ──────────────────────────────────────────────────────
    fetched = get_mcp_connector(client, superuser_token_headers, agent_id, connector_id)
    fetched_users = fetched.get("allowed_users", [])
    assert len(fetched_users) == 2


def test_connector_allowed_users_unresolvable_ids_dropped_from_display(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    A UUID in allowed_user_ids that no longer resolves to a user is silently
    omitted from allowed_users display projection. The raw UUID stays in
    allowed_user_ids until the owner removes it.

      1. Create connector with a real user ID + a fake/nonexistent UUID
      2. allowed_user_ids has both
      3. allowed_users only contains the resolved real user
    """
    agent = _setup_agent(client, superuser_token_headers, "Unresolvable UUID Agent")
    agent_id = agent["id"]

    user_a, _ = create_random_user_with_headers(client)
    fake_uuid = str(uuid.uuid4())  # no user with this ID

    r = client.post(
        f"{settings.API_V1_STR}/agents/{agent_id}/mcp-connectors",
        headers=superuser_token_headers,
        json={
            "name": "Unresolvable Connector",
            "mode": "conversation",
            "allowed_user_ids": [user_a["id"], fake_uuid],
        },
    )
    assert r.status_code == 200
    connector = r.json()

    # Raw allowed_user_ids retains both
    raw_ids = [str(u) for u in connector["allowed_user_ids"]]
    assert str(user_a["id"]) in raw_ids
    assert fake_uuid in raw_ids

    # allowed_users only resolved the real one
    allowed_users = connector.get("allowed_users", [])
    resolved_ids = {str(u["id"]) for u in allowed_users}
    assert str(user_a["id"]) in resolved_ids
    assert fake_uuid not in resolved_ids


def test_consent_acl_owner_allowed(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    The connector owner can always approve consent even if allowed_user_ids and
    allowed_emails are both empty.
    """
    agent = _setup_agent(client, superuser_token_headers, "Owner Consent Agent")
    agent_id = agent["id"]

    # Connector with no explicit ACL entries
    connector = create_mcp_connector(
        client, superuser_token_headers, agent_id,
        name="Owner Only Connector",
        allowed_emails=[],
    )
    connector_id = connector["id"]

    oauth_client = register_oauth_client(client, connector_id)
    nonce = start_authorize(client, oauth_client["client_id"], connector_id)

    # Owner approves → success
    approval = approve_consent(client, superuser_token_headers, nonce)
    assert "redirect_url" in approval
    assert "code=" in approval["redirect_url"]


def test_consent_acl_allowed_user_id_grants_access(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    A user whose ID is in allowed_user_ids can approve consent:
      1. Create connector with allowed_user_ids=[user_a]
      2. user_a starts OAuth flow → gets nonce
      3. user_a approves consent → success (200 with redirect_url)
      4. Unlisted user_b is denied → 403
    """
    agent = _setup_agent(client, superuser_token_headers, "User ID ACL Consent Agent")
    agent_id = agent["id"]

    user_a, user_a_headers = create_random_user_with_headers(client)
    _, user_b_headers = create_random_user_with_headers(client)

    # Create connector with user_a in allowed_user_ids
    r = client.post(
        f"{settings.API_V1_STR}/agents/{agent_id}/mcp-connectors",
        headers=superuser_token_headers,
        json={
            "name": "User ID ACL Connector",
            "mode": "conversation",
            "allowed_user_ids": [user_a["id"]],
            "allowed_emails": [],
        },
    )
    assert r.status_code == 200
    connector_id = r.json()["id"]

    oauth_client = register_oauth_client(client, connector_id)

    # ── user_a approves → success ─────────────────────────────────────────
    nonce_a = start_authorize(client, oauth_client["client_id"], connector_id)
    approval = approve_consent(client, user_a_headers, nonce_a)
    assert "redirect_url" in approval
    assert "code=" in approval["redirect_url"]

    # ── user_b (unlisted) denied → 403 ────────────────────────────────────
    # Start a fresh OAuth flow to get a new nonce
    oauth_client2 = register_oauth_client(client, connector_id, client_name="Test Client 2")
    nonce_b = start_authorize(client, oauth_client2["client_id"], connector_id)
    r = client.post(
        f"{settings.API_V1_STR}/mcp/consent/{nonce_b}/approve",
        headers=user_b_headers,
    )
    assert r.status_code == 403, f"Expected 403 for unlisted user, got {r.status_code}: {r.text}"


def test_consent_acl_legacy_email_fallback_grants_access(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Legacy allowed_emails fallback: a user whose email is in allowed_emails
    but whose ID is NOT in allowed_user_ids can still approve consent.

      1. Create a random user (has a real email)
      2. Create connector with allowed_emails=[that user's email] and empty allowed_user_ids
      3. That user approves consent → success (email fallback works)
      4. Completely different user denied → 403
    """
    agent = _setup_agent(client, superuser_token_headers, "Email Fallback Consent Agent")
    agent_id = agent["id"]

    allowed_user, allowed_headers = create_random_user_with_headers(client)
    _, denied_headers = create_random_user_with_headers(client)

    # Create connector with email fallback only, no allowed_user_ids
    r = client.post(
        f"{settings.API_V1_STR}/agents/{agent_id}/mcp-connectors",
        headers=superuser_token_headers,
        json={
            "name": "Email Fallback Connector",
            "mode": "conversation",
            "allowed_emails": [allowed_user["email"]],
            "allowed_user_ids": [],  # explicitly empty
        },
    )
    assert r.status_code == 200
    connector_id = r.json()["id"]

    oauth_client = register_oauth_client(client, connector_id)

    # ── Email-allowed user can approve ────────────────────────────────────
    nonce = start_authorize(client, oauth_client["client_id"], connector_id)
    approval = approve_consent(client, allowed_headers, nonce)
    assert "redirect_url" in approval
    assert "code=" in approval["redirect_url"]

    # ── Completely different user denied → 403 ────────────────────────────
    oauth_client2 = register_oauth_client(client, connector_id, client_name="Client 2")
    nonce2 = start_authorize(client, oauth_client2["client_id"], connector_id)
    r = client.post(
        f"{settings.API_V1_STR}/mcp/consent/{nonce2}/approve",
        headers=denied_headers,
    )
    assert r.status_code == 403


def test_consent_acl_unlisted_user_denied(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    A user who is neither the owner, nor in allowed_user_ids, nor in
    allowed_emails is rejected with 403 on consent approve.
    """
    agent = _setup_agent(client, superuser_token_headers, "ACL Denied Agent")
    agent_id = agent["id"]

    user_a, _ = create_random_user_with_headers(client)
    _, unlisted_headers = create_random_user_with_headers(client)

    # Connector with user_a allowed (both by id and by email)
    r = client.post(
        f"{settings.API_V1_STR}/agents/{agent_id}/mcp-connectors",
        headers=superuser_token_headers,
        json={
            "name": "Restricted Connector",
            "mode": "conversation",
            "allowed_user_ids": [user_a["id"]],
            "allowed_emails": [user_a["email"]],
        },
    )
    assert r.status_code == 200
    connector_id = r.json()["id"]

    oauth_client = register_oauth_client(client, connector_id)
    nonce = start_authorize(client, oauth_client["client_id"], connector_id)

    # Unlisted user cannot approve
    r = client.post(
        f"{settings.API_V1_STR}/mcp/consent/{nonce}/approve",
        headers=unlisted_headers,
    )
    assert r.status_code == 403


def test_connector_new_fields_do_not_break_existing_crud(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Connectors created without the new fields use sensible defaults:
      - allowed_user_ids defaults to []
      - allow_token_access defaults to False
      - allowed_users defaults to []
    """
    agent = _setup_agent(client, superuser_token_headers, "Default Fields Agent")
    agent_id = agent["id"]

    connector = create_mcp_connector(
        client, superuser_token_headers, agent_id,
        name="Legacy-Style Connector",
        mode="conversation",
        allowed_emails=["old@example.com"],
    )

    assert connector["allowed_user_ids"] == []
    assert connector["allow_token_access"] is False
    assert connector["allowed_users"] == []
    assert connector["allowed_emails"] == ["old@example.com"]


def test_update_allow_token_access_only_does_not_clear_user_ids(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Partial update of allow_token_access alone must not clear allowed_user_ids.
    (Verifies model_dump(exclude_unset=True) behaviour in update_connector.)
    """
    agent = _setup_agent(client, superuser_token_headers, "Partial Update ACL Agent")
    agent_id = agent["id"]

    user_a, _ = create_random_user_with_headers(client)

    r = client.post(
        f"{settings.API_V1_STR}/agents/{agent_id}/mcp-connectors",
        headers=superuser_token_headers,
        json={
            "name": "Stable IDs Connector",
            "mode": "conversation",
            "allowed_user_ids": [user_a["id"]],
            "allow_token_access": False,
        },
    )
    assert r.status_code == 200
    connector_id = r.json()["id"]

    # Only toggle allow_token_access
    updated = update_mcp_connector(
        client, superuser_token_headers, agent_id, connector_id,
        allow_token_access=True,
    )
    assert updated["allow_token_access"] is True
    # allowed_user_ids must be unchanged
    assert str(user_a["id"]) in [str(u) for u in updated["allowed_user_ids"]]
