"""
A2A API Routes - Agent-to-Agent protocol endpoints.

This module provides the A2A protocol endpoints for agent discovery
and communication via JSON-RPC 2.0 and SSE streaming.

URL scheme:
- /api/v1/a2a/{agent_id}/           — latest (v1.0), served by `router`
- /api/v1/a2a/v1.0/{agent_id}/      — explicit v1.0, served by `v1_router`
- /api/v1/a2a/v0.3/{agent_id}/      — legacy v0.3, served by `v03_router`

Authentication:
- Regular user JWT tokens (existing behavior)
- A2A access tokens (new) - scoped access for external A2A clients
"""
import uuid
import json
import logging
from typing import Any, Literal, Optional
from dataclasses import dataclass

from fastapi import APIRouter, HTTPException, Request, Depends
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlmodel import Session

from app.api.deps import SessionDep, get_db, get_current_user
from app.core.db import create_session
from app.models import Agent, User, A2ATokenPayload
from app.models.environments.environment import AgentEnvironment
from app.services.a2a.a2a_service import A2AService
from app.services.a2a.a2a_request_handler import A2ARequestHandler
from app.services.a2a.a2a_task_store import DatabaseTaskStore
from app.services.a2a.access_token_service import AccessTokenService
from app.services.a2a.a2a_v1_adapter import A2AV1Adapter
from app.services.a2a.jsonrpc_utils import jsonrpc_error, jsonrpc_success
from app.utils import get_base_url

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/a2a", tags=["a2a"])
v1_router = APIRouter(prefix="/a2a/v1.0", tags=["a2a"])
v03_router = APIRouter(prefix="/a2a/v0.3", tags=["a2a"])

# Optional bearer token auth
optional_bearer = HTTPBearer(auto_error=False)


@dataclass
class A2AAuthContext:
    """Authentication context for A2A requests."""
    user: Optional[User] = None
    a2a_token_payload: Optional[A2ATokenPayload] = None
    access_token_id: Optional[uuid.UUID] = None

    @property
    def user_id(self) -> uuid.UUID:
        """Get user ID (from user or agent owner)."""
        if self.user:
            return self.user.id
        # For A2A tokens, user_id will be set from agent owner
        raise ValueError("No user context available")

    def is_authenticated(self) -> bool:
        """Check if the context has valid authentication."""
        return self.user is not None or self.a2a_token_payload is not None

    def can_access_agent(self, agent: Agent, agent_id: uuid.UUID) -> bool:
        """Check if context can access the specified agent."""
        if self.user:
            # Regular user auth - must be owner or superuser
            return self.user.is_superuser or agent.owner_id == self.user.id
        if self.a2a_token_payload:
            # A2A token - must be for this agent
            return self.a2a_token_payload.agent_id == str(agent_id)
        return False


async def get_a2a_auth_context(
    request: Request,
    session: SessionDep,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(optional_bearer),
) -> A2AAuthContext:
    """
    Get authentication context for A2A requests.

    Supports both:
    - Regular user JWT tokens
    - A2A access tokens
    """
    if not credentials:
        raise HTTPException(status_code=401, detail="Not authenticated")

    token = credentials.credentials

    # First, try to decode as A2A token
    a2a_payload = AccessTokenService.verify_a2a_token(token)
    if a2a_payload:
        # It's an A2A token - validate it against the database
        agent_id = uuid.UUID(a2a_payload.agent_id)
        access_token, _ = AccessTokenService.validate_token_for_agent(
            session, token, agent_id
        )
        if access_token:
            return A2AAuthContext(
                a2a_token_payload=a2a_payload,
                access_token_id=uuid.UUID(a2a_payload.sub),
            )
        # Token failed validation (revoked, wrong hash, etc.)
        raise HTTPException(status_code=401, detail="Invalid or revoked access token")

    # Not an A2A token - try regular user auth
    try:
        user = get_current_user(session, token)
        return A2AAuthContext(user=user)
    except HTTPException:
        raise HTTPException(status_code=401, detail="Could not validate credentials")


async def get_optional_a2a_auth_context(
    request: Request,
    session: SessionDep,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(optional_bearer),
) -> Optional[A2AAuthContext]:
    """
    Get optional authentication context for A2A requests.

    Returns None if no credentials provided, otherwise validates and returns context.
    Used for endpoints that support both public and authenticated access.
    """
    if not credentials:
        return None

    token = credentials.credentials

    # First, try to decode as A2A token
    a2a_payload = AccessTokenService.verify_a2a_token(token)
    if a2a_payload:
        # It's an A2A token - validate it against the database
        agent_id = uuid.UUID(a2a_payload.agent_id)
        access_token, _ = AccessTokenService.validate_token_for_agent(
            session, token, agent_id
        )
        if access_token:
            return A2AAuthContext(
                a2a_token_payload=a2a_payload,
                access_token_id=uuid.UUID(a2a_payload.sub),
            )
        # Token failed validation (revoked, wrong hash, etc.)
        raise HTTPException(status_code=401, detail="Invalid or revoked access token")

    # Not an A2A token - try regular user auth
    try:
        user = get_current_user(session, token)
        return A2AAuthContext(user=user)
    except HTTPException:
        raise HTTPException(status_code=401, detail="Could not validate credentials")


