"""Unit tests for ``git_operations.git_log_subdir``.

Pure mock tests — no network, no DB, no HTTP. They verify:

  - Commit parsing: the ``\x1f``/``\x1e`` separated format is decoded into
    the expected dict shape.
  - Multiple commits: returned in the order git log produces them (newest
    first, as the caller passed ``--max-count`` to git).
  - Subdir filter: when ``subdir`` is given, the path filter ``-- <subdir>/``
    is forwarded to ``repo.git.log``; without ``subdir`` no path filter is used.
  - Empty log: an empty output string returns an empty list without crashing.
  - Malformed record: a record with fewer than 6 fields is silently skipped.

``clone_repository_context`` (which does the real network clone) is replaced
by a context-manager stub so no internet access is required.

API-observable behaviour — the list-commits endpoint, subdir scoping after
a real connect/push, non-owner 404 — is covered in
``tests/api/agents/git/agents_git_source_test.py`` (Scenario 7).
"""
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from app.services.knowledge.git_operations import git_log_subdir

# Module-level name that git_log_subdir references at call time.
_CLONE_CTX = "app.services.knowledge.git_operations.clone_repository_context"

# \x1f (Unit Separator) and \x1e (Record Separator) mirror the format in
# git_log_subdir itself.
_US = "\x1f"
_RS = "\x1e"

_SHA_1 = "a" * 40
_SHA_2 = "b" * 40
_SHA_3 = "c" * 40


# ── helpers ───────────────────────────────────────────────────────────────────


def _log_row(sha: str, short_sha: str, author_name: str, author_email: str,
             date: str, message: str) -> str:
    """Build one git-log record in the format git_log_subdir parses."""
    return _US.join([sha, short_sha, author_name, author_email, date, message]) + _RS


def _make_clone_stub(log_output: str):
    """Return a ``clone_repository_context`` replacement yielding a mock repo.

    The mock repo's ``repo.git.log(...)`` returns ``log_output`` regardless
    of the arguments passed, so callers can assert on the structure.
    """
    @contextmanager
    def _ctx(*args, **kwargs):
        mock_repo = MagicMock()
        mock_repo.git.log.return_value = log_output
        yield "/tmp/fake_repo", mock_repo

    return _ctx


def _make_spy_stub(log_output: str) -> tuple:
    """Return (stub, captured) where captured["args"] holds the log call's positional args.

    Used to assert that the correct path filter was (or was not) passed to
    ``repo.git.log``.
    """
    captured: dict = {}

    @contextmanager
    def _ctx(*args, **kwargs):
        mock_repo = MagicMock()

        def _capturing_log(*a, **kw):
            captured["args"] = a
            return log_output

        mock_repo.git.log.side_effect = _capturing_log
        yield "/tmp/fake_repo", mock_repo

    return _ctx, captured


# ── parsing ───────────────────────────────────────────────────────────────────


def test_git_log_subdir_parses_single_commit() -> None:
    """A single-commit log output is decoded into the expected dict shape."""
    row = _log_row(
        _SHA_1, _SHA_1[:7], "Alice", "alice@example.com",
        "2024-01-01T00:00:00+00:00", "Initial export",
    )

    with patch(_CLONE_CTX, _make_clone_stub(row)):
        result = git_log_subdir(repo_url="https://github.com/x/y.git", ref="main")

    assert len(result) == 1
    commit = result[0]
    assert commit["sha"] == _SHA_1
    assert commit["short_sha"] == _SHA_1[:7]
    assert commit["author_name"] == "Alice"
    assert commit["author_email"] == "alice@example.com"
    assert commit["date"] == "2024-01-01T00:00:00+00:00"
    assert commit["message"] == "Initial export"


def test_git_log_subdir_parses_multiple_commits_in_order() -> None:
    """Multiple commits are returned newest-first (order git log emits them)."""
    row1 = _log_row(
        _SHA_2, _SHA_2[:7], "Bob", "bob@example.com",
        "2024-01-02T00:00:00+00:00", "Second push",
    )
    row2 = _log_row(
        _SHA_1, _SHA_1[:7], "Alice", "alice@example.com",
        "2024-01-01T00:00:00+00:00", "Initial export",
    )
    # git emits newest first; a literal newline between records is common.
    log = row1 + "\n" + row2

    with patch(_CLONE_CTX, _make_clone_stub(log)):
        result = git_log_subdir(repo_url="https://github.com/x/y.git", ref="main")

    assert len(result) == 2
    assert result[0]["sha"] == _SHA_2  # newest first
    assert result[1]["sha"] == _SHA_1


