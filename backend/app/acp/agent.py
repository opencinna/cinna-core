"""ACP v1 agent backed by durable Cinna conversations and the shared stream pipeline."""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import json
import logging
import time
from contextlib import contextmanager
from typing import Any
from uuid import UUID

from acp import PROTOCOL_VERSION
from acp.connection import Connection
from acp.exceptions import RequestError
from acp.schema import (
    AgentCapabilities,
    CancelNotification,
    Implementation,
    InitializeRequest,
    InitializeResponse,
    LoadSessionRequest,
    LoadSessionResponse,
    NewSessionRequest,
    NewSessionResponse,
    PromptCapabilities,
    PromptRequest,
    PromptResponse,
    SessionNotification,
)
from pydantic import ValidationError
from sqlalchemy import func
from sqlmodel import Session as DBSession
from sqlmodel import select

from app.core.config import settings
from app.core.db import create_session, leader_session
from app.models import Session, SessionCreate, SessionMessage
from app.services.acp.connector_service import ACPConnectorService
from app.services.bundles.install_gate_dispatcher import InstallGateDispatcher
from app.services.sessions.active_streaming_manager import active_streaming_manager
from app.services.sessions.message_service import MessageService
from app.services.sessions.session_service import SessionService
from app.services.sessions.stream_processor import (
    SessionStreamProcessor,
    get_session_lock,
)

