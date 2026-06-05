"""
Environment Console Service.

Owns the full lifecycle of the two operator-facing environment consoles:

- **Web terminal** — a backend WS proxy from the browser to the env-core
  ``/shell/pty`` endpoint (interactive PTY shell inside the container).
- **Logs follow** — a backend WS endpoint that streams host-side
  ``docker logs -f`` output (read from the Docker adapter) to the browser.

The terminal path is explicitly modeled on ``CLIService.run_sync_tunnel``:
ensure-running guard → accept WS → register the keep-warm tracker → open the
env-core WS → bidirectional pump + heartbeat → teardown. The logs path reuses
the same teardown ergonomics but reads from the Docker adapter rather than
env-core (the container cannot see its own Docker log stream).

Routes stay thin and delegate the whole lifecycle here.
"""
import asyncio
import json
import logging
import time
import uuid
from typing import TYPE_CHECKING

from sqlmodel import Session, select
from starlette.websockets import WebSocketState

from app.core.config import settings
from app.core.db import engine
from app.models import AgentEnvironment, User
from app.models.events.security_event import (
    AGENT_ENV_TERMINAL_CLOSED,
    AGENT_ENV_TERMINAL_OPENED,
    SecurityEventCreate,
)
from app.services.environments.env_console_activity_tracker import (
    ENV_CONSOLE_HEARTBEAT_INTERVAL_SECONDS,
    ConsoleRateLimitError,
    env_console_activity_tracker,
)
# Import as a module so test patches of the connector instance are visible here.
from app.services.environments import agent_env_connector as _aec_module

if TYPE_CHECKING:
    from fastapi import WebSocket

logger = logging.getLogger(__name__)


# WS close codes (4000–4999 are application-defined).
WS_CLOSE_ENV_NOT_RUNNING = 4404
WS_CLOSE_CONCURRENCY_EXCEEDED = 4429
WS_CLOSE_INTERNAL = 1011

# Re-export so callers can ``except ConsoleRateLimitError`` from this module.
__all__ = ["EnvironmentConsoleService", "ConsoleRateLimitError", "ConsoleConcurrencyError"]


class ConsoleConcurrencyError(Exception):
    """Raised when per-env or per-user console concurrency caps are exceeded."""


