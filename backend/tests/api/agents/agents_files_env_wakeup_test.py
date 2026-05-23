"""
Integration tests: slash command auto-wakeup behavior.

Tests the Phase 1.5 logic in SessionService.send_session_message that
auto-wakes a suspended/stopped environment before dispatching
requires_running_environment commands.

Scenarios:
  1. /files on a suspended env → ensure_environment_ready_for_streaming called,
     env woken to "running", file listing returned (no error)
  2. /files-all on a suspended env → same auto-wakeup path
  3. /files on an already-running env → no wakeup attempted, listing returned
  4. Regression guard: /webapp (requires_running_environment=False) on a
     suspended env → ensure_environment_ready_for_streaming NOT called
  5. Wakeup failure: ensure_environment_ready_for_streaming raises RuntimeError
     → HTTP 500 with friendly error detail (no command message persisted)
  6. /agent-status on a suspended env → wakeup triggered (pre-wake so live
     STATUS.md fetch goes through immediately)
  7. /session-recover on a suspended env → wakeup triggered (recovery typically
     precedes a retry, so wake the env preemptively)
  8. /session-reset on a suspended env → wakeup triggered (reset typically
     precedes a fresh conversation, so wake the env preemptively)

Notes:
  - ensure_environment_ready_for_streaming is a staticmethod on SessionService.
    We patch it at its canonical location using AsyncMock.  The mock also flips
    the environment row to status="running" so that subsequent internal reads
    (e.g. in the command handler's create_session() call) see the correct state.
  - /webapp is a good regression guard because it defaults to
    requires_running_environment=False (the CommandHandler base default).
  - send_message() helper asserts status_code==200; for the failure path we call
    the endpoint directly to check the 500 response.
  - For /agent-status, /session-recover, /session-reset: assertion is solely
    on the wakeup call. Each handler's own behavior is covered by its
    dedicated test file (agents_status_test.py, agents_session_recovery_test.py,
    agents_session_reset_command_test.py).
"""
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.core.config import settings
from app.models.environments.environment import AgentEnvironment
from tests.stubs.agent_env_stub import StubAgentEnvConnector
from tests.stubs.environment_adapter_stub import EnvironmentTestAdapter
from tests.utils.agent import create_agent_via_api, get_agent
from tests.utils.background_tasks import drain_tasks
from tests.utils.message import list_messages, send_message
from tests.utils.session import create_session_via_api


_API = settings.API_V1_STR


# ── Helpers ───────────────────────────────────────────────────────────────────


def _suspend_environment(
    client: TestClient,
    headers: dict[str, str],
    env_id: str,
) -> None:
    """Suspend the environment via API; asserts the status changes."""
    r = client.post(
        f"{_API}/environments/{env_id}/suspend",
        headers=headers,
    )
    assert r.status_code == 200, f"suspend failed: {r.text}"
    r2 = client.get(f"{_API}/environments/{env_id}", headers=headers)
    assert r2.status_code == 200
    assert r2.json()["status"] == "suspended", (
        f"Expected 'suspended', got {r2.json()['status']!r}"
    )


def _get_command_messages(client, headers, session_id):
    """Return system messages that carry command metadata."""
    return [
        m for m in list_messages(client, headers, session_id)
        if m["role"] == "system" and m.get("message_metadata", {}).get("command") is True
    ]


def _make_wakeup_mock(db: Session, env_id: str):
    """
    Build an AsyncMock for SessionService.ensure_environment_ready_for_streaming
    that flips the environment row to status='running' and returns (env, agent).

    Using the patched create_session (pointing at the test DB session) means we
    can read/write the environment row through the test transaction and all
    subsequent in-process reads will see the updated status.
    """
    async def _fake_ensure(session_id, get_fresh_db_session, timeout_seconds=120):
        from uuid import UUID
        env = db.get(AgentEnvironment, UUID(env_id))
        assert env is not None, f"Environment {env_id!r} not found in test DB"
        env.status = "running"
        db.add(env)
        db.commit()
        # Return (environment, agent) as the real method does
        return env, None

    return AsyncMock(side_effect=_fake_ensure)