def test_git_log_subdir_empty_log_returns_empty_list() -> None:
    """An empty git log output returns an empty list without crashing."""
    with patch(_CLONE_CTX, _make_clone_stub("")):
        result = git_log_subdir(repo_url="https://github.com/x/y.git", ref="main")

    assert result == []


def test_git_log_subdir_malformed_record_is_skipped() -> None:
    """A record with fewer than 6 fields is silently skipped; others are parsed."""
    good_row = _log_row(
        _SHA_1, _SHA_1[:7], "Alice", "alice@example.com",
        "2024-01-01T00:00:00+00:00", "Good commit",
    )
    # Only 3 fields — too few to unpack 6.
    bad_row = "only" + _US + "three" + _US + "fields" + _RS

    # Good before bad: bad must not crash the parse loop.
    log = good_row + bad_row

    with patch(_CLONE_CTX, _make_clone_stub(log)):
        result = git_log_subdir(repo_url="https://github.com/x/y.git", ref="main")

    assert len(result) == 1
    assert result[0]["sha"] == _SHA_1


# ── subdir path filter ────────────────────────────────────────────────────────


def test_git_log_subdir_passes_subdir_path_filter_to_git_log() -> None:
    """When subdir is given, git log is called with ``-- <subdir>/``."""
    row = _log_row(
        _SHA_1, _SHA_1[:7], "Alice", "alice@example.com",
        "2024-01-01T00:00:00+00:00", "Add agent files",
    )

    stub, captured = _make_spy_stub(row)

    with patch(_CLONE_CTX, stub):
        result = git_log_subdir(
            repo_url="https://github.com/x/y.git",
            ref="main",
            subdir="myagent",
        )

    args = captured.get("args", ())
    # The path filter ``-- myagent/`` must appear as trailing positional args.
    assert "--" in args, f"Expected '--' separator in git log call, got {args}"
    assert "myagent/" in args, f"Expected 'myagent/' path filter in git log call, got {args}"
    # The commit was still parsed correctly.
    assert len(result) == 1
    assert result[0]["sha"] == _SHA_1


def test_git_log_subdir_no_subdir_omits_path_filter() -> None:
    """Without subdir, git log is called without a path filter (root scope)."""
    row = _log_row(
        _SHA_1, _SHA_1[:7], "Alice", "alice@example.com",
        "2024-01-01T00:00:00+00:00", "Root commit",
    )

    stub, captured = _make_spy_stub(row)

    with patch(_CLONE_CTX, stub):
        result = git_log_subdir(repo_url="https://github.com/x/y.git", ref="main")

    args = captured.get("args", ())
    # The path separator ``--`` must NOT appear when subdir is omitted.
    assert "--" not in args, (
        f"Expected no '--' separator in git log call without subdir, got {args}"
    )
    assert len(result) == 1


def test_git_log_subdir_three_commits_round_trip() -> None:
    """Three commits parse correctly; exercises the multi-record split loop."""
    rows = "".join([
        _log_row(_SHA_3, _SHA_3[:7], "Carol", "carol@x.com", "2024-01-03T00:00:00+00:00", "Third"),
        _log_row(_SHA_2, _SHA_2[:7], "Bob",   "bob@x.com",   "2024-01-02T00:00:00+00:00", "Second"),
        _log_row(_SHA_1, _SHA_1[:7], "Alice", "alice@x.com", "2024-01-01T00:00:00+00:00", "First"),
    ])

    with patch(_CLONE_CTX, _make_clone_stub(rows)):
        result = git_log_subdir(repo_url="https://github.com/x/y.git", max_count=10)

    assert len(result) == 3
    assert [c["sha"] for c in result] == [_SHA_3, _SHA_2, _SHA_1]
    assert result[2]["message"] == "First"
