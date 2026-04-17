"""Backend tests for the Desktop App Authentication feature.

Tests cover the consent-page OAuth 2.0 + PKCE flow:
  1. Instance discovery (/.well-known/cinna-desktop)
  2. Client management: list + revoke (lazy registration replaces POST /clients)
  3. Authorization via consent flow (public /authorize → /consent → code)
  4. GET /requests/{nonce} metadata endpoint
  5. POST /consent deny + approve
  6. Token exchange (code → access_token + refresh_token + client_id)
  7. Refresh token rotation (happy path + replay detection)
  8. Client revocation cascades to tokens
  9. Redirect URI validation (security guard)
  10. New client_id in token response (lazy registration)
  11. Consent nonce reuse rejection
  12. Expired consent nonce rejection
  13. Cross-user client_id rejection in consent
"""
import uuid
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.desktop_auth import (
    exchange_code_for_tokens,
    generate_pkce_pair,
    get_authorization_code,
    initiate_authorize,
    list_desktop_clients,
    refresh_access_token,
    revoke_desktop_client,
    submit_consent,
)
from tests.utils.user import create_random_user_with_headers

_BASE = f"{settings.API_V1_STR}/desktop-auth"


# ── Test: Instance discovery ────────────────────────────────────────────────


def test_instance_discovery(client: TestClient) -> None:
    """GET /.well-known/cinna-desktop returns RFC 8414-shaped metadata."""
    r = client.get("/.well-known/cinna-desktop")
    assert r.status_code == 200
    data = r.json()
    assert "instance_name" in data
    # RFC 8414 (OAuth 2.0 Authorization Server Metadata) field names
    assert "authorization_endpoint" in data
    assert "token_endpoint" in data
    assert "userinfo_endpoint" in data
    assert "version" in data
    assert data["desktop_auth_enabled"] is True
    assert "/desktop-auth/authorize" in data["authorization_endpoint"]
    assert "/desktop-auth/token" in data["token_endpoint"]
    assert "/desktop-auth/userinfo" in data["userinfo_endpoint"]


# ── Test: Client management via lazy registration ───────────────────────────


