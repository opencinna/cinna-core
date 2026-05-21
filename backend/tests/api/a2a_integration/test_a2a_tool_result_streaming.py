"""
Integration tests: A2A tool_result_delta streaming and history replay.

Verifies end-to-end that tool_result_delta events (stdout/stderr chunks from
CLI commands or LLM-tool streams) reach A2A clients as status-update SSE frames
carrying ``cinna.content_kind="tool_result"``, ``cinna.tool_id``, and
``cinna.tool_stream``, and that the same metadata is reproduced by GetTask on
history replay.

Test scenarios:
  1. Live SSE stream — a stub event sequence containing tool + tool_result_delta
     chunks (stdout and stderr) + assistant events produces status-update frames
     with correct metadata for each tool_result_delta chunk.
  2. GetTask history replay — after the stream completes, GetTask returns an
     agent message whose parts include one TextPart per tool_result_delta event,
     carrying the same three metadata keys (cinna.content_kind, cinna.tool_id,
     cinna.tool_stream).
  3. Mixed sequence replay fidelity — a streaming_events list interleaving a
     tool call with multiple tool_result_delta chunks (stdout + stderr) and a
     final assistant response replays to a sequence of parts with correct
     metadata on each, verifying that the tool_result branch coexists correctly
     with the existing assistant and tool branches.
"""
import uuid
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.stubs.agent_env_stub import StubAgentEnvConnector
from tests.utils.a2a import (
    build_streaming_request,
    parse_sse_events,
    post_a2a_jsonrpc,
    setup_a2a_agent,
)
from tests.utils.background_tasks import drain_tasks

# Mirror the mapper constants rather than re-typing string literals.
_CONTENT_KIND_KEY = "cinna.content_kind"
_TOOL_ID_KEY = "cinna.tool_id"
_TOOL_STREAM_KEY = "cinna.tool_stream"

_KIND_TOOL_RESULT = "tool_result"
_KIND_TOOL = "tool"
_KIND_TEXT = "text"

_STREAM_STDOUT = "stdout"
_STREAM_STDERR = "stderr"


# ── Shared helpers ────────────────────────────────────────────────────────────


def _extract_parts_from_sse_event(event: dict) -> list[dict]:
    """Return the list of parts from a status-update SSE event's message."""
    msg = event.get("result", {}).get("status", {}).get("message") or {}
    return msg.get("parts", [])


def _part_text(part: dict) -> str:
    """Extract text from a part dict, handling both flat and root-wrapped shapes."""
    return part.get("text") or (part.get("root") or {}).get("text", "")


def _part_metadata(part: dict) -> dict:
    """Extract metadata from a part dict, handling both flat and root-wrapped shapes."""
    return part.get("metadata") or (part.get("root") or {}).get("metadata") or {}


def _build_tool_result_events(
    exec_id: str,
    stdout_lines: list[str],
    stderr_lines: list[str] | None = None,
    answer_text: str = "Command executed.",
) -> list[dict]:
    """Build event sequence: session_created → tools_init → tool → tool_result_delta chunks → assistant → done.

    Uses the LLM-path event shape (injected directly into stream_chat events),
    which is the simplest approach that exercises both the live-SSE branch
    and the replay branch without requiring stream_command stubbing.
    """
    events: list[dict] = [
        {
            "type": "session_created",
            "content": "",
            "session_id": str(uuid.uuid4()),
            "metadata": {},
        },
        {
            "type": "system",
            "subtype": "tools_init",
            "content": "",
            "data": {"tools": ["bash"]},
            "metadata": {},
        },
        {
            "type": "tool",
            "tool_name": "bash",
            "content": exec_id,
            "metadata": {
                "tool_id": exec_id,
                "tool_input": {"command": "ls -la"},
                "synthesized": True,
            },
        },
    ]
    for line in stdout_lines:
        events.append({
            "type": "tool_result_delta",
            "content": line,
            "metadata": {"tool_id": exec_id, "stream": "stdout"},
        })
    for line in (stderr_lines or []):
        events.append({
            "type": "tool_result_delta",
            "content": line,
            "metadata": {"tool_id": exec_id, "stream": "stderr"},
        })
    events.append({"type": "assistant", "content": answer_text, "metadata": {}})
    events.append({"type": "done"})
    return events


# ── Integration tests ─────────────────────────────────────────────────────────


