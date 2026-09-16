"""
Integration tests for C3 history state and the live buffer (plan:
a2a_crash_recovery_plan.md §3.5, §6 T3):

- Every agent row in A2A task history carries ``cinna.message_state``
  (``complete`` / ``canceled`` / ``aborted`` / ``streaming``).
- A ``tasks/get`` on a turn still in flight merges the in-memory live-stream
  buffer into the returned history, beyond what has been durably flushed to
  the DB — and does not itself write anything (the merge works on copies).
- A session whose last agent row is ``aborted`` reports ``failed`` overall
  (plan §2.2 rule 6).

Uses the same raw-ASGI + gated-connector harness as
``test_a2a_crash_recovery.py`` / ``test_a2a_task_state.py`` for the
in-flight scenario — see ``tests/utils/a2a_raw_asgi.py``'s docstring for why,
and the same documented Rule-1 exception for
``wait_for_detached_a2a_turns``.
"""
from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.services.sessions.stream_event_handlers import wait_for_detached_a2a_turns
from tests.utils.a2a import (
    build_streaming_request,
    extract_task_id,
    part_text,
    post_a2a_jsonrpc,
    send_a2a_streaming_message,
    setup_a2a_agent,
)
from tests.utils.a2a_raw_asgi import post_a2a_json_at, start_a2a_sse
from tests.utils.message import get_messages_by_role, get_raw_message_metadata
from tests.utils.session import create_aborted_agent_message


def _agent_history_entry(task: dict, agent_message_id: str) -> dict:
    for entry in task.get("history", []):
        if entry.get("messageId") == agent_message_id:
            return entry
    raise AssertionError(
        f"agent message {agent_message_id} not found in history: {task.get('history')}"
    )


def _get_task(client: TestClient, agent_id: str, a2a_token: str, task_id: str) -> dict:
    body = post_a2a_jsonrpc(
        client, agent_id, a2a_token,
        {"jsonrpc": "2.0", "id": "get-1", "method": "tasks/get", "params": {"id": task_id}},
    )
    assert "result" in body, body
    return body["result"]


# ── cinna.message_state across complete / canceled / aborted / streaming ───


def test_message_state_is_complete_for_a_finished_turn(
    client: TestClient, superuser_token_headers: dict[str, str],
) -> None:
    agent, token_data = setup_a2a_agent(
        client, superuser_token_headers, name="A2A History Complete Agent",
    )
    agent_id = agent["id"]
    a2a_token = token_data["token"]

    events, _ = send_a2a_streaming_message(
        client, agent_id, a2a_token, message_text="Hi", response_text="Hello",
    )
    task_id = extract_task_id(events)
    assert task_id is not None

    agent_messages = get_messages_by_role(client, superuser_token_headers, task_id, role="agent")
    assert len(agent_messages) == 1

    task = _get_task(client, agent_id, a2a_token, task_id)
    entry = _agent_history_entry(task, agent_messages[0]["id"])
    assert entry["metadata"]["cinna.message_state"] == "complete"


