"""Unit tests for ``git_operations.commit_all`` / ``init_repo_with_remote``.

Pure local-git tests — no network, no DB, no HTTP. They exercise the
empty-remote bootstrap path of git-backed agent versioning (connect onto an
empty remote): ``init_repo_with_remote`` leaves an **unborn HEAD** (no commits
yet), and ``commit_all`` must create the first commit against it without
crashing on ``repo.head.commit`` (which raises ``ValueError`` on an unborn
HEAD). This is the real interaction the API tests mock away.

Unit tests for the ``git_log_subdir`` parsing primitive live in
``tests/unit/test_git_log_subdir.py``.

The API-observable connect / init-path behavior is covered in
``tests/api/agents/git/agents_git_source_test.py`` (Scenario 6).
"""
from pathlib import Path

import git
import pytest

from app.services.knowledge.git_operations import commit_all, init_repo_with_remote


def _init_unborn_repo(path) -> git.Repo:
    """A freshly-initialized repo with HEAD pointed at an (unborn) ``main``."""
    repo = git.Repo.init(str(path))
    repo.git.symbolic_ref("HEAD", "refs/heads/main")
    return repo


# ── commit_all ───────────────────────────────────────────────────────────────


def test_commit_all_first_commit_on_unborn_head(tmp_path) -> None:
    """commit_all creates the first commit on an unborn HEAD (no ValueError)."""
    repo = _init_unborn_repo(tmp_path)
    (tmp_path / "file.txt").write_text("hello")

    sha = commit_all(repo, "Initial export", "Tester", "tester@example.com")

    assert sha == repo.head.commit.hexsha
    assert repo.head.commit.message.strip() == "Initial export"
    assert repo.head.commit.author.email == "tester@example.com"


def test_commit_all_noop_after_first_commit(tmp_path) -> None:
    """A second commit_all with no changes is a no-op (returns the same SHA)."""
    repo = _init_unborn_repo(tmp_path)
    (tmp_path / "file.txt").write_text("hello")
    first = commit_all(repo, "first", "Tester", "tester@example.com")

    second = commit_all(repo, "noop", "Tester", "tester@example.com")

    assert second == first
    assert len(list(repo.iter_commits())) == 1


# ── init_repo_with_remote ────────────────────────────────────────────────────


def test_init_repo_with_remote_creates_git_structure_and_adds_remote(tmp_path: Path) -> None:
    """init_repo_with_remote creates .git, points HEAD at <ref>, and configures origin.

    Uses a public HTTPS URL whose hostname passes the egress guard statically
    (no DNS resolution required for hostnames; private-IP literal-blocking runs
    without network access).
    """
    workdir = tmp_path / "myrepo"
    workdir.mkdir()

    repo = init_repo_with_remote(
        workdir=str(workdir),
        repo_url="https://github.com/example/agent.git",
        ref="main",
    )

    # .git was created.
    assert (workdir / ".git").is_dir()
    # HEAD is unborn but points at the correct branch.
    symbolic = repo.git.symbolic_ref("HEAD")
    assert symbolic == "refs/heads/main"
    # "origin" remote was configured.
    assert "origin" in [r.name for r in repo.remotes]
    # Remote URL is the HTTPS form (no key → HTTPS conversion applied).
    assert "github.com" in repo.remotes["origin"].url


def test_init_repo_with_remote_custom_ref(tmp_path: Path) -> None:
    """init_repo_with_remote points HEAD at the specified branch name."""
    workdir = tmp_path / "custom_ref"
    workdir.mkdir()

    repo = init_repo_with_remote(
        workdir=str(workdir),
        repo_url="https://github.com/example/agent.git",
        ref="release",
    )

    symbolic = repo.git.symbolic_ref("HEAD")
    assert symbolic == "refs/heads/release"


def test_init_repo_with_remote_egress_blocked_for_private_ip(tmp_path: Path) -> None:
    """init_repo_with_remote raises EgressBlockedError for a private-IP URL.

    The static literal-IP check in the egress guard fires before any git
    operation — no DNS resolution or network call is needed.
    """
    from app.services.common.egress_guard import EgressBlockedError

    workdir = tmp_path / "blocked"
    workdir.mkdir()

    with pytest.raises(EgressBlockedError):
        init_repo_with_remote(
            workdir=str(workdir),
            repo_url="http://192.168.1.1/repo.git",
            ref="main",
        )


def test_init_repo_with_remote_unborn_head_then_commit_all(tmp_path: Path) -> None:
    """After init_repo_with_remote, commit_all creates the first commit (end-to-end).

    This exercises the exact sequence the connect flow uses for the init path
    (empty remote): init → write files → commit_all → (mocked) fast_forward_push.
    """
    workdir = tmp_path / "e2e"
    workdir.mkdir()

    repo = init_repo_with_remote(
        workdir=str(workdir),
        repo_url="https://github.com/example/agent.git",
        ref="main",
    )

    # Write a file then commit — must not raise ValueError on unborn HEAD.
    (workdir / "hello.txt").write_text("world")
    sha = commit_all(repo, "First commit", "Tester", "tester@example.com")

    assert sha == repo.head.commit.hexsha
    assert repo.head.commit.message.strip() == "First commit"
    # First commit lands on branch "main" (set by symbolic-ref during init).
    assert repo.active_branch.name == "main"
