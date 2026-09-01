"""Compact tool-activity summary for a turn that produced no prose.

A batch whose stream held only tool events still writes an agent
``SessionMessage`` — with the literal ``"Agent response"`` placeholder as its
content (``message_service``'s finalize fallback). The web UI never shows
that placeholder: it renders the stored ``streaming_events``, so a reader
there sees the tool blocks ("Reading file …", "Editing file …"). A channel
reader has no event renderer — until this module, they were sent the
placeholder itself.

This is the channel-side equivalent of the web UI's tool rendering
(``frontend/src/components/Chat/ToolCallBlock.tsx``), deliberately in its
**compact** register only: one line per tool call, custom labels for the
tools the UI special-cases ("Edit file: …", "Search web: …"), a generic
``Tool: name`` for the rest, and never a file's contents or a patch body — a
channel message has no expand/collapse, so either would be the whole screen.
Tool **arguments** do appear, truncated: the command line, the query, the
URL, the path — that is what makes the line say anything, and it is the same
detail the UI's compact blocks show. Worth knowing when reading a group
space: a command line the agent ran is visible to every reader of the
thread, where previously only agent-authored prose was.

The lines ship inside one fenced code block. On Google Chat,
``markdown_to_chat`` masks the fence and passes it through verbatim, so
nothing in a command or a path is reinterpreted as markup. On the polled
email transport — which resolves replies through the same arm — the body
goes out as plain text and the fence arrives as three literal backticks on
their own lines: cosmetic, accepted, and no worse than the raw markdown
agent prose already arrives as by that route.

The summary applies **only** to a turn with no assistant prose at all. A turn
that said anything delivers what it said, exactly as before — tool activity
alongside prose stays invisible on channels, the same trade the streaming
feature made when it deferred tool narration.

Everything here is total: called from the outbound delivery path, whose
discipline (``channel_outbound_service``) is that a formatting decision may
never cost a delivery. Any failure answers ``None``, which means "no summary
— deliver what you already have".
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlmodel import Session as DBSession

from app.models import SessionMessage

logger = logging.getLogger(__name__)

_LOG_PREFIX = "[ChannelToolSummary]"

#: Per-line cap. Chat renders the block in monospace with no wrapping help;
#: a full workspace path plus a long query stops being a summary.
_MAX_LINE_CHARS = 100
#: Line cap per summary. Beyond it the block stops reading as "what happened"
#: and starts reading as a log; the overflow is counted instead.
_MAX_LINES = 15
#: Workspace prefix stripped from paths — every reader of this thread knows
#: the files live in the agent's workspace, and the prefix is a third of the
#: line budget.
_WORKSPACE_PREFIX = "/app/workspace/"


def _input_value(tool_input: dict[str, Any], snake_key: str) -> Any:
    """A tool-input value by snake_case key, falling back to camelCase.

    The same dual lookup the UI's ``ToolCallBlock`` does, for the same
    reason: Claude Code writes ``file_path``, OpenCode writes ``filePath``.
    """
    if snake_key in tool_input:
        return tool_input[snake_key]
    parts = snake_key.split("_")
    camel_key = parts[0] + "".join(part.title() for part in parts[1:])
    return tool_input.get(camel_key)


def _clean(value: Any) -> str:
    """A value flattened to one safe line. Empty string when it has no text.

    Backticks are dropped rather than escaped: a stray ````` ``` ````` inside
    the block would terminate the fence and spill the rest of the summary
    into prose — and Chat's markup offers no escape for it.
    """
    if not isinstance(value, str):
        return ""
    text = value.replace("`", "").strip()
    if not text:
        return ""
    first_line = text.split("\n", 1)[0].strip()
    return first_line


def _short_path(value: Any) -> str:
    path = _clean(value)
    if path.startswith(_WORKSPACE_PREFIX):
        return path[len(_WORKSPACE_PREFIX) :]
    return path


def _labelled(label: str, detail: str) -> str:
    """``"label: detail"``, truncated — or the bare label with no detail."""
    if not detail:
        return label
    line = f"{label}: {detail}"
    if len(line) > _MAX_LINE_CHARS:
        line = line[: _MAX_LINE_CHARS - 1] + "…"
    return line


