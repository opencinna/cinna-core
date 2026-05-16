"""
Phase 5 migration — targeted coverage for ``execute_task`` gaps.

Two scenarios not yet covered by any existing test:

  Scenario 4 (must-have): POST /tasks/{id}/execute with a mismatched user_id
    — i.e., user B calls /execute on user A's task.
    The route calls ``get_task_with_ownership_check(user_id=current_user.id)``
    which raises ``PermissionDeniedError`` when ``task.owner_id != user_id``.
    This is the Fix 1 correctness gate: ``SessionSender.from_task_execution``
    now records the *executing* user's identity, so the ownership guard must
    block the wrong user before a spurious sender is stamped.

  Scenario 5: POST /tasks/{id}/execute when the agent has no active environment.
    ``ChannelIngestionService.ingest_inbound_message`` raises
    ``NoActiveEnvironmentError``, caught by ``execute_task`` which:
      - returns ``(False, None, "Failed to create session for agent")``
      - sets the task status to ``error``
    The route wraps this in ``ExecuteTaskResponse(success=False, error=...)``
    and returns HTTP 200.

Nothing else is tested here — existing tests in test_task_auto_execute.py
and test_task_agent_api.py cover the happy path and session stamping.
"""
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.stubs.agent_env_stub import StubAgentEnvConnector
from tests.utils.agent import create_agent_via_api
from tests.utils.background_tasks import drain_tasks
from tests.utils.environment import delete_environment, list_environments
from tests.utils.input_task import create_task, get_task
from tests.utils.user import create_random_user_with_headers

_BASE = f"{settings.API_V1_STR}/tasks"
_AGENT_ENV_PATCH = "app.services.sessions.message_service.agent_env_connector"


def test_execute_task_rejected_for_non_owner(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Scenario 4 — Fix 1 correctness gate:
    POST /tasks/{id}/execute by a non-owner must be rejected (400 or 404).

      1. User A (superuser) creates an agent and a task.
      2. User B is created.
      3. User B attempts POST /tasks/{id}/execute on User A's task.
      4. Route must return 400 (PermissionDeniedError) or 404.
      5. Task status is still 'new' — no session was created.

    This is the end-to-end guard for the SessionSender.from_task_execution
    identity stamp: the wrong user must never reach execute_task() so a
    spurious platform_user_id can never be stamped onto the session sender.
    """
    headers_a = superuser_token_headers

    # ── Phase 1: User A creates agent and task ───────────────────────────────
    agent = create_agent_via_api(client, headers_a, name="Execute Ownership Agent")
    drain_tasks()
    agent_id = agent["id"]

    task = create_task(
        client, headers_a,
        original_message="Ownership enforcement test task",
        selected_agent_id=agent_id,
    )
    task_id = task["id"]
    assert task["status"] == "new"

    # ── Phase 2: Create user B ────────────────────────────────────────────────
    _, headers_b = create_random_user_with_headers(client)

    # ── Phase 3: User B attempts /execute on user A's task ───────────────────
    r = client.post(
        f"{_BASE}/{task_id}/execute",
        headers=headers_b,
        json={"mode": "conversation"},
    )

    # ── Phase 4: Must be rejected ─────────────────────────────────────────────
    # get_task_with_ownership_check raises PermissionDeniedError (→ 400)
    # or, depending on isolaton, the task is simply not found (→ 404).
    assert r.status_code in (400, 404), (
        f"Expected 400 or 404 for non-owner execute, got {r.status_code}: {r.text}"
    )

    # ── Phase 5: Task status unchanged — no session was created ──────────────
    # Only user A can verify the task status.
    task_after = get_task(client, headers_a, task_id)
    assert task_after["status"] == "new", (
        f"Task status should remain 'new' after rejected /execute, "
        f"got '{task_after['status']}'"
    )
    # No session linked
    r_sessions = client.get(f"{_BASE}/{task_id}/sessions", headers=headers_a)
    assert r_sessions.status_code == 200
    assert r_sessions.json()["data"] == [], (
        "No session should be linked to the task after rejected execute"
    )


def test_execute_task_no_active_env_returns_error_status(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Scenario 5 — NoActiveEnvironmentError path in execute_task:
    POST /tasks/{id}/execute when the agent has no active environment.

      1. Create agent → env1 is auto-activated.
      2. Create task assigned to the agent.
      3. Delete env1 so the agent has no active_environment_id.
      4. POST /tasks/{id}/execute → must return HTTP 200 with success=False
         and an error message indicating session creation failed.
      5. GET task → status must be 'error' (set by the NoActiveEnvironmentError path).

    This exercises the ``except NoActiveEnvironmentError`` branch added in
    Phase 5 inside ``InputTaskService.execute_task``.
    """
    headers = superuser_token_headers

    # ── Phase 1: Create agent → env1 active ──────────────────────────────────
    agent = create_agent_via_api(client, headers, name="NoEnv Execute Agent")
    drain_tasks()
    agent_id = agent["id"]

    envs = list_environments(client, headers, agent_id)
    assert envs["count"] == 1
    env1_id = envs["data"][0]["id"]
    assert envs["data"][0]["is_active"] is True

    # ── Phase 2: Create task assigned to the agent ───────────────────────────
    task = create_task(
        client, headers,
        original_message="No-active-env execute test task",
        selected_agent_id=agent_id,
    )
    task_id = task["id"]
    assert task["status"] == "new"

    # ── Phase 3: Delete env1 → agent has no active environment ───────────────
    delete_environment(client, headers, env1_id)

    # Confirm agent now has no active environment by inspecting the agent record
    r_agent = client.get(f"{settings.API_V1_STR}/agents/{agent_id}", headers=headers)
    assert r_agent.status_code == 200
    assert r_agent.json()["active_environment_id"] is None, (
        "Agent should have no active environment after env deletion"
    )

    # ── Phase 4: Execute → success=False, HTTP 200 ───────────────────────────
    # No stub needed — session creation fails before streaming is reached.
    r = client.post(
        f"{_BASE}/{task_id}/execute",
        headers=headers,
        json={"mode": "conversation"},
    )
    assert r.status_code == 200, (
        f"execute_task error path must return HTTP 200, got {r.status_code}: {r.text}"
    )
    body = r.json()
    assert body["success"] is False, (
        f"Expected success=False for no-active-env execute, got: {body}"
    )
    assert body.get("error") is not None, (
        "Expected non-null error message in response body"
    )
    assert "session" in body["error"].lower() or "environment" in body["error"].lower(), (
        f"Error message should mention session/environment failure, got: {body['error']!r}"
    )

    # ── Phase 5: Task status → 'error' (set by NoActiveEnvironmentError handler) ─
    # Drain to let any background status-sync work settle.
    drain_tasks()

    task_after = get_task(client, headers, task_id)
    assert task_after["status"] == "error", (
        f"Expected task status 'error' after no-active-env execute, "
        f"got '{task_after['status']}'"
    )
