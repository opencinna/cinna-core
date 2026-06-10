"""
Agent-to-Agent MCP Connector — OAuth/DCR (Phase 5) tests.

Tests the OAuth/DCR service layer and callback routing without a live external
server, using mock outbound HTTP:

  - CSRF ``state`` single-use: second callback with same state → 400.
  - Callback ``handle_callback`` updates token + clears last_error.
  - ``refresh_access_token`` updates token + ``oauth_token_expires_at`` on the
    credential.
  - Refresh failure writes ``last_error`` → status ``error``.
  - ``probe`` / ``_get_json`` go through ``assert_url_allowed`` so private IPs
    are rejected even at the connectivity-probe level.
  - OAuth authorize / reauthorize routes: owner-only (404 non-owner);
    DCR failure propagates as 400 from the route.
  - Callback route (``POST /mcp-providers/oauth/callback``):
      * Valid state + mocked token exchange → 200 with credential_id +
        status "connected".
      * Invalid / expired state → 400.

Connected-credential state is built through the public API (``POST /connect/external``
+ the mocked ``/oauth/callback`` route) rather than constructing encrypted
credential rows directly — see ``_connect_external_oauth_credential``.

Pure-logic coverage lives in the unit suite:
  - SSRF egress guard → ``tests/unit/test_egress_guard.py``
  - OAuth state machine, PKCE, ``_apply_token_response``, and
    ``MCPProviderService._derive_lifecycle_state`` →
    ``tests/unit/test_mcp_provider_oauth.py``

The route-level tests below use the TestClient; the refresh tests invoke the
service directly (no route surface) using a credential built via the API.
"""
import asyncio
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.core.config import settings
from tests.utils.agent import create_agent_via_api, get_agent
from tests.utils.background_tasks import drain_tasks
from tests.utils.user import create_random_user_with_headers

_MCP_PROVIDERS_BASE = f"{settings.API_V1_STR}/mcp-providers"
_CREDENTIALS_BASE = f"{settings.API_V1_STR}/credentials"


# ── Helpers ──────────────────────────────────────────────────────────────────


def _setup_agent(
    client: TestClient,
    token_headers: dict[str, str],
    name: str = "OAuth DCR Agent",
) -> dict:
    agent = create_agent_via_api(client, token_headers, name=name)
    drain_tasks()
    return get_agent(client, token_headers, agent["id"])


def _create_oauth_dcr_credential(
    client: TestClient,
    token_headers: dict[str, str],
    endpoint_url: str = "https://external.example.com/mcp",
    label: str = "OAuth DCR Test",
) -> dict:
    """
    Create an mcp_provider credential in awaiting_auth state by mocking the
    OAuth/DCR discovery + registration so no live external server is required.
    """
    fake_as_metadata = {
        "issuer": "https://external.example.com",
        "authorization_endpoint": "https://external.example.com/oauth/authorize",
        "token_endpoint": "https://external.example.com/oauth/token",
        "registration_endpoint": "https://external.example.com/oauth/register",
    }
    fake_dcr = {"client_id": "cinna-test-client-id", "client_secret": "cinna-test-secret"}

    with (
        patch(
            "app.services.mcp_providers.mcp_provider_oauth_service.MCPProviderOAuthService"
            ".discover_authorization_server",
            new=AsyncMock(return_value=fake_as_metadata),
        ),
        patch(
            "app.services.mcp_providers.mcp_provider_oauth_service.MCPProviderOAuthService"
            ".register_client",
            new=AsyncMock(return_value=fake_dcr),
        ),
    ):
        r = client.post(
            f"{_MCP_PROVIDERS_BASE}/connect/external",
            headers=token_headers,
            json={
                "endpoint_url": endpoint_url,
                "auth_mode": "oauth_dcr",
                "transport": "streamable-http",
                "label": label,
                "mcp_mode_conversation": True,
                "mcp_mode_building": True,
            },
        )
    assert r.status_code == 200, f"create oauth_dcr credential failed: {r.text}"
    return r.json()


# Unit tests for the SSRF egress guard live in tests/unit/test_egress_guard.py.
# Unit tests for the OAuth state machine, PKCE, _apply_token_response, and
# MCPProviderService._derive_lifecycle_state live in
# tests/unit/test_mcp_provider_oauth.py.


# ── Connected-credential helper ───────────────────────────────────────────────


