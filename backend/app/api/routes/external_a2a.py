"""
External A2A API routes.

Exposes three target types under ``/api/v1/external/a2a/``:

  ``/a2a/agent/{agent_id}/``        — personal agents (owner-only)
  ``/a2a/route/{route_id}/``        — MCP Shared Agent routes
  ``/a2a/identity/{owner_id}/``     — identity contacts (per-caller)

Each target exposes:

  GET  ``{base}/``                                  — A2A AgentCard
  GET  ``{base}/.well-known/agent-card.json``       — well-known mirror
  POST ``{base}/``                                  — JSON-RPC endpoint

All endpoints accept ``?protocol=v1.0`` (default) or ``?protocol=v0.3``.
Authentication is the standard user JWT via ``CurrentUser`` (no A2A access
tokens) — access policy lives in ``ExternalAccessPolicy`` and domain
exceptions are raised from the service layer.
"""
import logging
import uuid
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlmodel import Session as DbSession

from app.api.deps import CurrentClientClaims, CurrentUser, SessionDep
from app.core.db import create_session
from app.services.a2a.jsonrpc_utils import resolve_protocol
from app.services.external.errors import (
    ExternalAccessError,
    InvalidExternalParamsError,
)
from app.services.external.external_a2a_request_handler import (
    ExternalA2ARequestHandler,
)
from app.services.external.external_a2a_service import ExternalA2AService
from app.utils import get_base_url

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/external/a2a", tags=["external-a2a"])


def _card_response(
    *,
    db: DbSession,
    user: Any,
    target_type: str,
    target_id: uuid.UUID,
    protocol: Optional[str],
    request: Request,
) -> JSONResponse:
    """Build the AgentCard JSON response for any target type.

    Raises:
        HTTPException: 400 for invalid protocol / params, 404 for access
            violations or missing targets.
    """
    try:
        resolved = resolve_protocol(protocol)
    except InvalidExternalParamsError as e:
        raise HTTPException(status_code=400, detail=e.message)

    try:
        card_dict = ExternalA2AService.build_card(
            db=db,
            user=user,
            target_type=target_type,
            target_id=target_id,
            request_base_url=get_base_url(request),
            protocol=resolved,
        )
    except ExternalAccessError as e:
        raise HTTPException(status_code=404, detail=e.message)
    except InvalidExternalParamsError as e:
        raise HTTPException(status_code=400, detail=e.message)
    return JSONResponse(content=card_dict)


