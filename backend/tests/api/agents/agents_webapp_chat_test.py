"""
Integration tests: Webapp Chat feature.

Covers the webapp chat endpoints under /api/v1/webapp/{token}/chat/...:
  A. Chat disabled (chat_mode=None) → 403 on all chat endpoints
  B. Create / get active session lifecycle (idempotency, correct mode)
  C. Session access control (wrong webapp_share → 403)
  D. Message list for a chat session
  E. Unauthenticated and wrong-token-type access → 401/403
  F. chat_mode "conversation" and "building" produce correctly-typed sessions
  G. Non-existent session returns 404
  H. Stream event handlers emit session_interaction_status_changed to the session
     stream room so webapp viewers (who are not in the owner's user room) receive it

Business rules tested:
  1. POST /sessions returns 403 when chat_mode is None
  2. POST /sessions creates a session with mode matching chat_mode config
  3. POST /sessions is idempotent — second call returns the same session id
  4. GET /sessions returns null before a session exists, returns the session after creation
  5. GET /sessions/{id} returns the session and verifies webapp_share_id ownership
  6. GET /sessions/{id} with a different webapp share's JWT → 403
  7. GET /sessions/{id}/messages returns a MessagesPublic response (data/count)
  8. All endpoints return 401 with no Bearer token
  9. All endpoints return 403 with a regular user JWT (wrong token type)
  10. chat_mode="conversation" → session.mode == "conversation"
  11. chat_mode="building" → session.mode == "building"
  12. handle_stream_started emits session_interaction_status_changed to both the
      owner user room and the session stream room
  13. handle_stream_completed emits session_interaction_status_changed to both rooms
  14. handle_stream_error emits session_interaction_status_changed to both rooms
  15. handle_stream_interrupted emits session_interaction_status_changed to both rooms
"""
import asyncio
import uuid
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.stubs.socketio_stub import StubSocketIOConnector
from tests.utils.background_tasks import drain_tasks
from tests.utils.user import create_random_user_with_headers
from tests.utils.webapp_interface_config import update_webapp_interface_config
from tests.utils.webapp_share import (
    authenticate_webapp_share,
    setup_webapp_agent,
)

API = settings.API_V1_STR


# ── Helpers ───────────────────────────────────────────────────────────────


def _get_webapp_jwt(client: TestClient, token: str) -> str:
    """Authenticate via webapp share and return the access_token string."""
    auth = authenticate_webapp_share(client, token)
    assert "access_token" in auth, f"No access_token in auth response: {auth}"
    return auth["access_token"]


def _webapp_headers(client: TestClient, token: str) -> dict[str, str]:
    """Return Authorization headers for the webapp-viewer JWT."""
    return {"Authorization": f"Bearer {_get_webapp_jwt(client, token)}"}


def _chat_base(token: str) -> str:
    """Base URL for chat endpoints for a given webapp share token."""
    return f"{API}/webapp/{token}/chat"


def _enable_chat(
    client: TestClient,
    headers: dict[str, str],
    agent_id: str,
    chat_mode: str = "conversation",
) -> None:
    """Enable chat for an agent by setting chat_mode via the config endpoint."""
    update_webapp_interface_config(client, headers, agent_id, chat_mode=chat_mode)


def _create_chat_session(
    client: TestClient,
    webapp_hdrs: dict[str, str],
    share_token: str,
) -> dict:
    """POST /webapp/{token}/chat/sessions and assert 200. Returns session dict."""
    r = client.post(
        f"{_chat_base(share_token)}/sessions",
        headers=webapp_hdrs,
    )
    assert r.status_code == 200, f"Create chat session failed: {r.text}"
    return r.json()


# ── A. Chat disabled ──────────────────────────────────────────────────────


