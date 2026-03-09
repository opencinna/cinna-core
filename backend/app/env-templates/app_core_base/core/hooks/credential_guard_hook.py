#!/usr/bin/env python3
"""
Credential Guard Hook — Claude Code PreToolUse hook

This script is invoked by Claude Code before tool execution when the tool
matches Bash|Read|Write|Edit. It checks whether the tool targets credential
files and, if so, reports the event to the backend via the environment server.

Protocol:
- Input: JSON object on stdin with tool_name and tool_input fields
- Output: Optionally JSON on stdout to provide a decision
- Exit code 0 = allow (proceed normally)
- Exit code 2 = block (Claude Code denies the tool call)

See: https://docs.anthropic.com/en/docs/claude-code/hooks

The hook is fail-open: any error (import failure, JSON parse error, network
timeout) results in exit code 0 (allow). Availability is not sacrificed for
security at this layer — the output redaction pipeline (Phase 2) provides a
second layer of defense.

Hook configuration (written by environment_lifecycle.py):
    /app/core/.claude/settings.json
    {
      "hooks": {
        "PreToolUse": [
          {
            "matcher": "Bash|Read|Write|Edit",
            "hooks": [
              {
                "type": "command",
                "command": "python3 /app/core/hooks/credential_guard_hook.py"
              }
            ]
          }
        ]
      }
    }
"""
import json
import sys
import os

# Add the core server to the Python path so we can use shared modules
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_CORE_DIR = os.path.dirname(_SCRIPT_DIR)  # /app/core/
_SERVER_DIR = os.path.join(_CORE_DIR, "server")
sys.path.insert(0, _SERVER_DIR)


def main() -> None:
    """
    Main hook entry point.

    Reads tool call JSON from stdin, checks for credential access patterns,
    and exits with code 0 (allow) or 2 (block).
    """
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            sys.exit(0)
        tool_data = json.loads(raw)
    except Exception:
        # JSON parse failure or empty stdin — allow (fail-open)
        sys.exit(0)

    tool_name = tool_data.get("tool_name", "")
    tool_input = tool_data.get("tool_input", {})

    # Determine what to check based on tool type
    tool_type = _get_tool_type(tool_name)
    if tool_type is None:
        sys.exit(0)  # Not a tool we intercept

    input_value = _extract_input_value(tool_name, tool_input)
    if not input_value:
        sys.exit(0)

    try:
        from security.credential_access_detector import is_credential_access, get_event_type
    except ImportError:
        sys.exit(0)  # Module not available — fail-open

    if not is_credential_access(input_value, tool_type):
        sys.exit(0)  # Not a credential access — allow

    # Credential access detected — report and get action decision
    event_type = get_event_type(tool_type)
    action = _report_event(
        event_type=event_type,
        tool_name=tool_name,
        tool_input=input_value,
        session_id=tool_data.get("session_id"),
    )

    if action == "block":
        decision = json.dumps({
            "decision": "block",
            "reason": "Credential file access denied by security policy.",
        })
        print(decision)
        sys.exit(2)

    sys.exit(0)


def _get_tool_type(tool_name: str) -> str | None:
    """Map Claude Code tool name to our internal tool type."""
    mapping = {
        "Read": "read",
        "Write": "write",
        "Edit": "edit",
        "MultiEdit": "edit",
        "Bash": "bash",
    }
    return mapping.get(tool_name)


def _extract_input_value(tool_name: str, tool_input: dict) -> str:
    """Extract the relevant string value from tool input based on tool type."""
    if tool_name == "Bash":
        return tool_input.get("command", "")
    elif tool_name in ("Read", "Write", "Edit", "MultiEdit"):
        return tool_input.get("file_path", "")
    return ""


def _report_event(
    event_type: str,
    tool_name: str,
    tool_input: str,
    session_id: str | None,
) -> str:
    """
    Report the security event to the backend and return action decision.

    Returns "allow" on any error (fail-open).
    """
    try:
        from security.event_reporter import SecurityEventReporter
        reporter = SecurityEventReporter()
        return reporter.report(
            event_type=event_type,
            tool_name=tool_name,
            tool_input=tool_input,
            session_id=session_id,
            severity="high",
        )
    except Exception:
        return "allow"


if __name__ == "__main__":
    main()
