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

Scenarios tested here (user status route, POST /{id}/status):
  1. A user drives their own task new → in_progress → completed; history and
     system comments appear, attributed to the user
  2. Invalid transition → 400 naming the valid set
  3. A status outside the user's allowed set (refining / archived / new) → 400
  4. Another user's task → rejected, nothing written
  5. A task with a *live* session: the user's mid-flight write lands, then
     the session-driven recompute supersedes it
  6. The user route and the agent route refuse each other's tokens

Status history and system comments from the collaboration layer are tested
in test_task_agent_api.py where update_task_status() is called via the agent API.

Valid transitions (from InputTaskStatus.VALID_TRANSITIONS):
  new: refining, open, in_progress, cancelled, archived
  refining: new, open, in_progress
  open: in_progress, cancelled
  in_progress: completed, blocked, cancelled, error
  blocked: in_progress, cancelled
  completed: in_progress, archived
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
_AGENT_ENV_PATCH = "app.services.sessions.message_service.agent_env_connector"


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


# ===========================================================================
# User-scoped status writes — POST /tasks/{id}/status
#
# For work a user-authenticated client executes *outside* cinna (the desktop
# app running a task on a local agent), so the web view mirrors what is really
# happening. The agent route (/agent/tasks/{id}/status) is the container's and
# refuses a user token; this one is the user's and refuses an env token. The
# pair of tests at the bottom pins that split, because the whole separation
# rests on it.
# ===========================================================================


def test_user_status_write_full_lifecycle(
    client: TestClient,
    normal_user_token_headers: dict[str, str],
) -> None:
    """
    A user drives their own task through the statuses a desktop client reports:

      1. Create task — status 'new'
      2. POST /status {in_progress} — accepted, status changes
      3. POST /status {completed} — accepted
      4. Status history holds both transitions, attributed to the *user*
         (changed_by_user_id set, changed_by_agent_id null)
      5. The reason travels into the history row
      6. A status_change system comment exists per transition
      7. No session was started — the route records, it does not act
    """
    headers = normal_user_token_headers

    # ── Phase 1: Create ───────────────────────────────────────────────────
    task = create_task(client, headers, original_message="Desktop-executed task")
    task_id = task["id"]
    assert task["status"] == "new"

    r = client.get(f"{settings.API_V1_STR}/users/me", headers=headers)
    assert r.status_code == 200
    user_id = r.json()["id"]

    # ── Phase 2: new → in_progress ────────────────────────────────────────
    r = client.post(
        f"{_BASE}/{task_id}/status",
        headers=headers,
        json={"status": "in_progress", "reason": "Running on Desktop (folder agent: reviewer)"},
    )
    assert r.status_code == 200, f"User status write failed: {r.text}"
    assert r.json()["status"] == "in_progress"

    # ── Phase 3: in_progress → completed ──────────────────────────────────
    r = client.post(
        f"{_BASE}/{task_id}/status",
        headers=headers,
        json={"status": "completed", "reason": "Finished locally"},
    )
    assert r.status_code == 200, f"User status write failed: {r.text}"
    assert r.json()["status"] == "completed"
    assert get_task(client, headers, task_id)["status"] == "completed"

    # ── Phase 4 + 5: history is attributed to the user, with the reason ───
    detail = get_task_detail(client, headers, task_id)
    history = detail["status_history"]
    transitions = {(h["from_status"], h["to_status"]): h for h in history}

    assert ("new", "in_progress") in transitions, f"Missing transition in {history}"
    assert ("in_progress", "completed") in transitions, f"Missing transition in {history}"

    started = transitions[("new", "in_progress")]
    assert started["changed_by_user_id"] == user_id, (
        "A user's client reported this; attributing it to the system or an agent "
        f"would be a lie about a human's action. Got {started}"
    )
    assert started["changed_by_agent_id"] is None
    assert started["reason"] == "Running on Desktop (folder agent: reviewer)"

    finished = transitions[("in_progress", "completed")]
    assert finished["changed_by_user_id"] == user_id
    assert finished["reason"] == "Finished locally"

    # ── Phase 6: system comments appear in the feed ───────────────────────
    r = client.get(f"{_BASE}/{task_id}/comments/", headers=headers)
    assert r.status_code == 200
    status_comments = [c for c in r.json()["data"] if c["comment_type"] == "status_change"]
    assert len(status_comments) >= 2, (
        f"Expected a status_change comment per transition, got {len(status_comments)}"
    )

    # ── Phase 7: no session side effects ──────────────────────────────────
    r = client.get(f"{_BASE}/{task_id}/sessions", headers=headers)
    assert r.status_code == 200
    assert r.json()["data"] == [], (
        "POST /status must not start, resume or interrupt a session — it only "
        "records what a client reports about work happening elsewhere."
    )


