"""
Unit tests for ``EnvConsoleActivityTracker`` — the in-memory open-rate and
concurrency tracker behind the env-console WebSocket caps.

Pure in-memory logic with a fresh tracker instance (the one DB call,
``_update_env_activity``, is patched out). The WebSocket close-code behavior and
the suspension-scheduler gate that consume this tracker live in
``tests/api/agent_environments/test_env_console.py``.
"""
import uuid
from unittest.mock import patch

import pytest


def test_open_rate_cap_enforced_and_resets() -> None:
    """
    EnvConsoleActivityTracker.enforce_open_rate:
      1. Allows opens up to the limit within the window.
      2. Raises ConsoleRateLimitError on the (limit+1)th open.
      3. After reset(), the window is clear and opens are allowed again.
      4. A different user is unaffected by the first user's window.
    """
    from app.services.environments.env_console_activity_tracker import (
        ConsoleRateLimitError,
        EnvConsoleActivityTracker,
    )

    tracker = EnvConsoleActivityTracker()
    user_id = uuid.uuid4()

    # ── Phase 1: Allow opens up to limit=3 ───────────────────────────────
    for _ in range(3):
        tracker.enforce_open_rate(user_id, limit=3, window=60.0)

    # ── Phase 2: 4th open raises ConsoleRateLimitError ───────────────────
    with pytest.raises(ConsoleRateLimitError):
        tracker.enforce_open_rate(user_id, limit=3, window=60.0)

    # ── Phase 3: reset() clears state; opens are allowed again ───────────
    tracker.reset()
    tracker.enforce_open_rate(user_id, limit=3, window=60.0)  # should not raise

    # ── Phase 4: A different user is unaffected by the first user's window ─
    other_user = uuid.uuid4()
    tracker.reset()
    for _ in range(3):
        tracker.enforce_open_rate(user_id, limit=3, window=60.0)
    # other_user has no events yet — should not raise
    for _ in range(3):
        tracker.enforce_open_rate(other_user, limit=3, window=60.0)

    tracker.reset()


def test_concurrency_cap_and_tracker_invariants() -> None:
    """
    EnvConsoleActivityTracker register/unregister/count/is_console_warm:
      1. Freshly reset tracker has no connections.
      2. register_connection increments count_for_env and is_console_warm=True.
      3. Multiple connections to same env are all tracked.
      4. unregister_connection decrements; last unregister → is_console_warm=False.
      5. count_for_user aggregates across multiple env ids.
      6. attached_env_ids returns only envs with ≥1 connection.
      7. reset() clears everything.
    """
    from app.services.environments.env_console_activity_tracker import (
        EnvConsoleActivityTracker,
    )

    tracker = EnvConsoleActivityTracker()

    # Prevent the _update_env_activity DB call (no DB in a unit test).
    # Patch the staticmethod on the class — side_effect=None means it does nothing.
    with patch.object(
        EnvConsoleActivityTracker, "_update_env_activity", return_value=None
    ):
        env_a = uuid.uuid4()
        env_b = uuid.uuid4()
        conn1 = "conn-1"
        conn2 = "conn-2"
        conn3 = "conn-3"

        # ── Phase 1: Fresh tracker ────────────────────────────────────────
        assert tracker.count_for_env(env_a) == 0
        assert tracker.is_console_warm(env_a) is False
        assert tracker.attached_env_ids() == set()

        # ── Phase 2: Register one connection ─────────────────────────────
        tracker.register_connection(env_a, conn1)
        assert tracker.count_for_env(env_a) == 1
        assert tracker.is_console_warm(env_a) is True

        # ── Phase 3: Register two more to the same env ────────────────────
        tracker.register_connection(env_a, conn2)
        tracker.register_connection(env_a, conn3)
        assert tracker.count_for_env(env_a) == 3

        # ── Phase 4: Unregister each; last one clears warm state ──────────
        tracker.unregister_connection(env_a, conn1)
        assert tracker.count_for_env(env_a) == 2
        assert tracker.is_console_warm(env_a) is True

        tracker.unregister_connection(env_a, conn2)
        tracker.unregister_connection(env_a, conn3)
        assert tracker.count_for_env(env_a) == 0
        assert tracker.is_console_warm(env_a) is False

        # ── Phase 5: count_for_user across two envs ───────────────────────
        tracker.register_connection(env_a, conn1)
        tracker.register_connection(env_b, conn2)
        assert tracker.count_for_user({env_a, env_b}) == 2
        assert tracker.count_for_user({env_a}) == 1
        assert tracker.count_for_user({env_b}) == 1

        # ── Phase 6: attached_env_ids ─────────────────────────────────────
        ids = tracker.attached_env_ids()
        assert env_a in ids
        assert env_b in ids
        assert len(ids) == 2

        # ── Phase 7: reset clears everything ─────────────────────────────
        tracker.reset()
        assert tracker.count_for_env(env_a) == 0
        assert tracker.count_for_env(env_b) == 0
        assert tracker.attached_env_ids() == set()
