"""Admin API for **user groups** — superuser-defined sharing principals.

Only superusers create, rename and delete groups and manage their members (D2).
New grants to a group are made in each resource's own share dialog, never here
(D3). Every mutation writes a ``SecurityEvent`` keyed to the acting admin.

Membership is live: adding a member has no side effects; removing one or
deleting a group releases materialised access for the users who lost every
path (see :class:`~app.services.users.user_group_access.UserGroupAccess`).
"""
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.deps import SessionDep, get_current_active_superuser
from app.models import (
    Message,
    User,
    UserGroupCreate,
    UserGroupImpact,
    UserGroupMembersAdd,
    UserGroupMembersPublic,
    UserGroupPublic,
    UserGroupShareResourceType,
    UserGroupSharesPublic,
    UserGroupsPublic,
    UserGroupUpdate,
)
from app.services.users.user_group_service import (
    UserGroupConflictError,
    UserGroupError,
    UserGroupNotFoundError,
    UserGroupService,
    UserGroupValidationError,
)
from app.services.users.user_group_shares_service import UserGroupSharesService

router = APIRouter(
    prefix="/admin/user-groups",
    tags=["admin-user-groups"],
    dependencies=[Depends(get_current_active_superuser)],
)

SuperUser = Annotated[User, Depends(get_current_active_superuser)]


def _http_error(exc: UserGroupError) -> HTTPException:
    if isinstance(exc, UserGroupNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, UserGroupConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, UserGroupValidationError):
        return HTTPException(status_code=422, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


@router.get("/", response_model=UserGroupsPublic)
def list_user_groups(
    session: SessionDep,
    q: str | None = None,
    skip: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> Any:
    """List groups (case-insensitive name filter) with member and share counts."""
    return UserGroupService.list_groups(session, q=q, skip=skip, limit=limit)


@router.post("/", response_model=UserGroupPublic)
def create_user_group(
    session: SessionDep, admin: SuperUser, group_in: UserGroupCreate
) -> Any:
    """Create a group. 409 when the name is taken (case-insensitive)."""
    try:
        return UserGroupService.create(session, admin, group_in)
    except UserGroupError as exc:
        raise _http_error(exc) from exc


@router.get("/{group_id}", response_model=UserGroupPublic)
def get_user_group(session: SessionDep, group_id: uuid.UUID) -> Any:
    try:
        return UserGroupService.get(session, group_id)
    except UserGroupError as exc:
        raise _http_error(exc) from exc


@router.patch("/{group_id}", response_model=UserGroupPublic)
def update_user_group(
    session: SessionDep,
    admin: SuperUser,
    group_id: uuid.UUID,
    group_in: UserGroupUpdate,
) -> Any:
    """Rename a group or change its description."""
    try:
        return UserGroupService.update(session, admin, group_id, group_in)
    except UserGroupError as exc:
        raise _http_error(exc) from exc


@router.delete("/{group_id}", response_model=Message)
async def delete_user_group(
    session: SessionDep, admin: SuperUser, group_id: uuid.UUID
) -> Any:
    """Delete a group, its memberships and every share made to it.
    Preview the cost with ``GET /{group_id}/delete-impact``."""
    try:
        await UserGroupSharesService.delete_group(session, admin, group_id)
    except UserGroupError as exc:
        raise _http_error(exc) from exc
    return Message(message="User group deleted")


@router.get("/{group_id}/members", response_model=UserGroupMembersPublic)
def list_user_group_members(
    session: SessionDep,
    group_id: uuid.UUID,
    q: str | None = None,
    skip: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> Any:
    """List a group's members (filter on email / full name)."""
    try:
        return UserGroupService.list_members(
            session, group_id, q=q, skip=skip, limit=limit
        )
    except UserGroupError as exc:
        raise _http_error(exc) from exc


@router.post("/{group_id}/members", response_model=UserGroupMembersPublic)
def add_user_group_members(
    session: SessionDep,
    admin: SuperUser,
    group_id: uuid.UUID,
    body: UserGroupMembersAdd,
) -> Any:
    """Add active users. Idempotent; returns only the rows actually added.
    422 lists unknown or inactive user ids."""
    try:
        return UserGroupService.add_members(session, admin, group_id, body.user_ids)
    except UserGroupError as exc:
        raise _http_error(exc) from exc


@router.delete("/{group_id}/members/{user_id}", response_model=Message)
async def remove_user_group_member(
    session: SessionDep,
    admin: SuperUser,
    group_id: uuid.UUID,
    user_id: uuid.UUID,
) -> Any:
    """Remove one member; access through the group ends immediately."""
    try:
        await UserGroupService.remove_member(session, admin, group_id, user_id)
    except UserGroupError as exc:
        raise _http_error(exc) from exc
    return Message(message="Member removed")


@router.get(
    "/{group_id}/members/{user_id}/removal-impact", response_model=UserGroupImpact
)
def get_user_group_member_removal_impact(
    session: SessionDep, group_id: uuid.UUID, user_id: uuid.UUID
) -> Any:
    """What removing this member costs them: credentials they would lose
    access to, agent links removed, AI environments cleared and restarted."""
    try:
        return UserGroupSharesService.member_removal_impact(
            session, group_id, user_id
        )
    except UserGroupError as exc:
        raise _http_error(exc) from exc


@router.get("/{group_id}/delete-impact", response_model=UserGroupImpact)
def get_user_group_delete_impact(session: SessionDep, group_id: uuid.UUID) -> Any:
    """What deleting the group costs its members (read-only preview)."""
    try:
        return UserGroupSharesService.impact(session, group_id)
    except UserGroupError as exc:
        raise _http_error(exc) from exc


@router.get("/{group_id}/shares", response_model=UserGroupSharesPublic)
def list_user_group_shares(session: SessionDep, group_id: uuid.UUID) -> Any:
    """Every resource shared with the group, by type then name. Grants are
    made in each resource's own share dialog (D3), never here."""
    try:
        return UserGroupSharesService.list_shares(session, group_id)
    except UserGroupError as exc:
        raise _http_error(exc) from exc


@router.get(
    "/{group_id}/shares/{resource_type}/{resource_id}/revoke-impact",
    response_model=UserGroupImpact,
)
def get_user_group_share_revoke_impact(
    session: SessionDep,
    group_id: uuid.UUID,
    resource_type: UserGroupShareResourceType,
    resource_id: uuid.UUID,
) -> Any:
    """What revoking this one share costs the group's members (read-only)."""
    try:
        return UserGroupSharesService.share_revoke_impact(
            session, group_id, resource_type, resource_id
        )
    except UserGroupError as exc:
        raise _http_error(exc) from exc


@router.delete(
    "/{group_id}/shares/{resource_type}/{resource_id}", response_model=Message
)
async def revoke_user_group_share(
    session: SessionDep,
    admin: SuperUser,
    group_id: uuid.UUID,
    resource_type: UserGroupShareResourceType,
    resource_id: uuid.UUID,
) -> Any:
    """Revoke one share from the group, overriding the owner check. Members
    who lose every path lose credential links / AI environment pins."""
    try:
        await UserGroupSharesService.revoke(
            session, admin, group_id, resource_type, resource_id
        )
    except UserGroupError as exc:
        raise _http_error(exc) from exc
    return Message(message="Share revoked")
