"""
MCP resource registration for workspace file access.

Exposes agent workspace files as MCP resources so clients (Claude Desktop,
Cursor, etc.) can browse and read them via the standard MCP resources/list
and resources/read protocol operations.

URI scheme:
    workspace://files/{path}            — files in files/ folder
    workspace://uploads/{path}          — files in uploads/ folder
    workspace://docs/{path}             — files in docs/ folder
    workspace://scripts/{path}          — files in scripts/ folder

Only a safe subset of workspace folders is exposed. Sensitive folders
(credentials/, databases/, knowledge/, logs/) are excluded.

The key design challenge: Claude Desktop (and most MCP clients) only display
concrete resources from `resources/list`, not templates from
`resources/templates/list`. So we override `list_resources()` to dynamically
enumerate actual workspace files from the agent environment, returning them
as concrete resources. The `get_resource()` override handles reading any
workspace URI (including nested paths) on demand.
"""
import json
import logging
import mimetypes
import uuid
from urllib.parse import urlparse

from mcp.server.fastmcp.resources.base import Resource
from mcp.server.fastmcp.resources.resource_manager import ResourceManager
from mcp.server.fastmcp.resources.types import FunctionResource

from app.core.db import create_session
from app.services.mcp.mcp_connector_service import MCPConnectorService
from app.services.environments.environment_service import EnvironmentService
from app.mcp.server import mcp_connector_id_var

logger = logging.getLogger(__name__)

ALLOWED_FOLDERS: set[str] = {"files", "uploads", "scripts"}

MAX_RESOURCE_SIZE_BYTES: int = 10 * 1024 * 1024  # 10 MB

# Text MIME type prefixes/types — everything else is treated as binary
_TEXT_MIME_PREFIXES = ("text/",)
_TEXT_MIME_TYPES = {
    "application/json",
    "application/xml",
    "application/javascript",
    "application/x-yaml",
    "application/yaml",
    "application/toml",
    "application/x-sh",
    "application/sql",
    "application/csv",
    "application/x-python-code",
}


# ── URI Parsing ──────────────────────────────────────────────────────────────


def _parse_workspace_uri(uri_str: str) -> tuple[str, str]:
    """Parse a workspace URI into (folder, path).

    Args:
        uri_str: e.g. "workspace://files/subfolder/report.csv"

    Returns:
        ("files", "subfolder/report.csv")

    Raises:
        ValueError: if the URI scheme is not workspace://, the folder is not
                    in ALLOWED_FOLDERS, or the path is missing.
    """
    parsed = urlparse(uri_str)
    if parsed.scheme != "workspace":
        raise ValueError(f"Not a workspace URI: {uri_str}")

    # netloc is the folder (e.g. "files"), path is the rest (e.g. "/subfolder/report.csv")
    folder = parsed.netloc
    if not folder:
        raise ValueError(f"No folder in workspace URI: {uri_str}")

    if folder not in ALLOWED_FOLDERS:
        raise ValueError(
            f"Folder '{folder}' is not accessible. "
            f"Allowed folders: {', '.join(sorted(ALLOWED_FOLDERS))}"
        )

    # Strip leading slash from path
    path = parsed.path.lstrip("/")
    if not path:
        raise ValueError(f"No file path in workspace URI: {uri_str}")

    return folder, path


def _guess_mime_type(path: str) -> str:
    """Guess MIME type from file path, defaulting to application/octet-stream."""
    mime, _ = mimetypes.guess_type(path)
    return mime or "application/octet-stream"


def _is_text_mime(mime_type: str) -> bool:
    """Check if a MIME type represents text content."""
    if any(mime_type.startswith(prefix) for prefix in _TEXT_MIME_PREFIXES):
        return True
    return mime_type in _TEXT_MIME_TYPES


# ── Adapter Resolution ───────────────────────────────────────────────────────


async def _get_adapter_for_connector():
    """Resolve context var → connector → agent → environment → adapter.

    Same resolution pattern as tools.py:_handle_send_message_inner.

    Returns:
        The environment adapter for the connector's active environment.

    Raises:
        ValueError: if the connector context is unavailable or invalid.
    """
    connector_id_str = mcp_connector_id_var.get(None)
    if not connector_id_str:
        raise ValueError("No connector context available")

    connector_id = uuid.UUID(connector_id_str)

    with create_session() as db:
        connector, agent, environment = MCPConnectorService.resolve_connector_context(
            db, connector_id,
        )

    lifecycle_manager = EnvironmentService.get_lifecycle_manager()
    adapter = lifecycle_manager.get_adapter(environment)
    return adapter


