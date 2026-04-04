"""
Input Task Service - handles task creation, retrieval, updates, and status management.
"""
from pathlib import Path
from uuid import UUID
from datetime import UTC, datetime
import logging
from typing import Any, Optional
from sqlmodel import Session as DBSession, select
from sqlalchemy.orm.attributes import flag_modified

from app.models import (
    InputTask,
    InputTaskCreate,
    InputTaskUpdate,
    InputTaskPublic,
    InputTaskPublicExtended,
    InputTaskDetailPublic,
    InputTaskStatus,
    Agent,
    Session,
    SessionMessage,
    SessionCreate,
    TaskStatusHistory,
    TaskStatusHistoryPublic,
    TaskComment,
    TaskCommentPublic,
    TaskAttachment,
    TaskAttachmentPublic,
)
from app.models.event import EventType
from app.models.email_message import EmailMessage
from app.models.outgoing_email_queue import OutgoingEmailQueue, OutgoingEmailStatus
from app.models.agent_email_integration import AgentEmailIntegration
from app.models.file_upload import FileUpload, FileUploadPublic, InputTaskFile
from app.models.agentic_team import AgenticTeamNode
from app.core.db import create_session
from app.utils import create_task_with_error_logging
from app.services.session_service import SessionService
from app.services.event_service import event_service
from app.services.ai_functions_service import AIFunctionsService
from app.services.activity_service import ActivityService

