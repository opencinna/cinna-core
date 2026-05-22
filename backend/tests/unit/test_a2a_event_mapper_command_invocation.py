"""
Unit tests for A2AEventMapper — cinna.command_invocation metadata key.

These tests call the mapper methods directly with no HTTP, no DB, and no
FastAPI TestClient. They verify that:

  1. ``map_stream_event`` stamps ``cinna.command_invocation`` on ``tool``
     events when the event metadata contains a non-empty command_invocation.
  2. ``map_stream_event`` omits ``cinna.command_invocation`` on ``tool``
     events when the key is absent (LLM-initiated tools are unchanged).
  3. ``map_stream_event`` stamps ``cinna.command_invocation`` on
     ``tool_result_delta`` events when present and non-empty.
  4. ``map_stream_event`` omits ``cinna.command_invocation`` on
     ``tool_result_delta`` events when absent.
  5. ``create_command_result_event`` stamps ``cinna.command_invocation``
     when called with a non-empty string.
  6. ``create_command_result_event`` omits ``cinna.command_invocation``
     when called with the default (None).
  7. ``create_command_result_event`` omits ``cinna.command_invocation``
     when called with an empty string.
  8. ``_build_parts_for_session_message`` (history replay) propagates
     ``cinna.command_invocation`` from persisted ``tool`` events.
  9. ``_build_parts_for_session_message`` (history replay) propagates
     ``cinna.command_invocation`` from persisted ``tool_result_delta`` events.
 10. ``_build_parts_for_session_message`` omits ``cinna.command_invocation``
     when persisted events do not carry it (absence = LLM-initiated).

These are mapper-level unit tests; service-layer imports are allowed here
following the same pattern as other files in tests/unit/.
"""
from unittest.mock import MagicMock

from app.services.a2a.a2a_event_mapper import (
    A2AEventMapper,
    COMMAND_INVOCATION_KEY,
    CONTENT_KIND_KEY,
    CONTENT_KIND_COMMAND_RESULT,
    CONTENT_KIND_TOOL,
    CONTENT_KIND_TOOL_RESULT,
    TOOL_ID_KEY,
    TOOL_NAME_KEY,
    TOOL_STREAM_KEY,
    TOOL_STREAM_STDOUT,
)

_FAKE_TASK_ID = "task-cmd-invocation-001"
_FAKE_CONTEXT_ID = "ctx-cmd-invocation-001"


# ── Helpers ───────────────────────────────────────────────────────────────────


def _call_mapper(event: dict) -> dict | None:
    """Convenience wrapper around map_stream_event for unit tests."""
    return A2AEventMapper.map_stream_event(
        event=event,
        task_id=_FAKE_TASK_ID,
        context_id=_FAKE_CONTEXT_ID,
    )


def _extract_first_part(result: dict) -> dict:
    """Return the first Part dict from a status-update result."""
    parts = (
        result.get("status", {})
        .get("message", {})
        .get("parts", [])
    )
    assert parts, f"Expected at least one part in result, got none. Result: {result}"
    return parts[0]


def _part_metadata(part: dict) -> dict:
    """Extract metadata from a part dict, handling both flat and root-wrapped shapes."""
    return (
        part.get("metadata")
        or (part.get("root") or {}).get("metadata")
        or {}
    )


def _part_text(part: dict) -> str:
    """Extract text from a part dict, handling both flat and root-wrapped shapes."""
    return (
        part.get("text", "")
        or (part.get("root") or {}).get("text", "")
    )


def _make_session_message(content: str, streaming_events: list[dict]) -> MagicMock:
    """Build a minimal SessionMessage-like mock for _build_parts_for_session_message."""
    msg = MagicMock()
    msg.content = content
    msg.message_metadata = {"streaming_events": streaming_events}
    return msg


def _part_metadata_from_part_obj(part) -> dict:
    """Extract metadata from a Part Pydantic object (returned by _build_parts_for_session_message).

    _build_parts_for_session_message returns list[Part] where Part is a Pydantic
    model with a ``root`` attribute (Part.root is a TextPart). The TextPart carries
    ``metadata`` as a dict attribute.  This differs from the SSE/HTTP layer which
    serialises to plain dicts, so the generic _part_metadata() dict helper does not
    work here.
    """
    # Part is a pydantic model — root is the TextPart
    root = getattr(part, "root", None)
    if root is not None:
        return getattr(root, "metadata", None) or {}
    # Fallback: if somehow serialized as dict
    if isinstance(part, dict):
        return _part_metadata(part)
    return {}


