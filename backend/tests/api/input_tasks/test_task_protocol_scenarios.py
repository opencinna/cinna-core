"""
Cinna Task Protocol (task_protocol_api.py) — scenarios beyond the smoke file.

test_task_protocol_api.py covers the happy round trip, `agent:<uuid>`
delegation with `id` idempotency, the done/cancelled status path and the
description cap. This file fills the gaps:

  1. Team-scoped targets (node:<uuid>, never itself) and a team subtask
     (parent + assignee) with the topology check; own subtask without assignee
  2. Delegation by bare name, unknown and self targets, notes for ignored
     fields, `id` scoped to its conversation, the combined title+description cap
  3. A bare name shared by two agents is refused as ambiguous
  4. tasks_update edits, single-step moves, refusals (blocked task, foreign
     task, illegal move, empty update) and session scoping
  5. tasks_comment stored on the task; another user's task is not found
  6. tasks_report status mapping (in_progress/blocked/done/failed), resume
     step, idempotent repeat, late report refused, task not assigned refused
  7. Delegated task: executor's default audience is the requester, blocked
     question answered with tasks_reply, follow-up into the child session,
     result packet text (first line, truncation pointer, question line)
  8. tasks_list relation and category filters, tasks_get scope
  9. Subtask `id` idempotency: one subtask, one run, external_ref at creation
 10. Report artifacts: links kept, paths recorded as files with a note
 11. tasks_reply follow-up retried in the same turn is sent once
 12. An inactive agent is not a target, by uuid or by name
 13. tasks_comment needs a relation to the task (assigned/created/delegated)
 14. A protocol-1 executor's delegated turn without a report ends blocked
 15. An own task counts as a ready task in the session context and takes tasks_report
 16. Chain depth: 1 -> 2 allowed, a third hop refused (also for chains
     built by the older routes, whose rows carry no chain_depth)
 17. 20 creates per conversation; a repeated `id` is still answered at the cap
 18. A non-lead team member's own subtask lands in its own queue

Like the other input_tasks protocol files, a few setup steps and the result
packet renderer touch the test DB / service directly: there is no API that
mints an env token, and the packet is only otherwise visible after a full
second executor turn.
"""
from unittest.mock import patch
from uuid import UUID

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.core.config import settings
from tests.stubs.agent_env_stub import StubAgentEnvConnector
from tests.utils.agent import create_agent_via_api, update_agent
from tests.utils.agent_env import create_env_with_token
from tests.utils.agentic_team import create_connection, create_node, create_team
from tests.utils.background_tasks import drain_tasks
from tests.utils.input_task import (
    create_task,
    execute_task,
    get_task,
    get_task_by_code,
    get_task_detail,
    get_task_sessions,
    list_comments,
    set_task_status,
)
from tests.utils.message import list_messages, send_message

_PROTOCOL = f"{settings.API_V1_STR}/agent/tasks/protocol"
_AGENT_ENV_PATCH = "app.services.sessions.message_service.agent_env_connector"


# ── helpers ───────────────────────────────────────────────────────────────

def _owner_id(client: TestClient, headers: dict[str, str]) -> str:
    r = client.get(f"{settings.API_V1_STR}/users/me", headers=headers)
    assert r.status_code == 200
    return r.json()["id"]


def _ensure_in_progress(client: TestClient, headers: dict[str, str], task_id: str) -> None:
    """A stub turn ends at once and settles the task; put it back under way."""
    if get_task(client, headers, task_id)["status"] != "in_progress":
        set_task_status(client, headers, task_id, "in_progress")


def _running_session(
    client: TestClient, headers: dict[str, str], agent_id: str, **task_fields,
) -> tuple[dict, str]:
    """A task assigned to the agent, executed, so a session is working on it."""
    task = create_task(
        client, headers, original_message="Protocol host task", selected_agent_id=agent_id,
        **task_fields,
    )
    with patch(_AGENT_ENV_PATCH, StubAgentEnvConnector(response_text="Working")):
        result = execute_task(client, headers, task["id"])
        drain_tasks()
    _ensure_in_progress(client, headers, task["id"])
    return task, str(result["session_id"])


def _post(client: TestClient, env_headers: dict, verb: str, session_id: str, **fields):
    return client.post(f"{_PROTOCOL}/{verb}", headers=env_headers,
                       json={"source_session_id": session_id, **fields})


def _get(client: TestClient, env_headers: dict, verb: str, session_id: str, **params):
    return client.get(f"{_PROTOCOL}/{verb}", headers=env_headers,
                      params={"source_session_id": session_id, **params})


def _create_streaming(client: TestClient, env_headers: dict, session_id: str, **fields):
    """tasks_create whose delegation executes at once: stub the child's turn."""
    with patch(_AGENT_ENV_PATCH, StubAgentEnvConnector(response_text="On it")):
        r = _post(client, env_headers, "create", session_id, **fields)
        drain_tasks()
    return r


def _set_env_version(client: TestClient, headers: dict, db: Session, agent_id: str,
                     version: int) -> None:
    """Set the agent's environment task protocol version (no public API does).

    Set explicitly in every test that depends on it: a created environment
    is at 1 only when the kit's protocol schema landed in its core, which the
    minimal test template does not carry.
    """
    from app.models.environments.environment import AgentEnvironment

    r = client.get(f"{settings.API_V1_STR}/agents/{agent_id}", headers=headers)
    assert r.status_code == 200, r.text
    environment = db.get(AgentEnvironment, UUID(r.json()["active_environment_id"]))
    environment.task_protocol_version = version
    db.add(environment)
    db.commit()


def _own(client: TestClient, env_headers: dict, session_id: str, title: str, **fields) -> dict:
    r = _post(client, env_headers, "create", session_id, title=title,
              description=f"{title} description", **fields)
    assert r.status_code == 200, r.text
    return r.json()


def _refs(response) -> set[str]:
    assert response.status_code == 200, response.text
    return {t["ref"] for t in response.json()["tasks"]}


# ── 1. Team scope: targets and subtasks ──────────────────────────────────

