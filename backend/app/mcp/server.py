import uuid
import logging
from contextlib import asynccontextmanager
from contextvars import ContextVar
from urllib.parse import urlparse

import anyio
from starlette.responses import JSONResponse
from starlette.routing import get_route_path
from starlette.types import ASGIApp, Receive, Scope, Send

from mcp.server.fastmcp import FastMCP
from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import TransportSecuritySettings

from sqlmodel import Session as DBSession

from app.core.config import settings
from app.core.db import engine
from app.models.mcp_connector import MCPConnector
from app.mcp.token_verifier import MCPTokenVerifier

logger = logging.getLogger(__name__)

# Context vars to pass connector_id and MCP session_id to tool handlers
mcp_connector_id_var: ContextVar[str] = ContextVar("mcp_connector_id")
mcp_session_id_var: ContextVar[str | None] = ContextVar("mcp_session_id", default=None)


def _build_transport_security() -> TransportSecuritySettings:
    """Build TransportSecuritySettings from the configured MCP_SERVER_BASE_URL.

    The MCP SDK auto-enables DNS rebinding protection for localhost, only
    allowing 127.0.0.1/localhost/[::1]. When the server is behind a tunnel
    (e.g. pinggy), the Host header is the tunnel hostname, which gets rejected.
    We extract the host from MCP_SERVER_BASE_URL and allow it explicitly.
    """
    base_url = settings.MCP_SERVER_BASE_URL or ""
    parsed = urlparse(base_url)
    hostname = parsed.hostname or ""

    # Always allow localhost variants
    allowed_hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
    allowed_origins = ["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"]

    # Add the configured external host (e.g. tunnel hostname)
    if hostname and hostname not in ("127.0.0.1", "localhost", "::1"):
        allowed_hosts.append(f"{hostname}:*")
        # Also allow without port (Host header may omit default ports)
        allowed_hosts.append(hostname)
        scheme = parsed.scheme or "https"
        allowed_origins.append(f"{scheme}://{hostname}:*")
        allowed_origins.append(f"{scheme}://{hostname}")

    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
    )


def create_mcp_server_for_connector(connector_id: str) -> FastMCP:
    """Create a FastMCP server instance for a specific connector."""
    base_url = settings.MCP_SERVER_BASE_URL.rstrip("/")
    resource_url = f"{base_url}/{connector_id}/mcp"
    issuer_url = f"{base_url}/oauth"

    server = FastMCP(
        name=f"mcp-connector-{connector_id}",
        auth=AuthSettings(
            issuer_url=issuer_url,
            resource_server_url=resource_url,
        ),
        token_verifier=MCPTokenVerifier(connector_id),
        transport_security=_build_transport_security(),
    )

    # Register tool handlers
    from app.mcp.tools import register_mcp_tools
    register_mcp_tools(server)

    return server


