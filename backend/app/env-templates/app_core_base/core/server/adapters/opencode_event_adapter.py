"""
OpenCode Event Adapter

Translates raw OpenCode SSE events into the unified SDKEvent format used by
all adapters. This module is intentionally stateful — it tracks part types
and text buffers across events within a single message exchange.

The adapter can be tested in isolation by feeding it raw event dicts and
checking the SDKEvent output.

Raw event logging (controlled by DUMP_LLM_SESSION env var) records every
incoming SSE event to a JSONL file for offline debugging and replay.

OpenCode SSE event types (observed from opencode serve 1.2.x):

    server.connected       — SSE stream connected
    server.heartbeat       — keep-alive ping

    message.updated        — message metadata (role, tokens, cost)
    message.part.updated   — complete part snapshot
    message.part.delta     — incremental text delta

    session.updated        — session metadata
    session.status         — busy / idle transition
    session.idle           — definitive completion signal
    session.diff           — file change diffs

    permission.asked       — tool requires user approval

Part types inside message.part.updated:

    text          — assistant text content
    reasoning     — chain-of-thought (not shown to user)
    tool          — tool invocation (callID, tool, state, input/output)
    step-start    — marks beginning of an LLM step
    step-finish   — marks end of an LLM step (includes reason, tokens, cost)
"""

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

from .base import SDKEvent, SDKEventType

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Raw event logger
# ---------------------------------------------------------------------------

class OpenCodeEventLogger:
    """
    Records bi-directional OpenCode communication to a JSONL file.

    Each line is a JSON object with:
    - ts: ISO timestamp
    - dir: "recv" (SSE event from opencode) or "send" (request to opencode)
    - event: the raw data

    This makes it possible to replay the full exchange in tests.
    Enabled when DUMP_LLM_SESSION=true.
    """

    def __init__(self, logs_dir: Path, enabled: bool = False):
        self.enabled = enabled
        self._log_file: Optional[Path] = None
        if enabled:
            logs_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S_%f")
            self._log_file = logs_dir / f"opencode_session_{ts}.jsonl"
            logger.info("OpenCode session logging enabled: %s", self._log_file)

    def _write(self, direction: str, event_data: dict) -> None:
        if not self._log_file:
            return
        try:
            record = {
                "ts": datetime.utcnow().isoformat(),
                "dir": direction,
                "event": event_data,
            }
            with open(self._log_file, "a") as f:
                f.write(json.dumps(record, default=str) + "\n")
        except Exception as exc:
            logger.debug("Failed to write session log: %s", exc)

    def log_recv(self, event_data: dict) -> None:
        """Log an event received from opencode (SSE)."""
        self._write("recv", event_data)

    def log_send(self, action: str, data: dict) -> None:
        """Log a request sent to opencode (HTTP)."""
        self._write("send", {"action": action, **data})


# ---------------------------------------------------------------------------
# Event adapter
# ---------------------------------------------------------------------------