def test_team_targets_and_subtask_topology(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session,
) -> None:
    """
      1. Team: lead -> worker connected, stranger in the team without a
         connection; outsider not in the team
      2. The lead's session on a team task lists node targets, never itself
      3. parent + assignee agent:/node: of the connected worker -> subtask
      4. Same `id` again -> the same subtask
      5. Stranger refused by topology, outsider refused as not a team member
      6. parent without assignee -> subtask assigned to the lead itself
      7. A parent that is no task of the caller's is refused
    """
    headers = superuser_token_headers
    owner_id = _owner_id(client, headers)
    lead = create_agent_via_api(client, headers, name="Team Lead CTP")
    worker = create_agent_via_api(client, headers, name="Team Worker CTP")
    stranger = create_agent_via_api(client, headers, name="Team Stranger CTP")
    outsider = create_agent_via_api(client, headers, name="Outsider CTP")
    drain_tasks()
    team = create_team(client, headers, name="CTP Team")
    lead_node = create_node(client, headers, team["id"], lead["id"], is_lead=True)
    worker_node = create_node(client, headers, team["id"], worker["id"])
    stranger_node = create_node(client, headers, team["id"], stranger["id"])
    create_connection(client, headers, team["id"], source_node_id=lead_node["id"],
                      target_node_id=worker_node["id"], enabled=True)

    host, session_id = _running_session(
        client, headers, lead["id"], team_id=team["id"], assigned_node_id=lead_node["id"],
    )
    _, env_headers = create_env_with_token(db, agent_id=lead["id"], owner_id=owner_id)

    # ── Phase 2: team targets ──────────────────────────────────────────
    r = _get(client, env_headers, "targets", session_id)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["protocol_version"] == 1
    targets = {t["target"]: t for t in body["targets"]}
    assert set(targets) == {f"node:{worker_node['id']}", f"node:{stranger_node['id']}"}
    entry = targets[f"node:{worker_node['id']}"]
    assert entry["name"] == worker_node["name"]
    assert entry["kind"] == "cinna" and entry["location"] == "remote"
    assert entry["protocol_version"] == 1 and entry["requires_approval"] is False
    assert entry["capabilities"] == {
        "structured_report": True, "reply": True, "priority": True,
        "custom_fields": False, "attachments": True,
    }

    # ── Phase 3: delegated subtask to the connected worker ─────────────
    r = _create_streaming(client, env_headers, session_id, title="Sub A", description="Part A",
                          parent=host["short_code"], assignee=f"agent:{worker['id']}",
                          id="sub-a-0001")
    assert r.status_code == 200, r.text
    sub_a = r.json()
    assert sub_a["created"] is True and sub_a["parent"] == host["short_code"]
    assert sub_a["assignee"] == worker["name"] and sub_a["relation"] == "delegated"
    sub_a_task = get_task_by_code(client, headers, sub_a["ref"])
    assert sub_a_task["parent_task_id"] == host["id"]
    assert sub_a_task["assigned_node_id"] == worker_node["id"]

    r = _create_streaming(client, env_headers, session_id, title="Sub B", description="Part B",
                          parent=host["short_code"], assignee=f"node:{worker_node['id']}")
    assert r.status_code == 200, r.text
    assert r.json()["assignee"] == worker["name"] and r.json()["ref"] != sub_a["ref"]

    # ── Phase 4: same id -> same subtask, nothing new ──────────────────
    r = _post(client, env_headers, "create", session_id, title="Sub A", description="Part A",
              parent=host["short_code"], assignee=f"agent:{worker['id']}", id="sub-a-0001")
    assert r.status_code == 200, r.text
    assert r.json()["created"] is False and r.json()["ref"] == sub_a["ref"]

    # ── Phase 5: topology and membership refusals ──────────────────────
    r = _post(client, env_headers, "create", session_id, title="Sub C", description="Part C",
              parent=host["short_code"], assignee=f"agent:{stranger['id']}")
    assert r.status_code == 400, r.text
    assert "topology" in r.json()["detail"]
    r = _post(client, env_headers, "create", session_id, title="Sub D", description="Part D",
              parent=host["short_code"], assignee=f"agent:{outsider['id']}")
    assert r.status_code == 400, r.text
    assert "team" in r.json()["detail"]

    # ── Phase 6: own subtask without assignee ──────────────────────────
    r = _post(client, env_headers, "create", session_id, title="Own sub", description="Mine",
              parent=host["short_code"])
    assert r.status_code == 200, r.text
    own_sub = r.json()
    # A subtask with no assignee lands on the creating node: the lead's queue.
    assert own_sub["parent"] == host["short_code"] and own_sub["category"] == "backlog"
    assert own_sub["assignee"] == lead["name"] and own_sub["relation"] == "assigned"

    # ── Phase 7: a parent that is not the caller's ─────────────────────
    foreign = create_task(client, headers, original_message="Not yours",
                          selected_agent_id=outsider["id"])
    r = _post(client, env_headers, "create", session_id, title="Sub E", description="Part E",
              parent=foreign["short_code"])
    assert r.status_code == 400, r.text
    assert "parent" in r.json()["detail"]


# ── 2. Delegation target resolution and create limits ───────────────────

def test_create_target_resolution_notes_and_limits(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session,
) -> None:
    """
      1. Bare name delegates; execution/category on a delegation become notes
      2. Unknown bare name, self (by uuid and by name) refused
      3. Own task without category is backlog/new
      4. `id` is scoped to its conversation: another session gets a new task
      5. title + description over 10000 together refused with the limit
      6. Malformed `id`, unknown field and session of another agent refused
    """
    headers = superuser_token_headers
    owner_id = _owner_id(client, headers)
    agent = create_agent_via_api(client, headers, name="CTP Requester Names")
    peer = create_agent_via_api(client, headers, name="CTP Executor Names")
    other = create_agent_via_api(client, headers, name="CTP Bystander Names")
    drain_tasks()
    _, session_id = _running_session(client, headers, agent["id"])
    _, env_headers = create_env_with_token(db, agent_id=agent["id"], owner_id=owner_id)

    # ── Phase 1: bare name ────────────────────────────────────────────
    r = _create_streaming(client, env_headers, session_id, title="By name", description="Do",
                          assignee=peer["name"], execution="auto", category="ready",
                          id="by-name-0001")
    assert r.status_code == 200, r.text
    by_name = r.json()
    assert by_name["assignee"] == peer["name"] and by_name["relation"] == "delegated"
    assert "End your turn" in by_name["message"]
    notes = " ".join(by_name["notes"])
    assert "`execution`" in notes and "`category`" in notes
    child = get_task_by_code(client, headers, by_name["ref"])
    assert child["selected_agent_id"] == peer["id"]
    assert child["source_session_id"] == session_id

    # ── Phase 2: unknown and self targets ──────────────────────────────
    for assignee in ("Nobody By This Name", f"agent:{UUID(int=0)}", "node:not-a-uuid"):
        r = _post(client, env_headers, "create", session_id, title="X", description="Y",
                  assignee=assignee)
        assert r.status_code == 400, (assignee, r.text)
        assert "tasks_targets" in r.json()["detail"]
    for assignee in (f"agent:{agent['id']}", agent["name"]):
        r = _post(client, env_headers, "create", session_id, title="X", description="Y",
                  assignee=assignee)
        assert r.status_code == 400, (assignee, r.text)
        assert "yourself" in r.json()["detail"]

    # ── Phase 3: own task: assigned to the caller, backlog, not started ─
    own = _own(client, env_headers, session_id, "Plain own")
    assert own["category"] == "backlog" and own["status"] == "new"
    assert own["assignee"] == agent["name"] and own["relation"] == "assigned"
    assert "notes" not in own and "your queue" in own["message"]
    own_task = get_task_by_code(client, headers, own["ref"])
    assert own_task["selected_agent_id"] == agent["id"]
    assert own_task["auto_execute"] is False and own_task["session_id"] is None
    assert get_task_sessions(client, headers, own_task["id"]) == []

    # ── Phase 4: id scoped to the conversation ─────────────────────────
    _, second_session = _running_session(client, headers, agent["id"])
    r = _create_streaming(client, env_headers, second_session, title="By name", description="Do",
                          assignee=peer["name"], id="by-name-0001")
    assert r.status_code == 200, r.text
    assert r.json()["created"] is True and r.json()["ref"] != by_name["ref"]

    # ── Phase 5: combined text cap ─────────────────────────────────────
    r = _post(client, env_headers, "create", session_id, title="T", description="d" * 9997)
    assert r.status_code == 200, r.text  # 1 + 9997 + 2 == 10000 fits
    r = _post(client, env_headers, "create", session_id, title="TT", description="d" * 9997)
    assert r.status_code == 400, r.text
    assert "10001" in r.json()["detail"] and "10000" in r.json()["detail"]

    # ── Phase 6: request validation and session scope ──────────────────
    r = _post(client, env_headers, "create", session_id, title="X", description="Y",
              assignee=peer["name"], id="Bad Id!")
    assert r.status_code == 422
    r = _post(client, env_headers, "create", session_id, title="X", description="Y", owner="me")
    assert r.status_code == 422
    r = _post(client, env_headers, "create", session_id, title="X", description="Y",
              category="draft")
    assert r.status_code == 400 and "draft" in r.json()["detail"]
    _, other_env_headers = create_env_with_token(db, agent_id=other["id"], owner_id=owner_id)
    r = _post(client, other_env_headers, "create", session_id, title="X", description="Y")
    assert r.status_code == 400, r.text
    assert "does not belong to this environment" in r.json()["detail"]
    r = _get(client, env_headers, "targets", str(UUID(int=1)))
    assert r.status_code == 400 and "Session not found" in r.json()["detail"]


