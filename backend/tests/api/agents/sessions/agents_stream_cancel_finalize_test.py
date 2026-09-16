"""
Integration tests for C3 cancel-finalize (plan: a2a_crash_recovery_plan.md
§3.2, §6 T3): when the task running ``MessageService.stream_message_with_events``
is cancelled (``asyncio.CancelledError``, not a graceful user-requested
interrupt), its ``finally`` block seals whatever agent row exists as
``aborted`` (``_seal_unfinished_turn`` → ``_apply_aborted``) rather than
leaving it ``streaming_in_progress`` forever.

**Why a raw task cancel, not the interrupt endpoint.** The interrupt/cancel
API path (``MessageService.interrupt_stream``) forwards to the agent-env and
waits for it to reply with a graceful ``"interrupted"`` SSE event — that
finalizes the row as ``user_interrupted`` through the normal (non-exception)
code path, covered by ``tests/api/a2a_integration/test_a2a_task_state.py``'s
cancel scenario. This file exercises the *other* branch: the surrounding
task itself is cancelled (the process/task-teardown case, e.g. a killed
worker or a shutdown), which is a ``CancelledError`` hitting the generator's
``finally`` with no interrupt ever requested — sealing ``aborted``, not
``user_interrupted`` (``active_streaming_manager.is_interrupt_requested_nowait``
is False in this path).

**Harness.** ``send_message`` + ``drain_tasks()`` cannot produce a real
mid-flight cancel: ``drain_tasks()`` runs the collected
``process_pending_messages`` coroutine via a blocking ``asyncio.run()`` from
the test thread, so there is nothing to `.cancel()` while it runs. Instead,
this file pulls the collected coroutine straight out of the test suite's
``BackgroundTaskCollector`` (bypassing ``drain_tasks()``) and drives it as a
real ``asyncio.Task`` inside its own event loop, so the task can be
cancelled while genuinely suspended mid-stream.
"""
from __future__ import annotations

import asyncio
import uuid
from unittest.mock import patch

from fastapi.testclient import TestClient

from tests.utils import background_tasks as bg_tasks_module
from tests.utils.agent import create_agent_via_api, get_agent
from tests.utils.background_tasks import drain_tasks
from tests.utils.message import get_messages_by_role, send_message
from tests.utils.session import create_session_via_api


class _GatedConnector:
    """Agent-env stub that optionally yields one assistant event, then
    blocks forever (the test cancels the task instead of releasing it).
    """

    def __init__(self, *, yield_assistant_before_gate: bool) -> None:
        self._yield_assistant_before_gate = yield_assistant_before_gate
        self.entered = asyncio.Event()
        self.reached_gate = asyncio.Event()
        self._never = asyncio.Event()
        self.stream_calls: list[dict] = []

    async def stream_chat(self, base_url, auth_headers, payload):
        self.stream_calls.append(payload)
        self.entered.set()
        yield {
            "type": "session_created", "content": "",
            "session_id": str(uuid.uuid4()), "metadata": {},
        }
        if self._yield_assistant_before_gate:
            yield {"type": "assistant", "content": "Partial reply before cancel"}
        self.reached_gate.set()
        await self._never.wait()  # never releases; the test cancels the task
        yield {"type": "assistant", "content": "unreachable"}  # pragma: no cover


async def _wait_until(predicate, timeout: float = 5.0) -> None:
    async def _poll() -> None:
        while not predicate():
            await asyncio.sleep(0.005)

    await asyncio.wait_for(_poll(), timeout=timeout)


def _take_collected_coro():
    """Pop the one coroutine ``BackgroundTaskCollector`` captured, bypassing
    ``drain_tasks()`` so the test can run (and cancel) it as a real task.
    """
    collector = bg_tasks_module._collector
    assert collector is not None and collector.pending, (
        "expected exactly one collected background task (process_pending_messages)"
    )
    coro, _name = collector.pending.pop()
    return coro


def _setup_session(client: TestClient, headers: dict[str, str]) -> str:
    agent = create_agent_via_api(client, headers, name="Cancel Finalize Agent")
    drain_tasks()  # build + auto-start the default environment
    agent = get_agent(client, headers, agent["id"])
    assert agent["active_environment_id"] is not None

    session = create_session_via_api(client, headers, agent["id"])
    return session["id"]


def test_cancel_after_first_assistant_event_seals_aborted_with_events_kept(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    A turn cancelled after its first assistant event:
      - the agent row status becomes ``aborted``
      - its streaming events are kept (not discarded)
      - ``streaming_in_progress`` is cleared
    """
    session_id = _setup_session(client, superuser_token_headers)

    stub = _GatedConnector(yield_assistant_before_gate=True)
    with patch("app.services.sessions.message_service.agent_env_connector", stub):
        send_message(client, superuser_token_headers, session_id, content="Hi, cancel me")
        coro = _take_collected_coro()

        async def run():
            with patch("app.services.sessions.message_service.agent_env_connector", stub):
                task = asyncio.create_task(coro)
                await _wait_until(lambda: stub.reached_gate.is_set())
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        asyncio.run(run())

    messages = get_messages_by_role(client, superuser_token_headers, session_id, role="agent")
    assert len(messages) == 1, f"expected exactly one agent row, got {messages}"
    row = messages[0]
    assert row["status"] == "aborted"
    assert row["status_message"] == "Turn aborted before completion"
    assert row["message_metadata"]["streaming_in_progress"] is False
    assert row["message_metadata"]["streaming_events"], (
        "the partial turn's events must be kept, not discarded"
    )
    assert "Partial reply before cancel" in row["content"]


def test_cancel_before_any_assistant_event_writes_no_row(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """A turn cancelled before any assistant content ever arrived writes no
    agent row at all — there is nothing for ``_seal_unfinished_turn`` to seal
    (``agent_message_id`` stays ``None``, so its ``finally`` guard is a no-op).
    """
    session_id = _setup_session(client, superuser_token_headers)

    stub = _GatedConnector(yield_assistant_before_gate=False)
    with patch("app.services.sessions.message_service.agent_env_connector", stub):
        send_message(client, superuser_token_headers, session_id, content="Hi, cancel me early")
        coro = _take_collected_coro()

        async def run():
            with patch("app.services.sessions.message_service.agent_env_connector", stub):
                task = asyncio.create_task(coro)
                await _wait_until(lambda: stub.reached_gate.is_set())
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        asyncio.run(run())

    messages = get_messages_by_role(client, superuser_token_headers, session_id, role="agent")
    assert messages == [], f"expected no agent row before any assistant content, got {messages}"
