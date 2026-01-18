"""
Input Tasks API routes.

Provides CRUD operations for input task management.
"""
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException
from sqlmodel import select

from app.api.deps import CurrentUser, SessionDep
from app.models import (
    InputTask,
    InputTaskCreate,
    InputTaskUpdate,
    InputTaskPublic,
    InputTaskPublicExtended,
    InputTasksPublic,
    InputTasksPublicExtended,
    InputTaskStatus,
    RefineTaskRequest,
    RefineTaskResponse,
    ExecuteTaskRequest,
    ExecuteTaskResponse,
    SessionCreate,
    SessionsPublic,
    SessionPublic,
    Message,
    Agent,
)
from app.services.input_task_service import InputTaskService
from app.services.ai_functions_service import AIFunctionsService
from app.services.session_service import SessionService

router = APIRouter(prefix="/tasks", tags=["tasks"])


@router.post("/", response_model=InputTaskPublic)
def create_task(
    *, session: SessionDep, current_user: CurrentUser, task_in: InputTaskCreate
) -> Any:
    """
    Create a new input task.
    """
    # Verify agent exists if specified
    if task_in.selected_agent_id:
        agent = session.get(Agent, task_in.selected_agent_id)
        if not agent:
            raise HTTPException(status_code=404, detail="Agent not found")
        if not current_user.is_superuser and agent.owner_id != current_user.id:
            raise HTTPException(status_code=400, detail="Not enough permissions")

    task = InputTaskService.create_task(
        db_session=session, user_id=current_user.id, data=task_in
    )
    return task


@router.get("/", response_model=InputTasksPublicExtended)
def list_tasks(
    session: SessionDep,
    current_user: CurrentUser,
    skip: int = 0,
    limit: int = 100,
    status: str | None = None,
    user_workspace_id: str | None = None,
) -> Any:
    """
    List user's input tasks.

    Args:
        skip: Number of records to skip
        limit: Number of records to return
        status: Filter by status. Can be:
            - "active": NEW, REFINING, READY, RUNNING, PENDING_INPUT
            - "completed": COMPLETED
            - "archived": ARCHIVED
            - "all": No filter
            - Specific status name (e.g., "new", "running")
        user_workspace_id: Optional workspace filter
            - None (not provided): returns all tasks
            - Empty string (""): filters for default workspace (NULL)
            - UUID string: filters for that workspace
    """
    # Parse status filter
    status_filter = None
    if status:
        if status == "active":
            status_filter = [
                InputTaskStatus.NEW,
                InputTaskStatus.REFINING,
                InputTaskStatus.READY,
                InputTaskStatus.RUNNING,
                InputTaskStatus.PENDING_INPUT,
                InputTaskStatus.ERROR,
            ]
        elif status == "completed":
            status_filter = [InputTaskStatus.COMPLETED]
        elif status == "archived":
            status_filter = [InputTaskStatus.ARCHIVED]
        elif status == "all":
            status_filter = None
        else:
            # Single status filter
            status_filter = [status]

    # Parse workspace filter
    workspace_filter: uuid.UUID | None = None
    apply_workspace_filter = False

    if user_workspace_id is None:
        apply_workspace_filter = False
    elif user_workspace_id == "":
        workspace_filter = None
        apply_workspace_filter = True
    else:
        try:
            workspace_filter = uuid.UUID(user_workspace_id)
            apply_workspace_filter = True
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid workspace ID format")

    results, count = InputTaskService.list_tasks(
        db_session=session,
        user_id=current_user.id,
        status_filter=status_filter,
        user_workspace_id=workspace_filter,
        apply_workspace_filter=apply_workspace_filter,
        skip=skip,
        limit=limit,
    )

    data = [
        InputTaskPublicExtended(
            **task.model_dump(),
            agent_name=agent_name,
        )
        for task, agent_name in results
    ]

    return InputTasksPublicExtended(data=data, count=count)


@router.get("/{id}", response_model=InputTaskPublicExtended)
def get_task(session: SessionDep, current_user: CurrentUser, id: uuid.UUID) -> Any:
    """
    Get a single input task with details.
    """
    result = InputTaskService.get_task_with_agent(db_session=session, task_id=id)
    if not result:
        raise HTTPException(status_code=404, detail="Task not found")

    task, agent_name = result

    # Verify ownership
    if not current_user.is_superuser and task.owner_id != current_user.id:
        raise HTTPException(status_code=400, detail="Not enough permissions")

    return InputTaskPublicExtended(
        **task.model_dump(),
        agent_name=agent_name,
    )