# ── 3. Ambiguous bare name ───────────────────────────────────────────────

def test_create_refuses_bare_name_shared_by_two_agents(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session,
) -> None:
    """Two agents named alike: a bare name cannot say which, so it is refused
    and the caller is pointed at tasks_targets for an exact `target`."""
    headers = superuser_token_headers
    owner_id = _owner_id(client, headers)
    agent = create_agent_via_api(client, headers, name="CTP Requester Twins")
    create_agent_via_api(client, headers, name="CTP Twin")
    create_agent_via_api(client, headers, name="CTP Twin")
    drain_tasks()
    _, session_id = _running_session(client, headers, agent["id"])
    _, env_headers = create_env_with_token(db, agent_id=agent["id"], owner_id=owner_id)

    r = _create_streaming(client, env_headers, session_id, title="Which twin",
                          description="Either", assignee="CTP Twin")
    assert r.status_code == 400, r.text
    assert "tasks_targets" in r.json()["detail"]


# ── 4. tasks_update ──────────────────────────────────────────────────────

def test_update_edits_moves_and_refusals(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session,
) -> None:
    """
      1. Edit title/description/priority of an own backlog task; tasks_get shows them
      2. backlog -> ready is one step; ready -> backlog is not a legal move
      3. Empty update and blank description refused
      4. A blocked task (open question) is refused with the tasks_report pointer
      5. A task neither assigned to nor created by the caller is refused
      6. An unknown ref is not found
    """
    headers = superuser_token_headers
    owner_id = _owner_id(client, headers)
    agent = create_agent_via_api(client, headers, name="CTP Editor")
    peer = create_agent_via_api(client, headers, name="CTP Editor Peer")
    drain_tasks()
    host, session_id = _running_session(client, headers, agent["id"])
    _, env_headers = create_env_with_token(db, agent_id=agent["id"], owner_id=owner_id)
    own = _own(client, env_headers, session_id, "Editable")

    # ── Phase 1: edits ─────────────────────────────────────────────────
    r = _post(client, env_headers, "update", session_id, ref=own["ref"], title="Edited",
              description="New description", priority="urgent")
    assert r.status_code == 200, r.text
    assert r.json()["title"] == "Edited" and r.json()["priority"] == "urgent"
    r = _get(client, env_headers, "get", session_id, ref=own["ref"])
    assert r.status_code == 200, r.text
    assert r.json()["title"] == "Edited" and r.json()["priority"] == "urgent"
    task = get_task_by_code(client, headers, own["ref"])
    assert task["current_description"] == "New description"

    # ── Phase 2: moves ─────────────────────────────────────────────────
    r = _post(client, env_headers, "update", session_id, ref=own["ref"], category="ready")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "open" and r.json()["category"] == "ready"
    r = _post(client, env_headers, "update", session_id, ref=own["ref"], category="backlog")
    assert r.status_code == 400, r.text
    assert "Cannot transition task from 'open' to 'new'" in r.json()["detail"]
    # Moving to where it already is records nothing and succeeds.
    r = _post(client, env_headers, "update", session_id, ref=own["ref"], category="ready")
    assert r.status_code == 200 and r.json()["status"] == "open"

    # ── Phase 3: nothing to update ─────────────────────────────────────
    r = _post(client, env_headers, "update", session_id, ref=own["ref"])
    assert r.status_code == 400 and "Nothing to update" in r.json()["detail"]
    r = _post(client, env_headers, "update", session_id, ref=own["ref"], description="   ")
    assert r.status_code == 400 and "empty" in r.json()["detail"]

    # ── Phase 4: a blocked task belongs to whoever must answer ─────────
    set_task_status(client, headers, host["id"], "blocked")
    r = _post(client, env_headers, "update", session_id, ref=host["short_code"], title="Mine now")
    assert r.status_code == 409, r.text
    assert "blocked" in r.json()["detail"] and "tasks_report" in r.json()["detail"]
    assert get_task(client, headers, host["id"])["title"] != "Mine now"

    # ── Phase 5: a task that is not the caller's ───────────────────────
    foreign = create_task(client, headers, original_message="Peer's own work",
                          selected_agent_id=peer["id"])
    r = _post(client, env_headers, "update", session_id, ref=foreign["short_code"],
              title="Hijacked")
    assert r.status_code == 400, r.text
    assert get_task(client, headers, foreign["id"])["title"] != "Hijacked"

    # ── Phase 6: unknown ref ───────────────────────────────────────────
    r = _post(client, env_headers, "update", session_id, ref="NOPE-99999", title="X")
    assert r.status_code == 404, r.text


# ── 5. tasks_comment ─────────────────────────────────────────────────────

