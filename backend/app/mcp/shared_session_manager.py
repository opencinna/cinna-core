"""
DB-backed StreamableHTTP session manager for multi-worker MCP deployments.

The standard MCP SDK ``StreamableHTTPSessionManager`` keeps transport sessions
in a per-process ``_server_instances`` dict.  With multiple uvicorn workers
(``--workers N``), a session created on worker A is invisible to worker B,
causing "Session not found" errors.

``SharedSessionManager`` extends the SDK manager with a PostgreSQL-backed
session registry.  When a request arrives for a session ID that is not in
local memory but *does* exist in the DB, the manager "warms" the session
by creating a new local transport with the same session ID and running the
MCP server in ``stateless=True`` mode (which starts in Initialized state,
bypassing the initialize handshake that already happened on the original
worker).

Session lifecycle:
- **Create**: new session ID is stored in both ``_server_instances`` and DB
- **Warm**: session ID found in DB but not locally → recreate locally
- **Terminate**: session removed from ``_server_instances`` and DB
- **Cascade**: connector deletion cascades to all its transport sessions
"""

import logging
import uuid
from http import HTTPStatus

import anyio
from anyio.abc import TaskStatus
from sqlmodel import Session as DBSession, select
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import Receive, Scope, Send

from mcp.server.streamable_http import (
    MCP_SESSION_ID_HEADER,
    EventStore,
    StreamableHTTPServerTransport,
)
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from mcp.server.lowlevel.server import Server as MCPServer
from mcp.types import INVALID_REQUEST, ErrorData, JSONRPCError

from app.core.db import engine
from app.models.mcp_transport_session import MCPTransportSession

logger = logging.getLogger(__name__)


