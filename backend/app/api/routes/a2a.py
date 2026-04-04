"""
A2A API Routes - Agent-to-Agent protocol endpoints.

This module provides the A2A protocol endpoints for agent discovery
and communication via JSON-RPC 2.0 and SSE streaming.

Authentication:
- Regular user JWT tokens (existing behavior)
- A2A access tokens (new) - scoped access for external A2A clients
"""
import uuid
import json
import logging
from typing import Any, Optional
from dataclasses import dataclass

from fastapi import APIRouter, HTTPException, Request, Depends
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlmodel import Session

from app.api.deps import SessionDep, get_db, get_current_user
from app.models import Agent, User, A2ATokenPayload
from app.models.environments.environment import AgentEnvironment
from app.services.a2a.a2a_service import A2AService
from app.services.a2a.a2a_request_handler import A2ARequestHandler
from app.services.a2a.a2a_task_store import DatabaseTaskStore
from app.services.a2a.access_token_service import AccessTokenService
from app.services.a2a.a2a_v1_adapter import A2AV1Adapter
from app.core.db import engine
from app.utils import get_base_url

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/a2a", tags=["a2a"])

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


def get_fresh_db_session():
    """Get a fresh database session context manager."""
    return Session(engine)


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


@router.get("/{agent_id}/")
async def get_agent_card(
    agent_id: uuid.UUID,
    session: SessionDep,
    request: Request,
    auth: Optional[A2AAuthContext] = Depends(get_optional_a2a_auth_context),
) -> JSONResponse:
    """
    Return A2A AgentCard for the specified agent.

    Access levels:
    - No authentication: Returns minimal public card (name only) if A2A is enabled
    - Authenticated: Returns full extended card with all details

    The AgentCard provides discovery information including
    agent capabilities, skills, and endpoint URLs.
    """
    agent = session.get(Agent, agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Check if A2A is enabled for this agent
    a2a_enabled = agent.a2a_config.get("enabled", False) if agent.a2a_config else False

    base_url = get_base_url(request)

    # Check if v1.0 format should be used
    use_v1 = A2AV1Adapter.should_use_v1(request)

    # If not authenticated
    if not auth or not auth.is_authenticated():
        # Only allow public access if A2A is enabled
        if not a2a_enabled:
            raise HTTPException(status_code=401, detail="Not authenticated")
        # Return minimal public card
        card_dict = A2AService.get_public_agent_card_dict(agent, base_url)
        if use_v1:
            card_dict = A2AV1Adapter.transform_agent_card_outbound(card_dict)
        return JSONResponse(content=card_dict)

    # Authenticated - check permissions
    if not auth.can_access_agent(agent, agent_id):
        raise HTTPException(status_code=403, detail="Not enough permissions")

    environment = session.get(AgentEnvironment, agent.active_environment_id) if agent.active_environment_id else None

    # Return full extended card
    card_dict = A2AService.get_agent_card_dict(agent, environment, base_url)
    if use_v1:
        card_dict = A2AV1Adapter.transform_agent_card_outbound(card_dict)
    return JSONResponse(content=card_dict)


@router.get("/{agent_id}/.well-known/agent-card.json")
async def get_agent_card_well_known(
    agent_id: uuid.UUID,
    session: SessionDep,
    request: Request,
    auth: Optional[A2AAuthContext] = Depends(get_optional_a2a_auth_context),
) -> JSONResponse:
    """
    Alternative well-known location for AgentCard.

    Standard A2A discovery endpoint.
    """
    return await get_agent_card(agent_id, session, request, auth)


@router.post("/{agent_id}/")
async def handle_jsonrpc(
    agent_id: uuid.UUID,
    request: Request,
    session: SessionDep,
    auth: A2AAuthContext = Depends(get_a2a_auth_context),
):
    """
    Handle A2A JSON-RPC requests.

    Supported methods:
    - message/send: Send message, wait for response (non-streaming)
    - message/stream: Send message, stream response (SSE)
    - tasks/get: Get task status and history
    - tasks/cancel: Cancel running task

    Authentication:
    - Regular user JWT: Full access to owned agents
    - A2A access token: Scoped access based on token mode and scope
    """
    # Validate agent access
    agent = session.get(Agent, agent_id)
    if not agent:
        return _jsonrpc_error(None, -32001, "Agent not found")
    if not auth.can_access_agent(agent, agent_id):
        return _jsonrpc_error(None, -32004, "Not enough permissions")

    environment = session.get(AgentEnvironment, agent.active_environment_id) if agent.active_environment_id else None
    if not environment:
        return _jsonrpc_error(None, -32002, "Agent has no active environment")

    # Parse JSON-RPC request
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return _jsonrpc_error(None, -32700, "Parse error")

    # Validate JSON-RPC structure
    if not isinstance(body, dict):
        return _jsonrpc_error(None, -32600, "Invalid Request")

    jsonrpc = body.get("jsonrpc")
    if jsonrpc != "2.0":
        return _jsonrpc_error(body.get("id"), -32600, "Invalid Request: jsonrpc must be '2.0'")

    # Check if v1.0 format should be used and transform method names
    use_v1 = A2AV1Adapter.should_use_v1(request)
    if use_v1:
        body = A2AV1Adapter.transform_request_inbound(body)

    method = body.get("method")
    request_id = body.get("id")
    params = body.get("params", {})

    if not method:
        return _jsonrpc_error(request_id, -32600, "Invalid Request: method is required")

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
        get_db_session=get_fresh_db_session,
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
                    return _jsonrpc_error(
                        request_id, -32004,
                        f"Access token does not allow '{requested_mode}' mode"
                    )

            # Return SSE stream
            return StreamingResponse(
                handler.handle_message_stream(params, str(request_id)),
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
                    return _jsonrpc_error(
                        request_id, -32004,
                        f"Access token does not allow '{requested_mode}' mode"
                    )

            # Synchronous message handling
            task = await handler.handle_message_send(params)
            result = task.model_dump(by_alias=True, exclude_none=True)
            if use_v1:
                result = A2AV1Adapter.transform_task_outbound(result)
            return _jsonrpc_success(request_id, result)

        elif method == "tasks/get":
            task = await handler.handle_tasks_get(params)
            if task:
                result = task.model_dump(by_alias=True, exclude_none=True)
                if use_v1:
                    result = A2AV1Adapter.transform_task_outbound(result)
                return _jsonrpc_success(request_id, result)
            else:
                return _jsonrpc_error(request_id, -32001, "Task not found")

        elif method == "tasks/cancel":
            try:
                result = await handler.handle_tasks_cancel(params)
                return _jsonrpc_success(request_id, result)
            except ValueError as e:
                return _jsonrpc_error(request_id, -32001, str(e))

        elif method == "tasks/list":
            # Custom extension to A2A protocol - list tasks for this agent
            tasks = await handler.handle_tasks_list(params)
            task_results = [task.model_dump(by_alias=True, exclude_none=True) for task in tasks]
            if use_v1:
                task_results = [A2AV1Adapter.transform_task_outbound(t) for t in task_results]
            return _jsonrpc_success(request_id, task_results)

        else:
            return _jsonrpc_error(request_id, -32601, f"Method not found: {method}")

    except Exception as e:
        logger.error(f"Error handling A2A request: {e}", exc_info=True)
        return _jsonrpc_error(request_id, -32603, f"Internal error: {str(e)}")


def _jsonrpc_success(request_id: Any, result: Any) -> JSONResponse:
    """Create a JSON-RPC success response."""
    return JSONResponse(content={
        "jsonrpc": "2.0",
        "id": request_id,
        "result": result,
    })


def _jsonrpc_error(request_id: Any, code: int, message: str, data: Any = None) -> JSONResponse:
    """Create a JSON-RPC error response."""
    error = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return JSONResponse(
        content={
            "jsonrpc": "2.0",
            "id": request_id,
            "error": error,
        },
        status_code=200,  # JSON-RPC errors are returned with HTTP 200
    )
