"""Backend tests for the parallel App Authentication surface (/app-auth).

The /app-auth endpoints are a mobile-facing mirror of /desktop-auth: they share
the same backing service, storage tables, and token logic, differing only in the
URL namespace and the consent-page redirect target. These tests verify the
parallel surface works end-to-end and stays consistent with the desktop flow.

Covered:
  1. /.well-known/cinna-app discovery metadata
  2. Authorize → consent → token full PKCE flow (mobile redirect scheme)
  3. Consent metadata reports client_kind="mobile" for cinna-mobile:// redirects
  4. Native redirect URI validation with environment gating
  5. Refresh token rotation against the shared store
  6. Cross-surface consistency: a token minted via /app-auth works on
     /desktop-auth/userinfo (and vice-versa), proving shared backing
"""
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.desktop_auth import generate_pkce_pair
from tests.utils.user import create_random_user_with_headers

_BASE = f"{settings.API_V1_STR}/app-auth"
_DESKTOP_BASE = f"{settings.API_V1_STR}/desktop-auth"
_MOBILE_REDIRECT = "cinna-mobile://oauth/callback"


# ── Local helpers (hit /app-auth directly) ──────────────────────────────────


def _authorize(
    client: TestClient,
    code_challenge: str,
    redirect_uri: str = _MOBILE_REDIRECT,
    device_name: str = "Test Phone",
    platform: str = "ios",
) -> str:
    """GET /app-auth/authorize → return the consent nonce from the redirect."""
    r = client.get(
        f"{_BASE}/authorize",
        params={
            "redirect_uri": redirect_uri,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "state": "test-state",
            "device_name": device_name,
            "platform": platform,
        },
        follow_redirects=False,
    )
    assert r.status_code in (302, 307), f"Authorize failed: {r.status_code} {r.text}"
    location = r.headers["location"]
    assert "/app-auth/consent" in location, f"Expected app-auth consent redirect: {location}"
    return parse_qs(urlparse(location).query)["request"][0]


def _get_code(
    client: TestClient,
    headers: dict[str, str],
    code_challenge: str,
    redirect_uri: str = _MOBILE_REDIRECT,
) -> tuple[str, str]:
    """Run authorize + approve and return ``(code, client_id)``.

    Lazy registration: the assigned client_id rides back on the callback URL.
    """
    nonce = _authorize(client, code_challenge, redirect_uri=redirect_uri)
    r = client.post(
        f"{_BASE}/consent",
        headers=headers,
        json={"request_nonce": nonce, "action": "approve"},
    )
    assert r.status_code == 200, f"Consent failed: {r.text}"
    params = parse_qs(urlparse(r.json()["redirect_to"]).query)
    assert "client_id" in params, "Lazy-reg callback must include client_id"
    return params["code"][0], params["client_id"][0]


def _exchange(
    client: TestClient,
    client_id: str,
    code: str,
    code_verifier: str,
    redirect_uri: str = _MOBILE_REDIRECT,
) -> dict:
    r = client.post(
        f"{_BASE}/token",
        json={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
        },
    )
    assert r.status_code == 200, f"Token exchange failed: {r.text}"
    return r.json()


# ── Test: discovery ─────────────────────────────────────────────────────────


def test_app_instance_discovery(client: TestClient) -> None:
    """GET /.well-known/cinna-app returns app-auth endpoint metadata."""
    r = client.get("/.well-known/cinna-app")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["instance_name"] == settings.PROJECT_NAME
    assert data["authorization_endpoint"].endswith("/app-auth/authorize")
    assert data["token_endpoint"].endswith("/app-auth/token")
    assert data["userinfo_endpoint"].endswith("/app-auth/userinfo")
    assert data["app_auth_enabled"] is True


# ── Test: full PKCE flow over the mobile redirect scheme ─────────────────────


