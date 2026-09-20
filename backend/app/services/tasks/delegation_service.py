"""Durable reports and exactly addressed replies, independent of task trees."""
from datetime import datetime, UTC
from uuid import UUID, uuid4, uuid5, NAMESPACE_URL

from sqlmodel import Session as DBSession, select

from app.core.db import create_session
from app.models.sessions.session import Session
from app.models.tasks.delegation import (
    DelegationReply,
    DelegationReplyResult,
    DelegationReport,
    DelegationResultPublic,
)
from app.models.tasks.input_task import InputTask, InputTaskStatus
from app.services.sessions.session_service import SessionService
from app.services.tasks.input_task_service import (
    ConflictError,
    InputTaskService,
    TaskNotFoundError,
    ValidationError,
)


# Appended to a delegated task's first message. Without it the executor has no way to know the
# work was requested by another agent, or that a structured report — not a chat answer — is what
# travels back to the requester.
HOW_TO_REPORT = (
    "\n\n---\nThis task was delegated to you by another agent, which is waiting for a structured result. "
    "When the work is finished, call the `handover_report` tool once with status `done` (or `failed`), "
    "a one-line `summary`, and the full answer in `body`. If you cannot continue without an answer, "
    "call `handover_report` with status `blocked` and your `question` — use audience `requester` when "
    "the requesting agent can answer, `user` when only a person can — then end your turn; the reply "
    "arrives in this same conversation. Do not only answer in chat: the requester reads the report."
)

# Report status -> task status.
STATUSES = {
    'in_progress': InputTaskStatus.IN_PROGRESS,
    'blocked': InputTaskStatus.BLOCKED,
    'done': InputTaskStatus.COMPLETED,
    'failed': InputTaskStatus.ERROR,
}

# A task in one of these states has finished (or was withdrawn); a late report
# must not reopen it.
CLOSED_STATUSES = frozenset({
    InputTaskStatus.COMPLETED,
    InputTaskStatus.ERROR,
    InputTaskStatus.CANCELLED,
    InputTaskStatus.ARCHIVED,
})

# States in which an external executor (reporting through the owner route) may
# write a result: it must have picked the task up first.
EXECUTOR_REPORTABLE_STATUSES = frozenset({InputTaskStatus.IN_PROGRESS, InputTaskStatus.BLOCKED})

REPORT_FIELDS = ('status', 'summary', 'question', 'audience', 'artifacts', 'body')


def _lock_task(db_session: DBSession, task_id: UUID) -> InputTask | None:
    """Load the task under a row lock, discarding any stale identity-map copy."""
    return db_session.exec(
        select(InputTask).where(InputTask.id == task_id)
        .with_for_update().execution_options(populate_existing=True)
    ).first()


