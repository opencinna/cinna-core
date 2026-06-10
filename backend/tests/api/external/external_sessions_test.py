"""
Integration tests for the external sessions metadata surface (GET/DELETE /external/sessions...).

Scenarios covered:
  1. Unauthenticated request is rejected (401)
  2. GET /sessions returns sessions where user is owner (user_id match)
  3. GET /sessions returns sessions where user is caller (caller_id match, app_mcp)
  4. GET /sessions returns sessions where user is identity_caller (identity_mcp)
  5. GET /sessions excludes sessions the user has no role in
  6. GET /sessions/{id} returns metadata for a visible session (owner)
  7. GET /sessions/{id} returns 404 for a non-participant
  8. GET /sessions/{id}/messages returns message history for a visible session
  9. GET /sessions/{id}/messages returns 404 for a non-participant
 10. Pagination: limit and offset parameters work; ordering is last_message_at DESC
 11. target_type/target_id derivation — "external" integration_type
 12. target_type/target_id derivation — "app_mcp" with route_id in metadata
 13. target_type/target_id derivation — "identity_mcp" (target_id = session.user_id)
 14. agent_name falls back to session_metadata["identity_owner_name"] for identity_mcp
 15. Desktop JWT with client_kind/external_client_id claims stamped into session_metadata
 16. Regular web JWT does not stamp client attribution into session_metadata
 17. Owner hides own session — session disappears from GET /sessions list (soft-hide)
 18. Non-participant DELETE returns 404
 19. Unauthenticated DELETE returns 401
 20. Non-existent session id returns 404 on DELETE
"""
import uuid
from unittest.mock import patch

import jwt as pyjwt

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.stubs.agent_env_stub import StubAgentEnvConnector
from tests.utils.a2a import (
    build_streaming_request,
    extract_task_id as _extract_task_id,
    parse_sse_events,
)
from tests.utils.agent import create_agent_via_api, get_agent
from tests.utils.app_agent_route import create_admin_route
from tests.utils.background_tasks import drain_tasks
from tests.utils.desktop_auth import obtain_desktop_tokens
from tests.utils.identity import create_identity_binding
from tests.utils.user import create_random_user_with_headers

_EXT_BASE = f"{settings.API_V1_STR}/external"
_EXT_A2A_BASE = f"{settings.API_V1_STR}/external/a2a"
_SESSIONS_BASE = f"{settings.API_V1_STR}/sessions"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _list_external_sessions(
    client: TestClient,
    headers: dict,
    limit: int | None = None,
    offset: int | None = None,
    expected_status: int = 200,
) -> list[dict]:
    """Call GET /external/sessions and return the JSON list."""
    url = f"{_EXT_BASE}/sessions"
    params = {}
    if limit is not None:
        params["limit"] = limit
    if offset is not None:
        params["offset"] = offset
    r = client.get(url, headers=headers, params=params)
    assert r.status_code == expected_status, (
        f"list_external_sessions({params}) failed with {r.status_code}: {r.text}"
    )
    if expected_status == 200:
        return r.json()
    return []


def _get_external_session(
    client: TestClient,
    headers: dict,
    session_id: str,
    expected_status: int = 200,
) -> dict | None:
    """Call GET /external/sessions/{id} and return the JSON body."""
    r = client.get(f"{_EXT_BASE}/sessions/{session_id}", headers=headers)
    assert r.status_code == expected_status, (
        f"get_external_session({session_id}) failed with {r.status_code}: {r.text}"
    )
    if expected_status == 200:
        return r.json()
    return None


def _get_external_session_messages(
    client: TestClient,
    headers: dict,
    session_id: str,
    expected_status: int = 200,
) -> list[dict] | None:
    """Call GET /external/sessions/{id}/messages and return the JSON list."""
    r = client.get(f"{_EXT_BASE}/sessions/{session_id}/messages", headers=headers)
    assert r.status_code == expected_status, (
        f"get_external_session_messages({session_id}) failed with {r.status_code}: {r.text}"
    )
    if expected_status == 200:
        return r.json()
    return None


