import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, File, UploadFile
from fastapi.responses import StreamingResponse

from app.api.deps import CurrentUser, CurrentUserOrGuest, GuestShareContext, SessionDep
from app.models.files.file_upload import FileUploadPublic, FileUpload
from app.models import User
from app.services.files.file_service import FileService
from app.services.files.file_storage_service import FileStorageService

router = APIRouter(prefix="/files", tags=["files"])


@router.post("/upload", response_model=FileUploadPublic)
async def upload_file(
    *,
    session: SessionDep,
    caller: CurrentUserOrGuest,
    file: UploadFile = File(...),
) -> Any:
    """
    Upload a file (creates temporary file record).

    Validates:
    - File size (max 100MB)
    - Mime type (whitelist)
    - User storage quota (max 10GB)

    For guest users, the file is attributed to the agent owner.
    """
    user_id = caller.owner_id if isinstance(caller, GuestShareContext) else caller.id
    db_file = await FileService.create_file_upload(
        session=session,
        user_id=user_id,
        file=file,
    )

    return FileUploadPublic(
        id=db_file.id,
        filename=db_file.filename,
        file_size=db_file.file_size,
        mime_type=db_file.mime_type,
        status=db_file.status,
        uploaded_at=db_file.uploaded_at,
    )


@router.delete("/{file_id}")
def delete_file(
    *,
    session: SessionDep,
    caller: CurrentUserOrGuest,
    file_id: uuid.UUID,
) -> Any:
    """
    Mark file for deletion (soft delete).

    Authorization:
    - File owner only (for guests, owner is the agent owner)
    """
    # Get file record
    file_record = session.get(FileUpload, file_id)
    if not file_record:
        raise HTTPException(status_code=404, detail="File not found")

    # For guests, resolve the effective user for permission checks
    if isinstance(caller, GuestShareContext):
        effective_user = session.get(User, caller.owner_id)
        if not effective_user:
            raise HTTPException(status_code=403, detail="Not authorized")
    else:
        effective_user = caller

    # Check permission
    if not FileService.check_delete_permission(file_record, effective_user):
        raise HTTPException(
            status_code=403, detail="Not authorized to delete this file"
        )

    FileService.mark_file_for_deletion(
        session=session,
        file_id=file_id,
        user_id=effective_user.id,
    )
    return {"message": "File marked for deletion"}


@router.get("/{file_id}/download")
def download_file(
    *,
    session: SessionDep,
    caller: CurrentUserOrGuest,
    file_id: uuid.UUID,
) -> StreamingResponse:
    """
    Download a file.

    Authorization:
    - File owner, OR
    - User owns a session with a message referencing this file
    - Guest users can download files owned by the agent owner
    """
    # Get file record
    file_record = session.get(FileUpload, file_id)
    if not file_record:
        raise HTTPException(status_code=404, detail="File not found")

    # For guests, resolve the effective user for permission checks
    if isinstance(caller, GuestShareContext):
        effective_user = session.get(User, caller.owner_id)
        if not effective_user:
            raise HTTPException(status_code=403, detail="Not authorized")
    else:
        effective_user = caller

    # Check permission
    if not FileService.check_download_permission(file_record, effective_user, session):
        raise HTTPException(
            status_code=403, detail="Not authorized to download this file"
        )

    # Get file path
    file_path = FileStorageService.get_file_path(file_record)
    if not file_path.exists():
        raise HTTPException(status_code=500, detail="File not found on disk")

    # Stream file
    return StreamingResponse(
        FileStorageService.stream_file(file_path),
        media_type=file_record.mime_type,
        headers={
            "Content-Disposition": f'attachment; filename="{file_record.filename}"',
            "Content-Length": str(file_record.file_size),
        },
    )