class MCPServerRegistry:
    """
    ASGI dispatcher that routes /{connector_id}/... to per-connector MCP server apps.

    Mounted at /mcp in the main FastAPI app, so full paths are:
    /mcp/{connector_id}/mcp -> per-connector MCP Streamable HTTP endpoint

    The registry manages session manager lifecycles: each per-connector FastMCP app
    has a StreamableHTTPSessionManager that requires an active anyio task group.
    The registry's run() context creates a parent task group and starts each
    connector's session_manager.run() within it.
    """

    def __init__(self):
        self._servers: dict[str, ASGIApp] = {}
        self._mcp_instances: dict[str, FastMCP] = {}
        self._task_group: anyio.abc.TaskGroup | None = None
        self._base_url_validated: bool = False

    @asynccontextmanager
    async def run(self):
        """Async context manager that manages the parent task group for all
        per-connector session managers. Must be active before handling requests.

        Use inside the FastAPI app lifespan:
            async with mcp_registry.run():
                yield
        """
        async with anyio.create_task_group() as tg:
            self._task_group = tg
            self._validate_base_url_config()
            try:
                yield
            finally:
                tg.cancel_scope.cancel()
                self._task_group = None
                self._servers.clear()
                self._mcp_instances.clear()
                logger.info("MCP server registry shut down")

    def _validate_base_url_config(self) -> None:
        """Validate MCP_SERVER_BASE_URL at startup."""
        base_url = settings.MCP_SERVER_BASE_URL
        if not base_url:
            logger.error(
                "[MCP] MCP_SERVER_BASE_URL is not configured. "
                "MCP connectors will not work. Set it to the external URL "
                "that proxies to the backend's /mcp/ path "
                "(e.g. https://mcp.example.com or https://tunnel.example.com/mcp)."
            )
            return

        parsed = urlparse(base_url)
        if parsed.scheme not in ("http", "https"):
            logger.error("[MCP] MCP_SERVER_BASE_URL has invalid scheme: %r (expected http or https)", base_url)
            return

        logger.info(
            "[MCP] MCP server registry started | MCP_SERVER_BASE_URL=%s | "
            "MCP server URLs will be: %s/{connector_id}/mcp",
            base_url, base_url.rstrip("/"),
        )

    def _validate_base_url_on_request(self, scope: Scope) -> None:
        """One-time check on the first MCP request: verify that the Host header
        and path prefix are consistent with MCP_SERVER_BASE_URL.

        Catches common misconfiguration where the base URL doesn't match the
        actual proxy routing (e.g. missing /mcp suffix).
        """
        if self._base_url_validated:
            return
        self._base_url_validated = True

        base_url = settings.MCP_SERVER_BASE_URL
        if not base_url:
            return

        parsed = urlparse(base_url)
        expected_host = parsed.hostname or ""
        expected_path = parsed.path.rstrip("/")  # e.g. "/mcp" or ""

        # Extract Host header from the request
        request_host = ""
        for name, value in scope.get("headers", []):
            if name == b"host":
                # Strip port if present
                request_host = value.decode("ascii", errors="replace").split(":")[0]
                break

        # The registry is mounted at /mcp, so scope["root_path"] should end
        # with the path portion of MCP_SERVER_BASE_URL.
        # e.g. if MCP_SERVER_BASE_URL=https://host/mcp, root_path should be "/mcp"
        root_path = scope.get("root_path", "")

        issues = []
        if expected_host and request_host and expected_host != request_host:
            issues.append(
                f"Host header '{request_host}' does not match "
                f"MCP_SERVER_BASE_URL hostname '{expected_host}'"
            )
        if expected_path and not root_path.endswith(expected_path):
            issues.append(
                f"Mount root_path '{root_path}' does not end with "
                f"MCP_SERVER_BASE_URL path '{expected_path}'. "
                f"Check that your reverse proxy forwards to the backend's /mcp/ path"
            )

        if issues:
            logger.error(
                "[MCP] MCP_SERVER_BASE_URL may be misconfigured (%s):\n  - %s\n"
                "  Configured: MCP_SERVER_BASE_URL=%s\n"
                "  The MCP client OAuth flow will likely fail. Ensure the external URL "
                "matches the proxy routing to the backend's /mcp/ mount point.",
                base_url, "\n  - ".join(issues), base_url,
            )
        else:
            logger.info("[MCP] MCP_SERVER_BASE_URL validated against incoming request (host=%s, root_path=%s)", request_host, root_path)

    async def get_or_create(self, connector_id: str) -> ASGIApp | None:
        """Get or lazily create an ASGI app for the given connector.

        When creating a new app, also starts the session manager's run()
        context in the parent task group so the anyio task group is initialized.
        """
        if connector_id in self._servers:
            return self._servers[connector_id]

        # Validate connector exists and is active
        with DBSession(engine) as db:
            connector = db.get(MCPConnector, uuid.UUID(connector_id))
            if not connector or not connector.is_active:
                return None

        mcp_server = create_mcp_server_for_connector(connector_id)
        asgi_app = mcp_server.streamable_http_app()

        # Start the session manager's run() context in our parent task group.
        # This initializes the anyio task group that StreamableHTTPSessionManager
        # needs for spawning per-session/per-request MCP server tasks.
        session_manager = mcp_server.session_manager

        async def _run_session_manager(*, task_status=anyio.TASK_STATUS_IGNORED):
            async with session_manager.run():
                task_status.started()
                # Keep alive until cancelled by parent task group on shutdown/remove
                await anyio.sleep_forever()

        if self._task_group is None:
            logger.error("MCPServerRegistry.run() is not active — cannot start session manager")
            return None

        await self._task_group.start(_run_session_manager)

        self._servers[connector_id] = asgi_app
        self._mcp_instances[connector_id] = mcp_server
        logger.info(f"Created MCP server for connector {connector_id}")
        return asgi_app

    def remove(self, connector_id: str) -> None:
        """Evict a connector's MCP server (on deactivation/deletion)."""
        removed = False
        if connector_id in self._servers:
            del self._servers[connector_id]
            removed = True
        if connector_id in self._mcp_instances:
            del self._mcp_instances[connector_id]
            removed = True
        if removed:
            logger.info(f"Removed MCP server for connector {connector_id}")

    def clear(self) -> None:
        """Clear all servers (for shutdown). Called by the run() context on exit."""
        self._servers.clear()
        self._mcp_instances.clear()
        logger.info("Cleared all MCP servers")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """ASGI dispatcher — extract connector_id from path, delegate to per-connector app."""
        if scope["type"] not in ("http", "websocket"):
            return

        self._validate_base_url_on_request(scope)

        # Use get_route_path to get the effective path with root_path prefix stripped
        # (Starlette Mount sets root_path but does NOT modify scope["path"])
        path = get_route_path(scope)
        # Path format: /{connector_id}/... (root_path prefix stripped by get_route_path)
        parts = path.strip("/").split("/", 1)
        if not parts or not parts[0]:
            response = JSONResponse({"detail": "Connector ID required"}, status_code=404)
            await response(scope, receive, send)
            return

        connector_id_str = parts[0]

        # Validate UUID format
        try:
            uuid.UUID(connector_id_str)
        except ValueError:
            response = JSONResponse({"detail": "Invalid connector ID"}, status_code=404)
            await response(scope, receive, send)
            return

        # Get or create the per-connector app (async — may start session manager)
        app = await self.get_or_create(connector_id_str)
        if app is None:
            response = JSONResponse({"detail": "Connector not found or inactive"}, status_code=404)
            await response(scope, receive, send)
            return

        # Extract MCP session ID from headers (used for stale check + context var)
        mcp_session_id_from_header = None
        for name, value in scope.get("headers", []):
            if name == b"mcp-session-id":
                mcp_session_id_from_header = value.decode("ascii", errors="replace")
                break

        method = scope.get("method", "?")
        logger.debug(
            "[MCP] %s %s | connector=%s | mcp_session_id=%s",
            method, path, connector_id_str, mcp_session_id_from_header or "(none)",
        )

        # Per MCP spec §Session Management: return 404 for stale session IDs
        # so the client re-initializes with a fresh InitializeRequest.
        mcp_instance = self._mcp_instances.get(connector_id_str)
        if mcp_instance and mcp_session_id_from_header:
            known_sessions = getattr(
                mcp_instance.session_manager, "_server_instances", {}
            )
            if mcp_session_id_from_header not in known_sessions:
                logger.warning(
                    "[MCP] Stale MCP session %s for connector %s — returning 404 "
                    "(known: %s)",
                    mcp_session_id_from_header,
                    connector_id_str,
                    list(known_sessions.keys()),
                )
                response = JSONResponse(
                    {"detail": "Session not found"}, status_code=404
                )
                await response(scope, receive, send)
                return

        # Set context vars for tool handlers
        token_conn = mcp_connector_id_var.set(connector_id_str)
        token_sess = mcp_session_id_var.set(mcp_session_id_from_header)
        try:
            # Rewrite path to strip the connector_id prefix
            remaining_path = f"/{parts[1]}" if len(parts) > 1 else "/"
            scope = dict(scope)
            scope["path"] = remaining_path
            original_root_path = scope.get("root_path", "")
            scope["root_path"] = f"{original_root_path}/mcp/{connector_id_str}"

            await app(scope, receive, send)
        finally:
            mcp_connector_id_var.reset(token_conn)
            mcp_session_id_var.reset(token_sess)


# Singleton registry instance
mcp_registry = MCPServerRegistry()