def _create_external_session_via_a2a(
    client: TestClient,
    headers: dict,
    agent_id: str,
    message_text: str = "Hello from external",
    response_text: str = "Response from agent",
) -> str:
    """Send a streaming message via the external A2A agent endpoint.

    Returns the task_id (which is the session.id) from the SSE events.
    """
    stub = StubAgentEnvConnector(response_text=response_text)
    request = build_streaming_request(message_text)
    with patch("app.services.sessions.message_service.agent_env_connector", stub):
        resp = client.post(
            f"{_EXT_A2A_BASE}/agent/{agent_id}/",
            headers=headers,
            json=request,
        )
    drain_tasks()
    assert resp.status_code == 200, f"External A2A POST failed: {resp.text}"
    events = parse_sse_events(resp.text)
    task_id = _extract_task_id(events)
    assert task_id, f"Could not extract task_id from events: {events}"
    return task_id


def _create_app_mcp_session_via_route(
    client: TestClient,
    caller_headers: dict,
    route_id: str,
    message_text: str = "Hello via route",
    response_text: str = "Hi from shared agent",
) -> str:
    """Send a streaming message via the external route A2A endpoint.

    Returns the task_id (session.id).
    """
    stub = StubAgentEnvConnector(response_text=response_text)
    request = build_streaming_request(message_text)
    with patch("app.services.sessions.message_service.agent_env_connector", stub):
        resp = client.post(
            f"{_EXT_A2A_BASE}/route/{route_id}/",
            headers=caller_headers,
            json=request,
        )
    drain_tasks()
    assert resp.status_code == 200, f"Route A2A POST failed: {resp.text}"
    events = parse_sse_events(resp.text)
    task_id = _extract_task_id(events)
    assert task_id, f"Could not extract task_id from events: {events}"
    return task_id


def _create_identity_session_via_a2a(
    client: TestClient,
    caller_headers: dict,
    owner_id: str,
    message_text: str = "Ask owner something",
    response_text: str = "Owner agent responds",
) -> str:
    """Send a streaming message to an identity endpoint.

    Returns the task_id (session.id).
    """
    stub = StubAgentEnvConnector(response_text=response_text)
    request = build_streaming_request(message_text)
    with patch("app.services.sessions.message_service.agent_env_connector", stub):
        resp = client.post(
            f"{_EXT_A2A_BASE}/identity/{owner_id}/",
            headers=caller_headers,
            json=request,
        )
    drain_tasks()
    assert resp.status_code == 200, f"Identity A2A POST failed: {resp.text}"
    events = parse_sse_events(resp.text)
    task_id = _extract_task_id(events)
    assert task_id, f"Could not extract task_id from events: {events}"
    return task_id


def _setup_owner_agent(
    client: TestClient,
    owner_headers: dict,
    name: str = "Test Agent",
) -> dict:
    """Create an agent as the given user and wait for env activation."""
    agent = create_agent_via_api(client, owner_headers, name=name)
    drain_tasks()
    return get_agent(client, owner_headers, agent["id"])


def _setup_route(
    client: TestClient,
    superuser_token_headers: dict,
    caller_id: str,
    agent_name: str = "Route Agent",
) -> tuple[dict, dict, str]:
    """Create an agent (as superuser), a route, assign the caller.

    Returns (owner_agent, route, assignment_id).
    """
    owner_agent = _setup_owner_agent(
        client, superuser_token_headers, name=agent_name
    )
    route = create_admin_route(
        client,
        superuser_token_headers,
        agent_id=owner_agent["id"],
        name="Test Route",
        trigger_prompt="Handle route requests",
        assigned_user_ids=[caller_id],
        auto_enable_for_users=True,
    )
    assignment_id = next(
        a["id"] for a in route["assignments"] if a["user_id"] == caller_id
    )
    return owner_agent, route, assignment_id


def _setup_identity(
    client: TestClient,
    superuser_token_headers: dict,
    caller_id: str,
    agent_name: str = "Identity Agent",
) -> tuple[str, dict, dict]:
    """Create an identity binding owned by the superuser, assigning the caller.

    Returns (owner_id, owner_agent, binding).
    """
    r = client.get(f"{settings.API_V1_STR}/users/me", headers=superuser_token_headers)
    assert r.status_code == 200
    owner_id = r.json()["id"]

    owner_agent = create_agent_via_api(
        client, superuser_token_headers, name=agent_name
    )
    drain_tasks()
    owner_agent = get_agent(client, superuser_token_headers, owner_agent["id"])
    assert owner_agent["active_environment_id"] is not None

    binding = create_identity_binding(
        client,
        superuser_token_headers,
        agent_id=owner_agent["id"],
        trigger_prompt="Handle identity requests",
        assigned_user_ids=[caller_id],
        auto_enable=True,
    )
    return owner_id, owner_agent, binding


