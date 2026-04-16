"""
App MCP session creation and context_id reuse tests.

Verifies that AppMCPRequestHandler.handle_send_message() correctly:
  - Creates a new session when no context_id is provided (routing required)
  - Sets session.user_id = agent.owner_id (agent owner sees the session)
  - Sets session.caller_id = caller's user_id (for audit/display)
  - Reuses an existing session when a valid context_id is passed back
  - Validates context_id against caller_id (not user_id) for app_mcp sessions
  - Creates a fresh session when an invalid/unknown context_id is given
  - Only reuses sessions with integration_type="app_mcp" (cross-isolation guard)
  - Returns the correct agent_name and context_id in the response payload
  - Returns caller_email in session list/get responses when caller_id is set

These tests call handle_send_message() directly (not through MCP protocol)
with the routing service and agent environment stubbed.

The routing service is mocked to return a fixed agent, so we do not need
a real AppAgentRoute configured — the handler's routing step is bypassed.
The agent environment is stubbed via StubAgentEnvConnector to avoid Docker.
"""
import asyncio
import json
import uuid
from unittest.mock import patch

from fastapi.testclient import TestClient

from tests.stubs.agent_env_stub import StubAgentEnvConnector
from tests.utils.agent import create_agent_via_api, get_agent
from tests.utils.background_tasks import drain_tasks
from tests.utils.session import list_sessions


# ── Helpers ──────────────────────────────────────────────────────────────────


def _setup_agent(
    client: TestClient,
    token_headers: dict[str, str],
    name: str = "App MCP Agent",
) -> dict:
    """Create an agent with an active environment. Returns agent dict."""
    agent = create_agent_via_api(client, token_headers, name=name)
    drain_tasks()
    return get_agent(client, token_headers, agent["id"])


def _find_app_mcp_sessions(
    client: TestClient,
    token_headers: dict[str, str],
) -> list[dict]:
    """Return all sessions with integration_type='app_mcp'."""
    sessions = list_sessions(client, token_headers)
    return [s for s in sessions if s.get("integration_type") == "app_mcp"]


def _run_handle_send_message(
    user_id: uuid.UUID,
    message: str,
    agent_id: uuid.UUID,
    agent_name: str,
    agent_env_stub: StubAgentEnvConnector,
    context_id: str | None = None,
) -> dict:
    """Call AppMCPRequestHandler.handle_send_message() with a mocked routing service.

    Patches:
      - AppMCPRoutingService.route_message — returns a RoutingResult pointing to agent_id
      - agent_env_connector (MessageService) — uses StubAgentEnvConnector

    Returns the parsed JSON response dict.
    """
    from app.services.app_mcp.app_mcp_request_handler import AppMCPRequestHandler
    from app.services.app_mcp.app_mcp_routing_service import RoutingResult

    fixed_routing_result = RoutingResult(
        agent_id=agent_id,
        agent_name=agent_name,
        session_mode="conversation",
        route_id=uuid.uuid4(),
        route_source="user",
        match_method="only_one",
    )

    async def _run():
        return await AppMCPRequestHandler.handle_send_message(
            user_id=user_id,
            message=message,
            context_id=context_id,
            mcp_ctx=None,
        )

    with patch(
        "app.services.app_mcp.app_mcp_request_handler.AppMCPRoutingService.route_message",
        return_value=fixed_routing_result,
    ):
        with patch(
            "app.services.sessions.message_service.agent_env_connector",
            agent_env_stub,
        ):
            raw = asyncio.run(_run())
    drain_tasks()

    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {"response": raw, "context_id": ""}


# ── Tests ────────────────────────────────────────────────────────────────────


