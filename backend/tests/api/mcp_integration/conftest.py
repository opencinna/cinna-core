"""
MCP integration test fixtures.

Same infrastructure as agents/conftest.py plus patches for:
- MCP OAuth routes `create_session` so they use the test DB session
- MCP tools `create_session` so tool handlers stay on the test tx
- MCP_SERVER_BASE_URL so resource URLs are predictable
"""
import pytest
from unittest.mock import patch

from tests.utils.db_proxy import NonClosingSessionProxy
from tests.utils.fixtures import (
    patch_asyncio_to_thread,
    setup_default_credentials,
    patched_create_sessions,
    patched_background_tasks,
    patched_external_services,
    setup_environment_adapter,
    teardown_environment_adapter,
    CREATE_SESSION_TARGETS_AGENT,
    BACKGROUND_TASK_TARGETS_FULL,
)

MCP_BASE_URL = "http://localhost:8000/mcp"


@pytest.fixture(autouse=True)
def patch_mcp_server_base_url():
    """Set MCP_SERVER_BASE_URL so all tests get predictable resource URLs."""
    with patch("app.core.config.settings.MCP_SERVER_BASE_URL", MCP_BASE_URL):
        yield


@pytest.fixture(autouse=True)
def patch_create_session(db):
    """Patch create_session at all service import sites, including MCP routes."""
    with patched_create_sessions(db, CREATE_SESSION_TARGETS_AGENT + [
        "app.mcp.oauth_routes.create_session",
        "app.mcp.tools.create_session",
        "app.mcp.upload_routes.create_session",
    ]):
        yield


@pytest.fixture(autouse=True)
def patch_environment_adapter(tmp_path_factory):
    """Patch lifecycle manager to use EnvironmentTestAdapter instead of Docker."""
    lm = setup_environment_adapter(tmp_path_factory)
    yield lm
    teardown_environment_adapter()


@pytest.fixture(autouse=True)
def background_tasks():
    """Collect background tasks for deferred execution."""
    with patched_background_tasks(BACKGROUND_TASK_TARGETS_FULL + [
        "app.mcp.request_handler.create_task_with_error_logging",
    ]):
        yield


@pytest.fixture(autouse=True)
def patch_external_services():
    """Mock external service calls (OAuth refresh, Socket.IO, LLM providers)."""
    with patched_external_services(mock_ai_functions=True, mock_a2a_skills=True):
        yield


@pytest.fixture(autouse=True)
def patch_mcp_registry_remove():
    """Prevent mcp_registry.remove from crashing when no real MCP servers exist."""
    with patch("app.mcp.server.mcp_registry") as mock_registry:
        mock_registry.remove = lambda cid: None
        yield