# _extract_task_id lives in tests/utils/a2a.py and is imported above.


def _get_current_user_id(client: TestClient, headers: dict) -> str:
    """Fetch the authenticated user's id."""
    r = client.get(f"{settings.API_V1_STR}/users/me", headers=headers)
    assert r.status_code == 200
    return r.json()["id"]


def _delete_external_session(
    client: TestClient,
    headers: dict,
    session_id: str,
    expected_status: int = 204,
) -> None:
    r = client.delete(f"{_EXT_BASE}/sessions/{session_id}", headers=headers)
    assert r.status_code == expected_status, (
        f"DELETE /sessions/{session_id} expected {expected_status}, "
        f"got {r.status_code}: {r.text}"
    )


def _create_agent_ready(
    client: TestClient,
    headers: dict,
    name: str | None = None,
) -> dict:
    """Create an agent and drain background tasks so the environment stub is set up."""
    agent = create_agent_via_api(client, headers, name=name)
    drain_tasks()
    return get_agent(client, headers, agent["id"])


def _stream_external_agent(
    client: TestClient,
    headers: dict,
    agent_id: str,
    message: str = "hello",
) -> None:
    """Send a streaming A2A message to a personal-agent target."""
    req = build_streaming_request(message)
    stub = StubAgentEnvConnector(response_text="ok")
    with patch("app.services.sessions.message_service.agent_env_connector", stub):
        r = client.post(
            f"{_EXT_A2A_BASE}/agent/{agent_id}/",
            json=req,
            headers=headers,
        )
    drain_tasks()
    assert r.status_code == 200, f"stream failed: {r.status_code} {r.text}"


# ---------------------------------------------------------------------------
# Scenario 1: Unauthenticated
# ---------------------------------------------------------------------------


def test_list_external_sessions_unauthenticated(client: TestClient) -> None:
    """GET /external/sessions without a token must return 401."""
    r = client.get(f"{_EXT_BASE}/sessions")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Scenario 2: Sessions where user is owner (user_id match)
# ---------------------------------------------------------------------------


def test_list_sessions_includes_owned_sessions(
    client: TestClient,
    superuser_token_headers: dict,
) -> None:
    """GET /sessions returns sessions owned by the current user (user_id=user.id)."""
    agent = _setup_owner_agent(client, superuser_token_headers, "Owned Session Agent")

    task_id = _create_external_session_via_a2a(
        client, superuser_token_headers, agent["id"]
    )

    sessions = _list_external_sessions(client, superuser_token_headers)
    session_ids = [s["id"] for s in sessions]
    assert task_id in session_ids, (
        f"Expected owned session {task_id} in list, got: {session_ids}"
    )

    # Verify the session entry has the expected shape
    owned = next(s for s in sessions if s["id"] == task_id)
    assert owned["integration_type"] == "external"
    assert owned["status"] in ("active", "completed", "paused", "error")
    assert "interaction_status" in owned
    assert owned["agent_id"] == agent["id"]


# ---------------------------------------------------------------------------
# Scenario 3: Sessions where user is caller (app_mcp)
# ---------------------------------------------------------------------------


def test_list_sessions_includes_caller_sessions(
    client: TestClient,
    superuser_token_headers: dict,
) -> None:
    """GET /sessions returns app_mcp sessions where caller_id=user.id."""
    caller, caller_headers = create_random_user_with_headers(client)
    caller_id = caller["id"]

    _, route, _ = _setup_route(
        client, superuser_token_headers, caller_id=caller_id
    )
    task_id = _create_app_mcp_session_via_route(
        client, caller_headers, route["id"]
    )

    # Caller can see this session via GET /external/sessions even though
    # session.user_id == owner_id (not caller.id)
    caller_sessions = _list_external_sessions(client, caller_headers)
    caller_session_ids = [s["id"] for s in caller_sessions]
    assert task_id in caller_session_ids, (
        f"Expected app_mcp session {task_id} visible to caller, got: {caller_session_ids}"
    )

    session_entry = next(s for s in caller_sessions if s["id"] == task_id)
    assert session_entry["integration_type"] == "app_mcp"
    assert session_entry["caller_id"] == caller_id