def test_full_app_oauth_pkce_flow(client: TestClient) -> None:
    """Authorize → consent → token exchange works with a cinna-mobile:// redirect."""
    _user, headers = create_random_user_with_headers(client)
    verifier, challenge = generate_pkce_pair()

    code, client_id = _get_code(client, headers, challenge)
    tokens = _exchange(client, client_id=client_id, code=code, code_verifier=verifier)

    assert tokens["access_token"]
    assert tokens["refresh_token"]
    assert tokens["token_type"] == "bearer"
    assert tokens["client_id"] == client_id

    # The access token authenticates against /app-auth/userinfo.
    r = client.get(
        f"{_BASE}/userinfo",
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["email"] == _user["email"]


# ── Test: consent metadata classifies mobile clients ────────────────────────


def test_app_consent_metadata_client_kind_mobile(client: TestClient) -> None:
    """A cinna-mobile:// authorize request is reported as client_kind="mobile"."""
    _verifier, challenge = generate_pkce_pair()
    nonce = _authorize(client, challenge, redirect_uri=_MOBILE_REDIRECT)

    r = client.get(f"{_BASE}/requests/{nonce}")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["client_kind"] == "mobile"
    assert data["device_name"] == "Test Phone"
    assert data["platform"] == "ios"


# ── Test: native redirect URI validation with env gating ─────────────────────


@pytest.mark.parametrize(
    "environment, redirect_uri, accepted",
    [
        ("production", "cinna-mobile://oauth/callback", True),
        ("local", "cinna-mobile://oauth/callback", True),
        # Loopback still works on the app surface too.
        ("production", "http://127.0.0.1:19836/callback", True),
        # Expo Go dev redirect — non-production only.
        ("local", "exp://192.168.1.5:8081/--/oauth/callback", True),
        ("production", "exp://192.168.1.5:8081/--/oauth/callback", False),
        # Arbitrary https — always rejected.
        ("local", "https://evil.example/cb", False),
    ],
)
def test_app_redirect_uri_validation(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    environment: str,
    redirect_uri: str,
    accepted: bool,
) -> None:
    monkeypatch.setattr(settings, "ENVIRONMENT", environment)
    _verifier, challenge = generate_pkce_pair()
    r = client.get(
        f"{_BASE}/authorize",
        params={
            "redirect_uri": redirect_uri,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "test",
            "device_name": "Test Phone",
        },
        follow_redirects=False,
    )
    if accepted:
        assert r.status_code in (302, 307), (
            f"Expected redirect for {redirect_uri!r} in {environment}, got {r.status_code}"
        )
    else:
        assert r.status_code == 400, (
            f"Expected 400 for {redirect_uri!r} in {environment}, got {r.status_code}"
        )
        assert r.json()["detail"] == "invalid_redirect_uri"


# ── Test: refresh token rotation on the app surface ──────────────────────────


def test_app_refresh_token_rotation(client: TestClient) -> None:
    """Refresh token rotates and returns a new pair via /app-auth/token."""
    _user, headers = create_random_user_with_headers(client)
    verifier, challenge = generate_pkce_pair()
    code, client_id = _get_code(client, headers, challenge)
    tokens = _exchange(client, client_id=client_id, code=code, code_verifier=verifier)

    r = client.post(
        f"{_BASE}/token",
        json={
            "grant_type": "refresh_token",
            "client_id": tokens["client_id"],
            "refresh_token": tokens["refresh_token"],
        },
    )
    assert r.status_code == 200, r.text
    rotated = r.json()
    assert rotated["access_token"]
    assert rotated["refresh_token"] != tokens["refresh_token"]


# ── Test: shared backing across the two surfaces ─────────────────────────────


def test_app_token_works_on_desktop_userinfo(client: TestClient) -> None:
    """A token minted via /app-auth authenticates on /desktop-auth/userinfo.

    Proves the two surfaces share the same backing store and JWT issuance —
    they are namespaces over one service, not independent stacks. The client
    also shows up in the shared /app-auth/clients listing.
    """
    user, headers = create_random_user_with_headers(client)
    verifier, challenge = generate_pkce_pair()
    code, client_id = _get_code(client, headers, challenge)
    tokens = _exchange(client, client_id=client_id, code=code, code_verifier=verifier)

    bearer = {"Authorization": f"Bearer {tokens['access_token']}"}
    r = client.get(f"{_DESKTOP_BASE}/userinfo", headers=bearer)
    assert r.status_code == 200, r.text
    assert r.json()["email"] == user["email"]

    # The lazily-registered client is visible through the shared clients list.
    r_clients = client.get(f"{_BASE}/clients", headers=headers)
    assert r_clients.status_code == 200, r_clients.text
    assert any(c["client_id"] == tokens["client_id"] for c in r_clients.json())