# ── Tree Helpers ─────────────────────────────────────────────────────────────


def _collect_files_from_tree(tree: dict) -> list[tuple[str, str, int | None]]:
    """Walk workspace tree and collect all files from allowed folders.

    Args:
        tree: Workspace tree dict from adapter (keys are folder names,
              values are FileNode dicts with name, type, path, children).

    Returns:
        List of (workspace_path, name, size) tuples for all files.
        Example: [("files/report.csv", "report.csv", 1234), ...]
    """
    results: list[tuple[str, str, int | None]] = []

    def _walk(node: dict) -> None:
        node_type = node.get("type", "")
        if node_type == "file":
            path = node.get("path", "")
            name = node.get("name", "")
            size = node.get("size")
            if path and name:
                results.append((path, name, size))
        elif node_type == "folder":
            for child in node.get("children") or []:
                _walk(child)

    for folder_name, folder_node in tree.items():
        if folder_name not in ALLOWED_FOLDERS:
            continue
        if isinstance(folder_node, dict):
            _walk(folder_node)

    return results


# ── Resource Readers ─────────────────────────────────────────────────────────


async def _read_workspace_tree() -> str:
    """Read the workspace tree, filtered to allowed folders.

    Returns:
        JSON string with the filtered workspace tree.
    """
    adapter = await _get_adapter_for_connector()
    tree = await adapter.get_workspace_tree()

    # Filter tree to only include allowed folders
    filtered = {}
    for key, value in tree.items():
        # Top-level keys may be folder names or metadata like "summaries"
        if key in ALLOWED_FOLDERS or key == "summaries":
            filtered[key] = value

    return json.dumps(filtered, indent=2, default=str)


async def _read_workspace_file(workspace_path: str) -> str | bytes:
    """Read a single file from the workspace.

    Args:
        workspace_path: relative path from workspace root, e.g. "files/report.csv"

    Returns:
        str for text files, bytes for binary files.

    Raises:
        ValueError: if the file exceeds MAX_RESOURCE_SIZE_BYTES.
    """
    adapter = await _get_adapter_for_connector()
    chunks: list[bytes] = []
    total_size = 0

    async for chunk in adapter.download_workspace_item(workspace_path):
        total_size += len(chunk)
        if total_size > MAX_RESOURCE_SIZE_BYTES:
            raise ValueError(
                f"File exceeds maximum resource size of "
                f"{MAX_RESOURCE_SIZE_BYTES // (1024 * 1024)}MB. "
                f"Use the file download tool instead."
            )
        chunks.append(chunk)

    content = b"".join(chunks)
    mime_type = _guess_mime_type(workspace_path)

    if _is_text_mime(mime_type):
        return content.decode("utf-8", errors="replace")
    return content


# ── Custom Resource Manager ──────────────────────────────────────────────────


