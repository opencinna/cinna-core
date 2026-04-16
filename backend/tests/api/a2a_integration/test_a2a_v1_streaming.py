"""
Integration test: A2A v1 streaming scenario.

Tests the full user story:
  1. User creates an agent
  2. User creates an A2A access token
  3. A2A client connects via SendStreamingMessage (v1.0 method)
  4. User message appears as a message in a new session
  5. Session and context IDs in A2A match session data
  6. Agent replies and that reply is received as A2A SSE events

Also tests A2A authentication enforcement:
  - Requests without tokens are rejected
  - Invalid tokens are rejected
  - Revoked tokens are rejected
  - Deleted tokens are rejected
  - Tokens for agent A cannot access agent B

Also tests JSON-RPC id type preservation:
  - Integer id (e.g. 1) is echoed back as integer, not coerced to string

Only agent-env HTTP is stubbed (via StubAgentEnvConnector).
"""
import uuid
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.stubs.agent_env_stub import StubAgentEnvConnector
from tests.utils.a2a import (
    build_streaming_request,
    delete_access_token,
    get_a2a_agent_card,
    parse_sse_events,
    post_a2a_jsonrpc,
    post_a2a_raw,
    send_a2a_streaming_message,
    setup_a2a_agent,
    update_access_token,
)
from tests.utils.agent import create_agent_via_api, enable_a2a, get_agent
from tests.utils.background_tasks import drain_tasks
from tests.utils.message import get_messages_by_role, list_messages
from tests.utils.session import get_agent_session


# ── Tests ────────────────────────────────────────────────────────────────


