"""
Environment Console Activity Tracker.

Tracks active web-console WebSocket connections (interactive terminal + logs
follow) per environment so the suspension scheduler treats "a console is
attached" as a keep-warm signal — mirroring ``SyncActivityTracker`` for CLI
live-sync.

This is a single-instance in-memory tracker. Unlike the sync tracker it does
not own a dedicated ``*_active`` DB flag; instead it bumps the environment's
``last_activity_at`` on register and heartbeat so the inactivity-based
suspension check naturally holds off while a console is attached, and exposes
``is_console_warm`` as a hard skip gate.
"""
import logging
import time
import uuid
from collections import deque
from datetime import UTC, datetime

logger = logging.getLogger(__name__)

# Heartbeat interval for active console connections (seconds). The terminal
# tunnel calls ``heartbeat`` on this cadence to refresh ``last_activity_at``.
ENV_CONSOLE_HEARTBEAT_INTERVAL_SECONDS = 30


class ConsoleRateLimitError(Exception):
    """Raised when a user exceeds the per-user console open-rate cap."""


class EnvConsoleActivityTracker:
    """
    Tracks active environment-console WebSocket connections per environment,
    plus the per-user open-rate sliding window.

    Uses an in-memory reference counter (``env_id → set(connection_id)``) to
    determine whether any console is live. The suspension scheduler queries
    ``is_console_warm`` and skips suspension for warm environments.

    This is the module-level singleton (see bottom of file) — all per-process
    console state lives here, mirroring ``sync_activity_tracker``. ``reset()``
    clears it for test isolation.
    """

    def __init__(self) -> None:
        # env_id → set of connection_id strings
        self._active_connections: dict[uuid.UUID, set[str]] = {}
        # user_id → deque[open_timestamps] (monotonic) for the open-rate cap.
        # Plain dict (not defaultdict) so empty entries can be pruned and no
        # permanent keys accumulate across opens/tests.
        self._open_events: dict[uuid.UUID, deque[float]] = {}

    # ── Open-rate cap ────────────────────────────────────────────────────────

    def enforce_open_rate(self, user_id: uuid.UUID, limit: int, window: float) -> None:
        """Sliding-window per-user open-rate cap.

        Raises ``ConsoleRateLimitError`` when the user has opened ``limit`` or
        more consoles within ``window`` seconds. Prunes stale/empty deques so
        the backing dict does not leak entries.
        """
        now = time.monotonic()
        events = self._open_events.get(user_id)
        if events is None:
            events = deque()
        while events and now - events[0] > window:
            events.popleft()
        if len(events) >= limit:
            if events:
                self._open_events[user_id] = events
            else:
                self._open_events.pop(user_id, None)
            raise ConsoleRateLimitError(
                f"Too many console opens — limit is {limit} per {int(window)}s"
            )
        events.append(now)
        self._open_events[user_id] = events

    # ── Public API ───────────────────────────────────────────────────────────

    def register_connection(
        self,
        environment_id: uuid.UUID,
        connection_id: str,
    ) -> None:
        """Register a new console WebSocket connection and mark env activity."""
        self._active_connections.setdefault(environment_id, set()).add(connection_id)
        self._update_env_activity(environment_id)
        logger.info(
            f"EnvConsoleActivityTracker: connection {connection_id} registered for env {environment_id}. "
            f"Active consoles: {len(self._active_connections[environment_id])}"
        )

    def unregister_connection(
        self,
        environment_id: uuid.UUID,
        connection_id: str,
    ) -> None:
        """Unregister a console WebSocket connection."""
        env_connections = self._active_connections.get(environment_id, set())
        env_connections.discard(connection_id)
        if env_connections:
            self._active_connections[environment_id] = env_connections
        else:
            self._active_connections.pop(environment_id, None)

        # Refresh activity so the normal inactivity grace period starts from the
        # moment the last console detached rather than from the last keystroke.
        self._update_env_activity(environment_id)

        remaining = len(self._active_connections.get(environment_id, set()))
        logger.info(
            f"EnvConsoleActivityTracker: connection {connection_id} unregistered for env {environment_id}. "
            f"Remaining consoles: {remaining}"
        )

    def heartbeat(self, environment_id: uuid.UUID) -> None:
        """Refresh ``last_activity_at`` for an environment with a live console."""
        if not self.is_console_warm(environment_id):
            return
        self._update_env_activity(environment_id)

    def is_console_warm(self, environment_id: uuid.UUID) -> bool:
        """Return True if the environment has at least one attached console.

        Used by the suspension scheduler as a skip gate — envs with an attached
        console are never auto-suspended regardless of the inactivity threshold.
        """
        return bool(self._active_connections.get(environment_id))

    def count_for_env(self, environment_id: uuid.UUID) -> int:
        """Number of consoles currently attached to one environment."""
        return len(self._active_connections.get(environment_id, set()))

    def count_for_user(self, environment_ids: set[uuid.UUID]) -> int:
        """Number of consoles attached across a set of the user's environments."""
        return sum(
            len(self._active_connections.get(env_id, set())) for env_id in environment_ids
        )

    def attached_env_ids(self) -> set[uuid.UUID]:
        """Env-ids that currently have ≥1 attached console.

        Used by the concurrency cap to bound the per-user check to the (usually
        tiny) set of envs that actually have consoles, so ownership resolution
        only needs those ids — no full owned-env scan.
        """
        return {env_id for env_id, conns in self._active_connections.items() if conns}

    def reset(self) -> None:
        """Clear all in-memory console state (connections + open-rate windows).

        Test isolation hook — production code never calls this.
        """
        self._active_connections.clear()
        self._open_events.clear()

    # ── Internal helpers ─────────────────────────────────────────────────────

    @staticmethod
    def _update_env_activity(environment_id: uuid.UUID) -> None:
        """Bump ``last_activity_at`` on the environment in a fresh session.

        Opens its own short-lived ``Session(engine)`` so it is safe to call from
        a long-lived WebSocket context without holding the request-scoped session.
        """
        from sqlmodel import Session

        from app.core.db import engine
        from app.models.environments.environment import AgentEnvironment

        try:
            with Session(engine) as fresh_db:
                env = fresh_db.get(AgentEnvironment, environment_id)
                if not env:
                    logger.warning(
                        f"EnvConsoleActivityTracker: environment {environment_id} not found in DB"
                    )
                    return
                env.last_activity_at = datetime.now(UTC)
                fresh_db.add(env)
                fresh_db.commit()
        except Exception as e:
            logger.error(
                f"EnvConsoleActivityTracker: failed to update activity for env {environment_id}: {e}"
            )


# Module-level singleton used by the console service and suspension scheduler
env_console_activity_tracker = EnvConsoleActivityTracker()
