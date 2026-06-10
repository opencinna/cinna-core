"""
Credentials-specific test fixtures.

Provides agent-related stubs needed by tests that involve credential-to-agent
linking and environment sync.

``patch_storage_dirs`` is included so that tests which publish bundles
(e.g. deletion-impact scenarios that need PBP wiring) redirect disk writes
to a temporary directory instead of writing to the real storage directories.

These stubs are heavy (tmp env-adapter setup, background-task collector,
external-service mocks, storage-dir redirects, ``create_session`` patching) and
only the agent/environment-touching files in this directory need them. The
pure-CRUD suites (``test_credentials.py``, ``test_credentials_sharing.py``,
``test_ssh_key_credential_create.py``) opt out by setting the module-level flag
``NEEDS_AGENT_STUBS = False`` and ``NEEDS_DEFAULT_CREDENTIALS = False`` — they
then run with only the cheap ``patch_asyncio_to_thread`` autouse.
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


def _needs_agent_stubs(request) -> bool:
    """Whether the requesting test module needs the heavy agent/env stubs.

    Defaults to True; pure-CRUD modules set ``NEEDS_AGENT_STUBS = False``.
    """
    return getattr(request.module, "NEEDS_AGENT_STUBS", True)


@pytest.fixture(autouse=True)
def patch_create_session(request, db):
    """Patch create_session at all service import sites (including session/task services)."""
    if not _needs_agent_stubs(request):
        yield
        return
    with patched_create_sessions(db, CREATE_SESSION_TARGETS_AGENT):
        yield


@pytest.fixture(autouse=True)
def patch_environment_adapter(request, tmp_path_factory):
    """Patch lifecycle manager to use EnvironmentTestAdapter instead of Docker."""
    if not _needs_agent_stubs(request):
        yield None
        return
    lm = setup_environment_adapter(tmp_path_factory)
    yield lm
    teardown_environment_adapter()


@pytest.fixture(autouse=True)
def background_tasks(request):
    """Collect background tasks for deferred execution."""
    if not _needs_agent_stubs(request):
        yield
        return
    with patched_background_tasks(BACKGROUND_TASK_TARGETS_FULL):
        yield


@pytest.fixture(autouse=True)
def patch_external_services(request):
    """Mock external service calls (OAuth refresh, Socket.IO, LLM providers)."""
    if not _needs_agent_stubs(request):
        yield
        return
    with patched_external_services(mock_ai_functions=True, mock_a2a_skills=True):
        yield


@pytest.fixture(autouse=True)
def patch_storage_dirs(request, tmp_path_factory):
    """Redirect bundle + app-data storage to a tmp tree (no host disk writes)."""
    if not _needs_agent_stubs(request):
        yield
        return
    with patched_storage_dirs(tmp_path_factory):
        yield
