"""
Integration tests for C2 ``tasks/get`` state and the ``message/send`` poll
(plan: a2a_crash_recovery_plan.md §2, §6 T2).

No test existed for either before this file (per the plan's §6 note). The
unit-level decision table lives in ``tests/unit/test_a2a_task_state_mapping.py``;
these tests exercise the same rules end to end, through the API, including
the "turn genuinely in flight" and "cancel mid-turn" cases that need real
concurrency — see ``tests/utils/a2a_raw_asgi.py`` for why (and for the
documented Rule-1 exception around ``wait_for_detached_a2a_turns``, used
here for the same reason as in ``test_a2a_crash_recovery.py``).
"""
from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.services.sessions.stream_event_handlers import wait_for_detached_a2a_turns
from tests.stubs.agent_env_stub import StubAgentEnvConnector
from tests.utils.a2a import (
    build_streaming_request,
    extract_parts_from_sse_event,
    extract_task_id,
    part_metadata,
    part_text,
    parse_sse_events,
    post_a2a_jsonrpc,
    send_a2a_streaming_message,
    setup_a2a_agent,
)
from tests.utils.a2a_raw_asgi import (
    GatedAgentEnvConnector,
    post_a2a_json_at,
    post_a2a_json_draining,
    start_a2a_sse,
)
from tests.utils.background_tasks import drain_tasks


def _build_send_request(message_text: str, task_id: str | None = None) -> dict:
    """A ``message/send`` (non-streaming) JSON-RPC payload."""
    message: dict = {
        "role": "user",
        "parts": [{"text": message_text}],
        "messageId": uuid.uuid4().hex,
    }
    if task_id:
        message["taskId"] = task_id
    return {
        "jsonrpc": "2.0",
        "id": "send-1",
        "method": "message/send",
        "params": {"message": message},
    }


async def _wait_until(predicate, timeout: float = 5.0) -> None:
    async def _poll() -> None:
        while not predicate():
            await asyncio.sleep(0.005)

    await asyncio.wait_for(_poll(), timeout=timeout)


# ── T2.1 — idle session after a finished turn → completed ──────────────────


