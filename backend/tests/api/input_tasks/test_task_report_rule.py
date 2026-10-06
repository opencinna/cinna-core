"""Cinna Task Protocol report rule at session end.

A session linked to a task ends (STREAM_COMPLETED) and the session-based status
computation says ``completed``. What the task becomes depends on three things:

  1. The environment's ``task_protocol_version`` (set at install/rebuild and
     by an explicit ``protocol_version`` on the env-core's ``done`` event; 0
     for an environment built before the protocol).
  2. Whether the task was handed to the agent by another agent or an
     automation (delegation_metadata, agent_initiated, source_session_id,
     a team node, or a trigger). ``auto_execute`` alone does not count: the
     UI's create dialog sets it on ordinary user tasks.
  3. Whether a final structured report (tasks_report / handover_report) is
     stored for the run.

Version >= 1 + delegated/automated + no final report -> ``blocked`` with the
system comment "Ended without reporting". Every other combination keeps the
pre-protocol auto-completion. The capabilities endpoint advertises the version
and the ``cinna`` server name.
"""
import asyncio
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.core.config import settings
from app.models import InputTask
from app.models import Session as ChatSession
from app.models.environments.environment import AgentEnvironment
from app.models.tasks.task_trigger import TaskTrigger
from app.services.sessions.message_service import MessageService
from app.services.tasks.input_task_service import InputTaskService
from tests.utils.agent import create_agent_via_api
from tests.utils.background_tasks import drain_tasks
from tests.utils.input_task import execute_task, get_task, get_task_detail

BASE = f'{settings.API_V1_STR}/tasks'


def _delegation_metadata() -> dict:
    return {
        'id': 'delegation-' + uuid4().hex[:8], 'requester_key': 'research',
        'origin_kind': 'local_chat', 'origin_agent_id': 'requester',
        'origin_chat_id': 'chat', 'origin_task_id': 'task',
        'depth': 1, 'root': 'root', 'group': 'research',
    }


def _run_task(
    client: TestClient, headers: dict, db: Session, *,
    protocol_version: int, delegated: bool = False, **task_fields,
) -> tuple[str, str]:
    """Create and execute a task, set its environment's protocol version and
    leave its session ended; returns (task_id, session_id)."""
    agent = create_agent_via_api(client, headers, name=f'Report Rule {uuid4().hex[:6]}')
    drain_tasks()
    body = {'title': 'Report rule', 'original_message': 'Do the work', 'selected_agent_id': agent['id']}
    if delegated:
        body |= {'external_ref': str(uuid4()), 'delegation_metadata': _delegation_metadata()}
    created = client.post(f'{BASE}/', headers=headers, json=body)
    assert created.status_code == 200, created.text
    task_id = created.json()['id']
    session_id = str(execute_task(client, headers, task_id)['session_id'])
    drain_tasks()

    task = db.get(InputTask, UUID(task_id))
    for field, value in task_fields.items():
        setattr(task, field, value)
    task.status = 'in_progress'
    db.add(task)
    chat = db.get(ChatSession, UUID(session_id))
    assert chat.environment_id, 'the executed session must run on an environment'
    chat.status = 'completed'
    chat.interaction_status = ''
    db.add(chat)
    environment = db.get(AgentEnvironment, chat.environment_id)
    environment.task_protocol_version = protocol_version
    db.add(environment)
    db.commit()
    return task_id, session_id


def _end_session(session_id: str) -> None:
    asyncio.run(InputTaskService.handle_stream_completed({'meta': {'session_id': session_id}}))
    drain_tasks()


def _ended_without_reporting(client: TestClient, headers: dict, task_id: str) -> bool:
    detail = get_task_detail(client, headers, task_id)
    return any('Ended without reporting' in comment['content'] for comment in detail['comments'])


def test_delegated_task_on_protocol_env_blocks_without_report(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session,
) -> None:
    headers = superuser_token_headers
    task_id, session_id = _run_task(client, headers, db, protocol_version=1, delegated=True)

    _end_session(session_id)

    assert get_task(client, headers, task_id)['status'] == 'blocked'
    assert _ended_without_reporting(client, headers, task_id)
    detail = get_task_detail(client, headers, task_id)
    blocked = [h for h in detail['status_history'] if h['to_status'] == 'blocked']
    assert blocked and blocked[-1]['reason'] == 'Ended without reporting'
    assert blocked[-1]['changed_by_agent_id'] is None and blocked[-1]['changed_by_user_id'] is None


