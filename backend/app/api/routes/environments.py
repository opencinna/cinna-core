import uuid
from typing import Any

from fastapi import APIRouter, HTTPException

from app.api.deps import CurrentUser, SessionDep
from app.models import (
    AgentEnvironment,
    AgentEnvironmentUpdate,
    AgentEnvironmentPublic,
    Message,
    Agent,
)
from app.services.environment_service import EnvironmentService

router = APIRouter(prefix="/environments", tags=["environments"])


@router.get("/{id}", response_model=AgentEnvironmentPublic)
def get_environment(
    session: SessionDep, current_user: CurrentUser, id: uuid.UUID
) -> Any:
    """
    Get environment details.
    """
    environment = session.get(AgentEnvironment, id)
    if not environment:
        raise HTTPException(status_code=404, detail="Environment not found")

    # Check permission: user must own the agent
    agent = session.get(Agent, environment.agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if not current_user.is_superuser and (agent.owner_id != current_user.id):
        raise HTTPException(status_code=400, detail="Not enough permissions")

    return environment


@router.patch("/{id}", response_model=AgentEnvironmentPublic)
def update_environment(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    id: uuid.UUID,
    environment_in: AgentEnvironmentUpdate,
) -> Any:
    """
    Update environment config.
    """
    environment = session.get(AgentEnvironment, id)
    if not environment:
        raise HTTPException(status_code=404, detail="Environment not found")

    # Check permission: user must own the agent
    agent = session.get(Agent, environment.agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if not current_user.is_superuser and (agent.owner_id != current_user.id):
        raise HTTPException(status_code=400, detail="Not enough permissions")

    updated_environment = EnvironmentService.update_environment(
        session=session, env_id=id, data=environment_in
    )
    return updated_environment


@router.delete("/{id}")
def delete_environment(
    session: SessionDep, current_user: CurrentUser, id: uuid.UUID
) -> Message:
    """
    Delete environment.
    """
    environment = session.get(AgentEnvironment, id)
    if not environment:
        raise HTTPException(status_code=404, detail="Environment not found")

    # Check permission: user must own the agent
    agent = session.get(Agent, environment.agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if not current_user.is_superuser and (agent.owner_id != current_user.id):
        raise HTTPException(status_code=400, detail="Not enough permissions")

    EnvironmentService.delete_environment(session=session, env_id=id)
    return Message(message="Environment deleted successfully")


# Lifecycle endpoints (stub for now)
@router.post("/{id}/start")
def start_environment(
    session: SessionDep, current_user: CurrentUser, id: uuid.UUID
) -> Message:
    """
    Start environment (stub).
    """
    environment = session.get(AgentEnvironment, id)
    if not environment:
        raise HTTPException(status_code=404, detail="Environment not found")

    # Check permission
    agent = session.get(Agent, environment.agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if not current_user.is_superuser and (agent.owner_id != current_user.id):
        raise HTTPException(status_code=400, detail="Not enough permissions")

    raise HTTPException(status_code=501, detail="Not implemented in Step 1")


@router.post("/{id}/stop")
def stop_environment(
    session: SessionDep, current_user: CurrentUser, id: uuid.UUID
) -> Message:
    """
    Stop environment (stub).
    """
    environment = session.get(AgentEnvironment, id)
    if not environment:
        raise HTTPException(status_code=404, detail="Environment not found")

    # Check permission
    agent = session.get(Agent, environment.agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if not current_user.is_superuser and (agent.owner_id != current_user.id):
        raise HTTPException(status_code=400, detail="Not enough permissions")

    raise HTTPException(status_code=501, detail="Not implemented in Step 1")
