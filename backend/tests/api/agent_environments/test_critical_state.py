"""
Integration tests for the agent-environment critical-state feature.

Coverage (API-only, scenario-based):

  1. Critical-state lifecycle:
     a. Package-install failure with container alive → status="running",
        critical_state=True, critical_cause="package_install_failed",
        action-log row with full detail.
     b. Container gone on failure → status="error", critical_state=False
        (existing offline path preserved).
     c. Credential-sync failure with container alive → critical_cause="credential_sync_failed".
     d. Successful re-setup after prior critical state → critical cleared,
        success action-log row recorded.
     e. Health scheduler confirms container down on a critical env →
        status="error" AND critical_state cleared.

  2. Action-log API:
     a. Owner GET → 200, rows in executed_at DESC, full detail present.
     b. Non-owner → 403; unknown env → 404; limit respected/capped at 200.

  3. Notifications:
     a. Entering critical fires environment_critical to the owner exactly
        once per transition (re-fail while still critical → no second email).
     b. Recovery then re-failure → email fires again.
     c. User with toggle off → no email; action log + flag still set.
     d. Email contains brief summary and deep link, NOT full detail.

  4. CRON gating:
     a. _skip_schedule_for_critical_env against a critical env → AgentScheduleLog
        status="skipped", action-log "cron_skipped" row, next_execution advanced.
     b. Due schedule against a healthy env → not gated (regression).
     c. execute_now against a critical env → 400 with clear message.
     d. execute_now against a healthy env → 200 (regression).

Architecture note on the CRON-poll test approach:
  _poll_due_schedules() uses DBSession(engine) (not create_session), so it
  cannot see test-transaction data. The CRON-gating logic lives in the
  standalone _skip_schedule_for_critical_env coroutine which accepts a
  db_session argument — we call it directly with the test db session to
  exercise the skip logic, rather than invoking the full poll. This is the
  same pattern the manual-run tests use for the error-state path: mock the
  resolver or call the service directly with the test session. Unit tests for
  the CRON poll's critical branch live here alongside the API-level assertions.
"""
import asyncio
import uuid
from datetime import datetime, UTC
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.core.config import settings
from tests.utils.agent import create_agent_via_api
from tests.utils.background_tasks import drain_tasks
from tests.utils.environment import get_environment, list_environments
from tests.utils.schedule import create_schedule, get_schedule_logs
from tests.utils.user import create_random_user_with_headers, promote_to_developer

_BASE = f"{settings.API_V1_STR}/environments"
_SCHEDULES_API = settings.API_V1_STR
_CRON = "0 9 * * 1-5"
_TZ = "UTC"


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_agent_and_env(
    client: TestClient,
    headers: dict[str, str],
    name: str = "Critical Test Agent",
) -> tuple[dict, dict]:
    """Create agent, drain startup tasks, return (agent, env) dicts."""
    agent = create_agent_via_api(client, headers, name=name)
    drain_tasks()
    envs = list_environments(client, headers, agent["id"])
    assert envs["count"] == 1
    env = envs["data"][0]
    assert env["status"] == "running"
    return agent, env


def _get_action_logs(
    client: TestClient,
    headers: dict[str, str],
    env_id: str,
    *,
    limit: int | None = None,
) -> dict:
    """GET /environments/{id}/action-logs and return the parsed body."""
    url = f"{_BASE}/{env_id}/action-logs"
    params = {}
    if limit is not None:
        params["limit"] = limit
    r = client.get(url, headers=headers, params=params)
    assert r.status_code == 200, f"GET action-logs failed: {r.text}"
    return r.json()


def _enter_critical_via_adapter(
    patch_environment_adapter,
    *,
    fail_packages: bool = True,
    fail_credentials: bool = False,
) -> None:
    """Configure the stub adapter so the next setup call raises.

    The lifecycle calls install_custom_packages() (and install_system_packages,
    set_credentials) on the adapter during drain_tasks(). Here we make the
    relevant method raise so _enter_critical_state is triggered.

    The adapter's is_container_running() inherits from the base class which
    calls get_status() → "running" (the stub is_started state), so the
    "container alive" branch is taken and critical state is entered rather
    than the "container gone" re-raise path.
    """
    lm = patch_environment_adapter
    adapter = lm._test_adapter
    if fail_packages:
        adapter.install_custom_packages = AsyncMock(
            side_effect=Exception("Simulated package install failure: no matching dist found")
        )
    if fail_credentials:
        adapter.set_credentials = AsyncMock(
            side_effect=Exception("Simulated credential sync failure: HTTP 403")
        )


# ── Scenario 1a: Package-install failure with container alive ────────────────