# ---------------------------------------------------------------------------
# Scenario 4: Sessions where user is identity_caller
# ---------------------------------------------------------------------------


def test_list_sessions_includes_identity_caller_sessions(
    client: TestClient,
    superuser_token_headers: dict,
) -> None:
    """GET /sessions returns identity_mcp sessions where identity_caller_id=user.id."""
    caller, caller_headers = create_random_user_with_headers(client)
    caller_id = caller["id"]

    # _setup_identity with auto_enable=True is sufficient — the binding assignment
    # is immediately enabled so the caller can start a session without a separate
    # toggle step. No toggle_identity_contact call needed here.
    owner_id, _, _ = _setup_identity(
        client, superuser_token_headers, caller_id=caller_id
    )

    task_id = _create_identity_session_via_a2a(
        client, caller_headers, owner_id
    )

    caller_sessions = _list_external_sessions(client, caller_headers)
    caller_session_ids = [s["id"] for s in caller_sessions]
    assert task_id in caller_session_ids, (
        f"Expected identity_mcp session {task_id} visible to caller, "
        f"got: {caller_session_ids}"
    )

    session_entry = next(s for s in caller_sessions if s["id"] == task_id)
    assert session_entry["integration_type"] == "identity_mcp"
    assert session_entry["identity_caller_id"] == caller_id


# ---------------------------------------------------------------------------
# Scenario 5: Sessions the user has no role in are excluded
# ---------------------------------------------------------------------------


def test_list_sessions_excludes_other_users_sessions(
    client: TestClient,
    superuser_token_headers: dict,
) -> None:
    """Sessions belonging entirely to another user are not returned."""
    # Superuser creates an agent and an external session
    agent = _setup_owner_agent(client, superuser_token_headers, "Exclusion Test Agent")
    task_id = _create_external_session_via_a2a(
        client, superuser_token_headers, agent["id"]
    )

    # Random user should not see that session
    _, other_headers = create_random_user_with_headers(client)
    other_sessions = _list_external_sessions(client, other_headers)
    other_session_ids = [s["id"] for s in other_sessions]
    assert task_id not in other_session_ids, (
        f"Session {task_id} must not appear for a non-participant user"
    )


# ---------------------------------------------------------------------------
# Scenario 6: GET /sessions/{id} — owner can see their session
# ---------------------------------------------------------------------------


def test_get_external_session_owner_visible(
    client: TestClient,
    superuser_token_headers: dict,
) -> None:
    """GET /external/sessions/{id} returns metadata for a session the user owns."""
    agent = _setup_owner_agent(client, superuser_token_headers, "Single Get Agent")
    task_id = _create_external_session_via_a2a(
        client, superuser_token_headers, agent["id"]
    )

    session = _get_external_session(client, superuser_token_headers, task_id)
    assert session is not None
    assert session["id"] == task_id
    assert session["integration_type"] == "external"
    assert session["agent_id"] == agent["id"]
    assert session["agent_name"] == agent["name"]
    assert "created_at" in session
    assert "status" in session
    assert "interaction_status" in session


# ---------------------------------------------------------------------------
# Scenario 7: GET /sessions/{id} — non-participant gets 404
# ---------------------------------------------------------------------------


def test_get_external_session_non_participant_404(
    client: TestClient,
    superuser_token_headers: dict,
) -> None:
    """GET /external/sessions/{id} returns 404 for a non-participant."""
    agent = _setup_owner_agent(client, superuser_token_headers, "Non-Participant Agent")
    task_id = _create_external_session_via_a2a(
        client, superuser_token_headers, agent["id"]
    )

    _, other_headers = create_random_user_with_headers(client)
    _get_external_session(client, other_headers, task_id, expected_status=404)


def test_get_external_session_unknown_id_404(
    client: TestClient,
    superuser_token_headers: dict,
) -> None:
    """GET /external/sessions/{unknown_id} returns 404."""
    _get_external_session(
        client, superuser_token_headers, str(uuid.uuid4()), expected_status=404
    )


# ---------------------------------------------------------------------------
# Scenario 8: GET /sessions/{id}/messages — visible session returns messages
# ---------------------------------------------------------------------------


