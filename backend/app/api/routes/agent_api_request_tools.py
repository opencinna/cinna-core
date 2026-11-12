"""The producer's capability-request tools, called by env-core (``AgentEnvContextDep``).

- ``POST /agent/api-requests/list {status?, limit?}`` — ``api_requests_list``.
- ``POST /agent/api-requests/get {request_id}`` — ``api_requests_get``.
- ``POST /agent/api-requests/update-status {request_id, status, resolution_note?}``
  — ``api_requests_update_status``.

Scope: capability requests (``kind = agent_api``) whose target is the env's own
agent. Anything else answers ``not_found``; an agent with its REST API turned
off answers ``agent_api_disabled``. All answers are HTTP 200 — the tool's result
or ``{error: {code, message}}`` — the same convention as the dashboard tools.
"""
import logging
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.api.deps import AgentEnvContext, AgentEnvContextDep, SessionDep
from app.models.improvement.agent_improvement_request import (
    IMPROVEMENT_STATUSES,
    AgentApiRequestToolDetail,
    AgentApiRequestToolError,
    AgentApiRequestToolGetRequest,
    AgentApiRequestToolListRequest,
    AgentApiRequestToolListResult,
    AgentApiRequestToolUpdateStatusRequest,
)
from app.services.improvement.improvement_request_service import (
    ImprovementRequestDenied,
    ImprovementRequestService,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agent/api-requests", tags=["agent-api-requests"])

_NO_STORE = {"Cache-Control": "no-store"}


def _tool_error(code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=200,
        content={"error": {"code": code, "message": message}},
        headers=_NO_STORE,
    )


def _ok(payload: Any) -> JSONResponse:
    return JSONResponse(content=payload.model_dump(mode="json"), headers=_NO_STORE)


def _disabled_error(ctx: AgentEnvContext) -> JSONResponse | None:
    if not ctx.agent.agent_api_enabled:
        return _tool_error(
            "agent_api_disabled", "This agent's REST API is not enabled."
        )
    return None


def _not_found() -> JSONResponse:
    return _tool_error("not_found", "Capability request not found.")


def _invalid_status() -> JSONResponse:
    return _tool_error(
        "invalid_input", f"Status must be one of: {', '.join(IMPROVEMENT_STATUSES)}."
    )


@router.post(
    "/list", response_model=AgentApiRequestToolListResult | AgentApiRequestToolError
)
async def agent_api_requests_list(
    body: AgentApiRequestToolListRequest, db: SessionDep, ctx: AgentEnvContextDep
) -> Any:
    if (error := _disabled_error(ctx)) is not None:
        return error
    if body.status and body.status not in IMPROVEMENT_STATUSES:
        return _invalid_status()
    rows, count = ImprovementRequestService.list_agent_api_for_target(
        db, ctx.agent.id, status=body.status, limit=body.limit
    )
    return _ok(AgentApiRequestToolListResult(requests=rows, count=count))


@router.post(
    "/get", response_model=AgentApiRequestToolDetail | AgentApiRequestToolError
)
async def agent_api_requests_get(
    body: AgentApiRequestToolGetRequest, db: SessionDep, ctx: AgentEnvContextDep
) -> Any:
    if (error := _disabled_error(ctx)) is not None:
        return error
    row = ImprovementRequestService.get_agent_api_for_target(
        db, body.request_id, ctx.agent.id
    )
    if row is None:
        return _not_found()
    return _ok(ImprovementRequestService.to_tool_detail(db, row))


@router.post(
    "/update-status",
    response_model=AgentApiRequestToolDetail | AgentApiRequestToolError,
)
async def agent_api_requests_update_status(
    body: AgentApiRequestToolUpdateStatusRequest,
    db: SessionDep,
    ctx: AgentEnvContextDep,
) -> Any:
    if (error := _disabled_error(ctx)) is not None:
        return error
    if body.status not in IMPROVEMENT_STATUSES:
        return _invalid_status()
    row = ImprovementRequestService.get_agent_api_for_target(
        db, body.request_id, ctx.agent.id
    )
    if row is None:
        return _not_found()
    try:
        row = await ImprovementRequestService.update_status(
            db, row, ctx.owner, status=body.status, note=body.resolution_note
        )
    except ImprovementRequestDenied as e:
        # The env's owner is the producer's owner, so this is unreachable in
        # practice; never leak a code the tool contract does not have.
        logger.warning("api_requests update refused: %s", e.reason)
        return _not_found()
    return _ok(ImprovementRequestService.to_tool_detail(db, row))
