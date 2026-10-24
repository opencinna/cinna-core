"""Addons directory fixtures.

Catalog skills are published from real (stubbed-environment) agents, so this
domain needs the same agent-environment stubs as ``tests/api/catalog/``, minus
the git bridge. ``clear_directory_cache`` (autouse) empties the directory's
per-user in-process index cache around every test: its key is
``(user_id, version)`` and the superuser id is stable across tests while the
version counter restarts at 0 on every rolled-back database, so a stale entry
could otherwise be served to a later test.
"""
from pathlib import Path

import pytest

from app.services.addons.addon_directory_service import AddonDirectoryService
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
def clear_directory_cache():
    AddonDirectoryService.clear_cache()
    yield
    AddonDirectoryService.clear_cache()


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