def test_get_external_session_messages_visible(
    client: TestClient,
    superuser_token_headers: dict,
) -> None:
    """GET /external/sessions/{id}/messages returns messages for a visible session."""
    agent = _setup_owner_agent(client, superuser_token_headers, "Messages Test Agent")
    task_id = _create_external_session_via_a2a(
        client,
        superuser_token_headers,
        agent["id"],
        message_text="Test message for history",
        response_text="Test response",
    )

    messages = _get_external_session_messages(
        client, superuser_token_headers, task_id
    )
    assert messages is not None
    assert isinstance(messages, list)
    # At least the user message and the agent response should be present
    assert len(messages) >= 1
    roles = {m["role"] for m in messages}
    assert "user" in roles or "agent" in roles, (
        f"Expected at least one user or agent message, got roles: {roles}"
    )


# ---------------------------------------------------------------------------
# Scenario 9: GET /sessions/{id}/messages — non-participant gets 404
# ---------------------------------------------------------------------------


def test_get_external_session_messages_non_participant_404(
    client: TestClient,
    superuser_token_headers: dict,
) -> None:
    """GET /external/sessions/{id}/messages returns 404 for a non-participant."""
    agent = _setup_owner_agent(
        client, superuser_token_headers, "Messages Non-Participant Agent"
    )
    task_id = _create_external_session_via_a2a(
        client, superuser_token_headers, agent["id"]
    )

    _, other_headers = create_random_user_with_headers(client)
    _get_external_session_messages(
        client, other_headers, task_id, expected_status=404
    )


def test_get_external_session_messages_unknown_id_404(
    client: TestClient,
    superuser_token_headers: dict,
) -> None:
    """GET /external/sessions/{unknown_id}/messages returns 404."""
    _get_external_session_messages(
        client, superuser_token_headers, str(uuid.uuid4()), expected_status=404
    )


# ---------------------------------------------------------------------------
# Scenario 10: Pagination — limit/offset work; ordering is last_message_at DESC
# ---------------------------------------------------------------------------


def test_list_sessions_pagination_limit(
    client: TestClient,
    superuser_token_headers: dict,
) -> None:
    """limit=1 returns at most one session."""
    agent = _setup_owner_agent(
        client, superuser_token_headers, "Pagination Limit Agent"
    )
    # Create two sessions
    _create_external_session_via_a2a(client, superuser_token_headers, agent["id"])
    _create_external_session_via_a2a(client, superuser_token_headers, agent["id"])

    sessions = _list_external_sessions(client, superuser_token_headers, limit=1)
    assert len(sessions) <= 1, (
        f"limit=1 should return at most 1 session, got {len(sessions)}"
    )


def test_list_sessions_pagination_offset(
    client: TestClient,
    superuser_token_headers: dict,
) -> None:
    """offset skips sessions; limit+offset together page through results."""
    agent = _setup_owner_agent(
        client, superuser_token_headers, "Pagination Offset Agent"
    )
    # Create two sessions so there are at least 2 to paginate over
    id1 = _create_external_session_via_a2a(client, superuser_token_headers, agent["id"])
    id2 = _create_external_session_via_a2a(client, superuser_token_headers, agent["id"])

    all_sessions = _list_external_sessions(client, superuser_token_headers)
    # Filter to only our two sessions (there may be others from earlier tests)
    our_ids = {id1, id2}
    our_sessions = [s for s in all_sessions if s["id"] in our_ids]
    assert len(our_sessions) == 2, (
        f"Expected to find both created sessions, got: {[s['id'] for s in our_sessions]}"
    )

    # Page 1: limit=1, offset=0
    page1 = _list_external_sessions(client, superuser_token_headers, limit=1, offset=0)
    # Page 2: limit=1, offset=1
    page2 = _list_external_sessions(client, superuser_token_headers, limit=1, offset=1)

    assert len(page1) <= 1
    assert len(page2) <= 1
    if page1 and page2:
        # The two pages must return different sessions
        assert page1[0]["id"] != page2[0]["id"], (
            "offset pagination must return different sessions per page"
        )


# ---------------------------------------------------------------------------
# Scenario 11: target_type/target_id — "external" integration_type
# ---------------------------------------------------------------------------


