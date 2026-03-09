"""
Agent Webapp Routes.

Owner preview routes for serving webapp content from agent environments.
"""
import uuid
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel

from app.api.deps import CurrentUser, SessionDep
from app.services.environment_service import EnvironmentService
from app.services.webapp_service import (
    WebappService,
    WebappError,
    WEBAPP_SIZE_LIMIT_BYTES,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agents/{agent_id}/webapp", tags=["webapp"])


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
    current_user: CurrentUser,
    body: WebappDataApiRequest | None = None,
):
    """Execute a data script endpoint in the webapp."""
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


@router.get("/{path:path}")
async def serve_webapp_file(
    agent_id: uuid.UUID,
    path: str,
    session: SessionDep,
    current_user: CurrentUser,
    request: Request,
):
    """Serve a static file from the agent's webapp directory."""
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
            status = await adapter.get_webapp_status()
            if not status.get("has_index"):
                raise HTTPException(
                    status_code=404,
                    detail="Web app not built yet. No index.html found in the webapp directory. Ask the agent to build a dashboard first."
                )
            if status.get("total_size_bytes", 0) > WEBAPP_SIZE_LIMIT_BYTES:
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

    try:
        status_code, resp_headers, body = await adapter.get_webapp_file(path, req_headers)
        if status_code == 404:
            raise HTTPException(status_code=404, detail=f"File not found: {path}")
        return Response(
            content=body,
            status_code=status_code,
            headers=resp_headers,
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
