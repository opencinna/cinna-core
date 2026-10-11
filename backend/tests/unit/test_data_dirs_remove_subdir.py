"""Unit tests: ``app.services.common.data_dirs.remove_data_subdir``.

Deleting a bundle or a user removes the folder named after the row under a
data root (``BUNDLE_STORAGE_DIR/<bundle_id>``, ``APP_DATA_STORAGE_DIR/<user_id>``,
``UPLOAD_BASE_PATH/<user_id>``). The guard must never let a bad name turn that
into removing the root itself or anything outside it.

Notes:
  The API-observable ends are in
  ``tests/api/catalog/catalog_bundle_storage_cleanup_test.py`` (bundles) and
  ``tests/api/users/users_delete_data_cleanup_test.py`` (users).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.services.common.data_dirs import remove_data_subdir


@pytest.fixture
def root(tmp_path: Path) -> Path:
    r = tmp_path / "root"
    r.mkdir()
    (r / "keep").mkdir()
    (r / "keep" / "file.txt").write_text("keep")
    return r


def _untouched(root: Path) -> None:
    assert root.is_dir()
    assert (root / "keep" / "file.txt").read_text() == "keep"


def test_removes_the_named_folder_and_its_contents(root: Path):
    target = root / "abc-123"
    (target / "nested" / "deeper").mkdir(parents=True)
    (target / "nested" / "f.bin").write_bytes(b"x")

    assert remove_data_subdir(root, "abc-123", what="test") is True

    assert not target.exists()
    _untouched(root)


def test_a_missing_folder_counts_as_removed(root: Path):
    assert remove_data_subdir(root, "never-there", what="test") is True
    _untouched(root)


def test_accepts_a_string_root(root: Path):
    (root / "s").mkdir()
    assert remove_data_subdir(str(root), "s", what="test") is True
    assert not (root / "s").exists()


@pytest.mark.parametrize(
    "name",
    [
        "",
        ".",
        "..",
        "keep/..",
        "../root",
        "a/b",
        "/etc",
        "a\\b",
        "a\x00b",
        " keep",
        "keep ",
    ],
)
def test_refuses_names_that_are_not_one_plain_segment(root: Path, name: str):
    assert remove_data_subdir(root, name, what="test") is False
    _untouched(root)


def test_refuses_a_non_string_name(root: Path):
    assert remove_data_subdir(root, None, what="test") is False  # type: ignore[arg-type]
    _untouched(root)


def test_refuses_an_empty_root(tmp_path: Path, monkeypatch):
    victim = tmp_path / "victim"
    victim.mkdir()
    monkeypatch.chdir(tmp_path)

    assert remove_data_subdir("", "victim", what="test") is False
    assert victim.is_dir()


def test_refuses_a_symlink_and_leaves_its_target_alone(root: Path, tmp_path: Path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious.txt").write_text("precious")
    os.symlink(outside, root / "link")

    assert remove_data_subdir(root, "link", what="test") is False

    assert (root / "link").is_symlink()
    assert (outside / "precious.txt").read_text() == "precious"
    _untouched(root)


def test_refuses_a_dangling_symlink(root: Path, tmp_path: Path):
    os.symlink(tmp_path / "nowhere", root / "dangling")

    assert remove_data_subdir(root, "dangling", what="test") is False
    assert (root / "dangling").is_symlink()


def test_a_symlinked_root_still_removes_its_own_child(tmp_path: Path):
    real_root = tmp_path / "real"
    (real_root / "child").mkdir(parents=True)
    linked_root = tmp_path / "linked"
    os.symlink(real_root, linked_root)

    assert remove_data_subdir(linked_root, "child", what="test") is True
    assert not (real_root / "child").exists()
    assert real_root.is_dir()
