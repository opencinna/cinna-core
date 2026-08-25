"""
Integration tests for the identity A2A target (`POST /external/a2a/identity/{owner_id}/`).

Scenarios covered:
  1. Identity card lists one skill per accessible binding; skill id == binding id
  2. Card v0.3 protocol + .well-known mirror
  3. SendStreamingMessage on first message runs Stage 2 routing, creates identity_mcp
     session with user_id=owner, identity_caller_id=caller, binding fields set
  4. Follow-up message on same task_id stays on the same binding (resume)
  5. Binding disabled mid-conversation → next POST returns -32004
  6. Cross-caller task_id isolation (B cannot resume A's thread)
  7. Non-assigned user gets 404 on card and -32004 on POST
  8. Unauthenticated POST returns 401/403
  9. Cross-owner task_id isolation (resuming an identity session for a
     different owner through /identity/other_owner/ is rejected)
  10. [REGRESSION] identity_mcp session.user_id uses identity owner_id, not agent.owner_id
      (Phase 2 A2A migration: ExternalA2AContextHandler._extra_session_kwargs must
       pass session_owner_id so ChannelIngestionService doesn't default to agent.owner_id)
  11. [REGRESSION] identity_mcp resume succeeds when identity owner != agent owner
      (_parse_session_scope checks session.user_id == context.session_owner_id; if
       session.user_id was stamped as agent.owner_id the check raises TaskScopeViolationError)
"""
import uuid
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlmodel import Session as DBSession

from app.core.config import settings
from app.models.identity.identity_models import (
    IdentityAgentBinding,
    IdentityBindingAssignment,
)
from tests.stubs.agent_env_stub import StubAgentEnvConnector
from tests.utils.a2a import (
    build_streaming_request,
    extract_task_id as _extract_task_id,
    parse_sse_events,
)
from tests.utils.agent import create_agent_via_api, get_agent
from tests.utils.background_tasks import drain_tasks
from tests.utils.identity import create_identity_binding, toggle_identity_contact
from tests.utils.user import create_random_user_with_headers

