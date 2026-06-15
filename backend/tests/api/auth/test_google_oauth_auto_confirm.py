"""Backend tests: Google OAuth users are automatically email-confirmed.

Coverage (focused — email-confirmation auto-confirm via Google only):

  1. New user created via the Google OAuth callback has email_confirmed=True.
     Contrast: a regular password-signup user has email_confirmed=False.
  2. Existing unconfirmed (password-signup) user who authenticates via Google
     has email_confirmed flipped to True by the login path.

Mock seam
---------
``POST /auth/google/callback`` internally calls:
  (a) ``AuthService.exchange_google_code(code)``  — real HTTP POST to Google
  (b) ``AuthService.verify_and_decode_google_token(id_token)`` — verifies the
      JWT with Google's public keys.

Both are class-methods on ``AuthService`` and are the natural patch boundary:
we replace them with simple coroutine stubs so the callback reaches all of
the user-lookup / creation / email-confirmation logic without touching Google's
servers.  ``settings.GOOGLE_CLIENT_ID`` and ``settings.GOOGLE_CLIENT_SECRET``
are also patched so ``AuthService.is_google_oauth_enabled()`` returns True.

This file creates no agents or environments — opt out of the heavy stubs
loaded by ``tests/api/users/conftest.py`` (which the auth/ directory does not
use) to keep the file fast.
"""
from __future__ import annotations

import uuid
from contextlib import contextmanager
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.utils import random_email, random_lower_string

_BASE = settings.API_V1_STR


# ── Helpers ───────────────────────────────────────────────────────────────────


def _signup(client: TestClient, email: str | None = None, password: str | None = None) -> dict:
    """Create a user via the public signup API; returns body + stashed password."""
    email = email or random_email()
    password = password or random_lower_string()
    r = client.post(f"{_BASE}/users/signup", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    body = r.json()
    body["_password"] = password
    return body


def _login_password(client: TestClient, email: str, password: str) -> dict[str, str]:
    """Log in with email+password and return auth headers (no 2FA)."""
    r = client.post(
        f"{_BASE}/login/access-token",
        data={"username": email, "password": password},
    )
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _me(client: TestClient, headers: dict[str, str]) -> dict:
    r = client.get(f"{_BASE}/users/me", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


@contextmanager
def _google_oauth_patched(email: str, google_id: str, name: str = "Test User"):
    """Context manager that patches the two Google-network calls on AuthService.

    Patches:
    - ``AuthService.exchange_google_code`` → returns ``{"id_token": "fake"}``
    - ``AuthService.verify_and_decode_google_token`` → returns fake claims dict
    - ``settings.GOOGLE_CLIENT_ID`` / ``settings.GOOGLE_CLIENT_SECRET`` → non-null

    All real user-creation and email-confirmation logic in ``AuthService`` still
    runs; only the two outbound HTTP/JWT steps are bypassed.
    """
    fake_claims = {
        "sub": google_id,
        "email": email,
        "name": name,
        "email_verified": True,
    }
    with (
        patch.object(settings, "GOOGLE_CLIENT_ID", "test-client-id"),
        patch.object(settings, "GOOGLE_CLIENT_SECRET", "test-client-secret"),
        patch(
            "app.services.users.auth_service.AuthService.exchange_google_code",
            new=AsyncMock(return_value={"id_token": "fake_id_token"}),
        ),
        patch(
            "app.services.users.auth_service.AuthService.verify_and_decode_google_token",
            new=AsyncMock(return_value=fake_claims),
        ),
    ):
        yield


def _google_callback(client: TestClient, email: str, google_id: str) -> dict:
    """Drive POST /auth/google/callback with mocked Google services.

    Returns the parsed JSON body (a LoginToken with access_token).
    """
    with _google_oauth_patched(email=email, google_id=google_id):
        r = client.post(
            f"{_BASE}/auth/google/callback",
            json={"code": "fake_code", "state": "fake_state"},
        )
    assert r.status_code == 200, f"Google callback failed: {r.text}"
    body = r.json()
    assert body.get("kind") == "token", f"Expected token response, got: {body}"
    return body


def _headers_from_google_token(body: dict) -> dict[str, str]:
    return {"Authorization": f"Bearer {body['access_token']}"}


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_google_signup_user_is_auto_confirmed(client: TestClient) -> None:
    """
    A brand-new user created through the Google OAuth callback has email_confirmed=True.
    Contrast: a regular password-signup user starts with email_confirmed=False.

      1. Signup a user with email+password → email_confirmed=False.
      2. Drive Google callback for a *different* email (new Google user) → 200 with token.
      3. Call GET /users/me for the Google user → email_confirmed=True.
    """
    # ── Phase 1: Regular signup user is unconfirmed ────────────────────────
    signup_user = _signup(client)
    signup_headers = _login_password(client, signup_user["email"], signup_user["_password"])
    me_signup = _me(client, signup_headers)
    assert me_signup["email_confirmed"] is False, (
        "Password-signup user should start unconfirmed"
    )

    # ── Phase 2: Google callback creates a brand-new user ─────────────────
    google_email = random_email()
    google_id = f"google-uid-{uuid.uuid4().hex}"

    callback_body = _google_callback(client, email=google_email, google_id=google_id)
    google_headers = _headers_from_google_token(callback_body)

    # ── Phase 3: Google user is confirmed from the moment of creation ─────
    me_google = _me(client, google_headers)
    assert me_google["email"] == google_email
    assert me_google["email_confirmed"] is True, (
        "User created via Google OAuth must have email_confirmed=True immediately"
    )
    assert me_google["email_confirmed_at"] is not None, (
        "email_confirmed_at must be set when auto-confirmed"
    )


def test_existing_unconfirmed_user_confirmed_on_google_login(client: TestClient) -> None:
    """
    An existing unconfirmed (password-signup) user who authenticates via Google
    has email_confirmed flipped to True by the Google login path.

      1. Signup user with email+password → unconfirmed.
      2. Drive Google callback for the *same* email (auto-link + confirm path).
      3. GET /users/me → email_confirmed=True.
      4. Original password login still works, and /users/me still shows confirmed.
    """
    # ── Phase 1: Create unconfirmed user ──────────────────────────────────
    email = random_email()
    password = random_lower_string()
    user = _signup(client, email=email, password=password)
    assert user["email_confirmed"] is False, "Signup user should start unconfirmed"

    # Verify via API: confirmed=False before Google login
    pre_headers = _login_password(client, email, password)
    me_pre = _me(client, pre_headers)
    assert me_pre["email_confirmed"] is False

    # ── Phase 2: Same email authenticates via Google ───────────────────────
    # AuthService.authenticate_with_google finds the existing user by email
    # (auto-link), then calls EmailConfirmationService.mark_confirmed because
    # user.email_confirmed is False at that point.
    google_id = f"google-uid-{uuid.uuid4().hex}"
    callback_body = _google_callback(client, email=email, google_id=google_id)
    google_headers = _headers_from_google_token(callback_body)

    # ── Phase 3: User is now confirmed ────────────────────────────────────
    me_post = _me(client, google_headers)
    assert me_post["email"] == email
    assert me_post["email_confirmed"] is True, (
        "Existing unconfirmed user must become email_confirmed=True after Google login"
    )
    assert me_post["email_confirmed_at"] is not None

    # ── Phase 4: Confirmation persists across subsequent password logins ───
    # The auto-confirm is durable — not just a token-level claim.
    post_password_headers = _login_password(client, email, password)
    me_password = _me(client, post_password_headers)
    assert me_password["email_confirmed"] is True, (
        "email_confirmed=True must persist for subsequent password logins"
    )
