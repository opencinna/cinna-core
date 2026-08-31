"""
Integration tests: guest share session access (Phase 4).

Tests that guests (anonymous and grant-based) can create, list, and
interact with sessions linked to guest shares.

Scenario coverage:
  1. Anonymous guest creates session via guest share → mode forced to conversation
  2. Anonymous guest cannot create building mode session → rejected
  3. Anonymous guest lists sessions → only sees sessions from their guest share
  4. Anonymous guest gets session details → works
  5. Anonymous guest cannot access another guest share's sessions
  6. Owner can see guest sessions in the normal session list
  7. Authenticated user with grant can create and list guest share sessions
  8. Authenticated user without grant cannot create guest share session
  9. Guest gets messages for their session

Only environment adapter is stubbed (via conftest autouse fixtures).
"""
from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.guest_share import (
    activate_guest_grant,
    create_guest_share,
    guest_headers,
    setup_guest_share_agent,
)
from tests.utils.message import list_messages
from tests.utils.session import create_session_via_api, list_sessions
from tests.utils.user import create_random_user_with_headers

API = settings.API_V1_STR


# ── Tests ────────────────────────────────────────────────────────────────


def test_anonymous_guest_session_lifecycle(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Full anonymous guest session lifecycle:
      1. Owner creates agent + guest share
      2. Anonymous guest authenticates
      3. Guest creates a session → mode is conversation, guest_share_id is set
      4. Guest lists sessions → only their guest share's sessions appear
      5. Guest gets session details → works
      6. Guest gets messages for session → works (empty initially)
      7. Owner lists sessions → guest session is visible
    """
    # ── Phase 1: Owner creates agent + guest share ───────────────────────

    agent, share = setup_guest_share_agent(
        client, superuser_token_headers,
        name="Guest Session Agent",
        share_label="Session Test Link",
    )
    agent_id = agent["id"]
    share_id = share["id"]

    # ── Phase 2: Anonymous guest authenticates ───────────────────────────

    guest_hdrs = guest_headers(client, share["token"], security_code=share["security_code"])

    # ── Phase 3: Guest creates a session ─────────────────────────────────

    guest_session = create_session_via_api(
        client, guest_hdrs, agent_id, guest_share_id=share_id,
    )
    assert guest_session["guest_share_id"] == share_id
    assert guest_session["mode"] == "conversation"
    session_id = guest_session["id"]

    # ── Phase 4: Guest lists sessions → sees their session ───────────────

    guest_sessions = list_sessions(client, guest_hdrs)
    assert len(guest_sessions) >= 1
    session_ids = [s["id"] for s in guest_sessions]
    assert session_id in session_ids
    for s in guest_sessions:
        assert s["guest_share_id"] == share_id

    # ── Phase 5: Guest gets session details ──────────────────────────────

    r = client.get(f"{API}/sessions/{session_id}", headers=guest_hdrs)
    assert r.status_code == 200
    details = r.json()
    assert details["id"] == session_id
    assert details["guest_share_id"] == share_id
    assert details["mode"] == "conversation"

    # ── Phase 6: Guest gets messages (initially empty) ───────────────────

    messages = list_messages(client, guest_hdrs, session_id)
    assert isinstance(messages, list)

    # ── Phase 7: Owner lists sessions → guest session is visible ─────────

    owner_sessions = list_sessions(client, superuser_token_headers)
    owner_session_ids = [s["id"] for s in owner_sessions]
    assert session_id in owner_session_ids


def test_anonymous_guest_cannot_create_building_mode_session(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Anonymous guest tries to create a building mode session → rejected.
    """
    agent, share = setup_guest_share_agent(
        client, superuser_token_headers,
        name="No Building Guest Agent",
    )
    guest_hdrs = guest_headers(client, share["token"], security_code=share["security_code"])

    r = client.post(
        f"{API}/sessions/",
        headers=guest_hdrs,
        json={
            "agent_id": agent["id"],
            "mode": "building",
            "guest_share_id": share["id"],
        },
    )
    assert r.status_code == 400
    assert "conversation mode" in r.json()["detail"].lower()


def test_anonymous_guest_session_uses_jwt_guest_share_id(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    When anonymous guest does not provide guest_share_id in the request,
    the JWT's guest_share_id is used automatically.
    """
    agent, share = setup_guest_share_agent(
        client, superuser_token_headers,
        name="Auto Share ID Agent",
    )
    guest_hdrs = guest_headers(client, share["token"], security_code=share["security_code"])

    # Create session WITHOUT specifying guest_share_id
    guest_session = create_session_via_api(client, guest_hdrs, agent["id"])
    assert guest_session["guest_share_id"] == share["id"]


def test_anonymous_guest_cannot_access_other_shares_sessions(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Anonymous guest from share A cannot access sessions from share B.

      1. Create agent with two guest shares (A and B)
      2. Guest A creates a session
      3. Guest B creates a session
      4. Guest A lists sessions → only sees share A's session
      5. Guest B lists sessions → only sees share B's session
      6. Guest A cannot get session B's details → 403
    """
    agent, share_a = setup_guest_share_agent(
        client, superuser_token_headers,
        name="Multi-Share Sessions Agent",
        share_label="Share A",
    )
    agent_id = agent["id"]
    share_b = create_guest_share(client, superuser_token_headers, agent_id, label="Share B")

    guest_a_hdrs = guest_headers(client, share_a["token"], security_code=share_a["security_code"])
    guest_b_hdrs = guest_headers(client, share_b["token"], security_code=share_b["security_code"])

    # ── Guest A creates a session ─────────────────────────────────────────

    session_a = create_session_via_api(
        client, guest_a_hdrs, agent_id, guest_share_id=share_a["id"],
    )

    # ── Guest B creates a session ─────────────────────────────────────────

    session_b = create_session_via_api(
        client, guest_b_hdrs, agent_id, guest_share_id=share_b["id"],
    )

    # ── Guest A lists sessions → only share A's session ───────────────────

    sessions_a = list_sessions(client, guest_a_hdrs)
    session_a_ids = [s["id"] for s in sessions_a]
    assert session_a["id"] in session_a_ids
    assert session_b["id"] not in session_a_ids

    # ── Guest B lists sessions → only share B's session ───────────────────

    sessions_b = list_sessions(client, guest_b_hdrs)
    session_b_ids = [s["id"] for s in sessions_b]
    assert session_b["id"] in session_b_ids
    assert session_a["id"] not in session_b_ids

    # ── Guest A cannot get Guest B's session details ──────────────────────

    r = client.get(f"{API}/sessions/{session_b['id']}", headers=guest_a_hdrs)
    assert r.status_code == 403

    # ── Guest B cannot get Guest A's session details ──────────────────────

    r = client.get(f"{API}/sessions/{session_a['id']}", headers=guest_b_hdrs)
    assert r.status_code == 403


def test_anonymous_guest_cannot_misuse_share_id(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Anonymous guest cannot pass a different guest_share_id than their JWT claims.
    """
    agent, share_a = setup_guest_share_agent(
        client, superuser_token_headers,
        name="Mismatch Agent",
        share_label="Share A",
    )
    share_b = create_guest_share(
        client, superuser_token_headers, agent["id"], label="Share B",
    )

    guest_a_hdrs = guest_headers(client, share_a["token"], security_code=share_a["security_code"])

    # Guest A tries to create session with share B's ID → 403
    r = client.post(
        f"{API}/sessions/",
        headers=guest_a_hdrs,
        json={
            "agent_id": agent["id"],
            "mode": "conversation",
            "guest_share_id": share_b["id"],
        },
    )
    assert r.status_code == 403


def test_anonymous_guest_cannot_access_wrong_agent(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Anonymous guest cannot create session for a different agent than their JWT claims.
    """
    agent_a, share_a = setup_guest_share_agent(
        client, superuser_token_headers, name="Agent A",
    )
    agent_b, _ = setup_guest_share_agent(
        client, superuser_token_headers, name="Agent B",
    )

    guest_a_hdrs = guest_headers(client, share_a["token"], security_code=share_a["security_code"])

    # Guest with share for agent_a tries to create session on agent_b → 403
    r = client.post(
        f"{API}/sessions/",
        headers=guest_a_hdrs,
        json={
            "agent_id": agent_b["id"],
            "mode": "conversation",
        },
    )
    assert r.status_code == 403


def test_grant_user_session_lifecycle(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Authenticated user with grant can create and interact with guest share sessions:
      1. Owner creates agent + guest share
      2. Second user activates grant
      3. User creates session with guest_share_id → mode forced to conversation
      4. User lists sessions with guest_share_id filter → sees the session
      5. User gets session details → works
      6. User without grant cannot create session → 403
    """
    # ── Phase 1: Owner creates agent + guest share ───────────────────────

    agent, share = setup_guest_share_agent(
        client, superuser_token_headers,
        name="Grant Session Agent",
        share_label="Grant Session Link",
    )
    agent_id = agent["id"]
    share_id = share["id"]

    # ── Phase 2: User B activates grant ──────────────────────────────────

    _, user_b_headers = create_random_user_with_headers(client)
    activate_guest_grant(client, user_b_headers, share["token"], security_code=share["security_code"])

    # ── Phase 3: User B creates session with guest_share_id ──────────────

    user_session = create_session_via_api(
        client, user_b_headers, agent_id, guest_share_id=share_id,
    )
    assert user_session["guest_share_id"] == share_id
    assert user_session["mode"] == "conversation"
    session_id = user_session["id"]

    # ── Phase 4: User B lists sessions with guest_share_id filter ────────

    sessions = list_sessions(client, user_b_headers, guest_share_id=share_id)
    session_ids = [s["id"] for s in sessions]
    assert session_id in session_ids

    # ── Phase 5: User B gets session details ─────────────────────────────

    r = client.get(f"{API}/sessions/{session_id}", headers=user_b_headers)
    assert r.status_code == 200
    details = r.json()
    assert details["id"] == session_id
    assert details["guest_share_id"] == share_id

    # ── Phase 6: User C (no grant) cannot create session ─────────────────

    _, user_c_headers = create_random_user_with_headers(client)
    r = client.post(
        f"{API}/sessions/",
        headers=user_c_headers,
        json={
            "agent_id": agent_id,
            "mode": "conversation",
            "guest_share_id": share_id,
        },
    )
    assert r.status_code == 403


def test_grant_user_cannot_create_building_mode_session(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Authenticated user with grant cannot create a building mode guest share session.
    """
    agent, share = setup_guest_share_agent(
        client, superuser_token_headers,
        name="No Build Grant Agent",
    )

    _, user_headers = create_random_user_with_headers(client)
    activate_guest_grant(client, user_headers, share["token"], security_code=share["security_code"])

    r = client.post(
        f"{API}/sessions/",
        headers=user_headers,
        json={
            "agent_id": agent["id"],
            "mode": "building",
            "guest_share_id": share["id"],
        },
    )
    assert r.status_code == 400
    assert "conversation mode" in r.json()["detail"].lower()


def test_anonymous_guest_blocked_from_restricted_endpoints(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Anonymous guest JWT cannot access restricted endpoints
    (update session, switch mode, delete, recover, reset-sdk).
    """
    agent, share = setup_guest_share_agent(
        client, superuser_token_headers,
        name="Restricted Agent",
    )
    guest_hdrs = guest_headers(client, share["token"], security_code=share["security_code"])

    # Create a valid guest session first
    guest_session = create_session_via_api(client, guest_hdrs, agent["id"])
    session_id = guest_session["id"]

    # ── Update session → should fail ──────────────────────────────────────

    r = client.patch(
        f"{API}/sessions/{session_id}",
        headers=guest_hdrs,
        json={"title": "New Title"},
    )
    assert r.status_code in (403, 404, 400, 422), (
        f"Expected auth error, got {r.status_code}: {r.text}"
    )

    # ── Switch mode → should fail ─────────────────────────────────────────

    r = client.patch(
        f"{API}/sessions/{session_id}/mode?new_mode=building",
        headers=guest_hdrs,
    )
    assert r.status_code in (403, 404, 400, 422), (
        f"Expected auth error, got {r.status_code}: {r.text}"
    )

    # ── Delete session → should fail ──────────────────────────────────────

    r = client.delete(f"{API}/sessions/{session_id}", headers=guest_hdrs)
    assert r.status_code in (403, 404, 400, 422), (
        f"Expected auth error, got {r.status_code}: {r.text}"
    )

    # ── Reset SDK → should fail ───────────────────────────────────────────

    r = client.post(f"{API}/sessions/{session_id}/reset-sdk", headers=guest_hdrs)
    assert r.status_code in (403, 404, 400, 422), (
        f"Expected auth error, got {r.status_code}: {r.text}"
    )

    # ── Recover → should fail ─────────────────────────────────────────────

    r = client.post(f"{API}/sessions/{session_id}/recover", headers=guest_hdrs)
    assert r.status_code in (403, 404, 400, 422), (
        f"Expected auth error, got {r.status_code}: {r.text}"
    )


def test_owner_session_list_includes_guest_sessions(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Owner's session list includes guest share sessions.
      1. Owner creates agent + guest share
      2. Owner creates a normal session
      3. Guest creates a guest session
      4. Owner lists all sessions → both are visible
    """
    agent, share = setup_guest_share_agent(
        client, superuser_token_headers,
        name="Owner List Agent",
    )
    agent_id = agent["id"]

    # Owner creates a normal session
    owner_session = create_session_via_api(client, superuser_token_headers, agent_id)

    # Guest creates a guest session
    guest_hdrs = guest_headers(client, share["token"], security_code=share["security_code"])
    guest_session = create_session_via_api(
        client, guest_hdrs, agent_id, guest_share_id=share["id"],
    )

    # Owner lists sessions → both are visible
    sessions = list_sessions(client, superuser_token_headers)
    session_ids = [s["id"] for s in sessions]
    assert owner_session["id"] in session_ids
    assert guest_session["id"] in session_ids

    # Verify the guest session has guest_share_id set
    guest_s = next(s for s in sessions if s["id"] == guest_session["id"])
    assert guest_s["guest_share_id"] == share["id"]

    # Verify the owner session does not have guest_share_id set
    owner_s = next(s for s in sessions if s["id"] == owner_session["id"])
    assert owner_s["guest_share_id"] is None
