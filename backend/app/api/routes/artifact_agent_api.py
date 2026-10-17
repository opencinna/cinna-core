"""Publishing artifacts out of an agent environment's workspace.

Two routers over :class:`ArtifactSourceService`:

* ``router`` — ``POST /agent/artifacts/publish``, called by the
  ``artifacts_publish`` tool on the environment's ``cinna`` MCP server. Auth is
  the scoped agent-environment token (``AgentEnvContextDep``: env token +
  ``X-Agent-Env-Id``); everything is scoped to that env's agent and owner.
* ``environment_router`` — ``POST /artifacts/from-environment``, the owner's
  "Publish as artifact" in the workspace file view (user JWT, agent owner only,
  D20).

Both create private ``core_agent`` artifacts and republish by agent + path
(A12). Sync routes (threadpool): only the workspace pull hops to the event loop. No
visibility, grant, link or delete operation is reachable here (A8); the request
models forbid extra fields.
"""
from typing import Any

from fastapi import APIRouter

from app.api.deps import AgentEnvContextDep, CurrentUser, SessionDep
from app.api.routes.artifacts import artifact_errors
from app.models.artifacts.artifact import ArtifactPublished
from app.models.artifacts.artifact_source import (
    AgentArtifactPublished,
    AgentArtifactPublishRequest,
    ArtifactFromEnvironmentRequest,
)
from app.services.artifacts.artifact_source_service import ArtifactSourceService

router = APIRouter(prefix="/agent/artifacts", tags=["agent-artifacts"])
environment_router = APIRouter(prefix="/artifacts", tags=["artifacts"])


@router.post("/publish", response_model=AgentArtifactPublished)
def agent_publish_artifact(
    data: AgentArtifactPublishRequest, db: SessionDep, ctx: AgentEnvContextDep,
) -> Any:
    """Publish a workspace file or folder as a private artifact (``artifacts_publish``).

    The same ``path`` again adds a revision to the same artifact. 404 for an
    ``artifact_id`` this agent did not publish; 409 when the environment is
    not running; 413 past a size limit.
    """
    with artifact_errors():
        return ArtifactSourceService.publish_from_agent(
            db, owner=ctx.owner, agent=ctx.agent, environment=ctx.environment, data=data,
        )


@environment_router.post("/from-environment", response_model=ArtifactPublished)
def publish_artifact_from_environment(
    data: ArtifactFromEnvironmentRequest, db: SessionDep, current_user: CurrentUser,
) -> Any:
    """Publish a file or folder from an agent environment's workspace (agent owner only).

    Same rules as the agent's ``artifacts_publish``: private, ``core_agent``,
    republished when the same environment path was published before. The
    response carries ``excluded``. 404 for anyone but the agent's owner (and
    for an unknown environment); 409 when the environment is not running.
    """
    with artifact_errors():
        return ArtifactSourceService.publish_from_environment(db, current_user, data)