# ── 1. tool event WITH command_invocation ─────────────────────────────────────


def test_map_stream_event_tool_with_command_invocation() -> None:
    """
    ``tool`` event with metadata.command_invocation="/run:check" produces a
    TextPart whose metadata carries ALL of:
      - cinna.content_kind = "tool"
      - cinna.tool_name = "bash"
      - cinna.tool_id = <exec_id>
      - cinna.command_invocation = "/run:check"

    The presence of command_invocation is the only signal that this part
    originated from a platform /run:* command, not an LLM-initiated tool call.
    """
    exec_id = "exec_run_check_01"
    event = {
        "type": "tool",
        "tool_name": "bash",
        "content": "Running health check...",
        "metadata": {
            "tool_id": exec_id,
            "tool_input": {"command": "uv run /app/scripts/check.py"},
            "command_invocation": "/run:check",
        },
    }

    result = _call_mapper(event)

    assert result is not None, (
        "Expected a non-None result for tool event with non-empty content"
    )
    assert result.get("kind") == "status-update", (
        f"Expected kind='status-update', got: {result.get('kind')!r}"
    )

    part = _extract_first_part(result)
    meta = _part_metadata(part)

    assert meta.get(CONTENT_KIND_KEY) == CONTENT_KIND_TOOL, (
        f"Expected cinna.content_kind='tool', got: {meta.get(CONTENT_KIND_KEY)!r}"
    )
    assert meta.get(TOOL_NAME_KEY) == "bash", (
        f"Expected cinna.tool_name='bash', got: {meta.get(TOOL_NAME_KEY)!r}"
    )
    assert meta.get(TOOL_ID_KEY) == exec_id, (
        f"Expected cinna.tool_id={exec_id!r}, got: {meta.get(TOOL_ID_KEY)!r}"
    )
    assert meta.get(COMMAND_INVOCATION_KEY) == "/run:check", (
        f"Expected cinna.command_invocation='/run:check', got: {meta.get(COMMAND_INVOCATION_KEY)!r}"
    )


# ── 2. tool event WITHOUT command_invocation ──────────────────────────────────


def test_map_stream_event_tool_without_command_invocation_key_absent() -> None:
    """
    ``tool`` event without metadata.command_invocation (LLM-initiated tool call)
    produces a TextPart whose metadata does NOT contain cinna.command_invocation.

    Absence is the signal that the tool was called by the LLM, not by a platform
    command.  This is the backward-compatibility check: existing behaviour must
    be unchanged when the key is not present.
    """
    event = {
        "type": "tool",
        "tool_name": "bash",
        "content": "Running ls -la",
        "metadata": {
            "tool_id": "exec_llm_01",
            "tool_input": {"command": "ls -la"},
            # command_invocation intentionally absent
        },
    }

    result = _call_mapper(event)

    assert result is not None, "Expected a non-None result for LLM-initiated tool event"

    meta = _part_metadata(_extract_first_part(result))

    assert COMMAND_INVOCATION_KEY not in meta, (
        f"Expected cinna.command_invocation to be absent for LLM-initiated tool, "
        f"but found: {meta.get(COMMAND_INVOCATION_KEY)!r}"
    )
    # Content kind and tool_name must still be correct — nothing else changed.
    assert meta.get(CONTENT_KIND_KEY) == CONTENT_KIND_TOOL
    assert meta.get(TOOL_NAME_KEY) == "bash"
    assert meta.get(TOOL_ID_KEY) == "exec_llm_01"


# ── 3. tool_result_delta WITH command_invocation ──────────────────────────────