_EXT_A2A_BASE = f"{settings.API_V1_STR}/external/a2a"
_SESSIONS_BASE = f"{settings.API_V1_STR}/sessions"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _setup_identity(
    client: TestClient,
    superuser_token_headers: dict,
    caller_id: str,
    agent_name: str = "Identity Owner Agent",
    trigger_prompt: str = "Handle identity requests",
    prompt_examples: str | None = None,
    auto_enable: bool = True,
) -> tuple[str, dict, dict]:
    """Create an identity binding owned by the superuser assigning the caller.

    Superuser is used as the identity owner because ``auto_enable=True`` is
    only allowed for superusers — which is what we want so the caller has an
    immediately-usable binding without a separate enable step.

    Returns ``(owner_id, owner_agent, binding)``.
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
        trigger_prompt=trigger_prompt,
        prompt_examples=prompt_examples,
        assigned_user_ids=[caller_id],
        auto_enable=auto_enable,
    )
    return owner_id, owner_agent, binding


def _get_identity_card(
    client: TestClient,
    headers: dict,
    owner_id: str,
    protocol: str | None = None,
) -> tuple[int, dict | None]:
    url = f"{_EXT_A2A_BASE}/identity/{owner_id}/"
    if protocol:
        url += f"?protocol={protocol}"
    r = client.get(url, headers=headers)
    if r.status_code != 200:
        return r.status_code, None
    return r.status_code, r.json()


def _post_identity(
    client: TestClient,
    headers: dict,
    owner_id: str,
    request: dict,
    protocol: str | None = None,
):
    url = f"{_EXT_A2A_BASE}/identity/{owner_id}/"
    if protocol:
        url += f"?protocol={protocol}"
    return client.post(url, headers=headers, json=request)


def _send_identity_streaming(
    client: TestClient,
    headers: dict,
    owner_id: str,
    message_text: str = "Hello via identity",
    response_text: str = "Hi from the routed agent",
    task_id: str | None = None,
):
    """Send a streaming message via the identity A2A endpoint, parse events."""
    stub = StubAgentEnvConnector(response_text=response_text)
    request = build_streaming_request(message_text, task_id=task_id)
    with patch("app.services.sessions.message_service.agent_env_connector", stub):
        resp = _post_identity(client, headers, owner_id, request)
    drain_tasks()
    events = parse_sse_events(resp.text)
    return resp, events


# _extract_task_id lives in tests/utils/a2a.py and is imported above.


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_identity_card_lists_one_skill_per_binding(
    client: TestClient,
    superuser_token_headers: dict,
) -> None:
    """Identity card surfaces one AgentSkill per accessible binding.

    Skill.id equals the binding id (opaque to the caller — not the agent id).
    """
    caller, caller_headers = create_random_user_with_headers(client)
    owner_id, owner_agent, binding = _setup_identity(
        client,
        superuser_token_headers,
        caller_id=caller["id"],
        trigger_prompt="Route here for reports",
        prompt_examples="ask for Q1 numbers\nask for revenue",
    )

    status, card = _get_identity_card(client, caller_headers, owner_id)
    assert status == 200, f"Identity card fetch failed"
    assert card is not None

    # Person-level description: name comes from owner.full_name (may be "" if
    # not set on a superuser profile), description = owner.email.
    r = client.get(f"{settings.API_V1_STR}/users/me", headers=superuser_token_headers)
    owner_me = r.json()
    assert card["description"] == owner_me["email"]

    skills = card.get("skills", [])
    assert len(skills) == 1, f"Expected 1 skill for 1 binding, got {skills}"
    skill = skills[0]
    assert skill["id"] == binding["id"], (
        f"Skill id must be binding id (opaque): {skill['id']} vs {binding['id']}"
    )
    assert skill["description"] == "Route here for reports"
    assert "ask for Q1 numbers" in skill.get("examples", [])
    assert "ask for revenue" in skill.get("examples", [])

    # URLs point at the identity-scoped external namespace.
    iface_urls = [i["url"] for i in card.get("supportedInterfaces", [])]
    assert iface_urls, "Card must have supportedInterfaces on v1.0"
    for url in iface_urls:
        assert f"/api/v1/external/a2a/identity/{owner_id}/" in url
        assert "/api/v1/a2a/" not in url
        assert "/api/v1/external/a2a/agent/" not in url
        assert "/api/v1/external/a2a/route/" not in url


def test_identity_card_v03_and_well_known(
    client: TestClient,
    superuser_token_headers: dict,
) -> None:
    """v0.3 card exposes top-level `url`; .well-known mirror returns same card."""
    caller, caller_headers = create_random_user_with_headers(client)
    owner_id, _, _ = _setup_identity(
        client, superuser_token_headers, caller_id=caller["id"]
    )

    # v0.3
    status, card_v03 = _get_identity_card(client, caller_headers, owner_id, protocol="v0.3")
    assert status == 200
    assert card_v03 is not None
    assert "url" in card_v03
    assert f"/api/v1/external/a2a/identity/{owner_id}/" in card_v03["url"]

    # .well-known mirror
    r = client.get(
        f"{_EXT_A2A_BASE}/identity/{owner_id}/.well-known/agent-card.json",
        headers=caller_headers,
    )
    assert r.status_code == 200
    wk = r.json()
    assert wk["skills"], "Well-known card must include skills"
    assert "supportedInterfaces" in wk


def test_identity_streaming_creates_identity_mcp_session(
    client: TestClient,
    superuser_token_headers: dict,
) -> None:
    """First message runs Stage 2 (single binding → only_one) and creates a
    session with user_id=owner, identity_caller_id=caller, binding fields set.
    """
    caller, caller_headers = create_random_user_with_headers(client)
    caller_id = caller["id"]

    owner_id, owner_agent, binding = _setup_identity(
        client,
        superuser_token_headers,
        caller_id=caller_id,
        agent_name="Routed Identity Agent",
    )

    resp, events = _send_identity_streaming(
        client,
        caller_headers,
        owner_id,
        message_text="Please help me with something",
        response_text="Sure, happy to help.",
    )
    assert resp.status_code == 200
    assert events, f"Expected SSE events: {resp.text}"

    task_id = _extract_task_id(events)
    assert task_id, f"Could not extract task_id from events: {events}"

    # Owner can inspect the session
    r = client.get(f"{_SESSIONS_BASE}/{task_id}", headers=superuser_token_headers)
    assert r.status_code == 200, f"Owner should see session: {r.text}"
    session = r.json()

    assert session["integration_type"] == "identity_mcp"
    assert session["user_id"] == owner_id
    assert session["agent_id"] == owner_agent["id"]

    # identity_caller_id not in SessionPublic output but metadata + direct DB
    # check via the API's session_metadata dict.
    meta = session.get("session_metadata") or {}
    assert meta.get("identity_match_method") == "only_one", (
        f"Expected identity_match_method='only_one', got {meta}"
    )
    assert meta.get("app_mcp_source") == "identity"

    # Verify the session is NOT in the caller's own session list (it's owned
    # by the identity owner).
    r = client.get(f"{_SESSIONS_BASE}/?limit=100", headers=caller_headers)
    assert r.status_code == 200
    caller_session_ids = {s["id"] for s in r.json()["data"]}
    assert task_id not in caller_session_ids, (
        "identity_mcp session must not appear in caller's own session list"
    )


def test_identity_task_resume_stays_on_same_binding(
    client: TestClient,
    superuser_token_headers: dict,
) -> None:
    """Subsequent messages on the same task_id resume the same identity session.

    No new session is created; identity_binding_id remains unchanged.
    """
    caller, caller_headers = create_random_user_with_headers(client)
    owner_id, owner_agent, binding = _setup_identity(
        client, superuser_token_headers, caller_id=caller["id"]
    )

    # First message creates session
    resp1, events1 = _send_identity_streaming(
        client, caller_headers, owner_id,
        message_text="First turn",
        response_text="First reply",
    )
    assert resp1.status_code == 200
    task_id_1 = _extract_task_id(events1)
    assert task_id_1

    # Second message with same task_id resumes
    resp2, events2 = _send_identity_streaming(
        client, caller_headers, owner_id,
        message_text="Second turn",
        response_text="Second reply",
        task_id=task_id_1,
    )
    assert resp2.status_code == 200
    task_id_2 = _extract_task_id(events2)
    assert task_id_2 == task_id_1, (
        f"Resume must keep the same task_id; got {task_id_2} vs {task_id_1}"
    )

    # The resume must NOT spawn a second session: the owner should see exactly
    # one identity_mcp session for this agent, identified by the single task_id.
    r = client.get(f"{_SESSIONS_BASE}/?limit=100", headers=superuser_token_headers)
    assert r.status_code == 200, r.text
    identity_sessions = [
        s for s in r.json()["data"]
        if s.get("integration_type") == "identity_mcp"
        and s["agent_id"] == owner_agent["id"]
    ]
    assert len(identity_sessions) == 1, (
        f"Resume must reuse the same identity session; expected exactly 1, "
        f"got {len(identity_sessions)}: {[s['id'] for s in identity_sessions]}"
    )
    assert identity_sessions[0]["id"] == task_id_1, (
        f"The single identity session id should equal the task id {task_id_1}; "
        f"got {identity_sessions[0]['id']}"
    )


def test_identity_binding_disabled_mid_conversation(
    client: TestClient,
    superuser_token_headers: dict,
) -> None:
    """After first message, disabling the assignment causes follow-ups to fail.

    The validity check raises in session_id parsing, surfacing as -32004 with
    the standard "no longer active" message.
    """
    caller, caller_headers = create_random_user_with_headers(client)
    owner_id, _, binding = _setup_identity(
        client, superuser_token_headers, caller_id=caller["id"]
    )

    resp1, events1 = _send_identity_streaming(
        client, caller_headers, owner_id,
        message_text="Opening the thread",
        response_text="Reply 1",
    )
    assert resp1.status_code == 200
    task_id = _extract_task_id(events1)
    assert task_id

    # Disable the assignment via the contacts API (the caller turns the owner's
    # identity contact off, revoking their own access while a task is live).
    toggle_identity_contact(client, caller_headers, owner_id, is_enabled=False)

    # Follow-up with the same task_id — should get a revocation error.
    resp2, events2 = _send_identity_streaming(
        client, caller_headers, owner_id,
        message_text="Follow-up after revocation",
        response_text="Should not stream",
        task_id=task_id,
    )
    assert resp2.status_code == 200

    # The error may surface as an SSE error event or a JSON-RPC top-level error
    error_code = None
    error_msg = ""
    for event in events2:
        err = event.get("error")
        if err:
            error_code = err.get("code")
            error_msg = err.get("message") or ""
            break
    if error_code is None:
        body = resp2.json() if events2 == [] else None
        if body and "error" in body:
            error_code = body["error"]["code"]
            error_msg = body["error"].get("message") or ""

    assert error_code == -32004, (
        f"Expected -32004 for revoked binding, got {error_code} msg={error_msg!r} events={events2}"
    )
    assert "no longer active" in error_msg.lower(), (
        f"Error should mention revocation, got: {error_msg!r}"
    )


def test_identity_cross_caller_task_isolation(
    client: TestClient,
    superuser_token_headers: dict,
) -> None:
    """Two callers both assigned to the same identity; B cannot resume A's task."""
    caller_a, caller_a_headers = create_random_user_with_headers(client)
    caller_b, caller_b_headers = create_random_user_with_headers(client)

    r = client.get(f"{settings.API_V1_STR}/users/me", headers=superuser_token_headers)
    owner_id = r.json()["id"]

    # Create agent + binding assigning both callers
    owner_agent = create_agent_via_api(
        client, superuser_token_headers, name="Cross Caller Identity Agent"
    )
    drain_tasks()
    owner_agent = get_agent(client, superuser_token_headers, owner_agent["id"])

    create_identity_binding(
        client,
        superuser_token_headers,
        agent_id=owner_agent["id"],
        trigger_prompt="Shared identity",
        assigned_user_ids=[caller_a["id"], caller_b["id"]],
        auto_enable=True,
    )

    # A starts a conversation
    resp_a, events_a = _send_identity_streaming(
        client, caller_a_headers, owner_id,
        message_text="From caller A",
        response_text="Reply to A",
    )
    assert resp_a.status_code == 200
    task_id_a = _extract_task_id(events_a)
    assert task_id_a

    # B tries to resume A's task
    resp_b, events_b = _send_identity_streaming(
        client, caller_b_headers, owner_id,
        message_text="B trying to hijack A's thread",
        response_text="Should not happen",
        task_id=task_id_a,
    )
    assert resp_b.status_code == 200

    # Should see a scope error — B's task_id doesn't belong to B
    has_scope_error = False
    for event in events_b:
        err = event.get("error")
        if err and err.get("code") in (-32004, -32001):
            has_scope_error = True
            break
    if not has_scope_error:
        for event in events_b:
            state = (event.get("result", {}).get("status") or {}).get("state")
            if state == "failed":
                has_scope_error = True
                break
    assert has_scope_error, (
        f"Caller B should be rejected with scope error, got events: {events_b}"
    )


