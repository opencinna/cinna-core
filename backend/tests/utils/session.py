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


def get_session(
    client: TestClient,
    token_headers: dict[str, str],
    session_id: str,
) -> dict:
    """Get a single session via GET /api/v1/sessions/{id}.

    Returns the ``SessionPublicExtended`` JSON, which carries ``title`` and
    ``interaction_status`` among other fields.
    """
    r = client.get(
        f"{settings.API_V1_STR}/sessions/{session_id}",
        headers=token_headers,
    )
    assert r.status_code == 200, f"Get session failed: {r.text}"
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


# ---------------------------------------------------------------------------
# Status-repair (Pass B) seam
#
# ``interaction_status`` / ``streaming_started_at`` / ``updated_at`` on the
# claim path the status-repair sweep reads (see
# ``app/services/system/status_repair_sessions.py``).
# ---------------------------------------------------------------------------

def force_session_interaction_claim(
    db,
    session_id: str | uuid.UUID,
    *,
    interaction_status: str,
    streaming_started_at=None,
    updated_at=None,
    pending_messages_count: int | None = None,
) -> None:
    """Force a session's interaction claim directly on the test DB.

    Documented seam for the status-repair sweep's tests (Pass B,
    ``app/services/system/status_repair_sessions.py``). Two different
    honesty levels hide behind one signature:

    * ``interaction_status="pending_stream"`` IS reachable through the API —
      sending a message while the session's environment is not running
      leaves the row in exactly this state, with a real ``updated_at``. The
      only thing this seam does for that case is move ``updated_at`` back in
      time, because the sweep's threshold (15 min by default) is too long to
      block a test on, the same "age it, don't fabricate it" seam as
      ``age_environment_status_changed_at``.
    * ``interaction_status="running"`` left stuck is, by contrast,
      unreachable through this API in-process. ``process_pending_messages``
      clears it in a bare ``finally`` (not ``except Exception``) shielded
      against cancellation, so anything this test process can do to a
      streaming task — raise, cancel — is exactly what that `finally` already
      recovers from. Only a killed *process* leaves the claim standing, and
      nothing in a single pytest process can simulate that. This branch
      therefore does not "age a real state" — it constructs the state
      directly, the same way ``set_environment_status`` does for
      ``AgentEnvironment.status`` when the equivalent real path is stubbed
      out for tests.

    ``pending_messages_count``, when given, is set independent of the actual
    pending-message rows — so a test can put the counter deliberately out of
    sync and assert Pass B's recompute step corrects it.
    """
    from datetime import UTC, datetime

    from app.models.sessions.session import Session as ChatSession

    if isinstance(session_id, str):
        session_id = uuid.UUID(session_id)
    chat_session = db.get(ChatSession, session_id)
    assert chat_session is not None, f"Session {session_id} not found"
    chat_session.interaction_status = interaction_status
    chat_session.streaming_started_at = streaming_started_at
    if updated_at is not None:
        chat_session.updated_at = updated_at
    else:
        chat_session.updated_at = datetime.now(UTC)
    if pending_messages_count is not None:
        chat_session.pending_messages_count = pending_messages_count
    db.add(chat_session)
    db.commit()
    db.refresh(chat_session)
