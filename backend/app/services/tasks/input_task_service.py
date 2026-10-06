"""
Input Task Service - handles task creation, retrieval, updates, and status management.
"""
from pathlib import Path
from uuid import UUID
from datetime import UTC, datetime
import logging
from typing import Any, Optional
from sqlmodel import Session as DBSession, select
from sqlalchemy import and_, or_
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy.exc import IntegrityError

from app.models import (
    InputTask,
    InputTaskCreate,
    InputTaskUpdate,
    InputTaskPublic,
    InputTaskPublicExtended,
    InputTaskDetailPublic,
    InputTaskStatus,
    Agent,
    ChannelAccessPolicy,
    Session,
    SessionMessage,
    SessionCreate,
    SessionSender,
    TaskStatusHistory,
    TaskStatusHistoryPublic,
    TaskComment,
    TaskCommentPublic,
    TaskAttachment,
    TaskAttachmentPublic,
)
from app.models.events.event import EventType
from app.models.files.file_upload import FileUpload, FileUploadPublic, InputTaskFile
from app.models.agentic_teams.agentic_team import AgenticTeamNode
from app.models.environments.environment import AgentEnvironment
from app.models.tasks.task_trigger import TaskTrigger
from app.core.db import create_session
from app.utils import create_task_with_error_logging
from app.services.sessions.session_service import SessionService
from app.services.sessions.channel_ingestion_service import (
    ChannelIngestionService,
    NoActiveEnvironmentError,
)
from app.services.events.event_service import event_service
from app.services.ai_functions.ai_functions_service import AIFunctionsService

logger = logging.getLogger(__name__)

# System comment reason when a protocol environment's session ends on delegated
# or automated work without a final tasks_report (see requires_report).
ENDED_WITHOUT_REPORTING = "Ended without reporting"

# How far chain_depth follows origins when nothing is recorded; the cap is 2.
CHAIN_DEPTH_WALK_LIMIT = 5

# Session outcome (SESSION_STATE_UPDATED / stream end) -> result packet status,
# used when the executor stored no tasks_report.
RESULT_STATUS_BY_SESSION_STATE = {
    "completed": "done",
    "needs_input": "blocked",
    "error": "failed",
}
# A result packet's body is a turn in the requester's conversation; the rest
# stays on the task (tasks_get).
RESULT_PACKET_BODY_LIMIT = 8000


class InputTaskError(Exception):
    """Base exception for input task service errors."""

    def __init__(self, message: str, status_code: int = 400):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class TaskNotFoundError(InputTaskError):
    """Task not found."""

    def __init__(self, message: str = "Task not found"):
        super().__init__(message, status_code=404)


class AgentNotFoundError(InputTaskError):
    """Agent not found."""

    def __init__(self, message: str = "Agent not found"):
        super().__init__(message, status_code=404)


class PermissionDeniedError(InputTaskError):
    """Permission denied."""

    def __init__(self, message: str = "Not enough permissions"):
        super().__init__(message, status_code=400)


class ValidationError(InputTaskError):
    """Validation error."""

    def __init__(self, message: str):
        super().__init__(message, status_code=400)


class ConflictError(InputTaskError):
    """The request is well-formed but conflicts with the task's current state."""

    def __init__(self, message: str):
        super().__init__(message, status_code=409)