def test_app_mcp_no_context_id_creates_new_session(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db,
) -> None:
    """
    handle_send_message() with no context_id creates a new app_mcp session:
      1. Create agent
      2. Call handle_send_message with context_id=None
      3. Verify response contains agent reply and non-empty context_id
      4. Verify one app_mcp session was created via API
      5. Verify session has integration_type="app_mcp"
      6. Verify context_id in response matches the new session UUID
    """
    from app.core.config import settings

    agent = _setup_agent(client, superuser_token_headers, name="No Context Agent")
    agent_id = uuid.UUID(agent["id"])

    # Resolve the superuser's user_id from the /users/me endpoint
    r = client.get(
        f"{settings.API_V1_STR}/users/me",
        headers=superuser_token_headers,
    )
    assert r.status_code == 200
    user_id = uuid.UUID(r.json()["id"])

    stub = StubAgentEnvConnector(response_text="Hello from App MCP!")
    result = _run_handle_send_message(
        user_id=user_id,
        message="Hello, route me to an agent",
        agent_id=agent_id,
        agent_name=agent["name"],
        agent_env_stub=stub,
        context_id=None,
    )

    # ── Verify response payload ──────────────────────────────────────────
    assert "error" not in result, f"Unexpected error: {result.get('error')}"
    assert "Hello from App MCP!" in result.get("response", "")
    assert result.get("context_id"), "context_id should be non-empty"
    assert result.get("agent_name") == agent["name"]

    # ── Verify session created via API ───────────────────────────────────
    app_mcp_sessions = _find_app_mcp_sessions(client, superuser_token_headers)
    assert len(app_mcp_sessions) == 1, (
        f"Expected 1 app_mcp session, got {len(app_mcp_sessions)}"
    )

    session = app_mcp_sessions[0]
    assert session["integration_type"] == "app_mcp"
    assert session["agent_id"] == agent["id"]
    # user_id is the agent owner (same as caller here since superuser owns the agent)
    assert session["user_id"] == str(user_id)
    # caller_id tracks who initiated via MCP
    assert session["caller_id"] == str(user_id)
    assert session["mode"] == "conversation"

    # ── Verify context_id matches session UUID ───────────────────────────
    assert result["context_id"] == session["id"], (
        f"context_id {result['context_id']!r} does not match session UUID {session['id']!r}"
    )


def test_app_mcp_context_id_reuses_existing_session(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db,
) -> None:
    """
    Passing back context_id from first response reuses the same session:
      1. Create agent
      2. First call — no context_id → new session, get context_id
      3. Second call — pass back context_id → same session reused
      4. Verify only one app_mcp session exists
      5. Verify both calls used the same session (two user messages)

    Both calls run within a single asyncio.run() so that the handler's
    create_session() calls share the test DB session state correctly.
    """
    from app.core.config import settings
    from app.services.app_mcp.app_mcp_request_handler import AppMCPRequestHandler
    from app.services.app_mcp.app_mcp_routing_service import RoutingResult
    from tests.utils.message import list_messages

    agent = _setup_agent(client, superuser_token_headers, name="Context Reuse Agent")
    agent_id = uuid.UUID(agent["id"])

    r = client.get(f"{settings.API_V1_STR}/users/me", headers=superuser_token_headers)
    user_id = uuid.UUID(r.json()["id"])

    fixed_routing_result = RoutingResult(
        agent_id=agent_id,
        agent_name=agent["name"],
        session_mode="conversation",
        route_id=uuid.uuid4(),
        route_source="user",
        match_method="only_one",
    )

    async def _run_both():
        # First call — no context_id → new session
        result1_raw = await AppMCPRequestHandler.handle_send_message(
            user_id=user_id,
            message="First message",
            context_id=None,
            mcp_ctx=None,
        )
        result1 = json.loads(result1_raw)

        # Second call — pass back context_id → reuse session
        result2_raw = await AppMCPRequestHandler.handle_send_message(
            user_id=user_id,
            message="Second message",
            context_id=result1.get("context_id"),
            mcp_ctx=None,
        )
        result2 = json.loads(result2_raw)
        return result1, result2

    stub = StubAgentEnvConnector(response_text="Response from agent")
    with patch(
        "app.services.app_mcp.app_mcp_request_handler.AppMCPRoutingService.route_message",
        return_value=fixed_routing_result,
    ):
        with patch(
            "app.services.sessions.message_service.agent_env_connector",
            stub,
        ):
            result1, result2 = asyncio.run(_run_both())
    drain_tasks()

    # ── Verify first call succeeded ─────────────────────────────────────
    assert "error" not in result1, f"First call failed: {result1.get('error')}"
    ctx_id = result1["context_id"]
    assert ctx_id, "First response should include context_id"

    # ── Verify second call reused same session ──────────────────────────
    assert "error" not in result2, f"Second call failed: {result2.get('error')}"
    assert result2["context_id"] == ctx_id, (
        f"context_id changed: got {result2['context_id']!r}, expected {ctx_id!r}"
    )

    # ── Only one session should exist ────────────────────────────────────
    app_mcp_sessions = _find_app_mcp_sessions(client, superuser_token_headers)
    assert len(app_mcp_sessions) == 1, (
        f"Expected 1 session (reused), got {len(app_mcp_sessions)}"
    )
    session_id = app_mcp_sessions[0]["id"]

    # ── Both messages are in the same session ────────────────────────────
    messages = list_messages(client, superuser_token_headers, session_id)
    user_msgs = [m for m in messages if m["role"] == "user"]
    assert len(user_msgs) >= 2, (
        f"Expected 2 user messages in same session, got {len(user_msgs)}"
    )
    user_contents = {m["content"] for m in user_msgs}
    assert "First message" in user_contents
    assert "Second message" in user_contents