# ── Scenario 1 + 2: /files and /files-all wake a suspended env ───────────────


def test_files_command_auto_wakes_suspended_env(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
    patch_environment_adapter,
) -> None:
    """
    /files invoked when env is suspended → env is auto-woken → listing returned.

    Scenario:
      1. Create agent + session (env starts running via stub)
      2. Suspend the environment via API
      3. Configure workspace with a file for the adapter
      4. Send /files — patch ensure_environment_ready_for_streaming to flip env
         back to running and return immediately
      5. Verify command_executed=True and file listing in response (not error)
      6. Verify ensure_environment_ready_for_streaming was called exactly once
      7. Verify no LLM call was made (command is sync, not streaming)
    """
    headers = superuser_token_headers

    # ── Phase 1: Create agent and session ─────────────────────────────
    agent = create_agent_via_api(client, headers)
    drain_tasks()
    agent = get_agent(client, headers, agent["id"])
    agent_id = agent["id"]
    env_id = agent["active_environment_id"]

    session_data = create_session_via_api(client, headers, agent_id)
    session_id = session_data["id"]

    # ── Phase 2: Suspend environment ──────────────────────────────────
    _suspend_environment(client, headers, env_id)

    # ── Phase 3: Configure workspace with a file ──────────────────────
    shared_adapter = EnvironmentTestAdapter()

    _workspace_tree = {
        "files": {
            "name": "files",
            "type": "directory",
            "children": [
                {
                    "name": "report.csv",
                    "type": "file",
                    "path": "files/report.csv",
                    "size": 2048,
                },
            ],
        },
    }

    async def _workspace():
        return _workspace_tree

    shared_adapter.get_workspace_tree = _workspace
    patch_environment_adapter.get_adapter = lambda env: shared_adapter

    # ── Phase 4: Send /files with mocked wakeup ───────────────────────
    stub = StubAgentEnvConnector(response_text="irrelevant")
    wakeup_mock = _make_wakeup_mock(db, env_id)

    with (
        patch("app.services.sessions.message_service.agent_env_connector", stub),
        patch(
            "app.services.sessions.session_service.SessionService.ensure_environment_ready_for_streaming",
            wakeup_mock,
        ),
    ):
        result = send_message(client, headers, session_id, content="/files")
        drain_tasks()

    # ── Phase 5: Verify listing returned ─────────────────────────────
    assert result.get("command_executed") is True, (
        f"Expected command_executed=True, got: {result}"
    )

    cmd_msgs = _get_command_messages(client, headers, session_id)
    assert len(cmd_msgs) == 1
    assert "report.csv" in cmd_msgs[0]["content"], (
        f"Expected file listing in response, got: {cmd_msgs[0]['content']!r}"
    )
    assert "(2.0 KB)" in cmd_msgs[0]["content"]
    assert cmd_msgs[0]["message_metadata"]["command_name"] == "/files"

    # Must NOT be the old "Environment is not running" error message
    assert "Environment is not running" not in cmd_msgs[0]["content"]

    # ── Phase 6: Wakeup was called exactly once ────────────────────────
    assert wakeup_mock.call_count == 1, (
        f"Expected ensure_environment_ready_for_streaming called once, "
        f"got {wakeup_mock.call_count}"
    )

    # ── Phase 7: No LLM call made ─────────────────────────────────────
    assert len(stub.stream_calls) == 0