def _tool_line(tool_name: str, tool_input: dict[str, Any]) -> str:
    """One compact line for one tool call.

    The special cases mirror ``ToolCallBlock.tsx``'s vocabulary in the label
    style the channel spec asked for ("Edit file: path"); everything else is
    the UI's own generic fallback, minus the input dump it can afford and a
    channel cannot.
    """
    name = (tool_name or "").strip()
    lowered = name.lower()

    if lowered == "read":
        return _labelled("Read file", _short_path(_input_value(tool_input, "file_path")))
    if lowered == "write":
        return _labelled("Write file", _short_path(_input_value(tool_input, "file_path")))
    if lowered == "edit":
        return _labelled("Edit file", _short_path(_input_value(tool_input, "file_path")))
    if lowered == "apply_patch":
        return "Apply patch"
    if lowered == "bash":
        return _labelled("Run", _clean(_input_value(tool_input, "command")))
    if lowered == "glob":
        return _labelled(
            "Find files",
            _clean(_input_value(tool_input, "pattern") or tool_input.get("glob")),
        )
    if lowered == "websearch":
        return _labelled("Search web", _clean(_input_value(tool_input, "query")))
    if lowered == "webfetch":
        return _labelled("Fetch", _clean(_input_value(tool_input, "url")))
    if lowered == "todowrite":
        return "Update to-do list"
    if lowered == "askuserquestion":
        return "Ask a question"
    if lowered == "mcp__knowledge__query_integration_knowledge":
        return _labelled("Search knowledge", _clean(_input_value(tool_input, "query")))
    if lowered == "mcp__agent_task__create_task":
        return _labelled("Create task", _clean(_input_value(tool_input, "title")))
    if lowered == "mcp__agent_task__update_status":
        return _labelled("Update status", _clean(_input_value(tool_input, "status")))

    return _labelled("Tool", _clean(name) or "unknown")


def tool_only_summary(events: Any) -> str | None:
    """A fenced summary of ``events``' tool calls — or ``None``.

    ``None`` unless the events describe a **tool-only** turn: at least one
    ``tool`` event and no ``assistant`` event with non-blank content. A turn
    that said anything must deliver what it said; a turn that did nothing at
    all (no tool events either) has nothing to summarize and keeps the
    existing "turn said nothing" behaviour.

    Consecutive identical lines collapse into one with a count — a retry loop
    reading the same file ten times is one fact, not ten lines.
    """
    try:
        if not isinstance(events, list) or not events:
            return None

        tool_lines: list[str] = []
        for event in events:
            if not isinstance(event, dict):
                continue
            event_type = event.get("type")
            if event_type == "assistant" and str(event.get("content") or "").strip():
                return None
            if event_type != "tool":
                continue
            metadata = event.get("metadata")
            tool_input = metadata.get("tool_input") if isinstance(metadata, dict) else None
            tool_lines.append(
                _tool_line(
                    str(event.get("tool_name") or ""),
                    tool_input if isinstance(tool_input, dict) else {},
                )
            )

        if not tool_lines:
            return None

        collapsed: list[str] = []
        counts: list[int] = []
        for line in tool_lines:
            if collapsed and collapsed[-1] == line:
                counts[-1] += 1
            else:
                collapsed.append(line)
                counts.append(1)
        rendered = [
            line if count == 1 else f"{line} (×{count})"
            for line, count in zip(collapsed, counts)
        ]

        if len(rendered) > _MAX_LINES:
            overflow = len(rendered) - _MAX_LINES
            rendered = rendered[:_MAX_LINES] + [f"… and {overflow} more"]

        return "```\n" + "\n".join(rendered) + "\n```"
    except Exception:  # noqa: BLE001 — a summary may never cost a delivery
        logger.warning(
            "%s Could not summarize tool events — delivering without a summary",
            _LOG_PREFIX,
            exc_info=True,
        )
        return None


def tool_only_summary_for_message(
    db: DBSession, message_uuid: uuid.UUID
) -> str | None:
    """:func:`tool_only_summary` over a stored agent message's events.

    Reads ``message_metadata["streaming_events"]`` from the row —
    ``tool_name`` and ``tool_input`` both survive storage
    (``_STORED_EVENT_METADATA_KEYS``). A row that is gone, has no metadata,
    or predates event storage answers ``None``: with nothing to prove the
    turn was tool-only, the caller keeps delivering the stored content
    exactly as before. Total, like everything on this path.
    """
    try:
        row = db.get(SessionMessage, message_uuid)
        if row is None:
            return None
        metadata = row.message_metadata or {}
        return tool_only_summary(metadata.get("streaming_events"))
    except Exception:  # noqa: BLE001 — see the docstring
        logger.warning(
            "%s Could not read agent message %s for a tool summary",
            _LOG_PREFIX,
            message_uuid,
            exc_info=True,
        )
        return None


__all__ = ["tool_only_summary", "tool_only_summary_for_message"]