def test_message_state_is_canceled_for_an_interrupted_turn(
    client: TestClient, superuser_token_headers: dict[str, str],
) -> None:
    agent, token_data = setup_a2a_agent(
        client, superuser_token_headers, name="A2A History Canceled Agent",
    )
    agent_id = agent["id"]
    a2a_token = token_data["token"]

    cancel_requested = asyncio.Event()
    reached_gate = asyncio.Event()

    class InterruptibleConnector:
        async def stream_chat(self, base_url, auth_headers, payload):
            yield {
                "type": "session_created", "content": "",
                "session_id": str(uuid.uuid4()), "metadata": {},
            }
            yield {"type": "assistant", "content": "Partial before cancel"}
            reached_gate.set()
            await cancel_requested.wait()
            yield {"type": "interrupted"}

    stub = InterruptibleConnector()

    async def _forward_interrupt(*args, **kwargs):
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

            async def _wait_gate():
                while not reached_gate.is_set():
                    await asyncio.sleep(0.005)
            await asyncio.wait_for(_wait_gate(), timeout=5.0)

            with patch(
                "app.services.sessions.message_service.MessageService.forward_interrupt_to_environment",
                AsyncMock(side_effect=_forward_interrupt),
            ):
                await post_a2a_json_at(
                    client.app, agent_id, a2a_token,
                    {"jsonrpc": "2.0", "id": "cancel-1", "method": "tasks/cancel", "params": {"id": task_id}},
                )
            await asyncio.wait_for(stream_session.done.wait(), timeout=10.0)
            await wait_for_detached_a2a_turns(timeout=10.0)
        return task_id

    task_id = asyncio.run(run())

    agent_messages = get_messages_by_role(client, superuser_token_headers, task_id, role="agent")
    assert len(agent_messages) == 1
    assert agent_messages[0]["status"] == "user_interrupted"

    task = _get_task(client, agent_id, a2a_token, task_id)
    entry = _agent_history_entry(task, agent_messages[0]["id"])
    assert entry["metadata"]["cinna.message_state"] == "canceled"
    # A canceled row with a recorded streaming trace carries the event's own
    # parts, not an empty placeholder part — the placeholder-suppression
    # rule only kicks in when the stored content IS the finalize placeholder
    # (see test_message_state_aborted_placeholder_content_renders_empty_part).
    all_text = "".join(
        (p.get("text") or (p.get("root") or {}).get("text", "")) for p in entry["parts"]
    )
    assert "Partial before cancel" in all_text


def test_message_state_aborted_placeholder_content_renders_empty_part(
    client: TestClient, superuser_token_headers: dict[str, str], db,
) -> None:
    """
    An aborted/canceled agent row whose stored content is the finalize
    placeholder ("Agent response" — what ``MessageService`` writes when the
    stream produced no assistant text) and whose events yield no parts must
    render an empty text part, not the placeholder string. Showing the
    literal placeholder to an A2A client would look like a real (empty but
    present) response, when the turn actually produced nothing.
    """
    agent, token_data = setup_a2a_agent(
        client, superuser_token_headers, name="A2A History Placeholder Agent",
    )
    agent_id = agent["id"]
    a2a_token = token_data["token"]

    events, _ = send_a2a_streaming_message(
        client, agent_id, a2a_token, message_text="Hi", response_text="First reply",
    )
    task_id = extract_task_id(events)

    # content= the exact finalize placeholder; default streaming_events=[]
    # (see create_aborted_agent_message) — no events to derive parts from.
    created = create_aborted_agent_message(db, task_id, content="Agent response")

    task = _get_task(client, agent_id, a2a_token, task_id)
    entry = _agent_history_entry(task, created["id"])
    assert entry["metadata"]["cinna.message_state"] == "aborted"
    assert len(entry["parts"]) == 1
    assert part_text(entry["parts"][0]) == "", (
        f"Expected an empty text part, not the placeholder: {entry['parts']}"
    )


def test_message_state_is_aborted_for_a_sealed_partial_turn(
    client: TestClient, superuser_token_headers: dict[str, str], db,
) -> None:
    """A finalized ``aborted`` row (e.g. sealed by the orphan pass or a raw
    task cancel) reports ``cinna.message_state == "aborted"`` in history.
    """
    agent, token_data = setup_a2a_agent(
        client, superuser_token_headers, name="A2A History Aborted Agent",
    )
    agent_id = agent["id"]
    a2a_token = token_data["token"]

    # Establish a session first, via a trivial completed turn, then seal a
    # second, separately-created agent row as aborted directly (the DB seam
    # every "process died mid-stream" scenario in this suite uses — see
    # tests/utils/session.py::create_aborted_agent_message).
    events, _ = send_a2a_streaming_message(
        client, agent_id, a2a_token, message_text="Hi", response_text="First reply",
    )
    task_id = extract_task_id(events)

    created = create_aborted_agent_message(db, task_id)

    task = _get_task(client, agent_id, a2a_token, task_id)
    entry = _agent_history_entry(task, created["id"])
    assert entry["metadata"]["cinna.message_state"] == "aborted"


