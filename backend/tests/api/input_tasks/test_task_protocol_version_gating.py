"""
Cinna Task Protocol version gating — which wording an environment receives.

An environment's ``task_protocol_version`` (1 once its core carries the
``cinna`` task tools, 0 for an image built before) decides the text the
platform hands it. Each text follows the environment that consumes it:

  1. Creating an environment records version 1; a rebuild records it again
  2. The delegated-task footer follows the EXECUTOR's environment:
     v0 names `handover_report`, v1 names `tasks_report`
  3. The handover prompt pushed to an environment follows the SOURCE agent's
     environment: v0 keeps the legacy `create_agent_task` instructions, v1
     lists `agent:<uuid>` targets for `tasks_create`
  4. Feedback to a requester session at v0 keeps the `[Sub-task completed]`
     line pointing at mcp__agent_task__get_details, with no tasks_* names
     (the v1 result packet is covered in test_task_protocol_scenarios.py)

The env version is not exposed by any public API, so it is read and set on
the test DB, as test_task_report_rule.py does, right before the step that
reads them.
"""
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.core.config import settings
from app.models.environments.environment import AgentEnvironment
from tests.stubs.agent_env_stub import StubAgentEnvConnector
from tests.utils.agent import create_agent_via_api, get_agent
from tests.utils.agent_env import create_env_with_token
from tests.utils.background_tasks import drain_tasks
from tests.utils.fixtures import setup_environment_adapter, teardown_environment_adapter
from tests.utils.input_task import create_task, execute_task, get_task, set_task_status
from tests.utils.message import list_messages

_AGENT_ENV_PATCH = "app.services.sessions.message_service.agent_env_connector"
_PROTOCOL = f"{settings.API_V1_STR}/agent/tasks/protocol"


@pytest.fixture(autouse=True)
def patch_environment_adapter(tmp_path_factory):
    """Overrides the domain fixture: one persistent adapter, so the handover
    config pushed to the environment can be read back (lm._test_adapter)."""
    lm = setup_environment_adapter(tmp_path_factory, persistent_adapter=True)
    yield lm
    teardown_environment_adapter()


def _owner_id(client: TestClient, headers: dict[str, str]) -> str:
    r = client.get(f"{settings.API_V1_STR}/users/me", headers=headers)
    assert r.status_code == 200
    return r.json()["id"]


def _environment(client: TestClient, headers: dict, db: Session, agent_id: str) -> AgentEnvironment:
    env_id = get_agent(client, headers, agent_id)["active_environment_id"]
    assert env_id, "the agent must have an active environment"
    db.expire_all()
    return db.get(AgentEnvironment, UUID(env_id))


def _set_version(db: Session, environment: AgentEnvironment, version: int) -> None:
    environment.task_protocol_version = version
    db.add(environment)
    db.commit()


def test_environment_create_and_rebuild_record_protocol_version(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session,
    patch_environment_adapter,
) -> None:
    """Version 1 is recorded only when the protocol schema landed in the
    environment's core: without the kit snapshot an environment keeps the
    column default 0; with it, create records 1 and a rebuild records 1 again
    after an explicit 0."""
    from app.services.environments.platform_protocol_files import SCHEMA_FILE, kit_snapshot_dir

    headers = superuser_token_headers

    # ── no kit snapshot in the templates: nothing installed, stays 0 ───
    bare = create_agent_via_api(client, headers, name="CTP Version Bare")
    drain_tasks()
    assert _environment(client, headers, db, bare["id"]).task_protocol_version == 0

    # ── with the snapshot: create records 1 ────────────────────────────
    schema = kit_snapshot_dir(patch_environment_adapter.templates_dir) / SCHEMA_FILE
    schema.parent.mkdir(parents=True, exist_ok=True)
    schema.write_text("{}")
    agent = create_agent_via_api(client, headers, name="CTP Version Fresh")
    drain_tasks()
    environment = _environment(client, headers, db, agent["id"])
    assert environment.task_protocol_version == 1

    # An explicit 0 (an older image's done event); replacing the core
    # records 1 again.
    _set_version(db, environment, 0)
    r = client.post(f"{settings.API_V1_STR}/environments/{environment.id}/rebuild", headers=headers)
    assert r.status_code == 200, r.text
    drain_tasks()
    db.expire_all()
    assert db.get(AgentEnvironment, environment.id).task_protocol_version == 1


