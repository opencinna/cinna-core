"""
MCP workspace resource integration tests.

Verifies that the MCP resource layer correctly:
  - Parses workspace:// URIs with folder validation and nested paths
  - Detects MIME types for common file extensions
  - Reads workspace files (text and binary) via the adapter
  - Enforces the max resource size limit
  - Dynamically lists workspace files via list_resources_async()
  - Collects files from the workspace tree structure
  - Handles multi-segment paths through WorkspaceResourceManager
  - Rejects access to blocked folders (credentials, databases, etc.)
  - Reports errors for missing connector context and inactive connectors

These tests call the resource functions directly with the adapter stubbed,
following the same pattern as test_mcp_file_upload.py.

Pure-logic helper unit tests (_parse_workspace_uri, _logical_to_disk_path,
_disk_to_logical_path, _guess_mime_type, _is_text_mime, _collect_files_from_tree)
live in tests/unit/test_mcp_resources_helpers.py. This file keeps the
adapter-backed read/list flows that need a connector + stubbed environment.
"""
import asyncio
import json
from unittest.mock import AsyncMock, patch, MagicMock

from fastapi.testclient import TestClient
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.resources.types import FunctionResource

from app.mcp.resources import (
    ALLOWED_FOLDERS,
    WorkspaceResourceManager,
    register_mcp_resources,
    _read_workspace_tree,
    _read_workspace_file,
    _get_adapter_for_connector,
)
from app.mcp.server import mcp_connector_id_var
from tests.utils.agent import create_agent_via_api, get_agent
from tests.utils.background_tasks import drain_tasks
from tests.utils.mcp import create_mcp_connector, update_mcp_connector


# ── Helpers ──────────────────────────────────────────────────────────────────


def _setup_agent_with_connector(
    client: TestClient,
    token_headers: dict[str, str],
    agent_name: str = "MCP Resource Agent",
    connector_name: str = "Resource Connector",
) -> tuple[dict, dict]:
    """Create agent + connector. Returns (agent, connector)."""
    agent = create_agent_via_api(client, token_headers, name=agent_name)
    drain_tasks()
    agent = get_agent(client, token_headers, agent["id"])
    connector = create_mcp_connector(
        client, token_headers, agent["id"],
        name=connector_name,
    )
    return agent, connector


def _run_async(coro):
    """Run an async function synchronously."""
    return asyncio.run(coro)


def _run_with_connector_context(connector_id: str, coro_fn):
    """Run an async function with the connector context var set."""
    async def _run():
        token = mcp_connector_id_var.set(connector_id)
        try:
            return await coro_fn()
        finally:
            mcp_connector_id_var.reset(token)
    return asyncio.run(_run())


# Sample workspace tree matching the real agent-env format (FileNode structure).
# Uploaded files live under app-data/uploads/ on disk but are surfaced to MCP
# clients at the legacy workspace://uploads/{path} URI by resources.py.
SAMPLE_TREE = {
    "files": {
        "name": "files", "type": "folder", "path": "files",
        "children": [
            {"name": "report.csv", "type": "file", "path": "files/report.csv", "size": 1234},
            {"name": "data", "type": "folder", "path": "files/data", "children": [
                {"name": "output.json", "type": "file", "path": "files/data/output.json", "size": 567},
            ]},
        ],
    },
    "scripts": {
        "name": "scripts", "type": "folder", "path": "scripts",
        "children": [
            {"name": "run.sh", "type": "file", "path": "scripts/run.sh", "size": 256},
        ],
    },
    "logs": {
        "name": "logs", "type": "folder", "path": "logs",
        "children": [
            {"name": "app.log", "type": "file", "path": "logs/app.log", "size": 9999},
        ],
    },
    "app-data": {
        "name": "app-data", "type": "folder", "path": "app-data",
        "children": [
            {"name": "uploads", "type": "folder", "path": "app-data/uploads", "children": [
                {"name": "photo.png", "type": "file", "path": "app-data/uploads/photo.png", "size": 89012},
            ]},
            # storage/ stays private — only app-data/uploads/ is exposed via MCP.
            {"name": "storage", "type": "folder", "path": "app-data/storage", "children": [
                {"name": "STATUS.md", "type": "file", "path": "app-data/storage/STATUS.md", "size": 42},
            ]},
        ],
    },
    "summaries": {
        "files": {"fileCount": 2, "totalSize": 1801},
        "uploads": {"fileCount": 1, "totalSize": 89012},
    },
}


