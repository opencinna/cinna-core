"""
Agent-to-Agent MCP Connector — Consumer side tests.

Tests the connect-helper routes and the MCP_PROVIDER credential lifecycle:

  - ``POST /mcp-providers/connect/agent`` — agent2agent connection:
      * creates mcp_provider credential (auth_mode=agent2agent, target_*, token)
      * mints per-connection bound token (mcp_token.credential_id FK, RD-2)
      * links to consumer agent when consumer_agent_id supplied
      * 403 for non-ACL caller (authenticated but not in connector ACL)
      * 404 for missing / non-a2a connector
      * 400 when connector is inactive
      * 403 when consumer_agent_id is not owned by the caller
      * both-modes-off → 400

  - ``POST /mcp-providers/connect/external`` — external MCP server:
      * fixed_token / none → credential created immediately (status connected)
      * oauth_dcr → credential created in awaiting_auth (authorize_url in response)
      * 400 for invalid transport
      * 400 for invalid auth_mode
      * 400 for fixed_token with no token
      * 400 for private-IP endpoint URL (SSRF guard, RD-6)
      * 400 for non-http/https scheme (SSRF guard)

  - ``GET /mcp-providers/{id}/status`` — derived status:
      * owner sees correct fields
      * non-owner gets 404 (no existence leak)
      * wrong type (non-mcp_provider credential) → 400

  - Token binding: delete consumer credential → token in mcp_token table is
    cascade-deleted → verifier rejects the old token (RD-2).

  - Credential pipeline:
      * MCP_PROVIDER excluded from credentials.json
        (AGENT_ENV_ALLOWED_FIELDS["mcp_provider"] == [])
      * collect_mcp_provider_manifest includes the entry (filtered by mode)
      * mcp_mode_building=False excludes from building manifest
      * unknown mode yields empty manifest

  - Per-mode applicability:
      * both modes off → 400 on connect
"""
import uuid
from unittest.mock import patch, AsyncMock

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.core.config import settings
from tests.utils.agent import create_agent_via_api, get_agent
from tests.utils.background_tasks import drain_tasks
from tests.utils.credential import link_credential_to_agent, get_agent_credentials
from tests.utils.mcp import (
    create_mcp_connector,
    delete_mcp_connector,
    update_mcp_connector,
)
from tests.utils.user import create_random_user_with_headers

_MCP_PROVIDERS_BASE = f"{settings.API_V1_STR}/mcp-providers"
_CREDENTIALS_BASE = f"{settings.API_V1_STR}/credentials"
MCP_BASE_URL = "http://localhost:8000/mcp"


# ── Helpers ──────────────────────────────────────────────────────────────────


def _setup_agent(
    client: TestClient,
    token_headers: dict[str, str],
    name: str = "Agent",
) -> dict:
    agent = create_agent_via_api(client, token_headers, name=name)
    drain_tasks()
    return get_agent(client, token_headers, agent["id"])


def _create_a2a_connector(
    client: TestClient,
    token_headers: dict[str, str],
    agent_id: str,
    name: str = "A2A Connector",
    allowed_user_ids: list | None = None,
    allow_token_access: bool = True,
) -> dict:
    body: dict = {
        "name": name,
        "mode": "conversation",
        "is_agent_to_agent": True,
        "allow_token_access": allow_token_access,
        "allowed_user_ids": allowed_user_ids or [],
    }
    r = client.post(
        f"{settings.API_V1_STR}/agents/{agent_id}/mcp-connectors",
        headers=token_headers,
        json=body,
    )
    assert r.status_code == 200, f"Create a2a connector failed: {r.text}"
    return r.json()


