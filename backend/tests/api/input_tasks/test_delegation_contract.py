"""Owner- and agent-authenticated durable delegation metadata and result contract.

The delegation task contract feature is DONE and reviewed. These scenarios
pin the final contract for agent-to-agent handover on input tasks:

  1. PUT /tasks/{id}/delegation-result (owner route): requires
     ``task.external_executor`` set AND status in (in_progress, blocked);
     otherwise 409. Non-owner -> 404. A task that isn't a delegation at all
     (no delegation_metadata) -> 404.
  2. Reports (owner route and the agent's ``/agent/tasks/current/`` route)
     are rejected on a terminal (completed/error) or archived task -> 409,
     except an identical retry of the last accepted report, which returns
     the stored result unchanged (the lost-acknowledgement safety net).
     Every accepted status change goes through the normal audit trail
     (TaskStatusHistory + system comment + TASK_STATUS_CHANGED). A report
     that resumes a blocked task straight to done/failed records TWO
     transitions (blocked -> in_progress "Delegation resumed", then ->
     target). Any other invalid transition -> 400.
  3. POST /tasks/{id}/delegation-reply: a stale/unknown result_id, a result
     that isn't 'blocked', or a second reply with a different message to an
     already-answered result -> 409. Non-owner or non-delegated -> 404. A
     failed send clears the in-flight reply_state so a retry is a fresh
     attempt.
  4. After a delivered reply: delegation_result.status stays 'blocked',
     reply_state becomes 'delivered', and the task itself moves to
     in_progress. A retried IDENTICAL blocked report (lost acknowledgement)
     returns the same result id, does not reopen the task, and writes no
     new history row or comment.
  5. Contract models reject unknown fields (extra='forbid') and cap the
     length of origin_*_id / group; unit-level coverage for the pure models
     and DelegationService logic lives in tests/unit/test_delegation_contract.py.
  6. Response fields are the typed DelegationResultPublic / DelegationCapabilities
     shapes: session_id is null when no session ever backed the task.
  7. POST /tasks/ with external_ref is idempotent: a replay with identical
     delegation_metadata (including both calls having none) returns the
     existing task; a mismatch -> 409.

A parity canary for the shared status-transition/event-emission helpers (so
a delegation-only change can't silently break the plain status routes) lives
in tests/api/input_tasks/test_task_status_transitions.py.
"""
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.core.config import settings
from tests.utils.agent import create_agent_via_api
from tests.utils.agent_env import create_env_with_token
from tests.utils.background_tasks import drain_tasks
from tests.utils.environment import activate_environment, create_environment, delete_environment, list_environments
from tests.utils.input_task import archive_task, create_task, execute_task, get_task, get_task_detail, set_task_status
from tests.utils.user import create_random_user, user_authentication_headers

BASE = f'{settings.API_V1_STR}/tasks'
AGENT_BASE = f'{settings.API_V1_STR}/agent/tasks'


def _delegation_metadata(**overrides) -> dict:
    metadata = {
        'id': 'delegation-' + uuid4().hex[:8], 'requester_key': 'research',
        'origin_kind': 'local_chat', 'origin_agent_id': 'requester',
        'origin_chat_id': 'chat', 'origin_task_id': 'task',
        'depth': 1, 'root': 'root', 'group': 'research',
    }
    metadata.update(overrides)
    return metadata


def _create_delegated_task(
    client, headers, *,
    external_executor: str | None = 'Delegate Worker',
    external_ref: str | None = None,
    metadata_overrides: dict | None = None,
    **body_overrides,
) -> dict:
    body = {
        'title': 'Delegated work', 'original_message': 'Find facts',
        'external_ref': external_ref if external_ref is not None else str(uuid4()),
        'delegation_metadata': _delegation_metadata(**(metadata_overrides or {})),
    }
    if external_executor is not None:
        body['external_executor'] = external_executor
    body.update(body_overrides)
    created = client.post(f'{BASE}/', headers=headers, json=body)
    assert created.status_code == 200, created.text
    return created.json()


def _agent_report_current(client, headers, session_id, **payload):
    """POST /agent/tasks/current/delegation-result — returns the raw response."""
    body = {'source_session_id': session_id, **payload}
    return client.post(f'{AGENT_BASE}/current/delegation-result', headers=headers, json=body)


