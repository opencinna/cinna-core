"""Artifact ingest: name rules, A10 drops, limits, kind detection, MIME table.

Pure unit tests over ``ArtifactIngestService`` / ``ArtifactStorage`` with the
storage root pointed at ``tmp_path`` — no DB, no TestClient. The API-level
publish scenarios live in ``tests/api/artifacts/`` (P2/P3).
"""
import hashlib
import io
import os
import stat
import time
import uuid

import pytest

from app.core.config import settings
from app.services.artifacts.artifact_ingest import (
    ArtifactIngestError,
    ArtifactIngestService,
    FilesSource,
    SourceFile,
    ZipSource,
    normalize_path,
)
from app.services.artifacts.artifact_mime import DEFAULT_MIME, mime_for_path
from app.services.artifacts.artifact_storage import ArtifactStorage
from tests.utils.artifacts import (
    build_bomb_zip,
    build_dotenv_zip,
    build_duplicate_zip,
    build_encrypted_flag_zip,
    build_lying_size_zip,
    build_site_zip,
    build_special_entry_zip,
    build_symlink_zip,
    build_traversal_zip,
    build_unsupported_compression_zip,
    build_zip,
)


@pytest.fixture(autouse=True)
def storage_root(tmp_path, monkeypatch):
    root = tmp_path / "artifacts"
    monkeypatch.setattr(settings, "ARTIFACT_STORAGE_DIR", str(root))
    return root


def _tmp_entries(root) -> list:
    tmp = root / ".tmp"
    return list(tmp.iterdir()) if tmp.exists() else []


def _ingest_zip(data: bytes, entry_path: str | None = None):
    return ArtifactIngestService.ingest(ZipSource(io.BytesIO(data)), entry_path)


def _files(**named: bytes) -> FilesSource:
    return FilesSource([SourceFile(name, io.BytesIO(body)) for name, body in named.items()])


def _expect_error(storage_root, status: int, fn, *args, match: str | None = None):
    with pytest.raises(ArtifactIngestError) as exc:
        fn(*args)
    assert exc.value.status == status, exc.value.message
    if match:
        assert match in exc.value.message
    assert _tmp_entries(storage_root) == [], "failed ingest left a scratch folder"
    return exc.value


# ── Happy path ──────────────────────────────────────────────────────


def test_site_zip_is_extracted_with_manifest(storage_root):
    result = _ingest_zip(build_site_zip())

    assert result.kind == "site"
    assert result.entry_path == "index.html"
    assert [f["path"] for f in result.files] == ["app.js", "data/data.sqlite", "index.html"]
    assert result.file_count == 3
    assert result.excluded == []
    by_path = {f["path"]: f for f in result.files}
    body = (result.tmp_dir / "app.js").read_bytes()
    assert by_path["app.js"]["sha256"] == hashlib.sha256(body).hexdigest()
    assert by_path["app.js"]["size"] == len(body)
    assert by_path["app.js"]["mime"] == "text/javascript; charset=utf-8"
    assert by_path["data/data.sqlite"]["mime"] == "application/vnd.sqlite3"
    assert result.size_bytes == sum(f["size"] for f in result.files)
    assert result.tmp_dir.parent == storage_root / ".tmp"


def test_promote_moves_scratch_into_revision_dir(storage_root):
    result = _ingest_zip(build_site_zip())
    artifact_id, revision_id = uuid.uuid4(), uuid.uuid4()

    target = ArtifactStorage.promote(result.tmp_dir, artifact_id, revision_id)

    assert target == storage_root / str(artifact_id) / str(revision_id)
    assert (target / "index.html").is_file()
    assert not result.tmp_dir.exists()
    with pytest.raises(FileExistsError):
        ArtifactStorage.promote(_ingest_zip(build_site_zip()).tmp_dir, artifact_id, revision_id)
    assert ArtifactStorage.remove_revision(artifact_id, revision_id)
    assert ArtifactStorage.remove_artifact(artifact_id)
    assert not (storage_root / str(artifact_id)).exists()


