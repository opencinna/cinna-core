"""Agent Skills API routes.

Routes:
  GET  /agents/{agent_id}/skills                  — cached skill index
  POST /agents/{agent_id}/skills/refresh          — wake + re-read, same shape
  GET  /agents/{agent_id}/skills/{name}/content   — one skill's SKILL.md text

All three are owner-scoped. Reads are cache-first by design: the index is
env-authoritative but the environment may be asleep, and a card that renders
the last known skills beats one that blocks on a container start.
"""
import logging
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException

from app.api.deps import CurrentUser, SessionDep
from app.models import (
    Agent,
    AgentSkillsPublic,
    SkillContentPublic,
    SkillEntryPublic,
    SkillIssuePublic,
)
from app.services.agents.agent_skills_service import (
    AgentSkillsService,
    SkillsIndexUnavailableError,
)
from app.services.agents.agent_status_service import AgentStatusService
from app.services.agents.skill_manifest import SkillEntry

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agents", tags=["agents"])


def _issue_to_public(issue) -> SkillIssuePublic | None:
    if issue is None:
        return None
    return SkillIssuePublic(
        code=issue.code, message=issue.message, paths=list(issue.paths)
    )


def _entry_to_public(entry: SkillEntry, *, can_publish: bool) -> SkillEntryPublic:
    """Project one cached entry, resolving its per-entry publish capability.

    ``can_publish`` here is the agent-level capability; the entry adds the
    condition that the skill itself is publishable — clean, and locally owned
    (a plugin's skill belongs to the plugin's publisher, not to this agent).
    """
    return SkillEntryPublic(
        name=entry.name,
        description=entry.description,
        source=entry.source,
        plugin_ref=entry.plugin_ref,
        path=entry.path,
        has_scripts=entry.has_scripts,
        user_invocable=entry.user_invocable,
        model_invocable=entry.model_invocable,
        size_bytes=entry.size_bytes,
        error=_issue_to_public(entry.error),
        warning=_issue_to_public(entry.warning),
        secret_paths=list(entry.secret_paths),
        can_publish=(
            can_publish and entry.source == "local" and entry.is_publishable
        ),
    )


def _get_owned_agent(session: SessionDep, agent_id: uuid.UUID, user) -> Agent:
    """Load an agent the caller may see, or raise the usual 404/403.

    Access goes through ``AgentService.user_can_access`` — the same predicate
    ``can_build`` folds in below — so the read gate and the capability reply
    can never disagree about who this agent belongs to.
    """
    from app.services.agents.agent_service import AgentService

    agent = session.get(Agent, agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if not user.is_superuser and not AgentService.user_can_access(
        session, user, agent
    ):
        raise HTTPException(status_code=403, detail="Not enough permissions")
    return agent


def _build_response(
    session, agent: Agent, user, environment
) -> AgentSkillsPublic:
    """Marshal the cached index for one agent."""
    from app.services.agents.agent_service import AgentService

    # The publish capability is a server answer, not a client role check: it is
    # the developer role AND an agent that is not a consumer install, and only
    # the server can see the second half.
    can_publish = AgentService.can_build(session, user, agent)

    if environment is None:
        return AgentSkillsPublic(agent_id=agent.id, can_publish=can_publish)

    entries = AgentSkillsService.get_cached_entries(environment)
    return AgentSkillsPublic(
        agent_id=agent.id,
        environment_id=environment.id,
        skills=[_entry_to_public(e, can_publish=can_publish) for e in entries],
        hash=environment.skills_hash,
        fetched_at=environment.skills_fetched_at,
        error=environment.skills_error,
        can_publish=can_publish,
    )


@router.get("/{agent_id}/skills", response_model=AgentSkillsPublic)
def get_agent_skills(
    agent_id: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
) -> Any:
    """Return the agent's cached skill index.

    Cache-only — safe to poll, never wakes a container. Use the refresh route
    when the caller wants current truth.
    """
    agent = _get_owned_agent(session, agent_id, current_user)
    environment = AgentStatusService.get_primary_environment(
        session, agent_id, agent.active_environment_id
    )
    return _build_response(session, agent, current_user, environment)


@router.post("/{agent_id}/skills/refresh", response_model=AgentSkillsPublic)
async def refresh_agent_skills(
    agent_id: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
) -> Any:
    """Re-read the skill index from the environment, waking it if suspended.

    Never fails on an unreachable environment: the response carries the cached
    rows plus an ``error`` code saying why they may be stale, which is what the
    card renders as a banner. The 30 s rate limit does not apply — this is a
    user asking, not a background sweep guessing.
    """
    agent = _get_owned_agent(session, agent_id, current_user)
    environment = AgentStatusService.get_primary_environment(
        session, agent_id, agent.active_environment_id
    )
    if environment is not None:
        await AgentSkillsService.force_refresh(
            environment, agent=agent, db_session=session
        )
        session.refresh(environment)
    return _build_response(session, agent, current_user, environment)


@router.get("/{agent_id}/skills/{name}/content", response_model=SkillContentPublic)
async def get_agent_skill_content(
    agent_id: uuid.UUID,
    name: str,
    session: SessionDep,
    current_user: CurrentUser,
) -> Any:
    """Return one skill's ``SKILL.md`` — the text the model reads.

    Reading is ``AgentSkillsService.read_skill_content``; this route only maps
    its outcomes onto status codes: no such skill (or no file behind it) is a
    404, an unreachable environment a 503.
    """
    agent = _get_owned_agent(session, agent_id, current_user)
    environment = AgentStatusService.get_primary_environment(
        session, agent_id, agent.active_environment_id
    )
    if environment is None:
        raise HTTPException(status_code=404, detail="This agent has no environment")

    try:
        result = await AgentSkillsService.read_skill_content(environment, name)
    except SkillsIndexUnavailableError as exc:
        logger.info(
            "skill_content_unavailable agent_id=%s skill=%s reason=%s",
            agent_id, name, exc,
        )
        raise HTTPException(
            status_code=503,
            detail="The environment is unavailable — start it and try again.",
        )

    if result is None:
        # Two different 404s: the skill is not in the index at all, or the
        # index still lists it and the file is gone. The second one tells the
        # user their view is stale, which is a different next step.
        known = AgentSkillsService.find_cached_skill(environment, name) is not None
        raise HTTPException(
            status_code=404,
            detail=(
                "SKILL.md not found — refresh the skills list."
                if known
                else "Skill not found"
            ),
        )

    path, content, truncated = result
    return SkillContentPublic(
        agent_id=agent_id,
        name=name,
        path=path,
        content=content,
        truncated=truncated,
    )
