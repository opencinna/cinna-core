"""
External API test fixtures.

Agents auto-create Docker environments when created, so we need the standard
environment adapter stub and session patches — same as other agent-related
test domains.

The external_a2a route's create_session is patched so the A2A handler
uses the test transaction (same pattern as tests/api/a2a_integration/conftest.py
which patches "app.api.routes.a2a.create_session").
"""
import pytest
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

# Re-export importable autouse fixtures
patch_asyncio_to_thread = patch_asyncio_to_thread
setup_default_credentials = setup_default_credentials


@pytest.fixture(autouse=True)
def patch_create_session(db):
    """Patch create_session at all service import sites, including the external A2A route.

    ``app.services.app_mcp.app_mcp_request_handler.create_session`` is
    included because ``external_sessions_test.py``'s app_mcp target-derivation
    scenarios call ``AppMCPRequestHandler.handle_send_message`` directly
    (there is no HTTP route for App MCP — it is an MCP tool-call surface),
    mirroring ``tests/api/app_mcp/conftest.py``'s own extension of this list.
    Without it, that handler's ``with create_session() as db:`` opens a real
    session on the real engine outside the test transaction instead of the
    test's own savepoint-scoped one.
    """
    with patched_create_sessions(db, CREATE_SESSION_TARGETS_AGENT + [
        "app.api.routes.external_a2a.create_session",
        "app.services.app_mcp.app_mcp_request_handler.create_session",
    ]):
        yield


@pytest.fixture(autouse=True)
def patch_background_tasks():
    """Collect background tasks instead of scheduling them.

    ``app.services.app_mcp.app_mcp_request_handler.create_task_with_error_logging``
    is its own patch target (not covered by patching ``app.utils``'s copy —
    ``from X import Y`` binds a local reference at the import site) for the
    same reason ``create_session`` above needs one: the handler schedules a
    session-title background task on every new session, and this domain now
    reaches that handler directly. Mirrors
    ``tests/api/app_mcp/conftest.py``'s ``BACKGROUND_TASK_TARGETS_APP_MCP``.
    """
    with patched_background_tasks(BACKGROUND_TASK_TARGETS_FULL + [
        "app.services.app_mcp.app_mcp_request_handler.create_task_with_error_logging",
    ]):
        yield


@pytest.fixture(autouse=True)
def patch_environment_adapter(tmp_path_factory):
    """Use the test environment adapter instead of Docker."""
    lm = setup_environment_adapter(tmp_path_factory)
    yield lm
    teardown_environment_adapter()


@pytest.fixture(autouse=True)
def patch_external_services():
    """Mock external service calls (OAuth refresh, Socket.IO, LLM providers)."""
    with patched_external_services(mock_ai_functions=True, mock_a2a_skills=True):
        yield