def test_comment_stored_and_other_users_task_not_found(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    normal_user_token_headers: dict[str, str],
    db: Session,
) -> None:
    headers = superuser_token_headers
    owner_id = _owner_id(client, headers)
    agent = create_agent_via_api(client, headers, name="CTP Commenter")
    drain_tasks()
    _, session_id = _running_session(client, headers, agent["id"])
    _, env_headers = create_env_with_token(db, agent_id=agent["id"], owner_id=owner_id)
    own = _own(client, env_headers, session_id, "Commented")
    own_task = get_task_by_code(client, headers, own["ref"])

    # ── stored on the named task ───────────────────────────────────────
    r = _post(client, env_headers, "comment", session_id, ref=own["ref"], body="Progress note")
    assert r.status_code == 200, r.text
    result = r.json()
    assert result["ref"] == own["ref"]
    assert result["attachments"] == 0 and result["failed_attachments"] == 0
    comments = list_comments(client, headers, own_task["id"])["data"]
    stored = [c for c in comments if c["id"] == result["comment_id"]]
    assert stored and stored[0]["content"] == "Progress note"
    assert stored[0]["author_agent_id"] == agent["id"]

    # ── another user's task, by UUID: not found, nothing stored ────────
    foreign = create_task(client, normal_user_token_headers, original_message="Private")
    r = _post(client, env_headers, "comment", session_id, ref=foreign["id"], body="Peek")
    assert r.status_code == 404, r.text
    foreign_comments = list_comments(client, normal_user_token_headers, foreign["id"])["data"]
    assert all(c["content"] != "Peek" for c in foreign_comments)

    # ── over-long body is a request error ──────────────────────────────
    r = _post(client, env_headers, "comment", session_id, ref=own["ref"], body="x" * 8001)
    assert r.status_code == 422


# ── 6. tasks_report on the conversation's own task ───────────────────────

def test_report_status_mapping_idempotency_and_scope(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session,
) -> None:
    """
      1. in_progress -> task stays in_progress, result stored (category active)
      2. blocked needs a question; with one -> blocked, explicit audience kept
      3. done after blocked -> completed through a recorded resume step
      4. failed -> error with the summary as error_message
      5. identical repeat returns the stored result; a different late report 409
      6. a task assigned to another agent cannot be reported on
    """
    headers = superuser_token_headers
    owner_id = _owner_id(client, headers)
    agent = create_agent_via_api(client, headers, name="CTP Reporter")
    peer = create_agent_via_api(client, headers, name="CTP Reporter Peer")
    drain_tasks()
    first, first_session = _running_session(client, headers, agent["id"])
    second, second_session = _running_session(client, headers, agent["id"])
    _, env_headers = create_env_with_token(db, agent_id=agent["id"], owner_id=owner_id)

    # ── Phase 1: in_progress ───────────────────────────────────────────
    r = _post(client, env_headers, "report", first_session, status="in_progress",
              summary="Halfway")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "in_progress" and r.json()["audience"] == "user"
    assert get_task(client, headers, first["id"])["status"] == "in_progress"
    r = _get(client, env_headers, "get", first_session, ref=first["short_code"])
    assert r.json()["category"] == "active" and r.json()["result"]["summary"] == "Halfway"

    # ── Phase 2: blocked ───────────────────────────────────────────────
    r = _post(client, env_headers, "report", first_session, status="blocked", summary="Stuck")
    assert r.status_code == 400 and "question" in r.json()["detail"]
    r = _post(client, env_headers, "report", first_session, status="blocked", summary="Stuck",
              question="Which region?", audience="requester")
    assert r.status_code == 200, r.text
    assert r.json()["audience"] == "requester" and r.json()["question"] == "Which region?"
    assert get_task(client, headers, first["id"])["status"] == "blocked"

    # ── Phase 3: done after blocked ────────────────────────────────────
    r = _post(client, env_headers, "report", first_session, status="done", summary="Finished")
    assert r.status_code == 200, r.text
    assert get_task(client, headers, first["id"])["status"] == "completed"
    reasons = [h["reason"] for h in get_task_detail(client, headers, first["id"])["status_history"]]
    assert "Delegation resumed" in reasons

    # ── Phase 4: failed (ref given from the other conversation's own session) ──
    failed = {"status": "failed", "summary": "Credentials rejected"}
    r = _post(client, env_headers, "report", second_session, **failed)
    assert r.status_code == 200, r.text
    stored = r.json()
    assert stored["ref"] == second["short_code"]
    task = get_task(client, headers, second["id"])
    assert task["status"] == "error" and task["error_message"] == "Credentials rejected"

    # ── Phase 5: idempotent repeat, late different report ──────────────
    r = _post(client, env_headers, "report", second_session, **failed)
    assert r.status_code == 200 and r.json()["id"] == stored["id"]
    r = _post(client, env_headers, "report", second_session, status="done", summary="Actually ok")
    assert r.status_code == 409, r.text
    assert get_task(client, headers, second["id"])["status"] == "error"

    # ── Phase 6: not assigned to the caller ────────────────────────────
    peer_task = create_task(client, headers, original_message="Peer work",
                            selected_agent_id=peer["id"])
    r = _post(client, env_headers, "report", first_session, ref=peer_task["short_code"],
              status="done", summary="Not mine")
    assert r.status_code == 400, r.text
    assert get_task(client, headers, peer_task["id"])["status"] == "new"


# ── 7. Delegated task: report, reply and result packet ───────────────────

