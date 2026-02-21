"""
Integration test: A2A v1 streaming scenario.

Tests the full user story:
  1. User creates an agent
  2. User creates an A2A access token
  3. A2A client connects via SendStreamingMessage (v1.0 method)
  4. User message appears as a message in a new session
  5. Session and context IDs in A2A match session data
  6. Agent replies and that reply is received as A2A SSE events

Only agent-env HTTP is stubbed (via StubAgentEnvConnector).
"""
from fastapi.testclient import TestClient

from tests.utils.a2a import (
    get_a2a_agent_card,
    post_a2a_jsonrpc,
    send_a2a_streaming_message,
    setup_a2a_agent,
)
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

    agent, a2a_token = setup_a2a_agent(
        client, superuser_token_headers, name="A2A Streaming Agent",
    )
    agent_id = agent["id"]

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

    agent, a2a_token = setup_a2a_agent(
        client, superuser_token_headers, name="A2A Continue Agent",
    )
    agent_id = agent["id"]

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

    agent, a2a_token = setup_a2a_agent(
        client, superuser_token_headers, name="A2A GetTask Agent",
    )
    agent_id = agent["id"]

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
    agent, a2a_token = setup_a2a_agent(
        client, superuser_token_headers, name="A2A Card Agent",
    )

    card = get_a2a_agent_card(client, agent["id"], a2a_token)

    # v1.0 format checks
    assert "protocolVersions" in card, "v1.0 should have protocolVersions array"
    assert isinstance(card["protocolVersions"], list)
    assert "supportedInterfaces" in card, "v1.0 should have supportedInterfaces"
    assert isinstance(card["supportedInterfaces"], list)
    assert len(card["supportedInterfaces"]) >= 1
    assert "url" in card["supportedInterfaces"][0]
    assert "transport" in card["supportedInterfaces"][0]

    # Agent name should be present
    assert card["name"] == "A2A Card Agent"