def test_delegated_task_footer_follows_executor_environment(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session,
) -> None:
    headers = superuser_token_headers
    executor = create_agent_via_api(client, headers, name="CTP Footer Executor")
    drain_tasks()
    environment = _environment(client, headers, db, executor["id"])

    def _first_message(version: int) -> str:
        _set_version(db, environment, version)
        r = client.post(f"{settings.API_V1_STR}/tasks/", headers=headers, json={
            "title": f"Footer v{version}", "original_message": "Summarise the inbox",
            "selected_agent_id": executor["id"], "external_ref": str(uuid4()),
            "delegation_metadata": {
                "id": "delegation-" + uuid4().hex[:8], "requester_key": "research",
                "origin_kind": "local_chat", "origin_agent_id": "requester",
                "origin_chat_id": "chat", "origin_task_id": "task",
                "depth": 1, "root": "root", "group": "research",
            },
        })
        assert r.status_code == 200, r.text
        with patch(_AGENT_ENV_PATCH, StubAgentEnvConnector(response_text="Done")):
            session_id = str(execute_task(client, headers, r.json()["id"])["session_id"])
            drain_tasks()
        users = [m["content"] for m in list_messages(client, headers, session_id) if m["role"] == "user"]
        assert users and users[0].startswith("Summarise the inbox")
        return users[0]

    legacy = _first_message(0)
    assert "call the `handover_report` tool once" in legacy
    assert "tasks_" not in legacy

    current = _first_message(1)
    assert "call `tasks_report` once" in current
    assert "handover_report" not in current
    # A delegation keeps its own footer; the generic one is not added too.
    assert "This task needs a report." not in current and "This task needs a report." not in legacy


def test_handover_prompt_follows_source_agent_environment(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session,
    patch_environment_adapter, monkeypatch: pytest.MonkeyPatch,
) -> None:
    headers = superuser_token_headers
    source = create_agent_via_api(client, headers, name="CTP Handover Source")
    target = create_agent_via_api(client, headers, name="CTP Handover Target")
    drain_tasks()
    adapter = patch_environment_adapter._test_adapter
    # sync_agent_handover_config builds its own EnvironmentLifecycleManager();
    # route it to the test manager so the push lands on the stub adapter.
    monkeypatch.setattr(
        "app.services.agents.agent_service.EnvironmentLifecycleManager",
        lambda: patch_environment_adapter,
    )
    environment = _environment(client, headers, db, source["id"])
    base = f"{settings.API_V1_STR}/agents/{source['id']}/handovers"

    # ── v0: the legacy instructions for the retired agent_task tools ───
    _set_version(db, environment, 0)
    r = client.post(base, headers=headers, json={
        "target_agent_id": target["id"], "handover_prompt": "When the draft is ready",
    })
    assert r.status_code == 200, r.text
    handover_id = r.json()["id"]
    pushed = adapter.handover_config_set
    assert pushed["handovers"] == [{
        "id": target["id"], "name": target["name"], "prompt": "When the draft is ready",
    }]
    legacy = pushed["handover_prompt"]
    assert legacy.startswith("## TASK CREATION INSTRUCTIONS")
    assert "create_agent_task" in legacy and target["id"] in legacy
    assert "[Sub-task completed]" in legacy
    assert "tasks_" not in legacy

    # ── v1: the configured pair as a tasks_create target ───────────────
    _set_version(db, environment, 1)
    r = client.put(f"{base}/{handover_id}", headers=headers,
                   json={"handover_prompt": "When the final draft is ready"})
    assert r.status_code == 200, r.text
    current = adapter.handover_config_set["handover_prompt"]
    assert current.startswith("## Configured handovers")
    assert "`tasks_create`" in current
    assert f"{target['name']} (target `agent:{target['id']}`)" in current
    assert "Trigger: When the final draft is ready" in current
    assert "create_agent_task" not in current and "[Sub-task" not in current

    # The target's own environment version plays no part.
    _set_version(db, _environment(client, headers, db, target["id"]), 0)
    r = client.put(f"{base}/{handover_id}", headers=headers,
                   json={"handover_prompt": "When the final draft is ready"})
    assert r.status_code == 200, r.text
    assert adapter.handover_config_set["handover_prompt"] == current


