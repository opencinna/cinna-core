from collections.abc import Generator
from datetime import UTC, datetime
from typing import Annotated, Any
import logging
import uuid

import jwt
from fastapi import Depends, HTTPException, WebSocket, status
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


# ── CLI token context ──────────────────────────────────────────────────


class CLIContext(SQLModel):
    """
    Context object for CLI-authenticated routes.

    Returned by ``get_cli_context`` when the Bearer token is a valid CLI JWT.
    The CLI token is scoped to one agent and one user.

    Uses ``Any`` for agent/environment/cli_token to avoid circular imports
    with models that depend on deps indirectly.
    """
    user: User
    agent: Any  # Agent
    environment: Any | None  # AgentEnvironment | None
    cli_token: Any  # CLIToken


def _ensure_utc(dt: datetime) -> datetime:
    """Ensure a datetime is timezone-aware (UTC). Handles naive datetimes from DB."""
    if dt.tzinfo is None:
        from datetime import timezone
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _resolve_cli_context(db: Session, raw_token: str) -> CLIContext:
    """
    Shared CLI JWT → CLIContext resolution used by both the HTTP and
    WebSocket deps.

    Decodes and validates the token, loads the agent/user/environment,
    bumps the rolling expiry, and returns the context. Raises
    ``CLIAuthError`` on any failure so each dep can translate to its
    own error channel (HTTPException vs WS close code).
    """
    from sqlmodel import select

    from app.models import Agent, AgentEnvironment
    from app.models.cli.cli_token import CLIToken
    from app.services.cli.cli_auth import CLIAuthError, CLIAuthService

    # Decode JWT
    try:
        payload = CLIAuthService.decode_cli_jwt(raw_token)
    except ValueError as e:
        raise CLIAuthError("invalid_token", str(e))

    try:
        token_id = uuid.UUID(payload["sub"])
    except (KeyError, ValueError):
        raise CLIAuthError("invalid_token", "Invalid CLI token payload")

    cli_token = db.get(CLIToken, token_id)
    if not cli_token:
        raise CLIAuthError("not_found", "CLI token not found")
    if cli_token.is_revoked:
        raise CLIAuthError("revoked", "CLI token has been revoked")
    if _ensure_utc(cli_token.expires_at) < datetime.now(UTC):
        raise CLIAuthError("expired", "CLI token has expired")

    agent = db.get(Agent, cli_token.agent_id)
    if not agent:
        raise CLIAuthError("agent_missing", "Agent not found")
    if agent.owner_id != cli_token.owner_id:
        raise CLIAuthError("ownership_mismatch", "Token ownership mismatch")

    user = db.get(User, cli_token.owner_id)
    if not user or not user.is_active:
        raise CLIAuthError("user_inactive", "User not found or inactive")

    env_stmt = select(AgentEnvironment).where(
        AgentEnvironment.agent_id == agent.id,
        AgentEnvironment.is_active == True,  # noqa: E712
    )
    environment = db.exec(env_stmt).first()

    # Roll expiry + mark env activity so the suspension scheduler holds off
    CLIAuthService.refresh_token_usage(db, cli_token, environment)

    return CLIContext(
        user=user,
        agent=agent,
        environment=environment,
        cli_token=cli_token,
    )


def get_cli_context(token: TokenDep, db: SessionDep) -> CLIContext:
    """
    Validate a CLI JWT (HTTP) and return the CLI context.

    CLI-auth errors are surfaced as 401/403/404 HTTPExceptions depending
    on the failure reason.
    """
    from app.services.cli.cli_auth import CLIAuthError

    try:
        return _resolve_cli_context(db, token)
    except CLIAuthError as e:
        if e.reason == "agent_missing":
            code = status.HTTP_404_NOT_FOUND
        elif e.reason == "ownership_mismatch":
            code = status.HTTP_403_FORBIDDEN
        else:
            code = status.HTTP_401_UNAUTHORIZED
        raise HTTPException(status_code=code, detail=e.message)


CLIContextDep = Annotated[CLIContext, Depends(get_cli_context)]


async def get_cli_context_ws(websocket: WebSocket, db: SessionDep) -> CLIContext:
    """
    WebSocket variant of ``get_cli_context``.

    Extracts the CLI JWT from the WS handshake (Authorization header or
    ``?token=`` query param). On auth failure closes the WebSocket with
    code 1008 and raises ``WebSocketDisconnect``.
    """
    from starlette.websockets import WebSocketDisconnect, WebSocketState

    from app.services.cli.cli_auth import CLIAuthError, CLIAuthService

    async def _close_and_raise(reason: str) -> None:
        try:
            if websocket.client_state != WebSocketState.DISCONNECTED:
                await websocket.close(code=1008)
        except Exception:
            pass
        raise WebSocketDisconnect(code=1008, reason=reason)

    try:
        raw_token = CLIAuthService.decode_cli_jwt_from_websocket(websocket)
    except ValueError as e:
        await _close_and_raise(str(e))

    try:
        return _resolve_cli_context(db, raw_token)
    except CLIAuthError as e:
        await _close_and_raise(e.message)


CLIContextWSDep = Annotated[CLIContext, Depends(get_cli_context_ws)]


# ── External client attribution ────────────────────────────────────────


def get_current_client_claims(token: TokenDep) -> tuple[str | None, str | None]:
    """Extract ``(client_kind, external_client_id)`` from the bearer JWT.

    Desktop-issued access tokens include these extra claims.  Ordinary web-session
    JWTs will not carry them, so both values default to ``None`` gracefully.

    This dependency piggybacks on the same token that ``CurrentUser`` has already
    validated — it does a second decode only to read the extra claims, and treats
    any decode failure as a non-fatal absence of those claims.

    Returns:
        ``(client_kind, external_client_id)`` — either may be ``None``.
    """
    payload = security.decode_token_claims(token)
    if payload is None:
        return None, None
    client_kind = payload.get("client_kind") or None
    external_client_id = payload.get("external_client_id") or None
    return client_kind, external_client_id


CurrentClientClaims = Annotated[
    tuple[str | None, str | None], Depends(get_current_client_claims)
]