def test_app_mcp_invalid_context_id_creates_new_session(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db,
) -> None:
    """
    An invalid/unknown context_id causes a new session to be created gracefully:
      1. Create agent
      2. Call handle_send_message with a garbage context_id
      3. Verify response contains a reply (no error)
      4. Verify one new app_mcp session exists with a different UUID than the garbage
    """
    from app.core.config import settings

    agent = _setup_agent(client, superuser_token_headers, name="Invalid Context Agent")
    agent_id = uuid.UUID(agent["id"])

    r = client.get(f"{settings.API_V1_STR}/users/me", headers=superuser_token_headers)
    user_id = uuid.UUID(r.json()["id"])

    stub = StubAgentEnvConnector(response_text="New session created!")
    result = _run_handle_send_message(
        user_id=user_id,
        message="Hello with bad context_id",
        agent_id=agent_id,
        agent_name=agent["name"],
        agent_env_stub=stub,
        context_id="not-a-valid-uuid-garbage",
    )

    # ── Should get a valid response (no error) ───────────────────────────
    assert "error" not in result, f"Unexpected error: {result.get('error')}"
    assert result.get("context_id"), "Should get a valid context_id for new session"
    assert result["context_id"] != "not-a-valid-uuid-garbage"

    # ── One app_mcp session should exist ────────────────────────────────
    app_mcp_sessions = _find_app_mcp_sessions(client, superuser_token_headers)
    assert len(app_mcp_sessions) == 1


def test_app_mcp_no_routes_returns_error(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db,
) -> None:
    """
    When routing returns None (no configured routes), handle_send_message
    returns a JSON error instead of raising an exception:
      1. Call handle_send_message with routing mocked to return None
      2. Verify response is a JSON object with "error" key
      3. Verify no session was created
    """
    from app.core.config import settings

    agent = _setup_agent(client, superuser_token_headers, name="No Routes Agent")

    r = client.get(f"{settings.API_V1_STR}/users/me", headers=superuser_token_headers)
    user_id = uuid.UUID(r.json()["id"])

    stub = StubAgentEnvConnector(response_text="Should not reach agent")

    async def _run():
        from app.services.app_mcp.app_mcp_request_handler import AppMCPRequestHandler
        return await AppMCPRequestHandler.handle_send_message(
            user_id=user_id,
            message="Route me to nowhere",
            context_id=None,
            mcp_ctx=None,
        )

    with patch(
        "app.services.app_mcp.app_mcp_request_handler.AppMCPRoutingService.route_message",
        return_value=None,
    ):
        with patch(
            "app.services.sessions.message_service.agent_env_connector",
            stub,
        ):
            raw = asyncio.run(_run())
    drain_tasks()

    result = json.loads(raw)

    # ── Verify error returned ────────────────────────────────────────────
    assert "error" in result, f"Expected error key, got: {result}"
    assert result.get("context_id") == "", "context_id should be empty string on error"

    # ── No session should have been created ──────────────────────────────
    app_mcp_sessions = _find_app_mcp_sessions(client, superuser_token_headers)
    assert len(app_mcp_sessions) == 0