logger = logging.getLogger(__name__)
REMOTE_CWD = "/app/workspace"
MAX_PROMPT_BYTES = 128 * 1024
MAX_REPLAY_MESSAGES = 2000
# An admitted prompt holds ``session_lease`` for its whole turn, and
# ``leader_session`` pins one *physical* pooled DB connection for the life of
# that block — up to ``PROMPT_TIMEOUT``. The budget is therefore a slice of the
# shared pool rather than a free-floating constant: a third of ``DB_POOL_SIZE``.
# A third, not a half, because a running prompt also takes *transient*
# checkouts on top of its pinned lease (the watchdog every
# ``AUTH_CHECK_INTERVAL``, each stream write, the finalizer), and REST traffic,
# Socket.IO and the schedulers need the remainder without falling into the
# overflow. Need more concurrency? Raise ``DB_POOL_SIZE``, not this.
MAX_ACTIVE_PROMPTS = settings.DB_POOL_SIZE // 3
# Sub-quota beneath the global cap: the cap alone is process-wide, so a single
# tenant could hold every slot and answer everyone else with -32004 until it
# finished. A third again, so saturating the server takes at least three
# connectors rather than the two the original report described.
# Residual, deliberately accepted: the quota is per *connector*, so one owner
# with many connectors can still crowd the cap. Connector is the granularity
# the protocol exposes (one connector ~ one client install, itself bounded by
# the connector's own ``max_connections``), and it is the granularity a client
# can reason about when it sees the error.
MAX_ACTIVE_PROMPTS_PER_CONNECTOR = max(1, MAX_ACTIVE_PROMPTS // 3)
assert 0 < MAX_ACTIVE_PROMPTS_PER_CONNECTOR < MAX_ACTIVE_PROMPTS, (
    "the per-connector prompt quota must sit strictly below the global cap "
    "(both derive from settings.DB_POOL_SIZE, floored at 8 in config)"
)
MAX_STREAM_BYTES = 8 * 1024 * 1024
MAX_REPLAY_BYTES = 8 * 1024 * 1024
PROMPT_TIMEOUT = 1800
AUTH_CHECK_INTERVAL = 5
# Consecutive *unexpected* watchdog failures tolerated before the turn is
# stopped. A real authorization failure still stops it on the first check.
MAX_AUTH_CHECK_FAILURES = 3
# Lifetime of the per-session authorization memo used by ``update()`` only.
AUTH_MEMO_TTL = 1.0
# ticket -> connector_id of every prompt currently admitted in this process.
_active_prompts: dict[int, UUID] = {}
_prompt_tickets = itertools.count()


def admit_prompt(connector_id: UUID, operation: str) -> int:
    """Reserve one global and one per-connector prompt slot, or raise -32004.

    Slots are keyed by a unique ticket instead of by session id, so a release
    can never free a slot another holder is still using, and both counts are
    derived from this single dict so they cannot drift apart from it.

    Callers must reserve synchronously — no ``await`` between the check and the
    reservation, or two prompts race past a full cap — and release the returned
    ticket in a ``finally``.
    """
    if len(_active_prompts) >= MAX_ACTIVE_PROMPTS:
        raise RequestError(-32004, f"Server {operation} capacity reached; retry later")
    mine = sum(1 for owner in _active_prompts.values() if owner == connector_id)
    if mine >= MAX_ACTIVE_PROMPTS_PER_CONNECTOR:
        raise RequestError(
            -32004, f"Connector {operation} capacity reached; retry later"
        )
    ticket = next(_prompt_tickets)
    _active_prompts[ticket] = connector_id
    return ticket


def release_prompt(ticket: int) -> None:
    _active_prompts.pop(ticket, None)


@contextmanager
def session_lease(session_id: str):
    # Same-session ACP admission across workers. leader_session pins its physical
    # Postgres connection and always releases the advisory lock.
    key = int.from_bytes(
        hashlib.sha256(("acp:" + session_id).encode()).digest()[:8], "big", signed=True
    )
    with leader_session(key) as owner:
        if owner is None:
            raise RequestError(-32006, "Session is busy")
        yield


class CinnaACPAgent:
    """One authenticated protocol connection. Sessions survive the connection."""

    def __init__(self, connector_id: UUID, raw_token: str) -> None:
        self.connector_id = connector_id
        self.raw_token = raw_token
        self.initialized = False
        self.connection: Connection | None = None
        self.loaded: set[str] = set()
        self.prompts: dict[str, asyncio.Task[Any]] = {}
        self.cancelled: set[str] = set()
        self.stop_errors: dict[str, RequestError] = {}
        # session_id -> monotonic time of its last real authorization check.
        # Read by ``authorize_stream`` only; see ``update()``.
        self.auth_checked_at: dict[str, float] = {}

    def authorize(self, db: DBSession):
        auth = ACPConnectorService.authenticate(self.connector_id, self.raw_token, db)
        if auth is None:
            raise RequestError.auth_required()
        return auth

    def session(self, db: DBSession, session_id: str) -> Session:
        auth = self.authorize(db)
        try:
            uid = UUID(session_id)
        except ValueError:
            raise RequestError.resource_not_found() from None
        if str(uid) != session_id:
            raise RequestError.resource_not_found()
        row = db.get(Session, uid)
        expected = {
            "connector_id": str(self.connector_id),
            "token_id": str(auth.token.id),
            "cwd": REMOTE_CWD,
        }
        if (
            row is None
            or row.integration_type != "acp"
            or row.user_id != auth.owner.id
            or row.agent_id != auth.agent.id
            or row.mode != auth.connector.mode
            or (row.session_metadata or {}).get("acp") != expected
        ):
            raise RequestError.resource_not_found()
        return row

    async def dispatch(self, method: str, params: Any, notification: bool) -> Any:
        try:
            return await self._dispatch(method, params, notification)
        except RequestError:
            raise
        except ValidationError:
            raise RequestError.invalid_params(
                {"details": "Invalid ACP request parameters"}
            ) from None
        except Exception:
            logger.exception("ACP request failed (%s)", method)
            raise RequestError.internal_error() from None

    async def _dispatch(self, method: str, params: Any, notification: bool) -> Any:
        """Use SDK models/JSON-RPC, with auth and raw-input gates before tolerant SDK parsing."""
        with create_session() as db:
            self.authorize(db)
        if not isinstance(params, dict):
            raise RequestError.invalid_params()
        if method != "initialize" and not self.initialized:
            raise RequestError(-32003, "Initialize the connection first")
        if notification and method != "session/cancel":
            return None
        if method == "initialize":
            if notification or self.initialized:
                raise RequestError.invalid_request()
            InitializeRequest.model_validate(params)
            self.initialized = True
            return InitializeResponse(
                protocol_version=PROTOCOL_VERSION,
                agent_info=Implementation(name="cinna-core", version="1.0.0"),
                agent_capabilities=AgentCapabilities(
                    load_session=True,
                    prompt_capabilities=PromptCapabilities(
                        image=False, audio=False, embedded_context=False
                    ),
                ),
                auth_methods=[],
                field_meta={
                    "cinna": {
                        "transport": "websocket",
                        "remoteCwd": REMOTE_CWD,
                        "clientTools": False,
                    }
                },
            )
        if method in {"session/new", "session/load"}:
            # Do not silently drop invalid client servers/directories through SDK salvage validators.
            if (
                params.get("mcpServers", []) != []
                or params.get("additionalDirectories", []) != []
            ):
                raise RequestError.invalid_params(
                    {
                        "details": "Client MCP servers and additional directories are unsupported"
                    }
                )
            if params.get("cwd") != REMOTE_CWD:
                raise RequestError.invalid_params(
                    {
                        "details": f"Remote sessions require cwd={REMOTE_CWD}; local client files are not mounted"
                    }
                )
            if len(self.loaded) >= 100 and params.get("sessionId") not in self.loaded:
                raise RequestError(
                    -32004,
                    "Connection session limit reached; reconnect to load another session",
                )
            if method == "session/new":
                NewSessionRequest.model_validate(params)
                return await self.new_session()
            request = LoadSessionRequest.model_validate(params)
            return await self.load_session(request.session_id)
        if method == "session/prompt":
            raw_prompt = params.get("prompt")
            if (
                not isinstance(raw_prompt, list)
                or not raw_prompt
                or any(
                    not isinstance(p, dict)
                    or p.get("type") != "text"
                    or not isinstance(p.get("text"), str)
                    for p in raw_prompt
                )
            ):
                raise RequestError.invalid_params(
                    {"details": "Only nonempty text prompts are supported"}
                )
            request = PromptRequest.model_validate(params)
            text = "\n".join(block["text"] for block in raw_prompt)
            if not text.strip() or len(text.encode()) > MAX_PROMPT_BYTES:
                raise RequestError.invalid_params(
                    {"details": "Prompt is empty or exceeds 128 KiB"}
                )
            return await self.prompt(request.session_id, text)
        if method == "session/cancel":
            if not notification:
                raise RequestError.invalid_request(
                    {"details": "session/cancel is a notification"}
                )
            request = CancelNotification.model_validate(params)
            with create_session() as db:
                self.session(db, request.session_id)
            await self.interrupt(request.session_id)
            return None
        raise RequestError.method_not_found(method)

    async def new_session(self) -> NewSessionResponse:
        with create_session() as db:
            auth = self.authorize(db)
            row = SessionService.create_session(
                db,
                auth.owner.id,
                SessionCreate(agent_id=auth.agent.id, mode=auth.connector.mode),
                integration_type="acp",
            )
            if row is None:
                raise RequestError(-32005, "Agent has no active environment")
            row.session_metadata = {
                **row.session_metadata,
                "acp": {
                    "connector_id": str(self.connector_id),
                    "token_id": str(auth.token.id),
                    "cwd": REMOTE_CWD,
                },
            }
            db.add(row)
            db.commit()
            session_id = str(row.id)
        self.loaded.add(session_id)
        return NewSessionResponse(session_id=session_id)

    async def load_session(self, session_id: str) -> LoadSessionResponse:
        with create_session() as db:
            self.session(db, session_id)
        lock = get_session_lock(session_id)
        if lock.locked():
            raise RequestError(-32006, "Session is busy")
        ticket = admit_prompt(self.connector_id, "session")
        try:
            async with lock:
                with session_lease(session_id):
                    return await self._replay_session(session_id)
        finally:
            release_prompt(ticket)
            self.auth_checked_at.pop(session_id, None)

    async def _replay_session(self, session_id: str) -> LoadSessionResponse:
        with create_session() as db:
            row = self.session(db, session_id)
            history_bytes = db.exec(
                select(
                    func.coalesce(
                        func.sum(func.octet_length(SessionMessage.content)), 0
                    )
                ).where(SessionMessage.session_id == row.id)
            ).one()
            if history_bytes > MAX_REPLAY_BYTES:
                raise RequestError(
                    -32007,
                    "Session history exceeds the 8 MiB replay limit; start a new session",
                )
            messages = db.exec(
                select(SessionMessage)
                .where(SessionMessage.session_id == row.id)
                .order_by(SessionMessage.sequence_number)
                .limit(MAX_REPLAY_MESSAGES + 1)
            ).all()
            if len(messages) > MAX_REPLAY_MESSAGES:
                raise RequestError(
                    -32007,
                    "Session history exceeds replay limit; start a new session",
                )
            history = [
                (m.role, m.content) for m in messages if m.role in {"user", "agent"}
            ]
        for role, text in history:
            await self.update(
                session_id,
                {
                    "sessionUpdate": "user_message_chunk"
                    if role == "user"
                    else "agent_message_chunk",
                    "content": {"type": "text", "text": text},
                },
            )
        self.loaded.add(session_id)
        return LoadSessionResponse()

    async def update(self, session_id: str, update: dict) -> None:
        content = update.get("content")
        if (
            isinstance(content, dict)
            and isinstance(content.get("text"), str)
            and len(content["text"]) > 8192
        ):
            for offset in range(0, len(content["text"]), 8192):
                await self.update(
                    session_id,
                    {
                        **update,
                        "content": {
                            **content,
                            "text": content["text"][offset : offset + 8192],
                        },
                    },
                )
            return
        # Streaming must not continue disclosing output after revocation, but
        # this runs per event *and* per sub-chunk of the split above, and each
        # real check is two blocking queries on the worker's only event loop
        # (a 2000-message replay issued thousands of them while holding the
        # prompt's leased connection). Hence the memo — and hence the memo is
        # confined to this one caller. The hard guarantee is ``watch_prompt``,
        # which re-checks uncached every ``AUTH_CHECK_INTERVAL`` and interrupts
        # the turn; ``AUTH_MEMO_TTL`` is well inside that window, so the memo
        # never becomes the weakest link in the revocation path.
        self.authorize_stream(session_id)
        if self.connection is not None:
            payload = SessionNotification.model_validate(
                {"sessionId": session_id, "update": update}
            )
            await self.connection.send_notification(
                "session/update",
                payload.model_dump(mode="json", by_alias=True, exclude_none=True),
            )

    def authorize_stream(self, session_id: str) -> None:
        """Authorize a streamed notification, reusing a check under a TTL.

        Per-operation callers (``_dispatch``, ``new_session``, ``load_session``,
        ``prompt``, ``session/cancel``, ``watch_prompt``) must keep calling
        ``session()`` directly: they run once per operation, and the watchdog's
        check is the revocation guarantee.
        """
        now = time.monotonic()
        checked_at = self.auth_checked_at.get(session_id)
        if checked_at is not None and now - checked_at < AUTH_MEMO_TTL:
            return
        with create_session() as db:
            self.session(db, session_id)
        self.auth_checked_at[session_id] = now

    async def prompt(self, session_id: str, text: str) -> PromptResponse:
        with create_session() as db:
            self.session(db, session_id)
        if session_id not in self.loaded:
            raise RequestError(
                -32003, "Load this session on the connection before prompting"
            )
        lock = get_session_lock(session_id)
        if lock.locked() or session_id in self.prompts:
            raise RequestError(-32006, "Session is busy")
        ticket = admit_prompt(self.connector_id, "prompt")
        try:
            async with lock:
                with session_lease(session_id):
                    return await self._run_prompt(session_id, text)
        finally:
            release_prompt(ticket)
            self.auth_checked_at.pop(session_id, None)

    async def _run_prompt(self, session_id: str, text: str) -> PromptResponse:
        task = asyncio.current_task()
        assert task is not None
        self.prompts[session_id] = task
        watchdog = asyncio.create_task(self.watch_prompt(session_id))
        self.cancelled.discard(session_id)
        message_id = None
        handler = ACPStreamEventHandler(self, session_id)
        try:
            with create_session() as db:
                row = self.session(db, session_id)
                auth = self.authorize(db)
                gate = InstallGateDispatcher.check(db, auth.agent)
                if gate is not None:
                    raise RequestError(
                        -32005,
                        "Agent setup is required",
                        {"details": gate.user_message, "setupUrl": gate.setup_url},
                    )
                message = MessageService.create_message(db, row.id, "user", text)
                message_id = message.id
                if not row.title:
                    row.title = text[:120]
                    db.add(row)
                    db.commit()
            processor = SessionStreamProcessor(
                session_id=UUID(session_id),
                get_fresh_db_session=create_session,
                event_handler=handler,
                use_session_lock=False,
                ensure_env_ready=True,
                log_prefix="[ACP]",
            )
            await processor.process()
            if handler.error:
                raise RequestError(-32005, "Agent execution failed")
            return PromptResponse(
                stop_reason="cancelled"
                if session_id in self.cancelled or handler.interrupted
                else "end_turn"
            )
        except asyncio.CancelledError:
            if error := self.stop_errors.pop(session_id, None):
                raise error
            return PromptResponse(stop_reason="cancelled")
        except RequestError:
            await self.forward_interrupt(session_id)
            raise
        except Exception:
            await self.forward_interrupt(session_id)
            logger.exception("ACP prompt failed for session %s", session_id)
            raise RequestError(
                -32005, "Agent execution failed; inspect the Cinna session"
            ) from None
        finally:
            watchdog.cancel()
            await asyncio.gather(watchdog, return_exceptions=True)
            self.prompts.pop(session_id, None)
            interrupted = session_id in self.cancelled
            self.cancelled.discard(session_id)
            self.stop_errors.pop(session_id, None)
            # A cancelled/failed preparation must not leave a pending prompt to run unexpectedly later.
            if message_id:
                with create_session() as db:
                    message = db.get(SessionMessage, message_id)
                    row = db.get(Session, UUID(session_id))
                    if row:
                        row.pending_messages_count = 0
                        row.streaming_started_at = None
                        db.add(row)
                    if message and not handler.completed:
                        partial = db.exec(
                            select(SessionMessage)
                            .where(
                                SessionMessage.session_id == message.session_id,
                                SessionMessage.role == "agent",
                                SessionMessage.sequence_number
                                > message.sequence_number,
                            )
                            .order_by(SessionMessage.sequence_number.desc())
                        ).first()
                        if partial and partial.message_metadata.get(
                            "streaming_in_progress"
                        ):
                            partial.content = (
                                "".join(handler.text_parts) or partial.content
                            )
                            partial.message_metadata = {
                                **partial.message_metadata,
                                "streaming_in_progress": False,
                                "streaming_events": handler.events,
                            }
                            partial.status = (
                                "user_interrupted" if interrupted else "error"
                            )
                            partial.status_message = (
                                "ACP prompt interrupted"
                                if interrupted
                                else "ACP prompt failed"
                            )
                            db.add(partial)
                    if message and message.sent_to_agent_status == "pending":
                        message.sent_to_agent_status = "sent"
                        message.status = "error"
                        message.status_message = "ACP prompt stopped before dispatch"
                        db.add(message)
                    db.commit()
            await SessionService.clear_interaction_status(
                UUID(session_id), reason="ACP prompt ended"
            )

    async def watch_prompt(self, session_id: str) -> None:
        deadline = time.monotonic() + PROMPT_TIMEOUT
        failures = 0
        while True:
            await asyncio.sleep(AUTH_CHECK_INTERVAL)
            try:
                with create_session() as db:
                    self.session(db, session_id)
                failures = 0
                if time.monotonic() < deadline:
                    continue
                error = RequestError(
                    -32008, "Prompt exceeded the 30-minute execution limit"
                )
            except RequestError as exc:
                # A real authorization failure (revoked, expired, deactivated)
                # stops the turn on the first check. Never tolerated.
                error = exc
            except Exception:
                # Anything else is infrastructure, not authorization: a pool
                # checkout timeout or a dropped connection must not kill a
                # 25-minute prompt. Tolerate a few in a row, then give up
                # rather than stream on with an unverifiable authorization.
                failures += 1
                logger.exception(
                    "ACP authorization watchdog failed for %s (%d/%d consecutive)",
                    session_id,
                    failures,
                    MAX_AUTH_CHECK_FAILURES,
                )
                if failures < MAX_AUTH_CHECK_FAILURES:
                    continue
                error = RequestError.internal_error()
            self.stop_errors[session_id] = error
            await self.interrupt(session_id)
            return

    async def forward_interrupt(self, session_id: str) -> None:
        # Only used for prompts owned by this authenticated connection. Cleanup
        # deliberately still interrupts after token revocation.
        try:
            with create_session() as db:
                row = db.get(Session, UUID(session_id))
                if row:
                    await asyncio.wait_for(
                        MessageService.interrupt_stream(
                            db_session=db,
                            session_id=row.id,
                            environment_id=row.environment_id,
                        ),
                        timeout=5,
                    )
        except Exception:
            logger.debug(
                "ACP interrupt forwarding unavailable for %s", session_id, exc_info=True
            )

    async def interrupt(self, session_id: str) -> None:
        # Drop the streaming memo first: once the server has decided this turn
        # must stop — a revoked token caught by the watchdog, a client cancel,
        # a disconnect — no further notification may go out on a check made
        # before that decision. Interrupting the environment and awaiting the
        # task takes seconds, and the processor keeps emitting throughout.
        self.auth_checked_at.pop(session_id, None)
        task = self.prompts.get(session_id)
        if task is None or session_id in self.cancelled:
            return
        self.cancelled.add(session_id)
        await self.forward_interrupt(session_id)
        # Allow the environment's interrupted event to run the normal durable
        # finalizer. A stalled environment gets forced cleanup in _run_prompt.
        _, pending = await asyncio.wait({task}, timeout=1)
        if pending:
            # Tell the stream the cancel is a requested stop, so the backend
            # seals the partial reply as user_interrupted, not aborted.
            await active_streaming_manager.mark_interrupted(UUID(session_id))
            task.cancel()

    async def close(self) -> None:
        tasks = list(self.prompts.values())
        await asyncio.gather(
            *(self.interrupt(sid) for sid in list(self.prompts)), return_exceptions=True
        )
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.raw_token = ""
        self.loaded.clear()
        self.auth_checked_at.clear()


class ACPStreamEventHandler:
    """Map incremental agent text/thought/tool events into ACP session updates."""

    def __init__(self, agent: CinnaACPAgent, session_id: str) -> None:
        self.agent = agent
        self.session_id = session_id
        self.error = False
        self.tools: set[str] = set()
        self.completed_tools: set[str] = set()
        self.interrupted = False
        self.completed = False
        self.text_parts: list[str] = []
        self.events: list[dict] = []
        self.stream_bytes = 0

    async def on_stream_starting(self, pending_count: int) -> None:
        with create_session() as db:
            self.agent.session(db, self.session_id)

    async def on_event(self, event: dict) -> None:
        kind = event.get("type")
        content = event.get("content", "")
        metadata = event.get("metadata") or {}
        self.stream_bytes += len(json.dumps(event, default=str).encode())
        if self.stream_bytes > MAX_STREAM_BYTES:
            raise RequestError(-32007, "Agent output exceeds the 8 MiB stream limit")
        if kind not in {"session_created", "done", "error"}:
            self.events.append(event.copy())
        if kind == "assistant" and isinstance(content, str):
            self.text_parts.append(content)
        if kind in {"assistant", "thinking"} and isinstance(content, str) and content:
            await self.agent.update(
                self.session_id,
                {
                    "sessionUpdate": "agent_message_chunk"
                    if kind == "assistant"
                    else "agent_thought_chunk",
                    "content": {"type": "text", "text": content},
                },
            )
        elif kind == "tool":
            tool_id = str(
                metadata.get("tool_id")
                or event.get("tool_use_id")
                or event.get("id")
                or f"tool-{len(self.tools) + 1}"
            )
            self.tools.add(tool_id)
            await self.agent.update(
                self.session_id,
                {
                    "sessionUpdate": "tool_call",
                    "toolCallId": tool_id,
                    "title": str(
                        event.get("tool_name")
                        or metadata.get("tool_name")
                        or event.get("name")
                        or "Agent tool"
                    ),
                    "status": "in_progress",
                    "kind": "other",
                },
            )
        elif kind in {"tool_result", "tool_result_delta"}:
            tool_id = str(metadata.get("tool_id") or event.get("tool_use_id") or "")
            if tool_id in self.tools:
                status = (
                    "in_progress"
                    if kind == "tool_result_delta"
                    else (
                        "failed"
                        if metadata.get("is_error") or event.get("is_error")
                        else "completed"
                    )
                )
                if status != "in_progress":
                    self.completed_tools.add(tool_id)
                await self.agent.update(
                    self.session_id,
                    {
                        "sessionUpdate": "tool_call_update",
                        "toolCallId": tool_id,
                        "status": status,
                    },
                )
        elif kind == "interrupted" or (kind == "done" and metadata.get("interrupted")):
            self.interrupted = True
        elif kind == "error":
            self.error = True

    async def on_error(self, error: Exception) -> None:
        self.error = True

    async def on_complete(self, response_text: str) -> None:
        self.completed = True
        for tool_id in self.tools - self.completed_tools:
            await self.agent.update(
                self.session_id,
                {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": tool_id,
                    "status": "failed"
                    if self.error or self.interrupted
                    else "completed",
                },
            )