def test_files_all_command_auto_wakes_suspended_env(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
    patch_environment_adapter,
) -> None:
    """
    /files-all invoked when env is suspended → env is auto-woken → full listing.

    Scenario:
      1. Create agent + session
      2. Suspend environment
      3. Configure multi-section workspace
      4. Send /files-all with mocked wakeup
      5. Verify all sections present in response
      6. Verify wakeup called once
    """
    headers = superuser_token_headers

    # ── Phase 1: Create agent + session ──────────────────────────────
    agent = create_agent_via_api(client, headers)
    drain_tasks()
    agent = get_agent(client, headers, agent["id"])
    agent_id = agent["id"]
    env_id = agent["active_environment_id"]

    session_data = create_session_via_api(client, headers, agent_id)
    session_id = session_data["id"]

    # ── Phase 2: Suspend ──────────────────────────────────────────────
    _suspend_environment(client, headers, env_id)

    # ── Phase 3: Multi-section workspace ──────────────────────────────
    shared_adapter = EnvironmentTestAdapter()

    _workspace_tree = {
        "files": {
            "name": "files",
            "type": "directory",
            "children": [{"name": "data.csv", "type": "file", "path": "files/data.csv", "size": 512}],
        },
        "scripts": {
            "name": "scripts",
            "type": "directory",
            "children": [{"name": "run.py", "type": "file", "path": "scripts/run.py", "size": 1024}],
        },
    }

    async def _workspace():
        return _workspace_tree

    shared_adapter.get_workspace_tree = _workspace
    patch_environment_adapter.get_adapter = lambda env: shared_adapter

    # ── Phase 4: Send /files-all ──────────────────────────────────────
    stub = StubAgentEnvConnector(response_text="irrelevant")
    wakeup_mock = _make_wakeup_mock(db, env_id)

    with (
        patch("app.services.sessions.message_service.agent_env_connector", stub),
        patch(
            "app.services.sessions.session_service.SessionService.ensure_environment_ready_for_streaming",
            wakeup_mock,
        ),
    ):
        result = send_message(client, headers, session_id, content="/files-all")
        drain_tasks()

    # ── Phase 5: All sections in response ────────────────────────────
    assert result.get("command_executed") is True

    cmd_msgs = _get_command_messages(client, headers, session_id)
    assert len(cmd_msgs) == 1
    content = cmd_msgs[0]["content"]

    assert "data.csv" in content
    assert "run.py" in content
    assert "**Files**" in content
    assert "**Scripts**" in content
    assert cmd_msgs[0]["message_metadata"]["command_name"] == "/files-all"
    assert "Environment is not running" not in content

    # ── Phase 6: Wakeup called once ───────────────────────────────────
    assert wakeup_mock.call_count == 1

    # No LLM call
    assert len(stub.stream_calls) == 0


# ── Scenario 3: /files on a running env does NOT trigger wakeup ───────────────


def test_files_command_running_env_no_wakeup(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
    patch_environment_adapter,
) -> None:
    """
    /files when env is already running → normal listing, wakeup NOT called.

    Scenario:
      1. Create agent + session (env is running)
      2. Do NOT suspend; send /files immediately
      3. Verify command_executed=True and listing returned
      4. Verify ensure_environment_ready_for_streaming was NOT called
    """
    headers = superuser_token_headers

    # ── Phase 1: Create agent + session ──────────────────────────────
    agent = create_agent_via_api(client, headers)
    drain_tasks()
    agent = get_agent(client, headers, agent["id"])
    agent_id = agent["id"]

    session_data = create_session_via_api(client, headers, agent_id)
    session_id = session_data["id"]

    # ── Phase 2: Workspace with files ────────────────────────────────
    shared_adapter = EnvironmentTestAdapter()

    async def _workspace():
        return {
            "files": {
                "name": "files",
                "type": "directory",
                "children": [
                    {"name": "output.txt", "type": "file", "path": "files/output.txt", "size": 128},
                ],
            },
        }

    shared_adapter.get_workspace_tree = _workspace
    patch_environment_adapter.get_adapter = lambda env: shared_adapter

    # ── Phase 3: Send /files — track wakeup calls ─────────────────────
    stub = StubAgentEnvConnector(response_text="irrelevant")
    wakeup_mock = AsyncMock(return_value=(None, None))

    with (
        patch("app.services.sessions.message_service.agent_env_connector", stub),
        patch(
            "app.services.sessions.session_service.SessionService.ensure_environment_ready_for_streaming",
            wakeup_mock,
        ),
    ):
        result = send_message(client, headers, session_id, content="/files")
        drain_tasks()

    # ── Phase 4: Listing returned, wakeup NOT called ──────────────────
    assert result.get("command_executed") is True

    cmd_msgs = _get_command_messages(client, headers, session_id)
    assert len(cmd_msgs) == 1
    assert "output.txt" in cmd_msgs[0]["content"]

    # Wakeup must NOT have been called (env was running)
    assert wakeup_mock.call_count == 0, (
        f"ensure_environment_ready_for_streaming should not be called for a "
        f"running env, but was called {wakeup_mock.call_count} time(s)"
    )

    assert len(stub.stream_calls) == 0


