"""
Tests for task status transitions.

Two status update mechanisms exist:

1. Legacy update_status() — used by the archive endpoint and session lifecycle.
   This updates task.status directly. Does NOT create TaskStatusHistory records
   or system comments.

2. New update_task_status() — used by the agent status API.
   Validates transitions, creates TaskStatusHistory, posts a system comment,
   and emits TASK_STATUS_CHANGED. Transition rules enforced.

The archive endpoint (POST /{id}/archive) uses the new validating method
(``update_task_status()``). The agent status API (POST /agent/tasks/{id}/status)
uses it too. Status history and system comments are only created via this method.

Scenarios tested here (archive endpoint):
  1. Archive a 'new' task — succeeds (new→archived is a valid transition)
  2. Archive a task that's already archived — idempotent 200 (same-status no-op)
  3. Non-existent task → 404
  4. Other user's task → 404

Status history and system comments from the collaboration layer are tested
in test_task_agent_api.py where update_task_status() is called via the agent API.

Valid transitions (from InputTaskStatus.VALID_TRANSITIONS):
  new: refining, open, in_progress, cancelled, archived
  refining: new, open, in_progress
  open: in_progress, cancelled
  in_progress: completed, blocked, cancelled, error
  blocked: in_progress, cancelled
  completed: archived
  error: new, in_progress, archived
  cancelled: archived
  archived: (empty — terminal state)
"""
import uuid

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.core.config import settings
from tests.utils.agent_env import create_env_with_token
from tests.utils.input_task import (
    create_task,
    get_task,
    get_task_detail,
)
from tests.utils.user import create_random_user, user_authentication_headers

_BASE = f"{settings.API_V1_STR}/tasks"


def _get_superuser_id(client: TestClient, headers: dict) -> str:
    """Return the superuser's user ID as a string."""
    r = client.get(f"{settings.API_V1_STR}/users/me", headers=headers)
    assert r.status_code == 200
    return r.json()["id"]


def test_archive_new_task_succeeds(
    client: TestClient,
    normal_user_token_headers: dict[str, str],
) -> None:
    """
    Archive a task in 'new' status:
      1. Create task — status is 'new'
      2. POST /tasks/{id}/archive — new → archived is a valid transition
      3. Response has status 'archived'
      4. GET task confirms status is 'archived'
    """
    headers = normal_user_token_headers

    task = create_task(client, headers, original_message="Archive new task test")
    task_id = task["id"]
    assert task["status"] == "new"

    r = client.post(f"{_BASE}/{task_id}/archive", headers=headers)
    assert r.status_code == 200, f"Archive failed: {r.text}"
    archived = r.json()
    assert archived["status"] == "archived"
    assert archived["id"] == task_id

    # Confirm via GET
    fetched = get_task(client, headers, task_id)
    assert fetched["status"] == "archived"


def test_archive_idempotency_and_terminal_state(
    client: TestClient,
    normal_user_token_headers: dict[str, str],
) -> None:
    """
    Archive is idempotent for an already-archived task (returns 200, no change):

      1. Archive task → 200, status 'archived'
      2. Archive again → 200, status still 'archived'

    Semantics decision (Phase 3c): the archive endpoint routes through the
    validating ``update_task_status()`` (NOT the legacy ``update_status()``).
    That method treats ``new_status == from_status`` as an explicit idempotent
    no-op — it short-circuits before the transition-validity check and returns
    the unchanged task. So archive-of-archived is a deterministic 200, not a
    400: re-archiving a terminal task is a safe no-op rather than an error.
    ``archived → archived`` is therefore pinned to ONE expected status code (200).
    """
    headers = normal_user_token_headers

    task = create_task(client, headers, original_message="Archive terminal state test")
    task_id = task["id"]

    # ── Phase 1: Archive once ─────────────────────────────────────────────
    r = client.post(f"{_BASE}/{task_id}/archive", headers=headers)
    assert r.status_code == 200, f"First archive failed: {r.text}"
    assert r.json()["status"] == "archived"

    # ── Phase 2: Archive again — idempotent no-op, still 200 / archived ────
    r = client.post(f"{_BASE}/{task_id}/archive", headers=headers)
    assert r.status_code == 200, (
        f"Re-archiving an archived task must be an idempotent 200, got "
        f"{r.status_code}: {r.text}"
    )
    assert r.json()["status"] == "archived"

    # ── Phase 3: Confirm via GET — status unchanged ───────────────────────
    assert get_task(client, headers, task_id)["status"] == "archived"


def test_archive_non_existent_task_404(
    client: TestClient,
    normal_user_token_headers: dict[str, str],
) -> None:
    """Archive a non-existent task UUID returns 404."""
    headers = normal_user_token_headers
    ghost = str(uuid.uuid4())
    r = client.post(f"{_BASE}/{ghost}/archive", headers=headers)
    assert r.status_code == 404


