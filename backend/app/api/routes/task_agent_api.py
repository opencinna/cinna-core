"""
Agent Task API Routes — internal endpoints called by MCP tools in agent environments.

Authentication: These endpoints use the same Bearer token auth as other agent-facing
endpoints (CurrentUser resolves agent identity via JWT token created per-session).

Agent identity is resolved via task.selected_agent_id (the agent assigned to the task).
The caller authenticates as the task owner using their JWT; the agent identity is then
derived from the task record itself.

Routes:
  POST  /agent/tasks/create              — Agent creates a new standalone task
  GET   /agent/tasks/by-code/{code}      — Resolve short code to task_id UUID
  POST  /agent/tasks/current/comment     — Agent posts comment (session-resolved task)
  POST  /agent/tasks/current/status      — Agent updates status (session-resolved task)
  GET   /agent/tasks/current/details     — Agent gets details (session-resolved task)
  POST  /agent/tasks/current/subtask     — Agent creates subtask under its current task
  POST  /agent/tasks/{task_id}/comment   — Agent posts comment (explicit task_id)
  POST  /agent/tasks/{task_id}/status    — Agent updates task status (explicit task_id)
  POST  /agent/tasks/{task_id}/subtask   — Agent creates subtask (explicit task_id)
  GET   /agent/tasks/my-tasks            — Agent lists assigned tasks
  GET   /agent/tasks/{task_id}/details   — Agent gets task detail (explicit task_id)
"""
import logging
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException

from app.api.deps import CurrentUser, SessionDep
from app.models import (
    InputTask,
    InputTasksPublicExtended,
    AgentTaskCreate,
    AgentTaskStatusUpdate,
    AgentSubtaskCreate,
    AgentTaskOperationResponse,
    AgentCommentResponse,
    AgentTaskCommentCreate,
    Session,
)
from app.services.tasks.input_task_service import (
    InputTaskService,
    InputTaskError,
    ValidationError,
)
from app.services.tasks.task_comment_service import TaskCommentService, AgentCommentResult

logger = logging.getLogger(__name__)

router = APIRouter(tags=["agent-tasks"])


def _handle_error(e: InputTaskError) -> None:
    """Convert service exceptions to HTTP exceptions."""
    raise HTTPException(status_code=e.status_code, detail=e.message)


def _resolve_task_from_session(
    db_session, session_id: uuid.UUID,
) -> InputTask:
    """Resolve the current task linked to a session.

    Uses Session.source_task_id (immutable FK set at session creation) rather
    than InputTask.session_id which is overwritten on task re-execution and
    would break resolution for agents still running in older sessions.
    """
    session_obj = db_session.get(Session, session_id)
    if not session_obj:
        raise ValidationError("Session not found")
    if not session_obj.source_task_id:
        raise ValidationError("No task linked to this session")
    task = db_session.get(InputTask, session_obj.source_task_id)
    if not task:
        raise ValidationError("No task linked to this session")
    return task


def _build_agent_comment_response(
    task: InputTask, result: AgentCommentResult,
) -> AgentCommentResponse:
    """Map service result to agent-facing response model."""
    message = None
    if result.attachments_failed > 0:
        message = (
            f"{result.attachments_failed} of "
            f"{result.attachments_created + result.attachments_failed} "
            f"file(s) could not be attached. "
            f"Check that the file paths exist in the agent workspace."
        )
    return AgentCommentResponse(
        comment_id=str(result.comment.id),
        task=task.short_code or "",
        attachments_count=result.attachments_created,
        failed_attachments=result.attachments_failed,
        message=message,
    )


@router.post("/agent/tasks/create", response_model=AgentTaskOperationResponse)
async def agent_create_task(
    *,
    db_session: SessionDep,
    current_user: CurrentUser,
    data: AgentTaskCreate,
) -> Any:
    """
    Agent creates a new standalone task.

    If assigned_to is provided, resolves agent by name and auto-executes.
    For team agents, the task inherits team context from the calling session's task.

    Called by the mcp__agent_task__create_task MCP tool.
    """
    try:
        task, resolved_name = await InputTaskService.create_task_from_agent(
            db_session=db_session,
            user_id=current_user.id,
            data=data,
        )
        return AgentTaskOperationResponse(
            success=True,
            task=task.short_code,
            assigned_to=resolved_name,
            message=f"Task {task.short_code} created",
        )
    except InputTaskError as e:
        _handle_error(e)


