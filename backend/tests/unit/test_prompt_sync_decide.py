"""Unit tests for prompt_sync.py — pure decision logic.

Covers the full decision table for ``decide()``, plus ``normalise()`` and
``content_hash()``. No database, no HTTP, no filesystem — all pure Python.

Decision table summary:
  NOOP         — db_hash == env_hash (either both None or same content)
  PUSH         — env is None/empty while DB has content (env-file restore)
  SEED_PUSH    — base is None, DB has content (first-sync, DB authoritative)
  SEED_PULL    — base is None, DB empty, env has content (pull from env)
  PULL         — only env changed relative to base
  PUSH         — only DB changed relative to base
  CONFLICT_PULL — both changed, LWW → env wins  (env_ts >= db_ts)
  CONFLICT_PUSH — both changed, LWW → DB wins   (db_ts > env_ts)
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone, timedelta

import pytest

from app.services.environments.prompt_sync import (
    ReconcileAction,
    PULL_ACTIONS,
    PUSH_ACTIONS,
    content_hash,
    decide,
    normalise,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _hash(text: str) -> str:
    """Compute the expected SHA-256 hex digest of a strip()'d UTF-8 string."""
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


def _ts(offset_seconds: float = 0) -> datetime:
    """Return a timezone-aware UTC datetime, optionally offset from the epoch."""
    return datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc) + timedelta(
        seconds=offset_seconds
    )


# Convenient short aliases
_OLDER = _ts(-100)
_NEWER = _ts(100)
_SAME = _ts(0)

# Content strings used across tests
_DB_CONTENT = "## Workflow\nDo step 1, step 2."
_ENV_CONTENT = "## Workflow\nDo step 1, step 2, step 3."
_BASE_HASH = _hash("## Workflow\nDo step 1.")


# ---------------------------------------------------------------------------
# 1. normalise()
# ---------------------------------------------------------------------------

class TestNormalise:

    def test_none_returns_none(self):
        assert normalise(None) is None

    def test_empty_string_returns_none(self):
        assert normalise("") is None

    def test_whitespace_only_returns_none(self):
        assert normalise("   \n\t  ") is None

    def test_strips_leading_and_trailing_whitespace(self):
        assert normalise("  hello  ") == "hello"

    def test_strips_trailing_newline(self):
        assert normalise("## Prompt\ncontent\n") == "## Prompt\ncontent"

    def test_preserves_internal_whitespace(self):
        raw = "line 1\n\nline 2\n"
        assert normalise(raw) == "line 1\n\nline 2"

    def test_non_empty_after_strip_returned_as_is(self):
        assert normalise("hello") == "hello"

    def test_single_newline_is_none(self):
        assert normalise("\n") is None


# ---------------------------------------------------------------------------
# 2. content_hash()
# ---------------------------------------------------------------------------

class TestContentHash:

    def test_none_returns_none(self):
        assert content_hash(None) is None

    def test_empty_string_returns_none(self):
        assert content_hash("") is None

    def test_whitespace_only_returns_none(self):
        assert content_hash("   ") is None

    def test_hash_is_sha256_of_normalised_content(self):
        text = "## Workflow\nDo step 1."
        expected = _hash(text)
        assert content_hash(text) == expected

    def test_trailing_newline_does_not_change_hash(self):
        """Two strings identical except for trailing newline produce the same hash."""
        a = "## Workflow\ncontent"
        b = "## Workflow\ncontent\n"
        assert content_hash(a) == content_hash(b)

    def test_leading_whitespace_does_not_change_hash(self):
        a = "  ## Prompt  \ncontent"
        b = "## Prompt  \ncontent"
        # only outer strip; internal whitespace preserved
        # normalise strips leading/trailing of the whole string
        assert content_hash("  hello") == content_hash("hello")

    def test_different_content_produces_different_hash(self):
        assert content_hash("version 1") != content_hash("version 2")

    def test_hash_is_64_hex_chars(self):
        h = content_hash("some content")
        assert h is not None
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)


# ---------------------------------------------------------------------------
# 3. decide() — NOOP cases
# ---------------------------------------------------------------------------