def test_client_management_lifecycle(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Client management lifecycle using lazy registration:
      1. GET /clients → initially empty (for fresh user state within test)
      2. Complete lazy-registration consent flow → client created automatically
      3. GET /clients → client appears
      4. Auth guard: unauthenticated GET /clients is rejected
      5. Other user cannot see our client
      6. Other user cannot revoke our client
      7. DELETE /clients/{client_id} → client revoked
      8. GET /clients → client is gone
    """
    # ── Phase 1: Authorize (lazy) → consent → get code ───────────────────
    verifier, challenge = generate_pkce_pair()
    nonce = initiate_authorize(
        client,
        code_challenge=challenge,
        device_name="My MacBook",
        platform="macos",
        app_version="2.1.0",
    )

    # ── Phase 2: Approve consent → authorization code ─────────────────────
    result = submit_consent(client, superuser_token_headers, nonce, action="approve")
    redirect_to = result["redirect_to"]
    parsed = urlparse(redirect_to)
    code_params = parse_qs(parsed.query)
    assert "code" in code_params, f"No code in redirect_to: {redirect_to}"
    code = code_params["code"][0]

    # ── Phase 3: GET /clients → lazy-created client should appear ─────────
    clients_before = list_desktop_clients(client, superuser_token_headers)
    # The newly created client should be visible
    assert any(c["device_name"] == "My MacBook" for c in clients_before), (
        f"Expected 'My MacBook' client in list: {clients_before}"
    )
    new_client = next(c for c in clients_before if c["device_name"] == "My MacBook")
    client_id = new_client["client_id"]
    assert new_client["platform"] == "macos"
    assert new_client["app_version"] == "2.1.0"
    assert new_client["is_revoked"] is False

    # ── Phase 4: Auth guard ───────────────────────────────────────────────
    assert client.get(f"{_BASE}/clients").status_code in (401, 403)

    # ── Phase 5: Other user isolation ─────────────────────────────────────
    _other_user, other_headers = create_random_user_with_headers(client)
    other_clients = list_desktop_clients(client, other_headers)
    assert not any(c["client_id"] == client_id for c in other_clients)

    # ── Phase 6: Other user cannot revoke our client ──────────────────────
    r = client.delete(f"{_BASE}/clients/{client_id}", headers=other_headers)
    assert r.status_code == 404

    # ── Phase 7: Revoke ───────────────────────────────────────────────────
    revoke_desktop_client(client, superuser_token_headers, client_id)

    # ── Phase 8: List → client is gone ───────────────────────────────────
    clients_after = list_desktop_clients(client, superuser_token_headers)
    assert not any(c["client_id"] == client_id for c in clients_after)


# ── Test: GET /requests/{nonce} metadata endpoint ──────────────────────────


def test_get_auth_request_metadata(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    GET /requests/{nonce} scenarios:
      1. Valid pending nonce → returns display metadata
      2. Unknown nonce → 404
      3. After approval, the nonce is marked used → 404
    """
    # ── Phase 1: Create a pending request ─────────────────────────────────
    _verifier, challenge = generate_pkce_pair()
    nonce = initiate_authorize(
        client,
        code_challenge=challenge,
        device_name="Test Laptop",
        platform="linux",
        app_version="3.0.0",
    )

    r = client.get(f"{_BASE}/requests/{nonce}")
    assert r.status_code == 200, f"Expected 200 for valid nonce: {r.text}"
    data = r.json()
    assert data["device_name"] == "Test Laptop"
    assert data["platform"] == "linux"
    assert data["app_version"] == "3.0.0"
    assert data["client_id"] is None  # lazy registration
    assert "expires_at" in data

    # ── Phase 2: Unknown nonce → 404 ─────────────────────────────────────
    r_unknown = client.get(f"{_BASE}/requests/totallyfakenonce1234567890abcdef12")
    assert r_unknown.status_code == 404

    # ── Phase 3: After approval, nonce is used → 404 ─────────────────────
    submit_consent(client, superuser_token_headers, nonce, action="approve")
    r_used = client.get(f"{_BASE}/requests/{nonce}")
    assert r_used.status_code == 404


# ── Test: Full authorization code + token flow with lazy registration ───────


def test_full_oauth_pkce_flow_lazy_registration(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Full OAuth 2.0 + PKCE flow using lazy client registration:
      1. GET /authorize (no client_id, provide device_name) → 307 to consent page
      2. GET /requests/{nonce} → display metadata
      3. POST /consent approve → redirect_to with authorization code
      4. GET /clients → discover newly created client_id
      5. POST /token (authorization_code) → access_token + refresh_token + client_id
      6. client_id in token response matches discovered client
      7. Use access_token with existing API endpoint (GET /users/me)
      8. Authorization code cannot be reused (single-use)
    """
    # ── Phase 1: Authorize (lazy) ─────────────────────────────────────────
    verifier, challenge = generate_pkce_pair()
    nonce = initiate_authorize(
        client,
        code_challenge=challenge,
        device_name="Dev MacBook",
        platform="macos",
        app_version="1.0.0",
    )
    assert nonce  # non-empty

    # ── Phase 2: GET /requests/{nonce} ────────────────────────────────────
    meta = client.get(f"{_BASE}/requests/{nonce}").json()
    assert meta["device_name"] == "Dev MacBook"

    # ── Phase 3: POST /consent approve → code + client_id ─────────────────
    consent_result = submit_consent(client, superuser_token_headers, nonce)
    redirect_to = consent_result["redirect_to"]
    parsed = urlparse(redirect_to)
    code_params = parse_qs(parsed.query)
    assert "code" in code_params, f"No code in: {redirect_to}"
    assert "client_id" in code_params, (
        f"Lazy-reg callback must include client_id: {redirect_to}"
    )
    code = code_params["code"][0]
    callback_client_id = code_params["client_id"][0]

    # ── Phase 4: client_id in callback matches /clients listing ───────────
    clients = list_desktop_clients(client, superuser_token_headers)
    lazy_client = next(
        (c for c in clients if c["device_name"] == "Dev MacBook"), None
    )
    assert lazy_client is not None, "Lazy-registered client not found in client list"
    client_id = lazy_client["client_id"]
    assert client_id  # non-empty
    assert callback_client_id == client_id

    # ── Phase 5: Exchange code for tokens ─────────────────────────────────
    tokens = exchange_code_for_tokens(client, client_id, code, verifier)
    assert tokens["token_type"] == "bearer"
    assert tokens["access_token"]
    assert tokens["refresh_token"]
    assert tokens["expires_in"] == settings.DESKTOP_ACCESS_TOKEN_EXPIRE_MINUTES * 60
    assert "client_id" in tokens  # new field

    # ── Phase 6: client_id in token response matches ───────────────────────
    assert tokens["client_id"] == client_id

    # ── Phase 7: Access token works with existing API ─────────────────────
    desktop_headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    r = client.get(f"{settings.API_V1_STR}/users/me", headers=desktop_headers)
    assert r.status_code == 200

    # ── Phase 8: Code cannot be reused ───────────────────────────────────
    r_reuse = client.post(
        f"{_BASE}/token",
        json={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": code,
            "redirect_uri": "http://localhost:19836/callback",
            "code_verifier": verifier,
        },
    )
    assert r_reuse.status_code == 400
    assert r_reuse.json()["detail"] == "invalid_grant"


# ── Test: Authorize validation ──────────────────────────────────────────────


def test_authorize_validation(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    GET /authorize validation:
      1. No client_id and no device_name → 400
      2. Wrong code_challenge_method → 400
      3. Invalid redirect_uri → 400
      4. Non-existent client_id → 400 (invalid_client)
      5. Valid with device_name only (lazy) → 307 redirect
      6. GET /authorize is now public (no auth required)
    """
    # ── Phase 1: No client_id and no device_name → 400 ───────────────────
    r = client.get(
        f"{_BASE}/authorize",
        params={
            "redirect_uri": "http://localhost:19836/callback",
            "code_challenge": "abc123",
            "state": "test",
        },
        follow_redirects=False,
    )
    assert r.status_code == 400
    assert "client_id or device_name" in r.json()["detail"]

    # ── Phase 2: Wrong code_challenge_method → 400 ───────────────────────
    r = client.get(
        f"{_BASE}/authorize",
        params={
            "redirect_uri": "http://localhost:19836/callback",
            "code_challenge": "abc123",
            "code_challenge_method": "plain",
            "state": "test",
            "device_name": "Test",
        },
        follow_redirects=False,
    )
    assert r.status_code == 400

    # ── Phase 3: Invalid redirect_uri → 400 ───────────────────────────────
    r = client.get(
        f"{_BASE}/authorize",
        params={
            "redirect_uri": "https://evil.com/callback",
            "code_challenge": "abc123",
            "state": "test",
            "device_name": "Test",
        },
        follow_redirects=False,
    )
    assert r.status_code == 400

    # ── Phase 4: Non-existent client_id → 400 ────────────────────────────
    r = client.get(
        f"{_BASE}/authorize",
        params={
            "redirect_uri": "http://localhost:19836/callback",
            "code_challenge": "abc123",
            "state": "test",
            "client_id": "does-not-exist",
        },
        follow_redirects=False,
    )
    assert r.status_code == 400
    assert r.json()["detail"] == "invalid_client"

    # ── Phase 5: Valid (lazy) → 307 redirect ─────────────────────────────
    _verifier, challenge = generate_pkce_pair()
    r = client.get(
        f"{_BASE}/authorize",
        params={
            "redirect_uri": "http://localhost:19836/callback",
            "code_challenge": challenge,
            "state": "test",
            "device_name": "My Device",
        },
        follow_redirects=False,
    )
    assert r.status_code in (302, 307)
    assert "/desktop-auth/consent" in r.headers["location"]

    # ── Phase 6: Public endpoint — no JWT needed ──────────────────────────
    _verifier2, challenge2 = generate_pkce_pair()
    r_public = client.get(
        f"{_BASE}/authorize",
        params={
            "redirect_uri": "http://localhost:19836/callback",
            "code_challenge": challenge2,
            "state": "test",
            "device_name": "No Auth Device",
        },
        follow_redirects=False,
        # No auth headers
    )
    assert r_public.status_code in (302, 307), (
        f"Expected redirect without auth, got: {r_public.status_code} {r_public.text}"
    )


# ── Test: POST /consent deny ────────────────────────────────────────────────


def test_consent_deny(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    POST /consent with action=deny:
      1. Initiate authorize flow
      2. Deny consent → redirect_to contains error=access_denied
      3. Nonce cannot be reused after deny
    """
    # ── Phase 1: Initiate ─────────────────────────────────────────────────
    _verifier, challenge = generate_pkce_pair()
    nonce = initiate_authorize(
        client,
        code_challenge=challenge,
        device_name="Denied Device",
    )

    # ── Phase 2: Deny ─────────────────────────────────────────────────────
    result = submit_consent(client, superuser_token_headers, nonce, action="deny")
    redirect_to = result["redirect_to"]
    assert "error=access_denied" in redirect_to
    assert "state=" in redirect_to

    # ── Phase 3: Nonce reuse → rejected ──────────────────────────────────
    r_reuse = client.post(
        f"{_BASE}/consent",
        headers=superuser_token_headers,
        json={"request_nonce": nonce, "action": "approve"},
    )
    assert r_reuse.status_code == 400
    assert r_reuse.json()["detail"] == "invalid_or_expired_request"


# ── Test: Consent nonce reuse ────────────────────────────────────────────────


def test_consent_nonce_reuse_rejected(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Nonce reuse: POST /consent twice with same nonce → second call rejected.
    """
    _verifier, challenge = generate_pkce_pair()
    nonce = initiate_authorize(
        client,
        code_challenge=challenge,
        device_name="Reuse Test Device",
    )

    # First consent → OK
    submit_consent(client, superuser_token_headers, nonce, action="approve")

    # Second consent with same nonce → 400
    r = client.post(
        f"{_BASE}/consent",
        headers=superuser_token_headers,
        json={"request_nonce": nonce, "action": "approve"},
    )
    assert r.status_code == 400
    assert r.json()["detail"] == "invalid_or_expired_request"


# ── Test: Cross-user client_id rejection ────────────────────────────────────


def test_consent_cross_user_client_rejected(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    POST /consent with a client_id belonging to a different user → 403.
      1. User A runs lazy-registration flow → gets a client
      2. User B initiates /authorize with User A's client_id
      3. User B tries to consent → 403 (client belongs to User A)
    """
    # ── Phase 1: User A creates a client via lazy registration ────────────
    verifier_a, challenge_a = generate_pkce_pair()
    code_a = get_authorization_code(
        client,
        superuser_token_headers,
        code_challenge=challenge_a,
        device_name="User A Device",
    )
    clients_a = list_desktop_clients(client, superuser_token_headers)
    client_a = next(c for c in clients_a if c["device_name"] == "User A Device")
    client_id_a = client_a["client_id"]

    # ── Phase 2: Create User B ────────────────────────────────────────────
    _other_user, other_headers = create_random_user_with_headers(client)

    # ── Phase 3: User B initiates /authorize with User A's client_id ──────
    _verifier_b, challenge_b = generate_pkce_pair()
    nonce_b = initiate_authorize(
        client,
        code_challenge=challenge_b,
        existing_client_id=client_id_a,
        device_name=None,  # client_id provided, device_name not needed
    )

    # ── Phase 4: User B tries to consent → 403 ───────────────────────────
    r = client.post(
        f"{_BASE}/consent",
        headers=other_headers,
        json={"request_nonce": nonce_b, "action": "approve"},
    )
    assert r.status_code == 403
    assert r.json()["detail"] == "client_not_found_or_forbidden"


# ── Test: Refresh token rotation ────────────────────────────────────────────


def test_refresh_token_rotation(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Refresh token rotation happy path:
      1. Obtain initial token pair via lazy-registration + code exchange
      2. Refresh → new access_token + new refresh_token
      3. New access_token works with API
      4. New refresh_token can itself be rotated
      5. (Separate client) old refresh_token replay is rejected
    """
    # ── Phase 1: Obtain initial token pair ───────────────────────────────
    verifier, challenge = generate_pkce_pair()
    code = get_authorization_code(
        client,
        superuser_token_headers,
        code_challenge=challenge,
        device_name="Rotation Device",
    )
    clients = list_desktop_clients(client, superuser_token_headers)
    reg_client = next(c for c in clients if c["device_name"] == "Rotation Device")
    client_id = reg_client["client_id"]
    tokens = exchange_code_for_tokens(client, client_id, code, verifier)
    original_refresh = tokens["refresh_token"]

    # ── Phase 2: Refresh → new pair ──────────────────────────────────────
    new_tokens = refresh_access_token(client, client_id, original_refresh)
    assert new_tokens["refresh_token"] != original_refresh
    assert new_tokens["access_token"]
    assert new_tokens["token_type"] == "bearer"
    assert "client_id" in new_tokens  # client_id present in refresh response too

    # ── Phase 3: New access_token works ──────────────────────────────────
    desktop_headers = {"Authorization": f"Bearer {new_tokens['access_token']}"}
    r = client.get(f"{settings.API_V1_STR}/users/me", headers=desktop_headers)
    assert r.status_code == 200

    # ── Phase 4: New refresh_token can be rotated again ──────────────────
    final_tokens = refresh_access_token(client, client_id, new_tokens["refresh_token"])
    assert final_tokens["refresh_token"]
    assert final_tokens["refresh_token"] != new_tokens["refresh_token"]

    # ── Phase 5: Old refresh_token replay is rejected (fresh client) ─────
    # Use a separate client so the replay-triggered family revocation above
    # does not interact with the assertions here.
    v2, c2 = generate_pkce_pair()
    code2 = get_authorization_code(
        client,
        superuser_token_headers,
        code_challenge=c2,
        device_name="Rotation Device 2",
    )
    clients2 = list_desktop_clients(client, superuser_token_headers)
    reg_client2 = next(c for c in clients2 if c["device_name"] == "Rotation Device 2")
    client_id2 = reg_client2["client_id"]
    tok2 = exchange_code_for_tokens(client, client_id2, code2, v2)
    refresh_access_token(client, client_id2, tok2["refresh_token"])  # rotate once

    # Try to use the original (now-revoked) token
    r_old = client.post(
        f"{_BASE}/token",
        json={
            "grant_type": "refresh_token",
            "client_id": client_id2,
            "refresh_token": tok2["refresh_token"],
        },
    )
    assert r_old.status_code == 400
    assert r_old.json()["detail"] == "invalid_grant"


# ── Test: Refresh token replay detection ────────────────────────────────────


def test_refresh_token_replay_detection(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Replay attack detection (RFC 9700 §4.14.2):
      1. Obtain token pair via lazy-reg
      2. Refresh twice to build a rotation chain: T1 → T2 → T3
      3. Replay T1 (already revoked by the first rotation)
      4. Entire family is revoked — the live T3 becomes invalid too
    """
    # ── Phase 1: Obtain initial token pair ───────────────────────────────
    verifier, challenge = generate_pkce_pair()
    code = get_authorization_code(
        client,
        superuser_token_headers,
        code_challenge=challenge,
        device_name="Replay Device",
    )
    clients = list_desktop_clients(client, superuser_token_headers)
    reg_client = next(c for c in clients if c["device_name"] == "Replay Device")
    client_id = reg_client["client_id"]
    tokens = exchange_code_for_tokens(client, client_id, code, verifier)
    t1 = tokens["refresh_token"]

    # ── Phase 2: Build a rotation chain T1 → T2 → T3 ─────────────────────
    t2 = refresh_access_token(client, client_id, t1)["refresh_token"]
    t3 = refresh_access_token(client, client_id, t2)["refresh_token"]

    # ── Phase 3: Replay T1 (already revoked) ─────────────────────────────
    r_replay = client.post(
        f"{_BASE}/token",
        json={
            "grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": t1,
        },
    )
    assert r_replay.status_code == 400
    assert r_replay.json()["detail"] == "invalid_grant"

    # ── Phase 4: The live T3 must now also be invalid ─────────────────────
    r_live = client.post(
        f"{_BASE}/token",
        json={
            "grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": t3,
        },
    )
    assert r_live.status_code == 400
    assert r_live.json()["detail"] == "invalid_grant"


# ── Test: Client revocation cascades to tokens ──────────────────────────────


def test_client_revocation_cascades(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Revoking a client invalidates all its refresh tokens:
      1. Obtain a token pair via lazy-reg
      2. Revoke the client via DELETE /clients/{client_id}
      3. Attempt to refresh → fails (client revoked)
    """
    # ── Phase 1: Obtain token pair ────────────────────────────────────────
    verifier, challenge = generate_pkce_pair()
    code = get_authorization_code(
        client,
        superuser_token_headers,
        code_challenge=challenge,
        device_name="Cascade Device",
    )
    clients = list_desktop_clients(client, superuser_token_headers)
    reg_client = next(c for c in clients if c["device_name"] == "Cascade Device")
    client_id = reg_client["client_id"]
    tokens = exchange_code_for_tokens(client, client_id, code, verifier)
    refresh_tok = tokens["refresh_token"]

    # ── Phase 2: Revoke client ────────────────────────────────────────────
    revoke_desktop_client(client, superuser_token_headers, client_id)

    # ── Phase 3: Refresh fails because client is revoked ─────────────────
    r = client.post(
        f"{_BASE}/token",
        json={
            "grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": refresh_tok,
        },
    )
    assert r.status_code == 400


# ── Test: PKCE verification ──────────────────────────────────────────────────


def test_pkce_verification_failure(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Wrong code_verifier causes token exchange to fail with invalid_grant."""
    _verifier, challenge = generate_pkce_pair()
    code = get_authorization_code(
        client,
        superuser_token_headers,
        code_challenge=challenge,
        device_name="PKCE Test Device",
    )
    clients = list_desktop_clients(client, superuser_token_headers)
    reg_client = next(c for c in clients if c["device_name"] == "PKCE Test Device")
    client_id = reg_client["client_id"]

    # Use wrong verifier
    wrong_verifier = "wrongverifier123456789012345678901234567"
    r = client.post(
        f"{_BASE}/token",
        json={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": code,
            "redirect_uri": "http://localhost:19836/callback",
            "code_verifier": wrong_verifier,
        },
    )
    assert r.status_code == 400
    assert r.json()["detail"] == "invalid_grant"


# ── Test: Redirect URI validation ────────────────────────────────────────────


def test_redirect_uri_validation(client: TestClient) -> None:
    """Only localhost redirect URIs are accepted by the authorize endpoint."""
    _verifier, challenge = generate_pkce_pair()

    invalid_uris = [
        "https://evil.com/callback",
        "http://evil.com/callback",
        "http://localhost/callback",          # no port
        "http://localhost:99/callback",       # port out of range (< 1024)
        "http://localhost:65536/callback",    # port out of range (> 65535)
    ]

    for bad_uri in invalid_uris:
        r = client.get(
            f"{_BASE}/authorize",
            params={
                "redirect_uri": bad_uri,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": "test",
                "device_name": "Test Device",
            },
            follow_redirects=False,
        )
        assert r.status_code == 400, f"Expected 400 for URI {bad_uri!r}, got {r.status_code}"

    # Valid URIs should produce a redirect. Path is unrestricted per RFC 8252 §7.3.
    valid_uris = [
        "http://localhost:19836/callback",
        "http://127.0.0.1:19836/callback",
        "http://127.0.0.1:53484/oauth/callback",
        "http://localhost:12345/",
    ]
    for good_uri in valid_uris:
        _v, c = generate_pkce_pair()
        r = client.get(
            f"{_BASE}/authorize",
            params={
                "redirect_uri": good_uri,
                "code_challenge": c,
                "code_challenge_method": "S256",
                "state": "test",
                "device_name": "Test Device",
            },
            follow_redirects=False,
        )
        assert r.status_code in (302, 307), (
            f"Expected redirect for valid URI {good_uri!r}, got {r.status_code}"
        )


# ── Test: Unsupported grant type ─────────────────────────────────────────────


def test_unsupported_grant_type(client: TestClient) -> None:
    """Unsupported grant_type returns 400 unsupported_grant_type."""
    r = client.post(
        f"{_BASE}/token",
        json={"grant_type": "password", "client_id": "any"},
    )
    assert r.status_code == 400
    assert r.json()["detail"] == "unsupported_grant_type"


# ── Test: Revoke via /revoke endpoint ────────────────────────────────────────


def test_revoke_endpoint(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    POST /revoke endpoint:
      1. Revoke by client_id — token refresh fails afterward
      2. Revoke by refresh_token — family is revoked
      3. Missing both fields → 400
    """
    # ── Phase 1: Revoke by client_id ─────────────────────────────────────
    v1, c1 = generate_pkce_pair()
    code1 = get_authorization_code(
        client,
        superuser_token_headers,
        code_challenge=c1,
        device_name="Revoke Device A",
    )
    clients = list_desktop_clients(client, superuser_token_headers)
    reg1 = next(c for c in clients if c["device_name"] == "Revoke Device A")
    client_id1 = reg1["client_id"]
    tok1 = exchange_code_for_tokens(client, client_id1, code1, v1)

    r_rev = client.post(
        f"{_BASE}/revoke",
        headers=superuser_token_headers,
        json={"client_id": client_id1},
    )
    assert r_rev.status_code == 204

    # Refresh now fails
    r_refresh = client.post(
        f"{_BASE}/token",
        json={
            "grant_type": "refresh_token",
            "client_id": client_id1,
            "refresh_token": tok1["refresh_token"],
        },
    )
    assert r_refresh.status_code == 400

    # ── Phase 2: Revoke by refresh_token ─────────────────────────────────
    v2, c2 = generate_pkce_pair()
    code2 = get_authorization_code(
        client,
        superuser_token_headers,
        code_challenge=c2,
        device_name="Revoke Device B",
    )
    clients2 = list_desktop_clients(client, superuser_token_headers)
    reg2 = next(c for c in clients2 if c["device_name"] == "Revoke Device B")
    client_id2 = reg2["client_id"]
    tok2 = exchange_code_for_tokens(client, client_id2, code2, v2)

    r_rev2 = client.post(
        f"{_BASE}/revoke",
        headers=superuser_token_headers,
        json={"refresh_token": tok2["refresh_token"]},
    )
    assert r_rev2.status_code == 204

    # Refresh using the same token fails (family revoked)
    r_refresh2 = client.post(
        f"{_BASE}/token",
        json={
            "grant_type": "refresh_token",
            "client_id": client_id2,
            "refresh_token": tok2["refresh_token"],
        },
    )
    assert r_refresh2.status_code == 400

    # ── Phase 3: Missing both fields ─────────────────────────────────────
    r_missing = client.post(
        f"{_BASE}/revoke",
        headers=superuser_token_headers,
        json={},
    )
    assert r_missing.status_code == 400


# ── Test: Unknown/invalid tokens ─────────────────────────────────────────────


def test_unknown_token_values(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Unknown authorization code and refresh token values return invalid_grant."""
    # We need a valid client_id to test against — use lazy reg to create one
    v, c = generate_pkce_pair()
    code = get_authorization_code(
        client,
        superuser_token_headers,
        code_challenge=c,
        device_name="Unknown Token Device",
    )
    clients = list_desktop_clients(client, superuser_token_headers)
    reg = next(c for c in clients if c["device_name"] == "Unknown Token Device")
    client_id = reg["client_id"]
    # Exchange the real code so we have a valid client state
    exchange_code_for_tokens(client, client_id, code, v)

    # Unknown authorization code
    r_code = client.post(
        f"{_BASE}/token",
        json={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": "totallyfakecode12345678901234567890123456",
            "redirect_uri": "http://localhost:19836/callback",
            "code_verifier": v,
        },
    )
    assert r_code.status_code == 400
    assert r_code.json()["detail"] == "invalid_grant"

    # Unknown refresh token
    r_refresh = client.post(
        f"{_BASE}/token",
        json={
            "grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": "totallyfakerefreshtoken12345678901234567890123456789012345678",
        },
    )
    assert r_refresh.status_code == 400
    assert r_refresh.json()["detail"] == "invalid_grant"


# ── Test: /token accepts form-urlencoded (OAuth 2.0 standard) ───────────────


def test_token_endpoint_accepts_form_urlencoded(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """POST /token must accept application/x-www-form-urlencoded (RFC 6749 §3.2).

    The Cinna Desktop app sends its token exchange body as form-urlencoded;
    this scenario covers both the code-exchange and refresh-token grants.
    """
    # Obtain an auth code via the full consent flow
    verifier, challenge = generate_pkce_pair()
    code = get_authorization_code(
        client,
        superuser_token_headers,
        code_challenge=challenge,
        device_name="Form Encoded Device",
    )
    clients = list_desktop_clients(client, superuser_token_headers)
    reg_client = next(
        c for c in clients if c["device_name"] == "Form Encoded Device"
    )
    client_id = reg_client["client_id"]

    # ── authorization_code grant via form-urlencoded ─────────────────────
    r = client.post(
        f"{_BASE}/token",
        data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": code,
            "redirect_uri": "http://localhost:19836/callback",
            "code_verifier": verifier,
        },
    )
    assert r.status_code == 200, f"Form token exchange failed: {r.text}"
    tokens = r.json()
    assert tokens["access_token"]
    assert tokens["refresh_token"]
    assert tokens["client_id"] == client_id

    # ── refresh_token grant via form-urlencoded ──────────────────────────
    r_ref = client.post(
        f"{_BASE}/token",
        data={
            "grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": tokens["refresh_token"],
        },
    )
    assert r_ref.status_code == 200, f"Form refresh failed: {r_ref.text}"
    assert r_ref.json()["access_token"]


# ── Test: GET /userinfo ─────────────────────────────────────────────────────


def test_userinfo_endpoint(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    GET /userinfo:
      1. No auth → 401/403
      2. Desktop access token obtained via full flow returns matching user
         profile (sub == user id, email and full_name match /users/me)
      3. Web-session JWT also works (standard CurrentUser dependency)
    """
    # ── Phase 1: Unauthenticated → rejected ──────────────────────────────
    assert client.get(f"{_BASE}/userinfo").status_code in (401, 403)

    # ── Phase 2: Obtain a desktop access token via lazy registration ─────
    verifier, challenge = generate_pkce_pair()
    code = get_authorization_code(
        client,
        superuser_token_headers,
        code_challenge=challenge,
        device_name="UserInfo Device",
    )
    clients = list_desktop_clients(client, superuser_token_headers)
    reg_client = next(c for c in clients if c["device_name"] == "UserInfo Device")
    tokens = exchange_code_for_tokens(
        client, reg_client["client_id"], code, verifier
    )
    desktop_headers = {"Authorization": f"Bearer {tokens['access_token']}"}

    # Cross-check against /users/me so the test stays agnostic to fixture data
    me = client.get(
        f"{settings.API_V1_STR}/users/me", headers=desktop_headers
    ).json()

    r = client.get(f"{_BASE}/userinfo", headers=desktop_headers)
    assert r.status_code == 200, f"userinfo failed: {r.text}"
    data = r.json()
    assert data["sub"] == me["id"]
    assert data["email"] == me["email"]
    assert data["full_name"] == me["full_name"]
    assert data.get("username") == me.get("username")
    # Validate id looks like a UUID
    uuid.UUID(data["sub"])

    # ── Phase 3: Same endpoint works with regular web JWT ────────────────
    r_web = client.get(f"{_BASE}/userinfo", headers=superuser_token_headers)
    assert r_web.status_code == 200
    assert r_web.json()["email"] == me["email"]


# ── Test: Authorize is now public ────────────────────────────────────────────


def test_authorize_is_public(client: TestClient) -> None:
    """GET /authorize without any JWT → succeeds (public endpoint, redirects to consent)."""
    _v, challenge = generate_pkce_pair()
    r = client.get(
        f"{_BASE}/authorize",
        params={
            "redirect_uri": "http://localhost:19836/callback",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "xyz",
            "device_name": "Public Test Device",
        },
        follow_redirects=False,
    )
    # Should redirect to consent page, not reject with 401/403
    assert r.status_code in (302, 307), (
        f"Expected redirect for public /authorize, got {r.status_code}: {r.text}"
    )
    assert "/desktop-auth/consent" in r.headers.get("location", "")