def test_feedback_to_pre_protocol_requester_keeps_legacy_wording(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session,
) -> None:
    headers = superuser_token_headers
    owner_id = _owner_id(client, headers)
    requester = create_agent_via_api(client, headers, name="CTP Legacy Requester")
    executor = create_agent_via_api(client, headers, name="CTP Legacy Executor")
    drain_tasks()
    host = create_task(client, headers, original_message="Host", selected_agent_id=requester["id"])
    with patch(_AGENT_ENV_PATCH, StubAgentEnvConnector(response_text="Working")):
        session_id = str(execute_task(client, headers, host["id"])["session_id"])
        drain_tasks()
    if get_task(client, headers, host["id"])["status"] != "in_progress":
        set_task_status(client, headers, host["id"], "in_progress")
    _, env_headers = create_env_with_token(db, agent_id=requester["id"], owner_id=owner_id)
    # Both environments are pre-protocol: the requester reads the old
    # feedback line, and the executor is not held to a report.
    _set_version(db, _environment(client, headers, db, requester["id"]), 0)
    _set_version(db, _environment(client, headers, db, executor["id"]), 0)

    with patch(_AGENT_ENV_PATCH, StubAgentEnvConnector(response_text="On it")):
        r = client.post(f"{_PROTOCOL}/create", headers=env_headers, json={
            "source_session_id": session_id, "title": "Legacy", "description": "Do it",
            "assignee": f"agent:{executor['id']}",
        })
        drain_tasks()
    assert r.status_code == 200, r.text
    ref = r.json()["ref"]

    feedback = [m["content"] for m in list_messages(client, headers, session_id)
                if m["role"] == "user" and ref in m["content"]]
    assert len(feedback) == 1, feedback
    text = feedback[0]
    assert text.startswith(f"[Sub-task completed] {ref} completed by {executor['name']}.")
    assert f'mcp__agent_task__get_details tool (task="{ref}")' in text
    assert "tasks_" not in text and "Task result from" not in text


def test_report_required_footer_on_automated_tasks_at_version_one(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session,
) -> None:
    """An agent-created own-queue task run on a protocol-1 environment gets
    the tasks_report footer; at version 0 it gets none, and a task the user
    opened gets none at any version."""
    headers = superuser_token_headers
    owner_id = _owner_id(client, headers)
    agent = create_agent_via_api(client, headers, name="CTP Footer Own")
    drain_tasks()
    environment = _environment(client, headers, db, agent["id"])
    host = create_task(client, headers, original_message="Host", selected_agent_id=agent["id"])
    with patch(_AGENT_ENV_PATCH, StubAgentEnvConnector(response_text="Working")):
        session_id = str(execute_task(client, headers, host["id"])["session_id"])
        drain_tasks()
    _, env_headers = create_env_with_token(db, agent_id=agent["id"], owner_id=owner_id)
    footer = "This task needs a report."

    def _run(task_id: str, version: int) -> str:
        _set_version(db, environment, version)
        with patch(_AGENT_ENV_PATCH, StubAgentEnvConnector(response_text="Done")):
            run_session = str(execute_task(client, headers, task_id)["session_id"])
            drain_tasks()
        users = [m["content"] for m in list_messages(client, headers, run_session) if m["role"] == "user"]
        assert users
        return users[0]

    def _own_task(title: str) -> str:
        r = client.post(f"{_PROTOCOL}/create", headers=env_headers, json={
            "source_session_id": session_id, "title": title, "description": f"{title} work",
        })
        assert r.status_code == 200, r.text
        return get_task(client, headers, _task_id_by_code(client, headers, r.json()["ref"]))["id"]

    current = _run(_own_task("Own v1"), 1)
    assert footer in current and "`tasks_report`" in current
    assert "delegated to you by another agent" not in current

    assert footer not in _run(_own_task("Own v0"), 0)

    user_task = create_task(client, headers, original_message="User asked",
                            selected_agent_id=agent["id"])
    assert footer not in _run(user_task["id"], 1)


def _task_id_by_code(client: TestClient, headers: dict, ref: str) -> str:
    r = client.get(f"{settings.API_V1_STR}/tasks/by-code/{ref}", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["id"]
