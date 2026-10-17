"""
The environment's artifact zip mode (``AgentEnvService.create_artifact_zip``)
and the backend's reading of its ``X-Cinna-Skipped`` report.

An artifact pull must not carry dotfiles or any symlink (A10), must refuse a
requested path that is or runs through a symlink, and must say what it left out so the backend lists it in the
revision's ``excluded``. The plain download zip (``create_workspace_zip``) used
by downloads and task attachments keeps its old behaviour.
"""
import json
import os
import zipfile
from pathlib import Path

import pytest


@pytest.fixture()
def service(tmp_path: Path):
    from core.server.agent_env_service import AgentEnvService

    workspace = tmp_path / "workspace"
    (workspace / "site" / "data").mkdir(parents=True)
    (workspace / "site" / "index.html").write_text("<html></html>")
    (workspace / "site" / "data" / "rows.json").write_text("[]")
    (workspace / "site" / ".env").write_text("SECRET=1")
    (workspace / "site" / ".cache").mkdir()
    (workspace / "site" / ".cache" / "x").write_text("x")
    (workspace / "site" / "__pycache__").mkdir()
    (workspace / "secret.txt").write_text("outside the folder")
    os.symlink(workspace / "secret.txt", workspace / "site" / "leak.txt")
    os.symlink("/etc/hostname", workspace / "site" / "host.txt")
    os.symlink(workspace / "site" / "index.html", workspace / "site" / "home.html")
    os.symlink(workspace / "site" / "data", workspace / "site" / "data-link")
    os.symlink(workspace / "site" / ".env", workspace / "site" / "link.txt")
    (workspace / "credentials").mkdir()
    (workspace / "credentials" / "credentials.json").write_text('{"token": "x"}')
    os.symlink(workspace / "credentials" / "credentials.json", workspace / "report.json")
    os.symlink(workspace / "credentials", workspace / "linked-site")
    return AgentEnvService(str(workspace))


def test_artifact_zip_skips_and_reports(service):
    zip_path, skipped = service.create_artifact_zip("site")
    try:
        with zipfile.ZipFile(zip_path) as zf:
            names = sorted(zf.namelist())
            assert zf.read("index.html") == b"<html></html>"
    finally:
        zip_path.unlink()

    # (a) every symlink is dropped, even one resolving inside the folder
    # (link.txt -> .env would otherwise publish an excluded file).
    assert names == ["data/rows.json", "index.html"]
    assert sorted(skipped) == sorted([
        ".cache/", "__pycache__/", "data-link/", ".env",
        "leak.txt", "host.txt", "home.html", "link.txt",
    ])


@pytest.mark.parametrize("path", [
    "report.json",                      # (b) the requested file is a link
    "linked-site",                      # (c) the requested folder is a link
    "linked-site/credentials.json",     # a component of the path is a link
])
def test_symlinked_request_paths_are_refused(service, path):
    with pytest.raises(ValueError, match="Symlinks are not published"):
        service.validate_artifact_path(path)
    with pytest.raises(ValueError, match="Symlinks are not published"):
        service.create_artifact_zip(path)


def test_plain_paths_pass_the_artifact_check(service):
    assert service.validate_artifact_path("site").name == "site"
    assert service.validate_artifact_path("./site/data/rows.json").name == "rows.json"


def test_plain_download_zip_is_unchanged(service):
    zip_path = service.create_workspace_zip("site")
    try:
        with zipfile.ZipFile(zip_path) as zf:
            names = set(zf.namelist())
    finally:
        zip_path.unlink()
    # The old zipper still dereferences links (existing callers rely on it).
    assert "leak.txt" in names and ".env" not in names


def test_skipped_header_is_ascii_json_and_capped():
    from core.server.routes import _skipped_header_value

    value = _skipped_header_value(["café/", "a.txt"])
    assert value.isascii()
    assert json.loads(value) == ["café/", "a.txt"]

    many = [f"folder/{i:05d}/.secret" for i in range(2000)]
    capped = json.loads(_skipped_header_value(many))
    assert len(_skipped_header_value(many)) <= 8000
    assert capped[-1].startswith("…and ") and capped[-1].endswith(" more")


def test_backend_tolerates_a_missing_or_malformed_report():
    from app.services.environments.adapters.docker_adapter import _parse_skipped_header

    assert _parse_skipped_header(None) is None
    assert _parse_skipped_header("not json") is None
    assert _parse_skipped_header('{"a": 1}') is None
    assert _parse_skipped_header('[".env", "x/"]') == [".env", "x/"]