def test_delegated_report_reply_and_result_packet(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session,
) -> None:
    """
      1. Requester (protocol 1) delegates to a pre-protocol executor; its
         finished turn reaches the requester as `Task result from <agent> on <ref>:`
      2. Executor reports blocked without audience -> requester; packet asks
         the requester to answer with tasks_reply
      3. Requester's tasks_reply answers the question into the child session;
         the same answer again is idempotent
      4. A different message is a follow-up into the child session
      5. The requester cannot report on the delegated task; another
         conversation of the requester cannot reply to it; an own task that
         never started has nothing to reply to
      6. Executor reports done with a long body -> packet body cut with a
         tasks_get pointer and artifacts listed
    """
    from app.models import InputTask
    from app.services.tasks.input_task_service import InputTaskService

    headers = superuser_token_headers
    owner_id = _owner_id(client, headers)
    requester = create_agent_via_api(client, headers, name="CTP Packet Requester")
    executor = create_agent_via_api(client, headers, name="CTP Packet Executor")
    drain_tasks()
    _, req_session = _running_session(client, headers, requester["id"])
    _, req_env = create_env_with_token(db, agent_id=requester["id"], owner_id=owner_id)
    # The requester speaks the protocol, so results reach it as result
    # packets; the executor is pre-protocol, so its silent first turn still
    # completes the task (the report rule is test 14).
    _set_env_version(client, headers, db, requester["id"], 1)
    _set_env_version(client, headers, db, executor["id"], 0)
    _, exe_env = create_env_with_token(db, agent_id=executor["id"], owner_id=owner_id)

    def _packet() -> str:
        db.expire_all()
        task = db.get(InputTask, UUID(child["id"]))
        return InputTaskService.result_packet_text(db, task, "completed", "session summary")

    # ── Phase 1: delegate; the finished turn comes back as a packet ────
    r = _create_streaming(client, req_env, req_session, title="Research",
                          description="Find the numbers", assignee=f"agent:{executor['id']}")
    assert r.status_code == 200, r.text
    ref = r.json()["ref"]
    child = get_task_by_code(client, headers, ref)
    exe_session = str(child["session_id"])
    first_line = f"Task result from {executor['name']} on {ref}:"
    packets = [m["content"] for m in list_messages(client, headers, req_session)
               if m["role"] == "user" and m["content"].startswith("Task result from")]
    assert packets, "the executor's finished turn must reach the requester"
    assert packets[-1].splitlines()[0] == first_line
    assert packets[-1].splitlines()[1] == "Status: done"
    # No report was filed: the summary points at tasks_get.
    assert packets[-1].splitlines()[2].startswith("Summary: Finished without a report")
    assert f"tasks_get {ref}" in packets[-1]
    _ensure_in_progress(client, headers, child["id"])

    # ── Phase 2: blocked, default audience is the requester ────────────
    r = _post(client, exe_env, "report", exe_session, status="blocked", summary="Need input",
              question="Which quarter?")
    assert r.status_code == 200, r.text
    assert r.json()["ref"] == ref and r.json()["audience"] == "requester"
    assert get_task(client, headers, child["id"])["status"] == "blocked"
    packet = _packet()
    assert packet.splitlines()[0] == first_line
    assert "Status: blocked" in packet and "Summary: Need input" in packet
    assert "Question for you (answer with tasks_reply): Which quarter?" in packet
    r = _get(client, req_env, "get", req_session, ref=ref)
    assert r.status_code == 200, r.text
    assert r.json()["category"] == "waiting" and r.json()["relation"] == "delegated"
    assert r.json()["result"]["question"] == "Which quarter?"

    # ── Phase 3: the answer goes into the child session ────────────────
    with patch(_AGENT_ENV_PATCH, StubAgentEnvConnector(response_text="Thanks")):
        r = _post(client, req_env, "reply", req_session, ref=ref, message="Q3 please")
        drain_tasks()
    assert r.status_code == 200, r.text
    assert r.json() == {"ref": ref, "answered_question": True, "delivered": True, "uncertain": False}
    child_texts = [m["content"] for m in list_messages(client, headers, exe_session)]
    assert any("Which quarter?" in t and "Q3 please" in t for t in child_texts)
    r = _post(client, req_env, "reply", req_session, ref=ref, message="Q3 please")
    assert r.status_code == 200 and r.json()["answered_question"] is True
    assert sum("Q3 please" in t for t in
               (m["content"] for m in list_messages(client, headers, exe_session))) == 1

    # ── Phase 4: a different message is a follow-up ────────────────────
    with patch(_AGENT_ENV_PATCH, StubAgentEnvConnector(response_text="Noted")):
        r = _post(client, req_env, "reply", req_session, ref=ref, message="Also add Q4")
        drain_tasks()
    assert r.status_code == 200, r.text
    assert r.json()["answered_question"] is False and r.json()["delivered"] is True
    assert any(m["content"] == "Also add Q4" for m in list_messages(client, headers, exe_session))

    # ── Phase 5: scope refusals ────────────────────────────────────────
    r = _post(client, req_env, "report", req_session, ref=ref, status="done", summary="Mine?")
    assert r.status_code == 400, r.text
    _, other_req_session = _running_session(client, headers, requester["id"])
    r = _post(client, req_env, "reply", other_req_session, ref=ref, message="Hello")
    assert r.status_code == 400, r.text
    own = _own(client, req_env, req_session, "Never started")
    r = _post(client, req_env, "reply", req_session, ref=own["ref"], message="Hello")
    assert r.status_code == 409, r.text
    assert "not started" in r.json()["detail"]

    # ── Phase 6: done with a long body ─────────────────────────────────
    _ensure_in_progress(client, headers, child["id"])
    body = "a" * 8000 + "TAIL-NOT-SHOWN"
    r = _post(client, exe_env, "report", exe_session, status="done", summary="Numbers found",
              body=body, artifacts=["https://example.com/q3.csv",
                                    {"name": "Chart", "ref": "https://example.com/chart.png"}])
    assert r.status_code == 200, r.text
    assert get_task(client, headers, child["id"])["status"] == "completed"
    packet = _packet()
    lines = packet.splitlines()
    assert lines[0] == first_line and lines[1] == "Status: done"
    assert lines[2] == "Summary: Numbers found"
    assert "TAIL-NOT-SHOWN" not in packet
    assert f"(cut at 8000 characters; tasks_get {ref} has the full result)" in packet
    assert "- q3.csv: https://example.com/q3.csv" in packet
    assert "- Chart: https://example.com/chart.png" in packet
    assert "Question for" not in packet
    # tasks_get keeps the full body.
    r = _get(client, req_env, "get", req_session, ref=ref)
    assert r.json()["result"]["body"] == body
    assert "reply_message" not in r.json()["result"]


# ── 8. tasks_list / tasks_get ────────────────────────────────────────────

