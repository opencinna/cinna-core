"""
MCP send_message tool handler integration tests.

Verifies that the MCP tool handler correctly:
  - Creates platform sessions with all required fields
  - Stores user messages and agent responses as SessionMessage records
  - Links sessions to MCP connectors
  - Preserves MCP transport session IDs (mcp_session_id)
  - Preserves external session IDs for multi-turn continuity
  - Reuses existing sessions for subsequent messages
  - Handles MCP session ID changes (client reconnection)
  - Uses context_id for per-chat session isolation

These tests call handle_send_message() directly (not through MCP protocol)
with the agent environment stubbed to return predefined responses.

Uses the same service pipeline as email and A2A integrations:
  - MessageService.create_message for user messages
  - MessageService.stream_message_with_events for agent streaming + storage
  - create_session() (patchable) for all DB access
"""
import asyncio
import json
from unittest.mock import patch

from fastapi.testclient import TestClient

from tests.stubs.agent_env_stub import StubAgentEnvConnector
from tests.utils.agent import create_agent_via_api, get_agent
from tests.utils.background_tasks import drain_tasks
from tests.utils.mcp import create_mcp_connector, update_mcp_connector
from tests.utils.message import list_messages, get_messages_by_role
from tests.utils.session import list_sessions


# ── Helpers ──────────────────────────────────────────────────────────────────


def _setup_agent_with_connector(
    client: TestClient,
    token_headers: dict[str, str],
    agent_name: str = "MCP Send Agent",
    connector_name: str = "Send Connector",
    mode: str = "conversation",
) -> tuple[dict, dict]:
    """Create agent + connector. Returns (agent, connector)."""
    agent = create_agent_via_api(client, token_headers, name=agent_name)
    drain_tasks()
    agent = get_agent(client, token_headers, agent["id"])
    connector = create_mcp_connector(
        client, token_headers, agent["id"],
        name=connector_name, mode=mode,
    )
    return agent, connector


def _find_mcp_sessions(
    client: TestClient,
    token_headers: dict[str, str],
    connector_id: str,
) -> list[dict]:
    """Find sessions linked to a specific MCP connector."""
    sessions = list_sessions(client, token_headers)
    return [s for s in sessions if s.get("mcp_connector_id") == connector_id]


def _run_send_message(
    connector_id: str,
    message: str,
    agent_env_stub: StubAgentEnvConnector,
    mcp_session_id: str | None = None,
    context_id: str = "",
) -> dict:
    """Call handle_send_message with the standard service pipeline.

    Patches agent_env_connector at the MessageService level (same as A2A tests)
    so that streaming goes through the full MessageService.stream_message_with_events
    pipeline, which stores both user and agent messages in the database.

    Args:
        connector_id: MCP connector UUID string
        message: User message to send
        agent_env_stub: Stub for the agent environment
        mcp_session_id: Optional MCP transport session ID (simulates the
            mcp-session-id header that Claude Desktop sends)
        context_id: Optional context_id for per-chat session isolation

    Returns:
        Parsed JSON dict with "response" and "context_id" (or "error" and "context_id")
    """
    from app.mcp.tools import handle_send_message
    from app.mcp.server import mcp_connector_id_var, mcp_session_id_var

    async def _run():
        token_conn = mcp_connector_id_var.set(connector_id)
        token_sess = mcp_session_id_var.set(mcp_session_id)
        try:
            return await handle_send_message(message, context_id=context_id)
        finally:
            mcp_connector_id_var.reset(token_conn)
            mcp_session_id_var.reset(token_sess)

    with patch("app.services.sessions.message_service.agent_env_connector", agent_env_stub):
        result = asyncio.run(_run())
    drain_tasks()

    # Parse JSON response; fall back to raw string wrapped in error dict
    try:
        return json.loads(result)
    except (json.JSONDecodeError, TypeError):
        return {"response": result, "context_id": ""}


# ── Tests ────────────────────────────────────────────────────────────────────


