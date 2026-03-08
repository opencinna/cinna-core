from collections.abc import Generator
from typing import Annotated
import logging
import uuid

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jwt.exceptions import InvalidTokenError
from pydantic import ValidationError
from sqlmodel import Session, SQLModel

from app.core import security
from app.core.config import settings
from app.core.db import engine
from app.models import TokenPayload, User

logger = logging.getLogger(__name__)

reusable_oauth2 = OAuth2PasswordBearer(
    tokenUrl=f"{settings.API_V1_STR}/login/access-token"
)


def get_db() -> Generator[Session, None, None]:
    with Session(engine) as session:
        yield session


SessionDep = Annotated[Session, Depends(get_db)]
TokenDep = Annotated[str, Depends(reusable_oauth2)]


def get_current_user(session: SessionDep, token: TokenDep) -> User:
    try:
        logger.debug(f"Attempting to decode token: {token[:20]}...")
        payload = jwt.decode(
            token, settings.SECRET_KEY, algorithms=[security.ALGORITHM]
        )
        logger.debug(f"Token payload: {payload}")
        token_data = TokenPayload(**payload)
    except (InvalidTokenError, ValidationError) as e:
        logger.error(f"Token validation failed: {type(e).__name__}: {str(e)}")
        logger.error(f"Token received: {token[:50]}...")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Could not validate credentials",
        )
    user = session.get(User, token_data.sub)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if not user.is_active:
        raise HTTPException(status_code=400, detail="Inactive user")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def get_current_active_superuser(current_user: CurrentUser) -> User:
    if not current_user.is_superuser:
        raise HTTPException(
            status_code=403, detail="The user doesn't have enough privileges"
        )
    return current_user


# ── Guest share context ─────────────────────────────────────────────────


class GuestShareContext(SQLModel):
    """
    Context object for guest share access.

    Returned by ``get_current_user_or_guest`` when the JWT token
    has ``role == "chat-guest"`` (anonymous guest access).
    """
    guest_share_id: uuid.UUID
    agent_id: uuid.UUID
    owner_id: uuid.UUID
    is_anonymous: bool  # True if JWT role=chat-guest, False if grant-based
    user_id: uuid.UUID | None = None  # Set for grant-based access, None for anonymous


def get_current_user_or_guest(
    session: SessionDep, token: TokenDep
) -> User | GuestShareContext:
    """
    Resolve the current caller as either a User or a GuestShareContext.

    - If the JWT has ``role == "chat-guest"`` and ``token_type == "guest_share"``,
      the caller is an anonymous guest. The JWT claims are validated and
      returned as a ``GuestShareContext``.
    - Otherwise, the token is treated as a regular user JWT and resolved
      via ``get_current_user`` logic.
    """
    try:
        payload = jwt.decode(
            token, settings.SECRET_KEY, algorithms=[security.ALGORITHM]
        )
    except InvalidTokenError:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Could not validate credentials",
        )

    role = payload.get("role")
    token_type = payload.get("token_type")

    if role == "chat-guest" and token_type == "guest_share":
        # Guest share JWT — build GuestShareContext from claims
        try:
            return GuestShareContext(
                guest_share_id=uuid.UUID(payload["sub"]),
                agent_id=uuid.UUID(payload["agent_id"]),
                owner_id=uuid.UUID(payload["owner_id"]),
                is_anonymous=True,
                user_id=None,
            )
        except (KeyError, ValueError) as e:
            logger.error(f"Invalid guest share JWT claims: {e}")
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Could not validate credentials",
            )

    # Regular user JWT — delegate to standard user resolution
    try:
        token_data = TokenPayload(**payload)
    except ValidationError:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Could not validate credentials",
        )

    user = session.get(User, token_data.sub)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if not user.is_active:
        raise HTTPException(status_code=400, detail="Inactive user")
    return user


CurrentUserOrGuest = Annotated[
    User | GuestShareContext, Depends(get_current_user_or_guest)
]


# ── Webapp chat context ────────────────────────────────────────────────


class WebappChatContext(SQLModel):
    """
    Context object for webapp chat access.

    Returned by ``get_webapp_chat_user`` when the JWT token
    has ``role == "webapp-viewer"`` (webapp share access).
    """
    webapp_share_id: uuid.UUID
    agent_id: uuid.UUID
    owner_id: uuid.UUID


def get_webapp_chat_user(
    session: SessionDep, token: str = Depends(OAuth2PasswordBearer(tokenUrl="token", auto_error=False)),
) -> WebappChatContext:
    """
    Resolve the current caller as a WebappChatContext from a webapp-viewer JWT.
    """
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )

    try:
        payload = jwt.decode(
            token, settings.SECRET_KEY, algorithms=[security.ALGORITHM]
        )
    except InvalidTokenError:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Could not validate credentials",
        )

    role = payload.get("role")
    token_type = payload.get("token_type")

    if role != "webapp-viewer" or token_type != "webapp_share":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid token type for webapp chat",
        )

    try:
        return WebappChatContext(
            webapp_share_id=uuid.UUID(payload["sub"]),
            agent_id=uuid.UUID(payload["agent_id"]),
            owner_id=uuid.UUID(payload["owner_id"]),
        )
    except (KeyError, ValueError) as e:
        logger.error(f"Invalid webapp chat JWT claims: {e}")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Could not validate credentials",
        )


CurrentWebappChatUser = Annotated[WebappChatContext, Depends(get_webapp_chat_user)]