# ── _read_workspace_tree Tests ───────────────────────────────────────────────


def test_read_workspace_tree_filters_to_allowed_folders(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db,
) -> None:
    """
    Workspace tree is filtered to only include allowed folders and summaries:
      1. Create agent + connector
      2. Mock adapter to return a tree with both allowed and blocked folders
      3. Read workspace tree
      4. Verify only allowed folders appear in the result
    """
    agent, connector = _setup_agent_with_connector(
        client, superuser_token_headers,
        agent_name="Tree Filter Agent",
    )
    connector_id = connector["id"]

    mock_tree = {
        "files": {"name": "files", "type": "folder", "path": "files", "children": [
            {"name": "data.csv", "type": "file", "path": "files/data.csv"},
        ]},
        "docs": {"name": "docs", "type": "folder", "path": "docs", "children": []},
        "scripts": {"name": "scripts", "type": "folder", "path": "scripts", "children": [
            {"name": "run.sh", "type": "file", "path": "scripts/run.sh"},
        ]},
        "credentials": {"name": "credentials", "type": "folder", "path": "credentials", "children": [
            {"name": "secret.key", "type": "file", "path": "credentials/secret.key"},
        ]},
        "databases": {"name": "databases", "type": "folder", "path": "databases", "children": [
            {"name": "db.sqlite", "type": "file", "path": "databases/db.sqlite"},
        ]},
        "logs": {"name": "logs", "type": "folder", "path": "logs", "children": [
            {"name": "app.log", "type": "file", "path": "logs/app.log"},
        ]},
        "app-data": {"name": "app-data", "type": "folder", "path": "app-data", "children": [
            {"name": "uploads", "type": "folder", "path": "app-data/uploads", "children": [
                {"name": "photo.jpg", "type": "file", "path": "app-data/uploads/photo.jpg"},
            ]},
            {"name": "storage", "type": "folder", "path": "app-data/storage", "children": []},
        ]},
        "summaries": {"total_files": 7},
    }

    mock_adapter = AsyncMock()
    mock_adapter.get_workspace_tree = AsyncMock(return_value=mock_tree)

    with patch("app.mcp.resources.EnvironmentService") as mock_env_svc:
        mock_lm = MagicMock()
        mock_lm.get_adapter.return_value = mock_adapter
        mock_env_svc.get_lifecycle_manager.return_value = mock_lm

        result = _run_with_connector_context(
            connector_id,
            _read_workspace_tree,
        )

    tree = json.loads(result)
    # Top-level allowed folders + the synthesised uploads alias + summaries
    # should be present.
    assert "files" in tree
    assert "scripts" in tree
    assert "uploads" in tree
    assert "summaries" in tree
    # The uploads alias must surface the app-data/uploads/ subtree, not the
    # disk root, so its node carries the photo.jpg child.
    uploads_children = tree["uploads"].get("children") or []
    assert any(c.get("name") == "photo.jpg" for c in uploads_children)
    # Blocked folders and the rest of app-data/ must NOT be present.
    assert "app-data" not in tree
    assert "docs" not in tree
    assert "credentials" not in tree
    assert "databases" not in tree
    assert "logs" not in tree


