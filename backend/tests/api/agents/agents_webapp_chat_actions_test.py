"""
Integration tests: Webapp Chat — Bi-directional communication (actions & events).

Covers the stream event emission and webapp action framework for webapp chat
endpoints under /api/v1/webapp/{token}/chat/...:
  H. Stream event handlers emit session_interaction_status_changed to the session
     stream room so webapp viewers (who are not in the owner's user room) receive it
  K. Webapp action framework — agent can emit webapp_action events via XML tags

Business rules tested:
  12. handle_stream_started emits session_interaction_status_changed to both the
      owner user room and the session stream room
  13. handle_stream_completed emits session_interaction_status_changed to both rooms
  14. handle_stream_error emits session_interaction_status_changed to both rooms
  15. handle_stream_interrupted emits session_interaction_status_changed to both rooms
  24. Agent response with webapp_action tag → action emitted as webapp_action stream event,
      tag stripped from stored message content
  25. Multiple webapp_action tags in one response → all actions emitted, none in stored content
  26. Malformed JSON inside webapp_action tag → graceful skip (no crash), tag still stripped
  27. webapp_action with various action types → correct action/data fields in emitted event
"""
import asyncio
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.core.config import settings
from app.services.message_service import _extract_webapp_actions
from tests.stubs.agent_env_stub import StubAgentEnvConnector
from tests.stubs.socketio_stub import StubSocketIOConnector
from tests.utils.background_tasks import drain_tasks
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


# ── K. Webapp action framework ─────────────────────────────────────────────