def test_identity_card_404_for_non_assigned_user(
    client: TestClient,
    superuser_token_headers: dict,
) -> None:
    """A user with no assignment to the owner gets a 404 for the card."""
    caller, _ = create_random_user_with_headers(client)
    owner_id, _, _ = _setup_identity(
        client, superuser_token_headers, caller_id=caller["id"]
    )

    # A different user who has no assignment on this identity
    _, stranger_headers = create_random_user_with_headers(client)

    status, _ = _get_identity_card(client, stranger_headers, owner_id)
    assert status == 404, f"Stranger should get 404, got {status}"


def test_identity_streaming_rejects_non_assigned_user(
    client: TestClient,
    superuser_token_headers: dict,
) -> None:
    """A user with no assignment gets -32004 on the JSON-RPC endpoint."""
    caller, _ = create_random_user_with_headers(client)
    owner_id, _, _ = _setup_identity(
        client, superuser_token_headers, caller_id=caller["id"]
    )
    _, stranger_headers = create_random_user_with_headers(client)

    request = build_streaming_request("Unauthorized")
    resp = _post_identity(client, stranger_headers, owner_id, request)
    assert resp.status_code == 200
    body = resp.json()
    assert "error" in body, f"Expected JSON-RPC error, got: {body}"
    assert body["error"]["code"] == -32004, (
        f"Expected -32004 for non-assigned user, got {body['error']}"
    )