def test_read_workspace_tree_empty(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db,
) -> None:
    """Empty workspace tree returns empty JSON object."""
    agent, connector = _setup_agent_with_connector(
        client, superuser_token_headers,
        agent_name="Empty Tree Agent",
    )

    mock_adapter = AsyncMock()
    mock_adapter.get_workspace_tree = AsyncMock(return_value={})

    with patch("app.mcp.resources.EnvironmentService") as mock_env_svc:
        mock_lm = MagicMock()
        mock_lm.get_adapter.return_value = mock_adapter
        mock_env_svc.get_lifecycle_manager.return_value = mock_lm

        result = _run_with_connector_context(
            connector["id"],
            _read_workspace_tree,
        )

    assert json.loads(result) == {}


# ── _read_workspace_file Tests ───────────────────────────────────────────────


def test_read_workspace_file_text(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db,
) -> None:
    """
    Text file returns string content:
      1. Create agent + connector
      2. Mock adapter to stream text file bytes
      3. Read workspace file
      4. Verify returned as string
    """
    agent, connector = _setup_agent_with_connector(
        client, superuser_token_headers,
        agent_name="Text File Agent",
    )
    connector_id = connector["id"]

    file_content = b"name,value\nfoo,42\nbar,99\n"

    async def mock_download(path):
        yield file_content

    mock_adapter = AsyncMock()
    mock_adapter.download_workspace_item = mock_download

    with patch("app.mcp.resources.EnvironmentService") as mock_env_svc:
        mock_lm = MagicMock()
        mock_lm.get_adapter.return_value = mock_adapter
        mock_env_svc.get_lifecycle_manager.return_value = mock_lm

        result = _run_with_connector_context(
            connector_id,
            lambda: _read_workspace_file("files/data.csv"),
        )

    assert isinstance(result, str)
    assert "name,value" in result
    assert "foo,42" in result


def test_read_workspace_file_uploads_uri_translates_to_disk_path(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db,
) -> None:
    """Reading workspace://uploads/{path} must request app-data/uploads/{path} from the adapter.

    The agent-env stores user-uploaded files under ``app-data/uploads/`` on
    the per-user app-data volume. MCP clients see them at the stable
    ``workspace://uploads/{path}`` URI; the resource reader is responsible
    for translating back to the on-disk prefix before calling the adapter.
    """
    agent, connector = _setup_agent_with_connector(
        client, superuser_token_headers,
        agent_name="Uploads URI Translation Agent",
    )
    connector_id = connector["id"]

    requested_paths: list[str] = []

    async def mock_download(path):
        requested_paths.append(path)
        yield b"image-bytes"

    mock_adapter = AsyncMock()
    mock_adapter.download_workspace_item = mock_download

    with patch("app.mcp.resources.EnvironmentService") as mock_env_svc:
        mock_lm = MagicMock()
        mock_lm.get_adapter.return_value = mock_adapter
        mock_env_svc.get_lifecycle_manager.return_value = mock_lm

        _run_with_connector_context(
            connector_id,
            lambda: _read_workspace_file("uploads/photo.png"),
        )

    assert requested_paths == ["app-data/uploads/photo.png"], (
        f"Expected adapter to be called with disk path; got {requested_paths}"
    )


def test_read_workspace_file_binary(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db,
) -> None:
    """
    Binary file returns bytes content:
      1. Create agent + connector
      2. Mock adapter to stream binary file bytes (PNG header)
      3. Read workspace file
      4. Verify returned as bytes
    """
    agent, connector = _setup_agent_with_connector(
        client, superuser_token_headers,
        agent_name="Binary File Agent",
    )
    connector_id = connector["id"]

    # PNG file signature
    png_content = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100

    async def mock_download(path):
        yield png_content

    mock_adapter = AsyncMock()
    mock_adapter.download_workspace_item = mock_download

    with patch("app.mcp.resources.EnvironmentService") as mock_env_svc:
        mock_lm = MagicMock()
        mock_lm.get_adapter.return_value = mock_adapter
        mock_env_svc.get_lifecycle_manager.return_value = mock_lm

        result = _run_with_connector_context(
            connector_id,
            lambda: _read_workspace_file("uploads/image.png"),
        )

    assert isinstance(result, bytes)
    assert result == png_content


