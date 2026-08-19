"""
Integration tests: same-session stream concurrency serialization.

Covers the bug fix in ``MessageService.process_pending_messages``
(see docs/plans/session_stream_concurrency_serialization_fix_plan.md):
two near-simultaneous sends to the SAME session used to spawn
independent, uncoordinated ``process_pending_messages`` background
tasks (the UI/web path opted out of the shared per-session lock). The
shorter stream's finalizers would reset ``interaction_status`` and fire
``session_interaction_status_changed`` while the longer stream kept
working for minutes — a false "done" in the frontend. The fix wraps the
ENTIRE body of ``process_pending_messages`` — including the ``finally``
teardown that clears ``interaction_status`` — in the shared
``get_session_lock`` (wait mode), so a second same-session send is
queued behind the first and its still-``pending`` message is collected
by the second processor run only after the first has fully finished.

Harness constraint (see backend/tests/README.md "Testing Session-Driven
Flows" and the plan's §6.1): ``drain_tasks()`` runs each collected
background task sequentially, each in its OWN ``asyncio.run()`` loop —
genuine event-loop interleaving of two same-session
``process_pending_messages`` calls is NOT reproducible through the API
harness. These tests therefore assert on **observable invariants** for
near-simultaneous sends (no message stranded, correct final idle
state, preserved message order, no duplicated/corrupted sequence
numbers) rather than forcing a race.

One consequence of the harness being sequential: by the time
``drain_tasks()`` runs the first background task, BOTH pending user
messages already exist in the DB (both POSTs happened before either
task ran). ``MessageService.collect_pending_batches`` collects all
currently-pending same-routing messages into a single contiguous batch
(pre-existing batching behavior, unrelated to this lock fix), so the
observed shape for "near-simultaneous sends" here is one combined LLM
turn carrying both messages, not two independent turns. That is a
correct outcome to assert on: it demonstrates the second message is
never left ``sent_to_agent_status == "pending"`` (never stranded) and
the session ends in a genuinely idle state — not a batching regression.

The test that actually proves the lock serializes two OVERLAPPING calls
on one real event loop (including the teardown clear happening before
the next call can proceed) lives in
``tests/unit/test_session_stream_concurrency_lock.py`` — see that
file's docstring for why the real concurrency assertion needs to live
outside the API-only harness.
"""
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.stubs.agent_env_stub import StubAgentEnvConnector
from tests.utils.agent import create_agent_via_api
from tests.utils.background_tasks import drain_tasks
from tests.utils.message import list_messages
from tests.utils.session import create_session_via_api, get_session

API = settings.API_V1_STR
_STREAM_PATCH_TARGET = "app.services.sessions.message_service.agent_env_connector"


# ── Helpers ───────────────────────────────────────────────────────────────


def _setup_agent_and_session(
    client: TestClient,
    headers: dict[str, str],
) -> tuple[str, str]:
    """
    Create an agent (with a running environment) and a conversation session.
    Returns (agent_id, session_id).
    """
    agent = create_agent_via_api(client, headers, name="Stream Concurrency Test Agent")
    drain_tasks()
    # Re-fetch to get active_environment_id
    r = client.get(f"{API}/agents/{agent['id']}", headers=headers)
    agent = r.json()
    agent_id = agent["id"]

    session = create_session_via_api(client, headers, agent_id, mode="conversation")
    return agent_id, session["id"]


def _post_message(
    client: TestClient,
    headers: dict[str, str],
    session_id: str,
    content: str,
) -> dict:
    r = client.post(
        f"{API}/sessions/{session_id}/messages/stream",
        headers=headers,
        json={"content": content, "file_ids": []},
    )
    assert r.status_code == 200, f"POST stream failed: {r.text}"
    return r.json()


def _get_streaming_status(
    client: TestClient,
    headers: dict[str, str],
    session_id: str,
) -> dict:
    r = client.get(
        f"{API}/sessions/{session_id}/messages/streaming-status",
        headers=headers,
    )
    assert r.status_code == 200, f"GET streaming-status failed: {r.text}"
    return r.json()


# ── Test 1: single-message happy path (regression guard) ──────────────────