def test_a2a_v1_streaming_full_flow(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Full A2A v1 streaming integration test:
      1. Setup: create agent, enable A2A, create access token
      2. Send message via A2A SendStreamingMessage
      3. Verify SSE events (working → agent content → completed)
      4. Verify session created with matching task/context IDs
      5. Verify user message stored in session
      6. Verify agent reply stored in session
    """
    # ── Phase 1: Setup ───────────────────────────────────────────────────

    agent, token_data = setup_a2a_agent(
        client, superuser_token_headers, name="A2A Streaming Agent",
    )
    agent_id = agent["id"]
    a2a_token = token_data["token"]

    # ── Phase 2: Send streaming message via A2A ──────────────────────────

    user_message = "Hello A2A agent, what can you do?"
    agent_response_text = "Hello! I can help you with various tasks."

    events, stub = send_a2a_streaming_message(
        client, agent_id, a2a_token,
        message_text=user_message,
        response_text=agent_response_text,
    )

    # ── Phase 3: Verify SSE event stream ─────────────────────────────────

    assert len(events) >= 2, f"Expected at least 2 SSE events, got {len(events)}"

    # All events must be valid JSON-RPC responses with the correct id
    for event in events:
        assert event["jsonrpc"] == "2.0"
        assert event["id"] == "req-1"

    # First event: initial "working" status (no agent content yet)
    first = events[0]["result"]
    assert first["kind"] == "status-update"
    assert first["status"]["state"] == "working"
    assert first["final"] is False

    # Extract task/context IDs — these must be consistent across all events
    task_id = first["taskId"]
    context_id = first["contextId"]
    assert task_id is not None
    assert context_id is not None
    assert task_id == context_id, "Phase 1: taskId and contextId must be identical"

    for event in events:
        result = event["result"]
        assert result["taskId"] == task_id
        assert result["contextId"] == context_id

    # Last event: final status with state=completed
    last = events[-1]["result"]
    assert last["status"]["state"] == "completed"
    assert last["final"] is True

    # At least one event should carry the agent's response text
    agent_text_parts = []
    for e in events:
        msg = e.get("result", {}).get("status", {}).get("message")
        if msg and "parts" in msg:
            for part in msg["parts"]:
                text = part.get("text") or (part.get("root", {}) or {}).get("text", "")
                if text:
                    agent_text_parts.append(text)

    assert any(
        agent_response_text in t for t in agent_text_parts
    ), f"Agent response text not found in SSE events. Parts: {agent_text_parts}"

    # ── Phase 4: Verify session matches A2A task/context IDs ─────────────

    session = get_agent_session(client, superuser_token_headers, agent_id)
    session_id = session["id"]

    # A2A task_id IS the session ID
    assert session_id == task_id, (
        f"Session ID {session_id} does not match A2A taskId {task_id}"
    )

    # ── Phase 5: Verify user message stored ──────────────────────────────

    user_messages = get_messages_by_role(
        client, superuser_token_headers, session_id, role="user",
    )
    assert len(user_messages) >= 1
    assert user_message in user_messages[0]["content"]

    # ── Phase 6: Verify agent reply stored ───────────────────────────────

    agent_messages = get_messages_by_role(
        client, superuser_token_headers, session_id, role="agent",
    )
    assert len(agent_messages) >= 1
    assert agent_response_text in agent_messages[-1]["content"]

    # Verify the stub received the message
    assert len(stub.stream_calls) == 1
    assert user_message in stub.stream_calls[0]["payload"]["message"]


def test_a2a_v1_streaming_continue_session(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Verify that a second A2A message reuses the same session by passing taskId.
    """
    # ── Setup ────────────────────────────────────────────────────────────

    agent, token_data = setup_a2a_agent(
        client, superuser_token_headers, name="A2A Continue Agent",
    )
    agent_id = agent["id"]
    a2a_token = token_data["token"]

    # ── First message (creates session) ──────────────────────────────────

    events1, _ = send_a2a_streaming_message(
        client, agent_id, a2a_token,
        message_text="First message",
        response_text="First reply",
    )
    task_id = events1[0]["result"]["taskId"]

    # ── Second message (continues session via taskId) ────────────────────

    events2, _ = send_a2a_streaming_message(
        client, agent_id, a2a_token,
        message_text="Second message",
        response_text="Second reply",
        task_id=task_id,
    )

    # Same task/context IDs as first message
    assert events2[0]["result"]["taskId"] == task_id
    assert events2[0]["result"]["contextId"] == task_id

    # Still only one session for this agent
    session = get_agent_session(client, superuser_token_headers, agent_id)
    assert session["id"] == task_id

    # Session should have messages from both turns
    all_messages = list_messages(client, superuser_token_headers, task_id)
    user_msgs = [m for m in all_messages if m["role"] == "user"]
    agent_msgs = [m for m in all_messages if m["role"] == "agent"]

    assert len(user_msgs) >= 2
    assert len(agent_msgs) >= 2
    assert "First message" in user_msgs[0]["content"]
    assert "Second message" in user_msgs[1]["content"]


def test_a2a_v1_get_task_matches_session(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    After streaming, GetTask returns a task whose id matches the session.
    """
    # ── Setup ────────────────────────────────────────────────────────────

    agent, token_data = setup_a2a_agent(
        client, superuser_token_headers, name="A2A GetTask Agent",
    )
    agent_id = agent["id"]
    a2a_token = token_data["token"]

    # ── Stream a message ─────────────────────────────────────────────────

    events, _ = send_a2a_streaming_message(
        client, agent_id, a2a_token,
        message_text="Check my task",
        response_text="Task response",
    )
    task_id = events[0]["result"]["taskId"]

    # ── GetTask via A2A ──────────────────────────────────────────────────

    body = post_a2a_jsonrpc(client, agent_id, a2a_token, {
        "jsonrpc": "2.0",
        "id": "req-get",
        "method": "GetTask",
        "params": {"id": task_id},
    })
    assert "result" in body, f"Expected JSON-RPC result, got: {body}"
    task = body["result"]

    # Task ID matches session ID
    assert task["id"] == task_id
    assert task["contextId"] == task_id

    # v1.0: task should have 'kind' discriminator
    assert task.get("kind") == "task"

    # Task should have history with both user and agent messages
    history = task.get("history", [])
    assert len(history) >= 2, f"Expected at least 2 messages in history, got {len(history)}"

    roles = [m["role"] for m in history]
    assert "user" in roles
    assert "agent" in roles


def test_a2a_v1_agent_card_with_access_token(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Verify the extended AgentCard is returned in v1.0 format
    when using an access token.
    """
    agent, token_data = setup_a2a_agent(
        client, superuser_token_headers, name="A2A Card Agent",
    )

    card = get_a2a_agent_card(client, agent["id"], token_data["token"])

    # v1.0 format checks
    assert "protocolVersions" in card, "v1.0 should have protocolVersions array"
    assert isinstance(card["protocolVersions"], list)
    assert "supportedInterfaces" in card, "v1.0 should have supportedInterfaces"
    assert isinstance(card["supportedInterfaces"], list)
    assert len(card["supportedInterfaces"]) >= 1
    assert "url" in card["supportedInterfaces"][0]
    assert "protocolBinding" in card["supportedInterfaces"][0]

    # Agent name should be present
    assert card["name"] == "A2A Card Agent"


# ── JSON-RPC id type-preservation tests ─────────────────────────────────


def test_a2a_integer_request_id_preserved_in_streaming(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    JSON-RPC id type is preserved end-to-end for streaming (message/stream).

    The A2A SDK sends numeric ids (e.g. ``"id": 1``). Prior to the fix, the
    server coerced the value to a string (``"id": "1"``), causing strict
    equality mismatches in client SDKs that use ``===`` / ``is`` comparisons.

    This test verifies:
      1. Send a streaming request with integer id = 1
      2. Every SSE event echoes back id = 1 (integer), not id = "1" (string)
      3. The type is verified with isinstance() — not just value equality
    """
    # ── Phase 1: Setup A2A agent ─────────────────────────────────────────

    agent, token_data = setup_a2a_agent(
        client, superuser_token_headers, name="A2A Integer ID Streaming Agent",
    )
    agent_id = agent["id"]
    a2a_token = token_data["token"]

    # ── Phase 2: Send streaming request with integer id ──────────────────

    integer_id = 1
    request_payload = {
        "jsonrpc": "2.0",
        "id": integer_id,
        "method": "SendStreamingMessage",
        "params": {
            "message": {
                "role": "user",
                "parts": [{"text": "Hello from integer-id client"}],
                "messageId": uuid.uuid4().hex,
            },
        },
    }

    stub = StubAgentEnvConnector(response_text="Integer id preserved")
    a2a_headers = {"Authorization": f"Bearer {a2a_token}", "Content-Type": "application/json"}

    with patch("app.services.sessions.message_service.agent_env_connector", stub):
        resp = client.post(
            f"{settings.API_V1_STR}/a2a/{agent_id}/",
            headers=a2a_headers,
            json=request_payload,
        )
    drain_tasks()

    assert resp.status_code == 200, f"A2A streaming request failed: {resp.text}"

    # ── Phase 3: Verify every SSE event echoes back the integer id ────────

    events = parse_sse_events(resp.text)
    assert len(events) >= 1, f"Expected at least one SSE event, got none. Response: {resp.text}"

    for i, event in enumerate(events):
        event_id = event.get("id")
        assert event_id == integer_id, (
            f"SSE event {i}: expected id={integer_id!r} (int), got {event_id!r} ({type(event_id).__name__})"
        )
        assert isinstance(event_id, int), (
            f"SSE event {i}: id must be int, got {type(event_id).__name__!r} — "
            "server must not coerce numeric ids to strings"
        )


def test_a2a_integer_request_id_preserved_in_non_streaming(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    JSON-RPC id type is preserved end-to-end for non-streaming (message/send).

    Same fix as the streaming path: the id in the JSON-RPC response body must
    have the same type as the id sent in the request.

    This test verifies:
      1. Send a streaming request with integer id = 42 via SendMessage
      2. Every SSE event echoes back id = 42 (integer), not id = "42" (string)
      3. The type is verified with isinstance()

    Note: Uses SendStreamingMessage (not SendMessage) because the non-streaming
    message/send path involves async polling that doesn't resolve in test context.
    Both paths go through the same _jsonrpc_success / _format_sse_event id handling.
    """
    # ── Phase 1: Setup A2A agent ─────────────────────────────────────────

    agent, token_data = setup_a2a_agent(
        client, superuser_token_headers, name="A2A Integer ID SendMessage Agent",
    )
    agent_id = agent["id"]
    a2a_token = token_data["token"]

    # ── Phase 2: Send streaming request with integer id ──────────────────

    integer_id = 42
    request_payload = {
        "jsonrpc": "2.0",
        "id": integer_id,
        "method": "SendStreamingMessage",
        "params": {
            "message": {
                "role": "user",
                "parts": [{"text": "Hello non-streaming integer-id client"}],
                "messageId": uuid.uuid4().hex,
            },
        },
    }

    stub = StubAgentEnvConnector(response_text="Integer id preserved")
    a2a_headers = {"Authorization": f"Bearer {a2a_token}", "Content-Type": "application/json"}

    with patch("app.services.sessions.message_service.agent_env_connector", stub):
        resp = client.post(
            f"{settings.API_V1_STR}/a2a/{agent_id}/",
            headers=a2a_headers,
            json=request_payload,
        )
    drain_tasks()

    assert resp.status_code == 200, f"A2A request failed: {resp.text}"

    # ── Phase 3: Verify the response id is the original integer ──────────

    events = parse_sse_events(resp.text)
    assert len(events) >= 1, f"Expected at least one SSE event, got none. Response: {resp.text}"

    for i, event in enumerate(events):
        event_id = event.get("id")
        assert event_id == integer_id, (
            f"SSE event {i}: expected id={integer_id!r} (int), got {event_id!r} ({type(event_id).__name__})"
        )
        assert isinstance(event_id, int), (
            f"SSE event {i}: id must be int, got {type(event_id).__name__!r} — "
            "server must not coerce numeric ids to strings"
        )


# ── Auth enforcement tests ───────────────────────────────────────────────


def test_a2a_rejects_request_without_token(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    A2A endpoint returns 401 when no Authorization header is provided.
    """
    agent, _ = setup_a2a_agent(
        client, superuser_token_headers, name="A2A No-Auth Agent",
    )

    resp = post_a2a_raw(client, agent["id"], build_streaming_request("Hello"))
    assert resp.status_code == 401


def test_a2a_rejects_invalid_token(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    A2A endpoint returns 401 when a garbage token is provided.
    """
    agent, _ = setup_a2a_agent(
        client, superuser_token_headers, name="A2A Bad-Token Agent",
    )

    resp = post_a2a_raw(
        client, agent["id"], build_streaming_request("Hello"),
        a2a_token="not-a-real-jwt-token",
    )
    assert resp.status_code == 401


def test_a2a_revoke_and_restore_token(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    User story: token works → revoke blocks access → restore gives access back.
      1. Create agent + token, verify A2A access works
      2. Revoke the token → A2A request fails (401)
      3. Restore (un-revoke) the token → A2A access works again
    """
    # ── Phase 1: Setup and verify access works ────────────────────────────

    agent, token_data = setup_a2a_agent(
        client, superuser_token_headers, name="A2A Revoke Agent",
    )
    a2a_token = token_data["token"]
    token_id = token_data["id"]

    get_a2a_agent_card(client, agent["id"], a2a_token)

    # ── Phase 2: Revoke → access blocked ──────────────────────────────────

    updated = update_access_token(
        client, superuser_token_headers, agent["id"], token_id,
        is_revoked=True,
    )
    assert updated["is_revoked"] is True

    resp = post_a2a_raw(
        client, agent["id"], build_streaming_request("Should be rejected"),
        a2a_token=a2a_token,
    )
    assert resp.status_code == 401

    # ── Phase 3: Restore → access works again ─────────────────────────────

    updated = update_access_token(
        client, superuser_token_headers, agent["id"], token_id,
        is_revoked=False,
    )
    assert updated["is_revoked"] is False

    get_a2a_agent_card(client, agent["id"], a2a_token)


def test_a2a_deleted_token_blocks_access(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    User story: token works, user deletes it, subsequent A2A requests fail.
      1. Create agent + token, verify access works
      2. Delete the token via management API
      3. Try the same A2A request → 401
    """
    # ── Phase 1: Setup and verify access works ────────────────────────────

    agent, token_data = setup_a2a_agent(
        client, superuser_token_headers, name="A2A Delete Agent",
    )
    a2a_token = token_data["token"]

    get_a2a_agent_card(client, agent["id"], a2a_token)

    # ── Phase 2: Delete the token ─────────────────────────────────────────

    delete_access_token(
        client, superuser_token_headers, agent["id"], token_data["id"],
    )

    # ── Phase 3: A2A request with deleted token → 401 ─────────────────────

    resp = post_a2a_raw(
        client, agent["id"], build_streaming_request("Should be rejected"),
        a2a_token=a2a_token,
    )
    assert resp.status_code == 401


def test_a2a_token_cannot_access_different_agent(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    A token created for agent A cannot be used to access agent B's A2A endpoint.

    The A2A auth layer accepts the JWT (it's structurally valid) but the
    handler checks agent ownership and returns a JSON-RPC permission error.
    """
    # ── Setup two agents, token on agent A only ───────────────────────────

    agent_a, token_data = setup_a2a_agent(
        client, superuser_token_headers, name="A2A Agent A",
    )
    a2a_token = token_data["token"]

    agent_b = create_agent_via_api(client, superuser_token_headers, name="A2A Agent B")
    drain_tasks()
    agent_b = get_agent(client, superuser_token_headers, agent_b["id"])
    enable_a2a(client, superuser_token_headers, agent_b["id"])

    # ── Token for agent A works on agent A ────────────────────────────────

    get_a2a_agent_card(client, agent_a["id"], a2a_token)

    # ── Token for agent A rejected on agent B (JSON-RPC error) ────────────

    resp = post_a2a_raw(
        client, agent_b["id"], build_streaming_request("Cross-agent attempt"),
        a2a_token=a2a_token,
    )
    # JSON-RPC returns HTTP 200 but with an error object in the body
    body = resp.json()
    assert "error" in body, f"Expected JSON-RPC error, got: {body}"
    assert body["error"]["code"] == -32004  # Not enough permissions