def _get_user_id(client: TestClient, headers: dict) -> str:
    r = client.get(f'{settings.API_V1_STR}/users/me', headers=headers)
    assert r.status_code == 200
    return r.json()['id']


def test_delegation_capabilities_are_versioned_and_authenticated(
    client: TestClient, normal_user_token_headers: dict[str, str]
) -> None:
    assert client.get(f'{BASE}/delegation-capabilities').status_code in (401, 403)
    capabilities = client.get(f'{BASE}/delegation-capabilities', headers=normal_user_token_headers)
    assert capabilities.status_code == 200
    body = capabilities.json()
    assert body == {'version': 1, 'metadata': True, 'structured_result': True, 'reply': True}


def test_delegation_report_gated_by_external_executor_and_active_status(
    client: TestClient, normal_user_token_headers: dict[str, str]
) -> None:
    """
    Full lifecycle of the owner route's report-gating contract:
      1. A plain (non-delegated) task -> PUT delegation-result -> 404
      2. A delegated task with NO external_executor, moved to in_progress
         -> PUT delegation-result -> 409 (not "active external work")
      3. A delegated task WITH external_executor, still 'new' -> 409
         (not in_progress/blocked yet)
      3b. Non-owner -> 404 (same task, another user)
      4. Moved to in_progress -> blocked report succeeds; audit trail
         (TaskStatusHistory + system comment) records new -> in_progress
         -> blocked; detail reflects the structured result; session_id is
         null (no platform session ever backed this external-executor task)
      5. done report succeeds -> task status becomes completed. Since the
         task was blocked, this records TWO transitions: blocked ->
         in_progress ("Delegation resumed") then -> completed.
      5b. An identical retry of the 'done' report after closure returns the
          SAME stored result (lost-acknowledgement safety net) rather than 409.
      6. A DIFFERENT report on the now-terminal task -> 409 (can't reopen)
      7. A separate delegated task, archived from in_progress -> 409
    """
    headers = normal_user_token_headers

    # ── Phase 1: not a delegation at all ────────────────────────────────
    plain = create_task(client, headers, original_message='Ordinary task')
    r = client.put(f'{BASE}/{plain["id"]}/delegation-result', headers=headers,
                    json={'status': 'done', 'summary': 'n/a'})
    assert r.status_code == 404, r.text

    # ── Phase 2: delegation without external_executor ───────────────────
    no_executor = _create_delegated_task(client, headers, external_executor=None)
    set_task_status(client, headers, no_executor['id'], 'in_progress')
    r = client.put(f'{BASE}/{no_executor["id"]}/delegation-result', headers=headers,
                    json={'status': 'done', 'summary': 'Ready'})
    assert r.status_code == 409, r.text

    # ── Phase 3: delegation with external_executor, still 'new' ─────────
    task = _create_delegated_task(client, headers)
    task_id = task['id']
    r = client.put(f'{BASE}/{task_id}/delegation-result', headers=headers,
                    json={'status': 'done', 'summary': 'Too early'})
    assert r.status_code == 409, r.text

    # ── Phase 3b: non-owner cannot report on someone else's delegation ──
    other = create_random_user(client)
    other_headers = user_authentication_headers(client=client, email=other['email'], password=other['_password'])
    foreign = client.put(f'{BASE}/{task_id}/delegation-result', headers=other_headers,
                          json={'status': 'done', 'summary': 'Not mine'})
    assert foreign.status_code == 404, foreign.text

    # ── Phase 4: in_progress -> blocked report succeeds, audit trail ────
    set_task_status(client, headers, task_id, 'in_progress')
    blocked_report = {'status': 'blocked', 'summary': 'Choose source', 'question': 'Which file?',
                       'audience': 'requester', 'body': 'Two candidates', 'artifacts': []}
    response = client.put(f'{BASE}/{task_id}/delegation-result', headers=headers, json=blocked_report)
    assert response.status_code == 200, response.text
    blocked_body = response.json()
    result_id = blocked_body['id']
    assert blocked_body['audience'] == 'requester'
    assert blocked_body['status'] == 'blocked'
    assert blocked_body['reply_state'] is None
    # No platform session ever backed this external-executor task.
    assert blocked_body['session_id'] is None

    detail = get_task_detail(client, headers, task_id)
    assert detail['status'] == 'blocked'
    assert detail['delegation_result']['id'] == result_id
    to_statuses = [h['to_status'] for h in detail['status_history']]
    assert 'in_progress' in to_statuses
    assert 'blocked' in to_statuses
    assert any('blocked' in c['content'] and c['comment_type'] == 'status_change' for c in detail['comments'])

    # ── Phase 5: done report completes the task via two transitions ─────
    history_before = len(get_task_detail(client, headers, task_id)['status_history'])
    done_report = {'status': 'done', 'summary': 'Completed', 'artifacts': []}
    response = client.put(f'{BASE}/{task_id}/delegation-result', headers=headers, json=done_report)
    assert response.status_code == 200, response.text
    done_result_id = response.json()['id']
    detail = get_task_detail(client, headers, task_id)
    assert detail['status'] == 'completed'
    new_history = detail['status_history'][history_before:]
    assert len(new_history) == 2, f"Expected a resume + target transition, got {new_history}"
    assert [h['to_status'] for h in new_history] == ['in_progress', 'completed']
    assert new_history[0]['reason'] == 'Delegation resumed'

    # ── Phase 5b: an identical retry of 'done' returns the stored result ─
    history_len = len(get_task_detail(client, headers, task_id)['status_history'])
    replay = client.put(f'{BASE}/{task_id}/delegation-result', headers=headers, json=done_report)
    assert replay.status_code == 200, replay.text
    assert replay.json()['id'] == done_result_id
    assert len(get_task_detail(client, headers, task_id)['status_history']) == history_len

    # ── Phase 6: a DIFFERENT report cannot reopen a terminal task ────────
    stale = client.put(f'{BASE}/{task_id}/delegation-result', headers=headers,
                        json={'status': 'in_progress', 'summary': 'Still going?'})
    assert stale.status_code == 409, stale.text

    # ── Phase 7: archived tasks refuse reports too ───────────────────────
    other_task = _create_delegated_task(client, headers)
    set_task_status(client, headers, other_task['id'], 'in_progress')
    archive_task(client, headers, other_task['id'])
    archived_report = client.put(f'{BASE}/{other_task["id"]}/delegation-result', headers=headers,
                                  json={'status': 'done', 'summary': 'Too late'})
    assert archived_report.status_code == 409, archived_report.text