def test_map_stream_event_tool_result_delta_with_command_invocation() -> None:
    """
    ``tool_result_delta`` with metadata.command_invocation="/run:check" produces
    a TextPart whose metadata carries:
      - cinna.content_kind = "tool_result"
      - cinna.tool_id = <exec_id>
      - cinna.tool_stream = "stdout"
      - cinna.command_invocation = "/run:check"
    """
    exec_id = "exec_run_check_02"
    event = {
        "type": "tool_result_delta",
        "content": "All checks passed.",
        "metadata": {
            "tool_id": exec_id,
            "stream": "stdout",
            "command_invocation": "/run:check",
        },
    }

    result = _call_mapper(event)

    assert result is not None, (
        "Expected a non-None result for tool_result_delta with command_invocation"
    )
    assert result.get("kind") == "status-update"

    part = _extract_first_part(result)
    meta = _part_metadata(part)

    assert meta.get(CONTENT_KIND_KEY) == CONTENT_KIND_TOOL_RESULT, (
        f"Expected cinna.content_kind='tool_result', got: {meta.get(CONTENT_KIND_KEY)!r}"
    )
    assert meta.get(TOOL_ID_KEY) == exec_id, (
        f"Expected cinna.tool_id={exec_id!r}, got: {meta.get(TOOL_ID_KEY)!r}"
    )
    assert meta.get(TOOL_STREAM_KEY) == TOOL_STREAM_STDOUT, (
        f"Expected cinna.tool_stream='stdout', got: {meta.get(TOOL_STREAM_KEY)!r}"
    )
    assert meta.get(COMMAND_INVOCATION_KEY) == "/run:check", (
        f"Expected cinna.command_invocation='/run:check', got: {meta.get(COMMAND_INVOCATION_KEY)!r}"
    )
    assert _part_text(part) == "All checks passed.", (
        f"Expected part text preserved, got: {_part_text(part)!r}"
    )


# ── 4. tool_result_delta WITHOUT command_invocation ───────────────────────────


def test_map_stream_event_tool_result_delta_without_command_invocation_key_absent() -> None:
    """
    ``tool_result_delta`` without metadata.command_invocation (LLM-initiated)
    does NOT emit cinna.command_invocation on the TextPart.

    Backward-compatibility check: existing tool_result_delta behaviour is
    unchanged.
    """
    event = {
        "type": "tool_result_delta",
        "content": "output line from LLM tool",
        "metadata": {"tool_id": "exec_llm_02", "stream": "stdout"},
        # command_invocation intentionally absent
    }

    result = _call_mapper(event)

    assert result is not None, "Expected a result for LLM-initiated tool_result_delta"

    meta = _part_metadata(_extract_first_part(result))

    assert COMMAND_INVOCATION_KEY not in meta, (
        f"Expected cinna.command_invocation to be absent for LLM-initiated "
        f"tool_result_delta, but found: {meta.get(COMMAND_INVOCATION_KEY)!r}"
    )
    # Required keys must still be present.
    assert meta.get(CONTENT_KIND_KEY) == CONTENT_KIND_TOOL_RESULT
    assert meta.get(TOOL_STREAM_KEY) == TOOL_STREAM_STDOUT


# ── 5. create_command_result_event WITH command_invocation ────────────────────


def test_create_command_result_event_with_command_invocation() -> None:
    """
    ``create_command_result_event`` called with command_invocation="/files"
    produces a completed status-update whose TextPart metadata carries:
      - cinna.content_kind = "command_result"
      - cinna.command_invocation = "/files"

    This is the sync backend-command path (e.g. /files, /agent-status).
    """
    result = A2AEventMapper.create_command_result_event(
        task_id=_FAKE_TASK_ID,
        context_id=_FAKE_CONTEXT_ID,
        message="No files found in workspace.",
        command_invocation="/files",
    )

    assert result is not None
    assert result.get("kind") == "status-update", (
        f"Expected kind='status-update', got: {result.get('kind')!r}"
    )

    status = result.get("status", {})
    assert status.get("state") == "completed", (
        f"Expected state='completed', got: {status.get('state')!r}"
    )
    assert result.get("final") is True, (
        f"Expected final=True, got: {result.get('final')!r}"
    )

    part = _extract_first_part(result)
    meta = _part_metadata(part)

    assert meta.get(CONTENT_KIND_KEY) == CONTENT_KIND_COMMAND_RESULT, (
        f"Expected cinna.content_kind='command_result', got: {meta.get(CONTENT_KIND_KEY)!r}"
    )
    assert meta.get(COMMAND_INVOCATION_KEY) == "/files", (
        f"Expected cinna.command_invocation='/files', got: {meta.get(COMMAND_INVOCATION_KEY)!r}"
    )
    assert _part_text(part) == "No files found in workspace.", (
        f"Expected part text to match message, got: {_part_text(part)!r}"
    )