logger = logging.getLogger(__name__)


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

        return InputTaskPublicExtended(
            **task.model_dump(),
            agent_name=agent_name,
            sessions_count=sessions_count,
            latest_session_id=latest_session_id,
            attached_files=attached_files_public,
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
        )

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
        from app.models.user import User

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
            from app.models.agentic_team import AgenticTeam
            team = db_session.get(AgenticTeam, team_id)
            if team and team.task_prefix:
                prefix = team.task_prefix.upper()

        short_code = f"{prefix}-{counter}"
        return short_code, counter

    @staticmethod
    def create_task(
        db_session: DBSession,
        user_id: UUID,
        data: InputTaskCreate,
    ) -> InputTask:
        """
        Create a new input task with auto-generated short code.

        Args:
            db_session: Database session
            user_id: User ID creating the task
            data: Task creation data (including agent_initiated, auto_execute, source_session_id, file_ids)
        """
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

        task = InputTask(
            owner_id=user_id,
            original_message=data.original_message,
            current_description=data.original_message,  # Start with same as original
            selected_agent_id=selected_agent_id,
            user_workspace_id=data.user_workspace_id,
            agent_initiated=data.agent_initiated,
            auto_execute=data.auto_execute,
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
        )
        db_session.add(task)
        db_session.commit()
        db_session.refresh(task)

        # Attach files if provided
        if data.file_ids:
            InputTaskService.attach_files_to_task(
                db_session=db_session,
                task_id=task.id,
                file_ids=data.file_ids,
                user_id=user_id,
            )

        return task

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

        message_to_send = data.original_message

        # Auto-refine if agent has refiner_prompt
        if data.selected_agent_id:
            agent = db_session.get(Agent, data.selected_agent_id)
            if agent and agent.refiner_prompt and AIFunctionsService.is_available():
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

        return task, message_to_send

    @staticmethod
    async def create_task_from_agent(
        db_session: DBSession,
        user_id: UUID,
        data,  # AgentTaskCreate
    ) -> tuple[InputTask, str | None]:
        """
        Create a task from an agent MCP tool call.

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
        from app.models.agentic_team import AgenticTeamNode as _AgenticTeamNode
        from app.services.message_service import MessageService

        source_session_id = data.source_session_id
        user_workspace_id = None
        team_id = None

        # Resolve source session context
        if source_session_id:
            source_session = db_session.get(Session, source_session_id)
            if not source_session:
                raise ValidationError("Source session not found")
            user_workspace_id = source_session.user_workspace_id

            # Inherit team context from the calling session's task
            current_task = db_session.exec(
                select(InputTask).where(
                    InputTask.session_id == source_session_id,
                )
            ).first()
            if current_task and current_task.team_id:
                team_id = current_task.team_id

        # Resolve assigned_to (name → agent_id)
        selected_agent_id = None
        assigned_node_id = None
        resolved_name = None

        if data.assigned_to:
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

        task_data = InputTaskCreate(
            original_message=message,
            selected_agent_id=selected_agent_id,
            user_workspace_id=user_workspace_id,
            agent_initiated=True,
            auto_execute=bool(selected_agent_id),
            source_session_id=source_session_id,
            title=data.title,
            priority=data.priority,
            team_id=team_id,
            assigned_node_id=assigned_node_id,
        )

        if selected_agent_id:
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
                    content=f"Task {task.short_code} created in inbox",
                    message_metadata={
                        "task_created": True,
                        "task_id": str(task.id),
                        "inbox_task": True,
                    },
                )

        return task, resolved_name

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
        if not task.selected_agent_id:
            return False, None, "Task has no selected agent"

        content = message_to_send or task.current_description

        # Create session for target agent
        session_title = f"Task: {content[:50]}..." if len(content) > 50 else f"Task: {content}"
        session_create = SessionCreate(
            agent_id=task.selected_agent_id,
            title=session_title,
            mode=mode,
        )

        new_session = SessionService.create_session(
            db_session=db_session,
            user_id=user_id,
            data=session_create,
            source_task_id=task.id,
        )

        if not new_session:
            InputTaskService.update_status(
                db_session=db_session,
                task=task,
                status=InputTaskStatus.ERROR,
                error_message="Failed to create session for agent",
            )
            return False, None, "Failed to create session for agent"

        # Link session to task
        InputTaskService.link_session(
            db_session=db_session,
            task=task,
            session_id=new_session.id,
        )

        # Send message to session
        result = await SessionService.send_session_message(
            session_id=new_session.id,
            user_id=user_id,
            content=content,
            file_ids=file_ids,
            answers_to_message_id=None,
            get_fresh_db_session=create_session,
        )

        if result["action"] == "error":
            InputTaskService.update_status(
                db_session=db_session,
                task=task,
                status=InputTaskStatus.ERROR,
                error_message=f"Failed to send message: {result['message']}",
            )
            return False, None, f"Failed to send message: {result['message']}"

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

        # Get count before pagination
        count_statement = statement.with_only_columns(InputTask.id)

        # Add ordering
        if order_desc:
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
        task.sqlmodel_update(update_dict)
        task.updated_at = datetime.now(UTC)

        db_session.add(task)
        db_session.commit()
        db_session.refresh(task)
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
        if status == InputTaskStatus.IN_PROGRESS and task.executed_at is None:
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
                    "is_email_task": bool(task.source_email_message_id),
                    "source_agent_id": str(task.source_agent_id) if task.source_agent_id else None,
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
        task.session_id = session_id
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

    @staticmethod
    def execute_task_sync(
        db_session: DBSession,
        task: InputTask,
        user_id: UUID,
        mode: str = "conversation",
    ) -> tuple[bool, Session | None, str | None]:
        """
        Execute a task by creating a session (synchronous version).

        This method validates the agent, creates a session linked to the task,
        but does NOT send a message. Used by the API endpoint.

        For the async version that also sends a message, see execute_task().

        Args:
            db_session: Database session
            task: Task to execute
            user_id: User ID executing the task
            mode: Session mode (conversation, etc.)

        Returns:
            Tuple of (success, session, error_message)

        Raises:
            ValidationError: If no agent selected
            AgentNotFoundError: If agent doesn't exist
            PermissionDeniedError: If user doesn't have access to agent
        """
        # Verify agent is selected
        if not task.selected_agent_id:
            raise ValidationError("No agent selected for this task")

        # Verify agent exists and user has access (with active environment required)
        InputTaskService.verify_agent_access(
            db_session=db_session,
            agent_id=task.selected_agent_id,
            user_id=user_id,
            require_active_environment=True,
        )

        # Create session
        session_data = SessionCreate(
            agent_id=task.selected_agent_id,
            title=task.current_description[:100],  # First 100 chars as title
            mode=mode,
        )
        new_session = SessionService.create_session(
            db_session=db_session,
            user_id=user_id,
            data=session_data,
            source_task_id=task.id,
        )

        if not new_session:
            return False, None, "Failed to create session"

        # Link session to task and update status
        InputTaskService.link_session(
            db_session=db_session, task=task, session_id=new_session.id
        )

        return True, new_session, None

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

        # Compute or use forced status
        new_status = force_status or InputTaskService.compute_status_from_sessions(
            db_session, task_id
        )

        if not new_status or new_status == task.status:
            # No change needed
            return None

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
                    short_code = task.short_code or str(task.id)[:8]
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

        # Compose feedback content
        content_map = {
            "completed": f"[Sub-task completed] {summary}",
            "needs_input": f"[Sub-task needs input] {summary}",
            "error": f"[Sub-task error] {summary}",
        }
        content = content_map.get(state, f"[Sub-task update] {summary}")

        # Create message in source session (role="user" triggers agent processing)
        from app.services.message_service import MessageService  # circular import
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

    # ==================== Email Answer Methods ====================

    @staticmethod
    def send_email_answer(
        db_session: DBSession,
        task: InputTask,
        user_id: UUID,
        custom_message: str | None = None,
    ) -> dict:
        """
        Generate and queue an email reply for an email-originated task.

        1. Validate task has source_email_message_id
        2. Load original EmailMessage
        3. Get session results (last agent message)
        4. Generate AI reply (or use custom_message)
        5. Look up SMTP config via source_agent_id
        6. Create OutgoingEmailQueue entry

        Args:
            db_session: Database session
            task: The email-originated task
            user_id: User ID performing the action
            custom_message: Optional custom reply text (skips AI generation)

        Returns:
            dict with success, queue_entry_id, generated_reply, or error
        """
        # Validate task is email-originated
        if not task.source_email_message_id:
            return {
                "success": False,
                "error": "Task is not email-originated (no source_email_message_id)",
            }

        # Load original email
        email_msg = db_session.get(EmailMessage, task.source_email_message_id)
        if not email_msg:
            return {
                "success": False,
                "error": "Original email message not found",
            }

        # Get session results
        session_result = None
        if task.session_id:
            session = db_session.get(Session, task.session_id)
            if session and session.result_summary:
                session_result = session.result_summary
            else:
                # Try to get the last agent message from the session
                last_msg = db_session.exec(
                    select(SessionMessage)
                    .where(SessionMessage.session_id == task.session_id)
                    .where(SessionMessage.role == "agent")
                    .order_by(SessionMessage.timestamp.desc())
                ).first()
                if last_msg:
                    session_result = last_msg.content

        if not session_result and not custom_message:
            return {
                "success": False,
                "error": "No session results and no custom message provided",
            }

        # Generate reply
        reply_body = custom_message
        reply_subject = f"Re: {email_msg.subject}" if email_msg.subject else "Re: your email"

        if not custom_message:
            # Use AI to generate reply (with per-user provider routing)
            from app.models.user import User
            user = db_session.get(User, user_id)
            ai_result = AIFunctionsService.generate_email_reply(
                original_subject=email_msg.subject or "",
                original_body=email_msg.body or "",
                original_sender=email_msg.sender,
                session_result=session_result or "",
                task_description=task.current_description,
                user=user,
                db=db_session,
            )

            if not ai_result.get("success"):
                return {
                    "success": False,
                    "error": ai_result.get("error", "Failed to generate email reply"),
                }

            reply_body = ai_result["reply_body"]
            reply_subject = ai_result.get("reply_subject", reply_subject)

        # Look up SMTP config via source_agent_id
        source_agent_id = task.source_agent_id or email_msg.agent_id
        integration = db_session.exec(
            select(AgentEmailIntegration)
            .where(AgentEmailIntegration.agent_id == source_agent_id)
        ).first()

        if not integration or not integration.outgoing_server_id:
            return {
                "success": False,
                "error": "No outgoing email server configured for this agent",
            }

        # Create outgoing email queue entry
        queue_entry = OutgoingEmailQueue(
            agent_id=source_agent_id,
            input_task_id=task.id,
            session_id=task.session_id,
            recipient=email_msg.sender,
            subject=reply_subject,
            body=reply_body,
            in_reply_to=email_msg.email_message_id,
            references=email_msg.email_message_id,
            status=OutgoingEmailStatus.PENDING,
        )
        db_session.add(queue_entry)
        db_session.commit()
        db_session.refresh(queue_entry)

        # Delete reply_pending activity since reply is being sent
        deleted_activity = ActivityService.delete_activity_by_task_and_type(
            db_session=db_session,
            input_task_id=task.id,
            activity_type="email_task_reply_pending"
        )
        if deleted_activity:
            create_task_with_error_logging(
                event_service.emit_event(
                    event_type=EventType.ACTIVITY_DELETED,
                    model_id=deleted_activity.id,
                    user_id=deleted_activity.user_id,
                    meta={
                        "activity_type": "email_task_reply_pending",
                        "input_task_id": str(task.id),
                    }
                ),
                task_name=f"emit_activity_deleted_reply_pending_{task.id}"
            )

        logger.info(
            f"Task {task.id}: queued email reply {queue_entry.id} "
            f"to {email_msg.sender} via agent {source_agent_id}"
        )

        return {
            "success": True,
            "queue_entry_id": queue_entry.id,
            "generated_reply": reply_body,
        }

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

        from_status = task.status

        # Validate the transition
        valid_nexts = InputTaskStatus.VALID_TRANSITIONS.get(from_status, set())
        if new_status not in valid_nexts and new_status != from_status:
            raise ValidationError(
                f"Cannot transition task from '{from_status}' to '{new_status}'. "
                f"Valid transitions: {sorted(valid_nexts)}"
            )

        if new_status == from_status:
            return task  # No-op

        # Create immutable status history record
        history = TaskStatusHistory(
            task_id=task_id,
            from_status=from_status,
            to_status=new_status,
            changed_by_agent_id=changed_by_agent_id,
            changed_by_user_id=changed_by_user_id,
            reason=reason,
        )
        db_session.add(history)

        # Post system comment visible in activity feed
        comment_content = InputTaskService._build_status_change_comment(
            from_status=from_status,
            to_status=new_status,
            agent_id=changed_by_agent_id,
            user_id=changed_by_user_id,
            is_system=changed_by_system,
            reason=reason,
            db_session=db_session,
        )
        from app.services.task_comment_service import TaskCommentService
        TaskCommentService.add_system_comment(
            db_session=db_session,
            task_id=task_id,
            content=comment_content,
            comment_type="status_change",
            comment_meta={"from_status": from_status, "to_status": new_status},
        )

        # Update task
        task.status = new_status
        task.updated_at = datetime.now(UTC)

        if new_status == InputTaskStatus.IN_PROGRESS and task.executed_at is None:
            task.executed_at = datetime.now(UTC)
        elif new_status == InputTaskStatus.COMPLETED:
            task.completed_at = datetime.now(UTC)
        elif new_status == InputTaskStatus.ARCHIVED:
            task.archived_at = datetime.now(UTC)

        db_session.add(task)
        db_session.commit()
        db_session.refresh(task)

        # Emit real-time event
        create_task_with_error_logging(
            event_service.emit_event(
                event_type=EventType.TASK_STATUS_CHANGED,
                model_id=task_id,
                user_id=task.owner_id,
                meta={
                    "task_id": str(task_id),
                    "short_code": task.short_code,
                    "from_status": from_status,
                    "to_status": new_status,
                    "changed_by_agent_id": str(changed_by_agent_id) if changed_by_agent_id else None,
                    "changed_by_user_id": str(changed_by_user_id) if changed_by_user_id else None,
                }
            ),
            task_name=f"emit_task_status_changed_{task_id}"
        )

        # If completed and has parent, notify parent
        if new_status == InputTaskStatus.COMPLETED and task.parent_task_id:
            create_task_with_error_logging(
                InputTaskService._notify_parent_task_async(
                    parent_task_id=task.parent_task_id,
                    completed_subtask_short_code=task.short_code or str(task_id),
                    completed_subtask_agent_id=task.selected_agent_id,
                ),
                task_name=f"notify_parent_task_{task.parent_task_id}"
            )

        logger.info(f"Task {task_id} status: {from_status} → {new_status}")
        return task

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
            from app.models.user import User
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
                from app.services.task_comment_service import TaskCommentService
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
            from app.models.agentic_team import AgenticTeamNode
            node = db_session.get(AgenticTeamNode, task.assigned_node_id)
            if node:
                assigned_node_name = node.name
        if task.team_id:
            from app.models.agentic_team import AgenticTeam
            team = db_session.get(AgenticTeam, task.team_id)
            if team:
                team_name = team.name

        # Load sessions info
        sessions_count, latest_session_id = SessionService.get_task_sessions_info(
            db_session=db_session, task_id=task_id
        )

        # Load comments
        from app.services.task_comment_service import TaskCommentService
        comments, _ = TaskCommentService.list_comments(db_session=db_session, task_id=task_id)

        # Load standalone attachments
        from app.services.task_attachment_service import TaskAttachmentService
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
                from app.models.user import User
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
    ) -> InputTask:
        """
        Create a subtask (team agents only).

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
        from app.models.agentic_team import AgenticTeamNode, AgenticTeamConnection
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
        if data.assigned_to:
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
            auto_execute=bool(assigned_agent_id),
            source_session_id=data.source_session_id,
            title=data.title,
            priority=data.priority,
            team_id=parent.team_id,
            assigned_node_id=assigned_node_id,
            parent_task_id=parent_task_id,
        )
        subtask = InputTaskService.create_task(
            db_session=db_session,
            user_id=parent.owner_id,
            data=subtask_create,
        )

        # Set created_by_node_id (not in InputTaskCreate, set directly)
        subtask.created_by_node_id = creator_node.id
        db_session.add(subtask)
        db_session.commit()
        db_session.refresh(subtask)

        # Post system comment on parent
        from app.services.task_comment_service import TaskCommentService
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
                InputTaskService._auto_execute_subtask(db_session, subtask),
                task_name=f"auto_execute_subtask_{subtask.id}"
            )

        logger.info(f"Subtask {subtask.short_code} created under {parent.short_code}")
        return subtask

    @staticmethod
    async def _auto_execute_subtask(
        db_session: DBSession,
        subtask: InputTask,
    ) -> None:
        """Auto-execute a newly created subtask by creating a session."""
        try:
            if not subtask.selected_agent_id:
                return
            from app.core.db import create_session as make_db
            with make_db() as db:
                task = db.get(InputTask, subtask.id)
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
                    logger.warning(f"Auto-execute subtask {subtask.id} failed: {error}")
        except Exception as e:
            logger.error(f"Error auto-executing subtask {subtask.id}: {e}", exc_info=True)

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
        progress counts — optimised for the mcp__agent_task__get_details tool.

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
        from app.services.task_comment_service import TaskCommentService
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
        task files to /app/workspace/uploads/task_{SHORT_CODE}/ in the container.
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
        from app.models.environment import AgentEnvironment

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
                        "path": result.get("path", f"./uploads/{subfolder}/{filename}"),
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