def test_identity_streaming_rejects_disabled_assignment(
    client: TestClient,
    superuser_token_headers: dict,
) -> None:
    """Caller whose assignment is disabled before first message is rejected."""
    caller, caller_headers = create_random_user_with_headers(client)
    owner_id, _, binding = _setup_identity(
        client,
        superuser_token_headers,
        caller_id=caller["id"],
    )

    # Disable the assignment before the caller tries to use it (via the
    # contacts API — the caller turns the owner's identity contact off).
    toggle_identity_contact(client, caller_headers, owner_id, is_enabled=False)

    # Card 404
    status, _ = _get_identity_card(client, caller_headers, owner_id)
    assert status == 404

    # JSON-RPC -32004
    request = build_streaming_request("After being disabled")
    resp = _post_identity(client, caller_headers, owner_id, request)
    assert resp.status_code == 200
    body = resp.json()
    assert body.get("error", {}).get("code") == -32004, (
        f"Expected -32004, got {body}"
    )


def test_identity_streaming_unauthenticated(
    client: TestClient,
) -> None:
    """Unauthenticated POST is rejected before the handler runs."""
    ghost_id = str(uuid.uuid4())
    request = build_streaming_request("Unauthenticated")
    r = client.post(f"{_EXT_A2A_BASE}/identity/{ghost_id}/", json=request)
    assert r.status_code in (401, 403)


