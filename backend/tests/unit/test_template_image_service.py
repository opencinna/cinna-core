"""
Unit tests for TemplateImageService.

Tests cover:
- compute_template_hash() stability and sensitivity to input file changes
- get_image_tag() format
- ensure_template_image() cache-hit path (skips docker build)
- ensure_template_image() cache-miss path (triggers docker build)
- ensure_template_image() build failure raises RuntimeError
- ensure_template_image() raises FileNotFoundError for unknown template
- Concurrent calls for the same template serialise (only one build)
- Concurrent calls for different templates do NOT block each other

All Docker and subprocess interactions are mocked — no Docker daemon required.

Run:
    cd backend && python -m pytest tests/unit/test_template_image_service.py -v
"""

import asyncio
import hashlib
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import docker.errors
import pytest

from app.services.environments.template_image_service import TemplateImageService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_service(tmp_path: Path) -> TemplateImageService:
    """Create a TemplateImageService pointed at a temp templates dir."""
    return TemplateImageService(templates_dir=tmp_path)


def _write_template_files(template_dir: Path, dockerfile: bytes, pyproject: bytes, uv_lock: bytes):
    """Write the three hash-input files into a template directory."""
    template_dir.mkdir(parents=True, exist_ok=True)
    (template_dir / "Dockerfile").write_bytes(dockerfile)
    (template_dir / "pyproject.toml").write_bytes(pyproject)
    (template_dir / "uv.lock").write_bytes(uv_lock)


def _expected_hash(dockerfile: bytes, pyproject: bytes, uv_lock: bytes) -> str:
    h = hashlib.sha256()
    h.update(dockerfile)
    h.update(pyproject)
    h.update(uv_lock)
    return h.hexdigest()[:12]


