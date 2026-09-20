"""
Input Tasks API routes.

Provides CRUD operations for input task management, plus collaboration endpoints
for comments, attachments, subtasks, and short-code access.
"""
import uuid
from datetime import datetime
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
    InputTaskStatusUpdate,
    RefineTaskRequest,
    RefineTaskResponse,
    ExecuteTaskRequest,
    ExecuteTaskResponse,
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
from app.models.tasks.delegation import (
    DelegationCapabilities,
    DelegationReply,
    DelegationReplyResult,
    DelegationReport,
    DelegationResultPublic,
)
from app.services.tasks.input_task_service import (
    InputTaskService,
    InputTaskError,
    TaskNotFoundError,
    AgentNotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from app.utils import create_task_with_error_logging
from app.services.sessions.session_service import SessionService
from app.services.tasks.task_comment_service import TaskCommentService
from app.services.tasks.task_attachment_service import TaskAttachmentService
from app.services.tasks.delegation_service import DelegationService

router = APIRouter(prefix="/tasks", tags=["tasks"])


# Must stay registered before any GET '/{id}' route: FastAPI matches in
# registration order, and '/{id}' would otherwise capture this path (422).
@router.get('/delegation-capabilities', response_model=DelegationCapabilities)
def delegation_capabilities(current_user: CurrentUser) -> DelegationCapabilities:
    """Versioned, authenticated extension discovery; older servers return 404/422."""
    return DelegationCapabilities(version=1, metadata=True, structured_result=True, reply=True)


# 404 for non-owners so new routes don't reveal task existence; the older task routes' 400 is deliberately not copied.
@router.put('/{id}/delegation-result', response_model=DelegationResultPublic)
def report_delegation(
    id: uuid.UUID, data: DelegationReport, session: SessionDep, current_user: CurrentUser,
) -> Any:
    """Result of a delegated task run by an external executor (not a platform session).

    Accepted only while such a task is in_progress or blocked (409 otherwise);
    404 when the task is not a delegation or not the caller's (same as reply).
    """
    try:
        return DelegationService.report(
            session, id, data,
            require_external_executor=True,
            owner_id=current_user.id,
            changed_by_user_id=current_user.id,
        )
    except InputTaskError as error:
        _handle_service_error(error)


# 404 for non-owners so new routes don't reveal task existence; the older task routes' 400 is deliberately not copied.
@router.post('/{id}/delegation-reply', response_model=DelegationReplyResult)
async def reply_delegation(
    id: uuid.UUID, data: DelegationReply, session: SessionDep, current_user: CurrentUser,
) -> Any:
    """Answer the current blocked result of a delegated task; resumes its session."""
    try:
        return await DelegationService.reply(session, current_user.id, id, data)
    except InputTaskError as error:
        _handle_service_error(error)


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
async def create_task(
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

        task, created = InputTaskService.create_task_idempotent(
            db_session=session, user_id=current_user.id, data=task_in
        )

        # Auto-execute if requested and agent is assigned.
        #
        # Gated on `created`. When an external_ref matched an existing task this
        # call is a retry of one already handled, and re-firing execution would
        # put a second agent session on the same task — duplicate work, duplicate
        # spend, duplicate results. Deduplicating the row while repeating its
        # side effect is worse than the duplicate row the key exists to prevent.
        if created and task.auto_execute and task.selected_agent_id:
            create_task_with_error_logging(
                InputTaskService._auto_execute_task(task),
                task_name=f"auto_execute_task_{task.id}"
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
    updated_since: datetime | None = None,
    updated_since_id: uuid.UUID | None = None,
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
        updated_since: Incremental-sync cursor — return only tasks changed
            strictly after this timestamp, ordered by (updated_at, id) ascending.

            Task activity counts: adding or deleting a comment, and uploading,
            attaching or deleting a file, all bump ``input_task.updated_at`` in
            the same transaction as the write, so they appear in a delta.

            One limit remains: deletes are not reported. A deleted task simply
            stops appearing; a client that needs to notice removals reconciles
            with a full list.

            Page by advancing the cursor, not by ``skip``: the sort key is
            mutable, so offset paging over it can skip unread rows. Take the
            last row's ``updated_at`` and ``id`` as ``updated_since`` and
            ``updated_since_id`` for the next page. The ID keeps timestamp ties
            from being skipped. Timestamp-only cursors retain strict-after semantics.
        updated_since_id: Last task ID at the cursor timestamp; requires updated_since.
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
            updated_since=updated_since,
            updated_since_id=updated_since_id,
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
async def update_task(
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


@router.post("/{id}/status", response_model=InputTaskPublic)
async def update_task_status(
    session: SessionDep,
    current_user: CurrentUser,
    id: uuid.UUID,
    status_in: InputTaskStatusUpdate,
) -> Any:
    """
    Set a task's status, attributed to the calling user.

    For work a user-authenticated client executes outside cinna (the desktop
    app running a task locally), so the web view mirrors what is actually
    happening. The change is validated against the transition table, written to
    the status history and posted to the comment feed.

    It records; it does not act — no session is started, resumed or interrupted.
    For a task cinna is itself executing, the session-driven recompute still
    has the last word.

    Allowed targets: open, in_progress, blocked, completed, error, cancelled.
    Use POST /{id}/archive to archive.

    ``async def`` like its sibling status routes: the point of this route is
    that the web view mirrors what is happening, so the TASK_STATUS_CHANGED
    emit is load-bearing. From a sync worker thread that emit is scheduled
    best-effort and dropped silently if scheduling fails.
    """
    try:
        return InputTaskService.update_task_status_from_user(
            db_session=session,
            task_id=id,
            user_id=current_user.id,
            data=status_in,
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
    """Create a subtask under a parent task (user-initiated).

    ``external_ref`` is not honoured here. It is an idempotency key for
    ``POST /tasks/``, and matching one on this route would return a task with a
    different parent than the caller asked for while reporting 200 — a silently
    wrong answer. It is ignored rather than rejected so a client that fills the
    field uniformly still gets a real subtask.
    """
    try:
        InputTaskService.get_task_with_ownership_check(
            db_session=session, task_id=id, user_id=current_user.id
        )
        # Set parent_task_id on the create data
        subtask_in.parent_task_id = id
        subtask_in.external_ref = None
        task = InputTaskService.create_task(
            db_session=session,
            user_id=current_user.id,
            data=subtask_in,
        )
        return task
    except InputTaskError as e:
        _handle_service_error(e)
