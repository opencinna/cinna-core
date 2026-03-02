"""
Shared MCP context variables.

Extracted into a separate module to avoid circular imports between
server.py and token_verifier.py.
"""
from contextvars import ContextVar

# Set by MCPServerRegistry.__call__() — identifies the connector for this request
mcp_connector_id_var: ContextVar[str] = ContextVar("mcp_connector_id")

# Set by MCPServerRegistry.__call__() — MCP transport session ID from client header
mcp_session_id_var: ContextVar[str | None] = ContextVar("mcp_session_id", default=None)

# Set by MCPTokenVerifier.verify_token() — the OAuth-authenticated user's ID
mcp_authenticated_user_id_var: ContextVar[str | None] = ContextVar(
    "mcp_authenticated_user_id", default=None
)
