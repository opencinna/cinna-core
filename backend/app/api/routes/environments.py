import logging
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, WebSocket, status
from pydantic import BaseModel
from sqlmodel import Session

from app.api.deps import (
    CurrentUser,
    EnvConsoleContext,
    SessionDep,
    get_env_console_context_ws,
)
from app.models import (
    AgentEnvironment,
    AgentEnvironmentUpdate,
    AgentEnvironmentReconfigure,
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


@router.post("/{id}/reconfigure", response_model=AgentEnvironmentPublic)
async def reconfigure_environment(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    id: uuid.UUID,
    data: AgentEnvironmentReconfigure,
) -> Any:
    """
    Dynamically change an environment's per-mode SDK / AI credential / model
    override and (by default) rebuild it immediately.

    Validates the new SDK↔credential pairing up front (400 on mismatch / missing
    key) so the change is rejected before any rebuild starts. Owner / superuser
    only (enforced by the access check).
    """
    try:
        EnvironmentService.get_environment_with_access_check(
            session, id, current_user.id, current_user.is_superuser
        )
        updated_environment = await EnvironmentService.reconfigure_environment(
            session=session,
            env_id=id,
            user=current_user,
            data=data,
            rebuild=data.rebuild,
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


class WorkspaceFilesChangedRequest(BaseModel):
    """Optional body for the workspace-files-changed callback.

    ``changed_files`` is informational — currently used for logging only;
    downstream handlers refresh all caches regardless.
    """
    changed_files: list[str] | None = None


async def _emit_workspace_files_changed_callback(
    session: Session,
    env: AgentEnvironment,
    path_id: uuid.UUID,
    changed_files: list[str] | None,
) -> Message:
    """
    Shared handler for the two env→backend file-change callbacks.

    Guards against environment-id mismatch, delegates to the service layer,
    and translates ``AgentEnvironmentError`` to HTTPException. Keeps the two
    route handlers below as one-liners.
    """
    if env.id != path_id:
        raise HTTPException(status_code=403, detail="Environment ID mismatch")
    try:
        await EnvironmentService.emit_workspace_files_changed(
            session=session,
            environment=env,
            changed_files=changed_files,
        )
    except AgentEnvironmentError as e:
        _handle_service_error(e)
    return Message(message="Workspace files change event emitted")


@router.post("/{id}/workspace-files-changed")
async def workspace_files_changed(
    id: uuid.UUID,
    session: SessionDep,
    env: _EnvFromAgentAuth,
    body: WorkspaceFilesChangedRequest | None = None,
) -> Message:
    """
    Callback from an agent environment when watched workspace files stabilise
    after a change — typically after a Mutagen sync from the CLI.

    Emits ``WORKSPACE_FILES_CHANGED``; downstream handlers refresh the agent's
    prompts, CLI commands cache, and status snapshot.

    Auth: AGENT_AUTH_TOKEN bearer + X-Agent-Env-Id environment header (internal only).
    """
    return await _emit_workspace_files_changed_callback(
        session=session,
        env=env,
        path_id=id,
        changed_files=body.changed_files if body else None,
    )


@router.post("/{id}/prompt-file-changed")
async def prompt_file_changed(
    id: uuid.UUID,
    session: SessionDep,
    env: _EnvFromAgentAuth,
) -> Message:
    """
    Legacy alias — emits ``WORKSPACE_FILES_CHANGED`` with no ``changed_files``
    list. Kept so agent environments built before the generic watcher shipped
    keep working without a rebuild.

    Auth: AGENT_AUTH_TOKEN bearer + X-Agent-Env-Id environment header (internal only).
    """
    return await _emit_workspace_files_changed_callback(
        session=session,
        env=env,
        path_id=id,
        changed_files=None,
    )


@router.post("/{id}/agent-api-reloaded")
async def agent_api_reloaded(
    id: uuid.UUID,
    env: _EnvFromAgentAuth,
) -> Message:
    """
    Callback from an agent environment after its agent REST API child reloads
    (uvicorn ``--reload`` picked up an ``agent_api/`` change) or after a boot
    error. Triggers a fresh import-only spec + policy re-harvest so the cached
    spec on the env row stays in sync and the owner sees boot errors without
    making a request.

    This is intentionally a dedicated endpoint rather than reusing the
    workspace-files mtime watcher (plan §3.4): the watcher tracks a fixed file
    list and notifies the backend for prompt / CLI-command refreshes, whereas
    the agent_api reload is driven by uvicorn's own reloader.

    Auth: AGENT_AUTH_TOKEN bearer + X-Agent-Env-Id environment header (internal only).
    """
    if env.id != id:
        raise HTTPException(status_code=403, detail="Environment ID mismatch")
    from app.services.agent_api.agent_api_service import AgentApiService

    # Best-effort: re-harvest + re-cache the spec in its own session.
    await AgentApiService.refresh_spec_cache(env.id)
    return Message(message="Agent API spec re-cache triggered")


# ── Environment console WebSocket routes (web terminal + logs follow) ─────────
# Browser-facing WebSockets authenticated with the platform JWT in ``?token=``
# (browsers cannot set Authorization on a WS handshake). Thin controllers: the
# auth dep enforces ownership / developer-role and the service owns the full
# proxy lifecycle. These endpoints do not appear in the OpenAPI client.


def _ws_source_ip(websocket: WebSocket) -> str | None:
    """Best-effort client IP for audit, honouring a reverse-proxy header."""
    forwarded = websocket.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return websocket.client.host if websocket.client else None


@router.websocket("/{id}/terminal")
async def environment_terminal_ws(
    websocket: WebSocket,
    id: uuid.UUID,
    ctx: Annotated[
        EnvConsoleContext, Depends(get_env_console_context_ws(require_terminal=True))
    ],
) -> None:
    """
    Interactive web-terminal WebSocket (owner + agent-developer/superuser only).

    The auth dep validates the token, loads the env, and enforces the
    ownership + developer gate; this controller just hands the socket to
    ``EnvironmentConsoleService.run_terminal_tunnel``.
    """
    from app.services.environments.environment_console_service import (
        EnvironmentConsoleService,
    )

    await EnvironmentConsoleService.run_terminal_tunnel(
        websocket=websocket,
        environment=ctx.environment,
        agent_id=ctx.agent.id,
        user=ctx.user,
        raw_token=ctx.raw_token,
        source_ip=_ws_source_ip(websocket),
    )


@router.websocket("/{id}/logs/stream")
async def environment_logs_stream_ws(
    websocket: WebSocket,
    id: uuid.UUID,
    ctx: Annotated[
        EnvConsoleContext, Depends(get_env_console_context_ws(require_terminal=False))
    ],
    tail: int = Query(default=200, ge=1),
) -> None:
    """
    Live container-logs follow WebSocket (owner/superuser).

    Streams a tail snapshot followed by host-side ``docker logs -f`` output.
    Thin controller — delegates the lifecycle to the console service.
    """
    from app.services.environments.environment_console_service import (
        EnvironmentConsoleService,
    )

    await EnvironmentConsoleService.follow_logs(
        websocket=websocket,
        environment=ctx.environment,
        user=ctx.user,
        raw_token=ctx.raw_token,
        tail=tail,
    )
