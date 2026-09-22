"""Helpers for the Desktop credential delivery API (``/external/credentials``).

Setup for these tests is API-only: credentials are created and rotated through
``/credentials``, shared through ``/credentials/{id}/shares``, and the Desktop
session comes from the real PKCE consent flow (``obtain_desktop_tokens``).
"""
from typing import Any

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.desktop_auth import obtain_desktop_tokens
from tests.utils.utils import random_lower_string

DESKTOP_CREDENTIALS_URL = f"{settings.API_V1_STR}/external/credentials"
MATERIALIZE_URL = f"{DESKTOP_CREDENTIALS_URL}/materialize"


def desktop_session(
    client: TestClient, headers: dict[str, str], device_name: str | None = None
) -> tuple[dict[str, str], str]:
    """Authorize an interactive Desktop client via PKCE consent.

    Returns ``(bearer_headers, client_id)``.
    """
    tokens = obtain_desktop_tokens(
        client, headers, device_name=device_name or f"desk-{random_lower_string()[:8]}"
    )
    return {"Authorization": f"Bearer {tokens['access_token']}"}, tokens["client_id"]


def list_desktop_credentials(
    client: TestClient, headers: dict[str, str], etag: str | None = None
):
    """GET /external/credentials. Returns the raw response (callers check 200/304)."""
    extra = {"If-None-Match": etag} if etag else {}
    return client.get(DESKTOP_CREDENTIALS_URL, headers={**headers, **extra})


def materialize_desktop_credentials(
    client: TestClient,
    headers: dict[str, str],
    credential_ids: list[str],
    include_current_user: bool = False,
) -> dict:
    """POST /external/credentials/materialize, asserting 200 + no-store."""
    r = client.post(
        MATERIALIZE_URL,
        headers=headers,
        json={
            "credential_ids": credential_ids,
            "include_current_user": include_current_user,
        },
    )
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-store"
    return r.json()


def create_credential_via_api(
    client: TestClient,
    headers: dict[str, str],
    credential_type: str,
    credential_data: dict | None,
    **fields: Any,
) -> dict:
    """POST /credentials/ with arbitrary extra fields (allow_sharing,
    allow_local_use, service_uri, mcp_auth_mode, ...)."""
    r = client.post(
        f"{settings.API_V1_STR}/credentials/",
        headers=headers,
        json={
            "name": fields.pop("name", f"desk-cred-{random_lower_string()[:10]}"),
            "type": credential_type,
            "credential_data": credential_data,
            **fields,
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


def revoke_credential_share(
    client: TestClient, owner_headers: dict[str, str], credential_id: str, share_id: str
) -> None:
    """DELETE /credentials/{id}/shares/{share_id}."""
    r = client.delete(
        f"{settings.API_V1_STR}/credentials/{credential_id}/shares/{share_id}",
        headers=owner_headers,
    )
    assert r.status_code == 200, r.text

