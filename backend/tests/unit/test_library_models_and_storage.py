"""Desktop agent repo building blocks that need no database: the repo model's
constants, and ``GitRepoStorage`` against real bare repos under ``tmp_path``.

Access to desktop agents is the catalog's (``tests/api/catalog/``).
"""
import json
import subprocess
import uuid
from pathlib import Path
from unittest.mock import patch

import re

import pytest

from app.core.config import settings
from app.models.library.git_repo import KIT_SLUG_PATTERN, GitRepo, GitRepoKind
from app.services.library.git_repo_storage import (
    GitRepoStorage,
    GitRepoStorageError,
    project_manifest,
)


@pytest.fixture
def storage(tmp_path: Path):
    with patch.object(settings, "GIT_STORAGE_DIR", str(tmp_path / "git")):
        yield tmp_path / "git"


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout.strip()


def _push_manifest(bare: Path, work: Path, manifest: object) -> str:
    _git("init", "--initial-branch=main", ".", cwd=work) if not (work / ".git").exists() else None
    (work / "cinna-agent.json").write_text(
        manifest if isinstance(manifest, str) else json.dumps(manifest)
    )
    _git("add", "-A", cwd=work)
    _git("commit", "-m", "m", cwd=work)
    # fetch (not push): the bare repo denies non-fast-forwards on receive
    _git("fetch", str(work), "+HEAD:refs/heads/main", cwd=bare)
    return _git("rev-parse", "HEAD", cwd=work)


# ── model ─────────────────────────────────────────────────────────────────────


def test_repo_kinds_slug_pattern_and_slim_columns() -> None:
    assert set(GitRepoKind.ALL) == {"agent", "mirror"}
    assert re.match(KIT_SLUG_PATTERN, "my-agent") and re.match(KIT_SLUG_PATTERN, "a1")
    for bad in ("A", "-x", "x", "has space", "a" * 64):
        assert not re.match(KIT_SLUG_PATTERN, bad), bad
    # Identity and access moved to the desktop bundle (C2).
    columns = set(GitRepo.__table__.columns.keys())
    assert not {"slug", "name", "description", "owner_user_id"} & columns
    assert {"id", "kind", "head_sha", "manifest_cache", "last_pushed_by_user_id"} <= columns


def test_clone_url_and_repo_id_in_url() -> None:
    rid = uuid.uuid4()
    url = GitRepoStorage.clone_url(rid)
    assert url == f"{settings.backend_base_url}/git/{rid}.git"
    assert GitRepoStorage.repo_id_in_url(url) == rid
    assert GitRepoStorage.repo_id_in_url(f"https://api.example.com/git/{rid}") == rid
    assert GitRepoStorage.repo_id_in_url(f"https://x/git/{rid}.git/") == rid
    for other in ("", "https://github.com/a/b.git", f"https://x/git/{rid}.git/extra"):
        assert GitRepoStorage.repo_id_in_url(other) is None


# ── manifest projection ────────────────────────────────────────────────────────


def test_project_manifest_keeps_only_strings_and_tolerates_garbage() -> None:
    assert project_manifest(None) is None
    assert project_manifest([1]) is None
    assert project_manifest({}) == {
        "id": None, "name": None, "description": None,
        "runtime": {"engine": None}, "contract_version": None,
    }
    projected = project_manifest(
        {
            "id": "abc", "name": {"x": 1}, "description": "d",
            "runtime": {"engine": "claude", "other": 1}, "contract_version": 1.8,
            "bundle": {"secret": "no"},
        }
    )
    assert projected == {
        "id": "abc", "name": None, "description": "d",
        "runtime": {"engine": "claude"}, "contract_version": None,
    }
    assert project_manifest({"runtime": "claude"})["runtime"] == {"engine": None}


# ── GitRepoStorage ─────────────────────────────────────────────────────────────


def test_repo_path_only_accepts_uuids(storage: Path) -> None:
    rid = uuid.uuid4()
    assert GitRepoStorage.repo_path(rid) == storage / f"{rid}.git"
    assert GitRepoStorage.repo_path(str(rid)) == storage / f"{rid}.git"
    for bad in ("../etc", "not-a-uuid", f"{rid}/../x", ""):
        with pytest.raises(ValueError):
            GitRepoStorage.repo_path(bad)


def test_git_env_is_hardened(storage: Path) -> None:
    env = GitRepoStorage.git_env()
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_CONFIG_GLOBAL"] == "/dev/null"
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["HOME"] == str(storage)
    assert GitRepoStorage.git_base_args()[-1] == f"core.hooksPath={settings.GIT_HOOKS_DIR}"