def test_delegation_reply_conflict_and_ownership_contract(
    client: TestClient, normal_user_token_headers: dict[str, str]
) -> None:
    """
    Contract for POST /tasks/{id}/delegation-reply's error paths (no session
    needed — each rejection happens before the route ever resumes one):
      1. Owner reports 'blocked' on an active delegation
      2. A stale/unknown result_id -> 409
      3. Another user -> 404
      4. Owner replies to a result that is no longer 'blocked' -> 409
    """
    headers = normal_user_token_headers
    task = _create_delegated_task(client, headers)
    task_id = task['id']
    set_task_status(client, headers, task_id, 'in_progress')
    blocked_report = {'status': 'blocked', 'summary': 'Choose source', 'question': 'Which file?'}
    response = client.put(f'{BASE}/{task_id}/delegation-result', headers=headers, json=blocked_report)
    assert response.status_code == 200, response.text
    result_id = response.json()['id']

    # ── Stale/unknown result_id -> 409, not a soft {'delivered': False} ──
    stale_reply = client.post(f'{BASE}/{task_id}/delegation-reply', headers=headers,
                               json={'result_id': str(uuid4()), 'message': 'Stale'})
    assert stale_reply.status_code == 409, stale_reply.text

    # ── Non-owner -> 404 ─────────────────────────────────────────────────
    other = create_random_user(client)
    other_headers = user_authentication_headers(client=client, email=other['email'], password=other['_password'])
    foreign_reply = client.post(f'{BASE}/{task_id}/delegation-reply', headers=other_headers,
                                 json={'result_id': result_id, 'message': 'Not mine'})
    assert foreign_reply.status_code == 404, foreign_reply.text

    # ── Reply to a report that is no longer 'blocked' -> 409 ────────────
    done_report = {'status': 'done', 'summary': 'Resolved without a reply', 'artifacts': []}
    assert client.put(f'{BASE}/{task_id}/delegation-result', headers=headers, json=done_report).status_code == 200
    late_reply = client.post(f'{BASE}/{task_id}/delegation-reply', headers=headers,
                              json={'result_id': result_id, 'message': 'Too late'})
    assert late_reply.status_code == 409, late_reply.text