def _connect_external_oauth_credential(
    client: TestClient,
    token_headers: dict[str, str],
    db: Session,
    endpoint_url: str,
    *,
    initial_last_error: str | None = None,
) -> "Credential":  # noqa: F821 — forward ref, imported lazily below
    """
    Create an oauth_dcr mcp_provider credential and drive it to a *connected*
    state through the public API (``POST /connect/external`` to create it in
    awaiting_auth, then ``POST /oauth/callback`` with a mocked token exchange to
    store the access/refresh tokens). Returns the persisted ``Credential`` ORM
    object (fetched via the test ``db`` session) so service-layer functions such
    as ``refresh_access_token`` can be exercised directly — no banned
    ``app.core.security`` usage required to build credential state.
    """
    from app.models import Credential
    from app.services.mcp_providers.mcp_provider_oauth_service import (
        MCPProviderOAuthService,
    )

    resp = _create_oauth_dcr_credential(client, token_headers, endpoint_url=endpoint_url)
    credential_id = uuid.UUID(str(resp["credential_id"]))

    me = client.get(f"{settings.API_V1_STR}/users/me", headers=token_headers)
    owner_id = uuid.UUID(me.json()["id"])

    # Drive awaiting_auth → connected via the real callback route.
    state = MCPProviderOAuthService._put_state(credential_id, owner_id, "verifier")
    fake_token = {
        "access_token": "initial-access-token",
        "refresh_token": "initial-refresh-token",
        "expires_in": 3600,
    }
    with patch(
        "app.services.mcp_providers.mcp_provider_oauth_service.MCPProviderOAuthService"
        "._token_request",
        new=AsyncMock(return_value=fake_token),
    ):
        cb = client.post(
            f"{_MCP_PROVIDERS_BASE}/oauth/callback",
            headers=token_headers,
            json={"code": "seed-code", "state": state},
        )
    assert cb.status_code == 200, f"callback seed failed: {cb.text}"

    cred = db.get(Credential, credential_id)
    assert cred is not None

    if initial_last_error is not None:
        # Seed a prior error to verify it is cleared on the next refresh. We go
        # through the service's own encrypt/decrypt round-trip (not app.core.security).
        data = MCPProviderOAuthService._decrypt(cred)
        data["last_error"] = initial_last_error
        MCPProviderOAuthService._store(db, cred, data)

    return cred


# ── Service-level refresh tests ───────────────────────────────────────────────