def test_leading_dot_slash_is_stripped():
    result = _ingest_zip(build_zip({"./index.html": "<html></html>", "./a/b.txt": "x"}))
    assert [f["path"] for f in result.files] == ["a/b.txt", "index.html"]


# ── Name rules ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "name",
    ["/etc/passwd", "C:/x.txt", "../escape.txt", "a/../../b", "a/./b", "a//b", "", "a\\b", "a\x00b", "a\nb"],
)
def test_normalize_path_rejects(name):
    with pytest.raises(ArtifactIngestError) as exc:
        normalize_path(name)
    assert exc.value.status == 400


def test_normalize_path_nfc_and_dir_marker():
    assert normalize_path("cafe\u0301/") == ("caf\u00e9", True)
    assert normalize_path("././x.html") == ("x.html", False)


def test_rejects_traversal_entry(storage_root):
    _expect_error(storage_root, 400, _ingest_zip, build_traversal_zip("../escape.txt"))


def test_rejects_absolute_entry(storage_root):
    _expect_error(storage_root, 400, _ingest_zip, build_traversal_zip("/abs.txt"), match="Absolute")


def test_rejects_backslash_entry(storage_root):
    _expect_error(storage_root, 400, _ingest_zip, build_traversal_zip("dir\\file.txt"), match="Backslash")


def test_rejects_symlink_entry_naming_it(storage_root):
    err = _expect_error(storage_root, 400, _ingest_zip, build_symlink_zip("assets/link"))
    assert "assets/link" in err.message and "symlink" in err.message


@pytest.mark.parametrize("file_type", [stat.S_IFCHR, stat.S_IFBLK, stat.S_IFIFO])
def test_rejects_device_and_fifo_entries(storage_root, file_type):
    _expect_error(storage_root, 400, _ingest_zip, build_special_entry_zip("dev", file_type))


def test_rejects_duplicate_after_nfc(storage_root):
    _expect_error(storage_root, 400, _ingest_zip, build_duplicate_zip(), match="Duplicate")


def test_rejects_case_insensitive_duplicates(storage_root):
    _expect_error(
        storage_root, 400, _ingest_zip, build_zip({"A.txt": "1", "a.txt": "2"}), match="case-insensitive"
    )


def test_rejects_file_and_folder_differing_only_in_case(storage_root):
    _expect_error(storage_root, 400, _ingest_zip, build_zip({"Foo/x.txt": "1", "foo": "2"}))


def test_rejects_encrypted_entry(storage_root):
    _expect_error(storage_root, 400, _ingest_zip, build_encrypted_flag_zip(), match="encrypted")


def test_rejects_unsupported_compression(storage_root):
    _expect_error(storage_root, 400, _ingest_zip, build_unsupported_compression_zip(), match="compression method")


def test_rejects_duplicate_in_files_source(storage_root):
    _expect_error(storage_root, 400, ArtifactIngestService.ingest, _files(**{"a.txt": b"1", "./a.txt": b"2"}))


def test_rejects_file_and_folder_with_same_name(storage_root):
    _expect_error(storage_root, 400, _ingest_zip, build_zip({"a": "1", "a/b.txt": "2"}))


def test_rejects_empty_upload(storage_root):
    _expect_error(storage_root, 400, _ingest_zip, build_zip({}))
    _expect_error(storage_root, 400, _ingest_zip, build_dotenv_zip()[:10])  # not a zip


# ── A10 drops ───────────────────────────────────────────────────────


def test_drops_and_lists_every_a10_pattern():
    result = _ingest_zip(
        build_zip(
            {
                "index.html": "<html></html>",
                ".git/config": "x",
                ".git/objects/ab/cd": "x",
                "sub/.git": "gitdir: ..",
                ".env": "S=1",
                ".env.local": "S=1",
                "cfg/.envrc": "S=1",
                "credentials/token.json": "{}",
                "app-data/db.sqlite": "x",
                "certs/server.pem": "x",
                "certs/server.KEY": "x",
                ".claude/settings.json": "{}",
                "node_modules/lib/index.js": "x",
                "web/node_modules/x.js": "x",
                ".DS_Store": "x",
                "img/.DS_Store": "x",
                "credentials.md": "kept: a file, not the folder",
                "keys.txt": "kept",
            }
        )
    )

    assert result.excluded == [
        ".DS_Store",
        ".claude/",
        ".env",
        ".env.local",
        ".git/",
        "app-data/",
        "certs/server.KEY",
        "certs/server.pem",
        "cfg/.envrc",
        "credentials/",
        "img/.DS_Store",
        "node_modules/",
        "sub/.git",
        "web/node_modules/",
    ]
    assert [f["path"] for f in result.files] == ["credentials.md", "index.html", "keys.txt"]
    assert not (result.tmp_dir / ".env").exists()