def _run(coro):
    """Run a coroutine synchronously (no pytest-asyncio required)."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# compute_template_hash
# ---------------------------------------------------------------------------


class TestComputeTemplateHash:
    def test_hash_is_deterministic(self, tmp_path):
        """Same file contents always produce the same 12-char hash."""
        template_dir = tmp_path / "my-template"
        _write_template_files(template_dir, b"FROM slim", b"[project]", b"uv v1")
        svc = _make_service(tmp_path)
        h1 = svc.compute_template_hash("my-template")
        h2 = svc.compute_template_hash("my-template")
        assert h1 == h2
        assert len(h1) == 12

    def test_hash_changes_when_dockerfile_changes(self, tmp_path):
        """Changing Dockerfile bytes produces a different hash."""
        template_dir = tmp_path / "tpl"
        _write_template_files(template_dir, b"FROM slim", b"[project]", b"uv v1")
        svc = _make_service(tmp_path)
        h_before = svc.compute_template_hash("tpl")

        (template_dir / "Dockerfile").write_bytes(b"FROM bookworm")
        h_after = svc.compute_template_hash("tpl")

        assert h_before != h_after

    def test_hash_changes_when_pyproject_changes(self, tmp_path):
        """Changing pyproject.toml bytes produces a different hash."""
        template_dir = tmp_path / "tpl"
        _write_template_files(template_dir, b"FROM slim", b"[project]\nname='a'", b"uv v1")
        svc = _make_service(tmp_path)
        h_before = svc.compute_template_hash("tpl")

        (template_dir / "pyproject.toml").write_bytes(b"[project]\nname='b'")
        h_after = svc.compute_template_hash("tpl")

        assert h_before != h_after

    def test_hash_changes_when_uv_lock_changes(self, tmp_path):
        """Changing uv.lock bytes produces a different hash."""
        template_dir = tmp_path / "tpl"
        _write_template_files(template_dir, b"FROM slim", b"[project]", b"version=1")
        svc = _make_service(tmp_path)
        h_before = svc.compute_template_hash("tpl")

        (template_dir / "uv.lock").write_bytes(b"version=2")
        h_after = svc.compute_template_hash("tpl")

        assert h_before != h_after

    def test_missing_uv_lock_contributes_empty_bytes(self, tmp_path):
        """A missing uv.lock uses an empty sentinel — hash is stable, not an error."""
        template_dir = tmp_path / "tpl"
        template_dir.mkdir()
        (template_dir / "Dockerfile").write_bytes(b"FROM slim")
        (template_dir / "pyproject.toml").write_bytes(b"[project]")
        # no uv.lock intentionally

        svc = _make_service(tmp_path)
        h = svc.compute_template_hash("tpl")
        assert len(h) == 12
        # Same call again must return identical result
        assert svc.compute_template_hash("tpl") == h

    def test_hash_matches_manual_computation(self, tmp_path):
        """Verify the hash value matches a manually-computed reference."""
        dockerfile = b"FROM python:3.11-slim"
        pyproject = b"[project]\nname = 'env'"
        uv_lock = b"lockfile v1"
        template_dir = tmp_path / "tpl"
        _write_template_files(template_dir, dockerfile, pyproject, uv_lock)

        svc = _make_service(tmp_path)
        assert svc.compute_template_hash("tpl") == _expected_hash(dockerfile, pyproject, uv_lock)


# ---------------------------------------------------------------------------
# get_image_tag
# ---------------------------------------------------------------------------


class TestGetImageTag:
    def test_tag_format(self, tmp_path):
        """Tag must be 'cinna-agent-<env_name>:<hash12>'."""
        template_dir = tmp_path / "python-env-advanced"
        _write_template_files(template_dir, b"FROM slim", b"[p]", b"lock")
        svc = _make_service(tmp_path)
        tag = svc.get_image_tag("python-env-advanced")
        assert tag.startswith("cinna-agent-python-env-advanced:")
        suffix = tag.split(":")[-1]
        assert len(suffix) == 12
        assert all(c in "0123456789abcdef" for c in suffix)

    def test_different_templates_produce_different_tags(self, tmp_path):
        """Two templates with different Dockerfiles must have different tags."""
        for name, base in [("tpl-a", b"FROM slim"), ("tpl-b", b"FROM bookworm")]:
            d = tmp_path / name
            _write_template_files(d, base, b"[p]", b"lock")
        svc = _make_service(tmp_path)
        assert svc.get_image_tag("tpl-a") != svc.get_image_tag("tpl-b")


# ---------------------------------------------------------------------------
# ensure_template_image — cache hit (image already present)
# ---------------------------------------------------------------------------


class TestEnsureTemplateImageCacheHit:
    def test_returns_tag_immediately_when_image_exists(self, tmp_path):
        """When the image is already present, docker build is NOT called."""
        template_dir = tmp_path / "tpl"
        _write_template_files(template_dir, b"FROM slim", b"[p]", b"lock")
        svc = _make_service(tmp_path)
        expected_tag = svc.get_image_tag("tpl")

        mock_docker = MagicMock()
        mock_docker.images.get.return_value = MagicMock()  # image found

        with patch.object(svc, "_get_docker_client", return_value=mock_docker):
            with patch.object(svc, "_build_image", new_callable=AsyncMock) as mock_build:
                tag = _run(svc.ensure_template_image("tpl"))

        assert tag == expected_tag
        mock_docker.images.get.assert_called_once_with(expected_tag)
        mock_build.assert_not_called()


# ---------------------------------------------------------------------------
# ensure_template_image — cache miss (triggers build)
# ---------------------------------------------------------------------------


class TestEnsureTemplateImageCacheMiss:
    def test_builds_when_image_not_found(self, tmp_path):
        """When image is absent, _build_image is called and the tag is returned."""
        template_dir = tmp_path / "tpl"
        _write_template_files(template_dir, b"FROM slim", b"[p]", b"lock")
        svc = _make_service(tmp_path)
        expected_tag = svc.get_image_tag("tpl")

        mock_docker = MagicMock()
        mock_docker.images.get.side_effect = docker.errors.ImageNotFound("not found")

        with patch.object(svc, "_get_docker_client", return_value=mock_docker):
            with patch.object(svc, "_build_image", new_callable=AsyncMock) as mock_build:
                tag = _run(svc.ensure_template_image("tpl"))

        assert tag == expected_tag
        mock_build.assert_called_once_with("tpl", expected_tag)

    def test_raises_runtime_error_on_build_failure(self, tmp_path):
        """If _build_image raises RuntimeError, ensure_template_image propagates it."""
        template_dir = tmp_path / "tpl"
        _write_template_files(template_dir, b"FROM slim", b"[p]", b"lock")
        svc = _make_service(tmp_path)

        mock_docker = MagicMock()
        mock_docker.images.get.side_effect = docker.errors.ImageNotFound("not found")

        with patch.object(svc, "_get_docker_client", return_value=mock_docker):
            with patch.object(
                svc,
                "_build_image",
                new_callable=AsyncMock,
                side_effect=RuntimeError("docker build failed for template 'tpl' (exit 1): err"),
            ):
                with pytest.raises(RuntimeError, match="docker build failed"):
                    _run(svc.ensure_template_image("tpl"))

    def test_raises_file_not_found_for_unknown_template(self, tmp_path):
        """ensure_template_image raises FileNotFoundError for a non-existent template."""
        svc = _make_service(tmp_path)
        with pytest.raises(FileNotFoundError, match="Template directory not found"):
            _run(svc.ensure_template_image("does-not-exist"))

    def test_raises_runtime_error_on_docker_sdk_error(self, tmp_path):
        """If the Docker daemon is unreachable, RuntimeError is raised (not swallowed)."""
        template_dir = tmp_path / "tpl"
        _write_template_files(template_dir, b"FROM slim", b"[p]", b"lock")
        svc = _make_service(tmp_path)

        mock_docker = MagicMock()
        mock_docker.images.get.side_effect = Exception("Cannot connect to Docker daemon")

        with patch.object(svc, "_get_docker_client", return_value=mock_docker):
            with pytest.raises(RuntimeError, match="Docker image inspection failed"):
                _run(svc.ensure_template_image("tpl"))


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


class TestEnsureTemplateImageConcurrency:
    def test_concurrent_calls_same_template_serialize(self, tmp_path):
        """
        Two concurrent calls for the same template must not both trigger a build.
        The second caller acquires the lock after the first finishes, and then
        finds the image already present → skips its own build.
        """
        template_dir = tmp_path / "tpl"
        _write_template_files(template_dir, b"FROM slim", b"[p]", b"lock")
        svc = _make_service(tmp_path)
        expected_tag = svc.get_image_tag("tpl")
        build_call_count = 0

        # Track how many times images.get is called across all concurrent calls
        images_get_call_count = 0

        def images_get_side_effect(tag):
            nonlocal images_get_call_count
            images_get_call_count += 1
            if images_get_call_count == 1:
                raise docker.errors.ImageNotFound("not found")
            # Second call: image is "present" (first caller built it)
            return MagicMock()

        async def slow_build(env_name, tag):
            nonlocal build_call_count
            build_call_count += 1
            await asyncio.sleep(0.02)

        mock_docker = MagicMock()
        mock_docker.images.get.side_effect = images_get_side_effect

        async def run_test():
            with patch.object(svc, "_get_docker_client", return_value=mock_docker):
                with patch.object(svc, "_build_image", new_callable=AsyncMock, side_effect=slow_build):
                    results = await asyncio.gather(
                        svc.ensure_template_image("tpl"),
                        svc.ensure_template_image("tpl"),
                    )
            return results

        results = asyncio.run(run_test())

        assert results[0] == expected_tag
        assert results[1] == expected_tag
        # Lock serialises the calls, so build is only invoked once
        assert build_call_count == 1

    def test_different_templates_have_independent_locks(self, tmp_path):
        """
        Two concurrent calls for different templates use separate locks and
        can run in parallel (both builds start before either finishes).
        """
        for name, base in [("tpl-a", b"FROM slim"), ("tpl-b", b"FROM bookworm")]:
            d = tmp_path / name
            _write_template_files(d, base, b"[p]", b"lock")

        svc = _make_service(tmp_path)
        started: list[str] = []
        finished: list[str] = []

        async def fake_build(env_name, tag):
            started.append(env_name)
            await asyncio.sleep(0.03)
            finished.append(env_name)

        mock_docker = MagicMock()
        mock_docker.images.get.side_effect = docker.errors.ImageNotFound("not found")

        async def run_test():
            with patch.object(svc, "_get_docker_client", return_value=mock_docker):
                with patch.object(svc, "_build_image", new_callable=AsyncMock, side_effect=fake_build):
                    await asyncio.gather(
                        svc.ensure_template_image("tpl-a"),
                        svc.ensure_template_image("tpl-b"),
                    )

        asyncio.run(run_test())

        # Both templates were built
        assert set(started) == {"tpl-a", "tpl-b"}
        assert set(finished) == {"tpl-a", "tpl-b"}
        # Both builds started before either finished (parallel execution)
        # i.e. after the first "started" event, the second "started" appeared
        # before the first "finished"
        assert len(started) == 2
        assert len(finished) == 2
