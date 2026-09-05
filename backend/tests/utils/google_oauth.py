"""Driving ``POST /auth/google/callback`` without talking to Google.

``AuthService.exchange_google_code`` and
``AuthService.verify_and_decode_google_token`` are the two outbound steps of
the callback — a token exchange over HTTP and a JWT verification against
Google's public keys. Both are class methods, which makes them the natural
seam: replacing them leaves every user-lookup / auto-link / registration-policy
branch of the callback running for real.

``settings.GOOGLE_CLIENT_ID`` / ``GOOGLE_CLIENT_SECRET`` are patched too so
``is_google_oauth_enabled()`` is true regardless of how the container that runs
the suite is configured.

The same seam is documented at length in
``tests/api/auth/test_google_oauth_auto_confirm.py``, which predates this
helper.
"""
from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient

from app.core.config import settings

CALLBACK_URL = f"{settings.API_V1_STR}/auth/google/callback"


def random_google_id() -> str:
    return f"google-uid-{uuid.uuid4().hex}"


@contextmanager
def google_oauth_patched(
    *, email: str, google_id: str, name: str = "Test User"
) -> Iterator[None]:
    """Patch the two Google network calls and the OAuth settings."""
    claims = {
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
            new=AsyncMock(return_value=claims),
        ),
    ):
        yield


def google_callback(
    client: TestClient, *, email: str, google_id: str, name: str = "Test User"
) -> httpx.Response:
    """Drive the callback and return the raw response.

    Raw, not parsed: the callers that matter here assert on a 403 reason code
    as often as on a token.
    """
    with google_oauth_patched(email=email, google_id=google_id, name=name):
        return client.post(
            CALLBACK_URL, json={"code": "fake_code", "state": "fake_state"}
        )


def login_with_google(
    client: TestClient, *, email: str, google_id: str, name: str = "Test User"
) -> dict[str, str]:
    """Complete a Google sign-in and return auth headers.

    Asserts the callback succeeded with a token (not an MFA challenge).
    """
    response = google_callback(
        client, email=email, google_id=google_id, name=name
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body.get("kind") == "token", body
    return {"Authorization": f"Bearer {body['access_token']}"}
