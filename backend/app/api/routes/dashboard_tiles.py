"""Routes of an ``agent_component`` dashboard block (a tile).

The owner's open dashboard page drives these: cached data first, refresh with a
reason, a whole-set params change, a page ``cinna.call``, a frame for a web
view, and the page's highlight readiness. Another user's dashboard or block,
or a block of another view type, is ``not_found``.
"""
import uuid
from typing import Any

from fastapi import APIRouter, Response, status

from app.api.deps import CurrentUser, SessionDep
from app.api.routes._dashboard_component_errors import (
    COMPONENT_ERROR_RESPONSES,
    component_error_response,
)
from app.models.dashboards.dashboard_component import (
    DashboardFramePublic,
    DashboardTileCallRequest,
    DashboardTileCallResult,
    DashboardTileDataPublic,
    DashboardTilePageStateRequest,
    DashboardTileParamsRequest,
    DashboardTileRefreshRequest,
)
from app.services.dashboards import frame_cap
from app.services.dashboards.component_service import DashboardComponentError
from app.services.dashboards.tile_service import TileService

router = APIRouter(
    prefix="/dashboards/{dashboard_id}/blocks/{block_id}/component",
    tags=["DashboardTiles"],
    responses=COMPONENT_ERROR_RESPONSES,
)


@router.get("/data", response_model=DashboardTileDataPublic)
async def get_dashboard_tile_data(
    dashboard_id: uuid.UUID, block_id: uuid.UUID, session: SessionDep, current_user: CurrentUser
) -> Any:
    """The tile's cached data (stale or not); never wakes the environment."""
    try:
        block = TileService.get_block(session, dashboard_id, block_id, current_user.id)
        return await TileService.get_data(session, block, current_user)
    except DashboardComponentError as e:
        return component_error_response(e)


@router.post("/refresh", response_model=DashboardTileDataPublic)
async def refresh_dashboard_tile(
    dashboard_id: uuid.UUID,
    block_id: uuid.UUID,
    body: DashboardTileRefreshRequest,
    session: SessionDep,
    current_user: CurrentUser,
) -> Any:
    try:
        block = TileService.get_block(session, dashboard_id, block_id, current_user.id)
        return await TileService.refresh(session, block, current_user, body.reason)
    except DashboardComponentError as e:
        return component_error_response(e)


@router.put("/params", response_model=DashboardTileDataPublic)
async def set_dashboard_tile_params(
    dashboard_id: uuid.UUID,
    block_id: uuid.UUID,
    body: DashboardTileParamsRequest,
    session: SessionDep,
    current_user: CurrentUser,
) -> Any:
    """Replace the tile's param set and recompute (budgeted)."""
    try:
        block = TileService.get_block(session, dashboard_id, block_id, current_user.id)
        return await TileService.set_params(session, block, current_user, body.params)
    except DashboardComponentError as e:
        return component_error_response(e)


@router.post("/call", response_model=DashboardTileCallResult)
async def call_dashboard_tile_endpoint(
    dashboard_id: uuid.UUID,
    block_id: uuid.UUID,
    body: DashboardTileCallRequest,
    session: SessionDep,
    current_user: CurrentUser,
) -> Any:
    """Run one of the component's endpoints for the page (budgeted, never cached)."""
    try:
        block = TileService.get_block(session, dashboard_id, block_id, current_user.id)
        value = await TileService.call(session, block, current_user, body.endpoint, body.params)
    except DashboardComponentError as e:
        return component_error_response(e)
    return DashboardTileCallResult(value=value)


@router.post("/frame", response_model=DashboardFramePublic)
async def mint_dashboard_tile_frame(
    dashboard_id: uuid.UUID, block_id: uuid.UUID, session: SessionDep, current_user: CurrentUser
) -> Any:
    """A frame URL for the tile's page (wakes the environment)."""
    try:
        block = TileService.get_block(session, dashboard_id, block_id, current_user.id)
        entry_page = await TileService.frame_entry(session, block)
    except DashboardComponentError as e:
        return component_error_response(e)
    cap, expires_at = frame_cap.mint(current_user.id, frame_cap.block_target(block.id))
    return DashboardFramePublic(frame_url=frame_cap.frame_url(cap, entry_page), expires_at=expires_at)


@router.put("/page-state", status_code=status.HTTP_204_NO_CONTENT)
async def set_dashboard_tile_page_state(
    dashboard_id: uuid.UUID,
    block_id: uuid.UUID,
    body: DashboardTilePageStateRequest,
    session: SessionDep,
    current_user: CurrentUser,
) -> Response:
    """Record whether the open page's current document draws highlights."""
    try:
        block = TileService.get_block(session, dashboard_id, block_id, current_user.id)
    except DashboardComponentError as e:
        return component_error_response(e)
    TileService.set_page_state(
        session, block, body.page_doc, body.highlight_ready, body.seq, body.if_page_doc
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
