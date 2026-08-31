"""Server Channels test fixtures.

Mirrors ``tests/api/agents/conftest.py``: session-proxy, environment-adapter,
background-task, and external-service stubs so the inbound pipeline (which
opens its own DB sessions via ``app.core.db.create_session`` and schedules
its routing/ingest work as background tasks) runs against the test
transaction instead of the real engine, and streaming runs through stubs
instead of Docker/a real LLM.

One addition beyond the ``agents/`` stack: ``patch_anyio_to_thread``. The
channel pipeline offloads both routing passes via ``anyio.to_thread.run_sync``
(``ChannelRoutingService.run_in_thread``), not ``asyncio.to_thread`` — the
existing ``patch_asyncio_to_thread`` fixture (imported below) does not cover
it. Left unpatched, routing would genuinely run on a separate OS thread
against the same SQLAlchemy test session, which is not supported. Patching it
to run inline keeps everything on the test thread/transaction, same
motivation as ``patch_asyncio_to_thread``.
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
    """Empty the debug capture buffer around every test.

    ``ChannelDebugBuffer`` is deliberately process-global class state (it has
    to outlive any one request to be a useful live view), which makes it
    shared mutable state between tests: every webhook test in this domain now
    fills it. Reset on both sides so a test never sees another's events and
    never leaves its own behind.
    """
    ChannelDebugBuffer.reset()
    yield
    ChannelDebugBuffer.reset()


@pytest.fixture(autouse=True)
def patch_create_session(db):
    """Patch create_session at every server_channels + agent import site.

    ``app.services.app_mcp.app_mcp_request_handler.create_session`` is
    included because a test in this domain (the App-MCP/channel candidate
    parity pair) calls ``AppMCPRequestHandler.handle_send_message`` directly
    to compare the two surfaces' candidate ballots — there is no HTTP route
    for App MCP, it is an MCP tool-call surface, same pattern as
    ``tests/api/app_mcp/conftest.py``'s own extension of this list. Without
    it, that handler's ``with create_session() as db:`` would open a real
    session outside the test transaction.
    """
    with patched_create_sessions(db, CREATE_SESSION_TARGETS_AGENT + [
        "app.services.app_mcp.app_mcp_request_handler.create_session",
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
    """Collect background tasks for deferred execution.

    Covers ``app.utils.create_task_with_error_logging`` — the target
    ``ChannelInboundService._schedule`` resolves at call time via
    ``from app.utils import create_task_with_error_logging``, so patching the
    module attribute here is picked up transparently.

    ``app.services.app_mcp.app_mcp_request_handler.create_task_with_error_
    logging`` is its own separate target (not covered by the ``app.utils``
    one above — ``from X import Y`` binds a local reference at the import
    site): the handler schedules a session-title background task on every
    new App MCP session, reached directly by the candidate-parity test.
    """
    with patched_background_tasks(BACKGROUND_TASK_TARGETS_FULL + [
        "app.services.app_mcp.app_mcp_request_handler.create_task_with_error_logging",
    ]):
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
    # anyio.to_thread.run_sync's real signature is
    # ``run_sync(func, *args, abandon_on_cancel=False, limiter=None)`` — the
    # keyword arguments are anyio's OWN thread-execution controls, not meant
    # to be forwarded to ``func`` (unlike ``asyncio.to_thread``, which does
    # forward kwargs). Any caller elsewhere in the app/test stack that passes
    # ``limiter=``/``abandon_on_cancel=`` (fixture setup does, via unrelated
    # code) would blow up if we forwarded them into ``func(...)`` — so they
    # are accepted and dropped, never forwarded.
    async def _run_sync(func, *args, **kwargs):
        return func(*args)

    with patch("anyio.to_thread.run_sync", _run_sync):
        yield


@pytest.fixture(autouse=True)
def patch_anyio_to_thread():
    """Run anyio.to_thread.run_sync synchronously (see module docstring)."""
    with _patched_anyio_to_thread():
        yield
