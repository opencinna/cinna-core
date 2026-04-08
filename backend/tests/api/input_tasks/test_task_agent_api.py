"""
Tests for the Agent Task API endpoints (task_agent_api.py).

These endpoints are consumed by MCP tools running in agent environments.
Authentication uses the same Bearer JWT as other endpoints.

Agent API routes:
  POST /agent/tasks/{task_id}/comment      — agent posts a comment
  POST /agent/tasks/{task_id}/status       — agent updates status (blocked/completed/cancelled)
  POST /agent/tasks/{task_id}/subtask      — agent creates subtask (team context)
  GET  /agent/tasks/my-tasks               — agent lists their tasks
  GET  /agent/tasks/{task_id}/details      — agent gets task details

Note: Agents require AI credentials to be set up. Tests in this file use
superuser_token_headers (which has default credentials from setup_default_credentials
fixture) to create agents and tasks.

Scenarios:
  1. Agent comment — user posts comment via agent endpoint, comment has agent attribution
  2. Agent status update — valid transitions (new→cancelled)
  3. Agent status update — invalid transition returns 400
  4. Agent status update — agent-disallowed targets (e.g., new, archived) return 400
  5. Agent status update — task with no selected_agent_id returns 400
  6. Agent status update — status history and system comment created
  7. Agent subtask creation — requires team context (no team_id → 400)
  8. Agent subtask creation — valid team member creates subtask
  9. Agent subtask creation — non-team agent returns 400
  10. Agent subtask creation — no connection to target returns 400
  11. My-tasks — returns owner's tasks
  12. Task details — returns task with recent_comments, subtask_progress
  13. Ownership enforcement on agent API endpoints
  14. Session-resolved subtask — POST /agent/tasks/current/subtask resolves parent via Session.source_task_id
  15. Session-resolved subtask after re-execution — old session still resolves after task.session_id is overwritten
"""
import uuid

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.agentic_team import (
    create_team,
    create_node,
    create_connection,
)
from unittest.mock import patch

from tests.stubs.agent_env_stub import StubAgentEnvConnector
from tests.utils.agent import create_agent_via_api
from tests.utils.background_tasks import drain_tasks
from tests.utils.input_task import (
    create_task,
    get_task,
    get_task_by_code,
    get_task_detail,
    add_comment,
    list_comments,
    execute_task,
)
from tests.utils.user import create_random_user, user_authentication_headers

_BASE = f"{settings.API_V1_STR}"
_TASKS_BASE = f"{settings.API_V1_STR}/tasks"


