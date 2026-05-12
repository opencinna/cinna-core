"""
Agent-scoped API routes for App MCP Server route management.

Any user can manage App MCP routes for agents they own.
Superusers can manage routes for any agent.
"""
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException

from app.api.deps import CurrentUser, SessionDep
from app.models import Agent, Message
from app.models.app_mcp.app_agent_route import (
    AppAgentRouteCreate,
    AppAgentRouteUpdate,
    AppAgentRoutePublic,
    AppAgentRouteAssignmentPublic,
    RouteConflictResponse,
)
from app.services.app_mcp.app_agent_route_service import AppAgentRouteService

router = APIRouter(
    prefix="/agents/{agent_id}/app-mcp-routes",
    tags=["agent-app-mcp-routes"],
)


def _validate_prompt_examples(value: str | None) -> None:
    if not value:
        return
    if len(value) > 2000:
        raise HTTPException(status_code=422, detail="Prompt examples must be under 2000 characters")
    non_empty = [line for line in value.splitlines() if line.strip()]
    if len(non_empty) > 10:
        raise HTTPException(status_code=422, detail="Maximum 10 prompt examples allowed")


@router.get("/", response_model=list[AppAgentRoutePublic])
def list_agent_app_mcp_routes(
    agent_id: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
) -> Any:
    """List App MCP routes for this agent.

    Non-superusers only see routes they created.
    Superusers see all routes for the agent.
    """
    agent = session.get(Agent, agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if not current_user.is_superuser and agent.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")
    return AppAgentRouteService.list_routes_for_agent(
        db_session=session,
        agent_id=agent_id,
        current_user=current_user,
    )


@router.post("/", response_model=AppAgentRoutePublic)
def create_agent_app_mcp_route(
    agent_id: uuid.UUID,
    route_in: AppAgentRouteCreate,
    session: SessionDep,
    current_user: CurrentUser,
) -> Any:
    """Create an App MCP route for this agent.

    Non-superusers can only create routes for agents they own.
    Only superusers can set auto_enable_for_users=True.
    """
    _validate_prompt_examples(route_in.prompt_examples)
    # Override agent_id in body with the path parameter
    route_in_with_agent = route_in.model_copy(update={"agent_id": agent_id})
    try:
        return AppAgentRouteService.create_route(
            db_session=session,
            data=route_in_with_agent,
            current_user=current_user,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.put("/{route_id}", response_model=AppAgentRoutePublic)
def update_agent_app_mcp_route(
    agent_id: uuid.UUID,
    route_id: uuid.UUID,
    route_in: AppAgentRouteUpdate,
    session: SessionDep,
    current_user: CurrentUser,
) -> Any:
    """Update an App MCP route."""
    _validate_prompt_examples(route_in.prompt_examples)
    try:
        route = AppAgentRouteService.update_route_for_agent(
            db_session=session,
            agent_id=agent_id,
            route_id=route_id,
            data=route_in,
            current_user=current_user,
        )
    except ValueError as e:
        raise HTTPException(status_code=403, detail=str(e))
    if not route:
        raise HTTPException(status_code=404, detail="Route not found")
    return route


@router.delete("/{route_id}")
def delete_agent_app_mcp_route(
    agent_id: uuid.UUID,
    route_id: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
) -> Message:
    """Delete an App MCP route."""
    try:
        deleted = AppAgentRouteService.delete_route_for_agent(
            db_session=session,
            agent_id=agent_id,
            route_id=route_id,
            current_user=current_user,
        )
    except ValueError as e:
        raise HTTPException(status_code=403, detail=str(e))
    if not deleted:
        raise HTTPException(status_code=404, detail="Route not found")
    return Message(message="Route deleted successfully")


@router.get("/conflicts", response_model=RouteConflictResponse)
def check_route_conflicts(
    agent_id: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
) -> Any:
    """Find effective routes similar to this agent's auto-managed route.

    Cheap token-overlap (Jaccard) comparison against the caller's other
    effective App MCP routes — surfaced as a non-blocking toast on the
    install completion page when a near-duplicate intent is detected.
    Returns an empty match list when the agent has no auto-managed route
    yet (no router_trigger_prompt at install time) or when no other route
    crosses the similarity threshold.

    Permission: same ownership gate as the other endpoints on this
    router — the caller must own the agent (or be a superuser).
    """
    agent = session.get(Agent, agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if not current_user.is_superuser and agent.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")
    return AppAgentRouteService.find_route_conflicts_for_agent(
        db_session=session,
        agent_id=agent_id,
        user_id=current_user.id,
    )


@router.post("/{route_id}/assignments", response_model=list[AppAgentRouteAssignmentPublic])
def assign_users_to_agent_route(
    agent_id: uuid.UUID,
    route_id: uuid.UUID,
    user_ids: list[uuid.UUID],
    session: SessionDep,
    current_user: CurrentUser,
) -> Any:
    """Assign users to an App MCP route."""
    route = AppAgentRouteService.get_route_for_agent(
        db_session=session,
        agent_id=agent_id,
        route_id=route_id,
        current_user=current_user,
    )
    if not route:
        raise HTTPException(status_code=404, detail="Route not found")
    return AppAgentRouteService.assign_users(
        db_session=session,
        route_id=route_id,
        user_ids=user_ids,
        auto_enable=route.auto_enable_for_users,
    )


@router.delete("/{route_id}/assignments/{user_id}")
def remove_user_assignment_from_agent_route(
    agent_id: uuid.UUID,
    route_id: uuid.UUID,
    user_id: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
) -> Message:
    """Remove a user assignment from an App MCP route."""
    route = AppAgentRouteService.get_route_for_agent(
        db_session=session,
        agent_id=agent_id,
        route_id=route_id,
        current_user=current_user,
    )
    if not route:
        raise HTTPException(status_code=404, detail="Route not found")
    removed = AppAgentRouteService.remove_assignment(
        db_session=session,
        route_id=route_id,
        user_id=user_id,
    )
    if not removed:
        raise HTTPException(status_code=404, detail="Assignment not found")
    return Message(message="Assignment removed successfully")