def test_send_message_creates_session_with_correct_fields(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db,
) -> None:
    """
    First send_message call creates a platform session with correct fields:
      1. Create agent + connector
      2. Call send_message tool with an MCP session ID
      3. Verify session created via API
      4. Check: mcp_connector_id, mcp_session_id, integration_type, mode, status
    """
    agent, connector = _setup_agent_with_connector(
        client, superuser_token_headers,
        agent_name="Session Fields Agent",
        connector_name="Fields Connector",
        mode="conversation",
    )
    connector_id = connector["id"]

    stub = StubAgentEnvConnector(response_text="Hello from the agent!")
    test_mcp_session_id = "abc123-mcp-session-from-claude-desktop"
    result = _run_send_message(
        connector_id, "Hi there", stub,
        mcp_session_id=test_mcp_session_id,
    )

    # ── Verify tool returned the agent response ──────────────────────────
    assert "Hello from the agent!" in result["response"]
    assert result["context_id"], "context_id should be non-empty"

    # ── Verify session created via API ───────────────────────────────────
    mcp_sessions = _find_mcp_sessions(client, superuser_token_headers, connector_id)
    assert len(mcp_sessions) == 1, f"Expected 1 MCP session, got {len(mcp_sessions)}"

    session = mcp_sessions[0]
    assert session["mcp_connector_id"] == connector_id
    assert session["integration_type"] == "mcp"
    assert session["mode"] == "conversation"
    assert session["status"] == "active"
    assert session["agent_id"] == agent["id"]
    assert session["user_id"] is not None
    # MCP transport session ID should be stored
    assert session["mcp_session_id"] == test_mcp_session_id, (
        f"mcp_session_id not stored: got {session.get('mcp_session_id')!r}"
    )


def test_send_message_stores_user_and_agent_messages(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db,
) -> None:
    """
    send_message should store both the user message and the agent response
    as SessionMessage records (same as email/web-UI integrations):
      1. Create agent + connector
      2. Call send_message tool with a user message
      3. Verify user message stored (role="user")
      4. Verify agent response stored (role="agent")
    """
    agent, connector = _setup_agent_with_connector(
        client, superuser_token_headers,
        agent_name="Message Storage Agent",
    )
    connector_id = connector["id"]

    stub = StubAgentEnvConnector(response_text="I can help with that!")
    result = _run_send_message(connector_id, "Please help me", stub)
    assert "I can help with that!" in result["response"]

    # ── Find the session ─────────────────────────────────────────────────
    mcp_sessions = _find_mcp_sessions(client, superuser_token_headers, connector_id)
    assert len(mcp_sessions) == 1
    session_id = mcp_sessions[0]["id"]

    # ── Verify messages stored ───────────────────────────────────────────
    messages = list_messages(client, superuser_token_headers, session_id)
    assert len(messages) >= 2, (
        f"Expected at least 2 messages (user + agent), got {len(messages)}: {messages}"
    )

    user_msgs = get_messages_by_role(client, superuser_token_headers, session_id, "user")
    assert len(user_msgs) >= 1, "No user message stored"
    assert user_msgs[0]["content"] == "Please help me"

    agent_msgs = get_messages_by_role(client, superuser_token_headers, session_id, "agent")
    assert len(agent_msgs) >= 1, "No agent message stored"
    assert "I can help with that!" in agent_msgs[0]["content"]


