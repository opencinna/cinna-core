"""
Identity Contacts API routes — target user management of received identity contacts.

Users manage which people's identities they have enabled/disabled in their routing.
"""
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException
from sqlmodel import SQLModel

from app.api.deps import CurrentUser, SessionDep
from app.models import Message
from app.models.identity.identity_models import IdentityContactPublic
from app.services.identity.identity_service import IdentityService

router = APIRouter(prefix="/users/me/identity-contacts", tags=["identity-contacts"])


class ToggleIdentityContactRequest(SQLModel):
    is_enabled: bool


@router.get("/", response_model=list[IdentityContactPublic])
def list_identity_contacts(
    session: SessionDep,
    current_user: CurrentUser,
) -> Any:
    """List identity contacts (people who shared agents with me via identity)."""
    return IdentityService.get_identity_contacts(
        db_session=session,
        user_id=current_user.id,
    )


@router.patch("/{owner_id}", response_model=Message)
def toggle_identity_contact(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    owner_id: uuid.UUID,
    toggle_in: ToggleIdentityContactRequest,
) -> Any:
    """Toggle all assignments from a given identity owner on/off (per-person toggle)."""
    success = IdentityService.toggle_identity_contact(
        db_session=session,
        owner_id=owner_id,
        user_id=current_user.id,
        is_enabled=toggle_in.is_enabled,
    )
    if not success:
        raise HTTPException(
            status_code=404,
            detail="No identity contact found from this owner",
        )
    return Message(message="Identity contact updated")
