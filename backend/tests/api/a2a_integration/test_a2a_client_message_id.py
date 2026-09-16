"""
Integration tests for C4 client ``messageId`` dedupe (plan:
a2a_crash_recovery_plan.md §4, §6 T4).

Covers both JSON-RPC surfaces (``message/stream`` SSE, ``message/send``
polling) and both A2A routes (``/api/v1/a2a``, ``/external/a2a``). Reuses the
raw-ASGI harness (``tests/utils/a2a_raw_asgi.py``) for the two scenarios that
need a turn genuinely in flight while a duplicate is resent — see that
module's docstring for why a plain synchronous ``client.post`` cannot
produce that, and the documented Rule-1 exception for
``wait_for_detached_a2a_turns``.
"""
from __future__ import annotations

import asyncio
import uuid
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.services.sessions.stream_event_handlers import wait_for_detached_a2a_turns
from tests.stubs.agent_env_stub import StubAgentEnvConnector
from tests.utils.a2a import (
    a2a_headers,
    extract_task_id,
    parse_sse_events,
    post_a2a_jsonrpc,
    setup_a2a_agent,
)
from tests.utils.a2a_raw_asgi import (
    GatedAgentEnvConnector,
    post_a2a_json_draining,
    start_a2a_sse,
)
from tests.utils.background_tasks import drain_tasks
from tests.utils.message import get_messages_by_role
from tests.utils.session import force_pending_user_message


def _stream_request(text: str, *, message_id: str | None, task_id: str | None = None) -> dict:
    message: dict = {"role": "user", "parts": [{"text": text}]}
    if message_id is not None:
        message["messageId"] = message_id
    if task_id:
        message["taskId"] = task_id
    return {
        "jsonrpc": "2.0", "id": "req-1", "method": "message/stream",
        "params": {"message": message},
    }


def _send_request(text: str, *, message_id: str | None, task_id: str | None = None) -> dict:
    message: dict = {"role": "user", "parts": [{"text": text}]}
    if message_id is not None:
        message["messageId"] = message_id
    if task_id:
        message["taskId"] = task_id
    return {
        "jsonrpc": "2.0", "id": "send-1", "method": "message/send",
        "params": {"message": message},
    }


def _post_stream(client: TestClient, agent_id: str, a2a_token: str, payload: dict):
    resp = client.post(
        f"/api/v1/a2a/{agent_id}/", headers=a2a_headers(a2a_token), json=payload,
    )
    drain_tasks()
    assert resp.status_code == 200, resp.text
    return parse_sse_events(resp.text)


# ── T4.1 — client_message_id echoed in history ──────────────────────────────


def test_stream_with_message_id_echoes_client_message_id_in_history(
    client: TestClient, superuser_token_headers: dict[str, str],
) -> None:
    agent, token_data = setup_a2a_agent(
        client, superuser_token_headers, name="A2A ClientId Echo Agent",
    )
    agent_id = agent["id"]
    a2a_token = token_data["token"]

    stub = StubAgentEnvConnector(response_text="Hello back")
    message_id = "client-mid-" + uuid.uuid4().hex

    with patch("app.services.sessions.message_service.agent_env_connector", stub):
        events = _post_stream(client, agent_id, a2a_token, _stream_request("Hi", message_id=message_id))
    task_id = extract_task_id(events)
    assert task_id is not None

    body = post_a2a_jsonrpc(
        client, agent_id, a2a_token,
        {"jsonrpc": "2.0", "id": "get-1", "method": "tasks/get", "params": {"id": task_id}},
    )
    history = body["result"]["history"]
    user_entries = [m for m in history if m["role"] == "user"]
    assert len(user_entries) == 1
    assert user_entries[0]["metadata"]["cinna.client_message_id"] == message_id


# ── T4.2 — resend after completion: no new row, stub not called again ──────