class WorkspaceResourceManager(ResourceManager):
    """Extended resource manager with dynamic workspace file listing.

    Overrides two key methods:

    1. list_resources() — dynamically enumerates actual workspace files from
       the agent environment (via adapter.get_workspace_tree()), returning
       them as concrete FunctionResource instances. This ensures Claude Desktop
       and other MCP clients that only read `resources/list` can see and
       select individual files.

    2. get_resource() — intercepts workspace:// URIs and parses multi-segment
       paths manually. The default SDK template matching uses regex [^/]+ per
       {param}, which doesn't match nested paths like "docs/sub/README.md".
    """

    def list_resources(self) -> list[Resource]:
        """Return statically registered resources only.

        Dynamic workspace files are listed via list_resources_async(), which
        the FastMCP server calls through our patched list_resources handler.
        """
        return list(self._resources.values())

    async def list_resources_async(self) -> list[Resource]:
        """Dynamically enumerate workspace files as concrete resources.

        Fetches the workspace tree from the agent environment and converts
        each file into a FunctionResource. Falls back to static resources
        only if the tree fetch fails (e.g., environment not running).
        """
        static = list(self._resources.values())

        try:
            adapter = await _get_adapter_for_connector()
            tree = await adapter.get_workspace_tree()
        except Exception as e:
            logger.debug("Could not fetch workspace tree for resource listing: %s", e)
            return static

        files = _collect_files_from_tree(tree)
        dynamic: list[Resource] = []

        for workspace_path, name, size in files:
            # Determine folder from path (e.g. "files/report.csv" → "files")
            folder = workspace_path.split("/", 1)[0]
            uri = f"workspace://{workspace_path}"
            mime_type = _guess_mime_type(name)

            desc = f"Workspace file: {workspace_path}"
            if size is not None:
                if size < 1024:
                    desc += f" ({size} B)"
                elif size < 1024 * 1024:
                    desc += f" ({size / 1024:.1f} KB)"
                else:
                    desc += f" ({size / (1024 * 1024):.1f} MB)"

            dynamic.append(FunctionResource(
                uri=uri,
                name=workspace_path,
                description=desc,
                mime_type=mime_type,
                fn=lambda wp=workspace_path: _read_workspace_file(wp),
            ))

        return static + dynamic

    async def get_resource(self, uri, context=None) -> "FunctionResource":
        uri_str = str(uri)

        # Check concrete resources first
        if uri_str in self._resources:
            return self._resources[uri_str]

        # Intercept workspace:// URIs for multi-segment path support
        if uri_str.startswith("workspace://"):
            try:
                folder, path = _parse_workspace_uri(uri_str)
            except ValueError:
                # Not a valid workspace file URI — fall through to default
                pass
            else:
                workspace_path = f"{folder}/{path}"
                mime_type = _guess_mime_type(path)

                async def _read(wp=workspace_path):
                    return await _read_workspace_file(wp)

                return FunctionResource(
                    uri=uri_str,
                    name=path,
                    description=f"Workspace file: {workspace_path}",
                    mime_type=mime_type,
                    fn=_read,
                )

        # Fall through to default template/resource matching
        return await super().get_resource(uri, context)


# ── Registration ─────────────────────────────────────────────────────────────


def register_mcp_resources(server) -> None:
    """Register workspace resources on a FastMCP server instance.

    Replaces the default resource manager with WorkspaceResourceManager
    and patches the server's list_resources handler to use the async
    version that dynamically enumerates workspace files.
    """
    # Replace resource manager with our custom one
    resource_manager = WorkspaceResourceManager(
        warn_on_duplicate_resources=server._resource_manager.warn_on_duplicate_resources,
    )
    server._resource_manager = resource_manager

    # Re-register the list_resources handler on the low-level MCP server.
    # FastMCP._setup_handlers() already registered server.list_resources via
    # a decorator that captures the original bound method in a closure.
    # Patching server.list_resources after init doesn't affect that closure,
    # so we must replace the handler directly in request_handlers.
    from mcp import types as mcp_types

    async def _dynamic_list_resources():
        resources = await resource_manager.list_resources_async()
        return [_resource_to_mcp(r) for r in resources]

    # Use the SDK's own decorator to re-register, which correctly wraps the handler
    server._mcp_server.list_resources()(_dynamic_list_resources)

    # Register templates for discovery (resources/templates/list).
    # Clients that support templates can use these; actual file resolution
    # is handled by WorkspaceResourceManager.get_resource().
    for folder in sorted(ALLOWED_FOLDERS):
        @server.resource(
            f"workspace://{folder}/{{path}}",
            name=f"workspace_{folder}",
            description=f"Read a file from the workspace {folder}/ folder.",
            mime_type="application/octet-stream",
        )
        async def _template_placeholder(path: str) -> str:
            # This is only used for template registration / discovery.
            # Actual reads go through WorkspaceResourceManager.get_resource().
            return await _read_workspace_file(f"{folder}/{path}")


def _resource_to_mcp(resource: Resource):
    """Convert a FastMCP Resource to MCP protocol Resource type."""
    from mcp.types import Resource as MCPResource
    return MCPResource(
        uri=resource.uri,
        name=resource.name or "",
        title=getattr(resource, "title", None),
        description=resource.description,
        mimeType=resource.mime_type,
        icons=getattr(resource, "icons", None),
        annotations=getattr(resource, "annotations", None),
        _meta=getattr(resource, "meta", None),
    )
