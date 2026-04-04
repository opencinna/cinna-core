"""
MCPSessionMeta integration tests.

Verifies that the MCP session meta tracking feature works correctly:

  1. MCPSessionMeta is created when a new MCP session is created with an
     authenticated user ID — the authenticated user's email must appear in the
     session_context sent to agent-env.
  2. MCPSessionMeta is NOT created when authenticated_user_id is None — no
     mcp_user_email in session_context.
  3. MCPSessionMeta is NOT duplicated on session reuse — second call with the
     same context_id should reuse the session, not create a new meta record.
  4. session_context includes mcp_user_email for MCP sessions with meta — the
     MessageService enrichment passes the authenticated email to agent-env.
  5. session_context does NOT include mcp_user_email when no meta exists — for
     old/pre-existing MCP sessions without a meta record.

These tests call handle_send_message() directly (not through MCP protocol)
with mcp_authenticated_user_id_var set to simulate a token-verified user.

Uses the same service pipeline as test_mcp_send_message.py:
  - MessageService.create_message for user messages
  - MessageService.stream_message_with_events for agent streaming + storage
  - create_session() (patchable) for all DB access
"""
import asyncio
import json
import uuid
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlmodel import Session

from tests.stubs.agent_env_stub import StubAgentEnvConnector
from tests.utils.agent import create_agent_via_api, get_agent
from tests.utils.background_tasks import drain_tasks
from tests.utils.mcp import create_mcp_connector
from tests.utils.session import list_sessions
from tests.utils.user import create_random_user_with_headers


# ── Helpers ──────────────────────────────────────────────────────────────────


def _setup_agent_with_connector(
    client: TestClient,
    token_headers: dict[str, str],
    agent_name: str = "MCP Meta Agent",
    connector_name: str = "Meta Connector",
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
    authenticated_user_id: str | None = None,
) -> dict:
    """Call handle_send_message with an optional authenticated_user_id set via ContextVar.

    Patches agent_env_connector at the MessageService level (same as other MCP tests)
    so that streaming goes through the full MessageService.stream_message_with_events
    pipeline, which stores messages and builds session_context.

    Args:
        connector_id: MCP connector UUID string
        message: User message to send
        agent_env_stub: Stub for the agent environment
        mcp_session_id: Optional MCP transport session ID
        context_id: Optional context_id for per-chat session isolation
        authenticated_user_id: Optional UUID string — simulates the user ID set by
            MCPTokenVerifier after successful OAuth token verification.

    Returns:
        Parsed JSON dict with "response" and "context_id" (or "error" and "context_id")
    """
    from app.mcp.tools import handle_send_message
    from app.mcp.context_vars import (
        mcp_connector_id_var,
        mcp_session_id_var,
        mcp_authenticated_user_id_var,
    )

    async def _run():
        token_conn = mcp_connector_id_var.set(connector_id)
        token_sess = mcp_session_id_var.set(mcp_session_id)
        token_auth = mcp_authenticated_user_id_var.set(authenticated_user_id)
        try:
            return await handle_send_message(message, context_id=context_id)
        finally:
            mcp_connector_id_var.reset(token_conn)
            mcp_session_id_var.reset(token_sess)
            mcp_authenticated_user_id_var.reset(token_auth)

    with patch("app.services.sessions.message_service.agent_env_connector", agent_env_stub):
        result = asyncio.run(_run())
    drain_tasks()

    # Parse JSON response; fall back to raw string wrapped in error dict
    try:
        return json.loads(result)
    except (json.JSONDecodeError, TypeError):
        return {"response": result, "context_id": ""}


def _extract_session_context(stub: StubAgentEnvConnector) -> dict | None:
    """Extract session_context from the first stream call payload, or None if absent."""
    if not stub.stream_calls:
        return None
    payload = stub.stream_calls[0]["payload"]
    session_state = payload.get("session_state")
    if not session_state:
        return None
    return session_state.get("session_context")


# ── Tests ────────────────────────────────────────────────────────────────────


