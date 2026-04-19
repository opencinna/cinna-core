"""
Agent Status API Routes.

Routes:
  GET  /agents/status                           — list status snapshots (workspace-scoped, cache-only)
  GET  /agents/{agent_id}/status                — fetch or return cached AgentStatusPublic
  POST /internal/environments/{env_id}/status-updated — push notification from agent-env process
"""
import logging
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from sqlmodel import select

from app.api.deps import CurrentUser, SessionDep
from app.models import Agent, AgentStatusPublic, AgentStatusListPublic
from app.models.environments.environment import AgentEnvironment
from app.services.agents.agent_status_service import (
    AgentStatusService,
    AgentStatusSnapshot,
    StatusUnavailableError,
)

logger = logging.getLogger(__name__)

# NOTE: router uses the same prefix as agents.py (/agents) so the /status list
# endpoint must be registered in main.py BEFORE agents.router to avoid FastAPI
# treating "status" as an agent_id UUID in /agents/{id}.
router = APIRouter(prefix="/agents", tags=["agents"])
internal_router = APIRouter(tags=["internal"])


def _snapshot_to_public(
    snapshot: AgentStatusSnapshot, agent_id: uuid.UUID
) -> AgentStatusPublic:
    """Convert an AgentStatusSnapshot dataclass to the public Pydantic model."""
    return AgentStatusPublic(
        agent_id=agent_id,
        environment_id=snapshot.environment_id,
        severity=snapshot.severity,
        summary=snapshot.summary,
        reported_at=snapshot.reported_at,
        reported_at_source=snapshot.reported_at_source,
        fetched_at=snapshot.fetched_at,
        raw=snapshot.raw,
        is_stale=snapshot.is_stale,
        has_structured_metadata=snapshot.has_structured_metadata,
        prev_severity=snapshot.prev_severity,
        severity_changed_at=snapshot.severity_changed_at,
    )


@router.get("/status", response_model=AgentStatusListPublic)
def list_agent_statuses(
    session: SessionDep,
    current_user: CurrentUser,
    workspace_id: uuid.UUID | None = Query(default=None),
) -> Any:
    """
    List cached status snapshots for all agents accessible to the current user.

    Cache-only — does not trigger any container fetches. Safe to poll frequently
    from a dashboard widget. Optionally filter by workspace_id.
    """
    stmt = select(Agent).where(Agent.owner_id == current_user.id)
    if workspace_id is not None:
        stmt = stmt.where(Agent.user_workspace_id == workspace_id)
    agents = session.exec(stmt).all()

    items = []
    for agent in agents:
        environment = AgentStatusService.get_primary_environment(session, agent.id, agent.active_environment_id)
        snapshot = (
            AgentStatusService.get_cached_status(environment)
            if environment
            else AgentStatusService.empty_snapshot(agent.id)
        )
        items.append(_snapshot_to_public(snapshot, agent.id))

    return AgentStatusListPublic(items=items)


@router.get("/{agent_id}/status", response_model=AgentStatusPublic)
async def get_agent_status(
    agent_id: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
    force_refresh: bool = Query(default=False),
) -> Any:
    """
    Get the agent's self-reported status snapshot.

    Returns the cached DB snapshot by default. Set force_refresh=true to
    re-fetch STATUS.md from the running environment (subject to a 30-second
    rate limit per environment — returns 429 when the limit is active).
    """
    agent = session.get(Agent, agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if not current_user.is_superuser and agent.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not enough permissions")

    environment = AgentStatusService.get_primary_environment(session, agent_id, agent.active_environment_id)
    if not environment:
        return AgentStatusPublic(agent_id=agent_id, is_stale=True)

    if force_refresh:
        if AgentStatusService.is_rate_limited(environment.id):
            raise HTTPException(
                status_code=429,
                detail="Status refresh rate limit exceeded. Try again in 30 seconds.",
                headers={"Retry-After": "30"},
            )
        try:
            snapshot = await AgentStatusService.fetch_status(environment)
            return _snapshot_to_public(snapshot, agent_id)
        except StatusUnavailableError:
            # Env not running or file missing — fall back to cache
            snapshot = AgentStatusService.get_cached_status(environment)
            return _snapshot_to_public(snapshot, agent_id)

    snapshot = AgentStatusService.get_cached_status(environment)
    return _snapshot_to_public(snapshot, agent_id)


@internal_router.post("/internal/environments/{env_id}/status-updated")
async def environment_status_updated(
    env_id: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
) -> Any:
    """
    Push notification from the agent-env process: STATUS.md has been rewritten.

    Called by the mtime watcher running inside the agent container. Uses the
    same Bearer JWT auth as other agent-env → backend endpoints. Rate-limited
    to one fetch per environment per 30 seconds.

    Returns {"ok": true, "fetched": bool} — fetched=false when rate-limited.
    """
    environment = session.get(AgentEnvironment, env_id)
    if not environment:
        raise HTTPException(status_code=404, detail="Environment not found")

    agent = session.get(Agent, environment.agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if not current_user.is_superuser and agent.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not enough permissions")

    if AgentStatusService.is_rate_limited(environment.id):
        return {"ok": True, "fetched": False}

    try:
        await AgentStatusService.fetch_status(environment)
        return {"ok": True, "fetched": True}
    except StatusUnavailableError:
        return {"ok": True, "fetched": False}
    except Exception as exc:
        logger.warning("Error fetching status for env %s: %s", env_id, exc)
        return {"ok": True, "fetched": False}
