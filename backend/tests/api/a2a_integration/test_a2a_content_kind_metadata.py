"""
Integration tests: A2A content-kind metadata on TextParts.

Verifies that A2A clients can distinguish the agent's text answer,
chain-of-thought (thinking), and tool-call narration via
``TextPart.metadata["cinna.content_kind"]``.

Test scenarios:
  1. Streaming SSE path — content-kind metadata present on each TextPart
     emitted during a stream (text / thinking / tool); cinna.tool_name,
     cinna.tool_input, and cinna.tool_id present on tool events.
  2. History replay path — GetTask returns an agent message whose parts
     are expanded to one TextPart per streaming event, each carrying
     cinna.content_kind metadata; tool parts also carry cinna.tool_input
     and cinna.tool_id.
  3. command_invocation SSE — /files sync command produces a completed
     status-update frame whose TextPart metadata carries both
     cinna.content_kind="command_result" AND cinna.command_invocation="/files".
  4. command_invocation on /run:* tool + tool_result_delta — synthesized
     tool and tool_result_delta events from stream_command_via_agent_env carry
     cinna.command_invocation on both the tool part and the result chunks in
     the live SSE stream, and history replay reproduces the same metadata.
"""
import uuid
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.stubs.agent_env_stub import StubAgentEnvConnector
from tests.utils.a2a import (
    build_streaming_request,
    extract_parts_from_sse_event as _extract_parts_from_sse_event,
    parse_sse_events,
    part_metadata as _part_metadata,
    part_text as _part_text,
    post_a2a_jsonrpc,
    setup_a2a_agent,
)
from tests.utils.background_tasks import drain_tasks

# Vendor-namespaced metadata keys — mirrors a2a_event_mapper constants.
_CONTENT_KIND_KEY = "cinna.content_kind"
_TOOL_NAME_KEY = "cinna.tool_name"
_TOOL_INPUT_KEY = "cinna.tool_input"
_TOOL_ID_KEY = "cinna.tool_id"
_TOOL_STREAM_KEY = "cinna.tool_stream"
_COMMAND_INVOCATION_KEY = "cinna.command_invocation"

_KIND_TEXT = "text"
_KIND_THINKING = "thinking"
_KIND_TOOL = "tool"
_KIND_TOOL_RESULT = "tool_result"
_KIND_COMMAND_RESULT = "command_result"


# SSE part extractors (_extract_parts_from_sse_event / _part_text /
# _part_metadata) live in tests/utils/a2a.py and are imported above.


def _build_rich_events(
    thinking_text: str = "Let me think carefully.",
    tool_name: str = "bash",
    tool_content: str = "ran ls -la",
    answer_text: str = "Here is the answer.",
    tool_input: dict | None = None,
    tool_id: str | None = None,
) -> list[dict]:
    """Build a realistic SSE event sequence: thinking → tool → assistant → done.

    ``tool_input`` and ``tool_id``, when provided, are embedded in the tool
    event's ``metadata`` dict, matching what the real adapters emit (e.g.
    claude_code_event_transformer).  The mapper will surface them as
    ``cinna.tool_input`` and ``cinna.tool_id`` on the resulting TextPart.
    """
    tool_metadata: dict = {}
    if isinstance(tool_input, dict):
        tool_metadata["tool_input"] = tool_input
    if isinstance(tool_id, str) and tool_id:
        tool_metadata["tool_id"] = tool_id

    return [
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
        {"type": "thinking", "content": thinking_text, "metadata": {}},
        {
            "type": "tool",
            "tool_name": tool_name,
            "content": tool_content,
            "metadata": tool_metadata,
        },
        {"type": "assistant", "content": answer_text, "metadata": {}},
        {"type": "done"},
    ]


# ── Tests ────────────────────────────────────────────────────────────────────