class OpenCodeEventAdapter:
    """
    Stateful translator from raw OpenCode SSE events to SDKEvent objects.

    Text buffering strategy:
    - Deltas are accumulated per partID.
    - When the buffer contains a newline, everything up to (and including)
      the last newline is flushed immediately as an ASSISTANT event.
    - The remainder stays in the buffer for the next delta.
    - When the part finishes (message.part.updated with time.end), whatever
      is left in the buffer is flushed.

    This produces a natural streaming cadence similar to Claude Code, where
    each event contains one or more complete lines.
    """

    def __init__(self, workspace_dir: str):
        # Part type tracking: partID -> part type string
        self._part_types: dict[str, str] = {}

        # Text accumulation: partID -> buffered text
        self._text_buffers: dict[str, str] = {}

        # Raw event logger
        dump_enabled = os.getenv("DUMP_LLM_SESSION", "false").lower() == "true"
        logs_dir = Path(workspace_dir) / "logs"
        self.event_logger = OpenCodeEventLogger(logs_dir, enabled=dump_enabled)

    def reset(self) -> None:
        """Clear state between messages (not between sessions)."""
        self._part_types.clear()
        self._text_buffers.clear()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def translate(
        self,
        event_data: dict,
        session_id: str,
    ) -> list[SDKEvent]:
        """
        Translate a single raw OpenCode event into zero or more SDKEvents.

        Returns a list because some events (e.g. text flush + tool start)
        can produce multiple SDKEvents.
        """
        self.event_logger.log_recv(event_data)

        event_type = event_data.get("type", "")
        properties = event_data.get("properties", {})

        # -- Error events --------------------------------------------------
        if "error" in event_type.lower() or properties.get("error"):
            return [self._make_error(event_type, properties, session_id)]

        # -- Session idle — definitive completion --------------------------
        if event_type == "session.idle":
            # Flush any remaining text buffers before DONE
            events = self._flush_all_buffers(session_id)
            events.append(SDKEvent(
                type=SDKEventType.DONE,
                session_id=session_id,
                content="",
                metadata={"opencode_event_type": event_type},
            ))
            return events

        # -- Part snapshot -------------------------------------------------
        if event_type == "message.part.updated":
            return self._handle_part_updated(properties, session_id)

        # -- Text delta ----------------------------------------------------
        if event_type == "message.part.delta":
            return self._handle_part_delta(properties, session_id)

        # -- Permission request (tool approval) ----------------------------
        if event_type == "permission.asked":
            return self._handle_permission_asked(properties, session_id)

        # -- Informational events (silently skip) --------------------------
        if event_type in (
            "message.updated", "session.updated", "session.status",
            "session.diff", "server.connected", "server.heartbeat",
            "project.updated",
        ):
            return []

        # -- Unknown -------------------------------------------------------
        logger.debug("Unhandled OpenCode SSE event: %s", event_type)
        return []

    # ------------------------------------------------------------------
    # Part handlers
    # ------------------------------------------------------------------

    def _handle_part_updated(
        self, properties: dict, session_id: str,
    ) -> list[SDKEvent]:
        part = properties.get("part", {})
        part_id = part.get("id", "")
        part_type = part.get("type", "")

        # Track for delta events
        if part_id and part_type:
            self._part_types[part_id] = part_type

        # -- Tool ----------------------------------------------------------
        if part_type == "tool":
            return self._handle_tool_part(part, session_id)

        # -- Text complete -------------------------------------------------
        if part_type == "text":
            time_info = part.get("time", {})
            if time_info.get("end"):
                # Part finished — flush buffer (prefer buffer over snapshot
                # since buffer was accumulated from deltas).
                text = self._text_buffers.pop(part_id, "") or part.get("text", "")
                if text:
                    return [SDKEvent(
                        type=SDKEventType.ASSISTANT,
                        session_id=session_id,
                        content=text,
                    )]
            return []

        # -- Reasoning complete --------------------------------------------
        if part_type == "reasoning":
            time_info = part.get("time", {})
            if time_info.get("end"):
                # Part finished — flush buffer
                text = self._text_buffers.pop(part_id, "") or part.get("text", "")
                if text:
                    return [SDKEvent(
                        type=SDKEventType.THINKING,
                        session_id=session_id,
                        content=text,
                    )]
            return []

        # -- step-finish ---------------------------------------------------
        if part_type == "step-finish":
            reason = part.get("reason", "")
            if reason == "stop":
                logger.debug("Step finished (reason=stop)")
            return []

        # step-start — skip
        return []

    def _handle_tool_part(
        self, part: dict, session_id: str,
    ) -> list[SDKEvent]:
        """
        Handle a tool part (type=tool).

        OpenCode tool state structure:
            {"status": "pending"|"running"|"completed"|"error",
             "input": {...},              # present when running/completed
             "output": "...",             # present when completed
             "error": "...",              # present when error
             "time": {"start": ..., "end": ...}}
        """
        tool_name = part.get("tool", "")
        call_id = part.get("callID", "")
        state = part.get("state", {})

        # state can be a dict {"status": ..., "input": ...} or sometimes
        # a plain string in older versions — handle both.
        if isinstance(state, str):
            status = state
            tool_input = part.get("input", {})
            tool_output = part.get("output", "")
            tool_error = part.get("error", "")
        else:
            status = state.get("status", "")
            tool_input = state.get("input", {})
            tool_output = state.get("output", "")
            tool_error = state.get("error", "")

        if status in ("pending", "running"):
            # Only emit TOOL_USE when we have input (running state)
            if not tool_input:
                return []
            try:
                input_str = json.dumps(tool_input, indent=2)
                if len(input_str) > 200:
                    input_str = input_str[:200] + "..."
            except Exception:
                input_str = str(tool_input)[:200]

            return [SDKEvent(
                type=SDKEventType.TOOL_USE,
                session_id=session_id,
                tool_name=tool_name,
                content=f"Using tool: {tool_name}\nInput: {input_str}",
                metadata={
                    "tool_call_id": call_id,
                    "tool_input": tool_input,
                },
            )]

        if status == "completed":
            result_str = str(tool_output)[:500] if tool_output else ""
            return [SDKEvent(
                type=SDKEventType.TOOL_RESULT,
                session_id=session_id,
                content=result_str,
                metadata={
                    "tool_call_id": call_id,
                    "tool_name": tool_name,
                },
            )]

        if status == "error":
            error_msg = tool_error or "Tool execution failed"
            return [SDKEvent(
                type=SDKEventType.TOOL_RESULT,
                session_id=session_id,
                content=f"Error: {error_msg}",
                metadata={
                    "tool_call_id": call_id,
                    "tool_name": tool_name,
                    "is_error": True,
                },
            )]

        return []

    def _handle_part_delta(
        self, properties: dict, session_id: str,
    ) -> list[SDKEvent]:
        """Buffer text/reasoning deltas, flush on newline boundaries."""
        part_id = properties.get("partID", "")
        part_type = self._part_types.get(part_id, "")
        delta = properties.get("delta", "")

        if part_type == "text":
            event_type = SDKEventType.ASSISTANT
        elif part_type == "reasoning":
            event_type = SDKEventType.THINKING
        else:
            return []

        if not delta:
            return []

        buf = self._text_buffers.get(part_id, "") + delta

        # Flush up to (and including) the last newline
        last_nl = buf.rfind("\n")
        if last_nl >= 0:
            flush = buf[: last_nl + 1]
            self._text_buffers[part_id] = buf[last_nl + 1 :]
            return [SDKEvent(
                type=event_type,
                session_id=session_id,
                content=flush,
            )]

        # No newline yet — keep buffering
        self._text_buffers[part_id] = buf
        return []

    def _handle_permission_asked(
        self, properties: dict, session_id: str,
    ) -> list[SDKEvent]:
        """Forward tool permission requests to the backend."""
        # Build a human-readable summary so the frontend can display it
        # even without a dedicated permission UI component.
        permission_type = properties.get("permission", "unknown")
        patterns = properties.get("patterns", [])
        tool_info = properties.get("tool", {})
        call_id = tool_info.get("callID", "")

        # e.g. "Permission requested: external_directory for /app/workspace/scripts/*"
        pattern_str = ", ".join(patterns) if patterns else ""
        summary = f"Permission requested: {permission_type}"
        if pattern_str:
            summary += f" for {pattern_str}"
        if call_id:
            summary += f" (tool call: {call_id})"

        return [SDKEvent(
            type=SDKEventType.SYSTEM,
            subtype="permission_asked",
            session_id=session_id,
            content=summary,
            metadata={
                "opencode_event_type": "permission.asked",
                "permission": properties,
            },
        )]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _flush_all_buffers(self, session_id: str) -> list[SDKEvent]:
        """Flush all remaining text/reasoning buffers."""
        events: list[SDKEvent] = []
        for part_id in list(self._text_buffers):
            text = self._text_buffers.pop(part_id, "")
            if text:
                part_type = self._part_types.get(part_id, "text")
                event_type = (
                    SDKEventType.THINKING if part_type == "reasoning"
                    else SDKEventType.ASSISTANT
                )
                events.append(SDKEvent(
                    type=event_type,
                    session_id=session_id,
                    content=text,
                ))
        return events

    @staticmethod
    def _make_error(
        event_type: str, properties: dict, session_id: str,
    ) -> SDKEvent:
        error_msg = (
            properties.get("error")
            or properties.get("message")
            or event_type
        )
        if isinstance(error_msg, dict):
            error_msg = error_msg.get("message", str(error_msg))
        return SDKEvent(
            type=SDKEventType.ERROR,
            session_id=session_id,
            content=str(error_msg),
            error_type="OpenCodeError",
            metadata={"opencode_event_type": event_type},
        )
