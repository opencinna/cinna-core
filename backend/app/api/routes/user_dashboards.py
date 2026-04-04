import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from app.api.deps import CurrentUser, SessionDep
from app.models import (
    Message,
    SessionPublic,
    UserDashboardCreate,
    UserDashboardUpdate,
    UserDashboardPublic,
    UserDashboardBlockCreate,
    UserDashboardBlockUpdate,
    UserDashboardBlockPublic,
    BlockLayoutUpdate,
    UserDashboardBlockPromptActionCreate,
    UserDashboardBlockPromptActionUpdate,
    UserDashboardBlockPromptActionPublic,
)
from app.services.sessions.session_service import SessionService
from app.services.users.user_dashboard_service import UserDashboardService, UserDashboardError

router = APIRouter(prefix="/dashboards", tags=["Dashboards"])


def _handle_service_error(e: UserDashboardError) -> None:
    """Convert service exceptions to HTTP exceptions."""
    raise HTTPException(status_code=e.status_code, detail=e.message)


def _action_to_public(action: Any) -> UserDashboardBlockPromptActionPublic:
    return UserDashboardBlockPromptActionPublic(
        id=action.id,
        block_id=action.block_id,
        prompt_text=action.prompt_text,
        label=action.label,
        sort_order=action.sort_order,
        created_at=action.created_at,
        updated_at=action.updated_at,
    )


def _block_to_public(block: Any) -> UserDashboardBlockPublic:
    return UserDashboardBlockPublic(
        id=block.id,
        agent_id=block.agent_id,
        view_type=block.view_type,
        title=block.title,
        show_border=block.show_border,
        show_header=block.show_header,
        grid_x=block.grid_x,
        grid_y=block.grid_y,
        grid_w=block.grid_w,
        grid_h=block.grid_h,
        config=block.config,
        prompt_actions=[_action_to_public(a) for a in (block.prompt_actions or [])],
        created_at=block.created_at,
        updated_at=block.updated_at,
    )


def _dashboard_to_public(dashboard: Any) -> UserDashboardPublic:
    return UserDashboardPublic(
        id=dashboard.id,
        name=dashboard.name,
        description=dashboard.description,
        sort_order=dashboard.sort_order,
        created_at=dashboard.created_at,
        updated_at=dashboard.updated_at,
        blocks=[_block_to_public(b) for b in dashboard.blocks],
    )


# ── Dashboard endpoints ──────────────────────────────────────────────────────


# IMPORTANT: /agent-env-files must be registered BEFORE /{dashboard_id} to avoid route conflicts.
@router.get("/agent-env-files", response_model=list[str])
def list_agent_env_files(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    agent_id: str = Query(..., description="Agent ID to list files for"),
    subfolder: str = Query("files", description="Workspace subfolder to list (default: 'files')"),
) -> list[str]:
    """
    List files available in a workspace subfolder of an agent's active environment.

    Does not require a block — used by the Add Block form to let users pick
    a file before the block is created.
    """
    try:
        return UserDashboardService.list_agent_env_files(
            session=session,
            agent_id=agent_id,
            owner_id=current_user.id,
            subfolder=subfolder,
        )
    except UserDashboardError as e:
        _handle_service_error(e)
    return []  # unreachable, satisfies type checker


@router.get("/", response_model=list[UserDashboardPublic])
def list_dashboards(session: SessionDep, current_user: CurrentUser) -> Any:
    """List all dashboards for the current user, with their blocks."""
    dashboards = UserDashboardService.list_dashboards(
        session=session, owner_id=current_user.id
    )
    return [_dashboard_to_public(d) for d in dashboards]


@router.post("/", response_model=UserDashboardPublic)
def create_dashboard(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    dashboard_in: UserDashboardCreate,
) -> Any:
    """Create a new dashboard."""
    try:
        dashboard = UserDashboardService.create_dashboard(
            session=session, owner_id=current_user.id, data=dashboard_in
        )
    except UserDashboardError as e:
        _handle_service_error(e)
    return _dashboard_to_public(dashboard)


@router.get("/{dashboard_id}", response_model=UserDashboardPublic)
def get_dashboard(
    dashboard_id: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
) -> Any:
    """Get a single dashboard with all its blocks."""
    try:
        dashboard = UserDashboardService.get_dashboard(
            session=session, dashboard_id=dashboard_id, owner_id=current_user.id
        )
    except UserDashboardError as e:
        _handle_service_error(e)
    return _dashboard_to_public(dashboard)


@router.put("/{dashboard_id}", response_model=UserDashboardPublic)
def update_dashboard(
    *,
    dashboard_id: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
    dashboard_in: UserDashboardUpdate,
) -> Any:
    """Update dashboard metadata."""
    try:
        dashboard = UserDashboardService.update_dashboard(
            session=session,
            dashboard_id=dashboard_id,
            owner_id=current_user.id,
            data=dashboard_in,
        )
    except UserDashboardError as e:
        _handle_service_error(e)
    return _dashboard_to_public(dashboard)


