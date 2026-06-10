"""
Unit tests for MCP workspace resource helper functions.

Pure logic only — URI parsing, logical↔disk path translation, MIME guessing,
and tree-collection. No DB, no TestClient, no adapter.

The adapter-backed read/list flows (WorkspaceResourceManager against a stubbed
environment adapter) live in
``tests/api/mcp_integration/test_mcp_resources.py``.
"""
import pytest

from app.mcp.resources import (
    ALLOWED_FOLDERS,
    _collect_files_from_tree,
    _disk_to_logical_path,
    _guess_mime_type,
    _is_text_mime,
    _logical_to_disk_path,
    _parse_workspace_uri,
)


# Sample workspace tree matching the real agent-env format (FileNode structure).
# Uploaded files live under app-data/uploads/ on disk but are surfaced to MCP
# clients at the legacy workspace://uploads/{path} URI by resources.py.
SAMPLE_TREE = {
    "files": {
        "name": "files", "type": "folder", "path": "files",
        "children": [
            {"name": "report.csv", "type": "file", "path": "files/report.csv", "size": 1234},
            {"name": "data", "type": "folder", "path": "files/data", "children": [
                {"name": "output.json", "type": "file", "path": "files/data/output.json", "size": 567},
            ]},
        ],
    },
    "scripts": {
        "name": "scripts", "type": "folder", "path": "scripts",
        "children": [
            {"name": "run.sh", "type": "file", "path": "scripts/run.sh", "size": 256},
        ],
    },
    "logs": {
        "name": "logs", "type": "folder", "path": "logs",
        "children": [
            {"name": "app.log", "type": "file", "path": "logs/app.log", "size": 9999},
        ],
    },
    "app-data": {
        "name": "app-data", "type": "folder", "path": "app-data",
        "children": [
            {"name": "uploads", "type": "folder", "path": "app-data/uploads", "children": [
                {"name": "photo.png", "type": "file", "path": "app-data/uploads/photo.png", "size": 89012},
            ]},
            # storage/ stays private — only app-data/uploads/ is exposed via MCP.
            {"name": "storage", "type": "folder", "path": "app-data/storage", "children": [
                {"name": "STATUS.md", "type": "file", "path": "app-data/storage/STATUS.md", "size": 42},
            ]},
        ],
    },
    "summaries": {
        "files": {"fileCount": 2, "totalSize": 1801},
        "uploads": {"fileCount": 1, "totalSize": 89012},
    },
}


# ── _parse_workspace_uri Tests ───────────────────────────────────────────────


def test_parse_workspace_uri_simple_file():
    """Parse a simple single-level file path."""
    folder, path = _parse_workspace_uri("workspace://files/report.csv")
    assert folder == "files"
    assert path == "report.csv"


def test_parse_workspace_uri_nested_path():
    """Parse a multi-segment nested file path."""
    folder, path = _parse_workspace_uri("workspace://scripts/sub/folder/run.sh")
    assert folder == "scripts"
    assert path == "sub/folder/run.sh"


def test_parse_workspace_uri_all_allowed_folders():
    """All allowed folders are accepted."""
    for folder_name in ALLOWED_FOLDERS:
        f, p = _parse_workspace_uri(f"workspace://{folder_name}/test.txt")
        assert f == folder_name
        assert p == "test.txt"


def test_parse_workspace_uri_blocked_folder():
    """Blocked folders (credentials, databases, etc.) are rejected."""
    for blocked in ("credentials", "databases", "docs", "knowledge", "logs"):
        with pytest.raises(ValueError, match="not accessible"):
            _parse_workspace_uri(f"workspace://{blocked}/secret.key")


def test_parse_workspace_uri_wrong_scheme():
    """Non-workspace schemes are rejected."""
    with pytest.raises(ValueError, match="Not a workspace URI"):
        _parse_workspace_uri("https://example.com/files/data.csv")


def test_parse_workspace_uri_no_path():
    """URI with folder but no file path is rejected."""
    with pytest.raises(ValueError, match="No file path"):
        _parse_workspace_uri("workspace://files/")


def test_parse_workspace_uri_no_folder():
    """URI with no folder is rejected."""
    with pytest.raises(ValueError, match="No folder"):
        _parse_workspace_uri("workspace:///test.txt")


# ── Logical ↔ disk path translation ──────────────────────────────────────────


def test_logical_to_disk_path_passthrough_for_top_level_folders():
    """Files under top-level allowed folders pass through unchanged."""
    assert _logical_to_disk_path("files/report.csv") == "files/report.csv"
    assert _logical_to_disk_path("scripts/run.sh") == "scripts/run.sh"
    assert _logical_to_disk_path("files/sub/deep/file.txt") == "files/sub/deep/file.txt"


def test_logical_to_disk_path_maps_uploads_to_app_data():
    """The uploads/ URI prefix maps to app-data/uploads/ on disk."""
    assert _logical_to_disk_path("uploads/photo.png") == "app-data/uploads/photo.png"
    assert _logical_to_disk_path("uploads/sub/img.jpg") == "app-data/uploads/sub/img.jpg"
    # Bare folder (no file path) still translates.
    assert _logical_to_disk_path("uploads") == "app-data/uploads"


