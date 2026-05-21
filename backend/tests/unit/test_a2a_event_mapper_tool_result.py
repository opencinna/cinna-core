"""
Unit tests for A2AEventMapper.map_stream_event — tool_result_delta branch.

These tests call the mapper function directly with no HTTP, no DB, and no
FastAPI TestClient. They verify the shape of the returned status-update dict
for the tool_result_delta event type.

Test cases:
  1. stream="stdout"  → cinna.content_kind="tool_result", cinna.tool_stream="stdout",
                        cinna.tool_id set, text matches content.
  2. stream="stderr"  → cinna.tool_stream="stderr".
  3. stream key absent → default cinna.tool_stream="stdout".
  4. content=""       → returns None (skipped, consistent with assistant/tool/thinking).
  5. tool_id absent   → cinna.tool_id key omitted from part metadata (don't emit
                        empty strings; chunk still delivered).

These are mapper-level unit tests; service-layer imports are allowed here
following the same pattern as other files in tests/unit/.
"""
from app.services.a2a.a2a_event_mapper import (
    A2AEventMapper,
    CONTENT_KIND_KEY,
    CONTENT_KIND_TOOL_RESULT,
    TOOL_ID_KEY,
    TOOL_STREAM_KEY,
    TOOL_STREAM_STDOUT,
    TOOL_STREAM_STDERR,
)

_FAKE_TASK_ID = "task-unit-001"
_FAKE_CONTEXT_ID = "ctx-unit-001"


# ── Helpers ───────────────────────────────────────────────────────────────────


def _call_mapper(event: dict) -> dict | None:
    """Convenience wrapper for the mapper call shared across unit tests."""
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


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_map_stream_event_tool_result_delta_stdout() -> None:
    """
    tool_result_delta with stream="stdout" produces a status-update dict with:
    - kind="status-update"
    - status.state="working", final=False
    - TextPart.text matches content
    - cinna.content_kind="tool_result"
    - cinna.tool_id set to the emitted exec_id
    - cinna.tool_stream="stdout"
    """
    event = {
        "type": "tool_result_delta",
        "content": "hello stdout",
        "metadata": {"tool_id": "exec_abc", "stream": "stdout"},
    }

    result = _call_mapper(event)

    assert result is not None, (
        "Expected a non-None result for tool_result_delta with non-empty content"
    )
    assert result.get("kind") == "status-update", (
        f"Expected kind='status-update', got: {result.get('kind')!r}"
    )

    status = result.get("status", {})
    assert status.get("state") == "working", (
        f"Expected state='working', got: {status.get('state')!r}"
    )
    assert result.get("final") is False, (
        f"Expected final=False, got: {result.get('final')!r}"
    )

    part = _extract_first_part(result)
    assert _part_text(part) == "hello stdout", (
        f"Expected text='hello stdout', got: {_part_text(part)!r}"
    )

    meta = _part_metadata(part)
    assert meta.get(CONTENT_KIND_KEY) == CONTENT_KIND_TOOL_RESULT, (
        f"Expected cinna.content_kind='tool_result', got: {meta.get(CONTENT_KIND_KEY)!r}"
    )
    assert meta.get(TOOL_ID_KEY) == "exec_abc", (
        f"Expected cinna.tool_id='exec_abc', got: {meta.get(TOOL_ID_KEY)!r}"
    )
    assert meta.get(TOOL_STREAM_KEY) == TOOL_STREAM_STDOUT, (
        f"Expected cinna.tool_stream='stdout', got: {meta.get(TOOL_STREAM_KEY)!r}"
    )


def test_map_stream_event_tool_result_delta_stderr() -> None:
    """
    tool_result_delta with stream="stderr" produces cinna.tool_stream="stderr".
    All other metadata is the same as for stdout.
    """
    event = {
        "type": "tool_result_delta",
        "content": "error: command not found",
        "metadata": {"tool_id": "exec_xyz", "stream": "stderr"},
    }

    result = _call_mapper(event)

    assert result is not None, "Expected a non-None result for stderr tool_result_delta"
    assert result.get("kind") == "status-update"

    part = _extract_first_part(result)
    meta = _part_metadata(part)

    assert meta.get(CONTENT_KIND_KEY) == CONTENT_KIND_TOOL_RESULT
    assert meta.get(TOOL_ID_KEY) == "exec_xyz", (
        f"Expected cinna.tool_id='exec_xyz', got: {meta.get(TOOL_ID_KEY)!r}"
    )
    assert meta.get(TOOL_STREAM_KEY) == TOOL_STREAM_STDERR, (
        f"Expected cinna.tool_stream='stderr', got: {meta.get(TOOL_STREAM_KEY)!r}"
    )
    assert _part_text(part) == "error: command not found", (
        f"Expected chunk text to be preserved, got: {_part_text(part)!r}"
    )


def test_map_stream_event_tool_result_delta_default_stream() -> None:
    """
    tool_result_delta with metadata present but stream key absent defaults
    cinna.tool_stream to "stdout".
    """
    event = {
        "type": "tool_result_delta",
        "content": "some output",
        "metadata": {"tool_id": "exec_nostream"},
        # stream key intentionally absent
    }

    result = _call_mapper(event)

    assert result is not None, "Expected a non-None result when stream key is absent"

    meta = _part_metadata(_extract_first_part(result))
    assert meta.get(TOOL_STREAM_KEY) == TOOL_STREAM_STDOUT, (
        f"Expected default cinna.tool_stream='stdout' when stream key missing, "
        f"got: {meta.get(TOOL_STREAM_KEY)!r}"
    )
    assert meta.get(CONTENT_KIND_KEY) == CONTENT_KIND_TOOL_RESULT


def test_map_stream_event_tool_result_delta_empty_content_returns_none() -> None:
    """
    tool_result_delta with empty content returns None — the event is skipped and
    no SSE frame is emitted. Consistent with the same empty-content guard on the
    assistant, tool, and thinking branches.
    """
    event = {
        "type": "tool_result_delta",
        "content": "",
        "metadata": {"tool_id": "exec_empty", "stream": "stdout"},
    }

    result = _call_mapper(event)

    assert result is None, (
        f"Expected None for empty-content tool_result_delta, got: {result!r}"
    )


def test_map_stream_event_tool_result_delta_missing_tool_id_omits_key() -> None:
    """
    tool_result_delta without metadata.tool_id does not add cinna.tool_id to the
    part metadata. The chunk is still delivered (clients can render text); they
    just cannot pair it with a prior tool part.

    Mirrors the same pattern in the tool branch: only set TOOL_ID_KEY when the
    value is a non-empty string.
    """
    event = {
        "type": "tool_result_delta",
        "content": "output without id",
        "metadata": {"stream": "stdout"},
        # tool_id intentionally absent
    }

    result = _call_mapper(event)

    assert result is not None, "Expected a result even when tool_id is absent"

    meta = _part_metadata(_extract_first_part(result))
    assert TOOL_ID_KEY not in meta, (
        f"Expected cinna.tool_id to be absent when tool_id is missing, "
        f"but found it: {meta.get(TOOL_ID_KEY)!r}"
    )
    # content_kind and stream must still be present
    assert meta.get(CONTENT_KIND_KEY) == CONTENT_KIND_TOOL_RESULT, (
        f"Expected cinna.content_kind='tool_result', got: {meta.get(CONTENT_KIND_KEY)!r}"
    )
    assert meta.get(TOOL_STREAM_KEY) == TOOL_STREAM_STDOUT, (
        f"Expected cinna.tool_stream='stdout', got: {meta.get(TOOL_STREAM_KEY)!r}"
    )