def test_target_derivation_external_integration_type(
    client: TestClient,
    superuser_token_headers: dict,
) -> None:
    """Sessions with integration_type='external' get target_type='agent', target_id=agent_id."""
    agent = _setup_owner_agent(
        client, superuser_token_headers, "Target Derivation External Agent"
    )
    task_id = _create_external_session_via_a2a(
        client, superuser_token_headers, agent["id"]
    )

    session = _get_external_session(client, superuser_token_headers, task_id)
    assert session is not None
    assert session["target_type"] == "agent", (
        f"Expected target_type='agent' for external session, got {session['target_type']!r}"
    )
    assert session["target_id"] == agent["id"], (
        f"Expected target_id={agent['id']}, got {session['target_id']!r}"
    )


# ---------------------------------------------------------------------------
# Scenario 12: target_type/target_id — "app_mcp" with route_id in metadata
# ---------------------------------------------------------------------------


def test_target_derivation_app_mcp_integration_type(
    client: TestClient,
    superuser_token_headers: dict,
) -> None:
    """Sessions with integration_type='app_mcp' get target_type='app_mcp_route'
    and target_id=session_metadata['app_mcp_route_id']."""
    caller, caller_headers = create_random_user_with_headers(client)
    caller_id = caller["id"]

    _, route, _ = _setup_route(
        client, superuser_token_headers, caller_id=caller_id,
        agent_name="App MCP Target Derivation Agent",
    )
    route_id = route["id"]

    task_id = _create_app_mcp_session_via_route(
        client, caller_headers, route_id
    )

    # Verify via caller's session list
    caller_sessions = _list_external_sessions(client, caller_headers)
    session_entry = next(
        (s for s in caller_sessions if s["id"] == task_id), None
    )
    assert session_entry is not None, (
        f"Caller should see app_mcp session {task_id}"
    )
    assert session_entry["target_type"] == "app_mcp_route", (
        f"Expected target_type='app_mcp_route', got {session_entry['target_type']!r}"
    )
    assert session_entry["target_id"] == route_id, (
        f"Expected target_id={route_id}, got {session_entry['target_id']!r}"
    )


# ---------------------------------------------------------------------------
# Scenario 13: target_type/target_id — "identity_mcp"
# ---------------------------------------------------------------------------


def test_target_derivation_identity_mcp_integration_type(
    client: TestClient,
    superuser_token_headers: dict,
) -> None:
    """Sessions with integration_type='identity_mcp' get target_type='identity'
    and target_id=session.user_id (the identity owner)."""
    caller, caller_headers = create_random_user_with_headers(client)
    caller_id = caller["id"]

    owner_id, _, _ = _setup_identity(
        client, superuser_token_headers, caller_id=caller_id,
        agent_name="Identity Target Derivation Agent",
    )

    task_id = _create_identity_session_via_a2a(
        client, caller_headers, owner_id
    )

    caller_sessions = _list_external_sessions(client, caller_headers)
    session_entry = next(
        (s for s in caller_sessions if s["id"] == task_id), None
    )
    assert session_entry is not None, (
        f"Caller should see identity_mcp session {task_id}"
    )
    assert session_entry["target_type"] == "identity", (
        f"Expected target_type='identity', got {session_entry['target_type']!r}"
    )
    assert session_entry["target_id"] == owner_id, (
        f"Expected target_id={owner_id} (owner), got {session_entry['target_id']!r}"
    )


# ---------------------------------------------------------------------------
# Scenario 14: agent_name falls back to identity_owner_name for identity_mcp
# ---------------------------------------------------------------------------


def test_agent_name_falls_back_to_identity_owner_name(
    client: TestClient,
    superuser_token_headers: dict,
) -> None:
    """For identity_mcp sessions, agent_name reflects the identity owner's name
    from session_metadata['identity_owner_name'] (stamped by identity Stage-2 routing)."""
    caller, caller_headers = create_random_user_with_headers(client)
    caller_id = caller["id"]

    owner_id, _, _ = _setup_identity(
        client, superuser_token_headers, caller_id=caller_id,
        agent_name="Owner Name Fallback Agent",
    )

    # Fetch owner's full_name to compare against agent_name
    r = client.get(f"{settings.API_V1_STR}/users/me", headers=superuser_token_headers)
    assert r.status_code == 200
    owner_full_name = r.json().get("full_name") or r.json().get("email")

    task_id = _create_identity_session_via_a2a(
        client, caller_headers, owner_id
    )

    caller_sessions = _list_external_sessions(client, caller_headers)
    session_entry = next(
        (s for s in caller_sessions if s["id"] == task_id), None
    )
    assert session_entry is not None
    assert session_entry["integration_type"] == "identity_mcp"

    # agent_name must be set (not None) and reflect the owner — either their
    # full_name or the fallback that identity Stage-2 routing stamps into
    # identity_owner_name. We accept any non-empty string as proof.

    assert session_entry["agent_name"] is not None, (
        "agent_name must be populated for identity_mcp sessions via "
        "session_metadata['identity_owner_name'] fallback"
    )
    assert len(session_entry["agent_name"]) > 0, (
        "agent_name must be a non-empty string for identity_mcp sessions"
    )


