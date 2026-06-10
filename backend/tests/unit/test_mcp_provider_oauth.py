"""
Unit tests for the MCP-provider OAuth service internals and lifecycle-state
derivation.

Pure in-memory logic — the CSRF state store, PKCE S256 generation, token-response
application, and ``MCPProviderService._derive_lifecycle_state`` need no DB or HTTP.
The full authorize → callback → refresh API flows (and the per-credential
connected state) are exercised in
``tests/api/mcp_integration/test_a2a_connector_oauth_dcr.py``.
"""
import base64
import hashlib
import time
import uuid

import pytest


# ── OAuth state machine ───────────────────────────────────────────────────────


def test_oauth_state_single_use() -> None:
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


def test_oauth_state_ttl_expiry() -> None:
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


# ── PKCE ──────────────────────────────────────────────────────────────────────


def test_pkce_pair_generates_s256_challenge() -> None:
    """_generate_pkce produces a S256 code_challenge derived from code_verifier."""
    from app.services.mcp_providers.mcp_provider_oauth_service import MCPProviderOAuthService

    verifier, challenge = MCPProviderOAuthService._generate_pkce()
    assert verifier
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    expected = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    assert challenge == expected, "PKCE challenge must be S256(verifier)"


# ── Token response application ────────────────────────────────────────────────


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


# ── Lifecycle-state derivation ────────────────────────────────────────────────


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