@router.get("/agent/tasks/by-code/{short_code}")
def agent_resolve_task_by_code(
    *,
    db_session: SessionDep,
    current_user: CurrentUser,
    short_code: str,
) -> Any:
    """
    Resolve a task short code (e.g. HR-17) to a task_id UUID.

    Called by MCP tools that accept a short_code param and need the UUID
    for subsequent API calls.
    """
    try:
        task = InputTaskService.get_task_by_short_code(
            db_session=db_session,
            short_code=short_code,
            user_id=current_user.id,
        )
        return {"task_id": str(task.id), "short_code": task.short_code}
    except InputTaskError as e:
        _handle_error(e)


@router.post("/agent/tasks/current/comment", response_model=AgentCommentResponse)
async def agent_add_comment_current(
    *,
    db_session: SessionDep,
    current_user: CurrentUser,
    data: AgentTaskCommentCreate,
) -> Any:
    """
    Agent posts a comment on its current task (resolved from session).

    Called by the mcp__agent_task__add_comment MCP tool when no task short code is specified.
    """
    try:
        if not data.source_session_id:
            raise ValidationError("source_session_id is required")
        task = _resolve_task_from_session(db_session, data.source_session_id)
        result = TaskCommentService.add_comment_from_agent(
            db_session=db_session,
            task_id=task.id,
            agent_id=task.selected_agent_id,
            data=data,
        )
        return _build_agent_comment_response(task, result)
    except InputTaskError as e:
        _handle_error(e)


@router.post("/agent/tasks/current/status", response_model=AgentTaskOperationResponse)
async def agent_update_status_current(
    *,
    db_session: SessionDep,
    current_user: CurrentUser,
    data: AgentTaskStatusUpdate,
) -> Any:
    """
    Agent updates status of its current task (resolved from session).

    Called by the mcp__agent_task__update_status MCP tool when no task short code is specified.
    """
    try:
        if not data.source_session_id:
            raise ValidationError("source_session_id is required")
        task = _resolve_task_from_session(db_session, data.source_session_id)
        if not task.selected_agent_id:
            raise ValidationError("Current task has no assigned agent")
        updated = InputTaskService.update_task_status_from_agent(
            db_session=db_session,
            task_id=task.id,
            agent_id=task.selected_agent_id,
            data=data,
        )
        return AgentTaskOperationResponse(
            success=True,
            task=updated.short_code,
            message=f"Status updated to {data.status}",
        )
    except InputTaskError as e:
        _handle_error(e)


@router.get("/agent/tasks/current/details")
async def agent_get_task_details_current(
    db_session: SessionDep,
    current_user: CurrentUser,
    source_session_id: uuid.UUID,
) -> Any:
    """
    Agent gets details of its current task (resolved from session).

    Called by the mcp__agent_task__get_details MCP tool when no task short code is specified.
    Automatically uploads task files to the calling agent's workspace.
    """
    try:
        task = _resolve_task_from_session(db_session, source_session_id)
        details = InputTaskService.get_agent_task_details(
            db_session=db_session,
            task_id=task.id,
            user_id=current_user.id,
        )
        return await InputTaskService.upload_task_files_to_agent_env(
            db_session, details, source_session_id,
        )
    except InputTaskError as e:
        _handle_error(e)


@router.post("/agent/tasks/current/subtask", response_model=AgentTaskOperationResponse)
async def agent_create_subtask_current(
    *,
    db_session: SessionDep,
    current_user: CurrentUser,
    data: AgentSubtaskCreate,
) -> Any:
    """
    Agent creates a subtask under its current task (resolved from session).

    Called by the mcp__agent_task__create_subtask MCP tool.
    """
    try:
        if not data.source_session_id:
            raise ValidationError("source_session_id is required")
        current_task = _resolve_task_from_session(db_session, data.source_session_id)
        if not current_task.selected_agent_id:
            raise ValidationError("Current task has no assigned agent")

        subtask = InputTaskService.create_subtask(
            db_session=db_session,
            parent_task_id=current_task.id,
            creating_agent_id=current_task.selected_agent_id,
            data=data,
        )
        return AgentTaskOperationResponse(
            success=True,
            task=subtask.short_code,
            parent_task=current_task.short_code,
            assigned_to=data.assigned_to,
            message=f"Subtask {subtask.short_code} created under {current_task.short_code}",
        )
    except InputTaskError as e:
        _handle_error(e)