class DelegationService:
    @staticmethod
    def report(
        db_session: DBSession,
        task_id: UUID,
        data: DelegationReport,
        session_id: UUID | None = None,
        *,
        require_external_executor: bool = False,
        owner_id: UUID | None = None,
        changed_by_agent_id: UUID | None = None,
        changed_by_user_id: UUID | None = None,
    ) -> DelegationResultPublic:
        """Store a structured report and move the task to the matching status.

        ``require_external_executor`` is set by the owner route: only a task
        handed to an external executor, and already picked up by it, accepts a
        result that does not come from a platform session. ``owner_id``, when
        given, scopes the task to that owner: anyone else gets 404.
        """
        if require_external_executor and owner_id is None:
            # The owner route's only ownership check is the owner_id scope below.
            raise ValueError('require_external_executor needs owner_id')
        # Lock under the same row lock used by link_session and replies, so a
        # session started by another execution cannot interleave.
        task = _lock_task(db_session, task_id)
        if not task or (owner_id is not None and task.owner_id != owner_id):
            raise TaskNotFoundError()
        if not task.delegation_metadata:
            raise TaskNotFoundError('This task is not a delegation')
        if session_id and task.session_id and session_id != task.session_id:
            raise ValidationError('This report belongs to an earlier execution')

        report = data.model_dump(mode='json', include=set(REPORT_FIELDS))
        stored_session_id = str(session_id or task.session_id) if (session_id or task.session_id) else None
        previous = task.delegation_result or {}
        # An identical retry (lost acknowledgement) gets the stored result back,
        # even once the task has closed because of it, or once a reply to its
        # question was delivered. Only report fields and the session are
        # compared; reply_state / reply_message are the owner's, not the report's.
        if previous and all(previous.get(key) == value for key, value in report.items()) \
                and previous.get('session_id') == stored_session_id:
            return DelegationResultPublic.model_validate(previous)

        if task.status in CLOSED_STATUSES:
            raise ConflictError(f"Task is {task.status}; it no longer accepts delegation reports")
        if require_external_executor:
            if not task.external_executor:
                raise ConflictError('Only a task handed to an external executor accepts results on this route')
            if task.status not in EXECUTOR_REPORTABLE_STATUSES:
                raise ConflictError(
                    f"Task is {task.status}; an external executor can report only once it is in_progress or blocked"
                )

        target = STATUSES[data.status]
        audit = dict(
            changed_by_agent_id=changed_by_agent_id,
            changed_by_user_id=changed_by_user_id,
            changed_by_system=not (changed_by_agent_id or changed_by_user_id),
        )
        transitions: list[str] = []
        # A new report after a blocked one means the executor resumed (e.g. a
        # person answered in the conversation directly), so record that step
        # rather than rejecting blocked -> completed/error as a bad transition.
        if task.status == InputTaskStatus.BLOCKED and target in (InputTaskStatus.COMPLETED, InputTaskStatus.ERROR):
            from_status = InputTaskService._apply_status_transition(
                db_session, task, InputTaskStatus.IN_PROGRESS, reason='Delegation resumed', **audit,
            )
            transitions.append(from_status)
        from_status = InputTaskService._apply_status_transition(
            db_session, task, target, reason=f'Delegation report: {data.summary}', **audit,
        )
        if from_status is not None:
            transitions.append(from_status)

        result = {**report, 'id': str(uuid4()), 'session_id': stored_session_id}
        # Assign a new dict: in-place mutation of the JSON column is not tracked.
        task.delegation_result = result
        task.updated_at = datetime.now(UTC)
        # The structured report owns these states, so a later session-finished
        # event cannot silently erase a blocked question or a failure.
        task.error_message = data.summary if data.status == 'failed' else None
        task.completed_at = datetime.now(UTC) if data.status in ('done', 'failed') else None
        db_session.add(task)
        db_session.commit()
        db_session.refresh(task)
        DelegationService._emit_transitions(task, transitions, target, changed_by_agent_id, changed_by_user_id)
        return DelegationResultPublic.model_validate(result)

    @staticmethod
    async def reply(
        db_session: DBSession, user_id: UUID, task_id: UUID, data: DelegationReply,
    ) -> DelegationReplyResult:
        """Deliver the owner's answer to a blocked report into its task session."""
        # Serialize duplicate replies before any streaming side effect.
        task = _lock_task(db_session, task_id)
        if not task or task.owner_id != user_id or not task.delegation_metadata:
            raise TaskNotFoundError()
        result = dict(task.delegation_result or {})
        if result.get('id') != str(data.result_id):
            raise ConflictError('That result is no longer the current one; read the task for its latest result')
        if result.get('reply_state'):
            if result.get('reply_message') != data.message:
                raise ConflictError('That question already has a different reply')
            # Unknown acknowledgement is deliberately not an invitation to send
            # twice. The caller can inspect the session before another action.
            return DelegationReplyResult(
                delivered=result['reply_state'] == 'delivered',
                uncertain=result['reply_state'] == 'sending',
            )
        if result.get('status') != 'blocked':
            raise ConflictError('The current result is not a blocked question')
        session_id = result.get('session_id')
        linked = db_session.get(Session, UUID(session_id)) if session_id else None
        if not linked or linked.user_id != user_id or linked.source_task_id != task_id:
            raise ValidationError('This report has no resumable task session')

        result.update(reply_state='sending', reply_message=data.message)
        task.delegation_result = result
        db_session.add(task)
        db_session.commit()

        try:
            response = await SessionService.send_session_message(
                session_id=linked.id, user_id=user_id,
                content=f"Reply to delegation question: {result.get('question', '')}\n\n{data.message}",
                get_fresh_db_session=create_session,
                # Deterministic per result: a retry after a cleared 'sending'
                # state cannot put the same reply into the session twice.
                client_message_id=str(uuid5(NAMESPACE_URL, f'delegation-reply:{data.result_id}')),
            )
        except Exception:
            DelegationService._clear_sending(db_session, task_id, data.result_id)
            raise
        if response.get('action') == 'error':
            DelegationService._clear_sending(db_session, task_id, data.result_id)
            raise ValidationError(response.get('message') or 'The session did not accept the reply')

        # A report may have arrived while the resumed session was starting. Do
        # not overwrite a newer result with this older question's acknowledgement.
        task = _lock_task(db_session, task_id)
        latest = dict(task.delegation_result or {}) if task else {}
        if task and latest.get('id') == str(data.result_id):
            # Report fields stay exactly as reported (status remains 'blocked'),
            # so a retried blocked report still matches and is not re-applied.
            latest.update(reply_state='delivered')
            task.delegation_result = latest
            from_status = None
            if task.status == InputTaskStatus.BLOCKED:
                from_status = InputTaskService._apply_status_transition(
                    db_session, task, InputTaskStatus.IN_PROGRESS,
                    changed_by_user_id=user_id, reason='Delegation reply delivered',
                )
            task.updated_at = datetime.now(UTC)
            db_session.add(task)
            db_session.commit()
            db_session.refresh(task)
            if from_status is not None:
                InputTaskService._emit_status_changed(task, from_status, changed_by_user_id=user_id)
        return DelegationReplyResult(delivered=True)

    @staticmethod
    def _clear_sending(db_session: DBSession, task_id: UUID, result_id: UUID) -> None:
        """Release a reply that definitely failed, so the owner can retry it."""
        db_session.rollback()
        task = _lock_task(db_session, task_id)
        result = dict(task.delegation_result or {}) if task else {}
        if task and result.get('id') == str(result_id) and result.get('reply_state') == 'sending':
            result.pop('reply_state', None)
            result.pop('reply_message', None)
            task.delegation_result = result
            db_session.add(task)
        db_session.commit()

    @staticmethod
    def _emit_transitions(
        task: InputTask,
        from_statuses: list[str],
        final_status: str,
        changed_by_agent_id: UUID | None,
        changed_by_user_id: UUID | None,
    ) -> None:
        """Emit TASK_STATUS_CHANGED for each committed step, in order."""
        if not from_statuses:
            return
        # All steps but the last ended in in_progress (the resume step).
        steps = [(from_status, InputTaskStatus.IN_PROGRESS) for from_status in from_statuses[:-1]]
        steps.append((from_statuses[-1], final_status))
        for from_status, to_status in steps:
            InputTaskService._emit_status_changed(
                task, from_status, changed_by_agent_id=changed_by_agent_id,
                changed_by_user_id=changed_by_user_id, to_status=to_status,
            )