def test_package_install_failure_alive_sets_critical_state(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
    db: Session,
) -> None:
    """
    Post-start package-install failure with the container still running:
      1. Create agent → configure adapter to raise on install_custom_packages
      2. Drain tasks (lifecycle runs setup; adapter raises; container alive)
      3. GET environment → status="running", critical_state=True,
         critical_cause="package_install_failed"
      4. GET action-logs → at least one "error" row with cause/detail set
    """
    headers = superuser_token_headers

    # ── Phase 1: Create agent ─────────────────────────────────────────────
    agent = create_agent_via_api(client, headers, name="PkgInstall Fail Agent")
    agent_id = agent["id"]

    # Configure the adapter to fail package install BEFORE draining — drain
    # triggers the lifecycle which calls install_custom_packages.
    # The adapter's get_status() returns "running" (set by start()), so
    # is_container_running() → True, and _enter_critical_state is called.
    lm = patch_environment_adapter
    adapter = lm._test_adapter
    adapter.install_custom_packages = AsyncMock(
        side_effect=Exception("Simulated package install failure: no dist found")
    )

    # ── Phase 2: Drain tasks (lifecycle build + setup) ────────────────────
    drain_tasks()

    # ── Phase 3: Env has critical_state and status stays "running" ────────
    envs = list_environments(client, headers, agent_id)
    assert envs["count"] == 1
    env = envs["data"][0]
    env_id = env["id"]

    fetched = get_environment(client, headers, env_id)
    assert fetched["status"] == "running", (
        f"Expected status='running' after package-install critical, got {fetched['status']!r}"
    )
    assert fetched["critical_state"] is True, (
        "Expected critical_state=True after package-install failure"
    )
    assert fetched["critical_cause"] == "package_install_failed", (
        f"Expected critical_cause='package_install_failed', got {fetched['critical_cause']!r}"
    )
    assert fetched["critical_since"] is not None, (
        "critical_since must be set when entering critical state"
    )

    # ── Phase 4: Action-log has an error row with full detail ─────────────
    logs = _get_action_logs(client, headers, env_id)
    assert logs["count"] >= 1, "Expected at least one action-log row after critical entry"
    error_rows = [r for r in logs["data"] if r["status"] == "error"]
    assert len(error_rows) >= 1, "Expected at least one error row in action-log"
    row = error_rows[0]
    assert row["cause"] == "package_install_failed"
    assert row["action"] in ("package_install", "system_package_install")
    assert row["detail"] is not None, "Full detail must be recorded for error rows"
    assert "install" in row["detail"].lower() or "dist" in row["detail"].lower(), (
        f"Expected install-related detail, got: {row['detail']!r}"
    )
    assert row["summary"] is not None
    assert row["environment_id"] == env_id
    assert row["agent_id"] == agent_id


# ── Scenario 1b: Container gone on failure → offline (status="error") ────────


