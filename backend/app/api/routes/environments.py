import uuid
from typing import Any

from fastapi import APIRouter, HTTPException

from app.api.deps import CurrentUser, SessionDep
from app.models import (
    AgentEnvironmentUpdate,
    AgentEnvironmentPublic,
    Message,
)
from app.services.environments.environment_service import (
    EnvironmentService,
    AgentEnvironmentError,
)

router = APIRouter(prefix="/environments", tags=["environments"])


def _handle_service_error(e: AgentEnvironmentError) -> None:
    raise HTTPException(status_code=e.status_code, detail=e.message)


@router.get("/{id}", response_model=AgentEnvironmentPublic)
def get_environment(
    session: SessionDep, current_user: CurrentUser, id: uuid.UUID
) -> Any:
    """
    Get environment details.
    """
    try:
        environment, _ = EnvironmentService.get_environment_with_access_check(
            session, id, current_user.id, current_user.is_superuser
        )
        return environment
    except AgentEnvironmentError as e:
        _handle_service_error(e)


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
    try:
        EnvironmentService.get_environment_with_access_check(
            session, id, current_user.id, current_user.is_superuser
        )
        updated_environment = EnvironmentService.update_environment(
            session=session, env_id=id, data=environment_in
        )
        return updated_environment
    except AgentEnvironmentError as e:
        _handle_service_error(e)


@router.delete("/{id}")
async def delete_environment(
    session: SessionDep, current_user: CurrentUser, id: uuid.UUID
) -> Message:
    """
    Delete environment.
    """
    try:
        EnvironmentService.get_environment_with_access_check(
            session, id, current_user.id, current_user.is_superuser
        )
        await EnvironmentService.delete_environment(session=session, env_id=id)
        return Message(message="Environment deleted successfully")
    except AgentEnvironmentError as e:
        _handle_service_error(e)
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
    try:
        EnvironmentService.get_environment_with_access_check(
            session, id, current_user.id, current_user.is_superuser
        )
        await EnvironmentService.start_environment(session=session, env_id=id)
        return Message(message="Environment started successfully")
    except AgentEnvironmentError as e:
        _handle_service_error(e)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to start environment: {str(e)}")


@router.post("/{id}/stop")
async def stop_environment(
    session: SessionDep, current_user: CurrentUser, id: uuid.UUID
) -> Message:
    """
    Stop environment.
    """
    try:
        EnvironmentService.get_environment_with_access_check(
            session, id, current_user.id, current_user.is_superuser
        )
        await EnvironmentService.stop_environment(session=session, env_id=id)
        return Message(message="Environment stopped successfully")
    except AgentEnvironmentError as e:
        _handle_service_error(e)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to stop environment: {str(e)}")


@router.post("/{id}/suspend")
async def suspend_environment(
    session: SessionDep, current_user: CurrentUser, id: uuid.UUID
) -> Message:
    """
    Suspend environment to save resources.

    Stops the container and sets status to 'suspended' instead of 'stopped',
    indicating it can be quickly reactivated when needed.
    """
    try:
        EnvironmentService.get_environment_with_access_check(
            session, id, current_user.id, current_user.is_superuser
        )
        await EnvironmentService.suspend_environment(session=session, env_id=id)
        return Message(message="Environment suspended successfully")
    except AgentEnvironmentError as e:
        _handle_service_error(e)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to suspend environment: {str(e)}")


@router.post("/{id}/restart")
async def restart_environment(
    session: SessionDep, current_user: CurrentUser, id: uuid.UUID
) -> Message:
    """
    Restart environment.
    """
    try:
        EnvironmentService.get_environment_with_access_check(
            session, id, current_user.id, current_user.is_superuser
        )
        await EnvironmentService.restart_environment(session=session, env_id=id)
        return Message(message="Environment restarted successfully")
    except AgentEnvironmentError as e:
        _handle_service_error(e)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to restart environment: {str(e)}")


@router.post("/{id}/rebuild")
async def rebuild_environment(
    session: SessionDep, current_user: CurrentUser, id: uuid.UUID
) -> Message:
    """
    Rebuild environment with updated core files while preserving workspace data.

    This operation:
    - Stops the container if running
    - Updates core system files from template
    - Rebuilds Docker image
    - Restarts container if it was running before
    - Preserves workspace data (scripts, files, docs, credentials, databases)
    """
    try:
        EnvironmentService.get_environment_with_access_check(
            session, id, current_user.id, current_user.is_superuser
        )
        await EnvironmentService.rebuild_environment(session=session, env_id=id)
        return Message(message="Environment rebuilt successfully")
    except AgentEnvironmentError as e:
        _handle_service_error(e)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to rebuild environment: {str(e)}")


@router.get("/{id}/status")
async def get_environment_status(
    session: SessionDep, current_user: CurrentUser, id: uuid.UUID
) -> dict:
    """
    Get environment status.
    """
    try:
        EnvironmentService.get_environment_with_access_check(
            session, id, current_user.id, current_user.is_superuser
        )
        status_data = await EnvironmentService.get_environment_status(session=session, env_id=id)
        return status_data
    except AgentEnvironmentError as e:
        _handle_service_error(e)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get status: {str(e)}")


@router.get("/{id}/health")
async def check_environment_health(
    session: SessionDep, current_user: CurrentUser, id: uuid.UUID
) -> dict:
    """
    Check environment health.
    """
    try:
        EnvironmentService.get_environment_with_access_check(
            session, id, current_user.id, current_user.is_superuser
        )
        health = await EnvironmentService.check_environment_health(session=session, env_id=id)
        return health
    except AgentEnvironmentError as e:
        _handle_service_error(e)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to check health: {str(e)}")


@router.get("/{id}/logs")
async def get_environment_logs(
    session: SessionDep, current_user: CurrentUser, id: uuid.UUID, lines: int = 100
) -> dict:
    """
    Get environment logs.
    """
    try:
        EnvironmentService.get_environment_with_access_check(
            session, id, current_user.id, current_user.is_superuser
        )
        logs = await EnvironmentService.get_environment_logs(session=session, env_id=id, lines=lines)
        return {"logs": logs}
    except AgentEnvironmentError as e:
        _handle_service_error(e)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get logs: {str(e)}")