def test_agent_comment_endpoint(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Agent posts a comment via POST /agent/tasks/{task_id}/comment:
      1. Create agent and task with selected_agent_id set
      2. POST agent comment — comment appears with agent attribution
      3. List comments — agent comment present
      4. Comment has correct content and author_agent_id
      5. Non-existent task returns 404
      6. Other user's task returns 400/404 (PermissionDeniedError)
    """
    headers = superuser_token_headers

    # ── Phase 1: Create agent and task ───────────────────────────────────────
    agent = create_agent_via_api(client, headers, name="Commenting Agent")
    agent_id = agent["id"]

    task = create_task(
        client, headers,
        original_message="Task for agent comment test",
        selected_agent_id=agent_id,
    )
    task_id = task["id"]

    # ── Phase 2: Agent posts comment ─────────────────────────────────────────
    r = client.post(
        f"{_BASE}/agent/tasks/{task_id}/comment",
        headers=headers,
        json={"content": "Agent progress update: completed phase 1", "comment_type": "message"},
    )
    assert r.status_code == 200, f"Agent comment failed: {r.text}"
    comment = r.json()

    # Response is AgentCommentResponse: comment_id, task (short_code), attachments_count
    assert "comment_id" in comment
    assert comment["task"] == task["short_code"]

    # ── Phase 3: Comment appears in list ─────────────────────────────────────
    comments_result = client.get(
        f"{_TASKS_BASE}/{task_id}/comments/",
        headers=headers,
    )
    assert comments_result.status_code == 200
    comment_ids = [c["id"] for c in comments_result.json()["data"]]
    assert comment["comment_id"] in comment_ids

    # ── Phase 4: Non-existent task returns 404 ───────────────────────────────
    ghost = str(uuid.uuid4())
    r = client.post(
        f"{_BASE}/agent/tasks/{ghost}/comment",
        headers=headers,
        json={"content": "Ghost comment", "comment_type": "message"},
    )
    assert r.status_code == 404

    # ── Phase 5: Other user's task returns 400/404 ───────────────────────────
    other = create_random_user(client)
    other_h = user_authentication_headers(
        client=client, email=other["email"], password=other["_password"]
    )
    r = client.post(
        f"{_BASE}/agent/tasks/{task_id}/comment",
        headers=other_h,
        json={"content": "Intruder", "comment_type": "message"},
    )
    assert r.status_code in (400, 404)  # PermissionDeniedError → 400


def test_agent_status_update_valid_transitions(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Agent updates task status via POST /agent/tasks/{task_id}/status:
      1. Create task with selected_agent_id, in 'new' status
      2. Agent updates new → cancelled (valid transition, valid agent target)
      3. Status history appears in task detail
      4. System comment of type status_change added
      5. Invalid target status for agent (e.g., 'new') returns 400
      6. Task with no selected_agent_id → 400
    """
    headers = superuser_token_headers

    agent = create_agent_via_api(client, headers, name="Status Agent")
    agent_id = agent["id"]

    # Create task with agent assigned, in 'new' status
    task = create_task(
        client, headers,
        original_message="Status update test task",
        selected_agent_id=agent_id,
    )
    task_id = task["id"]

    # ── Phase 1: Agent sets task to 'cancelled' (new→cancelled is valid) ─────
    r = client.post(
        f"{_BASE}/agent/tasks/{task_id}/status",
        headers=headers,
        json={"status": "cancelled", "reason": "No longer needed"},
    )
    assert r.status_code == 200, f"Agent status update failed: {r.text}"
    result = r.json()
    assert result["success"] is True
    assert result["task"] is not None  # short_code

    # Task is now cancelled
    fetched = get_task(client, headers, task_id)
    assert fetched["status"] == "cancelled"

    # ── Phase 2: Status history and system comment created ───────────────────
    detail = get_task_detail(client, headers, task_id)
    assert len(detail["status_history"]) >= 1
    last_history = detail["status_history"][-1]
    assert last_history["to_status"] == "cancelled"

    # System comment added for the transition
    comments_result = client.get(f"{_TASKS_BASE}/{task_id}/comments/", headers=headers)
    status_comments = [
        c for c in comments_result.json()["data"]
        if c["comment_type"] == "status_change"
    ]
    assert len(status_comments) >= 1

    # ── Phase 3: Disallowed agent status returns 400 ─────────────────────────
    # 'new', 'open', 'archived' etc. are not in {blocked, completed, cancelled}
    task2 = create_task(
        client, headers,
        original_message="Invalid target status test",
        selected_agent_id=agent_id,
    )
    r = client.post(
        f"{_BASE}/agent/tasks/{task2['id']}/status",
        headers=headers,
        json={"status": "new"},  # not in allowed_agent_statuses
    )
    assert r.status_code == 400

    r = client.post(
        f"{_BASE}/agent/tasks/{task2['id']}/status",
        headers=headers,
        json={"status": "archived"},  # not in allowed_agent_statuses
    )
    assert r.status_code == 400

    # ── Phase 4: Task with no selected_agent_id → 400 ────────────────────────
    task_no_agent = create_task(client, headers, original_message="No agent task")
    r = client.post(
        f"{_BASE}/agent/tasks/{task_no_agent['id']}/status",
        headers=headers,
        json={"status": "cancelled"},
    )
    assert r.status_code == 400


def test_agent_status_invalid_transition_400(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    An invalid status transition returns 400:
      1. Create task in 'new' status with agent
      2. Cancel it (new → cancelled)
      3. Attempt cancelled → completed — invalid transition (returns 400)
    """
    headers = superuser_token_headers

    agent = create_agent_via_api(client, headers, name="Invalid Transition Agent")
    agent_id = agent["id"]

    task = create_task(
        client, headers,
        original_message="Invalid transition test",
        selected_agent_id=agent_id,
    )
    task_id = task["id"]

    # valid: new → cancelled
    r = client.post(
        f"{_BASE}/agent/tasks/{task_id}/status",
        headers=headers,
        json={"status": "cancelled"},
    )
    assert r.status_code == 200

    # Invalid: cancelled → completed (not in VALID_TRANSITIONS["cancelled"])
    r = client.post(
        f"{_BASE}/agent/tasks/{task_id}/status",
        headers=headers,
        json={"status": "completed"},
    )
    assert r.status_code == 400, (
        f"Expected 400 for cancelled→completed, got {r.status_code}: {r.text}"
    )


def test_agent_subtask_creation_no_team_400(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Agent subtask creation without team context returns 400:
      1. Create task with agent but no team_id
      2. POST /agent/tasks/{id}/subtask — 400 (no team_id on parent task)
    """
    headers = superuser_token_headers

    agent = create_agent_via_api(client, headers, name="No-team Subtask Agent")
    agent_id = agent["id"]

    task = create_task(
        client, headers,
        original_message="No team task for agent subtask",
        selected_agent_id=agent_id,
    )

    r = client.post(
        f"{_BASE}/agent/tasks/{task['id']}/subtask",
        headers=headers,
        json={"title": "Agent subtask", "description": "Should fail"},
    )
    assert r.status_code == 400
    assert "team" in r.json()["detail"].lower()


def test_agent_subtask_creation_with_team_context(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Agent creates subtask in team context:
      1. Create team with two agents (lead and worker)
      2. Create connection from lead node to worker node
      3. Create parent task with team_id and lead agent selected
      4. POST /agent/tasks/{id}/subtask with assigned_to=worker_node_name
      5. Subtask created with correct team_id and assigned_node_id
      6. System comment posted on parent task
    """
    headers = superuser_token_headers

    # ── Phase 1: Create agents and team ──────────────────────────────────────
    lead_agent = create_agent_via_api(client, headers, name="Lead Agent for Subtask Test")
    worker_agent = create_agent_via_api(client, headers, name="Worker Agent for Subtask Test")

    team = create_team(client, headers, name="Subtask Delegation Team")
    team_id = team["id"]

    lead_node = create_node(client, headers, team_id, lead_agent["id"], is_lead=True)
    worker_node = create_node(client, headers, team_id, worker_agent["id"])
    worker_node_name = worker_node["name"]  # auto-populated from agent.name

    # ── Phase 2: Create connection from lead to worker ────────────────────────
    create_connection(
        client, headers, team_id,
        source_node_id=lead_node["id"],
        target_node_id=worker_node["id"],
        enabled=True,
    )

    # ── Phase 3: Create parent task with team and lead agent ─────────────────
    parent = create_task(
        client, headers,
        original_message="Parent team task for agent subtask",
        team_id=team_id,
        selected_agent_id=lead_agent["id"],
    )
    parent_id = parent["id"]

    # ── Phase 4: Agent creates subtask via agent API ──────────────────────────
    r = client.post(
        f"{_BASE}/agent/tasks/{parent_id}/subtask",
        headers=headers,
        json={
            "title": "Worker's delegated subtask",
            "description": "Please handle this part",
            "assigned_to": worker_node_name,
            "priority": "high",
        },
    )
    assert r.status_code == 200, f"Agent subtask creation failed: {r.text}"
    result = r.json()
    assert result["success"] is True
    assert result["task"] is not None  # short_code of created subtask

    subtask_short_code = result["task"]

    # ── Phase 5: Subtask has correct team and assignment ─────────────────────
    subtask_by_code = client.get(
        f"{_TASKS_BASE}/by-code/{subtask_short_code}",
        headers=headers,
    )
    assert subtask_by_code.status_code == 200
    subtask_data = subtask_by_code.json()
    assert subtask_data["parent_task_id"] == parent_id
    assert subtask_data["team_id"] == team_id
    assert subtask_data["assigned_node_id"] == worker_node["id"]
    assert subtask_data["selected_agent_id"] == worker_agent["id"]

    # ── Phase 6: System comment posted on parent task ─────────────────────────
    parent_comments = client.get(
        f"{_TASKS_BASE}/{parent_id}/comments/",
        headers=headers,
    )
    assert parent_comments.status_code == 200
    system_comments = [
        c for c in parent_comments.json()["data"]
        if c["comment_type"] == "system"
    ]
    assert len(system_comments) >= 1
    # Comment mentions the new subtask short_code
    assert any(subtask_short_code in c["content"] for c in system_comments)


def test_agent_subtask_non_team_member_400(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Agent not in the task's team cannot create subtasks:
      1. Create team with one agent (lead)
      2. Create separate agent NOT in the team
      3. Create parent task with team_id but selected_agent_id = outsider agent
      4. POST /agent/tasks/{id}/subtask — 400 (agent not in team)
    """
    headers = superuser_token_headers

    lead_agent = create_agent_via_api(client, headers, name="Lead in Team")
    outsider_agent = create_agent_via_api(client, headers, name="Outsider Agent")

    team = create_team(client, headers, name="Exclusive Team")
    team_id = team["id"]
    create_node(client, headers, team_id, lead_agent["id"], is_lead=True)
    # outsider_agent NOT added to team

    parent = create_task(
        client, headers,
        original_message="Team task with outsider agent",
        team_id=team_id,
        selected_agent_id=outsider_agent["id"],  # outsider assigned
    )

    r = client.post(
        f"{_BASE}/agent/tasks/{parent['id']}/subtask",
        headers=headers,
        json={"title": "Outsider subtask"},
    )
    assert r.status_code == 400
    assert "team" in r.json()["detail"].lower() or "member" in r.json()["detail"].lower()


def test_agent_subtask_no_connection_to_target_400(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Agent cannot delegate to a team member with no connection:
      1. Create team with lead and worker, but NO connection between them
      2. POST /agent/tasks/{id}/subtask with assigned_to=worker — 400
    """
    headers = superuser_token_headers

    lead_agent = create_agent_via_api(client, headers, name="Disconnected Lead")
    worker_agent = create_agent_via_api(client, headers, name="Disconnected Worker")

    team = create_team(client, headers, name="Disconnected Team")
    team_id = team["id"]
    create_node(client, headers, team_id, lead_agent["id"], is_lead=True)
    worker_node = create_node(client, headers, team_id, worker_agent["id"])
    # No connection created between lead and worker

    parent = create_task(
        client, headers,
        original_message="No connection test parent",
        team_id=team_id,
        selected_agent_id=lead_agent["id"],
    )

    r = client.post(
        f"{_BASE}/agent/tasks/{parent['id']}/subtask",
        headers=headers,
        json={
            "title": "Disconnected subtask",
            "assigned_to": worker_node["name"],
        },
    )
    assert r.status_code == 400
    assert "connection" in r.json()["detail"].lower() or "topology" in r.json()["detail"].lower()


def test_agent_list_my_tasks(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    GET /agent/tasks/my-tasks returns owner's tasks:
      1. Create several tasks
      2. GET my-tasks — all appear in data list
      3. Filter by status — works correctly
      4. Other user gets their own tasks, not User A's
    """
    headers = superuser_token_headers

    task1 = create_task(client, headers, original_message="My tasks test 1")
    task2 = create_task(client, headers, original_message="My tasks test 2")

    r = client.get(f"{_BASE}/agent/tasks/my-tasks", headers=headers)
    assert r.status_code == 200
    result = r.json()
    task_ids = [t["id"] for t in result["data"]]
    assert task1["id"] in task_ids
    assert task2["id"] in task_ids

    # Status filter
    r = client.get(
        f"{_BASE}/agent/tasks/my-tasks",
        headers=headers,
        params={"status": "new"},
    )
    assert r.status_code == 200
    new_tasks = r.json()["data"]
    assert all(t["status"] == "new" for t in new_tasks)
    assert task1["id"] in [t["id"] for t in new_tasks]

    # Other user only sees their own tasks
    other = create_random_user(client)
    other_h = user_authentication_headers(
        client=client, email=other["email"], password=other["_password"]
    )
    r = client.get(f"{_BASE}/agent/tasks/my-tasks", headers=other_h)
    assert r.status_code == 200
    other_task_ids = [t["id"] for t in r.json()["data"]]
    assert task1["id"] not in other_task_ids
    assert task2["id"] not in other_task_ids


def test_agent_get_task_details(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    GET /agent/tasks/{task_id}/details returns agent-optimized task view:
      1. Create task with a comment
      2. GET details — returns dict with task (short_code), title, status, recent_comments
      3. recent_comments include the comment we added
      4. subtask_progress has expected keys
      5. Non-existent task returns 404
      6. Other user returns 400/404
    """
    headers = superuser_token_headers

    task = create_task(client, headers, original_message="Agent details test task", title="Details Task")
    task_id = task["id"]

    # Add a comment via standard API
    add_comment(client, headers, task_id, content="Status: analyzing data", comment_type="message")

    # GET agent details
    r = client.get(f"{_BASE}/agent/tasks/{task_id}/details", headers=headers)
    assert r.status_code == 200
    details = r.json()

    # Agent details returns a simplified dict (not InputTaskPublic schema)
    assert "task" in details  # short_code
    assert details["status"] == "new"
    assert "recent_comments" in details
    assert "subtask_progress" in details
    assert "title" in details
    assert "description" in details
    assert "priority" in details

    # Comment appears in recent_comments
    comment_contents = [c["content"] for c in details["recent_comments"]]
    assert any("analyzing data" in content for content in comment_contents)

    # subtask_progress has expected keys
    progress = details["subtask_progress"]
    assert "total" in progress
    assert "completed" in progress
    assert "in_progress" in progress
    assert "blocked" in progress
    assert progress["total"] == 0

    # Non-existent task 404
    ghost = str(uuid.uuid4())
    r = client.get(f"{_BASE}/agent/tasks/{ghost}/details", headers=headers)
    assert r.status_code == 404

    # Other user 400/404
    other = create_random_user(client)
    other_h = user_authentication_headers(
        client=client, email=other["email"], password=other["_password"]
    )
    r = client.get(f"{_BASE}/agent/tasks/{task_id}/details", headers=other_h)
    assert r.status_code in (400, 404)  # PermissionDeniedError → 400


def test_agent_api_unauthenticated_rejected(
    client: TestClient,
) -> None:
    """All agent API endpoints reject unauthenticated requests."""
    ghost_id = str(uuid.uuid4())

    assert client.post(
        f"{_BASE}/agent/tasks/{ghost_id}/comment",
        json={"content": "test", "comment_type": "message"},
    ).status_code in (401, 403)

    assert client.post(
        f"{_BASE}/agent/tasks/{ghost_id}/status",
        json={"status": "completed"},
    ).status_code in (401, 403)

    assert client.post(
        f"{_BASE}/agent/tasks/{ghost_id}/subtask",
        json={"title": "test"},
    ).status_code in (401, 403)

    assert client.get(f"{_BASE}/agent/tasks/my-tasks").status_code in (401, 403)

    assert client.get(
        f"{_BASE}/agent/tasks/{ghost_id}/details"
    ).status_code in (401, 403)


_AGENT_ENV_PATCH = "app.services.sessions.message_service.agent_env_connector"


def test_session_resolved_subtask_creation(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    POST /agent/tasks/current/subtask resolves the parent task via
    Session.source_task_id (not InputTask.session_id).

      1. Create team with lead + worker agents and a connection
      2. Create team-scoped task assigned to lead agent
      3. Execute task → creates session with source_task_id pointing to task
      4. POST /agent/tasks/current/subtask with source_session_id = session ID
      5. Subtask created with correct parent_task_id
    """
    headers = superuser_token_headers

    # ── Phase 1: Create agents, team, and connection ────────────────────────
    lead_agent = create_agent_via_api(client, headers, name="SessionResolve Lead")
    worker_agent = create_agent_via_api(client, headers, name="SessionResolve Worker")
    drain_tasks()

    team = create_team(client, headers, name="SessionResolve Team")
    team_id = team["id"]

    lead_node = create_node(client, headers, team_id, lead_agent["id"], is_lead=True)
    worker_node = create_node(client, headers, team_id, worker_agent["id"])

    create_connection(
        client, headers, team_id,
        source_node_id=lead_node["id"],
        target_node_id=worker_node["id"],
        enabled=True,
    )

    # ── Phase 2: Create team-scoped task ────────────────────────────────────
    parent = create_task(
        client, headers,
        original_message="Session-resolved subtask parent",
        team_id=team_id,
        selected_agent_id=lead_agent["id"],
    )
    parent_id = parent["id"]

    # ── Phase 3: Execute → creates session linked to task ───────────────────
    with patch(_AGENT_ENV_PATCH, StubAgentEnvConnector(response_text="Working")):
        exec_result = execute_task(client, headers, parent_id)
        drain_tasks()

    session_id = str(exec_result["session_id"])

    # ── Phase 4: POST /agent/tasks/current/subtask ──────────────────────────
    r = client.post(
        f"{_BASE}/agent/tasks/current/subtask",
        headers=headers,
        json={
            "title": "Delegated via current route",
            "assigned_to": worker_node["name"],
            "source_session_id": session_id,
        },
    )
    assert r.status_code == 200, f"Session-resolved subtask failed: {r.text}"
    result = r.json()
    assert result["success"] is True
    assert result["parent_task"] == parent["short_code"]

    # ── Phase 5: Verify subtask has correct parent_task_id ──────────────────
    subtask = get_task_by_code(client, headers, result["task"])
    assert subtask["parent_task_id"] == parent_id
    assert subtask["team_id"] == team_id


def test_session_resolved_subtask_survives_reexecution(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: "Session",
) -> None:
    """
    After task.session_id is overwritten (simulating re-execution), the OLD
    session can still create subtasks because resolution uses
    Session.source_task_id (immutable) instead of InputTask.session_id.

      1. Create team with lead + worker agents and connection
      2. Create team-scoped task, execute → session S1
      3. Overwrite task.session_id to a different UUID (simulating re-execution)
      4. POST /agent/tasks/current/subtask with source_session_id = S1
      5. Subtask still created with correct parent_task_id
    """
    headers = superuser_token_headers

    # ── Phase 1: Create agents, team, connection ────────────────────────────
    lead_agent = create_agent_via_api(client, headers, name="Reexec Lead")
    worker_agent = create_agent_via_api(client, headers, name="Reexec Worker")
    drain_tasks()

    team = create_team(client, headers, name="Reexec Team")
    team_id = team["id"]

    lead_node = create_node(client, headers, team_id, lead_agent["id"], is_lead=True)
    worker_node = create_node(client, headers, team_id, worker_agent["id"])

    create_connection(
        client, headers, team_id,
        source_node_id=lead_node["id"],
        target_node_id=worker_node["id"],
        enabled=True,
    )

    # ── Phase 2: Create and execute task → session S1 ───────────────────────
    parent = create_task(
        client, headers,
        original_message="Reexec subtask parent",
        team_id=team_id,
        selected_agent_id=lead_agent["id"],
    )
    parent_id = parent["id"]

    with patch(_AGENT_ENV_PATCH, StubAgentEnvConnector(response_text="First run")):
        exec_result = execute_task(client, headers, parent_id)
        drain_tasks()

    session_s1 = str(exec_result["session_id"])

    # ── Phase 3: Overwrite task.session_id directly (simulate re-execution) ─
    from app.models import InputTask as InputTaskModel
    task_obj = db.get(InputTaskModel, uuid.UUID(parent_id))
    assert task_obj is not None
    task_obj.session_id = None  # clear — simulates session_id being overwritten to a different session
    task_obj.status = "in_progress"  # keep it in a valid state
    db.add(task_obj)
    db.commit()
    db.refresh(task_obj)

    # Verify task.session_id is no longer S1
    parent_after = get_task(client, headers, parent_id)
    assert parent_after["session_id"] != session_s1

    # ── Phase 4: Old session S1 still resolves for subtask creation ─────────
    r = client.post(
        f"{_BASE}/agent/tasks/current/subtask",
        headers=headers,
        json={
            "title": "Subtask from old session",
            "assigned_to": worker_node["name"],
            "source_session_id": session_s1,
        },
    )
    assert r.status_code == 200, (
        f"Subtask creation via old session should succeed after session_id overwrite: {r.text}"
    )
    result = r.json()
    assert result["success"] is True
    assert result["parent_task"] == parent["short_code"]

    # ── Phase 5: Subtask has correct parent link ────────────────────────────
    subtask = get_task_by_code(client, headers, result["task"])
    assert subtask["parent_task_id"] == parent_id
