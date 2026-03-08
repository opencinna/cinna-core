"""Helpers for webapp interface config integration tests."""
from fastapi.testclient import TestClient

from app.core.config import settings

API = settings.API_V1_STR


def get_webapp_interface_config(
    client: TestClient,
    token_headers: dict[str, str],
    agent_id: str,
) -> dict:
    """Get webapp interface config via GET /agents/{id}/webapp-interface-config/.

    Auto-creates a default config record if none exists.
    Returns the full response dict with ``show_header``, ``show_chat``, ``id``, etc.
    """
    r = client.get(
        f"{API}/agents/{agent_id}/webapp-interface-config/",
        headers=token_headers,
    )
    assert r.status_code == 200, f"Get webapp interface config failed: {r.text}"
    return r.json()


def update_webapp_interface_config(
    client: TestClient,
    token_headers: dict[str, str],
    agent_id: str,
    **kwargs,
) -> dict:
    """Update webapp interface config via PUT /agents/{id}/webapp-interface-config/.

    Pass ``show_header`` and/or ``chat_mode`` as keyword arguments.
    Returns the updated config dict.
    """
    r = client.put(
        f"{API}/agents/{agent_id}/webapp-interface-config/",
        headers=token_headers,
        json=kwargs,
    )
    assert r.status_code == 200, f"Update webapp interface config failed: {r.text}"
    return r.json()
