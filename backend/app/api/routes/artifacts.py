"""Artifacts API (``/api/v1/artifacts``), user JWT.

Thin routes over :class:`ArtifactService` / :class:`ArtifactLinkService`.
A caller who cannot view an artifact gets 404, never 403; writes are owner-only.
"""
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Literal

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from starlette.responses import FileResponse

from app.api.deps import CurrentUser, SessionDep
from app.models import Message
from app.models.artifacts.artifact import (
    ArtifactDetailPublic,
    ArtifactPublic,
    ArtifactPublished,
    ArtifactsCapabilities,
    ArtifactsPublic,
    ArtifactUpdate,
    ArtifactViewPublic,
)
from app.models.artifacts.artifact_grant import ArtifactGrantIn, ArtifactGrantPublic
from app.models.artifacts.artifact_link import ArtifactLinkCreate, ArtifactLinkCreated
from app.services.artifacts.artifact_errors import ArtifactError
from app.services.artifacts.artifact_ingest import FilesSource, SourceFile, ZipSource
from app.services.artifacts.artifact_link_service import ArtifactLinkService
from app.services.artifacts.artifact_service import ArtifactDownload, ArtifactService

router = APIRouter(prefix="/artifacts", tags=["artifacts"])

# ``core_agent`` is not accepted here: agents publish through their own route (P6).
UploadSourceKind = Literal["desktop", "cli", "upload"]


@contextmanager
def artifact_errors() -> Iterator[None]:
    """Map :class:`ArtifactError` to ``HTTPException`` (shared by every artifact route)."""
    try:
        yield
    except ArtifactError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from exc


def upload_source(
    archive: UploadFile | None, files: list[UploadFile] | None
) -> tuple[ZipSource | FilesSource, int | None]:
    """D14: a zip ``archive`` xor ``files[]`` (each filename = relative path)."""
    files = [f for f in (files or []) if f is not None]
    if (archive is None) == (not files):
        raise HTTPException(
            status_code=422,
            detail="Send either one 'archive' (a zip) or one or more 'files', not both",
        )
    if archive is not None:
        return ZipSource(archive.file), archive.size
    if any(not f.filename for f in files):
        raise HTTPException(status_code=422, detail="Every uploaded file needs a filename")
    declared = sum(f.size or 0 for f in files)
    return FilesSource([SourceFile(f.filename or "", f.file) for f in files]), declared


def zip_download_response(download: ArtifactDownload) -> FileResponse:
    return FileResponse(
        download.path,
        media_type="application/zip",
        filename=download.filename,
        content_disposition_type="attachment",
    )


@router.get("/capabilities", response_model=ArtifactsCapabilities)
def get_artifacts_capabilities(_current_user: CurrentUser) -> Any:
    """Feature version and the upload limits, so clients can pre-check (D13)."""
    return ArtifactService.capabilities()


@router.post("", response_model=ArtifactPublished)
def create_artifact(
    *,
    db: SessionDep,
    current_user: CurrentUser,
    title: str = Form(..., min_length=1, max_length=200),
    description: str | None = Form(None, max_length=2000),
    entry_path: str | None = Form(None, max_length=1024),
    source_kind: UploadSourceKind = Form("upload"),
    source_label: str | None = Form(None, max_length=200),
    user_workspace_id: uuid.UUID | None = Form(None),
    archive: UploadFile | None = File(None),
    files: list[UploadFile] | None = File(None),
) -> Any:
    """Publish a new artifact from a zip or a set of files (revision 1).

    The response carries the revision's ``excluded`` paths (dropped by the
    A10 rules). 400 for a rejected upload, 413 over a size limit or the quota.
    """
    if not title.strip():
        raise HTTPException(status_code=422, detail="Title must not be blank")
    source, declared = upload_source(archive, files)
    with artifact_errors():
        return ArtifactService.create_from_upload(
            db,
            current_user,
            source=source,
            declared_bytes=declared,
            title=title,
            description=description,
            entry_path=entry_path,
            source_kind=source_kind,
            source_label=source_label,
            user_workspace_id=user_workspace_id,
        )


@router.post("/{artifact_id}/revisions", response_model=ArtifactPublished)
def republish_artifact(
    *,
    db: SessionDep,
    current_user: CurrentUser,
    artifact_id: uuid.UUID,
    note: str | None = Form(None, max_length=2000),
    entry_path: str | None = Form(None, max_length=1024),
    title: str | None = Form(None, max_length=200),
    description: str | None = Form(None, max_length=2000),
    archive: UploadFile | None = File(None),
    files: list[UploadFile] | None = File(None),
) -> Any:
    """Add a revision (owner only). Same id and page URL; the new revision is current."""
    source, declared = upload_source(archive, files)
    with artifact_errors():
        return ArtifactService.republish(
            db,
            current_user,
            artifact_id,
            source=source,
            declared_bytes=declared,
            entry_path=entry_path,
            note=note,
            title=title,
            description=description,
        )


