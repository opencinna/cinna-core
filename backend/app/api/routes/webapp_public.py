"""
Public Webapp Serving Routes.

Token-authenticated routes that serve webapp content from agent environments.
These endpoints use the share token for auth (not the platform JWT).
"""
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response, HTMLResponse

from app.api.deps import SessionDep
from app.models import Agent, AgentWebappShare
from app.services.agent_webapp_share_service import AgentWebappShareService
from app.services.environment_service import EnvironmentService
from app.services.webapp_service import WebappService, WEBAPP_SIZE_LIMIT_BYTES
from app.api.routes.webapp_templates import ERROR_HTML, LOADING_HTML

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webapp", tags=["webapp-public"])


def _resolve_share_and_env(
    session,
    token: str,
) -> tuple[AgentWebappShare, Agent, "AgentEnvironment | None"]:
    """Validate share token and resolve agent + environment."""
    from app.models import AgentEnvironment

    share = AgentWebappShareService.validate_token(session, token)
    if not share:
        existing = AgentWebappShareService._find_share_by_token(session, token)
        if existing:
            raise HTTPException(status_code=410, detail="Webapp share link has expired or been deactivated")
        raise HTTPException(status_code=404, detail="Webapp share not found")

    agent = session.get(Agent, share.agent_id)
    if not agent or not agent.webapp_enabled:
        raise HTTPException(status_code=404, detail="Webapp not available")

    environment = None
    if agent.active_environment_id:
        environment = session.get(AgentEnvironment, agent.active_environment_id)

    return share, agent, environment


@router.get("/{token}/_status")
async def webapp_status(
    token: str,
    session: SessionDep,
):
    """Environment status endpoint for loading page polling."""
    share, agent, environment = _resolve_share_and_env(session, token)
    return await WebappService.get_public_status(session, agent, environment)


@router.get("/{token}/api/{endpoint}")
async def webapp_public_data_api_get(token: str, endpoint: str):
    """Redirect GET data API requests — data API only supports POST."""
    raise HTTPException(status_code=405, detail="Data API only supports POST requests")


@router.post("/{token}/api/{endpoint}")
async def webapp_public_data_api(
    token: str,
    endpoint: str,
    session: SessionDep,
    request: Request,
):
    """Execute a data script endpoint via share token."""
    # Strip .py extension if caller included it in the URL
    if endpoint.endswith(".py"):
        endpoint = endpoint[:-3]
    share, agent, environment = _resolve_share_and_env(session, token)

    if not share.allow_data_api:
        raise HTTPException(status_code=403, detail="Data API access is disabled for this share")

    if not environment or environment.status != "running":
        raise HTTPException(status_code=503, detail="Environment is not running")

    WebappService.update_last_activity(session, environment)

    lifecycle = EnvironmentService.get_lifecycle_manager()
    adapter = lifecycle.get_adapter(environment)

    try:
        body = await request.json()
    except Exception:
        body = {}

    params = body.get("params", {})
    timeout = min(body.get("timeout", 60), 300)

    try:
        status_code, content = await adapter.call_webapp_api(endpoint, params, timeout)
        return Response(
            content=content,
            status_code=status_code,
            media_type="application/json",
            headers={
                "Cache-Control": "no-store",
                "Content-Security-Policy": "frame-ancestors *",
            },
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{token}/{path:path}")
async def serve_webapp_public(
    token: str,
    path: str,
    session: SessionDep,
    request: Request,
):
    """Serve a static file from the agent's webapp via share token."""
    share, agent, environment = _resolve_share_and_env(session, token)

    if not path or path == "":
        path = "index.html"

    if not environment or environment.status != "running":
        # Return loading page if env is not running
        if path == "index.html" or path == "":
            return HTMLResponse(
                content=LOADING_HTML.format(token=token),
                headers={
                    "Content-Security-Policy": "frame-ancestors *",
                },
            )
        raise HTTPException(status_code=503, detail="Environment is not running")

    WebappService.update_last_activity(session, environment)

    lifecycle = EnvironmentService.get_lifecycle_manager()
    adapter = lifecycle.get_adapter(environment)

    # Check webapp status on index.html requests (size limit + existence)
    if path == "index.html":
        try:
            webapp_status = await adapter.get_webapp_status()
            if not webapp_status.get("has_index"):
                return HTMLResponse(
                    content=ERROR_HTML.format(
                        title="Web App Not Built Yet",
                        message="The agent hasn't created a web app yet. Ask the agent to build a dashboard, then try again.",
                    ),
                    status_code=404,
                    headers={"Content-Security-Policy": "frame-ancestors *"},
                )
            if webapp_status.get("total_size_bytes", 0) > WEBAPP_SIZE_LIMIT_BYTES:
                return HTMLResponse(
                    content=ERROR_HTML.format(
                        title="Web App Too Large",
                        message=f"The web app exceeds the {WEBAPP_SIZE_LIMIT_BYTES // (1024*1024)}MB size limit. Contact the owner to reduce its size.",
                    ),
                    status_code=413,
                    headers={"Content-Security-Policy": "frame-ancestors *"},
                )
        except Exception:
            pass

    req_headers = {}
    for h in ("if-modified-since", "if-none-match"):
        val = request.headers.get(h)
        if val:
            req_headers[h] = val

    try:
        status_code, resp_headers, body = await adapter.get_webapp_file(path, req_headers)

        # Return friendly HTML error pages for failed requests rendered in iframe
        if status_code == 404 and path == "index.html":
            return HTMLResponse(
                content=ERROR_HTML.format(
                    title="Web App Not Built Yet",
                    message="The agent hasn't created a web app yet. Ask the agent to build a dashboard, then try again.",
                ),
                status_code=404,
                headers={"Content-Security-Policy": "frame-ancestors *"},
            )

        # Add iframe-friendly headers
        resp_headers["Content-Security-Policy"] = "frame-ancestors *"
        return Response(
            content=body,
            status_code=status_code,
            headers=resp_headers,
        )
    except Exception as e:
        if path == "index.html":
            return HTMLResponse(
                content=ERROR_HTML.format(
                    title="Web App Unavailable",
                    message="Something went wrong while loading the web app. Please try again.",
                ),
                status_code=500,
                headers={"Content-Security-Policy": "frame-ancestors *"},
            )
        raise HTTPException(status_code=500, detail=str(e))
