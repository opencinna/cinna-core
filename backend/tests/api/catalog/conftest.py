"""Catalog test fixtures.

Catalog items are published from real agents (bundles) and from an agent's
host-side workspace (skill packages), so this domain needs the same
agent-environment stubs as ``tests/api/agents/``. On top of them:

1. ``skill_storage`` (autouse) points ``SKILL_STORAGE_DIR`` at a tmp tree, so
   skill revisions never land under the container's ``/app/data/skills``.
2. ``git_storage`` (autouse) points ``GIT_STORAGE_DIR`` at a tmp tree: desktop
   agents' bare repos are real directories that never touch ``/app/data/git``.
3. **The two ``Session(engine)`` call sites on the git path** --
   ``routes/git_http.py`` (authorize) and ``desktop_agent_revisions.py``
   (post-push bookkeeping, a Starlette ``BackgroundTask``) open a session of
   their own on the real engine, deliberately: the DB must not be held for the
   length of a transfer. Unpatched they would look for the test's users in the
   app database and find nothing. They are redirected to the test session
   (non-closing, so the rollback fixture still owns it). Requests are
   serialised by the bridge, so one connection is safe.
4. ``socket_stub`` captures Socket.IO emits for ``catalog.granted`` /
   ``catalog.updated``; ``bridge`` is a loopback server a real ``git`` binary
   can reach (``tests/utils/git_http_bridge.py``).
"""
from pathlib import Path
from unittest.mock import patch

import pytest

from tests.stubs.socketio_stub import StubSocketIOConnector
from tests.utils.db_proxy import NonClosingSessionProxy
from tests.utils.fixtures import (
    patch_asyncio_to_thread,  # noqa: F401 — autouse fixture, imported for discovery
    setup_default_credentials,  # noqa: F401 — autouse fixture, imported for discovery
    patched_create_sessions,
    patched_background_tasks,
    patched_external_services,
    patched_storage_dirs,
    setup_environment_adapter,
    teardown_environment_adapter,
    CREATE_SESSION_TARGETS_AGENT,
    BACKGROUND_TASK_TARGETS_FULL,
)
from tests.utils.git_http_bridge import GitHttpBridge
from tests.utils.skill_catalog import patched_skill_storage


@pytest.fixture(autouse=True)
def patch_create_session(db):
    with patched_create_sessions(db, CREATE_SESSION_TARGETS_AGENT):
        yield


@pytest.fixture(autouse=True)
def patch_environment_adapter(tmp_path_factory):
    lm = setup_environment_adapter(tmp_path_factory)
    yield lm
    teardown_environment_adapter()


@pytest.fixture(autouse=True)
def background_tasks():
    with patched_background_tasks(BACKGROUND_TASK_TARGETS_FULL):
        yield


@pytest.fixture(autouse=True)
def patch_external_services():
    with patched_external_services(mock_ai_functions=True, mock_a2a_skills=True):
        yield


@pytest.fixture(autouse=True)
def patch_storage_dirs(tmp_path_factory):
    with patched_storage_dirs(tmp_path_factory):
        yield


@pytest.fixture(autouse=True)
def skill_storage(tmp_path: Path):
    with patched_skill_storage(tmp_path / "skill-storage") as root:
        yield root


@pytest.fixture(autouse=True)
def git_storage(tmp_path: Path):
    """Bare repos live under the test's tmp dir."""
    from app.core.config import settings

    storage = tmp_path / "git-storage"
    storage.mkdir()
    with patch.object(settings, "GIT_STORAGE_DIR", str(storage)):
        yield storage


@pytest.fixture(autouse=True)
def patch_git_path_sessions(db):
    """Route the git path's own ``Session(engine)`` calls to the test session."""
    proxy = NonClosingSessionProxy(db)
    with (
        patch("app.api.routes.git_http.Session", lambda _engine: proxy),
        patch("app.services.catalog.desktop_agent_revisions.Session", lambda _engine: proxy),
    ):
        yield


@pytest.fixture()
def bridge(client):
    """Loopback server a real git binary can reach; forwards to the app."""
    server = GitHttpBridge(client).start()
    yield server
    server.stop()


@pytest.fixture()
def socket_stub():
    """Capture Socket.IO emits (``emitted_events`` entries: event/data/room)."""
    stub = StubSocketIOConnector()
    with patch("app.services.events.event_service.socketio_connector", stub):
        yield stub
