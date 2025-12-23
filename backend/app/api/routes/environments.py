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
async def delete_environment(
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

    try:
        await EnvironmentService.delete_environment(session=session, env_id=id)
        return Message(message="Environment deleted successfully")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete environment: {str(e)}")


# Lifecycle endpoints
@router.post("/{id}/start")
async def start_environment(
    session: SessionDep, current_user: CurrentUser, id: uuid.UUID
) -> Message:
    """
    Start environment.
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

    # Start environment
    try:
        await EnvironmentService.start_environment(session=session, env_id=id)
        return Message(message="Environment started successfully")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to start environment: {str(e)}")


@router.post("/{id}/stop")
async def stop_environment(
    session: SessionDep, current_user: CurrentUser, id: uuid.UUID
) -> Message:
    """
    Stop environment.
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

    # Stop environment
    try:
        await EnvironmentService.stop_environment(session=session, env_id=id)
        return Message(message="Environment stopped successfully")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to stop environment: {str(e)}")


@router.post("/{id}/restart")
async def restart_environment(
    session: SessionDep, current_user: CurrentUser, id: uuid.UUID
) -> Message:
    """
    Restart environment.
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

    # Restart environment
    try:
        await EnvironmentService.restart_environment(session=session, env_id=id)
        return Message(message="Environment restarted successfully")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to restart environment: {str(e)}")


@router.get("/{id}/status")
async def get_environment_status(
    session: SessionDep, current_user: CurrentUser, id: uuid.UUID
) -> dict:
    """
    Get environment status.
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

    # Get status
    try:
        status_data = await EnvironmentService.get_environment_status(session=session, env_id=id)
        return status_data
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get status: {str(e)}")


@router.get("/{id}/health")
async def check_environment_health(
    session: SessionDep, current_user: CurrentUser, id: uuid.UUID
) -> dict:
    """
    Check environment health.
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

    # Check health
    try:
        health = await EnvironmentService.check_environment_health(session=session, env_id=id)
        return health
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to check health: {str(e)}")


@router.get("/{id}/logs")
async def get_environment_logs(
    session: SessionDep, current_user: CurrentUser, id: uuid.UUID, lines: int = 100
) -> dict:
    """
    Get environment logs.
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

    # Get logs
    try:
        logs = await EnvironmentService.get_environment_logs(session=session, env_id=id, lines=lines)
        return {"logs": logs}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get logs: {str(e)}")
