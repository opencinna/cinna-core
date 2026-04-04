"""
Agent environments test fixtures.

Provides session proxy, environment adapter stub, background task collector,
and external service mocks so environment tests run real service logic without Docker.
Uses a persistent adapter so tests can inspect the adapter's call history.
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
    CREATE_SESSION_TARGETS_BASE,
    BACKGROUND_TASK_TARGETS_BASE,
)

# Additional create_session patch target for the environment status scheduler,
# which opens its own session independently of the service layer.
_EXTRA_SESSION_TARGETS = [
    "app.services.environments.environment_status_scheduler.create_session",
]


@pytest.fixture(autouse=True)
def patch_create_session(db):
    """Patch create_session at all service import sites, including the scheduler."""
    with patched_create_sessions(db, CREATE_SESSION_TARGETS_BASE + _EXTRA_SESSION_TARGETS):
        yield


@pytest.fixture(autouse=True)
def patch_environment_adapter(tmp_path_factory):
    """Patch lifecycle manager to use EnvironmentTestAdapter instead of Docker.

    Uses a persistent adapter so tests can inspect call history via lm._test_adapter.
    """
    lm = setup_environment_adapter(tmp_path_factory, persistent_adapter=True)
    yield lm
    teardown_environment_adapter()


@pytest.fixture(autouse=True)
def background_tasks():
    """Collect background tasks for deferred execution."""
    with patched_background_tasks(BACKGROUND_TASK_TARGETS_BASE):
        yield


@pytest.fixture(autouse=True)
def patch_external_services():
    """Mock external service calls (OAuth refresh, Socket.IO)."""
    with patched_external_services():
        yield
