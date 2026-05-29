"""
Credentials-specific test fixtures.

Provides agent-related stubs needed by tests that involve credential-to-agent
linking and environment sync.

``patch_storage_dirs`` is included so that tests which publish bundles
(e.g. deletion-impact scenarios that need PBP wiring) redirect disk writes
to a temporary directory instead of writing to the real storage directories.
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


@pytest.fixture(autouse=True)
def patch_create_session(db):
    """Patch create_session at all service import sites (including session/task services)."""
    with patched_create_sessions(db, CREATE_SESSION_TARGETS_AGENT):
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


@pytest.fixture(autouse=True)
def patch_storage_dirs(tmp_path_factory):
    """Redirect bundle + app-data storage to a tmp tree (no host disk writes)."""
    with patched_storage_dirs(tmp_path_factory):
        yield