A2AAuthDep = A2AAuthContext


# ---------------------------------------------------------------------------
# Shared handler implementations
# ---------------------------------------------------------------------------

async def _get_agent_card(
    agent_id: uuid.UUID,
    session: Session,
    request: Request,
    auth: Optional[A2AAuthContext],
    protocol: Literal["v1.0", "v0.3"],
) -> JSONResponse:
    """
    Shared AgentCard handler. Protocol determines the output format:
    - v1.0: apply A2AV1Adapter transformation, versioned URLs in supportedInterfaces
    - v0.3: return library-native format with v0.3-specific URL
    """
    agent = session.get(Agent, agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Check if A2A is enabled for this agent
    a2a_enabled = agent.a2a_config.get("enabled", False) if agent.a2a_config else False

    base_url = get_base_url(request)
    use_v1 = protocol == "v1.0"

    # For v0.3 endpoints, the card URL should point to the v0.3-specific endpoint
    url_override = None if use_v1 else f"{base_url}/api/v1/a2a/v0.3/{agent_id}/"

    # If not authenticated
    if not auth or not auth.is_authenticated():
        # Only allow public access if A2A is enabled
        if not a2a_enabled:
            raise HTTPException(status_code=401, detail="Not authenticated")
        # Return minimal public card
        card_dict = A2AService.get_public_agent_card_dict(
            agent, base_url, url_override=url_override, protocol=protocol
        )
        return JSONResponse(content=card_dict)

    # Authenticated - check permissions
    if not auth.can_access_agent(agent, agent_id):
        raise HTTPException(status_code=403, detail="Not enough permissions")

    environment = session.get(AgentEnvironment, agent.active_environment_id) if agent.active_environment_id else None

    # Return full extended card
    card_dict = A2AService.get_agent_card_dict(
        agent, environment, base_url, url_override=url_override, protocol=protocol
    )
    return JSONResponse(content=card_dict)


async def _handle_jsonrpc(
    agent_id: uuid.UUID,
    request: Request,
    session: Session,
    auth: A2AAuthContext,
    protocol: Literal["v1.0", "v0.3"],
):
    """
    Shared JSON-RPC handler. Protocol determines request/response transformation:
    - v1.0: transform inbound PascalCase method names, add 'kind' discriminator outbound
    - v0.3: passthrough (library speaks v0.3 natively)
    """
    # Validate agent access
    agent = session.get(Agent, agent_id)
    if not agent:
        return _error_response(None, -32001, "Agent not found")
    if not auth.can_access_agent(agent, agent_id):
        return _error_response(None, -32004, "Not enough permissions")

    environment = session.get(AgentEnvironment, agent.active_environment_id) if agent.active_environment_id else None
    if not environment:
        return _error_response(None, -32002, "Agent has no active environment")

    # Parse JSON-RPC request
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return _error_response(None, -32700, "Parse error")

    # Validate JSON-RPC structure
    if not isinstance(body, dict):
        return _error_response(None, -32600, "Invalid Request")

    jsonrpc = body.get("jsonrpc")
    if jsonrpc != "2.0":
        return _error_response(body.get("id"), -32600, "Invalid Request: jsonrpc must be '2.0'")

    # Determine if v1.0 transformation applies based on URL-resolved protocol
    use_v1 = protocol == "v1.0"
    if use_v1:
        body = A2AV1Adapter.transform_request_inbound(body)

    method = body.get("method")
    request_id = body.get("id")
    params = body.get("params", {})

    if not method:
        return _error_response(request_id, -32600, "Invalid Request: method is required")

    # Determine user_id for session operations
    # For A2A tokens, we use the agent owner's ID
    if auth.user:
        user_id = auth.user.id
    else:
        user_id = agent.owner_id

    backend_base_url = get_base_url(request)

    # Create request handler with A2A token context
    handler = A2ARequestHandler(
        agent=agent,
        environment=environment,
        user_id=user_id,
        get_db_session=create_session,
        a2a_token_payload=auth.a2a_token_payload,
        access_token_id=auth.access_token_id,
        backend_base_url=backend_base_url,
    )

    try:
        if method == "message/stream":
            # Check mode permission for A2A tokens
            if auth.a2a_token_payload:
                # Default mode is conversation
                requested_mode = params.get("configuration", {}).get("mode", "conversation")
                if not AccessTokenService.can_use_mode(auth.a2a_token_payload, requested_mode):
                    return _error_response(
                        request_id, -32004,
                        f"Access token does not allow '{requested_mode}' mode"
                    )

            # Return SSE stream
            return StreamingResponse(
                handler.handle_message_stream(params, request_id),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )

        elif method == "message/send":
            # Check mode permission for A2A tokens
            if auth.a2a_token_payload:
                requested_mode = params.get("configuration", {}).get("mode", "conversation")
                if not AccessTokenService.can_use_mode(auth.a2a_token_payload, requested_mode):
                    return _error_response(
                        request_id, -32004,
                        f"Access token does not allow '{requested_mode}' mode"
                    )

            # Synchronous message handling
            task = await handler.handle_message_send(params)
            result = task.model_dump(by_alias=True, exclude_none=True)
            if use_v1:
                result = A2AV1Adapter.transform_task_outbound(result)
            return JSONResponse(content=jsonrpc_success(request_id, result))

        elif method == "tasks/get":
            task = await handler.handle_tasks_get(params)
            if task:
                result = task.model_dump(by_alias=True, exclude_none=True)
                if use_v1:
                    result = A2AV1Adapter.transform_task_outbound(result)
                return JSONResponse(content=jsonrpc_success(request_id, result))
            else:
                return _error_response(request_id, -32001, "Task not found")

        elif method == "tasks/cancel":
            try:
                result = await handler.handle_tasks_cancel(params)
                return JSONResponse(content=jsonrpc_success(request_id, result))
            except ValueError as e:
                return _error_response(request_id, -32001, str(e))

        elif method == "tasks/list":
            # Custom extension to A2A protocol - list tasks for this agent
            tasks = await handler.handle_tasks_list(params)
            task_results = [task.model_dump(by_alias=True, exclude_none=True) for task in tasks]
            if use_v1:
                task_results = [A2AV1Adapter.transform_task_outbound(t) for t in task_results]
            return JSONResponse(content=jsonrpc_success(request_id, task_results))

        elif method == "agent/status":
            # Custom extension: return the agent's self-reported STATUS.md snapshot.
            # Params: { "force_refresh": bool } (optional)
            from app.services.agents.agent_status_service import AgentStatusService, StatusUnavailableError
            force_refresh = params.get("force_refresh", False)
            if force_refresh:
                if AgentStatusService.is_rate_limited(environment.id):
                    return _error_response(request_id, -32029, "Rate limited — try again in 30 seconds")
                try:
                    snapshot = await AgentStatusService.fetch_status(environment)
                except StatusUnavailableError:
                    snapshot = AgentStatusService.get_cached_status(environment)
            else:
                snapshot = AgentStatusService.get_cached_status(environment)
            result = {
                "agent_id": str(agent.id),
                "environment_id": str(snapshot.environment_id) if snapshot.environment_id else None,
                "severity": snapshot.severity,
                "summary": snapshot.summary,
                "reported_at": snapshot.reported_at.isoformat() if snapshot.reported_at else None,
                "reported_at_source": snapshot.reported_at_source,
                "fetched_at": snapshot.fetched_at.isoformat() if snapshot.fetched_at else None,
                "body": snapshot.body,
                "has_structured_metadata": snapshot.has_structured_metadata,
                "prev_severity": snapshot.prev_severity,
                "severity_changed_at": snapshot.severity_changed_at.isoformat() if snapshot.severity_changed_at else None,
            }
            return JSONResponse(content=jsonrpc_success(request_id, result))

        else:
            return _error_response(request_id, -32601, f"Method not found: {method}")

    except Exception as e:
        logger.error(f"Error handling A2A request: {e}", exc_info=True)
        return _error_response(request_id, -32603, f"Internal error: {str(e)}")


# ---------------------------------------------------------------------------
# Base router — latest (v1.0)  prefix: /a2a
# ---------------------------------------------------------------------------

@router.get("/{agent_id}/")
async def get_agent_card(
    agent_id: uuid.UUID,
    session: SessionDep,
    request: Request,
    auth: Optional[A2AAuthContext] = Depends(get_optional_a2a_auth_context),
) -> JSONResponse:
    """
    Return A2A AgentCard for the specified agent (latest / v1.0 format).

    Access levels:
    - No authentication: Returns minimal public card (name only) if A2A is enabled
    - Authenticated: Returns full extended card with all details

    The AgentCard provides discovery information including agent capabilities,
    skills, and versioned endpoint URLs in supportedInterfaces.
    """
    return await _get_agent_card(agent_id, session, request, auth, protocol="v1.0")


@router.get("/{agent_id}/.well-known/agent-card.json")
async def get_agent_card_well_known(
    agent_id: uuid.UUID,
    session: SessionDep,
    request: Request,
    auth: Optional[A2AAuthContext] = Depends(get_optional_a2a_auth_context),
) -> JSONResponse:
    """
    Alternative well-known location for AgentCard (latest / v1.0 format).

    Standard A2A discovery endpoint.
    """
    return await _get_agent_card(agent_id, session, request, auth, protocol="v1.0")


@router.post("/{agent_id}/")
async def handle_jsonrpc(
    agent_id: uuid.UUID,
    request: Request,
    session: SessionDep,
    auth: A2AAuthContext = Depends(get_a2a_auth_context),
):
    """
    Handle A2A JSON-RPC requests (latest / v1.0 protocol).

    Supported methods:
    - SendMessage: Send message, wait for response (non-streaming)
    - SendStreamingMessage: Send message, stream response (SSE)
    - GetTask: Get task status and history
    - CancelTask: Cancel running task

    Authentication:
    - Regular user JWT: Full access to owned agents
    - A2A access token: Scoped access based on token mode and scope
    """
    return await _handle_jsonrpc(agent_id, request, session, auth, protocol="v1.0")


# ---------------------------------------------------------------------------
# v1_router — explicit v1.0  prefix: /a2a/v1.0
# ---------------------------------------------------------------------------

@v1_router.get("/{agent_id}/")
async def get_agent_card_v1(
    agent_id: uuid.UUID,
    session: SessionDep,
    request: Request,
    auth: Optional[A2AAuthContext] = Depends(get_optional_a2a_auth_context),
) -> JSONResponse:
    """Return A2A AgentCard in explicit v1.0 format."""
    return await _get_agent_card(agent_id, session, request, auth, protocol="v1.0")


@v1_router.get("/{agent_id}/.well-known/agent-card.json")
async def get_agent_card_well_known_v1(
    agent_id: uuid.UUID,
    session: SessionDep,
    request: Request,
    auth: Optional[A2AAuthContext] = Depends(get_optional_a2a_auth_context),
) -> JSONResponse:
    """Alternative well-known location for AgentCard (v1.0 format)."""
    return await _get_agent_card(agent_id, session, request, auth, protocol="v1.0")


@v1_router.post("/{agent_id}/")
async def handle_jsonrpc_v1(
    agent_id: uuid.UUID,
    request: Request,
    session: SessionDep,
    auth: A2AAuthContext = Depends(get_a2a_auth_context),
):
    """
    Handle A2A JSON-RPC requests (explicit v1.0 protocol).

    Accepts PascalCase method names: SendMessage, SendStreamingMessage, GetTask, CancelTask.
    """
    return await _handle_jsonrpc(agent_id, request, session, auth, protocol="v1.0")


# ---------------------------------------------------------------------------
# v03_router — legacy v0.3  prefix: /a2a/v0.3
# ---------------------------------------------------------------------------

@v03_router.get("/{agent_id}/")
async def get_agent_card_v03(
    agent_id: uuid.UUID,
    session: SessionDep,
    request: Request,
    auth: Optional[A2AAuthContext] = Depends(get_optional_a2a_auth_context),
) -> JSONResponse:
    """
    Return A2A AgentCard in legacy v0.3 format.

    The card URL points to the v0.3-specific endpoint. No adapter transformation applied.
    """
    return await _get_agent_card(agent_id, session, request, auth, protocol="v0.3")


@v03_router.get("/{agent_id}/.well-known/agent-card.json")
async def get_agent_card_well_known_v03(
    agent_id: uuid.UUID,
    session: SessionDep,
    request: Request,
    auth: Optional[A2AAuthContext] = Depends(get_optional_a2a_auth_context),
) -> JSONResponse:
    """Alternative well-known location for AgentCard (v0.3 format)."""
    return await _get_agent_card(agent_id, session, request, auth, protocol="v0.3")


@v03_router.post("/{agent_id}/")
async def handle_jsonrpc_v03(
    agent_id: uuid.UUID,
    request: Request,
    session: SessionDep,
    auth: A2AAuthContext = Depends(get_a2a_auth_context),
):
    """
    Handle A2A JSON-RPC requests (legacy v0.3 protocol).

    Accepts slash-case method names: message/send, message/stream, tasks/get, tasks/cancel.
    No method name transformation applied — the library speaks v0.3 natively.
    """
    return await _handle_jsonrpc(agent_id, request, session, auth, protocol="v0.3")


# ---------------------------------------------------------------------------
# JSON-RPC helpers
# ---------------------------------------------------------------------------

def _error_response(request_id: Any, code: int, message: str) -> JSONResponse:
    """Wrap a JSON-RPC error envelope in a JSONResponse (HTTP 200)."""
    return JSONResponse(content=jsonrpc_error(request_id, code, message), status_code=200)
