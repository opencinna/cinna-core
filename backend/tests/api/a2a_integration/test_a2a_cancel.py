"""
Integration tests for A2A ``tasks/cancel`` (aka v1.0 ``CancelTask``).

Covers the fix that routes cancel through ``MessageService.interrupt_stream``
— the same end-to-end path the UI interrupt button uses — so the interrupt
is actually forwarded to the agent-env (not just flagged on the backend).

Scenarios:
  1. **Happy path** — a session with a known external_session_id is
     actively streaming; ``CancelTask`` triggers an HTTP forward to the
     agent-env at ``/chat/interrupt/{external_session_id}``.
  2. **Idempotent no-op** — calling ``CancelTask`` on a session with no
     active stream returns ``{}`` success (per A2A spec semantics),
     without contacting the agent-env.
  3. **Unknown task** — cancel against a nonexistent task id returns a
     JSON-RPC error, not success.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.a2a import (
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

        # ── Assert: JSON-RPC success, result is an empty dict ───────────
        assert response["jsonrpc"] == "2.0"
        assert response["id"] == "cancel-1"
        assert "result" in response, f"Expected result, got error: {response}"
        assert "error" not in response

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

    Expected: JSON-RPC ``result: {}`` and NO call to the agent-env
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

    # ── Assert: success with empty result, no env forward ───────────────
    assert response.get("jsonrpc") == "2.0"
    assert response.get("id") == "cancel-1"
    assert "result" in response, (
        f"CancelTask on a non-streaming task must be idempotent success, "
        f"got: {response}"
    )
    assert response["result"] == {} or response["result"] is None, (
        f"Expected empty result dict, got {response['result']!r}"
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