def test_stream_resend_after_completion_is_deduped(
    client: TestClient, superuser_token_headers: dict[str, str],
) -> None:
    agent, token_data = setup_a2a_agent(
        client, superuser_token_headers, name="A2A ClientId Resend Agent",
    )
    agent_id = agent["id"]
    a2a_token = token_data["token"]

    stub = StubAgentEnvConnector(response_text="Hello back")
    message_id = "client-mid-" + uuid.uuid4().hex

    with patch("app.services.sessions.message_service.agent_env_connector", stub):
        events1 = _post_stream(client, agent_id, a2a_token, _stream_request("Hi", message_id=message_id))
        task_id = extract_task_id(events1)
        assert task_id is not None

        events2 = _post_stream(
            client, agent_id, a2a_token,
            _stream_request("Hi", message_id=message_id, task_id=task_id),
        )

    assert len(stub.stream_calls) == 1, "the agent-env must not be called a second time for a resend"

    assert len(events2) == 1, f"a resend after completion must be a single event, got {events2}"
    result = events2[0]["result"]
    assert result["status"]["state"] == "completed"
    assert result["final"] is True

    user_messages = get_messages_by_role(client, superuser_token_headers, task_id, role="user")
    assert len(user_messages) == 1, "a resend must not create a new user row"


def test_stream_resend_after_completion_is_deduped_on_external_a2a(
    client: TestClient, superuser_token_headers: dict[str, str], db,
) -> None:
    from tests.api.external.external_a2a_agent_test import _EXT_A2A_BASE, _create_su_agent
    from tests.utils.fixtures import patched_create_sessions

    with patched_create_sessions(db, ["app.api.routes.external_a2a.create_session"]):
        agent = _create_su_agent(client, superuser_token_headers, "External ClientId Resend Agent")
        agent_id = agent["id"]
        path = f"{_EXT_A2A_BASE}/agent/{agent_id}/"

        stub = StubAgentEnvConnector(response_text="Hello back")
        message_id = "client-mid-" + uuid.uuid4().hex

        with patch("app.services.sessions.message_service.agent_env_connector", stub):
            resp1 = client.post(path, headers=superuser_token_headers, json=_stream_request("Hi", message_id=message_id))
            drain_tasks()
            assert resp1.status_code == 200, resp1.text
            events1 = parse_sse_events(resp1.text)
            task_id = extract_task_id(events1)
            assert task_id is not None

            resp2 = client.post(
                path, headers=superuser_token_headers,
                json=_stream_request("Hi", message_id=message_id, task_id=task_id),
            )
            drain_tasks()
            assert resp2.status_code == 200, resp2.text
            events2 = parse_sse_events(resp2.text)

        assert len(stub.stream_calls) == 1
        assert len(events2) == 1
        assert events2[0]["result"]["status"]["state"] == "completed"


# ── T4.3 — resend while running: one working event, no new row ─────────────


def test_stream_resend_while_running_reports_working_not_final(
    client: TestClient, superuser_token_headers: dict[str, str],
) -> None:
    agent, token_data = setup_a2a_agent(
        client, superuser_token_headers, name="A2A ClientId Resend While Running Agent",
    )
    agent_id = agent["id"]
    a2a_token = token_data["token"]

    stub = GatedAgentEnvConnector(response_text="reply", label="t")
    message_id = "client-mid-" + uuid.uuid4().hex

    async def run():
        with patch("app.services.sessions.message_service.agent_env_connector", stub):
            first = start_a2a_sse(
                client.app, agent_id, a2a_token,
                _stream_request("Hi", message_id=message_id),
            )
            await first.wait_for_event_count(1, timeout=5.0)
            task_id = extract_task_id(first.events)
            assert task_id is not None

            async def _wait_started():
                while not stub.stream_calls:
                    await asyncio.sleep(0.005)
            await asyncio.wait_for(_wait_started(), timeout=5.0)

            # The duplicate is ALSO a message/stream call — it returns SSE,
            # not plain JSON, even on the early-return "duplicate" path.
            dup_session = start_a2a_sse(
                client.app, agent_id, a2a_token,
                _stream_request("Hi", message_id=message_id, task_id=task_id),
            )
            await asyncio.wait_for(dup_session.done.wait(), timeout=10.0)

            stub.release()
            await asyncio.wait_for(first.done.wait(), timeout=10.0)
            await wait_for_detached_a2a_turns(timeout=10.0)

        return task_id, dup_session.events

    task_id, dup_events = asyncio.run(run())

    assert len(dup_events) == 1, f"the duplicate-while-running response must be a single event, got {dup_events}"
    result = dup_events[0]["result"]
    assert result["status"]["state"] == "working"
    assert result["final"] is False

    user_messages = get_messages_by_role(client, superuser_token_headers, task_id, role="user")
    assert len(user_messages) == 1, "resending while running must not create a new user row"