def test_list_relations_categories_and_get_scope(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    normal_user_token_headers: dict[str, str],
    db: Session,
) -> None:
    headers = superuser_token_headers
    owner_id = _owner_id(client, headers)
    agent = create_agent_via_api(client, headers, name="CTP Lister")
    peer = create_agent_via_api(client, headers, name="CTP Lister Peer")
    drain_tasks()
    host, session_id = _running_session(client, headers, agent["id"])
    _, env_headers = create_env_with_token(db, agent_id=agent["id"], owner_id=owner_id)

    # A delegation to a pre-protocol executor, whose stub turn completes it
    # at once (closed).
    _set_env_version(client, headers, db, peer["id"], 0)
    r = _create_streaming(client, env_headers, session_id, title="Delegated",
                          description="Go", assignee=f"agent:{peer['id']}")
    assert r.status_code == 200, r.text
    delegated = r.json()["ref"]
    _ensure_in_progress(client, headers, host["id"])
    backlog = _own(client, env_headers, session_id, "Backlog item")["ref"]
    ready = _own(client, env_headers, session_id, "Ready item", category="ready")["ref"]
    cancelled = _own(client, env_headers, session_id, "Dropped")["ref"]
    r = _post(client, env_headers, "update", session_id, ref=cancelled, category="cancelled")
    assert r.status_code == 200, r.text
    # Work of an unrelated agent is never listed.
    unrelated = create_task(client, headers, original_message="Other",
                            selected_agent_id=peer["id"])["short_code"]
    me = host["short_code"]
    assert get_task_by_code(client, headers, delegated)["status"] == "completed"

    def _list(**params) -> set[str]:
        refs = _refs(_get(client, env_headers, "list", session_id, **params))
        assert unrelated not in refs
        return refs

    # ── relation -> scope (open work only by default) ──────────────────
    # Own-queue tasks are assigned to the caller and created by it.
    assert _list(relation="assigned") == {me, backlog, ready}
    assert _list(relation="created") == {backlog, ready}
    assert _list(relation="delegated") == set()
    assert _list() == {me, backlog, ready}
    assert _list(relation="created", include_closed=True) == {backlog, ready, cancelled, delegated}
    assert _list(relation="delegated", include_closed=True) == {delegated}

    # ── category filter ────────────────────────────────────────────────
    assert _list(category="backlog") == {backlog}
    assert _list(category="ready") == {ready}
    assert _list(category="active") == {me}
    assert _list(category="waiting") == set()
    assert _list(category="done") == {cancelled, delegated}

    r = _get(client, env_headers, "list", session_id, relation="created", include_closed=True)
    by_ref = {t["ref"]: t for t in r.json()["tasks"]}
    assert by_ref[delegated]["relation"] == "delegated"
    assert by_ref[delegated]["assignee"] == peer["name"]
    assert by_ref[backlog]["relation"] == "assigned" and by_ref[backlog]["category"] == "backlog"
    assert by_ref[backlog]["assignee"] == agent["name"]
    assert by_ref[cancelled]["category"] == "done" and by_ref[cancelled]["status"] == "cancelled"
    r = _get(client, env_headers, "list", session_id, relation="assigned")
    assert {t["relation"] for t in r.json()["tasks"]} == {"assigned"}

    r = _get(client, env_headers, "list", session_id, relation="mine")
    assert r.status_code == 422
    r = _get(client, env_headers, "list", session_id, category="cancelled")
    assert r.status_code == 422

    # ── tasks_get scope ────────────────────────────────────────────────
    r = _get(client, env_headers, "get", session_id, ref=ready)
    assert r.status_code == 200, r.text
    assert r.json()["category"] == "ready" and r.json()["relation"] == "assigned"
    foreign = create_task(client, normal_user_token_headers, original_message="Private")
    r = _get(client, env_headers, "get", session_id, ref=foreign["id"])
    assert r.status_code == 404, r.text


# ── 9. Subtask idempotency ───────────────────────────────────────────────

def test_subtask_id_is_idempotent_with_one_run(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session,
) -> None:
    """The same `id` twice on the parent+assignee path: one subtask, carrying
    its external_ref from creation, executed once."""
    headers = superuser_token_headers
    owner_id = _owner_id(client, headers)
    lead = create_agent_via_api(client, headers, name="Idem Lead CTP")
    worker = create_agent_via_api(client, headers, name="Idem Worker CTP")
    drain_tasks()
    team = create_team(client, headers, name="CTP Idem Team")
    lead_node = create_node(client, headers, team["id"], lead["id"], is_lead=True)
    worker_node = create_node(client, headers, team["id"], worker["id"])
    create_connection(client, headers, team["id"], source_node_id=lead_node["id"],
                      target_node_id=worker_node["id"], enabled=True)
    host, session_id = _running_session(
        client, headers, lead["id"], team_id=team["id"], assigned_node_id=lead_node["id"],
    )
    _, env_headers = create_env_with_token(db, agent_id=lead["id"], owner_id=owner_id)
    request = dict(title="Idem sub", description="Once only", parent=host["short_code"],
                   assignee=f"agent:{worker['id']}", id="idem-sub-0001")

    r = _create_streaming(client, env_headers, session_id, **request)
    assert r.status_code == 200, r.text
    first = r.json()
    assert first["created"] is True
    sub = get_task_by_code(client, headers, first["ref"])
    assert sub["external_ref"] and sub["external_ref"].startswith("ctp-")
    assert len(get_task_sessions(client, headers, sub["id"])) == 1

    r = _create_streaming(client, env_headers, session_id, **request)
    assert r.status_code == 200, r.text
    assert r.json()["created"] is False and r.json()["ref"] == first["ref"]
    assert len(get_task_sessions(client, headers, sub["id"])) == 1
    r = _get(client, env_headers, "list", session_id, relation="delegated", include_closed=True)
    assert [t["ref"] for t in r.json()["tasks"] if t["title"] == "Idem sub"] == [first["ref"]]


# ── 10. Report artifacts ─────────────────────────────────────────────────

def test_report_keeps_links_and_records_paths_as_files(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session,
) -> None:
    """tasks_report is never refused over an artifact: http(s) refs stay links,
    a path (string, or an object whose ref is not http) becomes a file with
    no ref, and the result warns about path artifacts in `notes`."""
    headers = superuser_token_headers
    owner_id = _owner_id(client, headers)
    agent = create_agent_via_api(client, headers, name="CTP Artifacts")
    drain_tasks()
    _, links_session = _running_session(client, headers, agent["id"])
    paths_task, paths_session = _running_session(client, headers, agent["id"])
    _, env_headers = create_env_with_token(db, agent_id=agent["id"], owner_id=owner_id)

    # ── links only: kept as given, no warning ──────────────────────────
    r = _post(client, env_headers, "report", links_session, status="done", summary="Links", artifacts=[
        "http://example.com/plain.txt",
        {"kind": "link", "name": "Dashboard", "ref": "https://example.com/dash"},
    ])
    assert r.status_code == 200, r.text
    stored = r.json()
    assert stored["artifacts"] == [
        {"kind": "link", "name": "plain.txt", "ref": "http://example.com/plain.txt"},
        {"kind": "link", "name": "Dashboard", "ref": "https://example.com/dash"},
    ]
    assert not stored.get("notes")

    # ── paths: recorded as files, never refused ────────────────────────
    r = _post(client, env_headers, "report", paths_session, status="done", summary="Paths", artifacts=[
        "out/report.pdf",
        {"kind": "file", "ref": "data/q3.csv"},
        "https://example.com/kept.png",
    ])
    assert r.status_code == 200, r.text
    stored = r.json()
    files = [a for a in stored["artifacts"] if a["kind"] == "file"]
    assert [a["name"] for a in files] == ["out/report.pdf", "data/q3.csv"]
    assert all(not a.get("ref") for a in files)
    assert {"kind": "link", "name": "kept.png", "ref": "https://example.com/kept.png"} in stored["artifacts"]
    assert stored.get("notes"), "a path artifact must come back with a warning"
    assert get_task(client, headers, paths_task["id"])["status"] == "completed"


# ── 11. Follow-up retry deduplication ────────────────────────────────────