def test_disk_to_logical_path_passthrough_for_top_level_folders():
    """Files in top-level allowed folders surface at the same logical path."""
    assert _disk_to_logical_path("files/report.csv") == "files/report.csv"
    assert _disk_to_logical_path("scripts/run.sh") == "scripts/run.sh"


def test_disk_to_logical_path_remaps_app_data_uploads():
    """Disk files under app-data/uploads/ surface under the logical uploads/ URI."""
    assert _disk_to_logical_path("app-data/uploads/photo.png") == "uploads/photo.png"
    assert _disk_to_logical_path("app-data/uploads/sub/img.jpg") == "uploads/sub/img.jpg"


def test_disk_to_logical_path_blocks_private_folders():
    """The rest of app-data/ and other private folders are blocked (None)."""
    assert _disk_to_logical_path("app-data/storage/STATUS.md") is None
    assert _disk_to_logical_path("app-data/runtime.sqlite") is None
    assert _disk_to_logical_path("logs/app.log") is None
    assert _disk_to_logical_path("credentials/secret.key") is None
    assert _disk_to_logical_path("docs/README.md") is None


# ── _guess_mime_type Tests ───────────────────────────────────────────────────


def test_guess_mime_type_common_extensions():
    """Common file extensions return expected MIME types."""
    assert _guess_mime_type("report.csv") in ("text/csv", "text/plain")
    assert _guess_mime_type("data.json") == "application/json"
    assert _guess_mime_type("readme.md") == "text/markdown"
    assert _guess_mime_type("script.py") in ("text/x-python", "text/plain")
    assert _guess_mime_type("image.png") == "image/png"
    assert _guess_mime_type("doc.pdf") == "application/pdf"


def test_guess_mime_type_unknown():
    """Unknown extensions fallback to application/octet-stream."""
    assert _guess_mime_type("file.xyz123") == "application/octet-stream"


def test_is_text_mime():
    """Text MIME types are correctly identified."""
    assert _is_text_mime("text/plain") is True
    assert _is_text_mime("text/csv") is True
    assert _is_text_mime("text/markdown") is True
    assert _is_text_mime("application/json") is True
    assert _is_text_mime("application/xml") is True
    assert _is_text_mime("image/png") is False
    assert _is_text_mime("application/pdf") is False
    assert _is_text_mime("application/octet-stream") is False


# ── _collect_files_from_tree Tests ───────────────────────────────────────────


def test_collect_files_from_tree_extracts_allowed_files():
    """Collects files from allowed folders, ignoring blocked folders.

    Files uploaded by users live under ``app-data/uploads/`` on disk but
    are surfaced as logical ``uploads/{path}`` so MCP clients see them at
    the stable URI. The rest of ``app-data/`` stays private.
    """
    files = _collect_files_from_tree(SAMPLE_TREE)
    paths = [f[0] for f in files]

    # Top-level allowed folders surface as-is.
    assert "files/report.csv" in paths
    assert "files/data/output.json" in paths
    assert "scripts/run.sh" in paths

    # app-data/uploads/ is remapped to the logical "uploads/" URI prefix.
    assert "uploads/photo.png" in paths
    # The on-disk path must NOT leak into the listing.
    assert "app-data/uploads/photo.png" not in paths

    # Other app-data/ subtrees (storage, sqlite, runtime state) stay private.
    assert "app-data/storage/STATUS.md" not in paths
    # Blocked top-level folders never appear.
    assert "logs/app.log" not in paths


def test_collect_files_from_tree_returns_name_and_size():
    """Each collected file has correct name and size."""
    files = _collect_files_from_tree(SAMPLE_TREE)
    by_path = {f[0]: (f[1], f[2]) for f in files}

    assert by_path["files/report.csv"] == ("report.csv", 1234)
    assert by_path["uploads/photo.png"] == ("photo.png", 89012)


def test_collect_files_from_tree_empty():
    """Empty tree returns no files."""
    assert _collect_files_from_tree({}) == []


def test_collect_files_from_tree_empty_folders():
    """Folders with no children return no files."""
    tree = {
        "files": {"name": "files", "type": "folder", "path": "files", "children": []},
        "scripts": {"name": "scripts", "type": "folder", "path": "scripts", "children": None},
    }
    assert _collect_files_from_tree(tree) == []


def test_collect_files_from_tree_skips_non_dict():
    """Non-dict values (like summaries) are ignored."""
    tree = {
        "summaries": {"fileCount": 5, "totalSize": 1000},
        "files": {
            "name": "files", "type": "folder", "path": "files",
            "children": [
                {"name": "a.txt", "type": "file", "path": "files/a.txt", "size": 10},
            ],
        },
    }
    files = _collect_files_from_tree(tree)
    assert len(files) == 1
    assert files[0][0] == "files/a.txt"