def test_read_workspace_file_chunked(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db,
) -> None:
    """Multiple chunks are concatenated correctly."""
    agent, connector = _setup_agent_with_connector(
        client, superuser_token_headers,
        agent_name="Chunked File Agent",
    )
    connector_id = connector["id"]

    async def mock_download(path):
        yield b"chunk1-"
        yield b"chunk2-"
        yield b"chunk3"

    mock_adapter = AsyncMock()
    mock_adapter.download_workspace_item = mock_download

    with patch("app.mcp.resources.EnvironmentService") as mock_env_svc:
        mock_lm = MagicMock()
        mock_lm.get_adapter.return_value = mock_adapter
        mock_env_svc.get_lifecycle_manager.return_value = mock_lm

        result = _run_with_connector_context(
            connector_id,
            lambda: _read_workspace_file("files/report.txt"),
        )

    assert result == "chunk1-chunk2-chunk3"


def test_read_workspace_file_size_limit(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db,
) -> None:
    """
    Files exceeding MAX_RESOURCE_SIZE_BYTES raise ValueError:
      1. Create agent + connector
      2. Mock adapter to stream oversized content
      3. Verify ValueError is raised
    """
    agent, connector = _setup_agent_with_connector(
        client, superuser_token_headers,
        agent_name="Size Limit Agent",
    )
    connector_id = connector["id"]

    # Stream chunks that exceed the limit
    chunk = b"x" * (1024 * 1024)  # 1MB per chunk

    async def mock_download(path):
        for _ in range(11):  # 11MB > 10MB limit
            yield chunk

    mock_adapter = AsyncMock()
    mock_adapter.download_workspace_item = mock_download

    with patch("app.mcp.resources.EnvironmentService") as mock_env_svc:
        mock_lm = MagicMock()
        mock_lm.get_adapter.return_value = mock_adapter
        mock_env_svc.get_lifecycle_manager.return_value = mock_lm

        import pytest
        with pytest.raises(ValueError, match="maximum resource size"):
            _run_with_connector_context(
                connector_id,
                lambda: _read_workspace_file("files/huge.bin"),
            )


# ── WorkspaceResourceManager Tests ──────────────────────────────────────────


def test_workspace_resource_manager_concrete_resource():
    """WorkspaceResourceManager resolves concrete resources by URI."""
    manager = WorkspaceResourceManager()

    # Add a concrete resource
    resource = FunctionResource(
        uri="workspace://test/something",
        name="test_resource",
        mime_type="application/json",
        fn=lambda: '{"test": true}',
    )
    manager.add_resource(resource)

    result = _run_async(manager.get_resource("workspace://test/something"))
    assert str(result.uri) == "workspace://test/something"


def test_workspace_resource_manager_multi_segment_path(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db,
) -> None:
    """
    WorkspaceResourceManager resolves multi-segment workspace file paths:
      1. Create agent + connector
      2. Create WorkspaceResourceManager
      3. Request resource with nested path
      4. Verify FunctionResource is returned with correct attributes
    """
    agent, connector = _setup_agent_with_connector(
        client, superuser_token_headers,
        agent_name="Multi-Seg Agent",
    )
    connector_id = connector["id"]

    manager = WorkspaceResourceManager()

    async def _get():
        token = mcp_connector_id_var.set(connector_id)
        try:
            return await manager.get_resource("workspace://scripts/sub/folder/run.sh")
        finally:
            mcp_connector_id_var.reset(token)

    result = _run_async(_get())
    assert result is not None
    assert result.name == "sub/folder/run.sh"
    assert result.mime_type == "text/x-sh"
    assert "scripts/sub/folder/run.sh" in (result.description or "")