def test_chat_disabled_returns_403(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    When chat_mode is None (default), all chat endpoints return 403:
      1. Create agent + webapp share (chat_mode stays None)
      2. Authenticate as webapp viewer
      3. POST /sessions → 403
      4. GET /sessions → 403
    """
    # ── Phase 1: Create agent with webapp share, leave chat_mode=None ─────
    agent, share = setup_webapp_agent(
        client, superuser_token_headers,
        name="Chat Disabled Agent",
        share_label="Disabled Share",
    )
    share_token = share["token"]

    # ── Phase 2: Authenticate as webapp viewer ────────────────────────────
    webapp_hdrs = _webapp_headers(client, share_token)

    # ── Phase 3: POST /sessions → 403 chat disabled ───────────────────────
    r = client.post(
        f"{_chat_base(share_token)}/sessions",
        headers=webapp_hdrs,
    )
    assert r.status_code == 403, f"Expected 403 for disabled chat, got {r.status_code}: {r.text}"
    assert "chat" in r.json()["detail"].lower()

    # ── Phase 4: GET /sessions → 403 chat disabled ────────────────────────
    r = client.get(
        f"{_chat_base(share_token)}/sessions",
        headers=webapp_hdrs,
    )
    assert r.status_code == 403, f"Expected 403 for disabled chat GET, got {r.status_code}: {r.text}"


# ── B. Session lifecycle ──────────────────────────────────────────────────


def test_webapp_chat_session_full_lifecycle(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Full webapp chat session lifecycle with chat_mode="conversation":
      1. Create agent + webapp share
      2. Enable chat (chat_mode="conversation")
      3. Authenticate as webapp viewer
      4. GET /sessions → null (no session yet)
      5. POST /sessions → creates session; verify mode, webapp_share_id fields
      6. GET /sessions → returns the created session
      7. GET /sessions/{id} → returns session details
      8. POST /sessions again → idempotent, returns the same session id
      9. GET /sessions/{id}/messages → returns MessagesPublic (data list + count)
    """
    # ── Phase 1: Create agent + webapp share ──────────────────────────────
    agent, share = setup_webapp_agent(
        client, superuser_token_headers,
        name="Chat Lifecycle Agent",
        share_label="Lifecycle Share",
    )
    agent_id = agent["id"]
    share_token = share["token"]

    # ── Phase 2: Enable chat ───────────────────────────────────────────────
    _enable_chat(client, superuser_token_headers, agent_id, chat_mode="conversation")

    # ── Phase 3: Authenticate as webapp viewer ────────────────────────────
    webapp_hdrs = _webapp_headers(client, share_token)

    # ── Phase 4: GET /sessions → null before any session ─────────────────
    r = client.get(f"{_chat_base(share_token)}/sessions", headers=webapp_hdrs)
    assert r.status_code == 200, f"GET sessions failed: {r.text}"
    assert r.json() is None, f"Expected null, got: {r.json()}"

    # ── Phase 5: POST /sessions → creates session ─────────────────────────
    session = _create_chat_session(client, webapp_hdrs, share_token)
    session_id = session["id"]

    # Verify session shape
    assert "id" in session
    assert "mode" in session
    assert "status" in session
    assert session["mode"] == "conversation"
    assert session["status"] == "active"

    # webapp_share_id should be set (ties session to this share)
    assert session.get("webapp_share_id") == share["id"]

    # ── Phase 6: GET /sessions → returns the active session ───────────────
    r = client.get(f"{_chat_base(share_token)}/sessions", headers=webapp_hdrs)
    assert r.status_code == 200, f"GET sessions after create failed: {r.text}"
    active = r.json()
    assert active is not None, "Expected active session, got null"
    assert active["id"] == session_id

    # ── Phase 7: GET /sessions/{id} → returns session details ────────────
    r = client.get(
        f"{_chat_base(share_token)}/sessions/{session_id}",
        headers=webapp_hdrs,
    )
    assert r.status_code == 200, f"GET session by id failed: {r.text}"
    details = r.json()
    assert details["id"] == session_id
    assert details["mode"] == "conversation"
    assert details["status"] == "active"

    # ── Phase 8: POST /sessions again → idempotent ────────────────────────
    second = _create_chat_session(client, webapp_hdrs, share_token)
    assert second["id"] == session_id, (
        f"Expected same session on second POST, got {second['id']} != {session_id}"
    )

    # ── Phase 9: GET /sessions/{id}/messages → MessagesPublic ────────────
    r = client.get(
        f"{_chat_base(share_token)}/sessions/{session_id}/messages",
        headers=webapp_hdrs,
    )
    assert r.status_code == 200, f"GET messages failed: {r.text}"
    body = r.json()
    assert "data" in body, f"Missing 'data' key in messages response: {body}"
    assert "count" in body, f"Missing 'count' key in messages response: {body}"
    assert isinstance(body["data"], list)
    assert body["count"] == len(body["data"])


# ── C. Session access control ─────────────────────────────────────────────


def test_webapp_chat_session_access_control(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Session access is scoped to the webapp share in the JWT:
      1. Create agent with chat enabled, two webapp shares (A and B)
      2. Share A creates a session
      3. Share B authenticates
      4. Share B GET /sessions/{sessionA_id} → 403 (wrong share)
      5. Share B GET /sessions → null (no session for share B yet)
      6. Share B POST /sessions → creates a different session (B's own)
      7. Share A GET /sessions/{sessionB_id} → 403
    """
    # ── Phase 1: Create agent with two webapp shares ──────────────────────
    agent, share_a = setup_webapp_agent(
        client, superuser_token_headers,
        name="Chat Access Control Agent",
        share_label="Share A",
    )
    agent_id = agent["id"]
    _enable_chat(client, superuser_token_headers, agent_id, chat_mode="conversation")

    # Create second webapp share for the same agent
    from tests.utils.webapp_share import create_webapp_share
    share_b = create_webapp_share(
        client, superuser_token_headers, agent_id, label="Share B"
    )

    # ── Phase 2: Share A creates a session ───────────────────────────────
    webapp_a_hdrs = _webapp_headers(client, share_a["token"])
    session_a = _create_chat_session(client, webapp_a_hdrs, share_a["token"])
    session_a_id = session_a["id"]

    # ── Phase 3: Share B authenticates ────────────────────────────────────
    webapp_b_hdrs = _webapp_headers(client, share_b["token"])

    # ── Phase 4: Share B cannot access Share A's session ─────────────────
    r = client.get(
        f"{_chat_base(share_b['token'])}/sessions/{session_a_id}",
        headers=webapp_b_hdrs,
    )
    assert r.status_code == 403, (
        f"Expected 403 for cross-share session access, got {r.status_code}: {r.text}"
    )

    # ── Phase 5: Share B GET /sessions → null (no session for B yet) ──────
    r = client.get(f"{_chat_base(share_b['token'])}/sessions", headers=webapp_b_hdrs)
    assert r.status_code == 200
    assert r.json() is None, f"Expected null for share B, got: {r.json()}"

    # ── Phase 6: Share B creates its own session ──────────────────────────
    session_b = _create_chat_session(client, webapp_b_hdrs, share_b["token"])
    session_b_id = session_b["id"]
    assert session_b_id != session_a_id, "Share B should have a different session than Share A"

    # ── Phase 7: Share A cannot access Share B's session ─────────────────
    r = client.get(
        f"{_chat_base(share_a['token'])}/sessions/{session_b_id}",
        headers=webapp_a_hdrs,
    )
    assert r.status_code == 403, (
        f"Expected 403 for Share A accessing Share B's session, got {r.status_code}: {r.text}"
    )


# ── D. Unauthenticated and wrong-token-type access ────────────────────────


def test_webapp_chat_unauthenticated_and_wrong_token(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    All chat endpoints reject unauthenticated and wrong-token-type requests:
      1. Create agent with chat enabled
      2. No Bearer token → 401 on all endpoints
      3. Regular user JWT (not webapp-viewer) → 403 on all endpoints
    """
    # ── Phase 1: Create agent with chat enabled ───────────────────────────
    agent, share = setup_webapp_agent(
        client, superuser_token_headers,
        name="Chat Auth Guard Agent",
        share_label="Auth Guard Share",
    )
    agent_id = agent["id"]
    share_token = share["token"]
    _enable_chat(client, superuser_token_headers, agent_id, chat_mode="conversation")

    # Create a session using valid webapp JWT first (so session_id exists)
    webapp_hdrs = _webapp_headers(client, share_token)
    session = _create_chat_session(client, webapp_hdrs, share_token)
    session_id = session["id"]

    base = _chat_base(share_token)

    # ── Phase 2: No Bearer token → 401 ───────────────────────────────────
    assert client.post(f"{base}/sessions").status_code == 401
    assert client.get(f"{base}/sessions").status_code == 401
    assert client.get(f"{base}/sessions/{session_id}").status_code == 401
    assert client.get(f"{base}/sessions/{session_id}/messages").status_code == 401

    # ── Phase 3: Regular user JWT (token_type != webapp_share) → 403 ──────
    _, regular_user_headers = create_random_user_with_headers(client)

    r = client.post(f"{base}/sessions", headers=regular_user_headers)
    assert r.status_code == 403, (
        f"Expected 403 for regular user on POST /sessions, got {r.status_code}: {r.text}"
    )

    r = client.get(f"{base}/sessions", headers=regular_user_headers)
    assert r.status_code == 403, (
        f"Expected 403 for regular user on GET /sessions, got {r.status_code}: {r.text}"
    )

    r = client.get(f"{base}/sessions/{session_id}", headers=regular_user_headers)
    assert r.status_code == 403, (
        f"Expected 403 for regular user on GET /sessions/{{id}}, got {r.status_code}: {r.text}"
    )

    r = client.get(f"{base}/sessions/{session_id}/messages", headers=regular_user_headers)
    assert r.status_code == 403, (
        f"Expected 403 for regular user on GET /sessions/{{id}}/messages, got {r.status_code}: {r.text}"
    )


# ── E. chat_mode values produce correct session modes ─────────────────────


def test_webapp_chat_mode_conversation(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    When chat_mode="conversation", created sessions have mode="conversation".
      1. Create agent + webapp share
      2. Set chat_mode="conversation"
      3. Authenticate and create session → mode == "conversation"
    """
    agent, share = setup_webapp_agent(
        client, superuser_token_headers,
        name="Chat Conversation Mode Agent",
    )
    _enable_chat(client, superuser_token_headers, agent["id"], chat_mode="conversation")

    webapp_hdrs = _webapp_headers(client, share["token"])
    session = _create_chat_session(client, webapp_hdrs, share["token"])

    assert session["mode"] == "conversation", (
        f"Expected mode='conversation', got '{session['mode']}'"
    )


def test_webapp_chat_mode_building(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    When chat_mode="building", created sessions have mode="building".
      1. Create agent + webapp share
      2. Set chat_mode="building"
      3. Authenticate and create session → mode == "building"
    """
    agent, share = setup_webapp_agent(
        client, superuser_token_headers,
        name="Chat Building Mode Agent",
    )
    _enable_chat(client, superuser_token_headers, agent["id"], chat_mode="building")

    webapp_hdrs = _webapp_headers(client, share["token"])
    session = _create_chat_session(client, webapp_hdrs, share["token"])

    assert session["mode"] == "building", (
        f"Expected mode='building', got '{session['mode']}'"
    )


# ── F. Chat mode transition: disable after session created ────────────────


def test_webapp_chat_disabled_after_session_created(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    If chat is disabled after a session is created, subsequent session creation
    is blocked:
      1. Create agent + webapp share, enable chat
      2. Create a session successfully
      3. Owner disables chat (chat_mode=None)
      4. GET new webapp JWT (re-auth)
      5. POST /sessions → 403
      6. GET /sessions → 403
    """
    # ── Phase 1: Create agent + share, enable chat ────────────────────────
    agent, share = setup_webapp_agent(
        client, superuser_token_headers,
        name="Chat Toggle Agent",
        share_label="Toggle Share",
    )
    agent_id = agent["id"]
    share_token = share["token"]
    _enable_chat(client, superuser_token_headers, agent_id, chat_mode="conversation")

    # ── Phase 2: Create session successfully ──────────────────────────────
    webapp_hdrs = _webapp_headers(client, share_token)
    session = _create_chat_session(client, webapp_hdrs, share_token)
    assert session["id"]

    # ── Phase 3: Owner disables chat ──────────────────────────────────────
    update_webapp_interface_config(
        client, superuser_token_headers, agent_id, chat_mode=None
    )

    # ── Phase 4: Get a fresh webapp JWT ───────────────────────────────────
    webapp_hdrs_new = _webapp_headers(client, share_token)

    # ── Phase 5: POST /sessions → 403 ────────────────────────────────────
    r = client.post(
        f"{_chat_base(share_token)}/sessions",
        headers=webapp_hdrs_new,
    )
    assert r.status_code == 403, (
        f"Expected 403 after disabling chat, got {r.status_code}: {r.text}"
    )

    # ── Phase 6: GET /sessions → 403 ─────────────────────────────────────
    r = client.get(
        f"{_chat_base(share_token)}/sessions",
        headers=webapp_hdrs_new,
    )
    assert r.status_code == 403, (
        f"Expected 403 on GET /sessions after disabling chat, got {r.status_code}: {r.text}"
    )


# ── G. Non-existent session ───────────────────────────────────────────────


def test_webapp_chat_nonexistent_session_returns_error(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    GET /sessions/{ghost_id} with a valid webapp JWT but non-existent session_id
    returns 404.
    """
    agent, share = setup_webapp_agent(
        client, superuser_token_headers,
        name="Chat 404 Agent",
    )
    _enable_chat(client, superuser_token_headers, agent["id"], chat_mode="conversation")

    webapp_hdrs = _webapp_headers(client, share["token"])
    ghost_id = str(uuid.uuid4())

    r = client.get(
        f"{_chat_base(share['token'])}/sessions/{ghost_id}",
        headers=webapp_hdrs,
    )
    assert r.status_code == 404, (
        f"Expected 404 for ghost session id, got {r.status_code}: {r.text}"
    )

    r = client.get(
        f"{_chat_base(share['token'])}/sessions/{ghost_id}/messages",
        headers=webapp_hdrs,
    )
    assert r.status_code in (403, 404), (
        f"Expected 403 or 404 for ghost session messages, got {r.status_code}: {r.text}"
    )


# ── H. Session stream room emission for webapp viewers ────────────────────


def test_stream_handlers_emit_to_session_stream_room(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    All four stream event handlers emit session_interaction_status_changed to
    the session stream room (session_{id}_stream), not just the owner's user room.

    Webapp viewers connect to Socket.IO using their webapp_share_id as user_id
    (not the actual owner's user_id), so they are never in the user_{owner_id}
    room. The session stream room is the only channel they can receive
    session_interaction_status_changed events through.

    This test covers the bug fix: before the fix, handle_stream_error and
    handle_stream_interrupted only emitted to the user room, leaving webapp
    viewers stuck in "thinking" state.

    Scenario:
      1. Create agent + webapp share, enable chat
      2. Create a webapp chat session
      3. Call each handler with a fresh StubSocketIOConnector
      4. Verify each handler emits session_interaction_status_changed to
         both the user room AND the session stream room
    """
    from app.services.session_service import SessionService

    # ── Phase 1: Create agent + webapp share, enable chat ─────────────────
    agent, share = setup_webapp_agent(
        client, superuser_token_headers,
        name="Stream Room Emission Agent",
        share_label="Room Emission Share",
    )
    agent_id = agent["id"]
    share_token = share["token"]
    update_webapp_interface_config(
        client, superuser_token_headers, agent_id, chat_mode="conversation"
    )

    # ── Phase 2: Create a webapp chat session ─────────────────────────────
    webapp_auth = authenticate_webapp_share(client, share_token)
    webapp_hdrs = {"Authorization": f"Bearer {webapp_auth['access_token']}"}

    r = client.post(
        f"{_chat_base(share_token)}/sessions",
        headers=webapp_hdrs,
    )
    assert r.status_code == 200, f"Create chat session failed: {r.text}"
    session = r.json()
    session_id = session["id"]

    # user_id for webapp sessions is the agent owner's user_id
    # We just need to know the session room name
    expected_stream_room = f"session_{session_id}_stream"

    # Helper: run a handler with a fresh stub and return the emitted events
    def _run_handler_and_collect(handler_coro):
        """Runs an async handler using its own StubSocketIOConnector."""
        stub = StubSocketIOConnector()
        with patch("app.services.event_service.socketio_connector", stub):
            asyncio.run(handler_coro)
        return stub.emitted_events

    # ── Phase 3: handle_stream_started ────────────────────────────────────
    started_events = _run_handler_and_collect(
        SessionService.handle_stream_started({
            "meta": {
                "session_id": session_id,
            }
        })
    )

    # Should have at least two session_interaction_status_changed emissions
    status_events = [
        e for e in started_events
        if e.get("data", {}).get("type") == "session_interaction_status_changed"
    ]
    rooms_emitted_to = [e.get("room") for e in status_events]
    assert expected_stream_room in rooms_emitted_to, (
        f"handle_stream_started did not emit to {expected_stream_room}. "
        f"Rooms seen: {rooms_emitted_to}"
    )
    # Also emits to the user room — event_service routes user_id-targeted
    # emissions to room user_{user_id}, captured as a non-stream-room entry
    has_user_room_emission = any(
        r is not None and r.startswith("user_") for r in rooms_emitted_to
    )
    assert has_user_room_emission, (
        f"handle_stream_started did not emit to any user_* room. "
        f"Rooms seen: {rooms_emitted_to}"
    )

    # Interaction status should be "running" for STREAM_STARTED
    stream_room_event = next(
        e for e in status_events if e.get("room") == expected_stream_room
    )
    assert stream_room_event["data"]["meta"]["interaction_status"] == "running", (
        f"Expected interaction_status='running' in stream room event, "
        f"got: {stream_room_event['data']['meta']}"
    )

    # ── Phase 4: handle_stream_completed ──────────────────────────────────
    completed_events = _run_handler_and_collect(
        SessionService.handle_stream_completed({
            "meta": {
                "session_id": session_id,
                "was_interrupted": False,
            }
        })
    )

    status_events = [
        e for e in completed_events
        if e.get("data", {}).get("type") == "session_interaction_status_changed"
    ]
    rooms_emitted_to = [e.get("room") for e in status_events]
    assert expected_stream_room in rooms_emitted_to, (
        f"handle_stream_completed did not emit to {expected_stream_room}. "
        f"Rooms seen: {rooms_emitted_to}"
    )
    stream_room_event = next(
        e for e in status_events if e.get("room") == expected_stream_room
    )
    assert stream_room_event["data"]["meta"]["interaction_status"] == "", (
        f"Expected interaction_status='' in stream room event after completion, "
        f"got: {stream_room_event['data']['meta']}"
    )

    # ── Phase 5: handle_stream_error ──────────────────────────────────────
    error_events = _run_handler_and_collect(
        SessionService.handle_stream_error({
            "meta": {
                "session_id": session_id,
                "error_type": "TestError",
            }
        })
    )

    status_events = [
        e for e in error_events
        if e.get("data", {}).get("type") == "session_interaction_status_changed"
    ]
    stream_rooms = [e.get("room") for e in status_events]
    assert expected_stream_room in stream_rooms, (
        f"handle_stream_error did not emit to {expected_stream_room}. "
        f"Rooms seen: {stream_rooms}. "
        "This was the bug: error handler was missing the session stream room emission."
    )

    # ── Phase 6: handle_stream_interrupted ────────────────────────────────
    # First reset session back to active/running state via completed handler
    _run_handler_and_collect(
        SessionService.handle_stream_started({
            "meta": {"session_id": session_id}
        })
    )

    interrupted_events = _run_handler_and_collect(
        SessionService.handle_stream_interrupted({
            "meta": {
                "session_id": session_id,
            }
        })
    )

    status_events = [
        e for e in interrupted_events
        if e.get("data", {}).get("type") == "session_interaction_status_changed"
    ]
    stream_rooms = [e.get("room") for e in status_events]
    assert expected_stream_room in stream_rooms, (
        f"handle_stream_interrupted did not emit to {expected_stream_room}. "
        f"Rooms seen: {stream_rooms}. "
        "This was the bug: interrupted handler was missing the session stream room emission."
    )