def test_delegation_contract_models_are_validated(
    client: TestClient, normal_user_token_headers: dict[str, str]
) -> None:
    """
    Contract-model validation on POST /tasks/ (all 422 — schema-level):
      1. depth out of range (>2)
      2. an unrecognized field in delegation_metadata (extra='forbid')
      3. an over-length origin_agent_id / group (max_length)
      4. a 'blocked' report with no question is still rejected (existing rule)
    Business-rule rejection (missing external_ref) stays a 400.
    """
    headers = normal_user_token_headers

    payload = {'original_message': 'Work', 'external_ref': str(uuid4()),
               'delegation_metadata': _delegation_metadata(depth=3)}
    assert client.post(f'{BASE}/', headers=headers, json=payload).status_code == 422

    payload['delegation_metadata'] = _delegation_metadata(evil_extra_field='not part of the contract')
    r = client.post(f'{BASE}/', headers=headers, json=payload)
    assert r.status_code == 422, r.text

    payload['delegation_metadata'] = _delegation_metadata(origin_agent_id='x' * 1000)
    r = client.post(f'{BASE}/', headers=headers, json=payload)
    assert r.status_code == 422, r.text

    payload['delegation_metadata'] = _delegation_metadata(group='x' * 1000)
    r = client.post(f'{BASE}/', headers=headers, json=payload)
    assert r.status_code == 422, r.text

    missing_ref = {'original_message': 'Work', 'external_ref': None,
                   'delegation_metadata': _delegation_metadata()}
    assert client.post(f'{BASE}/', headers=headers, json=missing_ref).status_code == 400

    task = _create_delegated_task(client, headers)
    set_task_status(client, headers, task['id'], 'in_progress')
    blocked_without_question = client.put(f'{BASE}/{task["id"]}/delegation-result', headers=headers,
                                          json={'status': 'blocked', 'summary': 'Waiting'})
    assert blocked_without_question.status_code == 422, blocked_without_question.text


def test_delegation_create_with_external_ref_is_idempotent_and_rejects_metadata_mismatch(
    client: TestClient, normal_user_token_headers: dict[str, str]
) -> None:
    """
    POST /tasks/ replay semantics keyed on (owner, external_ref):
      1. A plain task (no delegation_metadata) replayed with the same
         external_ref returns the SAME task (both calls "absent" match).
      2. A delegation replayed with the identical delegation_metadata
         returns the SAME task.
      3. The same external_ref with DIFFERENT delegation_metadata -> 409.
      4. The same external_ref switching from "no delegation" to "has one"
         (or the reverse) is also a mismatch -> 409.
    """
    headers = normal_user_token_headers

    # ── Plain task replay: both calls have no delegation_metadata ───────
    ref = str(uuid4())
    first = client.post(f'{BASE}/', headers=headers, json={'original_message': 'Plain', 'external_ref': ref})
    assert first.status_code == 200, first.text
    second = client.post(f'{BASE}/', headers=headers, json={'original_message': 'Plain replay', 'external_ref': ref})
    assert second.status_code == 200, second.text
    assert second.json()['id'] == first.json()['id']

    # ── Delegation replay: identical metadata returns the same task ─────
    metadata = _delegation_metadata()
    delegation_ref = str(uuid4())
    body = {'original_message': 'Delegated', 'external_ref': delegation_ref, 'delegation_metadata': metadata}
    created = client.post(f'{BASE}/', headers=headers, json=body)
    assert created.status_code == 200, created.text
    replay = client.post(f'{BASE}/', headers=headers, json=body)
    assert replay.status_code == 200, replay.text
    assert replay.json()['id'] == created.json()['id']

    # ── Same ref, DIFFERENT metadata -> 409 ──────────────────────────────
    mismatched = dict(body, delegation_metadata=_delegation_metadata(id='a-different-delegation'))
    conflict = client.post(f'{BASE}/', headers=headers, json=mismatched)
    assert conflict.status_code == 409, conflict.text

    # ── Same ref, dropping the metadata entirely -> also a mismatch ─────
    dropped = {'original_message': 'Delegated', 'external_ref': delegation_ref}
    conflict2 = client.post(f'{BASE}/', headers=headers, json=dropped)
    assert conflict2.status_code == 409, conflict2.text