class SharedSessionManager(StreamableHTTPSessionManager):
    """Session manager that uses PostgreSQL to share session state across workers.

    Drop-in replacement for ``StreamableHTTPSessionManager``.  The public API
    is identical; only the internal session lookup gains a DB fallback.

    Args:
        connector_id: UUID string of the MCP connector this manager serves.
        app, event_store, json_response, stateless, security_settings:
            Passed through to the parent ``StreamableHTTPSessionManager``.
    """

    def __init__(
        self,
        connector_id: str,
        app: MCPServer,
        event_store: EventStore | None = None,
        json_response: bool = False,
        stateless: bool = False,
        security_settings: TransportSecuritySettings | None = None,
        retry_interval: int | None = None,
    ):
        if stateless:
            raise ValueError(
                "SharedSessionManager does not support stateless mode — "
                "stateless transports have no session IDs to share across workers"
            )
        super().__init__(
            app=app,
            event_store=event_store,
            json_response=json_response,
            stateless=stateless,
            security_settings=security_settings,
            retry_interval=retry_interval,
        )
        self._connector_id = connector_id
        # Per-session warming locks to prevent duplicate warming when
        # multiple concurrent requests arrive for the same unwarmed session.
        self._warming_locks: dict[str, anyio.Lock] = {}

    # ------------------------------------------------------------------
    # DB helpers (sync — fast single-row queries, consistent with codebase)
    # ------------------------------------------------------------------

    def _db_save_session(self, session_id: str) -> None:
        """Persist a new transport session to the shared registry."""
        try:
            with DBSession(engine) as db:
                record = MCPTransportSession(
                    session_id=session_id,
                    connector_id=uuid.UUID(self._connector_id),
                )
                db.add(record)
                db.commit()
                logger.debug(
                    "[MCP] Saved transport session %s to DB (connector=%s)",
                    session_id, self._connector_id,
                )
        except Exception:
            # Non-fatal: session still works in-memory on this worker.
            # Only multi-worker warm-up is affected.
            logger.warning(
                "[MCP] Failed to save transport session %s to DB",
                session_id, exc_info=True,
            )

    def _db_session_exists(self, session_id: str) -> bool:
        """Check whether a transport session exists in the shared registry."""
        try:
            with DBSession(engine) as db:
                stmt = select(MCPTransportSession).where(
                    MCPTransportSession.session_id == session_id,
                    MCPTransportSession.connector_id == uuid.UUID(self._connector_id),
                )
                return db.exec(stmt).first() is not None
        except Exception:
            logger.warning(
                "[MCP] Failed to check transport session %s in DB",
                session_id, exc_info=True,
            )
            return False

    def _db_delete_session(self, session_id: str) -> None:
        """Remove a transport session from the shared registry."""
        try:
            with DBSession(engine) as db:
                record = db.get(MCPTransportSession, session_id)
                if record:
                    db.delete(record)
                    db.commit()
                    logger.debug(
                        "[MCP] Deleted transport session %s from DB", session_id
                    )
        except Exception:
            logger.warning(
                "[MCP] Failed to delete transport session %s from DB",
                session_id, exc_info=True,
            )

    # ------------------------------------------------------------------
    # Overridden stateful request handling
    # ------------------------------------------------------------------

    async def _handle_stateful_request(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        """Process request with DB-backed session lookup.

        Flow:
        1. Session ID present & locally known → fast path (handle directly)
        2. No session ID → create new session (save to DB too)
        3. Session ID present, not local, in DB → warm session then handle
        4. Session ID present, not in DB → 404
        """
        request = Request(scope, receive)
        request_mcp_session_id = request.headers.get(MCP_SESSION_ID_HEADER)

        # ── Fast path: session exists locally ──
        if (
            request_mcp_session_id is not None
            and request_mcp_session_id in self._server_instances
        ):
            transport = self._server_instances[request_mcp_session_id]
            await transport.handle_request(scope, receive, send)
            return

        # ── New session: no session ID header ──
        if request_mcp_session_id is None:
            await self._create_new_session(scope, receive, send)
            return

        # ── Session ID provided but not local — check DB ──
        if self._db_session_exists(request_mcp_session_id):
            await self._warm_session(request_mcp_session_id, scope, receive, send)
            return

        # ── Unknown session — 404 ──
        logger.warning(
            "[MCP] Unknown transport session %s for connector %s — returning 404",
            request_mcp_session_id, self._connector_id,
        )
        error_response = JSONRPCError(
            jsonrpc="2.0",
            id="server-error",
            error=ErrorData(
                code=INVALID_REQUEST,
                message="Session not found",
            ),
        )
        response = Response(
            content=error_response.model_dump_json(by_alias=True, exclude_none=True),
            status_code=HTTPStatus.NOT_FOUND,
            media_type="application/json",
        )
        await response(scope, receive, send)

    # ------------------------------------------------------------------
    # Session creation (mirrors parent logic + DB persistence)
    # ------------------------------------------------------------------

    async def _create_new_session(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        """Create a new MCP transport session and persist it to the DB.

        NOTE: Mirrors the new-session branch of
        ``StreamableHTTPSessionManager._handle_stateful_request`` from
        mcp SDK >=1.26.  Review this method when upgrading the mcp package.
        """
        from uuid import uuid4

        async with self._session_creation_lock:
            new_session_id = uuid4().hex
            http_transport = StreamableHTTPServerTransport(
                mcp_session_id=new_session_id,
                is_json_response_enabled=self.json_response,
                event_store=self.event_store,
                security_settings=self.security_settings,
                retry_interval=self.retry_interval,
            )

            assert http_transport.mcp_session_id is not None
            self._server_instances[http_transport.mcp_session_id] = http_transport

            # Persist to DB for cross-worker discovery
            self._db_save_session(new_session_id)

            logger.info(
                "[MCP] Created new transport session %s (connector=%s)",
                new_session_id, self._connector_id,
            )

            async def run_server(
                *, task_status: TaskStatus[None] = anyio.TASK_STATUS_IGNORED
            ) -> None:
                async with http_transport.connect() as streams:
                    read_stream, write_stream = streams
                    task_status.started()
                    try:
                        await self.app.run(
                            read_stream,
                            write_stream,
                            self.app.create_initialization_options(),
                            stateless=False,
                        )
                    except Exception as e:
                        logger.error(
                            "[MCP] Session %s crashed: %s",
                            http_transport.mcp_session_id, e, exc_info=True,
                        )
                    finally:
                        sid = http_transport.mcp_session_id
                        if sid and sid in self._server_instances:
                            if not http_transport.is_terminated:
                                logger.info(
                                    "[MCP] Cleaning up crashed session %s", sid
                                )
                            del self._server_instances[sid]
                        # Always clean DB — whether terminated or crashed
                        if sid:
                            self._db_delete_session(sid)

            assert self._task_group is not None
            await self._task_group.start(run_server)
            await http_transport.handle_request(scope, receive, send)

    # ------------------------------------------------------------------
    # Session warming (recreate locally from DB registry)
    # ------------------------------------------------------------------

    async def _warm_session(
        self,
        session_id: str,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        """Warm a session that exists in DB but not in local memory.

        Creates a new transport with the same session ID and runs the MCP
        server in ``stateless=True`` mode so it starts in Initialized state
        (the real initialize handshake already happened on the original worker).
        """
        # Per-session lock to prevent duplicate warming from concurrent requests
        if session_id not in self._warming_locks:
            self._warming_locks[session_id] = anyio.Lock()

        async with self._warming_locks[session_id]:
            # Double-check: another request may have warmed it while we waited
            if session_id in self._server_instances:
                transport = self._server_instances[session_id]
                await transport.handle_request(scope, receive, send)
                return

            logger.info(
                "[MCP] Warming transport session %s on this worker (connector=%s)",
                session_id, self._connector_id,
            )

            http_transport = StreamableHTTPServerTransport(
                mcp_session_id=session_id,
                is_json_response_enabled=self.json_response,
                event_store=self.event_store,
                security_settings=self.security_settings,
                retry_interval=self.retry_interval,
            )

            self._server_instances[session_id] = http_transport

            async def run_warmed_server(
                *, task_status: TaskStatus[None] = anyio.TASK_STATUS_IGNORED
            ) -> None:
                async with http_transport.connect() as streams:
                    read_stream, write_stream = streams
                    task_status.started()
                    try:
                        # stateless=True → ServerSession starts in Initialized
                        # state, bypassing the initialize handshake that already
                        # happened on the original worker.
                        await self.app.run(
                            read_stream,
                            write_stream,
                            self.app.create_initialization_options(),
                            stateless=True,
                        )
                    except Exception as e:
                        logger.error(
                            "[MCP] Warmed session %s crashed: %s",
                            session_id, e, exc_info=True,
                        )
                    finally:
                        if session_id in self._server_instances:
                            if not http_transport.is_terminated:
                                logger.info(
                                    "[MCP] Cleaning up crashed warmed session %s",
                                    session_id,
                                )
                            del self._server_instances[session_id]
                        # Always clean DB — whether terminated or crashed
                        self._db_delete_session(session_id)

            assert self._task_group is not None
            await self._task_group.start(run_warmed_server)
            await http_transport.handle_request(scope, receive, send)
