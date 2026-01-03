import uuid
from typing import Any

from fastapi import APIRouter, HTTPException
from sqlmodel import func, select

from app.api.deps import CurrentUser, SessionDep
from app.models import (
    UserWorkspace,
    UserWorkspaceCreate,
    UserWorkspaceUpdate,
    UserWorkspacePublic,
    UserWorkspacesPublic,
    Message,
)
from app.services.user_workspace_service import UserWorkspaceService

router = APIRouter(prefix="/user-workspaces", tags=["user-workspaces"])


@router.get("/", response_model=UserWorkspacesPublic)
def read_workspaces(
    session: SessionDep, current_user: CurrentUser, skip: int = 0, limit: int = 100
) -> Any:
    """
    Retrieve user workspaces.
    """
    workspaces = UserWorkspaceService.get_user_workspaces(
        session=session, user_id=current_user.id, skip=skip, limit=limit
    )
    count = UserWorkspaceService.count_user_workspaces(
        session=session, user_id=current_user.id
    )

    return UserWorkspacesPublic(data=workspaces, count=count)


@router.get("/{workspace_id}", response_model=UserWorkspacePublic)
def read_workspace(
    workspace_id: uuid.UUID, session: SessionDep, current_user: CurrentUser
) -> Any:
    """
    Get workspace by ID.
    """
    workspace = UserWorkspaceService.get_workspace(
        session=session, workspace_id=workspace_id
    )
    if not workspace:
        raise HTTPException(status_code=404, detail="Workspace not found")
    if workspace.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not enough permissions")
    return workspace


@router.post("/", response_model=UserWorkspacePublic)
def create_workspace(
    *, session: SessionDep, current_user: CurrentUser, workspace_in: UserWorkspaceCreate
) -> Any:
    """
    Create new workspace.
    """
    workspace = UserWorkspaceService.create_workspace(
        session=session, user_id=current_user.id, data=workspace_in
    )
    return workspace


@router.put("/{workspace_id}", response_model=UserWorkspacePublic)
def update_workspace(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    workspace_id: uuid.UUID,
    workspace_in: UserWorkspaceUpdate,
) -> Any:
    """
    Update workspace.
    """
    workspace = UserWorkspaceService.get_workspace(
        session=session, workspace_id=workspace_id
    )
    if not workspace:
        raise HTTPException(status_code=404, detail="Workspace not found")
    if workspace.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not enough permissions")

    workspace = UserWorkspaceService.update_workspace(
        session=session, workspace_id=workspace_id, data=workspace_in
    )
    return workspace


@router.delete("/{workspace_id}")
def delete_workspace(
    workspace_id: uuid.UUID, session: SessionDep, current_user: CurrentUser
) -> Message:
    """
    Delete workspace.
    """
    workspace = UserWorkspaceService.get_workspace(
        session=session, workspace_id=workspace_id
    )
    if not workspace:
        raise HTTPException(status_code=404, detail="Workspace not found")
    if workspace.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not enough permissions")

    success = UserWorkspaceService.delete_workspace(
        session=session, workspace_id=workspace_id
    )
    if not success:
        raise HTTPException(status_code=500, detail="Failed to delete workspace")

    return Message(message="Workspace deleted successfully")
