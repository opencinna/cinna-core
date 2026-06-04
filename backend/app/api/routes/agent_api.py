"""
Agent REST API — owner-preview routes.

Phase 1 of the agent_api feature: owner-only routes that let the producer
preview the API they are building. There are no tokens and no consumer routes
yet (Phase 2).

Prefix: ``/api/v1/agents/{agent_id}/agent-api``

| Method | Path             | Purpose                                                   |
|--------|------------------|-----------------------------------------------------------|
| GET    | /_status         | Build/run status. Does NOT require ``agent_api_enabled``. |
| GET    | /openapi.json    | Harvested OpenAPI spec (from cache or import-only harvest).|
| ANY    | /proxy/{path}    | Full HTTP passthrough for owner testing. Requires a running env. |

All routes require the authenticated owner (or a superuser).
"""
import logging
import uuid

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from app.api.deps import CurrentUser, SessionDep
from app.models import (
    AgentApiProducerConnections,
    AgentEnvironment,
    ConnectAgentApiRequest,
    ConnectAgentApiResponse,
    Message,
)
from app.services.agent_api.agent_api_service import (
    AgentApiError,
    AgentApiService,
)
from app.services.agent_api.agent_api_token_service import (
    AgentApiTokenError,
    AgentApiTokenService,
)
from app.services.environments.environment_service import EnvironmentService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agents/{agent_id}/agent-api", tags=["agent-api"])


def _handle_agent_api_error(e: AgentApiError) -> None:
    """Convert agent-api service exceptions to HTTP exceptions."""
    raise HTTPException(status_code=e.status_code, detail=e.message)


def _handle_token_error(e: AgentApiTokenError) -> None:
    """Convert connect-helper service exceptions to HTTP exceptions."""
    raise HTTPException(status_code=e.status_code, detail=e.message)


@router.get("/_status")
async def get_agent_api_status(
    agent_id: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
):
    """
    Build/run status for the producer API.

    Works regardless of ``agent_api_enabled`` (reports ``disabled``) and never
    spawns the serving child. Resolves the agent (ownership-checked) without
    requiring a running env so it can report ``not_running`` cleanly.
    """
    try:
        agent = AgentApiService.resolve_agent_only(
            session, agent_id, current_user.id, is_superuser=current_user.is_superuser
        )
    except AgentApiError as e:
        _handle_agent_api_error(e)

    environment: AgentEnvironment | None = None
    if agent.active_environment_id:
        environment = session.get(AgentEnvironment, agent.active_environment_id)

    return await AgentApiService.get_status(session, agent, environment)


@router.post("/_refresh")
async def refresh_agent_api_status(
    agent_id: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
):
    """
    Force a fresh import-only re-harvest (spec + policy) and return the status.

    The cached spec/policy and any boot error (env-core in-memory + the env row)
    otherwise only refresh on the next *automatic* re-harvest (a producer file
    edit), so a transient harvest failure can stick on screen indefinitely and a
    policy.yaml edit won't take effect until the next reload. This lets the owner
    force it on demand: a successful re-harvest refreshes the spec, re-parses the
    policy, and clears the error; a failed one re-records it. The status is
    returned either way (never raises on a harvest failure — the returned
    ``last_error`` reflects the outcome).
    """
    try:
        agent = AgentApiService.resolve_agent_only(
            session, agent_id, current_user.id, is_superuser=current_user.is_superuser
        )
    except AgentApiError as e:
        _handle_agent_api_error(e)

    environment: AgentEnvironment | None = None
    if agent.active_environment_id:
        environment = session.get(AgentEnvironment, agent.active_environment_id)

    # Best-effort re-harvest. Only meaningful when enabled + env running; the
    # error (if any) is persisted by get_spec, and the status below reflects it.
    # Re-parse the policy.yaml alongside the spec so a policy edit is picked up
    # on demand (mirrors the background refresh_spec_cache).
    if agent.agent_api_enabled and environment is not None and environment.status == "running":
        try:
            await AgentApiService.get_spec(session, environment, force_refresh=True)
        except AgentApiError:
            pass  # persisted; surfaced via the status payload
        try:
            await AgentApiService.load_policy(session, environment, force_refresh=True)
        except Exception:  # best-effort; status still returns (matches refresh_spec_cache)
            logger.debug(
                "agent_api _refresh policy reload failed for env %s", environment.id
            )

    return await AgentApiService.get_status(session, agent, environment)


