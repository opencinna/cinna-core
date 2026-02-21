"""Helpers to query sessions via API for tests."""
from fastapi.testclient import TestClient

from app.core.config import settings


def list_sessions(
    client: TestClient,
    token_headers: dict[str, str],
    limit: int = 100,
) -> list[dict]:
    """List sessions via GET /api/v1/sessions/. Returns the data array."""
    r = client.get(
        f"{settings.API_V1_STR}/sessions/?limit={limit}",
        headers=token_headers,
    )
    assert r.status_code == 200, f"List sessions failed: {r.text}"
    return r.json()["data"]


def create_session_via_api(
    client: TestClient,
    token_headers: dict[str, str],
    agent_id: str,
    mode: str = "conversation",
) -> dict:
    """Create session via POST /api/v1/sessions/."""
    r = client.post(
        f"{settings.API_V1_STR}/sessions/",
        headers=token_headers,
        json={"agent_id": agent_id, "mode": mode},
    )
    assert r.status_code == 200, f"Create session failed: {r.text}"
    return r.json()


def get_agent_session(
    client: TestClient,
    token_headers: dict[str, str],
    agent_id: str,
) -> dict:
    """Find the single session belonging to an agent. Asserts exactly one exists."""
    sessions = list_sessions(client, token_headers)
    agent_sessions = [s for s in sessions if s["agent_id"] == agent_id]
    assert len(agent_sessions) == 1, (
        f"Expected 1 session for agent {agent_id}, got {len(agent_sessions)}"
    )
    return agent_sessions[0]
