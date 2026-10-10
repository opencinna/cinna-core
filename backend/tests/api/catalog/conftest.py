"""Catalog test fixtures.

Catalog items are published from real agents (bundles) and from an agent's
host-side workspace (skill packages), so this domain needs the same
agent-environment stubs as ``tests/api/agents/`` and ``tests/api/library/``.
On top of them:

1. ``skill_storage`` (autouse) points ``SKILL_STORAGE_DIR`` at a tmp tree, so
   skill revisions never land under the container's ``/app/data/skills``.
2. ``socket_stub`` captures Socket.IO emits for ``catalog.granted``.
"""
from pathlib import Path
from unittest.mock import patch

import pytest

from tests.stubs.socketio_stub import StubSocketIOConnector
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


@pytest.fixture()
def socket_stub():
    """Capture Socket.IO emits (``emitted_events`` entries: event/data/room)."""
    stub = StubSocketIOConnector()
    with patch("app.services.events.event_service.socketio_connector", stub):
        yield stub
