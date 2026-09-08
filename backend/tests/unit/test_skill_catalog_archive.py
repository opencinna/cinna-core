"""Skill-catalog archive construction and safe extraction — pure, no DB.

Two halves of one contract, both of which fail catastrophically and silently if
they regress:

* the backend builds a **deterministic** tarball, because the sha256 the
  container verifies is stored on the revision row at publish and the on-disk
  archive cache may be evicted and rebuilt at any later time. A non-reproducible
  tarball turns a cache eviction into a permanent checksum mismatch on every
  install of that revision, with no self-heal;
* the container **refuses** anything but plain files out of that tarball, so a
  hostile archive cannot write outside the plugin directory.

The API-level catalog tests (publish, visibility, install) live in
``tests/api/`` — these two are here because they are pure and because a
regression in either is unrecoverable rather than merely wrong.
"""
import hashlib
import io
import os
import stat
import tarfile
from pathlib import Path

import pytest

from app.services.skills.skill_catalog_service import SkillCatalogService


@pytest.fixture
def skill_tree(tmp_path: Path) -> Path:
    """A minimal but representative skill: a body, a nested asset, a script."""
    skill_dir = tmp_path / "skills" / "pdf-report"
    (skill_dir / "scripts").mkdir(parents=True)
    (skill_dir / "references").mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: pdf-report\ndescription: Makes PDFs\n---\nbody\n"
    )
    (skill_dir / "references" / "spec.md").write_text("reference text")
    script = skill_dir / "scripts" / "run.sh"
    script.write_text("#!/bin/sh\necho hi\n")
    os.chmod(script, 0o755)
    return skill_dir


def _members(data: bytes) -> list[tarfile.TarInfo]:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        return tar.getmembers()


def test_archive_is_reproducible_across_mtime_changes(skill_tree: Path) -> None:
    """The same bytes twice, even after every file's mtime moves.

    This is the property the stored ``archive_sha256`` rests on: the digest is
    written once at publish, and a rebuild after a cache eviction has to land on
    the same value or every container refuses the download.
    """
    first = SkillCatalogService._build_archive_bytes(skill_tree, "pdf-report")

    for path in skill_tree.rglob("*"):
        os.utime(path, (0, 0))
    os.utime(skill_tree, (0, 0))

    second = SkillCatalogService._build_archive_bytes(skill_tree, "pdf-report")

    assert hashlib.sha256(first).hexdigest() == hashlib.sha256(second).hexdigest()
    assert first == second


def test_archive_is_rooted_at_skills_name(skill_tree: Path) -> None:
    """Every path is ``skills/<name>/…`` — the layout the container extracts."""
    names = sorted(m.name for m in _members(
        SkillCatalogService._build_archive_bytes(skill_tree, "pdf-report")
    ))
    assert names == [
        "skills/pdf-report/SKILL.md",
        "skills/pdf-report/references/spec.md",
        "skills/pdf-report/scripts/run.sh",
    ]


def test_archive_zeroes_metadata_and_keeps_only_the_executable_bit(
    skill_tree: Path,
) -> None:
    """Timestamps and ownership are zeroed; ``scripts/`` stays runnable.

    Zeroing is what makes the tarball reproducible. The executable bit survives
    because a skill's scripts are meant to be run — but only as 0755/0644, so
    setuid/setgid/sticky can never ride along.
    """
    os.chmod(skill_tree / "scripts" / "run.sh", 0o6755)
    members = {
        m.name: m
        for m in _members(
            SkillCatalogService._build_archive_bytes(skill_tree, "pdf-report")
        )
    }

    for member in members.values():
        assert member.mtime == 0
        assert member.uid == member.gid == 0
        assert member.uname == member.gname == ""
        assert member.type == tarfile.REGTYPE

    assert members["skills/pdf-report/scripts/run.sh"].mode == 0o755
    assert members["skills/pdf-report/SKILL.md"].mode == 0o644
    assert not members["skills/pdf-report/scripts/run.sh"].mode & (
        stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX
    )


def test_archive_skips_symlinks(tmp_path: Path) -> None:
    """A symlink in the published tree is never followed into the archive."""
    skill_dir = tmp_path / "skills" / "leaky"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: leaky\ndescription: d\n---\n")
    secret = tmp_path / "secret.txt"
    secret.write_text("password")
    (skill_dir / "link.txt").symlink_to(secret)

    names = [m.name for m in _members(
        SkillCatalogService._build_archive_bytes(skill_dir, "leaky")
    )]
    assert names == ["skills/leaky/SKILL.md"]


