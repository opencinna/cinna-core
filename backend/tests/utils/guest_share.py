"""Helpers for guest share integration tests."""
from datetime import datetime, timedelta, UTC
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.agent import create_agent_via_api
from tests.utils.background_tasks import drain_tasks

API = settings.API_V1_STR


def create_guest_share(
    client: TestClient,
    token_headers: dict[str, str],
    agent_id: str,
    label: str | None = None,
    expires_in_hours: int = 24,
    allow_env_panel: bool | None = None,
) -> dict:
    """Create a guest share via POST /agents/{id}/guest-shares/.

    Returns the full response including ``token``, ``share_url``, and ``security_code``.
    """
    payload: dict = {"expires_in_hours": expires_in_hours}
    if label is not None:
        payload["label"] = label
    if allow_env_panel is not None:
        payload["allow_env_panel"] = allow_env_panel
    r = client.post(
        f"{API}/agents/{agent_id}/guest-shares/",
        headers=token_headers,
        json=payload,
    )
    assert r.status_code == 200, f"Create guest share failed: {r.text}"
    return r.json()


def list_guest_shares(
    client: TestClient,
    token_headers: dict[str, str],
    agent_id: str,
) -> list[dict]:
    """List guest shares via GET /agents/{id}/guest-shares/. Returns the ``data`` array."""
    r = client.get(
        f"{API}/agents/{agent_id}/guest-shares/",
        headers=token_headers,
    )
    assert r.status_code == 200, f"List guest shares failed: {r.text}"
    body = r.json()
    assert "data" in body
    assert "count" in body
    assert body["count"] == len(body["data"])
    return body["data"]


def get_guest_share(
    client: TestClient,
    token_headers: dict[str, str],
    agent_id: str,
    share_id: str,
) -> dict:
    """Get a single guest share via GET /agents/{id}/guest-shares/{share_id}."""
    r = client.get(
        f"{API}/agents/{agent_id}/guest-shares/{share_id}",
        headers=token_headers,
    )
    assert r.status_code == 200, f"Get guest share failed: {r.text}"
    return r.json()


def delete_guest_share(
    client: TestClient,
    token_headers: dict[str, str],
    agent_id: str,
    share_id: str,
) -> dict:
    """Delete a guest share via DELETE /agents/{id}/guest-shares/{share_id}."""
    r = client.delete(
        f"{API}/agents/{agent_id}/guest-shares/{share_id}",
        headers=token_headers,
    )
    assert r.status_code == 200, f"Delete guest share failed: {r.text}"
    return r.json()


def update_guest_share(
    client: TestClient,
    token_headers: dict[str, str],
    agent_id: str,
    share_id: str,
    label: str | None = None,
    security_code: str | None = None,
    allow_env_panel: bool | None = None,
) -> dict:
    """Update a guest share via PUT /agents/{id}/guest-shares/{share_id}."""
    payload: dict = {}
    if label is not None:
        payload["label"] = label
    if security_code is not None:
        payload["security_code"] = security_code
    if allow_env_panel is not None:
        payload["allow_env_panel"] = allow_env_panel
    r = client.put(
        f"{API}/agents/{agent_id}/guest-shares/{share_id}",
        headers=token_headers,
        json=payload,
    )
    assert r.status_code == 200, f"Update guest share failed: {r.text}"
    return r.json()


def guest_auth(
    client: TestClient,
    token: str,
    security_code: str | None = None,
) -> dict:
    """Authenticate anonymously via POST /guest-share/{token}/auth. Returns JWT info."""
    body = {}
    if security_code is not None:
        body["security_code"] = security_code
    r = client.post(f"{API}/guest-share/{token}/auth", json=body if body else None)
    assert r.status_code == 200, f"Guest auth failed: {r.text}"
    return r.json()


def guest_headers(
    client: TestClient,
    token: str,
    security_code: str | None = None,
) -> dict[str, str]:
    """Get Authorization headers for an anonymous guest."""
    auth = guest_auth(client, token, security_code=security_code)
    return {"Authorization": f"Bearer {auth['access_token']}"}


def activate_guest_grant(
    client: TestClient,
    user_headers: dict[str, str],
    token: str,
    security_code: str | None = None,
) -> dict:
    """Activate a guest share grant for an authenticated user via POST /guest-share/{token}/activate."""
    body = {}
    if security_code is not None:
        body["security_code"] = security_code
    r = client.post(
        f"{API}/guest-share/{token}/activate",
        headers=user_headers,
        json=body if body else None,
    )
    assert r.status_code == 200, f"Grant activation failed: {r.text}"
    return r.json()


def guest_share_info(client: TestClient, token: str) -> dict:
    """Get public info about a guest share via GET /guest-share/{token}/info."""
    r = client.get(f"{API}/guest-share/{token}/info")
    assert r.status_code == 200, f"Guest share info failed: {r.text}"
    return r.json()


def setup_guest_share_agent(
    client: TestClient,
    token_headers: dict[str, str],
    name: str = "Guest Share Agent",
    share_label: str | None = None,
    expires_in_hours: int = 24,
) -> tuple[dict, dict]:
    """Create agent + guest share in one call.

    Returns ``(agent_dict, share_data_dict)``.
    ``share_data_dict`` contains ``token``, ``share_url``, ``security_code``, ``id``, and other fields.
    """
    agent = create_agent_via_api(client, token_headers, name=name)
    drain_tasks()
    share = create_guest_share(
        client, token_headers, agent["id"],
        label=share_label,
        expires_in_hours=expires_in_hours,
    )
    return agent, share


class mock_expired_guest_share:
    """Context manager that patches datetime in the guest share service to simulate expiration.

    Usage::

        with mock_expired_guest_share():
            r = client.post(guest_auth_url)
            assert r.status_code == 410
    """

    def __enter__(self):
        future_time = datetime.now(UTC) + timedelta(hours=2)
        self._patcher = patch("app.services.sharing.agent_guest_share_service.datetime")
        mock_dt = self._patcher.start()
        mock_dt.now.return_value = future_time
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        return mock_dt

    def __exit__(self, *exc):
        self._patcher.stop()
        return False
