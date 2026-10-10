"""Team library push policy: a tracked ``.claude/settings.local.json`` copy.

Pure unit test (no DB, no TestClient): drives ``push_policy.main`` against a
tmp bare repo whose tip already tracks the never-travels file. That prior state
can't be created through a real push (the hook would refuse it), so it is
written directly. Covers the migration path Cinna Desktop relies on: a version
that deletes the file is accepted, a version that keeps it is refused.

Paired API-level file: ``tests/api/catalog/catalog_desktop_push_policy_test.py``.
"""
import io
import os
import subprocess
from pathlib import Path

import pytest

from app.services.library import push_policy

SETTINGS = ".claude/settings.local.json"
SENTENCE = (
    "This change includes the agent's Claude permission settings "
    "(.claude/settings.local.json), which never leave your computer."
)
ZERO = "0" * 40


def _git(cwd: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }
    out = subprocess.run(
        ["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True
    )
    return out.stdout.strip()


@pytest.fixture()
def seeded(tmp_path: Path):
    """(environ, work dir, tip sha) with the tip tracking SETTINGS in the bare repo."""
    from app.services.cli.platform_knowledge_assets import local_kit_dir

    bare, work = tmp_path / "bare.git", tmp_path / "work"
    bare.mkdir()
    work.mkdir()
    _git(bare, "init", "--bare", "-b", "main")
    _git(work, "init", "-b", "main")
    (work / ".claude").mkdir()
    (work / SETTINGS).write_text("{}")
    (work / "notes.md").write_text("one")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "prior state")
    _git(work, "push", str(bare), "main:refs/heads/main")  # direct, bypasses the hook
    environ = {
        **os.environ,
        "GIT_DIR": str(bare),
        "CINNA_KIT_DIR": str(local_kit_dir().resolve()),
        "CINNA_REPO_KIND": "mirror",  # skip the cinna-agent.json manifest rules
    }
    return environ, work, _git(work, "rev-parse", "HEAD")


def _push(environ, work: Path, old: str) -> tuple[int, str]:
    """Run the hook on the work tree's HEAD as a new version on top of ``old``."""
    new = _git(work, "rev-parse", "HEAD")
    # Objects land in the bare repo without moving any ref, like git's quarantine.
    _git(work, "push", environ["GIT_DIR"], f"{new}:refs/heads/incoming")
    _git(Path(environ["GIT_DIR"]), "update-ref", "-d", "refs/heads/incoming")
    err = io.StringIO()
    code = push_policy.main(io.StringIO(f"{old} {new} refs/heads/main\n"), environ, err)
    return code, err.getvalue()


def test_version_removing_tracked_settings_file_is_accepted(seeded) -> None:
    environ, work, tip = seeded
    (work / SETTINGS).unlink()
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "drop settings")
    code, err = _push(environ, work, tip)
    assert code == 0, err
    assert SENTENCE not in err


def test_version_keeping_tracked_settings_file_is_refused(seeded) -> None:
    environ, work, tip = seeded
    (work / "notes.md").write_text("two")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "edit other file")
    code, err = _push(environ, work, tip)
    assert code == 1
    assert SENTENCE in err
