"""Raw ASGI driver + gated agent-env stub for A2A crash-recovery tests.

Starlette's ``TestClient`` buffers the whole SSE response and blocks the
calling thread for the duration of the request (it drives the app on a
dedicated portal thread — see ``starlette.testclient._TestClientTransport``).
That makes it unusable for two things the crash-recovery tests need:

1. **Simulating a client disconnect mid-stream** — TestClient has no hook to
   send ``http.disconnect`` while the app is still producing body chunks.
2. **True concurrency** between a request in flight and something else that
   must run on the *same* event loop while it waits (a second overlapping
   request, or manually draining a background task that a poll loop is
   waiting on).

Both need the app driven directly (``await app(scope, receive, send)``) from
inside a single ``asyncio.run(...)``, so callers can use ``asyncio.create_task``
/ ``asyncio.gather`` freely. Tests get the app via the existing ``client``
fixture (``client.app``) — this module does not import ``app.main`` itself,
so it stays inside the "no direct app-internals" rule for ``tests/api/``.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

from app.core.config import settings


def _build_scope(path: str, headers: dict[str, str]) -> dict:
    header_list = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    if not any(k == b"host" for k, _ in header_list):
        header_list.append((b"host", b"testserver"))
    return {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "scheme": "http",
        "query_string": b"",
        "headers": header_list,
        "client": ("testclient", 50000),
        "server": ["testserver", 80],
        "extensions": {"http.response.debug": {}},
        "state": {},
    }


class A2ASSESession:
    """One in-flight POST to the A2A endpoint, driven via raw ASGI.

    ``events`` accumulates parsed SSE ``data:`` payloads as they arrive.
    ``disconnect_now()`` makes the next ``receive()`` call return
    ``http.disconnect`` — the coroutine awaiting it (inside the app) sees the
    disconnect exactly like a real dropped SSE client.
    """

    def __init__(self) -> None:
        self.events: list[dict] = []
        self.status: int | None = None
        self.done = asyncio.Event()
        self.task: asyncio.Task | None = None
        self._disconnect = asyncio.Event()
        self.raw_body = bytearray()

    def disconnect_now(self) -> None:
        self._disconnect.set()

    async def wait_for_event_count(self, count: int, timeout: float = 5.0) -> None:
        """Wait until at least ``count`` SSE events have been parsed."""

        async def _poll() -> None:
            while len(self.events) < count and not self.done.is_set():
                await asyncio.sleep(0.005)

        await asyncio.wait_for(_poll(), timeout=timeout)

    async def _run(self, app: Any, path: str, headers: dict[str, str], payload: dict) -> None:
        body = json.dumps(payload).encode()
        scope = _build_scope(path, headers)
        sent_body = False

        async def receive() -> dict:
            nonlocal sent_body
            if not sent_body:
                sent_body = True
                return {"type": "http.request", "body": body, "more_body": False}
            await self._disconnect.wait()
            return {"type": "http.disconnect"}

        async def send(message: dict) -> None:
            if message["type"] == "http.response.start":
                self.status = message["status"]
            elif message["type"] == "http.response.body":
                chunk = message.get("body", b"")
                if chunk:
                    self.raw_body.extend(chunk)
                    for line in chunk.decode().splitlines():
                        line = line.strip()
                        if line.startswith("data: "):
                            try:
                                self.events.append(json.loads(line[6:]))
                            except json.JSONDecodeError:
                                pass
                if not message.get("more_body", False):
                    self.done.set()

        try:
            await app(scope, receive, send)
        finally:
            self.done.set()


def _a2a_path(agent_id: str) -> str:
    return f"{settings.API_V1_STR}/a2a/{agent_id}/"


def _bearer_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def start_sse_at(app: Any, path: str, headers: dict[str, str], payload: dict) -> A2ASSESession:
    """Start a streaming POST to an arbitrary A2A-shaped ``path`` as a background
    task on the current loop (used for both ``/api/v1/a2a`` and ``/external/a2a``).

    Returns immediately with a session the caller can poll (``wait_for_event_count``)
    or disconnect (``disconnect_now``) while the request is still in flight.
    """
    session = A2ASSESession()
    session.task = asyncio.create_task(session._run(app, path, headers, payload))
    return session


def start_a2a_sse(
    app: Any,
    agent_id: str,
    a2a_token: str,
    payload: dict,
) -> A2ASSESession:
    """``start_sse_at`` for the standard ``/api/v1/a2a/{agent_id}/`` surface."""
    return start_sse_at(app, _a2a_path(agent_id), _bearer_headers(a2a_token), payload)


async def post_json_at(
    app: Any,
    path: str,
    headers: dict[str, str],
    payload: dict,
    *,
    timeout: float = 10.0,
) -> tuple[int | None, dict]:
    """Plain (non-draining) JSON-RPC POST on the current event loop.

    Use this — not ``post_json_at_draining`` — for a quick call (``tasks/get``,
    ``tasks/cancel``) made *while* another gated turn is still deliberately
    held open on the same loop: it must not race that turn's own background
    tasks, it only needs to run concurrently with it.
    """
    session = A2ASSESession()
    await asyncio.wait_for(session._run(app, path, headers, payload), timeout=timeout)
    body = json.loads(bytes(session.raw_body)) if session.raw_body else {}
    return session.status, body


async def post_a2a_json_at(
    app: Any,
    agent_id: str,
    a2a_token: str,
    payload: dict,
    *,
    timeout: float = 10.0,
) -> tuple[int | None, dict]:
    """``post_json_at`` for the standard ``/api/v1/a2a/{agent_id}/`` surface."""
    return await post_json_at(app, _a2a_path(agent_id), _bearer_headers(a2a_token), payload, timeout=timeout)


async def stream_sse_at_with_disconnect(
    app: Any,
    path: str,
    headers: dict[str, str],
    payload: dict,
    *,
    disconnect_after_events: int = 1,
    timeout: float = 15.0,
) -> A2ASSESession:
    """Run one streaming POST to ``path``, disconnecting after N SSE events.

    Waits for ``disconnect_after_events`` events, then signals
    ``http.disconnect`` and waits for the ASGI call to unwind (the app's own
    ``finally`` block detaches the consumer; the producer itself is NOT
    awaited here — it keeps running detached, per C1).
    """
    session = start_sse_at(app, path, headers, payload)
    await session.wait_for_event_count(disconnect_after_events, timeout=timeout)
    session.disconnect_now()
    await asyncio.wait_for(session.done.wait(), timeout=timeout)
    return session


async def stream_a2a_sse_with_disconnect(
    app: Any,
    agent_id: str,
    a2a_token: str,
    payload: dict,
    *,
    disconnect_after_events: int = 1,
    timeout: float = 15.0,
) -> A2ASSESession:
    """``stream_sse_at_with_disconnect`` for the standard ``/api/v1/a2a/{agent_id}/`` surface."""
    return await stream_sse_at_with_disconnect(
        app, _a2a_path(agent_id), _bearer_headers(a2a_token), payload,
        disconnect_after_events=disconnect_after_events, timeout=timeout,
    )


async def post_json_at_draining(
    app: Any,
    path: str,
    headers: dict[str, str],
    payload: dict,
    *,
    timeout: float = 30.0,
) -> tuple[int | None, dict]:
    """POST a non-streaming JSON-RPC request while draining collected background
    tasks concurrently, on the same event loop.

    ``message/send`` polls ``task_store.get_state`` in a loop, waiting for the
    turn kicked off via ``create_task_with_error_logging`` to finish. Under the
    test suite's ``BackgroundTaskCollector`` that coroutine is only *captured*,
    never scheduled — ``drain_tasks()`` is meant to run it afterward, from the
    test thread, which would deadlock a poll loop that is still waiting inside
    the very request that would collect it. Since this helper runs the request
    on the *current* event loop (no separate TestClient portal thread), a
    concurrent "drainer" coroutine can safely pop and ``await`` collected
    coroutines in place — same loop, no ``asyncio.run()`` re-entrancy issue,
    no cross-thread DB session sharing.
    """
    from tests.utils import background_tasks as bg_tasks_module

    session = A2ASSESession()

    async def _drainer() -> None:
        while not session.done.is_set():
            collector = bg_tasks_module._collector
            if collector is not None and collector.pending:
                batch = list(collector.pending)
                collector.pending.clear()
                for coro, _name in batch:
                    await coro
            else:
                await asyncio.sleep(0.005)

    drainer_task = asyncio.create_task(_drainer())
    try:
        await asyncio.wait_for(
            session._run(app, path, headers, payload), timeout=timeout,
        )
    finally:
        session.done.set()
        drainer_task.cancel()
        try:
            await drainer_task
        except asyncio.CancelledError:
            pass

    body = json.loads(bytes(session.raw_body)) if session.raw_body else {}
    return session.status, body


async def post_a2a_json_draining(
    app: Any,
    agent_id: str,
    a2a_token: str,
    payload: dict,
    *,
    timeout: float = 30.0,
) -> tuple[int | None, dict]:
    """``post_json_at_draining`` for the standard ``/api/v1/a2a/{agent_id}/`` surface."""
    return await post_json_at_draining(
        app, _a2a_path(agent_id), _bearer_headers(a2a_token), payload, timeout=timeout,
    )


class GatedAgentEnvConnector:
    """Agent-env stub whose stream blocks until explicitly released.

    Lets a test observe a turn genuinely "in flight" (deterministically,
    without racing a timer) before letting it finish. ``call_log`` — when
    shared across two instances — records ``"<label>:start"`` /
    ``"<label>:end"`` so a test can assert the order two overlapping turns
    actually ran in.
    """

    def __init__(
        self,
        response_text: str = "done",
        *,
        label: str = "turn",
        call_log: list[str] | None = None,
        end_event: dict | None = None,
    ) -> None:
        self.response_text = response_text
        self.label = label
        self.call_log = call_log if call_log is not None else []
        self.end_event = end_event
        self._release = asyncio.Event()
        self.stream_calls: list[dict] = []

    def release(self) -> None:
        self._release.set()

    async def stream_chat(self, base_url, auth_headers, payload):
        self.stream_calls.append({"base_url": base_url, "payload": payload})
        self.call_log.append(f"{self.label}:start")
        yield {
            "type": "session_created",
            "content": "",
            "session_id": str(uuid.uuid4()),
            "metadata": {},
        }
        await self._release.wait()
        if self.end_event is not None:
            yield self.end_event
        else:
            yield {"type": "assistant", "content": self.response_text, "metadata": {}}
            yield {"type": "done"}
        self.call_log.append(f"{self.label}:end")