def test_send_message_reuses_existing_session(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db,
) -> None:
    """
    Subsequent send_message calls reuse the existing session via context_id:
      1. Call send_message → get context_id in response
      2. Call send_message again with that context_id
      3. Verify only one session exists with both message exchanges
    """
    agent, connector = _setup_agent_with_connector(
        client, superuser_token_headers,
        agent_name="Session Reuse Agent",
    )
    connector_id = connector["id"]

    # First message — no context_id
    stub1 = StubAgentEnvConnector(response_text="First response")
    result1 = _run_send_message(connector_id, "First message", stub1)
    ctx_id = result1["context_id"]

    # Second message — pass back context_id
    stub2 = StubAgentEnvConnector(response_text="Second response")
    result2 = _run_send_message(connector_id, "Second message", stub2, context_id=ctx_id)
    assert result2["context_id"] == ctx_id

    # ── Verify single session ────────────────────────────────────────────
    mcp_sessions = _find_mcp_sessions(client, superuser_token_headers, connector_id)
    assert len(mcp_sessions) == 1, (
        f"Expected 1 session (reused), got {len(mcp_sessions)}"
    )

    session_id = mcp_sessions[0]["id"]

    # ── Verify all messages in same session ──────────────────────────────
    messages = list_messages(client, superuser_token_headers, session_id)
    user_msgs = [m for m in messages if m["role"] == "user"]
    agent_msgs = [m for m in messages if m["role"] == "agent"]
    assert len(user_msgs) >= 2, f"Expected 2 user messages, got {len(user_msgs)}"
    assert len(agent_msgs) >= 2, f"Expected 2 agent messages, got {len(agent_msgs)}"


def test_send_message_reuses_session_after_stream_completed(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db,
) -> None:
    """
    Reproduce the production bug: STREAM_COMPLETED event sets session status
    to "completed", causing the next send_message to create a new session
    instead of reusing the existing one.

    Simulates what handle_stream_completed does in production:
      1. Send first message → session created with status "active"
      2. Simulate STREAM_COMPLETED: set session status to "completed"
         (this is what the event handler does for non-integration sessions)
      3. Call handle_stream_completed with integration_type check
      4. Verify MCP session stays "active"
      5. Send second message with context_id → should reuse the same session
    """
    from uuid import UUID
    from app.models import Session

    agent, connector = _setup_agent_with_connector(
        client, superuser_token_headers,
        agent_name="Stream Completed Agent",
    )
    connector_id = connector["id"]

    # ── Phase 1: First message ───────────────────────────────────────────
    stub1 = StubAgentEnvConnector(response_text="First response")
    result1 = _run_send_message(connector_id, "Hello", stub1)
    ctx_id = result1["context_id"]

    mcp_sessions = _find_mcp_sessions(client, superuser_token_headers, connector_id)
    assert len(mcp_sessions) == 1
    session_id = mcp_sessions[0]["id"]
    assert mcp_sessions[0]["status"] == "active"

    # ── Phase 2: Simulate what STREAM_COMPLETED does in production ───────
    # In production, handle_stream_completed sets status = "completed" for
    # non-integration sessions. With our fix, MCP sessions should stay
    # "active". We test the fix by calling handle_stream_completed directly.
    from app.services.sessions.session_service import SessionService
    event_data = {
        "meta": {
            "session_id": session_id,
            "was_interrupted": False,
        }
    }
    # Patch create_session() to use the test DB session
    with patch("app.services.sessions.session_service.create_session", return_value=db):
        with patch.object(db, "close", lambda: None):
            asyncio.run(SessionService.handle_stream_completed(event_data))
    drain_tasks()

    # ── Phase 3: Verify MCP session stayed "active" ──────────────────────
    mcp_sessions = _find_mcp_sessions(client, superuser_token_headers, connector_id)
    assert len(mcp_sessions) == 1
    assert mcp_sessions[0]["status"] == "active", (
        f"MCP session should stay 'active' after STREAM_COMPLETED, "
        f"got '{mcp_sessions[0]['status']}'"
    )

    # ── Phase 4: Second message with context_id should reuse session ─────
    stub2 = StubAgentEnvConnector(response_text="Second response")
    result2 = _run_send_message(connector_id, "Still here", stub2, context_id=ctx_id)
    assert result2["context_id"] == ctx_id

    mcp_sessions = _find_mcp_sessions(client, superuser_token_headers, connector_id)
    assert len(mcp_sessions) == 1, (
        f"Expected 1 session (reused after STREAM_COMPLETED), got {len(mcp_sessions)}"
    )
    messages = list_messages(client, superuser_token_headers, session_id)
    user_msgs = [m for m in messages if m["role"] == "user"]
    assert len(user_msgs) >= 2, f"Expected 2 user messages in same session, got {len(user_msgs)}"


