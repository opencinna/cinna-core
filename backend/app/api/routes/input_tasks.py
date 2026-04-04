"""
Input Tasks API routes.

Provides CRUD operations for input task management, plus collaboration endpoints
for comments, attachments, subtasks, and short-code access.
"""
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, UploadFile, File
from fastapi.responses import FileResponse

from app.api.deps import CurrentUser, SessionDep
from app.models import (
    InputTaskCreate,
    InputTaskUpdate,
    InputTaskPublic,
    InputTaskPublicExtended,
    InputTaskDetailPublic,
    InputTasksPublicExtended,
    InputTaskStatus,
    RefineTaskRequest,
    RefineTaskResponse,
    ExecuteTaskRequest,
    ExecuteTaskResponse,
    SendAnswerRequest,
    SendAnswerResponse,
    SessionsPublic,
    SessionPublic,
    Message,
    TaskCommentCreate,
    TaskCommentPublic,
    TaskCommentsPublic,
    TaskAttachmentPublic,
    TaskAttachmentsPublic,
)
from app.models.files.file_upload import FileUploadPublic
from app.services.tasks.input_task_service import (
    InputTaskService,
    InputTaskError,
    TaskNotFoundError,
    AgentNotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from app.services.sessions.session_service import SessionService
from app.services.tasks.task_comment_service import TaskCommentService
from app.services.tasks.task_attachment_service import TaskAttachmentService

router = APIRouter(prefix="/tasks", tags=["tasks"])


def _handle_service_error(e: InputTaskError) -> None:
    """Convert service exceptions to HTTP exceptions."""
    raise HTTPException(status_code=e.status_code, detail=e.message)


@router.get("/by-source-session/{session_id}", response_model=InputTasksPublicExtended)
def list_tasks_by_source_session(
    session: SessionDep,
    current_user: CurrentUser,
    session_id: uuid.UUID,
) -> Any:
    """
    List all tasks created from a specific source session.

    Used by SubTasksPanel to show sub-tasks for the current session.
    """
    try:
        data, count = InputTaskService.list_tasks_by_source_session(
            db_session=session,
            source_session_id=session_id,
            user_id=current_user.id,
        )
        return InputTasksPublicExtended(data=data, count=count)
    except InputTaskError as e:
        _handle_service_error(e)


@router.post("/", response_model=InputTaskPublic)
def create_task(
    *, session: SessionDep, current_user: CurrentUser, task_in: InputTaskCreate
) -> Any:
    """
    Create a new input task.
    """
    try:
        # Verify agent access if specified
        if task_in.selected_agent_id:
            InputTaskService.verify_agent_access(
                db_session=session,
                agent_id=task_in.selected_agent_id,
                user_id=current_user.id,
            )

        task = InputTaskService.create_task(
            db_session=session, user_id=current_user.id, data=task_in
        )
        return task
    except InputTaskError as e:
        _handle_service_error(e)


@router.get("/", response_model=InputTasksPublicExtended)
def list_tasks(
    session: SessionDep,
    current_user: CurrentUser,
    skip: int = 0,
    limit: int = 100,
    status: str | None = None,
    user_workspace_id: str | None = None,
    root_only: bool = False,
    team_id: uuid.UUID | None = None,
    priority: str | None = None,
) -> Any:
    """
    List user's input tasks.

    Args:
        skip: Number of records to skip
        limit: Number of records to return
        status: Filter by status. Can be:
            - "active": NEW, REFINING, OPEN, IN_PROGRESS, BLOCKED, ERROR
            - "completed": COMPLETED
            - "archived": ARCHIVED
            - "all": No filter
            - Specific status name (e.g., "new", "in_progress")
        user_workspace_id: Optional workspace filter
            - None (not provided): returns all tasks
            - Empty string (""): filters for default workspace (NULL)
            - UUID string: filters for that workspace
        root_only: If true, only return root tasks (parent_task_id IS NULL)
        team_id: Filter by team UUID
        priority: Filter by priority (low, normal, high, urgent)
    """
    try:
        data, count = InputTaskService.list_tasks_extended(
            db_session=session,
            user_id=current_user.id,
            status=status,
            user_workspace_id=user_workspace_id,
            skip=skip,
            limit=limit,
            root_only=root_only,
            team_id=team_id,
            priority=priority,
        )
        return InputTasksPublicExtended(data=data, count=count)
    except InputTaskError as e:
        _handle_service_error(e)


@router.get("/{id}", response_model=InputTaskPublicExtended)
def get_task(session: SessionDep, current_user: CurrentUser, id: uuid.UUID) -> Any:
    """
    Get a single input task with details.
    """
    try:
        return InputTaskService.get_task_extended(
            db_session=session,
            task_id=id,
            user_id=current_user.id,
        )
    except InputTaskError as e:
        _handle_service_error(e)


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
    try:
        # Get task with ownership check
        task = InputTaskService.get_task_with_ownership_check(
            db_session=session,
            task_id=id,
            user_id=current_user.id,
        )

        # Verify agent access if being updated
        if task_in.selected_agent_id:
            InputTaskService.verify_agent_access(
                db_session=session,
                agent_id=task_in.selected_agent_id,
                user_id=current_user.id,
            )

        updated_task = InputTaskService.update_task(
            db_session=session, task=task, data=task_in
        )
        return updated_task
    except InputTaskError as e:
        _handle_service_error(e)


@router.delete("/{id}")
async def delete_task(
    session: SessionDep, current_user: CurrentUser, id: uuid.UUID
) -> Message:
    """
    Delete an input task.

    Cleans up any attached files (marks them for garbage collection) before deleting.
    Emits ACTIVITY_DELETED for any associated activities before CASCADE cleanup.
    """
    try:
        task = InputTaskService.get_task_with_ownership_check(
            db_session=session,
            task_id=id,
            user_id=current_user.id,
        )

        # Emit ACTIVITY_DELETED for any activities linked to this task
        # (CASCADE will clean up the DB rows, but we need WebSocket notifications)
        from sqlmodel import select
        from app.models.sessions.activity import Activity
        from app.services.events.event_service import event_service
        from app.models.events.event import EventType

        activities = session.exec(
            select(Activity).where(Activity.input_task_id == id)
        ).all()
        for activity in activities:
            await event_service.emit_event(
                event_type=EventType.ACTIVITY_DELETED,
                model_id=activity.id,
                user_id=current_user.id,
                meta={
                    "activity_type": activity.activity_type,
                    "input_task_id": str(id),
                }
            )

        # Clean up attached files before deleting task
        InputTaskService.cleanup_task_files(
            db_session=session,
            task_id=id,
            user_id=current_user.id,
        )
        InputTaskService.delete_task(db_session=session, task=task)
        return Message(message="Task deleted successfully")
    except InputTaskError as e:
        _handle_service_error(e)


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
    try:
        task = InputTaskService.get_task_with_ownership_check(
            db_session=session,
            task_id=id,
            user_id=current_user.id,
        )

        result = InputTaskService.refine_task(
            db_session=session,
            task=task,
            user_id=current_user.id,
            user_comment=refine_in.user_comment,
            user_selected_text=refine_in.user_selected_text,
        )

        return RefineTaskResponse(
            success=result["success"],
            refined_description=result.get("refined_description"),
            feedback_message=result.get("feedback_message"),
            error=result.get("error"),
        )
    except InputTaskError as e:
        _handle_service_error(e)


@router.post("/{id}/execute", response_model=ExecuteTaskResponse)
async def execute_task(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    id: uuid.UUID,
    execute_in: ExecuteTaskRequest,
) -> Any:
    """
    Execute a task by creating a session and sending the task description as the initial message.

    Requires a selected_agent_id to be set on the task.
    The backend handles sending the initial message to the session.
    """
    try:
        task = InputTaskService.get_task_with_ownership_check(
            db_session=session,
            task_id=id,
            user_id=current_user.id,
        )

        # Get attached file IDs to send with the message
        file_id_strings = InputTaskService.get_task_file_ids(db_session=session, task_id=id)
        file_ids = [uuid.UUID(fid) for fid in file_id_strings] if file_id_strings else None

        success, new_session, error = await InputTaskService.execute_task(
            db_session=session,
            task=task,
            user_id=current_user.id,
            mode=execute_in.mode,
            file_ids=file_ids,
        )

        if not success:
            return ExecuteTaskResponse(success=False, error=error)

        return ExecuteTaskResponse(
            success=True,
            session_id=new_session.id,
        )
    except InputTaskError as e:
        _handle_service_error(e)


@router.post("/{id}/send-answer", response_model=SendAnswerResponse)
def send_task_email_answer(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    id: uuid.UUID,
    body: SendAnswerRequest,
) -> Any:
    """
    Send an email reply for an email-originated task.

    Generates an AI-crafted email reply from the task's session results
    and queues it for delivery via the original agent's SMTP configuration.

    Only works for tasks with a source_email_message_id (email-originated tasks).
    """
    try:
        task = InputTaskService.get_task_with_ownership_check(
            db_session=session,
            task_id=id,
            user_id=current_user.id,
        )

        result = InputTaskService.send_email_answer(
            db_session=session,
            task=task,
            user_id=current_user.id,
            custom_message=body.custom_message,
        )

        return SendAnswerResponse(
            success=result["success"],
            queue_entry_id=result.get("queue_entry_id"),
            generated_reply=result.get("generated_reply"),
            error=result.get("error"),
        )
    except InputTaskError as e:
        _handle_service_error(e)


@router.post("/{id}/archive", response_model=InputTaskPublic)
async def archive_task(
    session: SessionDep, current_user: CurrentUser, id: uuid.UUID
) -> Any:
    """
    Archive a completed or error task.
    """
    try:
        InputTaskService.get_task_with_ownership_check(
            db_session=session,
            task_id=id,
            user_id=current_user.id,
        )

        updated_task = InputTaskService.update_task_status(
            db_session=session,
            task_id=id,
            new_status=InputTaskStatus.ARCHIVED,
            changed_by_user_id=current_user.id,
        )
        return updated_task
    except InputTaskError as e:
        _handle_service_error(e)


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
    try:
        # Verify task exists and user has access
        InputTaskService.get_task_with_ownership_check(
            db_session=session,
            task_id=id,
            user_id=current_user.id,
        )

        sessions = SessionService.list_task_sessions(
            db_session=session, task_id=id, limit=limit, offset=skip
        )

        return SessionsPublic(
            data=[SessionPublic(**s.model_dump()) for s in sessions],
            count=len(sessions),
        )
    except InputTaskError as e:
        _handle_service_error(e)


@router.post("/{id}/files/{file_id}", response_model=FileUploadPublic)
def attach_file_to_task(
    session: SessionDep,
    current_user: CurrentUser,
    id: uuid.UUID,
    file_id: uuid.UUID,
) -> Any:
    """
    Attach an uploaded file to a task.

    The file must already be uploaded via the /files endpoint.
    """
    try:
        # Verify task exists and user has access
        InputTaskService.get_task_with_ownership_check(
            db_session=session,
            task_id=id,
            user_id=current_user.id,
        )

        # Attach the file
        links = InputTaskService.attach_files_to_task(
            db_session=session,
            task_id=id,
            file_ids=[file_id],
            user_id=current_user.id,
        )

        if not links:
            raise HTTPException(
                status_code=400,
                detail="File not found or already attached",
            )

        # Return the file info
        from app.models.files.file_upload import FileUpload
        file = session.get(FileUpload, file_id)
        return FileUploadPublic.model_validate(file)

    except InputTaskError as e:
        _handle_service_error(e)


@router.delete("/{id}/files/{file_id}")
def detach_file_from_task(
    session: SessionDep,
    current_user: CurrentUser,
    id: uuid.UUID,
    file_id: uuid.UUID,
) -> Message:
    """
    Remove a file from a task.

    The file will be marked for garbage collection if it's a temporary upload.
    """
    try:
        # Verify task exists and user has access
        InputTaskService.get_task_with_ownership_check(
            db_session=session,
            task_id=id,
            user_id=current_user.id,
        )

        success = InputTaskService.detach_file_from_task(
            db_session=session,
            task_id=id,
            file_id=file_id,
            user_id=current_user.id,
        )

        if not success:
            raise HTTPException(
                status_code=404,
                detail="File not attached to this task",
            )

        return Message(message="File removed from task")

    except InputTaskError as e:
        _handle_service_error(e)


# ── Collaboration Endpoints (Phase A) ──────────────────────────────────────────

@router.get("/by-code/{short_code}", response_model=InputTaskPublicExtended)
def get_task_by_code(
    session: SessionDep,
    current_user: CurrentUser,
    short_code: str,
) -> Any:
    """Get a task by its short code (e.g., TASK-42)."""
    try:
        task = InputTaskService.get_task_by_short_code(
            db_session=session,
            short_code=short_code,
            user_id=current_user.id,
        )
        return InputTaskService.get_task_extended(
            db_session=session,
            task_id=task.id,
            user_id=current_user.id,
        )
    except InputTaskError as e:
        _handle_service_error(e)


@router.get("/by-code/{short_code}/detail", response_model=InputTaskDetailPublic)
def get_task_detail_by_code(
    session: SessionDep,
    current_user: CurrentUser,
    short_code: str,
) -> Any:
    """Get full task detail (comments, attachments, subtasks, history) by short code."""
    try:
        task = InputTaskService.get_task_by_short_code(
            db_session=session,
            short_code=short_code,
            user_id=current_user.id,
        )
        return InputTaskService.get_task_detail(
            db_session=session,
            task_id=task.id,
            user_id=current_user.id,
        )
    except InputTaskError as e:
        _handle_service_error(e)


@router.get("/by-code/{short_code}/tree")
def get_task_tree_by_code(
    session: SessionDep,
    current_user: CurrentUser,
    short_code: str,
) -> Any:
    """Get recursive subtask tree for a task identified by short code."""
    try:
        task = InputTaskService.get_task_by_short_code(
            db_session=session,
            short_code=short_code,
            user_id=current_user.id,
        )
        return InputTaskService.get_task_tree(
            db_session=session,
            task_id=task.id,
            user_id=current_user.id,
        )
    except InputTaskError as e:
        _handle_service_error(e)


@router.get("/{id}/detail", response_model=InputTaskDetailPublic)
def get_task_detail(
    session: SessionDep,
    current_user: CurrentUser,
    id: uuid.UUID,
) -> Any:
    """Get full task detail (comments, attachments, subtasks, history) by UUID."""
    try:
        return InputTaskService.get_task_detail(
            db_session=session,
            task_id=id,
            user_id=current_user.id,
        )
    except InputTaskError as e:
        _handle_service_error(e)


# ── Comments ───────────────────────────────────────────────────────────────────

@router.get("/{id}/comments/", response_model=TaskCommentsPublic)
def list_task_comments(
    session: SessionDep,
    current_user: CurrentUser,
    id: uuid.UUID,
    skip: int = 0,
    limit: int = 100,
) -> Any:
    """List comments on a task in chronological order."""
    try:
        InputTaskService.get_task_with_ownership_check(
            db_session=session, task_id=id, user_id=current_user.id
        )
        comments, count = TaskCommentService.list_comments(
            db_session=session, task_id=id, skip=skip, limit=limit
        )
        return TaskCommentsPublic(data=comments, count=count)
    except InputTaskError as e:
        _handle_service_error(e)


@router.post("/{id}/comments/", response_model=TaskCommentPublic)
async def add_task_comment(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    id: uuid.UUID,
    comment_in: TaskCommentCreate,
) -> Any:
    """Add a comment to a task (user-initiated)."""
    try:
        InputTaskService.get_task_with_ownership_check(
            db_session=session, task_id=id, user_id=current_user.id
        )
        comment = TaskCommentService.add_comment(
            db_session=session,
            task_id=id,
            data=comment_in,
            author_user_id=current_user.id,
        )
        return TaskCommentService._to_public(session, comment)
    except InputTaskError as e:
        _handle_service_error(e)


@router.delete("/{id}/comments/{comment_id}")
def delete_task_comment(
    session: SessionDep,
    current_user: CurrentUser,
    id: uuid.UUID,
    comment_id: uuid.UUID,
) -> Message:
    """Delete a comment from a task."""
    success = TaskCommentService.delete_comment(
        db_session=session,
        comment_id=comment_id,
        user_id=current_user.id,
    )
    if not success:
        raise HTTPException(status_code=404, detail="Comment not found")
    return Message(message="Comment deleted")


# ── Attachments ────────────────────────────────────────────────────────────────

@router.get("/{id}/attachments/", response_model=TaskAttachmentsPublic)
def list_task_attachments(
    session: SessionDep,
    current_user: CurrentUser,
    id: uuid.UUID,
) -> Any:
    """List attachments for a task."""
    try:
        InputTaskService.get_task_with_ownership_check(
            db_session=session, task_id=id, user_id=current_user.id
        )
        attachments = TaskAttachmentService.list_attachments(
            db_session=session, task_id=id
        )
        return TaskAttachmentsPublic(data=attachments, count=len(attachments))
    except InputTaskError as e:
        _handle_service_error(e)


@router.post("/{id}/attachments/", response_model=TaskAttachmentPublic)
async def upload_task_attachment(
    session: SessionDep,
    current_user: CurrentUser,
    id: uuid.UUID,
    file: UploadFile = File(...),
) -> Any:
    """Upload a file attachment to a task."""
    try:
        InputTaskService.get_task_with_ownership_check(
            db_session=session, task_id=id, user_id=current_user.id
        )
        attachment = await TaskAttachmentService.upload_attachment(
            db_session=session,
            task_id=id,
            file=file,
            uploaded_by_user_id=current_user.id,
        )
        return TaskAttachmentService._to_public(session, attachment)
    except InputTaskError as e:
        _handle_service_error(e)


@router.get("/{id}/attachments/{attachment_id}/download")
def download_task_attachment(
    session: SessionDep,
    current_user: CurrentUser,
    id: uuid.UUID,
    attachment_id: uuid.UUID,
) -> Any:
    """Download a task attachment file."""
    file_path, filename, content_type = TaskAttachmentService.get_download_stream(
        db_session=session,
        task_id=id,
        attachment_id=attachment_id,
        user_id=current_user.id,
    )
    return FileResponse(
        path=str(file_path),
        filename=filename,
        media_type=content_type,
    )


@router.delete("/{id}/attachments/{attachment_id}")
def delete_task_attachment(
    session: SessionDep,
    current_user: CurrentUser,
    id: uuid.UUID,
    attachment_id: uuid.UUID,
) -> Message:
    """Delete a task attachment."""
    success = TaskAttachmentService.delete_attachment(
        db_session=session,
        task_id=id,
        attachment_id=attachment_id,
        user_id=current_user.id,
    )
    if not success:
        raise HTTPException(status_code=404, detail="Attachment not found")
    return Message(message="Attachment deleted")


# ── Subtasks ───────────────────────────────────────────────────────────────────

@router.get("/{id}/subtasks/", response_model=InputTasksPublicExtended)
def list_subtasks(
    session: SessionDep,
    current_user: CurrentUser,
    id: uuid.UUID,
) -> Any:
    """List direct subtasks of a task."""
    try:
        InputTaskService.get_task_with_ownership_check(
            db_session=session, task_id=id, user_id=current_user.id
        )
        data, count = InputTaskService.list_subtasks(
            db_session=session, parent_task_id=id
        )
        return InputTasksPublicExtended(data=data, count=count)
    except InputTaskError as e:
        _handle_service_error(e)


@router.post("/{id}/subtasks/", response_model=InputTaskPublic)
def create_subtask(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    id: uuid.UUID,
    subtask_in: InputTaskCreate,
) -> Any:
    """Create a subtask under a parent task (user-initiated)."""
    try:
        InputTaskService.get_task_with_ownership_check(
            db_session=session, task_id=id, user_id=current_user.id
        )
        # Set parent_task_id on the create data
        subtask_in.parent_task_id = id
        task = InputTaskService.create_task(
            db_session=session,
            user_id=current_user.id,
            data=subtask_in,
        )
        return task
    except InputTaskError as e:
        _handle_service_error(e)
