"""
Integration tests: deleting an AgentEnvironment detaches sessions instead of
cascading.

Two scenario-based tests:

  A. Detach-and-rebind
       The active env (env1) is deleted while a session is bound to it.
       The session survives with environment_id=None, messages intact.
       After env2 is activated and a new message is sent, the session
       rebinds to env2 and the agent replies successfully.

  B. Agent deletion still removes sessions
       Deleting the agent itself (not just its environment) must still
       cascade-delete all sessions. GET /sessions/{id} returns 404 after
       the agent is gone.
"""
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.stubs.agent_env_stub import StubAgentEnvConnector
from tests.utils.agent import create_agent_via_api
from tests.utils.background_tasks import drain_tasks
from tests.utils.environment import (
    activate_environment,
    create_environment,
    delete_environment,
    list_environments,
)
from tests.utils.message import get_messages_by_role, list_messages, send_message
from tests.utils.session import create_session_via_api

_BASE_SESSIONS = f"{settings.API_V1_STR}/sessions"
_BASE_AGENTS = f"{settings.API_V1_STR}/agents"


# ---------------------------------------------------------------------------
# Scenario A: deleting env1 detaches the bound session; rebind on next send
# ---------------------------------------------------------------------------

def test_env_delete_detaches_session_and_rebinds(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Detach-and-rebind scenario:
      1.  Create agent → env1 is auto-created and activated.  Drain.
      2.  Create env2.  Do NOT activate it yet.  Drain.
      3.  Create a session → session.environment_id == env1.id.
      4.  Delete env1.
      5.  GET session → session still exists; environment_id is None.
          GET session messages → empty list (session never received a message).
      6.  Send a message while agent has no active environment → error
          "Agent has no active environment" (pins the no-active-env branch
          of ``SessionService.resolve_and_rebind_session_environment``).
      7.  Activate env2.  Drain.
      8.  Send a message with StubAgentEnvConnector, drain.
      9.  Agent replied → exactly one agent-role message in session.
          GET session → environment_id == env2.id  (rebind happened).
    """
    # ── Phase 1: Create agent → env1 active ──────────────────────────────
    agent = create_agent_via_api(client, superuser_token_headers)
    agent_id = agent["id"]
    drain_tasks()

    envs = list_environments(client, superuser_token_headers, agent_id)
    assert envs["count"] == 1
    env1 = envs["data"][0]
    env1_id = env1["id"]
    assert env1["is_active"] is True

    # ── Phase 2: Create env2 (not yet active) ────────────────────────────
    env2 = create_environment(
        client, superuser_token_headers, agent_id,
        instance_name="Secondary",
    )
    env2_id = env2["id"]
    drain_tasks()  # env2 build completes; is_active remains False

    # ── Phase 3: Create session → bound to env1 ──────────────────────────
    session_data = create_session_via_api(client, superuser_token_headers, agent_id)
    session_id = session_data["id"]
    assert session_data["environment_id"] == env1_id, (
        f"Expected session bound to env1 ({env1_id}), got {session_data['environment_id']}"
    )

    # ── Phase 4: Delete env1 ─────────────────────────────────────────────
    delete_environment(client, superuser_token_headers, env1_id)

    # env1 is gone
    r = client.get(f"{settings.API_V1_STR}/environments/{env1_id}", headers=superuser_token_headers)
    assert r.status_code == 404

    # ── Phase 5: Session still accessible; environment_id is None ────────
    r = client.get(f"{_BASE_SESSIONS}/{session_id}", headers=superuser_token_headers)
    assert r.status_code == 200, f"Session should survive env deletion; got {r.text}"
    fetched = r.json()
    assert fetched["id"] == session_id
    assert fetched["environment_id"] is None, (
        f"Expected environment_id=None after env1 deleted, got {fetched['environment_id']}"
    )

    # No prior messages had been sent to this session, so the list is empty
    # but still reachable (messages table survived the env delete).
    msgs = list_messages(client, superuser_token_headers, session_id)
    assert msgs == [], f"Expected empty message list after detach, got {msgs!r}"

    # ── Phase 6: Send while no active env → "Agent has no active environment" ─
    # env1 is gone and env2 has not been activated yet, so the agent currently
    # has no active_environment_id. A send must fail rather than silently
    # falling back to a non-active environment.
    r = client.post(
        f"{_BASE_SESSIONS}/{session_id}/messages/stream",
        headers=superuser_token_headers,
        json={"content": "Anyone home?"},
    )
    assert r.status_code >= 400, (
        f"Expected error status when agent has no active env, got {r.status_code}: {r.text}"
    )
    assert "no active environment" in r.text.lower(), (
        f"Expected 'no active environment' in error body, got: {r.text}"
    )

    # ── Phase 7: Activate env2 ────────────────────────────────────────────
    activate_environment(client, superuser_token_headers, agent_id, env2_id)
    drain_tasks()

    # ── Phase 8: Send message → agent streams via env2 ────────────────────
    stub = StubAgentEnvConnector(response_text="Hello from env2!")
    with patch("app.services.sessions.message_service.agent_env_connector", stub):
        send_message(client, superuser_token_headers, session_id, content="Are you there?")
        drain_tasks()

    # ── Phase 9: Agent replied; session rebound to env2 ───────────────────
    agent_msgs = get_messages_by_role(client, superuser_token_headers, session_id, "agent")
    assert len(agent_msgs) == 1, f"Expected exactly one agent reply after rebind, got {len(agent_msgs)}"
    assert "Hello from env2!" in agent_msgs[0]["content"]

    r = client.get(f"{_BASE_SESSIONS}/{session_id}", headers=superuser_token_headers)
    assert r.status_code == 200
    rebound = r.json()
    assert rebound["environment_id"] == env2_id, (
        f"Expected session rebound to env2 ({env2_id}), got {rebound['environment_id']}"
    )


# ---------------------------------------------------------------------------
# Scenario B: deleting the agent still removes its sessions
# ---------------------------------------------------------------------------

def test_agent_delete_cascades_sessions(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Agent deletion cascades to sessions:
      1.  Create agent.  Drain.
      2.  Create session.  Send a message to establish state.  Drain.
      3.  DELETE the agent.
      4.  GET /sessions/{id} → 404 (session deleted along with agent).
    """
    # ── Phase 1: Create agent ─────────────────────────────────────────────
    agent = create_agent_via_api(client, superuser_token_headers)
    agent_id = agent["id"]
    drain_tasks()

    # ── Phase 2: Create session and send a message ────────────────────────
    session_data = create_session_via_api(client, superuser_token_headers, agent_id)
    session_id = session_data["id"]

    stub = StubAgentEnvConnector(response_text="I'm here.")
    with patch("app.services.sessions.message_service.agent_env_connector", stub):
        send_message(client, superuser_token_headers, session_id, content="Hello!")
        drain_tasks()

    # Confirm session is healthy before agent deletion
    r = client.get(f"{_BASE_SESSIONS}/{session_id}", headers=superuser_token_headers)
    assert r.status_code == 200

    # ── Phase 3: Delete the agent ─────────────────────────────────────────
    r = client.delete(f"{_BASE_AGENTS}/{agent_id}", headers=superuser_token_headers)
    assert r.status_code == 200, f"Agent delete failed: {r.text}"

    # ── Phase 4: Session is gone ──────────────────────────────────────────
    r = client.get(f"{_BASE_SESSIONS}/{session_id}", headers=superuser_token_headers)
    assert r.status_code == 404, (
        f"Expected 404 after agent deletion, got {r.status_code}: {r.text}"
    )