def test_reply_follow_up_retry_in_same_turn_is_sent_once(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session,
) -> None:
    headers = superuser_token_headers
    owner_id = _owner_id(client, headers)
    requester = create_agent_via_api(client, headers, name="CTP Retry Requester")
    executor = create_agent_via_api(client, headers, name="CTP Retry Executor")
    drain_tasks()
    _, req_session = _running_session(client, headers, requester["id"])
    _, req_env = create_env_with_token(db, agent_id=requester["id"], owner_id=owner_id)
    r = _create_streaming(client, req_env, req_session, title="Retry", description="Do it",
                          assignee=f"agent:{executor['id']}")
    assert r.status_code == 200, r.text
    ref = r.json()["ref"]
    child_session = str(get_task_by_code(client, headers, ref)["session_id"])

    def _count() -> int:
        return sum(m["content"] == "Please also check Q4"
                   for m in list_messages(client, headers, child_session))

    # A lost acknowledgement makes the tool call again in the same turn.
    with patch(_AGENT_ENV_PATCH, StubAgentEnvConnector(response_text="Noted")):
        for _ in range(2):
            r = _post(client, req_env, "reply", req_session, ref=ref, message="Please also check Q4")
            assert r.status_code == 200, r.text
            assert r.json()["answered_question"] is False
        drain_tasks()
    assert _count() == 1

    # The same words in a later turn of the requester are a new message.
    with patch(_AGENT_ENV_PATCH, StubAgentEnvConnector(response_text="Next turn")):
        send_message(client, headers, req_session, "Ask them again")
        drain_tasks()
        r = _post(client, req_env, "reply", req_session, ref=ref, message="Please also check Q4")
        drain_tasks()
    assert r.status_code == 200, r.text
    assert _count() == 2


# ── 12. Inactive agents ──────────────────────────────────────────────────

def test_inactive_agent_is_not_a_target(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session,
) -> None:
    headers = superuser_token_headers
    owner_id = _owner_id(client, headers)
    agent = create_agent_via_api(client, headers, name="CTP Active Requester")
    dormant = create_agent_via_api(client, headers, name="CTP Dormant Executor")
    drain_tasks()
    _, session_id = _running_session(client, headers, agent["id"])
    _, env_headers = create_env_with_token(db, agent_id=agent["id"], owner_id=owner_id)
    update_agent(client, headers, dormant["id"], is_active=False)

    r = _get(client, env_headers, "targets", session_id)
    assert r.status_code == 200, r.text
    assert f"agent:{dormant['id']}" not in {t["target"] for t in r.json()["targets"]}
    for assignee in (f"agent:{dormant['id']}", dormant["name"]):
        r = _post(client, env_headers, "create", session_id, title="Wake up",
                  description="Do it", assignee=assignee)
        assert r.status_code == 400, (assignee, r.text)
        assert "tasks_targets" in r.json()["detail"]


# ── 13. tasks_comment relation rule ──────────────────────────────────────

def test_comment_needs_a_relation_to_the_task(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session,
) -> None:
    """Allowed on the assigned, created and delegated tasks; refused on the
    owner's task that the calling agent has nothing to do with."""
    headers = superuser_token_headers
    owner_id = _owner_id(client, headers)
    agent = create_agent_via_api(client, headers, name="CTP Relation Commenter")
    peer = create_agent_via_api(client, headers, name="CTP Relation Peer")
    drain_tasks()
    host, session_id = _running_session(client, headers, agent["id"])
    _, env_headers = create_env_with_token(db, agent_id=agent["id"], owner_id=owner_id)
    r = _create_streaming(client, env_headers, session_id, title="Delegated", description="Go",
                          assignee=f"agent:{peer['id']}")
    assert r.status_code == 200, r.text
    delegated = r.json()["ref"]
    created = _own(client, env_headers, session_id, "Created")["ref"]

    for ref in (host["short_code"], created, delegated):
        r = _post(client, env_headers, "comment", session_id, ref=ref, body=f"Note on {ref}")
        assert r.status_code == 200, (ref, r.text)
        task_id = get_task_by_code(client, headers, ref)["id"]
        assert any(c["content"] == f"Note on {ref}"
                   for c in list_comments(client, headers, task_id)["data"])

    unrelated = create_task(client, headers, original_message="Peer's own",
                            selected_agent_id=peer["id"])
    r = _post(client, env_headers, "comment", session_id, ref=unrelated["short_code"], body="Peek")
    assert r.status_code == 400, r.text
    assert "comment only" in r.json()["detail"]
    assert all(c["content"] != "Peek" for c in list_comments(client, headers, unrelated["id"])["data"])


# ── 14. Ended without reporting (real stub turn) ─────────────────────────

def test_delegation_ending_without_report_is_blocked(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session,
) -> None:
    """An executor environment at protocol 1 stays there through its turn (a
    bare done event does not lower it); the delegated turn ends with no
    tasks_report, so the task is blocked as Ended without reporting."""
    headers = superuser_token_headers
    owner_id = _owner_id(client, headers)
    requester = create_agent_via_api(client, headers, name="CTP Silent Requester")
    executor = create_agent_via_api(client, headers, name="CTP Silent Executor")
    drain_tasks()
    _, session_id = _running_session(client, headers, requester["id"])
    _, env_headers = create_env_with_token(db, agent_id=requester["id"], owner_id=owner_id)
    _set_env_version(client, headers, db, executor["id"], 1)

    r = _create_streaming(client, env_headers, session_id, title="Silent", description="Do it",
                          assignee=f"agent:{executor['id']}")
    assert r.status_code == 200, r.text
    child = get_task_by_code(client, headers, r.json()["ref"])
    assert child["status"] == "blocked"
    detail = get_task_detail(client, headers, child["id"])
    assert any("Ended without reporting" in c["content"] for c in detail["comments"])
    blocked = [h for h in detail["status_history"] if h["to_status"] == "blocked"]
    assert blocked and blocked[-1]["reason"] == "Ended without reporting"
    r = _get(client, env_headers, "get", session_id, ref=child["short_code"])
    assert r.json()["category"] == "waiting"


def _session_protocol(db: Session, session_id: str, agent_id: str) -> dict:
    """The data behind the env's task context block for this session."""
    from app.models import Agent
    from app.models import Session as ChatSession
    from app.services.sessions.message_service import _build_session_context

    db.expire_all()
    chat = db.get(ChatSession, UUID(session_id))
    return _build_session_context(db, chat, None, db.get(Agent, UUID(agent_id)))["task_protocol"]


# ── 15. Own task: ready count and report ─────────────────────────────────