def test_init_bare_sets_the_receive_rules_and_refuses_bad_input(storage: Path) -> None:
    rid = uuid.uuid4()
    path = GitRepoStorage.init_bare(rid, GitRepoKind.AGENT)
    assert path == storage / f"{rid}.git" and (path / "HEAD").is_file()
    assert (path / "HEAD").read_text().strip() == "ref: refs/heads/main"
    cfg = lambda key: _git("config", "--get", key, cwd=path)  # noqa: E731
    assert cfg("receive.denyNonFastForwards") == "true"
    assert cfg("receive.denyDeletes") == "true"
    assert cfg("core.hooksPath") == settings.GIT_HOOKS_DIR
    assert cfg("cinna.kind") == "agent"
    assert cfg("uploadpack.allowFilter") == "true"

    with pytest.raises(GitRepoStorageError):
        GitRepoStorage.init_bare(rid, GitRepoKind.AGENT)  # already exists
    with pytest.raises(ValueError):
        GitRepoStorage.init_bare(uuid.uuid4(), "bogus")
    assert GitRepoStorage.init_bare(uuid.uuid4(), GitRepoKind.MIRROR).is_dir()


def test_head_sha_and_manifest_reading(storage: Path, tmp_path: Path) -> None:
    rid = uuid.uuid4()
    assert GitRepoStorage.head_sha(rid) is None  # no such repo
    bare = GitRepoStorage.init_bare(rid, GitRepoKind.AGENT)
    assert GitRepoStorage.head_sha(rid) is None  # empty repo
    assert GitRepoStorage.read_manifest_at(rid, "a" * 40) is None  # unknown sha

    work = tmp_path / "work"
    work.mkdir()
    sha1 = _push_manifest(
        bare, work,
        {"id": "i", "name": "N", "description": "D", "runtime": {"engine": "claude"},
         "contract_version": "1.9.0", "bundle": {"x": 1}},
    )
    assert GitRepoStorage.head_sha(rid) == sha1
    assert GitRepoStorage.read_manifest_at(rid, sha1) == {
        "id": "i", "name": "N", "description": "D",
        "runtime": {"engine": "claude"}, "contract_version": "1.9.0",
    }

    # garbage / non-object / oversized manifests yield None, never an exception
    sha2 = _push_manifest(bare, work, "{not json")
    assert GitRepoStorage.head_sha(rid) == sha2
    assert GitRepoStorage.read_manifest_at(rid, sha2) is None
    sha3 = _push_manifest(bare, work, "[1, 2]")
    assert GitRepoStorage.read_manifest_at(rid, sha3) is None
    sha4 = _push_manifest(bare, work, json.dumps({"name": "x" * (2 * 1024 * 1024)}))
    assert GitRepoStorage.read_manifest_at(rid, sha4) is None
    # still readable at the older sha
    assert GitRepoStorage.read_manifest_at(rid, sha1)["name"] == "N"
    # non-hex "sha" (a ref expression) is refused without invoking git
    assert GitRepoStorage.read_manifest_at(rid, "main:../../x") is None


def test_remove_moves_to_trash_and_two_removes_never_collide(storage: Path) -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    GitRepoStorage.init_bare(a, GitRepoKind.AGENT)
    GitRepoStorage.init_bare(b, GitRepoKind.AGENT)
    ta = GitRepoStorage.remove(a)
    tb = GitRepoStorage.remove(b)
    assert ta is not None and tb is not None and ta != tb
    assert ta.parent == storage / ".deleted" and (ta / "HEAD").is_file()
    assert not GitRepoStorage.repo_path(a).exists()
    assert GitRepoStorage.head_sha(a) is None
    assert GitRepoStorage.remove(a) is None  # already gone

    # remove -> recreate -> remove with the same id in the same second
    GitRepoStorage.init_bare(a, GitRepoKind.AGENT)
    again = GitRepoStorage.remove(a)
    assert again is not None and again != ta and again.is_dir() and ta.is_dir()


def test_commit_messages_tree_id_and_export_paths(storage: Path, tmp_path: Path) -> None:
    rid = uuid.uuid4()
    bare = GitRepoStorage.init_bare(rid, GitRepoKind.AGENT)
    work = tmp_path / "work"
    (work / "skills" / "hello").mkdir(parents=True)
    (work / "skills" / "hello" / "SKILL.md").write_text("---\nname: hello\n---\n")
    (work / "secret.txt").write_text("not exported")
    sha1 = _push_manifest(bare, work, {"id": "i"})
    (work / "AGENTS.md").write_text("x")
    _git("add", "-A", cwd=work)
    _git("commit", "-m", "Second\n\nwith body", cwd=work)
    _git("fetch", str(work), "+HEAD:refs/heads/main", cwd=bare)
    sha2 = _git("rev-parse", "HEAD", cwd=work)

    assert GitRepoStorage.commit_messages(rid, None, sha2) == "Second\n\nwith body\n\nm"
    assert GitRepoStorage.commit_messages(rid, sha1, sha2) == "Second\n\nwith body"
    assert GitRepoStorage.commit_messages(rid, sha2, sha2) is None
    assert GitRepoStorage.tree_id(rid, sha2) == _git("rev-parse", f"{sha2}^{{tree}}", cwd=work)
    assert GitRepoStorage.tree_id(rid, "main") is None  # not a sha

    dest = GitRepoStorage.export_paths(
        rid, sha2, ["cinna-agent.json", "skills", "missing"], tmp_path / "out"
    )
    assert json.loads((dest / "cinna-agent.json").read_text()) == {"id": "i"}
    assert (dest / "skills" / "hello" / "SKILL.md").is_file()
    assert not (dest / "secret.txt").exists() and not (dest / ".export.tar").exists()