def test_mcp_session_meta_created_with_authenticated_user(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """
    MCPSessionMeta is created and mcp_user_email appears in session_context when
    a new MCP session is created with an authenticated_user_id:
      1. Create a second user (simulates the OAuth-authenticated caller)
      2. Create agent + connector owned by the superuser
      3. Call send_message with the second user's ID as authenticated_user_id
      4. Verify the agent received mcp_user_email in session_context
      5. Verify the email matches the authenticated user's email
    """
    # ── Phase 1: Create a user whose identity will be tracked ─────────────
    auth_user, auth_headers = create_random_user_with_headers(client)
    auth_user_email = auth_user["email"]
    auth_user_id = auth_user["id"]

    # ── Phase 2: Create agent + connector owned by superuser ──────────────
    agent, connector = _setup_agent_with_connector(
        client, superuser_token_headers,
        agent_name="Meta Creation Agent",
        connector_name="Meta Creation Connector",
    )
    connector_id = connector["id"]

    # ── Phase 3: Send message with authenticated_user_id set ──────────────
    stub = StubAgentEnvConnector(response_text="Hello, authenticated user!")
    result = _run_send_message(
        connector_id, "Hello", stub,
        authenticated_user_id=auth_user_id,
    )

    assert "Hello, authenticated user!" in result["response"]
    assert result["context_id"], "context_id should be non-empty"

    # ── Phase 4: Verify mcp_user_email in session_context ─────────────────
    ctx = _extract_session_context(stub)
    assert ctx is not None, "session_context must be present in payload"
    assert "mcp_user_email" in ctx, (
        f"mcp_user_email not found in session_context. Keys: {list(ctx.keys())}"
    )

    # ── Phase 5: Email matches the authenticated user ─────────────────────
    assert ctx["mcp_user_email"] == auth_user_email, (
        f"Expected mcp_user_email={auth_user_email!r}, got {ctx['mcp_user_email']!r}"
    )

    # ── Also verify basic MCP session fields are intact ───────────────────
    assert ctx["integration_type"] == "mcp"
    mcp_sessions = _find_mcp_sessions(client, superuser_token_headers, connector_id)
    assert len(mcp_sessions) == 1


def test_mcp_session_meta_not_created_without_authenticated_user(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """
    MCPSessionMeta is NOT created when authenticated_user_id is None:
      1. Create agent + connector
      2. Call send_message WITHOUT authenticated_user_id (None)
      3. Verify mcp_user_email is absent from session_context
      4. Verify a session was still created (integration still works)
    """
    # ── Phase 1: Create agent + connector ─────────────────────────────────
    agent, connector = _setup_agent_with_connector(
        client, superuser_token_headers,
        agent_name="No Meta Agent",
        connector_name="No Meta Connector",
    )
    connector_id = connector["id"]

    # ── Phase 2: Send message without authenticated_user_id ───────────────
    stub = StubAgentEnvConnector(response_text="Anonymous response")
    result = _run_send_message(
        connector_id, "Hello", stub,
        authenticated_user_id=None,  # No OAuth user
    )

    assert "Anonymous response" in result["response"]

    # ── Phase 3: mcp_user_email must NOT be in session_context ────────────
    ctx = _extract_session_context(stub)
    assert ctx is not None, "session_context must be present in payload"
    assert "mcp_user_email" not in ctx, (
        f"mcp_user_email should be absent when no authenticated user. Got: {ctx}"
    )

    # ── Phase 4: Session was still created normally ────────────────────────
    mcp_sessions = _find_mcp_sessions(client, superuser_token_headers, connector_id)
    assert len(mcp_sessions) == 1, (
        f"Expected 1 MCP session even without authenticated_user_id, got {len(mcp_sessions)}"
    )
    assert ctx["integration_type"] == "mcp"


def test_mcp_session_meta_not_duplicated_on_session_reuse(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """
    MCPSessionMeta is NOT duplicated when the session is reused via context_id:
      1. Create agent + connector
      2. First send_message with authenticated_user_id → session + meta created
      3. Second send_message with same context_id and same authenticated_user_id
         → session reused, no new meta record (verified by email still present)
      4. Verify only one platform session exists (reuse confirmed)
      5. Verify mcp_user_email is still correct in second call
    """
    # ── Phase 1: Create user + agent + connector ───────────────────────────
    auth_user, _ = create_random_user_with_headers(client)
    auth_user_email = auth_user["email"]
    auth_user_id = auth_user["id"]

    agent, connector = _setup_agent_with_connector(
        client, superuser_token_headers,
        agent_name="Meta No Duplicate Agent",
        connector_name="Meta No Duplicate Connector",
    )
    connector_id = connector["id"]

    # ── Phase 2: First message → session + meta created ───────────────────
    stub1 = StubAgentEnvConnector(response_text="First response")
    result1 = _run_send_message(
        connector_id, "First message", stub1,
        authenticated_user_id=auth_user_id,
    )
    ctx_id = result1["context_id"]
    assert ctx_id, "First response must include context_id"

    ctx1 = _extract_session_context(stub1)
    assert ctx1 is not None
    assert ctx1["mcp_user_email"] == auth_user_email

    # ── Phase 3: Second message with same context_id → session reused ─────
    stub2 = StubAgentEnvConnector(response_text="Second response")
    result2 = _run_send_message(
        connector_id, "Second message", stub2,
        context_id=ctx_id,
        authenticated_user_id=auth_user_id,
    )
    assert result2["context_id"] == ctx_id, "context_id must be preserved on reuse"

    # ── Phase 4: Only one session exists (reuse confirmed) ────────────────
    mcp_sessions = _find_mcp_sessions(client, superuser_token_headers, connector_id)
    assert len(mcp_sessions) == 1, (
        f"Expected 1 session (reused), got {len(mcp_sessions)}"
    )

    # ── Phase 5: mcp_user_email still present on second call ──────────────
    # (email is looked up fresh from MCPSessionMeta for each message)
    ctx2 = _extract_session_context(stub2)
    assert ctx2 is not None
    assert "mcp_user_email" in ctx2, (
        "mcp_user_email should still be in session_context on session reuse"
    )
    assert ctx2["mcp_user_email"] == auth_user_email, (
        f"Email mismatch on reuse: expected {auth_user_email!r}, got {ctx2['mcp_user_email']!r}"
    )


def test_mcp_session_context_includes_email_for_mcp_sessions(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """
    session_context includes mcp_user_email for MCP integration sessions that have
    an MCPSessionMeta record. The MessageService enrichment fetches the email from
    the MCPSessionMeta table and adds it to session_context:
      1. Create agent + connector
      2. Create an authenticated user
      3. Send message with authenticated_user_id → MCPSessionMeta created
      4. Send a SECOND message in the same session (to verify re-enrichment)
      5. Both stream calls must carry mcp_user_email in session_context
    """
    # ── Phase 1: Setup ────────────────────────────────────────────────────
    auth_user, _ = create_random_user_with_headers(client)
    auth_user_email = auth_user["email"]
    auth_user_id = auth_user["id"]

    agent, connector = _setup_agent_with_connector(
        client, superuser_token_headers,
        agent_name="Context Enrichment Agent",
        connector_name="Context Enrichment Connector",
    )
    connector_id = connector["id"]

    # ── Phase 2: First message ────────────────────────────────────────────
    stub1 = StubAgentEnvConnector(response_text="First enriched response")
    result1 = _run_send_message(
        connector_id, "First question", stub1,
        authenticated_user_id=auth_user_id,
    )
    assert result1["context_id"], "must get context_id"
    ctx_id = result1["context_id"]

    # Verify first stream call has mcp_user_email
    ctx1 = _extract_session_context(stub1)
    assert ctx1 is not None
    assert ctx1.get("mcp_user_email") == auth_user_email, (
        f"First message: mcp_user_email missing or wrong. session_context={ctx1}"
    )
    assert ctx1["integration_type"] == "mcp"

    # ── Phase 3: Second message in same session ───────────────────────────
    stub2 = StubAgentEnvConnector(response_text="Second enriched response")
    result2 = _run_send_message(
        connector_id, "Follow-up question", stub2,
        context_id=ctx_id,
        # No authenticated_user_id this time — email comes from stored MCPSessionMeta
    )
    assert result2["context_id"] == ctx_id

    # Verify second stream call also has mcp_user_email from stored MCPSessionMeta
    ctx2 = _extract_session_context(stub2)
    assert ctx2 is not None
    assert ctx2.get("mcp_user_email") == auth_user_email, (
        f"Second message: mcp_user_email missing or wrong. session_context={ctx2}"
    )


def test_mcp_session_context_no_email_without_meta(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """
    session_context does NOT include mcp_user_email when no MCPSessionMeta exists
    (old/pre-existing MCP sessions that were created before this feature):
      1. Create agent + connector
      2. Send message WITHOUT authenticated_user_id → no MCPSessionMeta created
      3. Send second message in same session
      4. Both calls must NOT have mcp_user_email in session_context
    """
    # ── Phase 1: Create agent + connector ─────────────────────────────────
    agent, connector = _setup_agent_with_connector(
        client, superuser_token_headers,
        agent_name="No Email Context Agent",
        connector_name="No Email Context Connector",
    )
    connector_id = connector["id"]

    # ── Phase 2: First message without authenticated user ─────────────────
    stub1 = StubAgentEnvConnector(response_text="First unenriched response")
    result1 = _run_send_message(
        connector_id, "First message", stub1,
        authenticated_user_id=None,
    )
    ctx_id = result1["context_id"]

    ctx1 = _extract_session_context(stub1)
    assert ctx1 is not None
    assert "mcp_user_email" not in ctx1, (
        f"mcp_user_email should be absent with no meta. Got: {ctx1}"
    )

    # ── Phase 3: Second message in same session ───────────────────────────
    stub2 = StubAgentEnvConnector(response_text="Second unenriched response")
    result2 = _run_send_message(
        connector_id, "Second message", stub2,
        context_id=ctx_id,
        authenticated_user_id=None,
    )
    assert result2["context_id"] == ctx_id

    ctx2 = _extract_session_context(stub2)
    assert ctx2 is not None
    assert "mcp_user_email" not in ctx2, (
        f"mcp_user_email should remain absent on reuse with no meta. Got: {ctx2}"
    )

    # Only one session created
    mcp_sessions = _find_mcp_sessions(client, superuser_token_headers, connector_id)
    assert len(mcp_sessions) == 1


def test_mcp_session_meta_owner_vs_authenticated_user(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """
    When the OAuth-authenticated user differs from the connector owner, the
    mcp_user_email in session_context reflects the authenticated user (not the owner):
      1. Connector is owned by the superuser
      2. A different user (auth_user) authenticates via OAuth
      3. send_message is called with auth_user's ID as authenticated_user_id
      4. session_context["mcp_user_email"] == auth_user's email (not superuser's)
      5. The platform session's user_id is still the owner's (connector.owner_id)
    """
    # ── Phase 1: Create non-owner user ────────────────────────────────────
    auth_user, _ = create_random_user_with_headers(client)
    auth_user_email = auth_user["email"]
    auth_user_id = auth_user["id"]

    # ── Phase 2: Connector owned by superuser ─────────────────────────────
    agent, connector = _setup_agent_with_connector(
        client, superuser_token_headers,
        agent_name="Owner vs Auth Agent",
        connector_name="Owner vs Auth Connector",
    )
    connector_id = connector["id"]

    # ── Phase 3: Send as auth_user (not the owner) ────────────────────────
    stub = StubAgentEnvConnector(response_text="Response to delegated user")
    result = _run_send_message(
        connector_id, "Hello from delegate", stub,
        authenticated_user_id=auth_user_id,
    )

    assert result["context_id"], "must get context_id"

    # ── Phase 4: mcp_user_email is auth_user's email ──────────────────────
    ctx = _extract_session_context(stub)
    assert ctx is not None
    assert ctx.get("mcp_user_email") == auth_user_email, (
        f"Expected auth_user email {auth_user_email!r} in mcp_user_email, "
        f"got {ctx.get('mcp_user_email')!r}"
    )

    # ── Phase 5: Session still associated with connector owner (user_id) ──
    mcp_sessions = _find_mcp_sessions(client, superuser_token_headers, connector_id)
    assert len(mcp_sessions) == 1
    session = mcp_sessions[0]
    # The session user_id is the connector owner (superuser), NOT auth_user
    assert session["user_id"] != auth_user_id, (
        "session.user_id should be the connector owner, not the authenticated user"
    )


def test_mcp_session_meta_invalid_user_id_does_not_crash(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """
    An invalid/nonexistent authenticated_user_id does not crash send_message.
    The MCPSessionMeta creation silently skips when the user is not found.
    The session is created and the tool returns a valid response:
      1. Use a random UUID that no user has
      2. Verify send_message still returns a valid response
      3. Verify session was created
      4. Verify mcp_user_email is absent (no meta was created)
    """
    agent, connector = _setup_agent_with_connector(
        client, superuser_token_headers,
        agent_name="Invalid Auth User Agent",
        connector_name="Invalid Auth User Connector",
    )
    connector_id = connector["id"]

    # ── Phase 1: Use a random nonexistent UUID ─────────────────────────────
    nonexistent_user_id = str(uuid.uuid4())

    stub = StubAgentEnvConnector(response_text="Still works with invalid user")
    result = _run_send_message(
        connector_id, "Hello", stub,
        authenticated_user_id=nonexistent_user_id,
    )

    # ── Phase 2: Tool returned a valid response (no crash) ────────────────
    assert "Still works with invalid user" in result["response"], (
        f"Tool should still work even with invalid user ID. Got: {result}"
    )
    assert result["context_id"], "context_id should be non-empty"

    # ── Phase 3: Session was created normally ─────────────────────────────
    mcp_sessions = _find_mcp_sessions(client, superuser_token_headers, connector_id)
    assert len(mcp_sessions) == 1

    # ── Phase 4: mcp_user_email absent (user not found → meta not created) ─
    ctx = _extract_session_context(stub)
    assert ctx is not None
    assert "mcp_user_email" not in ctx, (
        f"mcp_user_email should be absent when user lookup fails. Got: {ctx}"
    )