def test_workspace_resource_manager_simple_path(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db,
) -> None:
    """WorkspaceResourceManager resolves simple single-level paths."""
    agent, connector = _setup_agent_with_connector(
        client, superuser_token_headers,
        agent_name="Simple Path Agent",
    )
    connector_id = connector["id"]

    manager = WorkspaceResourceManager()

    async def _get():
        token = mcp_connector_id_var.set(connector_id)
        try:
            return await manager.get_resource("workspace://files/data.csv")
        finally:
            mcp_connector_id_var.reset(token)

    result = _run_async(_get())
    assert result is not None
    assert result.name == "data.csv"


def test_workspace_resource_manager_blocked_folder(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db,
) -> None:
    """WorkspaceResourceManager rejects blocked folders with ValueError."""
    agent, connector = _setup_agent_with_connector(
        client, superuser_token_headers,
        agent_name="Blocked Folder Agent",
    )
    connector_id = connector["id"]

    manager = WorkspaceResourceManager()

    async def _get():
        token = mcp_connector_id_var.set(connector_id)
        try:
            return await manager.get_resource("workspace://credentials/key.json")
        finally:
            mcp_connector_id_var.reset(token)

    import pytest
    with pytest.raises(ValueError):
        _run_async(_get())


def test_workspace_resource_manager_list_resources_async(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db,
) -> None:
    """
    list_resources_async dynamically enumerates workspace files:
      1. Create agent + connector
      2. Mock adapter to return a workspace tree with files
      3. Call list_resources_async
      4. Verify all allowed-folder files appear as concrete resources
    """
    agent, connector = _setup_agent_with_connector(
        client, superuser_token_headers,
        agent_name="List Resources Agent",
    )
    connector_id = connector["id"]

    manager = WorkspaceResourceManager()

    mock_adapter = AsyncMock()
    mock_adapter.get_workspace_tree = AsyncMock(return_value=SAMPLE_TREE)

    with patch("app.mcp.resources.EnvironmentService") as mock_env_svc:
        mock_lm = MagicMock()
        mock_lm.get_adapter.return_value = mock_adapter
        mock_env_svc.get_lifecycle_manager.return_value = mock_lm

        resources = _run_with_connector_context(
            connector_id,
            manager.list_resources_async,
        )

    uris = [str(r.uri) for r in resources]
    # Files from allowed folders should be present.
    assert "workspace://files/report.csv" in uris
    assert "workspace://files/data/output.json" in uris
    assert "workspace://scripts/run.sh" in uris
    # User-uploaded files surface at the logical workspace://uploads/ URI
    # (mapped from app-data/uploads/ on disk).
    assert "workspace://uploads/photo.png" in uris
    # Files from blocked folders / blocked app-data subtrees must NOT be present.
    assert "workspace://logs/app.log" not in uris
    assert "workspace://app-data/storage/STATUS.md" not in uris


def test_workspace_resource_manager_list_resources_async_includes_size(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db,
) -> None:
    """list_resources_async includes file size in resource description."""
    agent, connector = _setup_agent_with_connector(
        client, superuser_token_headers,
        agent_name="Size Desc Agent",
    )
    connector_id = connector["id"]

    manager = WorkspaceResourceManager()

    mock_adapter = AsyncMock()
    mock_adapter.get_workspace_tree = AsyncMock(return_value=SAMPLE_TREE)

    with patch("app.mcp.resources.EnvironmentService") as mock_env_svc:
        mock_lm = MagicMock()
        mock_lm.get_adapter.return_value = mock_adapter
        mock_env_svc.get_lifecycle_manager.return_value = mock_lm

        resources = _run_with_connector_context(
            connector_id,
            manager.list_resources_async,
        )

    by_uri = {str(r.uri): r for r in resources}
    # 1234 bytes → "1.2 KB"
    assert "1.2 KB" in by_uri["workspace://files/report.csv"].description
    # 89012 bytes → "86.9 KB" (uploaded file, surfaced under uploads/ alias)
    assert "KB" in by_uri["workspace://uploads/photo.png"].description