def test_a10_matching_ignores_case():
    result = _ingest_zip(
        build_zip(
            {
                "index.html": "<p>",
                ".ENV": "S=1",
                ".Env.local": "S=1",
                "Credentials/t.json": "{}",
                "NODE_MODULES/x.js": "x",
                ".Git/config": "x",
                "a/.ds_store": "x",
                "tls/Server.PEM": "x",
            }
        )
    )
    assert result.excluded == [
        ".ENV", ".Env.local", ".Git/", "Credentials/", "NODE_MODULES/", "a/.ds_store", "tls/Server.PEM"
    ]
    assert [f["path"] for f in result.files] == ["index.html"]


def test_excluded_list_is_capped(monkeypatch):
    from app.services.artifacts import artifact_ingest

    monkeypatch.setattr(artifact_ingest, "MAX_EXCLUDED_LISTED", 3)
    files = {"index.html": "<p>", **{f"k{i}.pem": "x" for i in range(5)}}
    result = _ingest_zip(build_zip(files))
    assert result.excluded == ["k0.pem", "k1.pem", "k2.pem", "…and 2 more"]


def test_only_excluded_files_is_empty_upload(storage_root):
    _expect_error(storage_root, 400, _ingest_zip, build_zip({".env": "x"}))


# ── Limits ──────────────────────────────────────────────────────────


def test_rejects_compression_ratio_over_100(storage_root):
    _expect_error(storage_root, 400, _ingest_zip, build_bomb_zip(), match="compression ratio")


def test_lying_headers_do_not_slip_past_the_cap(storage_root, monkeypatch):
    monkeypatch.setattr(settings, "ARTIFACT_MAX_FILE_BYTES", 1024)
    data = build_lying_size_zip(real_size=64 * 1024, declared_size=100)
    with pytest.raises(ArtifactIngestError):
        _ingest_zip(data)
    assert _tmp_entries(storage_root) == []


def test_actual_bytes_over_file_cap_without_headers(storage_root, monkeypatch):
    monkeypatch.setattr(settings, "ARTIFACT_MAX_FILE_BYTES", 1024)
    _expect_error(storage_root, 413, ArtifactIngestService.ingest, _files(**{"big.bin": b"x" * 1025}))


def test_actual_bytes_over_revision_cap_without_headers(storage_root, monkeypatch):
    monkeypatch.setattr(settings, "ARTIFACT_MAX_REVISION_BYTES", 1500)
    source = _files(**{"a.bin": b"x" * 1000, "b.bin": b"y" * 1000})
    _expect_error(storage_root, 413, ArtifactIngestService.ingest, source)


def test_rejects_more_than_max_files(storage_root):
    files = {f"f{i}.txt": "x" for i in range(settings.ARTIFACT_MAX_REVISION_FILES + 1)}
    _expect_error(storage_root, 413, _ingest_zip, build_zip(files, compression=0), match="Too many files")


def test_max_files_exactly_is_accepted(monkeypatch):
    monkeypatch.setattr(settings, "ARTIFACT_MAX_REVISION_FILES", 5)
    result = _ingest_zip(build_zip({f"f{i}.txt": "x" for i in range(5)}))
    assert result.file_count == 5


def test_rejects_file_over_25mb_by_declared_size(storage_root):
    # Stored, so the ratio rule does not fire first.
    data = build_zip({"big.bin": b"\x01" * (settings.ARTIFACT_MAX_FILE_BYTES + 1)}, compression=0)
    _expect_error(storage_root, 413, _ingest_zip, data, match="larger than")


