"""
Integration tests: Webapp Chat feature.

Covers the webapp chat endpoints under /api/v1/webapp/{token}/chat/...:
  A. Chat disabled (chat_mode=None) → 403 on all chat endpoints
  B. Create / get active session lifecycle (idempotency, correct mode)
  C. Session access control (wrong webapp_share → 403)
  D. Message list for a chat session
  E. Unauthenticated and wrong-token-type access → 401/403
  F. chat_mode "conversation" and "building" produce correctly-typed sessions

Business rules tested:
  1. POST /sessions returns 403 when chat_mode is None
  2. POST /sessions creates a session with mode matching chat_mode config
  3. POST /sessions is idempotent — second call returns the same session id
  4. GET /sessions returns null before a session exists, returns the session after creation
  5. GET /sessions/{id} returns the session and verifies webapp_share_id ownership
  6. GET /sessions/{id} with a different webapp share's JWT → 403
  7. GET /sessions/{id}/messages returns a MessagesPublic response (data/count)
  8. All endpoints return 401 with no Bearer token
  9. All endpoints return 403 with a regular user JWT (wrong token type)
  10. chat_mode="conversation" → session.mode == "conversation"
  11. chat_mode="building" → session.mode == "building"
"""
import uuid

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.background_tasks import drain_tasks
from tests.utils.user import create_random_user_with_headers
from tests.utils.webapp_interface_config import update_webapp_interface_config
from tests.utils.webapp_share import (
    authenticate_webapp_share,
    setup_webapp_agent,
)

API = settings.API_V1_STR


# ── Helpers ───────────────────────────────────────────────────────────────


def _get_webapp_jwt(client: TestClient, token: str) -> str:
    """Authenticate via webapp share and return the access_token string."""
    auth = authenticate_webapp_share(client, token)
    assert "access_token" in auth, f"No access_token in auth response: {auth}"
    return auth["access_token"]


def _webapp_headers(client: TestClient, token: str) -> dict[str, str]:
    """Return Authorization headers for the webapp-viewer JWT."""
    return {"Authorization": f"Bearer {_get_webapp_jwt(client, token)}"}


def _chat_base(token: str) -> str:
    """Base URL for chat endpoints for a given webapp share token."""
    return f"{API}/webapp/{token}/chat"


def _enable_chat(
    client: TestClient,
    headers: dict[str, str],
    agent_id: str,
    chat_mode: str = "conversation",
) -> None:
    """Enable chat for an agent by setting chat_mode via the config endpoint."""
    update_webapp_interface_config(client, headers, agent_id, chat_mode=chat_mode)


def _create_chat_session(
    client: TestClient,
    webapp_hdrs: dict[str, str],
    share_token: str,
) -> dict:
    """POST /webapp/{token}/chat/sessions and assert 200. Returns session dict."""
    r = client.post(
        f"{_chat_base(share_token)}/sessions",
        headers=webapp_hdrs,
    )
    assert r.status_code == 200, f"Create chat session failed: {r.text}"
    return r.json()


# ── A. Chat disabled ──────────────────────────────────────────────────────


