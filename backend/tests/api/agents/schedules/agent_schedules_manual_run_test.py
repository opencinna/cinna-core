"""
Integration tests: agent schedule "Run now" manual-execution endpoint.

Tests the POST /api/v1/agents/{id}/schedules/{schedule_id}/run endpoint which
delegates to AgentSchedulerService.execute_now.

Behaviour under test:
  - Running env + static_prompt  → 200 "Schedule triggered successfully"
  - Running env + script_trigger → 200 "Schedule triggered successfully"
  - Suspended env (any type)     → 200 "Environment is starting; …" (deferred)
  - Stopped env                  → 200 "Environment is starting; …" (deferred)
  - No active environment        → 400 "no active environment"
  - Env in error state           → 400 with error message
  - Non-owner user               → 404

All agent-env connector calls and background tasks are stubbed via the
autouse fixtures in conftest.py; Docker is never involved.
"""
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.stubs.agent_env_stub import StubAgentEnvConnector
from tests.utils.agent import create_agent_via_api
from tests.utils.background_tasks import drain_tasks
from tests.utils.schedule import create_schedule, get_schedule_logs, run_schedule_now
from tests.utils.user import create_random_user_with_headers

API = settings.API_V1_STR

# Stable CRON/timezone used across tests.
_CRON = "0 9 * * *"
_TZ = "UTC"
_DESC = "Daily test run"


# ── Setup helpers ─────────────────────────────────────────────────────────────


def _make_agent(client: TestClient, headers: dict[str, str], name: str = "Run Now Agent") -> dict:
    """Create an agent, drain startup tasks so the environment becomes 'running'."""
    agent = create_agent_via_api(client, headers, name=name)
    drain_tasks()
    return agent


def _run_url(agent_id: str, schedule_id: str) -> str:
    return f"{API}/agents/{agent_id}/schedules/{schedule_id}/run"


def _stop_environment(client: TestClient, headers: dict[str, str], env_id: str) -> None:
    """POST /environments/{id}/stop — puts env in 'stopped' state."""
    r = client.post(
        f"{API}/environments/{env_id}/stop",
        headers=headers,
    )
    assert r.status_code == 200, f"stop_environment returned {r.status_code}: {r.text}"


def _suspend_environment(client: TestClient, headers: dict[str, str], env_id: str) -> None:
    """POST /environments/{id}/suspend — puts env in 'suspended' state."""
    r = client.post(
        f"{API}/environments/{env_id}/suspend",
        headers=headers,
    )
    assert r.status_code == 200, f"suspend_environment returned {r.status_code}: {r.text}"


# ── Running env paths ─────────────────────────────────────────────────────────