# ── Scenario 4: /webapp (non-env-requiring) does NOT trigger wakeup ───────────


def test_webapp_command_suspended_env_no_wakeup(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
    patch_environment_adapter,
) -> None:
    """
    Regression guard: /webapp with env suspended → wakeup NOT triggered.

    /webapp has requires_running_environment=False (the CommandHandler base default),
    so even with a suspended environment the Phase 1.5 wakeup gate must be skipped.

    Scenario:
      1. Create agent + session
      2. Suspend environment
      3. Send /webapp (webapp_enabled=False by default → "No Web App available")
      4. Verify command_executed=True and response is the expected webapp message
      5. Verify ensure_environment_ready_for_streaming was NOT called
    """
    headers = superuser_token_headers

    # ── Phase 1: Create agent + session ──────────────────────────────
    agent = create_agent_via_api(client, headers)
    drain_tasks()
    agent = get_agent(client, headers, agent["id"])
    agent_id = agent["id"]
    env_id = agent["active_environment_id"]

    session_data = create_session_via_api(client, headers, agent_id)
    session_id = session_data["id"]

    # ── Phase 2: Suspend environment ──────────────────────────────────
    _suspend_environment(client, headers, env_id)

    # ── Phase 3: Send /webapp with wakeup spy ────────────────────────
    stub = StubAgentEnvConnector(response_text="irrelevant")
    wakeup_mock = AsyncMock(return_value=(None, None))

    with (
        patch("app.services.sessions.message_service.agent_env_connector", stub),
        patch(
            "app.services.sessions.session_service.SessionService.ensure_environment_ready_for_streaming",
            wakeup_mock,
        ),
    ):
        result = send_message(client, headers, session_id, content="/webapp")
        drain_tasks()

    # ── Phase 4: Command executed without error ────────────────────────
    assert result.get("command_executed") is True

    cmd_msgs = _get_command_messages(client, headers, session_id)
    assert len(cmd_msgs) == 1
    # webapp_enabled=False by default → informational message
    assert "No Web App available" in cmd_msgs[0]["content"]
    assert cmd_msgs[0]["message_metadata"]["command_name"] == "/webapp"

    # ── Phase 5: Wakeup NOT called ────────────────────────────────────
    assert wakeup_mock.call_count == 0, (
        f"/webapp must not trigger env wakeup (requires_running_environment=False), "
        f"but wakeup was called {wakeup_mock.call_count} time(s)"
    )

    assert len(stub.stream_calls) == 0


# ── Scenario 5: Wakeup failure → HTTP 500 with friendly message ──────────────


def test_files_command_wakeup_failure_returns_http_500(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
    patch_environment_adapter,
) -> None:
    """
    Wakeup failure: ensure_environment_ready_for_streaming raises RuntimeError
    → route returns HTTP 500 with friendly detail, no command message persisted.

    Scenario:
      1. Create agent + session
      2. Suspend environment
      3. Patch ensure_environment_ready_for_streaming to raise RuntimeError
      4. POST /sessions/{id}/messages/stream with /files
      5. Verify HTTP 500 with the error text in the detail field
      6. Verify no command message was persisted (no partial state)
    """
    headers = superuser_token_headers

    # ── Phase 1: Create agent + session ──────────────────────────────
    agent = create_agent_via_api(client, headers)
    drain_tasks()
    agent = get_agent(client, headers, agent["id"])
    agent_id = agent["id"]
    env_id = agent["active_environment_id"]

    session_data = create_session_via_api(client, headers, agent_id)
    session_id = session_data["id"]

    # ── Phase 2: Suspend environment ──────────────────────────────────
    _suspend_environment(client, headers, env_id)

    # ── Phase 3+4: Send /files with a failing wakeup ──────────────────
    stub = StubAgentEnvConnector(response_text="irrelevant")

    async def _fail_wakeup(session_id, get_fresh_db_session, timeout_seconds=120):
        raise RuntimeError("Activation timed out after 120 seconds")

    with (
        patch("app.services.sessions.message_service.agent_env_connector", stub),
        patch(
            "app.services.sessions.session_service.SessionService.ensure_environment_ready_for_streaming",
            AsyncMock(side_effect=_fail_wakeup),
        ),
    ):
        r = client.post(
            f"{_API}/sessions/{session_id}/messages/stream",
            headers=headers,
            json={"content": "/files"},
        )
        drain_tasks()

    # ── Phase 5: HTTP 500 with friendly error detail ───────────────────
    assert r.status_code == 500, (
        f"Expected HTTP 500 on wakeup failure, got {r.status_code}: {r.text}"
    )
    detail = r.json().get("detail", "")
    assert "Failed to start the agent environment" in detail, (
        f"Expected friendly error in detail, got: {detail!r}"
    )
    assert "Activation timed out" in detail

    # ── Phase 6: No command message persisted ─────────────────────────
    # On the error path no DB rows are created (matches the file-upload-wakeup
    # failure precedent in session_service.py ~line 1461).
    cmd_msgs = _get_command_messages(client, headers, session_id)
    assert len(cmd_msgs) == 0, (
        f"No command messages should be persisted on wakeup failure, "
        f"got {len(cmd_msgs)}"
    )

    # No LLM call was made
    assert len(stub.stream_calls) == 0