class EnvironmentConsoleService:
    """Lifecycle owner for the web terminal and logs-follow consoles.

    All per-process state (active connections + the per-user open-rate window)
    lives on the ``env_console_activity_tracker`` module-level singleton — this
    service is otherwise stateless, mirroring ``CLIService`` over
    ``sync_activity_tracker``.
    """

    # ── Guards ───────────────────────────────────────────────────────────────

    @staticmethod
    def _is_running(environment: AgentEnvironment) -> bool:
        return environment.status == "running"

    @staticmethod
    def _enforce_open_rate(user_id: uuid.UUID) -> None:
        """Sliding-window per-user open-rate cap. Raises ConsoleRateLimitError."""
        env_console_activity_tracker.enforce_open_rate(
            user_id,
            limit=settings.ENV_CONSOLE_OPEN_RATE_LIMIT,
            window=settings.ENV_CONSOLE_OPEN_RATE_WINDOW_SECONDS,
        )

    @staticmethod
    def _owned_attached_env_ids(user_id: uuid.UUID) -> set[uuid.UUID]:
        """Of the envs that currently have an attached console, the subset the
        user owns.

        Bounds the ownership query to the (tiny) set of envs that actually have
        consoles — intersect the tracker's attached set with ownership — rather
        than scanning every environment the user owns on each open.
        """
        attached = env_console_activity_tracker.attached_env_ids()
        if not attached:
            return set()
        from app.models import Agent

        with Session(engine) as db:
            stmt = (
                select(AgentEnvironment.id)
                .join(Agent, Agent.id == AgentEnvironment.agent_id)
                .where(
                    Agent.owner_id == user_id,
                    AgentEnvironment.id.in_(attached),  # type: ignore[attr-defined]
                )
            )
            return set(db.exec(stmt).all())

    @classmethod
    def _check_concurrency_caps(
        cls, environment: AgentEnvironment, user: User
    ) -> None:
        """Raise ConsoleConcurrencyError if the per-env or per-user cap is hit.

        Called twice per open: once before ``register_connection`` (fast reject)
        and once immediately after (TOCTOU close: N simultaneous opens that all
        passed the pre-check are re-validated post-register, and the loser bails
        out). The ``>`` vs ``>=`` distinction below makes the post-register check
        count *this* connection: per-env over-limit is ``> MAX`` once registered.
        """
        per_env = env_console_activity_tracker.count_for_env(environment.id)
        if per_env > settings.ENV_CONSOLE_MAX_PER_ENV:
            raise ConsoleConcurrencyError(
                f"Environment already has the maximum "
                f"{settings.ENV_CONSOLE_MAX_PER_ENV} consoles"
            )
        per_user = env_console_activity_tracker.count_for_user(
            cls._owned_attached_env_ids(user.id)
        )
        if per_user > settings.ENV_CONSOLE_MAX_PER_USER:
            raise ConsoleConcurrencyError(
                f"You already have the maximum "
                f"{settings.ENV_CONSOLE_MAX_PER_USER} consoles open"
            )

    @classmethod
    def _enforce_concurrency_pre(
        cls, environment: AgentEnvironment, user: User
    ) -> None:
        """Pre-register concurrency check (fast reject before accept).

        Uses ``>=`` semantics so an already-full env/user is rejected before we
        register and accept the socket.
        """
        per_env = env_console_activity_tracker.count_for_env(environment.id)
        if per_env >= settings.ENV_CONSOLE_MAX_PER_ENV:
            raise ConsoleConcurrencyError(
                f"Environment already has {per_env} consoles "
                f"(max {settings.ENV_CONSOLE_MAX_PER_ENV})"
            )
        per_user = env_console_activity_tracker.count_for_user(
            cls._owned_attached_env_ids(user.id)
        )
        if per_user >= settings.ENV_CONSOLE_MAX_PER_USER:
            raise ConsoleConcurrencyError(
                f"You already have {per_user} consoles open "
                f"(max {settings.ENV_CONSOLE_MAX_PER_USER})"
            )

    # ── Terminal tunnel ──────────────────────────────────────────────────────

    @classmethod
    async def run_terminal_tunnel(
        cls,
        websocket: "WebSocket",
        environment: AgentEnvironment,
        agent_id: uuid.UUID,
        user: User,
        raw_token: str,
        source_ip: str | None,
    ) -> None:
        """
        Full lifecycle of an interactive web-terminal WebSocket tunnel.

        Mirrors ``CLIService.run_sync_tunnel``:

        1. Status guard — reject (close 4404) unless the env is running.
        2. Open-rate + concurrency caps — reject (close 4429) when exceeded.
        3. Accept the browser WS, register the keep-warm tracker, then re-check
           the concurrency cap (TOCTOU close) and audit OPENED.
        4. Open a second WS to env-core ``/shell/pty`` (optional resize preamble).
        5. Pump bytes + resize control bidirectionally, plus a heartbeat that
           refreshes activity and re-validates the platform token (expiry +
           desktop revocation) each tick — an expired/revoked token tears the
           full-shell socket down (mirrors ``run_sync_tunnel``'s per-tick
           revocation check). The heartbeat uses its own fresh ``Session``.
        6. Teardown (either side disconnects): cancel tasks, close env WS,
           unregister tracker, audit CLOSED, close the browser WS.
        """
        # 1. Status guard (before accept)
        if not cls._is_running(environment):
            await websocket.close(code=WS_CLOSE_ENV_NOT_RUNNING, reason="Environment is not running")
            return

        # 2. Rate + concurrency caps (before accept)
        try:
            cls._enforce_open_rate(user.id)
            cls._enforce_concurrency_pre(environment, user)
        except ConsoleRateLimitError as e:
            await websocket.close(code=WS_CLOSE_CONCURRENCY_EXCEEDED, reason=str(e))
            return
        except ConsoleConcurrencyError as e:
            await websocket.close(code=WS_CLOSE_CONCURRENCY_EXCEEDED, reason=str(e))
            return

        # 3. Accept + register tracker, then re-check the cap atomically-ish.
        await websocket.accept()
        connection_id = str(uuid.uuid4())
        env_console_activity_tracker.register_connection(environment.id, connection_id)

        # TOCTOU close: N simultaneous opens can all pass the pre-check; after
        # registering, re-validate counting this connection and bail if over.
        try:
            cls._check_concurrency_caps(environment, user)
        except ConsoleConcurrencyError as e:
            env_console_activity_tracker.unregister_connection(environment.id, connection_id)
            await websocket.close(code=WS_CLOSE_CONCURRENCY_EXCEEDED, reason=str(e))
            return

        started_at = time.monotonic()
        exit_reason = "client_disconnect"
        await cls._audit_async(
            user_id=user.id,
            environment=environment,
            agent_id=agent_id,
            event_type=AGENT_ENV_TERMINAL_OPENED,
            details={
                "env_id": str(environment.id),
                "agent_id": str(agent_id),
                "user_id": str(user.id),
                "source_ip": source_ip,
            },
        )

        # 4. Open env-core WS
        from app.services.sessions.message_service import MessageService

        base_url = MessageService.get_environment_url(environment)
        auth_headers = MessageService.get_auth_headers(environment)

        try:
            env_ws = await _aec_module.agent_env_connector.open_shell_websocket(
                base_url,
                auth_headers,
                preamble={"cols": 80, "rows": 24, "shell": "bash"},
            )
        except RuntimeError as e:
            logger.error(f"terminal: cannot reach env-core for env {environment.id}: {e}")
            env_console_activity_tracker.unregister_connection(environment.id, connection_id)
            await cls._audit_async(
                user_id=user.id,
                environment=environment,
                agent_id=agent_id,
                event_type=AGENT_ENV_TERMINAL_CLOSED,
                details={
                    "env_id": str(environment.id),
                    "agent_id": str(agent_id),
                    "user_id": str(user.id),
                    "source_ip": source_ip,
                    "duration_seconds": 0,
                    "exit_reason": "env_unreachable",
                },
            )
            await websocket.close(code=WS_CLOSE_INTERNAL, reason="Cannot reach environment shell")
            return

        # 5. Bidirectional pump + heartbeat
        async def client_to_env() -> None:
            try:
                while True:
                    message = await websocket.receive()
                    if message.get("type") == "websocket.disconnect":
                        break
                    if message.get("bytes") is not None:
                        await env_ws.send(message["bytes"])
                    elif message.get("text") is not None:
                        # Resize control + any other text frames forwarded verbatim.
                        await env_ws.send(message["text"])
            except Exception as e:
                logger.debug(f"terminal client→env pump ended: {e}")

        async def env_to_client() -> None:
            try:
                while True:
                    data = await env_ws.recv()
                    if isinstance(data, str):
                        await websocket.send_text(data)
                    else:
                        await websocket.send_bytes(data)
            except Exception as e:
                logger.debug(f"terminal env→client pump ended: {e}")

        async def heartbeat_loop() -> None:
            nonlocal exit_reason
            try:
                while True:
                    await asyncio.sleep(ENV_CONSOLE_HEARTBEAT_INTERVAL_SECONDS)
                    # Re-validate the platform token each tick (expiry + desktop
                    # revocation + active user) with a fresh session — an
                    # expired/revoked token must not keep the full-shell socket
                    # open. Mirrors run_sync_tunnel's per-tick revocation check.
                    if not cls._token_still_valid(raw_token):
                        logger.info(
                            f"terminal: token invalid/expired mid-session for user {user.id}, closing"
                        )
                        exit_reason = "token_expired"
                        return
                    env_console_activity_tracker.heartbeat(environment.id)
            except asyncio.CancelledError:
                pass

        await cls._pump_bidirectional(
            websocket=websocket,
            env_ws=env_ws,
            client_to_env=client_to_env,
            env_to_client=env_to_client,
            heartbeat=heartbeat_loop,
        )

        # 6. Teardown bookkeeping
        env_console_activity_tracker.unregister_connection(environment.id, connection_id)
        await cls._audit_async(
            user_id=user.id,
            environment=environment,
            agent_id=agent_id,
            event_type=AGENT_ENV_TERMINAL_CLOSED,
            details={
                "env_id": str(environment.id),
                "agent_id": str(agent_id),
                "user_id": str(user.id),
                "source_ip": source_ip,
                "duration_seconds": round(time.monotonic() - started_at, 1),
                "exit_reason": exit_reason,
            },
        )
        logger.info(
            f"terminal: session closed for env {environment.id}, user {user.id} ({exit_reason})"
        )

    # ── Logs follow ──────────────────────────────────────────────────────────

    @classmethod
    async def follow_logs(
        cls,
        websocket: "WebSocket",
        environment: AgentEnvironment,
        user: User,
        raw_token: str,
        tail: int,
    ) -> None:
        """
        Stream host-side container logs to the browser over a WebSocket.

        1. Status guard — reject (close 4404) unless the env is running.
        2. Accept WS, register the keep-warm tracker (re-check the cap after).
        3. Send a recent tail snapshot, then forward live ``docker logs -f`` lines
           (read off the event loop via the fixed Docker adapter).
        4. Accept text control frames ({"type":"set_tail"} / {"type":"pause"}) —
           parsed but currently advisory (not yet implemented; see control_reader).
        5. A heartbeat re-validates the platform token each tick (expiry /
           revocation) so a stale token cannot keep the stream open.
        6. Teardown on client disconnect / container exit / token expiry (emit a
           final ``{"type":"closed"}`` control line).
        """
        if not cls._is_running(environment):
            await websocket.close(code=WS_CLOSE_ENV_NOT_RUNNING, reason="Environment is not running")
            return

        try:
            cls._enforce_open_rate(user.id)
            cls._enforce_concurrency_pre(environment, user)
        except (ConsoleRateLimitError, ConsoleConcurrencyError) as e:
            await websocket.close(code=WS_CLOSE_CONCURRENCY_EXCEEDED, reason=str(e))
            return

        tail = max(1, min(tail, settings.ENV_CONSOLE_LOGS_TAIL_MAX))

        await websocket.accept()
        connection_id = str(uuid.uuid4())
        env_console_activity_tracker.register_connection(environment.id, connection_id)

        # TOCTOU close (same as the terminal path).
        try:
            cls._check_concurrency_caps(environment, user)
        except ConsoleConcurrencyError as e:
            env_console_activity_tracker.unregister_connection(environment.id, connection_id)
            await websocket.close(code=WS_CLOSE_CONCURRENCY_EXCEEDED, reason=str(e))
            return

        from app.services.environments.environment_service import EnvironmentService

        lifecycle = EnvironmentService.get_lifecycle_manager()
        adapter = lifecycle.get_adapter(environment)

        async def control_reader() -> None:
            """Drain client→server text frames until disconnect.

            NOTE: the ``{"type":"set_tail"}`` / ``{"type":"pause"}`` control
            frames are NOT yet implemented — they are intentionally drained and
            ignored here (advisory only) so this does not read as wired. Live
            retail / pause is a future enhancement.
            """
            try:
                while True:
                    message = await websocket.receive()
                    if message.get("type") == "websocket.disconnect":
                        break
            except Exception as e:
                logger.debug(f"logs control reader ended: {e}")

        async def log_forwarder() -> None:
            try:
                stream = await adapter.get_logs(lines=tail, follow=True)
                async for line in stream:
                    await websocket.send_text(line)
            except Exception as e:
                logger.debug(f"logs forwarder ended: {e}")

        async def heartbeat_loop() -> None:
            """Re-validate the platform token each tick + refresh keep-warm."""
            try:
                while True:
                    await asyncio.sleep(ENV_CONSOLE_HEARTBEAT_INTERVAL_SECONDS)
                    if not cls._token_still_valid(raw_token):
                        logger.info(
                            f"logs: token invalid/expired mid-session for user {user.id}, closing"
                        )
                        return
                    env_console_activity_tracker.heartbeat(environment.id)
            except asyncio.CancelledError:
                pass

        control_task = asyncio.create_task(control_reader())
        forward_task = asyncio.create_task(log_forwarder())
        heartbeat_task = asyncio.create_task(heartbeat_loop())

        try:
            await asyncio.wait(
                [control_task, forward_task, heartbeat_task],
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for task in (control_task, forward_task, heartbeat_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(
                control_task, forward_task, heartbeat_task, return_exceptions=True
            )

            env_console_activity_tracker.unregister_connection(environment.id, connection_id)

            try:
                if websocket.client_state != WebSocketState.DISCONNECTED:
                    await websocket.send_text(json.dumps({"type": "closed"}))
            except Exception:
                pass
            try:
                if websocket.client_state != WebSocketState.DISCONNECTED:
                    await websocket.close()
            except Exception:
                pass
            logger.info(f"logs: follow ended for env {environment.id}, user {user.id}")

    # ── Shared helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _token_still_valid(raw_token: str) -> bool:
        """Re-decode + re-validate the platform token in a fresh session.

        Returns False when the token is expired/revoked (incl. desktop-token
        revocation) or the user is no longer active. Imported lazily to avoid a
        service→deps import cycle at module load.
        """
        from app.api.deps import _resolve_platform_user_from_token

        try:
            with Session(engine) as db:
                _resolve_platform_user_from_token(db, raw_token)
            return True
        except Exception:
            return False

    @staticmethod
    async def _pump_bidirectional(
        *,
        websocket: "WebSocket",
        env_ws,
        client_to_env,
        env_to_client,
        heartbeat,
    ) -> None:
        """Run the two pumps + heartbeat until any completes, then tear down.

        Shared by the terminal tunnel (and reusable by future console types).
        Cancels the survivors and closes the env-core WS in ``finally`` so a
        disconnect on either side tears the whole session down.
        """
        pump_a = asyncio.create_task(client_to_env())
        pump_b = asyncio.create_task(env_to_client())
        hb_task = asyncio.create_task(heartbeat())

        try:
            await asyncio.wait(
                [pump_a, pump_b, hb_task],
                return_when=asyncio.FIRST_COMPLETED,
            )
        except Exception as e:
            logger.debug(f"console pump ended: {e}")
        finally:
            for task in (pump_a, pump_b, hb_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(pump_a, pump_b, hb_task, return_exceptions=True)

            try:
                await env_ws.close()
            except Exception:
                pass

            try:
                if websocket.client_state != WebSocketState.DISCONNECTED:
                    await websocket.close()
            except Exception:
                pass

    @staticmethod
    async def _audit_async(
        *,
        user_id: uuid.UUID,
        environment: AgentEnvironment,
        agent_id: uuid.UUID,
        event_type: str,
        details: dict,
    ) -> None:
        """Write a SecurityEvent audit row (best-effort, own session)."""
        from app.services.events.security_event_service import SecurityEventService

        try:
            with Session(engine) as db:
                await SecurityEventService.create_event(
                    session=db,
                    user_id=user_id,
                    data=SecurityEventCreate(
                        environment_id=environment.id,
                        agent_id=agent_id,
                        event_type=event_type,
                        severity="medium",
                        details=details,
                    ),
                )
        except Exception as e:
            logger.warning(f"Failed to write console audit event {event_type}: {e}")