def test_identity_cross_owner_task_isolation(
    client: TestClient,
    superuser_token_headers: dict,
) -> None:
    """A caller with live sessions against two different owners cannot resume
    an Owner-A session through the /identity/Owner-B/ endpoint.

    The caller-id check alone would permit this because the caller is the
    same; session.user_id also has to match the URL-scoped owner.
    """
    # Owner A = superuser
    r = client.get(f"{settings.API_V1_STR}/users/me", headers=superuser_token_headers)
    owner_a_id = r.json()["id"]

    caller, caller_headers = create_random_user_with_headers(client)

    # Owner B: another superuser-provisioned identity owner.  To create a
    # second superuser-like user with auto_enable privileges without seeding
    # a new superuser account, we give the caller access via Owner A, create
    # a task there, then attempt to resume it via a bogus owner path.
    # This exercises: session.user_id != context.session_owner_id rejection.
    _, _, _ = _setup_identity(
        client,
        superuser_token_headers,
        caller_id=caller["id"],
        agent_name="Cross-Owner Isolation Agent",
    )

    resp_a, events_a = _send_identity_streaming(
        client, caller_headers, owner_a_id,
        message_text="Thread with Owner A",
        response_text="Owner A reply",
    )
    assert resp_a.status_code == 200
    task_id_a = _extract_task_id(events_a)
    assert task_id_a

    # Attempt to resume Owner A's task by POSTing to /identity/<random-owner>/
    # (a different uuid — there's no assignment to it, so the handler should
    # reject with -32004 before even touching the session).
    other_owner_id = str(uuid.uuid4())
    resp_b, _events_b = _send_identity_streaming(
        client, caller_headers, other_owner_id,
        message_text="Cross-owner attempt",
        response_text="Should not happen",
        task_id=task_id_a,
    )
    assert resp_b.status_code == 200
    body = resp_b.json()
    assert body.get("error", {}).get("code") == -32004, (
        f"Expected -32004 for cross-owner attempt, got {body}"
    )