@router.delete("/{dashboard_id}")
def delete_dashboard(
    dashboard_id: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
) -> Message:
    """Delete a dashboard and all its blocks."""
    try:
        UserDashboardService.delete_dashboard(
            session=session, dashboard_id=dashboard_id, owner_id=current_user.id
        )
    except UserDashboardError as e:
        _handle_service_error(e)
    return Message(message="Dashboard deleted")


# ── Block endpoints ──────────────────────────────────────────────────────────


@router.post("/{dashboard_id}/blocks", response_model=UserDashboardBlockPublic)
def add_block(
    *,
    dashboard_id: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
    block_in: UserDashboardBlockCreate,
) -> Any:
    """Add a block to a dashboard."""
    try:
        block = UserDashboardService.add_block(
            session=session,
            dashboard_id=dashboard_id,
            owner_id=current_user.id,
            data=block_in,
        )
    except UserDashboardError as e:
        _handle_service_error(e)
    return _block_to_public(block)


# IMPORTANT: /layout must be registered BEFORE /{block_id} to avoid route conflicts.
@router.put("/{dashboard_id}/blocks/layout", response_model=list[UserDashboardBlockPublic])
def update_block_layout(
    *,
    dashboard_id: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
    layouts: list[BlockLayoutUpdate],
) -> Any:
    """Bulk update block grid positions (for drag-and-drop rearrangement)."""
    try:
        blocks = UserDashboardService.update_block_layout(
            session=session,
            dashboard_id=dashboard_id,
            owner_id=current_user.id,
            layouts=layouts,
        )
    except UserDashboardError as e:
        _handle_service_error(e)
    return [_block_to_public(b) for b in blocks]


@router.put("/{dashboard_id}/blocks/{block_id}", response_model=UserDashboardBlockPublic)
def update_block(
    *,
    dashboard_id: uuid.UUID,
    block_id: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
    block_in: UserDashboardBlockUpdate,
) -> Any:
    """Update block configuration."""
    try:
        block = UserDashboardService.update_block(
            session=session,
            dashboard_id=dashboard_id,
            block_id=block_id,
            owner_id=current_user.id,
            data=block_in,
        )
    except UserDashboardError as e:
        _handle_service_error(e)
    return _block_to_public(block)


@router.delete("/{dashboard_id}/blocks/{block_id}")
def delete_block(
    dashboard_id: uuid.UUID,
    block_id: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
) -> Message:
    """Remove a block from a dashboard."""
    try:
        UserDashboardService.delete_block(
            session=session,
            dashboard_id=dashboard_id,
            block_id=block_id,
            owner_id=current_user.id,
        )
    except UserDashboardError as e:
        _handle_service_error(e)
    return Message(message="Block deleted")


# ── Prompt action endpoints ───────────────────────────────────────────────────
# NOTE: These are registered after the block-level routes. The path segment
# /prompt-actions is a literal string, so it does not conflict with /{block_id}.


@router.get(
    "/{dashboard_id}/blocks/{block_id}/prompt-actions",
    response_model=list[UserDashboardBlockPromptActionPublic],
)
def list_prompt_actions(
    dashboard_id: uuid.UUID,
    block_id: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
) -> Any:
    """List all prompt actions for a block, ordered by sort_order."""
    try:
        actions = UserDashboardService.list_prompt_actions(
            session=session,
            dashboard_id=dashboard_id,
            block_id=block_id,
            owner_id=current_user.id,
        )
    except UserDashboardError as e:
        _handle_service_error(e)
    return [_action_to_public(a) for a in actions]


@router.post(
    "/{dashboard_id}/blocks/{block_id}/prompt-actions",
    response_model=UserDashboardBlockPromptActionPublic,
)
def create_prompt_action(
    *,
    dashboard_id: uuid.UUID,
    block_id: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
    action_in: UserDashboardBlockPromptActionCreate,
) -> Any:
    """Add a prompt action to a block."""
    try:
        action = UserDashboardService.create_prompt_action(
            session=session,
            dashboard_id=dashboard_id,
            block_id=block_id,
            owner_id=current_user.id,
            data=action_in,
        )
    except UserDashboardError as e:
        _handle_service_error(e)
    return _action_to_public(action)


