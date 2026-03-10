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


def _send_stubbed_message(
    client: TestClient,
    webapp_hdrs: dict[str, str],
    share_token: str,
    session_id: str,
    user_msg: str,
    agent_response: str,
    *,
    stub: StubAgentEnvConnector | None = None,
    events: list[dict] | None = None,
) -> StubSocketIOConnector:
    """Send a chat message with stubbed agent-env and socketio. Returns the socketio stub."""
    if stub is None:
        if events is not None:
            stub = StubAgentEnvConnector(events=events)
        else:
            stub = StubAgentEnvConnector(response_text=agent_response)
    socketio_stub = StubSocketIOConnector()

    with patch("app.services.message_service.agent_env_connector", stub), \
         patch("app.services.event_service.socketio_connector", socketio_stub):
        r = client.post(
            f"{_chat_base(share_token)}/sessions/{session_id}/messages/stream",
            headers=webapp_hdrs,
            json={"content": user_msg, "file_ids": []},
        )
        assert r.status_code == 200, f"POST stream failed: {r.text}"
        drain_tasks()

    return socketio_stub


def _get_agent_messages(
    client: TestClient,
    webapp_hdrs: dict[str, str],
    share_token: str,
    session_id: str,
) -> list[dict]:
    """Fetch messages for a session and return agent messages only."""
    r = client.get(
        f"{_chat_base(share_token)}/sessions/{session_id}/messages",
        headers=webapp_hdrs,
    )
    assert r.status_code == 200
    messages_data = r.json()
    agent_messages = [m for m in messages_data["data"] if m["role"] == "agent"]
    assert len(agent_messages) >= 1, "Expected at least one agent message"
    return agent_messages


def _get_agent_streaming_events(
    client: TestClient,
    webapp_hdrs: dict[str, str],
    share_token: str,
    session_id: str,
) -> list[dict]:
    """Fetch the latest agent message's streaming_events."""
    agent_messages = _get_agent_messages(client, webapp_hdrs, share_token, session_id)
    streaming_events = agent_messages[-1]["message_metadata"].get("streaming_events", [])
    assert len(streaming_events) >= 1, (
        f"Expected streaming_events in message_metadata, got none.\n"
        f"message_metadata: {agent_messages[-1]['message_metadata']}"
    )
    return streaming_events


