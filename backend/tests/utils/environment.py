"""Helper functions for agent environment API calls in tests."""
from fastapi.testclient import TestClient

from app.core.config import settings


def create_environment(
    client: TestClient,
    token_headers: dict[str, str],
    agent_id: str,
    env_name: str = "python-env-advanced",
    instance_name: str | None = None,
) -> dict:
    """Create environment via POST /api/v1/agents/{agent_id}/environments."""
    data: dict = {"env_name": env_name}
    if instance_name:
        data["instance_name"] = instance_name
    r = client.post(
        f"{settings.API_V1_STR}/agents/{agent_id}/environments",
        headers=token_headers,
        json=data,
    )
    assert r.status_code == 200, f"Create environment failed: {r.text}"
    return r.json()


def list_environments(
    client: TestClient,
    token_headers: dict[str, str],
    agent_id: str,
) -> dict:
    """List environments via GET /api/v1/agents/{agent_id}/environments.

    Returns the full AgentEnvironmentsPublic response with ``data`` and ``count``.
    """
    r = client.get(
        f"{settings.API_V1_STR}/agents/{agent_id}/environments",
        headers=token_headers,
    )
    assert r.status_code == 200, f"List environments failed: {r.text}"
    return r.json()


def get_environment(
    client: TestClient,
    token_headers: dict[str, str],
    env_id: str,
) -> dict:
    """Get environment via GET /api/v1/environments/{env_id}."""
    r = client.get(
        f"{settings.API_V1_STR}/environments/{env_id}",
        headers=token_headers,
    )
    assert r.status_code == 200, f"Get environment failed: {r.text}"
    return r.json()


def update_environment(
    client: TestClient,
    token_headers: dict[str, str],
    env_id: str,
    **fields,
) -> dict:
    """Update environment via PATCH /api/v1/environments/{env_id}."""
    r = client.patch(
        f"{settings.API_V1_STR}/environments/{env_id}",
        headers=token_headers,
        json=fields,
    )
    assert r.status_code == 200, f"Update environment failed: {r.text}"
    return r.json()


def delete_environment(
    client: TestClient,
    token_headers: dict[str, str],
    env_id: str,
) -> dict:
    """Delete environment via DELETE /api/v1/environments/{env_id}."""
    r = client.delete(
        f"{settings.API_V1_STR}/environments/{env_id}",
        headers=token_headers,
    )
    assert r.status_code == 200, f"Delete environment failed: {r.text}"
    return r.json()


def activate_environment(
    client: TestClient,
    token_headers: dict[str, str],
    agent_id: str,
    env_id: str,
) -> dict:
    """Activate environment via POST /api/v1/agents/{agent_id}/environments/{env_id}/activate."""
    r = client.post(
        f"{settings.API_V1_STR}/agents/{agent_id}/environments/{env_id}/activate",
        headers=token_headers,
    )
    assert r.status_code == 200, f"Activate environment failed: {r.text}"
    return r.json()