# ── T4.4 — message/send resend: same task, one user row ────────────────────


def test_send_resend_reuses_same_task_and_single_user_row(
    client: TestClient, superuser_token_headers: dict[str, str],
) -> None:
    agent, token_data = setup_a2a_agent(
        client, superuser_token_headers, name="A2A ClientId Send Resend Agent",
    )
    agent_id = agent["id"]
    a2a_token = token_data["token"]

    stub = StubAgentEnvConnector(response_text="Hello via send")
    message_id = "client-mid-" + uuid.uuid4().hex

    async def run():
        with patch("app.services.sessions.message_service.agent_env_connector", stub):
            status1, body1 = await post_a2a_json_draining(
                client.app, agent_id, a2a_token,
                _send_request("Hi", message_id=message_id), timeout=30.0,
            )
            task_id = body1["result"]["id"]

            status2, body2 = await post_a2a_json_draining(
                client.app, agent_id, a2a_token,
                _send_request("Hi", message_id=message_id, task_id=task_id), timeout=30.0,
            )
        return task_id, status1, body1, status2, body2

    task_id, status1, body1, status2, body2 = asyncio.run(run())

    assert status1 == 200 and status2 == 200
    assert body2["result"]["id"] == task_id

    user_messages = get_messages_by_role(client, superuser_token_headers, task_id, role="user")
    assert len(user_messages) == 1


# ── T4.6 — blank / whitespace ids are never deduped ─────────────────────────


def test_blank_and_whitespace_message_ids_are_never_deduped(
    client: TestClient, superuser_token_headers: dict[str, str],
) -> None:
    agent, token_data = setup_a2a_agent(
        client, superuser_token_headers, name="A2A ClientId Blank Agent",
    )
    agent_id = agent["id"]
    a2a_token = token_data["token"]

    stub = StubAgentEnvConnector(response_text="ack")

    with patch("app.services.sessions.message_service.agent_env_connector", stub):
        events1 = _post_stream(client, agent_id, a2a_token, _stream_request("First", message_id="   "))
        task_id = extract_task_id(events1)
        assert task_id is not None

        events2 = _post_stream(
            client, agent_id, a2a_token,
            _stream_request("Second", message_id="   ", task_id=task_id),
        )
        assert extract_task_id(events2) == task_id

    assert len(stub.stream_calls) == 2, "blank ids must never be deduped"
    user_messages = get_messages_by_role(client, superuser_token_headers, task_id, role="user")
    assert len(user_messages) == 2


# ── T4.7 — same id in two different sessions → both stored ─────────────────


def test_same_message_id_in_two_sessions_both_stored(
    client: TestClient, superuser_token_headers: dict[str, str],
) -> None:
    agent, token_data = setup_a2a_agent(
        client, superuser_token_headers, name="A2A ClientId Cross-Session Agent",
    )
    agent_id = agent["id"]
    a2a_token = token_data["token"]

    stub = StubAgentEnvConnector(response_text="ack")
    message_id = "shared-mid-" + uuid.uuid4().hex

    with patch("app.services.sessions.message_service.agent_env_connector", stub):
        events1 = _post_stream(client, agent_id, a2a_token, _stream_request("Session one", message_id=message_id))
        task_id_1 = extract_task_id(events1)
        # No task_id -> a brand new session/task.
        events2 = _post_stream(client, agent_id, a2a_token, _stream_request("Session two", message_id=message_id))
        task_id_2 = extract_task_id(events2)

    assert task_id_1 != task_id_2
    assert len(stub.stream_calls) == 2

    messages_1 = get_messages_by_role(client, superuser_token_headers, task_id_1, role="user")
    messages_2 = get_messages_by_role(client, superuser_token_headers, task_id_2, role="user")
    assert len(messages_1) == 1
    assert len(messages_2) == 1
    assert messages_1[0]["message_metadata"]["client_message_id"] == message_id
    assert messages_2[0]["message_metadata"]["client_message_id"] == message_id


