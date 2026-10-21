"""Cinna Task Protocol (CTP) v1 — the agent-environment binding.

Maps the eight protocol verbs (``tasks_targets``, ``tasks_list``, ``tasks_get``,
``tasks_create``, ``tasks_update``, ``tasks_comment``, ``tasks_report``,
``tasks_reply``) onto the existing task services. Nothing here is a second task
model: own-queue creates and delegations go through
``InputTaskService.create_task_from_agent`` / ``create_subtask``, reports and
answered questions through ``DelegationService``, comments through
``TaskCommentService``. What this layer adds is the protocol's vocabulary
(``category`` words, ``target`` strings, ``ref``), its permission rules and its
refusals.

Every call is scoped to the calling environment: the session must belong to
``ctx.owner`` and ``ctx.agent``, every task to ``ctx.owner``.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import and_, func, or_
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session as DBSession, select

from app.core.db import create_session
from app.models import Agent, AgentSubtaskCreate, AgentTaskCreate, InputTaskUpdate
from app.models.agentic_teams.agentic_team import AgenticTeamNode
from app.models.sessions.session import Session, SessionMessage
from app.models.tasks.delegation import DelegationReply, DelegationReport
from app.models.tasks.input_task import InputTask, InputTaskStatus
from app.models.tasks.task_comment import AgentTaskCommentCreate
from app.models.tasks.task_protocol import (
    TaskTarget,
    TaskTargetsPublic,
    TasksCommentRequest,
    TasksCreateRequest,
    TasksReplyRequest,
    TasksReportRequest,
    TasksUpdateRequest,
)
from app.services.sessions.session_service import SessionService
from app.services.tasks.delegation_service import DelegationService
from app.services.tasks.input_task_service import (
    ConflictError,
    InputTaskService,
    PermissionDeniedError,
    TaskNotFoundError,
    ValidationError,
)
from app.services.tasks.task_comment_service import TaskCommentService

if TYPE_CHECKING:
    # Imported lazily to avoid a module-load cycle with app.api.deps.
    from app.api.deps import AgentEnvContext

logger = logging.getLogger(__name__)

# Task status -> the protocol's category word (what agents read and write).
CATEGORY_BY_STATUS = {
    InputTaskStatus.NEW: "backlog",
    InputTaskStatus.REFINING: "backlog",
    InputTaskStatus.OPEN: "ready",
    InputTaskStatus.IN_PROGRESS: "active",
    InputTaskStatus.BLOCKED: "waiting",
    InputTaskStatus.COMPLETED: "done",
    InputTaskStatus.ERROR: "done",
    InputTaskStatus.CANCELLED: "done",
    InputTaskStatus.ARCHIVED: "done",
}

# tasks_list `category` filter -> statuses.
STATUSES_BY_CATEGORY = {
    "backlog": [InputTaskStatus.NEW, InputTaskStatus.REFINING],
    "ready": [InputTaskStatus.OPEN],
    "active": [InputTaskStatus.IN_PROGRESS],
    "waiting": [InputTaskStatus.BLOCKED],
    "done": [InputTaskStatus.COMPLETED, InputTaskStatus.ERROR, InputTaskStatus.CANCELLED],
}

CLOSED_STATUSES = frozenset({
    InputTaskStatus.COMPLETED,
    InputTaskStatus.ERROR,
    InputTaskStatus.CANCELLED,
    InputTaskStatus.ARCHIVED,
})

# tasks_update `category` -> the manual move it makes. Validated by the
# ordinary transition table, exactly as a person's move on the board.
MANUAL_MOVES = {
    "backlog": InputTaskStatus.NEW,
    "ready": InputTaskStatus.OPEN,
    "done": InputTaskStatus.COMPLETED,
    "cancelled": InputTaskStatus.CANCELLED,
}

# tasks_update closing moves that may take more than one transition, and the
# states they may start from.
PATH_MOVES = frozenset({"done", "cancelled"})
PATH_MOVE_FROM = frozenset({InputTaskStatus.NEW, InputTaskStatus.OPEN})

# States in which something else owns the task; tasks_update refuses them.
WORKING_STATUSES = frozenset({InputTaskStatus.IN_PROGRESS, InputTaskStatus.BLOCKED})

LIST_LIMIT = 50
# One depth cap spans delegations and own-queue tasks (as on Cinna Desktop),
# and a conversation may create at most this many tasks.
MAX_CHAIN_DEPTH = 2
MAX_TASKS_PER_SESSION = 20
PATH_ARTIFACT_NOTE = (
    "path artifacts are recorded by name only; attach the file with tasks_comment to share it"
)
# InputTask.original_message / current_description are 10000 characters.
MAX_TASK_TEXT = 10000
# DelegationReply.message.
MAX_ANSWER = 10000

# Refusals of `category: active`, word for word as Cinna Desktop mirrors them.
ACTIVE_WITH_ASSIGNEE_REFUSAL = "`category: active` is for your own queue; a delegation starts at once"
ALREADY_WORKING_REFUSAL = "This conversation is already working on {ref}"
REPORT_ELSEWHERE_REFUSAL = (
    "{ref} is being worked on in another conversation; only that conversation can report on it"
)

# Statuses a task may be attached from by a report: never run, not dispatched.
ATTACHABLE_STATUSES = frozenset({InputTaskStatus.NEW, InputTaskStatus.OPEN})
ATTACHING_REPORTS = frozenset({"in_progress", "blocked"})

CUSTOM_FIELDS_REFUSAL = (
    "custom_fields are not supported on cinna-core (capabilities.custom_fields is false); "
    "put those values in the description instead"
)


def _pydantic_message(error: PydanticValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(p) for p in item.get('loc', ())) or 'value'}: {item.get('msg', '')}"
        for item in error.errors()
    )


def _too_long(what: str, length: int) -> str:
    return (
        f"{what} is {length} characters; the limit here is {MAX_TASK_TEXT}. "
        "Shorten it and call again: keep the essentials in the description and put "
        "long material in a file or a tasks_comment."
    )


def _ref_of(task: InputTask) -> str:
    return task.short_code or str(task.id)


class TaskProtocolService:
    # ── Scoping ───────────────────────────────────────────────────────────

    @staticmethod
    def _session(db: DBSession, ctx: AgentEnvContext, session_id: UUID) -> Session:
        """The calling session, verified to belong to this env's owner and agent."""
        session = db.get(Session, session_id)
        if not session:
            raise ValidationError("Session not found")
        if session.user_id != ctx.owner.id or session.agent_id != ctx.agent.id:
            raise PermissionDeniedError("Session does not belong to this environment")
        return session

    @staticmethod
    def _current_task(db: DBSession, ctx: AgentEnvContext, session: Session) -> InputTask | None:
        """The task this conversation is working on (Session.source_task_id)."""
        if not session.source_task_id:
            return None
        task = db.get(InputTask, session.source_task_id)
        if not task or task.owner_id != ctx.owner.id:
            return None
        return task

    @staticmethod
    def _require_task(
        db: DBSession, ctx: AgentEnvContext, session: Session, ref: str | None,
    ) -> InputTask:
        """``ref`` when given, else the conversation's current task."""
        if ref:
            return TaskProtocolService._resolve_ref(db, ctx, ref)
        task = TaskProtocolService._current_task(db, ctx, session)
        if not task:
            raise ValidationError("This conversation is not working on a task; pass `ref`")
        return task

    @staticmethod
    def _resolve_ref(db: DBSession, ctx: AgentEnvContext, ref: str) -> InputTask:
        """A ``ref`` as this host shows it: a short code (``TASK-5``) or a task UUID."""
        ref = ref.strip()
        task: InputTask | None
        try:
            task = db.get(InputTask, UUID(ref))
        except ValueError:
            task = db.exec(
                select(InputTask).where(
                    InputTask.owner_id == ctx.owner.id,
                    InputTask.short_code == ref,
                )
            ).first()
        if not task or task.owner_id != ctx.owner.id:
            raise TaskNotFoundError(f"Task '{ref}' not found")
        return task

    @staticmethod
    def _created_by_agent(db: DBSession, task: InputTask, agent_id: UUID) -> bool:
        if not task.source_session_id:
            return False
        source = db.get(Session, task.source_session_id)
        return bool(source and source.agent_id == agent_id)

    @staticmethod
    def _relation(db: DBSession, task: InputTask, agent_id: UUID) -> str | None:
        if task.selected_agent_id == agent_id:
            return "assigned"
        if TaskProtocolService._created_by_agent(db, task, agent_id):
            return "delegated" if task.selected_agent_id else "created"
        return None

    @staticmethod
    def _summary(db: DBSession, task: InputTask, agent_id: UUID) -> dict[str, Any]:
        """One task in protocol words, as every list and tool result shows it."""
        assignee = None
        if task.selected_agent_id:
            agent = db.get(Agent, task.selected_agent_id)
            assignee = agent.name if agent else None
        parent = None
        if task.parent_task_id:
            parent_task = db.get(InputTask, task.parent_task_id)
            parent = _ref_of(parent_task) if parent_task else None
        summary: dict[str, Any] = {
            "ref": _ref_of(task),
            "title": task.title or task.current_description[:120],
            "status": task.status,
            "category": CATEGORY_BY_STATUS.get(task.status, "backlog"),
            "priority": task.priority,
            "assignee": assignee,
            "parent": parent,
            "relation": TaskProtocolService._relation(db, task, agent_id),
            "updated_at": task.updated_at.isoformat() if task.updated_at else None,
        }
        result = task.delegation_result or {}
        if result:
            summary["result"] = {
                key: result.get(key)
                for key in ("status", "summary", "question", "audience", "reply_state")
                if result.get(key) is not None
            }
        return summary

    # ── Targets ──────────────────────────────────────────────────────────

    @staticmethod
    def targets(db: DBSession, ctx: AgentEnvContext, session_id: UUID) -> TaskTargetsPublic:
        """Who this conversation may hand work to.

        Team members (other than itself) when the current task belongs to a
        team, else the owner's other active agents — the same set
        ``create_task`` resolves names against.
        """
        session = TaskProtocolService._session(db, ctx, session_id)
        current = TaskProtocolService._current_task(db, ctx, session)
        targets: list[TaskTarget] = []
        if current and current.team_id:
            nodes = db.exec(
                select(AgenticTeamNode)
                .where(AgenticTeamNode.team_id == current.team_id)
                .order_by(AgenticTeamNode.name)
            ).all()
            for node in nodes:
                if node.agent_id == ctx.agent.id:
                    continue
                agent = db.get(Agent, node.agent_id)
                targets.append(TaskTarget(
                    target=f"node:{node.id}",
                    name=node.name,
                    description=agent.description[:500] if agent and agent.description else None,
                ))
        else:
            agents = db.exec(
                select(Agent)
                .where(
                    Agent.owner_id == ctx.owner.id,
                    Agent.id != ctx.agent.id,
                    Agent.is_active == True,  # noqa: E712
                )
                .order_by(Agent.name)
            ).all()
            targets = [
                TaskTarget(
                    target=f"agent:{agent.id}",
                    name=agent.name,
                    description=agent.description[:500] if agent.description else None,
                )
                for agent in agents
            ]
        return TaskTargetsPublic(targets=targets)

    @staticmethod
    def _resolve_target(
        db: DBSession, ctx: AgentEnvContext, target: str, team_id: UUID | None,
    ) -> tuple[UUID, UUID | None, str]:
        """``(agent_id, node_id, name)`` for a ``tasks_targets`` target string.

        A bare name is still accepted, resolved as ``create_task`` always has
        (team node name first, then the owner's agent name).
        """
        target = target.strip()
        unknown = ValidationError(
            f"Unknown assignee '{target}'; call tasks_targets and pass a `target` verbatim"
        )
        kind, _, raw_id = target.partition(":")
        if kind in ("agent", "node") and raw_id:
            try:
                target_id = UUID(raw_id)
            except ValueError:
                raise unknown
            if kind == "agent":
                agent = db.get(Agent, target_id)
                # The same set tasks_targets lists: the owner's active agents.
                if not agent or agent.owner_id != ctx.owner.id or not agent.is_active:
                    raise unknown
                node_id = None
                if team_id:
                    node = db.exec(
                        select(AgenticTeamNode).where(
                            AgenticTeamNode.team_id == team_id,
                            AgenticTeamNode.agent_id == agent.id,
                        )
                    ).first()
                    node_id = node.id if node else None
                resolved = (agent.id, node_id, agent.name)
            else:
                node = db.get(AgenticTeamNode, target_id)
                if not node or node.team_id != team_id:
                    raise unknown
                resolved = (node.agent_id, node.id, node.name)
        else:
            # A bare name: accepted only when it names exactly one target, so
            # two agents (or nodes) sharing it never get the work by chance.
            ambiguous = ValidationError(
                f"More than one target is named '{target}'; call tasks_targets and pass "
                "its `agent:` or `node:` target string verbatim"
            )
            resolved = None
            if team_id:
                nodes = db.exec(
                    select(AgenticTeamNode).where(
                        AgenticTeamNode.team_id == team_id,
                        AgenticTeamNode.name == target,
                    ).limit(2)
                ).all()
                if len(nodes) > 1:
                    raise ambiguous
                if nodes:
                    resolved = (nodes[0].agent_id, nodes[0].id, nodes[0].name)
            if resolved is None:
                agents = db.exec(
                    select(Agent).where(
                        Agent.owner_id == ctx.owner.id,
                        Agent.name == target,
                        Agent.is_active == True,  # noqa: E712
                    ).limit(2)
                ).all()
                if len(agents) > 1:
                    raise ambiguous
                if not agents:
                    raise unknown
                resolved = (agents[0].id, None, agents[0].name)
        if resolved[0] == ctx.agent.id:
            raise ValidationError("You cannot delegate to yourself; omit `assignee` to queue work for yourself")
        return resolved

    # ── Read ─────────────────────────────────────────────────────────────

    @staticmethod
    def list_tasks(
        db: DBSession,
        ctx: AgentEnvContext,
        session_id: UUID,
        relation: str = "all",
        category: str | None = None,
        include_closed: bool = False,
    ) -> dict[str, Any]:
        TaskProtocolService._session(db, ctx, session_id)
        agent_sessions = select(Session.id).where(Session.agent_id == ctx.agent.id)
        assigned = InputTask.selected_agent_id == ctx.agent.id
        created = InputTask.source_session_id.in_(agent_sessions)
        delegated = and_(
            created,
            InputTask.selected_agent_id.is_not(None),
            InputTask.selected_agent_id != ctx.agent.id,
        )
        condition = {
            "assigned": assigned,
            "created": created,
            "delegated": delegated,
            "all": or_(assigned, created),
        }[relation]
        stmt = select(InputTask).where(InputTask.owner_id == ctx.owner.id, condition)
        if category:
            stmt = stmt.where(InputTask.status.in_(STATUSES_BY_CATEGORY[category]))
        elif include_closed:
            stmt = stmt.where(InputTask.status != InputTaskStatus.ARCHIVED)
        else:
            stmt = stmt.where(InputTask.status.not_in(CLOSED_STATUSES))
        tasks = db.exec(stmt.order_by(InputTask.created_at.desc()).limit(LIST_LIMIT)).all()
        return {
            "relation": relation,
            "category": category,
            "include_closed": include_closed,
            "tasks": [TaskProtocolService._summary(db, task, ctx.agent.id) for task in tasks],
        }

    @staticmethod
    async def get_task(
        db: DBSession, ctx: AgentEnvContext, session_id: UUID, ref: str,
    ) -> dict[str, Any]:
        """``get_details`` plus the protocol fields; task files land in the caller's workspace."""
        session = TaskProtocolService._session(db, ctx, session_id)
        task = TaskProtocolService._resolve_ref(db, ctx, ref)
        details = InputTaskService.get_agent_task_details(
            db_session=db, task_id=task.id, user_id=ctx.owner.id,
        )
        details = await InputTaskService.upload_task_files_to_agent_env(db, details, session.id)
        details.update(TaskProtocolService._summary(db, task, ctx.agent.id))
        if task.delegation_result:
            details["result"] = {
                key: value for key, value in task.delegation_result.items()
                if key not in ("reply_message",)
            }
        return details

    # ── Create / update / comment ────────────────────────────────────────

    @staticmethod
    def _external_ref(session_id: UUID, key: str) -> str:
        """The caller's idempotency key, scoped to its conversation (fits external_ref's 64)."""
        return f"ctp-{uuid5(NAMESPACE_URL, f'cinna-tasks:{session_id}:{key}').hex}"

    @staticmethod
    async def create(db: DBSession, ctx: AgentEnvContext, data: TasksCreateRequest) -> dict[str, Any]:
        session = TaskProtocolService._session(db, ctx, data.source_session_id)
        if data.custom_fields is not None:
            raise ValidationError(CUSTOM_FIELDS_REFUSAL)
        if data.category == "draft":
            raise ValidationError("category `draft` is only for a delegation to a folder target; cinna-core has none")
        active = data.category == "active"
        if active:
            if data.assignee and data.assignee.strip():
                raise ValidationError(ACTIVE_WITH_ASSIGNEE_REFUSAL)
            current = TaskProtocolService._current_task(db, ctx, session)
            if current:
                raise ConflictError(ALREADY_WORKING_REFUSAL.format(ref=_ref_of(current)))
        text_length = len(data.title) + len(data.description) + 2
        if text_length > MAX_TASK_TEXT:
            raise ValidationError(_too_long("title and description together", text_length))

        delegation = bool(data.assignee and data.assignee.strip())
        notes: list[str] = []
        if not delegation:
            for field in ("id", "execution", "group"):
                if getattr(data, field):
                    notes.append(f"`{field}` applies to delegations only and was ignored")
        else:
            if data.execution:
                notes.append("`execution` is not supported here and was ignored; the assignee's own settings decide")
            if data.group:
                notes.append("`group` is not supported here and was ignored; each result arrives on its own")
            if data.category:
                notes.append("`category` applies to your own queue; a delegation starts at once")

        external_ref = (
            TaskProtocolService._external_ref(session.id, data.id) if delegation and data.id else None
        )
        if external_ref:
            existing = TaskProtocolService._by_external_ref(db, ctx, external_ref)
            if existing:
                return TaskProtocolService._create_result(db, ctx, existing, False, delegation, notes)

        # Chain limits, after the idempotency check so a repeated id is
        # answered even at the cap.
        depth = TaskProtocolService._chain_depth_for(
            db, TaskProtocolService._current_task(db, ctx, session),
        ) + 1
        if depth > MAX_CHAIN_DEPTH:
            raise ValidationError(
                f"This would be depth {depth} in a task chain; the limit is {MAX_CHAIN_DEPTH}. "
                "Do the work in this conversation, or report what is needed with tasks_report."
            )
        created_here = db.exec(
            select(func.count()).select_from(InputTask).where(InputTask.source_session_id == session.id)
        ).one()
        if created_here >= MAX_TASKS_PER_SESSION:
            raise ValidationError(
                f"This conversation has created {created_here} tasks; the limit is "
                f"{MAX_TASKS_PER_SESSION} per conversation."
            )

        # The key goes into the create itself, so the row and its run start
        # together; a concurrent call with the same id is matched inside the
        # create (no second run). An IntegrityError that still escapes is the
        # same race seen from here: return the row that won.
        try:
            task, created = await TaskProtocolService._create_task(
                db, ctx, session, data, delegation, external_ref,
            )
        except IntegrityError:
            db.rollback()
            existing = (
                TaskProtocolService._by_external_ref(db, ctx, external_ref) if external_ref else None
            )
            if not existing:
                raise
            return TaskProtocolService._create_result(db, ctx, existing, False, delegation, notes)

        if created and task.chain_depth is None and not task.delegation_metadata:
            task.chain_depth = depth
            db.add(task)
            db.commit()
            db.refresh(task)

        if not delegation and data.category == "ready" and task.status == InputTaskStatus.NEW:
            task = InputTaskService.update_task_status(
                db_session=db,
                task_id=task.id,
                new_status=InputTaskStatus.OPEN,
                changed_by_agent_id=ctx.agent.id,
                reason="Created as ready",
            )
        if active:
            # The create commits on its own, so a bind that loses a race (the
            # conversation took another task meanwhile) removes the new task
            # rather than leave a stray one in Backlog.
            task_id = task.id
            try:
                task = InputTaskService.bind_session_owned(
                    db, task_id, session.id,
                    changed_by_agent_id=ctx.agent.id,
                    reason="Created as active: this conversation is working on it",
                )
            except (ConflictError, ValidationError):
                db.rollback()
                orphan = db.get(InputTask, task_id)
                if orphan:
                    InputTaskService.delete_task(db, orphan)
                raise
            result = TaskProtocolService._create_result(db, ctx, task, created, delegation, notes)
            result["attached"] = True
            result["message"] = f"Task {result['ref']} created; this conversation is working on it."
            return result
        return TaskProtocolService._create_result(db, ctx, task, created, delegation, notes)

    @staticmethod
    def _chain_depth_for(db: DBSession, task: InputTask | None) -> int:
        """Depth of the chain ``task`` sits in: 0 for a chat with no task."""
        return InputTaskService.chain_depth(db, task) if task is not None else 0

    @staticmethod
    def _by_external_ref(db: DBSession, ctx: AgentEnvContext, external_ref: str) -> InputTask | None:
        return db.exec(
            select(InputTask).where(
                InputTask.owner_id == ctx.owner.id,
                InputTask.external_ref == external_ref,
            )
        ).first()

    @staticmethod
    async def _create_task(
        db: DBSession, ctx: AgentEnvContext, session: Session, data: TasksCreateRequest,
        delegation: bool, external_ref: str | None,
    ) -> tuple[InputTask, bool]:
        """Create (or, for a keyed create that lost a race, match) the task; ``(task, created)``."""
        current = TaskProtocolService._current_task(db, ctx, session)
        priority = data.priority or "normal"

        if data.parent:
            parent = TaskProtocolService._resolve_ref(db, ctx, data.parent)
            if parent.id != (current.id if current else None) and \
                    TaskProtocolService._relation(db, parent, ctx.agent.id) is None:
                raise PermissionDeniedError("`parent` must be a task of yours")
            assigned_name = None
            if delegation:
                _agent_id, node_id, assigned_name = TaskProtocolService._resolve_target(
                    db, ctx, data.assignee, parent.team_id,
                )
                if not node_id:
                    raise ValidationError(
                        "A delegated subtask needs a member of the parent task's team as `assignee`"
                    )
            subtask, created = InputTaskService.create_subtask(
                db_session=db,
                parent_task_id=parent.id,
                creating_agent_id=ctx.agent.id,
                with_created=True,
                data=AgentSubtaskCreate(
                    title=data.title,
                    description=data.description,
                    assigned_to=assigned_name,
                    priority=priority,
                    source_session_id=session.id,
                    external_ref=external_ref,
                    own_queue=not delegation,
                ),
            )
            return subtask, created
        else:
            # Without an assignee the task goes to the calling agent's own
            # queue: assigned to it, not started.
            agent_id, node_id = ctx.agent.id, None
            if delegation:
                agent_id, node_id, _name = TaskProtocolService._resolve_target(
                    db, ctx, data.assignee, current.team_id if current else None,
                )
            task, _resolved, created = await InputTaskService.create_task_from_agent(
                db_session=db,
                user_id=ctx.owner.id,
                with_created=True,
                data=AgentTaskCreate(
                    title=data.title,
                    description=data.description,
                    priority=priority,
                    source_session_id=session.id,
                    assigned_agent_id=agent_id,
                    assigned_node_id=node_id,
                    external_ref=external_ref,
                    own_queue=not delegation,
                ),
            )
        return task, created

    @staticmethod
    def _create_result(
        db: DBSession, ctx: AgentEnvContext, task: InputTask, created: bool,
        delegation: bool, notes: list[str],
    ) -> dict[str, Any]:
        result = TaskProtocolService._summary(db, task, ctx.agent.id)
        result["created"] = created
        if notes:
            result["notes"] = notes
        if delegation:
            result["message"] = (
                f"Delegated as {result['ref']}. End your turn; the result arrives later as a "
                "message in this conversation."
            )
        else:
            result["message"] = f"Task {result['ref']} created in your queue."
        return result

    @staticmethod
    def update(db: DBSession, ctx: AgentEnvContext, data: TasksUpdateRequest) -> dict[str, Any]:
        TaskProtocolService._session(db, ctx, data.source_session_id)
        task = TaskProtocolService._resolve_ref(db, ctx, data.ref)
        if TaskProtocolService._relation(db, task, ctx.agent.id) is None:
            raise PermissionDeniedError(
                "You can edit only tasks assigned to you or created in your conversations"
            )
        if data.custom_fields is not None:
            raise ValidationError(CUSTOM_FIELDS_REFUSAL)
        if task.status in WORKING_STATUSES:
            raise ConflictError(
                f"{_ref_of(task)} is {task.status}: work on it is under way. "
                "To say how your work went, use tasks_report."
            )

        fields: dict[str, Any] = {}
        if data.title is not None:
            fields["title"] = data.title
        if data.description is not None:
            if not data.description.strip():
                raise ValidationError("description cannot be empty")
            if len(data.description) > MAX_TASK_TEXT:
                raise ValidationError(_too_long("description", len(data.description)))
            fields["current_description"] = data.description
        if data.priority is not None:
            fields["priority"] = data.priority
        if not fields and data.category is None:
            raise ValidationError("Nothing to update")

        if fields:
            task = InputTaskService.update_task(db, task, InputTaskUpdate(**fields))
        if data.category is not None:
            # A person's move. Closing a task that never started (done or
            # cancelled from new/open) walks the shortest legal path, one
            # audited step each (new -> in_progress -> completed); every other
            # move is the single transition the table allows, or a refusal.
            new_status = MANUAL_MOVES[data.category]
            path: list[str] | None = [new_status] if task.status != new_status else []
            if data.category in PATH_MOVES and task.status in PATH_MOVE_FROM:
                path = InputTaskService.status_path(task.status, new_status)
            if path is None:
                raise ValidationError(
                    f"{_ref_of(task)} is {task.status} and cannot be moved to {data.category}"
                )
            for step in path:
                task = InputTaskService.update_task_status(
                    db_session=db,
                    task_id=task.id,
                    new_status=step,
                    changed_by_agent_id=ctx.agent.id,
                    reason=f"Moved to {data.category} by agent",
                )
        return TaskProtocolService._summary(db, task, ctx.agent.id)

    @staticmethod
    def comment(db: DBSession, ctx: AgentEnvContext, data: TasksCommentRequest) -> dict[str, Any]:
        session = TaskProtocolService._session(db, ctx, data.source_session_id)
        task = TaskProtocolService._require_task(db, ctx, session, data.ref)
        # The visibility rule of tasks_list / tasks_update, not owner-wide.
        if TaskProtocolService._relation(db, task, ctx.agent.id) is None:
            raise PermissionDeniedError(
                "You can comment only on tasks assigned to you or created in your conversations"
            )
        result = TaskCommentService.add_comment_from_agent(
            db_session=db,
            task_id=task.id,
            # The caller is the author, and its workspace holds the files.
            agent_id=ctx.agent.id,
            data=AgentTaskCommentCreate(
                content=data.body,
                file_paths=data.files or None,
                source_session_id=session.id,
            ),
        )
        response: dict[str, Any] = {
            "ref": _ref_of(task),
            "comment_id": str(result.comment.id),
            "attachments": result.attachments_created,
            "failed_attachments": result.attachments_failed,
        }
        if result.attachments_failed:
            response["message"] = (
                f"{result.attachments_failed} of "
                f"{result.attachments_created + result.attachments_failed} file(s) could not be attached"
            )
        return response

    # ── Report / reply ───────────────────────────────────────────────────

    @staticmethod
    def _artifact(value: str | dict[str, Any]) -> tuple[dict[str, Any], bool]:
        """A protocol artifact as the delegation contract stores it; ``(artifact, by_name)``.

        An http(s) ref is kept as a link (or the file kind given). A
        working-folder path — a bare string or a file ref that is not a URL,
        both allowed by the schema — is not reachable by the requester, so it
        is recorded by name only (``{kind: "file", name: <path>}``, no ref) and
        ``by_name`` is True so the caller can say so. Never a refusal: a report
        must not be lost over an artifact.
        """
        if isinstance(value, str):
            value = {"ref": value}
        ref = str(value.get("ref") or "").strip()
        if ref.startswith(("https://", "http://")):
            return {
                "kind": value.get("kind") or "link",
                "name": (value.get("name") or ref.rstrip("/").rsplit("/", 1)[-1] or ref)[:500],
                "ref": ref,
            }, False
        name = ref or str(value.get("name") or "").strip() or "file"
        return {"kind": "file", "name": name[:500]}, True

    @staticmethod
    def _attachable(db: DBSession, ctx: AgentEnvContext, task: InputTask) -> bool:
        """Whether a report may bind the calling conversation to ``task``.

        The task is the caller's (assigned to it, or created by it in its own
        queue), and nothing has run or owns it: no session, no delegation, no
        external executor, not dispatched, still in Backlog or Ready.
        """
        if task.status not in ATTACHABLE_STATUSES or task.executed_at is not None:
            return False
        if task.session_owned or task.session_id or task.auto_execute:
            return False
        if task.delegation_metadata or task.external_executor:
            return False
        if task.selected_agent_id != ctx.agent.id and not (
            task.selected_agent_id is None and TaskProtocolService._created_by_agent(db, task, ctx.agent.id)
        ):
            return False
        ran = db.exec(select(Session.id).where(Session.source_task_id == task.id).limit(1)).first()
        return ran is None

    @staticmethod
    def report(db: DBSession, ctx: AgentEnvContext, data: TasksReportRequest) -> dict[str, Any]:
        """Store the executor's statement on any task assigned to this agent.

        From a conversation with no current task, an ``in_progress`` or
        ``blocked`` report on an unowned task of the caller's first binds the
        conversation to it (session-owned); ``done`` / ``failed`` never bind.
        """
        session = TaskProtocolService._session(db, ctx, data.source_session_id)
        current = TaskProtocolService._current_task(db, ctx, session)
        task = TaskProtocolService._require_task(db, ctx, session, data.ref)
        attach = (
            current is None
            and data.status in ATTACHING_REPORTS
            and TaskProtocolService._attachable(db, ctx, task)
        )
        if not attach and task.selected_agent_id != ctx.agent.id:
            raise PermissionDeniedError("You can report only on a task assigned to you")
        # A task another conversation is running (dispatched, delegated or
        # session-owned) takes reports from that conversation only. Its own
        # session's earlier executions are refused by DelegationService.
        if task.session_id and task.session_id != session.id and not (current and current.id == task.id):
            raise ConflictError(REPORT_ELSEWHERE_REFUSAL.format(ref=_ref_of(task)))

        # The person in a session-owned conversation is the one to ask.
        audience = data.audience or (
            "user" if (attach or task.session_owned)
            else "requester" if (task.delegation_metadata or task.source_session_id) else "user"
        )
        artifacts = [TaskProtocolService._artifact(item) for item in data.artifacts]
        notes = [PATH_ARTIFACT_NOTE] if any(by_name for _a, by_name in artifacts) else []
        try:
            report = DelegationReport(
                status=data.status,
                summary=data.summary,
                question=data.question,
                audience=audience,
                artifacts=[artifact for artifact, _by_name in artifacts],
                body=data.body,
            )
        except PydanticValidationError as error:
            raise ValidationError(_pydantic_message(error))

        if attach:
            task = InputTaskService.bind_session_owned(
                db, task.id, session.id,
                changed_by_agent_id=ctx.agent.id,
                reason="Taken up by this conversation's report",
            )
            current = task

        # The conversation's own task carries its session, so a report from an
        # earlier execution is refused exactly as for a delegation.
        session_id = session.id if current and current.id == task.id else None
        stored = DelegationService.report(
            db, task.id, report, session_id,
            changed_by_agent_id=ctx.agent.id,
            require_delegation=False,
        )
        result = {"ref": _ref_of(task), **stored.model_dump(mode="json")}
        if attach:
            result["attached"] = True
        if notes:
            result["notes"] = notes
        return result

    @staticmethod
    async def reply(db: DBSession, ctx: AgentEnvContext, data: TasksReplyRequest) -> dict[str, Any]:
        """Answer work this conversation created: a blocked question, or a follow-up."""
        session = TaskProtocolService._session(db, ctx, data.source_session_id)
        task = TaskProtocolService._resolve_ref(db, ctx, data.ref)
        if task.source_session_id != session.id:
            raise PermissionDeniedError("You can reply only to work this conversation created")

        result = task.delegation_result or {}
        answers_question = result.get("status") == "blocked" and (
            not result.get("reply_state") or result.get("reply_message") == data.message
        )
        if answers_question:
            if len(data.message) > MAX_ANSWER:
                raise ValidationError(f"An answer to a question is limited to {MAX_ANSWER} characters")
            outcome = await DelegationService.reply(
                db, ctx.owner.id, task.id,
                DelegationReply(result_id=UUID(result["id"]), message=data.message),
                require_delegation=False,
            )
            return {
                "ref": _ref_of(task),
                "answered_question": True,
                "delivered": outcome.delivered,
                "uncertain": outcome.uncertain,
            }

        if not task.session_id:
            raise ConflictError(f"{_ref_of(task)} has not started a conversation yet; nothing to reply to")
        linked = db.get(Session, task.session_id)
        if not linked or linked.user_id != ctx.owner.id or linked.source_task_id != task.id:
            raise ValidationError(f"{_ref_of(task)} has no resumable conversation")
        response = await SessionService.send_session_message(
            session_id=linked.id,
            user_id=ctx.owner.id,
            content=data.message,
            get_fresh_db_session=create_session,
            client_message_id=TaskProtocolService._follow_up_id(db, session, task, data.message),
        )
        if response.get("action") == "error":
            raise ValidationError(response.get("message") or "The conversation did not accept the message")
        return {"ref": _ref_of(task), "answered_question": False, "delivered": True}

    @staticmethod
    def _follow_up_id(db: DBSession, session: Session, task: InputTask, message: str) -> str:
        """Deterministic client_message_id for a tasks_reply follow-up.

        Keyed on the task, the message and the requester's current turn (its
        latest user-role message), so a retried call in the same turn is
        deduplicated by send_session_message, while the same words sent again
        in a later turn are a new message.
        """
        turn = db.exec(
            select(SessionMessage.id)
            .where(SessionMessage.session_id == session.id, SessionMessage.role == "user")
            .order_by(SessionMessage.sequence_number.desc())
            .limit(1)
        ).first()
        return str(uuid5(NAMESPACE_URL, f"cinna-tasks-reply:{task.id}:{turn}:{message}"))
