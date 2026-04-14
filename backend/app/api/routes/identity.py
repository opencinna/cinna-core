"""
Identity API routes — identity agent bindings and assignments.

Identity owners manage which of their agents are exposed behind their identity,
and which users can reach each agent.
"""
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException
from sqlalchemy.exc import IntegrityError

from app.api.deps import CurrentUser, SessionDep
from app.models import Message
from app.models.identity.identity_models import (
    IdentityAgentBindingCreate,
    IdentityAgentBindingUpdate,
    IdentityAgentBindingPublic,
    IdentityBindingAssignmentPublic,
)
from app.services.identity.identity_service import (
    IdentityService,
    IdentityPermissionError,
    IdentityNotFoundError,
)

router = APIRouter(prefix="/identity", tags=["identity"])


# ---------------------------------------------------------------------------
# Identity Agent Bindings
# ---------------------------------------------------------------------------


@router.get("/bindings/", response_model=list[IdentityAgentBindingPublic])
def list_identity_bindings(
    session: SessionDep,
    current_user: CurrentUser,
) -> Any:
    """List all identity agent bindings for the current user (owner view)."""
    return IdentityService.list_bindings(
        db_session=session,
        owner_id=current_user.id,
    )


@router.post("/bindings/", response_model=IdentityAgentBindingPublic)
def create_identity_binding(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    binding_in: IdentityAgentBindingCreate,
) -> Any:
    """Create a new identity agent binding."""
    try:
        return IdentityService.create_binding(
            db_session=session,
            owner_id=current_user.id,
            data=binding_in,
            is_superuser=current_user.is_superuser,
        )
    except IdentityPermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except IdentityNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except IntegrityError:
        raise HTTPException(
            status_code=409,
            detail="Agent already added to identity",
        )


@router.put("/bindings/{binding_id}", response_model=IdentityAgentBindingPublic)
def update_identity_binding(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    binding_id: uuid.UUID,
    binding_in: IdentityAgentBindingUpdate,
) -> Any:
    """Update an identity agent binding."""
    result = IdentityService.update_binding(
        db_session=session,
        binding_id=binding_id,
        owner_id=current_user.id,
        data=binding_in,
    )
    if not result:
        raise HTTPException(status_code=404, detail="Binding not found")
    return result


@router.delete("/bindings/{binding_id}", response_model=Message)
def delete_identity_binding(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    binding_id: uuid.UUID,
) -> Any:
    """Delete an identity agent binding (cascades assignments)."""
    deleted = IdentityService.delete_binding(
        db_session=session,
        binding_id=binding_id,
        owner_id=current_user.id,
    )
    if not deleted:
        raise HTTPException(status_code=404, detail="Binding not found")
    return Message(message="Binding deleted successfully")


# ---------------------------------------------------------------------------
# Binding Assignments
# ---------------------------------------------------------------------------


@router.post(
    "/bindings/{binding_id}/assignments",
    response_model=list[IdentityBindingAssignmentPublic],
)
def assign_users_to_binding(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    binding_id: uuid.UUID,
    user_ids: list[uuid.UUID],
) -> Any:
    """Assign users to a binding (share this agent via identity with those users)."""
    try:
        return IdentityService.assign_users(
            db_session=session,
            binding_id=binding_id,
            owner_id=current_user.id,
            user_ids=user_ids,
            auto_enable=False,
        )
    except IdentityNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except IdentityPermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))


@router.delete("/bindings/{binding_id}/assignments/{user_id}", response_model=Message)
def remove_user_assignment(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    binding_id: uuid.UUID,
    user_id: uuid.UUID,
) -> Any:
    """Remove a user's assignment from a binding."""
    deleted = IdentityService.remove_assignment(
        db_session=session,
        binding_id=binding_id,
        owner_id=current_user.id,
        target_user_id=user_id,
    )
    if not deleted:
        raise HTTPException(status_code=404, detail="Assignment not found")
    return Message(message="Assignment removed successfully")


# ---------------------------------------------------------------------------
# Identity Summary
# ---------------------------------------------------------------------------


@router.get("/summary/", response_model=list[IdentityAgentBindingPublic])
def get_identity_summary(
    session: SessionDep,
    current_user: CurrentUser,
) -> Any:
    """Full identity summary for the current user (all bindings with assignments)."""
    return IdentityService.list_bindings(
        db_session=session,
        owner_id=current_user.id,
    )
