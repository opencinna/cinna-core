"""
Security Events API — ingest and retrieval of credential access and output
redaction events from agent environments.

Authentication:
- POST endpoints use AGENT_AUTH_TOKEN (agent environment's JWT) which resolves
  to the owning user via the standard CurrentUser dependency.
- GET endpoint uses the user's own JWT for frontend audit access.
"""
import uuid
import logging
from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import BaseModel

from app.api.deps import SessionDep, CurrentUser
from app.models.events.security_event import (
    SecurityEventCreate,
    SecurityEventPublic,
    SecurityEventsPublic,
)
from app.services.events.security_event_service import SecurityEventService

router = APIRouter(prefix="/security-events", tags=["security-events"])
logger = logging.getLogger(__name__)


# ── Request / Response models for the blockable report endpoint ──────────────

class SecurityEventReport(BaseModel):
    """
    Event payload sent by SDK interceptors for blockable event reporting.
    The environment server proxies this from the hook script to the backend.
    """
    event_type: str                    # e.g. "CREDENTIAL_READ_ATTEMPT"
    tool_name: str | None = None       # "Read", "Bash", "Edit"
    tool_input: str | None = None      # file path or command string
    session_id: str | None = None      # backend session UUID (string form)
    environment_id: str | None = None  # environment UUID (string form)
    agent_id: str | None = None        # agent UUID (string form)
    severity: str = "high"
    details: dict = {}


class SecurityEventReportResponse(BaseModel):
    """
    Response returned to the SDK interceptor. The `action` field determines
    whether the tool call should proceed or be blocked.
    """
    action: str = "allow"   # "allow" | "block"
    reason: str | None = None


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/report")
async def report_security_event(
    event_data: SecurityEventReport,
    session: SessionDep,
    current_user: CurrentUser,
) -> SecurityEventReportResponse:
    """
    Blockable security event ingest.

    Logs the event and returns an action decision. Called synchronously by SDK
    hooks — must respond quickly (hook has a 3-second timeout).

    The `action` field is the hook point for future policy logic. Currently
    always returns "allow". When policy evaluation is added, "block" can be
    returned here without any SDK-side changes.

    Auth: AGENT_AUTH_TOKEN (agent environment JWT resolves to owning user)
    """
    await SecurityEventService.create_event_from_report(
        session=session,
        user_id=current_user.id,
        event_type=event_data.event_type,
        severity=event_data.severity,
        details=event_data.details,
        tool_name=event_data.tool_name,
        tool_input=event_data.tool_input,
        environment_id=event_data.environment_id,
        session_id=event_data.session_id,
        agent_id=event_data.agent_id,
    )

    # Policy hook point — always allow for now.
    # Future: plug in policy engine here (risk scoring, guest session rules, etc.)
    return SecurityEventReportResponse(action="allow", reason=None)


@router.post("/")
async def ingest_security_event(
    event_data: SecurityEventCreate,
    session: SessionDep,
    current_user: CurrentUser,
) -> SecurityEventPublic:
    """
    Fire-and-forget security event ingest (non-blockable).

    Used for informational events like OUTPUT_REDACTED where the caller does
    not wait for the response (asyncio.create_task pattern).

    Auth: AGENT_AUTH_TOKEN (agent environment JWT resolves to owning user)
    """
    event = await SecurityEventService.create_event(
        session=session,
        user_id=current_user.id,
        data=event_data,
    )
    return SecurityEventService.to_public(event)


@router.get("/")
async def list_security_events(
    session: SessionDep,
    current_user: CurrentUser,
    agent_id: Annotated[uuid.UUID | None, Query()] = None,
    environment_id: Annotated[uuid.UUID | None, Query()] = None,
    session_id: Annotated[uuid.UUID | None, Query()] = None,
    event_type: Annotated[str | None, Query()] = None,
    skip: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> SecurityEventsPublic:
    """
    List security events for the current user.

    Results are paginated (default 50, max 200) and ordered newest first.
    Optional filters: agent_id, environment_id, session_id, event_type.

    Auth: User JWT (for frontend audit view)
    """
    events, count = await SecurityEventService.list_events(
        session=session,
        user_id=current_user.id,
        agent_id=agent_id,
        environment_id=environment_id,
        session_id_filter=session_id,
        event_type=event_type,
        skip=skip,
        limit=limit,
    )
    return SecurityEventsPublic(
        data=[SecurityEventService.to_public(e) for e in events],
        count=count,
    )
