"""
Integration tests for the app_mcp-route A2A target (`POST /external/a2a/route/{route_id}/`).

Scenarios covered:
  1. Card reflects the route's name + trigger_prompt with route-scoped URLs.
  2. Card fetch honours ?protocol=v0.3.
  3. SendStreamingMessage creates an app_mcp session:
       - session.user_id == agent.owner_id
       - session.caller_id == caller.id
       - session_metadata["app_mcp_match_method"] == "external_direct"
       - session_metadata["app_mcp_route_id"] == route.id
  4. Disabled assignment → card 404 and JSON-RPC -32004.
  5. Cross-caller task_id → -32004 (can't resume someone else's thread).
  6. Revoked (deleted) route → 404 / -32004.
"""
import uuid
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.stubs.agent_env_stub import StubAgentEnvConnector
from tests.utils.a2a import build_streaming_request, parse_sse_events
from tests.utils.agent import create_agent_via_api, get_agent
from tests.utils.app_agent_route import (
    create_admin_route,
    delete_admin_route,
    toggle_admin_assignment,
)
from tests.utils.background_tasks import drain_tasks
from tests.utils.user import create_random_user_with_headers

_EXT_A2A_BASE = f"{settings.API_V1_STR}/external/a2a"
_SESSIONS_BASE = f"{settings.API_V1_STR}/sessions"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _setup_owner_agent(
    client: TestClient,
    owner_headers: dict,
    name: str,
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
    trigger_prompt: str = "Handle shared requests",
    route_name: str | None = None,
    auto_enable: bool = True,
) -> tuple[dict, dict, str]:
    """Create an owner agent as superuser, a route, assign the caller.

    Returns ``(owner_agent, route, assignment_id)``. Assignments are auto-enabled
    when ``auto_enable=True`` so the route is immediately usable.
    """
    owner_agent = _setup_owner_agent(client, superuser_token_headers, agent_name)
    route = create_admin_route(
        client,
        superuser_token_headers,
        agent_id=owner_agent["id"],
        name=route_name,
        trigger_prompt=trigger_prompt,
        assigned_user_ids=[caller_id],
        auto_enable_for_users=auto_enable,
    )
    assignment_id = next(
        a["id"] for a in route["assignments"] if a["user_id"] == caller_id
    )
    return owner_agent, route, assignment_id


def _get_route_card(
    client: TestClient,
    headers: dict,
    route_id: str,
    protocol: str | None = None,
) -> tuple[int, dict | None]:
    url = f"{_EXT_A2A_BASE}/route/{route_id}/"
    if protocol:
        url += f"?protocol={protocol}"
    r = client.get(url, headers=headers)
    if r.status_code != 200:
        return r.status_code, None
    return r.status_code, r.json()


def _post_route(
    client: TestClient,
    headers: dict,
    route_id: str,
    request: dict,
    protocol: str | None = None,
):
    url = f"{_EXT_A2A_BASE}/route/{route_id}/"
    if protocol:
        url += f"?protocol={protocol}"
    return client.post(url, headers=headers, json=request)


def _send_route_streaming(
    client: TestClient,
    headers: dict,
    route_id: str,
    message_text: str = "Hello via route",
    response_text: str = "Hi from the shared agent",
    task_id: str | None = None,
    protocol: str | None = None,
):
    """Send a streaming message via the route A2A endpoint and parse SSE events.

    Returns ``(raw_response, events)``.
    """
    stub = StubAgentEnvConnector(response_text=response_text)
    request = build_streaming_request(message_text, task_id=task_id)
    with patch("app.services.sessions.message_service.agent_env_connector", stub):
        resp = _post_route(client, headers, route_id, request, protocol=protocol)
    drain_tasks()
    events = parse_sse_events(resp.text)
    return resp, events