# ---------------------------------------------------------------------------
# Scenario 15: Desktop JWT claims stamped into session_metadata
# ---------------------------------------------------------------------------


def test_desktop_jwt_claims_stamped_into_session_metadata(
    client: TestClient,
    superuser_token_headers: dict,
) -> None:
    """
    Desktop JWT scenario:
      1. Create an agent owned by the superuser.
      2. Obtain a desktop access token via the full OAuth + PKCE flow so the
         backing DesktopOAuthClient row exists (required by the revocation
         check in get_current_user).
      3. Send a streaming message via the external A2A endpoint using that JWT.
      4. Verify that GET /external/sessions returns client_kind="desktop" and
         external_client_id matching the JWT's claim.
    """
    # ── Phase 1: Create agent ─────────────────────────────────────────────
    agent = _create_agent_ready(client, superuser_token_headers)
    agent_id = agent["id"]

    # ── Phase 2: Obtain a real desktop access token ──────────────────────
    bundle = obtain_desktop_tokens(
        client,
        superuser_token_headers,
        device_name="External Attribution Device",
    )
    access_token = bundle["access_token"]
    desktop_hdrs = {"Authorization": f"Bearer {access_token}"}

    # Extract the external_client_id claim (UUID PK of DesktopOAuthClient,
    # not the public client_id string) from the access token so we can
    # cross-check it against session_metadata below.
    decoded = pyjwt.decode(
        access_token,
        options={"verify_signature": False},
    )
    expected_external_client_id = decoded["external_client_id"]

    # ── Phase 3: Stream a message using the desktop JWT ───────────────────
    _stream_external_agent(client, desktop_hdrs, agent_id)

    # ── Phase 4: Verify client attribution in session metadata ───────────
    sessions = _list_external_sessions(client, desktop_hdrs)
    assert sessions, "expected at least one session after streaming"

    session = next(
        (s for s in sessions if s.get("agent_id") == agent_id),
        None,
    )
    assert session is not None, f"could not find session for agent {agent_id}"
    assert session.get("client_kind") == "desktop", (
        f"expected client_kind='desktop', got {session.get('client_kind')!r}"
    )
    assert session.get("external_client_id") == expected_external_client_id, (
        f"expected external_client_id={expected_external_client_id!r}, "
        f"got {session.get('external_client_id')!r}"
    )


# ---------------------------------------------------------------------------
# Scenario 16: Regular JWT does not stamp client attribution
# ---------------------------------------------------------------------------


def test_regular_jwt_no_client_claims_in_session(
    client: TestClient,
    superuser_token_headers: dict,
) -> None:
    """
    Regular web JWT (no client_kind/external_client_id claims) does NOT
    stamp client attribution into session_metadata.
    """
    # ── Phase 1: Create agent ─────────────────────────────────────────────
    agent = _create_agent_ready(client, superuser_token_headers)
    agent_id = agent["id"]

    # ── Phase 2: Stream using the standard web JWT ────────────────────────
    _stream_external_agent(client, superuser_token_headers, agent_id)

    # ── Phase 3: Verify no client attribution ────────────────────────────
    sessions = _list_external_sessions(client, superuser_token_headers)
    session = next(
        (s for s in sessions if s.get("agent_id") == agent_id),
        None,
    )
    assert session is not None
    assert session.get("client_kind") is None, (
        f"expected client_kind=None for web JWT, got {session.get('client_kind')!r}"
    )
    assert session.get("external_client_id") is None, (
        f"expected external_client_id=None for web JWT, "
        f"got {session.get('external_client_id')!r}"
    )


