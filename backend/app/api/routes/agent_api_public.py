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
from app.models import Agent, AgentApiToken, AgentApiTokenKind
from app.services.agent_api.agent_api_service import (
    AgentApiAuthError,
    AgentApiError,
    AgentApiPolicyError,
    AgentApiService,
)
from app.services.agent_api.agent_api_grant_service import AgentApiGrantService
from app.services.agent_api.agent_api_identity_service import (
    CALLER_HEADER_PREFIX,
    CALLER_SCOPES_HEADER,
    CALLER_USER_ID_HEADER,
    IDENTITY_HEADER,
    AgentApiIdentityService,
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
    # The producer's external-access opt-in is the proxy's KILL SWITCH: it does
    # not merely gate minting. Turning it off must immediately stop every issued
    # key, without touching agent-to-agent connections and without revoking keys
    # one at a time. Threaded into validate_token so a rejected key never bumps
    # last_used_at (which would read as "this key still works").
    token = AgentApiTokenService.validate_token(
        session,
        agent_id,
        token_value,
        external_access_enabled=agent.agent_api_external_access_enabled,
    )
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
    # Spec retrieval is gated ONLY by ``expose_spec`` — per-endpoint scope gating
    # (enforce_policy) applies to data routes (consumer_proxy), not the spec.
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
        # Token validation is the gate — it runs first, so all caller-identity /
        # grant work below stays behind the authenticated boundary (no DB grant
        # lookup for an unauthenticated request).
        agent, token = _validate_token_or_401(session, agent_id, creds)

        # Resolve caller identity → trusted attribution headers + per-user grant
        # scopes. Computed BEFORE policy enforcement because edge scope
        # enforcement (D8) gates inside enforce_policy. Caller resolution makes
        # no authz decision on its own — the token validation above is the gate.
        #
        # TWO identity sources, and the ORDER IS THE SECURITY PROPERTY:
        #  * An EXTERNAL key carries its identity IN THE TOKEN (subject_user_id,
        #    bound at mint, immutable). It is used verbatim and the request's
        #    X-Cinna-Caller-Identity header is NEVER consulted — otherwise the
        #    holder of a key bound to user A could assert user B by supplying
        #    their own identity header (plan D2 precedence rule).
        #  * A CONNECTION token is anonymous by construction, so identity comes
        #    from the L2 identity header the platform injects into the consumer
        #    container. Missing / invalid / expired ⇒ empty dict ⇒ anonymous
        #    (never an error). Never log the identity token.
        # The branch is on `kind` ALONE: a key with no subject falls back to
        # anonymous, never to the header, so "an external key cannot assert an
        # identity from the request" holds structurally rather than by luck.
        if token.kind == AgentApiTokenKind.EXTERNAL.value:
            caller_headers = (
                AgentApiIdentityService.resolve_caller_headers_for_user(
                    session, token.subject_user_id
                )
                if token.subject_user_id is not None
                else {}
            )
        else:
            caller_headers = AgentApiIdentityService.resolve_caller_headers(
                session, request.headers.get(IDENTITY_HEADER)
            )
        # When the caller was attributed AND identity applies to this call,
        # resolve the live grant. The user id is read back from the just-set
        # attribution header (single source of truth — no second resolution).
        # Identity ATTRIBUTION is honored regardless of the flag (backward
        # compatible); only SCOPES (and the optional edge enforcement) follow
        # `resolve_identity_enabled` — the producer's opt-in OR an external key,
        # which is self-evidently intentional (plan D3).
        resolved_user_id = caller_headers.get(CALLER_USER_ID_HEADER)
        identity_enabled = AgentApiService.resolve_identity_enabled(agent, token)
        caller_scopes: list[str] = []
        if resolved_user_id and identity_enabled:
            caller_scopes = AgentApiGrantService.resolve_scopes_for_caller(
                session, agent_id, uuid.UUID(resolved_user_id)
            )
            if caller_scopes:
                caller_headers[CALLER_SCOPES_HEADER] = " ".join(caller_scopes)

        # Single auditable orchestration: enforce policy BEFORE env resolution so
        # a 405/413/429/403 never wakes a suspended env; returns the propagated
        # deadline + hop-depth headers to inject downstream. Edge scope
        # enforcement (D8) uses caller_scopes + the producer's identity opt-in.
        environment, hop_headers = await AgentApiService.authorize_consumer_request(
            session,
            agent=agent,
            token=token,
            method=request.method,
            path=path,
            body_size=len(body),
            incoming_headers=dict(request.headers),
            caller_scopes=caller_scopes,
        )
    except AgentApiError as e:
        _handle_agent_api_error(e)

    AgentApiService.update_last_activity(session, environment)

    # Forward headers, applying the four non-negotiable proxy rules:
    #  1. Strip the consumer's bearer — it's for our edge, not the producer's app.
    #  2. Strip the raw identity token — the producer must NEVER receive it
    #     (otherwise it could harvest + replay it to impersonate callers).
    #  3. Strip ALL inbound X-Cinna-Caller-* headers — the identity token is the
    #     ONLY accepted identity input; client-supplied caller headers are forged.
    #  4. Set X-Cinna-Caller-* authoritatively from the resolved caller — the
    #     external key's bound subject, or the verified identity token's owner
    #     (none when anonymous). Then inject the request-loop guard headers.
    # Header keys are lowercased before comparison (request.headers yields raw
    # wire casing). The identity header is stripped explicitly AND is also
    # covered by the X-Cinna-Caller-* prefix strip — the redundant explicit
    # clause future-proofs rule 1 against a rename that moves the identity header
    # outside the prefix.
    fwd_headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() != "authorization"
        and k.lower() != IDENTITY_HEADER.lower()
        and not k.lower().startswith(CALLER_HEADER_PREFIX)
    }
    fwd_headers.update(caller_headers)
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
