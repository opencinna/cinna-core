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
  - Reuses an existing identity_mcp session when a valid context_id is passed back
  - Returns an error (not raises) when the identity binding is deactivated mid-session
  - Falls back to the caller's email for `identity_caller_name` when their `full_name` is empty

These tests call handle_send_message() directly (not through MCP protocol)
with the routing service and agent environment stubbed.

The routing service is mocked to return a fixed agent, so we do not need
real routing candidates (owned agents / identity bindings) configured — the
handler's routing step is bypassed.
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


def _find_identity_mcp_sessions(
    client: TestClient,
    token_headers: dict[str, str],
) -> list[dict]:
    """Return all sessions with integration_type='identity_mcp'."""
    sessions = list_sessions(client, token_headers)
    return [s for s in sessions if s.get("integration_type") == "identity_mcp"]


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
        source="owned",
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
        source="owned",
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
    App MCP sessions opened by someone other than the agent owner are owned
    by the owner (user_id = owner), with the caller tracked separately:

      1. Superuser creates an agent (becomes agent owner)
      2. A second user (caller) sends a message via App MCP, reaching the
         owner's agent through a real identity grant. Since the
         ``AppAgentRoute`` family is deleted, ``ChannelIngestionService
         .assert_access``'s ``mcp_caller`` arm now authorises a foreign
         agent only via ownership or an identity grant re-verified against
         the database — so a caller who does not own the agent can only
         ever reach it this way, and the session it creates is stamped
         ``integration_type="identity_mcp"`` with the caller tracked via
         ``identity_caller_id``, not the plain-``app_mcp`` ``caller_id``
         (which, post-deletion, can only ever equal the owner's own id: a
         "owned"-source routing result is never produced for an agent the
         caller does not own, so ``caller_id != user_id`` on a plain
         ``app_mcp`` session is no longer reachable at all).
      3. Verify session.user_id == superuser.id (owner sees session)
      4. Verify session.identity_caller_id == caller.id (caller tracked),
         read back through GET /external/sessions — the surface that
         projects the column (`GET /sessions/` deliberately does not)
      5. Verify the caller does NOT see the session in their own session list
      6. Verify the owner's session_metadata carries identity_caller_name,
         which is what labels the caller's message in the owner's own list
    """
    from app.core.config import settings
    from app.services.app_mcp.app_mcp_request_handler import AppMCPRequestHandler
    from app.services.app_mcp.app_mcp_routing_service import RoutingResult
    from tests.utils.identity import create_identity_binding
    from tests.utils.user import user_authentication_headers
    from tests.utils.utils import random_email, random_lower_string

    # ── Phase 1: Superuser creates an agent ──────────────────────────────
    agent = _setup_agent(client, superuser_token_headers, name="Ownership Test Agent")
    agent_id = uuid.UUID(agent["id"])

    r = client.get(f"{settings.API_V1_STR}/users/me", headers=superuser_token_headers)
    assert r.status_code == 200
    owner_id = uuid.UUID(r.json()["id"])

    # ── Phase 2: Create a second user (the MCP caller) and grant them
    # identity access to the owner's agent. Signed up with an explicit
    # full_name so Phase 7 below pins the name itself. The nameless case —
    # where session_metadata["identity_caller_name"] falls back to the email,
    # as the channel and external-A2A identity paths have always done — is
    # covered by
    # ``test_app_mcp_identity_caller_name_falls_back_to_email_when_unnamed``.
    caller_email = random_email()
    caller_password = random_lower_string()
    caller_full_name = f"Caller {random_lower_string()[:8]}"
    r = client.post(
        f"{settings.API_V1_STR}/users/signup",
        json={
            "email": caller_email,
            "password": caller_password,
            "full_name": caller_full_name,
        },
    )
    assert r.status_code == 200, r.text
    caller_user = r.json()
    caller_headers = user_authentication_headers(
        client=client, email=caller_email, password=caller_password
    )
    caller_id = uuid.UUID(caller_user["id"])

    binding = create_identity_binding(
        client,
        superuser_token_headers,
        agent_id=agent["id"],
        trigger_prompt="Route to this agent for ownership test.",
        assigned_user_ids=[caller_user["id"]],
        auto_enable=True,
    )
    binding_id = uuid.UUID(binding["id"])
    assignments = binding.get("assignments", [])
    assert len(assignments) == 1, f"Expected 1 assignment, got {assignments}"
    assignment_id = uuid.UUID(assignments[0]["id"])

    fixed_identity_result = RoutingResult(
        agent_id=agent_id,
        agent_name=agent["name"],
        session_mode="conversation",
        source="identity",
        match_method="only_one",
        is_identity=True,
        identity_owner_id=owner_id,
        identity_owner_name="Identity Owner",
        identity_binding_id=binding_id,
        identity_binding_assignment_id=assignment_id,
    )

    # ── Phase 3: Caller sends a message via App MCP ──────────────────────
    stub = StubAgentEnvConnector(response_text="Response from owner's agent")

    async def _run():
        return await AppMCPRequestHandler.handle_send_message(
            user_id=caller_id,
            message="Hello from the caller",
            context_id=None,
            mcp_ctx=None,
        )

    with patch(
        "app.services.app_mcp.app_mcp_request_handler.AppMCPRoutingService.route_message",
        return_value=fixed_identity_result,
    ):
        with patch(
            "app.services.sessions.message_service.agent_env_connector",
            stub,
        ):
            raw = asyncio.run(_run())
    drain_tasks()
    result = json.loads(raw)

    assert "error" not in result, f"Unexpected error: {result.get('error')}"
    context_id = result["context_id"]
    assert context_id

    # ── Phase 4: Owner sees the session ──────────────────────────────────
    owner_mcp_sessions = _find_identity_mcp_sessions(client, superuser_token_headers)
    assert len(owner_mcp_sessions) == 1, (
        f"Owner should see 1 identity_mcp session, got {len(owner_mcp_sessions)}"
    )
    owner_session = owner_mcp_sessions[0]
    assert owner_session["id"] == context_id

    # user_id is the agent owner
    assert owner_session["user_id"] == str(owner_id), (
        f"session.user_id should be owner {owner_id}, got {owner_session['user_id']}"
    )

    # ── Phase 5: identity_caller_id tracks the caller, read back through
    # GET /external/sessions — GET /sessions/ does not project the column ──
    r = client.get(f"{settings.API_V1_STR}/external/sessions", headers=caller_headers)
    assert r.status_code == 200, r.text
    caller_external = [s for s in r.json() if s["id"] == context_id]
    assert len(caller_external) == 1, r.json()
    assert caller_external[0]["identity_caller_id"] == str(caller_id)

    # ── Phase 6: Caller does NOT see the session in their own session list ─
    caller_own_sessions = list_sessions(client, caller_headers)
    assert all(s["id"] != context_id for s in caller_own_sessions), (
        f"Caller should NOT see the owner's session in their own list, "
        f"got {caller_own_sessions}"
    )

    # ── Phase 7: session_metadata carries identity_caller_name for the owner
    metadata = owner_session.get("session_metadata") or {}
    assert metadata.get("identity_caller_name") == caller_full_name, metadata


def test_app_mcp_identity_caller_name_falls_back_to_email_when_unnamed(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """A caller with no ``full_name`` is labelled by their email, not by nothing.

    ``full_name`` is optional and routinely empty — a signup that does not send
    one, or an OAuth provider that supplies none — so it cannot be the last word
    in a field that is read back as a **person**. The owner's session list
    renders a "Via Identity — {caller}" badge from
    ``session_metadata["identity_caller_name"]``, and a blank there leaves them
    a conversation they never started, containing a stranger's message,
    identified by nothing.

    Both sibling identity paths already fall through to the email
    (``external_a2a_request_handler``: ``full_name or email or str(id)``;
    ``channel_inbound_service``: ``(full_name or "").strip() or email``). App
    MCP was the only one of the three that read ``full_name`` alone. This is
    that gap, closed, and it is asserted **through the same surface the owner
    reads** rather than against the helper.

    ``identity_owner_name`` is checked on the same row: it had the identical
    defect on the adjacent line, and the property that matters for both is that
    a user who exists is never identified by a raw uuid.
    """
    from app.core.config import settings
    from app.services.app_mcp.app_mcp_request_handler import AppMCPRequestHandler
    from app.services.app_mcp.app_mcp_routing_service import RoutingResult
    from tests.utils.identity import create_identity_binding
    from tests.utils.user import user_authentication_headers
    from tests.utils.utils import random_email, random_lower_string

    agent = _setup_agent(client, superuser_token_headers, name="Unnamed Caller Agent")
    agent_id = uuid.UUID(agent["id"])

    r = client.get(f"{settings.API_V1_STR}/users/me", headers=superuser_token_headers)
    assert r.status_code == 200
    owner_id = uuid.UUID(r.json()["id"])

    # Signed up with NO full_name — the whole point of this test.
    caller_email = random_email()
    caller_password = random_lower_string()
    r = client.post(
        f"{settings.API_V1_STR}/users/signup",
        json={"email": caller_email, "password": caller_password},
    )
    assert r.status_code == 200, r.text
    caller_user = r.json()
    assert not (caller_user.get("full_name") or "").strip(), caller_user
    caller_headers = user_authentication_headers(
        client=client, email=caller_email, password=caller_password
    )
    caller_id = uuid.UUID(caller_user["id"])

    binding = create_identity_binding(
        client,
        superuser_token_headers,
        agent_id=agent["id"],
        trigger_prompt="Route to this agent for the unnamed-caller test.",
        assigned_user_ids=[caller_user["id"]],
        auto_enable=True,
    )
    assignments = binding.get("assignments", [])
    assert len(assignments) == 1, f"Expected 1 assignment, got {assignments}"

    fixed_identity_result = RoutingResult(
        agent_id=agent_id,
        agent_name=agent["name"],
        session_mode="conversation",
        source="identity",
        match_method="only_one",
        is_identity=True,
        identity_owner_id=owner_id,
        identity_owner_name="Identity Owner",
        identity_binding_id=uuid.UUID(binding["id"]),
        identity_binding_assignment_id=uuid.UUID(assignments[0]["id"]),
    )

    stub = StubAgentEnvConnector(response_text="Response from owner's agent")

    async def _run():
        return await AppMCPRequestHandler.handle_send_message(
            user_id=caller_id,
            message="Hello from an unnamed caller",
            context_id=None,
            mcp_ctx=None,
        )

    with patch(
        "app.services.app_mcp.app_mcp_request_handler.AppMCPRoutingService.route_message",
        return_value=fixed_identity_result,
    ):
        with patch("app.services.sessions.message_service.agent_env_connector", stub):
            raw = asyncio.run(_run())
    drain_tasks()
    result = json.loads(raw)
    assert "error" not in result, f"Unexpected error: {result.get('error')}"
    context_id = result["context_id"]
    assert context_id

    owner_sessions = _find_identity_mcp_sessions(client, superuser_token_headers)
    owner_session = next(s for s in owner_sessions if s["id"] == context_id)
    metadata = owner_session.get("session_metadata") or {}

    # The fix: the email, not None and not the raw id.
    assert metadata.get("identity_caller_name") == caller_email, metadata
    # The adjacent line, which had the same defect: an existing user is never
    # identified by a bare uuid.
    owner_name = metadata.get("identity_owner_name")
    assert owner_name, metadata
    assert owner_name != str(owner_id), metadata

    # The caller's headers are live — the grant is real, not a stub artefact.
    r = client.get(f"{settings.API_V1_STR}/external/sessions", headers=caller_headers)
    assert r.status_code == 200, r.text
    assert any(s["id"] == context_id for s in r.json()), r.json()


def test_app_mcp_context_id_caller_isolation(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db,
) -> None:
    """
    Context ID validation checks identity_caller_id (not user_id) for
    identity_mcp session resumption. A different caller cannot resume
    another caller's session using their context_id:

      1. Superuser creates an agent and grants both callers identity access
         to it (``AppAgentRoute`` is deleted, so a non-owner caller now needs
         a real, re-verified identity grant to reach the agent at all — see
         ``test_app_mcp_session_owned_by_agent_owner_not_caller``, which is
         also why these are ``identity_mcp`` sessions rather than plain
         ``app_mcp`` ones: a caller who does not own the agent can only ever
         reach it through an identity grant now)
      2. Caller A sends a message → gets context_id A
      3. Caller B tries to resume context_id A → gets a NEW session (not A's session)
      4. Verify two distinct identity_mcp sessions exist under the owner,
         each with the right caller's identity_caller_id
    """
    from app.core.config import settings
    from app.services.app_mcp.app_mcp_request_handler import AppMCPRequestHandler
    from app.services.app_mcp.app_mcp_routing_service import RoutingResult
    from tests.utils.identity import create_identity_binding
    from tests.utils.user import create_random_user_with_headers

    # ── Phase 1: Superuser creates an agent ──────────────────────────────
    agent = _setup_agent(client, superuser_token_headers, name="Isolation Test Agent")
    agent_id = uuid.UUID(agent["id"])

    r = client.get(f"{settings.API_V1_STR}/users/me", headers=superuser_token_headers)
    assert r.status_code == 200
    owner_id = uuid.UUID(r.json()["id"])

    # ── Phase 2: Create two callers, each with their own identity grant ──
    caller_a_user, caller_a_headers = create_random_user_with_headers(client)
    caller_b_user, caller_b_headers = create_random_user_with_headers(client)
    caller_a_id = uuid.UUID(caller_a_user["id"])
    caller_b_id = uuid.UUID(caller_b_user["id"])

    binding = create_identity_binding(
        client,
        superuser_token_headers,
        agent_id=agent["id"],
        trigger_prompt="Route to this agent for isolation test.",
        assigned_user_ids=[caller_a_user["id"], caller_b_user["id"]],
        auto_enable=True,
    )
    binding_id = uuid.UUID(binding["id"])
    assignments = {a["target_user_id"]: uuid.UUID(a["id"]) for a in binding.get("assignments", [])}
    assert len(assignments) == 2, f"Expected 2 assignments, got {binding.get('assignments')}"

    def _identity_result(caller_user: dict) -> "RoutingResult":
        return RoutingResult(
            agent_id=agent_id,
            agent_name=agent["name"],
            session_mode="conversation",
            source="identity",
            match_method="only_one",
            is_identity=True,
            identity_owner_id=owner_id,
            identity_owner_name="Identity Owner",
            identity_binding_id=binding_id,
            identity_binding_assignment_id=assignments[caller_user["id"]],
        )

    async def _run(user_id, message, context_id):
        return await AppMCPRequestHandler.handle_send_message(
            user_id=user_id,
            message=message,
            context_id=context_id,
            mcp_ctx=None,
        )

    # ── Phase 3: Caller A sends a message → gets context_id A ────────────
    stub_a = StubAgentEnvConnector(response_text="Response to A")
    with patch(
        "app.services.app_mcp.app_mcp_request_handler.AppMCPRoutingService.route_message",
        return_value=_identity_result(caller_a_user),
    ):
        with patch(
            "app.services.sessions.message_service.agent_env_connector",
            stub_a,
        ):
            raw_a = asyncio.run(_run(caller_a_id, "Hello from caller A", None))
    drain_tasks()
    result_a = json.loads(raw_a)
    assert "error" not in result_a, f"Caller A failed: {result_a.get('error')}"
    context_id_a = result_a["context_id"]

    # ── Phase 4: Caller B tries to resume context_id A ───────────────────
    stub_b = StubAgentEnvConnector(response_text="Response to B with A's context")
    with patch(
        "app.services.app_mcp.app_mcp_request_handler.AppMCPRoutingService.route_message",
        return_value=_identity_result(caller_b_user),
    ):
        with patch(
            "app.services.sessions.message_service.agent_env_connector",
            stub_b,
        ):
            raw_b = asyncio.run(
                _run(caller_b_id, "Caller B trying to resume A's session", context_id_a)
            )
    drain_tasks()
    result_b = json.loads(raw_b)
    assert "error" not in result_b, f"Caller B failed: {result_b.get('error')}"

    # Caller B should NOT reuse caller A's session — a new session is created
    assert result_b["context_id"] != context_id_a, (
        "Caller B should NOT be able to resume Caller A's session"
    )

    # ── Phase 5: Owner sees two distinct sessions ─────────────────────────
    owner_mcp_sessions = _find_identity_mcp_sessions(client, superuser_token_headers)
    assert len(owner_mcp_sessions) == 2, (
        f"Expected 2 identity_mcp sessions (one per caller), got {len(owner_mcp_sessions)}"
    )
    ids_by_context = {s["id"]: s for s in owner_mcp_sessions}
    assert set(ids_by_context) == {context_id_a, result_b["context_id"]}

    # Each session's identity_caller_id is the right caller — read back
    # through GET /external/sessions, the surface that projects the column
    # (GET /sessions/ deliberately does not).
    r = client.get(f"{settings.API_V1_STR}/external/sessions", headers=caller_a_headers)
    assert r.status_code == 200, r.text
    a_external = next(s for s in r.json() if s["id"] == context_id_a)
    assert a_external["identity_caller_id"] == str(caller_a_id)

    r = client.get(f"{settings.API_V1_STR}/external/sessions", headers=caller_b_headers)
    assert r.status_code == 200, r.text
    b_external = next(s for s in r.json() if s["id"] == result_b["context_id"])
    assert b_external["identity_caller_id"] == str(caller_b_id)


def test_app_mcp_identity_mcp_context_id_reuses_existing_session(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db,
) -> None:
    """
    Passing back context_id from a first identity_mcp call reuses the same session.

    The _try_resume_session branch for integration_type="identity_mcp" matches on
    session.identity_caller_id (not session.caller_id). This test exercises that
    branch end-to-end through handle_send_message():

      1. Create agent + identity binding with the caller assigned (auto_enable)
      2. First call — identity routing → new identity_mcp session, get context_id
      3. Second call — pass back context_id → same session reused (identity branch)
      4. Verify only one identity_mcp session exists (no duplicate created)
      5. Verify both user messages are in the same session

    Both calls run within a single asyncio.run() so the handler's create_session()
    calls share the test DB session state correctly.
    """
    from app.core.config import settings
    from app.services.app_mcp.app_mcp_request_handler import AppMCPRequestHandler
    from app.services.app_mcp.app_mcp_routing_service import RoutingResult
    from tests.utils.identity import create_identity_binding, assign_users_to_binding
    from tests.utils.message import list_messages
    from tests.utils.server_channel import find_server_channel_by_type
    from tests.utils.user import create_random_user_with_headers
    from tests.utils.user_channel import update_my_channel

    # ── Phase 1: Create agent (owned by superuser) ────────────────────────
    agent = _setup_agent(client, superuser_token_headers, name="Identity Resume Agent")
    agent_id = uuid.UUID(agent["id"])

    r = client.get(f"{settings.API_V1_STR}/users/me", headers=superuser_token_headers)
    assert r.status_code == 200
    owner_id = uuid.UUID(r.json()["id"])

    # ── Phase 2: Create caller + identity binding ─────────────────────────
    caller_user, caller_headers = create_random_user_with_headers(client)
    caller_id = uuid.UUID(caller_user["id"])

    # Setup only — no assertion below changes. `route_message` is patched out
    # further down, so the forged identity result stands in for a decision the
    # routing layer would only ever have made with this caller's own
    # `allow_identity_routing` switch on. The resume path re-reads that switch
    # per message (parity with the channel path, which has always done so), so
    # the premise now has to be true rather than merely implied by the patch.
    app_mcp_channel = find_server_channel_by_type(
        client, superuser_token_headers, "app_mcp"
    )
    update_my_channel(
        client, caller_headers, app_mcp_channel["id"], allow_identity_routing=True
    )

    binding = create_identity_binding(
        client,
        superuser_token_headers,
        agent_id=agent["id"],
        trigger_prompt="Route to this agent for identity test.",
        assigned_user_ids=[caller_user["id"]],
        auto_enable=True,
    )
    binding_id = uuid.UUID(binding["id"])

    # Resolve the assignment ID — it's the first assignment in the binding.
    assignments = binding.get("assignments", [])
    assert len(assignments) == 1, f"Expected 1 assignment, got {assignments}"
    assignment_id = uuid.UUID(assignments[0]["id"])

    # Build an identity routing result pointing at this agent + binding.
    fixed_identity_result = RoutingResult(
        agent_id=agent_id,
        agent_name=agent["name"],
        session_mode="conversation",
        source="identity",
        match_method="only_one",
        is_identity=True,
        identity_owner_id=owner_id,
        identity_owner_name="Identity Owner",
        identity_binding_id=binding_id,
        identity_binding_assignment_id=assignment_id,
    )

    async def _run_both():
        # First call — no context_id → new identity_mcp session
        result1_raw = await AppMCPRequestHandler.handle_send_message(
            user_id=caller_id,
            message="First identity message",
            context_id=None,
            mcp_ctx=None,
        )
        result1 = json.loads(result1_raw)

        # Second call — pass back context_id → reuse identity_mcp session
        result2_raw = await AppMCPRequestHandler.handle_send_message(
            user_id=caller_id,
            message="Second identity message",
            context_id=result1.get("context_id"),
            mcp_ctx=None,
        )
        result2 = json.loads(result2_raw)
        return result1, result2

    stub = StubAgentEnvConnector(response_text="Identity agent reply")
    with patch(
        "app.services.app_mcp.app_mcp_request_handler.AppMCPRoutingService.route_message",
        return_value=fixed_identity_result,
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
        f"identity_mcp context_id changed on resume: "
        f"got {result2['context_id']!r}, expected {ctx_id!r}"
    )

    # ── Only one identity_mcp session should exist ────────────────────────
    all_sessions = list_sessions(client, superuser_token_headers)
    identity_sessions = [s for s in all_sessions if s.get("integration_type") == "identity_mcp"]
    assert len(identity_sessions) == 1, (
        f"Expected 1 identity_mcp session (reused), got {len(identity_sessions)}"
    )
    session_id = identity_sessions[0]["id"]
    assert session_id == ctx_id, (
        f"identity_mcp session_id {session_id!r} does not match context_id {ctx_id!r}"
    )

    # ── Both user messages are in the same session ────────────────────────
    messages = list_messages(client, superuser_token_headers, session_id)
    user_msgs = [m for m in messages if m["role"] == "user"]
    assert len(user_msgs) >= 2, (
        f"Expected 2 user messages in same session, got {len(user_msgs)}"
    )
    user_contents = {m["content"] for m in user_msgs}
    assert "First identity message" in user_contents
    assert "Second identity message" in user_contents


def test_app_mcp_identity_binding_deactivated_mid_session_returns_error(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db,
) -> None:
    """
    Deactivating an identity binding mid-session causes the next
    handle_send_message() call with the same context_id to return a JSON error
    (not raise), via AppMCPRequestHandler._check_identity_session_validity.

    The handler's _try_resume_session finds the identity_mcp session, calls
    _check_identity_session_validity, gets a non-None error string, and returns
    (None, error_message, False). The outer _handle_inner then returns a JSON
    response with an "error" key.

      1. Create agent + identity binding with a caller assigned
      2. First call — creates identity_mcp session, capture context_id
      3. Deactivate the binding via PUT /identity/bindings/{id} (is_active=False)
      4. Second call — same context_id → error JSON returned, context_id empty
    """
    from app.core.config import settings
    from app.services.app_mcp.app_mcp_request_handler import AppMCPRequestHandler
    from app.services.app_mcp.app_mcp_routing_service import RoutingResult
    from tests.utils.identity import (
        create_identity_binding,
        update_identity_binding,
    )
    from tests.utils.user import create_random_user_with_headers

    # ── Phase 1: Create agent + identity binding with caller assigned ────
    agent = _setup_agent(client, superuser_token_headers, name="Revocation Test Agent")
    agent_id = uuid.UUID(agent["id"])

    r = client.get(f"{settings.API_V1_STR}/users/me", headers=superuser_token_headers)
    assert r.status_code == 200
    owner_id = uuid.UUID(r.json()["id"])

    caller_user, _ = create_random_user_with_headers(client)
    caller_id = uuid.UUID(caller_user["id"])

    binding = create_identity_binding(
        client,
        superuser_token_headers,
        agent_id=agent["id"],
        trigger_prompt="Route to this agent for revocation test.",
        assigned_user_ids=[caller_user["id"]],
        auto_enable=True,
    )
    binding_id = uuid.UUID(binding["id"])

    assignments = binding.get("assignments", [])
    assert len(assignments) == 1, f"Expected 1 assignment, got {assignments}"
    assignment_id = uuid.UUID(assignments[0]["id"])

    fixed_identity_result = RoutingResult(
        agent_id=agent_id,
        agent_name=agent["name"],
        session_mode="conversation",
        source="identity",
        match_method="only_one",
        is_identity=True,
        identity_owner_id=owner_id,
        identity_owner_name="Identity Owner",
        identity_binding_id=binding_id,
        identity_binding_assignment_id=assignment_id,
    )

    async def _run(context_id):
        return await AppMCPRequestHandler.handle_send_message(
            user_id=caller_id,
            message="Test message",
            context_id=context_id,
            mcp_ctx=None,
        )

    # ── Phase 2: First call — establish identity_mcp session ─────────────
    stub = StubAgentEnvConnector(response_text="Initial identity reply")
    with patch(
        "app.services.app_mcp.app_mcp_request_handler.AppMCPRoutingService.route_message",
        return_value=fixed_identity_result,
    ):
        with patch(
            "app.services.sessions.message_service.agent_env_connector",
            stub,
        ):
            raw1 = asyncio.run(_run(context_id=None))
    drain_tasks()

    result1 = json.loads(raw1)
    assert "error" not in result1, f"First call should succeed: {result1.get('error')}"
    ctx_id = result1["context_id"]
    assert ctx_id, "First call must return a context_id"

    # ── Phase 3: Deactivate the binding via API ───────────────────────────
    update_identity_binding(
        client,
        superuser_token_headers,
        str(binding_id),
        is_active=False,
    )

    # ── Phase 4: Second call with same context_id → error (not exception) ─
    stub2 = StubAgentEnvConnector(response_text="Should not reach agent")
    with patch(
        "app.services.app_mcp.app_mcp_request_handler.AppMCPRoutingService.route_message",
        return_value=fixed_identity_result,
    ):
        with patch(
            "app.services.sessions.message_service.agent_env_connector",
            stub2,
        ):
            raw2 = asyncio.run(_run(context_id=ctx_id))
    drain_tasks()

    result2 = json.loads(raw2)
    assert "error" in result2, (
        f"Expected error key in response after binding deactivation, got: {result2}"
    )
    assert "no longer active" in result2["error"].lower(), (
        f"Error message should mention 'no longer active', got: {result2['error']!r}"
    )
    # context_id should be empty on error (handler contract)
    assert result2.get("context_id") == "", (
        f"context_id should be empty string on validity error, got: {result2.get('context_id')!r}"
    )


def test_app_mcp_caller_withdrawing_consent_closes_the_identity_session(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db,
) -> None:
    """Switching `allow_identity_routing` off closes an OPEN identity session.

    The sibling above covers the *owner's* half of revocation (the binding goes
    inactive). This is the *caller's* own half, and it used to have no effect
    at all on a conversation already in progress:
    `_check_identity_session_validity` re-reads the binding and the assignment,
    neither of which the caller's per-channel consent switch touches, so
    `_try_resume_session` happily resumed a session the caller had just
    withdrawn permission for. Turning the switch off stopped new identity
    routing and left every open identity session answering.

    The channel path has always re-read the flag per message on a bound
    identity thread (`ChannelInboundService._ingest`, raising `ChannelDecline`
    — see `tests/api/server_channels/server_channels_identity_revocation_test.py`
    for the same story on Google Chat). Revocation must close both surfaces,
    so this asserts App MCP's half.

    Two properties, and the second is what makes this more than a smoke test:

      1. The next call is refused rather than answered.
      2. It is refused with the **same** message a revoked binding gets, and
         it does not quietly fall through to routing and open a *fresh*
         identity session instead — `route_message` is patched to keep
         returning the identity result, so a fall-through would show up as a
         second `identity_mcp` session and a new `context_id`, not as an error.
    """
    from app.core.config import settings
    from app.services.app_mcp.app_mcp_request_handler import AppMCPRequestHandler
    from app.services.app_mcp.app_mcp_routing_service import RoutingResult
    from app.services.identity.identity_service import IdentityService
    from tests.utils.identity import create_identity_binding
    from tests.utils.server_channel import find_server_channel_by_type
    from tests.utils.user import create_random_user_with_headers
    from tests.utils.user_channel import update_my_channel

    agent = _setup_agent(client, superuser_token_headers, name="Consent Test Agent")
    agent_id = uuid.UUID(agent["id"])

    r = client.get(f"{settings.API_V1_STR}/users/me", headers=superuser_token_headers)
    assert r.status_code == 200
    owner_id = uuid.UUID(r.json()["id"])

    caller_user, caller_headers = create_random_user_with_headers(client)
    caller_id = uuid.UUID(caller_user["id"])

    # The caller consents. It never inherits and has no channel-level default,
    # so this route is the only way it can ever become true.
    app_mcp_channel = find_server_channel_by_type(
        client, superuser_token_headers, "app_mcp"
    )
    update_my_channel(
        client, caller_headers, app_mcp_channel["id"], allow_identity_routing=True
    )

    binding = create_identity_binding(
        client,
        superuser_token_headers,
        agent_id=agent["id"],
        trigger_prompt="Route to this agent for consent test.",
        assigned_user_ids=[caller_user["id"]],
        auto_enable=True,
    )
    binding_id = uuid.UUID(binding["id"])
    assignments = binding.get("assignments", [])
    assert len(assignments) == 1, f"Expected 1 assignment, got {assignments}"
    assignment_id = uuid.UUID(assignments[0]["id"])

    fixed_identity_result = RoutingResult(
        agent_id=agent_id,
        agent_name=agent["name"],
        session_mode="conversation",
        source="identity",
        match_method="only_one",
        is_identity=True,
        identity_owner_id=owner_id,
        identity_owner_name="Identity Owner",
        identity_binding_id=binding_id,
        identity_binding_assignment_id=assignment_id,
    )

    async def _run(context_id):
        return await AppMCPRequestHandler.handle_send_message(
            user_id=caller_id,
            message="Test message",
            context_id=context_id,
            mcp_ctx=None,
        )

    # ── The conversation opens while consent stands ──────────────────────
    stub = StubAgentEnvConnector(response_text="Identity reply")
    with patch(
        "app.services.app_mcp.app_mcp_request_handler.AppMCPRoutingService.route_message",
        return_value=fixed_identity_result,
    ):
        with patch("app.services.sessions.message_service.agent_env_connector", stub):
            raw1 = asyncio.run(_run(context_id=None))
    drain_tasks()

    result1 = json.loads(raw1)
    assert "error" not in result1, f"First call should succeed: {result1.get('error')}"
    ctx_id = result1["context_id"]
    assert ctx_id, "First call must return a context_id"

    # ── The caller withdraws it, mid-conversation ────────────────────────
    update_my_channel(
        client, caller_headers, app_mcp_channel["id"], allow_identity_routing=False
    )

    stub2 = StubAgentEnvConnector(response_text="Should not reach agent")
    with patch(
        "app.services.app_mcp.app_mcp_request_handler.AppMCPRoutingService.route_message",
        return_value=fixed_identity_result,
    ):
        with patch("app.services.sessions.message_service.agent_env_connector", stub2):
            raw2 = asyncio.run(_run(context_id=ctx_id))
    drain_tasks()

    result2 = json.loads(raw2)
    # 1: refused.
    assert "error" in result2, (
        f"Expected the resume to be refused after consent withdrawal, got: {result2}"
    )
    # 2a: refused with the same words a revoked binding gets — the caller must
    #     not be able to tell which of the switches closed.
    assert result2["error"] == IdentityService.IDENTITY_REVOKED_MESSAGE, result2
    assert result2.get("context_id") == "", result2

    # 2b: and NOT quietly re-routed into a brand-new identity session.
    identity_sessions = [
        s
        for s in list_sessions(client, superuser_token_headers)
        if s.get("integration_type") == "identity_mcp"
    ]
    assert len(identity_sessions) == 1, (
        f"consent withdrawal must refuse, not open a second identity session: "
        f"{identity_sessions}"
    )
    assert identity_sessions[0]["id"] == ctx_id


def test_app_mcp_reaches_a_standalone_agent_with_only_a_trigger_prompt(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """The case that motivated the whole channel/App-MCP scope split (plan
    §1 of docs/plans/channels_identity_unification/phase_5_app_mcp_channel.md):
    a standalone (non-bundle) agent with a router trigger prompt is reachable
    over App MCP with no route, no assignment, no toggle — because none of
    those exist any more.

    Unlike every other test in this file, ``AppMCPRoutingService.route_
    message`` is deliberately NOT mocked here — the whole point is that the
    real routing path reaches this agent with nothing configured beyond the
    trigger prompt. Only the LLM/agent-env connector is stubbed. A single
    owned agent takes Stage 1's `only_one` short-circuit
    (`routing_reachability_verdict_test.py` pins the same shortcut on the
    channel side), so no classifier call is needed either.
    """
    from app.services.app_mcp.app_mcp_request_handler import AppMCPRequestHandler
    from tests.utils.agent import set_router_trigger_prompt

    agent = _setup_agent(client, superuser_token_headers, name="Standalone Reachable Agent")
    assert agent.get("bundle_uuid") is None, (
        "Precondition: this must be a standalone (non-bundle) agent — the "
        "shape that never got an auto-managed AppAgentRoute even before "
        "the family was deleted."
    )
    set_router_trigger_prompt(
        client, superuser_token_headers, agent["id"], "Handle anything for the owner"
    )

    from app.core.config import settings

    r = client.get(f"{settings.API_V1_STR}/users/me", headers=superuser_token_headers)
    assert r.status_code == 200
    owner_id = r.json()["id"]

    stub = StubAgentEnvConnector(response_text="Reached the standalone agent")

    async def _run():
        return await AppMCPRequestHandler.handle_send_message(
            user_id=uuid.UUID(owner_id),
            message="Hello, route me for real",
            context_id=None,
            mcp_ctx=None,
        )

    # Real routing — AppMCPRoutingService.route_message is NOT patched.
    with patch(
        "app.services.sessions.message_service.agent_env_connector",
        stub,
    ):
        raw = asyncio.run(_run())
    drain_tasks()
    result = json.loads(raw)

    assert "error" not in result, f"Unexpected error: {result.get('error')}"
    assert "Reached the standalone agent" in result.get("response", "")
    context_id = result.get("context_id")
    assert context_id

    app_mcp_sessions = _find_app_mcp_sessions(client, superuser_token_headers)
    matching = [s for s in app_mcp_sessions if s["id"] == context_id]
    assert len(matching) == 1, (
        f"Expected the real-routing session to exist, got {app_mcp_sessions}"
    )
    assert matching[0]["agent_id"] == agent["id"]
