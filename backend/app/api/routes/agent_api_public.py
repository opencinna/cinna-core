"""
Agent REST API — consumer serving routes (token-authenticated).

Phase 2: any caller presenting a valid ``agent_api`` token for the producer
agent can fetch the spec and call the API. Cross-user is allowed because the
*thing shared* is the narrowed proxy (URL + token), not the upstream secret.

Prefix: ``/api/v1/agent-api/{agent_id}``

| Method | Path           | Purpose                                                       |
|--------|----------------|---------------------------------------------------------------|
| GET    | /openapi.json  | Spec passthrough (subject to ``expose_spec``).                |
| POST   | /_cinna/improvement-requests | File a capability request (identified caller). |
| GET    | /_cinna/improvement-requests | The caller's own capability requests.          |
| ANY    | /_cinna/{rest} | Reserved: 404 / 405, never forwarded to the producer.        |
| ANY    | /{path:path}   | Full HTTP passthrough: validate token → enforce policy →      |
|        |                | keep-alive → auto-activate producer env → adapter proxy.      |

Auth: ``Authorization: Bearer <token>``. The token resolves to the producer
agent + its owner; the producer env is auto-activated on the producer's behalf —
the consumer never needs the producer's session/auth.

``_cinna/`` is a reserved, platform-owned prefix: those routes are registered
above the passthrough, never reach the producer env (and never wake it), and
are not subject to the producer's ``policy.yaml``.
"""
import logging
import uuid

from fastapi import APIRouter, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.api.deps import SessionDep
from app.models import Agent, AgentApiToken, AgentApiTokenKind, User
from app.models.improvement.agent_improvement_request import (
    CAPABILITY_REQUEST_BODY_SHAPE,
    AgentApiCapabilityRequestPublic,
    AgentApiCapabilityRequestsPublic,
)
from app.services.agent_api.agent_api_grant_service import AgentApiGrantService
from app.services.agent_api.agent_api_identity_service import (
    CALLER_HEADER_PREFIX,
    CALLER_SCOPES_HEADER,
    CALLER_USER_ID_HEADER,
    IDENTITY_HEADER,
    AgentApiIdentityService,
)
from app.services.agent_api.agent_api_service import (
    AgentApiAuthError,
    AgentApiError,
    AgentApiPolicyError,
    AgentApiService,
)
from app.services.agent_api.agent_api_token_service import AgentApiTokenService
from app.services.environments.environment_service import EnvironmentService
from app.services.improvement.capability_request_body import (
    CapabilityRequestBodyError,
    capability_request_openapi_extra,
    parse_capability_request_body,
    reject_oversized_content_length,
)
from app.services.improvement.improvement_request_service import (
    ImprovementRequestDenied,
    ImprovementRequestService,
)

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


class _IdentifiedCaller:
    """The authenticated, identified caller of a ``_cinna/`` route."""

    def __init__(
        self,
        agent: Agent,
        token: AgentApiToken,
        user: User,
        consumer_agent_id: uuid.UUID | None,
        raw_secrets: set[str],
    ) -> None:
        self.agent = agent
        self.token = token
        self.user = user
        self.consumer_agent_id = consumer_agent_id
        self.raw_secrets = raw_secrets


async def _resolve_identified_caller(
    session: SessionDep, request: Request, agent_id: uuid.UUID
) -> _IdentifiedCaller:
    """Validate the token and resolve the caller's identity, or raise.

    Same identity precedence as ``consumer_proxy``: an EXTERNAL key's bound
    subject is used verbatim and the identity header is never consulted; a
    CONNECTION token takes identity from the verified identity header. These
    routes act on behalf of a person, so an anonymous caller is a 401.
    Never touches the producer env. These ``_cinna`` routes have no scope
    gate; any future one must use ``AgentApiGrantService.effective_scopes_for_call``.
    """
    creds: HTTPAuthorizationCredentials | None = await _bearer(request)
    try:
        agent, token = _validate_token_or_401(session, agent_id, creds)
    except AgentApiError as e:
        _handle_agent_api_error(e)

    identity_header = request.headers.get(IDENTITY_HEADER)
    user_id: uuid.UUID | None = None
    consumer_agent_id: uuid.UUID | None = None
    if token.kind == AgentApiTokenKind.EXTERNAL.value:
        user_id = token.subject_user_id
    else:
        claims = AgentApiIdentityService.verify_claims(identity_header)
        if claims is not None:
            user_id = claims.user_id
            consumer_agent_id = claims.agent_id

    user = session.get(User, user_id) if user_id is not None else None
    if user is None:
        raise HTTPException(
            status_code=401,
            detail="Caller identity required to file capability requests",
        )

    raw_secrets = {v for v in (creds.credentials if creds else None, identity_header) if v}
    return _IdentifiedCaller(agent, token, user, consumer_agent_id, raw_secrets)


_CAPABILITY_REQUESTS_ROUTE = "improvement-requests"
_CAPABILITY_REQUESTS_METHODS = ("GET", "POST")