def test_delegation_local_agent_session_lifecycle_pins_status_and_reply_semantics(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session,
) -> None:
    """
    A local (platform) agent-to-agent delegation, reported through the
    agent's own session via /agent/tasks/current/delegation-result:

      1. Execute the delegated task -> a session is created and linked.
         Linking a session to a delegation task is itself a status
         transition (InputTaskService.link_session special-cases
         delegations: it moves the task straight to in_progress, attributed
         to the system, "Session started" — a delegation never sits at
         'new' once an executor session exists).
      2. Agent reports 'blocked' with a question -> 200 (in_progress ->
         blocked is valid); session_id in the response is the REAL session
         id (not null, unlike the external-executor path); reply_state is
         null.
      3. Owner replies -> {'delivered': True}; task moves to in_progress,
         attributed to the owner; delegation_result.status STAYS 'blocked'
         and reply_state becomes 'delivered' (the report is not rewritten).
      4. Lost acknowledgement: the agent retries the IDENTICAL blocked
         report (same session). It gets back the SAME result id, the task
         stays in_progress (not reopened to blocked), and no new history
         row or comment is written — the question is not re-asked.
      5. A second reply to the same (now-delivered) result with a
         DIFFERENT message -> 409 (an answered question can't be answered
         twice with something else).

    Note: an "other invalid transition -> 400" case (item 2 of the module
    docstring) is not independently exercised here — for a delegation task,
    every status DelegationService.report can target (in_progress, blocked,
    done, failed) is a valid transition from every status a delegation task
    can be reporting FROM (in_progress or blocked, since link_session skips
    'new' entirely and closed statuses are already rejected as 409). The
    underlying ValidationError/400 mechanism (_apply_status_transition) is
    exhaustively covered independently in test_task_status_transitions.py.
    """
    headers = superuser_token_headers
    owner_id = _get_user_id(client, headers)

    agent = create_agent_via_api(client, headers, name="Delegation Session Agent")
    drain_tasks()
    agent_id = agent['id']

    task = _create_delegated_task(client, headers, external_executor=None, selected_agent_id=agent_id)
    task_id = task['id']

    # ── Phase 1: execute -> session created; delegation jumps to in_progress
    exec_result = execute_task(client, headers, task_id)
    session_id = str(exec_result['session_id'])
    detail = get_task_detail(client, headers, task_id)
    assert detail['status'] == 'in_progress'
    started_history = [h for h in detail['status_history'] if h['reason'] == 'Session started']
    assert len(started_history) == 1
    assert started_history[0]['changed_by_agent_id'] is None and started_history[0]['changed_by_user_id'] is None

    _, env_headers = create_env_with_token(db, agent_id=agent_id, owner_id=owner_id)

    # ── Phase 2: agent blocks on a question ──────────────────────────────
    blocked_payload = dict(status='blocked', summary='Need file choice', question='Which file?',
                            audience='requester', artifacts=[], body='')
    blocked = _agent_report_current(client, env_headers, session_id, **blocked_payload)
    assert blocked.status_code == 200, blocked.text
    result_id = blocked.json()['id']
    assert blocked.json()['session_id'] == session_id
    assert blocked.json()['reply_state'] is None
    assert get_task(client, headers, task_id)['status'] == 'blocked'

    # ── Phase 3: owner replies; the report keeps its 'blocked' status ───
    reply = client.post(f'{BASE}/{task_id}/delegation-reply', headers=headers,
                         json={'result_id': result_id, 'message': 'Use file A'})
    assert reply.status_code == 200, reply.text
    assert reply.json() == {'delivered': True, 'uncertain': False}

    detail = get_task_detail(client, headers, task_id)
    assert detail['status'] == 'in_progress'
    assert detail['delegation_result']['status'] == 'blocked'
    assert detail['delegation_result']['reply_state'] == 'delivered'
    reply_history = [h for h in detail['status_history'] if h['reason'] == 'Delegation reply delivered']
    assert len(reply_history) == 1
    assert reply_history[0]['changed_by_user_id'] == owner_id
    assert reply_history[0]['from_status'] == 'blocked' and reply_history[0]['to_status'] == 'in_progress'

    # ── Phase 4: lost acknowledgement — identical retry is a no-op ──────
    history_len = len(detail['status_history'])
    comments_len = len(get_task_detail(client, headers, task_id)['comments'])
    retried = _agent_report_current(client, env_headers, session_id, **blocked_payload)
    assert retried.status_code == 200, retried.text
    assert retried.json()['id'] == result_id
    after = get_task_detail(client, headers, task_id)
    assert after['status'] == 'in_progress', "A retried (already-answered) report must not reopen the task"
    assert len(after['status_history']) == history_len, "No new transition for an identical retry"
    assert len(after['comments']) == comments_len, "No new comment (question not re-asked) for an identical retry"

    # ── Phase 5: a different reply to the answered question conflicts ───
    second_reply = client.post(f'{BASE}/{task_id}/delegation-reply', headers=headers,
                                json={'result_id': result_id, 'message': 'Use file B instead'})
    assert second_reply.status_code == 409, second_reply.text


