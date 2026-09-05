"""Cinna Desktop download resolver — public, unauthenticated.

  GET /desktop/download?os=darwin|linux&arch=arm64|x64&kind=dmg|appimage|deb

The onboarding landing page links here so it never has to hard-code a version.
The endpoint **only ever redirects**: it resolves the current release's asset
URL and 302s the browser to GitHub (or to the operator's configured mirror),
and never proxies the installer bytes. That keeps the backend out of the
executable-distribution path entirely — it cannot launder a binary behind a
trusted origin, and an unauthenticated caller cannot turn it into a bandwidth
amplifier. See ``DesktopReleaseService`` for the rest of that reasoning.

Resolution failure is never a 5xx: any error, or a combination we publish no
asset for, redirects to the human-readable releases page instead.
"""
from typing import Literal

from fastapi import APIRouter, Query
from fastapi.responses import RedirectResponse

from app.services.desktop_auth.desktop_release_service import DesktopReleaseService

router = APIRouter(prefix="/desktop", tags=["desktop-download"])


@router.get(
    "/download",
    # Declared so the OpenAPI spec (and the generated client) describe what
    # this endpoint actually does — a redirect — instead of an empty 200.
    status_code=302,
    response_class=RedirectResponse,
)
async def download_desktop(
    os: Literal["darwin", "linux"] = Query(
        ..., description="Target operating system."
    ),
    arch: Literal["arm64", "x64"] = Query(..., description="Target architecture."),
    kind: Literal["dmg", "appimage", "deb"] = Query(
        ..., description="Installer format."
    ),
) -> RedirectResponse:
    """Redirect to the current Cinna Desktop installer for this platform.

    Public — the visitor has not signed in yet when they click the download
    button in the new-account email's landing page.

    The three parameters are ``Literal``-typed, so an out-of-range value is
    rejected by FastAPI's own validation with a 422 before this function runs.
    That is deliberate: a value we do not recognise is a malformed request and
    should say so, whereas a combination of individually-valid values that we
    publish no asset for (``os=linux&kind=dmg``, ``os=linux&arch=arm64``, ...)
    is a resolution miss and falls back to the releases page like any other.
    """
    url = await DesktopReleaseService.resolve_download_url(
        os=os, arch=arch, kind=kind
    )
    return RedirectResponse(url=url, status_code=302)
