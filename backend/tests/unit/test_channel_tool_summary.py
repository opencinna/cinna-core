"""Unit tests for channel_tool_summary — pure event-list logic.

The channel-side twin of the web UI's compact tool blocks: a turn whose
stream held only tool events delivers a fenced one-line-per-call summary
instead of the ``"Agent response"`` finalize placeholder. The API-observable
side (the uuid arm of ``handle_stream_completed`` substituting the summary
for the stored content) rides the same delivery path
``server_channels_turn_identity_test.py`` covers; everything about *what the
summary says* is pure logic and lives here.

Non-vacuity: the assertions below check exact rendered lines (labels, paths,
truncation marks, collapse counts), so any mutation of the label table or
the caps flips them red — no relay, no stub connector, no vacuity hazard on
this file.
"""
from app.services.server_channels.channel_tool_summary import tool_only_summary


def _tool(name: str, **tool_input) -> dict:
    return {
        "type": "tool",
        "content": "",
        "tool_name": name,
        "metadata": {"tool_input": tool_input},
    }


def _lines(summary: str) -> list[str]:
    assert summary.startswith("```\n") and summary.endswith("\n```")
    return summary[4:-4].split("\n")


# ---------------------------------------------------------------------------
# Tool-only detection
# ---------------------------------------------------------------------------


def test_no_summary_when_any_assistant_event_has_content() -> None:
    events = [
        _tool("read", file_path="/app/workspace/a.md"),
        {"type": "assistant", "content": "Here is the answer."},
    ]
    assert tool_only_summary(events) is None


def test_no_summary_when_there_are_no_tool_events() -> None:
    # Thinking-only turn: nothing to summarize; the "turn said nothing"
    # behaviour must stay in charge.
    events = [{"type": "thinking", "content": "hmm"}]
    assert tool_only_summary(events) is None


def test_no_summary_for_empty_or_non_list_input() -> None:
    assert tool_only_summary([]) is None
    assert tool_only_summary(None) is None
    assert tool_only_summary("nonsense") is None


def test_blank_assistant_events_do_not_block_the_summary() -> None:
    # OpenCode can flush an empty assistant fragment; whitespace is not prose.
    events = [
        {"type": "assistant", "content": "   "},
        _tool("read", file_path="/app/workspace/a.md"),
    ]
    summary = tool_only_summary(events)
    assert summary is not None
    assert _lines(summary) == ["Read file: a.md"]


# ---------------------------------------------------------------------------
# Labels — the UI's compact vocabulary
# ---------------------------------------------------------------------------


def test_custom_labels_for_the_special_cased_tools() -> None:
    events = [
        _tool("Read", file_path="/app/workspace/notes.md"),
        _tool("Edit", file_path="/app/workspace/src/main.py"),
        _tool("Write", file_path="/tmp/out.txt"),
        _tool("Bash", command="ls reports/"),
        _tool("WebSearch", query="fastapi streaming"),
        _tool("WebFetch", url="https://example.com/doc"),
        _tool("Glob", pattern="**/*.py"),
        _tool("TodoWrite", todos=[]),
    ]
    assert _lines(tool_only_summary(events)) == [
        "Read file: notes.md",
        "Edit file: src/main.py",
        "Write file: /tmp/out.txt",
        "Run: ls reports/",
        "Search web: fastapi streaming",
        "Fetch: https://example.com/doc",
        "Find files: **/*.py",
        "Update to-do list",
    ]


def test_labels_for_patch_question_and_mcp_tools() -> None:
    events = [
        _tool("apply_patch", patch_text="*** Begin Patch\n..."),
        _tool("AskUserQuestion", questions=[{"question": "Which one?"}]),
        _tool("mcp__knowledge__query_integration_knowledge", query="refund policy"),
        _tool("mcp__agent_task__create_task", title="Follow up with billing"),
        _tool("mcp__agent_task__update_status", status="waiting_on_user"),
    ]
    assert _lines(tool_only_summary(events)) == [
        "Apply patch",
        "Ask a question",
        "Search knowledge: refund policy",
        "Create task: Follow up with billing",
        "Update status: waiting_on_user",
    ]


def test_camel_case_inputs_resolve_like_snake_case() -> None:
    # OpenCode writes filePath where Claude Code writes file_path.
    events = [{
        "type": "tool",
        "tool_name": "edit",
        "metadata": {"tool_input": {"filePath": "/app/workspace/x.py"}},
    }]
    assert _lines(tool_only_summary(events)) == ["Edit file: x.py"]


def test_generic_fallback_names_the_tool_without_its_input() -> None:
    events = [_tool("mcp__custom__do_thing", payload="secret contents")]
    assert _lines(tool_only_summary(events)) == ["Tool: mcp__custom__do_thing"]


def test_missing_input_degrades_to_the_bare_label() -> None:
    events = [{"type": "tool", "tool_name": "read"}]
    assert _lines(tool_only_summary(events)) == ["Read file"]


# ---------------------------------------------------------------------------
# Compactness: truncation, caps, collapse, fence safety
# ---------------------------------------------------------------------------


def test_long_values_truncate_and_multiline_commands_keep_first_line_only() -> None:
    long_command = "echo " + "x" * 200
    events = [_tool("bash", command=f"{long_command}\nrm -rf /never-shown")]
    (line,) = _lines(tool_only_summary(events))
    assert line.startswith("Run: echo ")
    assert line.endswith("…")
    assert len(line) <= 100
    assert "never-shown" not in line


def test_consecutive_identical_calls_collapse_with_a_count() -> None:
    events = [_tool("read", file_path="/app/workspace/a.md")] * 3 + [
        _tool("bash", command="pytest"),
    ]
    assert _lines(tool_only_summary(events)) == [
        "Read file: a.md (×3)",
        "Run: pytest",
    ]


def test_line_cap_counts_the_overflow() -> None:
    events = [_tool("read", file_path=f"/app/workspace/f{i}.md") for i in range(20)]
    lines = _lines(tool_only_summary(events))
    assert len(lines) == 16
    assert lines[-1] == "… and 5 more"


def test_backticks_in_values_cannot_break_the_fence() -> None:
    events = [_tool("bash", command="echo ```dangerous```")]
    summary = tool_only_summary(events)
    # The whole summary must still be exactly one fenced block: the only
    # backticks anywhere are its own two fence markers.
    assert summary.count("```") == 2
    (line,) = _lines(summary)
    assert "`" not in line


def test_a_tilde_fence_line_cannot_close_the_block_either() -> None:
    # Not a property of this module — tildes are passed through untouched —
    # but of the renderer it feeds: ``google_chat_format``'s fence matcher
    # requires a closing marker to match the OPENING marker's character, so a
    # ``~~~`` line inside a backtick block is content, not a terminator. This
    # module's fence safety depends on that rule, so it is pinned here, next
    # to the backtick half of the argument.
    from app.services.server_channels.adapters.google_chat_format import (
        markdown_to_chat,
    )

    events = [_tool("bash", command="~~~")]
    summary = tool_only_summary(events)
    (line,) = _lines(summary)
    assert line == "Run: ~~~"
    rendered = markdown_to_chat(summary + "\nafter the block")
    # The trailing prose must land OUTSIDE the fence: the block closed at its
    # own ```` ``` ````, not at the tilde line.
    assert rendered.endswith("```\nafter the block")
