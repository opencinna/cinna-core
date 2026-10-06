"""
Cinna Task Protocol endpoints (task_protocol_api.py) — smoke scenarios.

The `cinna` MCP tools in an agent environment post here; authentication is the
scoped agent-environment token, every call carries the calling session.

Scenarios:
  1. Own queue: targets → create (ready) → list → get → update → comment →
     report done on the conversation's own task → update refused once closed
  2. Delegation: create with an `agent:<uuid>` target executes it; repeating
     the same `id` returns the same task; reply to a task this conversation
     did not create is refused; custom_fields are refused
"""
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.core.config import settings
from tests.stubs.agent_env_stub import StubAgentEnvConnector
from tests.utils.agent import create_agent_via_api
from tests.utils.agent_env import create_env_with_token
from tests.utils.background_tasks import drain_tasks
from tests.utils.input_task import create_task, execute_task, get_task

_PROTOCOL = f"{settings.API_V1_STR}/agent/tasks/protocol"
_AGENT_ENV_PATCH = "app.services.sessions.message_service.agent_env_connector"


def _owner_id(client: TestClient, headers: dict[str, str]) -> str:
    r = client.get(f"{settings.API_V1_STR}/users/me", headers=headers)
    assert r.status_code == 200
    return r.json()["id"]


def _running_session(client: TestClient, headers: dict[str, str], agent_id: str) -> tuple[dict, str]:
    """A task assigned to the agent, executed, so a session is working on it."""
    task = create_task(
        client, headers, original_message="Protocol host task", selected_agent_id=agent_id,
    )
    with patch(_AGENT_ENV_PATCH, StubAgentEnvConnector(response_text="Working")):
        result = execute_task(client, headers, task["id"])
        drain_tasks()
    # The stub turn ends at once and settles the task; put it back under way,
    # as it is while a real turn is still running.
    if get_task(client, headers, task["id"])["status"] != "in_progress":
        r = client.post(f"{settings.API_V1_STR}/tasks/{task['id']}/status", headers=headers,
                        json={"status": "in_progress"})
        assert r.status_code == 200, r.text
    return task, str(result["session_id"])


