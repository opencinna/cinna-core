"""
Integration tests for C1 crash recovery (plan: a2a_crash_recovery_plan.md §1, §6 T1):
the A2A producer is detached from its SSE consumer, so a client disconnect no
longer aborts the turn.

**Disconnect harness.** Starlette's ``TestClient`` buffers the whole SSE
response and blocks the calling thread for the request, so it cannot produce
a genuine mid-stream disconnect. These tests instead drive the ASGI app
directly (``tests.utils.a2a_raw_asgi``), on a single event loop per test, so
the detached producer task and the test's own polling/waiting share the same
loop. See that module's docstring for why.

**Rule-1 exception, called out explicitly.** ``wait_for_detached_a2a_turns``
(``app.services.sessions.stream_event_handlers``) is imported directly here.
The plan (§1.1) documents it as "Tests use it" for exactly this purpose: a
detached producer is by design *not observable* through any endpoint (that is
the point of C1 — nothing in the API tells a client "there is still a
producer task running for a turn you disconnected from"), so there is no
API-only way to avoid a forced-cancellation race against a bare
``asyncio.run()`` teardown. Every other assertion in this file goes through
the JSON-RPC / REST API only.
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.services.sessions.stream_event_handlers import wait_for_detached_a2a_turns
from tests.stubs.agent_env_stub import StubAgentEnvConnector
from tests.utils.a2a import (
    build_streaming_request,
    extract_task_id,
    post_a2a_jsonrpc,
    setup_a2a_agent,
)
from tests.utils.a2a_raw_asgi import (
    GatedAgentEnvConnector,
    start_a2a_sse,
    stream_a2a_sse_with_disconnect,
    stream_sse_at_with_disconnect,
)
from tests.utils.message import get_messages_by_role
from tests.utils.session import get_agent_session


# ── T1.1 — disconnect mid-stream, turn still completes ─────────────────────


def test_disconnect_mid_stream_turn_completes_and_tasks_get_reports_completed(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    1. Client sends message/stream and disconnects after the first SSE event.
    2. The detached producer keeps running and finishes the turn.
    3. The agent row is finalized (not mid-stream, full content).
    4. tasks/get reports ``completed``.
    """
    agent, token_data = setup_a2a_agent(
        client, superuser_token_headers, name="A2A Disconnect Agent",
    )
    agent_id = agent["id"]
    a2a_token = token_data["token"]

    stub = StubAgentEnvConnector(response_text="Full response delivered after disconnect")
    request = build_streaming_request("Hello, I will disconnect on you")

    async def run():
        with patch("app.services.sessions.message_service.agent_env_connector", stub):
            session = await stream_a2a_sse_with_disconnect(
                client.app, agent_id, a2a_token, request, disconnect_after_events=1,
            )
            # The detached task lives on this event loop only for the
            # duration of this coroutine — wait for it here or asyncio.run()
            # tears it down mid-flight before it can finish the turn.
            await wait_for_detached_a2a_turns(timeout=10.0)
        return session

    session = asyncio.run(run())

    assert len(session.events) >= 1, "must have seen at least the initial working event"
    task_id = extract_task_id(session.events)
    assert task_id is not None

    # ── tasks/get reports the turn as completed ────────────────────────
    body = post_a2a_jsonrpc(
        client, agent_id, a2a_token,
        {"jsonrpc": "2.0", "id": "get-1", "method": "tasks/get", "params": {"id": task_id}},
    )
    task = body["result"]
    assert task["status"]["state"] == "completed"

    # ── The session and agent message reflect a real completed turn ────
    session_data = get_agent_session(client, superuser_token_headers, agent_id)
    assert session_data["id"] == task_id

    agent_messages = get_messages_by_role(client, superuser_token_headers, task_id, role="agent")
    assert len(agent_messages) >= 1
    last_agent_message = agent_messages[-1]
    assert "Full response delivered after disconnect" in last_agent_message["content"]
    assert last_agent_message["message_metadata"].get("streaming_in_progress") is False

    # The producer did in fact run to completion — the stub was invoked.
    assert len(stub.stream_calls) == 1


# ── T1.2 — two message/stream calls on one taskId serialize via the lock ───