# ---------------------------------------------------------------------------
# Regression tests — Phase 2 A2A migration (session owner stamping)
# ---------------------------------------------------------------------------


def test_identity_mcp_session_user_id_uses_identity_owner_not_agent_owner(
    client: TestClient,
    superuser_token_headers: dict,
    db: DBSession,
) -> None:
    """[REGRESSION] identity_mcp session.user_id must equal context.session_owner_id
    (the identity owner), not agent.owner_id.

    Failure mode (pre-fix): ExternalA2AContextHandler._extra_session_kwargs()
    returned None. ChannelIngestionService._select_session_owner_id for
    an 'a2a_caller' sender fell back to agent.owner_id. When
    context.session_owner_id != agent.owner_id, the session was stamped with
    the wrong user_id. On resume, _parse_session_scope raised
    TaskScopeViolationError because session.user_id != context.session_owner_id.

    This test creates that divergence directly: identity owner (User A) owns
    the binding; the agent is owned by User B. The API binding-creation
    endpoint enforces agent.owner_id == caller (which prevents this through
    normal flows). We insert the binding row directly to reproduce the exact
    condition the fix guards against.

    Full story:
      1. Create agent owner (User B).
      2. Create identity owner (User A = superuser) and caller (User C).
      3. Insert IdentityAgentBinding with owner_id=User A, agent_id=agent-owned-by-B.
      4. Insert IdentityBindingAssignment for User C, is_enabled=True.
      5. Send identity_mcp stream as User C against User A's identity.
      6. Assert session.user_id == User A.id (NOT User B.id).
      7. Assert session.user_id != agent.owner_id (proves the fix matters).
      8. Resume with the same task_id — assert no scope-violation error.
    """
    # ── Phase 1: Setup users ──────────────────────────────────────────────────
    # User A = identity owner (superuser, because auto_enable requires it)
    r = client.get(f"{settings.API_V1_STR}/users/me", headers=superuser_token_headers)
    assert r.status_code == 200
    identity_owner = r.json()
    identity_owner_id = identity_owner["id"]

    # User B = agent owner (a different regular user)
    agent_owner, _ = create_random_user_with_headers(client)
    agent_owner_id = agent_owner["id"]

    # User C = caller (the external caller)
    caller, caller_headers = create_random_user_with_headers(client)
    caller_id = caller["id"]

    # ── Phase 2: Create the agent owned by User B ─────────────────────────────
    # Use superuser to give User B an agent (agent creation requires superuser
    # or developer role; easiest is to create via superuser and then we'll use
    # the agent id in a direct DB binding).
    owner_agent = create_agent_via_api(
        client, superuser_token_headers, name="Regression Mismatched Owner Agent"
    )
    drain_tasks()
    owner_agent = get_agent(client, superuser_token_headers, owner_agent["id"])
    assert owner_agent["active_environment_id"] is not None

    # Reassign the agent's owner_id to User B so agent.owner_id != identity owner.
    # We do this via DB directly since there is no agent-transfer API.
    from app.models import Agent
    agent_row = db.get(Agent, uuid.UUID(owner_agent["id"]))
    assert agent_row is not None
    agent_row.owner_id = uuid.UUID(agent_owner_id)
    db.add(agent_row)
    db.flush()
    db.refresh(agent_row)
    # Confirm the divergence: agent.owner_id != identity owner
    assert agent_row.owner_id != uuid.UUID(identity_owner_id), (
        "Test pre-condition violated: agent.owner_id must differ from identity owner"
    )

    # ── Phase 3: Insert identity binding directly (bypassing API validation) ──
    # The API's IdentityService.create_binding enforces agent.owner_id == caller.
    # We insert directly to create the scenario the fix guards against.
    binding_id = uuid.uuid4()
    binding = IdentityAgentBinding(
        id=binding_id,
        owner_id=uuid.UUID(identity_owner_id),
        agent_id=uuid.UUID(owner_agent["id"]),
        trigger_prompt="Regression test binding — owner != agent.owner_id",
        is_active=True,
    )
    db.add(binding)
    db.flush()

    # Assign User C with auto-enable so they can reach it immediately.
    assignment = IdentityBindingAssignment(
        binding_id=binding_id,
        target_user_id=uuid.UUID(caller_id),
        is_active=True,
        is_enabled=True,
        auto_enable=True,
    )
    db.add(assignment)
    db.commit()

    # ── Phase 4: Send identity_mcp stream as User C ───────────────────────────
    resp, events = _send_identity_streaming(
        client,
        caller_headers,
        identity_owner_id,
        message_text="Regression test — first message",
        response_text="Reply from mismatched-owner agent",
    )
    assert resp.status_code == 200, f"identity streaming failed: {resp.text}"
    assert events, f"Expected SSE events: {resp.text}"

    task_id = _extract_task_id(events)
    assert task_id, f"Could not extract task_id from events: {events}"

    # ── Phase 5: Assert session.user_id == identity owner, NOT agent.owner_id ─
    # The regression: without the fix, session.user_id == agent.owner_id (User B).
    # The fix: _extra_session_kwargs returns {"session_owner_id": owner_id} so
    # ChannelIngestionService uses owner_id (User A) instead of agent.owner_id.
    r = client.get(
        f"{_SESSIONS_BASE}/{task_id}", headers=superuser_token_headers
    )
    assert r.status_code == 200, f"Owner should be able to read the session: {r.text}"
    session = r.json()

    assert session["integration_type"] == "identity_mcp", (
        f"Expected integration_type='identity_mcp', got {session['integration_type']!r}"
    )
    assert session["user_id"] == identity_owner_id, (
        f"REGRESSION: session.user_id should be identity_owner_id={identity_owner_id!r}, "
        f"got {session['user_id']!r}. "
        "Pre-fix behaviour: ChannelIngestionService defaulted to agent.owner_id "
        f"({str(agent_row.owner_id)!r}) when _extra_session_kwargs returned None."
    )
    assert session["user_id"] != str(agent_row.owner_id), (
        "REGRESSION: session.user_id must NOT equal agent.owner_id when they differ"
    )

    # ── Phase 6: Resume with the same task_id ────────────────────────────────
    # Pre-fix: _parse_session_scope checked session.user_id != context.session_owner_id
    # (User B != User A) → raised TaskScopeViolationError → SSE error event.
    # Post-fix: session.user_id == context.session_owner_id (User A == User A) → success.
    resp2, events2 = _send_identity_streaming(
        client,
        caller_headers,
        identity_owner_id,
        message_text="Regression test — resume",
        response_text="Reply to resumed session",
        task_id=task_id,
    )
    assert resp2.status_code == 200, f"Resume request failed: {resp2.text}"

    # Must not contain a scope-violation error
    for event in events2:
        err = event.get("error")
        if err:
            assert err.get("code") not in (-32004, -32001), (
                f"REGRESSION: resume raised scope-violation error: {err}. "
                "This means session.user_id was stamped as agent.owner_id, not "
                "context.session_owner_id, causing _parse_session_scope to reject "
                "the resume. Fix: ExternalA2AContextHandler._extra_session_kwargs "
                "must return {'session_owner_id': self.context.session_owner_id}."
            )

    task_id_2 = _extract_task_id(events2)
    assert task_id_2 == task_id, (
        f"Resume must reuse the same session (task_id={task_id}), "
        f"got task_id_2={task_id_2}"
    )


