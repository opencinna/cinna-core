"""Deterministic skill archive builder, shared by catalog and marketplace skills.

One tarball format for every skill core hands out: members
``skills/<name>/<rel>``, zeroed mtimes and ownership, two file modes, and a
gzip wrapper with ``mtime=0``. The same folder content therefore always gives
the same bytes, and so the same sha256, across syncs, rebuilds and machines.

Catalog revisions store that digest at publish and rebuild the archive after a
cache eviction, so the bytes this module produces for a given file list must
never change (``tests/unit/test_skill_catalog_archive.py`` pins a golden
digest). Marketplace skill snapshots content-address their archives by it.
"""
from __future__ import annotations

import gzip
import io
import os
import stat
import tarfile
from collections.abc import Sequence
from pathlib import Path, PurePosixPath

# ``_walk_files`` is private to ``skill_manifest``, but it is the exact walk
# that produced the parser's ``size_bytes`` (symlinked dirs and files skipped,
# ``SKIP_DIR_NAMES`` pruned). Reusing it keeps "what the parser measured" and
# "what the archive ships" one definition. ``skill_manifest.py`` is vendored
# into env-core, so it is imported here rather than edited to export a name.
from app.services.agents.skill_manifest import _walk_files


def walk_skill_tree(skill_dir: Path) -> tuple[list[tuple[str, Path]], int]:
    """Every regular file of a skill folder, sorted, plus their total size.

    Returns ``([(relative_posix, absolute_path), ...], total_bytes)`` in the
    order :func:`build_skill_archive` expects. Non-regular entries (FIFOs,
    sockets, devices) are dropped: they are not skill content and opening one
    could block.
    """
    files: list[tuple[str, Path]] = []
    total_bytes = 0
    for relative, stat_result in _walk_files(skill_dir):
        if not stat.S_ISREG(stat_result.st_mode):
            continue
        files.append((relative, skill_dir / relative))
        total_bytes += stat_result.st_size
    files.sort(key=lambda item: item[0])
    return files, total_bytes


def _check_member(rel: str, path: Path) -> None:
    pure = PurePosixPath(rel)
    if not rel or pure.is_absolute() or ".." in pure.parts:
        raise ValueError(f"skill archive member must be a relative path: {rel!r}")
    if path.is_symlink():
        raise ValueError(f"skill archive member is a symlink: {rel!r}")


def build_skill_archive(skill_name: str, files: Sequence[tuple[str, Path]]) -> bytes:
    """Build the deterministic ``.tar.gz`` for one skill.

    ``files`` is ``(relative_posix, absolute_path)`` pairs, already sorted by
    the relative path: the caller owns the order, and this function asserts it
    rather than re-sorting. Raises ``ValueError`` for a member that is absolute,
    contains ``..`` or is a symlink.
    """
    rels = [rel for rel, _ in files]
    assert rels == sorted(rels), "skill archive members must be pre-sorted"

    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        for rel, path in files:
            _check_member(rel, path)
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as handle:
                stat_result = os.fstat(handle.fileno())
                info = tarfile.TarInfo(name=f"skills/{skill_name}/{rel}")
                info.size = stat_result.st_size
                info.mtime = 0
                # Two modes only, chosen by whether the source file was
                # executable. Preserving the bit matters: a skill's
                # ``scripts/run.sh`` is meant to be run, and an install that
                # silently drops the bit would behave differently from the
                # same skill sitting in a local ``skills/`` folder. Collapsing
                # to two values (rather than copying st_mode) is what keeps the
                # tarball deterministic and drops setuid/setgid/sticky, which is
                # the actual privilege concern.
                info.mode = 0o755 if stat_result.st_mode & 0o111 else 0o644
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                info.type = tarfile.REGTYPE
                tar.addfile(info, handle)
    compressed = io.BytesIO()
    with gzip.GzipFile(fileobj=compressed, mode="wb", mtime=0) as gz:
        gz.write(raw.getvalue())
    return compressed.getvalue()