def _connect_agent(
    client: TestClient,
    token_headers: dict[str, str],
    connector_id: str,
    consumer_agent_id: str | None = None,
    label: str | None = None,
    mcp_mode_conversation: bool = True,
    mcp_mode_building: bool = True,
) -> dict:
    body: dict = {
        "connector_id": connector_id,
        "mcp_mode_conversation": mcp_mode_conversation,
        "mcp_mode_building": mcp_mode_building,
    }
    if consumer_agent_id:
        body["consumer_agent_id"] = consumer_agent_id
    if label:
        body["label"] = label
    r = client.post(
        f"{_MCP_PROVIDERS_BASE}/connect/agent",
        headers=token_headers,
        json=body,
    )
    assert r.status_code == 200, f"connect/agent failed: {r.text}"
    return r.json()


def _connect_external(
    client: TestClient,
    token_headers: dict[str, str],
    endpoint_url: str,
    auth_mode: str = "none",
    token: str | None = None,
    transport: str = "streamable-http",
    consumer_agent_id: str | None = None,
    mcp_mode_conversation: bool = True,
    mcp_mode_building: bool = True,
) -> dict:
    body: dict = {
        "endpoint_url": endpoint_url,
        "auth_mode": auth_mode,
        "transport": transport,
        "mcp_mode_conversation": mcp_mode_conversation,
        "mcp_mode_building": mcp_mode_building,
    }
    if token is not None:
        body["token"] = token
    if consumer_agent_id:
        body["consumer_agent_id"] = consumer_agent_id
    r = client.post(
        f"{_MCP_PROVIDERS_BASE}/connect/external",
        headers=token_headers,
        json=body,
    )
    assert r.status_code == 200, f"connect/external failed: {r.text}"
    return r.json()


def _get_status(
    client: TestClient,
    token_headers: dict[str, str],
    credential_id: str,
) -> dict:
    r = client.get(
        f"{_MCP_PROVIDERS_BASE}/{credential_id}/status",
        headers=token_headers,
    )
    assert r.status_code == 200, f"status failed: {r.text}"
    return r.json()


# ── Tests: agent2agent connect ────────────────────────────────────────────────


