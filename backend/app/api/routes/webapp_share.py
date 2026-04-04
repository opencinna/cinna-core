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
from app.services.webapp.agent_webapp_share_service import (
    AgentWebappShareService,
    WebappShareError,
    ShareNotFoundError,
)


class WebappShareAuthRequest(BaseModel):
    security_code: str | None = None


router = APIRouter(prefix="/agents/{agent_id}/webapp-shares", tags=["webapp-shares"])


def _handle_service_error(e: WebappShareError) -> None:
    """Convert service exceptions to HTTP exceptions."""
    raise HTTPException(status_code=e.status_code, detail=e.message)


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
    except WebappShareError as e:
        _handle_service_error(e)


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
    except WebappShareError as e:
        _handle_service_error(e)


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
    except WebappShareError as e:
        _handle_service_error(e)

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
    except WebappShareError as e:
        _handle_service_error(e)

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
        return AgentWebappShareService.authenticate(
            session, token, security_code=security_code
        )
    except WebappShareError as e:
        _handle_service_error(e)
