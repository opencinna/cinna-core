"""
Unified Tool Name Registry

Single source of truth for tool naming across all SDK adapters.

Convention: **lowercase** for all tool names.

Built-in SDK tools use simple lowercase names (e.g. "read", "bash").
MCP bridge tools use the mcp__server__tool_name format (already lowercase).

Claude Code natively emits PascalCase names (Read, Bash, WebFetch).
OpenCode natively emits lowercase names (read, bash, webfetch).

Each adapter is responsible for mapping its native names to the unified
lowercase convention before emitting SDKEvents to the backend.
"""

import re

# ---------------------------------------------------------------------------
# Claude Code PascalCase -> unified lowercase mapping
# ---------------------------------------------------------------------------

CLAUDE_CODE_TOOL_NAME_MAP: dict[str, str] = {
    "Read": "read",
    "Write": "write",
    "Edit": "edit",
    "Bash": "bash",
    "Glob": "glob",
    "Grep": "grep",
    "WebFetch": "webfetch",
    "WebSearch": "websearch",
    "TodoWrite": "todowrite",
    "Task": "task",
    "Skill": "skill",
    "AskUserQuestion": "askuserquestion",
    "EnterPlanMode": "enterplanmode",
    "ExitPlanMode": "exitplanmode",
    "NotebookEdit": "notebookedit",
    "KillShell": "killshell",
    "TaskOutput": "taskoutput",
}

# ---------------------------------------------------------------------------
# OpenCode MCP bridge tool name unification
#
# OpenCode runs collaboration tools on a separate "collaboration" MCP server,
# producing names like mcp__collaboration__create_collaboration.
# Claude Code bundles them on the "task" server: mcp__task__create_collaboration.
#
# We unify to the mcp__task__* prefix so the backend and frontend only need
# one set of names.
# ---------------------------------------------------------------------------

OPENCODE_MCP_TOOL_NAME_MAP: dict[str, str] = {
    "mcp__collaboration__create_collaboration": "mcp__task__create_collaboration",
    "mcp__collaboration__post_finding": "mcp__task__post_finding",
    "mcp__collaboration__get_collaboration_status": "mcp__task__get_collaboration_status",
}

# ---------------------------------------------------------------------------
# Canonical pre-approved tools (lowercase)
#
# These tools never require user approval. Used by the backend message service.
# ---------------------------------------------------------------------------

PRE_APPROVED_TOOLS: frozenset[str] = frozenset([
    # Built-in SDK tools
    "read", "write", "edit", "bash", "glob", "grep",
    "webfetch", "websearch", "todowrite",
    "task", "skill", "askuserquestion",
    "enterplanmode", "exitplanmode", "notebookedit",
    "killshell", "taskoutput",
    # OpenCode-only built-ins
    "list", "patch",
    # MCP bridge tools (knowledge)
    "mcp__knowledge__query_integration_knowledge",
    # MCP bridge tools (task + collaboration — unified under task server)
    "mcp__task__create_agent_task",
    "mcp__task__update_session_state",
    "mcp__task__respond_to_task",
    "mcp__task__create_collaboration",
    "mcp__task__post_finding",
    "mcp__task__get_collaboration_status",
])

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Precompiled regexes for camelCase → snake_case conversion.
# Two-pass approach handles acronyms correctly:
#   "callID"   → "call_ID"  → "call_id"
#   "filePath" → "file_Path" → "file_path"
_RE_ACRONYM_BOUNDARY = re.compile(r"([A-Z]+)([A-Z][a-z])")
_RE_CAMEL_BOUNDARY = re.compile(r"([a-z0-9])([A-Z])")


def _camel_to_snake(name: str) -> str:
    """Convert camelCase to snake_case. Handles acronyms correctly."""
    s1 = _RE_ACRONYM_BOUNDARY.sub(r"\1_\2", name)
    s2 = _RE_CAMEL_BOUNDARY.sub(r"\1_\2", s1)
    return s2.lower()


def normalize_tool_input(tool_input: dict, sdk: str = "opencode") -> dict:
    """
    Normalize tool input parameter keys to snake_case.

    OpenCode uses camelCase keys (filePath, oldString, newString).
    Claude Code uses snake_case keys (file_path, old_string, new_string).
    We unify to snake_case so the frontend and backend only need one convention.

    Only applies to OpenCode — Claude Code already uses snake_case.
    """
    if sdk != "opencode" or not isinstance(tool_input, dict):
        return tool_input

    normalized: dict = {}
    for key, value in tool_input.items():
        snake_key = _camel_to_snake(key)
        normalized[snake_key] = value
    return normalized


def normalize_tool_name(name: str, sdk: str = "claude-code") -> str:
    """
    Normalize a tool name to the unified lowercase convention.

    Args:
        name: Raw tool name from the SDK.
        sdk: "claude-code", "opencode", or "google-adk".

    Returns:
        Unified lowercase tool name.
    """
    if sdk == "claude-code":
        # Check explicit mapping first (handles PascalCase built-ins)
        if name in CLAUDE_CODE_TOOL_NAME_MAP:
            return CLAUDE_CODE_TOOL_NAME_MAP[name]
        # Safety net: lowercase unmapped tools so new PascalCase tools
        # from Claude SDK still normalize correctly
        return name.lower() if name else name

    if sdk == "opencode":
        # Remap collaboration MCP tools to unified task prefix
        if name in OPENCODE_MCP_TOOL_NAME_MAP:
            return OPENCODE_MCP_TOOL_NAME_MAP[name]
        # OpenCode built-ins are already lowercase
        return name

    # Unknown SDK or google-adk — lowercase as fallback
    return name.lower() if name else name
