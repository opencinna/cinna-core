"""Artifact content: ``/a/v/<cap>/<path>`` and ``/a/z/<cap>`` (not in the schema).

No user authentication — the cap in the path is the credential, and every
request re-checks it against the database (D1). Every failure is a plain 404
carrying the security headers and ``Cache-Control: no-store`` (D19); CORS and
the baseline headers for responses that never reach these handlers are the
job of :class:`app.core.artifact_content_middleware.ArtifactContentMiddleware`.
"""
import posixpath
from urllib.parse import quote

from fastapi import APIRouter, Request
from starlette.responses import FileResponse, RedirectResponse, Response

from app.api.deps import SessionDep
from app.services.artifacts.artifact_content import (
    ArtifactContentService,
    build_headers,
    download_name,
    error_headers,
    file_basename,
)
from app.services.artifacts.artifact_storage import ArtifactStorage

router = APIRouter(tags=["artifact-content"], include_in_schema=False)


def _not_found() -> Response:
    return Response(
        content="Not found",
        status_code=404,
        media_type="text/plain; charset=utf-8",
        headers=error_headers(),
    )


@router.api_route("/a/v/{cap}", methods=["GET", "HEAD"])
def artifact_content_root(cap: str) -> Response:
    """``/a/v/<cap>`` ⇒ ``/a/v/<cap>/`` so relative URLs resolve under the cap."""
    return RedirectResponse(url=f"{cap}/", status_code=307, headers=error_headers())


@router.api_route("/a/v/{cap}/{path:path}", methods=["GET", "HEAD"])
def artifact_content(cap: str, path: str, db: SessionDep, dl: str | None = None) -> Response:
    revision = ArtifactContentService.resolve_revision(db, cap)
    if revision is None:
        return _not_found()
    resolved = ArtifactContentService.lookup(revision, path)
    if resolved is None:
        if ArtifactContentService.is_directory(revision, path):
            # ``docs`` ⇒ ``docs/`` so the folder's relative URLs resolve inside it.
            name = quote(posixpath.basename(path))
            return RedirectResponse(url=f"{name}/", status_code=307, headers=error_headers())
        return _not_found()
    headers = build_headers(cap, etag=resolved.entry["sha256"])
    attachment = dl == "1"
    return FileResponse(
        resolved.abs_path,
        media_type=resolved.entry["mime"],
        headers=headers,
        filename=file_basename(resolved.entry["path"]) if attachment else None,
        content_disposition_type="attachment" if attachment else "inline",
    )


@router.api_route("/a/z/{cap}", methods=["GET", "HEAD"])
def artifact_content_zip(cap: str, request: Request, db: SessionDep) -> Response:
    """Zip of the cap's revision (D18) — plain anchors work, signed in or not.

    Built once per revision and cached; a HEAD never builds it.
    """
    revision = ArtifactContentService.resolve_revision(db, cap)
    if revision is None:
        return _not_found()
    filename = download_name(revision.artifact.title, f"-r{revision.revision_number}.zip")
    headers = build_headers(cap)
    cached = ArtifactStorage.revision_zip(revision.artifact.id, revision.claims.revision_id)
    if request.method == "HEAD" and not cached.is_file():
        return Response(
            status_code=200,
            media_type="application/zip",
            headers={
                **headers,
                "Content-Disposition": f'attachment; filename="{filename}"',
            },
        )
    target = ArtifactContentService.revision_zip(
        revision.artifact.id, revision.claims.revision_id, revision.manifest
    )
    return FileResponse(
        target,
        media_type="application/zip",
        headers=headers,
        filename=filename,
        content_disposition_type="attachment",
    )