# ── Scenarios 6/7/8: Newly-flagged handlers auto-wake suspended envs ──────────
#
# These commands set requires_running_environment=True for ergonomic reasons:
# /agent-status to make the live STATUS.md fetch immediate, /session-recover
# and /session-reset to pre-wake the env for the user's next interaction.
# The handlers themselves don't strictly need a running env (they fall back
# to cached data / pure DB ops), but pre-waking removes friction.


def test_agent_status_command_auto_wakes_suspended_env(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
    patch_environment_adapter,
) -> None:
    """
    /agent-status invoked when env is suspended → env is auto-woken.

    Scenario:
      1. Create agent + session (env starts running)
      2. Suspend the environment via API
      3. Send /agent-status with mocked wakeup
      4. Verify ensure_environment_ready_for_streaming was called exactly once
      5. Verify command_executed=True (handler still returns a result even
         when no STATUS.md exists — falls back to informational message)

    Rationale: pre-wake makes the live fetch immediate. The handler's
    internal StatusUnavailableError → cached snapshot fallback is still
    intact for adapter failures after wakeup.
    """
    headers = superuser_token_headers

    # ── Phase 1: Create agent + session ──────────────────────────────
    agent = create_agent_via_api(client, headers)
    drain_tasks()
    agent = get_agent(client, headers, agent["id"])
    agent_id = agent["id"]
    env_id = agent["active_environment_id"]

    session_data = create_session_via_api(client, headers, agent_id)
    session_id = session_data["id"]

    # ── Phase 2: Suspend environment ──────────────────────────────────
    _suspend_environment(client, headers, env_id)

    # ── Phase 3: Send /agent-status with mocked wakeup ────────────────
    stub = StubAgentEnvConnector(response_text="irrelevant")
    wakeup_mock = _make_wakeup_mock(db, env_id)

    with (
        patch("app.services.sessions.message_service.agent_env_connector", stub),
        patch(
            "app.services.sessions.session_service.SessionService.ensure_environment_ready_for_streaming",
            wakeup_mock,
        ),
    ):
        result = send_message(client, headers, session_id, content="/agent-status")
        drain_tasks()

    # ── Phase 4: Wakeup was called exactly once ────────────────────────
    assert wakeup_mock.call_count == 1, (
        f"Expected ensure_environment_ready_for_streaming called once for "
        f"/agent-status on suspended env, got {wakeup_mock.call_count}"
    )

    # ── Phase 5: Command produced a result (no env-status error) ──────
    assert result.get("command_executed") is True, (
        f"Expected command_executed=True, got: {result}"
    )

    # No LLM call
    assert len(stub.stream_calls) == 0


