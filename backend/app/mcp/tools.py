"""
MCP tool registration and MCP-specific request dispatching.

This module contains only MCP-specific logic:
- Context variable extraction (connector_id, mcp_session_id)
- MCP transport session ID resolution from multiple sources
- Tool registration on FastMCP server instances

All business logic (session management, message creation, streaming)
is delegated to MCPRequestHandler, following the same isolation pattern
as A2ARequestHandler.
"""
import uuid
import logging

from app.core.config import settings
from app.core.db import create_session
from app.services.mcp.mcp_connector_service import MCPConnectorService
from app.services.mcp.mcp_errors import MCPError
from app.mcp.request_handler import MCPRequestHandler
from app.mcp.upload_token import create_file_upload_token
from app.mcp.context_vars import mcp_connector_id_var, mcp_session_id_var, mcp_authenticated_user_id_var

logger = logging.getLogger(__name__)


async def handle_send_message(message: str, context_id: str = "", ctx=None) -> str:
    """
    MCP tool handler: send a message to the agent and return the response.

    This is registered on each per-connector FastMCP server instance.

    IMPORTANT: Any unhandled exception here kills the MCP session, causing all
    subsequent requests to return 400. Always return an error string instead.
    """
    try:
        return await _handle_send_message_inner(message, context_id, ctx)
    except Exception as e:
        logger.exception("Unhandled error in MCP send_message tool: %s", e)
        return f"Error: {str(e)}"


async def _handle_send_message_inner(message: str, context_id: str = "", ctx=None) -> str:
    """
    MCP-specific entry point: extract MCP context, resolve entities, delegate.

    Analogous to the A2A route handler that resolves agent/environment
    before creating A2ARequestHandler.
    """
    # Extract connector_id from context var (set by MCPServerRegistry)
    connector_id_str = mcp_connector_id_var.get(None)
    if not connector_id_str:
        return "Error: No connector context available"

    # Extract MCP transport session ID from context var (set by MCPServerRegistry)
    mcp_transport_session_id = mcp_session_id_var.get(None)

    # Also try to get it from the tool context (Starlette request headers)
    ctx_session_id = None
    if ctx is not None:
        try:
            req_ctx = getattr(ctx, 'request_context', None)
            if req_ctx is not None:
                starlette_request = getattr(req_ctx, 'request', None)
                if starlette_request is not None:
                    ctx_session_id = starlette_request.headers.get("mcp-session-id")
        except Exception as e:
            logger.debug("[MCP] Could not extract session ID from ctx: %s", e)

    # Use whichever source has the session ID
    effective_mcp_session_id = mcp_transport_session_id or ctx_session_id

    connector_id = uuid.UUID(connector_id_str)

    # Resolve connector, agent, and environment (analogous to A2A route resolution)
    with create_session() as db:
        try:
            connector, agent, environment = MCPConnectorService.resolve_connector_context(
                db, connector_id,
            )
        except MCPError as e:
            logger.warning("[MCP] Context resolution failed for connector %s: %s", connector_id_str, e)
            return f"Error: {e}"

    # Read authenticated user identity (set by MCPTokenVerifier)
    auth_user_id_str = mcp_authenticated_user_id_var.get(None)
    authenticated_user_id = uuid.UUID(auth_user_id_str) if auth_user_id_str else None

    # Create handler and delegate (same pattern as A2ARequestHandler)
    handler = MCPRequestHandler(
        agent=agent,
        environment=environment,
        connector=connector,
        get_db_session=create_session,
        authenticated_user_id=authenticated_user_id,
    )
    return await handler.handle_send_message(
        message,
        effective_mcp_session_id,
        context_id=context_id or None,
        mcp_ctx=ctx,
    )


async def handle_get_file_upload_url(filename: str, workspace_path: str = "uploads") -> str:
    """
    MCP tool handler: generate a temporary upload URL + CURL command.

    IMPORTANT: Any unhandled exception here kills the MCP session, causing all
    subsequent requests to return 400. Always return an error string instead.
    """
    try:
        return _handle_get_file_upload_url_inner(filename, workspace_path)
    except Exception as e:
        logger.exception("Unhandled error in MCP get_file_upload_url tool: %s", e)
        return f"Error: {str(e)}"


def _handle_get_file_upload_url_inner(filename: str, workspace_path: str = "uploads") -> str:
    """Generate a CURL command with a temporary JWT for file upload."""
    connector_id_str = mcp_connector_id_var.get(None)
    if not connector_id_str:
        return "Error: No connector context available"

    # Validate connector exists and is active
    connector_id = uuid.UUID(connector_id_str)
    with create_session() as db:
        try:
            MCPConnectorService.resolve_connector_context(db, connector_id)
        except MCPError as e:
            return f"Error: {e}"

    # Build upload URL
    base_url = settings.MCP_SERVER_BASE_URL
    if not base_url:
        return "Error: MCP_SERVER_BASE_URL is not configured"
    base_url = base_url.rstrip("/")
    upload_url = f"{base_url}/{connector_id_str}/upload"

    # Generate temporary JWT
    token = create_file_upload_token(connector_id_str)

    return (
        f"Upload your file using this command (token valid for 15 minutes):\n\n"
        f'curl -X POST "{upload_url}" \\\n'
        f'  -H "Authorization: Bearer {token}" \\\n'
        f'  -F "file=@{filename}" \\\n'
        f'  -F "workspace_path={workspace_path}"'
    )


def register_mcp_tools(server) -> None:
    """Register all MCP tools on the given FastMCP server instance."""
    from mcp.server.fastmcp.server import Context

    @server.tool(
        name="send_message",
        description=(
            "Send a message to the AI agent and receive a response. "
            "The agent can use tools, write code, and perform tasks based on your message.\n\n"
            "Returns a JSON object with 'response' and 'context_id' fields. "
            "IMPORTANT: Always pass back the 'context_id' from the previous response "
            "to maintain conversation continuity. On the first message in a new conversation, "
            "pass an empty string for context_id."
        ),
    )
    async def send_message(message: str, context_id: str = "", ctx: Context = None) -> str:
        result = await handle_send_message(message, context_id, ctx)

        # Notify MCP client that workspace resources may have changed
        # (the agent may have created/modified files during processing)
        if ctx is not None:
            try:
                from app.mcp.server import mcp_registry
                connector_id = mcp_connector_id_var.get(None)
                mcp_sid = mcp_session_id_var.get(None)
                session = ctx.session
                # Register session for broadcast reuse (e.g. upload route)
                if connector_id and session and mcp_sid:
                    mcp_registry.register_session(connector_id, mcp_sid, session)
                if session:
                    await session.send_resource_list_changed()
            except Exception:
                logger.debug(
                    "[MCP] Failed to send resource list changed notification "
                    "after send_message (non-fatal)",
                    exc_info=True,
                )

        return result

    @server.tool(
        name="get_file_upload_url",
        description="Get a temporary upload URL and CURL command to upload a file to the agent's workspace. The returned CURL command includes a short-lived authentication token (valid for 15 minutes). Execute the CURL command to upload the file.",
    )
    async def get_file_upload_url(filename: str, workspace_path: str = "uploads") -> str:
        return await handle_get_file_upload_url(filename, workspace_path)