@router.post(
    "/_cinna/improvement-requests",
    response_model=AgentApiCapabilityRequestPublic,
    status_code=201,
    openapi_extra=capability_request_openapi_extra(),
)
async def consumer_create_capability_request(
    agent_id: uuid.UUID,
    request: Request,
    session: SessionDep,
):
    """File a capability request against this API (identified callers only).

    The body (``AgentApiCapabilityRequestCreate``) is read raw, after auth, so
    an oversized payload is refused (413) before it is parsed and an
    unauthenticated caller never gets a validation report. Rate limited per
    caller (429 with ``Retry-After``).
    """
    caller = await _resolve_identified_caller(session, request, agent_id)

    try:
        reject_oversized_content_length(request.headers.get("content-length"))
        body = parse_capability_request_body(await request.body())
    except CapabilityRequestBodyError as e:
        if e.status_code == 422:
            raise HTTPException(
                status_code=422,
                detail={
                    "message": e.message,
                    "errors": e.errors,
                    "expected": CAPABILITY_REQUEST_BODY_SHAPE,
                },
            )
        raise HTTPException(status_code=e.status_code, detail=e.message)

    try:
        row = await ImprovementRequestService.create_from_agent_api(
            session,
            producer=caller.agent,
            token=caller.token,
            caller=caller.user,
            consumer_agent_id=caller.consumer_agent_id,
            body=body,
            raw_secrets=caller.raw_secrets,
        )
    except ImprovementRequestDenied as e:
        headers = (
            {"Retry-After": str(e.retry_after)} if e.retry_after is not None else None
        )
        raise HTTPException(status_code=e.status_code, detail=e.message, headers=headers)
    return ImprovementRequestService.to_capability_public(row)


@router.get(
    "/_cinna/improvement-requests",
    response_model=AgentApiCapabilityRequestsPublic,
)
async def consumer_list_capability_requests(
    agent_id: uuid.UUID,
    request: Request,
    session: SessionDep,
):
    """The caller's own capability requests on this API, newest first (≤ 50)."""
    caller = await _resolve_identified_caller(session, request, agent_id)
    rows, count = ImprovementRequestService.list_for_requester_on_target(
        session, caller.user.id, caller.agent.id
    )
    return AgentApiCapabilityRequestsPublic(
        data=[ImprovementRequestService.to_capability_public(r) for r in rows],
        count=count,
    )


@router.api_route(
    "/_cinna",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
    include_in_schema=False,
)
@router.api_route(
    "/_cinna/{rest:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
    include_in_schema=False,
)
async def consumer_reserved_prefix(
    agent_id: uuid.UUID,
    request: Request,
    session: SessionDep,
    rest: str = "",
):
    """The rest of the reserved ``_cinna/`` prefix: never reaches the producer.

    Registered after the exact routes and before the passthrough. The token is
    validated first (an unauthenticated caller learns nothing about which paths
    exist). A trailing-slash spelling of a known route is served by it; a known
    route with another method is 405; anything else is 404. Never wakes the env.
    """
    route = rest.strip("/")
    if route == _CAPABILITY_REQUESTS_ROUTE and request.method == "POST":
        created = await consumer_create_capability_request(agent_id, request, session)
        # The shared catch-all answers 200 by default; match the canonical 201.
        return JSONResponse(status_code=201, content=jsonable_encoder(created))
    if route == _CAPABILITY_REQUESTS_ROUTE and request.method == "GET":
        return await consumer_list_capability_requests(agent_id, request, session)

    creds: HTTPAuthorizationCredentials | None = await _bearer(request)
    try:
        _validate_token_or_401(session, agent_id, creds)
    except AgentApiError as e:
        _handle_agent_api_error(e)
    if route == _CAPABILITY_REQUESTS_ROUTE:
        raise HTTPException(
            status_code=405,
            detail="Method not allowed",
            headers={"Allow": ", ".join(_CAPABILITY_REQUESTS_METHODS)},
        )
    raise HTTPException(status_code=404, detail="Not found")


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
        # Effective scopes = the caller's live grant (only when attributed AND
        # identity applies: the producer's opt-in OR an external key, plan D3)
        # ∪ the connection's owner-chosen scopes (the bearer matched a
        # kind="connection" token row; nothing the caller sends names it).
        # The user id is read back from the just-set attribution header (single
        # source of truth). Invariants:
        #  * Identity off: connection scopes only; the grant stays gated by the
        #    producer's switch; no edge deny (enforcement gate unchanged).
        #  * Identity on, identity header absent/invalid/expired: anonymous
        #    caller holding the connection scopes; edge enforcement uses them.
        #  * Inbound X-Cinna-Caller-* headers are stripped below (rule 3), so a
        #    forged scopes header adds nothing.
        #  * External key: no connection scopes, behaviour unchanged.
        #  * `token` is read fresh by validate_token per request: a scope change
        #    applies from the next call.
        resolved_user_id = caller_headers.get(CALLER_USER_ID_HEADER)
        caller_scopes = AgentApiGrantService.effective_scopes_for_call(
            session,
            agent,
            token,
            uuid.UUID(resolved_user_id) if resolved_user_id else None,
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
