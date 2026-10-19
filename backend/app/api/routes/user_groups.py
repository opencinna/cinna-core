"""User group search for share pickers.

Open to **every** authenticated user, with no role gate (D2: never gate a
credential exchange on role). The response carries name and member count only,
never who the members are. Group management lives on ``/admin/user-groups``.
"""
from typing import Any

from fastapi import APIRouter

from app.api.deps import CurrentUser, SessionDep
from app.models import UserGroupsSearchPublic
from app.services.users.user_group_service import UserGroupService

router = APIRouter(prefix="/user-groups", tags=["user-groups"])


@router.get("/search", response_model=UserGroupsSearchPublic)
def search_user_groups(
    session: SessionDep,
    current_user: CurrentUser,  # noqa: ARG001 — authentication only
    q: str | None = None,
    limit: int = 10,
) -> Any:
    """Case-insensitive name search; ``limit`` is clamped to 1–25 and an
    empty ``q`` returns the first groups by name."""
    return UserGroupService.search(session, q=q, limit=limit)