def test_workspace_resource_manager_list_resources_async_graceful_on_error(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db,
) -> None:
    """list_resources_async returns static resources if tree fetch fails."""
    agent, connector = _setup_agent_with_connector(
        client, superuser_token_headers,
        agent_name="Error Fallback Agent",
    )
    connector_id = connector["id"]

    manager = WorkspaceResourceManager()

    # Add a static resource to verify it's still returned
    static = FunctionResource(
        uri="test://static",
        name="static_resource",
        mime_type="text/plain",
        fn=lambda: "static",
    )
    manager.add_resource(static)

    mock_adapter = AsyncMock()
    mock_adapter.get_workspace_tree = AsyncMock(side_effect=Exception("env not running"))

    with patch("app.mcp.resources.EnvironmentService") as mock_env_svc:
        mock_lm = MagicMock()
        mock_lm.get_adapter.return_value = mock_adapter
        mock_env_svc.get_lifecycle_manager.return_value = mock_lm

        resources = _run_with_connector_context(
            connector_id,
            manager.list_resources_async,
        )

    uris = [str(r.uri) for r in resources]
    assert "test://static" in uris
    assert len(resources) == 1  # Only the static resource


# ── register_mcp_resources Tests ─────────────────────────────────────────────


def test_register_mcp_resources_installs_custom_manager():
    """register_mcp_resources replaces the resource manager with WorkspaceResourceManager."""
    server = FastMCP(name="test-server")
    register_mcp_resources(server)
    assert isinstance(server._resource_manager, WorkspaceResourceManager)


def test_register_mcp_resources_registers_list_resources_handler():
    """register_mcp_resources re-registers list_resources on the low-level MCP server."""
    from mcp import types as mcp_types

    server = FastMCP(name="test-server")
    # Capture the handler registered by _setup_handlers
    original_handler = server._mcp_server.request_handlers.get(mcp_types.ListResourcesRequest)
    register_mcp_resources(server)
    # The handler should have been replaced with our dynamic version
    new_handler = server._mcp_server.request_handlers.get(mcp_types.ListResourcesRequest)
    assert new_handler is not None
    assert new_handler is not original_handler


def test_register_mcp_resources_registers_folder_templates():
    """register_mcp_resources registers templates for all allowed folders."""
    server = FastMCP(name="test-server")
    register_mcp_resources(server)

    templates = server._resource_manager.list_templates()
    template_uris = [t.uri_template for t in templates]

    for folder in ALLOWED_FOLDERS:
        expected = f"workspace://{folder}/{{path}}"
        assert expected in template_uris, f"Missing template for {folder}"


# ── Error Cases ──────────────────────────────────────────────────────────────


def test_get_adapter_no_connector_context():
    """_get_adapter_for_connector raises ValueError when no context var is set."""
    import pytest
    with pytest.raises(ValueError, match="No connector context"):
        _run_async(_get_adapter_for_connector())


def test_get_adapter_inactive_connector(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db,
) -> None:
    """_get_adapter_for_connector raises ConnectorInactiveError for inactive connector."""
    agent, connector = _setup_agent_with_connector(
        client, superuser_token_headers,
        agent_name="Inactive Resource Agent",
    )
    connector_id = connector["id"]
    agent_id = agent["id"]

    # Deactivate the connector
    update_mcp_connector(
        client, superuser_token_headers, agent_id, connector_id,
        is_active=False,
    )

    import pytest
    from app.services.mcp.mcp_errors import ConnectorInactiveError
    with pytest.raises(ConnectorInactiveError):
        _run_with_connector_context(
            connector_id,
            _get_adapter_for_connector,
        )


def test_read_workspace_tree_no_context():
    """_read_workspace_tree raises ValueError when no connector context is set."""
    import pytest
    with pytest.raises(ValueError, match="No connector context"):
        _run_async(_read_workspace_tree())


def test_read_workspace_file_no_context():
    """_read_workspace_file raises ValueError when no connector context is set."""
    import pytest
    with pytest.raises(ValueError, match="No connector context"):
        _run_async(_read_workspace_file("files/test.txt"))