# ── Container-side extraction ──────────────────────────────────────────


def _service(tmp_path: Path):
    """An ``AgentEnvService`` pointed at a throwaway workspace.

    Imported inside the helper because the env-template tree is on ``sys.path``
    through this package's conftest, not through the app.
    """
    from core.server.agent_env_service import AgentEnvService

    return AgentEnvService(str(tmp_path / "workspace"))


def _tar_of(entries) -> tarfile.TarFile:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        for info, payload in entries:
            tar.addfile(info, io.BytesIO(payload) if payload is not None else None)
    raw.seek(0)
    return tarfile.open(fileobj=raw, mode="r")


def _regular(name: str, payload: bytes = b"x", mode: int = 0o644):
    info = tarfile.TarInfo(name)
    info.size = len(payload)
    info.mode = mode
    info.type = tarfile.REGTYPE
    return info, payload


def _link(name: str, target: str, kind=tarfile.SYMTYPE):
    info = tarfile.TarInfo(name)
    info.type = kind
    info.linkname = target
    return info, None


def test_safe_extract_writes_regular_files(tmp_path: Path) -> None:
    dest = tmp_path / "out"
    dest.mkdir()
    written = _service(tmp_path)._safe_extract_tar(
        _tar_of([
            _regular("skills/a/SKILL.md"),
            _regular("skills/a/scripts/go.sh", mode=0o755),
        ]),
        dest,
    )
    assert written == 2
    assert (dest / "skills/a/SKILL.md").is_file()
    assert (dest / "skills/a/scripts/go.sh").stat().st_mode & 0o111


@pytest.mark.parametrize(
    "entry",
    [
        pytest.param(_regular("/etc/passwd"), id="absolute_path"),
        pytest.param(_regular("../escape.txt"), id="parent_traversal"),
        pytest.param(_regular("skills/a/../../escape.txt"), id="nested_traversal"),
        pytest.param(_link("skills/a/l", "/etc/passwd"), id="symlink"),
        pytest.param(
            _link("skills/a/h", "SKILL.md", kind=tarfile.LNKTYPE), id="hardlink"
        ),
    ],
)
def test_safe_extract_rejects_the_whole_archive(tmp_path: Path, entry) -> None:
    """One bad member fails the archive — it is not quietly skipped.

    A tarball carrying a symlink or a traversal is not an archive we understand,
    and extracting the rest of it would install a half-trusted tree.
    """
    dest = tmp_path / "out"
    dest.mkdir()
    with pytest.raises(ValueError):
        _service(tmp_path)._safe_extract_tar(
            _tar_of([_regular("skills/a/SKILL.md"), entry]), dest
        )


def test_safe_extract_enforces_the_expansion_cap(tmp_path: Path) -> None:
    """The cap is cumulative across members, not per member.

    A tarball can stay small while expanding to something that fills the
    container's disk, so the running total is what has to be checked. The cap is
    lowered on the instance rather than fed 64 MB of real data.
    """
    dest = tmp_path / "out"
    dest.mkdir()
    service = _service(tmp_path)
    service._CATALOG_EXTRACT_MAX_BYTES = 12

    with pytest.raises(ValueError, match="size limit"):
        service._safe_extract_tar(
            _tar_of([
                _regular("skills/a/one.bin", b"12345678"),
                _regular("skills/a/two.bin", b"12345678"),
            ]),
            dest,
        )


def test_download_cap_exceeds_the_publish_cap() -> None:
    """The container must accept anything the backend let a publisher publish.

    The publish cap measures the uncompressed tree; the download cap measures
    the compressed archive. A skill of already-compressed assets barely shrinks,
    so equal limits would make a skill at the boundary publishable and then
    uninstallable — a failure the consumer discovers and only the publisher can
    fix.
    """
    from core.server.agent_env_service import AgentEnvService

    from app.services.skills.skill_catalog_service import MAX_PACKAGE_BYTES

    assert AgentEnvService._CATALOG_ARCHIVE_MAX_BYTES > MAX_PACKAGE_BYTES
    assert (
        AgentEnvService._CATALOG_EXTRACT_MAX_BYTES
        >= AgentEnvService._CATALOG_ARCHIVE_MAX_BYTES
    )