async def _jsonrpc_response(
    *,
    target_type: str,
    target_id: uuid.UUID,
    request: Request,
    session: DbSession,
    current_user: Any,
    client_claims: tuple[str | None, str | None],
    protocol: Optional[str],
) -> JSONResponse | StreamingResponse:
    """Parse, dispatch, and render a JSON-RPC request for any target type."""
    client_kind, external_client_id = client_claims
    handler = ExternalA2ARequestHandler(
        get_db_session=create_session,
        backend_base_url=get_base_url(request),
    )
    raw_body = await request.body()
    outcome = await handler.handle_request(
        db=session,
        user=current_user,
        target_type=target_type,
        target_id=target_id,
        raw_body=raw_body,
        protocol_param=protocol,
        client_kind=client_kind,
        external_client_id=external_client_id,
    )
    if outcome.is_stream:
        return StreamingResponse(
            outcome.stream,
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
    return JSONResponse(content=outcome.result_envelope)


_PROTOCOL_QUERY = Query(
    None,
    description="Protocol version: 'v1.0' (default) or 'v0.3'",
)


# ---------------------------------------------------------------------------
# Agent target (personal agents, owner-only)
# ---------------------------------------------------------------------------


@router.get("/agent/{agent_id}/")
async def get_external_agent_card(
    agent_id: uuid.UUID,
    session: SessionDep,
    request: Request,
    current_user: CurrentUser,
    protocol: Optional[str] = _PROTOCOL_QUERY,
) -> JSONResponse:
    """Return the AgentCard for the authenticated user's own agent."""
    return _card_response(
        db=session, user=current_user,
        target_type="agent", target_id=agent_id,
        protocol=protocol, request=request,
    )


@router.get("/agent/{agent_id}/.well-known/agent-card.json")
async def get_external_agent_card_well_known(
    agent_id: uuid.UUID,
    session: SessionDep,
    request: Request,
    current_user: CurrentUser,
    protocol: Optional[str] = _PROTOCOL_QUERY,
) -> JSONResponse:
    """Well-known mirror of the agent AgentCard endpoint."""
    return _card_response(
        db=session, user=current_user,
        target_type="agent", target_id=agent_id,
        protocol=protocol, request=request,
    )


@router.post("/agent/{agent_id}/")
async def handle_external_agent_jsonrpc(
    agent_id: uuid.UUID,
    request: Request,
    session: SessionDep,
    current_user: CurrentUser,
    client_claims: CurrentClientClaims,
    protocol: Optional[str] = _PROTOCOL_QUERY,
) -> Any:
    """Handle JSON-RPC requests for the authenticated user's own agent."""
    return await _jsonrpc_response(
        target_type="agent", target_id=agent_id,
        request=request, session=session, current_user=current_user,
        client_claims=client_claims, protocol=protocol,
    )


# ---------------------------------------------------------------------------
# App MCP route target (shared agents)
# ---------------------------------------------------------------------------


@router.get("/route/{route_id}/")
async def get_external_route_card(
    route_id: uuid.UUID,
    session: SessionDep,
    request: Request,
    current_user: CurrentUser,
    protocol: Optional[str] = _PROTOCOL_QUERY,
) -> JSONResponse:
    """Return the AgentCard for a MCP Shared Agent route."""
    return _card_response(
        db=session, user=current_user,
        target_type="app_mcp_route", target_id=route_id,
        protocol=protocol, request=request,
    )


@router.get("/route/{route_id}/.well-known/agent-card.json")
async def get_external_route_card_well_known(
    route_id: uuid.UUID,
    session: SessionDep,
    request: Request,
    current_user: CurrentUser,
    protocol: Optional[str] = _PROTOCOL_QUERY,
) -> JSONResponse:
    """Well-known mirror of the route AgentCard endpoint."""
    return _card_response(
        db=session, user=current_user,
        target_type="app_mcp_route", target_id=route_id,
        protocol=protocol, request=request,
    )


@router.post("/route/{route_id}/")
async def handle_external_route_jsonrpc(
    route_id: uuid.UUID,
    request: Request,
    session: SessionDep,
    current_user: CurrentUser,
    client_claims: CurrentClientClaims,
    protocol: Optional[str] = _PROTOCOL_QUERY,
) -> Any:
    """Handle JSON-RPC requests for a MCP Shared Agent route."""
    return await _jsonrpc_response(
        target_type="app_mcp_route", target_id=route_id,
        request=request, session=session, current_user=current_user,
        client_claims=client_claims, protocol=protocol,
    )


# ---------------------------------------------------------------------------
# Identity target (person-level, per-caller bindings)
# ---------------------------------------------------------------------------


@router.get("/identity/{owner_id}/")
async def get_external_identity_card(
    owner_id: uuid.UUID,
    session: SessionDep,
    request: Request,
    current_user: CurrentUser,
    protocol: Optional[str] = _PROTOCOL_QUERY,
) -> JSONResponse:
    """Return the AgentCard for an identity contact (a person)."""
    return _card_response(
        db=session, user=current_user,
        target_type="identity", target_id=owner_id,
        protocol=protocol, request=request,
    )


@router.get("/identity/{owner_id}/.well-known/agent-card.json")
async def get_external_identity_card_well_known(
    owner_id: uuid.UUID,
    session: SessionDep,
    request: Request,
    current_user: CurrentUser,
    protocol: Optional[str] = _PROTOCOL_QUERY,
) -> JSONResponse:
    """Well-known mirror of the identity AgentCard endpoint."""
    return _card_response(
        db=session, user=current_user,
        target_type="identity", target_id=owner_id,
        protocol=protocol, request=request,
    )


@router.post("/identity/{owner_id}/")
async def handle_external_identity_jsonrpc(
    owner_id: uuid.UUID,
    request: Request,
    session: SessionDep,
    current_user: CurrentUser,
    client_claims: CurrentClientClaims,
    protocol: Optional[str] = _PROTOCOL_QUERY,
) -> Any:
    """Handle JSON-RPC requests for an identity contact."""
    return await _jsonrpc_response(
        target_type="identity", target_id=owner_id,
        request=request, session=session, current_user=current_user,
        client_claims=client_claims, protocol=protocol,
    )