def _extract_task_id(events: list[dict]) -> str | None:
    for event in events:
        result = event.get("result", {})
        tid = result.get("id") or result.get("taskId")
        if tid:
            return tid
    return None


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_route_card_reflects_route_name_and_description(
    client: TestClient,
    superuser_token_headers: dict,
) -> None:
    """The route card uses route.name + route.trigger_prompt and a route URL."""
    caller, caller_headers = create_random_user_with_headers(client)
    _, route, _ = _setup_route(
        client,
        superuser_token_headers,
        caller_id=caller["id"],
        agent_name="Underlying Agent",
        trigger_prompt="Shared agent description via route",
        route_name="Friendly Route Name",
    )
    route_id = route["id"]

    status, card = _get_route_card(client, caller_headers, route_id)
    assert status == 200
    assert card is not None

    # Card surface reflects route.name and route.trigger_prompt, NOT the
    # underlying agent's name/description.
    assert card["name"] == "Friendly Route Name"
    assert card["description"] == "Shared agent description via route"

    # supportedInterfaces point at the route-scoped external namespace.
    iface_urls = [i["url"] for i in card.get("supportedInterfaces", [])]
    assert iface_urls, "Card must have supportedInterfaces for v1.0"
    for url in iface_urls:
        assert f"/api/v1/external/a2a/route/{route_id}/" in url, (
            f"supportedInterfaces url should be route-scoped: {url}"
        )
        assert "/api/v1/a2a/" not in url
        assert "/api/v1/external/a2a/agent/" not in url


def test_route_card_cinna_mcp_uses_route_identity(
    client: TestClient,
    superuser_token_headers: dict,
) -> None:
    """The route card's cinna.mcp descriptor reflects the route identity.

    The descriptor must use the route's name as display_name and the route's
    trigger_prompt as description — not the raw underlying agent's name.
    """
    caller, caller_headers = create_random_user_with_headers(client)
    _, route, _ = _setup_route(
        client,
        superuser_token_headers,
        caller_id=caller["id"],
        agent_name="Raw Underlying Agent",
        trigger_prompt="Route-level trigger prompt",
        route_name="Route Display Name",
    )
    route_id = route["id"]

    status, card = _get_route_card(client, caller_headers, route_id)
    assert status == 200 and card is not None

    extensions = (card.get("capabilities") or {}).get("extensions") or []
    params = next(
        (e.get("params") for e in extensions if e.get("uri") == "urn:cinna:mcp"),
        None,
    )
    assert params is not None, "Route card must carry the urn:cinna:mcp descriptor"

    assert params["display_name"] == "Route Display Name"
    assert params["description"] == "Route-level trigger prompt"
    assert params["tool_name"] == "route_display_name", (
        f"Slug should derive from the route name, got {params['tool_name']!r}"
    )


def test_route_card_v03_protocol(
    client: TestClient,
    superuser_token_headers: dict,
) -> None:
    """v0.3 route card exposes the route URL on the top-level `url` field."""
    caller, caller_headers = create_random_user_with_headers(client)
    _, route, _ = _setup_route(
        client,
        superuser_token_headers,
        caller_id=caller["id"],
    )
    route_id = route["id"]

    status, card = _get_route_card(
        client, caller_headers, route_id, protocol="v0.3"
    )
    assert status == 200
    assert card is not None
    assert "url" in card
    assert f"/api/v1/external/a2a/route/{route_id}/" in card["url"]


def test_route_card_well_known_mirror(
    client: TestClient,
    superuser_token_headers: dict,
) -> None:
    """The .well-known mirror returns the same card."""
    caller, caller_headers = create_random_user_with_headers(client)
    _, route, _ = _setup_route(
        client, superuser_token_headers, caller_id=caller["id"]
    )
    route_id = route["id"]

    r = client.get(
        f"{_EXT_A2A_BASE}/route/{route_id}/.well-known/agent-card.json",
        headers=caller_headers,
    )
    assert r.status_code == 200
    card = r.json()
    assert "supportedInterfaces" in card
    assert card["name"]  # route.name — non-empty