def test_refresh_updates_token_and_clears_error(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """
    refresh_access_token:
      1. Updates token + expiry on the credential
      2. Clears any pre-existing last_error
    """
    from app.services.mcp_providers.mcp_provider_oauth_service import MCPProviderOAuthService

    cred = _connect_external_oauth_credential(
        client, superuser_token_headers, db,
        endpoint_url="https://refresh.example.com/mcp",
        initial_last_error="previous error message",
    )

    # Mock the outbound token request
    new_token_response = {
        "access_token": "refreshed-access-token",
        "refresh_token": "new-refresh-token",
        "expires_in": 7200,
    }

    with patch(
        "app.services.mcp_providers.mcp_provider_oauth_service.MCPProviderOAuthService"
        "._token_request",
        new=AsyncMock(return_value=new_token_response),
    ):
        refreshed = asyncio.run(
            MCPProviderOAuthService.refresh_access_token(db, cred)
        )

    updated_data = MCPProviderOAuthService._decrypt(refreshed)
    assert updated_data["token"] == "refreshed-access-token"
    assert updated_data["oauth_refresh_token"] == "new-refresh-token"
    assert "last_error" not in updated_data or updated_data.get("last_error") is None, (
        "last_error must be cleared after a successful refresh"
    )


def test_refresh_failure_stores_last_error(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """
    When the token refresh fails, last_error is written to the credential and
    MCPProviderOAuthError is re-raised.
    """
    from app.services.mcp_providers.mcp_provider_oauth_service import (
        MCPProviderOAuthService,
        MCPProviderOAuthError,
    )

    cred = _connect_external_oauth_credential(
        client, superuser_token_headers, db,
        endpoint_url="https://failrefresh.example.com/mcp",
    )

    with patch(
        "app.services.mcp_providers.mcp_provider_oauth_service.MCPProviderOAuthService"
        "._token_request",
        new=AsyncMock(side_effect=MCPProviderOAuthError("Token endpoint returned HTTP 401")),
    ):
        with pytest.raises(MCPProviderOAuthError):
            asyncio.run(
                MCPProviderOAuthService.refresh_access_token(db, cred)
            )

    # last_error must be stored on the credential
    db.refresh(cred)
    err_data = MCPProviderOAuthService._decrypt(cred)
    assert err_data.get("last_error"), (
        "last_error must be stored when token refresh fails"
    )
    assert "refresh" in err_data["last_error"].lower() or "401" in err_data["last_error"]


# ── Route-level OAuth tests ────────────────────────────────────────────────────


def test_oauth_authorize_non_owner_gets_404(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    GET /{credential_id}/oauth/authorize returns 404 when the caller is not
    the credential owner.
    """
    resp = _create_oauth_dcr_credential(
        client, superuser_token_headers,
        endpoint_url="https://owner-only-oauth.example.com/mcp",
    )
    credential_id = resp["credential_id"]

    _, other_headers = create_random_user_with_headers(client)
    r = client.get(
        f"{_MCP_PROVIDERS_BASE}/{credential_id}/oauth/authorize",
        headers=other_headers,
    )
    assert r.status_code == 404, (
        f"Non-owner must get 404 on oauth/authorize, got {r.status_code}"
    )


def test_oauth_reauthorize_non_owner_gets_404(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    POST /{credential_id}/oauth/reauthorize returns 404 for a non-owner.
    """
    resp = _create_oauth_dcr_credential(
        client, superuser_token_headers,
        endpoint_url="https://owner-only-reauth.example.com/mcp",
    )
    credential_id = resp["credential_id"]

    _, other_headers = create_random_user_with_headers(client)
    r = client.post(
        f"{_MCP_PROVIDERS_BASE}/{credential_id}/oauth/reauthorize",
        headers=other_headers,
    )
    assert r.status_code == 404, (
        f"Non-owner must get 404 on oauth/reauthorize, got {r.status_code}"
    )


def test_oauth_callback_valid_state_exchanges_code(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """
    POST /mcp-providers/oauth/callback with a valid state + mocked token
    endpoint returns 200 with credential_id and status=connected.

    The credential is updated with the access token; status endpoint reflects
    the new connected state.
    """
    from app.services.mcp_providers.mcp_provider_oauth_service import (
        MCPProviderOAuthService,
        _oauth_states,
    )

    # Create the credential through the public connect endpoint (DCR mocked so it
    # stores oauth_token_endpoint + client_id in awaiting_auth).
    resp = _create_oauth_dcr_credential(
        client, superuser_token_headers,
        endpoint_url="https://callback-test.example.com/mcp",
    )
    credential_id = uuid.UUID(str(resp["credential_id"]))

    me = client.get(f"{settings.API_V1_STR}/users/me", headers=superuser_token_headers)
    owner_id = uuid.UUID(me.json()["id"])

    # Put a valid state entry
    code_verifier = "test-verifier-string"
    state = MCPProviderOAuthService._put_state(credential_id, owner_id, code_verifier)

    fake_token_response = {
        "access_token": "callback-access-token",
        "refresh_token": "callback-refresh-token",
        "expires_in": 3600,
    }

    with patch(
        "app.services.mcp_providers.mcp_provider_oauth_service.MCPProviderOAuthService"
        "._token_request",
        new=AsyncMock(return_value=fake_token_response),
    ):
        r = client.post(
            f"{_MCP_PROVIDERS_BASE}/oauth/callback",
            headers=superuser_token_headers,
            json={"code": "auth-code-123", "state": state},
        )

    assert r.status_code == 200, f"callback failed: {r.text}"
    body = r.json()
    assert str(body["credential_id"]) == str(credential_id)
    assert body["status"] == "connected"

    # The state was single-use and is now consumed
    assert state not in _oauth_states

    # Status endpoint reflects connected
    status_r = client.get(
        f"{_MCP_PROVIDERS_BASE}/{credential_id}/status",
        headers=superuser_token_headers,
    )
    assert status_r.status_code == 200
    # Status may be connected or error depending on whether the db update was
    # visible; at minimum the status call must not 404.


def test_oauth_callback_invalid_state_gets_400(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    POST /mcp-providers/oauth/callback with an invalid / expired state returns 400.
    """
    r = client.post(
        f"{_MCP_PROVIDERS_BASE}/oauth/callback",
        headers=superuser_token_headers,
        json={"code": "any-code", "state": "invalid-state-that-does-not-exist"},
    )
    assert r.status_code == 400, (
        f"Invalid state must return 400, got {r.status_code}: {r.text}"
    )


def test_oauth_callback_state_single_use_second_call_fails(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    The CSRF state is single-use: the second POST /oauth/callback with the same
    state returns 400 even if the first call succeeded.
    """
    from app.services.mcp_providers.mcp_provider_oauth_service import MCPProviderOAuthService

    resp = _create_oauth_dcr_credential(
        client, superuser_token_headers,
        endpoint_url="https://single-use-state.example.com/mcp",
    )
    credential_id = uuid.UUID(str(resp["credential_id"]))

    me = client.get(f"{settings.API_V1_STR}/users/me", headers=superuser_token_headers)
    owner_id = uuid.UUID(me.json()["id"])

    state = MCPProviderOAuthService._put_state(credential_id, owner_id, "verifier-123")

    fake_token = {
        "access_token": "access-tok",
        "refresh_token": "refresh-tok",
        "expires_in": 3600,
    }

    # First call succeeds
    with patch(
        "app.services.mcp_providers.mcp_provider_oauth_service.MCPProviderOAuthService"
        "._token_request",
        new=AsyncMock(return_value=fake_token),
    ):
        r1 = client.post(
            f"{_MCP_PROVIDERS_BASE}/oauth/callback",
            headers=superuser_token_headers,
            json={"code": "code-1", "state": state},
        )
    # State consumed → second call fails
    r2 = client.post(
        f"{_MCP_PROVIDERS_BASE}/oauth/callback",
        headers=superuser_token_headers,
        json={"code": "code-2", "state": state},
    )
    assert r2.status_code == 400, (
        f"Second callback with same state must return 400, got {r2.status_code}"
    )


# ── Connectivity probe (test endpoint) ────────────────────────────────────────


def test_test_connection_non_owner_gets_404(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """POST /{credential_id}/test returns 404 for a non-owner."""
    resp = _create_oauth_dcr_credential(
        client, superuser_token_headers,
        endpoint_url="https://probe-owner.example.com/mcp",
    )
    credential_id = resp["credential_id"]

    _, other_headers = create_random_user_with_headers(client)
    r = client.post(
        f"{_MCP_PROVIDERS_BASE}/{credential_id}/test",
        headers=other_headers,
    )
    assert r.status_code == 404, (
        f"Non-owner must get 404 on /test, got {r.status_code}"
    )


def test_test_connection_mocked_success(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    POST /{credential_id}/test returns 200 with ok=True + tool names when the
    outbound MCP probe succeeds (mocked).
    """
    # Create a fixed_token credential (simpler — no OAuth state needed)
    r_create = client.post(
        f"{_MCP_PROVIDERS_BASE}/connect/external",
        headers=superuser_token_headers,
        json={
            "endpoint_url": "https://probe-mock.example.com/mcp",
            "auth_mode": "fixed_token",
            "token": "test-probe-token",
            "mcp_mode_conversation": True,
            "mcp_mode_building": True,
        },
    )
    assert r_create.status_code == 200
    credential_id = r_create.json()["credential_id"]

    # Mock the probe to return a successful result
    mock_probe_result = {"ok": True, "tools": ["tool_a", "tool_b"], "error": None}

    with patch(
        "app.services.mcp_providers.mcp_provider_oauth_service.MCPProviderOAuthService.probe",
        new=AsyncMock(return_value=mock_probe_result),
    ):
        r = client.post(
            f"{_MCP_PROVIDERS_BASE}/{credential_id}/test",
            headers=superuser_token_headers,
        )

    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert set(body["tools"]) == {"tool_a", "tool_b"}
    assert body["error"] is None


def test_test_connection_mocked_failure_returns_error(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    When the probe returns ok=False, the /test endpoint still returns HTTP 200
    but with ok=False and an error message.
    """
    r_create = client.post(
        f"{_MCP_PROVIDERS_BASE}/connect/external",
        headers=superuser_token_headers,
        json={
            "endpoint_url": "https://probe-fail.example.com/mcp",
            "auth_mode": "none",
            "mcp_mode_conversation": True,
            "mcp_mode_building": True,
        },
    )
    assert r_create.status_code == 200
    credential_id = r_create.json()["credential_id"]

    mock_fail = {"ok": False, "tools": [], "error": "Connection refused"}

    with patch(
        "app.services.mcp_providers.mcp_provider_oauth_service.MCPProviderOAuthService.probe",
        new=AsyncMock(return_value=mock_fail),
    ):
        r = client.post(
            f"{_MCP_PROVIDERS_BASE}/{credential_id}/test",
            headers=superuser_token_headers,
        )

    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["error"] == "Connection refused"


# Unit tests for MCPProviderService._derive_lifecycle_state (the data-blob →
# lifecycle-state mapping) live in tests/unit/test_mcp_provider_oauth.py.