def test_a2a_streaming_tool_result_delta_emits_text_parts(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Live SSE stream: tool_result_delta events produce status-update frames with
    the correct metadata on each TextPart.

      1. Setup agent with A2A enabled and an access token
      2. Send a streaming message with a stub that emits a tool event followed by
         two stdout chunks and one stderr chunk, then an assistant event
      3. Parse SSE events and collect all tool_result parts
      4. Verify each stdout chunk has cinna.tool_stream="stdout"
      5. Verify the stderr chunk has cinna.tool_stream="stderr"
      6. Verify cinna.content_kind="tool_result" on all collected parts
      7. Verify cinna.tool_id is present and matches the exec_id on all parts
      8. Verify part text matches the emitted chunk content
    """
    # ── Phase 1: Setup ────────────────────────────────────────────────────

    agent, token_data = setup_a2a_agent(
        client, superuser_token_headers, name="A2A Tool Result Streaming Agent",
    )
    agent_id = agent["id"]
    a2a_token = token_data["token"]

    exec_id = f"exec_{uuid.uuid4().hex[:8]}"
    stdout_lines = ["total 8", "drwxr-xr-x  2 user user 4096 May 01 10:00 ."]
    stderr_lines = ["ls: cannot access '/missing': No such file or directory"]
    answer_text = "Done listing the directory."

    # ── Phase 2: Send streaming message with tool_result_delta events ─────

    stub = StubAgentEnvConnector(
        events=_build_tool_result_events(
            exec_id=exec_id,
            stdout_lines=stdout_lines,
            stderr_lines=stderr_lines,
            answer_text=answer_text,
        )
    )

    request = build_streaming_request("List the current directory")
    a2a_headers = {
        "Authorization": f"Bearer {a2a_token}",
        "Content-Type": "application/json",
    }

    with patch("app.services.sessions.message_service.agent_env_connector", stub):
        resp = client.post(
            f"{settings.API_V1_STR}/a2a/{agent_id}/",
            headers=a2a_headers,
            json=request,
        )
        drain_tasks()

    assert resp.status_code == 200, f"A2A streaming request failed: {resp.text}"

    sse_events = parse_sse_events(resp.text)
    assert len(sse_events) >= 2, f"Expected at least 2 SSE events, got {len(sse_events)}"

    # ── Phase 3: Collect all tool_result TextParts from SSE ───────────────

    stdout_parts: list[dict] = []
    stderr_parts: list[dict] = []

    for event in sse_events:
        for part in _extract_parts_from_sse_event(event):
            meta = _part_metadata(part)
            if meta.get(_CONTENT_KIND_KEY) == _KIND_TOOL_RESULT:
                stream = meta.get(_TOOL_STREAM_KEY)
                if stream == _STREAM_STDOUT:
                    stdout_parts.append(part)
                elif stream == _STREAM_STDERR:
                    stderr_parts.append(part)

    # ── Phase 4: Verify stdout chunks ─────────────────────────────────────

    assert len(stdout_parts) == len(stdout_lines), (
        f"Expected {len(stdout_lines)} stdout tool_result part(s), "
        f"got {len(stdout_parts)}. "
        f"All SSE events: {sse_events}"
    )

    emitted_stdout_texts = [_part_text(p) for p in stdout_parts]
    for expected_line in stdout_lines:
        assert expected_line in emitted_stdout_texts, (
            f"Stdout line {expected_line!r} not found in tool_result parts. "
            f"Texts found: {emitted_stdout_texts!r}"
        )

    for part in stdout_parts:
        meta = _part_metadata(part)
        assert meta.get(_CONTENT_KIND_KEY) == _KIND_TOOL_RESULT
        assert meta.get(_TOOL_STREAM_KEY) == _STREAM_STDOUT
        tool_id = meta.get(_TOOL_ID_KEY)
        assert isinstance(tool_id, str) and tool_id, (
            f"Expected cinna.tool_id to be a non-empty string, got: {tool_id!r}"
        )
        assert tool_id == exec_id, (
            f"Expected cinna.tool_id={exec_id!r}, got: {tool_id!r}"
        )

    # ── Phase 5: Verify stderr chunk ─────────────────────────────────────

    assert len(stderr_parts) == len(stderr_lines), (
        f"Expected {len(stderr_lines)} stderr tool_result part(s), "
        f"got {len(stderr_parts)}."
    )

    emitted_stderr_texts = [_part_text(p) for p in stderr_parts]
    for expected_line in stderr_lines:
        assert expected_line in emitted_stderr_texts, (
            f"Stderr line {expected_line!r} not found in tool_result parts. "
            f"Texts found: {emitted_stderr_texts!r}"
        )

    for part in stderr_parts:
        meta = _part_metadata(part)
        assert meta.get(_CONTENT_KIND_KEY) == _KIND_TOOL_RESULT
        assert meta.get(_TOOL_STREAM_KEY) == _STREAM_STDERR
        tool_id = meta.get(_TOOL_ID_KEY)
        assert isinstance(tool_id, str) and tool_id
        assert tool_id == exec_id


def test_a2a_get_task_history_replay_tool_result_delta(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    History replay via GetTask: persisted tool_result_delta events are expanded
    to TextParts carrying the same three metadata keys as the live-SSE path.

      1. Setup agent with A2A enabled and an access token
      2. Send a streaming message using a stub that emits tool + stdout +
         stderr + assistant events
      3. Extract the task ID from the first SSE event
      4. Call GetTask and retrieve the agent message from history
      5. Verify the agent message has TextParts with cinna.content_kind="tool_result"
      6. Verify stdout parts have cinna.tool_stream="stdout"
      7. Verify stderr parts have cinna.tool_stream="stderr"
      8. Verify cinna.tool_id is set correctly on all tool_result parts
      9. Verify part text matches the original chunk content
    """
    # ── Phase 1: Setup ────────────────────────────────────────────────────

    agent, token_data = setup_a2a_agent(
        client, superuser_token_headers, name="A2A Tool Result Replay Agent",
    )
    agent_id = agent["id"]
    a2a_token = token_data["token"]

    exec_id = f"exec_{uuid.uuid4().hex[:8]}"
    stdout_lines = ["file1.txt", "file2.txt"]
    stderr_lines = ["warning: deprecated flag used"]
    answer_text = "I found 2 files in the directory."

    # ── Phase 2: Send streaming message with tool_result_delta events ─────

    stub = StubAgentEnvConnector(
        events=_build_tool_result_events(
            exec_id=exec_id,
            stdout_lines=stdout_lines,
            stderr_lines=stderr_lines,
            answer_text=answer_text,
        )
    )

    request = build_streaming_request("List files in the workspace")
    a2a_headers = {
        "Authorization": f"Bearer {a2a_token}",
        "Content-Type": "application/json",
    }

    with patch("app.services.sessions.message_service.agent_env_connector", stub):
        resp = client.post(
            f"{settings.API_V1_STR}/a2a/{agent_id}/",
            headers=a2a_headers,
            json=request,
        )
        drain_tasks()

    assert resp.status_code == 200, f"A2A streaming request failed: {resp.text}"

    sse_events = parse_sse_events(resp.text)
    assert len(sse_events) >= 1, "Expected at least one SSE event"
    task_id = sse_events[0]["result"]["taskId"]

    # ── Phase 3: Call GetTask ─────────────────────────────────────────────

    body = post_a2a_jsonrpc(client, agent_id, a2a_token, {
        "jsonrpc": "2.0",
        "id": "req-history-tool-result",
        "method": "GetTask",
        "params": {"id": task_id},
    })
    assert "result" in body, f"Expected JSON-RPC result, got: {body}"
    task = body["result"]

    history = task.get("history", [])
    assert len(history) >= 2, (
        f"Expected at least 2 messages in history (user + agent), got {len(history)}"
    )

    # ── Phase 4: Find the agent message ───────────────────────────────────

    agent_msgs = [m for m in history if m.get("role") == "agent"]
    assert agent_msgs, "Expected at least one agent message in task history"

    agent_msg = agent_msgs[-1]
    parts = agent_msg.get("parts", [])

    # ── Phase 5: Collect tool_result parts ────────────────────────────────

    # Emitted: 1 tool + 2 stdout + 1 stderr + 1 assistant = 5 content events
    expected_min_parts = 1 + len(stdout_lines) + len(stderr_lines) + 1
    assert len(parts) >= expected_min_parts, (
        f"Expected at least {expected_min_parts} parts in history agent message "
        f"(tool + {len(stdout_lines)} stdout + {len(stderr_lines)} stderr + assistant), "
        f"got {len(parts)}. Parts: {parts}"
    )

    stdout_replay_parts: list[dict] = []
    stderr_replay_parts: list[dict] = []

    for part in parts:
        meta = _part_metadata(part)
        if meta.get(_CONTENT_KIND_KEY) == _KIND_TOOL_RESULT:
            stream = meta.get(_TOOL_STREAM_KEY)
            if stream == _STREAM_STDOUT:
                stdout_replay_parts.append(part)
            elif stream == _STREAM_STDERR:
                stderr_replay_parts.append(part)

    # ── Phase 6: Verify stdout replay parts ───────────────────────────────

    assert len(stdout_replay_parts) == len(stdout_lines), (
        f"Expected {len(stdout_lines)} stdout tool_result part(s) in history, "
        f"got {len(stdout_replay_parts)}. All parts: {parts}"
    )

    replay_stdout_texts = [_part_text(p) for p in stdout_replay_parts]
    for expected_line in stdout_lines:
        assert expected_line in replay_stdout_texts, (
            f"Stdout line {expected_line!r} not found in history tool_result parts. "
            f"Texts found: {replay_stdout_texts!r}"
        )

    for part in stdout_replay_parts:
        meta = _part_metadata(part)
        assert meta.get(_CONTENT_KIND_KEY) == _KIND_TOOL_RESULT
        assert meta.get(_TOOL_STREAM_KEY) == _STREAM_STDOUT, (
            f"Expected cinna.tool_stream='stdout' on replay part, got: {meta.get(_TOOL_STREAM_KEY)!r}"
        )
        tool_id = meta.get(_TOOL_ID_KEY)
        assert isinstance(tool_id, str) and tool_id, (
            f"Expected cinna.tool_id to be a non-empty string on replay, got: {tool_id!r}"
        )
        assert tool_id == exec_id, (
            f"Expected cinna.tool_id={exec_id!r} on replay, got: {tool_id!r}"
        )

    # ── Phase 7: Verify stderr replay parts ───────────────────────────────

    assert len(stderr_replay_parts) == len(stderr_lines), (
        f"Expected {len(stderr_lines)} stderr tool_result part(s) in history, "
        f"got {len(stderr_replay_parts)}."
    )

    replay_stderr_texts = [_part_text(p) for p in stderr_replay_parts]
    for expected_line in stderr_lines:
        assert expected_line in replay_stderr_texts, (
            f"Stderr line {expected_line!r} not found in history tool_result parts. "
            f"Texts found: {replay_stderr_texts!r}"
        )

    for part in stderr_replay_parts:
        meta = _part_metadata(part)
        assert meta.get(_CONTENT_KIND_KEY) == _KIND_TOOL_RESULT
        assert meta.get(_TOOL_STREAM_KEY) == _STREAM_STDERR, (
            f"Expected cinna.tool_stream='stderr' on replay part, got: {meta.get(_TOOL_STREAM_KEY)!r}"
        )
        tool_id = meta.get(_TOOL_ID_KEY)
        assert isinstance(tool_id, str) and tool_id
        assert tool_id == exec_id

    # ── Phase 8: Verify no tool_result part has empty text ────────────────

    for part in stdout_replay_parts + stderr_replay_parts:
        text = _part_text(part)
        assert text, (
            f"tool_result replay part must not have empty text, got: {part!r}"
        )


def test_a2a_history_replay_mixed_sequence_preserves_all_kinds(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Mixed streaming_events sequence replay fidelity: a stream containing tool +
    tool_result_delta (stdout) + tool_result_delta (stderr) + assistant events
    replays to parts with correct metadata on each, verifying the new
    tool_result branch coexists correctly with the existing tool and text branches.

      1. Setup agent with A2A enabled and an access token
      2. Send a streaming message with a mixed event sequence
      3. Call GetTask and retrieve the agent message from history
      4. Verify that tool, tool_result (stdout), tool_result (stderr), and text
         parts are all present in the history with correct metadata
      5. Verify ordering is preserved: tool before tool_result before text
    """
    # ── Phase 1: Setup ────────────────────────────────────────────────────

    agent, token_data = setup_a2a_agent(
        client, superuser_token_headers, name="A2A Mixed Replay Fidelity Agent",
    )
    agent_id = agent["id"]
    a2a_token = token_data["token"]

    exec_id = f"exec_{uuid.uuid4().hex[:8]}"
    stdout_chunk = "Hello from stdout"
    stderr_chunk = "Warning from stderr"
    answer_text = "All done."

    mixed_events = [
        {
            "type": "session_created",
            "content": "",
            "session_id": str(uuid.uuid4()),
            "metadata": {},
        },
        {
            "type": "system",
            "subtype": "tools_init",
            "content": "",
            "data": {"tools": ["bash"]},
            "metadata": {},
        },
        {
            "type": "tool",
            "tool_name": "bash",
            "content": exec_id,
            "metadata": {
                "tool_id": exec_id,
                "tool_input": {"command": "echo hello"},
            },
        },
        {
            "type": "tool_result_delta",
            "content": stdout_chunk,
            "metadata": {"tool_id": exec_id, "stream": "stdout"},
        },
        {
            "type": "tool_result_delta",
            "content": stderr_chunk,
            "metadata": {"tool_id": exec_id, "stream": "stderr"},
        },
        {"type": "assistant", "content": answer_text, "metadata": {}},
        {"type": "done"},
    ]

    # ── Phase 2: Stream and collect task ID ───────────────────────────────

    stub = StubAgentEnvConnector(events=mixed_events)
    request = build_streaming_request("Run echo hello")
    a2a_headers = {
        "Authorization": f"Bearer {a2a_token}",
        "Content-Type": "application/json",
    }

    with patch("app.services.sessions.message_service.agent_env_connector", stub):
        resp = client.post(
            f"{settings.API_V1_STR}/a2a/{agent_id}/",
            headers=a2a_headers,
            json=request,
        )
        drain_tasks()

    assert resp.status_code == 200, f"A2A streaming request failed: {resp.text}"

    sse_events = parse_sse_events(resp.text)
    assert len(sse_events) >= 1
    task_id = sse_events[0]["result"]["taskId"]

    # ── Phase 3: Call GetTask ─────────────────────────────────────────────

    body = post_a2a_jsonrpc(client, agent_id, a2a_token, {
        "jsonrpc": "2.0",
        "id": "req-mixed-replay",
        "method": "GetTask",
        "params": {"id": task_id},
    })
    assert "result" in body
    history = body["result"].get("history", [])

    agent_msgs = [m for m in history if m.get("role") == "agent"]
    assert agent_msgs, "Expected at least one agent message in history"
    parts = agent_msgs[-1].get("parts", [])

    # ── Phase 4: Verify all four kinds present ────────────────────────────

    # Expected: tool + tool_result(stdout) + tool_result(stderr) + text = 4 parts
    assert len(parts) >= 4, (
        f"Expected at least 4 parts (tool + 2x tool_result + text), "
        f"got {len(parts)}. Parts: {parts}"
    )

    kinds_found: list[str] = []
    tool_result_streams: list[str] = []

    for part in parts:
        meta = _part_metadata(part)
        kind = meta.get(_CONTENT_KIND_KEY)
        if kind:
            kinds_found.append(kind)
            if kind == _KIND_TOOL_RESULT:
                stream = meta.get(_TOOL_STREAM_KEY)
                if stream:
                    tool_result_streams.append(stream)

    assert _KIND_TOOL in kinds_found, (
        f"Expected a 'tool' part in history. Kinds found: {kinds_found}"
    )
    assert _KIND_TOOL_RESULT in kinds_found, (
        f"Expected a 'tool_result' part in history. Kinds found: {kinds_found}"
    )
    assert _KIND_TEXT in kinds_found, (
        f"Expected a 'text' part in history. Kinds found: {kinds_found}"
    )

    assert _STREAM_STDOUT in tool_result_streams, (
        f"Expected a stdout tool_result part in history. Streams found: {tool_result_streams}"
    )
    assert _STREAM_STDERR in tool_result_streams, (
        f"Expected a stderr tool_result part in history. Streams found: {tool_result_streams}"
    )

    # ── Phase 5: Verify ordering ──────────────────────────────────────────
    # tool must appear before any tool_result; tool_result before text.

    tool_idx: int | None = None
    first_tool_result_idx: int | None = None
    text_idx: int | None = None

    for i, part in enumerate(parts):
        meta = _part_metadata(part)
        kind = meta.get(_CONTENT_KIND_KEY)
        if kind == _KIND_TOOL and tool_idx is None:
            tool_idx = i
        elif kind == _KIND_TOOL_RESULT and first_tool_result_idx is None:
            first_tool_result_idx = i
        elif kind == _KIND_TEXT and text_idx is None:
            text_idx = i

    assert tool_idx is not None
    assert first_tool_result_idx is not None
    assert text_idx is not None

    assert tool_idx < first_tool_result_idx, (
        f"Expected tool part (idx={tool_idx}) before first tool_result part "
        f"(idx={first_tool_result_idx})"
    )
    assert first_tool_result_idx < text_idx, (
        f"Expected first tool_result part (idx={first_tool_result_idx}) before "
        f"text part (idx={text_idx})"
    )