def test_send_message_same_mcp_session_id_does_not_reuse(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db,
) -> None:
    """
    mcp_session_id alone does NOT cause session reuse.

    Claude Desktop reuses the same mcp_session_id across all chats, so if
    mcp_session_id drove session lookup, all chats would land in the same
    platform session. Only context_id should drive reuse.

      1. Send first message with mcp_session_id "shared-transport"
      2. Send second message with the SAME mcp_session_id but NO context_id
      3. Verify 2 separate platform sessions exist (no reuse)
      4. Verify mcp_session_id is still stored as metadata on both
    """
    agent, connector = _setup_agent_with_connector(
        client, superuser_token_headers,
        agent_name="No MCP Reuse Agent",
    )
    connector_id = connector["id"]
    shared_mcp_sid = "shared-transport-session"

    # First message (chat 1)
    stub1 = StubAgentEnvConnector(response_text="First response")
    result1 = _run_send_message(
        connector_id, "Hello from chat 1", stub1,
        mcp_session_id=shared_mcp_sid, context_id="",
    )

    # Second message (chat 2) — same mcp_session_id, no context_id
    stub2 = StubAgentEnvConnector(response_text="Second response")
    result2 = _run_send_message(
        connector_id, "Hello from chat 2", stub2,
        mcp_session_id=shared_mcp_sid, context_id="",
    )

    # ── Must be 2 separate sessions (no reuse via mcp_session_id) ────────
    assert result1["context_id"] != result2["context_id"], (
        "Same mcp_session_id without context_id should create separate sessions"
    )

    mcp_sessions = _find_mcp_sessions(client, superuser_token_headers, connector_id)
    assert len(mcp_sessions) == 2, (
        f"Expected 2 sessions (mcp_session_id must NOT drive reuse), got {len(mcp_sessions)}"
    )

    # mcp_session_id should still be stored as metadata on both
    for s in mcp_sessions:
        assert s["mcp_session_id"] == shared_mcp_sid


def test_send_message_without_mcp_session_id(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db,
) -> None:
    """
    When no MCP session ID is provided, each message creates a new
    platform session — there is no connector-based fallback.
    """
    agent, connector = _setup_agent_with_connector(
        client, superuser_token_headers,
        agent_name="No Session ID Agent",
    )
    connector_id = connector["id"]

    # First message without MCP session ID
    stub1 = StubAgentEnvConnector(response_text="First")
    _run_send_message(connector_id, "Hello", stub1, mcp_session_id=None)

    # Second message also without MCP session ID
    stub2 = StubAgentEnvConnector(response_text="Second")
    _run_send_message(connector_id, "Again", stub2, mcp_session_id=None)

    mcp_sessions = _find_mcp_sessions(client, superuser_token_headers, connector_id)
    assert len(mcp_sessions) == 2, (
        f"Expected 2 sessions without mcp_session_id, got {len(mcp_sessions)}"
    )
    # mcp_session_id should be null on both since none was provided
    for s in mcp_sessions:
        assert s["mcp_session_id"] is None


def test_send_message_preserves_external_session_id(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db,
) -> None:
    """
    External session ID from agent environment is stored in session metadata:
      1. Call send_message with stub that returns session_created event
      2. Verify external_session_id appears in session metadata
    """
    agent, connector = _setup_agent_with_connector(
        client, superuser_token_headers,
        agent_name="External Session Agent",
    )
    connector_id = connector["id"]

    stub = StubAgentEnvConnector(response_text="Hello!")
    _run_send_message(connector_id, "Hello", stub)

    # The stub's build_simple_response_events includes a session_created event
    # with a UUID as session_id. Verify it was stored.
    mcp_sessions = _find_mcp_sessions(client, superuser_token_headers, connector_id)
    assert len(mcp_sessions) == 1

    session = mcp_sessions[0]
    # external_session_id should be populated from the session_created event
    assert session.get("external_session_id") is not None, (
        "external_session_id not set — session_created event from agent env not captured"
    )


