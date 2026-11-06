"""Brand theme API (README §3, core.md C1 §4).

Public, unauthenticated, rate-limited per caller:

* ``GET /brand-theme`` — the published theme, ``204`` when none. ``ETag`` is
  the revision and ``Cache-Control: no-cache``: clients revalidate every time
  and a ``304`` costs one indexed query.
* ``GET /brand-theme/assets/{sha256}`` — content-addressed, immutable bytes.

Superuser only, under ``/admin/brand-theme``: upload a package as a draft
(an invalid package is never saved; the errors are the response), list, read
and delete drafts, publish one (re-validated first) and unpublish.
"""
from __future__ import annotations

import logging
import re
import uuid
from typing import Annotated, Any

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    HTTPException,
    Path,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import JSONResponse

from app.api.deps import SessionDep, get_current_active_superuser
from app.core.config import settings
from app.models import User
from app.models.brand_theme.brand_theme import (
    BRAND_THEME_MAX_DRAFTS,
    BrandThemeDraftPublic,
    BrandThemeDraftsPublic,
    BrandThemeIssue,
    BrandThemePublic,
    BrandThemePublishErrors,
    BrandThemeUploadResult,
)
from app.services.brand_theme.brand_theme_service import (
    BrandThemeError,
    BrandThemeEvents,
    BrandThemeService,
)
from app.services.brand_theme.package import (
    MAX_ENTRIES,
    MAX_FILE_BYTES,
    MAX_TOTAL_BYTES,
    THEME_MAX_BYTES,
    PackageError,
    assets_from_files,
    read_zip,
)
from app.services.brand_theme.schema import MIME_SVG
from app.services.common.rate_limiter import RateLimiter, anonymous_caller_key

logger = logging.getLogger(__name__)

router = APIRouter(tags=["brand-theme"])

SuperUser = Annotated[User, Depends(get_current_active_superuser)]

# One budget for both public reads (see BRAND_THEME_RATE_LIMIT_PER_MIN).
_public_limiter = RateLimiter()


def _public_rate_limit(request: Request) -> None:
    """Declared before ``SessionDep`` so a throttled request costs no query."""
    retry_after = _public_limiter.check(
        anonymous_caller_key(request), settings.BRAND_THEME_RATE_LIMIT_PER_MIN
    )
    if retry_after is not None:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded",
            headers={"Retry-After": str(int(retry_after))},
        )


def _http(exc: BrandThemeError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=exc.detail)


def _etag_matches(header: str | None, etag: str) -> bool:
    if not header:
        return False
    if header.strip() == "*":
        return True
    for candidate in header.split(","):
        candidate = candidate.strip()
        if candidate.startswith("W/"):
            candidate = candidate[2:]
        if candidate == etag:
            return True
    return False


# ── public ─────────────────────────────────────────────────────────────

@router.get(
    "/brand-theme",
    response_model=BrandThemePublic,
    responses={
        204: {"description": "No brand theme is published; show stock Cinna."},
        304: {"description": "The revision in If-None-Match is still current."},
    },
    dependencies=[Depends(_public_rate_limit)],
)
def get_brand_theme(request: Request, session: SessionDep) -> Response:
    """The published brand theme (README §3), or ``204`` when there is none.

    Public on purpose (B7): it brands sign-in screens, before any token.
    """
    headers = {"Cache-Control": "no-cache"}
    row = BrandThemeService.published(session)
    if row is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT, headers=headers)
    headers["ETag"] = f'"{row.revision}"'
    if _etag_matches(request.headers.get("if-none-match"), headers["ETag"]):
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)
    return JSONResponse(content=BrandThemeService.document(session, row), headers=headers)


_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_SHA256_RE = re.compile(_SHA256_PATTERN)