class TestDecideNoop:

    def test_both_none_is_noop(self):
        """Both sides empty/None and no base → NOOP (content_hash(None)==None both sides)."""
        assert decide(None, None, None, None, None) == ReconcileAction.NOOP

    def test_same_content_is_noop(self):
        c = "## Workflow\nDo step 1."
        h = content_hash(c)
        assert decide(c, c, h, _OLDER, _NEWER) == ReconcileAction.NOOP

    def test_same_content_trailing_newline_difference_is_noop(self):
        """Trailing newline is stripped in the comparison key → NOOP."""
        db = "## Workflow\ncontent"
        env = "## Workflow\ncontent\n"
        assert decide(db, env, content_hash(db), _OLDER, _NEWER) == ReconcileAction.NOOP

    def test_noop_when_base_matches_both(self):
        """DB == env (same hash) with a valid base → NOOP regardless of timestamps."""
        c = "hello"
        h = content_hash(c)
        assert decide(c, c, h, _NEWER, _OLDER) == ReconcileAction.NOOP

    def test_both_none_with_some_base_is_noop(self):
        """If both sides normalise to None, they share the same hash (None == None)."""
        assert decide(None, None, "some_old_hash", _NEWER, _OLDER) == ReconcileAction.NOOP


# ---------------------------------------------------------------------------
# 4. decide() — env None/empty + DB has content → PUSH (env-file restore)
# ---------------------------------------------------------------------------

class TestDecidePushEnvEmpty:

    def test_env_none_db_content_no_base_is_push(self):
        """env=None, db has content, base=None → PUSH (restore env file)."""
        assert decide(_DB_CONTENT, None, None, None, None) == ReconcileAction.PUSH

    def test_env_none_db_content_with_base_is_push(self):
        """env=None overrides the normal baseline logic → always PUSH to restore."""
        base = content_hash(_DB_CONTENT)
        assert decide(_DB_CONTENT, None, base, _OLDER, None) == ReconcileAction.PUSH

    def test_env_empty_string_db_content_is_push(self):
        """Empty-string env content normalises to None → PUSH."""
        assert decide(_DB_CONTENT, "", None, None, None) == ReconcileAction.PUSH

    def test_env_whitespace_db_content_is_push(self):
        """Whitespace-only env content normalises to None → PUSH."""
        assert decide(_DB_CONTENT, "   \n  ", None, None, None) == ReconcileAction.PUSH

    def test_env_none_db_none_is_noop_not_push(self):
        """PUSH is triggered only when DB has content. Both None → NOOP."""
        assert decide(None, None, None, None, None) == ReconcileAction.NOOP


# ---------------------------------------------------------------------------
# 5. decide() — SEED cases (base is None, but env is not empty)
# ---------------------------------------------------------------------------

class TestDecideSeed:

    def test_seed_push_db_content_env_content_no_base(self):
        """Both sides have different content, no base → SEED_PUSH (DB authoritative)."""
        assert decide(_DB_CONTENT, _ENV_CONTENT, None, None, None) == ReconcileAction.SEED_PUSH

    def test_seed_push_db_content_env_none_handled_as_push(self):
        """env=None is handled earlier (push to restore) before reaching seed logic."""
        # The PUSH path fires before the base=None check.
        assert decide(_DB_CONTENT, None, None, None, None) == ReconcileAction.PUSH

    def test_seed_pull_db_none_env_content_no_base(self):
        """DB is None/empty, env has content, base is None → SEED_PULL."""
        assert decide(None, _ENV_CONTENT, None, None, None) == ReconcileAction.SEED_PULL

    def test_seed_pull_db_empty_string_env_content_no_base(self):
        """Empty-string DB side normalises to None → SEED_PULL."""
        assert decide("", _ENV_CONTENT, None, None, None) == ReconcileAction.SEED_PULL

    def test_seed_pull_db_whitespace_env_content_no_base(self):
        """Whitespace DB side normalises to None → SEED_PULL."""
        assert decide("   ", _ENV_CONTENT, None, None, None) == ReconcileAction.SEED_PULL

    def test_seed_push_is_in_push_actions(self):
        action = decide(_DB_CONTENT, _ENV_CONTENT, None, None, None)
        assert action in PUSH_ACTIONS

    def test_seed_pull_is_in_pull_actions(self):
        action = decide(None, _ENV_CONTENT, None, None, None)
        assert action in PULL_ACTIONS


# ---------------------------------------------------------------------------
# 6. decide() — PULL (only env changed relative to base)
# ---------------------------------------------------------------------------