def test_own_task_is_a_ready_task_and_takes_a_report(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session,
) -> None:
    headers = superuser_token_headers
    owner_id = _owner_id(client, headers)
    agent = create_agent_via_api(client, headers, name="CTP Own Queue")
    drain_tasks()
    _, session_id = _running_session(client, headers, agent["id"])
    _, env_headers = create_env_with_token(db, agent_id=agent["id"], owner_id=owner_id)
    assert _session_protocol(db, session_id, agent["id"])["ready_tasks"] == 0

    ready = _own(client, env_headers, session_id, "Queue item", category="ready")
    _own(client, env_headers, session_id, "Backlog item")  # backlog is not ready
    assert _session_protocol(db, session_id, agent["id"])["ready_tasks"] == 1

    r = _post(client, env_headers, "report", session_id, ref=ready["ref"], status="done",
              summary="Done from the queue")
    assert r.status_code == 200, r.text
    assert r.json()["ref"] == ready["ref"] and r.json()["status"] == "done"
    assert get_task_by_code(client, headers, ready["ref"])["status"] == "completed"
    assert _session_protocol(db, session_id, agent["id"])["ready_tasks"] == 0


# ── 16. Chain depth ──────────────────────────────────────────────────────

def test_chain_depth_two_allowed_third_hop_refused(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session,
) -> None:
    """A user's task (depth 0) delegates to B (depth 1), B delegates to C
    (depth 2); anything C creates would be depth 3 and is refused."""
    headers = superuser_token_headers
    owner_id = _owner_id(client, headers)
    a = create_agent_via_api(client, headers, name="CTP Chain A")
    b = create_agent_via_api(client, headers, name="CTP Chain B")
    c = create_agent_via_api(client, headers, name="CTP Chain C")
    d = create_agent_via_api(client, headers, name="CTP Chain D")
    drain_tasks()
    _, a_session = _running_session(client, headers, a["id"])
    _, a_env = create_env_with_token(db, agent_id=a["id"], owner_id=owner_id)
    _, b_env = create_env_with_token(db, agent_id=b["id"], owner_id=owner_id)
    _, c_env = create_env_with_token(db, agent_id=c["id"], owner_id=owner_id)
    assert _session_protocol(db, a_session, a["id"])["chain_depth"] == 0

    r = _create_streaming(client, a_env, a_session, title="Hop 1", description="Go",
                          assignee=f"agent:{b['id']}")
    assert r.status_code == 200, r.text
    b_session = str(get_task_by_code(client, headers, r.json()["ref"])["session_id"])
    assert _session_protocol(db, b_session, b["id"])["chain_depth"] == 1

    r = _create_streaming(client, b_env, b_session, title="Hop 2", description="Go",
                          assignee=f"agent:{c['id']}")
    assert r.status_code == 200, r.text
    c_session = str(get_task_by_code(client, headers, r.json()["ref"])["session_id"])
    assert _session_protocol(db, c_session, c["id"])["chain_depth"] == 2

    for fields in ({"assignee": f"agent:{d['id']}"}, {}):
        r = _post(client, c_env, "create", c_session, title="Hop 3", description="Go", **fields)
        assert r.status_code == 400, (fields, r.text)
        assert "depth 3" in r.json()["detail"] and "limit is 2" in r.json()["detail"]


def test_chain_depth_walks_chains_built_by_older_routes(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session,
) -> None:
    """Tasks linked only by source_session_id (no chain_depth recorded) still
    count: the third hop is refused."""
    headers = superuser_token_headers
    owner_id = _owner_id(client, headers)
    a = create_agent_via_api(client, headers, name="CTP Old Chain A")
    b = create_agent_via_api(client, headers, name="CTP Old Chain B")
    c = create_agent_via_api(client, headers, name="CTP Old Chain C")
    drain_tasks()
    _, a_session = _running_session(client, headers, a["id"])
    _, b_session = _running_session(client, headers, b["id"], source_session_id=a_session)
    _, c_session = _running_session(client, headers, c["id"], source_session_id=b_session)
    _, b_env = create_env_with_token(db, agent_id=b["id"], owner_id=owner_id)
    _, c_env = create_env_with_token(db, agent_id=c["id"], owner_id=owner_id)

    # B's task is depth 1: creating from it is depth 2, allowed.
    r = _post(client, b_env, "create", b_session, title="From B", description="Fine")
    assert r.status_code == 200, r.text
    # C's task is depth 2: creating from it is depth 3, refused.
    r = _post(client, c_env, "create", c_session, title="From C", description="Too deep")
    assert r.status_code == 400, r.text
    assert "depth 3" in r.json()["detail"]


# ── 17. Per-conversation cap ─────────────────────────────────────────────

def test_twenty_creates_per_conversation_and_repeated_id_at_cap(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session,
) -> None:
    headers = superuser_token_headers
    owner_id = _owner_id(client, headers)
    agent = create_agent_via_api(client, headers, name="CTP Cap Requester")
    peer = create_agent_via_api(client, headers, name="CTP Cap Executor")
    drain_tasks()
    _, session_id = _running_session(client, headers, agent["id"])
    _, env_headers = create_env_with_token(db, agent_id=agent["id"], owner_id=owner_id)

    keyed = dict(title="Keyed", description="Once", assignee=f"agent:{peer['id']}", id="cap-key-0001")
    r = _create_streaming(client, env_headers, session_id, **keyed)
    assert r.status_code == 200, r.text
    first = r.json()
    for n in range(19):
        _own(client, env_headers, session_id, f"Item {n}")

    r = _post(client, env_headers, "create", session_id, title="One too many", description="No")
    assert r.status_code == 400, r.text
    assert "20" in r.json()["detail"]

    r = _post(client, env_headers, "create", session_id, **keyed)
    assert r.status_code == 200, r.text
    assert r.json()["created"] is False and r.json()["ref"] == first["ref"]

    # The cap is per conversation: another session of the same agent may create.
    _, other_session = _running_session(client, headers, agent["id"])
    _own(client, env_headers, other_session, "Fresh conversation")


# ── 18. Team member's own subtask ────────────────────────────────────────

def test_team_member_own_subtask_lands_in_its_own_queue(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session,
) -> None:
    headers = superuser_token_headers
    owner_id = _owner_id(client, headers)
    lead = create_agent_via_api(client, headers, name="CTP Queue Lead")
    member = create_agent_via_api(client, headers, name="CTP Queue Member")
    drain_tasks()
    team = create_team(client, headers, name="CTP Queue Team")
    create_node(client, headers, team["id"], lead["id"], is_lead=True)
    member_node = create_node(client, headers, team["id"], member["id"])
    host, session_id = _running_session(
        client, headers, member["id"], team_id=team["id"], assigned_node_id=member_node["id"],
    )
    _, env_headers = create_env_with_token(db, agent_id=member["id"], owner_id=owner_id)

    r = _post(client, env_headers, "create", session_id, title="Member follow-up",
              description="Later", parent=host["short_code"])
    assert r.status_code == 200, r.text
    created = r.json()
    assert created["assignee"] == member["name"] and created["relation"] == "assigned"
    task = get_task_by_code(client, headers, created["ref"])
    assert task["selected_agent_id"] == member["id"]
    assert task["assigned_node_id"] == member_node["id"]
    assert task["status"] == "new" and task["session_id"] is None
    assert get_task_sessions(client, headers, task["id"]) == []
