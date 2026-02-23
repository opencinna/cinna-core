"""
Input Task Service - handles task creation, retrieval, updates, and status management.
"""
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
    InputTaskPublicExtended,
    InputTaskStatus,
    Agent,
    Session,
    SessionMessage,
    SessionCreate,
)
from app.core.db import engine, create_session
from app.services.session_service import SessionService

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
                InputTaskStatus.RUNNING,
                InputTaskStatus.PENDING_INPUT,
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
        from app.models.file_upload import FileUploadPublic

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
        )

        data = []
        for task, agent_name in results:
            sessions_count, latest_session_id = SessionService.get_task_sessions_info(
                db_session=db_session, task_id=task.id
            )
            data.append(
                InputTaskPublicExtended(
                    **task.model_dump(),
                    agent_name=agent_name,
                    sessions_count=sessions_count,
                    latest_session_id=latest_session_id,
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
    def create_task(
        db_session: DBSession,
        user_id: UUID,
        data: InputTaskCreate,
    ) -> InputTask:
        """
        Create a new input task.

        Args:
            db_session: Database session
            user_id: User ID creating the task
            data: Task creation data (including agent_initiated, auto_execute, source_session_id, file_ids)
        """
        task = InputTask(
            owner_id=user_id,
            original_message=data.original_message,
            current_description=data.original_message,  # Start with same as original
            selected_agent_id=data.selected_agent_id,
            user_workspace_id=data.user_workspace_id,
            agent_initiated=data.agent_initiated,
            auto_execute=data.auto_execute,
            source_session_id=data.source_session_id,
            status=InputTaskStatus.NEW,
            refinement_history=[],
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
        from app.services.ai_functions_service import AIFunctionsService

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
        if status == InputTaskStatus.RUNNING:
            task.executed_at = datetime.now(UTC)
        elif status == InputTaskStatus.COMPLETED:
            task.completed_at = datetime.now(UTC)
        elif status == InputTaskStatus.ARCHIVED:
            task.archived_at = datetime.now(UTC)

        db_session.add(task)
        db_session.commit()
        db_session.refresh(task)
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
        """Link a session to the task"""
        task.session_id = session_id
        task.status = InputTaskStatus.RUNNING
        task.executed_at = datetime.now(UTC)
        task.updated_at = datetime.now(UTC)

        db_session.add(task)
        db_session.commit()
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
            InputTaskStatus.RUNNING,
            InputTaskStatus.PENDING_INPUT,
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
        from app.services.ai_functions_service import AIFunctionsService

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
        Compute task status from all connected sessions.

        Status computation logic (priority order):
        - If ANY session has status='error' → ERROR
        - If ANY session has unanswered tool_questions → PENDING_INPUT
        - If ANY session is active with interaction_status='running' → RUNNING
        - If ALL sessions are completed → COMPLETED
        - Otherwise → RUNNING (active but not streaming)

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

        # Apply priority: error > pending_input > running > completed
        if has_error:
            return InputTaskStatus.ERROR
        if has_pending_input:
            return InputTaskStatus.PENDING_INPUT
        if has_running:
            return InputTaskStatus.RUNNING
        if all_completed:
            return InputTaskStatus.COMPLETED

        # Default: session exists but not streaming
        return InputTaskStatus.RUNNING

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
            InputTaskStatus.RUNNING,
            InputTaskStatus.PENDING_INPUT,
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

            with DBSession(engine) as db:
                # Get session to find source_task_id
                session = db.get(Session, UUID(session_id))
                if not session or not session.source_task_id:
                    return  # Session not linked to a task

                task = db.get(InputTask, session.source_task_id)
                if not task:
                    return

                # Only update if task is in execution phase
                if task.status in [
                    InputTaskStatus.RUNNING,
                    InputTaskStatus.PENDING_INPUT,
                    InputTaskStatus.COMPLETED,
                    InputTaskStatus.ERROR,
                ]:
                    InputTaskService.update_status(
                        db_session=db,
                        task=task,
                        status=InputTaskStatus.RUNNING,
                    )
                    logger.info(f"Task {task.id} status synced to RUNNING (STREAM_STARTED)")

        except Exception as e:
            logger.error(f"Error handling STREAM_STARTED for task sync: {e}", exc_info=True)

    @staticmethod
    async def handle_stream_completed(event_data: dict[str, Any]) -> None:
        """
        React to STREAM_COMPLETED events and compute task status from sessions.

        Args:
            event_data: Full event dict with type, model_id, meta, user_id, timestamp
        """
        try:
            meta = event_data.get("meta", {})
            session_id = meta.get("session_id")

            if not session_id:
                logger.debug("STREAM_COMPLETED event missing session_id, skipping task sync")
                return

            with DBSession(engine) as db:
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

            with DBSession(engine) as db:
                # Get session to find source_task_id
                session = db.get(Session, UUID(session_id))
                if not session or not session.source_task_id:
                    return  # Session not linked to a task

                task = db.get(InputTask, session.source_task_id)
                if not task:
                    return

                # Only update if task is in execution phase
                if task.status in [
                    InputTaskStatus.RUNNING,
                    InputTaskStatus.PENDING_INPUT,
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
        from app.services.event_service import event_service
        from app.models.event import EventType

        try:
            meta = event_data.get("meta", {})
            session_id = meta.get("session_id")
            todos = meta.get("todos", [])
            user_id = event_data.get("user_id")

            if not session_id:
                logger.debug("TODO_LIST_UPDATED event missing session_id, skipping")
                return

            with DBSession(engine) as db:
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

            with DBSession(engine) as db:
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
        from app.services.message_service import MessageService

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
            from app.utils import create_task_with_error_logging
            from app.core.db import engine as db_engine
            from sqlmodel import Session as FreshDBSession

            create_task_with_error_logging(
                SessionService.initiate_stream(
                    session_id=task.source_session_id,
                    get_fresh_db_session=lambda: FreshDBSession(db_engine),
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
        from app.models.email_message import EmailMessage
        from app.models.outgoing_email_queue import OutgoingEmailQueue, OutgoingEmailStatus
        from app.models.agent_email_integration import AgentEmailIntegration
        from app.services.ai_functions_service import AIFunctionsService

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
            # Use AI to generate reply
            ai_result = AIFunctionsService.generate_email_reply(
                original_subject=email_msg.subject or "",
                original_body=email_msg.body or "",
                original_sender=email_msg.sender,
                session_result=session_result or "",
                task_description=task.current_description,
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
        from app.models.file_upload import FileUpload, InputTaskFile

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
        from app.models.file_upload import FileUpload, InputTaskFile

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
        from app.models.file_upload import InputTaskFile

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
        from app.models.file_upload import FileUpload, InputTaskFile

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
        from app.models.file_upload import FileUpload, InputTaskFile

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
