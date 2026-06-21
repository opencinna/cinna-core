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
    AgentApiAccessGrantCreate,
    AgentApiAccessGrantPublic,
    AgentApiAccessGrantsPublic,
    AgentApiAccessGrantUpdate,
    AgentApiProducerConnections,
    AgentApiScopeCatalog,
    AgentEnvironment,
    ConnectAgentApiRequest,
    ConnectAgentApiResponse,
    Message,
)
from app.services.agent_api.agent_api_grant_service import AgentApiGrantService
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
    Wake a suspended env if needed, force a fresh import-only re-harvest
    (spec + policy), and return the status.

    The cached spec/policy and any boot error (env-core in-memory + the env row)
    otherwise only refresh on the next *automatic* re-harvest (a producer file
    edit), so a transient harvest failure can stick on screen indefinitely and a
    policy.yaml edit won't take effect until the next reload. This lets the owner
    force it on demand: a successful re-harvest refreshes the spec, re-parses the
    policy, and clears the error; a failed one re-records it.

    A re-harvest can only run against a *running* env, so when the producer's env
    is suspended / stopped (idle) this endpoint first kicks off activation (and
    blocks briefly for it to come up), mirroring the consumer cold-start path —
    so a Refresh after an idle period wakes the env and only reports success once
    it is running and the re-harvest actually ran. If the env is still booting
    after the grace window, the returned ``state`` reflects that (``not_running``
    with ``env_status: starting``) and the caller polls again; it never raises on
    a wake/harvest failure (the returned ``state``/``last_error`` reflect it).
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

    # Wake a suspended/stopped env so the re-harvest below has something to talk
    # to. Best-effort: resolve_running_producer_env handles the fast path (already
    # running → returned immediately) and the cold path (kick off activation +
    # block up to the grace window). On failure / still-booting we fall through
    # to report the live status; we never raise here (Refresh is best-effort).
    if agent.agent_api_enabled and environment is not None and environment.status != "running":
        try:
            environment = await AgentApiService.resolve_running_producer_env(
                session, agent
            )
        except AgentApiError:
            # Activation failed or still booting after the grace window. Re-read
            # the env so the status below reflects its current state, and let the
            # caller poll again.
            if agent.active_environment_id:
                environment = session.get(
                    AgentEnvironment, agent.active_environment_id
                )

    # Best-effort re-harvest. Only meaningful when enabled + env running; the
    # error (if any) is persisted by get_spec, and the status below reflects it.
    # Re-parse the policy.yaml alongside the spec so a policy edit is picked up
    # on demand (mirrors the background refresh_spec_cache).
    if agent.agent_api_enabled and environment is not None and environment.status == "running":
        # Bump keep-alive so a freshly woken env survives long enough for the
        # refreshed state to be useful (mirrors the spec/proxy routes).
        AgentApiService.update_last_activity(session, environment)
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
            query_string=request.url.query,
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


# ── Access & Scopes (per-user grants, owner-gated) ───────────────────────────
# Prefix: /api/v1/agents/{agent_id}/agent-api/grants
#
# The producer agent's owner assigns scopes to individual platform users. The
# proxy resolves these live and injects X-Cinna-Caller-Scopes (see
# agent_api_public.consumer_proxy). Routes mirror the MCP connector ACL shape:
# the frontend uses UserAllowlistPicker + GET /users/search to pick users.


@router.get("/grants/scope-catalog", response_model=AgentApiScopeCatalog)
def get_agent_api_scope_catalog(
    agent_id: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
):
    """Available scopes the producer declared in policy.yaml (for the picker).

    Graceful: an empty catalog when none are declared in the policy ``scopes:``
    map.
    """
    try:
        return AgentApiGrantService.get_scope_catalog(
            session, agent_id, current_user.id,
            is_superuser=current_user.is_superuser,
        )
    except AgentApiError as e:
        _handle_agent_api_error(e)


@router.get("/grants", response_model=AgentApiAccessGrantsPublic)
def list_agent_api_grants(
    agent_id: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
):
    """List per-user access grants for this producer agent (owner-gated)."""
    try:
        grants = AgentApiGrantService.list_grants(
            session, agent_id, current_user.id,
            is_superuser=current_user.is_superuser,
        )
        data = [AgentApiGrantService.to_public(session, g) for g in grants]
        return AgentApiAccessGrantsPublic(data=data, count=len(data))
    except AgentApiError as e:
        _handle_agent_api_error(e)


@router.post("/grants", response_model=AgentApiAccessGrantPublic)
async def create_agent_api_grant(
    agent_id: uuid.UUID,
    body: AgentApiAccessGrantCreate,
    session: SessionDep,
    current_user: CurrentUser,
):
    """Grant a platform user scopes on this producer agent's API (owner-gated)."""
    try:
        grant = await AgentApiGrantService.create_grant(
            session, agent_id, current_user.id, body,
            is_superuser=current_user.is_superuser,
        )
        return AgentApiGrantService.to_public(session, grant)
    except AgentApiError as e:
        _handle_agent_api_error(e)


@router.put("/grants/{grant_id}", response_model=AgentApiAccessGrantPublic)
async def update_agent_api_grant(
    agent_id: uuid.UUID,
    grant_id: uuid.UUID,
    body: AgentApiAccessGrantUpdate,
    session: SessionDep,
    current_user: CurrentUser,
):
    """Update a grant's scopes (owner-gated). Takes effect on the next call."""
    try:
        grant = await AgentApiGrantService.update_grant(
            session, agent_id, grant_id, current_user.id, body,
            is_superuser=current_user.is_superuser,
        )
        return AgentApiGrantService.to_public(session, grant)
    except AgentApiError as e:
        _handle_agent_api_error(e)


@router.delete("/grants/{grant_id}")
async def delete_agent_api_grant(
    agent_id: uuid.UUID,
    grant_id: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
) -> Message:
    """Remove a user's grant (owner-gated). Takes effect on the next call."""
    try:
        await AgentApiGrantService.delete_grant(
            session, agent_id, grant_id, current_user.id,
            is_superuser=current_user.is_superuser,
        )
        return Message(message="Grant removed")
    except AgentApiError as e:
        _handle_agent_api_error(e)