@router.get(
    "/brand-theme/assets/{sha256}",
    response_class=Response,
    responses={200: {"content": {"image/*": {}}, "description": "The asset bytes."}},
    dependencies=[Depends(_public_rate_limit)],
)
def get_brand_theme_asset(
    session: SessionDep,
    sha256: Annotated[str, Path(pattern=_SHA256_PATTERN)],
) -> Response:
    """One stored asset by content hash. Immutable: the URL changes when the
    bytes do. SVGs get a CSP that forbids scripts and external loads even if
    opened directly rather than through ``<img>``."""
    asset = BrandThemeService.get_asset(session, sha256) if _SHA256_RE.match(sha256) else None
    if asset is None:
        raise HTTPException(status_code=404, detail="Asset not found")
    headers = {
        "Cache-Control": "public, max-age=31536000, immutable",
        "X-Content-Type-Options": "nosniff",
        "ETag": f'"{asset.sha256}"',
    }
    if asset.mime == MIME_SVG:
        headers["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'"
    return Response(content=asset.data, media_type=asset.mime, headers=headers)


# ── admin ──────────────────────────────────────────────────────────────

def _read_capped(upload: UploadFile, cap: int, label: str) -> bytes:
    data = upload.file.read(cap + 1)
    if len(data) > cap:
        raise HTTPException(status_code=413, detail=f"{label} is larger than {cap // (1024 * 1024)} MB")
    return data


@router.post("/admin/brand-theme/drafts", response_model=BrandThemeUploadResult)
def upload_brand_theme_draft(
    session: SessionDep,
    current_user: SuperUser,
    package: Annotated[UploadFile | None, File(description="The package as a .zip")] = None,
    theme: Annotated[UploadFile | None, File(description="theme.json, when not sending a zip")] = None,
    files: Annotated[list[UploadFile] | None, File(description="Asset files, keyed by file name")] = None,
) -> Any:
    """Upload a package (a zip, or ``theme`` plus asset ``files``).

    A valid package is stored as a draft and its ``id`` returned. An invalid
    one is **not saved**: ``id`` is null and ``errors`` lists every problem,
    each with a dotted path, a stable code and what to change.
    """
    if (package is None) == (theme is None):
        raise HTTPException(status_code=400, detail="Send either 'package' (a zip) or 'theme' (theme.json), not both")
    try:
        if package is not None:
            if files:
                raise HTTPException(status_code=400, detail="'files' go with 'theme', not with a zip 'package'")
            theme_bytes, assets = read_zip(_read_capped(package, MAX_TOTAL_BYTES, "The package"))
        else:
            assert theme is not None
            theme_bytes = _read_capped(theme, THEME_MAX_BYTES, "theme.json")
            uploads = files or []
            if len(uploads) > MAX_ENTRIES:
                raise HTTPException(status_code=413, detail=f"At most {MAX_ENTRIES} files are accepted")
            # A generator: assets_from_files enforces the total as it reads,
            # so an oversized upload stops before every file is in memory.
            assets = assets_from_files(
                (f.filename or "", _read_capped(f, MAX_FILE_BYTES, f.filename or "A file")) for f in uploads
            )
    except PackageError as exc:
        return BrandThemeUploadResult(
            id=None, errors=[BrandThemeIssue(path="package", code=exc.code, message=exc.message)]
        )
    try:
        return BrandThemeService.create_draft(session, current_user, theme_bytes, assets)
    except BrandThemeError as exc:
        raise _http(exc)


@router.get("/admin/brand-theme/drafts", response_model=BrandThemeDraftsPublic)
def list_brand_theme_drafts(session: SessionDep, current_user: SuperUser) -> Any:
    """Every stored theme, newest first; the published one has ``status: published``."""
    rows = BrandThemeService.list_drafts(session)
    return BrandThemeDraftsPublic(data=rows, count=len(rows), max_drafts=BRAND_THEME_MAX_DRAFTS)


@router.get("/admin/brand-theme/drafts/{theme_id}", response_model=BrandThemeDraftPublic)
def get_brand_theme_draft(theme_id: uuid.UUID, session: SessionDep, current_user: SuperUser) -> Any:
    try:
        return BrandThemeService.get_draft(session, theme_id)
    except BrandThemeError as exc:
        raise _http(exc)


@router.delete("/admin/brand-theme/drafts/{theme_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_brand_theme_draft(theme_id: uuid.UUID, session: SessionDep, current_user: SuperUser) -> Response:
    """Delete a draft (409 while it is published) and any asset no other draft uses."""
    try:
        BrandThemeService.delete_draft(session, theme_id)
    except BrandThemeError as exc:
        raise _http(exc)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/admin/brand-theme/drafts/{theme_id}/publish",
    response_model=BrandThemeDraftPublic,
    responses={409: {"model": BrandThemePublishErrors, "description": "The draft no longer passes validation."}},
)
def publish_brand_theme_draft(
    theme_id: uuid.UUID,
    session: SessionDep,
    current_user: SuperUser,
    background_tasks: BackgroundTasks,
) -> Any:
    """Re-validate the draft, then make it the server's one published theme
    (the previous one returns to draft). 409 with ``errors`` if the rules have
    tightened since it was uploaded."""
    try:
        published, changed = BrandThemeService.publish(session, current_user, theme_id)
    except BrandThemeError as exc:
        if exc.status == 409 and isinstance(exc.detail, dict):
            return JSONResponse(status_code=409, content=exc.detail)
        raise _http(exc)
    if changed:
        background_tasks.add_task(BrandThemeEvents.updated, published.revision)
    return published


@router.post("/admin/brand-theme/unpublish", status_code=status.HTTP_204_NO_CONTENT)
def unpublish_brand_theme(
    session: SessionDep, current_user: SuperUser, background_tasks: BackgroundTasks
) -> Response:
    """Return the published theme to draft; everyone goes back to stock Cinna.
    Idempotent."""
    previous = BrandThemeService.unpublish(session)
    if previous is not None:
        background_tasks.add_task(BrandThemeEvents.updated, None)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
