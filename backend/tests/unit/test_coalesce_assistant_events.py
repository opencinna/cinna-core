"""Unit tests for ``_coalesce_assistant_events`` — pure list transformation.

Some SDK adapters (notably OpenCode) flush assistant text on every newline,
producing one ``assistant`` streaming event per line. Rendering each event as an
independent markdown block shatters multi-line constructs (code fences, tables,
lists). ``_coalesce_assistant_events`` merges runs of consecutive ``assistant``
events back into whole blocks — matching Claude Code's one-event-per-block shape
— while leaving tool / thinking / webapp_action / attachment events (and the
interleaving boundaries they create) untouched.

No database, no HTTP — all pure Python.
"""
from __future__ import annotations

from app.services.sessions.message_service import _coalesce_assistant_events


def _seqs(events: list[dict]) -> list[int]:
    return [e["event_seq"] for e in events]


def test_empty_list_returns_empty():
    assert _coalesce_assistant_events([]) == []


def test_single_assistant_event_unchanged_content():
    events = [{"type": "assistant", "content": "hello", "event_seq": 1}]
    out = _coalesce_assistant_events(events)
    assert len(out) == 1
    assert out[0]["content"] == "hello"
    assert out[0]["event_seq"] == 1


def test_consecutive_assistant_events_merge_and_concatenate():
    # The OpenCode failure mode: a fenced code block flushed line-by-line.
    events = [
        {"type": "assistant", "content": "Created file:\n\n", "event_seq": 1},
        {"type": "assistant", "content": "```text\n", "event_seq": 2},
        {"type": "assistant", "content": "line1\n", "event_seq": 3},
        {"type": "assistant", "content": "line2\n", "event_seq": 4},
        {"type": "assistant", "content": "```", "event_seq": 5},
    ]
    out = _coalesce_assistant_events(events)
    assert len(out) == 1
    assert out[0]["content"] == "Created file:\n\n```text\nline1\nline2\n```"
    assert _seqs(out) == [1]


def test_other_event_types_break_the_run():
    # assistant runs separated by a tool / tool_result must NOT merge across them.
    events = [
        {"type": "thinking", "content": "t", "event_seq": 1},
        {"type": "assistant", "content": "Intro.", "event_seq": 2},
        {"type": "tool", "content": "tool", "event_seq": 3, "tool_name": "bash"},
        {"type": "tool_result", "content": "res", "event_seq": 4},
        {"type": "assistant", "content": "a\n", "event_seq": 5},
        {"type": "assistant", "content": "b", "event_seq": 6},
    ]
    out = _coalesce_assistant_events(events)
    assert [e["type"] for e in out] == ["thinking", "assistant", "tool", "tool_result", "assistant"]
    assert out[1]["content"] == "Intro."
    assert out[-1]["content"] == "a\nb"


def test_event_seq_is_renumbered_contiguously_after_merge():
    # A merged run in the middle must not leave a gap in event_seq.
    events = [
        {"type": "assistant", "content": "x", "event_seq": 1},
        {"type": "assistant", "content": "y", "event_seq": 2},
        {"type": "tool", "content": "t", "event_seq": 3, "tool_name": "bash"},
    ]
    out = _coalesce_assistant_events(events)
    assert _seqs(out) == [1, 2]


def test_first_chunk_metadata_is_preserved():
    events = [
        {"type": "assistant", "content": "a", "event_seq": 1, "metadata": {"model": "sonnet"}},
        {"type": "assistant", "content": "b", "event_seq": 2, "metadata": {"model": "ignored"}},
    ]
    out = _coalesce_assistant_events(events)
    assert len(out) == 1
    assert out[0]["metadata"] == {"model": "sonnet"}


def test_later_chunk_metadata_backfills_missing_keys():
    events = [
        {"type": "assistant", "content": "a", "event_seq": 1, "metadata": {"model": "sonnet"}},
        {"type": "assistant", "content": "b", "event_seq": 2, "metadata": {"extra": "v"}},
    ]
    out = _coalesce_assistant_events(events)
    assert out[0]["metadata"] == {"model": "sonnet", "extra": "v"}


def test_input_events_are_not_mutated():
    events = [
        {"type": "assistant", "content": "a", "event_seq": 1},
        {"type": "assistant", "content": "b", "event_seq": 2},
    ]
    _coalesce_assistant_events(events)
    # Originals untouched (function copies before merging).
    assert events[0]["content"] == "a"
    assert events[1]["content"] == "b"


def test_empty_content_assistant_events_merge_harmlessly():
    events = [
        {"type": "assistant", "content": "", "event_seq": 1},
        {"type": "assistant", "content": "real", "event_seq": 2},
    ]
    out = _coalesce_assistant_events(events)
    assert len(out) == 1
    assert out[0]["content"] == "real"


def test_claude_code_shape_is_a_noop():
    # Claude Code already emits one whole-block assistant event per turn,
    # separated by tools — coalescing must leave it structurally identical.
    events = [
        {"type": "assistant", "content": "I'll create a file.", "event_seq": 1, "metadata": {"model": "m"}},
        {"type": "tool", "content": "write", "event_seq": 2, "tool_name": "write"},
        {"type": "assistant", "content": "Done, see `/x`.", "event_seq": 3, "metadata": {"model": "m"}},
        {"type": "attachment", "content": "x.txt", "event_seq": 4, "metadata": {"file_id": "f"}},
    ]
    out = _coalesce_assistant_events(events)
    assert [e["type"] for e in out] == ["assistant", "tool", "assistant", "attachment"]
    assert [e["content"] for e in out] == [e["content"] for e in events]
    assert _seqs(out) == [1, 2, 3, 4]