def test_single_message_happy_path_unchanged(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    One send -> one stream -> interaction_status transitions back to "",
    exactly one agent reply, session status "completed". Confirms the
    lock adds no behavioral change for the uncontended, common case
    (an uncontended asyncio.Lock acquire/release is effectively free).
    """
    _, session_id = _setup_agent_and_session(client, superuser_token_headers)
    stub = StubAgentEnvConnector(response_text="Hello there.")

    with patch(_STREAM_PATCH_TARGET, stub):
        _post_message(client, superuser_token_headers, session_id, "Hi")
        drain_tasks()

    assert len(stub.stream_calls) == 1

    messages = list_messages(client, superuser_token_headers, session_id)
    user_msgs = [m for m in messages if m["role"] == "user"]
    agent_msgs = [m for m in messages if m["role"] == "agent"]
    assert len(user_msgs) == 1
    assert user_msgs[0]["sent_to_agent_status"] == "sent"
    assert len(agent_msgs) == 1

    session = get_session(client, superuser_token_headers, session_id)
    assert session["interaction_status"] == ""
    assert session["pending_messages_count"] == 0
    assert session["status"] == "completed"

    status = _get_streaming_status(client, superuser_token_headers, session_id)
    assert status["is_streaming"] is False


# ── Test 2: near-simultaneous sends — second message never stranded ───────


def test_near_simultaneous_sends_second_message_not_stranded(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Two sends to the same session, both posted BEFORE either is drained
    (mirrors two browser tabs / a double-send racing ``initiate_stream``,
    which spawns one ``process_pending_messages`` background task per send
    with no "already processing this session" check):

      1. Both POSTs succeed immediately; both schedule a background task.
      2. After draining, NEITHER user message is left
         ``sent_to_agent_status == "pending"`` — the core "never
         stranded" requirement the fix guarantees.
      3. The session ends in a correct, fully idle state — not a
         premature/false "done" while work remained (the reported bug):
         interaction_status == "", pending_messages_count == 0, and the
         streaming-status endpoint reports not-streaming.
      4. No content is dropped: both message texts reach the agent-env,
         in order, and every LLM turn produced exactly one agent reply.
    """
    _, session_id = _setup_agent_and_session(client, superuser_token_headers)
    stub = StubAgentEnvConnector(response_text="Got both.")

    with patch(_STREAM_PATCH_TARGET, stub):
        # Both sends happen BEFORE any draining -- both background tasks
        # are captured while the session still has the other message
        # pending, mirroring two near-simultaneous same-session sends.
        _post_message(client, superuser_token_headers, session_id, "First message")
        _post_message(client, superuser_token_headers, session_id, "Second message")
        drain_tasks()

    messages = list_messages(client, superuser_token_headers, session_id)
    user_msgs = sorted(
        (m for m in messages if m["role"] == "user"),
        key=lambda m: m["sequence_number"],
    )
    agent_msgs = [m for m in messages if m["role"] == "agent"]

    assert len(user_msgs) == 2, f"Expected 2 user messages, got {len(user_msgs)}"
    assert user_msgs[0]["content"] == "First message"
    assert user_msgs[1]["content"] == "Second message"
    # Core requirement: neither message is left stranded as "pending".
    assert all(m["sent_to_agent_status"] == "sent" for m in user_msgs), (
        f"A message was left un-sent: "
        f"{[m['sent_to_agent_status'] for m in user_msgs]}"
    )
    # One agent reply per LLM turn actually taken -- ties the reply count
    # to what really happened at the connector instead of a hardcoded
    # guess about internal batching.
    assert len(agent_msgs) == len(stub.stream_calls) and len(agent_msgs) >= 1, (
        f"Expected one agent reply per stream call, got "
        f"{len(agent_msgs)} replies / {len(stub.stream_calls)} calls"
    )

    # Both message texts reach the agent-env, in order (whether the
    # harness's pre-existing pending-message batching combines them into
    # one call, or they land in separate calls, order must be preserved
    # and nothing may be dropped).
    combined_payload = " ".join(call["payload"]["message"] for call in stub.stream_calls)
    first_idx = combined_payload.find("First message")
    second_idx = combined_payload.find("Second message")
    assert first_idx != -1 and second_idx != -1, (
        f"Both message texts must reach the agent-env: {combined_payload!r}"
    )
    assert first_idx < second_idx, (
        "First message's content must appear before second message's "
        "content in the agent-env payload (message order preserved)"
    )

    # Final state: correct idle state, not a false "done" left over as if
    # a still-working second stream had been silently abandoned.
    session = get_session(client, superuser_token_headers, session_id)
    assert session["interaction_status"] == "", (
        f"Session left in non-idle interaction_status: "
        f"{session['interaction_status']!r}"
    )
    assert session["pending_messages_count"] == 0
    assert session["status"] == "completed"

    status = _get_streaming_status(client, superuser_token_headers, session_id)
    assert status["is_streaming"] is False, (
        "streaming-status must report not-streaming once both messages "
        "are fully processed"
    )


# ── Test 3: sequential rounds — no sequence/state corruption ──────────────


def test_sequential_sends_across_rounds_preserve_sequence_and_state(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Three separate send -> drain rounds to the same session. Each round
    acquires and releases the per-session lock uncontended. Regression
    guard for message ordering / role alternation / no duplicate or
    corrupted sequence numbers across repeated lock acquisitions on the
    same session (the lock must never leak or get stuck held).
    """
    _, session_id = _setup_agent_and_session(client, superuser_token_headers)

    for i in range(3):
        stub = StubAgentEnvConnector(response_text=f"Reply {i}")
        with patch(_STREAM_PATCH_TARGET, stub):
            _post_message(client, superuser_token_headers, session_id, f"Message {i}")
            drain_tasks()
        assert len(stub.stream_calls) == 1, f"Round {i}: expected exactly one stream call"

        session = get_session(client, superuser_token_headers, session_id)
        assert session["interaction_status"] == "", f"Round {i}: lock/state not released"
        assert session["status"] == "completed"

    messages = list_messages(client, superuser_token_headers, session_id)
    user_msgs = sorted(
        (m for m in messages if m["role"] == "user"),
        key=lambda m: m["sequence_number"],
    )
    agent_msgs = [m for m in messages if m["role"] == "agent"]

    assert len(user_msgs) == 3
    assert len(agent_msgs) == 3
    assert all(m["sent_to_agent_status"] == "sent" for m in user_msgs)
    assert [m["content"] for m in user_msgs] == ["Message 0", "Message 1", "Message 2"]

    # sequence_number is strictly increasing across the whole session --
    # no interleaving/duplication introduced by repeated lock acquisition.
    all_seqs = [m["sequence_number"] for m in messages]
    assert all_seqs == sorted(all_seqs), "Message sequence numbers are not monotonic"
    assert len(all_seqs) == len(set(all_seqs)), "Duplicate sequence numbers detected"