def test_archive_other_users_task_404(
    client: TestClient,
    normal_user_token_headers: dict[str, str],
) -> None:
    """User B cannot archive User A's task."""
    headers_a = normal_user_token_headers
    task = create_task(client, headers_a, original_message="Archive ownership test task")

    user_b = create_random_user(client)
    headers_b = user_authentication_headers(
        client=client, email=user_b["email"], password=user_b["_password"]
    )

    r = client.post(f"{_BASE}/{task['id']}/archive", headers=headers_b)
    assert r.status_code in (400, 404)  # PermissionDeniedError → 400


def test_archive_unauthenticated_rejected(
    client: TestClient,
    normal_user_token_headers: dict[str, str],
) -> None:
    """Unauthenticated archive request is rejected."""
    headers = normal_user_token_headers
    task = create_task(client, headers, original_message="Auth archive test task")

    r = client.post(f"{_BASE}/{task['id']}/archive")
    assert r.status_code in (401, 403)


def test_status_history_created_by_agent_status_api(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """
    The collaboration layer (agent status API) creates status history entries
    and system comments.

    This test verifies the full collaboration status update path:
      1. Create task with agent assigned
      2. Create scoped env token for the agent
      3. Agent updates status to cancelled via /agent/tasks/{id}/status (env token)
      4. Status history appears in task detail
      5. System comment of type status_change appears in comments
      6. History entry has correct from/to status and task_id
    """
    from tests.utils.agent import create_agent_via_api
    headers = superuser_token_headers
    owner_id = _get_superuser_id(client, headers)

    agent = create_agent_via_api(client, headers, name="Status History Test Agent")
    agent_id = agent["id"]

    _, env_headers = create_env_with_token(db, agent_id=agent_id, owner_id=owner_id)

    task = create_task(
        client, headers,
        original_message="Status history via agent API test",
        selected_agent_id=agent_id,
    )
    task_id = task["id"]
    original_status = task["status"]  # "new"

    # Agent updates status: new → cancelled (using scoped env token)
    r = client.post(
        f"{settings.API_V1_STR}/agent/tasks/{task_id}/status",
        headers=env_headers,
        json={"status": "cancelled", "reason": "Test cancellation reason"},
    )
    assert r.status_code == 200, f"Agent status update failed: {r.text}"
    result = r.json()
    assert result["success"] is True

    # Verify task status changed
    fetched = get_task(client, headers, task_id)
    assert fetched["status"] == "cancelled"

    # Status history entry appears in detail
    detail = get_task_detail(client, headers, task_id)
    assert len(detail["status_history"]) >= 1

    # Find the new→cancelled transition
    matching_history = [
        h for h in detail["status_history"]
        if h["from_status"] == original_status and h["to_status"] == "cancelled"
    ]
    assert len(matching_history) >= 1
    hist_entry = matching_history[0]
    assert hist_entry["task_id"] == task_id
    assert hist_entry["id"] is not None
    assert hist_entry["created_at"] is not None

    # System comment of type status_change appears
    r = client.get(f"{_BASE}/{task_id}/comments/", headers=headers)
    assert r.status_code == 200
    status_comments = [
        c for c in r.json()["data"]
        if c["comment_type"] == "status_change"
    ]
    assert len(status_comments) >= 1
    sc = status_comments[0]
    assert sc["task_id"] == task_id
    # System comment has no user author (agent attribution used in content)
    assert sc["author_user_id"] is None


def test_invalid_transition_via_agent_api_returns_400(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """
    Invalid status transitions return 400 via the agent status API:
      1. new → completed is not in VALID_TRANSITIONS["new"]
         but completed is not in allowed_agent_statuses either,
         so it would return 400 (disallowed agent target)
      2. cancelled → completed: cancelled is not in allowed_agent_statuses from cancelled
         Actually: cancelled → completed is invalid in VALID_TRANSITIONS["cancelled"]
         AND completed is in allowed_agent_statuses, so it hits the transition validator.
    """
    from tests.utils.agent import create_agent_via_api
    headers = superuser_token_headers
    owner_id = _get_superuser_id(client, headers)

    agent = create_agent_via_api(client, headers, name="Invalid Transition Test Agent")
    agent_id = agent["id"]

    _, env_headers = create_env_with_token(db, agent_id=agent_id, owner_id=owner_id)

    # Create a task, cancel it (new → cancelled)
    task = create_task(
        client, headers,
        original_message="Invalid transition test task",
        selected_agent_id=agent_id,
    )
    task_id = task["id"]

    r = client.post(
        f"{settings.API_V1_STR}/agent/tasks/{task_id}/status",
        headers=env_headers,
        json={"status": "cancelled"},
    )
    assert r.status_code == 200

    # Now try cancelled → completed (invalid transition)
    r = client.post(
        f"{settings.API_V1_STR}/agent/tasks/{task_id}/status",
        headers=env_headers,
        json={"status": "completed"},
    )
    assert r.status_code == 400, (
        f"Expected 400 for cancelled→completed, got {r.status_code}: {r.text}"
    )
