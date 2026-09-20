"""Unit coverage for the delegation contract models and DelegationService.

The delegation task contract feature is DONE and reviewed. These tests
characterize the final ``DelegationService`` implementation directly (pure
logic, mocked DB session — no ``client``/``db`` fixtures), complementing the
API-level scenarios in ``tests/api/input_tasks/test_delegation_contract.py``
which drive the same behavior through HTTP.

Key contract points pinned here:
  - ``DelegationMetadata`` / ``DelegationReport`` / ``DelegationReply`` reject
    unknown fields (``extra='forbid'``) and cap ``origin_*_id`` / ``group``
    length; ``depth`` is 1-2; a ``blocked`` report requires a ``question``;
    artifact refs must be portable http(s) URLs.
  - ``DelegationService.report`` / ``.reply`` return TYPED pydantic models
    (``DelegationResultPublic`` / ``DelegationReplyResult``), not raw dicts —
    callers read them via attribute access.
  - A report to a task that isn't a delegation, or owned by someone else
    (when ``owner_id`` scoping is used), raises ``TaskNotFoundError`` (the
    owner route's 404). A report from a stale session raises
    ``ValidationError``.
  - ``require_external_executor=True`` (the owner route's gate) needs
    ``owner_id`` too (a caller mistake otherwise); it rejects a task with no
    ``external_executor`` or one not in an executor-reportable status, both
    as ``ConflictError``.
  - An identical retry of the last accepted report (same six report fields +
    session) returns the *stored* result unchanged, even once the task has
    closed because of it — this is the lost-acknowledgement safety net.
  - ``DelegationService.reply``: a stale/unknown ``result_id``, or a second
    reply with a different message to an already-replied result, is a
    ``ConflictError``. Non-owner or non-delegated task is ``TaskNotFoundError``.
    A session that isn't resumable for this task/user is ``ValidationError``.
    A failed send (any exception) clears the in-flight ``reply_state`` so a
    retry is a fresh attempt, not stuck "uncertain".
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from pydantic import ValidationError as ModelError

from app.models.tasks.delegation import DelegationMetadata, DelegationReport, DelegationReply
from app.services.tasks.delegation_service import DelegationService
from app.services.tasks.input_task_service import (
    ConflictError,
    TaskNotFoundError,
    ValidationError,
)


@pytest.fixture
def anyio_backend():
    return 'asyncio'


def task_fixture():
    owner, session_id, task_id = uuid4(), uuid4(), uuid4()
    task = SimpleNamespace(id=task_id, owner_id=owner, delegation_metadata={'id': 'delegation'},
                           delegation_result=None, session_id=session_id, status='in_progress',
                           short_code='DEL-1', parent_task_id=None, selected_agent_id=None,
                           executed_at=None, external_executor=None)
    session = SimpleNamespace(id=session_id, user_id=owner, source_task_id=task_id)
    db = MagicMock()
    db.exec.return_value.first.return_value = task

    # db.get() is used for two unrelated lookups along the reply path: the
    # linked Session (by session_id) and, when a status transition is
    # attributed to a user, the User for the audit comment's actor name.
    # Route by the model class's name rather than importing both classes.
    def _db_get(model, model_id):
        name = getattr(model, '__name__', '')
        if name == 'Session':
            return session
        if name == 'User':
            return SimpleNamespace(full_name='Test User', email='test@example.com')
        return None
    db.get.side_effect = _db_get
    db.session_fixture = session  # exposed so tests can mutate the linked Session directly
    return db, task


def test_metadata_rejects_unknown_fields_and_caps_origin_and_group_length():
    """extra='forbid' plus max_length on origin_*_id/group."""
    base = dict(id='one', requester_key='key', origin_kind='local_chat', depth=1, root='one')
    with pytest.raises(ModelError):
        DelegationMetadata(**base, unexpected_field='not part of the contract')
    with pytest.raises(ModelError):
        DelegationMetadata(**base, origin_agent_id='x' * 1000)
    with pytest.raises(ModelError):
        DelegationMetadata(**base, origin_chat_id='x' * 1000)
    with pytest.raises(ModelError):
        DelegationMetadata(**base, origin_task_id='x' * 1000)
    with pytest.raises(ModelError):
        DelegationMetadata(**base, group='x' * 1000)


def test_metadata_depth_and_report_question_are_validated():
    with pytest.raises(ModelError):
        DelegationMetadata(id='one', requester_key='key', origin_kind='external', depth=3, root='one')
    with pytest.raises(ModelError):
        DelegationReport(status='blocked', summary='Waiting')
    with pytest.raises(ModelError):
        DelegationReport(status='done', summary='Ready', artifacts=[{'kind': 'file', 'name': 'file', 'ref': '/workspace/report'}])


def test_report_preserves_structured_result_and_is_idempotent():
    db, task = task_fixture()
    report = DelegationReport(status='blocked', summary='Choose input', question='Which file?', audience='requester')
    first = DelegationService.report(db, task.id, report, task.session_id)
    second = DelegationService.report(db, task.id, report, task.session_id)
    assert first == second
    assert task.status == 'blocked'
    assert first.audience == 'requester'
    assert first.session_id == task.session_id
    assert db.commit.call_count == 1


def test_report_rejects_an_old_executor_session_and_non_delegated_work():
    db, task = task_fixture()
    report = DelegationReport(status='done', summary='Ready')
    with pytest.raises(ValidationError):
        DelegationService.report(db, task.id, report, uuid4())
    task.delegation_metadata = None
    with pytest.raises(TaskNotFoundError):
        DelegationService.report(db, task.id, report, task.session_id)
    db.commit.assert_not_called()


def test_report_owner_route_gating_requires_owner_id_and_active_executor_status():
    """The ``require_external_executor`` gate used by the owner route.

    ``owner_id`` is mandatory with it (a caller bug otherwise, not a user
    error), owner mismatch is a 404-equivalent, a task with no
    ``external_executor`` is rejected, and so is one whose status the
    executor hasn't picked up (or has already finished).
    """
    db, task = task_fixture()
    task.external_executor = None

    with pytest.raises(ValueError):
        DelegationService.report(db, task.id, DelegationReport(status='done', summary='x'), require_external_executor=True)

    with pytest.raises(TaskNotFoundError):
        DelegationService.report(
            db, task.id, DelegationReport(status='done', summary='x'),
            require_external_executor=True, owner_id=uuid4(),
        )

    with pytest.raises(ConflictError):
        DelegationService.report(
            db, task.id, DelegationReport(status='done', summary='x'),
            require_external_executor=True, owner_id=task.owner_id,
        )

    task.external_executor = 'Delegate Worker'
    task.status = 'new'
    with pytest.raises(ConflictError):
        DelegationService.report(
            db, task.id, DelegationReport(status='done', summary='x'),
            require_external_executor=True, owner_id=task.owner_id,
        )
    db.commit.assert_not_called()


@pytest.mark.anyio
async def test_reply_claims_before_resume_and_does_not_duplicate(monkeypatch):
    db, task = task_fixture()
    result = DelegationService.report(db, task.id, DelegationReport(status='blocked', summary='Input', question='Which file?', audience='requester'))

    async def send(**kwargs):
        assert task.delegation_result['reply_state'] == 'sending'
        assert kwargs['session_id'] == task.session_id
        assert kwargs['client_message_id']
        return {'action': 'message_sent'}
    sender = AsyncMock(side_effect=send)
    monkeypatch.setattr('app.services.sessions.session_service.SessionService.send_session_message', sender)
    reply = DelegationReply(result_id=result.id, message='README')
    first = await DelegationService.reply(db, task.owner_id, task.id, reply)
    assert first.delivered is True
    second = await DelegationService.reply(db, task.owner_id, task.id, reply)
    assert second.delivered is True
    sender.assert_awaited_once()
    # The report's own status is preserved verbatim (a retried blocked report
    # must still match it); only the task itself moves on.
    assert task.delegation_result['status'] == 'blocked'
    assert task.delegation_result['reply_state'] == 'delivered'
    assert task.status == 'in_progress'


@pytest.mark.anyio
async def test_reply_failed_send_clears_reply_state_for_retry_and_guards_replayed_or_stale_ids(monkeypatch):
    db, task = task_fixture()
    result = DelegationService.report(db, task.id, DelegationReport(status='blocked', summary='Input', question='Which file?'))
    sender = AsyncMock(side_effect=[RuntimeError('connection dropped'), {'action': 'message_sent'}])
    monkeypatch.setattr('app.services.sessions.session_service.SessionService.send_session_message', sender)
    reply = DelegationReply(result_id=result.id, message='README')

    # A failed send propagates, but clears the in-flight claim...
    with pytest.raises(RuntimeError):
        await DelegationService.reply(db, task.owner_id, task.id, reply)
    assert 'reply_state' not in (task.delegation_result or {})

    # ...so an identical retry is a fresh attempt, not stuck "uncertain".
    retried = await DelegationService.reply(db, task.owner_id, task.id, reply)
    assert retried.delivered is True
    assert sender.await_count == 2
    assert task.delegation_result['status'] == 'blocked'
    assert task.status == 'in_progress'

    # A second reply to the now-delivered result with a different message conflicts.
    with pytest.raises(ConflictError):
        await DelegationService.reply(db, task.owner_id, task.id, DelegationReply(result_id=result.id, message='Different'))

    # A stale/unknown result_id is rejected outright, not a soft {'delivered': False}.
    with pytest.raises(ConflictError):
        await DelegationService.reply(db, task.owner_id, task.id, DelegationReply(result_id=uuid4(), message='Stale'))


@pytest.mark.anyio
async def test_reply_owner_and_session_scope_are_enforced():
    db, task = task_fixture()
    result = DelegationService.report(db, task.id, DelegationReport(status='blocked', summary='Input', question='Which file?'))
    reply = DelegationReply(result_id=result.id, message='README')
    with pytest.raises(TaskNotFoundError):
        await DelegationService.reply(db, uuid4(), task.id, reply)
    # The linked Session no longer resolves back to this task.
    db.session_fixture.source_task_id = uuid4()
    with pytest.raises(ValidationError):
        await DelegationService.reply(db, task.owner_id, task.id, reply)


def test_report_reloads_session_under_row_lock_before_accepting_stale_route_object():
    db, stale = task_fixture()
    current = SimpleNamespace(**vars(stale))
    current.session_id = uuid4()
    db.exec.return_value.first.return_value = current
    with pytest.raises(ValidationError):
        DelegationService.report(db, stale.id, DelegationReport(status='done', summary='Old result'), stale.session_id)
    statement = db.exec.call_args.args[0]
    assert statement._for_update_arg is not None
    assert statement.get_execution_options()['populate_existing']
    db.commit.assert_not_called()


@pytest.mark.anyio
async def test_reply_ack_reloads_locked_current_report_instead_of_overwriting_it(monkeypatch):
    db, task = task_fixture()
    result = DelegationService.report(db, task.id, DelegationReport(status='blocked', summary='Input', question='Which file?'))
    newer = SimpleNamespace(**vars(task))
    newer.delegation_result = {'id': str(uuid4()), 'status': 'done', 'summary': 'New result'}
    newer.status = 'completed'

    async def send(**kwargs):
        db.exec.return_value.first.return_value = newer
        return {'action': 'message_sent'}
    monkeypatch.setattr('app.services.sessions.session_service.SessionService.send_session_message', AsyncMock(side_effect=send))
    reply_result = await DelegationService.reply(db, task.owner_id, task.id, DelegationReply(result_id=result.id, message='README'))
    assert reply_result.delivered is True
    statement = db.exec.call_args.args[0]
    assert statement._for_update_arg is not None
    assert statement.get_execution_options()['populate_existing']
    assert newer.delegation_result['status'] == 'done'
    assert newer.status == 'completed'
    assert db.commit.call_count == 2  # report, sending claim; newer result is untouched