@router.post("/agent/tasks/{task_id}/comment", response_model=AgentCommentResponse)
async def agent_add_comment(
    *,
    db_session: SessionDep,
    current_user: CurrentUser,
    task_id: uuid.UUID,
    data: AgentTaskCommentCreate,
) -> Any:
    """
    Agent posts a comment on a task (with optional workspace file attachments).

    Called by the mcp__agent_task__add_comment MCP tool.
    """
    try:
        task = InputTaskService.get_task_with_ownership_check(
            db_session=db_session,
            task_id=task_id,
            user_id=current_user.id,
        )
        result = TaskCommentService.add_comment_from_agent(
            db_session=db_session,
            task_id=task_id,
            agent_id=task.selected_agent_id,
            data=data,
        )
        return _build_agent_comment_response(task, result)
    except InputTaskError as e:
        _handle_error(e)


@router.post("/agent/tasks/{task_id}/status", response_model=AgentTaskOperationResponse)
async def agent_update_status(
    *,
    db_session: SessionDep,
    current_user: CurrentUser,
    task_id: uuid.UUID,
    data: AgentTaskStatusUpdate,
) -> Any:
    """
    Agent explicitly updates task status.

    Only for edge cases: blocked, completed (explicit), cancelled.
    Standard transitions (in_progress, completed) are handled automatically
    by the backend session lifecycle events.

    Called by the mcp__agent_task__update_status MCP tool.
    """
    try:
        task = InputTaskService.get_task_with_ownership_check(
            db_session=db_session,
            task_id=task_id,
            user_id=current_user.id,
        )
        if not task.selected_agent_id:
            raise ValidationError("Task has no assigned agent")

        updated = InputTaskService.update_task_status_from_agent(
            db_session=db_session,
            task_id=task_id,
            agent_id=task.selected_agent_id,
            data=data,
        )
        return AgentTaskOperationResponse(
            success=True,
            task=updated.short_code,
            message=f"Status updated to {data.status}",
        )
    except InputTaskError as e:
        _handle_error(e)


@router.post("/agent/tasks/{task_id}/subtask", response_model=AgentTaskOperationResponse)
async def agent_create_subtask(
    *,
    db_session: SessionDep,
    current_user: CurrentUser,
    task_id: uuid.UUID,
    data: AgentSubtaskCreate,
) -> Any:
    """
    Agent creates a subtask (team context required).

    Validates team membership and connection topology before creating the subtask.

    Called by the mcp__agent_task__create_subtask MCP tool.
    """
    try:
        task = InputTaskService.get_task_with_ownership_check(
            db_session=db_session,
            task_id=task_id,
            user_id=current_user.id,
        )
        if not task.selected_agent_id:
            raise ValidationError("Task has no assigned agent")

        subtask = InputTaskService.create_subtask(
            db_session=db_session,
            parent_task_id=task_id,
            creating_agent_id=task.selected_agent_id,
            data=data,
        )
        return AgentTaskOperationResponse(
            success=True,
            task=subtask.short_code,
            parent_task=task.short_code,
            assigned_to=data.assigned_to,
            message=f"Subtask {subtask.short_code} created under {task.short_code}",
        )
    except InputTaskError as e:
        _handle_error(e)


@router.get("/agent/tasks/my-tasks", response_model=InputTasksPublicExtended)
def agent_list_tasks(
    db_session: SessionDep,
    current_user: CurrentUser,
    status: str | None = None,
    scope: str = "assigned",
) -> Any:
    """
    Agent lists tasks assigned to them or created by them.

    scope values:
    - "assigned" (default): tasks where selected_agent_id matches the calling agent
    - "created": tasks created by this agent (agent_initiated=True from this agent)
    - "team": all tasks in the agent's team

    Called by the mcp__agent_task__list_tasks MCP tool.
    """
    try:
        data, count = InputTaskService.list_agent_tasks(
            db_session=db_session,
            user_id=current_user.id,
            status=status,
            scope=scope,
        )
        return InputTasksPublicExtended(data=data, count=count)
    except InputTaskError as e:
        _handle_error(e)


@router.get("/agent/tasks/{task_id}/details")
async def agent_get_task_details(
    db_session: SessionDep,
    current_user: CurrentUser,
    task_id: uuid.UUID,
    source_session_id: uuid.UUID | None = None,
) -> Any:
    """
    Agent gets full task details including recent comments and subtask progress.

    Called by the mcp__agent_task__get_details MCP tool.
    Returns a simplified view optimized for agent consumption.
    Automatically uploads task files to the calling agent's workspace when
    source_session_id is provided.
    """
    try:
        details = InputTaskService.get_agent_task_details(
            db_session=db_session,
            task_id=task_id,
            user_id=current_user.id,
        )
        if source_session_id:
            return await InputTaskService.upload_task_files_to_agent_env(
                db_session, details, source_session_id,
            )
        # No session to upload to — strip internal storage paths
        details.pop("files", None)
        return details
    except InputTaskError as e:
        _handle_error(e)