def test_chat_disabled_returns_403(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    When chat_mode is None (default), all chat endpoints return 403:
      1. Create agent + webapp share (chat_mode stays None)
      2. Authenticate as webapp viewer
      3. POST /sessions → 403
      4. GET /sessions → 403
    """
    # ── Phase 1: Create agent with webapp share, leave chat_mode=None ─────
    agent, share = setup_webapp_agent(
        client, superuser_token_headers,
        name="Chat Disabled Agent",
        share_label="Disabled Share",
    )
    share_token = share["token"]

    # ── Phase 2: Authenticate as webapp viewer ────────────────────────────
    webapp_hdrs = _webapp_headers(client, share_token)

    # ── Phase 3: POST /sessions → 403 chat disabled ───────────────────────
    r = client.post(
        f"{_chat_base(share_token)}/sessions",
        headers=webapp_hdrs,
    )
    assert r.status_code == 403, f"Expected 403 for disabled chat, got {r.status_code}: {r.text}"
    assert "chat" in r.json()["detail"].lower()

    # ── Phase 4: GET /sessions → 403 chat disabled ────────────────────────
    r = client.get(
        f"{_chat_base(share_token)}/sessions",
        headers=webapp_hdrs,
    )
    assert r.status_code == 403, f"Expected 403 for disabled chat GET, got {r.status_code}: {r.text}"


# ── B. Session lifecycle ──────────────────────────────────────────────────


def test_webapp_chat_session_full_lifecycle(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Full webapp chat session lifecycle with chat_mode="conversation":
      1. Create agent + webapp share
      2. Enable chat (chat_mode="conversation")
      3. Authenticate as webapp viewer
      4. GET /sessions → null (no session yet)
      5. POST /sessions → creates session; verify mode, webapp_share_id fields
      6. GET /sessions → returns the created session
      7. GET /sessions/{id} → returns session details
      8. POST /sessions again → idempotent, returns the same session id
      9. GET /sessions/{id}/messages → returns MessagesPublic (data list + count)
    """
    # ── Phase 1: Create agent + webapp share ──────────────────────────────
    agent, share = setup_webapp_agent(
        client, superuser_token_headers,
        name="Chat Lifecycle Agent",
        share_label="Lifecycle Share",
    )
    agent_id = agent["id"]
    share_token = share["token"]

    # ── Phase 2: Enable chat ───────────────────────────────────────────────
    _enable_chat(client, superuser_token_headers, agent_id, chat_mode="conversation")

    # ── Phase 3: Authenticate as webapp viewer ────────────────────────────
    webapp_hdrs = _webapp_headers(client, share_token)

    # ── Phase 4: GET /sessions → null before any session ─────────────────
    r = client.get(f"{_chat_base(share_token)}/sessions", headers=webapp_hdrs)
    assert r.status_code == 200, f"GET sessions failed: {r.text}"
    assert r.json() is None, f"Expected null, got: {r.json()}"

    # ── Phase 5: POST /sessions → creates session ─────────────────────────
    session = _create_chat_session(client, webapp_hdrs, share_token)
    session_id = session["id"]

    # Verify session shape
    assert "id" in session
    assert "mode" in session
    assert "status" in session
    assert session["mode"] == "conversation"
    assert session["status"] == "active"

    # webapp_share_id should be set (ties session to this share)
    assert session.get("webapp_share_id") == share["id"]

    # ── Phase 6: GET /sessions → returns the active session ───────────────
    r = client.get(f"{_chat_base(share_token)}/sessions", headers=webapp_hdrs)
    assert r.status_code == 200, f"GET sessions after create failed: {r.text}"
    active = r.json()
    assert active is not None, "Expected active session, got null"
    assert active["id"] == session_id

    # ── Phase 7: GET /sessions/{id} → returns session details ────────────
    r = client.get(
        f"{_chat_base(share_token)}/sessions/{session_id}",
        headers=webapp_hdrs,
    )
    assert r.status_code == 200, f"GET session by id failed: {r.text}"
    details = r.json()
    assert details["id"] == session_id
    assert details["mode"] == "conversation"
    assert details["status"] == "active"

    # ── Phase 8: POST /sessions again → idempotent ────────────────────────
    second = _create_chat_session(client, webapp_hdrs, share_token)
    assert second["id"] == session_id, (
        f"Expected same session on second POST, got {second['id']} != {session_id}"
    )

    # ── Phase 9: GET /sessions/{id}/messages → MessagesPublic ────────────
    r = client.get(
        f"{_chat_base(share_token)}/sessions/{session_id}/messages",
        headers=webapp_hdrs,
    )
    assert r.status_code == 200, f"GET messages failed: {r.text}"
    body = r.json()
    assert "data" in body, f"Missing 'data' key in messages response: {body}"
    assert "count" in body, f"Missing 'count' key in messages response: {body}"
    assert isinstance(body["data"], list)
    assert body["count"] == len(body["data"])


# ── C. Session access control ─────────────────────────────────────────────


def test_webapp_chat_session_access_control(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Session access is scoped to the webapp share in the JWT:
      1. Create agent with chat enabled, two webapp shares (A and B)
      2. Share A creates a session
      3. Share B authenticates
      4. Share B GET /sessions/{sessionA_id} → 403 (wrong share)
      5. Share B GET /sessions → null (no session for share B yet)
      6. Share B POST /sessions → creates a different session (B's own)
      7. Share A GET /sessions/{sessionB_id} → 403
    """
    # ── Phase 1: Create agent with two webapp shares ──────────────────────
    agent, share_a = setup_webapp_agent(
        client, superuser_token_headers,
        name="Chat Access Control Agent",
        share_label="Share A",
    )
    agent_id = agent["id"]
    _enable_chat(client, superuser_token_headers, agent_id, chat_mode="conversation")

    # Create second webapp share for the same agent
    from tests.utils.webapp_share import create_webapp_share
    share_b = create_webapp_share(
        client, superuser_token_headers, agent_id, label="Share B"
    )

    # ── Phase 2: Share A creates a session ───────────────────────────────
    webapp_a_hdrs = _webapp_headers(client, share_a["token"])
    session_a = _create_chat_session(client, webapp_a_hdrs, share_a["token"])
    session_a_id = session_a["id"]

    # ── Phase 3: Share B authenticates ────────────────────────────────────
    webapp_b_hdrs = _webapp_headers(client, share_b["token"])

    # ── Phase 4: Share B cannot access Share A's session ─────────────────
    r = client.get(
        f"{_chat_base(share_b['token'])}/sessions/{session_a_id}",
        headers=webapp_b_hdrs,
    )
    assert r.status_code == 403, (
        f"Expected 403 for cross-share session access, got {r.status_code}: {r.text}"
    )

    # ── Phase 5: Share B GET /sessions → null (no session for B yet) ──────
    r = client.get(f"{_chat_base(share_b['token'])}/sessions", headers=webapp_b_hdrs)
    assert r.status_code == 200
    assert r.json() is None, f"Expected null for share B, got: {r.json()}"

    # ── Phase 6: Share B creates its own session ──────────────────────────
    session_b = _create_chat_session(client, webapp_b_hdrs, share_b["token"])
    session_b_id = session_b["id"]
    assert session_b_id != session_a_id, "Share B should have a different session than Share A"

    # ── Phase 7: Share A cannot access Share B's session ─────────────────
    r = client.get(
        f"{_chat_base(share_a['token'])}/sessions/{session_b_id}",
        headers=webapp_a_hdrs,
    )
    assert r.status_code == 403, (
        f"Expected 403 for Share A accessing Share B's session, got {r.status_code}: {r.text}"
    )


# ── D. Unauthenticated and wrong-token-type access ────────────────────────


def test_webapp_chat_unauthenticated_and_wrong_token(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    All chat endpoints reject unauthenticated and wrong-token-type requests:
      1. Create agent with chat enabled
      2. No Bearer token → 401 on all endpoints
      3. Regular user JWT (not webapp-viewer) → 403 on all endpoints
    """
    # ── Phase 1: Create agent with chat enabled ───────────────────────────
    agent, share = setup_webapp_agent(
        client, superuser_token_headers,
        name="Chat Auth Guard Agent",
        share_label="Auth Guard Share",
    )
    agent_id = agent["id"]
    share_token = share["token"]
    _enable_chat(client, superuser_token_headers, agent_id, chat_mode="conversation")

    # Create a session using valid webapp JWT first (so session_id exists)
    webapp_hdrs = _webapp_headers(client, share_token)
    session = _create_chat_session(client, webapp_hdrs, share_token)
    session_id = session["id"]

    base = _chat_base(share_token)

    # ── Phase 2: No Bearer token → 401 ───────────────────────────────────
    assert client.post(f"{base}/sessions").status_code == 401
    assert client.get(f"{base}/sessions").status_code == 401
    assert client.get(f"{base}/sessions/{session_id}").status_code == 401
    assert client.get(f"{base}/sessions/{session_id}/messages").status_code == 401

    # ── Phase 3: Regular user JWT (token_type != webapp_share) → 403 ──────
    _, regular_user_headers = create_random_user_with_headers(client)

    r = client.post(f"{base}/sessions", headers=regular_user_headers)
    assert r.status_code == 403, (
        f"Expected 403 for regular user on POST /sessions, got {r.status_code}: {r.text}"
    )

    r = client.get(f"{base}/sessions", headers=regular_user_headers)
    assert r.status_code == 403, (
        f"Expected 403 for regular user on GET /sessions, got {r.status_code}: {r.text}"
    )

    r = client.get(f"{base}/sessions/{session_id}", headers=regular_user_headers)
    assert r.status_code == 403, (
        f"Expected 403 for regular user on GET /sessions/{{id}}, got {r.status_code}: {r.text}"
    )

    r = client.get(f"{base}/sessions/{session_id}/messages", headers=regular_user_headers)
    assert r.status_code == 403, (
        f"Expected 403 for regular user on GET /sessions/{{id}}/messages, got {r.status_code}: {r.text}"
    )


# ── E. chat_mode values produce correct session modes ─────────────────────


def test_webapp_chat_mode_conversation(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    When chat_mode="conversation", created sessions have mode="conversation".
      1. Create agent + webapp share
      2. Set chat_mode="conversation"
      3. Authenticate and create session → mode == "conversation"
    """
    agent, share = setup_webapp_agent(
        client, superuser_token_headers,
        name="Chat Conversation Mode Agent",
    )
    _enable_chat(client, superuser_token_headers, agent["id"], chat_mode="conversation")

    webapp_hdrs = _webapp_headers(client, share["token"])
    session = _create_chat_session(client, webapp_hdrs, share["token"])

    assert session["mode"] == "conversation", (
        f"Expected mode='conversation', got '{session['mode']}'"
    )


def test_webapp_chat_mode_building(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    When chat_mode="building", created sessions have mode="building".
      1. Create agent + webapp share
      2. Set chat_mode="building"
      3. Authenticate and create session → mode == "building"
    """
    agent, share = setup_webapp_agent(
        client, superuser_token_headers,
        name="Chat Building Mode Agent",
    )
    _enable_chat(client, superuser_token_headers, agent["id"], chat_mode="building")

    webapp_hdrs = _webapp_headers(client, share["token"])
    session = _create_chat_session(client, webapp_hdrs, share["token"])

    assert session["mode"] == "building", (
        f"Expected mode='building', got '{session['mode']}'"
    )


# ── F. Chat mode transition: disable after session created ────────────────


def test_webapp_chat_disabled_after_session_created(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    If chat is disabled after a session is created, subsequent session creation
    is blocked:
      1. Create agent + webapp share, enable chat
      2. Create a session successfully
      3. Owner disables chat (chat_mode=None)
      4. GET new webapp JWT (re-auth)
      5. POST /sessions → 403
      6. GET /sessions → 403
    """
    # ── Phase 1: Create agent + share, enable chat ────────────────────────
    agent, share = setup_webapp_agent(
        client, superuser_token_headers,
        name="Chat Toggle Agent",
        share_label="Toggle Share",
    )
    agent_id = agent["id"]
    share_token = share["token"]
    _enable_chat(client, superuser_token_headers, agent_id, chat_mode="conversation")

    # ── Phase 2: Create session successfully ──────────────────────────────
    webapp_hdrs = _webapp_headers(client, share_token)
    session = _create_chat_session(client, webapp_hdrs, share_token)
    assert session["id"]

    # ── Phase 3: Owner disables chat ──────────────────────────────────────
    update_webapp_interface_config(
        client, superuser_token_headers, agent_id, chat_mode=None
    )

    # ── Phase 4: Get a fresh webapp JWT ───────────────────────────────────
    webapp_hdrs_new = _webapp_headers(client, share_token)

    # ── Phase 5: POST /sessions → 403 ────────────────────────────────────
    r = client.post(
        f"{_chat_base(share_token)}/sessions",
        headers=webapp_hdrs_new,
    )
    assert r.status_code == 403, (
        f"Expected 403 after disabling chat, got {r.status_code}: {r.text}"
    )

    # ── Phase 6: GET /sessions → 403 ─────────────────────────────────────
    r = client.get(
        f"{_chat_base(share_token)}/sessions",
        headers=webapp_hdrs_new,
    )
    assert r.status_code == 403, (
        f"Expected 403 on GET /sessions after disabling chat, got {r.status_code}: {r.text}"
    )


# ── G. Non-existent session ───────────────────────────────────────────────


def test_webapp_chat_nonexistent_session_returns_error(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    GET /sessions/{ghost_id} with a valid webapp JWT but non-existent session_id
    returns 404.
    """
    agent, share = setup_webapp_agent(
        client, superuser_token_headers,
        name="Chat 404 Agent",
    )
    _enable_chat(client, superuser_token_headers, agent["id"], chat_mode="conversation")

    webapp_hdrs = _webapp_headers(client, share["token"])
    ghost_id = str(uuid.uuid4())

    r = client.get(
        f"{_chat_base(share['token'])}/sessions/{ghost_id}",
        headers=webapp_hdrs,
    )
    assert r.status_code == 404, (
        f"Expected 404 for ghost session id, got {r.status_code}: {r.text}"
    )

    r = client.get(
        f"{_chat_base(share['token'])}/sessions/{ghost_id}/messages",
        headers=webapp_hdrs,
    )
    assert r.status_code in (403, 404), (
        f"Expected 403 or 404 for ghost session messages, got {r.status_code}: {r.text}"
    )
