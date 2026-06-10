"""
AI Credentials test fixtures.

Provides adapter-level environment stubbing so agent creation runs real service
logic without Docker. Uses a persistent adapter so tests can inspect call history.

Only ``test_ai_credentials_propagation.py`` actually creates agents/environments
and needs these heavy stubs. The pure-CRUD suites (``test_ai_credentials.py``,
``test_ai_credential_test_connection.py`` — the latter does its own targeted
probe patching) opt out by setting the module-level flags
``NEEDS_AGENT_STUBS = False`` and ``NEEDS_DEFAULT_CREDENTIALS = False``.
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
)


def _needs_agent_stubs(request) -> bool:
    """Whether the requesting test module needs the heavy agent/env stubs.

    Defaults to True; pure-CRUD modules set ``NEEDS_AGENT_STUBS = False``.
    """
    return getattr(request.module, "NEEDS_AGENT_STUBS", True)


@pytest.fixture(autouse=True)
def patch_create_session(request, db):
    """Patch create_session at all service import sites."""
    if not _needs_agent_stubs(request):
        yield
        return
    with patched_create_sessions(db):
        yield


@pytest.fixture(autouse=True)
def patch_environment_adapter(request, tmp_path_factory):
    """Use persistent adapter so tests can inspect call history."""
    if not _needs_agent_stubs(request):
        yield None
        return
    lm = setup_environment_adapter(
        tmp_path_factory, persistent_adapter=True, extra_template_dirs=["app/core"],
    )
    yield lm
    teardown_environment_adapter()


@pytest.fixture(autouse=True)
def background_tasks(request):
    """Collect background tasks for deferred execution."""
    if not _needs_agent_stubs(request):
        yield
        return
    with patched_background_tasks():
        yield


@pytest.fixture(autouse=True)
def patch_external_services(request):
    """Mock external service calls (OAuth refresh, Socket.IO)."""
    if not _needs_agent_stubs(request):
        yield
        return
    with patched_external_services():
        yield