@router.get("", response_model=ArtifactsPublic)
def list_artifacts(
    *,
    db: SessionDep,
    current_user: CurrentUser,
    scope: Literal["mine", "shared_with_me"] = "mine",
    user_workspace_id: str | None = None,
    q: str | None = Query(None, max_length=200),
    source_key: str | None = Query(None, max_length=1024),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=200),
) -> Any:
    """List artifacts, newest change first (``updated_at`` desc, then id).

    ``user_workspace_id`` and ``source_key`` apply to ``scope=mine`` only;
    ``user_workspace_id=""`` is the default workspace.
    """
    with artifact_errors():
        return ArtifactService.list_artifacts(
            db,
            current_user,
            scope=scope,
            user_workspace_id=user_workspace_id,
            q=q,
            source_key=source_key,
            skip=skip,
            limit=limit,
        )


@router.get("/{artifact_id}", response_model=ArtifactDetailPublic)
def get_artifact(*, db: SessionDep, current_user: CurrentUser, artifact_id: uuid.UUID) -> Any:
    """Detail. Owners also get every revision, the grants and the links."""
    with artifact_errors():
        return ArtifactService.get_detail(db, current_user, artifact_id)


@router.patch("/{artifact_id}", response_model=ArtifactPublic)
def update_artifact(
    *, db: SessionDep, current_user: CurrentUser, artifact_id: uuid.UUID, body: ArtifactUpdate
) -> Any:
    """Owner: title, description, start page (``null`` ⇒ a file set), visibility."""
    with artifact_errors():
        return ArtifactService.update(db, current_user, artifact_id, body)


@router.delete("/{artifact_id}", response_model=Message)
def delete_artifact(*, db: SessionDep, current_user: CurrentUser, artifact_id: uuid.UUID) -> Any:
    """Owner: delete the artifact, its revisions, grants and links, then its files."""
    with artifact_errors():
        ArtifactService.delete(db, current_user, artifact_id)
    return Message(message="Artifact deleted")


@router.delete("/{artifact_id}/revisions/{number}", response_model=Message)
def delete_artifact_revision(
    *, db: SessionDep, current_user: CurrentUser, artifact_id: uuid.UUID, number: int
) -> Any:
    """Owner: delete one revision. 409 for the current one or one a live link pins."""
    with artifact_errors():
        ArtifactService.delete_revision(db, current_user, artifact_id, number)
    return Message(message="Revision deleted")


@router.get("/{artifact_id}/download")
def download_artifact(
    *, db: SessionDep, current_user: CurrentUser, artifact_id: uuid.UUID, rev: int | None = None
) -> FileResponse:
    """Zip of the current (or, for the owner, any ``rev``) revision."""
    with artifact_errors():
        download = ArtifactService.download(db, current_user, artifact_id, rev)
    return zip_download_response(download)


@router.post("/{artifact_id}/view", response_model=ArtifactViewPublic)
def view_artifact(
    *,
    db: SessionDep,
    current_user: CurrentUser,
    artifact_id: uuid.UUID,
    rev: int | None = None,
) -> Any:
    """Mint a view capability for the current revision (owners: any ``rev``)."""
    with artifact_errors():
        return ArtifactService.view_for_user(db, artifact_id, current_user, rev)


@router.put("/{artifact_id}/grants", response_model=list[ArtifactGrantPublic])
def replace_artifact_grants(
    *,
    db: SessionDep,
    current_user: CurrentUser,
    artifact_id: uuid.UUID,
    body: list[ArtifactGrantIn],
) -> Any:
    """Owner: replace the people and user groups who can view (roles ⇒ 422)."""
    with artifact_errors():
        return ArtifactService.replace_grants(db, current_user, artifact_id, body)


@router.post("/{artifact_id}/links", response_model=ArtifactLinkCreated)
def create_artifact_link(
    *,
    db: SessionDep,
    current_user: CurrentUser,
    artifact_id: uuid.UUID,
    body: ArtifactLinkCreate,
) -> Any:
    """Owner: create a public link. The token is in this response only."""
    with artifact_errors():
        return ArtifactLinkService.create(db, current_user, artifact_id, body)


@router.delete("/{artifact_id}/links/{link_id}", response_model=Message)
def revoke_artifact_link(
    *, db: SessionDep, current_user: CurrentUser, artifact_id: uuid.UUID, link_id: uuid.UUID
) -> Any:
    """Owner: revoke a link; open viewers stop at their next file load."""
    with artifact_errors():
        ArtifactLinkService.revoke(db, current_user, artifact_id, link_id)
    return Message(message="Link revoked")
