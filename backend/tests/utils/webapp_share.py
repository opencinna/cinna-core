"""Helpers for webapp share integration tests."""
from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.agent import create_agent_via_api, update_agent
from tests.utils.background_tasks import drain_tasks

API = settings.API_V1_STR


def create_webapp_share(
    client: TestClient,
    token_headers: dict[str, str],
    agent_id: str,
    label: str | None = None,
    expires_in_hours: int | None = None,
    allow_data_api: bool = True,
    require_security_code: bool = False,
) -> dict:
    """Create a webapp share via POST /agents/{id}/webapp-shares/.

    Returns the full response including ``token``, ``share_url``, and optionally ``security_code``.
    """
    payload: dict = {"allow_data_api": allow_data_api, "require_security_code": require_security_code}
    if label is not None:
        payload["label"] = label
    if expires_in_hours is not None:
        payload["expires_in_hours"] = expires_in_hours
    r = client.post(
        f"{API}/agents/{agent_id}/webapp-shares/",
        headers=token_headers,
        json=payload,
    )
    assert r.status_code == 200, f"Create webapp share failed: {r.text}"
    return r.json()


def list_webapp_shares(
    client: TestClient,
    token_headers: dict[str, str],
    agent_id: str,
) -> list[dict]:
    """List webapp shares via GET /agents/{id}/webapp-shares/. Returns the ``data`` array."""
    r = client.get(
        f"{API}/agents/{agent_id}/webapp-shares/",
        headers=token_headers,
    )
    assert r.status_code == 200, f"List webapp shares failed: {r.text}"
    body = r.json()
    assert "data" in body
    assert "count" in body
    assert body["count"] == len(body["data"])
    return body["data"]


def get_webapp_share_info(client: TestClient, token: str) -> dict:
    """Get public info about a webapp share via GET /webapp-share/{token}/info."""
    r = client.get(f"{API}/webapp-share/{token}/info")
    assert r.status_code == 200, f"Get webapp share info failed: {r.text}"
    return r.json()


def authenticate_webapp_share(
    client: TestClient,
    token: str,
    security_code: str | None = None,
) -> dict:
    """Authenticate via POST /webapp-share/{token}/auth. Returns JWT info."""
    body: dict = {}
    if security_code is not None:
        body["security_code"] = security_code
    r = client.post(
        f"{API}/webapp-share/{token}/auth",
        json=body if body else None,
    )
    assert r.status_code == 200, f"Webapp share auth failed: {r.text}"
    return r.json()


def update_webapp_share(
    client: TestClient,
    token_headers: dict[str, str],
    agent_id: str,
    share_id: str,
    **kwargs,
) -> dict:
    """Update a webapp share via PATCH /agents/{id}/webapp-shares/{share_id}."""
    r = client.patch(
        f"{API}/agents/{agent_id}/webapp-shares/{share_id}",
        headers=token_headers,
        json=kwargs,
    )
    assert r.status_code == 200, f"Update webapp share failed: {r.text}"
    return r.json()


def setup_webapp_agent(
    client: TestClient,
    token_headers: dict[str, str],
    name: str = "Webapp Agent",
    share_label: str | None = None,
    expires_in_hours: int | None = None,
    allow_data_api: bool = True,
    require_security_code: bool = False,
) -> tuple[dict, dict]:
    """Create an agent with webapp_enabled=True and a webapp share.

    Returns ``(agent_dict, share_data_dict)``.
    ``share_data_dict`` contains ``token``, ``share_url``, ``id``, and optionally ``security_code``.
    """
    agent = create_agent_via_api(client, token_headers, name=name)
    drain_tasks()
    agent = update_agent(client, token_headers, agent["id"], webapp_enabled=True)
    share = create_webapp_share(
        client, token_headers, agent["id"],
        label=share_label,
        expires_in_hours=expires_in_hours,
        allow_data_api=allow_data_api,
        require_security_code=require_security_code,
    )
    return agent, share
