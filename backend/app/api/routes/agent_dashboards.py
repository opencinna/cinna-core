"""Agent-scoped dashboard component routes (D-C9).

The primitive the core preview and the desktop's remote tiles use: list an
agent's components, run one of them with explicit params, and mint a frame for
its page. Owner only; another user's agent is ``not_found``. Block routes
(``dashboard_tiles.py``) add stored params, the cache and the per-tile budget
on top.
"""
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Query

from app.api.deps import CurrentUser, SessionDep
from app.api.routes._dashboard_component_errors import (
    COMPONENT_ERROR_RESPONSES,
    check_tile_key,
    component_error_response,
)
from app.models.dashboards.dashboard_component import (
    DashboardComponentFrameRequest,
    DashboardComponentPublic,
    DashboardComponentRunRequest,
    DashboardComponentRunResult,
    DashboardComponentsPublic,
    DashboardFramePublic,
)
from app.services.dashboards import frame_cap
from app.services.dashboards.agent_component_service import AgentComponentService
from app.services.dashboards.component_service import DashboardComponentError

# Reads list env-core's components through a short per-worker cache (a few
# seconds), which is right for the web UI and Cinna Desktop. A caller that just
# changed the agent's files (``cinna dashboards`` after a push) asks for a
# fresh read. No extra guard: the route is owner-only, a fresh read is one
# listing round-trip to the owner's own running environment (the same call the
# cache makes when it expires), and it never wakes an environment the cached
# read would not.
FRESH_DESCRIPTION = (
    "Skip the short per-worker component list cache and read the environment now "
    "(use right after changing the agent's files)."
)

router = APIRouter(
    prefix="/agents/{agent_id}/dashboards",
    tags=["AgentDashboards"],
    responses=COMPONENT_ERROR_RESPONSES,
)


@router.get("/", response_model=DashboardComponentsPublic)
async def list_agent_dashboard_components(
    agent_id: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
    fresh: bool = Query(default=False, description=FRESH_DESCRIPTION),
) -> Any:
    """The agent's components with the checker's findings and where each is placed."""
    try:
        return await AgentComponentService.list_public(session, current_user, agent_id, fresh=fresh)
    except DashboardComponentError as e:
        return component_error_response(e)


@router.get("/{name}", response_model=DashboardComponentPublic)
async def get_agent_dashboard_component(
    agent_id: uuid.UUID,
    name: str,
    session: SessionDep,
    current_user: CurrentUser,
    skill: str | None = Query(default=None, max_length=128),
    fresh: bool = Query(default=False, description=FRESH_DESCRIPTION),
) -> Any:
    try:
        return await AgentComponentService.get_public(
            session, current_user, agent_id, name, skill, fresh=fresh
        )
    except DashboardComponentError as e:
        return component_error_response(e)


@router.post("/{name}/run", response_model=DashboardComponentRunResult)
async def run_agent_dashboard_component(
    agent_id: uuid.UUID,
    name: str,
    body: DashboardComponentRunRequest,
    session: SessionDep,
    current_user: CurrentUser,
) -> Any:
    """Run the component's source (no ``endpoint``) or one of its endpoints with these params."""
    try:
        check_tile_key(body.tile_key)
        value, params_resolved = await AgentComponentService.run(
            session, current_user, agent_id, name, body
        )
    except DashboardComponentError as e:
        return component_error_response(e)
    notice = value.get("notice") if isinstance(value, dict) else None
    return DashboardComponentRunResult(
        value=value,
        fetched_at=datetime.now(UTC),
        notice=notice.splitlines()[0][:500] if isinstance(notice, str) and notice else None,
        params_resolved=params_resolved,
    )


@router.post("/{name}/frame", response_model=DashboardFramePublic)
async def mint_agent_dashboard_component_frame(
    agent_id: uuid.UUID,
    name: str,
    body: DashboardComponentFrameRequest,
    session: SessionDep,
    current_user: CurrentUser,
) -> Any:
    """A frame URL for the component's page (wakes the environment)."""
    try:
        check_tile_key(body.tile_key)
        entry_page = await AgentComponentService.frame_entry(
            session, current_user, agent_id, name, body.skill
        )
    except DashboardComponentError as e:
        return component_error_response(e)
    cap, expires_at = frame_cap.mint(
        current_user.id, frame_cap.component_target(agent_id, body.skill, name, body.tile_key)
    )
    return DashboardFramePublic(frame_url=frame_cap.frame_url(cap, entry_page), expires_at=expires_at)