def test_session_recover_command_auto_wakes_suspended_env(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
    patch_environment_adapter,
) -> None:
    """
    /session-recover invoked when env is suspended → env is auto-woken.

    Scenario:
      1. Create agent + session (env starts running)
      2. Suspend the environment via API
      3. Send /session-recover with mocked wakeup
      4. Verify ensure_environment_ready_for_streaming was called exactly once
      5. Verify command_executed=True

    Rationale: recovery usually precedes a retry, so wake the env preemptively.
    """
    headers = superuser_token_headers

    # ── Phase 1: Create agent + session ──────────────────────────────
    agent = create_agent_via_api(client, headers)
    drain_tasks()
    agent = get_agent(client, headers, agent["id"])
    agent_id = agent["id"]
    env_id = agent["active_environment_id"]

    session_data = create_session_via_api(client, headers, agent_id)
    session_id = session_data["id"]

    # ── Phase 2: Suspend environment ──────────────────────────────────
    _suspend_environment(client, headers, env_id)

    # ── Phase 3: Send /session-recover with mocked wakeup ─────────────
    stub = StubAgentEnvConnector(response_text="irrelevant")
    wakeup_mock = _make_wakeup_mock(db, env_id)

    with (
        patch("app.services.sessions.message_service.agent_env_connector", stub),
        patch(
            "app.services.sessions.session_service.SessionService.ensure_environment_ready_for_streaming",
            wakeup_mock,
        ),
    ):
        result = send_message(client, headers, session_id, content="/session-recover")
        drain_tasks()

    # ── Phase 4: Wakeup was called exactly once ────────────────────────
    assert wakeup_mock.call_count == 1, (
        f"Expected ensure_environment_ready_for_streaming called once for "
        f"/session-recover on suspended env, got {wakeup_mock.call_count}"
    )

    # ── Phase 5: Command produced a result ────────────────────────────
    assert result.get("command_executed") is True, (
        f"Expected command_executed=True, got: {result}"
    )


def test_session_reset_command_auto_wakes_suspended_env(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
    patch_environment_adapter,
) -> None:
    """
    /session-reset invoked when env is suspended → env is auto-woken.

    Scenario:
      1. Create agent + session (env starts running)
      2. Suspend the environment via API
      3. Send /session-reset with mocked wakeup
      4. Verify ensure_environment_ready_for_streaming was called exactly once
      5. Verify command_executed=True and "Session reset" message present

    Rationale: reset usually precedes a fresh conversation, so wake the env
    preemptively.
    """
    headers = superuser_token_headers

    # ── Phase 1: Create agent + session ──────────────────────────────
    agent = create_agent_via_api(client, headers)
    drain_tasks()
    agent = get_agent(client, headers, agent["id"])
    agent_id = agent["id"]
    env_id = agent["active_environment_id"]

    session_data = create_session_via_api(client, headers, agent_id)
    session_id = session_data["id"]

    # ── Phase 2: Suspend environment ──────────────────────────────────
    _suspend_environment(client, headers, env_id)

    # ── Phase 3: Send /session-reset with mocked wakeup ───────────────
    stub = StubAgentEnvConnector(response_text="irrelevant")
    wakeup_mock = _make_wakeup_mock(db, env_id)

    with (
        patch("app.services.sessions.message_service.agent_env_connector", stub),
        patch(
            "app.services.sessions.session_service.SessionService.ensure_environment_ready_for_streaming",
            wakeup_mock,
        ),
    ):
        result = send_message(client, headers, session_id, content="/session-reset")
        drain_tasks()

    # ── Phase 4: Wakeup was called exactly once ────────────────────────
    assert wakeup_mock.call_count == 1, (
        f"Expected ensure_environment_ready_for_streaming called once for "
        f"/session-reset on suspended env, got {wakeup_mock.call_count}"
    )

    # ── Phase 5: Command produced the reset confirmation ──────────────
    assert result.get("command_executed") is True, (
        f"Expected command_executed=True, got: {result}"
    )

    cmd_msgs = _get_command_messages(client, headers, session_id)
    assert len(cmd_msgs) == 1
    assert "Session reset" in cmd_msgs[0]["content"]
    assert cmd_msgs[0]["message_metadata"]["command_name"] == "/session-reset"

    # No LLM call
    assert len(stub.stream_calls) == 0
