"""The agent's dashboard tile tools, called by env-core (``AgentEnvContextDep``).

- ``POST /agent/dashboards/tiles {session_id}`` — ``dashboards_tiles``.
- ``POST /agent/dashboards/update-tile {session_id, tile_id, params?, highlight?}``
  — ``dashboards_update_tile``.

Both answer HTTP 200: the tool's result, or ``{error: {code, message, …}}``
with the codes of ``dashboards-tools.schema.json`` only. env-core turns an
error into ``isError`` plus ``structuredContent``.
"""
import logging
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.api.deps import AgentEnvContextDep, SessionDep
from app.models.dashboards.dashboard_chat import (
    AgentDashboardTilesRequest,
    AgentDashboardTilesResult,
    AgentDashboardUpdateTileRequest,
    AgentDashboardUpdateTileResult,
    AgentTileToolError,
)
from app.services.dashboards.component_service import DashboardComponentError
from app.services.dashboards.tile_tools_service import TileToolsService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agent/dashboards", tags=["agent-dashboards"])

TOOL_ERROR_CODES = frozenset({"not_found", "invalid_input", "rate_limited", "no_dashboard"})


def _tool_error(error: DashboardComponentError) -> JSONResponse:
    if error.code not in TOOL_ERROR_CODES:
        # Never leak a code the tool contract does not have.
        logger.warning("Dashboard tile tool refusal with code %s mapped to not_found", error.code)
        error = DashboardComponentError("not_found", error.message)
    return JSONResponse(status_code=200, content=error.to_body(), headers={"Cache-Control": "no-store"})


@router.post("/tiles", response_model=AgentDashboardTilesResult | AgentTileToolError)
async def agent_dashboard_tiles(
    body: AgentDashboardTilesRequest, db: SessionDep, ctx: AgentEnvContextDep
) -> Any:
    try:
        result = await TileToolsService.tiles(db, ctx.agent, ctx.owner, body.session_id)
    except DashboardComponentError as e:
        return _tool_error(e)
    return JSONResponse(content=result, headers={"Cache-Control": "no-store"})


@router.post("/update-tile", response_model=AgentDashboardUpdateTileResult | AgentTileToolError)
async def agent_dashboard_update_tile(
    body: AgentDashboardUpdateTileRequest, db: SessionDep, ctx: AgentEnvContextDep
) -> Any:
    try:
        result = await TileToolsService.update_tile(
            db, ctx.agent, ctx.owner, body.session_id, body.tile_id, body.params, body.highlight
        )
    except DashboardComponentError as e:
        return _tool_error(e)
    return JSONResponse(content=result, headers={"Cache-Control": "no-store"})