class TestDecidePull:

    def test_only_env_changed_is_pull(self):
        original = "original content"
        base = content_hash(original)
        db = original          # DB unchanged
        env = _ENV_CONTENT     # env changed
        assert decide(db, env, base, _OLDER, _NEWER) == ReconcileAction.PULL

    def test_pull_regardless_of_timestamps(self):
        """PULL fires when only env changed, even if DB ts is newer."""
        original = "original content"
        base = content_hash(original)
        db = original
        env = _ENV_CONTENT
        assert decide(db, env, base, _NEWER, _OLDER) == ReconcileAction.PULL

    def test_pull_is_in_pull_actions(self):
        original = "original content"
        base = content_hash(original)
        action = decide(original, _ENV_CONTENT, base, _OLDER, _NEWER)
        assert action in PULL_ACTIONS

    def test_pull_not_triggered_if_base_is_none(self):
        """Without a base hash the decision falls to SEED_PULL, not PULL."""
        original = "original content"
        action = decide(original, _ENV_CONTENT, None, _OLDER, _NEWER)
        assert action == ReconcileAction.SEED_PUSH  # DB present → SEED_PUSH


# ---------------------------------------------------------------------------
# 7. decide() — PUSH (only DB changed relative to base)
# ---------------------------------------------------------------------------

class TestDecidePush:

    def test_only_db_changed_is_push(self):
        original = "original content"
        base = content_hash(original)
        db = _DB_CONTENT       # DB changed
        env = original         # env unchanged
        assert decide(db, env, base, _NEWER, _OLDER) == ReconcileAction.PUSH

    def test_push_regardless_of_timestamps(self):
        """PUSH fires when only DB changed, even if env ts is newer."""
        original = "original content"
        base = content_hash(original)
        db = _DB_CONTENT
        env = original
        assert decide(db, env, base, _OLDER, _NEWER) == ReconcileAction.PUSH

    def test_push_is_in_push_actions(self):
        original = "original content"
        base = content_hash(original)
        action = decide(_DB_CONTENT, original, base, _NEWER, _OLDER)
        assert action in PUSH_ACTIONS


# ---------------------------------------------------------------------------
# 8. decide() — CONFLICT (both sides changed, LWW tiebreak)
# ---------------------------------------------------------------------------

class TestDecideConflict:

    def _setup(self):
        """Return (db, env, base_hash) where both sides diverge from the base."""
        original = "original shared content"
        base = content_hash(original)
        db = "## DB edited version"
        env = "## Env edited version"
        return db, env, base

    def test_conflict_pull_when_env_ts_newer(self):
        """env_ts > db_ts → env wins → CONFLICT_PULL."""
        db, env, base = self._setup()
        result = decide(db, env, base, _OLDER, _NEWER)
        assert result == ReconcileAction.CONFLICT_PULL

    def test_conflict_push_when_db_ts_newer(self):
        """db_ts > env_ts → DB wins → CONFLICT_PUSH."""
        db, env, base = self._setup()
        result = decide(db, env, base, _NEWER, _OLDER)
        assert result == ReconcileAction.CONFLICT_PUSH

    def test_conflict_pull_on_tie_favours_env(self):
        """env_ts == db_ts → tie → env wins (>= semantics) → CONFLICT_PULL."""
        db, env, base = self._setup()
        result = decide(db, env, base, _SAME, _SAME)
        assert result == ReconcileAction.CONFLICT_PULL

    def test_conflict_pull_when_db_ts_none_env_ts_set(self):
        """db_ts=None treated as MIN → env wins."""
        db, env, base = self._setup()
        result = decide(db, env, base, None, _NEWER)
        assert result == ReconcileAction.CONFLICT_PULL

    def test_conflict_pull_when_both_ts_none(self):
        """Both None → MIN >= MIN (tie) → env wins."""
        db, env, base = self._setup()
        result = decide(db, env, base, None, None)
        assert result == ReconcileAction.CONFLICT_PULL

    def test_conflict_push_when_env_ts_none_db_ts_set(self):
        """env_ts=None treated as MIN, db_ts set → db_ts > MIN → DB wins."""
        db, env, base = self._setup()
        result = decide(db, env, base, _NEWER, None)
        assert result == ReconcileAction.CONFLICT_PUSH

    def test_conflict_pull_is_in_pull_actions(self):
        db, env, base = self._setup()
        action = decide(db, env, base, _OLDER, _NEWER)
        assert action in PULL_ACTIONS

    def test_conflict_push_is_in_push_actions(self):
        db, env, base = self._setup()
        action = decide(db, env, base, _NEWER, _OLDER)
        action_name = action
        assert action_name in PUSH_ACTIONS


