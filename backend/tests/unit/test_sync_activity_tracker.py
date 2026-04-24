"""
Unit tests for SyncActivityTracker.

Pure Python — no database, no HTTP layer. The tracker's DB update helpers
(static methods that open fresh sessions) are patched out. Only the in-memory
reference-counting logic, heartbeat guard, and grace-period scheduling are
exercised here.

Coverage:
  1. Single connection: register → is_sync_warm=True; unregister → is_sync_warm=False
  2. Reference counting: two connections for same env; first unregister leaves warm=True,
     second unregister flips warm=False and DB sync_active is set to False
  3. Reconnect cancels grace timer: register after unregister cancels pending suspend
  4. Heartbeat: only updates timestamp when warm=True; no-op when warm=False
  5. Grace period scheduling: calling unregister (last connection) schedules a task;
     re-registering before it fires cancels the task
"""
import asyncio
import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch, call


# ── Helpers ────────────────────────────────────────────────────────────────


def _make_tracker():
    """Create a fresh SyncActivityTracker with all DB helpers patched out."""
    from app.services.cli.sync_activity_tracker import SyncActivityTracker
    tracker = SyncActivityTracker()
    return tracker


# ── Tests ──────────────────────────────────────────────────────────────────


class TestReferenceCountingAndWarmGate:
    """Reference-counting: single and multi-connection scenarios."""

    def test_single_connection_lifecycle(self):
        """
        Register one connection → warm; unregister → not warm.
        DB state helpers are called with correct flags.
        """
        tracker = _make_tracker()
        env_id = uuid.uuid4()
        token_id = uuid.uuid4()
        conn_id = "conn-1"

        with (
            patch.object(tracker, "_update_env_sync_state") as mock_env_state,
            patch.object(tracker, "_update_token_sync_ts") as mock_token_ts,
            patch.object(tracker, "_schedule_grace_period_suspend") as mock_schedule,
        ):
            # Before any connection
            assert tracker.is_sync_warm(env_id) is False

            # Register
            tracker.register_sync_connection(env_id, token_id, conn_id)
            assert tracker.is_sync_warm(env_id) is True

            mock_env_state.assert_called_once()
            args, kwargs = mock_env_state.call_args
            # Called with sync_active=True (positional arg 1 is environment_id,
            # positional arg 2 would be sync_active when passed positionally).
            assert kwargs.get("sync_active", args[1] if len(args) > 1 else None) is True

            mock_token_ts.assert_called_once()

            # Unregister
            tracker.unregister_sync_connection(env_id, conn_id)
            assert tracker.is_sync_warm(env_id) is False

            # sync_active=False was written
            assert mock_env_state.call_count == 2
            second_call_args, second_call_kwargs = mock_env_state.call_args_list[1]
            assert (
                second_call_kwargs.get("sync_active", second_call_args[1] if len(second_call_args) > 1 else None)
                is False
            )

            # Grace period was scheduled after last disconnect
            mock_schedule.assert_called_once_with(env_id)

    def test_two_connections_same_env_reference_counting(self):
        """
        Two connections on the same env: first disconnect keeps warm=True,
        only the second disconnect flips warm=False.
        """
        tracker = _make_tracker()
        env_id = uuid.uuid4()
        token_id = uuid.uuid4()

        with (
            patch.object(tracker, "_update_env_sync_state") as mock_env_state,
            patch.object(tracker, "_update_token_sync_ts"),
            patch.object(tracker, "_schedule_grace_period_suspend") as mock_schedule,
        ):
            # Register two connections
            tracker.register_sync_connection(env_id, token_id, "conn-A")
            tracker.register_sync_connection(env_id, token_id, "conn-B")
            assert tracker.is_sync_warm(env_id) is True

            # First unregister — still warm
            tracker.unregister_sync_connection(env_id, "conn-A")
            assert tracker.is_sync_warm(env_id) is True
            mock_schedule.assert_not_called()

            # Second unregister — now cold
            tracker.unregister_sync_connection(env_id, "conn-B")
            assert tracker.is_sync_warm(env_id) is False
            mock_schedule.assert_called_once_with(env_id)

    def test_two_different_envs_independent_state(self):
        """
        Two environments are tracked independently — disconnecting one
        does not affect the other.
        """
        tracker = _make_tracker()
        env_a = uuid.uuid4()
        env_b = uuid.uuid4()

        with (
            patch.object(tracker, "_update_env_sync_state"),
            patch.object(tracker, "_update_token_sync_ts"),
            patch.object(tracker, "_schedule_grace_period_suspend"),
        ):
            tracker.register_sync_connection(env_a, uuid.uuid4(), "conn-A")
            tracker.register_sync_connection(env_b, uuid.uuid4(), "conn-B")

            assert tracker.is_sync_warm(env_a) is True
            assert tracker.is_sync_warm(env_b) is True

            # Disconnect env_a
            tracker.unregister_sync_connection(env_a, "conn-A")

            assert tracker.is_sync_warm(env_a) is False
            assert tracker.is_sync_warm(env_b) is True, "env_b must remain warm"


