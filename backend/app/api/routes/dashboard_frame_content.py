"""Dashboard component frames: ``/d/v/<cap>/<path>`` (not in the schema).

No user authentication: the frame cap in the path is the credential, and every
request re-checks its owner against the block or agent. GET and HEAD only; any
other method answers 405 (OPTIONS preflights are answered by the content
middleware). CORS and the baseline headers for responses that never reach this
handler are the job of
:class:`app.core.artifact_content_middleware.ArtifactContentMiddleware`.
"""
from fastapi import APIRouter, Request
from starlette.responses import Response

from app.api.deps import SessionDep
from app.services.dashboards.frame_content import (
    FrameContentError,
    FrameContentService,
    error_headers,
)

router = APIRouter(tags=["dashboard-frame-content"], include_in_schema=False)

_REFUSED_METHODS = ["POST", "PUT", "PATCH", "DELETE"]


def _error(status: int, message: str) -> Response:
    return Response(
        content=message,
        status_code=status,
        media_type="text/plain; charset=utf-8",
        headers=error_headers(),
    )


@router.api_route("/d/v/{cap}/{path:path}", methods=["GET", "HEAD"])
async def dashboard_frame_content(
    cap: str, path: str, request: Request, session: SessionDep
) -> Response:
    try:
        served = await FrameContentService.serve(
            session, cap, path, is_head=request.method == "HEAD"
        )
    except FrameContentError as e:
        return _error(e.status, e.message)
    return Response(content=served.body, media_type=served.media_type, headers=served.headers)


@router.api_route("/d/v/{cap}/{path:path}", methods=_REFUSED_METHODS)
def dashboard_frame_content_method_not_allowed(cap: str, path: str) -> Response:
    """Explicit 405 so a write never reaches a handler, with the error headers."""
    response = _error(405, "Method Not Allowed")
    response.headers["Allow"] = "GET, HEAD"
    return response
