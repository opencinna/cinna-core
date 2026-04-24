"""
Sync Activity Tracker.

Tracks active CLI sync WebSocket connections per environment.
Manages sync_active flag, last_sync_activity_at heartbeat, and
schedules grace-period suspension after the last WS disconnects.

This is a single-instance in-memory tracker. For multi-instance deployments,
the DB sync_active flag acts as the fallback source of truth for the
suspension scheduler; the in-memory reference count handles sub-second
connect/disconnect sequencing within a single process.
"""
import asyncio
import logging
import uuid
from datetime import UTC, datetime

logger = logging.getLogger(__name__)

# Grace period before auto-suspend after the last sync WS disconnects (seconds)
SYNC_GRACE_PERIOD_SECONDS = 300  # 5 minutes

# Heartbeat interval for active sync connections (seconds)
SYNC_HEARTBEAT_INTERVAL_SECONDS = 30


class SyncActivityTracker:
    """
    Tracks active CLI sync WebSocket connections per environment.

    Uses an in-memory reference counter (env_id → set of connection_ids)
    to determine if any sync connections are live. Updates DB fields
    (sync_active, last_sync_activity_at, last_sync_connected_at) to persist
    state across process restarts and for the suspension scheduler.
    """

    def __init__(self) -> None:
        # env_id → set of connection_id strings
        self._active_connections: dict[uuid.UUID, set[str]] = {}
        # env_id → pending grace-period suspend task
        self._grace_tasks: dict[uuid.UUID, asyncio.Task] = {}

    # ── Public API ───────────────────────────────────────────────────────────

    def register_sync_connection(
        self,
        environment_id: uuid.UUID,
        token_id: uuid.UUID,
        connection_id: str,
    ) -> None:
        """
        Register a new sync WebSocket connection.

        Sets sync_active=True on the environment, updates last_sync_activity_at
        and last_sync_connected_at on the token. Cancels any pending grace-period
        suspend task for this environment.

        DB writes open their own short-lived ``Session(engine)`` so this method
        is safe to call from a long-lived WebSocket context without holding the
        request-scoped session.
        """
        if environment_id not in self._active_connections:
            self._active_connections[environment_id] = set()
        self._active_connections[environment_id].add(connection_id)

        # Cancel any pending grace-period suspend
        self._cancel_grace_task(environment_id)

        now = datetime.now(UTC)
        self._update_env_sync_state(environment_id, sync_active=True, activity_at=now)
        self._update_token_sync_ts(token_id, last_sync_connected_at=now)

        logger.info(
            f"SyncActivityTracker: connection {connection_id} registered for env {environment_id}. "
            f"Active connections: {len(self._active_connections[environment_id])}"
        )

    def unregister_sync_connection(
        self,
        environment_id: uuid.UUID,
        connection_id: str,
    ) -> None:
        """
        Unregister a sync WebSocket connection.

        If this was the last connection for the environment, sets sync_active=False
        and schedules a grace-period suspend. If other connections remain, only
        removes the connection from the reference counter.
        """
        env_connections = self._active_connections.get(environment_id, set())
        env_connections.discard(connection_id)
        if env_connections:
            self._active_connections[environment_id] = env_connections
        else:
            self._active_connections.pop(environment_id, None)

        remaining = len(self._active_connections.get(environment_id, set()))
        logger.info(
            f"SyncActivityTracker: connection {connection_id} unregistered for env {environment_id}. "
            f"Remaining connections: {remaining}"
        )

        if remaining == 0:
            # Last connection gone — clear sync_active and schedule grace-period suspend
            self._update_env_sync_state(environment_id, sync_active=False)
            self._schedule_grace_period_suspend(environment_id)

    def heartbeat(self, environment_id: uuid.UUID) -> None:
        """
        Update last_sync_activity_at for an environment with an active sync session.

        Called periodically (every SYNC_HEARTBEAT_INTERVAL_SECONDS) by the
        sync-stream WebSocket handler coroutine. Only updates the activity timestamp —
        sync_active flag transitions are handled solely by register/unregister.
        """
        if not self.is_sync_warm(environment_id):
            return
        self._update_env_sync_activity_ts(environment_id, activity_at=datetime.now(UTC))

    def is_sync_warm(self, environment_id: uuid.UUID) -> bool:
        """
        Return True if the environment has at least one active sync connection.

        Used by the suspension scheduler as a skip gate — envs with active sync
        are not auto-suspended regardless of inactivity threshold.
        """
        return bool(self._active_connections.get(environment_id))

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _cancel_grace_task(self, environment_id: uuid.UUID) -> None:
        """Cancel any pending grace-period suspend task for an environment."""
        task = self._grace_tasks.pop(environment_id, None)
        if task and not task.done():
            task.cancel()
            logger.debug(f"SyncActivityTracker: cancelled grace-period task for env {environment_id}")

    def _schedule_grace_period_suspend(self, environment_id: uuid.UUID) -> None:
        """Schedule a grace-period check that may suspend the environment."""
        self._cancel_grace_task(environment_id)
        try:
            loop = asyncio.get_event_loop()
            task = loop.create_task(self._grace_period_suspend(environment_id))
            self._grace_tasks[environment_id] = task
            logger.info(
                f"SyncActivityTracker: grace-period suspend scheduled for env {environment_id} "
                f"in {SYNC_GRACE_PERIOD_SECONDS}s"
            )
        except RuntimeError:
            # No running event loop (e.g., in tests) — skip scheduling
            logger.debug(
                f"SyncActivityTracker: no event loop available, skipping grace-period scheduling "
                f"for env {environment_id}"
            )

    async def _grace_period_suspend(self, environment_id: uuid.UUID) -> None:
        """
        Wait for the grace period, then suspend the environment if still idle.

        Re-checks is_sync_warm before suspending — if a new connection arrived
        during the grace period, the suspend is skipped.
        """
        try:
            await asyncio.sleep(SYNC_GRACE_PERIOD_SECONDS)
        except asyncio.CancelledError:
            logger.debug(f"SyncActivityTracker: grace-period task cancelled for env {environment_id}")
            return

        if self.is_sync_warm(environment_id):
            logger.debug(
                f"SyncActivityTracker: new sync connection arrived during grace period for env {environment_id}, "
                "skipping suspend"
            )
            return

        logger.info(
            f"SyncActivityTracker: grace period elapsed for env {environment_id}, suspending"
        )
        self._grace_tasks.pop(environment_id, None)

        # Suspend the environment directly rather than waiting for the scheduler's
        # next tick. A fresh Session is required since we're in an async background task.
        try:
            from app.core.db import engine
            from app.models.environments.environment import AgentEnvironment
            from app.services.environments.environment_lifecycle import EnvironmentLifecycleManager
            from sqlmodel import Session

            with Session(engine) as db:
                env = db.get(AgentEnvironment, environment_id)
                if not env:
                    logger.warning(
                        f"SyncActivityTracker: env {environment_id} not found, skipping suspend"
                    )
                    return
                if env.status not in ("running",):
                    logger.debug(
                        f"SyncActivityTracker: env {environment_id} is '{env.status}', "
                        "skipping suspend"
                    )
                    return
                lifecycle_manager = EnvironmentLifecycleManager()
                await lifecycle_manager.suspend_environment(db_session=db, environment=env)
                logger.info(
                    f"SyncActivityTracker: env {environment_id} suspended after sync grace period"
                )
        except Exception as e:
            logger.error(
                f"SyncActivityTracker: failed to suspend env {environment_id} after grace period: {e}",
                exc_info=True,
            )

    @staticmethod
    def _update_env_sync_state(
        environment_id: uuid.UUID,
        sync_active: bool,
        activity_at: datetime | None = None,
    ) -> None:
        """Update sync_active and optionally last_sync_activity_at on the environment."""
        from app.core.db import engine
        from app.models.environments.environment import AgentEnvironment
        from sqlmodel import Session

        try:
            with Session(engine) as fresh_db:
                env = fresh_db.get(AgentEnvironment, environment_id)
                if not env:
                    logger.warning(f"SyncActivityTracker: environment {environment_id} not found in DB")
                    return
                env.sync_active = sync_active
                if activity_at is not None:
                    env.last_sync_activity_at = activity_at
                fresh_db.add(env)
                fresh_db.commit()
        except Exception as e:
            logger.error(f"SyncActivityTracker: failed to update env {environment_id} sync state: {e}")

    @staticmethod
    def _update_env_sync_activity_ts(
        environment_id: uuid.UUID,
        activity_at: datetime,
    ) -> None:
        """Update only last_sync_activity_at (not sync_active) on the environment."""
        from app.core.db import engine
        from app.models.environments.environment import AgentEnvironment
        from sqlmodel import Session

        try:
            with Session(engine) as fresh_db:
                env = fresh_db.get(AgentEnvironment, environment_id)
                if not env:
                    logger.warning(f"SyncActivityTracker: environment {environment_id} not found in DB")
                    return
                env.last_sync_activity_at = activity_at
                fresh_db.add(env)
                fresh_db.commit()
        except Exception as e:
            logger.error(
                f"SyncActivityTracker: failed to update last_sync_activity_at for env {environment_id}: {e}"
            )

    @staticmethod
    def _update_token_sync_ts(
        token_id: uuid.UUID,
        last_sync_connected_at: datetime,
    ) -> None:
        """Update last_sync_connected_at on the CLI token."""
        from app.core.db import engine
        from app.models.cli.cli_token import CLIToken
        from sqlmodel import Session

        try:
            with Session(engine) as fresh_db:
                token = fresh_db.get(CLIToken, token_id)
                if not token:
                    logger.warning(f"SyncActivityTracker: CLI token {token_id} not found in DB")
                    return
                token.last_sync_connected_at = last_sync_connected_at
                fresh_db.add(token)
                fresh_db.commit()
        except Exception as e:
            logger.error(f"SyncActivityTracker: failed to update token {token_id} last_sync_connected_at: {e}")


# Module-level singleton used by routes
sync_activity_tracker = SyncActivityTracker()
