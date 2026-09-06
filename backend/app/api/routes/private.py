from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.api.deps import SessionDep
from app.api.routes._user_public import user_to_public
from app.models import (
    AccountOrigin,
    UserPublic,
)
from app.services.users.user_service import UserService

router = APIRouter(tags=["private"], prefix="/private")


class PrivateUserCreate(BaseModel):
    email: str
    password: str
    full_name: str
    is_verified: bool = False


@router.post("/users/", response_model=UserPublic)
def create_user(user_in: PrivateUserCreate, session: SessionDep) -> Any:
    """
    Create a new user.
    """

    # Local-development helper, but still not a licence to build a ``User``
    # row by hand: it goes through the one chokepoint like every other
    # arrival path, so a dev-seeded account is indistinguishable from a real
    # one (policy-derived role, normalised address, auto-provisioned keys).
    # ``admin`` origin — this route is already superuser-equivalent by virtue
    # of only existing in a local environment.
    try:
        user = UserService.create_account(
            session,
            email=user_in.email,
            origin=AccountOrigin.ADMIN,
            password=user_in.password,
            full_name=user_in.full_name,
        )
    except ValueError as e:
        # ``email`` here is a plain ``str``, not ``EmailStr`` — the chokepoint
        # is the first thing that validates it, and it also rejects a
        # duplicate. Both are the caller's mistake, so answer 400 rather than
        # letting a dev-fixture script see a 500.
        raise HTTPException(status_code=400, detail=str(e))

    # Through the one builder, never the raw row: ``UserPublic`` carries
    # derived fields (``can_change_email``, the enrolment flags) that exist
    # nowhere on ``User``, and ``can_change_email`` has no default, so
    # returning the ORM object is a 500 rather than a wrong answer.
    return user_to_public(session, user)
