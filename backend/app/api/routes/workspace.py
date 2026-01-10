import uuid
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.api.deps import CurrentUser, SessionDep
from app.models import AgentEnvironment, Agent
from app.services.environment_service import EnvironmentService
from app.services.ai_functions_service import AIFunctionsService

router = APIRouter(prefix="/environments", tags=["workspace"])


# Request/Response models for database endpoints

class DatabaseQueryRequest(BaseModel):
    """Request to execute SQL query on SQLite database"""
    path: str  # Relative path to SQLite file
    query: str  # SQL query to execute
    page: int | None = None  # Page number (1-based), None = no pagination
    page_size: int | None = None  # Rows per page, None = no pagination
    timeout_seconds: int = 30  # Query timeout


class GenerateSQLRequest(BaseModel):
    """Request to generate SQL query from natural language"""
    path: str  # Relative path to SQLite file
    user_request: str  # Natural language description of desired query
    current_query: str | None = None  # Current SQL query in editor (optional)


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


# SQLite Database Endpoints

@router.get("/{env_id}/database/tables/{path:path}")
async def get_database_tables(
    session: SessionDep,
    current_user: CurrentUser,
    env_id: uuid.UUID,
    path: str
) -> list[dict]:
    """
    Get list of tables and views from SQLite database.

    Args:
        env_id: Environment ID
        path: Relative path to SQLite file from workspace root

    Returns:
        List of dicts with 'name' and 'type' keys

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

    try:
        lifecycle_manager = EnvironmentService.get_lifecycle_manager()
        adapter = lifecycle_manager.get_adapter(environment)
        return await adapter.get_database_tables(path)

    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to get database tables: {str(e)}"
        )


@router.get("/{env_id}/database/schema/{path:path}")
async def get_database_schema(
    session: SessionDep,
    current_user: CurrentUser,
    env_id: uuid.UUID,
    path: str
) -> Any:
    """
    Get complete schema for SQLite database.

    Args:
        env_id: Environment ID
        path: Relative path to SQLite file from workspace root

    Returns:
        Database schema with path, tables, and views (each with columns)

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

    try:
        lifecycle_manager = EnvironmentService.get_lifecycle_manager()
        adapter = lifecycle_manager.get_adapter(environment)
        return await adapter.get_database_schema(path)

    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to get database schema: {str(e)}"
        )


@router.post("/{env_id}/database/query")
async def execute_database_query(
    session: SessionDep,
    current_user: CurrentUser,
    env_id: uuid.UUID,
    request: DatabaseQueryRequest
) -> Any:
    """
    Execute SQL query on SQLite database.

    For SELECT queries: returns paginated results
    For DML queries (INSERT/UPDATE/DELETE): returns rows_affected count

    Args:
        env_id: Environment ID
        request: Query request with path, SQL, pagination, and timeout

    Returns:
        Query result with columns, rows, pagination info, and execution stats

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

    try:
        lifecycle_manager = EnvironmentService.get_lifecycle_manager()
        adapter = lifecycle_manager.get_adapter(environment)
        return await adapter.execute_database_query(
            path=request.path,
            query=request.query,
            page=request.page,
            page_size=request.page_size,
            timeout_seconds=request.timeout_seconds
        )

    except Exception as e:
        # Return error in response format rather than HTTP error
        # This allows the frontend to display the error message
        return {
            "columns": [],
            "rows": [],
            "total_rows": 0,
            "page": request.page,
            "page_size": request.page_size,
            "has_more": False,
            "execution_time_ms": 0,
            "query_type": "OTHER",
            "rows_affected": None,
            "error": str(e),
            "error_type": "execution_error"
        }


@router.post("/{env_id}/database/generate-sql")
async def generate_sql_query(
    session: SessionDep,
    current_user: CurrentUser,
    env_id: uuid.UUID,
    request: GenerateSQLRequest
) -> Any:
    """
    Generate SQL query from natural language description using AI.

    Args:
        env_id: Environment ID
        request: GenerateSQLRequest with path, user_request, and optional current_query

    Returns:
        dict with keys:
            - success: bool
            - sql: Generated SQL query (if success)
            - error: Error message or clarifying questions (if not success)

    Permissions: User must own the agent
    """
    # Check if AI functions are available
    if not AIFunctionsService.is_available():
        return {
            "success": False,
            "error": "AI features are not available. Please configure GOOGLE_API_KEY."
        }

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

    try:
        # First, get the database schema
        lifecycle_manager = EnvironmentService.get_lifecycle_manager()
        adapter = lifecycle_manager.get_adapter(environment)
        schema = await adapter.get_database_schema(request.path)

        # Generate SQL using AI
        result = AIFunctionsService.generate_sql(
            user_request=request.user_request,
            database_schema=schema,
            current_query=request.current_query
        )

        return result

    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        return {
            "success": False,
            "error": str(e)
        }
    except Exception as e:
        return {
            "success": False,
            "error": f"Failed to generate SQL: {str(e)}"
        }