def test_user_status_write_invalid_transition_400_names_valid_set(
    client: TestClient,
    normal_user_token_headers: dict[str, str],
) -> None:
    """
    An invalid transition is a loud 400 that names what *is* valid.

      1. Task driven to 'completed'
      2. POST /status {blocked} — completed must restart before blocking
      3. 400, and the detail names the valid set
      4. POST /status {in_progress} restarts the completed task, with history

    The desktop pins the same transition table locally, so a 400 here means the
    two copies drifted. It has to say which way.
    """
    headers = normal_user_token_headers
    task = create_task(client, headers, original_message="Invalid user transition test")
    task_id = task["id"]

    for target in ("in_progress", "completed"):
        r = client.post(f"{_BASE}/{task_id}/status", headers=headers, json={"status": target})
        assert r.status_code == 200, f"Setup transition to {target} failed: {r.text}"

    r = client.post(
        f"{_BASE}/{task_id}/status", headers=headers, json={"status": "blocked"}
    )
    assert r.status_code == 400, (
        f"Expected 400 for completed→blocked, got {r.status_code}: {r.text}"
    )
    detail = r.json()["detail"]
    assert "archived" in detail, (
        f"The 400 must name the valid set so a drifted client can see which way; got: {detail}"
    )
    assert get_task(client, headers, task_id)["status"] == "completed"

    restarted = client.post(
        f"{_BASE}/{task_id}/status", headers=headers,
        json={"status": "in_progress", "reason": "Run the completed task again"},
    )
    assert restarted.status_code == 200, restarted.text
    assert restarted.json()["status"] == "in_progress"
    history = get_task_detail(client, headers, task_id)["status_history"]
    assert any(row["from_status"] == "completed" and row["to_status"] == "in_progress" for row in history)


def test_user_status_write_rejects_statuses_outside_the_user_set(
    client: TestClient,
    normal_user_token_headers: dict[str, str],
) -> None:
    """
    Statuses this route does not own are refused, in the agent route's shape.

      - 'refining' belongs to the refine flow
      - 'archived' belongs to POST /{id}/archive, which owns archived_at
      - 'new' is the create state
      - a status that is not a status at all

    Each is a 400 naming the set the caller may use. Nothing is written.
    """
    headers = normal_user_token_headers
    task = create_task(client, headers, original_message="Disallowed user status test")
    task_id = task["id"]

    for bad in ("refining", "archived", "new", "not_a_status"):
        r = client.post(f"{_BASE}/{task_id}/status", headers=headers, json={"status": bad})
        assert r.status_code == 400, (
            f"Expected 400 for disallowed status {bad!r}, got {r.status_code}: {r.text}"
        )
        # Match the allowed-set message specifically, not just a status name.
        # 'new → refining' IS a valid transition, so if this route only had the
        # transition check the refining case would be a 200 — and asserting on
        # a bare "in_progress" would not have noticed, because in_progress also
        # appears in the *transition* error text.
        assert "User can only set status to" in r.json()["detail"], (
            f"Expected the allowed-set refusal, not a transition error; "
            f"got: {r.json()['detail']}"
        )

    # Nothing was written — the task never left 'new'
    assert get_task(client, headers, task_id)["status"] == "new"
    assert get_task_detail(client, headers, task_id)["status_history"] == []

    # ...and 'archived' is still reachable by its own route
    r = client.post(f"{_BASE}/{task_id}/archive", headers=headers)
    assert r.status_code == 200
    assert r.json()["status"] == "archived"


def test_user_status_write_rejected_for_non_owner(
    client: TestClient,
    normal_user_token_headers: dict[str, str],
) -> None:
    """
    User B cannot write the status of User A's task.

    Ownership runs through ``get_task_with_ownership_check``, exactly as every
    sibling route in input_tasks.py does — which raises PermissionDeniedError
    (400). Asserted as (400, 404) to match the sibling tests.
    """
    headers_a = normal_user_token_headers
    task = create_task(client, headers_a, original_message="Status ownership test")
    task_id = task["id"]

    user_b = create_random_user(client)
    headers_b = user_authentication_headers(
        client=client, email=user_b["email"], password=user_b["_password"]
    )

    r = client.post(
        f"{_BASE}/{task_id}/status", headers=headers_b, json={"status": "in_progress"}
    )
    assert r.status_code in (400, 404), (
        f"A non-owner must not reach another user's task; got {r.status_code}: {r.text}"
    )
    assert get_task(client, headers_a, task_id)["status"] == "new"

    # Unauthenticated is rejected too
    r = client.post(f"{_BASE}/{task_id}/status", json={"status": "in_progress"})
    assert r.status_code in (401, 403)


