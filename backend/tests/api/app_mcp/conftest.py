"""
App MCP test fixtures.

Agent creation requires a live environment adapter stub and patched sessions.
These fixtures mirror the agents conftest to keep app_mcp tests self-contained.
"""
import pytest
from tests.utils.fixtures import (
    patch_asyncio_to_thread,
    setup_default_credentials,
    patched_create_sessions,
    patched_background_tasks,
    patched_external_services,
    patched_storage_dirs,
    setup_environment_adapter,
    teardown_environment_adapter,
    CREATE_SESSION_TARGETS_AGENT,
    BACKGROUND_TASK_TARGETS_FULL,
)

# Extend the standard agent targets with the App MCP request handler's
# create_session import so it uses the test transaction.
CREATE_SESSION_TARGETS_APP_MCP = CREATE_SESSION_TARGETS_AGENT + [
    "app.services.app_mcp.app_mcp_request_handler.create_session",
]


@pytest.fixture(autouse=True)
def patch_create_session(db):
    """Patch create_session at all service import sites (including app_mcp handler)."""
    with patched_create_sessions(db, CREATE_SESSION_TARGETS_APP_MCP):
        yield


@pytest.fixture(autouse=True)
def patch_environment_adapter(tmp_path_factory):
    """Patch lifecycle manager to use EnvironmentTestAdapter instead of Docker."""
    lm = setup_environment_adapter(tmp_path_factory)
    yield lm
    teardown_environment_adapter()


BACKGROUND_TASK_TARGETS_APP_MCP = BACKGROUND_TASK_TARGETS_FULL + [
    "app.services.app_mcp.app_mcp_request_handler.create_task_with_error_logging",
]


@pytest.fixture(autouse=True)
def background_tasks():
    """Collect background tasks for deferred execution."""
    with patched_background_tasks(BACKGROUND_TASK_TARGETS_APP_MCP):
        yield


@pytest.fixture(autouse=True)
def patch_external_services():
    """Mock external service calls (OAuth refresh, Socket.IO, LLM providers)."""
    with patched_external_services(mock_ai_functions=True, mock_a2a_skills=True):
        yield


@pytest.fixture(autouse=True)
def patch_storage_dirs(tmp_path_factory):
    """Redirect bundle + app-data storage to a tmp tree (no host disk writes)."""
    with patched_storage_dirs(tmp_path_factory):
        yield
