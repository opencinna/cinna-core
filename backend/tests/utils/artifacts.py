"""Artifact test helpers: zip builders for the ingest rules, API wrappers.

Each builder returns the zip as ``bytes``; wrap in ``io.BytesIO`` for
``ZipSource`` or send as a multipart ``archive``. The API wrappers
(``publish``, ``view``, ``content_path``) go through HTTP only.
"""
import io
import stat
import struct
import zipfile
from urllib.parse import urlsplit

from fastapi.testclient import TestClient

from app.core.config import settings

API = settings.API_V1_STR


def build_zip(
    files: dict[str, bytes | str],
    *,
    compression: int = zipfile.ZIP_DEFLATED,
) -> bytes:
    """A zip with one regular entry per ``name -> content``."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=compression) as zf:
        for name, content in files.items():
            info = zipfile.ZipInfo(name)
            info.compress_type = compression
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            zf.writestr(info, content)
    return buf.getvalue()


def build_site_zip() -> bytes:
    """A small dashboard: root index.html, a module script, a sqlite file."""
    return build_zip(
        {
            "index.html": "<!doctype html><script type=module src=app.js></script>",
            "app.js": "console.log('hi')",
            "data/data.sqlite": b"SQLite format 3\x00" + b"\x00" * 64,
        }
    )


def build_special_entry_zip(name: str, file_type: int, target: bytes = b"/etc/passwd") -> bytes:
    """A zip whose entry carries a non-regular Unix mode (``stat.S_IFLNK`` …)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("index.html", "<html></html>")
        info = zipfile.ZipInfo(name)
        info.create_system = 3
        info.external_attr = (file_type | 0o777) << 16
        zf.writestr(info, target)
    return buf.getvalue()


def build_symlink_zip(name: str = "link") -> bytes:
    return build_special_entry_zip(name, stat.S_IFLNK)


def build_traversal_zip(name: str = "../escape.txt") -> bytes:
    return build_zip({"index.html": "<html></html>", name: "x"})


def build_bomb_zip(size: int = 1024 * 1024) -> bytes:
    """One entry of ``size`` zero bytes: deflate ratio far above 100."""
    return build_zip({"zeros.bin": b"\x00" * size})


def build_duplicate_zip() -> bytes:
    """Two entries equal after NFC normalisation (é precomposed vs decomposed)."""
    return build_zip({"café.txt": "a", "café.txt": "b"})


def build_dotenv_zip() -> bytes:
    return build_zip({"index.html": "<html></html>", ".env": "SECRET=1"})


def build_lying_size_zip(real_size: int, declared_size: int) -> bytes:
    """A stored single-entry zip whose headers understate the entry size."""
    raw = bytearray(build_zip({"big.bin": b"A" * real_size}, compression=zipfile.ZIP_STORED))
    local = raw.find(b"PK\x03\x04")
    central = raw.find(b"PK\x01\x02")
    struct.pack_into("<I", raw, local + 22, declared_size)
    struct.pack_into("<I", raw, central + 24, declared_size)
    return bytes(raw)


def _patch_single_entry_headers(raw: bytearray, *, local_offset: int, central_offset: int, fmt: str, value: int) -> None:
    struct.pack_into(fmt, raw, raw.find(b"PK\x03\x04") + local_offset, value)
    struct.pack_into(fmt, raw, raw.find(b"PK\x01\x02") + central_offset, value)


def build_encrypted_flag_zip() -> bytes:
    """A single-entry zip whose headers mark the entry encrypted (flag bit 0)."""
    raw = bytearray(build_zip({"index.html": "<html></html>"}, compression=zipfile.ZIP_STORED))
    _patch_single_entry_headers(raw, local_offset=6, central_offset=8, fmt="<H", value=0x1)
    return bytes(raw)


