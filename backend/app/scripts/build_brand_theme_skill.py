"""Build the uploadable archive of the ``brand-theme`` catalog skill.

The skill's source lives in ``app/services/brand_theme/skill/brand-theme/``. A
superuser publishes it through the catalog's upload path
(``POST /api/v1/skills/packages/publish``, the archive Cinna Desktop sends),
which takes ``tar -C skills -czf x.tgz <name>``: one top-level folder named
like the skill. This writes that archive deterministically (sorted entries,
zeroed times and owners), leaving out caches and OS junk.

Standard library only, so it runs on the host::

    python3 backend/app/scripts/build_brand_theme_skill.py drafts/brand-theme.tgz

(``make brand-theme-skill`` does the same).
"""
from __future__ import annotations

import argparse
import gzip
import io
import os
import sys
import tarfile
from pathlib import Path

SKILL_NAME = "brand-theme"
SKILL_DIR = Path(__file__).resolve().parents[1] / "services" / "brand_theme" / "skill" / SKILL_NAME
_SKIPPED_DIRS = frozenset({"__pycache__", ".pytest_cache"})
_SKIPPED_FILES = frozenset({".DS_Store", "Thumbs.db"})


def _skipped(name: str) -> bool:
    return name in _SKIPPED_FILES or name.startswith("._") or name.endswith((".pyc", ".pyo"))


def skill_files(skill_dir: Path = SKILL_DIR) -> list[Path]:
    """Every file the archive carries, relative to ``skill_dir``, sorted."""
    files: list[Path] = []
    for root, dirs, names in os.walk(skill_dir):
        dirs[:] = sorted(d for d in dirs if d not in _SKIPPED_DIRS and not d.startswith("."))
        for name in sorted(names):
            if not _skipped(name):
                files.append((Path(root) / name).relative_to(skill_dir))
    return files


def _info(name: str, *, is_dir: bool, size: int = 0, executable: bool = False) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = tarfile.DIRTYPE if is_dir else tarfile.REGTYPE
    info.mode = 0o755 if is_dir or executable else 0o644
    info.size = size
    info.mtime = 0
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    return info


def build_archive(skill_dir: Path = SKILL_DIR) -> bytes:
    """The skill as a deterministic ``<name>/…`` tar.gz."""
    raw = io.BytesIO()
    with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as gz:
        with tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tar:
            written_dirs: set[str] = set()
            for rel in skill_files(skill_dir):
                # Parent folders first, from the skill folder itself down.
                for parent in reversed(rel.parents):
                    name = skill_dir.name if parent == Path(".") else f"{skill_dir.name}/{parent.as_posix()}"
                    if name not in written_dirs:
                        written_dirs.add(name)
                        tar.addfile(_info(name, is_dir=True))
                source = skill_dir / rel
                data = source.read_bytes()
                # The four entry points are executable whatever the checkout's
                # file modes (core.fileMode=false), so the archive is the same everywhere.
                executable = rel.parent == Path("scripts")
                tar.addfile(
                    _info(f"{skill_dir.name}/{rel.as_posix()}", is_dir=False, size=len(data), executable=executable),
                    io.BytesIO(data),
                )
    return raw.getvalue()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the brand-theme skill archive for catalog upload.")
    parser.add_argument("out", help="archive to write, e.g. drafts/brand-theme.tgz")
    args = parser.parse_args(argv)
    data = build_archive()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    print(f"Wrote {out} ({len(data) // 1024} KB, {len(skill_files())} files from {SKILL_DIR}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
