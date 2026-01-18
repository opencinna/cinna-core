"""
Input Task Service - handles task creation, retrieval, updates, and status management.
"""
from uuid import UUID
from datetime import datetime
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
from app.core.db import engine
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
        Get task with extended info (agent name, sessions count).

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

        return InputTaskPublicExtended(
            **task.model_dump(),
            agent_name=agent_name,
            sessions_count=sessions_count,
            latest_session_id=latest_session_id,
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
            data: Task creation data (including agent_initiated, auto_execute, source_session_id)
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
                        task.updated_at = datetime.utcnow()
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
        message_to_send: str,
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
            message_to_send: The message to send (possibly refined)

        Returns:
            Tuple of (success, session, error_message)
        """
        if not task.selected_agent_id:
            return False, None, "Task has no selected agent"

        # Create session for target agent
        session_title = f"Task: {message_to_send[:50]}..." if len(message_to_send) > 50 else f"Task: {message_to_send}"
        session_create = SessionCreate(
            agent_id=task.selected_agent_id,
            title=session_title,
            mode="conversation",
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
            content=message_to_send,
            file_ids=None,
            answers_to_message_id=None,
            get_fresh_db_session=lambda: DBSession(engine)
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
        task.updated_at = datetime.utcnow()

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
        task.updated_at = datetime.utcnow()

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
        task.updated_at = datetime.utcnow()

        if error_message:
            task.error_message = error_message
        elif status != InputTaskStatus.ERROR:
            task.error_message = None

        # Update timestamps based on status
        if status == InputTaskStatus.RUNNING:
            task.executed_at = datetime.utcnow()
        elif status == InputTaskStatus.COMPLETED:
            task.completed_at = datetime.utcnow()
        elif status == InputTaskStatus.ARCHIVED:
            task.archived_at = datetime.utcnow()

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
            "timestamp": datetime.utcnow().isoformat(),
        }

        if task.refinement_history is None:
            task.refinement_history = []

        task.refinement_history.append(history_item)
        flag_modified(task, "refinement_history")
        task.updated_at = datetime.utcnow()

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
        task.executed_at = datetime.utcnow()
        task.updated_at = datetime.utcnow()

        db_session.add(task)
        db_session.commit()
        db_session.refresh(task)
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
            # Check for error status
            if session.status == "error":
                has_error = True
                continue

            # Check if session is not completed
            if session.status != "completed":
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
                    task.updated_at = datetime.utcnow()
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