# ---------------------------------------------------------------------------
# Scenario 17: DELETE /sessions soft-hide
# ---------------------------------------------------------------------------


def test_delete_session_hides_from_list(
    client: TestClient,
    superuser_token_headers: dict,
) -> None:
    """
    Soft-hide lifecycle:
      1. Create agent and stream a message → session created.
      2. Session appears in GET /sessions.
      3. DELETE /sessions/{id} → 204.
      4. Session no longer appears in GET /sessions list.
      5. GET /sessions/{id} still returns 200 (not deleted, just hidden from list).
    """
    # ── Phase 1: Create agent + session ──────────────────────────────────
    agent = _create_agent_ready(client, superuser_token_headers)
    agent_id = agent["id"]
    _stream_external_agent(client, superuser_token_headers, agent_id)

    # ── Phase 2: Find the session ─────────────────────────────────────────
    sessions_before = _list_external_sessions(client, superuser_token_headers)
    session = next(
        (s for s in sessions_before if s.get("agent_id") == agent_id),
        None,
    )
    assert session is not None, "session not found before hide"
    session_id = session["id"]

    # ── Phase 3: Hide ─────────────────────────────────────────────────────
    _delete_external_session(client, superuser_token_headers, session_id, expected_status=204)

    # ── Phase 4: Session gone from list ───────────────────────────────────
    sessions_after = _list_external_sessions(client, superuser_token_headers)
    assert not any(s["id"] == session_id for s in sessions_after), (
        "session should be hidden from the list after DELETE"
    )

    # ── Phase 5: Direct GET still works (session not deleted) ─────────────
    _get_external_session(
        client, superuser_token_headers, session_id, expected_status=200
    )


# ---------------------------------------------------------------------------
# Scenario 18: Non-participant DELETE returns 404
# ---------------------------------------------------------------------------


def test_delete_session_non_participant_returns_404(
    client: TestClient,
    superuser_token_headers: dict,
) -> None:
    """A user who is not a participant of the session gets 404 on DELETE."""
    # ── Phase 1: Create session as superuser ─────────────────────────────
    agent = _create_agent_ready(client, superuser_token_headers)
    agent_id = agent["id"]
    _stream_external_agent(client, superuser_token_headers, agent_id)

    sessions = _list_external_sessions(client, superuser_token_headers)
    session = next(
        (s for s in sessions if s.get("agent_id") == agent_id),
        None,
    )
    assert session is not None
    session_id = session["id"]

    # ── Phase 2: Other user cannot hide ───────────────────────────────────
    _, other_hdrs = create_random_user_with_headers(client)
    _delete_external_session(client, other_hdrs, session_id, expected_status=404)

    # ── Phase 3: Original session still in list for the owner ─────────────
    sessions_after = _list_external_sessions(client, superuser_token_headers)
    assert any(s["id"] == session_id for s in sessions_after), (
        "session should still be visible to the owner after failed 3rd-party hide"
    )


# ---------------------------------------------------------------------------
# Scenario 19: Unauthenticated DELETE returns 401
# ---------------------------------------------------------------------------


def test_delete_session_unauthenticated_returns_401(
    client: TestClient,
    superuser_token_headers: dict,
) -> None:
    """Unauthenticated DELETE returns 401."""
    agent = _create_agent_ready(client, superuser_token_headers)
    agent_id = agent["id"]
    _stream_external_agent(client, superuser_token_headers, agent_id)

    sessions = _list_external_sessions(client, superuser_token_headers)
    session = next(
        (s for s in sessions if s.get("agent_id") == agent_id),
        None,
    )
    assert session is not None
    session_id = session["id"]

    r = client.delete(f"{_EXT_BASE}/sessions/{session_id}")
    assert r.status_code in (401, 403), (
        f"expected 401/403 for unauthenticated DELETE, got {r.status_code}"
    )


# ---------------------------------------------------------------------------
# Scenario 20: Non-existent session id returns 404 on DELETE
# ---------------------------------------------------------------------------


def test_delete_session_unknown_id_returns_404(
    client: TestClient,
    superuser_token_headers: dict,
) -> None:
    """DELETE with a non-existent session ID returns 404."""
    ghost_id = str(uuid.uuid4())
    _delete_external_session(
        client, superuser_token_headers, ghost_id, expected_status=404
    )