def test_route_streaming_creates_app_mcp_session_with_correct_ownership(
    client: TestClient,
    superuser_token_headers: dict,
) -> None:
    """A streaming message via the route creates an app_mcp session with
    session.user_id=owner, session.caller_id=caller, and session_metadata
    stamped with app_mcp_match_method='external_direct' + route info.
    """
    caller, caller_headers = create_random_user_with_headers(client)
    caller_id = caller["id"]

    owner_agent, route, _ = _setup_route(
        client,
        superuser_token_headers,
        caller_id=caller_id,
        agent_name="Routed Ownership Agent",
    )
    route_id = route["id"]

    # Superuser is the agent owner — capture their user id
    r = client.get(f"{settings.API_V1_STR}/users/me", headers=superuser_token_headers)
    assert r.status_code == 200
    owner_id = r.json()["id"]

    resp, events = _send_route_streaming(
        client, caller_headers, route_id,
        message_text="Hi via the route!",
        response_text="Responding from shared agent.",
    )
    assert resp.status_code == 200
    assert events, f"Expected SSE events: {resp.text}"

    task_id = _extract_task_id(events)
    assert task_id, f"Could not extract task_id from events: {events}"

    # Owner can fetch the session via GET /sessions/{id} (includes session_metadata)
    r = client.get(f"{_SESSIONS_BASE}/{task_id}", headers=superuser_token_headers)
    assert r.status_code == 200, f"Owner should see session: {r.text}"
    session = r.json()

    assert session["integration_type"] == "app_mcp", (
        f"Expected integration_type='app_mcp', got {session['integration_type']!r}"
    )
    assert session["user_id"] == owner_id, (
        f"Expected session.user_id={owner_id}, got {session['user_id']}"
    )
    assert session["caller_id"] == caller_id, (
        f"Expected session.caller_id={caller_id}, got {session['caller_id']}"
    )
    assert session["agent_id"] == owner_agent["id"]

    meta = session.get("session_metadata") or {}
    assert meta.get("app_mcp_match_method") == "external_direct", (
        f"Expected app_mcp_match_method='external_direct', got {meta}"
    )
    assert meta.get("app_mcp_route_id") == route_id
    # route_source comes from EffectiveRoute.source — superuser-created routes
    # are classified as "admin".
    assert meta.get("app_mcp_route_type") == "admin"

    # Caller must NOT see this session in their own session list — app_mcp
    # sessions are owned by the agent owner.
    r = client.get(f"{_SESSIONS_BASE}/?limit=100", headers=caller_headers)
    assert r.status_code == 200
    caller_session_ids = {s["id"] for s in r.json()["data"]}
    assert task_id not in caller_session_ids, (
        "app_mcp session must not appear in caller's own session list"
    )


def test_route_card_404_when_assignment_disabled(
    client: TestClient,
    superuser_token_headers: dict,
) -> None:
    """Caller with a disabled assignment cannot fetch the route card."""
    caller, caller_headers = create_random_user_with_headers(client)
    _, route, assignment_id = _setup_route(
        client,
        superuser_token_headers,
        caller_id=caller["id"],
        auto_enable=True,
    )
    route_id = route["id"]

    # Caller disables their assignment
    toggle_admin_assignment(client, caller_headers, assignment_id, is_enabled=False)

    status, _ = _get_route_card(client, caller_headers, route_id)
    assert status == 404


def test_route_streaming_rejects_disabled_assignment(
    client: TestClient,
    superuser_token_headers: dict,
) -> None:
    """POST from a caller whose assignment is disabled returns -32004."""
    caller, caller_headers = create_random_user_with_headers(client)
    _, route, assignment_id = _setup_route(
        client,
        superuser_token_headers,
        caller_id=caller["id"],
        auto_enable=True,
    )
    route_id = route["id"]
    toggle_admin_assignment(client, caller_headers, assignment_id, is_enabled=False)

    request = build_streaming_request("Unauthorized attempt")
    resp = _post_route(client, caller_headers, route_id, request)
    assert resp.status_code == 200
    body = resp.json()

    assert "error" in body, f"Expected JSON-RPC error, got: {body}"
    assert body["error"]["code"] == -32004, (
        f"Expected -32004 for disabled assignment, got {body['error']}"
    )


