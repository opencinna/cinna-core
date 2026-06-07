import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, File, Query, Request, UploadFile
from fastapi.responses import StreamingResponse

from app.api.deps import CurrentUserOrGuest, GuestShareContext, SessionDep
from app.models.files.file_upload import FileUploadPublic, FileUpload
from app.models import User
from app.services.files.file_service import FileService
from app.services.files.file_storage_service import FileStorageService
from app.services.environments.agent_workspace_token_service import (
    AgentWorkspaceTokenService,
)

router = APIRouter(prefix="/files", tags=["files"])

# MIME types we are willing to render inline (in-place preview) same-origin.
# Anything outside this set — notably text/html and image/svg+xml, which can
# execute script in the app origin — is forced to a download even when
# ``?disposition=inline`` is requested (defends against stored XSS via
# agent-authored attachments).
_INLINE_SAFE_MIME_TYPES = frozenset(
    {
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
        "application/pdf",
        "text/plain",
        "text/csv",
        "text/markdown",
        "application/json",
    }
)


def _resolve_caller_or_403(request: Request, session: SessionDep):
    """Resolve the bearer-token caller (User | GuestShareContext) or raise 403.

    Used by the download route's JWT fallback path, where the token must be
    optional (the signed ``?token=`` is the alternative). Reuses the same
    user/guest resolution as the ``CurrentUserOrGuest`` dependency.
    """
    from app.api.deps import get_current_user_or_guest

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.lower().startswith("bearer "):
        raise HTTPException(
            status_code=401, detail="Authentication required to download this file"
        )
    bearer_token = auth_header.split(" ", 1)[1].strip()
    return get_current_user_or_guest(session=session, token=bearer_token)


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
    request: Request,
    session: SessionDep,
    file_id: uuid.UUID,
    token: str | None = Query(
        default=None,
        description="Signed file-download token (alternative to a session JWT, "
        "used by A2A/native clients).",
    ),
    disposition: str | None = Query(
        default=None,
        description="Set to 'inline' to render the file in-place (preview); "
        "defaults to 'attachment' (download).",
    ),
) -> StreamingResponse:
    """
    Download a file.

    Authorization (either is sufficient):
    - A valid signed ``?token=`` whose ``file_id`` claim matches this file, OR
    - a session JWT belonging to the file owner / a session participant / the
      agent owner (for guests).

    ``?disposition=inline`` serves the file with ``Content-Disposition: inline``
    for browser/in-place preview, but ONLY for a known-safe preview MIME set;
    any other type (e.g. text/html, image/svg+xml) is forced to a download to
    prevent same-origin script execution. The default is ``attachment``.

    All responses carry ``X-Content-Type-Options: nosniff``; inline responses
    additionally carry a restrictive ``Content-Security-Policy``.
    """
    # Get file record
    file_record = session.get(FileUpload, file_id)
    if not file_record:
        raise HTTPException(status_code=404, detail="File not found")

    # 1. Signed download token path (A2A / native clients) — preferred when present.
    authorized = False
    if token:
        claims = AgentWorkspaceTokenService.verify_file_download_token(token)
        if claims is None:
            raise HTTPException(status_code=401, detail="Invalid or expired token")
        if claims.get("file_id") != str(file_id):
            raise HTTPException(status_code=403, detail="Token does not authorize this file")
        authorized = True

    # 2. Fall back to session-JWT auth (owner / participant / guest).
    if not authorized:
        caller = _resolve_caller_or_403(request, session)
        if isinstance(caller, GuestShareContext):
            effective_user = session.get(User, caller.owner_id)
            if not effective_user:
                raise HTTPException(status_code=403, detail="Not authorized")
        else:
            effective_user = caller
        if not FileService.check_download_permission(
            file_record, effective_user, session
        ):
            raise HTTPException(
                status_code=403, detail="Not authorized to download this file"
            )

    # Get file path
    file_path = FileStorageService.get_file_path(file_record)
    if not file_path.exists():
        raise HTTPException(status_code=500, detail="File not found on disk")

    # Inline rendering is permitted only for a known-safe preview MIME set.
    # Everything else (notably text/html and image/svg+xml, which can run
    # script in the app origin) is forced to a download regardless of the
    # requested disposition — closing the stored-XSS path.
    render_inline = (
        disposition == "inline"
        and file_record.mime_type in _INLINE_SAFE_MIME_TYPES
    )
    content_disposition = "inline" if render_inline else "attachment"

    headers = {
        "Content-Disposition": (
            f'{content_disposition}; filename="{file_record.filename}"'
        ),
        "Content-Length": str(file_record.file_size),
        # Never let the browser MIME-sniff content into an executable type.
        "X-Content-Type-Options": "nosniff",
    }
    if render_inline:
        # Defense-in-depth: even a previewable type renders with no script,
        # no network, and a sandboxed origin.
        headers["Content-Security-Policy"] = "default-src 'none'; sandbox"

    return StreamingResponse(
        FileStorageService.stream_file(file_path),
        media_type=file_record.mime_type,
        headers=headers,
    )
