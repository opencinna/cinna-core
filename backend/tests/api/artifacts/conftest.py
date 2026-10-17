"""Artifacts-domain fixtures.

``artifact_storage`` points ``ARTIFACT_STORAGE_DIR`` at a per-test tmp dir so
no test writes into the real ``./backend/data/artifacts`` bind mount. The root
conftest already redirects it suite-wide (``artifact_storage_dir``); this
fixture re-asserts it for the domain and hands the path to tests that check
what is on disk.
"""
import pytest

from app.core.config import settings


@pytest.fixture(autouse=True)
def artifact_storage(artifact_storage_dir, monkeypatch):
    monkeypatch.setattr(settings, "ARTIFACT_STORAGE_DIR", str(artifact_storage_dir))
    return artifact_storage_dir
