import uuid
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from app.api.deps import CurrentUser, SessionDep
from app.models import AgentEnvironment, Agent
from app.services.environment_service import EnvironmentService

router = APIRouter(prefix="/environments", tags=["workspace"])


@router.get("/{env_id}/workspace/tree")
async def get_workspace_tree(
    session: SessionDep,
    current_user: CurrentUser,
    env_id: uuid.UUID
) -> Any:
    """
    Get complete workspace tree structure for an environment.

    Returns tree for 4 folders: files, logs, scripts, docs
    Includes folder summaries (fileCount, totalSize)

    Permissions: User must own the agent
    """
    # Get environment and verify permissions
    environment = session.get(AgentEnvironment, env_id)
    if not environment:
        raise HTTPException(status_code=404, detail="Environment not found")

    agent = session.get(Agent, environment.agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    if not current_user.is_superuser and (agent.owner_id != current_user.id):
        raise HTTPException(status_code=403, detail="Not enough permissions")

    # Check environment is running
    if environment.status != "running":
        raise HTTPException(
            status_code=400,
            detail=f"Environment must be running (current status: {environment.status})"
        )

    # Get tree via adapter
    try:
        lifecycle_manager = EnvironmentService.get_lifecycle_manager()
        adapter = lifecycle_manager.get_adapter(environment)

        tree_data = await adapter.get_workspace_tree()
        return tree_data

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to get workspace tree: {str(e)}"
        )


@router.get("/{env_id}/workspace/download/{path:path}")
async def download_workspace_item(
    session: SessionDep,
    current_user: CurrentUser,
    env_id: uuid.UUID,
    path: str
):
    """
    Download a file or folder from workspace.

    - Files: streamed directly with original filename
    - Folders: streamed as zip archive with folder name

    Args:
        env_id: Environment ID
        path: Relative path from workspace root (e.g., "files/data.csv" or "logs")

    Security:
    - Path validation performed by agent-env
    - Prevents directory traversal
    - Rejects paths outside workspace

    Permissions: User must own the agent
    """
    # Get environment and verify permissions
    environment = session.get(AgentEnvironment, env_id)
    if not environment:
        raise HTTPException(status_code=404, detail="Environment not found")

    agent = session.get(Agent, environment.agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    if not current_user.is_superuser and (agent.owner_id != current_user.id):
        raise HTTPException(status_code=403, detail="Not enough permissions")

    # Check environment is running
    if environment.status != "running":
        raise HTTPException(
            status_code=400,
            detail=f"Environment must be running (current status: {environment.status})"
        )

    # Stream download via adapter
    try:
        lifecycle_manager = EnvironmentService.get_lifecycle_manager()
        adapter = lifecycle_manager.get_adapter(environment)

        # Extract filename from path
        path_parts = path.rstrip('/').split('/')
        filename = path_parts[-1] if path_parts else "workspace"

        # Stream response
        async def stream_download():
            async for chunk in adapter.download_workspace_item(path):
                yield chunk

        # Determine media type (backend doesn't know if file or folder, use generic)
        # Agent-env sets proper Content-Type, but we'll use generic for safety
        return StreamingResponse(
            stream_download(),
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "X-Accel-Buffering": "no"  # Disable nginx buffering
            }
        )

    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to download: {str(e)}"
        )


@router.get("/{env_id}/workspace/view-file/{path:path}")
async def view_workspace_file(
    session: SessionDep,
    current_user: CurrentUser,
    env_id: uuid.UUID,
    path: str
):
    """
    View a file from workspace (for CSV and other text files).

    Streams the file content as text for rendering in the UI.

    Args:
        env_id: Environment ID
        path: Relative path from workspace root (e.g., "files/data.csv")

    Security:
    - Path validation performed by agent-env
    - Prevents directory traversal
    - Rejects paths outside workspace

    Permissions: User must own the agent
    """
    # Get environment and verify permissions
    environment = session.get(AgentEnvironment, env_id)
    if not environment:
        raise HTTPException(status_code=404, detail="Environment not found")

    agent = session.get(Agent, environment.agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    if not current_user.is_superuser and (agent.owner_id != current_user.id):
        raise HTTPException(status_code=403, detail="Not enough permissions")

    # Check environment is running
    if environment.status != "running":
        raise HTTPException(
            status_code=400,
            detail=f"Environment must be running (current status: {environment.status})"
        )

    # Stream file content via adapter
    try:
        lifecycle_manager = EnvironmentService.get_lifecycle_manager()
        adapter = lifecycle_manager.get_adapter(environment)

        # Stream response
        async def stream_file():
            async for chunk in adapter.download_workspace_item(path):
                yield chunk

        # Return as plain text for CSV files
        return StreamingResponse(
            stream_file(),
            media_type="text/plain; charset=utf-8",
            headers={
                "X-Accel-Buffering": "no"  # Disable nginx buffering
            }
        )

    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to view file: {str(e)}"
        )