def build_unsupported_compression_zip(method: int = 99) -> bytes:
    """A single-entry zip whose headers name an unknown compression method."""
    raw = bytearray(build_zip({"index.html": "<html></html>"}, compression=zipfile.ZIP_STORED))
    _patch_single_entry_headers(raw, local_offset=8, central_offset=10, fmt="<H", value=method)
    return bytes(raw)


# ── API wrappers ────────────────────────────────────────────────────


def publish(
    client: TestClient,
    headers: dict[str, str],
    *,
    archive: bytes | None = None,
    files: dict[str, bytes] | None = None,
    title: str = "Test artifact",
    expect: int = 200,
    **form: str,
) -> dict:
    """``POST /artifacts`` with a zip ``archive`` or ``files`` (name → bytes)."""
    upload: list[tuple[str, tuple[str, bytes, str]]] = []
    if archive is not None:
        upload.append(("archive", ("upload.zip", archive, "application/zip")))
    for name, body in (files or {}).items():
        upload.append(("files", (name, body, "application/octet-stream")))
    r = client.post(
        f"{API}/artifacts",
        headers=headers,
        data={"title": title, **form},
        files=upload or None,
    )
    assert r.status_code == expect, r.text
    return r.json()


def view(
    client: TestClient,
    headers: dict[str, str],
    artifact_id: str,
    *,
    rev: int | None = None,
    expect: int = 200,
) -> dict:
    """``POST /artifacts/{id}/view`` → ``ArtifactViewPublic``."""
    params = {"rev": rev} if rev is not None else None
    r = client.post(f"{API}/artifacts/{artifact_id}/view", headers=headers, params=params)
    assert r.status_code == expect, r.text
    return r.json()


def content_path(url: str) -> str:
    """Path (+ query) of a content / download URL, for ``TestClient``."""
    parts = urlsplit(url)
    return parts.path + (f"?{parts.query}" if parts.query else "")


def cap_of(view_payload: dict) -> str:
    """The cap segment of a view's ``content_base`` (``…/a/v/<cap>/``)."""
    return urlsplit(view_payload["content_base"]).path.split("/")[3]


def _upload_parts(archive: bytes | None, files: dict[str, bytes] | None) -> list:
    parts: list[tuple[str, tuple[str, bytes, str]]] = []
    if archive is not None:
        parts.append(("archive", ("upload.zip", archive, "application/zip")))
    for name, body in (files or {}).items():
        parts.append(("files", (name, body, "application/octet-stream")))
    return parts


def republish(
    client: TestClient,
    headers: dict[str, str],
    artifact_id: str,
    *,
    archive: bytes | None = None,
    files: dict[str, bytes] | None = None,
    expect: int = 200,
    **form: str,
) -> dict:
    """``POST /artifacts/{id}/revisions``."""
    r = client.post(
        f"{API}/artifacts/{artifact_id}/revisions",
        headers=headers,
        data=form or None,
        files=_upload_parts(archive, files) or None,
    )
    assert r.status_code == expect, r.text
    return r.json()


def get_artifact(client: TestClient, headers: dict[str, str], artifact_id: str, *, expect: int = 200) -> dict:
    r = client.get(f"{API}/artifacts/{artifact_id}", headers=headers)
    assert r.status_code == expect, r.text
    return r.json()


def list_artifacts(client: TestClient, headers: dict[str, str], **params) -> dict:
    r = client.get(f"{API}/artifacts", headers=headers, params=params)
    assert r.status_code == 200, r.text
    return r.json()


def update_artifact(
    client: TestClient, headers: dict[str, str], artifact_id: str, *, expect: int = 200, **body
) -> dict:
    r = client.patch(f"{API}/artifacts/{artifact_id}", headers=headers, json=body)
    assert r.status_code == expect, r.text
    return r.json()


def delete_artifact(client: TestClient, headers: dict[str, str], artifact_id: str, *, expect: int = 200) -> None:
    r = client.delete(f"{API}/artifacts/{artifact_id}", headers=headers)
    assert r.status_code == expect, r.text


