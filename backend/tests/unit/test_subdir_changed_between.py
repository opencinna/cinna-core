"""Unit tests for ``git_operations.subdir_changed_between``.

Pure MagicMock tests — no network, no DB, no HTTP. They verify the three
decision branches of the subdir-scoped update-available check:

  - Same tree hash at HEAD and base_commit → ``False`` (subdir unchanged,
    no update visible). This is the core regression that was failing before
    the bug fix.
  - Different tree hashes → ``True`` (subdir changed, update available).
  - ``repo.remotes.origin.fetch(...)`` raises ``GitCommandError``
    (server disallows fetch-by-SHA, base_commit unreachable, or history
    rewritten) → conservatively returns ``True`` so we never silently
    hide a real update.
  - Leading / trailing slashes on the ``subdir`` argument are tolerated
    (the function strips them before building rev-parse paths).

``clone_repository_context`` is replaced by a context-manager stub so no
network access is required.  ``assert_git_url_allowed`` is also stubbed:
the egress check on the mock repo's remote URL runs inside the
``with clone_repository_context(...)`` block body (not inside the context
manager itself), so it still executes even with the clone mocked out.

The API-observable behavior — subdir-scoped and root-path update detection
through both API endpoints — is covered by
``tests/api/agents/agents_git_subdir_update_test.py``.
"""

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from git import GitCommandError

from app.services.knowledge.git_operations import subdir_changed_between

# ── Patch targets ─────────────────────────────────────────────────────────────

# clone_repository_context is called by subdir_changed_between at the module level.
_CLONE_CTX = "app.services.knowledge.git_operations.clone_repository_context"

# assert_git_url_allowed is called inside the `with clone_repository_context(...)`
# block (not inside the context manager itself), so it still fires even when the
# clone is mocked.  Patching it avoids DNS resolution in unit tests.
_EGRESS_GUARD = "app.services.knowledge.git_operations.assert_git_url_allowed"

# ── Reusable test values ──────────────────────────────────────────────────────

_SHA_BASE = "b" * 40  # last_synced_commit baseline (the base_commit argument)

# Two distinct subdir tree object hashes (what `git rev-parse HEAD:<subdir>` emits).
_TREE_HASH_SAME = "c" * 40  # both tip and base use this → subdir unchanged
_TREE_HASH_TIP = "d" * 40   # tip uses this, base uses _TREE_HASH_SAME → subdir changed


# ── Stub helpers ──────────────────────────────────────────────────────────────


def _make_stub(*, tip_tree: str, base_tree: str):
    """Return a ``clone_repository_context`` replacement with a mock repo.

    The yielded mock repo satisfies:
    - ``repo.remotes.origin.url`` — a HTTPS public URL so the egress guard
      can parse it (though we also stub the guard out).
    - ``repo.remotes.origin.fetch()`` — succeeds (no-op).
    - ``repo.git.rev_parse()`` — returns ``tip_tree`` on the first call
      (``HEAD:<subdir>``) and ``base_tree`` on the second (``<base>:<subdir>``).
    """

    @contextmanager
    def _ctx(*args, **kwargs):
        mock_repo = MagicMock()
        mock_repo.remotes.origin.url = "https://github.com/example/repo.git"
        mock_repo.git.rev_parse.side_effect = [tip_tree, base_tree]
        yield "/tmp/fake_repo", mock_repo

    return _ctx


def _make_fetch_error_stub():
    """Return a ``clone_repository_context`` replacement where fetch raises ``GitCommandError``.

    Simulates a server that disallows fetch-by-SHA (e.g. GitHub with
    ``uploadpack.allowReachableSHA1InWant`` disabled) or a base_commit that
    the remote no longer has (force-push / GC).
    """

    @contextmanager
    def _ctx(*args, **kwargs):
        mock_repo = MagicMock()
        mock_repo.remotes.origin.url = "https://github.com/example/repo.git"
        mock_repo.remotes.origin.fetch.side_effect = GitCommandError(
            "git fetch", 128
        )
        yield "/tmp/fake_repo", mock_repo

    return _ctx


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_unchanged_subdir_returns_false() -> None:
    """Same subdir tree hash at HEAD and base_commit → False (no update).

    This is the core regression path: commits advanced the remote HEAD but
    none touched ``myagent/``, so the tree object hash is byte-for-byte
    identical between the two commits.  The function must return False — the
    caller must NOT show an "update available" banner.
    """
    with (
        patch(_CLONE_CTX, _make_stub(tip_tree=_TREE_HASH_SAME, base_tree=_TREE_HASH_SAME)),
        patch(_EGRESS_GUARD),
    ):
        result = subdir_changed_between(
            repo_url="https://github.com/example/repo.git",
            ref="main",
            subdir="myagent",
            base_commit=_SHA_BASE,
        )

    assert result is False


def test_changed_subdir_returns_true() -> None:
    """Different subdir tree hashes → True (update available).

    A commit beyond the baseline touched ``myagent/``, so the tree object
    hash at HEAD differs from the hash at base_commit.
    """
    with (
        patch(_CLONE_CTX, _make_stub(tip_tree=_TREE_HASH_TIP, base_tree=_TREE_HASH_SAME)),
        patch(_EGRESS_GUARD),
    ):
        result = subdir_changed_between(
            repo_url="https://github.com/example/repo.git",
            ref="main",
            subdir="myagent",
            base_commit=_SHA_BASE,
        )

    assert result is True


def test_indeterminate_fetch_failure_returns_true_conservatively() -> None:
    """``GitCommandError`` during fetch → conservatively returns True.

    When the server disallows fetch-by-SHA (or the commit is unreachable),
    the comparison is indeterminate.  The safe choice is True — never hide a
    real update.  This is the conservative error path documented in
    ``subdir_changed_between``'s docstring.
    """
    with (
        patch(_CLONE_CTX, _make_fetch_error_stub()),
        patch(_EGRESS_GUARD),
    ):
        result = subdir_changed_between(
            repo_url="https://github.com/example/repo.git",
            ref="main",
            subdir="myagent",
            base_commit=_SHA_BASE,
        )

    assert result is True, (
        "Indeterminate fetch failure must return True to avoid silently "
        "hiding a real update"
    )


def test_subdir_leading_trailing_slashes_stripped() -> None:
    """Leading/trailing slashes on ``subdir`` are stripped and the result is still correct.

    Callers may pass ``"/myagent/"`` instead of ``"myagent"``.  The function
    strips slashes before building the ``rev-parse`` path arguments, so
    ``HEAD:/myagent/`` (invalid) never reaches git.
    """
    with (
        patch(_CLONE_CTX, _make_stub(tip_tree=_TREE_HASH_SAME, base_tree=_TREE_HASH_SAME)),
        patch(_EGRESS_GUARD),
    ):
        result = subdir_changed_between(
            repo_url="https://github.com/example/repo.git",
            ref="main",
            subdir="/myagent/",   # leading + trailing slashes
            base_commit=_SHA_BASE,
        )

    assert result is False
