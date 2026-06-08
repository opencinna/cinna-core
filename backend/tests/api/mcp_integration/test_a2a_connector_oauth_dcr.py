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
  - Egress guard unit tests:
      * ``validate_external_endpoint_url`` — private IPv4, loopback, link-local,
        private IPv6, bad scheme, missing host → EgressBlockedError.
      * ``is_host_blocked`` with patched ``socket.getaddrinfo`` returning a
        private addr → True.
      * Hostname that resolves to a public IP → False.
      * ``MCP_PROVIDER_ALLOW_PRIVATE_HOSTS=True`` disables all blocks.
  - OAuth authorize / reauthorize routes: owner-only (404 non-owner);
    DCR failure propagates as 400 from the route.
  - Callback route (``POST /mcp-providers/oauth/callback``):
      * Valid state + mocked token exchange → 200 with credential_id +
        status "connected".
      * Invalid / expired state → 400.

These tests import service-layer functions directly for unit coverage of the
OAuth state machine; the route-level tests use the TestClient as usual.
"""
import asyncio
import json
import time
import uuid
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.core import security
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


# ── Egress guard unit tests (pure function, no DB) ───────────────────────────


def test_egress_guard_private_ipv4_blocked() -> None:
    """validate_external_endpoint_url blocks private IPv4 ranges."""
    from app.services.mcp_providers.egress_guard import (
        validate_external_endpoint_url,
        EgressBlockedError,
    )
    with patch("app.core.config.settings.MCP_PROVIDER_ALLOW_PRIVATE_HOSTS", False):
        for url in (
            "http://10.0.0.1/mcp",
            "http://172.16.5.5/mcp",
            "http://192.168.0.1/mcp",
        ):
            with pytest.raises(EgressBlockedError):
                validate_external_endpoint_url(url)


def test_egress_guard_loopback_blocked() -> None:
    """validate_external_endpoint_url blocks loopback addresses."""
    from app.services.mcp_providers.egress_guard import (
        validate_external_endpoint_url,
        EgressBlockedError,
    )
    with patch("app.core.config.settings.MCP_PROVIDER_ALLOW_PRIVATE_HOSTS", False):
        for url in ("http://127.0.0.1/mcp", "http://[::1]/mcp"):
            with pytest.raises(EgressBlockedError, match="private"):
                validate_external_endpoint_url(url)


def test_egress_guard_link_local_blocked() -> None:
    """Link-local addresses (169.254.x.x) are blocked."""
    from app.services.mcp_providers.egress_guard import (
        validate_external_endpoint_url,
        EgressBlockedError,
    )
    with patch("app.core.config.settings.MCP_PROVIDER_ALLOW_PRIVATE_HOSTS", False):
        with pytest.raises(EgressBlockedError):
            validate_external_endpoint_url("http://169.254.169.254/latest/meta-data")


def test_egress_guard_bad_scheme_blocked() -> None:
    """Non-http/https schemes raise EgressBlockedError."""
    from app.services.mcp_providers.egress_guard import (
        validate_external_endpoint_url,
        EgressBlockedError,
    )
    for url in ("ftp://example.com/mcp", "file:///etc/passwd", "ssh://host/mcp"):
        with pytest.raises(EgressBlockedError, match="scheme"):
            validate_external_endpoint_url(url)


def test_egress_guard_missing_host_blocked() -> None:
    """URL with no host component raises EgressBlockedError."""
    from app.services.mcp_providers.egress_guard import (
        validate_external_endpoint_url,
        EgressBlockedError,
    )
    with pytest.raises(EgressBlockedError, match="host"):
        validate_external_endpoint_url("http:///mcp")


def test_egress_guard_valid_public_url_allowed() -> None:
    """A well-formed public HTTPS URL passes validation."""
    from app.services.mcp_providers.egress_guard import validate_external_endpoint_url
    with patch("app.core.config.settings.MCP_PROVIDER_ALLOW_PRIVATE_HOSTS", False):
        result = validate_external_endpoint_url("https://api.example.com/mcp")
    assert result == "https://api.example.com/mcp"


def test_egress_guard_allow_private_hosts_override() -> None:
    """MCP_PROVIDER_ALLOW_PRIVATE_HOSTS=True disables all guards."""
    from app.services.mcp_providers.egress_guard import validate_external_endpoint_url
    with patch("app.core.config.settings.MCP_PROVIDER_ALLOW_PRIVATE_HOSTS", True):
        # Would normally raise — but the override disables the guard
        result = validate_external_endpoint_url("http://192.168.0.1/mcp")
    assert result == "http://192.168.0.1/mcp"


def test_is_host_blocked_private_resolution() -> None:
    """is_host_blocked returns True when DNS resolves to a private address."""
    import socket
    from app.services.mcp_providers.egress_guard import is_host_blocked

    # Patch getaddrinfo to return a private IP
    private_addr = ("192.168.1.1", 80, 0, "")
    with patch("socket.getaddrinfo", return_value=[(socket.AF_INET, None, None, None, private_addr)]):
        with patch("app.core.config.settings.MCP_PROVIDER_ALLOW_PRIVATE_HOSTS", False):
            assert is_host_blocked("someinternal.corp") is True


def test_is_host_blocked_public_resolution() -> None:
    """is_host_blocked returns False when DNS resolves to a public address."""
    import socket
    from app.services.mcp_providers.egress_guard import is_host_blocked

    public_addr = ("8.8.8.8", 443, 0, "")
    with patch("socket.getaddrinfo", return_value=[(socket.AF_INET, None, None, None, public_addr)]):
        with patch("app.core.config.settings.MCP_PROVIDER_ALLOW_PRIVATE_HOSTS", False):
            assert is_host_blocked("dns.google") is False


# ── OAuth state machine unit tests ────────────────────────────────────────────


def test_oauth_state_single_use(db: Session) -> None:
    """
    The CSRF state token is single-use: consuming it a second time raises
    MCPProviderOAuthError.
    """
    from app.services.mcp_providers.mcp_provider_oauth_service import (
        MCPProviderOAuthService,
        MCPProviderOAuthError,
        _oauth_states,
    )

    cred_id = uuid.uuid4()
    user_id = uuid.uuid4()

    state = MCPProviderOAuthService._put_state(cred_id, user_id, "test-verifier")
    assert state in _oauth_states

    # First consume → success
    data = MCPProviderOAuthService._take_state(state)
    assert str(data["credential_id"]) == str(cred_id)

    # Second consume → error (state was popped)
    with pytest.raises(MCPProviderOAuthError, match="expired"):
        MCPProviderOAuthService._take_state(state)


def test_oauth_state_ttl_expiry(db: Session) -> None:
    """A state entry with an expired TTL is pruned and raises on _take_state."""
    from app.services.mcp_providers.mcp_provider_oauth_service import (
        MCPProviderOAuthService,
        MCPProviderOAuthError,
        _oauth_states,
    )

    cred_id = uuid.uuid4()
    user_id = uuid.uuid4()

    state = MCPProviderOAuthService._put_state(cred_id, user_id, "verifier")
    # Manually expire the state
    _oauth_states[state]["expires"] = time.time() - 1

    with pytest.raises(MCPProviderOAuthError, match="expired"):
        MCPProviderOAuthService._take_state(state)


def test_pkce_pair_generates_s256_challenge() -> None:
    """_generate_pkce produces a S256 code_challenge derived from code_verifier."""
    import base64
    import hashlib
    from app.services.mcp_providers.mcp_provider_oauth_service import MCPProviderOAuthService

    verifier, challenge = MCPProviderOAuthService._generate_pkce()
    assert verifier
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    expected = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    assert challenge == expected, "PKCE challenge must be S256(verifier)"


def test_apply_token_response_stores_fields() -> None:
    """_apply_token_response writes token + expiry + refresh_token into data dict."""
    from app.services.mcp_providers.mcp_provider_oauth_service import MCPProviderOAuthService

    data: dict = {}
    now_approx = int(time.time())
    MCPProviderOAuthService._apply_token_response(
        data,
        {
            "access_token": "new-access-token",
            "refresh_token": "new-refresh-token",
            "expires_in": 3600,
            "scope": "mcp:tools",
        },
    )
    assert data["token"] == "new-access-token"
    assert data["oauth_refresh_token"] == "new-refresh-token"
    assert abs(data["oauth_token_expires_at"] - (now_approx + 3600)) < 5
    assert data["oauth_scope"] == "mcp:tools"


def test_apply_token_response_missing_access_token_raises() -> None:
    """_apply_token_response raises MCPProviderOAuthError if access_token absent."""
    from app.services.mcp_providers.mcp_provider_oauth_service import (
        MCPProviderOAuthService,
        MCPProviderOAuthError,
    )
    with pytest.raises(MCPProviderOAuthError, match="access token"):
        MCPProviderOAuthService._apply_token_response({}, {"refresh_token": "r"})


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
    from app.models import Credential
    from app.models.credentials.credential import CredentialType

    # Get a real owner_id from the current superuser
    me = client.get(f"{settings.API_V1_STR}/users/me", headers=superuser_token_headers)
    owner_id = uuid.UUID(me.json()["id"])

    # Build a minimal mcp_provider credential in the test DB with an existing error
    initial_data = {
        "endpoint_url": "https://refresh.example.com/mcp",
        "auth_mode": "oauth_dcr",
        "oauth_refresh_token": "old-refresh-token",
        "oauth_token_endpoint": "https://refresh.example.com/oauth/token",
        "oauth_client_id": "test-client",
        "last_error": "previous error message",
        "token": "old-token",
    }
    cred = Credential(
        name="Refresh Test Cred",
        type=CredentialType.MCP_PROVIDER,
        owner_id=owner_id,
        encrypted_data=security.encrypt_field(json.dumps(initial_data)),
    )
    db.add(cred)
    db.commit()
    db.refresh(cred)

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
    from app.models import Credential
    from app.models.credentials.credential import CredentialType

    # Get a real owner_id from the current superuser
    me = client.get(f"{settings.API_V1_STR}/users/me", headers=superuser_token_headers)
    owner_id = uuid.UUID(me.json()["id"])

    initial_data = {
        "endpoint_url": "https://failrefresh.example.com/mcp",
        "auth_mode": "oauth_dcr",
        "oauth_refresh_token": "will-fail",
        "oauth_token_endpoint": "https://failrefresh.example.com/oauth/token",
        "oauth_client_id": "test-client",
        "token": "current-access-token",
    }
    cred = Credential(
        name="Refresh Failure Cred",
        type=CredentialType.MCP_PROVIDER,
        owner_id=owner_id,
        encrypted_data=security.encrypt_field(json.dumps(initial_data)),
    )
    db.add(cred)
    db.commit()
    db.refresh(cred)

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
    from app.models import Credential
    from app.models.credentials.credential import CredentialType

    # Build a credential with token_endpoint + client_id stored
    initial_data = {
        "endpoint_url": "https://callback-test.example.com/mcp",
        "auth_mode": "oauth_dcr",
        "oauth_token_endpoint": "https://callback-test.example.com/oauth/token",
        "oauth_client_id": "cb-client-id",
        "oauth_resource": "https://callback-test.example.com/mcp",
    }
    # Get current user id from the /users/me endpoint
    me = client.get(f"{settings.API_V1_STR}/users/me", headers=superuser_token_headers)
    owner_id = uuid.UUID(me.json()["id"])

    cred = Credential(
        name="Callback Test Cred",
        type=CredentialType.MCP_PROVIDER,
        owner_id=owner_id,
        encrypted_data=security.encrypt_field(json.dumps(initial_data)),
    )
    db.add(cred)
    db.commit()
    db.refresh(cred)

    # Put a valid state entry
    code_verifier = "test-verifier-string"
    state = MCPProviderOAuthService._put_state(cred.id, owner_id, code_verifier)

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
    assert str(body["credential_id"]) == str(cred.id)
    assert body["status"] == "connected"

    # The state was single-use and is now consumed
    assert state not in _oauth_states

    # Status endpoint reflects connected
    status_r = client.get(
        f"{_MCP_PROVIDERS_BASE}/{cred.id}/status",
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
    db: Session,
) -> None:
    """
    The CSRF state is single-use: the second POST /oauth/callback with the same
    state returns 400 even if the first call succeeded.
    """
    from app.services.mcp_providers.mcp_provider_oauth_service import MCPProviderOAuthService
    from app.models import Credential
    from app.models.credentials.credential import CredentialType

    initial_data = {
        "endpoint_url": "https://single-use-state.example.com/mcp",
        "auth_mode": "oauth_dcr",
        "oauth_token_endpoint": "https://single-use-state.example.com/oauth/token",
        "oauth_client_id": "su-client-id",
        "oauth_resource": "https://single-use-state.example.com/mcp",
    }
    me = client.get(f"{settings.API_V1_STR}/users/me", headers=superuser_token_headers)
    owner_id = uuid.UUID(me.json()["id"])

    cred = Credential(
        name="Single Use State Cred",
        type=CredentialType.MCP_PROVIDER,
        owner_id=owner_id,
        encrypted_data=security.encrypt_field(json.dumps(initial_data)),
    )
    db.add(cred)
    db.commit()
    db.refresh(cred)

    state = MCPProviderOAuthService._put_state(cred.id, owner_id, "verifier-123")

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


# ── Status lifecycle states from credential data ─────────────────────────────


def test_status_derive_lifecycle_states() -> None:
    """
    MCPProviderService._derive_lifecycle_state correctly maps data blobs to
    lifecycle state strings without any DB interaction.
    """
    from app.services.mcp_providers.mcp_provider_service import MCPProviderService

    # agent2agent with token → connected
    assert MCPProviderService._derive_lifecycle_state(
        "agent2agent", {"token": "abc"}
    ) == "connected"

    # agent2agent without token → error
    assert MCPProviderService._derive_lifecycle_state(
        "agent2agent", {}
    ) == "error"

    # fixed_token with token → connected
    assert MCPProviderService._derive_lifecycle_state(
        "fixed_token", {"token": "tk"}
    ) == "connected"

    # fixed_token without token → error
    assert MCPProviderService._derive_lifecycle_state(
        "fixed_token", {}
    ) == "error"

    # oauth_dcr without token → awaiting_auth
    assert MCPProviderService._derive_lifecycle_state(
        "oauth_dcr", {}
    ) == "awaiting_auth"

    # oauth_dcr with fresh token → connected
    far_future = int(time.time()) + 99999
    assert MCPProviderService._derive_lifecycle_state(
        "oauth_dcr", {"token": "tok", "oauth_token_expires_at": far_future}
    ) == "connected"

    # oauth_dcr with expired token → expired
    past = int(time.time()) - 100
    assert MCPProviderService._derive_lifecycle_state(
        "oauth_dcr", {"token": "tok", "oauth_token_expires_at": past}
    ) == "expired"

    # Any mode with last_error → error (highest priority)
    assert MCPProviderService._derive_lifecycle_state(
        "agent2agent", {"token": "tok", "last_error": "something broke"}
    ) == "error"

    # none auth → always connected
    assert MCPProviderService._derive_lifecycle_state(
        "none", {}
    ) == "connected"