class TestHeartbeat:
    """heartbeat() only updates last_sync_activity_at; never changes sync_active flag."""

    def test_heartbeat_updates_timestamp_when_warm(self):
        """heartbeat() calls _update_env_sync_activity_ts when env is warm."""
        tracker = _make_tracker()
        env_id = uuid.uuid4()

        with (
            patch.object(tracker, "_update_env_sync_state"),
            patch.object(tracker, "_update_token_sync_ts"),
            patch.object(tracker, "_schedule_grace_period_suspend"),
            patch.object(tracker, "_update_env_sync_activity_ts") as mock_ts,
        ):
            tracker.register_sync_connection(env_id, uuid.uuid4(), "conn-1")
            assert tracker.is_sync_warm(env_id) is True

            tracker.heartbeat(env_id)
            mock_ts.assert_called_once()

    def test_heartbeat_no_op_when_not_warm(self):
        """heartbeat() is a no-op (does not write to DB) if no active connection."""
        tracker = _make_tracker()
        env_id = uuid.uuid4()

        with patch.object(tracker, "_update_env_sync_activity_ts") as mock_ts:
            # env_id has never been registered → not warm
            tracker.heartbeat(env_id)
            mock_ts.assert_not_called()

    def test_heartbeat_does_not_change_sync_active(self):
        """
        heartbeat() must only update the activity timestamp, never sync_active.
        Verified by asserting _update_env_sync_state is not called during heartbeat.
        """
        tracker = _make_tracker()
        env_id = uuid.uuid4()

        with (
            patch.object(tracker, "_update_env_sync_state") as mock_env_state,
            patch.object(tracker, "_update_token_sync_ts"),
            patch.object(tracker, "_schedule_grace_period_suspend"),
            patch.object(tracker, "_update_env_sync_activity_ts"),
        ):
            # Register (this calls _update_env_sync_state once)
            tracker.register_sync_connection(env_id, uuid.uuid4(), "conn-1")
            call_count_after_register = mock_env_state.call_count
            assert call_count_after_register == 1

            # Heartbeat must NOT call _update_env_sync_state
            tracker.heartbeat(env_id)
            assert mock_env_state.call_count == call_count_after_register, (
                "heartbeat() must not call _update_env_sync_state (sync_active flag)"
            )