def test_app_mcp_two_calls_no_context_create_two_sessions(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db,
) -> None:
    """
    Two calls with no context_id each create a separate session
    (simulates two different MCP client conversations):
      1. Create agent
      2. First call with context_id=None → session A
      3. Second call with context_id=None → session B (different from A)
      4. Verify two distinct app_mcp sessions exist
    """
    from app.core.config import settings

    agent = _setup_agent(client, superuser_token_headers, name="Two Sessions Agent")
    agent_id = uuid.UUID(agent["id"])

    r = client.get(f"{settings.API_V1_STR}/users/me", headers=superuser_token_headers)
    user_id = uuid.UUID(r.json()["id"])

    stub1 = StubAgentEnvConnector(response_text="Chat A reply")
    result1 = _run_handle_send_message(
        user_id=user_id,
        message="Hello from chat A",
        agent_id=agent_id,
        agent_name=agent["name"],
        agent_env_stub=stub1,
        context_id=None,
    )

    stub2 = StubAgentEnvConnector(response_text="Chat B reply")
    result2 = _run_handle_send_message(
        user_id=user_id,
        message="Hello from chat B",
        agent_id=agent_id,
        agent_name=agent["name"],
        agent_env_stub=stub2,
        context_id=None,
    )

    # ── Different context_ids for different conversations ────────────────
    assert result1["context_id"] != result2["context_id"], (
        "Two independent calls should produce different context_ids"
    )

    # ── Two separate sessions should exist ───────────────────────────────
    app_mcp_sessions = _find_app_mcp_sessions(client, superuser_token_headers)
    assert len(app_mcp_sessions) == 2, (
        f"Expected 2 app_mcp sessions, got {len(app_mcp_sessions)}"
    )


def test_app_mcp_session_owned_by_agent_owner_not_caller(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db,
) -> None:
    """
    App MCP sessions are owned by the agent owner (user_id = owner), with
    the caller tracked separately (caller_id = caller):

      1. Superuser creates an agent (becomes agent owner)
      2. A second user (caller) sends a message via App MCP
      3. Verify session.user_id == superuser.id (owner sees session)
      4. Verify session.caller_id == caller.id (caller tracked)
      5. Verify the caller does NOT see the session in their own session list
      6. Verify the owner sees the session via GET /sessions/{id}
         and caller_email is returned
    """
    from app.core.config import settings
    from tests.utils.user import create_random_user_with_headers

    # ── Phase 1: Superuser creates an agent ──────────────────────────────
    agent = _setup_agent(client, superuser_token_headers, name="Ownership Test Agent")
    agent_id = uuid.UUID(agent["id"])

    r = client.get(f"{settings.API_V1_STR}/users/me", headers=superuser_token_headers)
    assert r.status_code == 200
    owner_id = uuid.UUID(r.json()["id"])

    # ── Phase 2: Create a second user (the MCP caller) ───────────────────
    caller_user, caller_headers = create_random_user_with_headers(client)
    caller_id = uuid.UUID(caller_user["id"])
    caller_email = caller_user["email"]

    # ── Phase 3: Caller sends a message via App MCP ──────────────────────
    stub = StubAgentEnvConnector(response_text="Response from owner's agent")
    result = _run_handle_send_message(
        user_id=caller_id,
        message="Hello from the caller",
        agent_id=agent_id,
        agent_name=agent["name"],
        agent_env_stub=stub,
        context_id=None,
    )

    assert "error" not in result, f"Unexpected error: {result.get('error')}"
    context_id = result["context_id"]
    assert context_id

    # ── Phase 4: Owner sees the session ──────────────────────────────────
    owner_mcp_sessions = _find_app_mcp_sessions(client, superuser_token_headers)
    assert len(owner_mcp_sessions) == 1, (
        f"Owner should see 1 app_mcp session, got {len(owner_mcp_sessions)}"
    )
    owner_session = owner_mcp_sessions[0]

    # user_id is the agent owner
    assert owner_session["user_id"] == str(owner_id), (
        f"session.user_id should be owner {owner_id}, got {owner_session['user_id']}"
    )
    # caller_id tracks who initiated via MCP
    assert owner_session["caller_id"] == str(caller_id), (
        f"session.caller_id should be caller {caller_id}, got {owner_session['caller_id']}"
    )

    # ── Phase 5: Caller does NOT see the session in their own list ────────
    caller_sessions = _find_app_mcp_sessions(client, caller_headers)
    assert len(caller_sessions) == 0, (
        f"Caller should NOT see app_mcp sessions in their list, got {len(caller_sessions)}"
    )

    # ── Phase 6: GET /sessions/{id} returns caller_email for the owner ───
    r = client.get(
        f"{settings.API_V1_STR}/sessions/{context_id}",
        headers=superuser_token_headers,
    )
    assert r.status_code == 200
    session_detail = r.json()
    assert session_detail["caller_id"] == str(caller_id)
    assert session_detail["caller_email"] == caller_email


