"""Utility helpers for Desktop Auth API tests.

The authorization flow now uses a consent-page pattern:
  1. GET /authorize (public) → 307 redirect to /desktop-auth/consent?request={nonce}
  2. GET /requests/{nonce} (public) → display metadata for the consent page
  3. POST /consent (authenticated) → {"redirect_to": "http://localhost:.../callback?code=..."}
  4. POST /token (public) → access_token + refresh_token + client_id

Client registration is now done lazily via the consent flow (no separate POST /clients).
"""
import base64
import hashlib
import secrets
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient

from app.core.config import settings

_BASE = f"{settings.API_V1_STR}/desktop-auth"


# ── PKCE helpers ───────────────────────────────────────────────────────────


def generate_pkce_pair() -> tuple[str, str]:
    """Generate a (code_verifier, code_challenge) pair for S256 PKCE."""
    verifier = secrets.token_urlsafe(32)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


# ── Client management helpers ──────────────────────────────────────────────


def list_desktop_clients(
    client: TestClient,
    headers: dict[str, str],
) -> list[dict]:
    """GET /api/v1/desktop-auth/clients — list registered desktop clients."""
    r = client.get(f"{_BASE}/clients", headers=headers)
    assert r.status_code == 200, f"List desktop clients failed: {r.text}"
    return r.json()


def revoke_desktop_client(
    client: TestClient,
    headers: dict[str, str],
    client_id: str,
) -> None:
    """DELETE /api/v1/desktop-auth/clients/{client_id} — revoke a desktop client."""
    r = client.delete(f"{_BASE}/clients/{client_id}", headers=headers)
    assert r.status_code == 204, f"Revoke desktop client failed: {r.text}"


# ── Authorization flow helpers ─────────────────────────────────────────────


def initiate_authorize(
    client: TestClient,
    code_challenge: str,
    state: str = "test-state",
    redirect_uri: str = "http://localhost:19836/callback",
    existing_client_id: str | None = None,
    device_name: str | None = "Test Device",
    platform: str | None = "linux",
    app_version: str | None = "1.0.0",
) -> str:
    """GET /authorize (public) → extract and return the nonce from the redirect Location.

    Returns the raw nonce string from the consent page redirect URL.
    """
    params: dict = {
        "redirect_uri": redirect_uri,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    if existing_client_id is not None:
        params["client_id"] = existing_client_id
    if device_name is not None:
        params["device_name"] = device_name
    if platform is not None:
        params["platform"] = platform
    if app_version is not None:
        params["app_version"] = app_version

    r = client.get(
        f"{_BASE}/authorize",
        params=params,
        follow_redirects=False,
    )
    assert r.status_code in (302, 307), (
        f"Authorize failed: {r.status_code} {r.text}"
    )
    location = r.headers["location"]
    assert "/desktop-auth/consent" in location, (
        f"Expected consent redirect, got: {location}"
    )

    parsed = urlparse(location)
    location_params = parse_qs(parsed.query)
    assert "request" in location_params, f"No request nonce in redirect: {location}"
    return location_params["request"][0]


def submit_consent(
    client: TestClient,
    headers: dict[str, str],
    nonce: str,
    action: str = "approve",
) -> dict:
    """POST /consent (authenticated) — approve or deny a consent request.

    Returns the full response dict: {"redirect_to": "..."}.
    """
    r = client.post(
        f"{_BASE}/consent",
        headers=headers,
        json={"request_nonce": nonce, "action": action},
    )
    assert r.status_code == 200, f"Consent ({action}) failed: {r.text}"
    return r.json()


def get_authorization_code(
    client: TestClient,
    headers: dict[str, str],
    code_challenge: str,
    state: str = "test-state",
    redirect_uri: str = "http://localhost:19836/callback",
    existing_client_id: str | None = None,
    device_name: str | None = "Test Device",
    platform: str | None = "linux",
    app_version: str | None = "1.0.0",
) -> str:
    """Obtain an authorization code via the full consent flow.

    Steps:
      1. GET /authorize (public) → 307 to consent page, extract nonce
      2. POST /consent (authenticated, action=approve) → redirect_to with code
      3. Extract and return the authorization code from redirect_to
    """
    nonce = initiate_authorize(
        client,
        code_challenge=code_challenge,
        state=state,
        redirect_uri=redirect_uri,
        existing_client_id=existing_client_id,
        device_name=device_name,
        platform=platform,
        app_version=app_version,
    )

    result = submit_consent(client, headers, nonce, action="approve")
    redirect_to = result["redirect_to"]

    parsed = urlparse(redirect_to)
    code_params = parse_qs(parsed.query)
    assert "code" in code_params, f"No code in redirect_to: {redirect_to}"
    return code_params["code"][0]


def exchange_code_for_tokens(
    client: TestClient,
    client_id: str,
    code: str,
    code_verifier: str,
    redirect_uri: str = "http://localhost:19836/callback",
) -> dict:
    """POST /api/v1/desktop-auth/token (grant_type=authorization_code)."""
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


def refresh_access_token(
    client: TestClient,
    client_id: str,
    refresh_token: str,
) -> dict:
    """POST /api/v1/desktop-auth/token (grant_type=refresh_token)."""
    r = client.post(
        f"{_BASE}/token",
        json={
            "grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": refresh_token,
        },
    )
    assert r.status_code == 200, f"Token refresh failed: {r.text}"
    return r.json()
