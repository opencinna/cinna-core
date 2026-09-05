from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from app.api.deps import SessionDep
from app.api.routes._user_public import user_to_public
from app.core.security import get_password_hash
from app.models import (
    User,
    UserPublic,
)

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

    user = User(
        email=user_in.email,
        full_name=user_in.full_name,
        hashed_password=get_password_hash(user_in.password),
    )

    session.add(user)
    session.commit()

    # Through the one builder, never the raw row: ``UserPublic`` carries
    # derived fields (``can_change_email``, the enrolment flags) that exist
    # nowhere on ``User``, and ``can_change_email`` has no default, so
    # returning the ORM object is a 500 rather than a wrong answer.
    return user_to_public(session, user)
