import logging
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, status

from app.api.deps import CurrentUser, SessionDep
from app.models import (
    Agent,
    AgentEnvironment,
    AgentEnvironmentUpdate,
    AgentEnvironmentPublic,
    Message,
)
from app.services.environments.environment_service import (
    EnvironmentService,
    AgentEnvironmentError,
)

router = APIRouter(prefix="/environments", tags=["environments"])
logger = logging.getLogger(__name__)


async def _verify_env_agent_auth(
    session: SessionDep,
    authorization: Annotated[str | None, Header()] = None,
    x_agent_env_id: Annotated[str | None, Header()] = None,
):
    """
    Verify an inbound request from an agent environment container.

    Validates that the Authorization bearer token matches the environment's
    stored AGENT_AUTH_TOKEN and the X-Agent-Env-Id identifies a live environment.
    Used by env→backend callback endpoints.
    """
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header",
        )
    if not x_agent_env_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-Agent-Env-Id header",
        )

    try:
        scheme, token = authorization.split(" ", 1)
        if scheme.lower() != "bearer":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid authentication scheme",
            )
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Authorization header format",
        )

    try:
        env_id = uuid.UUID(x_agent_env_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid X-Agent-Env-Id format",
        )

    env = session.get(AgentEnvironment, env_id)
    if not env:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid environment ID",
        )

    stored_token = env.config.get("auth_token") if env.config else None
    if not stored_token or token != stored_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid agent auth token",
        )

    return env


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


# ── Env-core callback endpoints ──────────────────────────────────────────────
# Called by agent environment containers over the internal Docker network.
# Auth: Authorization: Bearer {AGENT_AUTH_TOKEN} + X-Agent-Env-Id: {env_id}

_EnvFromAgentAuth = Annotated[AgentEnvironment, Depends(_verify_env_agent_auth)]


@router.post("/{id}/prompt-file-changed")
async def prompt_file_changed(
    id: uuid.UUID,
    session: SessionDep,
    env: _EnvFromAgentAuth,
) -> Message:
    """
    Callback from an agent environment when prompt files stabilise after a change.

    Called by the env-core file-watcher (polling docs/WORKFLOW_PROMPT.md,
    docs/ENTRYPOINT_PROMPT.md, docs/REFINER_PROMPT.md) after a 5-second stable
    window. Fires sync_agent_prompts_from_environment, which updates the agent
    model and triggers A2A skill regeneration + background description update.

    Auth: AGENT_AUTH_TOKEN bearer + X-Agent-Env-Id environment header (internal only).
    """
    if env.id != id:
        raise HTTPException(status_code=403, detail="Environment ID mismatch")

    agent = session.get(Agent, env.agent_id)
    if not agent:
        logger.warning(f"prompt-file-changed: agent {env.agent_id} not found for env {env.id}")
        raise HTTPException(status_code=404, detail="Agent not found")

    try:
        await EnvironmentService.sync_agent_prompts_from_environment(
            session=session,
            environment=env,
            agent=agent,
        )
        logger.info(f"prompt-file-changed: synced prompts for agent {agent.id} from env {env.id}")
        return Message(message="Prompts synced successfully")
    except Exception as e:
        logger.error(f"prompt-file-changed: failed to sync prompts for env {env.id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to sync prompts: {str(e)}")