def test_own_queue_round_trip(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session,
) -> None:
    headers = superuser_token_headers
    owner_id = _owner_id(client, headers)
    agent = create_agent_via_api(client, headers, name="Protocol Worker")
    peer = create_agent_via_api(client, headers, name="Protocol Peer")
    drain_tasks()
    host_task, session_id = _running_session(client, headers, agent["id"])
    _, env_headers = create_env_with_token(db, agent_id=agent["id"], owner_id=owner_id)

    # ── targets: the owner's other agents, never itself ──────────────────
    r = client.get(f"{_PROTOCOL}/targets", headers=env_headers, params={"source_session_id": session_id})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["protocol_version"] == 1
    targets = {t["target"]: t for t in body["targets"]}
    assert f"agent:{peer['id']}" in targets
    assert f"agent:{agent['id']}" not in targets
    entry = targets[f"agent:{peer['id']}"]
    assert entry["kind"] == "cinna" and entry["location"] == "remote"
    assert entry["capabilities"]["custom_fields"] is False

    # ── create in own queue as ready ─────────────────────────────────────
    r = client.post(f"{_PROTOCOL}/create", headers=env_headers, json={
        "source_session_id": session_id, "title": "Follow-up", "description": "Check the logs",
        "category": "ready", "id": "ignored-key",
    })
    assert r.status_code == 200, r.text
    created = r.json()
    assert created["category"] == "ready" and created["status"] == "open"
    # Own queue: assigned to the calling agent, not started.
    assert created["relation"] == "assigned" and created["assignee"] == "Protocol Worker"
    assert any("`id`" in note for note in created["notes"])
    ref = created["ref"]

    # ── list / get ───────────────────────────────────────────────────────
    r = client.get(f"{_PROTOCOL}/list", headers=env_headers,
                   params={"source_session_id": session_id, "relation": "created"})
    assert r.status_code == 200, r.text
    assert ref in [t["ref"] for t in r.json()["tasks"]]
    r = client.get(f"{_PROTOCOL}/list", headers=env_headers,
                   params={"source_session_id": session_id, "relation": "assigned"})
    assert host_task["short_code"] in [t["ref"] for t in r.json()["tasks"]]

    r = client.get(f"{_PROTOCOL}/get", headers=env_headers,
                   params={"source_session_id": session_id, "ref": ref})
    assert r.status_code == 200, r.text
    assert r.json()["ref"] == ref and r.json()["category"] == "ready"

    # ── update fields and a manual move; custom_fields refused ───────────
    r = client.post(f"{_PROTOCOL}/update", headers=env_headers, json={
        "source_session_id": session_id, "ref": ref, "priority": "high", "category": "cancelled",
    })
    assert r.status_code == 200, r.text
    assert r.json()["priority"] == "high" and r.json()["status"] == "cancelled"
    r = client.post(f"{_PROTOCOL}/update", headers=env_headers, json={
        "source_session_id": session_id, "ref": ref, "custom_fields": {"x": 1},
    })
    assert r.status_code == 400 and "custom_fields" in r.json()["detail"]

    # ── comment on the current task (no ref) ─────────────────────────────
    r = client.post(f"{_PROTOCOL}/comment", headers=env_headers, json={
        "source_session_id": session_id, "body": "Halfway there",
    })
    assert r.status_code == 200, r.text
    assert r.json()["ref"] == host_task["short_code"]

    # ── report on the conversation's own (non-delegated) task ────────────
    report = {"source_session_id": session_id, "status": "done", "summary": "All checked",
              "artifacts": ["https://example.com/report.pdf", "out/report.pdf"]}
    r = client.post(f"{_PROTOCOL}/report", headers=env_headers, json=report)
    assert r.status_code == 200, r.text
    stored = r.json()
    assert stored["ref"] == host_task["short_code"] and stored["status"] == "done"
    assert stored["audience"] == "user"
    assert stored["artifacts"][0]["kind"] == "link"
    # A working-folder path is recorded by name only, with a note; the report
    # itself is never refused over an artifact.
    assert stored["artifacts"][1]["kind"] == "file"
    assert stored["artifacts"][1]["name"] == "out/report.pdf"
    assert not stored["artifacts"][1].get("ref")
    assert any("tasks_comment" in note for note in stored["notes"])
    assert get_task(client, headers, host_task["id"])["status"] == "completed"
    # Idempotent: the same report returns the stored result.
    r = client.post(f"{_PROTOCOL}/report", headers=env_headers, json=report)
    assert r.status_code == 200 and r.json()["id"] == stored["id"]

    # ── update refused on the task under way? it is closed now: a move from
    #    completed to backlog is an invalid transition, reported as such.
    r = client.post(f"{_PROTOCOL}/update", headers=env_headers, json={
        "source_session_id": session_id, "ref": host_task["short_code"], "category": "backlog",
    })
    assert r.status_code == 400, r.text


def test_delegation_create_idempotent_and_reply_scope(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session,
) -> None:
    headers = superuser_token_headers
    owner_id = _owner_id(client, headers)
    agent = create_agent_via_api(client, headers, name="Protocol Requester")
    peer = create_agent_via_api(client, headers, name="Protocol Executor")
    drain_tasks()
    host_task, session_id = _running_session(client, headers, agent["id"])
    _, env_headers = create_env_with_token(db, agent_id=agent["id"], owner_id=owner_id)

    request = {
        "source_session_id": session_id, "title": "Summarise", "description": "Summarise the inbox",
        "assignee": f"agent:{peer['id']}", "id": "20261006-1200-summary", "group": "batch-one",
    }
    with patch(_AGENT_ENV_PATCH, StubAgentEnvConnector(response_text="On it")):
        r = client.post(f"{_PROTOCOL}/create", headers=env_headers, json=request)
        drain_tasks()
    assert r.status_code == 200, r.text
    first = r.json()
    assert first["created"] is True and first["assignee"] == peer["name"]
    assert first["relation"] == "delegated"
    assert any("`group`" in note for note in first["notes"])

    r = client.post(f"{_PROTOCOL}/create", headers=env_headers, json=request)
    assert r.status_code == 200, r.text
    assert r.json()["ref"] == first["ref"] and r.json()["created"] is False

    # Unknown target names the tool to call.
    r = client.post(f"{_PROTOCOL}/create", headers=env_headers, json={
        **request, "id": None, "assignee": "agent:not-a-uuid",
    })
    assert r.status_code == 400 and "tasks_targets" in r.json()["detail"]

    r = client.post(f"{_PROTOCOL}/create", headers=env_headers, json={
        **request, "id": None, "custom_fields": {"k": "v"},
    })
    assert r.status_code == 400 and "custom_fields" in r.json()["detail"]

    # The host task was not created by this conversation.
    r = client.post(f"{_PROTOCOL}/reply", headers=env_headers, json={
        "source_session_id": session_id, "ref": host_task["short_code"], "message": "hello",
    })
    assert r.status_code == 400 and "created" in r.json()["detail"]

    # A user JWT is not an agent-environment token.
    r = client.get(f"{_PROTOCOL}/targets", headers=headers, params={"source_session_id": session_id})
    assert r.status_code in (401, 403)


