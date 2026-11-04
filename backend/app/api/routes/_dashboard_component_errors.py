"""Shared error translation for the agent-component routes.

``services/dashboards`` raises ``DashboardComponentError(code, message, …)``;
both route modules (``agent_dashboards.py``, ``dashboard_tiles.py``) answer
with the body ``{error: {code, message, retry_after_s?, field?}}`` and the
code's status. Kept in one module so the mapping cannot drift between them.
"""
import re
from typing import Any

from fastapi.responses import JSONResponse

from app.models.dashboards.dashboard_component import DashboardComponentErrorBody
from app.services.dashboards.component_service import DashboardComponentError

TILE_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

COMPONENT_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    status: {"model": DashboardComponentErrorBody}
    for status in (400, 404, 409, 429, 502, 503)
}


def component_error_response(error: DashboardComponentError) -> JSONResponse:
    headers = {"Cache-Control": "no-store"}
    if error.retry_after_s is not None:
        headers["Retry-After"] = str(error.retry_after_s)
    return JSONResponse(status_code=error.status_code, content=error.to_body(), headers=headers)


def check_tile_key(tile_key: str | None) -> None:
    """A desktop tile key is a short opaque name; it becomes part of a budget key."""
    if tile_key is not None and not TILE_KEY_RE.match(tile_key):
        raise DashboardComponentError(
            "invalid_input", "`tile_key` uses letters, digits, `.`, `_` and `-` only.", field="tile_key"
        )
