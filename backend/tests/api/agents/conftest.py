"""
Agent-specific test fixtures.

Provides session proxy, environment adapter stub, background task collector,
and external service mocks so agent tests run real service logic without Docker.
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


@pytest.fixture(autouse=True)
def patch_create_session(db):
    """Patch create_session at all service import sites."""
    with patched_create_sessions(db, CREATE_SESSION_TARGETS_AGENT + [
        "app.services.agents.commands.session_recover_command.create_db_session",
        "app.services.agents.commands.session_reset_command.create_db_session",
        "app.services.agents.commands.webapp_command.create_session",
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
    with patched_background_tasks(BACKGROUND_TASK_TARGETS_FULL):
        yield


@pytest.fixture(autouse=True)
def patch_external_services():
    """Mock external service calls (OAuth refresh, Socket.IO, LLM providers)."""
    with patched_external_services(mock_ai_functions=True, mock_a2a_skills=True):
        yield