def set_grants(
    client: TestClient, headers: dict[str, str], artifact_id: str, user_ids: list[str], *, expect: int = 200
) -> list:
    body = [{"principal": {"user_id": uid}, "access": "view"} for uid in user_ids]
    r = client.put(f"{API}/artifacts/{artifact_id}/grants", headers=headers, json=body)
    assert r.status_code == expect, r.text
    return r.json()


def create_link(
    client: TestClient, headers: dict[str, str], artifact_id: str, *, expect: int = 200, **body
) -> dict:
    r = client.post(f"{API}/artifacts/{artifact_id}/links", headers=headers, json=body)
    assert r.status_code == expect, r.text
    return r.json()


def revoke_link(client: TestClient, headers: dict[str, str], artifact_id: str, link_id: str, *, expect: int = 200) -> None:
    r = client.delete(f"{API}/artifacts/{artifact_id}/links/{link_id}", headers=headers)
    assert r.status_code == expect, r.text


def link_info(client: TestClient, token: str, *, expect: int = 200) -> dict:
    r = client.get(f"{API}/artifact-links/{token}/info")
    assert r.status_code == expect, r.text
    return r.json()


def open_link(client: TestClient, token: str, code: str | None = None, *, expect: int = 200) -> dict:
    r = client.post(f"{API}/artifact-links/{token}/open", json={"code": code} if code is not None else None)
    assert r.status_code == expect, r.text
    return r.json()


def entry_url(view_payload: dict, path: str | None = None) -> str:
    """TestClient path of a file (default: the entry) under a view."""
    return content_path(view_payload["content_base"]) + (path if path is not None else (view_payload["entry"] or ""))


# ── Publishing out of an agent environment (P6) ─────────────────────


def stage_workspace_item(
    path: str,
    content: bytes,
    *,
    folder: bool = False,
    skipped: list[str] | None = None,
    artifact_error: str | None = None,
) -> None:
    """Make ``EnvironmentTestAdapter`` serve ``content`` at workspace ``path``.

    ``folder=True`` serves it the way the env server serves a folder: a zip
    with ``Content-Type: application/zip``; ``skipped`` is what the env's
    artifact zipper reports as left out; ``artifact_error`` makes the env
    refuse the path in artifact mode (its 400 detail). Undo with :func:`clear_workspace_items`.
    """
    from tests.stubs.environment_adapter_stub import EnvironmentTestAdapter

    EnvironmentTestAdapter.workspace_files[path] = content
    if folder:
        EnvironmentTestAdapter.workspace_content_types[path] = "application/zip"
    if skipped is not None:
        EnvironmentTestAdapter.workspace_skipped[path] = skipped
    if artifact_error is not None:
        EnvironmentTestAdapter.workspace_artifact_errors[path] = artifact_error


def clear_workspace_items(*paths: str) -> None:
    from tests.stubs.environment_adapter_stub import EnvironmentTestAdapter

    for path in paths:
        EnvironmentTestAdapter.workspace_files.pop(path, None)
        EnvironmentTestAdapter.workspace_content_types.pop(path, None)
        EnvironmentTestAdapter.workspace_skipped.pop(path, None)
        EnvironmentTestAdapter.workspace_artifact_errors.pop(path, None)


def agent_publish(
    client: TestClient, env_headers: dict[str, str], *, expect: int = 200, **body
) -> dict:
    """``POST /agent/artifacts/publish`` with the env token (the ``artifacts_publish`` tool)."""
    r = client.post(f"{API}/agent/artifacts/publish", headers=env_headers, json=body)
    assert r.status_code == expect, r.text
    return r.json()


def publish_from_environment(
    client: TestClient, headers: dict[str, str], *, expect: int = 200, **body
) -> dict:
    """``POST /artifacts/from-environment`` with a user JWT (workspace "Publish as artifact")."""
    r = client.post(f"{API}/artifacts/from-environment", headers=headers, json=body)
    assert r.status_code == expect, r.text
    return r.json()
