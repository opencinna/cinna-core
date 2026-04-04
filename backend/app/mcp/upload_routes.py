"""
MCP file upload endpoint.

POST /mcp/{connector_id}/upload — accepts multipart file upload authenticated
with a short-lived JWT from the `get_file_upload_url` MCP tool.

The backend proxies the file to the agent-env Docker container's
POST /files/upload endpoint, which handles sanitization and placement.
"""
import logging
import uuid

from fastapi import APIRouter, HTTPException, Header, UploadFile, File, Form
from sqlmodel import Session as DBSession

from app.core.config import settings
from app.core.db import create_session
from app.mcp.upload_token import verify_file_upload_token
from app.services.mcp.mcp_connector_service import MCPConnectorService
from app.services.mcp.mcp_errors import MCPError
from app.services.environments.environment_service import EnvironmentService

logger = logging.getLogger(__name__)

router = APIRouter(tags=["mcp-upload"])


def _extract_bearer_token(authorization: str | None) -> str | None:
    """Extract token from 'Bearer <token>' header value."""
    if not authorization:
        return None
    parts = authorization.split(" ", 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1]
    return None


@router.post("/mcp/{connector_id}/upload")
async def upload_file_to_mcp(
    connector_id: str,
    file: UploadFile = File(...),
    workspace_path: str = Form("uploads"),
    authorization: str | None = Header(None),
):
    """Upload a file to an agent environment via MCP.

    Authenticated with a short-lived JWT from the get_file_upload_url MCP tool.
    The file is proxied to the agent-env container's /files/upload endpoint.
    """
    # Validate connector_id format
    try:
        connector_uuid = uuid.UUID(connector_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid connector ID format")

    # Extract and verify JWT
    token = _extract_bearer_token(authorization)
    if not token:
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")

    token_connector_id = verify_file_upload_token(token)
    if not token_connector_id:
        raise HTTPException(status_code=401, detail="Invalid or expired upload token")

    # Check connector_id in JWT matches URL path
    if token_connector_id != connector_id:
        raise HTTPException(status_code=403, detail="Token not valid for this connector")

    # Read file content
    content = await file.read()
    filename = file.filename or "unnamed_file"

    # Validate file size
    if len(content) > settings.upload_max_file_size_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"File too large. Max size: {settings.UPLOAD_MAX_FILE_SIZE_MB}MB",
        )

    # Resolve connector context (connector, agent, environment)
    with create_session() as db:
        try:
            connector, agent, environment = MCPConnectorService.resolve_connector_context(
                db, connector_uuid,
            )
        except MCPError as e:
            raise HTTPException(status_code=e.status_code, detail=e.message)

        # Validate environment is running
        if environment.status != "running":
            raise HTTPException(
                status_code=503,
                detail=f"Agent environment is not running (status: {environment.status})",
            )

    # Get adapter and upload file
    lifecycle_manager = EnvironmentService.get_lifecycle_manager()
    adapter = lifecycle_manager.get_adapter(environment)

    try:
        result = await adapter.upload_file_to_agent_env(filename, content)
    except Exception as e:
        logger.exception("[MCP Upload] Failed to upload file to agent-env: %s", e)
        raise HTTPException(status_code=502, detail=f"Failed to upload file to agent environment: {e}")

    logger.info(
        "[MCP Upload] File uploaded | connector=%s | filename=%s | size=%d | path=%s",
        connector_id, filename, len(content), result.get("path", "unknown"),
    )

    # Notify connected MCP clients that workspace resources changed
    try:
        from app.mcp.notifications import broadcast_resource_list_changed
        await broadcast_resource_list_changed(connector_id)
    except Exception:
        logger.debug(
            "[MCP Upload] Failed to broadcast resource list changed notification "
            "for connector %s (non-fatal)",
            connector_id,
            exc_info=True,
        )

    return {
        "status": "uploaded",
        "workspace_path": result.get("path", f"./uploads/{filename}"),
        "filename": result.get("filename", filename),
        "size": len(content),
    }