# ── 6. create_command_result_event without command_invocation (None) ──────────


def test_create_command_result_event_without_command_invocation_key_absent() -> None:
    """
    ``create_command_result_event`` called without command_invocation (default None)
    omits cinna.command_invocation from the TextPart metadata.

    Backward-compatibility: callers that don't pass the new parameter still get
    a valid command_result event.
    """
    result = A2AEventMapper.create_command_result_event(
        task_id=_FAKE_TASK_ID,
        context_id=_FAKE_CONTEXT_ID,
        message="Some command output.",
        # command_invocation intentionally omitted (uses default None)
    )

    assert result is not None

    part = _extract_first_part(result)
    meta = _part_metadata(part)

    assert meta.get(CONTENT_KIND_KEY) == CONTENT_KIND_COMMAND_RESULT, (
        f"Expected cinna.content_kind='command_result', got: {meta.get(CONTENT_KIND_KEY)!r}"
    )
    assert COMMAND_INVOCATION_KEY not in meta, (
        f"Expected cinna.command_invocation to be absent when None passed, "
        f"but found: {meta.get(COMMAND_INVOCATION_KEY)!r}"
    )


# ── 7. create_command_result_event with empty string command_invocation ────────


def test_create_command_result_event_empty_string_command_invocation_key_absent() -> None:
    """
    ``create_command_result_event`` called with command_invocation="" omits
    cinna.command_invocation from the TextPart metadata.

    An empty string is not a valid invocation and must not poison metadata
    with a blank key.
    """
    result = A2AEventMapper.create_command_result_event(
        task_id=_FAKE_TASK_ID,
        context_id=_FAKE_CONTEXT_ID,
        message="Command output.",
        command_invocation="",
    )

    assert result is not None

    part = _extract_first_part(result)
    meta = _part_metadata(part)

    assert meta.get(CONTENT_KIND_KEY) == CONTENT_KIND_COMMAND_RESULT
    assert COMMAND_INVOCATION_KEY not in meta, (
        f"Expected cinna.command_invocation to be absent for empty-string invocation, "
        f"but found: {meta.get(COMMAND_INVOCATION_KEY)!r}"
    )


# ── 8. History replay: tool event WITH command_invocation ─────────────────────


def test_build_parts_for_session_message_tool_with_command_invocation() -> None:
    """
    History replay: a persisted ``tool`` streaming event carrying
    ``command_invocation`` in its metadata produces a TextPart with
    cinna.command_invocation on replay, matching the live-SSE path (test 1).

    This tests the symmetry guarantee: what went over the wire live also
    comes back from GetTask.
    """
    exec_id = "exec_replay_tool_01"
    streaming_events = [
        {
            "type": "tool",
            "tool_name": "bash",
            "content": "Executing /run:check script...",
            "metadata": {
                "tool_id": exec_id,
                "tool_input": {"command": "uv run /app/scripts/check.py"},
                "command_invocation": "/run:check",
            },
        },
        {
            "type": "assistant",
            "content": "Health check completed.",
            "metadata": {},
        },
    ]
    msg = _make_session_message(
        content="Health check completed.",
        streaming_events=streaming_events,
    )

    parts = A2AEventMapper._build_parts_for_session_message(msg, role="agent")

    assert len(parts) >= 2, (
        f"Expected at least 2 parts (tool + assistant), got {len(parts)}"
    )

    # First part must be the tool part.
    tool_parts = [
        p for p in parts
        if _part_metadata_from_part_obj(p).get(CONTENT_KIND_KEY) == CONTENT_KIND_TOOL
    ]
    assert tool_parts, (
        f"Expected at least one part with cinna.content_kind='tool' in replay parts"
    )

    tool_meta = _part_metadata_from_part_obj(tool_parts[0])
    assert tool_meta.get(COMMAND_INVOCATION_KEY) == "/run:check", (
        f"Expected cinna.command_invocation='/run:check' on replayed tool part, "
        f"got: {tool_meta.get(COMMAND_INVOCATION_KEY)!r}"
    )
    assert tool_meta.get(TOOL_ID_KEY) == exec_id, (
        f"Expected cinna.tool_id={exec_id!r} on replayed tool part, "
        f"got: {tool_meta.get(TOOL_ID_KEY)!r}"
    )