def test_session_driven_recompute_supersedes_a_live_user_write(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Where cinna is doing the work, the server's opinion wins — even over a
    write the user made while the session was live.

      1. Create an agent and a task
      2. Execute it on cinna. The session is created here; the stream itself is
         deferred to the background-task collector, so between this step and
         the drain the task has a *live, unfinished* session.
      3. Mid-flight, the user's client claims the task is blocked. The write
         lands: the task really is 'blocked' at this point, so this is not a
         no-op the rest of the test could mask.
      4. Drain — the stream completes and the session lifecycle recomputes the
         status from its sessions. The task is 'completed'.

    Note what step 4 gets away with: ``blocked → completed`` is NOT in
    VALID_TRANSITIONS['blocked']. The recompute goes through the bare
    ``update_status``, which does not consult the table, so the server
    overrides the client on a transition the client itself would be refused.
    That asymmetry is deliberate — the client's write is a *report* about work
    elsewhere, not a lock — and it is the whole content of plan rule 5.

    A desktop-executed task has no session at all, so nothing contends for it.
    """
    from unittest.mock import patch

    from tests.stubs.agent_env_stub import StubAgentEnvConnector
    from tests.utils.agent import create_agent_via_api
    from tests.utils.background_tasks import drain_tasks
    from tests.utils.input_task import execute_task, get_task_sessions

    headers = superuser_token_headers

    # ── Phase 1: agent + task ─────────────────────────────────────────────
    agent = create_agent_via_api(client, headers, name="Recompute Precedence Agent")
    drain_tasks()
    task = create_task(
        client, headers,
        original_message="Task cinna will actually run",
        selected_agent_id=agent["id"],
    )
    task_id = task["id"]

    stub = StubAgentEnvConnector(response_text="Done on the server.")
    with patch(_AGENT_ENV_PATCH, stub):
        # ── Phase 2: execute — session exists, stream not yet drained ─────
        execute_task(client, headers, task_id)
        assert len(get_task_sessions(client, headers, task_id)) >= 1, (
            "The session must exist before the user's write, or this test is "
            "not exercising the contended case at all."
        )

        # ── Phase 3: the client writes while the session is live ──────────
        r = client.post(
            f"{_BASE}/{task_id}/status",
            headers=headers,
            json={"status": "blocked", "reason": "Client says it is waiting on a human"},
        )
        assert r.status_code == 200, f"Mid-flight user write failed: {r.text}"
        assert get_task(client, headers, task_id)["status"] == "blocked", (
            "The user's write must actually land — otherwise phase 4 proves "
            "nothing about it being overridden."
        )

        # ── Phase 4: the stream finishes and the recompute has the last word
        drain_tasks()

    assert get_task(client, headers, task_id)["status"] == "completed", (
        "A task with sessions is one cinna is executing; the session-driven "
        "recompute must override what a client reported about it — including "
        "across a transition the client would itself be refused."
    )


def test_user_route_and_agent_route_do_not_accept_each_others_tokens(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """
    The user/container split holds in both directions.

      1. An agent_env token cannot reach POST /tasks/{id}/status
         (the user route resolves CurrentUser, and get_current_user rejects an
         aud-bearing env token)
      2. A user token cannot reach POST /agent/tasks/{id}/status
         (AgentEnvContextDep rejects a user token by design)

    Cheap, and it is the invariant the whole two-route design rests on.
    """
    from tests.utils.agent import create_agent_via_api

    headers = superuser_token_headers
    owner_id = _get_superuser_id(client, headers)

    agent = create_agent_via_api(client, headers, name="Token Split Agent")
    agent_id = agent["id"]
    _, env_headers = create_env_with_token(db, agent_id=agent_id, owner_id=owner_id)

    task = create_task(
        client, headers,
        original_message="Token split test",
        selected_agent_id=agent_id,
    )
    task_id = task["id"]

    # ── Direction 1: env token → user route ───────────────────────────────
    r = client.post(
        f"{_BASE}/{task_id}/status",
        headers=env_headers,
        json={"status": "in_progress"},
    )
    assert r.status_code in (401, 403), (
        f"An agent_env token must not resolve to a full user on the user route; "
        f"got {r.status_code}: {r.text}"
    )

    # ── Direction 2: user token → agent route ─────────────────────────────
    r = client.post(
        f"{settings.API_V1_STR}/agent/tasks/{task_id}/status",
        headers=headers,
        json={"status": "completed"},
    )
    assert r.status_code in (401, 403), (
        f"A user token must not reach the container-scoped agent route; "
        f"got {r.status_code}: {r.text}"
    )

    # Neither attempt moved the task
    assert get_task(client, headers, task_id)["status"] == "new"


def test_status_change_helper_still_emits_activity_after_delegation_refactor(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """
    Regression guard for the delegation contract's shared refactor.

    DelegationService now drives its status changes through the same
    ``InputTaskService._apply_status_transition`` / ``_emit_status_changed``
    helpers as the plain status routes (see
    tests/api/input_tasks/test_delegation_contract.py). This is a canary that
    a change made for delegation's sake didn't alter the TASK_STATUS_CHANGED
    payload shape the plain (non-delegation) routes rely on: the Activities
    feed is built entirely from that event, so a broken payload key shows up
    here as a missing activity rather than as an assertion on the event
    itself.

      1. Owner route (POST /{id}/status, attributed to the user):
         new → in_progress → blocked. History + system comment + a
         task_blocked Activity all appear; GET /tasks/ shows 'blocked'.
      2. Agent route (POST /agent/tasks/{id}/status, attributed to the agent):
         a second task, in_progress → blocked the same way, confirming the
         event payload works for agent attribution too.
    """
    from tests.utils.background_tasks import drain_tasks

    headers = superuser_token_headers
    owner_id = _get_superuser_id(client, headers)

    def _activities(activity_type: str) -> list[dict]:
        r = client.get(f"{settings.API_V1_STR}/activities/", headers=headers)
        assert r.status_code == 200, r.text
        return [a for a in r.json()["data"] if a["activity_type"] == activity_type]

    # ── Phase 1: owner-attributed status change ─────────────────────────
    task = create_task(client, headers, original_message="Owner-route event canary")
    task_id = task["id"]

    r = client.post(f"{_BASE}/{task_id}/status", headers=headers, json={"status": "in_progress"})
    assert r.status_code == 200, r.text
    drain_tasks()
    r = client.post(f"{_BASE}/{task_id}/status", headers=headers,
                     json={"status": "blocked", "reason": "Waiting on a dependency"})
    assert r.status_code == 200, r.text
    drain_tasks()
    assert get_task(client, headers, task_id)["status"] == "blocked"

    detail = get_task_detail(client, headers, task_id)
    blocked_history = [h for h in detail["status_history"] if h["to_status"] == "blocked"]
    assert len(blocked_history) == 1
    assert blocked_history[0]["changed_by_user_id"] == owner_id
    assert blocked_history[0]["changed_by_agent_id"] is None

    r = client.get(f"{_BASE}/{task_id}/comments/", headers=headers)
    assert r.status_code == 200
    assert any(c["comment_type"] == "status_change" and "blocked" in c["content"]
               for c in r.json()["data"])

    owner_blocked_activities = [
        a for a in _activities("task_blocked") if a["input_task_id"] == task_id
    ]
    assert len(owner_blocked_activities) >= 1, (
        "TASK_STATUS_CHANGED from the owner status route no longer produces a "
        "task_blocked Activity — the shared helper's event payload likely drifted"
    )

    # ── Phase 2: agent-attributed status change ─────────────────────────
    from tests.utils.agent import create_agent_via_api

    agent = create_agent_via_api(client, headers, name="Status event canary agent")
    drain_tasks()
    agent_id = agent["id"]
    _, env_headers = create_env_with_token(db, agent_id=agent_id, owner_id=owner_id)

    agent_task = create_task(
        client, headers, original_message="Agent-route event canary", selected_agent_id=agent_id,
    )
    agent_task_id = agent_task["id"]
    r = client.post(f"{_BASE}/{agent_task_id}/status", headers=headers, json={"status": "in_progress"})
    assert r.status_code == 200, r.text
    drain_tasks()

    r = client.post(
        f"{settings.API_V1_STR}/agent/tasks/{agent_task_id}/status",
        headers=env_headers,
        json={"status": "blocked", "reason": "Needs external input"},
    )
    assert r.status_code == 200, r.text
    drain_tasks()
    assert get_task(client, headers, agent_task_id)["status"] == "blocked"

    detail = get_task_detail(client, headers, agent_task_id)
    blocked_history = [h for h in detail["status_history"] if h["to_status"] == "blocked"]
    assert len(blocked_history) == 1
    assert blocked_history[0]["changed_by_agent_id"] == agent_id
    assert blocked_history[0]["changed_by_user_id"] is None

    agent_blocked_activities = [
        a for a in _activities("task_blocked") if a["input_task_id"] == agent_task_id
    ]
    assert len(agent_blocked_activities) >= 1, (
        "TASK_STATUS_CHANGED from the agent status route no longer produces a "
        "task_blocked Activity — the shared helper's event payload likely drifted"
    )
