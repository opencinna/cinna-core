"""
Integration tests for A2A ``tasks/cancel`` (aka v1.0 ``CancelTask``).

Covers the fix that routes cancel through ``MessageService.interrupt_stream``
— the same end-to-end path the UI interrupt button uses — so the interrupt
is actually forwarded to the agent-env (not just flagged on the backend).

Also covers the follow-up fix that makes ``tasks/cancel`` return the Task
(same shape as ``tasks/get``, v1-transformed on v1 requests) instead of
``{}``: a stream that was just interrupted reports ``status.state ==
"canceled"`` immediately, because the task store checks
``active_streaming_manager.is_interrupt_requested_nowait`` before falling
through to the ``interaction_status == "running"`` rule.

Scenarios:
  1. **Happy path** — a session with a known external_session_id is
     actively streaming; ``CancelTask`` triggers an HTTP forward to the
     agent-env at ``/chat/interrupt/{external_session_id}``, and both the
     cancel response and a follow-up ``tasks/get`` report ``canceled``.
  2. **Idempotent no-op** — calling ``tasks/cancel`` on a session with no
     active stream returns success with the task's *current* state (per
     A2A spec semantics), without contacting the agent-env.
  3. **Unknown task** — cancel against a nonexistent task id returns a
     JSON-RPC error, not success.
  4. **v1.0 shape** — ``CancelTask`` (the v1.0 method name) returns a
     v1-transformed Task, matching legacy ``tasks/cancel``'s state.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.a2a import (
    a2a_headers,
    post_a2a_jsonrpc,
    send_a2a_streaming_message,
    setup_a2a_agent,
)
from tests.utils.session import (
    get_agent_session,
    register_active_stream,
    unregister_active_stream,
)


def _cancel_request(task_id: str, req_id: str = "cancel-1") -> dict:
    """Build a v1.0 CancelTask JSON-RPC payload."""
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "method": "CancelTask",
        "params": {"id": task_id},
    }


def _legacy_cancel_request(task_id: str, req_id: str = "cancel-1") -> dict:
    """Build a legacy (v0.3) ``tasks/cancel`` JSON-RPC payload."""
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "method": "tasks/cancel",
        "params": {"id": task_id},
    }


# ---------------------------------------------------------------------------
# 1. Happy path: cancel forwards interrupt to agent-env
# ---------------------------------------------------------------------------

def test_a2a_cancel_forwards_interrupt_to_agent_env(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Full cancel flow: once a session exists with a known external_session_id
    and is registered in ``ActiveStreamingManager``, ``CancelTask`` must
    POST to the agent-env's ``/chat/interrupt/{external_session_id}``
    endpoint.

    Pre-fix behavior: the backend flag was flipped but no HTTP call was
    made, so the agent-env kept running — a silent no-op cancel. This
    test guards against regression.

    Also covers: the cancel response itself now carries the Task with
    ``status.state == "canceled"`` (not ``{}``) — the task store's
    ``is_interrupt_requested_nowait`` check reports canceled immediately,
    before the agent-env has actually wound the stream down — and a
    follow-up ``tasks/get`` on the same session agrees.
    """
    # ── Setup: agent + session ──────────────────────────────────────────
    agent, token_data = setup_a2a_agent(
        client, superuser_token_headers, name="A2A Cancel Agent",
    )
    agent_id = agent["id"]
    a2a_token = token_data["token"]

    # Establish a session via a streaming message.
    events, _ = send_a2a_streaming_message(
        client, agent_id, a2a_token,
        message_text="Initial message",
        response_text="Initial reply",
    )
    task_id = events[0]["result"]["taskId"]

    # Session should exist server-side and match the A2A taskId.
    session = get_agent_session(client, superuser_token_headers, agent_id)
    assert session["id"] == task_id

    # ── Arrange: register an active stream with a known external id ──────
    # The real stream completed synchronously during the test call, so we
    # simulate "still streaming" by re-registering via the test utility.
    session_uuid = uuid.UUID(task_id)
    external_session_id = "ext-session-abc-123"

    register_active_stream(session_uuid, external_session_id)

    try:
        # ── Act: send CancelTask, with the env forward call mocked ──────
        forward_mock = AsyncMock(return_value={"status": "ok"})
        with patch(
            "app.services.sessions.message_service.MessageService.forward_interrupt_to_environment",
            forward_mock,
        ):
            response = post_a2a_jsonrpc(
                client, agent_id, a2a_token, _cancel_request(task_id),
            )

        # ── Assert: JSON-RPC success, result is the Task, canceled ──────
        assert response["jsonrpc"] == "2.0"
        assert response["id"] == "cancel-1"
        assert "result" in response, f"Expected result, got error: {response}"
        assert "error" not in response
        assert response["result"]["status"]["state"] == "canceled", (
            f"Expected a canceled Task, got: {response['result']}"
        )

        # ── Assert: the agent-env forward actually happened ─────────────
        assert forward_mock.await_count == 1, (
            "CancelTask must POST to the agent-env; the pre-fix bug skipped "
            "this call and made cancels silent no-ops"
        )
        call_kwargs = forward_mock.await_args.kwargs
        assert call_kwargs.get("external_session_id") == external_session_id, (
            f"Forward called with wrong external_session_id: {call_kwargs}"
        )
        # base_url / auth_headers come from the environment config — just
        # assert they were provided (not None / empty-path).
        assert call_kwargs.get("base_url")

        # ── Assert: a follow-up tasks/get agrees (same in-flight rule) ──
        get_body = post_a2a_jsonrpc(
            client, agent_id, a2a_token,
            {"jsonrpc": "2.0", "id": "get-1", "method": "tasks/get", "params": {"id": task_id}},
        )
        assert get_body["result"]["status"]["state"] == "canceled"
    finally:
        # Clean up — even on assertion failure, don't leak streams across tests.
        unregister_active_stream(session_uuid)


