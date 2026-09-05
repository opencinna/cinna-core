"""Helpers for the server-config singleton and its access-policy projection.

The access policy is instance-wide state that many domains read (signup, login,
password paths, the Google callback, ``UserPublic.can_change_email``). Tests
drive it **through the admin API** rather than patching module attributes: the
policy is a database row, and a patched module attribute would prove the test's
own mock works rather than that the route reads the row (see the
zero-touch-onboarding invariant on patch-target drift).
"""
from fastapi.testclient import TestClient

from app.core.config import settings

SERVER_CONFIG_URL = f"{settings.API_V1_STR}/admin/server-config"
ACCESS_POLICY_URL = f"{settings.API_V1_STR}/server-config/access-policy"


def get_server_config(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> dict:
    """Read the full (superuser-only) server config row."""
    response = client.get(SERVER_CONFIG_URL, headers=superuser_token_headers)
    assert response.status_code == 200, response.text
    return response.json()


def set_access_policy(
    client: TestClient, superuser_token_headers: dict[str, str], **fields: object
) -> dict:
    """Apply access-policy fields through ``PUT /admin/server-config``.

    Only accepts updates the server accepts — a rejection is a test bug here,
    so the assertion carries the reason code. Tests that are *about* a
    rejection call the endpoint inline instead.
    """
    response = client.put(
        SERVER_CONFIG_URL, headers=superuser_token_headers, json=fields
    )
    assert response.status_code == 200, response.text
    return response.json()


def get_public_access_policy(client: TestClient) -> dict:
    """Read the anonymous access-policy projection (no auth header)."""
    response = client.get(ACCESS_POLICY_URL)
    assert response.status_code == 200, response.text
    return response.json()