@router.get("/openapi.json")
async def get_agent_api_spec(
    agent_id: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
):
    """
    Return the harvested OpenAPI spec (owner preview).

    Served from the env cache when present; otherwise harvested import-only via
    env-core (no serving child spawned). Requires ``agent_api_enabled``.
    """
    try:
        agent, environment = AgentApiService.resolve_producer_environment(
            session, agent_id, current_user.id, is_superuser=current_user.is_superuser
        )
        spec = await AgentApiService.get_spec(session, environment)
    except AgentApiError as e:
        _handle_agent_api_error(e)

    # Keep the env alive while the owner inspects the spec.
    AgentApiService.update_last_activity(session, environment)
    return JSONResponse(content=spec)


@router.api_route(
    "/proxy/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
    # Excluded from the OpenAPI schema: this is a raw multi-verb HTTP passthrough,
    # not a typed endpoint the generated frontend client should call. Including it
    # produces a duplicate operationId per HTTP method. The frontend hits this via
    # a plain fetch with the sub-path, not the typed SDK.
    include_in_schema=False,
)
async def proxy_agent_api(
    agent_id: uuid.UUID,
    path: str,
    request: Request,
    session: SessionDep,
    current_user: CurrentUser,
):
    """
    Full HTTP passthrough to the producer's API child (owner testing).

    Requires a running producer env. env-core lazily spawns the serving child
    on first call; if it cannot become healthy within env-core's budget it
    returns ``503 + Retry-After``, which we pass straight through so the caller
    can retry. Supports multipart request bodies and streaming responses.

    NOTE: Phase 1 has no proxy-edge policy enforcement (that is Phase 2). This
    route is owner-only and exists for previewing.
    """
    try:
        agent, environment = AgentApiService.resolve_producer_environment(
            session, agent_id, current_user.id, is_superuser=current_user.is_superuser
        )
    except AgentApiError as e:
        _handle_agent_api_error(e)

    AgentApiService.update_last_activity(session, environment)

    body = await request.body()
    fwd_headers = dict(request.headers)

    lifecycle = EnvironmentService.get_lifecycle_manager()
    adapter = lifecycle.get_adapter(environment)

    try:
        status_code, resp_headers, stream = await adapter.proxy_agent_api(
            method=request.method,
            path=path,
            headers=fwd_headers,
            body=body if body else None,
            stream=True,
        )
    except Exception as e:
        logger.error("agent_api owner proxy failed for agent %s path %s: %s", agent_id, path, e)
        raise HTTPException(status_code=502, detail=f"Agent API proxy error: {e}")

    return StreamingResponse(
        stream,
        status_code=status_code,
        headers=resp_headers,
    )


# ── Connect helper (owner) ───────────────────────────────────────────────────
# Prefix: /api/v1/agents/{agent_id}/agent-api/connect


@router.post("/connect", response_model=ConnectAgentApiResponse)
async def connect_agent_api(
    agent_id: uuid.UUID,
    body: ConnectAgentApiRequest,
    session: SessionDep,
    current_user: CurrentUser,
):
    """
    One-click "Connect to another agent" helper (plan §6.3).

    Mints an ``agent_api`` token on the producer ``agent_id``, creates an
    ``agent_api`` credential pre-filled with the connection info, and optionally
    links it to a consumer agent. The caller must own the producer agent.
    """
    try:
        return await AgentApiTokenService.connect_agent_api(
            session, agent_id, current_user.id, body,
            is_superuser=current_user.is_superuser,
        )
    except AgentApiTokenError as e:
        _handle_token_error(e)


@router.get("/connections", response_model=AgentApiProducerConnections)
def list_agent_api_connections(
    agent_id: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
):
    """
    List the agents consuming this producer agent's API — one entry per
    ``agent_api`` connection (credential) plus the consumer agents it is linked
    to. Surfaced on the producer's "Agent REST API" card.
    """
    try:
        connections = AgentApiTokenService.list_producer_connections(
            session, agent_id, current_user.id,
            is_superuser=current_user.is_superuser,
        )
        return AgentApiProducerConnections(data=connections, count=len(connections))
    except AgentApiTokenError as e:
        _handle_token_error(e)


@router.delete("/connections/{token_id}")
async def delete_agent_api_connection(
    agent_id: uuid.UUID,
    token_id: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
) -> Message:
    """
    Disconnect a consumer from this producer's API: deletes the connection
    credential (cascade-deletes its token) or an orphaned token directly.
    """
    try:
        await AgentApiTokenService.delete_producer_connection(
            session, agent_id, token_id, current_user.id,
            is_superuser=current_user.is_superuser,
        )
        return Message(message="Disconnected")
    except AgentApiTokenError as e:
        _handle_token_error(e)