def test_second_stream_on_same_task_waits_for_first_then_completes(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Two overlapping ``message/stream`` calls on the same ``taskId``:
      - the second call yields its initial ``working`` event immediately
        (queued before the lock is taken, per plan §1.3),
      - its underlying agent-env stream only *starts* after the first
        turn's stream finished (asserted via the stub's call-order log),
      - both turns end with a final event.
    """
    agent, token_data = setup_a2a_agent(
        client, superuser_token_headers, name="A2A Concurrent Stream Agent",
    )
    agent_id = agent["id"]
    a2a_token = token_data["token"]

    call_log: list[str] = []
    first_stub = GatedAgentEnvConnector(
        response_text="First turn reply", label="first", call_log=call_log,
    )

    async def run():
        with patch("app.services.sessions.message_service.agent_env_connector", first_stub):
            first_request = build_streaming_request("First message")
            first_session = start_a2a_sse(client.app, agent_id, a2a_token, first_request)
            # Wait for the first turn's producer to actually start (proves
            # it is genuinely in flight, not racing a timer).
            await first_session.wait_for_event_count(1, timeout=5.0)

            task_id = extract_task_id(first_session.events)
            assert task_id is not None

            second_stub = GatedAgentEnvConnector(
                response_text="Second turn reply", label="second", call_log=call_log,
            )
            second_request = build_streaming_request("Second message", task_id=task_id)

            with patch(
                "app.services.sessions.message_service.agent_env_connector", second_stub,
            ):
                second_session = start_a2a_sse(
                    client.app, agent_id, a2a_token, second_request,
                )
                # The second call's initial "working" event must arrive
                # even though the first turn is still gated open — it is
                # queued before the per-session lock is taken.
                await second_session.wait_for_event_count(1, timeout=5.0)
                assert (
                    second_session.events[0]["result"]["status"]["state"] == "working"
                )
                # The second turn's agent-env stream must NOT have started yet.
                assert call_log == ["first:start"], (
                    f"second turn's env stream started before the first finished: {call_log}"
                )

                # Release the first turn; it finishes, then the second
                # (queued on the lock) gets to run.
                first_stub.release()
                await wait_for_detached_a2a_turns(timeout=10.0)

                second_stub.release()
                await asyncio.wait_for(second_session.done.wait(), timeout=10.0)
                await wait_for_detached_a2a_turns(timeout=10.0)

        return first_session, second_session

    first_session, second_session = asyncio.run(run())

    assert call_log == ["first:start", "first:end", "second:start", "second:end"], (
        "the second turn's env stream must start only after the first ended"
    )

    assert first_session.events[-1]["result"]["status"]["state"] == "completed"
    assert first_session.events[-1]["result"]["final"] is True
    assert second_session.events[-1]["result"]["status"]["state"] == "completed"
    assert second_session.events[-1]["result"]["final"] is True


# ── T1.3 — second stream finds nothing pending → final completed ───────────


def test_second_stream_with_nothing_pending_gets_immediate_final_completed(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Both user messages get batched into the first turn (e.g. sent in quick
    succession before the first turn's env call starts). The second
    ``message/stream`` call finds no pending work of its own once it gets
    the lock, and must still close out with a final ``completed`` event
    (``emit_final_state`` — the "nothing to stream" case), not hang.
    """
    agent, token_data = setup_a2a_agent(
        client, superuser_token_headers, name="A2A Batched Stream Agent",
    )
    agent_id = agent["id"]
    a2a_token = token_data["token"]

    stub = GatedAgentEnvConnector(response_text="Batched reply", label="only")

    async def run():
        with patch("app.services.sessions.message_service.agent_env_connector", stub):
            first_request = build_streaming_request("First message")
            first_session = start_a2a_sse(client.app, agent_id, a2a_token, first_request)
            await first_session.wait_for_event_count(1, timeout=5.0)
            task_id = extract_task_id(first_session.events)
            assert task_id is not None

            # Second message piggybacks onto the same in-flight turn: by the
            # time it is picked up, the first turn's env call has already
            # collected all pending messages, so the second stream call has
            # nothing new to process itself.
            second_request = build_streaming_request("Second message", task_id=task_id)
            second_session = start_a2a_sse(client.app, agent_id, a2a_token, second_request)
            await second_session.wait_for_event_count(1, timeout=5.0)

            stub.release()
            await wait_for_detached_a2a_turns(timeout=10.0)
            await asyncio.wait_for(second_session.done.wait(), timeout=10.0)
            await wait_for_detached_a2a_turns(timeout=10.0)

        return second_session

    second_session = asyncio.run(run())

    assert second_session.events, "second stream must not hang without ever emitting anything"
    last = second_session.events[-1]["result"]
    assert last["status"]["state"] == "completed"
    assert last["final"] is True


# ── T1.4 — T1.1 repeated on /external/a2a ───────────────────────────────────


def test_disconnect_mid_stream_completes_on_external_a2a(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db,
) -> None:
    """T1.1, but through ``/external/a2a`` — both surfaces share
    ``handle_message_stream`` / ``handle_tasks_get`` (plan §0), so the
    same disconnect-survives guarantee must hold there too.

    Reuses the external-A2A agent setup from
    ``tests/api/external/external_a2a_agent_test.py`` (superuser-owned agent,
    caller's own bearer token — the external surface has no separate access
    token concept, per that file's ``_create_su_agent`` / ``_post_external_a2a``).

    ``tests/api/external/`` has its own conftest that additionally patches
    ``app.api.routes.external_a2a.create_session`` — ``a2a_integration``'s
    conftest does not (no reason to know about that route). Without it the
    external route's session lookups run outside this test's transaction and
    can't see the agent/environment just created here. Patched locally, just
    for this test, rather than growing ``a2a_integration/conftest.py`` for
    one test that borrows another domain's route.
    """
    from tests.api.external.external_a2a_agent_test import _EXT_A2A_BASE, _create_su_agent
    from tests.utils.fixtures import patched_create_sessions

    with patched_create_sessions(db, ["app.api.routes.external_a2a.create_session"]):
        agent = _create_su_agent(
            client, superuser_token_headers, "External A2A Disconnect Agent",
        )
        agent_id = agent["id"]
        path = f"{_EXT_A2A_BASE}/agent/{agent_id}/"
        headers = {
            **superuser_token_headers,
            "Content-Type": "application/json",
        }

        stub = StubAgentEnvConnector(response_text="External full response after disconnect")
        request = build_streaming_request("External hello, disconnecting now")

        async def run():
            with patch("app.services.sessions.message_service.agent_env_connector", stub):
                session = await stream_sse_at_with_disconnect(
                    client.app, path, headers, request, disconnect_after_events=1,
                )
                await wait_for_detached_a2a_turns(timeout=10.0)
            return session

        session = asyncio.run(run())

        assert len(session.events) >= 1
        task_id = extract_task_id(session.events)
        assert task_id is not None

        resp = client.post(
            path,
            headers=headers,
            json={
                "jsonrpc": "2.0", "id": "ext-get-1", "method": "tasks/get",
                "params": {"id": task_id},
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "result" in body, body
        assert body["result"]["status"]["state"] == "completed"