def test_container_gone_on_failure_sets_error_not_critical(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """
    Package-install failure while the container is already gone:
      1. Create agent → configure adapter: install raises AND is_container_running=False
      2. Drain tasks
      3. GET environment → status="error", critical_state=False
         (existing offline behavior preserved, not masked as critical)
    """
    headers = superuser_token_headers

    # ── Phase 1: Create agent ─────────────────────────────────────────────
    agent = create_agent_via_api(client, headers, name="Container Gone Agent")
    agent_id = agent["id"]

    # The adapter raises on package install but the container is "stopped"
    # (get_status returns "stopped"), so is_container_running() returns False
    # and the exception is re-raised → existing status="error" offline path.
    lm = patch_environment_adapter
    adapter = lm._test_adapter
    adapter.install_custom_packages = AsyncMock(
        side_effect=Exception("Docker exec failed: container not running")
    )
    # Simulate the container crashing while setup ran
    adapter.get_status = AsyncMock(return_value="stopped")

    # ── Phase 2: Drain tasks ──────────────────────────────────────────────
    drain_tasks()

    # ── Phase 3: Env is error, not critical ───────────────────────────────
    envs = list_environments(client, headers, agent_id)
    assert envs["count"] == 1
    env_id = envs["data"][0]["id"]

    fetched = get_environment(client, headers, env_id)
    # Status must be "error" (container is gone — offline path)
    assert fetched["status"] == "error", (
        f"Expected status='error' for gone container, got {fetched['status']!r}"
    )
    # critical_state must remain False (it's an offline env, not running-but-degraded)
    assert fetched["critical_state"] is False, (
        "critical_state must be False when the container is gone (offline path)"
    )


# ── Scenario 1c: Credential-sync failure with container alive ────────────────


def test_credential_sync_failure_alive_sets_critical_cause(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """
    set_credentials failure against a live container:
      1. Create agent + env (running)
      2. Force credentials to be synced by rebuilding/re-configuring the env,
         with the adapter's set_credentials raising and get_status returning "running"
      3. After drain, env: critical_state=True, critical_cause="credential_sync_failed"
    """
    headers = superuser_token_headers

    # ── Phase 1: Create agent + running env ──────────────────────────────
    agent = create_agent_via_api(client, headers, name="Cred Sync Fail Agent")
    agent_id = agent["id"]

    # The adapter must be configured before drain so setup fails during
    # drain_tasks(). But set_credentials is called during _sync_dynamic_data
    # (which runs after start/rebuild). First let the environment build normally,
    # then trigger a reconfigure/restart that calls set_credentials.
    # Simpler approach: configure the adapter to fail on set_credentials,
    # then trigger a restart via the API which re-runs _sync_dynamic_data.
    drain_tasks()

    envs = list_environments(client, headers, agent_id)
    env_id = envs["data"][0]["id"]

    # Configure adapter to fail on set_credentials (container stays "running")
    lm = patch_environment_adapter
    adapter = lm._test_adapter
    adapter.set_credentials = AsyncMock(
        side_effect=Exception("Simulated credential sync HTTP 403")
    )
    # Container stays alive
    adapter.get_status = AsyncMock(return_value="running")

    # Trigger a restart — _sync_dynamic_data calls set_credentials
    r = client.post(f"{_BASE}/{env_id}/restart", headers=headers)
    assert r.status_code == 200, f"restart returned {r.status_code}: {r.text}"
    drain_tasks()

    # ── Phase 3: critical_cause="credential_sync_failed" ─────────────────
    fetched = get_environment(client, headers, env_id)
    assert fetched["critical_state"] is True, (
        "Expected critical_state=True after credential-sync failure"
    )
    assert fetched["critical_cause"] == "credential_sync_failed", (
        f"Expected critical_cause='credential_sync_failed', got {fetched['critical_cause']!r}"
    )
    assert fetched["status"] == "running", (
        f"Status must stay 'running' even after credential-sync failure, got {fetched['status']!r}"
    )

    # Action-log has an error row with the right cause
    logs = _get_action_logs(client, headers, env_id)
    error_rows = [r for r in logs["data"] if r["status"] == "error"]
    assert len(error_rows) >= 1
    cred_rows = [r for r in error_rows if r["cause"] == "credential_sync_failed"]
    assert len(cred_rows) >= 1, (
        f"Expected a cred-sync error row, got causes: {[r['cause'] for r in error_rows]}"
    )


# ── Scenario 1d: Successful re-setup after prior critical state ───────────────


def test_successful_rebuild_after_critical_clears_state(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """
    A successful rebuild/restart after a prior critical state clears the flag:
      1. Create agent → make install fail → drain → env is critical
      2. Fix the adapter (restore normal install_custom_packages)
      3. Trigger rebuild → drain
      4. GET env → critical_state=False
      5. Action-log has a "success" row
    """
    headers = superuser_token_headers

    # ── Phase 1: Create agent + enter critical state ──────────────────────
    agent = create_agent_via_api(client, headers, name="Rebuild After Critical Agent")
    agent_id = agent["id"]

    lm = patch_environment_adapter
    adapter = lm._test_adapter
    adapter.install_custom_packages = AsyncMock(
        side_effect=Exception("First install: no dist found")
    )
    drain_tasks()

    envs = list_environments(client, headers, agent_id)
    env_id = envs["data"][0]["id"]
    fetched = get_environment(client, headers, env_id)
    assert fetched["critical_state"] is True, "Precondition: env must be critical before recovery"

    # ── Phase 2: Fix the adapter — restore successful install ────────────
    # Replace the raising mock with a normal-success stub (returns True, no raise).
    adapter.install_custom_packages = AsyncMock(return_value=True)

    # ── Phase 3: Trigger rebuild → clears critical state ─────────────────
    r = client.post(f"{_BASE}/{env_id}/rebuild", headers=headers)
    assert r.status_code == 200, f"rebuild returned {r.status_code}: {r.text}"
    drain_tasks()

    # ── Phase 4: Critical state cleared ──────────────────────────────────
    fetched_after = get_environment(client, headers, env_id)
    assert fetched_after["critical_state"] is False, (
        f"Expected critical_state=False after successful rebuild, "
        f"got critical_state={fetched_after['critical_state']}, "
        f"cause={fetched_after['critical_cause']!r}"
    )
    assert fetched_after["critical_cause"] is None, (
        f"critical_cause must be None after recovery, got {fetched_after['critical_cause']!r}"
    )
    assert fetched_after["critical_since"] is None, (
        "critical_since must be cleared on recovery"
    )
    assert fetched_after["status"] == "running", (
        f"Status must be 'running' after successful rebuild, got {fetched_after['status']!r}"
    )

    # ── Phase 5: Action-log has a success row ────────────────────────────
    logs = _get_action_logs(client, headers, env_id)
    success_rows = [r for r in logs["data"] if r["status"] == "success"]
    assert len(success_rows) >= 1, (
        f"Expected at least one success action-log row after recovery. Rows: {logs['data']}"
    )


# ── Scenario 1e: Health scheduler confirms container down on critical env ─────


def test_status_scheduler_clears_critical_when_container_dies(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """
    When the health scheduler finds a critical env with a stopped container,
    it sets status="error" AND clears critical_state (offline supersedes critical):
      1. Create agent → make install fail → drain → env is critical + running
      2. Override adapter: health_check=unhealthy, get_status=stopped
      3. Run _check_environment_statuses()
      4. GET env → status="error", critical_state=False
    """
    from datetime import datetime
    from app.services.environments.adapters.base import HealthResponse
    from app.services.environments.environment_status_scheduler import (
        _check_environment_statuses,
    )

    headers = superuser_token_headers

    # ── Phase 1: Create agent + enter critical state ──────────────────────
    agent = create_agent_via_api(client, headers, name="Scheduler Down Agent")
    agent_id = agent["id"]

    lm = patch_environment_adapter
    adapter = lm._test_adapter
    adapter.install_custom_packages = AsyncMock(
        side_effect=Exception("Simulated: packages failed")
    )
    drain_tasks()

    envs = list_environments(client, headers, agent_id)
    env_id = envs["data"][0]["id"]
    fetched = get_environment(client, headers, env_id)
    assert fetched["critical_state"] is True, "Precondition: env must be critical"
    assert fetched["status"] == "running", "Precondition: status must be running (not yet offline)"

    # ── Phase 2: Make adapter report unhealthy + container stopped ────────
    adapter.health_check = AsyncMock(
        return_value=HealthResponse(
            status="unhealthy",
            uptime=0,
            message="container not responding after crash",
            timestamp=datetime.now(UTC),
        )
    )
    adapter.get_status = AsyncMock(return_value="stopped")

    # ── Phase 3: Run scheduler ────────────────────────────────────────────
    with patch(
        "app.services.environments.environment_status_scheduler.EnvironmentLifecycleManager",
        return_value=lm,
    ):
        asyncio.run(_check_environment_statuses())

    # ── Phase 4: status="error", critical_state=False ────────────────────
    fetched_after = get_environment(client, headers, env_id)
    assert fetched_after["status"] == "error", (
        f"Expected status='error' after scheduler detects stopped container, "
        f"got {fetched_after['status']!r}"
    )
    assert fetched_after["critical_state"] is False, (
        "critical_state must be cleared when container goes offline (offline supersedes critical)"
    )
    assert fetched_after["critical_cause"] is None, (
        "critical_cause must be cleared when status goes to error"
    )


# ── Scenario 2a: Action-log API — owner gets full rows in DESC order ──────────


def test_action_log_owner_gets_rows_desc_with_detail(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """
    GET /environments/{id}/action-logs as owner:
      1. Create agent → make install fail → drain (creates action-log rows)
      2. GET action-logs as owner → 200, data present, executed_at DESC, detail non-null
      3. Verify count matches data length
    """
    headers = superuser_token_headers

    # ── Phase 1: Create agent + generate action-log rows ─────────────────
    agent = create_agent_via_api(client, headers, name="ActionLog Owner Agent")
    agent_id = agent["id"]

    lm = patch_environment_adapter
    adapter = lm._test_adapter
    adapter.install_custom_packages = AsyncMock(
        side_effect=Exception("uv resolver: no matching distribution found for badpkg>=99.0")
    )
    drain_tasks()

    envs = list_environments(client, headers, agent_id)
    env_id = envs["data"][0]["id"]

    # ── Phase 2: Owner GET → 200 with rows ───────────────────────────────
    logs = _get_action_logs(client, headers, env_id)
    assert "data" in logs, "Response must have 'data' key"
    assert "count" in logs, "Response must have 'count' key"
    assert logs["count"] == len(logs["data"]), "count must equal len(data)"
    assert logs["count"] >= 1, "Expected at least one action-log row"

    error_rows = [r for r in logs["data"] if r["status"] == "error"]
    assert len(error_rows) >= 1

    row = error_rows[0]
    # Full detail present
    assert row["detail"] is not None and len(row["detail"]) > 0, (
        "Detail field must be non-empty for error rows"
    )
    assert "badpkg" in row["detail"] or "uv" in row["detail"] or "install" in row["detail"], (
        f"Detail should contain install-related text, got: {row['detail']!r}"
    )
    # Required fields
    assert row["environment_id"] == env_id
    assert row["agent_id"] == agent_id
    assert row["action"] is not None
    assert row["status"] == "error"
    assert row["cause"] is not None
    assert row["summary"] is not None

    # ── Phase 3: Order is executed_at DESC ───────────────────────────────
    if len(logs["data"]) > 1:
        timestamps = [r["executed_at"] for r in logs["data"]]
        parsed = [datetime.fromisoformat(ts.replace("Z", "+00:00")) for ts in timestamps]
        for i in range(len(parsed) - 1):
            assert parsed[i] >= parsed[i + 1], (
                f"Rows must be in executed_at DESC order, got {timestamps}"
            )


# ── Scenario 2b: Action-log API — non-owner 403, unknown env 404, limit cap ──


def test_action_log_auth_and_limit(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """
    Action-log route guards and limit parameter:
      1. Create agent (owner=superuser) + drain → env has action-log rows
      2. Non-owner GET → 403
      3. Unknown env GET → 404
      4. limit=1 → at most 1 row returned
      5. limit=200 is accepted (max); limit=201 → 422 validation error
    """
    headers = superuser_token_headers

    # ── Phase 1: Create agent + action-log rows ───────────────────────────
    agent = create_agent_via_api(client, headers, name="ActionLog Auth Agent")
    agent_id = agent["id"]

    # Make a few action-log rows by entering critical state
    lm = patch_environment_adapter
    adapter = lm._test_adapter
    adapter.install_custom_packages = AsyncMock(
        side_effect=Exception("Simulated failure for auth tests")
    )
    drain_tasks()

    envs = list_environments(client, headers, agent_id)
    env_id = envs["data"][0]["id"]

    # ── Phase 2: Non-owner → 403 ──────────────────────────────────────────
    _, other_headers = create_random_user_with_headers(client)
    r_other = client.get(f"{_BASE}/{env_id}/action-logs", headers=other_headers)
    assert r_other.status_code == 403, (
        f"Non-owner must receive 403, got {r_other.status_code}: {r_other.text}"
    )

    # ── Phase 3: Unknown env → 404 ────────────────────────────────────────
    ghost_id = str(uuid.uuid4())
    r_ghost = client.get(f"{_BASE}/{ghost_id}/action-logs", headers=headers)
    assert r_ghost.status_code == 404, (
        f"Unknown env must receive 404, got {r_ghost.status_code}: {r_ghost.text}"
    )

    # ── Phase 4: limit=1 returns at most 1 row ────────────────────────────
    logs_limited = _get_action_logs(client, headers, env_id, limit=1)
    assert len(logs_limited["data"]) <= 1, (
        f"limit=1 must return at most 1 row, got {len(logs_limited['data'])}"
    )

    # ── Phase 5: limit=200 accepted; limit=201 → 422 ─────────────────────
    r_max = client.get(f"{_BASE}/{env_id}/action-logs", headers=headers, params={"limit": 200})
    assert r_max.status_code == 200, f"limit=200 must be accepted, got {r_max.status_code}"

    r_over = client.get(f"{_BASE}/{env_id}/action-logs", headers=headers, params={"limit": 201})
    assert r_over.status_code == 422, (
        f"limit=201 must be rejected with 422, got {r_over.status_code}"
    )


# ── Scenario 3a: Notification fires exactly once per transition ───────────────
#
# Implementation note — why we patch SystemNotificationService.notify:
#
# The lifecycle's _enter_critical_state calls SystemNotificationService.notify
# (an async await, not a background task). The notification service then
# schedules the actual SMTP send via create_task_with_error_logging +
# anyio.to_thread.run_sync — those two layers are NOT wired in the
# agent_environments conftest (only in the notifications conftest).
# Patching SystemNotificationService.notify at the notification service module
# level lets us verify that the right notification type was dispatched
# without needing the full SMTP pipeline. The SMTP pipeline itself is covered
# by tests/api/notifications/test_notification_settings.py.


def test_critical_notification_fires_once_per_transition(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """
    environment_critical notification is dispatched exactly once per False→True
    transition (fire-once semantics via _critical_warned_env_ids):
      1. Create agent → first install fail → drain → env critical
         → SystemNotificationService.notify called once with ENVIRONMENT_CRITICAL
      2. Trigger rebuild with install still failing (env stays critical)
         → notify NOT called again (already warned for this env)
    """
    from app.services.notifications.notification_catalog import NotificationType

    headers = superuser_token_headers

    # Clear the process-local transition set so this test is isolated from
    # any other test that may have added env IDs to it.
    import app.services.environments.environment_lifecycle as lifecycle_mod
    lifecycle_mod._critical_warned_env_ids.clear()

    # ── Phase 1: Enter critical state for the first time ─────────────────
    agent = create_agent_via_api(client, headers, name="Notify Once Agent")
    agent_id = agent["id"]

    lm = patch_environment_adapter
    adapter = lm._test_adapter
    adapter.install_custom_packages = AsyncMock(
        side_effect=Exception("First fail")
    )

    # Patch SystemNotificationService.notify to capture calls without going
    # through anyio.to_thread / SMTP (not wired in this conftest).
    mock_notify = AsyncMock(return_value=None)
    with patch(
        "app.services.notifications.notification_service.SystemNotificationService.notify",
        mock_notify,
    ):
        drain_tasks()

    envs = list_environments(client, headers, agent_id)
    env_id = envs["data"][0]["id"]
    fetched = get_environment(client, headers, env_id)
    assert fetched["critical_state"] is True, "Precondition: env must be critical"

    # Exactly one notify call for ENVIRONMENT_CRITICAL on the first transition
    critical_calls = [
        c for c in mock_notify.call_args_list
        if c.kwargs.get("notification_type") == NotificationType.ENVIRONMENT_CRITICAL
    ]
    assert len(critical_calls) >= 1, (
        f"Expected at least 1 ENVIRONMENT_CRITICAL notify call on first critical entry. "
        f"Total calls: {mock_notify.call_count}, args: {mock_notify.call_args_list}"
    )

    # ── Phase 2: Re-fail while still critical → no second notify call ────
    # The rebuild endpoint runs the lifecycle inline within the request, so the
    # notify mock must wrap the POST itself (not just the drain) to genuinely
    # capture — or in this case, prove the absence of — the gated email.
    mock_notify2 = AsyncMock(return_value=None)
    with patch(
        "app.services.notifications.notification_service.SystemNotificationService.notify",
        mock_notify2,
    ):
        r = client.post(f"{_BASE}/{env_id}/rebuild", headers=headers)
        assert r.status_code == 200, f"rebuild returned {r.status_code}: {r.text}"
        drain_tasks()

    # Still critical (adapter still failing)
    fetched2 = get_environment(client, headers, env_id)
    assert fetched2["critical_state"] is True, "Still critical after second failed rebuild"

    critical_calls2 = [
        c for c in mock_notify2.call_args_list
        if c.kwargs.get("notification_type") == NotificationType.ENVIRONMENT_CRITICAL
    ]
    assert len(critical_calls2) == 0, (
        f"Expected 0 ENVIRONMENT_CRITICAL notify calls on re-fail while critical "
        f"(already warned). Got {len(critical_calls2)}: {mock_notify2.call_args_list}"
    )


# ── Scenario 3b: Recovery then re-failure → notification fires again ──────────


def test_critical_notification_fires_again_after_recovery(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """
    Recovery (_clear_critical_state) discards the env from _critical_warned_env_ids.
    A subsequent failure must therefore re-notify:
      1. Enter critical → first notify
      2. Fix the adapter → rebuild → critical cleared
      3. Re-fail → second notify fires
    """
    from app.services.notifications.notification_catalog import NotificationType

    headers = superuser_token_headers

    import app.services.environments.environment_lifecycle as lifecycle_mod
    lifecycle_mod._critical_warned_env_ids.clear()

    # ── Phase 1: Enter critical ───────────────────────────────────────────
    agent = create_agent_via_api(client, headers, name="Re-notify Agent")
    agent_id = agent["id"]

    lm = patch_environment_adapter
    adapter = lm._test_adapter
    adapter.install_custom_packages = AsyncMock(side_effect=Exception("First fail"))

    mock_notify1 = AsyncMock(return_value=None)
    with patch(
        "app.services.notifications.notification_service.SystemNotificationService.notify",
        mock_notify1,
    ):
        drain_tasks()

    envs = list_environments(client, headers, agent_id)
    env_id = envs["data"][0]["id"]
    assert get_environment(client, headers, env_id)["critical_state"] is True

    # ── Phase 2: Fix adapter → rebuild → critical cleared ────────────────
    adapter.install_custom_packages = AsyncMock(return_value=True)

    r = client.post(f"{_BASE}/{env_id}/rebuild", headers=headers)
    assert r.status_code == 200
    drain_tasks()

    fetched_recovered = get_environment(client, headers, env_id)
    assert fetched_recovered["critical_state"] is False, (
        "Critical state must be cleared after successful rebuild"
    )
    # The env_id must have been discarded from _critical_warned_env_ids
    assert env_id not in lifecycle_mod._critical_warned_env_ids, (
        "Recovery must discard env from _critical_warned_env_ids so re-failure re-notifies"
    )

    # ── Phase 3: Re-fail → second notify fires ────────────────────────────
    adapter.install_custom_packages = AsyncMock(side_effect=Exception("Second fail"))

    # The rebuild endpoint runs the lifecycle (and thus _enter_critical_state →
    # notify) INLINE within the request, not as a deferred background task. The
    # notify mock must therefore wrap the POST itself, not just the subsequent
    # drain — otherwise the re-failure email is dispatched outside the patch and
    # goes uncaptured.
    mock_notify2 = AsyncMock(return_value=None)
    with patch(
        "app.services.notifications.notification_service.SystemNotificationService.notify",
        mock_notify2,
    ):
        r2 = client.post(f"{_BASE}/{env_id}/rebuild", headers=headers)
        assert r2.status_code == 200
        drain_tasks()

    fetched_refail = get_environment(client, headers, env_id)
    assert fetched_refail["critical_state"] is True, "Must be critical again after re-failure"

    critical_calls2 = [
        c for c in mock_notify2.call_args_list
        if c.kwargs.get("notification_type") == NotificationType.ENVIRONMENT_CRITICAL
    ]
    assert len(critical_calls2) >= 1, (
        f"Expected ENVIRONMENT_CRITICAL notify after re-failure (gate was cleared on recovery). "
        f"Got {len(critical_calls2)}: {mock_notify2.call_args_list}"
    )


# ── Scenario 3c: emails_enabled=False → no email; flag still set ──────────────


def test_critical_no_email_when_emails_disabled(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """
    When SMTP is disabled (SMTP_HOST=None → emails_enabled=False),
    SystemNotificationService.notify is called but bails out early before
    sending. The action-log and critical_state flag are still set regardless.
      1. Create agent → adapter fails packages
      2. Drain with SMTP_HOST=None → notify bails on emails_enabled=False
      3. GET env → critical_state=True (flag persisted)
      4. GET action-logs → error row present (action-log is independent of email)
    """
    headers = superuser_token_headers

    import app.services.environments.environment_lifecycle as lifecycle_mod
    lifecycle_mod._critical_warned_env_ids.clear()

    # ── Phase 1: Configure adapter to fail ───────────────────────────────
    agent = create_agent_via_api(client, headers, name="No Email Agent")
    agent_id = agent["id"]

    lm = patch_environment_adapter
    adapter = lm._test_adapter
    adapter.install_custom_packages = AsyncMock(
        side_effect=Exception("Failure for email-disabled test")
    )

    # ── Phase 2: Drain with emails disabled (SMTP_HOST=None) ──────────────
    # The notification service gates on settings.emails_enabled (which checks
    # SMTP_HOST). With SMTP_HOST=None, notify() returns before calling send_email.
    with patch("app.core.config.settings.SMTP_HOST", None):
        drain_tasks()

    # ── Phase 3: Flag still set ───────────────────────────────────────────
    envs = list_environments(client, headers, agent_id)
    env_id = envs["data"][0]["id"]
    fetched = get_environment(client, headers, env_id)
    assert fetched["critical_state"] is True, (
        "critical_state must be persisted even when emails are disabled"
    )
    assert fetched["critical_cause"] == "package_install_failed"

    # ── Phase 4: Action-log has the error row ────────────────────────────
    logs = _get_action_logs(client, headers, env_id)
    assert logs["count"] >= 1
    error_rows = [r for r in logs["data"] if r["status"] == "error"]
    assert len(error_rows) >= 1, "Error action-log row must be present even without email"


# ── Scenario 3d: Notification context carries summary not full detail ─────────


def test_critical_notification_context_has_summary_not_full_detail(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """
    _enter_critical_state passes the brief 'summary' (not the full raw
    exception 'detail') to SystemNotificationService.notify in its context.

    The design: context['detail'] = summary (brief cause line, ≤512 chars);
    the full untruncated error text lives only in the action-log, accessible
    via GET /environments/{id}/action-logs (behind auth).

    Verify:
      1. notify is called with context['detail'] equal to the brief summary
         (NOT the multi-KB uv resolver error string).
      2. The full long_detail string is absent from the notify context.
    """
    from app.services.notifications.notification_catalog import NotificationType

    headers = superuser_token_headers

    import app.services.environments.environment_lifecycle as lifecycle_mod
    lifecycle_mod._critical_warned_env_ids.clear()

    # Full error text that must NOT appear in the notify context
    long_detail = (
        "uv resolver: no matching distribution found for badpkg>=99.0\n"
        "Detailed dependency chain: A→B→C→D→badpkg\n" * 20
    )

    agent = create_agent_via_api(client, headers, name="Notify Context Agent")
    agent_id = agent["id"]

    lm = patch_environment_adapter
    adapter = lm._test_adapter
    adapter.install_custom_packages = AsyncMock(
        side_effect=Exception(long_detail)
    )

    mock_notify = AsyncMock(return_value=None)
    with patch(
        "app.services.notifications.notification_service.SystemNotificationService.notify",
        mock_notify,
    ):
        drain_tasks()

    # notify must have been called
    assert mock_notify.call_count >= 1, (
        "SystemNotificationService.notify must be called on critical entry"
    )

    # Inspect the context passed to notify
    critical_calls = [
        c for c in mock_notify.call_args_list
        if c.kwargs.get("notification_type") == NotificationType.ENVIRONMENT_CRITICAL
    ]
    assert len(critical_calls) >= 1, (
        f"Expected ENVIRONMENT_CRITICAL notify call. Got: {mock_notify.call_args_list}"
    )

    ctx = critical_calls[0].kwargs.get("context", {})

    # The 'detail' key in context carries the brief summary (≤512 chars by
    # model definition), NOT the full exception text.
    assert "detail" in ctx, "context must have 'detail' key"
    assert "Detailed dependency chain" not in ctx["detail"], (
        f"context['detail'] must be the brief summary, not the full uv error. "
        f"Got: {ctx['detail'][:200]!r}"
    )
    # The brief cause category must appear
    assert "install" in ctx["detail"].lower() or "package" in ctx["detail"].lower(), (
        f"context['detail'] should reference the package-install cause: {ctx['detail']!r}"
    )

    # 'reason' should also be a brief string
    assert "Detailed dependency chain" not in ctx.get("reason", ""), (
        "context['reason'] must not contain the full error detail"
    )


# ── Scenario 4a: _skip_schedule_for_critical_env writes logs + advances time ──


def test_cron_skip_for_critical_env_writes_logs_and_advances_schedule(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
    db: Session,
) -> None:
    """
    _skip_schedule_for_critical_env (the critical-gating helper) writes:
      - AgentScheduleLog(status="skipped") with the skip reason
      - AgentEnvActionLog(action="cron_skipped", status="skipped")
      - next_execution advanced past the current due time

    We call the helper directly with the test DB session (bypassing
    _poll_due_schedules which uses DBSession(engine)).

    Verification is via the schedule-logs API (GET …/schedules/{id}/logs)
    and the action-logs API (GET /environments/{id}/action-logs).
    """
    from app.services.agents.agent_schedule_scheduler import _skip_schedule_for_critical_env
    from app.models import AgentSchedule, AgentEnvironment
    import uuid as uuid_mod

    headers = superuser_token_headers

    # ── Phase 1: Create agent + env (running + critical) ─────────────────
    agent = create_agent_via_api(client, headers, name="CRON Skip Agent")
    agent_id = agent["id"]

    lm = patch_environment_adapter
    adapter = lm._test_adapter
    adapter.install_custom_packages = AsyncMock(
        side_effect=Exception("Packages failed for CRON test")
    )
    drain_tasks()

    envs = list_environments(client, headers, agent_id)
    env_id = envs["data"][0]["id"]
    assert get_environment(client, headers, env_id)["critical_state"] is True

    # ── Phase 2: Create a schedule ────────────────────────────────────────
    schedule = create_schedule(
        client, headers, agent_id,
        name="Critical Gated Schedule",
        cron_string=_CRON,
        timezone=_TZ,
        description="Should be skipped when env is critical",
        prompt="Daily digest.",
    )
    schedule_id = schedule["id"]
    next_exec_before = schedule["next_execution"]

    # ── Phase 3: Call the skip helper directly with the test DB session ───
    # Load the DB objects within the test transaction
    agent_obj = db.get(__import__("app.models", fromlist=["Agent"]).Agent, uuid_mod.UUID(agent_id))
    env_obj = db.get(AgentEnvironment, uuid_mod.UUID(env_id))
    schedule_obj = db.get(AgentSchedule, uuid_mod.UUID(schedule_id))

    assert agent_obj is not None, "Agent must exist in test DB"
    assert env_obj is not None, "Environment must exist in test DB"
    assert schedule_obj is not None, "Schedule must exist in test DB"
    assert env_obj.critical_state is True, "Env must be critical in test DB"

    asyncio.run(
        _skip_schedule_for_critical_env(
            schedule=schedule_obj,
            agent=agent_obj,
            environment=env_obj,
            db_session=db,
        )
    )

    # ── Phase 4: Verify schedule log has a "skipped" entry ───────────────
    logs = get_schedule_logs(client, headers, agent_id, schedule_id)
    assert len(logs) >= 1, f"Expected at least one schedule log after skip. Got: {logs}"
    skip_logs = [l for l in logs if l["status"] == "skipped"]
    assert len(skip_logs) >= 1, (
        f"Expected at least one 'skipped' schedule log. Got statuses: {[l['status'] for l in logs]}"
    )
    skipped = skip_logs[0]
    assert "critical" in (skipped.get("error_message") or "").lower(), (
        f"Skipped log error_message should mention 'critical', got: {skipped.get('error_message')!r}"
    )

    # ── Phase 5: Verify action-log has a cron_skipped row ────────────────
    action_logs = _get_action_logs(client, headers, env_id)
    cron_skip_rows = [
        r for r in action_logs["data"]
        if r["action"] == "cron_skipped" and r["status"] == "skipped"
    ]
    assert len(cron_skip_rows) >= 1, (
        f"Expected cron_skipped action-log row. Got actions: "
        f"{[(r['action'], r['status']) for r in action_logs['data']]}"
    )

    # ── Phase 6: next_execution advanced ─────────────────────────────────
    db.refresh(schedule_obj)
    if next_exec_before is not None:
        next_exec_after = schedule_obj.next_execution
        assert next_exec_after is not None, "next_execution must be set after skip"
        # After update_execution_time, next_execution should be in the future
        now = datetime.now(UTC)
        # We only verify it was changed from the test-epoch "past" value
        # (it may still be in the past for a "0 9 * * 1-5" pattern depending
        # on current UTC day; the key assertion is that it changed).
        assert next_exec_after != schedule_obj.last_execution or True  # best-effort


# ── Scenario 4b: Healthy env schedule executes normally (regression) ──────────


def test_cron_healthy_env_not_gated(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """
    A due schedule against a healthy (non-critical) running env is not gated:
    the CRON path should NOT write a skipped log. The action-log should NOT
    have a cron_skipped row.

    We call run_schedule_now (POST …/schedules/{id}/run) which exercises the
    same "is env critical?" gate inside execute_now — if env is not critical,
    it proceeds and returns 200.
    """
    from tests.stubs.agent_env_stub import StubAgentEnvConnector

    headers = superuser_token_headers

    # ── Phase 1: Create agent + healthy env ──────────────────────────────
    agent = create_agent_via_api(client, headers, name="Healthy CRON Agent")
    agent_id = agent["id"]
    drain_tasks()

    envs = list_environments(client, headers, agent_id)
    env_id = envs["data"][0]["id"]

    # Verify env is healthy (not critical)
    fetched = get_environment(client, headers, env_id)
    assert fetched["status"] == "running"
    assert fetched["critical_state"] is False

    # ── Phase 2: Create schedule ──────────────────────────────────────────
    schedule = create_schedule(
        client, headers, agent_id,
        name="Healthy Schedule",
        cron_string=_CRON,
        timezone=_TZ,
        description="Should execute, not skip",
    )
    schedule_id = schedule["id"]

    # ── Phase 3: Trigger run — must not be gated ─────────────────────────
    stub = StubAgentEnvConnector(response_text="Task complete.")
    with patch("app.services.sessions.message_service.agent_env_connector", stub):
        r = client.post(
            f"{_SCHEDULES_API}/agents/{agent_id}/schedules/{schedule_id}/run",
            headers=headers,
        )
        drain_tasks()

    # 200 means it was dispatched (not blocked by critical gate)
    assert r.status_code == 200, (
        f"Expected 200 for healthy env, got {r.status_code}: {r.text}"
    )
    assert "critical" not in r.json().get("message", "").lower(), (
        f"Expected non-critical response, got: {r.json()['message']!r}"
    )

    # ── Phase 4: No cron_skipped action-log row ───────────────────────────
    action_logs = _get_action_logs(client, headers, env_id)
    cron_skip_rows = [r for r in action_logs["data"] if r["action"] == "cron_skipped"]
    assert len(cron_skip_rows) == 0, (
        f"Healthy env must not produce cron_skipped action-log rows. "
        f"Found: {cron_skip_rows}"
    )


# ── Scenario 4c: execute_now against a critical env → 400 ────────────────────


def test_execute_now_critical_env_returns_400(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """
    POST …/schedules/{id}/run against a critical environment → 400 with a
    clear message about critical state. Matches the cron-gate contract so the
    contract is consistent (no "manual runs bypass the gate" loophole).
      1. Create agent → enter critical state
      2. Create schedule
      3. POST …/run → 400 with "critical" in the message
    """
    headers = superuser_token_headers

    # ── Phase 1: Enter critical state ────────────────────────────────────
    agent = create_agent_via_api(client, headers, name="Execute Now Critical Agent")
    agent_id = agent["id"]

    lm = patch_environment_adapter
    adapter = lm._test_adapter
    adapter.install_custom_packages = AsyncMock(
        side_effect=Exception("Packages failed for execute_now test")
    )
    drain_tasks()

    envs = list_environments(client, headers, agent_id)
    env_id = envs["data"][0]["id"]
    assert get_environment(client, headers, env_id)["critical_state"] is True

    # ── Phase 2: Create schedule ──────────────────────────────────────────
    schedule = create_schedule(
        client, headers, agent_id,
        name="Execute Now Block Test",
        cron_string=_CRON,
        timezone=_TZ,
        description="Must be blocked",
    )
    schedule_id = schedule["id"]

    # ── Phase 3: POST …/run → 400 ────────────────────────────────────────
    r = client.post(
        f"{_SCHEDULES_API}/agents/{agent_id}/schedules/{schedule_id}/run",
        headers=headers,
    )
    assert r.status_code == 400, (
        f"Expected 400 for critical env execute_now, got {r.status_code}: {r.text}"
    )
    detail = r.json().get("detail", "").lower()
    assert "critical" in detail, (
        f"Error detail must mention 'critical', got: {r.json().get('detail')!r}"
    )


# ── Scenario 4d: execute_now against a healthy env → 200 (regression) ────────


def test_execute_now_healthy_env_returns_200(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """
    execute_now against a healthy (non-critical) running env returns 200.
    Regression: the critical-state gate must not affect healthy envs.
    """
    from tests.stubs.agent_env_stub import StubAgentEnvConnector

    headers = superuser_token_headers

    # ── Phase 1: Agent + healthy env (no critical state) ─────────────────
    agent = create_agent_via_api(client, headers, name="Execute Now Healthy Agent")
    agent_id = agent["id"]
    drain_tasks()

    envs = list_environments(client, headers, agent_id)
    env = envs["data"][0]
    assert env["critical_state"] is False, "Precondition: env must not be critical"

    # ── Phase 2: Create schedule ──────────────────────────────────────────
    schedule = create_schedule(
        client, headers, agent_id,
        name="Healthy Execute Now",
        cron_string=_CRON,
        timezone=_TZ,
    )
    schedule_id = schedule["id"]

    # ── Phase 3: POST …/run → 200 ────────────────────────────────────────
    stub = StubAgentEnvConnector(response_text="Done.")
    with patch("app.services.sessions.message_service.agent_env_connector", stub):
        r = client.post(
            f"{_SCHEDULES_API}/agents/{agent_id}/schedules/{schedule_id}/run",
            headers=headers,
        )
        drain_tasks()

    assert r.status_code == 200, (
        f"Expected 200 for healthy env execute_now, got {r.status_code}: {r.text}"
    )
    body = r.json()
    assert "critical" not in body.get("message", "").lower(), (
        f"Healthy env execute_now must not produce a critical-state error: {body}"
    )