# ── In-flight tasks/get merges the live buffer, without writing to the DB ──


def test_in_flight_tasks_get_merges_live_events_without_writing_to_db(
    client: TestClient, superuser_token_headers: dict[str, str], db,
) -> None:
    agent, token_data = setup_a2a_agent(
        client, superuser_token_headers, name="A2A History Live Buffer Agent",
    )
    agent_id = agent["id"]
    a2a_token = token_data["token"]

    reached_gate = asyncio.Event()
    release = asyncio.Event()

    class MultiEventConnector:
        async def stream_chat(self, base_url, auth_headers, payload):
            yield {
                "type": "session_created", "content": "",
                "session_id": str(uuid.uuid4()), "metadata": {},
            }
            # First chunk creates the agent row.
            yield {"type": "assistant", "content": "First chunk. "}
            # These arrive well inside the 2s flush interval: they land in
            # the in-memory live buffer but are not (yet) persisted.
            yield {"type": "assistant", "content": "Second chunk. "}
            yield {"type": "assistant", "content": "Third chunk."}
            reached_gate.set()
            await release.wait()
            yield {"type": "done"}

    stub = MultiEventConnector()

    async def run():
        with patch("app.services.sessions.message_service.agent_env_connector", stub):
            stream_session = start_a2a_sse(
                client.app, agent_id, a2a_token, build_streaming_request("Hi"),
            )
            await stream_session.wait_for_event_count(1, timeout=5.0)
            task_id = extract_task_id(stream_session.events)
            assert task_id is not None

            async def _wait_gate():
                while not reached_gate.is_set():
                    await asyncio.sleep(0.005)
            await asyncio.wait_for(_wait_gate(), timeout=5.0)

            agent_messages_before = get_messages_by_role(
                client, superuser_token_headers, task_id, role="agent",
            )
            assert len(agent_messages_before) == 1
            message_id = agent_messages_before[0]["id"]
            metadata_before = get_raw_message_metadata(db, message_id)

            status1, live_task = await post_a2a_json_at(
                client.app, agent_id, a2a_token,
                {"jsonrpc": "2.0", "id": "get-live", "method": "tasks/get", "params": {"id": task_id}},
            )

            metadata_after = get_raw_message_metadata(db, message_id)

            release.set()
            await asyncio.wait_for(stream_session.done.wait(), timeout=10.0)
            await wait_for_detached_a2a_turns(timeout=10.0)

        return task_id, message_id, live_task, metadata_before, metadata_after

    task_id, message_id, live_task, metadata_before, metadata_after = asyncio.run(run())

    # The live merge surfaced content beyond the single flushed/initial chunk.
    entry = _agent_history_entry(live_task["result"], message_id)
    all_text = "".join(
        (p.get("text") or (p.get("root") or {}).get("text", "")) for p in entry["parts"]
    )
    assert "Second chunk" in all_text
    assert "Third chunk" in all_text

    # The read itself did not write anything to the DB row.
    assert metadata_before == metadata_after, (
        "tasks/get must merge live events on copies, never mutate the stored row"
    )


# ── tasks/get of a session whose last row is aborted → failed ──────────────


def test_tasks_get_of_session_with_aborted_last_row_reports_failed(
    client: TestClient, superuser_token_headers: dict[str, str], db,
) -> None:
    agent, token_data = setup_a2a_agent(
        client, superuser_token_headers, name="A2A Aborted Last Row Agent",
    )
    agent_id = agent["id"]
    a2a_token = token_data["token"]

    events, _ = send_a2a_streaming_message(
        client, agent_id, a2a_token, message_text="Hi", response_text="First reply",
    )
    task_id = extract_task_id(events)

    create_aborted_agent_message(db, task_id)

    body = post_a2a_jsonrpc(
        client, agent_id, a2a_token,
        {"jsonrpc": "2.0", "id": "get-1", "method": "tasks/get", "params": {"id": task_id}},
    )
    assert body["result"]["status"]["state"] == "failed"