def test_automated_task_on_protocol_env_blocks_without_report(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session,
) -> None:
    headers = superuser_token_headers
    task_id, session_id = _run_task(
        client, headers, db, protocol_version=1, agent_initiated=True,
    )

    _end_session(session_id)

    assert get_task(client, headers, task_id)['status'] == 'blocked'
    assert _ended_without_reporting(client, headers, task_id)


def test_triggered_task_on_protocol_env_blocks_without_report(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session,
) -> None:
    headers = superuser_token_headers
    task_id, session_id = _run_task(client, headers, db, protocol_version=1)
    task = db.get(InputTask, UUID(task_id))
    db.add(TaskTrigger(task_id=task.id, owner_id=task.owner_id, type='webhook', name='Hook'))
    db.commit()

    _end_session(session_id)

    assert get_task(client, headers, task_id)['status'] == 'blocked'


def test_user_opened_task_on_protocol_env_still_completes(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session,
) -> None:
    headers = superuser_token_headers
    # auto_execute is what the UI's create dialog sends by default.
    task_id, session_id = _run_task(client, headers, db, protocol_version=1, auto_execute=True)

    _end_session(session_id)

    assert get_task(client, headers, task_id)['status'] == 'completed'
    assert not _ended_without_reporting(client, headers, task_id)


def test_delegated_task_on_pre_protocol_env_still_completes(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session,
) -> None:
    headers = superuser_token_headers
    task_id, session_id = _run_task(client, headers, db, protocol_version=0, delegated=True)

    _end_session(session_id)

    assert get_task(client, headers, task_id)['status'] == 'completed'
    assert not _ended_without_reporting(client, headers, task_id)


def test_reported_task_on_protocol_env_is_not_blocked(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session,
) -> None:
    """A stored done report settles the task; the session end leaves it alone."""
    headers = superuser_token_headers
    task_id, session_id = _run_task(client, headers, db, protocol_version=1, delegated=True)
    task = db.get(InputTask, UUID(task_id))
    task.delegation_result = {
        'id': str(uuid4()), 'status': 'done', 'summary': 'Finished', 'session_id': session_id,
    }
    task.status = 'completed'
    db.add(task)
    db.commit()

    _end_session(session_id)

    assert get_task(client, headers, task_id)['status'] == 'completed'
    assert not _ended_without_reporting(client, headers, task_id)


def test_in_progress_report_does_not_count_as_final(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session,
) -> None:
    """An in_progress report says nothing about how the run ended."""
    headers = superuser_token_headers
    task_id, session_id = _run_task(
        client, headers, db, protocol_version=1, delegated=True,
        delegation_result={'id': str(uuid4()), 'status': 'in_progress', 'summary': 'Working'},
    )

    _end_session(session_id)

    assert get_task(client, headers, task_id)['status'] == 'blocked'


def test_done_event_records_environment_protocol_version(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session,
) -> None:
    """The env-core's done event sets the version; a done without the field changes nothing."""
    headers = superuser_token_headers
    _, session_id = _run_task(client, headers, db, protocol_version=0)
    environment_id = db.get(ChatSession, UUID(session_id)).environment_id

    class _Reuse:
        def __enter__(self) -> Session:
            return db

        def __exit__(self, *exc: object) -> None:
            return None

    asyncio.run(MessageService._record_task_protocol_version(
        {'type': 'done', 'protocol_version': 1}, environment_id, _Reuse,
    ))
    db.expire_all()
    assert db.get(AgentEnvironment, environment_id).task_protocol_version == 1

    # No protocol_version (an error path, a cancelled stream): no information,
    # so a stored 1 survives rather than switching the report rule off.
    asyncio.run(MessageService._record_task_protocol_version(
        {'type': 'done'}, environment_id, _Reuse,
    ))
    db.expire_all()
    assert db.get(AgentEnvironment, environment_id).task_protocol_version == 1

    # An explicit 0 is still an announcement.
    asyncio.run(MessageService._record_task_protocol_version(
        {'type': 'done', 'protocol_version': 0}, environment_id, _Reuse,
    ))
    db.expire_all()
    assert db.get(AgentEnvironment, environment_id).task_protocol_version == 0


def test_delegation_capabilities_advertise_task_protocol(
    client: TestClient, normal_user_token_headers: dict[str, str],
) -> None:
    response = client.get(f'{BASE}/delegation-capabilities', headers=normal_user_token_headers)
    assert response.status_code == 200
    body = response.json()
    assert body['protocol_version'] == 1
    assert body['server'] == 'cinna'
    assert body['version'] == 1 and body['structured_result'] is True
