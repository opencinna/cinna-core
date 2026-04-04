"""
Guest Share API Routes.

Two routers:
1. ``router`` — Owner management of guest share links (CRUD, requires auth).
2. ``guest_router`` — Guest auth flow (anonymous JWT, grant activation, info).
"""
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.api.deps import CurrentUser, SessionDep
from app.models import (
    AgentGuestShareCreate,
    AgentGuestShareUpdate,
    AgentGuestSharePublic,
    AgentGuestShareCreated,
    AgentGuestSharesPublic,
    Message,
)
from app.services.sharing.agent_guest_share_service import AgentGuestShareService


class GuestShareAuthRequest(BaseModel):
    """Optional body for guest share auth/activate endpoints."""
    security_code: str | None = None

router = APIRouter(prefix="/agents/{agent_id}/guest-shares", tags=["guest-shares"])


@router.post("/", response_model=AgentGuestShareCreated)
def create_guest_share(
    agent_id: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
    share_in: AgentGuestShareCreate,
) -> Any:
    """
    Create a new guest share link for an agent.

    IMPORTANT: The token value and share URL are only returned once on creation.
    Store them securely - they cannot be retrieved later.
    """
    try:
        share = AgentGuestShareService.create_guest_share(
            session, current_user.id, agent_id, share_in
        )
        return share
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/", response_model=AgentGuestSharesPublic)
def list_guest_shares(
    agent_id: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
) -> Any:
    """
    List all guest share links for an agent.
    """
    try:
        return AgentGuestShareService.list_guest_shares(
            session, current_user.id, agent_id
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/{guest_share_id}", response_model=AgentGuestSharePublic)
def get_guest_share(
    agent_id: uuid.UUID,
    guest_share_id: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
) -> Any:
    """
    Get a specific guest share link by ID.
    """
    try:
        share = AgentGuestShareService.get_guest_share(
            session, current_user.id, agent_id, guest_share_id
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    if not share:
        raise HTTPException(status_code=404, detail="Guest share not found")
    return share


@router.delete("/{guest_share_id}")
def delete_guest_share(
    agent_id: uuid.UUID,
    guest_share_id: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
) -> Message:
    """
    Delete a guest share link.

    Note: Existing sessions created via this share will retain their
    guest_share_id reference but the share link will no longer be usable.
    """
    try:
        success = AgentGuestShareService.delete_guest_share(
            session, current_user.id, agent_id, guest_share_id
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    if not success:
        raise HTTPException(status_code=404, detail="Guest share not found")
    return Message(message="Guest share deleted successfully")


@router.put("/{guest_share_id}", response_model=AgentGuestSharePublic)
def update_guest_share(
    agent_id: uuid.UUID,
    guest_share_id: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
    share_in: AgentGuestShareUpdate,
) -> Any:
    """
    Update a guest share link (label and/or security code).

    If a new security code is provided, the failed attempt counter and
    blocked state are reset automatically.
    """
    try:
        share = AgentGuestShareService.update_guest_share(
            session, current_user.id, agent_id, guest_share_id, share_in
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    if not share:
        raise HTTPException(status_code=404, detail="Guest share not found")
    return share


# ── Guest auth flow router ──────────────────────────────────────────────
# These endpoints are used by guests (unauthenticated or authenticated)
# to interact with guest share links.

guest_router = APIRouter(prefix="/guest-share", tags=["guest-share"])


@guest_router.post("/{token}/auth")
def guest_share_authenticate(
    token: str,
    session: SessionDep,
    body: GuestShareAuthRequest | None = None,
) -> Any:
    """
    Authenticate anonymously via a guest share token.

    No authentication required. Returns a short-lived guest JWT
    for anonymous chat access to the agent.

    If the share has a security code, include it in the request body.
    """
    security_code = body.security_code if body else None
    try:
        result = AgentGuestShareService.authenticate_anonymous(
            session, token, security_code=security_code
        )
    except ValueError as e:
        detail = str(e)
        if "security code" in detail.lower() or "blocked" in detail.lower():
            raise HTTPException(status_code=403, detail=detail)
        raise HTTPException(status_code=410, detail=detail)

    if result is None:
        raise HTTPException(status_code=404, detail="Guest share not found")

    return result


@guest_router.post("/{token}/activate")
def guest_share_activate(
    token: str,
    session: SessionDep,
    current_user: CurrentUser,
    body: GuestShareAuthRequest | None = None,
) -> Any:
    """
    Activate a guest share grant for the current authenticated user.

    Requires a valid user JWT. Creates a persistent grant record
    so the user can access the agent without the share link.
    Idempotent — calling twice with the same user and token is safe.

    If the share has a security code, include it in the request body.
    """
    security_code = body.security_code if body else None
    try:
        result = AgentGuestShareService.activate_for_user(
            session, token, current_user.id, security_code=security_code
        )
    except ValueError as e:
        detail = str(e)
        if "security code" in detail.lower() or "blocked" in detail.lower():
            raise HTTPException(status_code=403, detail=detail)
        raise HTTPException(status_code=410, detail=detail)

    if result is None:
        raise HTTPException(status_code=404, detail="Guest share not found")

    return result


@guest_router.get("/{token}/info")
def guest_share_info(
    token: str,
    session: SessionDep,
) -> Any:
    """
    Get public information about a guest share link.

    No authentication required. Returns agent name, description,
    and whether the share link is still valid.
    """
    return AgentGuestShareService.get_guest_share_info(session, token)
