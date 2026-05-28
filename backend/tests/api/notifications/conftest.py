"""Notification-test fixtures.

Provides:
- ``patch_create_session``        — routes create_session at all import sites to the
  test transaction so service calls stay within the rollback boundary.
- ``background_tasks``            — captures create_task_with_error_logging calls
  (including the notification service's offload) so drain_tasks() works.
- ``patch_asyncio_to_thread``     — re-exported so asyncio.to_thread runs inline.
- ``patch_anyio_to_thread``       — patches anyio.to_thread.run_sync so the
  notification service's _async_send runs inline without spawning a thread.
- ``setup_default_credentials``   — re-exported so agent creation passes validation.
- ``patch_environment_adapter``   — stub lifecycle manager so agent creation does
  not spawn Docker.
- ``patch_external_services``     — mocks OAuth refresh + Socket.IO.
- ``reset_throttle_state``        — clears module-level dedup / rate-window dicts
  and the disabled-warned flag before every test so throttle tests don't bleed.
"""
import pytest
from unittest.mock import patch

from tests.utils.fixtures import (
    patch_asyncio_to_thread,  # noqa: F401  (re-exported as autouse fixture)
    setup_default_credentials,  # noqa: F401  (re-exported as autouse fixture)
    patched_create_sessions,
    patched_background_tasks,
    patched_external_services,
    setup_environment_adapter,
    teardown_environment_adapter,
    CREATE_SESSION_TARGETS_AGENT,
    BACKGROUND_TASK_TARGETS_FULL,
)


# ── Patch-target lists ──────────────────────────────────────────────────────
# Add the notification-service import sites to the base lists.

_NOTIFICATION_CREATE_SESSION_TARGETS = CREATE_SESSION_TARGETS_AGENT

_NOTIFICATION_BG_TARGETS = BACKGROUND_TASK_TARGETS_FULL + [
    "app.services.notifications.notification_service.create_task_with_error_logging",
]


# ── Autouse fixtures ────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def patch_create_session(db):
    """Patch create_session at all service import sites."""
    with patched_create_sessions(db, _NOTIFICATION_CREATE_SESSION_TARGETS):
        yield


@pytest.fixture(autouse=True)
def background_tasks():
    """Collect all background tasks (incl. notification send offloads) for drain_tasks()."""
    with patched_background_tasks(_NOTIFICATION_BG_TARGETS):
        yield


@pytest.fixture(autouse=True)
def patch_environment_adapter(tmp_path_factory):
    """Stub lifecycle manager so agent creation does not spawn Docker."""
    lm = setup_environment_adapter(tmp_path_factory)
    yield lm
    teardown_environment_adapter()


@pytest.fixture(autouse=True)
def patch_anyio_to_thread():
    """Run anyio.to_thread.run_sync synchronously at the notification service's import site.

    The notification service's ``_async_send`` calls
    ``anyio.to_thread.run_sync(lambda: send_email(...))`` to offload the blocking
    SMTP send. We patch the ``run_sync`` attribute on the ``anyio.to_thread``
    sub-module directly, but only during tests, so FastAPI's own use of anyio is
    unaffected (FastAPI imports `anyio.to_thread` and calls the attribute at
    call-time; our patch replaces the attribute for the duration of each test).
    """
    import anyio.to_thread as _anyio_to_thread

    original_run_sync = _anyio_to_thread.run_sync

    async def _run_sync_inline(func, /, *args, **kwargs):
        # Strip anyio-specific kwargs (e.g. ``limiter``) that the real function
        # accepts but our sync shim does not need.
        return func(*args)

    _anyio_to_thread.run_sync = _run_sync_inline
    yield
    _anyio_to_thread.run_sync = original_run_sync


@pytest.fixture(autouse=True)
def patch_external_services_fixture():
    """Mock OAuth credential refresh and Socket.IO."""
    with patched_external_services():
        yield


@pytest.fixture(autouse=True)
def reset_throttle_state():
    """Clear module-level throttle state before and after every test.

    ``app.services.notifications.notification_service`` keeps three pieces of
    module-level state that survive between tests:
      - ``_dedup_seen``     : dict mapping (type, key) -> timestamp
      - ``_user_window``    : dict mapping user_id -> deque of timestamps
      - ``_disabled_warned``: bool, set to True on the first disabled-path call

    Clearing them ensures throttle tests see a clean slate.
    """
    import app.services.notifications.notification_service as ns

    ns._dedup_seen.clear()
    ns._user_window.clear()
    ns._disabled_warned = False
    yield
    ns._dedup_seen.clear()
    ns._user_window.clear()
    ns._disabled_warned = False