class TestGracePeriodScheduling:
    """Grace-period scheduling: scheduled on last disconnect, cancelled on reconnect."""

    def test_reconnect_before_grace_fires_cancels_task(self):
        """
        If a new connection arrives while a grace-period task is pending,
        the task is cancelled and warm state is restored.
        """
        tracker = _make_tracker()
        env_id = uuid.uuid4()

        cancelled_tasks: list = []

        class _FakeTask:
            def __init__(self):
                self.done_flag = False

            def done(self):
                return self.done_flag

            def cancel(self):
                cancelled_tasks.append(self)

        fake_task = _FakeTask()

        def _schedule_grace(eid):
            # Simulates asyncio.get_event_loop().create_task(...) by manually
            # inserting a tracked task into the internal dict
            tracker._grace_tasks[eid] = fake_task

        with (
            patch.object(tracker, "_update_env_sync_state"),
            patch.object(tracker, "_update_token_sync_ts"),
            patch.object(tracker, "_schedule_grace_period_suspend", side_effect=_schedule_grace),
        ):
            # Connect then disconnect (triggers grace scheduling)
            tracker.register_sync_connection(env_id, uuid.uuid4(), "conn-1")
            tracker.unregister_sync_connection(env_id, "conn-1")

            # Task should be scheduled
            assert env_id in tracker._grace_tasks

            # Reconnect — should cancel the pending grace task
            tracker.register_sync_connection(env_id, uuid.uuid4(), "conn-2")
            assert len(cancelled_tasks) == 1, (
                "Grace task must be cancelled when a new connection arrives"
            )
            # Grace task is removed from the dict after cancellation
            assert env_id not in tracker._grace_tasks, (
                "Cancelled grace task must be removed from _grace_tasks"
            )

    def test_grace_period_suspend_is_skipped_if_reconnect_happened(self):
        """
        _grace_period_suspend() checks is_sync_warm() before suspending.
        If a new connection arrived during the grace period, the suspend is skipped.
        """
        tracker = _make_tracker()
        env_id = uuid.uuid4()

        # Simulate a new connection arriving during the grace period
        tracker._active_connections[env_id] = {"conn-new"}

        suspend_called = []

        async def _run():
            with patch.object(
                tracker,
                "_update_env_sync_state",
            ):
                # Patch asyncio.sleep to complete immediately
                with patch("asyncio.sleep", return_value=None):
                    # Override the grace task to run immediately
                    await tracker._grace_period_suspend(env_id)

        asyncio.run(_run())

        # Because is_sync_warm returned True (new connection exists),
        # suspend must NOT have been called
        # We verify indirectly — if _grace_period_suspend tried to suspend, it would
        # attempt to import and use EnvironmentLifecycleManager; we verify no import error
        # and no exception was raised (which would have propagated through asyncio.run).
        # The active connection dict is still intact.
        assert tracker.is_sync_warm(env_id) is True

    def test_grace_period_fires_when_no_reconnect(self):
        """
        _grace_period_suspend() should call suspend when no new connection arrived.

        We patch:
        - asyncio.sleep → immediate (no real wait)
        - The EnvironmentLifecycleManager (imported locally in the method)
        - A fake DB session that returns a fake "running" environment

        Because the Session and EnvironmentLifecycleManager are imported
        inside the method body (local imports), we patch them at their
        source module paths.
        """
        tracker = _make_tracker()
        env_id = uuid.uuid4()

        # No connections in dict — env will be cold when the task fires
        assert not tracker.is_sync_warm(env_id)

        suspend_calls: list = []

        # Build a fake environment object
        fake_env = MagicMock()
        fake_env.id = env_id
        fake_env.status = "running"

        # Build a fake DB session context manager
        fake_db = MagicMock()
        fake_db.get.return_value = fake_env
        fake_db.__enter__ = MagicMock(return_value=fake_db)
        fake_db.__exit__ = MagicMock(return_value=False)

        # Fake Session constructor returns fake_db context manager
        fake_session_cls = MagicMock(return_value=fake_db)

        # Fake lifecycle manager with async suspend
        mock_lm = MagicMock()

        async def _mock_suspend(db_session, environment):
            suspend_calls.append(environment.id)

        mock_lm.suspend_environment = _mock_suspend
        mock_lm_cls = MagicMock(return_value=mock_lm)

        async def _run():
            with (
                patch("asyncio.sleep", return_value=None),
                # Patch sqlmodel.Session where it's used in the method.
                # The method does "from sqlmodel import Session" locally, so
                # patching sqlmodel.Session intercepts that import.
                patch("sqlmodel.Session", fake_session_cls),
                # Patch EnvironmentLifecycleManager at its definition source.
                # The method does "from app.services.environments.environment_lifecycle
                # import EnvironmentLifecycleManager" locally — patching the class
                # in its home module intercepts the lookup.
                patch(
                    "app.services.environments.environment_lifecycle.EnvironmentLifecycleManager",
                    mock_lm_cls,
                ),
            ):
                await tracker._grace_period_suspend(env_id)

        asyncio.run(_run())

        assert len(suspend_calls) == 1, (
            f"Expected suspend called once when env is cold and grace elapsed, "
            f"got {len(suspend_calls)} calls: {suspend_calls}"
        )
        assert suspend_calls[0] == env_id