def test_app_mcp_context_id_caller_isolation(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db,
) -> None:
    """
    Context ID validation checks caller_id (not user_id) for app_mcp session resumption.
    A different caller cannot resume another caller's session using their context_id:

      1. Superuser creates an agent
      2. Caller A sends a message → gets context_id A
      3. Caller B tries to resume context_id A → gets a NEW session (not A's session)
      4. Verify two distinct app_mcp sessions exist under the owner
    """
    from app.core.config import settings
    from tests.utils.user import create_random_user_with_headers

    # ── Phase 1: Superuser creates an agent ──────────────────────────────
    agent = _setup_agent(client, superuser_token_headers, name="Isolation Test Agent")
    agent_id = uuid.UUID(agent["id"])

    # ── Phase 2: Create two callers ──────────────────────────────────────
    caller_a_user, _ = create_random_user_with_headers(client)
    caller_b_user, _ = create_random_user_with_headers(client)
    caller_a_id = uuid.UUID(caller_a_user["id"])
    caller_b_id = uuid.UUID(caller_b_user["id"])

    # ── Phase 3: Caller A sends a message → gets context_id A ────────────
    stub_a = StubAgentEnvConnector(response_text="Response to A")
    result_a = _run_handle_send_message(
        user_id=caller_a_id,
        message="Hello from caller A",
        agent_id=agent_id,
        agent_name=agent["name"],
        agent_env_stub=stub_a,
        context_id=None,
    )
    assert "error" not in result_a, f"Caller A failed: {result_a.get('error')}"
    context_id_a = result_a["context_id"]

    # ── Phase 4: Caller B tries to resume context_id A ───────────────────
    stub_b = StubAgentEnvConnector(response_text="Response to B with A's context")
    result_b = _run_handle_send_message(
        user_id=caller_b_id,
        message="Caller B trying to resume A's session",
        agent_id=agent_id,
        agent_name=agent["name"],
        agent_env_stub=stub_b,
        context_id=context_id_a,
    )
    assert "error" not in result_b, f"Caller B failed: {result_b.get('error')}"

    # Caller B should NOT reuse caller A's session — a new session is created
    assert result_b["context_id"] != context_id_a, (
        "Caller B should NOT be able to resume Caller A's session"
    )

    # ── Phase 5: Owner sees two distinct sessions ─────────────────────────
    owner_mcp_sessions = _find_app_mcp_sessions(client, superuser_token_headers)
    assert len(owner_mcp_sessions) == 2, (
        f"Expected 2 app_mcp sessions (one per caller), got {len(owner_mcp_sessions)}"
    )
    # Each session should have a different caller_id
    caller_ids = {s["caller_id"] for s in owner_mcp_sessions}
    assert str(caller_a_id) in caller_ids
    assert str(caller_b_id) in caller_ids