# ---------------------------------------------------------------------------
# 2. Idempotent no-op when there's nothing to cancel
# ---------------------------------------------------------------------------

def test_a2a_cancel_is_idempotent_when_no_active_stream(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Per the A2A spec, cancelling a task that is no longer running must be
    treated as a best-effort no-op, not an error. This test exercises
    the common case where a task has already completed by the time the
    client sends ``CancelTask``.

    Expected: JSON-RPC success whose result is the Task in its *current*
    state (``completed`` here, not ``{}``) and NO call to the agent-env
    interrupt endpoint (nothing to interrupt).
    """
    # ── Setup: create a session, let it finish ──────────────────────────
    agent, token_data = setup_a2a_agent(
        client, superuser_token_headers, name="A2A Cancel Idempotent",
    )
    agent_id = agent["id"]
    a2a_token = token_data["token"]

    events, _ = send_a2a_streaming_message(
        client, agent_id, a2a_token,
        message_text="One-off message",
        response_text="Done",
    )
    task_id = events[0]["result"]["taskId"]

    # At this point the stream completed and was unregistered by the
    # stream processor's finalize step — there is no active stream.

    # ── Act: CancelTask, expecting idempotent success ───────────────────
    forward_mock = AsyncMock(return_value={"status": "ok"})
    with patch(
        "app.services.sessions.message_service.MessageService.forward_interrupt_to_environment",
        forward_mock,
    ):
        response = post_a2a_jsonrpc(
            client, agent_id, a2a_token, _cancel_request(task_id),
        )

    # ── Assert: success with the task's current state, no env forward ───
    assert response.get("jsonrpc") == "2.0"
    assert response.get("id") == "cancel-1"
    assert "result" in response, (
        f"CancelTask on a non-streaming task must be idempotent success, "
        f"got: {response}"
    )
    assert response["result"]["status"]["state"] == "completed", (
        f"Expected the task's current (completed) state, not an empty "
        f"result: {response['result']!r}"
    )
    assert forward_mock.await_count == 0, (
        "Nothing to interrupt — agent-env forward should NOT have been called"
    )


# ---------------------------------------------------------------------------
# 3. Cancel with unknown task id
# ---------------------------------------------------------------------------

def test_a2a_cancel_rejects_unknown_task(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Cancel against a task id that doesn't exist returns a JSON-RPC error
    (not a silent success). "Unknown task" is distinct from "task that
    already finished" — the former is malformed input, the latter is the
    idempotency case covered above.
    """
    agent, token_data = setup_a2a_agent(
        client, superuser_token_headers, name="A2A Cancel Unknown",
    )
    agent_id = agent["id"]
    a2a_token = token_data["token"]

    bogus_task_id = str(uuid.uuid4())

    response = post_a2a_jsonrpc(
        client, agent_id, a2a_token, _cancel_request(bogus_task_id),
    )

    assert response.get("jsonrpc") == "2.0"
    assert response.get("id") == "cancel-1"
    assert "error" in response, (
        f"CancelTask on unknown task must return an error, got: {response}"
    )
    # -32001: application error (task not found)
    assert response["error"]["code"] == -32001


# ---------------------------------------------------------------------------
# 4. v1.0 CancelTask returns a v1-shaped Task, matching legacy tasks/cancel
# ---------------------------------------------------------------------------

def test_a2a_cancel_v1_request_returns_v1_shaped_task(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    The explicit v1.0 endpoint (``/a2a/v1.0/{agent_id}/``, PascalCase
    ``CancelTask``) and the legacy v0.3 endpoint (``/a2a/v0.3/{agent_id}/``,
    slash-case ``tasks/cancel``) must report the same task state.
    ``A2AV1Adapter.transform_task_outbound`` ensures the v1 result carries
    the ``kind: "task"`` discriminator (already present on the underlying
    a2a-sdk ``Task`` model, so this also guards that the adapter doesn't
    strip or rename it).
    """
    agent, token_data = setup_a2a_agent(
        client, superuser_token_headers, name="A2A Cancel v1 Shape Agent",
    )
    agent_id = agent["id"]
    a2a_token = token_data["token"]

    events, _ = send_a2a_streaming_message(
        client, agent_id, a2a_token,
        message_text="Hello", response_text="Hi",
    )
    task_id = events[0]["result"]["taskId"]
    # Turn already completed — no active stream, so both calls exercise the
    # idempotent "current state" path, not the mid-stream canceled path.

    v1_resp = client.post(
        f"{settings.API_V1_STR}/a2a/v1.0/{agent_id}/",
        headers=a2a_headers(a2a_token),
        json=_cancel_request(task_id, req_id="cancel-v1"),
    )
    assert v1_resp.status_code == 200, v1_resp.text
    v1_body = v1_resp.json()
    assert "result" in v1_body, f"Expected result, got error: {v1_body}"
    assert v1_body["result"]["kind"] == "task"
    assert v1_body["result"]["status"]["state"] == "completed"

    legacy_resp = client.post(
        f"{settings.API_V1_STR}/a2a/v0.3/{agent_id}/",
        headers=a2a_headers(a2a_token),
        json=_legacy_cancel_request(task_id, req_id="cancel-legacy"),
    )
    assert legacy_resp.status_code == 200, legacy_resp.text
    legacy_body = legacy_resp.json()
    assert "result" in legacy_body, f"Expected result, got error: {legacy_body}"
    assert legacy_body["result"]["status"]["state"] == v1_body["result"]["status"]["state"], (
        "v1.0 CancelTask and legacy tasks/cancel must agree on task state"
    )
