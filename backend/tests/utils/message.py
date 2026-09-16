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


def get_raw_message_metadata(db, message_id: str) -> dict:
    """Read a message row's ``message_metadata`` straight off the test DB.

    Documented seam, same posture as ``force_session_interaction_claim`` in
    ``tests/utils/session.py``: every read surface (``GET .../messages``,
    A2A ``tasks/get``) merges the in-memory live-stream buffer into what it
    returns (``MessageService.enrich_messages_with_streaming`` /
    ``convert_session_messages_to_a2a``'s ``live_stream`` merge), so neither
    can be used to observe the *persisted* row on its own — only a direct
    read can prove a read path didn't also write to it.
    """
    import uuid as _uuid

    from app.models.sessions.session import SessionMessage

    row = db.get(SessionMessage, _uuid.UUID(message_id))
    assert row is not None, f"Message {message_id} not found"
    return dict(row.message_metadata or {})