def test_update_moves_walk_the_path_and_refusals_name_the_limit(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session,
) -> None:
    """tasks_update done|cancelled from new/open walks the shortest legal path;
    a task under way is refused with the pointer to tasks_report; a text over
    the cap is refused with the limit in the message."""
    headers = superuser_token_headers
    owner_id = _owner_id(client, headers)
    agent = create_agent_via_api(client, headers, name="Protocol Mover")
    drain_tasks()
    host_task, session_id = _running_session(client, headers, agent["id"])
    _, env_headers = create_env_with_token(db, agent_id=agent["id"], owner_id=owner_id)

    def _create(category: str) -> dict:
        r = client.post(f"{_PROTOCOL}/create", headers=env_headers, json={
            "source_session_id": session_id, "title": f"Own {category}",
            "description": "Something to do", "category": category,
        })
        assert r.status_code == 200, r.text
        return r.json()

    def _update(ref: str, **fields) -> object:
        return client.post(f"{_PROTOCOL}/update", headers=env_headers, json={
            "source_session_id": session_id, "ref": ref, **fields,
        })

    backlog = _create("backlog")
    ready = _create("ready")
    assert backlog["status"] == "new" and ready["status"] == "open"

    # new -> in_progress -> completed, open -> in_progress -> completed
    for created in (backlog, ready):
        r = _update(created["ref"], category="done")
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "completed"

    # new -> cancelled is one step
    r = _update(_create("backlog")["ref"], category="cancelled")
    assert r.status_code == 200 and r.json()["status"] == "cancelled"

    # in_progress belongs to the executor
    r = _update(host_task["short_code"], category="done")
    assert r.status_code == 409, r.text
    assert "tasks_report" in r.json()["detail"]

    # the cap is stated so the agent can shorten
    r = _update(_create("backlog")["ref"], description="x" * 10001)
    assert r.status_code == 400, r.text
    assert "10000" in r.json()["detail"] and "10001" in r.json()["detail"]
    r = client.post(f"{_PROTOCOL}/create", headers=env_headers, json={
        "source_session_id": session_id, "title": "Long", "description": "y" * 10000,
    })
    assert r.status_code == 400, r.text
    assert "limit here is 10000" in r.json()["detail"]


def test_session_context_carries_the_task_protocol_block_data(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session,
) -> None:
    """The env's data-only task context block is fed by session_context["task_protocol"]."""
    from uuid import UUID

    from app.models import Agent, Session as ChatSession
    from app.services.sessions.message_service import _build_session_context

    headers = superuser_token_headers
    owner_id = _owner_id(client, headers)
    agent = create_agent_via_api(client, headers, name="Context Worker")
    peer = create_agent_via_api(client, headers, name="Context Peer")
    drain_tasks()
    host_task, session_id = _running_session(client, headers, agent["id"])
    _, env_headers = create_env_with_token(db, agent_id=agent["id"], owner_id=owner_id)

    def _protocol() -> dict:
        db.expire_all()
        chat = db.get(ChatSession, UUID(session_id))
        return _build_session_context(db, chat, None, db.get(Agent, UUID(agent["id"])))["task_protocol"]

    protocol = _protocol()
    assert protocol["here"] == "cinna-core agent Context Worker"
    assert protocol["current_task"]["ref"] == host_task["short_code"]
    # A user-created task on an environment that has not advertised the protocol.
    assert protocol["current_task"]["report_required"] is False
    assert protocol["chain_depth"] == 0 and protocol["open_delegations"] == 0

    with patch(_AGENT_ENV_PATCH, StubAgentEnvConnector(response_text="Working")):
        r = client.post(f"{_PROTOCOL}/create", headers=env_headers, json={
            "source_session_id": session_id, "title": "Delegated", "description": "Do it",
            "assignee": f"agent:{peer['id']}", "id": "ctx-1",
        })
        assert r.status_code == 200, r.text
        r = client.post(f"{_PROTOCOL}/create", headers=env_headers, json={
            "source_session_id": session_id, "title": "Mine", "description": "Later",
        })
        assert r.status_code == 200, r.text
    protocol = _protocol()
    assert protocol["open_delegations"] == 1