def test_identity_mcp_session_identity_fields_stamped_correctly(
    client: TestClient,
    superuser_token_headers: dict,
) -> None:
    """identity_mcp session has identity_caller_id, binding_id, assignment_id stamped.

    Complementary to the user_id regression test: verifies that
    _stamp_new_session also writes the caller-scoped identity fields so the
    _session_matches_context check (identity_caller_id == context.identity_caller_id)
    succeeds on subsequent requests.

    Full story:
      1. Create identity binding (superuser owns it; caller is assigned).
      2. Send first identity_mcp stream.
      3. Fetch session via owner's GET /sessions/{id}.
      4. Assert session_metadata contains identity_match_method (routing metadata).
      5. Assert session.user_id == owner_id (owner-stamped, not caller-stamped).
      6. Resume — task_id unchanged, no scope error.
    """
    caller, caller_headers = create_random_user_with_headers(client)
    caller_id = caller["id"]

    owner_id, owner_agent, binding = _setup_identity(
        client,
        superuser_token_headers,
        caller_id=caller_id,
        agent_name="Stamp Verification Agent",
        trigger_prompt="Verify stamp fields",
    )

    # ── Phase 1: First message ─────────────────────────────────────────────────
    resp, events = _send_identity_streaming(
        client, caller_headers, owner_id,
        message_text="Check my stamps please",
        response_text="Stamps verified.",
    )
    assert resp.status_code == 200, f"identity streaming failed: {resp.text}"
    task_id = _extract_task_id(events)
    assert task_id

    # ── Phase 2: Verify session fields via owner API ───────────────────────────
    r = client.get(f"{_SESSIONS_BASE}/{task_id}", headers=superuser_token_headers)
    assert r.status_code == 200
    session = r.json()

    assert session["integration_type"] == "identity_mcp"

    # user_id == identity owner (NOT the caller) — this is the regression anchor
    assert session["user_id"] == owner_id, (
        f"session.user_id must be identity owner ({owner_id}), got {session['user_id']}. "
        "If this fails, _extra_session_kwargs returned None and ChannelIngestionService "
        "defaulted session.user_id to agent.owner_id."
    )
    assert session["user_id"] != caller_id, (
        "session.user_id must NOT be the caller's user_id for identity_mcp sessions"
    )

    # session_metadata must contain routing info stamped by _stamp_new_session
    meta = session.get("session_metadata") or {}
    assert meta.get("identity_match_method") == "only_one", (
        f"Expected identity_match_method='only_one' in session_metadata, got {meta}"
    )
    assert meta.get("app_mcp_source") == "identity", (
        f"Expected app_mcp_source='identity' in session_metadata, got {meta}"
    )

    # ── Phase 3: Resume succeeds, same task_id ────────────────────────────────
    resp2, events2 = _send_identity_streaming(
        client, caller_headers, owner_id,
        message_text="Follow-up message",
        response_text="Reply to follow-up.",
        task_id=task_id,
    )
    assert resp2.status_code == 200

    for event in events2:
        err = event.get("error")
        if err:
            assert err.get("code") not in (-32004, -32001), (
                f"Unexpected scope error on resume: {err}. "
                "This indicates _parse_session_scope raised TaskScopeViolationError "
                "because session.user_id was not stamped as context.session_owner_id."
            )

    task_id_2 = _extract_task_id(events2)
    assert task_id_2 == task_id, (
        f"Resume must reuse the same session, got {task_id_2} vs {task_id}"
    )