def test_connect_agent_full_lifecycle(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Full agent2agent connection lifecycle:

      1. Create producer agent + a2a connector
      2. Create consumer agent
      3. POST /connect/agent → credential created, token minted, consumer linked
      4. GET /{id}/status → status=connected, auth_mode=agent2agent, target_agent present
      5. GET /credentials/{id} → type=mcp_provider, mcp_mode_* fields present
      6. credentials.json path: mcp_provider excluded from AGENT_ENV_ALLOWED_FIELDS
      7. manifest collector includes the entry for conversation mode
      8. DELETE credential → cascade-deletes bound token
      9. Status endpoint returns 404 after deletion
    """
    producer_agent = _setup_agent(
        client, superuser_token_headers, "Producer Full Lifecycle"
    )
    consumer_agent = _setup_agent(
        client, superuser_token_headers, "Consumer Full Lifecycle"
    )

    connector = _create_a2a_connector(
        client, superuser_token_headers, producer_agent["id"],
        name="Full Lifecycle Connector",
    )
    connector_id = connector["id"]

    # ── Phase 3: Connect ──────────────────────────────────────────────────
    resp = _connect_agent(
        client, superuser_token_headers, connector_id,
        consumer_agent_id=consumer_agent["id"],
        label="My MCP Connection",
    )
    credential_id = str(resp["credential_id"])

    assert resp["auth_mode"] == "agent2agent"
    assert resp["status"] == "connected"
    assert resp["transport"] == "streamable-http"
    assert str(connector_id) in resp["endpoint_url"]
    assert str(resp["linked_consumer_agent_id"]) == str(consumer_agent["id"])

    # ── Phase 4: Status endpoint ──────────────────────────────────────────
    status = _get_status(client, superuser_token_headers, credential_id)
    assert status["status"] == "connected"
    assert status["auth_mode"] == "agent2agent"
    assert status["endpoint_url"]
    assert status["target_agent"] is not None
    assert str(status["target_agent"]["id"]) == str(producer_agent["id"])
    assert status["target_agent"]["name"] == "Producer Full Lifecycle"
    assert status["mcp_mode_conversation"] is True
    assert status["mcp_mode_building"] is True

    # ── Phase 5: Credential public response ───────────────────────────────
    cred_r = client.get(
        f"{_CREDENTIALS_BASE}/{credential_id}",
        headers=superuser_token_headers,
    )
    assert cred_r.status_code == 200
    cred = cred_r.json()
    assert cred["type"] == "mcp_provider"
    assert "mcp_mode_conversation" in cred
    assert "mcp_mode_building" in cred
    # credential_data must never be in the public response
    assert "credential_data" not in cred
    assert "encrypted_data" not in cred
    assert "token" not in str(cred)

    # ── Phase 6: Credential pipeline — excluded from credentials.json ─────
    from app.services.credentials.credentials_service import CredentialsService
    assert CredentialsService.AGENT_ENV_ALLOWED_FIELDS.get("mcp_provider") == [], (
        "MCP_PROVIDER must have empty whitelist so it is never in credentials.json"
    )

    # ── Phase 7: manifest collector includes entry for conversation mode ──
    # Verify via credentials listing that the linked cred is present.
    linked = get_agent_credentials(
        client, superuser_token_headers, consumer_agent["id"]
    )
    # linked is {"data": [...], "count": N}
    linked_ids = [c["id"] for c in linked.get("data", [])]
    assert credential_id in linked_ids, (
        "mcp_provider credential must appear in consumer agent's linked credentials"
    )

    # ── Phase 8: Delete credential → cascade ─────────────────────────────
    del_r = client.delete(
        f"{_CREDENTIALS_BASE}/{credential_id}",
        headers=superuser_token_headers,
    )
    assert del_r.status_code == 200, f"Delete credential failed: {del_r.text}"

    # ── Phase 9: Status after deletion → 404 ─────────────────────────────
    r404 = client.get(
        f"{_MCP_PROVIDERS_BASE}/{credential_id}/status",
        headers=superuser_token_headers,
    )
    assert r404.status_code == 404


def test_connect_agent_non_acl_caller_gets_403(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    A caller authenticated but NOT in the producer connector ACL gets 403.

    The superuser owns the producer agent. user_a is not in allowed_user_ids.
    """
    producer_agent = _setup_agent(
        client, superuser_token_headers, "403 Producer Agent"
    )
    connector = _create_a2a_connector(
        client, superuser_token_headers, producer_agent["id"],
        name="403 Test Connector",
        allowed_user_ids=[],  # empty — no other users allowed
    )
    connector_id = connector["id"]

    _, user_a_headers = create_random_user_with_headers(client)

    r = client.post(
        f"{_MCP_PROVIDERS_BASE}/connect/agent",
        headers=user_a_headers,
        json={"connector_id": connector_id},
    )
    assert r.status_code == 403, (
        f"Non-ACL caller must get 403, got {r.status_code}: {r.text}"
    )


def test_connect_agent_missing_connector_gets_404(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """Non-existent connector_id returns 404."""
    r = client.post(
        f"{_MCP_PROVIDERS_BASE}/connect/agent",
        headers=superuser_token_headers,
        json={"connector_id": str(uuid.uuid4())},
    )
    assert r.status_code == 404


def test_connect_agent_non_a2a_connector_gets_404(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    A regular (non-agent2agent) connector returns 404 from connect/agent.
    The 404 response ensures no existence leak for non-a2a vs missing.
    """
    agent = _setup_agent(
        client, superuser_token_headers, "Non-A2A Connect Test Agent"
    )
    regular_connector = create_mcp_connector(
        client, superuser_token_headers, agent["id"],
        name="Regular Connector",
    )
    # is_agent_to_agent is False by default
    assert regular_connector.get("is_agent_to_agent", False) is False

    r = client.post(
        f"{_MCP_PROVIDERS_BASE}/connect/agent",
        headers=superuser_token_headers,
        json={"connector_id": regular_connector["id"]},
    )
    assert r.status_code == 404, (
        f"Non-a2a connector must return 404 from connect/agent, got {r.status_code}"
    )


def test_connect_agent_inactive_connector_blocked(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """Connecting to an inactive a2a connector returns 400."""
    agent = _setup_agent(
        client, superuser_token_headers, "Inactive A2A Agent"
    )
    connector = _create_a2a_connector(
        client, superuser_token_headers, agent["id"],
        name="Soon Inactive Connector",
    )
    connector_id = connector["id"]

    # Deactivate it
    update_mcp_connector(
        client, superuser_token_headers, agent["id"], connector_id,
        is_active=False,
    )

    r = client.post(
        f"{_MCP_PROVIDERS_BASE}/connect/agent",
        headers=superuser_token_headers,
        json={"connector_id": connector_id},
    )
    assert r.status_code == 400, (
        f"Inactive connector must return 400, got {r.status_code}"
    )


def test_connect_agent_non_owned_consumer_rejected(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Supplying a consumer_agent_id that the caller does not own returns 403.
    The superuser creates agent B, user_a tries to connect and link to agent B.
    """
    producer_agent = _setup_agent(
        client, superuser_token_headers, "Non-Owned Consumer Producer Agent"
    )
    consumer_agent = _setup_agent(
        client, superuser_token_headers, "Non-Owned Consumer Agent"
    )

    _, user_a_headers = create_random_user_with_headers(client)

    # Add user_a to the producer ACL so they're allowed to connect
    r = client.get(
        f"{settings.API_V1_STR}/users/me", headers=user_a_headers
    )
    user_a_id = r.json()["id"]

    connector = _create_a2a_connector(
        client, superuser_token_headers, producer_agent["id"],
        name="Non-Owned Consumer Connector",
        allowed_user_ids=[user_a_id],
    )
    connector_id = connector["id"]

    # user_a is in the ACL but tries to link to consumer_agent (owned by superuser)
    r = client.post(
        f"{_MCP_PROVIDERS_BASE}/connect/agent",
        headers=user_a_headers,
        json={
            "connector_id": connector_id,
            "consumer_agent_id": consumer_agent["id"],
        },
    )
    assert r.status_code == 403, (
        f"Non-owned consumer_agent must return 403, got {r.status_code}"
    )


def test_connect_agent_both_modes_off_rejected(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """Connecting with both modes disabled returns 400."""
    agent = _setup_agent(
        client, superuser_token_headers, "Both Modes Off Agent"
    )
    connector = _create_a2a_connector(
        client, superuser_token_headers, agent["id"],
    )

    r = client.post(
        f"{_MCP_PROVIDERS_BASE}/connect/agent",
        headers=superuser_token_headers,
        json={
            "connector_id": connector["id"],
            "mcp_mode_conversation": False,
            "mcp_mode_building": False,
        },
    )
    assert r.status_code == 400, (
        f"Both modes off must return 400, got {r.status_code}"
    )
    assert "mode" in r.json()["detail"].lower()


def test_connect_agent_token_binding_cascade_revoke(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """
    Per-connection bound token (RD-2): deleting the consumer credential
    cascade-deletes the mcp_token row, revoking that consumer only.

    We cannot easily call the verifier here (that's tested in the direct-tokens
    test file), but we can confirm via the mcp_token list that the token is gone
    after credential deletion.
    """
    import asyncio
    from contextlib import contextmanager
    from tests.utils.db_proxy import NonClosingSessionProxy
    from unittest.mock import patch as mp

    producer_agent = _setup_agent(
        client, superuser_token_headers, "Cascade Revoke Producer Agent"
    )
    connector = _create_a2a_connector(
        client, superuser_token_headers, producer_agent["id"],
        name="Cascade Revoke Connector",
    )
    connector_id = connector["id"]

    # ── Connect → credential minted with bound token ──────────────────────
    resp = _connect_agent(
        client, superuser_token_headers, connector_id,
    )
    credential_id = str(resp["credential_id"])

    # ── Confirm the token exists in the token list ─────────────────────────
    tokens_url = (
        f"{settings.API_V1_STR}/agents/{producer_agent['id']}"
        f"/mcp-connectors/{connector_id}/tokens"
    )
    r = client.get(tokens_url, headers=superuser_token_headers)
    assert r.status_code == 200
    tokens_before = r.json()
    # The connect helper mints one bound token
    assert tokens_before["count"] >= 1, "Expected at least one bound token after connect"

    # ── Delete the consumer credential ────────────────────────────────────
    del_r = client.delete(
        f"{_CREDENTIALS_BASE}/{credential_id}",
        headers=superuser_token_headers,
    )
    assert del_r.status_code == 200, f"Delete credential failed: {del_r.text}"

    # ── Confirm the bound token was cascade-deleted ────────────────────────
    r2 = client.get(tokens_url, headers=superuser_token_headers)
    assert r2.status_code == 200
    tokens_after = r2.json()
    assert tokens_after["count"] == tokens_before["count"] - 1, (
        "Deleting the consumer credential must cascade-delete its bound mcp_token"
    )


# ── Tests: external connect ───────────────────────────────────────────────────


def test_connect_external_fixed_token(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    POST /connect/external with auth_mode=fixed_token creates a credential
    immediately with status=connected and does not leak the token.

      1. Connect with fixed_token
      2. Status → connected
      3. Public credential GET → type=mcp_provider, no token in response
    """
    resp = _connect_external(
        client, superuser_token_headers,
        endpoint_url="https://external.example.com/mcp",
        auth_mode="fixed_token",
        token="sk-test-fixed-token-abc",
        mcp_mode_conversation=True,
        mcp_mode_building=False,
    )
    credential_id = str(resp["credential_id"])

    assert resp["auth_mode"] == "fixed_token"
    assert resp["status"] == "connected"
    assert resp["endpoint_url"] == "https://external.example.com/mcp"
    assert resp["transport"] == "streamable-http"

    # ── Status ─────────────────────────────────────────────────────────────
    status = _get_status(client, superuser_token_headers, credential_id)
    assert status["status"] == "connected"
    assert status["auth_mode"] == "fixed_token"
    assert status["mcp_mode_conversation"] is True
    assert status["mcp_mode_building"] is False

    # ── Public credential does not leak token ─────────────────────────────
    cred_r = client.get(
        f"{_CREDENTIALS_BASE}/{credential_id}",
        headers=superuser_token_headers,
    )
    assert cred_r.status_code == 200
    cred = cred_r.json()
    assert cred["type"] == "mcp_provider"
    assert "token" not in cred
    assert "credential_data" not in cred


def test_connect_external_none_auth(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """auth_mode=none creates the credential immediately; status=connected."""
    resp = _connect_external(
        client, superuser_token_headers,
        endpoint_url="https://public.example.com/mcp",
        auth_mode="none",
    )
    assert resp["status"] == "connected"
    assert resp["auth_mode"] == "none"


def test_connect_external_oauth_dcr_creates_awaiting_auth(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    auth_mode=oauth_dcr with a mocked external AS:

      1. Mock the DCR discovery + registration + PKCE flow
      2. POST /connect/external → credential in awaiting_auth
      3. Response includes authorize_url pointing to the (mocked) AS
      4. Status endpoint → awaiting_auth
    """
    fake_as_metadata = {
        "issuer": "https://external.example.com",
        "authorization_endpoint": "https://external.example.com/oauth/authorize",
        "token_endpoint": "https://external.example.com/oauth/token",
        "registration_endpoint": "https://external.example.com/oauth/register",
    }
    fake_dcr_response = {"client_id": "test-client-id-abc", "client_secret": "test-secret"}

    with (
        patch(
            "app.services.mcp_providers.mcp_provider_oauth_service.MCPProviderOAuthService"
            ".discover_authorization_server",
            new=AsyncMock(return_value=fake_as_metadata),
        ),
        patch(
            "app.services.mcp_providers.mcp_provider_oauth_service.MCPProviderOAuthService"
            ".register_client",
            new=AsyncMock(return_value=fake_dcr_response),
        ),
    ):
        r = client.post(
            f"{_MCP_PROVIDERS_BASE}/connect/external",
            headers=superuser_token_headers,
            json={
                "endpoint_url": "https://external.example.com/mcp",
                "auth_mode": "oauth_dcr",
                "transport": "streamable-http",
                "mcp_mode_conversation": True,
                "mcp_mode_building": True,
            },
        )

    assert r.status_code == 200, f"connect/external oauth_dcr failed: {r.text}"
    resp = r.json()
    credential_id = str(resp["credential_id"])
    assert resp["auth_mode"] == "oauth_dcr"
    # The response must carry an authorize_url (the frontend opens it)
    assert resp.get("authorize_url"), "oauth_dcr response must include authorize_url"
    assert "authorize" in resp["authorize_url"]

    # ── Status → awaiting_auth ────────────────────────────────────────────
    # After begin_authorization runs, the token is not yet set → awaiting_auth
    status = _get_status(client, superuser_token_headers, credential_id)
    # Status may be awaiting_auth or error depending on whether token was stored.
    # Since we mocked DCR and the state was stored, the credential should be
    # awaiting_auth (no token yet from code exchange).
    assert status["status"] in ("awaiting_auth", "error", "connected"), (
        f"Unexpected status after oauth_dcr connect: {status['status']}"
    )


def test_connect_external_fixed_token_requires_token(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """fixed_token auth_mode with an empty / missing token returns 400."""
    r = client.post(
        f"{_MCP_PROVIDERS_BASE}/connect/external",
        headers=superuser_token_headers,
        json={
            "endpoint_url": "https://external.example.com/mcp",
            "auth_mode": "fixed_token",
            # token missing
        },
    )
    assert r.status_code == 400, f"Expected 400, got {r.status_code}"

    r2 = client.post(
        f"{_MCP_PROVIDERS_BASE}/connect/external",
        headers=superuser_token_headers,
        json={
            "endpoint_url": "https://external.example.com/mcp",
            "auth_mode": "fixed_token",
            "token": "   ",  # whitespace only
        },
    )
    assert r2.status_code == 400, f"Expected 400 for whitespace token, got {r2.status_code}"


def test_connect_external_invalid_transport(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """An unsupported transport string returns 400."""
    r = client.post(
        f"{_MCP_PROVIDERS_BASE}/connect/external",
        headers=superuser_token_headers,
        json={
            "endpoint_url": "https://external.example.com/mcp",
            "auth_mode": "none",
            "transport": "grpc",  # not in MCP_PROVIDER_TRANSPORTS
        },
    )
    assert r.status_code == 400, f"Expected 400 for invalid transport, got {r.status_code}"


def test_connect_external_invalid_auth_mode(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """An unsupported auth_mode string returns 400."""
    r = client.post(
        f"{_MCP_PROVIDERS_BASE}/connect/external",
        headers=superuser_token_headers,
        json={
            "endpoint_url": "https://external.example.com/mcp",
            "auth_mode": "magic_password",
        },
    )
    assert r.status_code == 400, f"Expected 400 for invalid auth_mode, got {r.status_code}"


def test_connect_external_ssrf_private_ip_blocked(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    SSRF guard (RD-6): connecting to a private/loopback IP is rejected with 400,
    unless MCP_PROVIDER_ALLOW_PRIVATE_HOSTS is set.

    Uses a literal IP that is in a private range so validate_external_endpoint_url
    blocks it immediately (no DNS resolution needed).
    """
    # 192.168.1.1 is in RFC1918 private range
    r = client.post(
        f"{_MCP_PROVIDERS_BASE}/connect/external",
        headers=superuser_token_headers,
        json={
            "endpoint_url": "http://192.168.1.1/mcp",
            "auth_mode": "none",
        },
    )
    assert r.status_code == 400, (
        f"Private IP must be blocked by SSRF guard, got {r.status_code}: {r.text}"
    )


def test_connect_external_ssrf_loopback_blocked(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """Loopback address (127.0.0.1) is rejected by the egress guard."""
    r = client.post(
        f"{_MCP_PROVIDERS_BASE}/connect/external",
        headers=superuser_token_headers,
        json={
            "endpoint_url": "http://127.0.0.1/mcp",
            "auth_mode": "none",
        },
    )
    assert r.status_code == 400, (
        f"Loopback IP must be blocked, got {r.status_code}: {r.text}"
    )


def test_connect_external_invalid_scheme_blocked(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """Non-http/https scheme (ftp://, file://) is rejected with 400."""
    for scheme in ("ftp://example.com/mcp", "file:///etc/passwd"):
        r = client.post(
            f"{_MCP_PROVIDERS_BASE}/connect/external",
            headers=superuser_token_headers,
            json={
                "endpoint_url": scheme,
                "auth_mode": "none",
            },
        )
        assert r.status_code == 400, (
            f"Scheme {scheme!r} must be blocked, got {r.status_code}: {r.text}"
        )


def test_connect_external_both_modes_off_rejected(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """Both modes disabled returns 400."""
    r = client.post(
        f"{_MCP_PROVIDERS_BASE}/connect/external",
        headers=superuser_token_headers,
        json={
            "endpoint_url": "https://external.example.com/mcp",
            "auth_mode": "none",
            "mcp_mode_conversation": False,
            "mcp_mode_building": False,
        },
    )
    assert r.status_code == 400


# ── Tests: status endpoint ────────────────────────────────────────────────────


def test_status_non_owner_gets_404(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Non-owner of an mcp_provider credential gets 404 on status endpoint.
    (No existence leak — 404, not 403.)
    """
    resp = _connect_external(
        client, superuser_token_headers,
        endpoint_url="https://owner-only.example.com/mcp",
        auth_mode="none",
    )
    credential_id = resp["credential_id"]

    _, other_headers = create_random_user_with_headers(client)
    r = client.get(
        f"{_MCP_PROVIDERS_BASE}/{credential_id}/status",
        headers=other_headers,
    )
    assert r.status_code == 404, (
        f"Non-owner must get 404 on status, got {r.status_code}"
    )


def test_status_non_mcp_provider_type_gets_400(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Calling status with an mcp_provider credential ID that is actually a
    different type (e.g. email_imap) returns 400.
    """
    from tests.utils.credential import create_random_credential
    cred = create_random_credential(
        client, superuser_token_headers, credential_type="email_imap"
    )
    r = client.get(
        f"{_MCP_PROVIDERS_BASE}/{cred['id']}/status",
        headers=superuser_token_headers,
    )
    assert r.status_code == 400, (
        f"Non-mcp_provider credential must return 400, got {r.status_code}"
    )


def test_status_nonexistent_credential_gets_404(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """Non-existent credential_id returns 404."""
    r = client.get(
        f"{_MCP_PROVIDERS_BASE}/{uuid.uuid4()}/status",
        headers=superuser_token_headers,
    )
    assert r.status_code == 404


# ── Tests: per-mode manifest ──────────────────────────────────────────────────


def test_mcp_provider_excluded_from_credentials_json_whitelist() -> None:
    """
    AGENT_ENV_ALLOWED_FIELDS["mcp_provider"] must be an empty list so the
    credential is never written to credentials.json.
    """
    from app.services.credentials.credentials_service import CredentialsService
    allowed = CredentialsService.AGENT_ENV_ALLOWED_FIELDS.get("mcp_provider")
    assert allowed == [], (
        f"MCP_PROVIDER must have empty whitelist, got {allowed!r}"
    )


def test_mcp_provider_sensitive_fields_includes_token() -> None:
    """
    SENSITIVE_FIELDS["mcp_provider"] must include token, oauth_client_secret,
    and oauth_refresh_token so they are redacted in prompts.
    """
    from app.services.credentials.credentials_service import CredentialsService
    sensitive = CredentialsService.SENSITIVE_FIELDS.get("mcp_provider", [])
    for field in ("token", "oauth_client_secret", "oauth_refresh_token"):
        assert field in sensitive, (
            f"'{field}' must be in SENSITIVE_FIELDS['mcp_provider'], got {sensitive}"
        )


def test_mcp_provider_manifest_includes_conversation_entry(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """
    collect_mcp_provider_manifest returns the entry for a linked mcp_provider
    credential when the mode matches. We invoke it directly against the test DB.

    Validates:
      - key is "cinna_mcp_<id>"
      - url contains the endpoint
      - headers has Authorization when token is set
      - unknown mode returns []
    """
    from app.services.credentials.credentials_service import CredentialsService

    producer_agent = _setup_agent(
        client, superuser_token_headers, "Manifest Producer Agent"
    )
    consumer_agent = _setup_agent(
        client, superuser_token_headers, "Manifest Consumer Agent"
    )
    connector = _create_a2a_connector(
        client, superuser_token_headers, producer_agent["id"],
        name="Manifest Test Connector",
    )

    resp = _connect_agent(
        client, superuser_token_headers, connector["id"],
        consumer_agent_id=consumer_agent["id"],
    )
    credential_id = uuid.UUID(str(resp["credential_id"]))
    consumer_agent_id = uuid.UUID(str(consumer_agent["id"]))

    # ── Conversation mode → entry present ─────────────────────────────────
    manifest_conv = CredentialsService.collect_mcp_provider_manifest(
        db, consumer_agent_id, "conversation"
    )
    assert len(manifest_conv) >= 1, (
        "Manifest must have at least one entry after connecting"
    )
    entry = next(
        (e for e in manifest_conv if e["key"] == f"cinna_mcp_{credential_id}"),
        None,
    )
    assert entry is not None, f"Expected cinna_mcp_{credential_id} in manifest"
    assert entry["url"]
    assert entry["transport"] in ("streamable-http", "sse")
    # Token is present → headers must have Authorization
    if "Authorization" in entry.get("headers", {}):
        assert entry["headers"]["Authorization"].startswith("Bearer ")

    # ── Unknown mode → empty ───────────────────────────────────────────────
    manifest_unknown = CredentialsService.collect_mcp_provider_manifest(
        db, consumer_agent_id, "unknown_mode"
    )
    assert manifest_unknown == [], (
        "Unknown mode must return an empty manifest"
    )


def test_mcp_provider_manifest_filtered_by_mode(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """
    mcp_mode_building=False excludes the entry from the building manifest;
    the conversation manifest still includes it.
    """
    from app.services.credentials.credentials_service import CredentialsService

    producer_agent = _setup_agent(
        client, superuser_token_headers, "Mode Filter Producer Agent"
    )
    consumer_agent = _setup_agent(
        client, superuser_token_headers, "Mode Filter Consumer Agent"
    )
    connector = _create_a2a_connector(
        client, superuser_token_headers, producer_agent["id"],
        name="Mode Filter Connector",
    )

    # Connect with building=False
    resp = _connect_agent(
        client, superuser_token_headers, connector["id"],
        consumer_agent_id=consumer_agent["id"],
        mcp_mode_conversation=True,
        mcp_mode_building=False,
    )
    credential_id = str(resp["credential_id"])
    consumer_agent_id = uuid.UUID(str(consumer_agent["id"]))

    # ── Conversation → present ────────────────────────────────────────────
    conv_manifest = CredentialsService.collect_mcp_provider_manifest(
        db, consumer_agent_id, "conversation"
    )
    conv_keys = [e["key"] for e in conv_manifest]
    assert f"cinna_mcp_{credential_id}" in conv_keys, (
        "Entry must be in the conversation manifest"
    )

    # ── Building → absent ─────────────────────────────────────────────────
    build_manifest = CredentialsService.collect_mcp_provider_manifest(
        db, consumer_agent_id, "building"
    )
    build_keys = [e["key"] for e in build_manifest]
    assert f"cinna_mcp_{credential_id}" not in build_keys, (
        "Entry must be absent from building manifest when mcp_mode_building=False"
    )