# ── 9. History replay: tool_result_delta WITH command_invocation ──────────────


def test_build_parts_for_session_message_tool_result_delta_with_command_invocation() -> None:
    """
    History replay: a persisted ``tool_result_delta`` event carrying
    ``command_invocation`` in its metadata produces a TextPart with
    cinna.command_invocation on replay, matching the live-SSE path (test 3).
    """
    exec_id = "exec_replay_result_01"
    streaming_events = [
        {
            "type": "tool_result_delta",
            "content": "stdout: check passed",
            "metadata": {
                "tool_id": exec_id,
                "stream": "stdout",
                "command_invocation": "/run:check",
            },
        },
        {
            "type": "assistant",
            "content": "All checks passed.",
            "metadata": {},
        },
    ]
    msg = _make_session_message(
        content="All checks passed.",
        streaming_events=streaming_events,
    )

    parts = A2AEventMapper._build_parts_for_session_message(msg, role="agent")

    assert len(parts) >= 2, (
        f"Expected at least 2 parts (tool_result + assistant), got {len(parts)}"
    )

    tool_result_parts = [
        p for p in parts
        if _part_metadata_from_part_obj(p).get(CONTENT_KIND_KEY) == CONTENT_KIND_TOOL_RESULT
    ]
    assert tool_result_parts, (
        f"Expected at least one part with cinna.content_kind='tool_result' in replay"
    )

    tr_meta = _part_metadata_from_part_obj(tool_result_parts[0])
    assert tr_meta.get(COMMAND_INVOCATION_KEY) == "/run:check", (
        f"Expected cinna.command_invocation='/run:check' on replayed tool_result part, "
        f"got: {tr_meta.get(COMMAND_INVOCATION_KEY)!r}"
    )
    assert tr_meta.get(TOOL_ID_KEY) == exec_id, (
        f"Expected cinna.tool_id={exec_id!r} on replayed tool_result part, "
        f"got: {tr_meta.get(TOOL_ID_KEY)!r}"
    )
    assert tr_meta.get(TOOL_STREAM_KEY) == TOOL_STREAM_STDOUT, (
        f"Expected cinna.tool_stream='stdout' on replayed tool_result part, "
        f"got: {tr_meta.get(TOOL_STREAM_KEY)!r}"
    )


# ── 10. History replay: no command_invocation in persisted events ─────────────


def test_build_parts_for_session_message_without_command_invocation_key_absent() -> None:
    """
    History replay: persisted ``tool`` and ``tool_result_delta`` events that
    do NOT carry ``command_invocation`` (LLM-initiated) produce TextParts
    without cinna.command_invocation.

    This is the symmetry check for the negative case: the key must be absent
    from replayed LLM-tool parts, just as it is absent from live-SSE parts
    (tests 2 and 4).
    """
    exec_id = "exec_replay_llm_01"
    streaming_events = [
        {
            "type": "tool",
            "tool_name": "read",
            "content": "reading file...",
            "metadata": {
                "tool_id": exec_id,
                "tool_input": {"path": "/workspace/notes.txt"},
                # command_invocation intentionally absent
            },
        },
        {
            "type": "tool_result_delta",
            "content": "file content here",
            "metadata": {
                "tool_id": exec_id,
                "stream": "stdout",
                # command_invocation intentionally absent
            },
        },
        {
            "type": "assistant",
            "content": "Based on the file, the answer is 42.",
            "metadata": {},
        },
    ]
    msg = _make_session_message(
        content="Based on the file, the answer is 42.",
        streaming_events=streaming_events,
    )

    parts = A2AEventMapper._build_parts_for_session_message(msg, role="agent")

    assert len(parts) >= 3, (
        f"Expected at least 3 parts (tool + tool_result + assistant), got {len(parts)}"
    )

    for part in parts:
        meta = _part_metadata_from_part_obj(part)
        kind = meta.get(CONTENT_KIND_KEY)
        if kind in (CONTENT_KIND_TOOL, CONTENT_KIND_TOOL_RESULT):
            assert COMMAND_INVOCATION_KEY not in meta, (
                f"Expected cinna.command_invocation to be absent on LLM-initiated "
                f"{kind!r} replay part, but found: {meta.get(COMMAND_INVOCATION_KEY)!r}"
            )