def test_send_message_inactive_connector_rejected(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db,
) -> None:
    """send_message on an inactive connector returns error."""
    agent, connector = _setup_agent_with_connector(
        client, superuser_token_headers,
        agent_name="Inactive Send Agent",
    )
    connector_id = connector["id"]
    agent_id = agent["id"]

    # Deactivate
    update_mcp_connector(
        client, superuser_token_headers, agent_id, connector_id,
        is_active=False,
    )

    stub = StubAgentEnvConnector(response_text="Should not reach")
    result = _run_send_message(connector_id, "Hello", stub)

    # Result may be a JSON error or a raw error string
    raw = result.get("error", result.get("response", ""))
    assert "error" in raw.lower() or "inactive" in raw.lower() or "not found" in raw.lower()

    # No session should have been created
    mcp_sessions = _find_mcp_sessions(client, superuser_token_headers, connector_id)
    assert len(mcp_sessions) == 0


def test_send_message_agent_response_metadata(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db,
) -> None:
    """
    Agent response message should include streaming metadata:
      1. Call send_message
      2. Verify agent message has metadata with external_session_id and
         streaming_events
    """
    agent, connector = _setup_agent_with_connector(
        client, superuser_token_headers,
        agent_name="Metadata Agent",
    )
    connector_id = connector["id"]

    stub = StubAgentEnvConnector(response_text="Response with metadata")
    _run_send_message(connector_id, "Check metadata", stub)

    mcp_sessions = _find_mcp_sessions(client, superuser_token_headers, connector_id)
    assert len(mcp_sessions) == 1
    session_id = mcp_sessions[0]["id"]

    agent_msgs = get_messages_by_role(
        client, superuser_token_headers, session_id, "agent",
    )
    assert len(agent_msgs) >= 1

    meta = agent_msgs[0].get("message_metadata") or {}
    assert "external_session_id" in meta, (
        f"Expected external_session_id in metadata, got keys: {list(meta.keys())}"
    )
    assert "streaming_events" in meta, (
        f"Expected streaming_events in metadata, got keys: {list(meta.keys())}"
    )
    assert meta.get("streaming_in_progress") is False, (
        "streaming_in_progress should be False after stream completes"
    )


# ── context_id tests ─────────────────────────────────────────────────────────


def test_send_message_context_id_new_chat(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db,
) -> None:
    """
    Empty context_id creates a new session; response contains the session's
    context_id (which is the platform session UUID).
    """
    agent, connector = _setup_agent_with_connector(
        client, superuser_token_headers,
        agent_name="Context ID New Chat Agent",
    )
    connector_id = connector["id"]

    stub = StubAgentEnvConnector(response_text="Welcome!")
    result = _run_send_message(connector_id, "Hello", stub, context_id="")

    assert "Welcome!" in result["response"]
    assert result["context_id"], "context_id should be non-empty for new session"

    # context_id should match the session's UUID
    mcp_sessions = _find_mcp_sessions(client, superuser_token_headers, connector_id)
    assert len(mcp_sessions) == 1
    assert result["context_id"] == mcp_sessions[0]["id"]


def test_send_message_context_id_reuse(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db,
) -> None:
    """
    Passing back context_id from the first response reuses the same session.
    """
    agent, connector = _setup_agent_with_connector(
        client, superuser_token_headers,
        agent_name="Context ID Reuse Agent",
    )
    connector_id = connector["id"]

    # First message — no context_id
    stub1 = StubAgentEnvConnector(response_text="First reply")
    result1 = _run_send_message(connector_id, "Hello", stub1, context_id="")
    ctx_id = result1["context_id"]
    assert ctx_id, "First response should include context_id"

    # Second message — pass back context_id
    stub2 = StubAgentEnvConnector(response_text="Second reply")
    result2 = _run_send_message(connector_id, "Follow up", stub2, context_id=ctx_id)
    assert result2["context_id"] == ctx_id, "context_id should stay the same"

    # Only one session should exist
    mcp_sessions = _find_mcp_sessions(client, superuser_token_headers, connector_id)
    assert len(mcp_sessions) == 1, (
        f"Expected 1 session (reused via context_id), got {len(mcp_sessions)}"
    )

    # Verify both messages are in the same session
    messages = list_messages(client, superuser_token_headers, mcp_sessions[0]["id"])
    user_msgs = [m for m in messages if m["role"] == "user"]
    assert len(user_msgs) >= 2, f"Expected 2 user messages, got {len(user_msgs)}"


