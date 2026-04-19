"""Helpers to query sessions via API for tests."""
import asyncio
import uuid

from fastapi.testclient import TestClient

from app.core.config import settings


def list_sessions(
    client: TestClient,
    token_headers: dict[str, str],
    limit: int = 100,
    guest_share_id: str | None = None,
) -> list[dict]:
    """List sessions via GET /api/v1/sessions/. Returns the data array."""
    url = f"{settings.API_V1_STR}/sessions/?limit={limit}"
    if guest_share_id:
        url += f"&guest_share_id={guest_share_id}"
    r = client.get(url, headers=token_headers)
    assert r.status_code == 200, f"List sessions failed: {r.text}"
    return r.json()["data"]


def create_session_via_api(
    client: TestClient,
    token_headers: dict[str, str],
    agent_id: str,
    mode: str = "conversation",
    guest_share_id: str | None = None,
) -> dict:
    """Create session via POST /api/v1/sessions/."""
    payload: dict = {"agent_id": agent_id, "mode": mode}
    if guest_share_id is not None:
        payload["guest_share_id"] = guest_share_id
    r = client.post(
        f"{settings.API_V1_STR}/sessions/",
        headers=token_headers,
        json=payload,
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


def create_session_with_block(
    client: TestClient,
    token_headers: dict[str, str],
    agent_id: str,
    dashboard_block_id: str,
    mode: str = "conversation",
) -> dict:
    """Create a session tagged with a dashboard block via POST /api/v1/sessions/."""
    payload: dict = {
        "agent_id": agent_id,
        "mode": mode,
        "dashboard_block_id": dashboard_block_id,
    }
    r = client.post(
        f"{settings.API_V1_STR}/sessions/",
        headers=token_headers,
        json=payload,
    )
    assert r.status_code == 200, f"Create session with block failed: {r.text}"
    return r.json()


# ---------------------------------------------------------------------------
# Active-streaming-manager helpers
#
# ActiveStreamingManager is internal state with no public API surface —
# there is no HTTP endpoint to register or unregister an active stream.
# These helpers isolate the app.services import to this utility module
# so individual test files remain free of app.services imports.
# ---------------------------------------------------------------------------

def register_active_stream(session_id: uuid.UUID, external_session_id: str) -> None:
    """Register a session as actively streaming in the in-process manager.

    Used by A2A cancel tests to simulate a still-running stream after the
    synchronous test call has completed.
    """
    from app.services.sessions.active_streaming_manager import active_streaming_manager

    asyncio.run(
        active_streaming_manager.register_stream(
            session_id=session_id,
            external_session_id=external_session_id,
        )
    )


def unregister_active_stream(session_id: uuid.UUID) -> None:
    """Unregister a session from the active-streaming manager.

    Call this in a ``finally`` block after ``register_active_stream`` to
    prevent leaking state across tests.
    """
    from app.services.sessions.active_streaming_manager import active_streaming_manager

    asyncio.run(active_streaming_manager.unregister_stream(session_id))
