"""
Integration tests for the environment status scheduler.

The scheduler's ``_check_environment_statuses()`` queries all environments
with status=="running", calls health_check() and (conditionally) get_status()
on each, and marks unhealthy + stopped environments as "error".

Four scenario-based tests:
  1. Healthy environments remain "running" and get last_health_check set
  2. Unhealthy environment with stopped container is marked "error"
  3. Unhealthy health check but container still running → no status change
  4. No running environments → function is a no-op
"""
import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.services.environments.adapters.base import HealthResponse
from app.services.environments.environment_status_scheduler import _check_environment_statuses
from tests.utils.agent import create_agent_via_api
from tests.utils.background_tasks import drain_tasks
from tests.utils.environment import get_environment, list_environments


# ---------------------------------------------------------------------------
# Scenario 1: Healthy environment stays "running", last_health_check is set
# ---------------------------------------------------------------------------

def test_healthy_environment_remains_running(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """
    Healthy environments are not affected by the scheduler:
      1.  Create agent → drain tasks → env is "running"
      2.  Record last_health_check timestamp set by the lifecycle manager on start
      3.  Run _check_environment_statuses() (adapter returns healthy by default)
      4.  GET environment → status still "running", last_health_check is set and
          is a valid ISO timestamp (the scheduler refreshes it on every check)
    """
    # ── Phase 1: Create agent → drain → env running ────────────────────────
    agent = create_agent_via_api(client, superuser_token_headers)
    agent_id = agent["id"]
    drain_tasks()

    envs = list_environments(client, superuser_token_headers, agent_id)
    assert envs["count"] == 1
    env = envs["data"][0]
    env_id = env["id"]
    assert env["status"] == "running"

    # ── Phase 2: Record baseline last_health_check (set during start) ──────
    # The lifecycle manager already writes last_health_check when it transitions
    # the environment to "running", so it will already be set here.
    fetched_before = get_environment(client, superuser_token_headers, env_id)
    health_check_before = fetched_before["last_health_check"]
    # It may or may not be None depending on lifecycle path, but we note the value.

    # ── Phase 3: Patch scheduler's EnvironmentLifecycleManager to use lm ──
    lm = patch_environment_adapter
    with patch(
        "app.services.environments.environment_status_scheduler.EnvironmentLifecycleManager",
        return_value=lm,
    ):
        asyncio.run(_check_environment_statuses())

    # ── Phase 4: Status still "running", last_health_check is set ──────────
    fetched_after = get_environment(client, superuser_token_headers, env_id)
    assert fetched_after["status"] == "running"
    # The scheduler always writes last_health_check for every processed env
    assert fetched_after["last_health_check"] is not None
    # It must be a valid ISO timestamp string
    assert isinstance(fetched_after["last_health_check"], str)
    ts_after = datetime.fromisoformat(fetched_after["last_health_check"])
    # If there was a timestamp before, the scheduler must have refreshed it
    # (written a value >= the pre-check value)
    if health_check_before is not None:
        ts_before = datetime.fromisoformat(health_check_before)
        assert ts_after >= ts_before


# ---------------------------------------------------------------------------
# Scenario 2: Unhealthy + container stopped → environment marked "error"
# ---------------------------------------------------------------------------

def test_unhealthy_environment_marked_as_error(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """
    When health_check returns unhealthy AND get_status returns non-running,
    the scheduler marks the environment as "error":
      1.  Create agent → drain → env is "running"
      2.  Override adapter: health_check returns unhealthy, get_status="stopped"
      3.  Run _check_environment_statuses()
      4.  GET environment → status="error", status_message contains "unreachable"
    """
    # ── Phase 1: Create agent → drain → env running ────────────────────────
    agent = create_agent_via_api(client, superuser_token_headers)
    agent_id = agent["id"]
    drain_tasks()

    envs = list_environments(client, superuser_token_headers, agent_id)
    assert envs["count"] == 1
    env_id = envs["data"][0]["id"]
    assert envs["data"][0]["status"] == "running"

    # ── Phase 2: Make adapter report unhealthy + stopped ───────────────────
    lm = patch_environment_adapter
    adapter = lm._test_adapter

    unhealthy_response = HealthResponse(
        status="unhealthy",
        uptime=0,
        message="container not responding",
        timestamp=datetime.now(),
    )
    adapter.health_check = AsyncMock(return_value=unhealthy_response)
    # get_status returns "stopped" (adapter's default after creation, before start)
    # but here the _status was set to "running" by the start() call during drain_tasks.
    # Override it explicitly to simulate a crashed container.
    adapter.get_status = AsyncMock(return_value="stopped")

    # ── Phase 3: Run scheduler ─────────────────────────────────────────────
    with patch(
        "app.services.environments.environment_status_scheduler.EnvironmentLifecycleManager",
        return_value=lm,
    ):
        asyncio.run(_check_environment_statuses())

    # ── Phase 4: Environment is now "error" with descriptive message ────────
    fetched = get_environment(client, superuser_token_headers, env_id)
    assert fetched["status"] == "error"
    assert fetched["status_message"] is not None
    assert "unreachable" in fetched["status_message"].lower()
    # last_health_check was also updated
    assert fetched["last_health_check"] is not None


# ---------------------------------------------------------------------------
# Scenario 3: Unhealthy health check but container still running → no error
# ---------------------------------------------------------------------------

def test_unhealthy_health_check_but_container_running_no_error(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """
    Transient unhealthy health check with a still-running container is ignored:
      1.  Create agent → drain → env is "running"
      2.  Override adapter: health_check returns unhealthy, get_status="running"
      3.  Run _check_environment_statuses()
      4.  GET environment → status still "running" (not errored)
    """
    # ── Phase 1: Create agent → drain → env running ────────────────────────
    agent = create_agent_via_api(client, superuser_token_headers)
    agent_id = agent["id"]
    drain_tasks()

    envs = list_environments(client, superuser_token_headers, agent_id)
    assert envs["count"] == 1
    env_id = envs["data"][0]["id"]
    assert envs["data"][0]["status"] == "running"

    # ── Phase 2: Adapter reports unhealthy but container is still running ──
    lm = patch_environment_adapter
    adapter = lm._test_adapter

    unhealthy_response = HealthResponse(
        status="unhealthy",
        uptime=0,
        message="slow response",
        timestamp=datetime.now(),
    )
    adapter.health_check = AsyncMock(return_value=unhealthy_response)
    # Container is still up — this is a transient issue
    adapter.get_status = AsyncMock(return_value="running")

    # ── Phase 3: Run scheduler ─────────────────────────────────────────────
    with patch(
        "app.services.environments.environment_status_scheduler.EnvironmentLifecycleManager",
        return_value=lm,
    ):
        asyncio.run(_check_environment_statuses())

    # ── Phase 4: Status unchanged — transient unhealthy is not an error ────
    fetched = get_environment(client, superuser_token_headers, env_id)
    assert fetched["status"] == "running"
    # last_health_check is still updated (the timestamp write happens before the
    # status decision)
    assert fetched["last_health_check"] is not None


# ---------------------------------------------------------------------------
# Scenario 4: No running environments → scheduler is a no-op
# ---------------------------------------------------------------------------

def test_no_running_environments_is_noop(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """
    When no environments are in "running" state, _check_environment_statuses()
    returns immediately without touching any records or raising errors:
      1.  Create agent but do NOT drain tasks → env stays in "building" state
      2.  Run _check_environment_statuses()
      3.  No exception raised; environment status unchanged (still building/not running)
    """
    # ── Phase 1: Create agent without draining (env not yet running) ───────
    agent = create_agent_via_api(client, superuser_token_headers)
    agent_id = agent["id"]
    # Intentionally do NOT call drain_tasks() here.
    # The environment exists but is in "building" status, not "running".

    envs = list_environments(client, superuser_token_headers, agent_id)
    assert envs["count"] == 1
    env_id = envs["data"][0]["id"]
    # Status is NOT "running" at this point (could be "building" or "stopped")
    assert envs["data"][0]["status"] != "running"

    # ── Phase 2: Run scheduler — should be a no-op ─────────────────────────
    lm = patch_environment_adapter
    with patch(
        "app.services.environments.environment_status_scheduler.EnvironmentLifecycleManager",
        return_value=lm,
    ):
        # Must not raise any exception
        asyncio.run(_check_environment_statuses())

    # ── Phase 3: Environment status is still unchanged ─────────────────────
    fetched = get_environment(client, superuser_token_headers, env_id)
    assert fetched["status"] != "running"
    # No health check was performed, so last_health_check remains None
    assert fetched["last_health_check"] is None
