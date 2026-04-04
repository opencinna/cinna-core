"""
Agent Webapp Routes.

Owner preview routes for serving webapp content from agent environments.
"""
import uuid
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import Response
from fastapi.security import OAuth2PasswordBearer
from pydantic import BaseModel

import jwt as pyjwt
from jwt.exceptions import InvalidTokenError
from pydantic import ValidationError

from app.api.deps import CurrentUser, SessionDep
from app.core import security
from app.core.config import settings
from app.models import Agent, AgentEnvironment, TokenPayload, User
from app.services.environments.environment_service import EnvironmentService
from app.services.webapp.webapp_service import (
    WebappService,
    WebappError,
    WEBAPP_SIZE_LIMIT_BYTES,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agents/{agent_id}/webapp", tags=["webapp"])

_optional_oauth2 = OAuth2PasswordBearer(
    tokenUrl=f"{settings.API_V1_STR}/login/access-token", auto_error=False
)


def _resolve_user_from_token(session, raw_token: str) -> User:
    """Validate a JWT string and return the User, or raise 403."""
    try:
        payload = pyjwt.decode(
            raw_token, settings.SECRET_KEY, algorithms=[security.ALGORITHM]
        )
        token_data = TokenPayload(**payload)
    except (InvalidTokenError, ValidationError):
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


def _handle_webapp_error(e: WebappError) -> None:
    """Convert webapp service exceptions to HTTP exceptions."""
    raise HTTPException(status_code=e.status_code, detail=e.message)


@router.get("/status")
async def get_webapp_status(
    agent_id: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
):
    """Get webapp metadata: exists, size, file count, entry point, api endpoints."""
    try:
        agent, environment = WebappService.resolve_agent_environment(
            session, agent_id, current_user.id,
            is_superuser=current_user.is_superuser,
            require_webapp_enabled=False,
        )
    except WebappError as e:
        _handle_webapp_error(e)

    lifecycle = EnvironmentService.get_lifecycle_manager()
    adapter = lifecycle.get_adapter(environment)

    try:
        status = await adapter.get_webapp_status()
        status["webapp_enabled"] = agent.webapp_enabled
        status["size_limit_bytes"] = WEBAPP_SIZE_LIMIT_BYTES
        status["size_limit_exceeded"] = status.get("total_size_bytes", 0) > WEBAPP_SIZE_LIMIT_BYTES
        return status
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class WebappDataApiRequest(BaseModel):
    params: dict = {}
    timeout: int = 60


@router.post("/api/{endpoint}")
async def webapp_data_api(
    agent_id: uuid.UUID,
    endpoint: str,
    session: SessionDep,
    request: Request,
    bearer_token: str | None = Depends(_optional_oauth2),
    body: WebappDataApiRequest | None = None,
):
    """Execute a data script endpoint in the webapp."""
    # Resolve user from bearer token or cookie (for iframe embedding)
    raw_token = bearer_token or request.cookies.get("webapp_owner_token")
    if not raw_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )
    current_user = _resolve_user_from_token(session, raw_token)

    # Strip .py extension if caller included it in the URL
    if endpoint.endswith(".py"):
        endpoint = endpoint[:-3]
    try:
        agent, environment = WebappService.resolve_agent_environment(
            session, agent_id, current_user.id,
            is_superuser=current_user.is_superuser,
        )
    except WebappError as e:
        _handle_webapp_error(e)
    WebappService.update_last_activity(session, environment)

    lifecycle = EnvironmentService.get_lifecycle_manager()
    adapter = lifecycle.get_adapter(environment)

    params = body.params if body else {}
    timeout = min(body.timeout if body else 60, 300)

    try:
        status_code, content = await adapter.call_webapp_api(endpoint, params, timeout)
        return Response(
            content=content,
            status_code=status_code,
            media_type="application/json",
            headers={"Cache-Control": "no-store"},
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/owner-status")
async def get_webapp_owner_status(
    agent_id: uuid.UUID,
    session: SessionDep,
    request: Request,
    bearer_token: str | None = Depends(_optional_oauth2),
    token: str | None = Query(None),
):
    """Environment status for the owner's dashboard iframe — triggers auto-activation if suspended."""
    # Resolve user from bearer token, query param, or cookie (same pattern as serve_webapp_file).
    raw_token = bearer_token or token or request.cookies.get("webapp_owner_token")
    if not raw_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )
    current_user = _resolve_user_from_token(session, raw_token)

    # Resolve agent without requiring the environment to be running.
    agent = session.get(Agent, agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if agent.owner_id != current_user.id and not current_user.is_superuser:
        raise HTTPException(status_code=403, detail="Not enough permissions")

    environment: AgentEnvironment | None = None
    if agent.active_environment_id:
        environment = session.get(AgentEnvironment, agent.active_environment_id)

    return await WebappService.get_public_status(session, agent, environment)


@router.get("/{path:path}")
async def serve_webapp_file(
    agent_id: uuid.UUID,
    path: str,
    session: SessionDep,
    request: Request,
    bearer_token: str | None = Depends(_optional_oauth2),
    token: str | None = Query(None),
):
    """Serve a static file from the agent's webapp directory."""
    # Resolve user from bearer token, query param, or cookie (for iframe embedding).
    # Priority: Authorization header > ?token= query param > webapp_owner_token cookie.
    raw_token = bearer_token or token or request.cookies.get("webapp_owner_token")
    if not raw_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )
    current_user = _resolve_user_from_token(session, raw_token)

    try:
        agent, environment = WebappService.resolve_agent_environment(
            session, agent_id, current_user.id,
            is_superuser=current_user.is_superuser,
        )
    except WebappError as e:
        _handle_webapp_error(e)
    WebappService.update_last_activity(session, environment)

    lifecycle = EnvironmentService.get_lifecycle_manager()
    adapter = lifecycle.get_adapter(environment)

    # Check webapp status on index.html requests (existence + size limit)
    if not path or path == "":
        path = "index.html"
    if path == "index.html":
        try:
            webapp_status = await adapter.get_webapp_status()
            if not webapp_status.get("has_index"):
                raise HTTPException(
                    status_code=404,
                    detail="Web app not built yet. No index.html found in the webapp directory. Ask the agent to build a dashboard first."
                )
            if webapp_status.get("total_size_bytes", 0) > WEBAPP_SIZE_LIMIT_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail=f"Webapp exceeds size limit ({WEBAPP_SIZE_LIMIT_BYTES // (1024*1024)}MB). Reduce webapp size to continue serving."
                )
        except HTTPException:
            raise
        except Exception:
            pass  # Don't block serving if status check fails

    # Pass through caching headers
    req_headers = {}
    for h in ("if-modified-since", "if-none-match"):
        val = request.headers.get(h)
        if val:
            req_headers[h] = val

    # Set a cookie so sub-resource requests (JS, CSS, images) from the iframe
    # are authenticated without needing the ?token= query param on every URL.
    set_cookie = token and request.cookies.get("webapp_owner_token") != raw_token

    try:
        status_code, resp_headers, body = await adapter.get_webapp_file(path, req_headers)
        if status_code == 404:
            raise HTTPException(status_code=404, detail=f"File not found: {path}")
        response = Response(
            content=body,
            status_code=status_code,
            headers=resp_headers,
        )
        if set_cookie:
            response.set_cookie(
                key="webapp_owner_token",
                value=raw_token,
                httponly=True,
                samesite="strict",
                secure=request.url.scheme == "https",
                path=f"/api/v1/agents/{agent_id}/webapp",
                max_age=8 * 24 * 3600,  # Match JWT expiry
            )
        return response
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
