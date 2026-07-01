"""Unit tests for the provider-aware git web-URL builders.

Pure string logic — no network, no DB. Verifies ``build_web_history_url`` and
``build_web_commit_url``:

  - GitHub SSH + HTTPS repo URLs both resolve to the same web URL.
  - The subdir is appended (slashes normalized); the documented example matches.
  - No subdir → URL ends at the ref.
  - Single-commit URL uses singular ``/commit/<sha>`` and matches the example.
  - Non-GitHub hosts (GitLab, Bitbucket, self-hosted) return ``None`` (until a
    provider is registered for them).
  - Unparseable / empty input returns ``None``.
"""
from app.services.knowledge.git_operations import (
    build_web_commit_url,
    build_web_history_url,
    build_web_tree_url,
)


def test_github_ssh_with_subdir_matches_documented_example() -> None:
    url = build_web_history_url(
        "git@github.com:evgeny-l/cinna-agents-testing.git",
        ref="main",
        subdir="agents/localhost/hello-testing",
    )
    assert url == (
        "https://github.com/evgeny-l/cinna-agents-testing/commits/main/"
        "agents/localhost/hello-testing"
    )


def test_github_https_equivalent_to_ssh() -> None:
    ssh = build_web_history_url(
        "git@github.com:org/repo.git", ref="dev", subdir="a/b"
    )
    https = build_web_history_url(
        "https://github.com/org/repo.git", ref="dev", subdir="a/b"
    )
    assert ssh == https == "https://github.com/org/repo/commits/dev/a/b"


def test_no_subdir_ends_at_ref() -> None:
    assert (
        build_web_history_url("https://github.com/org/repo", ref="main")
        == "https://github.com/org/repo/commits/main"
    )


def test_subdir_slashes_normalized() -> None:
    assert build_web_history_url(
        "git@github.com:org/repo.git", ref="main", subdir="/sub/dir/"
    ) == "https://github.com/org/repo/commits/main/sub/dir"


def test_default_ref_is_main() -> None:
    assert build_web_history_url("git@github.com:org/repo.git").endswith(
        "/commits/main"
    )


def test_non_github_hosts_return_none() -> None:
    for repo_url in (
        "git@gitlab.com:org/repo.git",
        "https://bitbucket.org/org/repo.git",
        "git@git.internal.example.com:team/repo.git",
        "https://192.168.1.10/team/repo.git",
    ):
        assert build_web_history_url(repo_url, subdir="x") is None, repo_url
        assert build_web_commit_url(repo_url, "abc123") is None, repo_url
        assert build_web_tree_url(repo_url, subdir="x") is None, repo_url


def test_unparseable_returns_none() -> None:
    assert build_web_history_url("not a url") is None
    assert build_web_history_url("") is None


# ── build_web_commit_url ──────────────────────────────────────────────────────


def test_github_commit_url_matches_documented_example() -> None:
    sha = "3e5f397420161adfbfa7fc6614f20f96b9158505"
    url = build_web_commit_url(
        "git@github.com:evgeny-l/cinna-agents-testing.git", sha
    )
    assert url == (
        f"https://github.com/evgeny-l/cinna-agents-testing/commit/{sha}"
    )


def test_github_commit_url_ssh_https_parity() -> None:
    ssh = build_web_commit_url("git@github.com:org/repo.git", "deadbeef")
    https = build_web_commit_url("https://github.com/org/repo", "deadbeef")
    assert ssh == https == "https://github.com/org/repo/commit/deadbeef"


def test_commit_url_empty_sha_returns_none() -> None:
    assert build_web_commit_url("git@github.com:org/repo.git", "") is None


# ── build_web_tree_url ────────────────────────────────────────────────────────


def test_github_tree_url_matches_documented_example() -> None:
    url = build_web_tree_url(
        "git@github.com:evgeny-l/cinna-agents-testing.git",
        ref="main",
        subdir="agents/localhost/hello-testing",
    )
    assert url == (
        "https://github.com/evgeny-l/cinna-agents-testing/tree/main/"
        "agents/localhost/hello-testing"
    )


def test_github_tree_url_ssh_https_parity_and_no_subdir() -> None:
    ssh = build_web_tree_url("git@github.com:org/repo.git", ref="dev")
    https = build_web_tree_url("https://github.com/org/repo", ref="dev")
    assert ssh == https == "https://github.com/org/repo/tree/dev"