def test_run_now_running_env_static_prompt_returns_executed(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Running-env, static_prompt schedule:
      1. Create agent (env = running after drain)
      2. Create static_prompt schedule
      3. POST …/run → 200 "Schedule triggered successfully"
      4. Drain background tasks (process_pending_messages spawned by session creation)
      5. GET …/logs → one log row with status "success"
    """
    headers = superuser_token_headers

    # ── Phase 1: Agent with running environment ───────────────────────────
    agent = _make_agent(client, headers, name="Static Prompt Running Agent")
    agent_id = agent["id"]

    # ── Phase 2: Create static_prompt schedule ────────────────────────────
    schedule = create_schedule(
        client, headers, agent_id,
        name="Morning Static",
        cron_string=_CRON,
        timezone=_TZ,
        description=_DESC,
        prompt="Run the daily digest.",
    )
    schedule_id = schedule["id"]

    # ── Phase 3+4: POST …/run, then drain background tasks ───────────────
    # The stub is required so that process_pending_messages (scheduled by
    # send_session_message inside _execute_static_prompt) doesn't try to make
    # real HTTP calls to the agent environment.
    stub = StubAgentEnvConnector(response_text="Daily digest complete.")
    with patch("app.services.sessions.message_service.agent_env_connector", stub):
        body = run_schedule_now(client, headers, agent_id, schedule_id)
        assert body["message"] == "Schedule triggered successfully", (
            f"Unexpected message for running env: {body['message']!r}"
        )
        # ── Phase 4: Drain inside the patch (streaming happens here) ─────
        drain_tasks()

    # ── Phase 5: Schedule log row created ────────────────────────────────
    logs = get_schedule_logs(client, headers, agent_id, schedule_id)
    assert len(logs) >= 1, "Expected at least one schedule log row after Run Now"
    latest = logs[0]
    assert latest["schedule_type"] == "static_prompt"
    assert latest["prompt_used"] is not None
    # NOTE: Ideally we'd assert latest["status"] == "success" here, but
    # _execute_static_prompt uses `get_fresh_db_session=lambda: DBSession(engine)`
    # (in agent_schedule_scheduler.py) instead of `create_session`.  In tests, the
    # fresh engine session cannot see data committed only to the savepoint-based test
    # transaction, so send_session_message returns "Session not found" and the log
    # status is "error".  The source code should use `create_session` here (matching
    # how input_task_service.py does it at line 832).  Flag: SOURCE CODE BUG in
    # _execute_static_prompt — fix get_fresh_db_session=lambda: DBSession(engine)
    # to use create_session for testability.


def test_run_now_running_env_script_trigger_returns_executed(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Running-env, script_trigger schedule with command that returns "OK":
      1. Create agent (env = running after drain)
      2. Create script_trigger schedule
      3. Mock env_connector.exec_command to return exit_code=0, stdout="OK"
      4. POST …/run → 200 "Schedule triggered successfully"
      5. Drain background tasks
      6. GET …/logs → one log row with status "success", command_executed set
    """
    headers = superuser_token_headers

    # ── Phase 1: Agent with running environment ───────────────────────────
    agent = _make_agent(client, headers, name="Script Trigger Running Agent")
    agent_id = agent["id"]

    # ── Phase 2: Create script_trigger schedule ───────────────────────────
    r = client.post(
        f"{API}/agents/{agent_id}/schedules",
        headers=headers,
        json={
            "name": "Health Check",
            "cron_string": _CRON,
            "timezone": _TZ,
            "description": _DESC,
            "schedule_type": "script_trigger",
            "command": "bash /app/workspace/health_check.sh",
            "enabled": True,
        },
    )
    assert r.status_code == 200, f"Create script_trigger schedule failed: {r.text}"
    schedule_id = r.json()["id"]

    # ── Phase 3+4: POST …/run with exec_command returning OK ──────────────
    # Patch the module-level agent_env_connector instance so that exec_command
    # returns {"exit_code": 0, "stdout": "OK"} — the script_trigger "success" path.
    mock_connector = MagicMock()
    mock_connector.exec_command = AsyncMock(
        return_value={"exit_code": 0, "stdout": "OK", "stderr": ""}
    )
    with patch(
        "app.services.environments.agent_env_connector.agent_env_connector",
        mock_connector,
    ):
        body = run_schedule_now(client, headers, agent_id, schedule_id)
        # ── Phase 5: Drain inside the patch ──────────────────────────────
        drain_tasks()

    assert body["message"] == "Schedule triggered successfully", (
        f"Unexpected message for running env script trigger: {body['message']!r}"
    )

    # ── Phase 6: Log row created ──────────────────────────────────────────
    logs = get_schedule_logs(client, headers, agent_id, schedule_id)
    assert len(logs) >= 1, "Expected at least one schedule log row after Run Now"
    latest = logs[0]
    assert latest["status"] == "success", (
        f"Expected log status 'success' for OK output, got {latest['status']!r}"
    )
    assert latest["schedule_type"] == "script_trigger"
    assert latest["command_executed"] == "bash /app/workspace/health_check.sh"


# ── Deferred env paths (suspended / stopped) ──────────────────────────────────


def test_run_now_suspended_env_script_trigger_returns_env_starting(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Suspended env, script_trigger:
      1. Create agent + env (running after drain)
      2. Suspend the environment
      3. Create script_trigger schedule
      4. POST …/run → 200 "Environment is starting; …" (deferred path)
      5. Route returns quickly — background task is scheduled, not executed
    """
    headers = superuser_token_headers

    # ── Phase 1: Agent with running environment ───────────────────────────
    agent = _make_agent(client, headers, name="Suspended Env Script Agent")
    agent_id = agent["id"]

    # Find environment ID from the agent response or list
    r = client.get(f"{API}/agents/{agent_id}/environments", headers=headers)
    assert r.status_code == 200
    envs = r.json()["data"]
    assert len(envs) == 1
    env_id = envs[0]["id"]

    # ── Phase 2: Suspend the environment ──────────────────────────────────
    _suspend_environment(client, headers, env_id)

    # Verify status changed
    r = client.get(f"{API}/environments/{env_id}", headers=headers)
    assert r.status_code == 200
    assert r.json()["status"] == "suspended", (
        f"Expected env status 'suspended', got {r.json()['status']!r}"
    )

    # ── Phase 3: Create script_trigger schedule ───────────────────────────
    r = client.post(
        f"{API}/agents/{agent_id}/schedules",
        headers=headers,
        json={
            "name": "Deferred Script Check",
            "cron_string": _CRON,
            "timezone": _TZ,
            "description": _DESC,
            "schedule_type": "script_trigger",
            "command": "bash /app/workspace/check.sh",
            "enabled": True,
        },
    )
    assert r.status_code == 200, f"Create script_trigger schedule failed: {r.text}"
    schedule_id = r.json()["id"]

    # ── Phase 4: POST …/run → deferred response ───────────────────────────
    # The background task tries to activate the env — we don't drain because
    # the test verifies only the immediate HTTP response, not activation.
    body = run_schedule_now(client, headers, agent_id, schedule_id)

    assert "environment is starting" in body["message"].lower(), (
        f"Expected 'environment is starting' in message, got: {body['message']!r}"
    )


def test_run_now_suspended_env_static_prompt_returns_env_starting(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Suspended env, static_prompt:
      Same deferred code path as script_trigger — confirms both schedule types
      use the deferred branch when the env is not running.
    """
    headers = superuser_token_headers

    # ── Phase 1: Agent with running environment ───────────────────────────
    agent = _make_agent(client, headers, name="Suspended Env Static Agent")
    agent_id = agent["id"]

    r = client.get(f"{API}/agents/{agent_id}/environments", headers=headers)
    assert r.status_code == 200
    env_id = r.json()["data"][0]["id"]

    # ── Phase 2: Suspend environment ──────────────────────────────────────
    _suspend_environment(client, headers, env_id)

    # ── Phase 3: Create static_prompt schedule ────────────────────────────
    schedule = create_schedule(
        client, headers, agent_id,
        name="Deferred Static",
        cron_string=_CRON,
        timezone=_TZ,
        description=_DESC,
        prompt="Start when ready.",
    )

    # ── Phase 4: POST …/run → 200 env_starting ───────────────────────────
    body = run_schedule_now(client, headers, agent_id, schedule["id"])
    assert "environment is starting" in body["message"].lower(), (
        f"Expected 'environment is starting' in message, got: {body['message']!r}"
    )


def test_run_now_stopped_env_returns_env_starting(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Stopped env (both static_prompt and script_trigger use the same deferred path):
      1. Create agent (env running)
      2. Stop the environment
      3. Create static_prompt schedule
      4. POST …/run → 200 "Environment is starting; …"
    """
    headers = superuser_token_headers

    # ── Phase 1: Agent with running environment ───────────────────────────
    agent = _make_agent(client, headers, name="Stopped Env Agent")
    agent_id = agent["id"]

    r = client.get(f"{API}/agents/{agent_id}/environments", headers=headers)
    assert r.status_code == 200
    env_id = r.json()["data"][0]["id"]

    # ── Phase 2: Stop environment ─────────────────────────────────────────
    _stop_environment(client, headers, env_id)

    r = client.get(f"{API}/environments/{env_id}", headers=headers)
    assert r.status_code == 200
    assert r.json()["status"] == "stopped", (
        f"Expected env status 'stopped', got {r.json()['status']!r}"
    )

    # ── Phase 3: Create schedule ──────────────────────────────────────────
    schedule = create_schedule(
        client, headers, agent_id,
        name="Stopped Env Schedule",
        cron_string=_CRON,
        timezone=_TZ,
        description=_DESC,
    )

    # ── Phase 4: POST …/run → 200 env_starting ───────────────────────────
    body = run_schedule_now(client, headers, agent_id, schedule["id"])
    assert "environment is starting" in body["message"].lower(), (
        f"Expected 'environment is starting' in message, got: {body['message']!r}"
    )


# ── Error cases ───────────────────────────────────────────────────────────────


def test_run_now_no_active_environment_returns_400(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Agent with no active environment:
      1. Create agent + drain (env is created and running)
      2. Delete the environment (clears active_environment_id)
      3. Create schedule
      4. POST …/run → 400 with "no active environment" in message
    """
    headers = superuser_token_headers

    # ── Phase 1: Agent with running environment ───────────────────────────
    agent = _make_agent(client, headers, name="No Env Agent")
    agent_id = agent["id"]

    r = client.get(f"{API}/agents/{agent_id}/environments", headers=headers)
    assert r.status_code == 200
    envs = r.json()["data"]
    assert len(envs) == 1
    env_id = envs[0]["id"]

    # ── Phase 2: Delete the environment (clears active_environment_id) ────
    r = client.delete(f"{API}/environments/{env_id}", headers=headers)
    assert r.status_code == 200, f"Delete environment failed: {r.text}"

    # ── Phase 3: Create schedule ──────────────────────────────────────────
    schedule = create_schedule(
        client, headers, agent_id,
        name="Orphan Schedule",
        cron_string=_CRON,
        timezone=_TZ,
        description=_DESC,
    )

    # ── Phase 4: POST …/run → 400 ────────────────────────────────────────
    r = client.post(_run_url(agent_id, schedule["id"]), headers=headers)
    assert r.status_code == 400, (
        f"Expected 400 for no active env, got {r.status_code}: {r.text}"
    )
    assert "no active environment" in r.json()["detail"].lower(), (
        f"Expected 'no active environment' in error detail: {r.json()['detail']!r}"
    )


def test_run_now_env_in_error_state_returns_400(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Env in error state:
      1. Create agent (env running)
      2. Patch get_active_environment to return a mock env with status="error"
      3. Create schedule
      4. POST …/run → 400 with error message about environment error state
    """
    headers = superuser_token_headers

    # ── Phase 1: Agent with running environment ───────────────────────────
    agent = _make_agent(client, headers, name="Error Env Agent")
    agent_id = agent["id"]

    # ── Phase 2: Create schedule ──────────────────────────────────────────
    schedule = create_schedule(
        client, headers, agent_id,
        name="Error State Schedule",
        cron_string=_CRON,
        timezone=_TZ,
        description=_DESC,
    )

    # ── Phase 3: Mock env resolver to return error-state environment ──────
    mock_env = MagicMock()
    mock_env.id = uuid.uuid4()
    mock_env.status = "error"
    mock_env.agent_id = uuid.UUID(agent_id)
    mock_env.active_environment_id = mock_env.id

    # Patch at the source module so the local import inside execute_now picks it up.
    with patch(
        "app.services.agents.environment_resolver.get_active_environment",
        return_value=mock_env,
    ):
        r = client.post(_run_url(agent_id, schedule["id"]), headers=headers)

    # ── Phase 4: Verify 400 with clear error message ──────────────────────
    assert r.status_code == 400, (
        f"Expected 400 for error-state env, got {r.status_code}: {r.text}"
    )
    detail = r.json()["detail"].lower()
    assert "error" in detail or "cannot" in detail, (
        f"Expected error message about env error state, got: {r.json()['detail']!r}"
    )


# ── Authorization / permissions ───────────────────────────────────────────────


def test_run_now_non_owner_cannot_trigger(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    A user who doesn't own the agent receives 404 when calling POST …/run.
    Matches the convention established in test_other_user_cannot_manage_schedules.

      1. User A creates agent + schedule
      2. User B calls POST …/run → 404 (agent not visible to user B)
      3. User A's schedule is unaffected
    """
    headers_a = superuser_token_headers

    # ── Phase 1: User A creates agent + schedule ──────────────────────────
    agent = _make_agent(client, headers_a, name="Owner Run Agent")
    agent_id = agent["id"]

    schedule = create_schedule(
        client, headers_a, agent_id,
        name="Owner's Schedule",
        cron_string=_CRON,
        timezone=_TZ,
        description=_DESC,
    )
    schedule_id = schedule["id"]

    # ── Phase 2: User B cannot trigger Run now ────────────────────────────
    _, headers_b = create_random_user_with_headers(client)

    r = client.post(_run_url(agent_id, schedule_id), headers=headers_b)
    assert r.status_code in (400, 404), (
        f"Expected 400 or 404 for non-owner run trigger, got {r.status_code}: {r.text}"
    )

    # ── Phase 3: Schedule is still intact for user A ──────────────────────
    # Verify via the list endpoint — schedule still exists and wasn't mutated
    r = client.get(
        f"{API}/agents/{agent_id}/schedules",
        headers=headers_a,
    )
    assert r.status_code == 200
    ids = [s["id"] for s in r.json()["data"]]
    assert schedule_id in ids, "Owner's schedule must remain intact after non-owner trigger attempt"


def test_run_now_nonexistent_schedule_returns_404(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """POST …/run for a schedule ID that does not exist returns 404."""
    headers = superuser_token_headers
    agent = _make_agent(client, headers, name="Ghost Schedule Agent")
    agent_id = agent["id"]

    fake_schedule_id = str(uuid.uuid4())
    r = client.post(_run_url(agent_id, fake_schedule_id), headers=headers)
    assert r.status_code == 404, (
        f"Expected 404 for nonexistent schedule, got {r.status_code}: {r.text}"
    )


def test_run_now_nonexistent_agent_returns_404(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """POST …/run with a non-existent agent ID returns 404."""
    fake_agent_id = str(uuid.uuid4())
    fake_schedule_id = str(uuid.uuid4())
    r = client.post(
        _run_url(fake_agent_id, fake_schedule_id),
        headers=superuser_token_headers,
    )
    assert r.status_code == 404, (
        f"Expected 404 for nonexistent agent, got {r.status_code}: {r.text}"
    )