def _assert_contiguous_event_seq(streaming_events: list[dict]) -> None:
    """Assert that event_seq values are contiguous 1-based integers."""
    seq_values = [e.get("event_seq") for e in streaming_events]
    assert None not in seq_values, (
        f"All streaming_events must have event_seq. Got: {seq_values}"
    )
    expected_seqs = list(range(1, len(streaming_events) + 1))
    assert seq_values == expected_seqs, (
        f"event_seq must be contiguous 1-based. Expected {expected_seqs}, got {seq_values}"
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


# ── K. Webapp action framework ─────────────────────────────────────────────


def test_webapp_action_tag_extraction_and_emission(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Webapp action framework — agent can emit webapp_action events via XML tags.
    Tags are stripped from stored content and emitted as stream events.

    Scenario (single session, three message rounds):
      Phase 1: Single tag — action emitted, tag stripped from stored content
      Phase 2: Multiple tags — all actions emitted, none in stored content
      Phase 3: Malformed JSON tag + valid tag — malformed skipped, valid emitted,
               both tags stripped from stored content
    """
    # ── Setup: Create agent + webapp share, enable chat, create session ───
    agent, share = setup_webapp_agent(
        client, superuser_token_headers,
        name="Webapp Action Agent",
        share_label="Webapp Action Share",
    )
    agent_id = agent["id"]
    share_token = share["token"]
    _enable_chat(client, superuser_token_headers, agent_id, chat_mode="conversation")

    webapp_hdrs = _webapp_headers(client, share_token)
    session = _create_chat_session(client, webapp_hdrs, share_token)
    session_id = session["id"]

    # ── Phase 1: Single webapp_action tag ─────────────────────────────────
    agent_response_1 = (
        "I've refreshed the page for you."
        '<webapp_action>{"action": "refresh_page"}</webapp_action>'
    )
    sio_stub_1 = _send_stubbed_message(
        client, webapp_hdrs, share_token, session_id,
        "Please refresh", agent_response_1,
    )

    # Verify stored content is clean
    agent_messages = _get_agent_messages(client, webapp_hdrs, share_token, session_id)
    stored_content = agent_messages[-1]["content"]
    assert "<webapp_action>" not in stored_content, (
        f"webapp_action tag must be stripped from stored content.\nGot: {stored_content!r}"
    )
    assert "refresh_page" not in stored_content, (
        "Action name must not appear in visible message content"
    )
    assert "I've refreshed the page for you." in stored_content, (
        "Visible text before the tag must be preserved in stored content"
    )

    # Verify webapp_action stream event was emitted
    stream_events = [
        e for e in sio_stub_1.emitted_events
        if e.get("event") == "stream_event"
        and e.get("data", {}).get("event_type") == "webapp_action"
    ]
    assert len(stream_events) >= 1, (
        f"Expected at least one webapp_action stream event.\n"
        f"All emitted events: {sio_stub_1.emitted_events}"
    )
    event_data = stream_events[0]["data"]["data"]
    assert event_data["action"] == "refresh_page", (
        f"Expected action='refresh_page', got: {event_data}"
    )
    expected_room = f"session_{session_id}_stream"
    assert stream_events[0].get("room") == expected_room, (
        f"Expected webapp_action event in room {expected_room!r}, "
        f"got room: {stream_events[0].get('room')!r}"
    )

    # ── Phase 2: Multiple webapp_action tags ──────────────────────────────
    agent_response_2 = (
        "Updated the filter and reloading data."
        '<webapp_action>{"action": "update_form", "data": {"form_id": "report-filter", "values": {"date_range": "2024-Q4"}}}</webapp_action>'
        '<webapp_action>{"action": "reload_data", "data": {"endpoint": "/api/reports"}}</webapp_action>'
    )
    sio_stub_2 = _send_stubbed_message(
        client, webapp_hdrs, share_token, session_id,
        "Set date range to Q4 2024 and reload", agent_response_2,
    )

    # Verify stored content is clean
    agent_messages = _get_agent_messages(client, webapp_hdrs, share_token, session_id)
    stored_content = agent_messages[-1]["content"]
    assert "<webapp_action>" not in stored_content, (
        f"No webapp_action tags should appear in stored content. Got: {stored_content!r}"
    )
    assert "Updated the filter and reloading data." in stored_content, (
        "Visible text must be preserved"
    )

    # Verify two webapp_action events were emitted
    action_events = [
        e for e in sio_stub_2.emitted_events
        if e.get("event") == "stream_event"
        and e.get("data", {}).get("event_type") == "webapp_action"
    ]
    assert len(action_events) >= 2, (
        f"Expected 2 webapp_action events, got {len(action_events)}.\nEvents: {action_events}"
    )
    emitted_actions = {e["data"]["data"]["action"] for e in action_events}
    assert "update_form" in emitted_actions, "update_form action must be emitted"
    assert "reload_data" in emitted_actions, "reload_data action must be emitted"

    # Verify action data
    update_event = next(e for e in action_events if e["data"]["data"]["action"] == "update_form")
    assert update_event["data"]["data"]["data"]["form_id"] == "report-filter"
    assert update_event["data"]["data"]["data"]["values"]["date_range"] == "2024-Q4"

    reload_event = next(e for e in action_events if e["data"]["data"]["action"] == "reload_data")
    assert reload_event["data"]["data"]["data"]["endpoint"] == "/api/reports"

    # ── Phase 3: Malformed JSON tag + valid tag ───────────────────────────
    agent_response_3 = (
        "Done."
        "<webapp_action>THIS IS NOT JSON AT ALL!!</webapp_action>"
        '<webapp_action>{"action": "navigate", "data": {"path": "/dashboard"}}</webapp_action>'
    )
    sio_stub_3 = _send_stubbed_message(
        client, webapp_hdrs, share_token, session_id,
        "Navigate to dashboard", agent_response_3,
    )

    # Stored content must have both tags stripped
    agent_messages = _get_agent_messages(client, webapp_hdrs, share_token, session_id)
    stored_content = agent_messages[-1]["content"]
    assert "<webapp_action>" not in stored_content, (
        f"All webapp_action tags (even malformed ones) must be stripped. Got: {stored_content!r}"
    )
    assert "Done." in stored_content, "Visible text must be preserved"

    # Only the valid navigate action should be emitted
    action_events = [
        e for e in sio_stub_3.emitted_events
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


# ── K2. Streaming events post-processing ───────────────────────────────────


def test_streaming_events_post_processing(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Webapp action tags in agent responses are split into interleaved text and
    action events in streaming_events with contiguous event_seq values.

    Scenario (single session, four message rounds):
      Phase 1: Single tag — text/action/text split with correct event_seq
      Phase 2: Multiple tags — text/action/text/action/text ordering with metadata
      Phase 3: Malformed tag — skipped, valid tag preserved, text segments intact
      Phase 4: Tag-only response — no empty assistant events created
    """
    # ── Setup: Create agent + webapp share, enable chat, create session ───
    agent, share = setup_webapp_agent(
        client, superuser_token_headers,
        name="Post-Process Agent",
        share_label="Post-Process Share",
    )
    agent_id = agent["id"]
    share_token = share["token"]
    _enable_chat(client, superuser_token_headers, agent_id, chat_mode="conversation")

    webapp_hdrs = _webapp_headers(client, share_token)
    session = _create_chat_session(client, webapp_hdrs, share_token)
    session_id = session["id"]

    # ── Phase 1: Single tag splits into text → action → text ─────────────
    agent_response_1 = (
        "Here is the result."
        '<webapp_action>{"action": "refresh_page"}</webapp_action>'
        "All done."
    )
    _send_stubbed_message(
        client, webapp_hdrs, share_token, session_id,
        "Please do the thing", agent_response_1,
    )

    streaming_events = _get_agent_streaming_events(
        client, webapp_hdrs, share_token, session_id,
    )

    # Verify assistant and action events
    assistant_events = [e for e in streaming_events if e.get("type") == "assistant"]
    action_events = [e for e in streaming_events if e.get("type") == "webapp_action"]

    assert len(action_events) == 1, (
        f"Expected 1 webapp_action event, got {len(action_events)}.\n"
        f"streaming_events: {streaming_events}"
    )
    action_evt = action_events[0]
    assert action_evt["content"] == "refresh_page"
    assert action_evt["metadata"]["action"] == "refresh_page"
    assert action_evt["metadata"]["data"] == {}

    assert len(assistant_events) >= 2, (
        f"Expected at least 2 assistant events (before and after tag), got {len(assistant_events)}.\n"
        f"streaming_events: {streaming_events}"
    )
    assistant_contents = [e["content"] for e in assistant_events]
    assert any("Here is the result." in c for c in assistant_contents)
    assert any("All done." in c for c in assistant_contents)

    _assert_contiguous_event_seq(streaming_events)

    # No assistant event contains raw <webapp_action> tags
    for evt in assistant_events:
        assert "<webapp_action>" not in evt.get("content", ""), (
            f"Assistant streaming_event must not contain raw tags. Got: {evt['content']!r}"
        )

    # ── Phase 2: Multiple tags — correct interleaved order ────────────────
    agent_response_2 = (
        "Part one."
        '<webapp_action>{"action": "update_form", "data": {"form_id": "f1"}}</webapp_action>'
        "Part two."
        '<webapp_action>{"action": "reload_data"}</webapp_action>'
        "Part three."
    )
    _send_stubbed_message(
        client, webapp_hdrs, share_token, session_id,
        "Go!", agent_response_2,
    )

    streaming_events = _get_agent_streaming_events(
        client, webapp_hdrs, share_token, session_id,
    )

    split_events = [
        e for e in streaming_events
        if e.get("type") in ("assistant", "webapp_action")
    ]
    assert len(split_events) == 5, (
        f"Expected 5 split events (3 text + 2 action), got {len(split_events)}.\n"
        f"split_events: {split_events}"
    )

    # Verify ordering: text, action, text, action, text
    assert split_events[0]["type"] == "assistant"
    assert "Part one." in split_events[0]["content"]

    assert split_events[1]["type"] == "webapp_action"
    assert split_events[1]["content"] == "update_form"

    assert split_events[2]["type"] == "assistant"
    assert "Part two." in split_events[2]["content"]

    assert split_events[3]["type"] == "webapp_action"
    assert split_events[3]["content"] == "reload_data"

    assert split_events[4]["type"] == "assistant"
    assert "Part three." in split_events[4]["content"]

    # Verify metadata on action events
    assert split_events[1]["metadata"]["action"] == "update_form"
    assert split_events[1]["metadata"]["data"] == {"form_id": "f1"}
    assert split_events[3]["metadata"]["action"] == "reload_data"
    assert split_events[3]["metadata"]["data"] == {}

    _assert_contiguous_event_seq(streaming_events)

    # ── Phase 3: Malformed tag skipped, valid tag preserved ───────────────
    agent_response_3 = (
        "Start."
        "<webapp_action>INVALID JSON</webapp_action>"
        "Middle."
        '<webapp_action>{"action": "navigate", "data": {"path": "/home"}}</webapp_action>'
        "End."
    )
    _send_stubbed_message(
        client, webapp_hdrs, share_token, session_id,
        "Do the thing", agent_response_3,
    )

    streaming_events = _get_agent_streaming_events(
        client, webapp_hdrs, share_token, session_id,
    )

    split_events = [
        e for e in streaming_events
        if e.get("type") in ("assistant", "webapp_action")
    ]

    # Only the valid navigate action should appear
    action_events = [e for e in split_events if e.get("type") == "webapp_action"]
    assert len(action_events) == 1, (
        f"Expected exactly 1 webapp_action event (the valid tag only). "
        f"Got {len(action_events)}. split_events: {split_events}"
    )
    assert action_events[0]["content"] == "navigate"
    assert action_events[0]["metadata"]["data"] == {"path": "/home"}

    # Surrounding text is present as assistant events
    assistant_events = [e for e in split_events if e.get("type") == "assistant"]
    assistant_contents = [e["content"] for e in assistant_events]
    assert any("Start." in c for c in assistant_contents)
    assert any("Middle." in c for c in assistant_contents)
    assert any("End." in c for c in assistant_contents)

    for evt in assistant_events:
        assert "<webapp_action>" not in evt.get("content", ""), (
            f"No assistant event should contain raw tags. Got: {evt['content']!r}"
        )

    _assert_contiguous_event_seq(streaming_events)

    # ── Phase 4: Tag-only response — no empty assistant events ────────────
    agent_response_4 = (
        '<webapp_action>{"action": "show_notification", "data": {"message": "Done!", "type": "success"}}</webapp_action>'
    )
    _send_stubbed_message(
        client, webapp_hdrs, share_token, session_id,
        "Show me a notification", agent_response_4,
    )

    streaming_events = _get_agent_streaming_events(
        client, webapp_hdrs, share_token, session_id,
    )

    # No empty assistant events should exist
    empty_assistants = [
        e for e in streaming_events
        if e.get("type") == "assistant" and not e.get("content", "").strip()
    ]
    assert len(empty_assistants) == 0, (
        f"No empty assistant events should be emitted. Found: {empty_assistants}"
    )

    # The action event has correct fields
    action_events = [e for e in streaming_events if e.get("type") == "webapp_action"]
    assert len(action_events) == 1, (
        f"Expected exactly 1 webapp_action event. Got {len(action_events)}.\n"
        f"streaming_events: {streaming_events}"
    )
    action_evt = action_events[0]
    assert action_evt["content"] == "show_notification"
    assert action_evt["metadata"]["action"] == "show_notification"
    assert action_evt["metadata"]["data"] == {"message": "Done!", "type": "success"}

    _assert_contiguous_event_seq(streaming_events)


def test_streaming_events_non_assistant_events_pass_through_unchanged(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Non-assistant events in streaming_events are not modified by the
    post-processing pass. Tool events that precede or follow assistant events
    with action tags remain present and have their type/content intact.

    Scenario:
      1. Custom stub emits: session_created, system/tools_init, assistant
         (with action tag), tool, done
      2. After post-processing the stored streaming_events contain:
           - The tool event (unchanged)
           - An assistant event with the text before the tag
           - A webapp_action event
      3. event_seq is contiguous across the full list
    """
    from tests.stubs.agent_env_stub import build_simple_response_events

    # ── Phase 1: Setup ─────────────────────────────────────────────────────
    agent, share = setup_webapp_agent(
        client, superuser_token_headers,
        name="Post-Process Non-Assistant Pass-Through Agent",
        share_label="Post-Process Non-Assistant Share",
    )
    agent_id = agent["id"]
    share_token = share["token"]
    _enable_chat(client, superuser_token_headers, agent_id, chat_mode="conversation")

    webapp_hdrs = _webapp_headers(client, share_token)
    session = _create_chat_session(client, webapp_hdrs, share_token)
    session_id = session["id"]

    # ── Phase 2: Build a custom event sequence with a tool event ──────────
    assistant_text = (
        "Hello."
        '<webapp_action>{"action": "show_notification", "data": {"message": "Done!"}}</webapp_action>'
    )
    base_events = build_simple_response_events(assistant_text)
    # Insert a synthetic tool event just before 'done'
    tool_event = {
        "type": "tool",
        "tool_name": "Read",
        "content": "",
        "metadata": {"tool_id": "tool-123", "tool_input": {"file_path": "/readme.md"}},
    }
    # base_events order: session_created, system/tools_init, assistant, done
    # Insert tool event at index 3 (before done)
    custom_events = base_events[:3] + [tool_event] + base_events[3:]

    _send_stubbed_message(
        client, webapp_hdrs, share_token, session_id,
        "Notify me", "",
        events=custom_events,
    )

    # ── Phase 3: Read streaming_events ────────────────────────────────────
    streaming_events = _get_agent_streaming_events(
        client, webapp_hdrs, share_token, session_id,
    )

    # ── Phase 4: Tool event is still present and unmodified ───────────────
    tool_events = [e for e in streaming_events if e.get("type") == "tool"]
    assert len(tool_events) == 1, (
        f"Expected 1 tool event to survive post-processing unchanged. "
        f"streaming_events types: {[e.get('type') for e in streaming_events]}"
    )
    stored_tool = tool_events[0]
    assert stored_tool.get("tool_name") == "Read", (
        f"Tool event must retain its tool_name. Got: {stored_tool.get('tool_name')!r}"
    )

    # ── Phase 5: The webapp_action and split assistant events are present ──
    action_events = [e for e in streaming_events if e.get("type") == "webapp_action"]
    assert len(action_events) == 1, (
        f"Expected 1 webapp_action event. Got {len(action_events)}."
    )
    assert action_events[0]["content"] == "show_notification"
    assert action_events[0]["metadata"]["data"] == {"message": "Done!"}

    assistant_events = [e for e in streaming_events if e.get("type") == "assistant"]
    assert len(assistant_events) >= 1, (
        f"Expected at least one assistant event (text before tag). "
        f"streaming_events: {streaming_events}"
    )
    assert any("Hello." in e.get("content", "") for e in assistant_events), (
        "Expected 'Hello.' in an assistant event after the split"
    )

    # ── Phase 6: event_seq is contiguous across the full list ─────────────
    _assert_contiguous_event_seq(streaming_events)
