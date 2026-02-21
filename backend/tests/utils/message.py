"""Helpers to query session messages via API for tests."""
from fastapi.testclient import TestClient

from app.core.config import settings


def list_messages(
    client: TestClient,
    token_headers: dict[str, str],
    session_id: str,
) -> list[dict]:
    """List messages via GET /api/v1/sessions/{id}/messages. Returns the data array."""
    r = client.get(
        f"{settings.API_V1_STR}/sessions/{session_id}/messages",
        headers=token_headers,
    )
    assert r.status_code == 200, f"List messages failed: {r.text}"
    return r.json()["data"]


def send_message(
    client: TestClient,
    token_headers: dict[str, str],
    session_id: str,
    content: str,
) -> dict:
    """Send message via POST /api/v1/sessions/{id}/messages/stream."""
    r = client.post(
        f"{settings.API_V1_STR}/sessions/{session_id}/messages/stream",
        headers=token_headers,
        json={"content": content},
    )
    assert r.status_code == 200, f"Send message failed: {r.text}"
    return r.json()


def get_messages_by_role(
    client: TestClient,
    token_headers: dict[str, str],
    session_id: str,
    role: str,
) -> list[dict]:
    """List messages filtered by role (e.g. 'user', 'agent')."""
    messages = list_messages(client, token_headers, session_id)
    return [m for m in messages if m["role"] == role]