def test_rejects_revision_over_cap_by_declared_size(storage_root, monkeypatch):
    monkeypatch.setattr(settings, "ARTIFACT_MAX_REVISION_BYTES", 1000)
    data = build_zip({"a.bin": b"\x01" * 600, "b.bin": b"\x02" * 600}, compression=0)
    _expect_error(storage_root, 413, _ingest_zip, data)


# ── Kind detection ──────────────────────────────────────────────────


def test_kind_entry_path_given():
    result = _ingest_zip(build_zip({"docs/report.html": "<p>", "index.html": "<p>"}), "docs/report.html")
    assert (result.kind, result.entry_path) == ("site", "docs/report.html")


def test_kind_root_index():
    result = _ingest_zip(build_zip({"index.html": "<p>", "data.csv": "a,b"}))
    assert (result.kind, result.entry_path) == ("site", "index.html")


def test_kind_nested_index_alone_is_files():
    result = _ingest_zip(build_zip({"site/index.html": "<p>", "data.csv": "a,b"}))
    assert (result.kind, result.entry_path) == ("files", None)


def test_kind_single_html():
    result = ArtifactIngestService.ingest(_files(**{"report.html": b"<p>"}))
    assert (result.kind, result.entry_path) == ("site", "report.html")


def test_kind_files_otherwise():
    result = ArtifactIngestService.ingest(_files(**{"report.pdf": b"%PDF", "notes.md": b"# n"}))
    assert (result.kind, result.entry_path) == ("files", None)


def test_kind_single_non_html_file_is_files():
    result = ArtifactIngestService.ingest(_files(**{"data.csv": b"a,b"}))
    assert (result.kind, result.entry_path) == ("files", None)


@pytest.mark.parametrize("entry", ["missing.html", "data.csv", "../index.html"])
def test_entry_path_must_be_a_manifest_html_file(storage_root, entry):
    data = build_zip({"index.html": "<p>", "data.csv": "a,b"})
    _expect_error(storage_root, 400, _ingest_zip, data, entry)


def test_entry_path_pointing_at_excluded_file_is_rejected(storage_root):
    data = build_zip({"index.html": "<p>", "node_modules/x.html": "<p>"})
    _expect_error(storage_root, 400, _ingest_zip, data, "node_modules/x.html")


# ── MIME table ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "path,mime",
    [
        ("index.html", "text/html; charset=utf-8"),
        ("a/b.HTM", "text/html; charset=utf-8"),
        ("app.mjs", "text/javascript; charset=utf-8"),
        ("style.css", "text/css; charset=utf-8"),
        ("data.json", "application/json; charset=utf-8"),
        ("data.csv", "text/csv; charset=utf-8"),
        ("data.tsv", "text/tab-separated-values; charset=utf-8"),
        ("README.md", "text/markdown; charset=utf-8"),
        ("logo.svg", "image/svg+xml; charset=utf-8"),
        ("sql-wasm.wasm", "application/wasm"),
        ("data.sqlite", "application/vnd.sqlite3"),
        ("data.sqlite3", "application/vnd.sqlite3"),
        ("data.db", "application/vnd.sqlite3"),
        ("doc.pdf", "application/pdf"),
        ("font.woff2", "font/woff2"),
        ("clip.mp4", "video/mp4"),
        ("photo.JPG", "image/jpeg"),
        ("Makefile", DEFAULT_MIME),
        ("archive.bin", DEFAULT_MIME),
        (".hidden", DEFAULT_MIME),
    ],
)
def test_mime_table(path, mime):
    assert mime_for_path(path) == mime


# ── Scratch hygiene ─────────────────────────────────────────────────


def test_stale_scratch_is_swept_at_ingest_start():
    stale = ArtifactStorage.tmp_dir()
    fresh = ArtifactStorage.tmp_dir()
    old = time.time() - 2 * 60 * 60
    os.utime(stale, (old, old))

    result = _ingest_zip(build_site_zip())

    assert not stale.exists()
    assert fresh.exists()
    assert result.tmp_dir.exists()
