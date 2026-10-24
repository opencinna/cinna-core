"""Archive determinism: build_skill_archive is build_tree_archive with a skills/ prefix."""
import hashlib
import io
import tarfile
from pathlib import Path

import pytest

from app.services.skills.skill_archive import build_skill_archive, build_tree_archive


def _files(root: Path) -> list[tuple[str, Path]]:
    (root / "scripts").mkdir(parents=True)
    (root / "SKILL.md").write_text("---\nname: demo\n---\nbody\n", encoding="utf-8")
    (root / "notes.txt").write_text("hello\n", encoding="utf-8")
    run = root / "scripts" / "run.sh"
    run.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    run.chmod(0o755)
    return sorted(
        ((p.relative_to(root).as_posix(), p) for p in root.rglob("*") if p.is_file()),
        key=lambda i: i[0],
    )


def test_skill_archive_is_deterministic_and_pinned(tmp_path: Path) -> None:
    files = _files(tmp_path)
    first = build_skill_archive("demo", files)
    assert build_skill_archive("demo", files) == first
    assert hashlib.sha256(first).hexdigest() == PINNED_DIGEST


def test_tree_archive_prefix_and_skill_equivalence(tmp_path: Path) -> None:
    files = _files(tmp_path)
    assert build_skill_archive("demo", files) == build_tree_archive("skills/demo", files)
    data = build_tree_archive("my-plugin", files)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
        names = [m.name for m in tf.getmembers()]
        modes = {m.name: m.mode for m in tf.getmembers()}
    assert names == [f"my-plugin/{rel}" for rel, _ in files]
    assert modes["my-plugin/scripts/run.sh"] == 0o755
    assert modes["my-plugin/notes.txt"] == 0o644


@pytest.mark.parametrize("prefix", ["", "/abs", "a/../b"])
def test_tree_archive_rejects_bad_prefix(tmp_path: Path, prefix: str) -> None:
    with pytest.raises(ValueError):
        build_tree_archive(prefix, _files(tmp_path))


PINNED_DIGEST = "07cb54886296d4e44ecc48bb6c82000ffca98b08807ca40368987329f4b416fd"
