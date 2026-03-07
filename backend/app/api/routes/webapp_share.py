"""
Webapp Share API Routes.

Two routers:
1. ``router`` — Owner management of webapp share links (CRUD, requires auth).
2. ``public_router`` — Public auth flow (token validation, JWT issuance).
"""
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.api.deps import CurrentUser, SessionDep
from app.models import (
    AgentWebappShareCreate,
    AgentWebappShareUpdate,
    AgentWebappSharePublic,
    AgentWebappShareCreated,
    AgentWebappSharesPublic,
    Message,
)
from app.services.agent_webapp_share_service import AgentWebappShareService


class WebappShareAuthRequest(BaseModel):
    security_code: str | None = None


router = APIRouter(prefix="/agents/{agent_id}/webapp-shares", tags=["webapp-shares"])


@router.post("/", response_model=AgentWebappShareCreated)
def create_webapp_share(
    agent_id: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
    share_in: AgentWebappShareCreate,
) -> Any:
    """Create a new webapp share link for an agent."""
    try:
        return AgentWebappShareService.create_webapp_share(
            session, current_user.id, agent_id, share_in
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/", response_model=AgentWebappSharesPublic)
def list_webapp_shares(
    agent_id: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
) -> Any:
    """List all webapp share links for an agent."""
    try:
        return AgentWebappShareService.list_webapp_shares(
            session, current_user.id, agent_id
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.patch("/{share_id}", response_model=AgentWebappSharePublic)
def update_webapp_share(
    agent_id: uuid.UUID,
    share_id: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
    share_in: AgentWebappShareUpdate,
) -> Any:
    """Update a webapp share link."""
    try:
        share = AgentWebappShareService.update_webapp_share(
            session, current_user.id, agent_id, share_id, share_in
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    if not share:
        raise HTTPException(status_code=404, detail="Webapp share not found")
    return share


@router.delete("/{share_id}")
def delete_webapp_share(
    agent_id: uuid.UUID,
    share_id: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
) -> Message:
    """Delete a webapp share link."""
    try:
        success = AgentWebappShareService.delete_webapp_share(
            session, current_user.id, agent_id, share_id
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    if not success:
        raise HTTPException(status_code=404, detail="Webapp share not found")
    return Message(message="Webapp share deleted successfully")


# ── Public auth flow router ──────────────────────────────────────────────

public_router = APIRouter(prefix="/webapp-share", tags=["webapp-share"])


@public_router.get("/{token}/info")
def webapp_share_info(
    token: str,
    session: SessionDep,
) -> Any:
    """Get public information about a webapp share link."""
    return AgentWebappShareService.get_share_info(session, token)


@public_router.post("/{token}/auth")
def webapp_share_authenticate(
    token: str,
    session: SessionDep,
    body: WebappShareAuthRequest | None = None,
) -> Any:
    """Authenticate via a webapp share token. Returns a short-lived JWT."""
    security_code = body.security_code if body else None
    try:
        result = AgentWebappShareService.authenticate(
            session, token, security_code=security_code
        )
    except ValueError as e:
        detail = str(e)
        if "security code" in detail.lower() or "blocked" in detail.lower():
            raise HTTPException(status_code=403, detail=detail)
        raise HTTPException(status_code=410, detail=detail)

    if result is None:
        raise HTTPException(status_code=404, detail="Webapp share not found")
    return result