@router.patch("/{id}", response_model=InputTaskPublic)
def update_task(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    id: uuid.UUID,
    task_in: InputTaskUpdate,
) -> Any:
    """
    Update an input task.
    """
    task = session.get(InputTask, id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    # Verify ownership
    if not current_user.is_superuser and task.owner_id != current_user.id:
        raise HTTPException(status_code=400, detail="Not enough permissions")

    # Verify agent exists if being updated
    if task_in.selected_agent_id:
        agent = session.get(Agent, task_in.selected_agent_id)
        if not agent:
            raise HTTPException(status_code=404, detail="Agent not found")
        if not current_user.is_superuser and agent.owner_id != current_user.id:
            raise HTTPException(status_code=400, detail="Not enough permissions for this agent")

    updated_task = InputTaskService.update_task(
        db_session=session, task=task, data=task_in
    )
    return updated_task


@router.delete("/{id}")
def delete_task(
    session: SessionDep, current_user: CurrentUser, id: uuid.UUID
) -> Message:
    """
    Delete an input task.
    """
    task = session.get(InputTask, id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    # Verify ownership
    if not current_user.is_superuser and task.owner_id != current_user.id:
        raise HTTPException(status_code=400, detail="Not enough permissions")

    InputTaskService.delete_task(db_session=session, task=task)
    return Message(message="Task deleted successfully")


@router.post("/{id}/refine", response_model=RefineTaskResponse)
def refine_task(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    id: uuid.UUID,
    refine_in: RefineTaskRequest,
) -> Any:
    """
    Refine a task description with AI assistance.

    Uses AI to improve the task description based on user's feedback or comments.
    Appends the conversation to refinement_history.
    """
    task = session.get(InputTask, id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    # Verify ownership
    if not current_user.is_superuser and task.owner_id != current_user.id:
        raise HTTPException(status_code=400, detail="Not enough permissions")

    # Set status to refining
    if task.status == InputTaskStatus.NEW:
        InputTaskService.update_status(
            db_session=session, task=task, status=InputTaskStatus.REFINING
        )

    # Append user comment to history
    InputTaskService.append_to_refinement_history(
        db_session=session, task=task, role="user", content=refine_in.user_comment
    )

    # Call AI refinement service
    result = AIFunctionsService.refine_task(
        db=session,
        current_description=task.current_description,
        user_comment=refine_in.user_comment,
        agent_id=task.selected_agent_id,
        owner_id=current_user.id,
        refinement_history=task.refinement_history,
    )

    if not result.get("success"):
        return RefineTaskResponse(
            success=False,
            error=result.get("error", "Failed to refine task"),
        )

    # Update task description with refined version
    refined_description = result.get("refined_description", "")
    feedback_message = result.get("feedback_message", "")

    InputTaskService.update_description(
        db_session=session, task=task, new_description=refined_description
    )

    # Append AI response to history
    InputTaskService.append_to_refinement_history(
        db_session=session, task=task, role="ai", content=feedback_message
    )

    # Set status to ready (user can refine more or execute)
    InputTaskService.update_status(
        db_session=session, task=task, status=InputTaskStatus.READY
    )

    return RefineTaskResponse(
        success=True,
        refined_description=refined_description,
        feedback_message=feedback_message,
    )


@router.post("/{id}/execute", response_model=ExecuteTaskResponse)
def execute_task(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    id: uuid.UUID,
    execute_in: ExecuteTaskRequest,
) -> Any:
    """
    Execute a task by creating a session and sending the task description as the initial message.

    Requires a selected_agent_id to be set on the task.
    """
    task = session.get(InputTask, id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    # Verify ownership
    if not current_user.is_superuser and task.owner_id != current_user.id:
        raise HTTPException(status_code=400, detail="Not enough permissions")

    # Verify agent is selected
    if not task.selected_agent_id:
        raise HTTPException(status_code=400, detail="No agent selected for this task")

    # Verify agent exists and user owns it
    agent = session.get(Agent, task.selected_agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Selected agent not found")
    if not current_user.is_superuser and agent.owner_id != current_user.id:
        raise HTTPException(status_code=400, detail="Not enough permissions for this agent")

    # Verify agent has an active environment
    if not agent.active_environment_id:
        raise HTTPException(
            status_code=400,
            detail="Selected agent has no active environment",
        )

    # Create session
    session_data = SessionCreate(
        agent_id=task.selected_agent_id,
        title=task.current_description[:100],  # First 100 chars as title
        mode=execute_in.mode,
    )
    new_session = SessionService.create_session(
        db_session=session,
        user_id=current_user.id,
        data=session_data,
        source_task_id=task.id,
    )

    if not new_session:
        return ExecuteTaskResponse(
            success=False,
            error="Failed to create session",
        )

    # Link session to task and update status
    InputTaskService.link_session(
        db_session=session, task=task, session_id=new_session.id
    )

    return ExecuteTaskResponse(
        success=True,
        session_id=new_session.id,
    )


@router.post("/{id}/archive", response_model=InputTaskPublic)
def archive_task(
    session: SessionDep, current_user: CurrentUser, id: uuid.UUID
) -> Any:
    """
    Archive a completed or error task.
    """
    task = session.get(InputTask, id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    # Verify ownership
    if not current_user.is_superuser and task.owner_id != current_user.id:
        raise HTTPException(status_code=400, detail="Not enough permissions")

    # Only allow archiving completed or error tasks
    if task.status not in [InputTaskStatus.COMPLETED, InputTaskStatus.ERROR]:
        raise HTTPException(
            status_code=400,
            detail="Only completed or error tasks can be archived",
        )

    updated_task = InputTaskService.update_status(
        db_session=session, task=task, status=InputTaskStatus.ARCHIVED
    )

    return updated_task


@router.get("/{id}/sessions", response_model=SessionsPublic)
def list_task_sessions(
    session: SessionDep,
    current_user: CurrentUser,
    id: uuid.UUID,
    skip: int = 0,
    limit: int = 20,
) -> Any:
    """
    List all sessions spawned by this task.

    A single task can trigger multiple sessions (e.g., retries, re-runs).
    """
    task = session.get(InputTask, id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    # Verify ownership
    if not current_user.is_superuser and task.owner_id != current_user.id:
        raise HTTPException(status_code=400, detail="Not enough permissions")

    sessions = SessionService.list_task_sessions(
        db_session=session, task_id=id, limit=limit, offset=skip
    )

    return SessionsPublic(
        data=[SessionPublic(**s.model_dump()) for s in sessions],
        count=len(sessions),
    )