def test_delegation_reply_failed_send_clears_reply_state_for_retry(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session,
) -> None:
    """
    A reply's send can fail synchronously (e.g. the agent's environment goes
    away between the report and the reply) — this must not leave the
    delegation stuck in a permanent 'sending' state:

      1. Set up a blocked local-agent delegation with a real session.
      2. Delete the agent's active environment -> send_session_message's
         synchronous "Agent has no active environment" path fires.
      3. Reply -> 400; reply_state is cleared back to null (not stuck).
      4. Bring a fresh environment up and activate it.
      5. Retry the SAME reply -> now succeeds.
    """
    headers = superuser_token_headers
    owner_id = _get_user_id(client, headers)

    agent = create_agent_via_api(client, headers, name="Delegation Env Recovery Agent")
    drain_tasks()
    agent_id = agent['id']

    envs = list_environments(client, headers, agent_id)
    assert envs['count'] == 1
    default_env_id = envs['data'][0]['id']

    task = _create_delegated_task(client, headers, external_executor=None, selected_agent_id=agent_id)
    task_id = task['id']
    exec_result = execute_task(client, headers, task_id)
    session_id = str(exec_result['session_id'])

    _, env_headers = create_env_with_token(db, agent_id=agent_id, owner_id=owner_id)
    picked_up = _agent_report_current(client, env_headers, session_id, status='in_progress', summary='Starting')
    assert picked_up.status_code == 200, picked_up.text
    blocked = _agent_report_current(client, env_headers, session_id, status='blocked',
                                     summary='Need input', question='Which file?')
    assert blocked.status_code == 200, blocked.text
    result_id = blocked.json()['id']

    # ── Phase 2/3: no active environment -> reply fails, state clears ───
    delete_environment(client, headers, default_env_id)
    r_agent = client.get(f"{settings.API_V1_STR}/agents/{agent_id}", headers=headers)
    assert r_agent.json()['active_environment_id'] is None

    failed_reply = client.post(f'{BASE}/{task_id}/delegation-reply', headers=headers,
                                json={'result_id': result_id, 'message': 'Use file A'})
    assert failed_reply.status_code == 400, failed_reply.text
    assert get_task_detail(client, headers, task_id)['delegation_result']['reply_state'] is None
    assert get_task(client, headers, task_id)['status'] == 'blocked'

    # ── Phase 4: bring a new environment up and make it active ─────────
    new_env = create_environment(client, headers, agent_id)
    drain_tasks()
    activate_environment(client, headers, agent_id, new_env['id'])
    drain_tasks()

    # ── Phase 5: the identical retry now succeeds ───────────────────────
    retried_reply = client.post(f'{BASE}/{task_id}/delegation-reply', headers=headers,
                                 json={'result_id': result_id, 'message': 'Use file A'})
    assert retried_reply.status_code == 200, retried_reply.text
    assert retried_reply.json()['delivered'] is True
    assert get_task_detail(client, headers, task_id)['delegation_result']['reply_state'] == 'delivered'
    assert get_task(client, headers, task_id)['status'] == 'in_progress'