def test_route_streaming_rejects_unassigned_user(
    client: TestClient,
    superuser_token_headers: dict,
) -> None:
    """A completely different user (no assignment at all) is rejected."""
    caller, _ = create_random_user_with_headers(client)
    _, route, _ = _setup_route(
        client, superuser_token_headers, caller_id=caller["id"]
    )
    route_id = route["id"]

    # A different user with no assignment to this route
    _, stranger_headers = create_random_user_with_headers(client)

    status, _ = _get_route_card(client, stranger_headers, route_id)
    assert status == 404

    request = build_streaming_request("Unauthorized")
    resp = _post_route(client, stranger_headers, route_id, request)
    assert resp.status_code == 200
    body = resp.json()
    assert "error" in body
    assert body["error"]["code"] == -32004


def test_route_streaming_rejects_revoked_route(
    client: TestClient,
    superuser_token_headers: dict,
) -> None:
    """Deleting the route after discovery → card 404 + -32004 on POST."""
    caller, caller_headers = create_random_user_with_headers(client)
    _, route, _ = _setup_route(
        client, superuser_token_headers, caller_id=caller["id"]
    )
    route_id = route["id"]

    # Delete the route
    delete_admin_route(client, superuser_token_headers, route_id)

    status, _ = _get_route_card(client, caller_headers, route_id)
    assert status == 404

    request = build_streaming_request("After revocation")
    resp = _post_route(client, caller_headers, route_id, request)
    assert resp.status_code == 200
    body = resp.json()
    assert "error" in body
    assert body["error"]["code"] == -32004


def test_route_task_id_cross_caller_isolation(
    client: TestClient,
    superuser_token_headers: dict,
) -> None:
    """One caller cannot resume another caller's task_id on the same route.

    Two callers both have assignments to the same route. Caller A creates a
    session; Caller B tries to send with the same task_id → must be rejected
    (cross-caller scope check in A2ARequestHandler context methods).
    """
    caller_a, caller_a_headers = create_random_user_with_headers(client)
    caller_b, caller_b_headers = create_random_user_with_headers(client)

    # Create agent + route, assign both callers with auto-enable
    owner_agent = _setup_owner_agent(
        client, superuser_token_headers, "Cross-Caller Route Agent"
    )
    route = create_admin_route(
        client,
        superuser_token_headers,
        agent_id=owner_agent["id"],
        trigger_prompt="Shared route for isolation test",
        assigned_user_ids=[caller_a["id"], caller_b["id"]],
        auto_enable_for_users=True,
    )
    route_id = route["id"]

    # Caller A streams → gets task_id_a
    resp_a, events_a = _send_route_streaming(
        client, caller_a_headers, route_id,
        message_text="From caller A",
        response_text="Reply to A",
    )
    assert resp_a.status_code == 200
    task_id_a = _extract_task_id(events_a)
    assert task_id_a

    # Caller B tries to resume Caller A's task_id
    resp_b, events_b = _send_route_streaming(
        client, caller_b_headers, route_id,
        message_text="B trying to steal A's thread",
        response_text="Should not happen",
        task_id=task_id_a,
    )
    assert resp_b.status_code == 200
    # Look for the error signal: either an SSE error event or a JSON-RPC error body
    has_scope_error = False
    for event in events_b:
        err = event.get("error")
        if err and err.get("code") in (-32004, -32001):
            has_scope_error = True
            break
    if not has_scope_error:
        # Could be SSE failed state
        for event in events_b:
            result = event.get("result", {})
            state = (result.get("status") or {}).get("state")
            if state == "failed":
                has_scope_error = True
                break
    assert has_scope_error, (
        f"Caller B should be rejected with scope error, got events: {events_b}"
    )


def test_route_streaming_unauthenticated(
    client: TestClient,
    superuser_token_headers: dict,
) -> None:
    """Unauthenticated POST to the route A2A endpoint is rejected before handler."""
    ghost_id = str(uuid.uuid4())
    request = build_streaming_request("Unauthenticated")
    r = client.post(f"{_EXT_A2A_BASE}/route/{ghost_id}/", json=request)
    assert r.status_code in (401, 403)
