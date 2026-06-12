"""
Agent REST API — consumer serving routes (token-authenticated).

Phase 2: any caller presenting a valid ``agent_api`` token for the producer
agent can fetch the spec and call the API. Cross-user is allowed because the
*thing shared* is the narrowed proxy (URL + token), not the upstream secret.

Prefix: ``/api/v1/agent-api/{agent_id}``

| Method | Path           | Purpose                                                       |
|--------|----------------|---------------------------------------------------------------|
| GET    | /openapi.json  | Spec passthrough (subject to ``expose_spec``).                |
| ANY    | /{path:path}   | Full HTTP passthrough: validate token → enforce policy →      |
|        |                | keep-alive → auto-activate producer env → adapter proxy.      |

Auth: ``Authorization: Bearer <token>``. The token resolves to the producer
agent + its owner; the producer env is auto-activated on the producer's behalf —
the consumer never needs the producer's session/auth.
"""
import logging
import uuid

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.api.deps import SessionDep
from app.models import Agent, AgentApiToken
from app.services.agent_api.agent_api_service import (
    AgentApiAuthError,
    AgentApiError,
    AgentApiPolicyError,
    AgentApiService,
)
from app.services.agent_api.agent_api_token_service import AgentApiTokenService
from app.services.environments.environment_service import EnvironmentService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agent-api/{agent_id}", tags=["agent-api-public"])

_bearer = HTTPBearer(auto_error=False)


def _handle_agent_api_error(e: AgentApiError) -> None:
    """Convert agent-api service exceptions to HTTP exceptions (with Retry-After)."""
    headers = None
    retry_after = getattr(e, "retry_after", None)
    if retry_after is not None:
        headers = {"Retry-After": str(retry_after)}
    raise HTTPException(status_code=e.status_code, detail=e.message, headers=headers)


def _validate_token_or_401(
    session: SessionDep,
    agent_id: uuid.UUID,
    creds: HTTPAuthorizationCredentials | None,
) -> tuple[Agent, AgentApiToken]:
    """
    Validate the bearer token for the producer agent.

    Order matters for not-leaking existence: a disabled feature returns 404
    (same as a missing agent). An invalid token returns 401.
    """
    # Resolve the agent first; 404 if missing or feature disabled (no leak).
    agent = session.get(Agent, agent_id)
    if not agent or not agent.agent_api_enabled:
        raise HTTPException(status_code=404, detail="Not found")

    token_value = creds.credentials if creds else None
    token = AgentApiTokenService.validate_token(session, agent_id, token_value)
    if token is None:
        raise AgentApiAuthError("Invalid or expired token")
    return agent, token


@router.get("/openapi.json")
async def consumer_spec(
    agent_id: uuid.UUID,
    request: Request,
    session: SessionDep,
):
    """Spec passthrough for consumers (subject to ``expose_spec`` in policy)."""
    creds: HTTPAuthorizationCredentials | None = await _bearer(request)
    try:
        agent, token = _validate_token_or_401(session, agent_id, creds)
        environment = await AgentApiService.resolve_running_producer_env(session, agent)

        policy = await AgentApiService.load_policy(session, environment)
        if not policy.get("expose_spec", True):
            raise AgentApiPolicyError("Spec is not exposed by this API", status_code=403)

        spec = await AgentApiService.get_spec(session, environment)
    except AgentApiError as e:
        _handle_agent_api_error(e)

    AgentApiService.update_last_activity(session, environment)
    return JSONResponse(content=spec)


@router.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
    include_in_schema=False,
)
async def consumer_proxy(
    agent_id: uuid.UUID,
    path: str,
    request: Request,
    session: SessionDep,
):
    """
    Full HTTP passthrough for consumers.

    validate token → authorize (enforce policy + compute request-loop headers +
    auto-activate producer env) → keep-alive → adapter proxy.
    """
    creds: HTTPAuthorizationCredentials | None = await _bearer(request)
    body = await request.body()

    try:
        agent, token = _validate_token_or_401(session, agent_id, creds)
        # Single auditable orchestration: enforce policy BEFORE env resolution so
        # a 405/413/429 never wakes a suspended env; returns the propagated
        # deadline + hop-depth headers to inject downstream.
        environment, hop_headers = await AgentApiService.authorize_consumer_request(
            session,
            agent=agent,
            token=token,
            method=request.method,
            path=path,
            body_size=len(body),
            incoming_headers=dict(request.headers),
        )
    except AgentApiError as e:
        _handle_agent_api_error(e)

    AgentApiService.update_last_activity(session, environment)

    # Forward headers: drop the consumer's bearer (it's for our edge, not the
    # producer's app) and inject the request-loop guard headers.
    fwd_headers = {
        k: v for k, v in request.headers.items() if k.lower() != "authorization"
    }
    fwd_headers.update(hop_headers)

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
        logger.error("agent_api consumer proxy failed for agent %s path %s: %s", agent_id, path, e)
        raise HTTPException(status_code=502, detail=f"Agent API proxy error: {e}")

    return StreamingResponse(
        stream,
        status_code=status_code,
        headers=resp_headers,
    )