@router.put(
    "/{dashboard_id}/blocks/{block_id}/prompt-actions/{action_id}",
    response_model=UserDashboardBlockPromptActionPublic,
)
def update_prompt_action(
    *,
    dashboard_id: uuid.UUID,
    block_id: uuid.UUID,
    action_id: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
    action_in: UserDashboardBlockPromptActionUpdate,
) -> Any:
    """Update a prompt action."""
    try:
        action = UserDashboardService.update_prompt_action(
            session=session,
            dashboard_id=dashboard_id,
            block_id=block_id,
            action_id=action_id,
            owner_id=current_user.id,
            data=action_in,
        )
    except UserDashboardError as e:
        _handle_service_error(e)
    return _action_to_public(action)


@router.delete("/{dashboard_id}/blocks/{block_id}/prompt-actions/{action_id}")
def delete_prompt_action(
    dashboard_id: uuid.UUID,
    block_id: uuid.UUID,
    action_id: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
) -> Message:
    """Delete a prompt action from a block."""
    try:
        UserDashboardService.delete_prompt_action(
            session=session,
            dashboard_id=dashboard_id,
            block_id=block_id,
            action_id=action_id,
            owner_id=current_user.id,
        )
    except UserDashboardError as e:
        _handle_service_error(e)
    return Message(message="Prompt action deleted")


# ── Session reuse endpoint ────────────────────────────────────────────────────
# NOTE: This path contains the literal segment /latest-session which does not
# conflict with any /{action_id} route above.


@router.get(
    "/{dashboard_id}/blocks/{block_id}/latest-session",
    response_model=SessionPublic,
)
def get_block_latest_session(
    *,
    dashboard_id: uuid.UUID,
    block_id: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
) -> Any:
    """
    Return the most recent session tagged to this block that has a message
    within the last 12 hours, or 404 if none exists.

    Ownership is enforced by verifying the dashboard belongs to the current user.
    Used by the frontend PromptActionsOverlay to decide whether to reuse an
    existing session or create a new one.
    """
    try:
        UserDashboardService.get_dashboard(session, dashboard_id, current_user.id)
    except UserDashboardError as e:
        _handle_service_error(e)

    recent = SessionService.get_recent_block_session(
        db_session=session,
        block_id=block_id,
        user_id=current_user.id,
    )
    if not recent:
        raise HTTPException(status_code=404, detail="No recent session found for this block")
    return recent


# ── Agent Env File endpoints ─────────────────────────────────────────────────
# NOTE: The literal path segment /env-file prevents conflict with /{action_id}.


@router.get("/{dashboard_id}/blocks/{block_id}/env-files", response_model=list[str])
def list_block_env_files(
    *,
    dashboard_id: uuid.UUID,
    block_id: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
    subfolder: str = Query("files", description="Workspace subfolder to list (default: 'files')"),
) -> list[str]:
    """
    List files available in a workspace subfolder of the agent's default environment.

    Uses local filesystem access when the adapter supports it (no container needed).
    Returns empty list if adapter does not support local file listing.
    """
    try:
        return UserDashboardService.list_env_files(
            session=session,
            dashboard_id=dashboard_id,
            block_id=block_id,
            owner_id=current_user.id,
            subfolder=subfolder,
        )
    except UserDashboardError as e:
        _handle_service_error(e)
    return []  # unreachable, satisfies type checker


@router.get("/{dashboard_id}/blocks/{block_id}/env-file")
async def get_block_env_file(
    *,
    dashboard_id: uuid.UUID,
    block_id: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
    path: str = Query(..., description="Workspace-relative path (e.g. 'files/report.csv')"),
) -> StreamingResponse:
    """
    Stream the content of a workspace file from the agent's default environment.

    The path is workspace-relative (e.g., 'files/report.csv'). Uses local
    filesystem access when the adapter supports it (no container needed).
    Falls back to HTTP proxy through the container when local access is not available.

    Ownership is enforced via the dashboard -> block -> agent chain.
    """
    try:
        local_path = UserDashboardService.get_env_file_local_path(
            session=session,
            dashboard_id=dashboard_id,
            block_id=block_id,
            owner_id=current_user.id,
            path=path,
        )
    except UserDashboardError as e:
        _handle_service_error(e)

    if local_path is not None:
        def _stream_local():
            with open(local_path, "rb") as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    yield chunk

        return StreamingResponse(
            _stream_local(),
            media_type="text/plain; charset=utf-8",
            headers={"X-Accel-Buffering": "no"},
        )

    # Non-local adapter: stream from remote
    try:
        adapter = UserDashboardService.get_env_file_adapter(
            session=session,
            dashboard_id=dashboard_id,
            block_id=block_id,
            owner_id=current_user.id,
            path=path,
        )
    except UserDashboardError as e:
        _handle_service_error(e)

    async def _stream_remote():
        async for chunk in adapter.download_workspace_item(path):
            yield chunk

    return StreamingResponse(
        _stream_remote(),
        media_type="text/plain; charset=utf-8",
        headers={"X-Accel-Buffering": "no"},
    )