# ---------------------------------------------------------------------------
# 9. Full decision table — exhaustive cross-check
# ---------------------------------------------------------------------------

class TestDecideFullTable:
    """Walk the complete decision table from the plan's Section 'Decision table'."""

    def test_row_equal_hashes_noop(self):
        """db_hash == env_hash (both non-None) → NOOP."""
        c = "shared content"
        h = content_hash(c)
        assert decide(c, c, h, _OLDER, _NEWER) == ReconcileAction.NOOP

    def test_row_both_none_noop(self):
        """db None and env None → NOOP (both hash to None, equal)."""
        assert decide(None, None, None, None, None) == ReconcileAction.NOOP

    def test_row_base_none_db_content_env_content_different_seed_push(self):
        """base=None, db non-None, env non-None but different → SEED_PUSH."""
        assert decide("db v1", "env v1", None, None, None) == ReconcileAction.SEED_PUSH

    def test_row_base_none_db_none_env_content_seed_pull(self):
        """base=None, db=None, env has content → SEED_PULL."""
        assert decide(None, "env v1", None, None, None) == ReconcileAction.SEED_PULL

    def test_row_only_env_changed_pull(self):
        """base matches db_hash, env diverged → PULL."""
        orig = "v1"
        base = content_hash(orig)
        assert decide(orig, "v2", base, _OLDER, _NEWER) == ReconcileAction.PULL

    def test_row_only_db_changed_push(self):
        """base matches env_hash, db diverged → PUSH."""
        orig = "v1"
        base = content_hash(orig)
        assert decide("v2", orig, base, _NEWER, _OLDER) == ReconcileAction.PUSH

    def test_row_both_changed_env_ts_gt_db_ts_conflict_pull(self):
        """Both changed, env newer → CONFLICT_PULL."""
        orig = "v1"
        base = content_hash(orig)
        assert decide("db v2", "env v2", base, _OLDER, _NEWER) == ReconcileAction.CONFLICT_PULL

    def test_row_both_changed_db_ts_gt_env_ts_conflict_push(self):
        """Both changed, DB newer → CONFLICT_PUSH."""
        orig = "v1"
        base = content_hash(orig)
        assert decide("db v2", "env v2", base, _NEWER, _OLDER) == ReconcileAction.CONFLICT_PUSH

    def test_row_env_none_empty_db_content_push_restore(self):
        """env None/empty, db non-None → PUSH (env-file restore)."""
        base = content_hash("v1")
        assert decide("db v2", None, base, _OLDER, None) == ReconcileAction.PUSH
        assert decide("db v2", "", base, _OLDER, None) == ReconcileAction.PUSH
        assert decide("db v2", "   ", base, _OLDER, None) == ReconcileAction.PUSH

    def test_row_tie_favours_env_conflict_pull(self):
        """Tie (equal timestamps) → env wins (>= semantics) → CONFLICT_PULL."""
        orig = "v1"
        base = content_hash(orig)
        ts = _ts(0)
        assert decide("db v2", "env v2", base, ts, ts) == ReconcileAction.CONFLICT_PULL


# ---------------------------------------------------------------------------
# 10. PULL_ACTIONS / PUSH_ACTIONS membership sanity check
# ---------------------------------------------------------------------------

class TestActionSets:

    def test_pull_actions_membership(self):
        assert ReconcileAction.PULL in PULL_ACTIONS
        assert ReconcileAction.CONFLICT_PULL in PULL_ACTIONS
        assert ReconcileAction.SEED_PULL in PULL_ACTIONS

    def test_push_actions_membership(self):
        assert ReconcileAction.PUSH in PUSH_ACTIONS
        assert ReconcileAction.CONFLICT_PUSH in PUSH_ACTIONS
        assert ReconcileAction.SEED_PUSH in PUSH_ACTIONS

    def test_noop_in_neither_set(self):
        assert ReconcileAction.NOOP not in PULL_ACTIONS
        assert ReconcileAction.NOOP not in PUSH_ACTIONS

    def test_pull_and_push_sets_are_disjoint(self):
        assert PULL_ACTIONS.isdisjoint(PUSH_ACTIONS)
