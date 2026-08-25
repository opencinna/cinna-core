"""Auto Routing Tuning test fixtures.

Most scenarios here drive real webhook deliveries through
``ChannelInboundService`` (this domain's producer of routing traces — no longer
the only one anywhere: ``POST /admin/routing/simulate`` writes ``simulate``
rows and ``AppMCPRoutingService.route_message`` writes ``app_mcp`` ones, the
latter covered from ``tests/api/app_mcp/``; see the domain README), so this
mirrors ``tests/api/server_channels/conftest.py``
exactly: session-proxy, environment-adapter, background-task, and
external-service stubs so the inbound pipeline runs against the test
transaction instead of the real engine, plus ``patch_anyio_to_thread`` so the
two routing passes (offloaded via ``anyio.to_thread.run_sync``) stay on the
test thread/transaction instead of genuinely forking a worker thread against
the same SQLAlchemy session.
"""
from contextlib import contextmanager
from unittest.mock import patch

import pytest

from app.services.server_channels.channel_debug_buffer import ChannelDebugBuffer
from tests.utils.fixtures import (
    BACKGROUND_TASK_TARGETS_FULL,
    CREATE_SESSION_TARGETS_AGENT,
    patch_asyncio_to_thread,  # noqa: F401 — re-exported as a fixture
    patched_background_tasks,
    patched_create_sessions,
    patched_external_services,
    patched_storage_dirs,
    setup_default_credentials,  # noqa: F401 — re-exported as a fixture
    setup_environment_adapter,
    teardown_environment_adapter,
)


@pytest.fixture(autouse=True)
def reset_channel_debug_buffer():
    """Empty the debug capture buffer around every test (process-global state)."""
    ChannelDebugBuffer.reset()
    yield
    ChannelDebugBuffer.reset()


@pytest.fixture(autouse=True)
def reset_simulate_rate_limiter():
    """Give every test a fresh per-admin simulate budget.

    ``admin_routing._simulate_rate_limiter`` is a module-level ``RateLimiter``
    with a 60-second real-time window, and every test in this suite acts as the
    SAME superuser — so simulate/replay/recommendation calls accumulate in one
    bucket across the whole file, and past ten of them in a minute a later test
    gets a 429 for something an earlier test did.

    That is not hypothetical and it is exactly the shape §11a warns about: the
    per-file runs were green and the combined run was not, because only the
    combined run put more than ten of these calls inside one minute. Process-
    global mutable state shared between tests, reset on both sides — the same
    treatment ``reset_channel_debug_buffer`` gives the debug buffer, for the
    same reason.

    The production behaviour is deliberately unchanged: the limiter really is
    process-global there, which is the point of it.
    """
    from app.api.routes import admin_routing
    from app.services.common.rate_limiter import RateLimiter

    original = admin_routing._simulate_rate_limiter
    admin_routing._simulate_rate_limiter = RateLimiter()
    yield
    admin_routing._simulate_rate_limiter = original


@pytest.fixture(autouse=True)
def patch_create_session(db):
    """Patch create_session at every server_channels + agent import site."""
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
    """Mock external service calls (OAuth refresh, Socket.IO, LLM availability)."""
    with patched_external_services(mock_ai_functions=True, mock_a2a_skills=True):
        yield


@pytest.fixture(autouse=True)
def patch_storage_dirs(tmp_path_factory):
    """Redirect bundle + app-data storage to a tmp tree (no host disk writes)."""
    with patched_storage_dirs(tmp_path_factory):
        yield


@contextmanager
def _patched_anyio_to_thread():
    # See tests/api/server_channels/conftest.py for why the kwargs are
    # accepted and dropped rather than forwarded.
    async def _run_sync(func, *args, **kwargs):
        return func(*args)

    with patch("anyio.to_thread.run_sync", _run_sync):
        yield


@pytest.fixture(autouse=True)
def patch_anyio_to_thread():
    """Run anyio.to_thread.run_sync synchronously (see module docstring)."""
    with _patched_anyio_to_thread():
        yield
