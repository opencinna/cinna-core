"""Driving the Google credential OAuth flow (``/credentials/.../oauth/*``) without Google.

The two outbound steps of the callback — the authorization-code exchange and
the ID-token verification — are patched at their single binding sites, so the
state handling, ownership checks and blob storage run for real. Refreshes are
mocked the same way at ``_exchange_refresh_token``.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit

import httpx
from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.network_guard import no_outbound_http

OAUTH_CALLBACK_URL = f"{settings.API_V1_STR}/credentials/oauth/callback"
_SERVICE = "app.services.credentials.oauth_credentials_service"
EXCHANGE_CODE = f"{_SERVICE}.OAuthCredentialsService._exchange_authorization_code"
EXCHANGE_REFRESH = f"{_SERVICE}.OAuthCredentialsService._exchange_refresh_token"
VERIFY_ID_TOKEN = f"{_SERVICE}.security.verify_google_token"


@contextmanager
def google_oauth_configured() -> Iterator[None]:
    """``settings.google_oauth_enabled`` true regardless of the container's env."""
    with (
        patch.object(settings, "GOOGLE_CLIENT_ID", "test-client-id"),
        patch.object(settings, "GOOGLE_CLIENT_SECRET", "test-client-secret"),
    ):
        yield


@contextmanager
def google_oauth_unconfigured() -> Iterator[None]:
    with (
        patch.object(settings, "GOOGLE_CLIENT_ID", None),
        patch.object(settings, "GOOGLE_CLIENT_SECRET", None),
    ):
        yield


def query_of(url: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(url).query)


def state_from_url(url: str) -> str:
    return query_of(url)["state"][0]


def complete_oauth_callback(
    client: TestClient,
    state: str,
    *,
    email: str = "granted@example.com",
    access_token: str = "ya29.callback-access",
    refresh_token: str = "1//callback-refresh",
    expires_in: int = 3600,
    code: str = "auth-code",
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    """POST /credentials/oauth/callback with Google mocked; returns the raw response.

    ``headers`` carries the browser's Cinna session, if any (the SPA sends its
    JWT when signed in); omit it for a sessionless browser.
    """
    token_data = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_in": expires_in,
        "token_type": "Bearer",
        "scope": "granted-scope",
        "id_token": "fake-id-token",
    }
    with (
        google_oauth_configured(),
        no_outbound_http(),
        patch(EXCHANGE_CODE, new=AsyncMock(return_value=token_data)),
        patch(VERIFY_ID_TOKEN, new=AsyncMock(return_value={"email": email, "name": "Granted"})),
    ):
        return client.post(
            OAUTH_CALLBACK_URL, json={"code": code, "state": state}, headers=headers
        )