def test_idle_session_after_finished_turn_reports_completed(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    agent, token_data = setup_a2a_agent(
        client, superuser_token_headers, name="A2A State Idle Agent",
    )
    agent_id = agent["id"]
    a2a_token = token_data["token"]

    events, _ = send_a2a_streaming_message(
        client, agent_id, a2a_token,
        message_text="Hello", response_text="Hi there",
    )
    task_id = extract_task_id(events)
    assert task_id is not None

    body = post_a2a_jsonrpc(
        client, agent_id, a2a_token,
        {"jsonrpc": "2.0", "id": "get-1", "method": "tasks/get", "params": {"id": task_id}},
    )
    assert body["result"]["status"]["state"] == "completed"


# ── T2.2 — turn in flight (gated stub) → working ────────────────────────────


def test_turn_in_flight_reports_working(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    agent, token_data = setup_a2a_agent(
        client, superuser_token_headers, name="A2A State Working Agent",
    )
    agent_id = agent["id"]
    a2a_token = token_data["token"]

    stub = GatedAgentEnvConnector(response_text="reply", label="t")

    async def run():
        with patch("app.services.sessions.message_service.agent_env_connector", stub):
            stream_session = start_a2a_sse(
                client.app, agent_id, a2a_token, build_streaming_request("Hi"),
            )
            await stream_session.wait_for_event_count(1, timeout=5.0)
            task_id = extract_task_id(stream_session.events)
            assert task_id is not None

            # Wait until the turn has genuinely reached the agent-env (lock
            # held, stream registered) before probing — not just until the
            # unconditional pre-lock "working" ack has been sent.
            await _wait_until(lambda: len(stub.stream_calls) >= 1)

            status, body = await post_a2a_json_at(
                client.app, agent_id, a2a_token,
                {"jsonrpc": "2.0", "id": "get-mid", "method": "tasks/get", "params": {"id": task_id}},
            )

            stub.release()
            await asyncio.wait_for(stream_session.done.wait(), timeout=10.0)
            await wait_for_detached_a2a_turns(timeout=10.0)

        return status, body

    status, body = asyncio.run(run())

    assert status == 200
    assert "result" in body, body
    assert body["result"]["status"]["state"] == "working"


# ── T2.3 — tasks/cancel mid-turn → canceled ─────────────────────────────────


def test_cancel_mid_turn_reports_canceled(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Cancels a genuinely in-flight turn (not the already-idle case
    ``test_a2a_cancel.py`` covers) and verifies ``tasks/get`` reflects it as
    ``canceled`` afterward, via the real ``was_interrupted`` → status
    ``user_interrupted`` → ``TaskState.canceled`` path (plan §2.2 rule 6).

    ``forward_interrupt_to_environment`` is mocked (as in ``test_a2a_cancel.py``
    — it would otherwise make a real HTTP call to the agent-env); the mock's
    side effect signals the stub's stream to actually stop and emit an
    ``interrupted`` event, since in production that is exactly what the
    agent-env is expected to do once interrupted.
    """
    agent, token_data = setup_a2a_agent(
        client, superuser_token_headers, name="A2A State Cancel Agent",
    )
    agent_id = agent["id"]
    a2a_token = token_data["token"]

    cancel_requested = asyncio.Event()
    reached_gate = asyncio.Event()
    stream_calls: list[dict] = []

    class InterruptibleConnector:
        async def stream_chat(self, base_url, auth_headers, payload):
            stream_calls.append(payload)
            yield {
                "type": "session_created", "content": "",
                "session_id": str(uuid.uuid4()), "metadata": {},
            }
            # An agent message row is only created once the first assistant
            # content event is processed. Interrupting before that leaves no
            # row for get_last_agent_message_of_current_turn to find, so
            # last_agent_status stays None and the state falls through to
            # rule 7 ("completed") instead of rule 6 ("canceled") — not a
            # bug, just not what this test means to exercise. Yielding the
            # assistant event here, before the gate, guarantees the row
            # exists: the consumer only resumes this generator (past this
            # yield) once it has finished processing that event, including
            # the synchronous row-creation await.
            yield {"type": "assistant", "content": "Partial reply before interruption"}
            reached_gate.set()
            await cancel_requested.wait()
            yield {"type": "interrupted"}

    stub = InterruptibleConnector()

    async def _forward_interrupt_side_effect(*args, **kwargs):
        cancel_requested.set()
        return {"status": "ok"}

    async def run():
        with patch("app.services.sessions.message_service.agent_env_connector", stub):
            stream_session = start_a2a_sse(
                client.app, agent_id, a2a_token, build_streaming_request("Hi"),
            )
            await stream_session.wait_for_event_count(1, timeout=5.0)
            task_id = extract_task_id(stream_session.events)
            assert task_id is not None

            await _wait_until(lambda: reached_gate.is_set())

            with patch(
                "app.services.sessions.message_service.MessageService.forward_interrupt_to_environment",
                AsyncMock(side_effect=_forward_interrupt_side_effect),
            ):
                cancel_status, cancel_body = await post_a2a_json_at(
                    client.app, agent_id, a2a_token,
                    {"jsonrpc": "2.0", "id": "cancel-1", "method": "tasks/cancel", "params": {"id": task_id}},
                )

            await asyncio.wait_for(stream_session.done.wait(), timeout=10.0)
            await wait_for_detached_a2a_turns(timeout=10.0)

            get_status, get_body = await post_a2a_json_at(
                client.app, agent_id, a2a_token,
                {"jsonrpc": "2.0", "id": "get-1", "method": "tasks/get", "params": {"id": task_id}},
            )

        return cancel_status, cancel_body, get_status, get_body

    cancel_status, cancel_body, get_status, get_body = asyncio.run(run())

    assert cancel_status == 200
    assert "result" in cancel_body, cancel_body

    assert get_status == 200
    assert get_body["result"]["status"]["state"] == "canceled"


# ── T2.4 — unanswered question → input-required ─────────────────────────────


def test_unanswered_question_reports_input_required(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    agent, token_data = setup_a2a_agent(
        client, superuser_token_headers, name="A2A State Question Agent",
    )
    agent_id = agent["id"]
    a2a_token = token_data["token"]

    stub = StubAgentEnvConnector(events=[
        {
            "type": "tool", "tool_name": "AskUserQuestion",
            "content": "Which option do you want?", "metadata": {},
        },
        {"type": "done"},
    ])
    request = build_streaming_request("Please ask me something")

    with patch("app.services.sessions.message_service.agent_env_connector", stub):
        resp = client.post(
            f"/api/v1/a2a/{agent_id}/",
            headers={"Authorization": f"Bearer {a2a_token}", "Content-Type": "application/json"},
            json=request,
        )
    drain_tasks()
    assert resp.status_code == 200, resp.text

    events = parse_sse_events(resp.text)
    task_id = extract_task_id(events)
    assert task_id is not None

    body = post_a2a_jsonrpc(
        client, agent_id, a2a_token,
        {"jsonrpc": "2.0", "id": "get-1", "method": "tasks/get", "params": {"id": task_id}},
    )
    assert body["result"]["status"]["state"] == "input-required"


# ── T2.5 — T2.1 on /external/a2a ────────────────────────────────────────────


def test_idle_session_after_finished_turn_reports_completed_on_external_a2a(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db,
) -> None:
    """
    ``tests/api/external/`` has its own conftest that additionally patches
    ``app.api.routes.external_a2a.create_session`` (the external route's own
    import site) — ``a2a_integration``'s conftest does not, since it has no
    reason to know about that route. Without it, the external route's
    session lookups run outside this test's transaction and can't see the
    agent/environment just created here (observed as "agent has no active
    environment"). Patch it locally, just for this test, rather than
    growing ``a2a_integration/conftest.py`` for two tests that borrow
    another domain's route.
    """
    from tests.api.external.external_a2a_agent_test import (
        _EXT_A2A_BASE,
        _create_su_agent,
        _send_streaming_message,
    )
    from tests.utils.fixtures import patched_create_sessions

    with patched_create_sessions(db, ["app.api.routes.external_a2a.create_session"]):
        agent = _create_su_agent(
            client, superuser_token_headers, "External A2A State Idle Agent",
        )
        agent_id = agent["id"]

        resp, events = _send_streaming_message(
            client, superuser_token_headers, agent_id,
            message_text="Hello external", response_text="Hi from external",
        )
        assert resp.status_code == 200
        task_id = extract_task_id(events)
        assert task_id is not None

        r = client.post(
            f"{_EXT_A2A_BASE}/agent/{agent_id}/",
            headers=superuser_token_headers,
            json={"jsonrpc": "2.0", "id": "ext-get-1", "method": "tasks/get", "params": {"id": task_id}},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["result"]["status"]["state"] == "completed"


# ── T2.6 — message/send returns once the turn ends ──────────────────────────


def test_message_send_returns_once_turn_completes(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    ``message/send`` (non-streaming) polls internally until the turn ends.
    Under the test suite's background-task collector that poll would spin
    for the full 300s cap unless something drains the collected
    ``process_pending_messages`` coroutine concurrently — see
    ``post_a2a_json_draining``'s docstring. Wall clock here must stay well
    under the 300s cap (a handful of poll intervals at most).
    """
    agent, token_data = setup_a2a_agent(
        client, superuser_token_headers, name="A2A State Send Agent",
    )
    agent_id = agent["id"]
    a2a_token = token_data["token"]

    stub = StubAgentEnvConnector(response_text="Fast reply via message/send")
    payload = _build_send_request("Hello via message/send")

    async def run():
        loop = asyncio.get_running_loop()
        start = loop.time()
        with patch("app.services.sessions.message_service.agent_env_connector", stub):
            status, body = await post_a2a_json_draining(
                client.app, agent_id, a2a_token, payload, timeout=30.0,
            )
        elapsed = loop.time() - start
        return status, body, elapsed

    status, body, elapsed = asyncio.run(run())

    assert status == 200, body
    assert "result" in body, body
    assert body["result"]["status"]["state"] == "completed"
    assert elapsed < 30.0, (
        f"message/send took {elapsed:.1f}s — should resolve within a few poll "
        f"intervals once the turn is drained, nowhere near the 300s cap"
    )


# ── T2.7 — message/send with an error turn → failed ─────────────────────────


def test_message_send_with_error_turn_reports_failed(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    agent, token_data = setup_a2a_agent(
        client, superuser_token_headers, name="A2A State Send Error Agent",
    )
    agent_id = agent["id"]
    a2a_token = token_data["token"]

    stub = StubAgentEnvConnector(events=[
        {"type": "error", "content": "SDK session not found", "error_type": "SessionNotFound"},
    ])
    payload = _build_send_request("Trigger an error")

    async def run():
        with patch("app.services.sessions.message_service.agent_env_connector", stub):
            return await post_a2a_json_draining(
                client.app, agent_id, a2a_token, payload, timeout=30.0,
            )

    status, body = asyncio.run(run())

    assert status == 200, body
    assert "result" in body, body
    assert body["result"]["status"]["state"] == "failed"


# ── T2.8 — v1.0 GetTask returns the same state ──────────────────────────────


def test_v1_get_task_returns_same_state_as_legacy_tasks_get(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    agent, token_data = setup_a2a_agent(
        client, superuser_token_headers, name="A2A State v1 Parity Agent",
    )
    agent_id = agent["id"]
    a2a_token = token_data["token"]

    events, _ = send_a2a_streaming_message(
        client, agent_id, a2a_token,
        message_text="Hello v1", response_text="Hi v1",
    )
    task_id = extract_task_id(events)
    assert task_id is not None

    legacy_body = post_a2a_jsonrpc(
        client, agent_id, a2a_token,
        {"jsonrpc": "2.0", "id": "get-legacy", "method": "tasks/get", "params": {"id": task_id}},
    )
    v1_body = post_a2a_jsonrpc(
        client, agent_id, a2a_token,
        {"jsonrpc": "2.0", "id": "get-v1", "method": "GetTask", "params": {"id": task_id}},
    )

    assert legacy_body["result"]["status"]["state"] == v1_body["result"]["status"]["state"]
    assert legacy_body["result"]["status"]["state"] == "completed"


# ── T2.9 — turn stopped before any output → canceled, not completed ────────


def test_turn_canceled_before_output_reports_canceled(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    A turn interrupted before the agent produced anything leaves no agent
    row — only the finalize path (``MessageService._finalize_agent_message``)
    creates one, and that only runs on the (non-interrupted) success path,
    or ``_seal_unfinished_turn`` if a row was already opened by an assistant
    event. Neither ran here, since the stream never emitted one.

    Pre-fix, ``tasks/get`` fell through to "no agent row for this turn" and
    reported ``completed``. The fix stamps ``TURN_CANCELED_META_KEY`` on the
    turn's user row from the stream's ``finally`` block
    (``MessageService.mark_turn_canceled_before_output``), and the task
    store reads it back as ``user_interrupted`` -> ``canceled``.
    """
    agent, token_data = setup_a2a_agent(
        client, superuser_token_headers, name="A2A State Canceled Before Output",
    )
    agent_id = agent["id"]
    a2a_token = token_data["token"]

    class InterruptedBeforeOutputConnector:
        async def stream_chat(self, base_url, auth_headers, payload):
            yield {
                "type": "session_created", "content": "",
                "session_id": str(uuid.uuid4()), "metadata": {},
            }
            # No assistant/tool event at all — the agent said nothing before
            # the stop landed, so no agent row is ever opened.
            yield {"type": "interrupted"}

    stub = InterruptedBeforeOutputConnector()
    request = build_streaming_request("Please respond")

    with patch("app.services.sessions.message_service.agent_env_connector", stub):
        resp = client.post(
            f"/api/v1/a2a/{agent_id}/",
            headers={"Authorization": f"Bearer {a2a_token}", "Content-Type": "application/json"},
            json=request,
        )
    drain_tasks()
    assert resp.status_code == 200, resp.text

    events = parse_sse_events(resp.text)
    task_id = extract_task_id(events)
    assert task_id is not None

    body = post_a2a_jsonrpc(
        client, agent_id, a2a_token,
        {"jsonrpc": "2.0", "id": "get-1", "method": "tasks/get", "params": {"id": task_id}},
    )
    assert body["result"]["status"]["state"] == "canceled", (
        f"A turn stopped before any output must report canceled, not "
        f"completed: {body['result']}"
    )


# ── T2.10 — streamed AskUserQuestion → final input-required with parts ─────


def test_streamed_ask_user_question_final_event_is_input_required(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    The final ``done`` event of a turn whose streaming_events include an
    AskUserQuestion tool call must be mapped, live in the SSE stream, to a
    ``status-update`` with ``final: true``, ``state: "input-required"``,
    and the question tool call carried as a Part on ``status.message`` —
    not the generic ``completed`` the "done" event mapped to before this
    fix, and not a bare (message-less) status update either. A follow-up
    ``tasks/get`` on the same session must agree.
    """
    agent, token_data = setup_a2a_agent(
        client, superuser_token_headers, name="A2A State Streamed Question",
    )
    agent_id = agent["id"]
    a2a_token = token_data["token"]

    stub = StubAgentEnvConnector(events=[
        {
            "type": "tool", "tool_name": "AskUserQuestion",
            "content": "Which option do you want?",
            "metadata": {"tool_input": {"question": "Which option do you want?"}},
        },
        {"type": "done"},
    ])
    request = build_streaming_request("Please ask me something")

    with patch("app.services.sessions.message_service.agent_env_connector", stub):
        resp = client.post(
            f"/api/v1/a2a/{agent_id}/",
            headers={"Authorization": f"Bearer {a2a_token}", "Content-Type": "application/json"},
            json=request,
        )
    drain_tasks()
    assert resp.status_code == 200, resp.text

    events = parse_sse_events(resp.text)
    task_id = extract_task_id(events)
    assert task_id is not None

    final_events = [e for e in events if e.get("result", {}).get("final") is True]
    assert len(final_events) == 1, f"Expected exactly one final event, got: {events}"
    final_result = final_events[0]["result"]
    assert final_result["kind"] == "status-update"
    assert final_result["status"]["state"] == "input-required"

    parts = extract_parts_from_sse_event(final_events[0])
    assert len(parts) == 1, f"Expected one question part, got: {parts}"
    assert part_text(parts[0]) == "Which option do you want?"
    meta = part_metadata(parts[0])
    assert meta.get("cinna.content_kind") == "tool"
    assert meta.get("cinna.tool_name") == "AskUserQuestion"
    assert meta.get("cinna.tool_input") == {"question": "Which option do you want?"}

    body = post_a2a_jsonrpc(
        client, agent_id, a2a_token,
        {"jsonrpc": "2.0", "id": "get-1", "method": "tasks/get", "params": {"id": task_id}},
    )
    assert body["result"]["status"]["state"] == "input-required", (
        "tasks/get must agree with the streamed final event"
    )


# ── T2.11 — turn cut off by a crash before any agent row → failed ──────────


def test_turn_orphaned_before_any_agent_row_reports_failed(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db,
) -> None:
    """The backend died after delivering the message and before writing an
    agent row. Once the orphan pass clears the stale ``running`` claim,
    ``tasks/get`` must read the turn as cut off (``failed``), not
    ``completed``. The message row and stale claim are forged: only a killed
    process leaves them (see ``force_session_interaction_claim``).
    """
    from datetime import UTC, datetime, timedelta

    from tests.utils.session import (
        force_delivered_user_message,
        force_session_interaction_claim,
    )
    from tests.utils.status_repair import RepairTick

    agent, token_data = setup_a2a_agent(
        client, superuser_token_headers, name="A2A State Orphaned No Row",
    )
    agent_id = agent["id"]
    a2a_token = token_data["token"]

    events, _ = send_a2a_streaming_message(
        client, agent_id, a2a_token, message_text="Hello", response_text="Hi",
    )
    task_id = extract_task_id(events)

    stale = datetime.now(UTC) - timedelta(minutes=4)
    force_delivered_user_message(db, task_id, content="Run sleep 90")
    force_session_interaction_claim(
        db, task_id,
        interaction_status="running",
        streaming_started_at=stale,
        stream_heartbeat_at=stale,
        set_stream_heartbeat=True,
    )
    assert RepairTick(db).run_orphaned_streams() == 1

    body = post_a2a_jsonrpc(
        client, agent_id, a2a_token,
        {"jsonrpc": "2.0", "id": "get-1", "method": "tasks/get", "params": {"id": task_id}},
    )
    assert body["result"]["status"]["state"] == "failed", body["result"]
    assert body["result"]["history"][-1]["role"] == "user"