def test_webapp_action_tag_stripped_from_stored_content_and_event_emitted(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    When an agent response contains a <webapp_action> tag, the framework:
      1. Strips the tag from the message content stored in the database
      2. Emits a webapp_action stream event with the correct action and data

    Scenario:
      1. Create agent + webapp share, enable chat
      2. Create a webapp chat session
      3. Send a message; configure stub agent-env to return a response with
         a webapp_action tag for "refresh_page"
      4. Drain background tasks (triggers process_pending_messages)
      5. Verify the stored agent message content has NO webapp_action tags
      6. Verify a webapp_action stream event was emitted with action="refresh_page"
    """
    # ── Phase 1: Create agent + webapp share, enable chat ─────────────────
    agent, share = setup_webapp_agent(
        client, superuser_token_headers,
        name="Webapp Action Refresh Agent",
        share_label="Action Refresh Share",
    )
    agent_id = agent["id"]
    share_token = share["token"]
    _enable_chat(client, superuser_token_headers, agent_id, chat_mode="conversation")

    # ── Phase 2: Create webapp chat session ───────────────────────────────
    webapp_hdrs = _webapp_headers(client, share["token"])
    session = _create_chat_session(client, webapp_hdrs, share["token"])
    session_id = session["id"]

    # ── Phase 3: Send message; agent returns response with webapp_action tag
    agent_response_text = (
        "I've refreshed the page for you."
        "<webapp_action>{\"action\": \"refresh_page\"}</webapp_action>"
    )
    stub = StubAgentEnvConnector(response_text=agent_response_text)
    socketio_stub = StubSocketIOConnector()

    patch_env = "app.services.message_service.agent_env_connector"
    patch_sio = "app.services.event_service.socketio_connector"

    with patch(patch_env, stub), patch(patch_sio, socketio_stub):
        r = client.post(
            f"{_chat_base(share_token)}/sessions/{session_id}/messages/stream",
            headers=webapp_hdrs,
            json={"content": "Please refresh", "file_ids": []},
        )
        assert r.status_code == 200, f"POST stream failed: {r.text}"
        drain_tasks()

    # ── Phase 4: Verify stored agent message content is clean ─────────────
    r_msgs = client.get(
        f"{_chat_base(share_token)}/sessions/{session_id}/messages",
        headers=webapp_hdrs,
    )
    assert r_msgs.status_code == 200
    messages_data = r_msgs.json()

    agent_messages = [m for m in messages_data["data"] if m["role"] == "agent"]
    assert len(agent_messages) >= 1, "Expected at least one agent message"
    stored_content = agent_messages[-1]["content"]

    assert "<webapp_action>" not in stored_content, (
        f"webapp_action tag must be stripped from stored content.\n"
        f"Got: {stored_content!r}"
    )
    assert "refresh_page" not in stored_content, (
        "Action name must not appear in visible message content"
    )
    assert "I've refreshed the page for you." in stored_content, (
        "Visible text before the tag must be preserved in stored content"
    )

    # ── Phase 5: Verify webapp_action stream event was emitted ────────────
    stream_events = [
        e for e in socketio_stub.emitted_events
        if e.get("event") == "stream_event"
        and e.get("data", {}).get("event_type") == "webapp_action"
    ]
    assert len(stream_events) >= 1, (
        f"Expected at least one webapp_action stream event to be emitted.\n"
        f"All emitted events: {socketio_stub.emitted_events}"
    )

    action_event = stream_events[0]
    event_data = action_event["data"]["data"]
    assert event_data["action"] == "refresh_page", (
        f"Expected action='refresh_page', got: {event_data}"
    )

    # Emitted to the session stream room
    expected_room = f"session_{session_id}_stream"
    assert action_event.get("room") == expected_room, (
        f"Expected webapp_action event in room {expected_room!r}, "
        f"got room: {action_event.get('room')!r}"
    )


def test_webapp_action_multiple_tags_all_emitted_none_in_stored_content(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    When an agent response contains multiple <webapp_action> tags, all actions
    are emitted as separate stream events and none appear in the stored content.

    Scenario:
      1. Agent response with two action tags: update_form and reload_data
      2. Both events emitted, stored content is clean
    """
    agent, share = setup_webapp_agent(
        client, superuser_token_headers,
        name="Multi Action Agent",
        share_label="Multi Action Share",
    )
    _enable_chat(client, superuser_token_headers, agent["id"], chat_mode="conversation")

    webapp_hdrs = _webapp_headers(client, share["token"])
    session = _create_chat_session(client, webapp_hdrs, share["token"])
    session_id = session["id"]

    agent_response = (
        "Updated the filter and reloading data."
        "<webapp_action>{\"action\": \"update_form\", \"data\": {\"form_id\": \"report-filter\", \"values\": {\"date_range\": \"2024-Q4\"}}}</webapp_action>"
        "<webapp_action>{\"action\": \"reload_data\", \"data\": {\"endpoint\": \"/api/reports\"}}</webapp_action>"
    )
    stub = StubAgentEnvConnector(response_text=agent_response)
    socketio_stub = StubSocketIOConnector()

    with patch("app.services.message_service.agent_env_connector", stub), \
         patch("app.services.event_service.socketio_connector", socketio_stub):
        r = client.post(
            f"{_chat_base(share['token'])}/sessions/{session_id}/messages/stream",
            headers=webapp_hdrs,
            json={"content": "Set date range to Q4 2024 and reload", "file_ids": []},
        )
        assert r.status_code == 200
        drain_tasks()

    # Verify stored content is clean
    r_msgs = client.get(
        f"{_chat_base(share['token'])}/sessions/{session_id}/messages",
        headers=webapp_hdrs,
    )
    messages_data = r_msgs.json()
    agent_messages = [m for m in messages_data["data"] if m["role"] == "agent"]
    assert len(agent_messages) >= 1
    stored_content = agent_messages[-1]["content"]

    assert "<webapp_action>" not in stored_content, (
        f"No webapp_action tags should appear in stored content. Got: {stored_content!r}"
    )
    assert "Updated the filter and reloading data." in stored_content, (
        "Visible text must be preserved"
    )

    # Verify two webapp_action events were emitted
    action_events = [
        e for e in socketio_stub.emitted_events
        if e.get("event") == "stream_event"
        and e.get("data", {}).get("event_type") == "webapp_action"
    ]
    assert len(action_events) >= 2, (
        f"Expected 2 webapp_action events, got {len(action_events)}.\n"
        f"Events: {action_events}"
    )

    emitted_actions = {e["data"]["data"]["action"] for e in action_events}
    assert "update_form" in emitted_actions, "update_form action must be emitted"
    assert "reload_data" in emitted_actions, "reload_data action must be emitted"

    # Verify action data is correct for update_form
    update_event = next(
        e for e in action_events if e["data"]["data"]["action"] == "update_form"
    )
    assert update_event["data"]["data"]["data"]["form_id"] == "report-filter"
    assert update_event["data"]["data"]["data"]["values"]["date_range"] == "2024-Q4"

    # Verify action data is correct for reload_data
    reload_event = next(
        e for e in action_events if e["data"]["data"]["action"] == "reload_data"
    )
    assert reload_event["data"]["data"]["data"]["endpoint"] == "/api/reports"


def test_webapp_action_malformed_json_does_not_crash_stream(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    When a <webapp_action> tag contains malformed JSON, the stream continues
    successfully — the bad tag is silently skipped (no event emitted for it)
    but the tag is still stripped from stored content. Valid tags in the same
    response are still processed normally.

    Scenario:
      1. Agent response with one malformed tag and one valid tag
      2. Only the valid action is emitted; no crash
      3. Both tags are stripped from stored content
    """
    agent, share = setup_webapp_agent(
        client, superuser_token_headers,
        name="Malformed Action Agent",
        share_label="Malformed Action Share",
    )
    _enable_chat(client, superuser_token_headers, agent["id"], chat_mode="conversation")

    webapp_hdrs = _webapp_headers(client, share["token"])
    session = _create_chat_session(client, webapp_hdrs, share["token"])
    session_id = session["id"]

    agent_response = (
        "Done."
        "<webapp_action>THIS IS NOT JSON AT ALL!!</webapp_action>"
        "<webapp_action>{\"action\": \"navigate\", \"data\": {\"path\": \"/dashboard\"}}</webapp_action>"
    )
    stub = StubAgentEnvConnector(response_text=agent_response)
    socketio_stub = StubSocketIOConnector()

    with patch("app.services.message_service.agent_env_connector", stub), \
         patch("app.services.event_service.socketio_connector", socketio_stub):
        r = client.post(
            f"{_chat_base(share['token'])}/sessions/{session_id}/messages/stream",
            headers=webapp_hdrs,
            json={"content": "Navigate to dashboard", "file_ids": []},
        )
        assert r.status_code == 200, f"Stream should succeed despite bad JSON tag: {r.text}"
        drain_tasks()

    # Stored content must have both tags stripped (clean)
    r_msgs = client.get(
        f"{_chat_base(share['token'])}/sessions/{session_id}/messages",
        headers=webapp_hdrs,
    )
    messages_data = r_msgs.json()
    agent_messages = [m for m in messages_data["data"] if m["role"] == "agent"]
    assert len(agent_messages) >= 1
    stored_content = agent_messages[-1]["content"]

    assert "<webapp_action>" not in stored_content, (
        f"All webapp_action tags (even malformed ones) must be stripped. Got: {stored_content!r}"
    )
    assert "Done." in stored_content, "Visible text must be preserved"

    # Only the valid navigate action should be emitted (the malformed one is skipped)
    action_events = [
        e for e in socketio_stub.emitted_events
        if e.get("event") == "stream_event"
        and e.get("data", {}).get("event_type") == "webapp_action"
    ]
    assert len(action_events) == 1, (
        f"Only 1 valid action event should be emitted; malformed tag is skipped. "
        f"Got: {[e['data']['data']['action'] for e in action_events]}"
    )
    assert action_events[0]["data"]["data"]["action"] == "navigate"
    assert action_events[0]["data"]["data"]["data"]["path"] == "/dashboard"


def test_extract_webapp_actions_unit() -> None:
    """
    Unit test for the _extract_webapp_actions helper.

    Verifies the extraction logic in isolation — no HTTP calls, no DB.
    """
    # Basic: single tag, no surrounding text
    actions, cleaned = _extract_webapp_actions(
        '<webapp_action>{"action": "refresh_page"}</webapp_action>'
    )
    assert len(actions) == 1
    assert actions[0]["action"] == "refresh_page"
    assert actions[0]["data"] == {}
    assert "<webapp_action>" not in cleaned

    # Tag with data
    actions, cleaned = _extract_webapp_actions(
        'Hello <webapp_action>{"action": "navigate", "data": {"path": "/foo"}}</webapp_action> world'
    )
    assert len(actions) == 1
    assert actions[0]["action"] == "navigate"
    assert actions[0]["data"] == {"path": "/foo"}
    assert "Hello" in cleaned
    assert "world" in cleaned
    assert "<webapp_action>" not in cleaned

    # Multiple tags
    actions, cleaned = _extract_webapp_actions(
        '<webapp_action>{"action": "update_form", "data": {"form_id": "f1"}}</webapp_action>'
        "Some text"
        '<webapp_action>{"action": "show_notification", "data": {"message": "Done", "type": "success"}}</webapp_action>'
    )
    assert len(actions) == 2
    assert actions[0]["action"] == "update_form"
    assert actions[1]["action"] == "show_notification"
    assert "<webapp_action>" not in cleaned
    assert "Some text" in cleaned

    # Malformed JSON: tag is stripped but action skipped
    actions, cleaned = _extract_webapp_actions(
        '<webapp_action>not json</webapp_action>'
        "visible text"
    )
    assert len(actions) == 0  # malformed → skipped
    assert "<webapp_action>" not in cleaned
    assert "visible text" in cleaned

    # Missing action field: tag stripped, action skipped
    actions, cleaned = _extract_webapp_actions(
        '<webapp_action>{"foo": "bar"}</webapp_action>'
    )
    assert len(actions) == 0
    assert "<webapp_action>" not in cleaned

    # No tags: no change
    actions, cleaned = _extract_webapp_actions("plain text without tags")
    assert len(actions) == 0
    assert cleaned == "plain text without tags"

    # Multiline content inside tag
    actions, cleaned = _extract_webapp_actions(
        '<webapp_action>\n{"action": "refresh_page"}\n</webapp_action>'
    )
    assert len(actions) == 1
    assert actions[0]["action"] == "refresh_page"
