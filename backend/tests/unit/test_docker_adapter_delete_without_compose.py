"""Unit tests: ``DockerEnvironmentAdapter.delete`` with no ``docker-compose.yml``.

An environment deleted while it was still building has an instance folder
without a compose file (the build never got that far). ``docker-compose down``
then failed with "docker-compose.yml: no such file or directory". The adapter
now skips compose in that case and only removes a container of that name if
one exists.

No Docker daemon: ``docker.from_env`` is stubbed (the adapter builds a client
in its constructor) and ``_run_compose_command`` is replaced with an AsyncMock.

Notes:
  The API-observable end (a delete during the build leaves no instance
  folder) is in ``tests/api/agents/core/agents_env_build_delete_race_test.py``.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.environments.adapters.docker_adapter import (
    DockerEnvironmentAdapter,
)


@pytest.fixture
def make_adapter(monkeypatch):
    monkeypatch.setattr(
        "docker.from_env", lambda *args, **kwargs: MagicMock(), raising=True
    )

    def _make(env_dir: Path) -> DockerEnvironmentAdapter:
        adapter = DockerEnvironmentAdapter(
            env_id=uuid.uuid4(), env_dir=env_dir, port=8000, auth_token="t"
        )
        adapter._run_compose_command = AsyncMock()
        return adapter

    return _make


@pytest.mark.anyio
async def test_no_compose_file_skips_compose_and_removes_a_leftover_container(
    make_adapter, tmp_path
):
    adapter = make_adapter(tmp_path)  # exists, no docker-compose.yml
    container = MagicMock()
    adapter.get_container = MagicMock(return_value=container)

    assert await adapter.delete() is True

    adapter._run_compose_command.assert_not_called()
    container.remove.assert_called_once_with(force=True, v=True)


@pytest.mark.anyio
async def test_no_compose_file_and_no_container_is_a_quiet_success(
    make_adapter, tmp_path
):
    adapter = make_adapter(tmp_path / "never-created")
    adapter.get_container = MagicMock(return_value=None)

    assert await adapter.delete() is True
    adapter._run_compose_command.assert_not_called()


@pytest.mark.anyio
async def test_no_compose_file_swallows_a_container_lookup_failure(
    make_adapter, tmp_path
):
    adapter = make_adapter(tmp_path)
    adapter.get_container = MagicMock(side_effect=RuntimeError("daemon gone"))

    assert await adapter.delete() is True
    adapter._run_compose_command.assert_not_called()


@pytest.mark.anyio
async def test_with_compose_file_runs_compose_down(make_adapter, tmp_path):
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")
    adapter = make_adapter(tmp_path)
    adapter.get_container = MagicMock()

    assert await adapter.delete() is True

    adapter._run_compose_command.assert_awaited_once_with(
        ["down", "-v", "--remove-orphans"]
    )
    adapter.get_container.assert_not_called()