def test_a2a_streaming_content_kind_metadata(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Streaming SSE path: content-kind metadata is present on each TextPart.

      1. Setup agent with A2A enabled and an access token
      2. Send a streaming message using a stub that emits thinking, tool,
         and assistant events (tool event carries tool_input + tool_id)
      3. Verify working-state SSE events carry TextPart metadata:
         - thinking events → cinna.content_kind = "thinking"
         - tool events → cinna.content_kind = "tool", cinna.tool_name,
           cinna.tool_input (non-empty dict), cinna.tool_id (non-empty str)
         - assistant events → cinna.content_kind = "text"
      4. Verify no metadata-carrying event has empty text
    """
    # ── Phase 1: Setup ────────────────────────────────────────────────────

    agent, token_data = setup_a2a_agent(
        client, superuser_token_headers, name="A2A Content-Kind Streaming Agent",
    )
    agent_id = agent["id"]
    a2a_token = token_data["token"]

    thinking_text = "Let me reason through this step by step."
    tool_name = "bash"
    tool_content = "total 8\ndrwxr-xr-x  2 user user 4096 Apr 18 10:00 ."
    answer_text = "The directory has one entry."
    tool_input = {"command": "ls -la"}
    tool_id = "toolu_streaming_01"

    # ── Phase 2: Send streaming message with custom events ────────────────

    stub = StubAgentEnvConnector(
        events=_build_rich_events(
            thinking_text=thinking_text,
            tool_name=tool_name,
            tool_content=tool_content,
            answer_text=answer_text,
            tool_input=tool_input,
            tool_id=tool_id,
        )
    )

    request = build_streaming_request("Show me the directory listing")
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

    events = parse_sse_events(resp.text)
    assert len(events) >= 2, f"Expected at least 2 SSE events, got {len(events)}"

    # ── Phase 3: Collect all parts that carry content-kind metadata ────────

    parts_by_kind: dict[str, list[dict]] = {
        _KIND_TEXT: [],
        _KIND_THINKING: [],
        _KIND_TOOL: [],
    }

    for event in events:
        for part in _extract_parts_from_sse_event(event):
            metadata = _part_metadata(part)
            kind = metadata.get(_CONTENT_KIND_KEY)
            if kind in parts_by_kind:
                parts_by_kind[kind].append(part)

    # ── Phase 4: Assert thinking event metadata ────────────────────────────

    assert parts_by_kind[_KIND_THINKING], (
        "Expected at least one SSE event with cinna.content_kind='thinking'"
    )
    for part in parts_by_kind[_KIND_THINKING]:
        text = _part_text(part)
        assert text, "Thinking part must have non-empty text"
        assert thinking_text in text, (
            f"Expected thinking text in part, got: {text!r}"
        )

    # ── Phase 5: Assert tool event metadata ───────────────────────────────

    assert parts_by_kind[_KIND_TOOL], (
        "Expected at least one SSE event with cinna.content_kind='tool'"
    )
    for part in parts_by_kind[_KIND_TOOL]:
        text = _part_text(part)
        metadata = _part_metadata(part)

        assert tool_content in text, (
            f"Expected tool content in part text, got: {text!r}"
        )
        # No legacy "[Tool: X]" prefix — content-kind/tool_name metadata is
        # the sole discriminator.
        assert "[Tool:" not in text, (
            f"Tool part text must not carry a '[Tool: ...]' prefix, got: {text!r}"
        )

        # cinna.tool_name must be present
        assert metadata.get(_TOOL_NAME_KEY) == tool_name, (
            f"Expected cinna.tool_name={tool_name!r}, got: {metadata.get(_TOOL_NAME_KEY)!r}"
        )

        # cinna.tool_input must be a non-empty dict matching the emitted input
        part_tool_input = metadata.get(_TOOL_INPUT_KEY)
        assert isinstance(part_tool_input, dict), (
            f"Expected cinna.tool_input to be a dict, got: {type(part_tool_input)!r} "
            f"({part_tool_input!r})"
        )
        assert part_tool_input, (
            "Expected cinna.tool_input to be non-empty"
        )
        assert "command" in part_tool_input, (
            f"Expected 'command' key in cinna.tool_input, got keys: {list(part_tool_input.keys())}"
        )

        # cinna.tool_id must be a non-empty string matching the emitted tool_id
        part_tool_id = metadata.get(_TOOL_ID_KEY)
        assert isinstance(part_tool_id, str), (
            f"Expected cinna.tool_id to be a str, got: {type(part_tool_id)!r} "
            f"({part_tool_id!r})"
        )
        assert part_tool_id, (
            "Expected cinna.tool_id to be a non-empty string"
        )
        assert part_tool_id == tool_id, (
            f"Expected cinna.tool_id={tool_id!r}, got: {part_tool_id!r}"
        )

    # ── Phase 6: Assert assistant (text) event metadata ───────────────────

    assert parts_by_kind[_KIND_TEXT], (
        "Expected at least one SSE event with cinna.content_kind='text'"
    )
    for part in parts_by_kind[_KIND_TEXT]:
        text = _part_text(part)
        assert text, "Text part must have non-empty text"
        assert answer_text in text, (
            f"Expected answer text in part, got: {text!r}"
        )

    # ── Phase 7: No metadata-bearing part should have empty text ──────────

    for kind, parts in parts_by_kind.items():
        for part in parts:
            assert _part_text(part), (
                f"Part with cinna.content_kind={kind!r} must not have empty text"
            )


def test_a2a_get_task_history_replay_content_kind_metadata(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    History replay path: GetTask returns agent message with one TextPart per
    streaming event, each carrying cinna.content_kind metadata.

      1. Setup agent with A2A enabled and an access token
      2. Send a streaming message using a stub emitting thinking + tool +
         assistant events (tool event carries tool_input + tool_id)
      3. Call GetTask and retrieve the history
      4. Verify the agent message has multiple parts (not a single collapsed part)
      5. Verify each part carries the correct cinna.content_kind
      6. Verify the tool part carries cinna.tool_name
      7. Verify the tool part carries cinna.tool_input (non-empty dict)
         and cinna.tool_id (non-empty string)
    """
    # ── Phase 1: Setup ────────────────────────────────────────────────────

    agent, token_data = setup_a2a_agent(
        client, superuser_token_headers, name="A2A Content-Kind History Agent",
    )
    agent_id = agent["id"]
    a2a_token = token_data["token"]

    thinking_text = "I need to check the files first."
    tool_name = "read"
    tool_content = "file contents here"
    answer_text = "Based on the file, the answer is 42."
    tool_input = {"path": "/workspace/notes.txt"}
    tool_id = "toolu_history_01"

    # ── Phase 2: Send streaming message with thinking + tool + assistant ──

    stub = StubAgentEnvConnector(
        events=_build_rich_events(
            thinking_text=thinking_text,
            tool_name=tool_name,
            tool_content=tool_content,
            answer_text=answer_text,
            tool_input=tool_input,
            tool_id=tool_id,
        )
    )

    request = build_streaming_request("What is in the file?")
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
        "id": "req-history",
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

    # ── Phase 5: Verify multiple parts are returned ────────────────────────

    # The stub emits thinking + tool + assistant = 3 content events.
    # All three must map to a distinct TextPart in the history.
    assert len(parts) >= 3, (
        f"Expected at least 3 TextParts in agent message (thinking + tool + assistant), "
        f"got {len(parts)}. Parts: {parts}"
    )

    # ── Phase 6: Collect and verify part metadata ─────────────────────────

    kinds_found: set[str] = set()
    tool_name_found: str | None = None
    tool_part_metadata: dict | None = None

    for part in parts:
        metadata = _part_metadata(part)
        kind = metadata.get(_CONTENT_KIND_KEY)
        if kind:
            kinds_found.add(kind)
        if kind == _KIND_TOOL:
            tool_name_found = metadata.get(_TOOL_NAME_KEY)
            tool_part_metadata = metadata

    assert _KIND_THINKING in kinds_found, (
        f"Expected a part with cinna.content_kind='thinking' in history. "
        f"Kinds found: {kinds_found}"
    )
    assert _KIND_TOOL in kinds_found, (
        f"Expected a part with cinna.content_kind='tool' in history. "
        f"Kinds found: {kinds_found}"
    )
    assert _KIND_TEXT in kinds_found, (
        f"Expected a part with cinna.content_kind='text' in history. "
        f"Kinds found: {kinds_found}"
    )

    # ── Phase 7: Verify cinna.tool_name on the tool part ─────────────────

    assert tool_name_found == tool_name, (
        f"Expected cinna.tool_name={tool_name!r} on history tool part, "
        f"got: {tool_name_found!r}"
    )

    # ── Phase 8: Verify cinna.tool_input and cinna.tool_id on tool part ──

    assert tool_part_metadata is not None, "tool_part_metadata was not collected (no tool part found)"

    history_tool_input = tool_part_metadata.get(_TOOL_INPUT_KEY)
    assert isinstance(history_tool_input, dict), (
        f"Expected cinna.tool_input in history tool part to be a dict, "
        f"got: {type(history_tool_input)!r} ({history_tool_input!r})"
    )
    assert history_tool_input, (
        "Expected cinna.tool_input in history tool part to be non-empty"
    )

    history_tool_id = tool_part_metadata.get(_TOOL_ID_KEY)
    assert isinstance(history_tool_id, str), (
        f"Expected cinna.tool_id in history tool part to be a str, "
        f"got: {type(history_tool_id)!r} ({history_tool_id!r})"
    )
    assert history_tool_id, (
        "Expected cinna.tool_id in history tool part to be a non-empty string"
    )

    # ── Phase 9: Verify no part has empty text ────────────────────────────

    for part in parts:
        text = _part_text(part)
        metadata = _part_metadata(part)
        if metadata.get(_CONTENT_KIND_KEY):
            assert text, (
                f"History part with cinna.content_kind={metadata.get(_CONTENT_KIND_KEY)!r} "
                f"must not have empty text"
            )


def test_a2a_files_command_result_part_carries_command_invocation(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    /files sync command: the completed status-update SSE frame's TextPart
    carries both cinna.content_kind="command_result" AND
    cinna.command_invocation="/files".

      1. Setup agent with A2A enabled and an access token
      2. Send /files via A2A (no LLM call — purely sync platform command)
      3. Parse SSE events; find the single completed frame
      4. Verify the TextPart metadata has cinna.content_kind="command_result"
      5. Verify the TextPart metadata has cinna.command_invocation="/files"
    """
    # ── Phase 1: Setup ────────────────────────────────────────────────────

    agent, token_data = setup_a2a_agent(
        client, superuser_token_headers, name="A2A Command Invocation Files Agent",
    )
    agent_id = agent["id"]
    a2a_token = token_data["token"]

    a2a_headers = {
        "Authorization": f"Bearer {a2a_token}",
        "Content-Type": "application/json",
    }

    # ── Phase 2: Send /files via A2A ──────────────────────────────────────

    # No LLM stub needed — /files is a sync backend command that bypasses the
    # agent-env connector entirely.
    with patch("app.services.sessions.message_service.agent_env_connector",
               StubAgentEnvConnector(response_text="should not be called")):
        resp = client.post(
            f"{settings.API_V1_STR}/a2a/{agent_id}/",
            headers=a2a_headers,
            json=build_streaming_request("/files"),
        )
        drain_tasks()

    assert resp.status_code == 200, f"A2A /files request failed: {resp.text}"

    events = parse_sse_events(resp.text)
    assert len(events) >= 1, f"Expected at least one SSE event for /files, got {len(events)}"

    # ── Phase 3: Find the completed frame ────────────────────────────────

    completed_events = [
        e for e in events
        if e.get("result", {}).get("status", {}).get("state") == "completed"
    ]
    assert completed_events, (
        f"Expected at least one 'completed' SSE event from /files, none found. "
        f"Events: {events}"
    )

    # The /files command emits exactly one terminal frame.
    completed = completed_events[0]
    parts = _extract_parts_from_sse_event(completed)
    assert parts, (
        f"Expected the completed /files frame to carry a message with parts, "
        f"got: {completed}"
    )

    # ── Phase 4 & 5: Verify content_kind + command_invocation on TextPart ─

    first_part = parts[0]
    metadata = _part_metadata(first_part)

    assert metadata.get(_CONTENT_KIND_KEY) == _KIND_COMMAND_RESULT, (
        f"Expected cinna.content_kind='command_result' on /files part, "
        f"got: {metadata.get(_CONTENT_KIND_KEY)!r}"
    )
    assert metadata.get(_COMMAND_INVOCATION_KEY) == "/files", (
        f"Expected cinna.command_invocation='/files' on /files part, "
        f"got: {metadata.get(_COMMAND_INVOCATION_KEY)!r}"
    )


def test_a2a_run_command_tool_and_result_parts_carry_command_invocation(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Mapper propagation of cinna.command_invocation through the live SSE stream
    and GetTask history replay for events shaped like stream_command_via_agent_env
    output (synthesized tool + tool_result_delta events carrying command_invocation).

    The stub injects a pre-built event sequence that mirrors what
    stream_command_via_agent_env emits for a /run:* command — a synthesized tool
    event and a tool_result_delta event, both with command_invocation in metadata.
    The inbound message text is a plain sentence (not a slash command) so it is
    routed to the agent-env connector and the stub fires.

      1. Setup agent with A2A enabled and an access token
      2. Build a stub that emits a tool + tool_result_delta + assistant sequence,
         each carrying command_invocation="/run:check" in metadata
      3. Send a plain-text message so the A2A handler routes to agent-env
      4. Verify live SSE: tool part has cinna.command_invocation="/run:check"
      5. Verify live SSE: tool_result_delta part has cinna.command_invocation="/run:check"
      6. Call GetTask and verify history replay preserves cinna.command_invocation
         on both the tool part and the tool_result part
    """
    # ── Phase 1: Setup ────────────────────────────────────────────────────

    agent, token_data = setup_a2a_agent(
        client, superuser_token_headers, name="A2A Run Command Invocation Agent",
    )
    agent_id = agent["id"]
    a2a_token = token_data["token"]

    exec_id = "exec_run_check_01"
    command_invocation = "/run:check"

    # Build event sequence that mirrors stream_command_via_agent_env output:
    # synthesized tool event → stdout chunks → assistant summary → done.
    # The command_invocation key in each event's metadata is what the production
    # code writes; the mapper copies it to the TextPart metadata.
    run_events = [
        {
            "type": "session_created",
            "content": "",
            "session_id": "00000000-0000-0000-0000-000000000001",
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
            "content": f"Running {command_invocation}",
            "metadata": {
                "tool_id": exec_id,
                "tool_input": {"command": "uv run /app/scripts/check.py"},
                "command_invocation": command_invocation,
            },
        },
        {
            "type": "tool_result_delta",
            "content": "All checks passed.",
            "metadata": {
                "tool_id": exec_id,
                "stream": "stdout",
                "command_invocation": command_invocation,
            },
        },
        {
            "type": "assistant",
            "content": "Health check completed successfully.",
            "metadata": {},
        },
        {"type": "done"},
    ]

    stub = StubAgentEnvConnector(events=run_events)
    # Use a plain-text message (not a slash command) so the A2A request handler
    # routes to the agent-env connector and the stub fires, rather than
    # intercepting it as a sync platform command.
    request = build_streaming_request("Run the health check script please")
    a2a_headers = {
        "Authorization": f"Bearer {a2a_token}",
        "Content-Type": "application/json",
    }

    # ── Phase 2: Stream and collect SSE events ────────────────────────────

    with patch("app.services.sessions.message_service.agent_env_connector", stub):
        resp = client.post(
            f"{settings.API_V1_STR}/a2a/{agent_id}/",
            headers=a2a_headers,
            json=request,
        )
        drain_tasks()

    assert resp.status_code == 200, f"A2A /run:check request failed: {resp.text}"

    sse_events = parse_sse_events(resp.text)
    assert len(sse_events) >= 2, (
        f"Expected at least 2 SSE events, got {len(sse_events)}"
    )
    task_id = sse_events[0]["result"]["taskId"]

    # ── Phase 3: Collect parts from live SSE by kind ──────────────────────

    tool_sse_parts: list[dict] = []
    tool_result_sse_parts: list[dict] = []

    for event in sse_events:
        for part in _extract_parts_from_sse_event(event):
            meta = _part_metadata(part)
            kind = meta.get(_CONTENT_KIND_KEY)
            if kind == _KIND_TOOL:
                tool_sse_parts.append(part)
            elif kind == _KIND_TOOL_RESULT:
                tool_result_sse_parts.append(part)

    # ── Phase 4: tool part carries command_invocation ────────────────────

    assert tool_sse_parts, (
        "Expected at least one tool part in live SSE events"
    )
    for part in tool_sse_parts:
        meta = _part_metadata(part)
        assert meta.get(_COMMAND_INVOCATION_KEY) == command_invocation, (
            f"Expected cinna.command_invocation={command_invocation!r} on SSE tool part, "
            f"got: {meta.get(_COMMAND_INVOCATION_KEY)!r}"
        )

    # ── Phase 5: tool_result_delta part carries command_invocation ────────

    assert tool_result_sse_parts, (
        "Expected at least one tool_result part in live SSE events"
    )
    for part in tool_result_sse_parts:
        meta = _part_metadata(part)
        assert meta.get(_COMMAND_INVOCATION_KEY) == command_invocation, (
            f"Expected cinna.command_invocation={command_invocation!r} on SSE "
            f"tool_result part, got: {meta.get(_COMMAND_INVOCATION_KEY)!r}"
        )

    # ── Phase 6: GetTask history replay preserves command_invocation ──────

    body = post_a2a_jsonrpc(client, agent_id, a2a_token, {
        "jsonrpc": "2.0",
        "id": "req-run-history",
        "method": "GetTask",
        "params": {"id": task_id},
    })
    assert "result" in body, f"Expected JSON-RPC result, got: {body}"

    history = body["result"].get("history", [])
    agent_msgs = [m for m in history if m.get("role") == "agent"]
    assert agent_msgs, "Expected at least one agent message in history"

    parts = agent_msgs[-1].get("parts", [])

    tool_replay_parts = [
        p for p in parts
        if _part_metadata(p).get(_CONTENT_KIND_KEY) == _KIND_TOOL
    ]
    tool_result_replay_parts = [
        p for p in parts
        if _part_metadata(p).get(_CONTENT_KIND_KEY) == _KIND_TOOL_RESULT
    ]

    assert tool_replay_parts, (
        "Expected at least one tool part in history replay"
    )
    for part in tool_replay_parts:
        meta = _part_metadata(part)
        assert meta.get(_COMMAND_INVOCATION_KEY) == command_invocation, (
            f"Expected cinna.command_invocation={command_invocation!r} on replayed "
            f"tool part, got: {meta.get(_COMMAND_INVOCATION_KEY)!r}"
        )

    assert tool_result_replay_parts, (
        "Expected at least one tool_result part in history replay"
    )
    for part in tool_result_replay_parts:
        meta = _part_metadata(part)
        assert meta.get(_COMMAND_INVOCATION_KEY) == command_invocation, (
            f"Expected cinna.command_invocation={command_invocation!r} on replayed "
            f"tool_result part, got: {meta.get(_COMMAND_INVOCATION_KEY)!r}"
        )