class InputTaskService:
    # ==================== Helper Methods ====================

    @staticmethod
    def verify_agent_access(
        db_session: DBSession,
        agent_id: UUID,
        user_id: UUID,
        require_active_environment: bool = False,
    ) -> Agent:
        """
        Verify agent exists and user has access to it.

        Args:
            db_session: Database session
            agent_id: Agent UUID to verify
            user_id: User ID requesting access
            require_active_environment: If True, verify agent has active environment

        Returns:
            Agent instance if valid

        Raises:
            AgentNotFoundError: If agent doesn't exist
            PermissionDeniedError: If user doesn't own the agent
            ValidationError: If agent has no active environment (when required)
        """
        agent = db_session.get(Agent, agent_id)
        if not agent:
            raise AgentNotFoundError()
        if agent.owner_id != user_id:
            raise PermissionDeniedError("Not enough permissions for this agent")
        if require_active_environment and not agent.active_environment_id:
            raise ValidationError("Selected agent has no active environment")
        return agent

    @staticmethod
    def get_task_with_ownership_check(
        db_session: DBSession,
        task_id: UUID,
        user_id: UUID,
    ) -> InputTask:
        """
        Get task and verify ownership.

        Args:
            db_session: Database session
            task_id: Task UUID
            user_id: User ID requesting access

        Returns:
            InputTask instance if valid

        Raises:
            TaskNotFoundError: If task doesn't exist
            PermissionDeniedError: If user doesn't own the task
        """
        task = db_session.get(InputTask, task_id)
        if not task:
            raise TaskNotFoundError()
        if task.owner_id != user_id:
            raise PermissionDeniedError()
        return task

    @staticmethod
    def parse_status_filter(status: str | None) -> list[str] | None:
        """
        Parse status filter string into list of statuses.

        Args:
            status: Filter string - "active", "completed", "archived", "all", or specific status

        Returns:
            List of status values to filter by, or None for no filter
        """
        if not status or status == "all":
            return None

        if status == "active":
            return [
                InputTaskStatus.NEW,
                InputTaskStatus.REFINING,
                InputTaskStatus.OPEN,
                InputTaskStatus.IN_PROGRESS,
                InputTaskStatus.BLOCKED,
                InputTaskStatus.ERROR,
            ]
        elif status == "completed":
            return [InputTaskStatus.COMPLETED]
        elif status == "archived":
            return [InputTaskStatus.ARCHIVED]
        else:
            # Single status filter
            return [status]

    @staticmethod
    def parse_workspace_filter(
        user_workspace_id: str | None,
    ) -> tuple[UUID | None, bool]:
        """
        Parse workspace filter parameter.

        Args:
            user_workspace_id: Workspace filter string
                - None: no filter
                - "": filter for default workspace (NULL)
                - UUID string: filter for that workspace

        Returns:
            Tuple of (workspace_uuid, apply_filter)

        Raises:
            ValidationError: If workspace ID format is invalid
        """
        if user_workspace_id is None:
            return None, False
        elif user_workspace_id == "":
            return None, True
        else:
            try:
                return UUID(user_workspace_id), True
            except ValueError:
                raise ValidationError("Invalid workspace ID format")

    @staticmethod
    def get_task_extended(
        db_session: DBSession,
        task_id: UUID,
        user_id: UUID,
    ) -> InputTaskPublicExtended:
        """
        Get task with extended info (agent name, sessions count, attached files).

        Args:
            db_session: Database session
            task_id: Task UUID
            user_id: User ID requesting access

        Returns:
            InputTaskPublicExtended with full task details

        Raises:
            TaskNotFoundError: If task doesn't exist
            PermissionDeniedError: If user doesn't own the task
        """
        result = InputTaskService.get_task_with_agent(db_session=db_session, task_id=task_id)
        if not result:
            raise TaskNotFoundError()

        task, agent_name = result

        if task.owner_id != user_id:
            raise PermissionDeniedError()

        sessions_count, latest_session_id = SessionService.get_task_sessions_info(
            db_session=db_session, task_id=task_id
        )

        # Get attached files
        attached_files = InputTaskService.get_task_files(db_session=db_session, task_id=task_id)
        attached_files_public = [FileUploadPublic.model_validate(f) for f in attached_files]

        # Resolve parent and root short codes
        parent_short_code = None
        root_short_code = None
        if task.parent_task_id:
            parent = db_session.get(InputTask, task.parent_task_id)
            if parent:
                parent_short_code = parent.short_code
            # Walk up to root (max 20 levels to avoid infinite loops)
            current = task
            for _ in range(20):
                if not current.parent_task_id:
                    break
                current = db_session.get(InputTask, current.parent_task_id)
                if not current:
                    break
            if current and current.id != task.id:
                root_short_code = current.short_code

        return InputTaskPublicExtended(
            **task.model_dump(),
            agent_name=agent_name,
            sessions_count=sessions_count,
            latest_session_id=latest_session_id,
            attached_files=attached_files_public,
            parent_short_code=parent_short_code,
            root_short_code=root_short_code,
        )

    @staticmethod
    def list_tasks_extended(
        db_session: DBSession,
        user_id: UUID,
        status: str | None = None,
        user_workspace_id: str | None = None,
        skip: int = 0,
        limit: int = 100,
        root_only: bool = False,
        team_id: UUID | None = None,
        priority: str | None = None,
        updated_since: datetime | None = None,
        updated_since_id: UUID | None = None,
    ) -> tuple[list[InputTaskPublicExtended], int]:
        """
        List user's tasks with extended info.

        Args:
            db_session: Database session
            user_id: User ID
            status: Status filter string
            user_workspace_id: Workspace filter string
            skip: Number of records to skip
            limit: Number of records to return
            updated_since: Incremental-sync cursor (see ``list_tasks``)

        Returns:
            Tuple of (list of extended tasks, total count)

        Raises:
            ValidationError: If workspace ID format is invalid
        """
        status_filter = InputTaskService.parse_status_filter(status)
        workspace_filter, apply_workspace_filter = InputTaskService.parse_workspace_filter(
            user_workspace_id
        )

        results, count = InputTaskService.list_tasks(
            db_session=db_session,
            user_id=user_id,
            status_filter=status_filter,
            user_workspace_id=workspace_filter,
            apply_workspace_filter=apply_workspace_filter,
            skip=skip,
            limit=limit,
            root_only=root_only,
            team_id=team_id,
            priority=priority,
            updated_since=updated_since,
            updated_since_id=updated_since_id,
        )

        # Batch-resolve parent short codes
        parent_ids = {task.parent_task_id for task, _ in results if task.parent_task_id}
        parent_short_codes: dict[UUID, str] = {}
        if parent_ids:
            parent_rows = db_session.exec(
                select(InputTask.id, InputTask.short_code).where(
                    InputTask.id.in_(parent_ids)  # type: ignore[union-attr]
                )
            ).all()
            parent_short_codes = {pid: sc for pid, sc in parent_rows if sc}

        data = []
        for task, agent_name in results:
            sessions_count, latest_session_id = SessionService.get_task_sessions_info(
                db_session=db_session, task_id=task.id
            )
            subtask_progress = InputTaskService.get_subtask_progress(db_session, task.id)
            data.append(
                InputTaskPublicExtended(
                    **task.model_dump(),
                    agent_name=agent_name,
                    sessions_count=sessions_count,
                    latest_session_id=latest_session_id,
                    subtask_count=subtask_progress["total"],
                    subtask_completed_count=subtask_progress["completed"],
                    parent_short_code=parent_short_codes.get(task.parent_task_id) if task.parent_task_id else None,
                )
            )

        return data, count

    @staticmethod
    def list_tasks_by_source_session(
        db_session: DBSession,
        source_session_id: UUID,
        user_id: UUID,
    ) -> tuple[list[InputTaskPublicExtended], int]:
        """
        List all tasks created from a specific source session.

        Args:
            db_session: Database session
            source_session_id: Source session UUID
            user_id: User ID (for ownership verification)

        Returns:
            Tuple of (list of extended tasks, total count)
        """
        statement = (
            select(InputTask, Agent.name)
            .outerjoin(Agent, InputTask.selected_agent_id == Agent.id)
            .where(
                InputTask.source_session_id == source_session_id,
                InputTask.owner_id == user_id,
            )
            .order_by(InputTask.created_at.desc())
        )

        results = db_session.exec(statement).all()

        data = []
        for task, agent_name in results:
            sessions_count, latest_session_id = SessionService.get_task_sessions_info(
                db_session=db_session, task_id=task.id
            )

            # Join session result_state/result_summary
            result_state = None
            result_summary = None
            if task.session_id:
                task_session = db_session.get(Session, task.session_id)
                if task_session:
                    result_state = task_session.result_state
                    result_summary = task_session.result_summary

            data.append(
                InputTaskPublicExtended(
                    **task.model_dump(),
                    agent_name=agent_name,
                    sessions_count=sessions_count,
                    latest_session_id=latest_session_id,
                    result_state=result_state,
                    result_summary=result_summary,
                )
            )

        return data, len(data)

    # ==================== CRUD Operations ====================

    @staticmethod
    def _generate_short_code(
        db_session: DBSession,
        owner_id: UUID,
        team_id: UUID | None = None,
    ) -> tuple[str, int]:
        """
        Atomically generate a short code for a new task.

        Increments the user's task_sequence_counter and returns the new counter
        along with the formatted short code (e.g., "TASK-42" or "HR-42").

        Args:
            db_session: Database session
            owner_id: User UUID who owns the task
            team_id: Optional team UUID (to look up team prefix)

        Returns:
            Tuple of (short_code string, sequence_number int)
        """
        from sqlalchemy import text
        from app.models.users.user import User

        # Atomically increment and fetch the new counter
        result = db_session.exec(
            text(
                'UPDATE "user" SET task_sequence_counter = task_sequence_counter + 1 '
                'WHERE id = CAST(:uid AS uuid) RETURNING task_sequence_counter'
            ).bindparams(uid=str(owner_id))
        ).first()

        if result is None:
            # Fallback: read current value
            user = db_session.get(User, owner_id)
            counter = (user.task_sequence_counter if user else 0) + 1
        else:
            counter = result[0]

        # Determine prefix
        prefix = "TASK"
        if team_id:
            from app.models.agentic_teams.agentic_team import AgenticTeam
            team = db_session.get(AgenticTeam, team_id)
            if team and team.task_prefix:
                prefix = team.task_prefix.upper()

        short_code = f"{prefix}-{counter}"
        return short_code, counter

    _EXTERNAL_REF_INDEX = "ix_input_task_owner_external_ref"

    @staticmethod
    def create_task(
        db_session: DBSession,
        user_id: UUID,
        data: InputTaskCreate,
    ) -> InputTask:
        """
        Create a new input task with auto-generated short code.

        Thin wrapper over ``create_task_idempotent`` for the callers that do not
        care whether a row was created or matched. Any caller that performs a
        **side effect** on the new task — scheduling execution, notifying a
        parent — must use ``create_task_idempotent`` instead and gate that side
        effect on ``created``: deduplicating the row while re-firing the effect
        is worse than the duplicate the key exists to prevent.

        Args:
            db_session: Database session
            user_id: User ID creating the task
            data: Task creation data (including agent_initiated, auto_execute, source_session_id, file_ids)
        """
        task, _created = InputTaskService.create_task_idempotent(
            db_session=db_session,
            user_id=user_id,
            data=data,
        )
        return task

    @staticmethod
    def create_task_idempotent(
        db_session: DBSession,
        user_id: UUID,
        data: InputTaskCreate,
    ) -> tuple[InputTask, bool]:
        """
        Create a task, or return the one this owner already created under the
        same ``external_ref``.

        Returns:
            ``(task, created)``. ``created`` is False when an existing row was
            matched by ``external_ref`` — the caller must then skip anything it
            would have done *because* the task is new. A retry after a lost
            response has to be indistinguishable from the first call on the
            wire **and** in its effects.

        Raises:
            IntegrityError: propagated for any constraint failure other than
                the external_ref collision this method handles.
        """
        # An empty or whitespace-only ref is "no ref". It must not reach the
        # database: '' IS NOT NULL, so the partial unique index covers it, and
        # a second create under '' would be a constraint violation rather than
        # an ordinary unconstrained create.
        external_ref = (getattr(data, 'external_ref', None) or '').strip() or None
        delegation_metadata = (
            data.delegation_metadata.model_dump(mode='json') if data.delegation_metadata else None
        )

        # Idempotency: a caller that supplies an external_ref it already used
        # gets its first task back, not a second one.
        if external_ref is not None:
            existing = db_session.exec(
                select(InputTask).where(
                    InputTask.owner_id == user_id,
                    InputTask.external_ref == external_ref,
                )
            ).first()
            if existing:
                # A replay must describe the same delegation (or the same lack
                # of one). Handing a plain task back to a delegation create, or
                # the reverse, would silently drop the reporting contract.
                if existing.delegation_metadata != delegation_metadata:
                    raise ConflictError(
                        'external_ref is already used by a task with different delegation metadata'
                    )
                return existing, False

        if delegation_metadata and (not external_ref or data.parent_task_id):
            raise ValidationError('A delegation requires external_ref and must not use parent_task_id')

        # Generate short code
        short_code, sequence_number = InputTaskService._generate_short_code(
            db_session=db_session,
            owner_id=user_id,
            team_id=data.team_id if hasattr(data, 'team_id') else None,
        )

        # Derive title from original_message if not provided
        title = getattr(data, 'title', None)
        if not title:
            first_line = data.original_message.strip().split('\n')[0]
            title = first_line[:500] if len(first_line) > 500 else first_line

        # Auto-assign team lead agent when team is selected but no agent specified
        selected_agent_id = data.selected_agent_id
        assigned_node_id = getattr(data, 'assigned_node_id', None)
        team_id = getattr(data, 'team_id', None)
        if team_id and not selected_agent_id and not assigned_node_id:
            lead_node = db_session.exec(
                select(AgenticTeamNode).where(
                    AgenticTeamNode.team_id == team_id,
                    AgenticTeamNode.is_lead == True,  # noqa: E712
                )
            ).first()
            if lead_node:
                selected_agent_id = lead_node.agent_id
                assigned_node_id = lead_node.id

        # Inherit workspace from the assigned agent when caller did not specify one
        user_workspace_id = data.user_workspace_id
        if user_workspace_id is None and selected_agent_id is not None:
            agent = db_session.get(Agent, selected_agent_id)
            if agent is not None:
                user_workspace_id = agent.user_workspace_id

        task = InputTask(
            owner_id=user_id,
            original_message=data.original_message,
            current_description=data.original_message,  # Start with same as original
            selected_agent_id=selected_agent_id,
            user_workspace_id=user_workspace_id,
            agent_initiated=data.agent_initiated,
            auto_execute=data.auto_execute and not data.external_executor,
            source_session_id=data.source_session_id,
            status=InputTaskStatus.NEW,
            refinement_history=[],
            short_code=short_code,
            sequence_number=sequence_number,
            title=title,
            priority=getattr(data, 'priority', 'normal') or 'normal',
            team_id=team_id,
            assigned_node_id=assigned_node_id,
            parent_task_id=getattr(data, 'parent_task_id', None),
            external_ref=external_ref,
            delegation_metadata=delegation_metadata,
            external_executor=data.external_executor,
        )
        db_session.add(task)
        try:
            db_session.commit()
        except IntegrityError as exc:
            # Two concurrent retries of the same create raced past the lookup
            # above. The unique index settled it; return whichever row won.
            #
            # Narrowed to *this* index on purpose: any other constraint failure
            # (a short_code collision, say) must keep its own shape rather than
            # be unwound here and re-raised as something else.
            if not InputTaskService._is_external_ref_collision(exc):
                raise
            db_session.rollback()
            if external_ref is not None:
                winner = db_session.exec(
                    select(InputTask).where(
                        InputTask.owner_id == user_id,
                        InputTask.external_ref == external_ref,
                    )
                ).first()
                if winner:
                    return winner, False
            raise
        db_session.refresh(task)

        # Attach files if provided
        if data.file_ids:
            InputTaskService.attach_files_to_task(
                db_session=db_session,
                task_id=task.id,
                file_ids=data.file_ids,
                user_id=user_id,
            )

        return task, True

    @staticmethod
    def _touch(db_session: DBSession, task_id: UUID) -> None:
        """Bump the task's ``updated_at`` for an activity write on a side table.

        See ``app/services/tasks/task_touch.py`` for why this exists and why it
        must ride the caller's transaction rather than commit its own.
        """
        from app.services.tasks.task_touch import touch_task
        touch_task(db_session, task_id)

    @staticmethod
    def _is_external_ref_collision(exc: IntegrityError) -> bool:
        """True if this IntegrityError is the external_ref unique index firing."""
        orig = getattr(exc, "orig", None)
        constraint = getattr(getattr(orig, "diag", None), "constraint_name", None)
        if constraint:
            return constraint == InputTaskService._EXTERNAL_REF_INDEX
        # No structured diagnostics (older driver, wrapped error) — fall back
        # to the index name in the message rather than swallowing everything.
        return InputTaskService._EXTERNAL_REF_INDEX in str(orig or exc)

    @staticmethod
    def create_task_with_auto_refine(
        db_session: DBSession,
        user_id: UUID,
        data: InputTaskCreate,
    ) -> tuple[InputTask, str]:
        """
        Create a new input task with optional auto-refinement.

        If the selected agent has a refiner_prompt configured, the task description
        will be automatically refined before returning.

        Args:
            db_session: Database session
            user_id: User ID creating the task
            data: Task creation data

        Returns:
            Tuple of (task, message_to_send) where message_to_send is the
            possibly-refined task description ready for execution.
        """

        # Create the task first
        task = InputTaskService.create_task(
            db_session=db_session,
            user_id=user_id,
            data=data,
        )
        return task, InputTaskService._auto_refine(db_session, user_id, task, data)

    @staticmethod
    def _auto_refine(
        db_session: DBSession,
        user_id: UUID,
        task: InputTask,
        data: InputTaskCreate,
    ) -> str:
        """Refine a just-created task for its agent; returns the message to send."""
        message_to_send = data.original_message

        # External clients own their mirror's description and execution.
        if task.external_executor:
            return task.current_description

        # Auto-refine if agent has refiner_prompt
        if data.selected_agent_id:
            agent = db_session.get(Agent, data.selected_agent_id)
            from app.models.users.user import User
            user = db_session.get(User, user_id) if user_id else None
            if agent and agent.refiner_prompt and AIFunctionsService.is_available(user):
                try:
                    refine_result = AIFunctionsService.refine_task(
                        db=db_session,
                        current_description=data.original_message,
                        user_comment="Auto-refine for task execution",
                        agent_id=data.selected_agent_id,
                        owner_id=user_id,
                        refinement_history=None,
                        user_selected_text=None,
                    )

                    if refine_result.get("success") and refine_result.get("refined_description"):
                        message_to_send = refine_result["refined_description"]

                        # Update task description
                        task.current_description = message_to_send
                        task.updated_at = datetime.now(UTC)
                        db_session.add(task)
                        db_session.commit()
                        db_session.refresh(task)

                        # Append to refinement history
                        InputTaskService.append_to_refinement_history(
                            db_session=db_session,
                            task=task,
                            role="ai",
                            content=f"Auto-refined: {refine_result.get('feedback_message', 'Task refined for agent execution')}",
                        )

                        logger.info(f"Task {task.id} auto-refined")
                    else:
                        logger.warning(f"Auto-refine failed or returned no result: {refine_result.get('error')}")

                except Exception as e:
                    logger.warning(f"Auto-refine failed for task {task.id}: {e}")
                    # Continue with original message if refinement fails

        return message_to_send

    @staticmethod
    async def create_task_from_agent(
        db_session: DBSession,
        user_id: UUID,
        data,  # AgentTaskCreate
        *,
        with_created: bool = False,
    ) -> tuple[InputTask, str | None] | tuple[InputTask, str | None, bool]:
        """
        Create a task from an agent MCP tool call.

        With ``with_created=True`` the result gains a third item: False when a
        keyed create (``external_ref``) matched an existing row — a retry or
        the loser of a concurrent insert — and nothing was started.

        Resolves session context, agent name, and team inheritance, then creates
        and optionally auto-executes the task.

        Args:
            db_session: Database session
            user_id: Owner user ID (from agent auth token)
            data: AgentTaskCreate with title, description, assigned_to, priority,
                  source_session_id

        Returns:
            Tuple of (task, resolved_agent_name)

        Raises:
            ValidationError: If source session is provided but not found
        """
        from app.models.agentic_teams.agentic_team import AgenticTeamNode as _AgenticTeamNode
        from app.services.sessions.message_service import MessageService

        source_session_id = data.source_session_id
        user_workspace_id = None
        team_id = None

        # Resolve source session context
        if source_session_id:
            source_session = db_session.get(Session, source_session_id)
            if not source_session:
                raise ValidationError("Source session not found")
            user_workspace_id = source_session.user_workspace_id

            # Inherit team context from the calling session's task.
            # Use Session.source_task_id (immutable) instead of
            # InputTask.session_id which is overwritten on re-execution.
            if source_session.source_task_id:
                current_task = db_session.get(InputTask, source_session.source_task_id)
                if current_task and current_task.team_id:
                    team_id = current_task.team_id

        # Resolve assigned_to (name → agent_id)
        selected_agent_id = None
        assigned_node_id = None
        resolved_name = None

        if getattr(data, "assigned_agent_id", None):
            # Exact assignee from a task-protocol target: no name lookup.
            agent = db_session.get(Agent, data.assigned_agent_id)
            if not agent or agent.owner_id != user_id:
                raise ValidationError("Assignee not found")
            selected_agent_id = agent.id
            resolved_name = agent.name
            node_id = getattr(data, "assigned_node_id", None)
            if node_id:
                node = db_session.get(_AgenticTeamNode, node_id)
                if not node or node.team_id != team_id or node.agent_id != agent.id:
                    raise ValidationError("Assignee is not a member of this task's team")
                assigned_node_id = node.id
                resolved_name = node.name
        elif data.assigned_to:
            if team_id:
                # Team context: try node name first
                target_node = db_session.exec(
                    select(_AgenticTeamNode).where(
                        _AgenticTeamNode.team_id == team_id,
                        _AgenticTeamNode.name == data.assigned_to,
                    )
                ).first()
                if target_node:
                    selected_agent_id = target_node.agent_id
                    assigned_node_id = target_node.id
                    resolved_name = target_node.name

            if not selected_agent_id:
                # Fallback: resolve by agent name across user's agents
                agent = db_session.exec(
                    select(Agent).where(
                        Agent.owner_id == user_id,
                        Agent.name == data.assigned_to,
                    )
                ).first()
                if agent:
                    selected_agent_id = agent.id
                    resolved_name = agent.name

        # Build task message from title + description
        message = data.title
        if data.description:
            message = f"{data.title}\n\n{data.description}"

        # Own queue (tasks_create without an assignee): assigned to the
        # calling agent but never started here; a person runs it.
        own_queue = bool(getattr(data, "own_queue", False))
        start_now = bool(selected_agent_id) and not own_queue

        task_data = InputTaskCreate(
            original_message=message,
            selected_agent_id=selected_agent_id,
            user_workspace_id=user_workspace_id,
            agent_initiated=True,
            auto_execute=start_now,
            source_session_id=source_session_id,
            title=data.title,
            priority=data.priority,
            team_id=team_id,
            assigned_node_id=assigned_node_id,
            external_ref=getattr(data, "external_ref", None),
        )

        if task_data.external_ref:
            # A keyed create (tasks_create with an id): the row is created or
            # matched in one step, and a matched row — a retry, or a concurrent
            # call that lost the insert race — fires none of the side effects.
            task, created = InputTaskService.create_task_idempotent(
                db_session=db_session, user_id=user_id, data=task_data,
            )
            if not created:
                return (task, resolved_name, False) if with_created else (task, resolved_name)

        if start_now:
            if task_data.external_ref:
                message_to_send = InputTaskService._auto_refine(
                    db_session, user_id, task, task_data,
                )
            else:
                task, message_to_send = InputTaskService.create_task_with_auto_refine(
                    db_session=db_session,
                    user_id=user_id,
                    data=task_data,
                )

            # Auto-execute: create session and send message
            success, new_session, error = await InputTaskService.execute_task(
                db_session=db_session,
                task=task,
                user_id=user_id,
                message_to_send=message_to_send,
            )

            if not success:
                raise ValidationError(error or "Failed to execute task")

            # Post system message to source session
            if source_session_id:
                MessageService.create_message(
                    session=db_session,
                    session_id=source_session_id,
                    role="system",
                    content=f"Task {task.short_code} created and assigned to {resolved_name}",
                    message_metadata={
                        "task_created": True,
                        "task_id": str(task.id),
                        "target_agent_name": resolved_name,
                        "session_id": str(new_session.id) if new_session else None,
                    },
                )
        else:
            # Inbox task (no agent assigned)
            if not task_data.external_ref:
                task = InputTaskService.create_task(
                    db_session=db_session,
                    user_id=user_id,
                    data=task_data,
                )

            if source_session_id:
                MessageService.create_message(
                    session=db_session,
                    session_id=source_session_id,
                    role="system",
                    content=(
                        f"Task {task.short_code} added to {resolved_name}'s queue"
                        if own_queue else f"Task {task.short_code} created in inbox"
                    ),
                    message_metadata={
                        "task_created": True,
                        "task_id": str(task.id),
                        "inbox_task": True,
                    },
                )

        return (task, resolved_name, True) if with_created else (task, resolved_name)

    @staticmethod
    async def execute_task(
        db_session: DBSession,
        task: InputTask,
        user_id: UUID,
        message_to_send: str | None = None,
        mode: str = "conversation",
        file_ids: list[UUID] | None = None,
    ) -> tuple[bool, Session | None, str | None]:
        """
        Execute a task by creating a session and sending the message.

        This method:
        1. Creates a session for the task's selected agent
        2. Links the session to the task
        3. Sends the message to initiate the agent

        Args:
            db_session: Database session
            task: The task to execute
            user_id: User ID executing the task
            message_to_send: The message to send (defaults to task.current_description)
            mode: Session mode (conversation, building)
            file_ids: Optional file IDs to attach to the message

        Returns:
            Tuple of (success, session, error_message)
        """
        db_session.refresh(task)
        if task.external_executor:
            return False, None, (
                f"Task is executed by {task.external_executor}. "
                "Finish external work and clear external_executor before executing in cinna."
            )
        if not task.selected_agent_id:
            return False, None, "Task has no selected agent"

        content = message_to_send or task.current_description

        agent = db_session.get(Agent, task.selected_agent_id)
        if agent is None:
            InputTaskService.update_status(
                db_session=db_session,
                task=task,
                status=InputTaskStatus.ERROR,
                error_message="Selected agent not found",
            )
            return False, None, "Selected agent not found"

        # Create session for target agent
        session_title = f"Task: {content[:50]}..." if len(content) > 50 else f"Task: {content}"

        sender = SessionSender.from_task_execution(
            user_id=user_id,
            task_id=task.id,
            task_name=session_title,
        )

        # Report footer, after the title is derived so the session is still
        # named after the work. Chosen by the executor environment's protocol
        # version: a delegation gets its contract footer at any version; any
        # other task the report rule applies to (requires_report would say yes
        # for a run on this environment) gets the tasks_report footer at >= 1.
        executor_env = (
            db_session.get(AgentEnvironment, agent.active_environment_id)
            if agent.active_environment_id else None
        )
        executor_version = executor_env.task_protocol_version if executor_env else 0
        if task.delegation_metadata:
            from app.services.tasks.delegation_service import how_to_report
            content = f"{content}{how_to_report(executor_version)}"
        elif (
            executor_version >= 1
            and not InputTaskService.has_final_report(task)
            and InputTaskService.is_automated(db_session, task)
        ):
            from app.services.tasks.delegation_service import REPORT_REQUIRED_FOOTER
            content = f"{content}{REPORT_REQUIRED_FOOTER}"

        try:
            ingestion = await ChannelIngestionService.ingest_inbound_message(
                db=db_session,
                agent=agent,
                sender=sender,
                thread_key=None,
                content=content,
                file_ids=file_ids,
                integration_type="task",
                access_policy=ChannelAccessPolicy(
                    expected_owner_id=user_id,
                ),
                get_fresh_db_session=create_session,
                extra_session_kwargs={
                    "title": session_title,
                    "mode": mode,
                    "source_task_id": task.id,
                    "session_metadata_extra": {"task_id": str(task.id)},
                },
            )
        except ValidationError as exc:
            return False, None, exc.message
        except NoActiveEnvironmentError as exc:
            # Mirrors today's "no session created" path: SessionService.create_session
            # returned None when the agent had no active environment.
            InputTaskService.update_status(
                db_session=db_session,
                task=task,
                status=InputTaskStatus.ERROR,
                error_message="Failed to create session for agent",
            )
            logger.warning(
                f"Task {task.id}: failed to create session for agent {agent.id}: {exc}"
            )
            return False, None, "Failed to create session for agent"

        new_session = ingestion.session

        # Link session to task
        InputTaskService.link_session(
            db_session=db_session,
            task=task,
            session_id=new_session.id,
        )

        if ingestion.action == "error":
            error_msg = ingestion.message
            InputTaskService.update_status(
                db_session=db_session,
                task=task,
                status=InputTaskStatus.ERROR,
                error_message=f"Failed to send message: {error_msg}",
            )
            return False, None, f"Failed to send message: {error_msg}"

        logger.info(f"Task {task.id} executed: session {new_session.id} created")
        return True, new_session, None

    @staticmethod
    def get_task(db_session: DBSession, task_id: UUID) -> Optional[InputTask]:
        """Get task by ID"""
        return db_session.get(InputTask, task_id)

    @staticmethod
    def get_task_with_agent(
        db_session: DBSession, task_id: UUID
    ) -> Optional[tuple[InputTask, str | None]]:
        """Get task with agent name"""
        statement = (
            select(InputTask, Agent.name)
            .outerjoin(Agent, InputTask.selected_agent_id == Agent.id)
            .where(InputTask.id == task_id)
        )
        result = db_session.exec(statement).first()
        if not result:
            return None
        return result  # (task, agent_name)

    @staticmethod
    def list_tasks(
        db_session: DBSession,
        user_id: UUID,
        status_filter: list[str] | None = None,
        user_workspace_id: UUID | None = None,
        apply_workspace_filter: bool = False,
        skip: int = 0,
        limit: int = 100,
        order_desc: bool = True,
        root_only: bool = False,
        team_id: UUID | None = None,
        priority: str | None = None,
        updated_since: datetime | None = None,
        updated_since_id: UUID | None = None,
    ) -> tuple[list[tuple[InputTask, str | None]], int]:
        """
        List user's tasks with agent names.

        Args:
            db_session: Database session
            user_id: User ID
            status_filter: Optional list of statuses to filter by
            user_workspace_id: Optional workspace filter
            apply_workspace_filter: If True, filter by workspace (None means default workspace)
            skip: Number of records to skip
            limit: Number of records to return
            order_desc: Order by created_at descending
            updated_since: Incremental-sync cursor — only tasks whose updated_at
                is strictly newer. Switches the ordering to
                ``(updated_at asc, id asc)``, because a cursor needs a stable
                ascending order to page through; without the param the existing
                ``created_at`` list order stands.

                Paging: advance the cursor, do not use ``skip``. The sort key is
                mutable, so a row already returned that is updated again moves to
                the tail and shifts an unread row back into the consumed window —
                offset paging would skip it entirely. Read a page, take the last
                row's ``updated_at`` and ``id`` as ``updated_since`` and
                ``updated_since_id`` respectively, and repeat until a page
                comes back short.

        Returns:
            Tuple of (list of (task, agent_name) tuples, total count)
        """
        # Base query with outer join to get agent name
        statement = (
            select(InputTask, Agent.name)
            .outerjoin(Agent, InputTask.selected_agent_id == Agent.id)
            .where(InputTask.owner_id == user_id)
        )

        # Apply status filter
        if status_filter:
            statement = statement.where(InputTask.status.in_(status_filter))

        # Apply workspace filter
        if apply_workspace_filter:
            statement = statement.where(InputTask.user_workspace_id == user_workspace_id)

        # Apply new filters
        if root_only:
            statement = statement.where(InputTask.parent_task_id == None)  # noqa: E711
        if team_id:
            statement = statement.where(InputTask.team_id == team_id)
        if priority:
            statement = statement.where(InputTask.priority == priority)
        if updated_since_id is not None and updated_since is None:
            raise ValidationError("updated_since_id requires updated_since")
        if updated_since is not None:
            cursor_filter = InputTask.updated_at > updated_since
            if updated_since_id is not None:
                cursor_filter = or_(
                    cursor_filter,
                    and_(
                        InputTask.updated_at == updated_since,
                        InputTask.id > updated_since_id,
                    ),
                )
            statement = statement.where(cursor_filter)

        # Get count before pagination
        count_statement = statement.with_only_columns(InputTask.id)

        # Add ordering
        if updated_since is not None:
            # Sync order, not list order: ascending by the cursor column, with
            # id as a tiebreak so the sort is total — updated_at is not unique,
            # and two rows written in the same transaction would otherwise page
            # in an arbitrary order that can change between requests.
            statement = statement.order_by(
                InputTask.updated_at.asc(), InputTask.id.asc()
            )
        elif order_desc:
            statement = statement.order_by(InputTask.created_at.desc())
        else:
            statement = statement.order_by(InputTask.created_at.asc())

        # Add pagination
        statement = statement.offset(skip).limit(limit)

        results = db_session.exec(statement).all()
        count = len(db_session.exec(count_statement).all())

        return results, count

    @staticmethod
    def update_task(
        db_session: DBSession,
        task: InputTask,
        data: InputTaskUpdate,
    ) -> InputTask:
        """
        Update task fields.

        Args:
            db_session: Database session
            task: Task to update
            data: Update data
        """
        update_dict = data.model_dump(exclude_unset=True)
        marker_changed = False
        if "external_executor" in update_dict:
            # Serialize ownership changes with SessionService.create_session's
            # task lock. Its lock covers the session INSERT and commit, so an
            # execute already in flight cannot slip past this session check.
            task = db_session.exec(
                select(InputTask)
                .where(InputTask.id == task.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            ).one()
            executor = update_dict["external_executor"]
            marker_changed = executor != task.external_executor
            if marker_changed:
                if task.external_executor and task.status in {
                    InputTaskStatus.IN_PROGRESS, InputTaskStatus.BLOCKED,
                    InputTaskStatus.REFINING,
                }:
                    raise ValidationError(
                        "Finish or cancel external work before changing external_executor"
                    )
                if executor is not None:
                    existing_session = db_session.exec(
                        select(Session.id).where(Session.source_task_id == task.id)
                    ).first()
                    if task.session_id is not None or existing_session is not None:
                        raise ValidationError(
                            "Cannot set external_executor on a task with cinna sessions"
                        )
                    update_dict["auto_execute"] = False
                    # A legacy sessionless mirror may already have reported
                    # in_progress before markers existed. It was never cinna work.
                    task.executed_at = None
        task.sqlmodel_update(update_dict)
        task.updated_at = datetime.now(UTC)

        db_session.add(task)
        db_session.commit()
        db_session.refresh(task)
        if marker_changed:
            create_task_with_error_logging(
                event_service.emit_event(
                    event_type=EventType.TASK_UPDATED,
                    model_id=task.id,
                    user_id=task.owner_id,
                    meta={
                        "task_id": str(task.id),
                        "short_code": task.short_code,
                        "parent_task_id": str(task.parent_task_id) if task.parent_task_id else None,
                        "external_executor": task.external_executor,
                    },
                ),
                task_name=f"emit_task_updated_{task.id}",
            )
        return task

    @staticmethod
    def update_description(
        db_session: DBSession,
        task: InputTask,
        new_description: str,
    ) -> InputTask:
        """Update task description"""
        task.current_description = new_description
        task.updated_at = datetime.now(UTC)

        db_session.add(task)
        db_session.commit()
        db_session.refresh(task)
        return task

    @staticmethod
    def update_status(
        db_session: DBSession,
        task: InputTask,
        status: str,
        error_message: str | None = None,
    ) -> InputTask:
        """Update task status"""
        task.status = status
        task.updated_at = datetime.now(UTC)

        if error_message:
            task.error_message = error_message
        elif status != InputTaskStatus.ERROR:
            task.error_message = None

        # Update timestamps based on status
        if (
            status == InputTaskStatus.IN_PROGRESS
            and task.executed_at is None
            and not task.external_executor
        ):
            task.executed_at = datetime.now(UTC)
        elif status == InputTaskStatus.COMPLETED:
            task.completed_at = datetime.now(UTC)
        elif status == InputTaskStatus.ARCHIVED:
            task.archived_at = datetime.now(UTC)

        db_session.add(task)
        db_session.commit()
        db_session.refresh(task)

        # Emit TASK_STATUS_UPDATED for all tasks (handlers filter by meta as needed)

        create_task_with_error_logging(
            event_service.emit_event(
                event_type=EventType.TASK_STATUS_UPDATED,
                model_id=task.id,
                user_id=task.owner_id,
                meta={
                    "new_status": status,
                }
            ),
            task_name=f"emit_task_status_changed_{task.id}"
        )

        return task

    @staticmethod
    def append_to_refinement_history(
        db_session: DBSession,
        task: InputTask,
        role: str,
        content: str,
    ) -> InputTask:
        """
        Append a message to refinement history.

        Args:
            db_session: Database session
            task: Task to update
            role: "user" or "ai"
            content: Message content
        """
        history_item = {
            "role": role,
            "content": content,
            "timestamp": datetime.now(UTC).isoformat(),
        }

        if task.refinement_history is None:
            task.refinement_history = []

        task.refinement_history.append(history_item)
        flag_modified(task, "refinement_history")
        task.updated_at = datetime.now(UTC)

        db_session.add(task)
        db_session.commit()
        db_session.refresh(task)
        return task

    @staticmethod
    def link_session(
        db_session: DBSession,
        task: InputTask,
        session_id: UUID,
    ) -> InputTask:
        """Link a session to the task and transition to in_progress.

        Sets session_id first, then delegates to update_task_status so the
        transition gets a TaskStatusHistory entry and system comment.
        """
        task = db_session.exec(select(InputTask).where(InputTask.id == task.id)
                               .with_for_update().execution_options(populate_existing=True)).first()
        if not task:
            raise TaskNotFoundError()
        if task.delegation_metadata:
            # Session identity, cleared report and execution state must become
            # visible together, in one commit under the row lock, so a report
            # from the new session cannot land between them and be overwritten.
            from_status = InputTaskService._apply_status_transition(
                db_session, task, InputTaskStatus.IN_PROGRESS,
                changed_by_system=True, reason="Session started",
            )
            task.session_id = session_id
            task.delegation_result = None
            task.error_message = None
            task.completed_at = None
            task.updated_at = datetime.now(UTC)
            if task.executed_at is None and not task.external_executor:
                task.executed_at = datetime.now(UTC)
            db_session.add(task)
            db_session.commit()
            db_session.refresh(task)
            if from_status is not None:
                InputTaskService._emit_status_changed(task, from_status)
            return task

        task.session_id = session_id
        task.delegation_result = None
        db_session.add(task)
        db_session.commit()
        db_session.refresh(task)

        # Transition via update_task_status for audit trail (history + comment).
        # If task is already in_progress (re-execute), this is a no-op.
        if task.status != InputTaskStatus.IN_PROGRESS:
            InputTaskService.update_task_status(
                db_session=db_session,
                task_id=task.id,
                new_status=InputTaskStatus.IN_PROGRESS,
                changed_by_system=True,
                reason="Session started",
            )
            db_session.refresh(task)

        return task

    @staticmethod
    def reset_task_if_no_sessions(db_session: DBSession, task_id: UUID) -> InputTask | None:
        """
        Reset task to 'new' status if no sessions remain linked to it.

        Called after a session is deleted. If the task has no remaining sessions,
        it means there are no results, so the task should revert to 'new'.

        Args:
            db_session: Database session
            task_id: Task UUID

        Returns:
            Updated task if reset, None if task not found or still has sessions
        """
        task = db_session.get(InputTask, task_id)
        if not task:
            return None

        # Only reset tasks that are in execution phase
        execution_statuses = [
            InputTaskStatus.IN_PROGRESS,
            InputTaskStatus.BLOCKED,
            InputTaskStatus.COMPLETED,
            InputTaskStatus.ERROR,
        ]
        if task.status not in execution_statuses:
            return None

        # Check if any sessions remain
        remaining_count = db_session.exec(
            select(Session).where(Session.source_task_id == task_id)
        ).first()

        if remaining_count is not None:
            # Still has sessions - recompute status instead
            return InputTaskService.sync_task_status_from_sessions(db_session, task_id)

        # No sessions remain - reset to 'new'
        task.status = InputTaskStatus.NEW
        task.session_id = None
        task.todo_progress = None
        task.error_message = None
        task.executed_at = None
        task.completed_at = None
        task.updated_at = datetime.now(UTC)

        db_session.add(task)
        db_session.commit()
        db_session.refresh(task)

        logger.info(f"Task {task_id} reset to NEW (all sessions deleted)")
        return task

    @staticmethod
    def delete_task(db_session: DBSession, task: InputTask) -> None:
        """Delete a task"""
        db_session.delete(task)
        db_session.commit()

    # ==================== Business Logic Operations ====================

    @staticmethod
    def refine_task(
        db_session: DBSession,
        task: InputTask,
        user_id: UUID,
        user_comment: str,
        user_selected_text: str | None = None,
    ) -> dict[str, Any]:
        """
        Refine a task description with AI assistance.

        Orchestrates the full refinement flow:
        1. Updates status to REFINING if NEW
        2. Appends user comment to history
        3. Calls AI refinement service
        4. Updates description with refined version
        5. Appends AI response to history

        Args:
            db_session: Database session
            task: Task to refine
            user_id: User ID performing refinement
            user_comment: User's refinement comment/feedback
            user_selected_text: Optional selected text for targeted refinement

        Returns:
            Dict with success, refined_description, feedback_message, or error
        """

        db_session.refresh(task)
        if task.external_executor:
            raise ValidationError(
                "Task is executed externally; finish external work and clear "
                "external_executor before refining in cinna"
            )

        # Set status to refining if new
        if task.status == InputTaskStatus.NEW:
            InputTaskService.update_status(
                db_session=db_session, task=task, status=InputTaskStatus.REFINING
            )

        # Append user comment to history
        InputTaskService.append_to_refinement_history(
            db_session=db_session, task=task, role="user", content=user_comment
        )

        # Call AI refinement service
        result = AIFunctionsService.refine_task(
            db=db_session,
            current_description=task.current_description,
            user_comment=user_comment,
            agent_id=task.selected_agent_id,
            owner_id=user_id,
            refinement_history=task.refinement_history,
            user_selected_text=user_selected_text,
        )

        if not result.get("success"):
            return {
                "success": False,
                "error": result.get("error", "Failed to refine task"),
            }

        # Update task description with refined version
        refined_description = result.get("refined_description", "")
        feedback_message = result.get("feedback_message", "")

        InputTaskService.update_description(
            db_session=db_session, task=task, new_description=refined_description
        )

        # Append AI response to history
        InputTaskService.append_to_refinement_history(
            db_session=db_session, task=task, role="ai", content=feedback_message
        )

        return {
            "success": True,
            "refined_description": refined_description,
            "feedback_message": feedback_message,
        }

    # ==================== Status Sync Methods ====================
    # Compute and sync task status from connected sessions

    @staticmethod
    def compute_status_from_sessions(db_session: DBSession, task_id: UUID) -> str | None:
        """
        Compute task status from all connected sessions and subtasks.

        Status computation logic (priority order):
        - If ANY session has status='error' → ERROR
        - If ANY session has unanswered tool_questions → BLOCKED
        - If ANY session is active with interaction_status='running' → IN_PROGRESS
        - If ALL sessions are completed AND all subtasks are completed → COMPLETED
        - Otherwise → IN_PROGRESS (active but not streaming, or subtasks pending)

        Args:
            db_session: Database session
            task_id: Task UUID

        Returns:
            Computed status string, or None if no sessions connected
        """
        # Query all sessions connected to this task
        sessions = db_session.exec(
            select(Session).where(Session.source_task_id == task_id)
        ).all()

        if not sessions:
            return None

        has_error = False
        has_pending_input = False
        has_running = False
        all_completed = True

        for session in sessions:
            # Check for error status (session-level or agent-reported)
            if session.status == "error" or session.result_state == "error":
                has_error = True
                continue

            # Check agent-reported needs_input state
            if session.result_state == "needs_input":
                has_pending_input = True

            # Check if session is not completed
            if session.status != "completed":
                # Idle active sessions (not streaming, no pending work) don't block
                # task completion — only actively running sessions do
                is_idle = (
                    session.status == "active"
                    and session.interaction_status == ""
                    and session.result_state != "needs_input"
                )
                if not is_idle:
                    all_completed = False
            elif session.result_state == "needs_input":
                # Session stream completed but agent is waiting for input
                all_completed = False

            # Check for running interaction
            if session.interaction_status == "running":
                has_running = True

            # Check for unanswered tool questions in this session
            unanswered_count = db_session.exec(
                select(SessionMessage)
                .where(SessionMessage.session_id == session.id)
                .where(SessionMessage.tool_questions_status == "unanswered")
            ).first()

            if unanswered_count:
                has_pending_input = True

        # Check subtask completion — incomplete subtasks block task completion
        if all_completed:
            subtask_progress = InputTaskService.get_subtask_progress(db_session, task_id)
            if subtask_progress["total"] > 0 and subtask_progress["completed"] < subtask_progress["total"]:
                all_completed = False

        # Apply priority: error > blocked > in_progress > completed
        if has_error:
            return InputTaskStatus.ERROR
        if has_pending_input:
            return InputTaskStatus.BLOCKED
        if has_running:
            return InputTaskStatus.IN_PROGRESS
        if all_completed:
            return InputTaskStatus.COMPLETED

        # Default: session exists but not streaming, or subtasks still pending
        return InputTaskStatus.IN_PROGRESS

    @staticmethod
    def has_final_report(task: InputTask) -> bool:
        """Whether the stored structured result says how the work ended.

        ``done``/``failed``, or a ``blocked`` question not yet answered. An
        ``in_progress`` report, or a ``blocked`` one whose reply was delivered
        (the agent resumed), says nothing about the end of the run.
        """
        result = task.delegation_result or {}
        return result.get('status') in ('done', 'failed') or (
            result.get('status') == 'blocked' and result.get('reply_state') != 'delivered'
        )

    @staticmethod
    def chain_depth(db_session: DBSession, task: InputTask) -> int:
        """How many agent hand-offs led to ``task`` (0 for work a person started).

        The deeper of the delegation contract's ``depth`` and the recorded
        ``chain_depth``. When neither is set (rows from the older routes, or
        chains created before the column), one per hop to the originating task
        (``parent_task_id``, or the task of the conversation that created it),
        each hop using this same rule. One helper for the context block and the
        tasks_create depth limit, so the two cannot diverge.
        """
        depth = 0
        hops = 0
        current = task
        seen = {task.id}
        while hops <= CHAIN_DEPTH_WALK_LIMIT:
            contract = (current.delegation_metadata or {}).get("depth")
            recorded = max(contract if isinstance(contract, int) else 0, current.chain_depth or 0)
            if recorded:
                return hops + recorded
            if not (current.source_session_id or current.parent_task_id):
                return hops
            hops += 1
            origin_id = current.parent_task_id
            if not origin_id and current.source_session_id:
                source = db_session.get(Session, current.source_session_id)
                origin_id = source.source_task_id if source else None
            origin = db_session.get(InputTask, origin_id) if origin_id else None
            if not origin or origin.id in seen:
                return hops
            seen.add(origin.id)
            current = origin
        return hops

    @staticmethod
    def is_automated(db_session: DBSession, task: InputTask) -> bool:
        """Whether another agent or an automation handed the task over (report rule)."""
        return bool(
            task.delegation_metadata
            or task.agent_initiated
            or task.source_session_id
            or task.created_by_node_id
        ) or db_session.exec(
            select(TaskTrigger.id).where(TaskTrigger.task_id == task.id)
        ).first() is not None

    @staticmethod
    def requires_report(db_session: DBSession, task: InputTask) -> bool:
        """Whether a session ending without a final report must block the task.

        True only when the task's environment advertises the task protocol
        (``tasks_report`` available, version >= 1), the task was handed to the
        agent by another agent or an automation, and no final report is stored.
        An ordinary user-opened task, or one run on an older image, keeps
        session-end auto-completion.
        """
        if InputTaskService.has_final_report(task):
            return False
        if not InputTaskService.is_automated(db_session, task):
            return False
        session = db_session.get(Session, task.session_id) if task.session_id else None
        if not session or not session.environment_id:
            return False
        environment = db_session.get(AgentEnvironment, session.environment_id)
        return bool(environment and environment.task_protocol_version >= 1)

    @staticmethod
    def sync_task_status_from_sessions(
        db_session: DBSession,
        task_id: UUID,
        force_status: str | None = None,
    ) -> InputTask | None:
        """
        Sync task status from connected sessions.

        Only syncs for tasks in execution phase (running, pending_input, completed, error).
        Does NOT override: new, refining, archived statuses.

        Args:
            db_session: Database session
            task_id: Task UUID
            force_status: Optional status to force (skips computation)

        Returns:
            Updated task or None if task not found or status unchanged
        """
        task = db_session.get(InputTask, task_id)
        if not task:
            return None

        # Only sync for tasks in execution phase
        execution_statuses = [
            InputTaskStatus.IN_PROGRESS,
            InputTaskStatus.BLOCKED,
            InputTaskStatus.COMPLETED,
            InputTaskStatus.ERROR,
        ]

        if task.status not in execution_statuses:
            # Task is in new, refining, or archived - don't override
            return None

        # A structured delegation result is authoritative until a reply or a
        # new execution clears it; session completion must not erase its ask.
        # A blocked result whose reply was delivered no longer holds the task:
        # the report keeps status 'blocked' verbatim (so a retried report still
        # matches), and reply progress lives in reply_state.
        if InputTaskService.has_final_report(task):
            return None

        # Compute or use forced status
        new_status = force_status or InputTaskService.compute_status_from_sessions(
            db_session, task_id
        )

        if not new_status or new_status == task.status:
            # No change needed
            return None

        # Report rule: on a protocol environment, delegated or automated work
        # is settled by the agent's tasks_report, never by the session ending.
        if (
            not force_status
            and new_status == InputTaskStatus.COMPLETED
            and InputTaskService.requires_report(db_session, task)
        ):
            if task.status != InputTaskStatus.IN_PROGRESS:
                return None
            return InputTaskService.update_task_status(
                db_session=db_session,
                task_id=task.id,
                new_status=InputTaskStatus.BLOCKED,
                changed_by_system=True,
                reason=ENDED_WITHOUT_REPORTING,
            )

        # Update task status
        return InputTaskService.update_status(
            db_session=db_session,
            task=task,
            status=new_status,
        )

    # Event handlers for streaming lifecycle events

    @staticmethod
    async def handle_stream_started(event_data: dict[str, Any]) -> None:
        """
        React to STREAM_STARTED events and sync task status to RUNNING.

        Args:
            event_data: Full event dict with type, model_id, meta, user_id, timestamp
        """
        try:
            meta = event_data.get("meta", {})
            session_id = meta.get("session_id")

            if not session_id:
                logger.debug("STREAM_STARTED event missing session_id, skipping task sync")
                return

            with create_session() as db:
                # Get session to find source_task_id
                session = db.get(Session, UUID(session_id))
                if not session or not session.source_task_id:
                    return  # Session not linked to a task

                task = db.get(InputTask, session.source_task_id)
                if not task:
                    return

                # Only update if task is in execution phase
                if task.status in [
                    InputTaskStatus.IN_PROGRESS,
                    InputTaskStatus.BLOCKED,
                    InputTaskStatus.COMPLETED,
                    InputTaskStatus.ERROR,
                ]:
                    InputTaskService.update_status(
                        db_session=db,
                        task=task,
                        status=InputTaskStatus.IN_PROGRESS,
                    )
                    logger.info(f"Task {task.id} status synced to IN_PROGRESS (STREAM_STARTED)")

        except Exception as e:
            logger.error(f"Error handling STREAM_STARTED for task sync: {e}", exc_info=True)

    @staticmethod
    async def handle_stream_completed(event_data: dict[str, Any]) -> None:
        """
        React to STREAM_COMPLETED events and compute task status from sessions.
        When a task transitions to completed and has a source_session_id,
        delivers a feedback message to the originating session so the parent
        agent can continue its work.

        Args:
            event_data: Full event dict with type, model_id, meta, user_id, timestamp
        """
        try:
            meta = event_data.get("meta", {})
            session_id = meta.get("session_id")

            if not session_id:
                logger.debug("STREAM_COMPLETED event missing session_id, skipping task sync")
                return

            with create_session() as db:
                # Get session to find source_task_id
                session = db.get(Session, UUID(session_id))
                if not session or not session.source_task_id:
                    return  # Session not linked to a task

                updated_task = InputTaskService.sync_task_status_from_sessions(
                    db_session=db,
                    task_id=session.source_task_id,
                )

                if updated_task:
                    logger.info(
                        f"Task {updated_task.id} status synced to {updated_task.status} (STREAM_COMPLETED)"
                    )

                # Notify source session when task completes
                task = updated_task or db.get(InputTask, session.source_task_id)
                if (
                    task
                    and task.status == InputTaskStatus.COMPLETED
                    and task.source_session_id
                    and task.auto_feedback
                    and not task.feedback_delivered
                ):
                    # Used only when the executor left no report; a stored
                    # report supplies its own summary. Worded for the tools
                    # the requester's environment has.
                    short_code = task.short_code or str(task.id)[:8]
                    if InputTaskService.session_protocol_version(db, task.source_session_id) >= 1:
                        summary = (
                            f"Finished without a report; tasks_get {short_code} shows its comments and files."
                        )
                    else:
                        agent_name = "Agent"
                        if task.selected_agent_id:
                            agent = db.get(Agent, task.selected_agent_id)
                            if agent:
                                agent_name = agent.name
                        summary = (
                            f"{short_code} completed by {agent_name}. "
                            f"Read results with mcp__agent_task__get_details tool (task=\"{short_code}\")."
                        )
                    await InputTaskService.deliver_feedback_to_source(
                        db, task, "completed", summary
                    )

        except Exception as e:
            logger.error(f"Error handling STREAM_COMPLETED for task sync: {e}", exc_info=True)

    @staticmethod
    async def handle_stream_error(event_data: dict[str, Any]) -> None:
        """
        React to STREAM_ERROR events and sync task status to ERROR.

        Args:
            event_data: Full event dict with type, model_id, meta, user_id, timestamp
        """
        try:
            meta = event_data.get("meta", {})
            session_id = meta.get("session_id")
            error_message = meta.get("error_message", "Unknown error")

            if not session_id:
                logger.debug("STREAM_ERROR event missing session_id, skipping task sync")
                return

            with create_session() as db:
                # Get session to find source_task_id
                session = db.get(Session, UUID(session_id))
                if not session or not session.source_task_id:
                    return  # Session not linked to a task

                task = db.get(InputTask, session.source_task_id)
                if not task:
                    return

                # Only update if task is in execution phase
                if task.status in [
                    InputTaskStatus.IN_PROGRESS,
                    InputTaskStatus.BLOCKED,
                    InputTaskStatus.COMPLETED,
                    InputTaskStatus.ERROR,
                ]:
                    InputTaskService.update_status(
                        db_session=db,
                        task=task,
                        status=InputTaskStatus.ERROR,
                        error_message=error_message,
                    )
                    logger.info(f"Task {task.id} status synced to ERROR (STREAM_ERROR)")

                    # Notify source session about the error
                    if task.source_session_id and task.auto_feedback and not task.feedback_delivered:
                        short_code = task.short_code or str(task.id)[:8]
                        await InputTaskService.deliver_feedback_to_source(
                            db, task, "error", f"{short_code} failed: {error_message}"
                        )

        except Exception as e:
            logger.error(f"Error handling STREAM_ERROR for task sync: {e}", exc_info=True)

    @staticmethod
    async def handle_todo_list_updated(event_data: dict[str, Any]) -> None:
        """
        React to TODO_LIST_UPDATED events.
        If the session is linked to a task, save todos to task and emit TASK_TODO_UPDATED.

        Args:
            event_data: Full event dict with type, model_id, meta, user_id, timestamp
        """
        try:
            meta = event_data.get("meta", {})
            session_id = meta.get("session_id")
            todos = meta.get("todos", [])
            user_id = event_data.get("user_id")

            if not session_id:
                logger.debug("TODO_LIST_UPDATED event missing session_id, skipping")
                return

            with create_session() as db:
                session = db.get(Session, UUID(session_id))
                if not session or not session.source_task_id:
                    return  # Session not linked to a task

                task_id = session.source_task_id

                # Save todos to task for persistence
                task = db.get(InputTask, task_id)
                if task:
                    task.todo_progress = todos
                    task.updated_at = datetime.now(UTC)
                    db.add(task)
                    db.commit()
                    logger.info(f"Saved todo_progress to task {task_id}")

            # Emit task-level event for real-time updates
            await event_service.emit_event(
                event_type=EventType.TASK_TODO_UPDATED,
                model_id=task_id,
                user_id=UUID(user_id) if user_id else None,
                meta={
                    "task_id": str(task_id),
                    "session_id": session_id,
                    "todos": todos
                }
            )
            logger.info(f"Emitted TASK_TODO_UPDATED for task {task_id}")

        except Exception as e:
            logger.error(f"Error handling TODO_LIST_UPDATED for task sync: {e}", exc_info=True)

    @staticmethod
    async def handle_session_state_updated(event_data: dict[str, Any]) -> None:
        """
        React to SESSION_STATE_UPDATED events.
        Syncs task status based on session result_state, and delivers
        feedback to source session if auto_feedback is enabled.
        """
        try:
            meta = event_data.get("meta", {})
            session_id = meta.get("session_id")
            state = meta.get("state")
            summary = meta.get("summary")

            if not session_id or not state or not summary:
                logger.debug("SESSION_STATE_UPDATED event missing fields, skipping")
                return

            with create_session() as db:
                session = db.get(Session, UUID(session_id))
                if not session or not session.source_task_id:
                    return  # Not a task session

                # Sync task status based on reported session state
                updated_task = InputTaskService.sync_task_status_from_sessions(
                    db_session=db,
                    task_id=session.source_task_id,
                )
                if updated_task:
                    logger.info(
                        f"Task {updated_task.id} status synced to {updated_task.status} "
                        f"(SESSION_STATE_UPDATED: {state})"
                    )

                task = updated_task or db.get(InputTask, session.source_task_id)
                if not task:
                    return

                # Deliver feedback if auto_feedback enabled and not already delivered
                if task.auto_feedback and not task.feedback_delivered:
                    await InputTaskService.deliver_feedback_to_source(
                        db, task, state, summary
                    )

        except Exception as e:
            logger.error(f"Error in InputTaskService.handle_session_state_updated: {e}", exc_info=True)

    @staticmethod
    def session_protocol_version(db_session: DBSession, session_id: UUID | None) -> int:
        """Task protocol version of the environment a session runs on (0 if unknown)."""
        session = db_session.get(Session, session_id) if session_id else None
        if not session or not session.environment_id:
            return 0
        environment = db_session.get(AgentEnvironment, session.environment_id)
        return environment.task_protocol_version if environment else 0

    @staticmethod
    def result_packet_text(
        db_session: DBSession,
        task: InputTask,
        state: str,
        summary: str,
    ) -> str:
        """The task protocol's result packet as the message the requester receives.

        One shape on every host (tasks.schema.json ``result_packet``): a first
        line ``Task result from <agent> on <ref>:`` then status, summary, body,
        artifacts and question. The executor's stored ``tasks_report`` supplies
        the fields when there is one; otherwise ``state`` (the session outcome:
        completed / needs_input / error) maps onto the report status and
        ``summary`` is the session's own text. The body is cut at
        ``RESULT_PACKET_BODY_LIMIT`` with a pointer to ``tasks_get``.
        """
        ref = task.short_code or str(task.id)[:8]
        agent_name = "Agent"
        if task.selected_agent_id:
            agent = db_session.get(Agent, task.selected_agent_id)
            if agent:
                agent_name = agent.name

        report = task.delegation_result or {}
        if report.get("status") in ("done", "failed", "blocked"):
            status = report["status"]
            summary = report.get("summary") or summary
            body = report.get("body") or ""
            artifacts = report.get("artifacts") or []
            question = report.get("question") if status == "blocked" else None
            audience = report.get("audience") or "user"
        else:
            status = RESULT_STATUS_BY_SESSION_STATE.get(state, "in_progress")
            # No stored report: the session's summary is all there is, and a
            # question in it has no result to answer through tasks_reply.
            body, artifacts, audience, question = "", [], "user", None

        lines = [
            f"Task result from {agent_name} on {ref}:",
            f"Status: {status}",
            f"Summary: {summary.strip()}",
        ]
        body = body.strip()
        if body:
            if len(body) > RESULT_PACKET_BODY_LIMIT:
                body = (
                    body[:RESULT_PACKET_BODY_LIMIT].rstrip()
                    + f"\n… (cut at {RESULT_PACKET_BODY_LIMIT} characters; tasks_get {ref} has the full result)"
                )
            lines += ["", body]
        if artifacts:
            lines += ["", "Artifacts:"]
            for artifact in artifacts:
                if isinstance(artifact, dict):
                    if artifact.get('ref'):
                        lines.append(f"- {artifact.get('name', '')}: {artifact['ref']}")
                    else:
                        # Recorded by name only: a path in the executor's folder.
                        lines.append(f"- {artifact.get('name', '')} (file, not attached)")
                else:
                    lines.append(f"- {artifact}")
        if question:
            asked = "you (answer with tasks_reply)" if audience == "requester" else "the user"
            lines += ["", f"Question for {asked}: {question.strip()}"]
        return "\n".join(lines)

    @staticmethod
    async def deliver_feedback_to_source(
        db_session: DBSession,
        task: InputTask,
        state: str,
        summary: str,
    ) -> bool:
        """
        Send session state feedback as a message to the source session.

        Args:
            db_session: Database session
            task: The input task with source_session_id
            state: Session state ("completed", "needs_input", "error")
            summary: The agent's summary text

        Returns:
            True if feedback was delivered successfully
        """
        if not task.source_session_id:
            return False

        source_session = db_session.get(Session, task.source_session_id)
        if not source_session:
            return False

        if InputTaskService.session_protocol_version(db_session, task.source_session_id) >= 1:
            content = InputTaskService.result_packet_text(db_session, task, state, summary)
        else:
            # A requester environment built before the task protocol reads the
            # pre-protocol feedback lines its handover prompt describes.
            content_map = {
                "completed": f"[Sub-task completed] {summary}",
                "needs_input": f"[Sub-task needs input] {summary}",
                "error": f"[Sub-task error] {summary}",
            }
            content = content_map.get(state, f"[Sub-task update] {summary}")

        # Create message in source session (role="user" triggers agent processing)
        from app.services.sessions.message_service import MessageService  # circular import
        MessageService.create_message(
            session=db_session,
            session_id=task.source_session_id,
            role="user",
            content=content,
            message_metadata={
                "task_feedback": True,
                "task_id": str(task.id),
                "task_state": state,
                "task_summary": summary,
            },
            sent_to_agent_status="pending",
        )

        task.feedback_delivered = True
        db_session.add(task)
        db_session.commit()

        # If source session is idle, trigger agent processing
        if source_session.interaction_status == "":
            create_task_with_error_logging(
                SessionService.initiate_stream(
                    session_id=task.source_session_id,
                    get_fresh_db_session=create_session,
                ),
                task_name=f"feedback_initiate_stream_{task.source_session_id}"
            )

        logger.info(
            f"Delivered feedback to source session {task.source_session_id} "
            f"for task {task.id} (state={state})"
        )
        return True

    # ==================== File Attachment Methods ====================

    @staticmethod
    def attach_files_to_task(
        db_session: DBSession,
        task_id: UUID,
        file_ids: list[UUID],
        user_id: UUID,
    ) -> list:
        """
        Attach files to a task.

        Args:
            db_session: Database session
            task_id: Task UUID
            file_ids: List of file UUIDs to attach
            user_id: User ID (for ownership verification)

        Returns:
            List of InputTaskFile junction records created
        """

        created_links = []
        for file_id in file_ids:
            # Verify file exists and user owns it
            file = db_session.get(FileUpload, file_id)
            if not file or file.user_id != user_id:
                logger.warning(f"File {file_id} not found or not owned by user {user_id}")
                continue

            # Check if already linked
            existing = db_session.exec(
                select(InputTaskFile).where(
                    InputTaskFile.task_id == task_id,
                    InputTaskFile.file_id == file_id,
                )
            ).first()

            if existing:
                continue  # Already linked

            # Create link
            link = InputTaskFile(task_id=task_id, file_id=file_id)
            db_session.add(link)
            created_links.append(link)

        if created_links:
            InputTaskService._touch(db_session, task_id)
            db_session.commit()
            for link in created_links:
                db_session.refresh(link)

        return created_links

    @staticmethod
    def get_task_files(
        db_session: DBSession,
        task_id: UUID,
    ) -> list:
        """
        Get all files attached to a task.

        Args:
            db_session: Database session
            task_id: Task UUID

        Returns:
            List of FileUpload records
        """

        statement = (
            select(FileUpload)
            .join(InputTaskFile, FileUpload.id == InputTaskFile.file_id)
            .where(InputTaskFile.task_id == task_id)
        )
        return list(db_session.exec(statement).all())

    @staticmethod
    def get_task_file_ids(
        db_session: DBSession,
        task_id: UUID,
    ) -> list[str]:
        """
        Get all file IDs attached to a task as strings.

        Args:
            db_session: Database session
            task_id: Task UUID

        Returns:
            List of file ID strings
        """

        statement = select(InputTaskFile.file_id).where(InputTaskFile.task_id == task_id)
        return [str(fid) for fid in db_session.exec(statement).all()]

    @staticmethod
    def detach_file_from_task(
        db_session: DBSession,
        task_id: UUID,
        file_id: UUID,
        user_id: UUID,
        mark_for_deletion: bool = True,
    ) -> bool:
        """
        Remove a file from a task.

        Args:
            db_session: Database session
            task_id: Task UUID
            file_id: File UUID to remove
            user_id: User ID (for ownership verification)
            mark_for_deletion: If True, mark the file for garbage collection

        Returns:
            True if file was detached, False otherwise
        """

        # Find the link
        link = db_session.exec(
            select(InputTaskFile).where(
                InputTaskFile.task_id == task_id,
                InputTaskFile.file_id == file_id,
            )
        ).first()

        if not link:
            return False

        # Remove the link
        db_session.delete(link)

        # Optionally mark file for deletion if temporary
        if mark_for_deletion:
            file = db_session.get(FileUpload, file_id)
            if file and file.user_id == user_id and file.status == "temporary":
                file.status = "marked_for_deletion"
                file.marked_for_deletion_at = datetime.now(UTC)
                db_session.add(file)

        InputTaskService._touch(db_session, task_id)
        db_session.commit()
        return True

    @staticmethod
    def cleanup_task_files(
        db_session: DBSession,
        task_id: UUID,
        user_id: UUID,
    ) -> int:
        """
        Clean up all files attached to a task (mark for deletion).

        Used when a task is deleted without being executed.

        Args:
            db_session: Database session
            task_id: Task UUID
            user_id: User ID (for ownership verification)

        Returns:
            Number of files marked for deletion
        """
        # Get all attached files
        statement = (
            select(FileUpload)
            .join(InputTaskFile, FileUpload.id == InputTaskFile.file_id)
            .where(InputTaskFile.task_id == task_id)
            .where(FileUpload.user_id == user_id)
            .where(FileUpload.status == "temporary")
        )
        files = db_session.exec(statement).all()

        count = 0
        for file in files:
            file.status = "marked_for_deletion"
            file.marked_for_deletion_at = datetime.now(UTC)
            db_session.add(file)
            count += 1

        if count > 0:
            db_session.commit()

        return count

    # ==================== Collaboration Methods (Phase A) ====================

    @staticmethod
    def get_task_by_short_code(
        db_session: DBSession,
        short_code: str,
        user_id: UUID,
    ) -> InputTask:
        """
        Get a task by its short code (e.g., 'TASK-42').

        Args:
            db_session: Database session
            short_code: Task short code
            user_id: Requesting user (ownership check)

        Returns:
            InputTask

        Raises:
            TaskNotFoundError: If not found or not owned by user
        """
        task = db_session.exec(
            select(InputTask).where(
                InputTask.short_code == short_code,
                InputTask.owner_id == user_id,
            )
        ).first()
        if not task:
            raise TaskNotFoundError(f"Task {short_code} not found")
        return task

    @staticmethod
    def update_task_status(
        db_session: DBSession,
        task_id: UUID,
        new_status: str,
        changed_by_agent_id: UUID | None = None,
        changed_by_user_id: UUID | None = None,
        changed_by_system: bool = False,
        reason: str | None = None,
    ) -> InputTask:
        """
        Update task status with full audit trail (history record + system comment).

        Validates transition rules. Records a TaskStatusHistory entry and posts
        a system comment so the transition appears in the task's activity feed.

        Args:
            db_session: Database session
            task_id: Task UUID
            new_status: Target status
            changed_by_agent_id: Agent that triggered the change (or None)
            changed_by_user_id: User that triggered the change (or None)
            changed_by_system: True if platform-triggered (no agent/user attribution)
            reason: Optional explanation for the change

        Returns:
            Updated InputTask

        Raises:
            TaskNotFoundError: If task not found
            ValidationError: If transition is invalid
        """
        task = db_session.get(InputTask, task_id)
        if not task:
            raise TaskNotFoundError()

        from_status = InputTaskService._apply_status_transition(
            db_session,
            task,
            new_status,
            changed_by_agent_id=changed_by_agent_id,
            changed_by_user_id=changed_by_user_id,
            changed_by_system=changed_by_system,
            reason=reason,
            commit=True,
        )
        if from_status is not None:
            logger.info(f"Task {task_id} status: {from_status} → {new_status}")
        return task

    @staticmethod
    def status_path(from_status: str, to_status: str) -> list[str] | None:
        """The shortest chain of legal transitions from ``from_status`` to ``to_status``.

        ``[]`` when already there, ``None`` when ``VALID_TRANSITIONS`` has no
        route. Breadth-first, so a move records only the steps it must: a task
        cancelled from ``new`` goes there in one step, one completed from
        ``new`` passes through ``in_progress`` once (the same steps Cinna
        Desktop pushes for a local task finished before its remote copy moved).
        """
        if from_status == to_status:
            return []
        previous: dict[str, str] = {from_status: from_status}
        queue = [from_status]
        while queue:
            current = queue.pop(0)
            for nxt in sorted(InputTaskStatus.VALID_TRANSITIONS.get(current, set())):
                if nxt in previous:
                    continue
                previous[nxt] = current
                if nxt == to_status:
                    path = [nxt]
                    while previous[path[0]] != from_status:
                        path.insert(0, previous[path[0]])
                    return path
                queue.append(nxt)
        return None

    @staticmethod
    def _apply_status_transition(
        db_session: DBSession,
        task: InputTask,
        new_status: str,
        changed_by_agent_id: UUID | None = None,
        changed_by_user_id: UUID | None = None,
        changed_by_system: bool = False,
        reason: str | None = None,
        commit: bool = False,
    ) -> str | None:
        """Validate and apply one status transition with its audit trail.

        Checks ``VALID_TRANSITIONS``, adds the TaskStatusHistory row and the
        status_change system comment, and sets the status and its timestamps.

        With ``commit=True`` it commits, refreshes and emits
        TASK_STATUS_CHANGED. With ``commit=False`` nothing is committed, so a
        caller can make other fields visible in the same commit (e.g. under a
        row lock); it must then call ``_emit_status_changed`` after committing.

        Returns:
            The previous status when a transition was applied, None for a no-op
            (``new_status`` equals the current status).

        Raises:
            ValidationError: If the transition is not allowed.
        """
        from_status = task.status

        valid_nexts = InputTaskStatus.VALID_TRANSITIONS.get(from_status, set())
        if new_status not in valid_nexts and new_status != from_status:
            raise ValidationError(
                f"Cannot transition task from '{from_status}' to '{new_status}'. "
                f"Valid transitions: {sorted(valid_nexts)}"
            )

        if new_status == from_status:
            return None

        # Immutable status history record
        db_session.add(TaskStatusHistory(
            task_id=task.id,
            from_status=from_status,
            to_status=new_status,
            changed_by_agent_id=changed_by_agent_id,
            changed_by_user_id=changed_by_user_id,
            reason=reason,
        ))

        # System comment visible in the activity feed. Added directly rather
        # than via TaskCommentService.add_system_comment, which commits.
        db_session.add(TaskComment(
            task_id=task.id,
            content=InputTaskService._build_status_change_comment(
                from_status=from_status,
                to_status=new_status,
                agent_id=changed_by_agent_id,
                user_id=changed_by_user_id,
                is_system=changed_by_system,
                reason=reason,
                db_session=db_session,
            ),
            comment_type="status_change",
            comment_meta={"from_status": from_status, "to_status": new_status},
        ))

        task.status = new_status
        task.updated_at = datetime.now(UTC)

        if (
            new_status == InputTaskStatus.IN_PROGRESS
            and task.executed_at is None
            and not task.external_executor
        ):
            task.executed_at = datetime.now(UTC)
        elif new_status == InputTaskStatus.COMPLETED:
            task.completed_at = datetime.now(UTC)
        elif new_status == InputTaskStatus.ARCHIVED:
            task.archived_at = datetime.now(UTC)

        db_session.add(task)

        if commit:
            db_session.commit()
            db_session.refresh(task)
            InputTaskService._emit_status_changed(
                task, from_status,
                changed_by_agent_id=changed_by_agent_id,
                changed_by_user_id=changed_by_user_id,
            )
        return from_status

    @staticmethod
    def _emit_status_changed(
        task: InputTask,
        from_status: str,
        changed_by_agent_id: UUID | None = None,
        changed_by_user_id: UUID | None = None,
        to_status: str | None = None,
    ) -> None:
        """Emit TASK_STATUS_CHANGED for a committed transition to ``to_status``
        (default ``task.status``), and notify the parent task when a subtask
        completed."""
        new_status = to_status or task.status
        create_task_with_error_logging(
            event_service.emit_event(
                event_type=EventType.TASK_STATUS_CHANGED,
                model_id=task.id,
                user_id=task.owner_id,
                meta={
                    "task_id": str(task.id),
                    "short_code": task.short_code,
                    "from_status": from_status,
                    "to_status": new_status,
                    "changed_by_agent_id": str(changed_by_agent_id) if changed_by_agent_id else None,
                    "changed_by_user_id": str(changed_by_user_id) if changed_by_user_id else None,
                }
            ),
            task_name=f"emit_task_status_changed_{task.id}"
        )

        if new_status == InputTaskStatus.COMPLETED and task.parent_task_id:
            create_task_with_error_logging(
                InputTaskService._notify_parent_task_async(
                    parent_task_id=task.parent_task_id,
                    completed_subtask_short_code=task.short_code or str(task.id),
                    completed_subtask_agent_id=task.selected_agent_id,
                ),
                task_name=f"notify_parent_task_{task.parent_task_id}"
            )

    @staticmethod
    def _build_status_change_comment(
        from_status: str,
        to_status: str,
        agent_id: UUID | None,
        user_id: UUID | None,
        is_system: bool,
        reason: str | None,
        db_session: DBSession,
    ) -> str:
        """Build a human-readable status change comment."""
        actor = "System"
        if agent_id:
            agent = db_session.get(Agent, agent_id)
            actor = agent.name if agent else "Agent"
        elif user_id:
            from app.models.users.user import User
            user = db_session.get(User, user_id)
            actor = user.full_name or user.email if user else "User"

        msg = f"Status changed from **{from_status}** to **{to_status}**"
        if not is_system and actor != "System":
            msg = f"{actor}: {msg}"
        if reason:
            msg += f" — {reason}"
        return msg

    @staticmethod
    async def _notify_parent_task_async(
        parent_task_id: UUID,
        completed_subtask_short_code: str,
        completed_subtask_agent_id: UUID | None,
    ) -> None:
        """
        Notify the parent task that a subtask has completed.

        Posts a system comment on the parent and checks if all subtasks are done.
        """
        try:
            from app.core.db import create_session as make_db
            with make_db() as db:
                parent_task = db.get(InputTask, parent_task_id)
                if not parent_task:
                    return

                # Resolve completing agent name
                agent_name = "Agent"
                if completed_subtask_agent_id:
                    agent = db.get(Agent, completed_subtask_agent_id)
                    if agent:
                        agent_name = agent.name

                # Post system comment on parent
                from app.services.tasks.task_comment_service import TaskCommentService
                TaskCommentService.add_system_comment(
                    db_session=db,
                    task_id=parent_task_id,
                    content=f"Subtask **{completed_subtask_short_code}** completed by {agent_name}",
                    comment_type="system",
                )

                # Check if all subtasks are completed
                subtasks = db.exec(
                    select(InputTask).where(InputTask.parent_task_id == parent_task_id)
                ).all()
                total = len(subtasks)
                completed_count = sum(1 for s in subtasks if s.status == InputTaskStatus.COMPLETED)

                if total > 0 and completed_count == total:
                    TaskCommentService.add_system_comment(
                        db_session=db,
                        task_id=parent_task_id,
                        content=f"All {total} subtasks completed — ready to finalize",
                        comment_type="system",
                    )

                # Emit subtask completed event
                create_task_with_error_logging(
                    event_service.emit_event(
                        event_type=EventType.SUBTASK_COMPLETED,
                        model_id=parent_task_id,
                        user_id=parent_task.owner_id,
                        meta={
                            "parent_task_id": str(parent_task_id),
                            "parent_short_code": parent_task.short_code,
                            "subtask_short_code": completed_subtask_short_code,
                            "agent_name": agent_name,
                        }
                    ),
                    task_name=f"emit_subtask_completed_{parent_task_id}"
                )

        except Exception as e:
            logger.error(f"Error notifying parent task {parent_task_id}: {e}", exc_info=True)

    @staticmethod
    def get_task_detail(
        db_session: DBSession,
        task_id: UUID,
        user_id: UUID,
    ) -> InputTaskDetailPublic:
        """
        Get full task detail including comments, attachments, subtasks, and status history.

        Args:
            db_session: Database session
            task_id: Task UUID
            user_id: Requesting user (ownership check)

        Returns:
            InputTaskDetailPublic with all related data

        Raises:
            TaskNotFoundError, PermissionDeniedError
        """
        task = db_session.get(InputTask, task_id)
        if not task:
            raise TaskNotFoundError()
        if task.owner_id != user_id:
            raise PermissionDeniedError()

        # Resolve agent name
        agent_name = None
        if task.selected_agent_id:
            agent = db_session.get(Agent, task.selected_agent_id)
            if agent:
                agent_name = agent.name

        # Resolve assigned node and team names
        assigned_node_name = None
        team_name = None
        if task.assigned_node_id:
            from app.models.agentic_teams.agentic_team import AgenticTeamNode
            node = db_session.get(AgenticTeamNode, task.assigned_node_id)
            if node:
                assigned_node_name = node.name
        if task.team_id:
            from app.models.agentic_teams.agentic_team import AgenticTeam
            team = db_session.get(AgenticTeam, task.team_id)
            if team:
                team_name = team.name

        # Load sessions info
        sessions_count, latest_session_id = SessionService.get_task_sessions_info(
            db_session=db_session, task_id=task_id
        )

        # Load comments
        from app.services.tasks.task_comment_service import TaskCommentService
        comments, _ = TaskCommentService.list_comments(db_session=db_session, task_id=task_id)

        # Load standalone attachments
        from app.services.tasks.task_attachment_service import TaskAttachmentService
        attachments = TaskAttachmentService.list_attachments(db_session=db_session, task_id=task_id)

        # Load subtasks
        subtasks_raw = db_session.exec(
            select(InputTask)
            .where(InputTask.parent_task_id == task_id)
            .order_by(InputTask.created_at.asc())
        ).all()
        subtasks = [
            InputTaskPublic(
                **subtask.model_dump(),
                subtask_count=InputTaskService.get_subtask_progress(db_session, subtask.id)["total"],
                subtask_completed_count=InputTaskService.get_subtask_progress(db_session, subtask.id)["completed"],
            )
            for subtask in subtasks_raw
        ]

        # Load status history
        history_raw = db_session.exec(
            select(TaskStatusHistory)
            .where(TaskStatusHistory.task_id == task_id)
            .order_by(TaskStatusHistory.created_at.asc())
        ).all()
        status_history = []
        for h in history_raw:
            changed_by_name = None
            if h.changed_by_agent_id:
                a = db_session.get(Agent, h.changed_by_agent_id)
                changed_by_name = a.name if a else None
            elif h.changed_by_user_id:
                from app.models.users.user import User
                u = db_session.get(User, h.changed_by_user_id)
                changed_by_name = (u.full_name or u.email) if u else None
            status_history.append(TaskStatusHistoryPublic(
                id=h.id,
                task_id=h.task_id,
                from_status=h.from_status,
                to_status=h.to_status,
                changed_by_agent_id=h.changed_by_agent_id,
                changed_by_user_id=h.changed_by_user_id,
                reason=h.reason,
                created_at=h.created_at,
                changed_by_name=changed_by_name,
            ))

        # Compute subtask counts for this task
        subtask_progress = InputTaskService.get_subtask_progress(db_session, task_id)

        # Attached files (legacy)
        attached_files = InputTaskService.get_task_files(db_session=db_session, task_id=task_id)
        attached_files_public = [FileUploadPublic.model_validate(f) for f in attached_files]

        # Resolve parent and root short codes
        parent_short_code = None
        root_short_code = None
        if task.parent_task_id:
            parent = db_session.get(InputTask, task.parent_task_id)
            if parent:
                parent_short_code = parent.short_code
            # Walk up to root (max 20 levels)
            current = task
            for _ in range(20):
                if not current.parent_task_id:
                    break
                current = db_session.get(InputTask, current.parent_task_id)
                if not current:
                    break
            if current and current.id != task.id:
                root_short_code = current.short_code

        return InputTaskDetailPublic(
            **task.model_dump(),
            agent_name=agent_name,
            assigned_node_name=assigned_node_name,
            team_name=team_name,
            sessions_count=sessions_count,
            latest_session_id=latest_session_id,
            attached_files=attached_files_public,
            subtask_count=subtask_progress["total"],
            subtask_completed_count=subtask_progress["completed"],
            comments=comments,
            attachments=attachments,
            subtasks=subtasks,
            status_history=status_history,
            parent_short_code=parent_short_code,
            root_short_code=root_short_code,
        )

    @staticmethod
    def get_subtask_progress(
        db_session: DBSession,
        task_id: UUID,
    ) -> dict:
        """
        Get subtask progress counts for a task.

        Args:
            db_session: Database session
            task_id: Task UUID

        Returns:
            Dict with keys: total, completed, in_progress, blocked
        """
        subtasks = db_session.exec(
            select(InputTask).where(
                InputTask.parent_task_id == task_id,
                InputTask.status != InputTaskStatus.ARCHIVED,
            )
        ).all()

        total = len(subtasks)
        completed = sum(1 for s in subtasks if s.status == InputTaskStatus.COMPLETED)
        in_progress = sum(1 for s in subtasks if s.status == InputTaskStatus.IN_PROGRESS)
        blocked = sum(1 for s in subtasks if s.status == InputTaskStatus.BLOCKED)

        return {
            "total": total,
            "completed": completed,
            "in_progress": in_progress,
            "blocked": blocked,
        }

    @staticmethod
    def create_subtask(
        db_session: DBSession,
        parent_task_id: UUID,
        creating_agent_id: UUID,
        data,  # AgentSubtaskCreate
        *,
        with_created: bool = False,
    ) -> InputTask | tuple[InputTask, bool]:
        """
        Create a subtask (team agents only).

        With ``with_created=True`` returns ``(subtask, created)``; ``created``
        is False when ``external_ref`` matched an existing row.

        Validates that:
        - Parent task has team_id
        - Creating agent has a node in that team
        - If assigned_to provided, target node is connected to creator's node

        Args:
            db_session: Database session
            parent_task_id: Parent task UUID
            creating_agent_id: Agent creating the subtask
            data: AgentSubtaskCreate with title, description, assigned_to, priority

        Returns:
            Created InputTask (subtask)

        Raises:
            TaskNotFoundError, ValidationError
        """
        parent = db_session.get(InputTask, parent_task_id)
        if not parent:
            raise TaskNotFoundError(f"Parent task {parent_task_id} not found")

        if not parent.team_id:
            raise ValidationError("Subtask delegation requires team context (parent task has no team_id)")

        # Verify creating agent is in the team
        from app.models.agentic_teams.agentic_team import AgenticTeamNode, AgenticTeamConnection
        creator_node = db_session.exec(
            select(AgenticTeamNode).where(
                AgenticTeamNode.team_id == parent.team_id,
                AgenticTeamNode.agent_id == creating_agent_id,
            )
        ).first()

        if not creator_node:
            raise ValidationError(
                f"Agent is not a member of the task's team — cannot create subtasks"
            )

        # Resolve assignment if provided
        assigned_agent_id = None
        assigned_node_id = None
        own_queue = bool(getattr(data, "own_queue", False))
        if own_queue:
            # The creator's own queue: its node, so the team-lead fallback in
            # create_task_idempotent never runs; not started (auto_execute off).
            assigned_agent_id = creator_node.agent_id
            assigned_node_id = creator_node.id
        elif data.assigned_to:
            # Find target node by name within the team
            target_node = db_session.exec(
                select(AgenticTeamNode).where(
                    AgenticTeamNode.team_id == parent.team_id,
                    AgenticTeamNode.name == data.assigned_to,
                )
            ).first()
            if not target_node:
                raise ValidationError(f"Team member '{data.assigned_to}' not found in this team")

            # Verify connection exists from creator to target
            connection = db_session.exec(
                select(AgenticTeamConnection).where(
                    AgenticTeamConnection.team_id == parent.team_id,
                    AgenticTeamConnection.source_node_id == creator_node.id,
                    AgenticTeamConnection.target_node_id == target_node.id,
                    AgenticTeamConnection.enabled == True,
                )
            ).first()
            if not connection:
                raise ValidationError(
                    f"Cannot delegate to '{data.assigned_to}' — no connection in team topology"
                )

            assigned_node_id = target_node.id
            assigned_agent_id = target_node.agent_id

        # Create the subtask
        subtask_create = InputTaskCreate(
            original_message=f"{data.title}\n\n{data.description or ''}".strip(),
            selected_agent_id=assigned_agent_id,
            user_workspace_id=parent.user_workspace_id,
            agent_initiated=True,
            auto_execute=bool(assigned_agent_id) and not own_queue,
            source_session_id=data.source_session_id,
            title=data.title,
            priority=data.priority,
            team_id=parent.team_id,
            assigned_node_id=assigned_node_id,
            parent_task_id=parent_task_id,
            external_ref=getattr(data, "external_ref", None),
        )
        subtask, created = InputTaskService.create_task_idempotent(
            db_session=db_session,
            user_id=parent.owner_id,
            data=subtask_create,
        )
        if not created:
            # Matched by external_ref (a retried or concurrent tasks_create):
            # the first call already commented, emitted and started the run.
            return (subtask, False) if with_created else subtask

        # Set created_by_node_id (not in InputTaskCreate, set directly)
        subtask.created_by_node_id = creator_node.id
        db_session.add(subtask)
        db_session.commit()
        db_session.refresh(subtask)

        # Post system comment on parent
        from app.services.tasks.task_comment_service import TaskCommentService
        assigned_name = data.assigned_to or "unassigned"
        TaskCommentService.add_system_comment(
            db_session=db_session,
            task_id=parent_task_id,
            content=f"Created subtask **{subtask.short_code}** → {assigned_name}",
            comment_type="system",
        )

        # Emit event
        create_task_with_error_logging(
            event_service.emit_event(
                event_type=EventType.TASK_SUBTASK_CREATED,
                model_id=parent_task_id,
                user_id=parent.owner_id,
                meta={
                    "parent_task_id": str(parent_task_id),
                    "parent_short_code": parent.short_code,
                    "subtask_short_code": subtask.short_code,
                    "assigned_to": assigned_name,
                }
            ),
            task_name=f"emit_subtask_created_{subtask.id}"
        )

        # If assigned and auto_execute: create session for target agent
        if assigned_agent_id and subtask.auto_execute:
            create_task_with_error_logging(
                InputTaskService._auto_execute_task(subtask),
                task_name=f"auto_execute_subtask_{subtask.id}"
            )

        logger.info(f"Subtask {subtask.short_code} created under {parent.short_code}")
        return (subtask, True) if with_created else subtask

    @staticmethod
    async def _auto_execute_task(
        task_ref: InputTask,
    ) -> None:
        """Auto-execute a newly created task by creating a session.

        Works for both user-created tasks with auto_execute=True and
        agent-created subtasks with an assigned agent.
        Opens its own DB session to run independently of the request lifecycle.
        """
        try:
            if not task_ref.selected_agent_id:
                return
            from app.core.db import create_session as make_db
            with make_db() as db:
                task = db.get(InputTask, task_ref.id)
                if not task:
                    return
                user_id = task.owner_id
                success, _, error = await InputTaskService.execute_task(
                    db_session=db,
                    task=task,
                    user_id=user_id,
                    message_to_send=task.current_description,
                )
                if not success:
                    logger.warning(f"Auto-execute task {task_ref.id} failed: {error}")
        except Exception as e:
            logger.error(f"Error auto-executing task {task_ref.id}: {e}", exc_info=True)

    @staticmethod
    def get_task_tree(
        db_session: DBSession,
        task_id: UUID,
        user_id: UUID,
        max_depth: int = 5,
    ) -> dict:
        """
        Get recursive subtask tree for a task.

        Args:
            db_session: Database session
            task_id: Root task UUID
            user_id: Requesting user (ownership check)
            max_depth: Maximum recursion depth

        Returns:
            Dict with task info and nested subtasks
        """
        task = db_session.get(InputTask, task_id)
        if not task or task.owner_id != user_id:
            raise TaskNotFoundError()

        def _build_node(t: InputTask, depth: int) -> dict:
            node: dict = {
                "id": str(t.id),
                "short_code": t.short_code,
                "title": t.title or t.original_message[:100],
                "status": t.status,
                "priority": t.priority,
                "selected_agent_id": str(t.selected_agent_id) if t.selected_agent_id else None,
            }
            if depth < max_depth:
                children = db_session.exec(
                    select(InputTask).where(InputTask.parent_task_id == t.id)
                ).all()
                node["subtasks"] = [_build_node(c, depth + 1) for c in children]
            else:
                node["subtasks"] = []
            return node

        return _build_node(task, 0)

    @staticmethod
    def update_task_status_from_agent(
        db_session: DBSession,
        task_id: UUID,
        agent_id: UUID,
        data,  # AgentTaskStatusUpdate
    ) -> InputTask:
        """
        Agent explicitly updates task status (edge cases: blocked, completed, cancelled).

        Verifies the agent is assigned to the task before allowing the update.

        Args:
            db_session: Database session
            task_id: Task UUID
            agent_id: Agent requesting the change
            data: AgentTaskStatusUpdate with status and optional reason

        Returns:
            Updated InputTask

        Raises:
            TaskNotFoundError, PermissionDeniedError, ValidationError
        """
        task = db_session.get(InputTask, task_id)
        if not task:
            raise TaskNotFoundError()

        if task.selected_agent_id != agent_id:
            raise PermissionDeniedError("Only the assigned agent can update task status")

        allowed_agent_statuses = {"blocked", "completed", "cancelled"}
        if data.status not in allowed_agent_statuses:
            raise ValidationError(
                f"Agent can only set status to: {sorted(allowed_agent_statuses)}"
            )

        return InputTaskService.update_task_status(
            db_session=db_session,
            task_id=task_id,
            new_status=data.status,
            changed_by_agent_id=agent_id,
            reason=data.reason,
        )

    @staticmethod
    def update_task_status_from_user(
        db_session: DBSession,
        task_id: UUID,
        user_id: UUID,
        data,  # InputTaskStatusUpdate
    ) -> InputTask:
        """
        User (or a user-authenticated client) explicitly updates task status.

        The mirror of ``update_task_status_from_agent``: ownership is checked,
        the caller's allowed target set is narrower than the full status
        vocabulary, and the change is then written through the auditing
        ``update_task_status`` so it lands in the history and the comment feed
        attributed to the user.

        This exists for work executed *outside* cinna — a desktop client running
        the task locally reports progress here. It records what the client says;
        it never touches sessions. Where cinna is itself executing the task, the
        session-driven recompute (``sync_task_status_from_sessions``) keeps the
        last word.

        Args:
            db_session: Database session
            task_id: Task UUID
            user_id: User requesting the change (must own the task)
            data: InputTaskStatusUpdate with status and optional reason

        Returns:
            Updated InputTask

        Raises:
            TaskNotFoundError, PermissionDeniedError, ValidationError
        """
        InputTaskService.get_task_with_ownership_check(
            db_session=db_session,
            task_id=task_id,
            user_id=user_id,
        )

        # ``new`` is the create state, ``refining`` belongs to the refine flow,
        # and ``archived`` has its own route (which owns ``archived_at``).
        allowed_user_statuses = {
            "open", "in_progress", "blocked", "completed", "error", "cancelled",
        }
        if data.status not in allowed_user_statuses:
            raise ValidationError(
                f"User can only set status to: {sorted(allowed_user_statuses)}"
            )

        return InputTaskService.update_task_status(
            db_session=db_session,
            task_id=task_id,
            new_status=data.status,
            changed_by_user_id=user_id,
            reason=data.reason,
        )

    @staticmethod
    def list_subtasks(
        db_session: DBSession,
        parent_task_id: UUID,
    ) -> tuple[list[InputTaskPublicExtended], int]:
        """
        List direct subtasks of a task with progress counts.

        Args:
            db_session: Database session
            parent_task_id: Parent task UUID

        Returns:
            Tuple of (list of extended public subtasks, total count)
        """
        subtasks = db_session.exec(
            select(InputTask)
            .where(InputTask.parent_task_id == parent_task_id)
            .order_by(InputTask.created_at.asc())
        ).all()

        data = []
        for t in subtasks:
            progress = InputTaskService.get_subtask_progress(db_session, t.id)
            data.append(
                InputTaskPublicExtended(
                    **t.model_dump(),
                    subtask_count=progress["total"],
                    subtask_completed_count=progress["completed"],
                )
            )
        return data, len(data)

    @staticmethod
    def list_agent_tasks(
        db_session: DBSession,
        user_id: UUID,
        status: str | None = None,
        scope: str = "assigned",
    ) -> tuple[list[InputTaskPublicExtended], int]:
        """
        List tasks for agent MCP tool consumption.

        Args:
            db_session: Database session
            user_id: Owner user UUID (agent-env calls via user JWT)
            status: Optional status filter string (passed to parse_status_filter)
            scope: "assigned" (default) | "created" | "team" — currently all return
                   owner-scoped tasks; scope filtering is a future enhancement

        Returns:
            Tuple of (list of extended public tasks, total count)
        """
        status_filter = InputTaskService.parse_status_filter(status)

        stmt = select(InputTask).where(InputTask.owner_id == user_id)
        if status_filter:
            stmt = stmt.where(InputTask.status.in_(status_filter))

        tasks = db_session.exec(
            stmt.order_by(InputTask.created_at.desc()).limit(100)
        ).all()

        data = []
        for t in tasks:
            progress = InputTaskService.get_subtask_progress(db_session, t.id)
            agent_name = None
            if t.selected_agent_id:
                agent = db_session.get(Agent, t.selected_agent_id)
                agent_name = agent.name if agent else None
            data.append(
                InputTaskPublicExtended(
                    **t.model_dump(),
                    agent_name=agent_name,
                    subtask_count=progress["total"],
                    subtask_completed_count=progress["completed"],
                )
            )
        return data, len(data)

    @staticmethod
    def get_agent_task_details(
        db_session: DBSession,
        task_id: UUID,
        user_id: UUID,
    ) -> dict:
        """
        Get task details formatted for agent MCP tool consumption.

        Returns a simplified dict view with recent comments, subtask list, and
        progress counts — served to pre-protocol environments (the retired
        ``agent_task`` get_details tool; current ones use the `cinna` server's tasks_get).

        Args:
            db_session: Database session
            task_id: Task UUID
            user_id: Requesting user (ownership check)

        Returns:
            Dict with task, title, description, status, priority, assigned_to,
            recent_comments, subtasks, subtask_progress keys.

        Raises:
            TaskNotFoundError: If task not found or not owned by user
        """
        task = InputTaskService.get_task_with_ownership_check(
            db_session=db_session,
            task_id=task_id,
            user_id=user_id,
        )

        # Resolve assigned agent name
        agent_name = None
        if task.selected_agent_id:
            agent = db_session.get(Agent, task.selected_agent_id)
            if agent:
                agent_name = agent.name

        # Recent comments (last 10, oldest first for context)
        from app.services.tasks.task_comment_service import TaskCommentService
        comments, _ = TaskCommentService.list_comments(
            db_session=db_session, task_id=task_id, skip=0, limit=10
        )
        recent_comments = [
            {
                "author": c.author_name or "System",
                "content": c.content,
                "created_at": c.created_at.isoformat(),
                "has_files": len(c.inline_attachments) > 0,
            }
            for c in comments[-10:]
        ]

        # Subtask progress
        progress = InputTaskService.get_subtask_progress(db_session, task_id)

        # Subtasks list
        subtasks_raw = db_session.exec(
            select(InputTask).where(InputTask.parent_task_id == task_id)
        ).all()
        subtasks = []
        for s in subtasks_raw:
            sub_agent_name = None
            if s.selected_agent_id:
                a = db_session.get(Agent, s.selected_agent_id)
                sub_agent_name = a.name if a else None
            subtasks.append({
                "task": s.short_code,
                "title": s.title or s.original_message[:100],
                "status": s.status,
                "assigned_to": sub_agent_name,
            })

        # Collect task file info (InputTaskFile uploads + TaskAttachment files)
        task_files_info = InputTaskService._collect_task_files_info(db_session, task_id)

        result = {
            "task": task.short_code,
            "title": task.title or task.original_message[:100],
            "description": task.current_description,
            "status": task.status,
            "priority": task.priority,
            "assigned_to": agent_name,
            "recent_comments": recent_comments,
            "subtasks": subtasks,
            "subtask_progress": progress,
        }
        if task_files_info:
            result["files"] = task_files_info
        return result

    @staticmethod
    def _collect_task_files_info(
        db_session: DBSession,
        task_id: UUID,
    ) -> list[dict]:
        """
        Collect metadata for all files associated with a task.

        Gathers both InputTaskFile (user-uploaded files linked to the task)
        and TaskAttachment (agent/user attachments on the task or its comments).

        Returns:
            List of dicts with file_name, file_size, content_type, source, storage_path.
        """
        files_info: list[dict] = []
        seen_filenames: set[str] = set()

        # 1. InputTaskFile → FileUpload (user uploads linked to task)
        task_files = db_session.exec(
            select(FileUpload)
            .join(InputTaskFile, FileUpload.id == InputTaskFile.file_id)
            .where(InputTaskFile.task_id == task_id)
        ).all()
        for f in task_files:
            if f.filename not in seen_filenames:
                files_info.append({
                    "file_name": f.filename,
                    "file_size": f.file_size,
                    "content_type": f.mime_type or "application/octet-stream",
                    "source": "task_upload",
                    "storage_path": f.file_path,
                })
                seen_filenames.add(f.filename)

        # 2. TaskAttachment (standalone + comment-linked)
        attachments = db_session.exec(
            select(TaskAttachment)
            .where(TaskAttachment.task_id == task_id)
            .order_by(TaskAttachment.created_at.asc())
        ).all()
        for att in attachments:
            if att.file_name not in seen_filenames:
                files_info.append({
                    "file_name": att.file_name,
                    "file_size": att.file_size,
                    "content_type": att.content_type or "application/octet-stream",
                    "source": "task_attachment",
                    "storage_path": att.file_path,
                })
                seen_filenames.add(att.file_name)

        return files_info

    @staticmethod
    async def upload_task_files_to_agent_env(
        db_session: DBSession,
        task_details: dict,
        source_session_id: UUID,
    ) -> dict:
        """
        Upload task files to the calling agent's environment workspace.

        Resolves the agent environment from the source session, then uploads all
        task files to /app/workspace/app-data/uploads/task_{SHORT_CODE}/ in the
        container.
        Adds 'uploaded_files' list with local paths to the task_details dict.
        Always strips the internal 'files' key before returning.

        Args:
            db_session: Database session
            task_details: Dict returned by get_agent_task_details (modified in place)
            source_session_id: The calling session UUID (to resolve environment)

        Returns:
            The task_details dict with 'uploaded_files' added if files were uploaded.
        """
        import httpx
        from app.models.environments.environment import AgentEnvironment

        files_info = task_details.pop("files", None)
        if not files_info:
            return task_details

        # Resolve environment from session
        session = db_session.get(Session, source_session_id)
        if not session:
            logger.warning(f"Session {source_session_id} not found for task file upload")
            return task_details

        environment = db_session.get(AgentEnvironment, session.environment_id)
        if not environment or environment.status != "running":
            logger.warning(
                f"Environment {session.environment_id} not available for task file upload"
            )
            return task_details

        container_name = environment.config.get("container_name", f"agent-{environment.id}")
        port = environment.config.get("port", 8000)
        base_url = f"http://{container_name}:{port}"
        auth_token = environment.config.get("auth_token")
        headers = {"Authorization": f"Bearer {auth_token}"} if auth_token else {}

        short_code = task_details.get("task", "unknown")
        subfolder = f"task_{short_code}"

        uploaded_files: list[dict] = []
        from app.core.config import settings
        upload_base = Path(settings.UPLOAD_BASE_PATH)
        allowed_base = upload_base.parent.resolve()

        async with httpx.AsyncClient(timeout=30.0) as client:
            for file_info in files_info:
                try:
                    storage_path = file_info["storage_path"]
                    abs_path = (upload_base.parent / storage_path).resolve()

                    # Security: ensure path is within the upload directory
                    if not str(abs_path).startswith(str(allowed_base)):
                        logger.warning(f"Path traversal blocked: {storage_path}")
                        continue

                    if not abs_path.exists():
                        logger.warning(f"Task file not found on disk: {abs_path}")
                        continue

                    # Skip oversized files to avoid memory pressure
                    file_size = abs_path.stat().st_size
                    if file_size > settings.upload_max_file_size_bytes:
                        logger.warning(
                            f"Skipping oversized task file: {file_info['file_name']} "
                            f"({file_size} bytes)"
                        )
                        continue

                    content = abs_path.read_bytes()
                    filename = file_info["file_name"]

                    response = await client.post(
                        f"{base_url}/files/upload",
                        files={"file": (filename, content, "application/octet-stream")},
                        data={"filename": filename, "subfolder": subfolder},
                        headers=headers,
                    )
                    response.raise_for_status()
                    result = response.json()

                    uploaded_files.append({
                        "file_name": filename,
                        "path": result.get("path", f"./app-data/uploads/{subfolder}/{filename}"),
                        "size": file_info.get("file_size"),
                    })
                except Exception as e:
                    logger.warning(
                        f"Failed to upload task file '{file_info.get('file_name')}' "
                        f"to agent env: {e}"
                    )

        if uploaded_files:
            task_details["uploaded_files"] = uploaded_files

        return task_details