def test_send_message_context_id_different_chats(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db,
) -> None:
    """
    Two calls with empty context_id create two separate sessions
    (simulates two different Claude Desktop chats).
    """
    agent, connector = _setup_agent_with_connector(
        client, superuser_token_headers,
        agent_name="Context ID Diff Chats Agent",
    )
    connector_id = connector["id"]

    # Chat 1 — new session
    stub1 = StubAgentEnvConnector(response_text="Chat 1 reply")
    result1 = _run_send_message(connector_id, "Hello from chat 1", stub1, context_id="")

    # Chat 2 — another new session (empty context_id = new chat)
    stub2 = StubAgentEnvConnector(response_text="Chat 2 reply")
    result2 = _run_send_message(connector_id, "Hello from chat 2", stub2, context_id="")

    assert result1["context_id"] != result2["context_id"], (
        "Different chats should get different context_ids"
    )

    mcp_sessions = _find_mcp_sessions(client, superuser_token_headers, connector_id)
    assert len(mcp_sessions) == 2, (
        f"Expected 2 sessions for 2 different chats, got {len(mcp_sessions)}"
    )


def test_send_message_context_id_cross_connector_rejected(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db,
) -> None:
    """
    context_id from connector A is rejected when used with connector B.
    A new session is created instead.
    """
    agent, connector_a = _setup_agent_with_connector(
        client, superuser_token_headers,
        agent_name="Cross Connector Agent",
        connector_name="Connector A",
    )
    # Create a second connector on the same agent
    connector_b = create_mcp_connector(
        client, superuser_token_headers, agent["id"],
        name="Connector B", mode="conversation",
    )

    # Get context_id from connector A
    stub1 = StubAgentEnvConnector(response_text="Reply from A")
    result_a = _run_send_message(connector_a["id"], "Hello A", stub1, context_id="")
    ctx_id_a = result_a["context_id"]

    # Try to use connector A's context_id with connector B
    stub2 = StubAgentEnvConnector(response_text="Reply from B")
    result_b = _run_send_message(connector_b["id"], "Hello B", stub2, context_id=ctx_id_a)

    # Should get a different context_id (new session created)
    assert result_b["context_id"] != ctx_id_a, (
        "context_id from connector A should not work on connector B"
    )

    # Connector A should have 1 session, connector B should have 1 session
    sessions_a = _find_mcp_sessions(client, superuser_token_headers, connector_a["id"])
    sessions_b = _find_mcp_sessions(client, superuser_token_headers, connector_b["id"])
    assert len(sessions_a) == 1
    assert len(sessions_b) == 1
    assert sessions_a[0]["id"] != sessions_b[0]["id"]


def test_send_message_invalid_context_id_creates_new_session(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db,
) -> None:
    """
    Garbage/invalid context_id gracefully creates a new session instead
    of returning an error.
    """
    agent, connector = _setup_agent_with_connector(
        client, superuser_token_headers,
        agent_name="Invalid Context Agent",
    )
    connector_id = connector["id"]

    stub = StubAgentEnvConnector(response_text="Hello!")
    result = _run_send_message(
        connector_id, "Hello", stub,
        context_id="not-a-valid-uuid-garbage",
    )

    assert "Hello!" in result["response"]
    assert result["context_id"], "Should get a valid context_id for the new session"

    mcp_sessions = _find_mcp_sessions(client, superuser_token_headers, connector_id)
    assert len(mcp_sessions) == 1