# ── T4.8 — duplicate of a slash command does not re-execute it ─────────────


def test_duplicate_slash_command_does_not_re_execute(
    client: TestClient, superuser_token_headers: dict[str, str],
) -> None:
    agent, token_data = setup_a2a_agent(
        client, superuser_token_headers, name="A2A ClientId Command Agent",
    )
    agent_id = agent["id"]
    a2a_token = token_data["token"]

    message_id = "cmd-mid-" + uuid.uuid4().hex

    events1 = _post_stream(client, agent_id, a2a_token, _stream_request("/files", message_id=message_id))
    task_id = extract_task_id(events1)
    assert task_id is not None
    first_final = events1[-1]["result"]
    assert first_final["status"]["state"] == "completed"

    system_messages_before = get_messages_by_role(client, superuser_token_headers, task_id, role="system")
    count_before = len(system_messages_before)

    events2 = _post_stream(
        client, agent_id, a2a_token,
        _stream_request("/files", message_id=message_id, task_id=task_id),
    )
    assert events2, "a duplicate command send must still get a response, not hang"

    system_messages_after = get_messages_by_role(client, superuser_token_headers, task_id, role="system")
    assert len(system_messages_after) == count_before, (
        "a duplicate of a slash command must not re-execute it (no new system row)"
    )


# ── T4.9 — duplicate of a stuck pending row re-drives delivery once ────────


def test_duplicate_of_stuck_pending_row_redrives_delivery_once(
    client: TestClient, superuser_token_headers: dict[str, str], db,
) -> None:
    """
    A user row stored ``pending`` with no turn ever kicked off for it (e.g.
    an earlier send whose environment activation never completed) is not "a
    message that was already sent" — a duplicate ``message/send`` with the
    same ``client_message_id`` re-drives delivery instead of being treated
    as a no-op resend (plan D9, §4.3's ``redrive`` branch — specific to
    ``handle_message_send``; ``message/stream`` reaches the same row through
    its ordinary "pending → fall through to the locked runner" path, with no
    special-casing needed there). The agent-env must be called exactly
    once: the redrive, not a second independent turn.
    """
    agent, token_data = setup_a2a_agent(
        client, superuser_token_headers, name="A2A ClientId Redrive Agent",
    )
    agent_id = agent["id"]
    a2a_token = token_data["token"]

    # The session must be established through A2A first — a "limited" scope
    # token (setup_a2a_agent's default) can only reach sessions it opened
    # itself, so a session created directly via the web API is rejected.
    setup_stub = StubAgentEnvConnector(response_text="Initial reply")
    with patch("app.services.sessions.message_service.agent_env_connector", setup_stub):
        setup_events = _post_stream(client, agent_id, a2a_token, _stream_request("Setup", message_id=None))
    task_id = extract_task_id(setup_events)
    assert task_id is not None

    message_id = "stuck-mid-" + uuid.uuid4().hex
    force_pending_user_message(
        db, task_id, content="Never delivered", client_message_id=message_id,
    )

    stub = StubAgentEnvConnector(response_text="Redriven reply")

    async def run():
        with patch("app.services.sessions.message_service.agent_env_connector", stub):
            return await post_a2a_json_draining(
                client.app, agent_id, a2a_token,
                _send_request("Never delivered", message_id=message_id, task_id=task_id),
                timeout=30.0,
            )

    status, body = asyncio.run(run())

    assert status == 200, body
    assert len(stub.stream_calls) == 1, "the redrive must call the agent-env exactly once"
    assert body["result"]["status"]["state"] == "completed"

    user_messages = get_messages_by_role(client, superuser_token_headers, task_id, role="user")
    assert len(user_messages) == 2, (
        "the redrive must reuse the stuck row (plus the earlier setup message), not create a new one"
    )
