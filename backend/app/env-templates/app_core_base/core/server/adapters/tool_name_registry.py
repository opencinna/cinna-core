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
# The Cinna Task Protocol tools live on the "cinna" MCP server in both
# adapters (Claude Code in-process server, OpenCode stdio bridge), producing
# mcp__cinna__tasks_create (Claude Code) / cinna_tasks_create (OpenCode).
# Names and schemas come from core/protocols/tasks.schema.json; the unit test
# test_cinna_tasks_parity.py asserts CINNA_TASK_TOOLS equals that file.
#
# No remapping needed — both adapters use the same server names.
# ---------------------------------------------------------------------------

OPENCODE_MCP_TOOL_NAME_MAP: dict[str, str] = {}

CINNA_TASKS_SERVER = "cinna"
CINNA_TASK_TOOLS: tuple[str, ...] = (
    "tasks_targets",
    "tasks_list",
    "tasks_get",
    "tasks_create",
    "tasks_update",
    "tasks_comment",
    "tasks_report",
    "tasks_reply",
)
# Claude Code form (mcp__{server}__{tool}) and OpenCode form ({server}_{tool}).
CINNA_TASK_TOOL_NAMES_CLAUDE: tuple[str, ...] = tuple(
    f"mcp__{CINNA_TASKS_SERVER}__{name}" for name in CINNA_TASK_TOOLS
)
CINNA_TASK_TOOL_NAMES_OPENCODE: tuple[str, ...] = tuple(
    f"{CINNA_TASKS_SERVER}_{name}" for name in CINNA_TASK_TOOLS
)

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
    # MCP bridge tools (knowledge) — Claude Code form
    "mcp__knowledge__query_integration_knowledge",
    # MCP bridge tools (knowledge) — OpenCode form ({server}_{tool})
    "knowledge_query_integration_knowledge",
    # Cinna Task Protocol tools (server "cinna") — both forms
    *CINNA_TASK_TOOL_NAMES_CLAUDE,
    *CINNA_TASK_TOOL_NAMES_OPENCODE,
    # DEPRECATED: the former "agent_task" server, replaced by "cinna" above.
    # Kept only so environments built before the change are still
    # pre-approved; drop in the release after the Cinna Task Protocol.
    # MCP bridge tools (agent task) — Claude Code form
    "mcp__agent_task__add_comment",
    "mcp__agent_task__update_status",
    "mcp__agent_task__create_task",
    "mcp__agent_task__create_subtask",
    "mcp__agent_task__get_details",
    "mcp__agent_task__list_tasks",
    "mcp__agent_task__handover_report",
    # MCP bridge tools (agent task) — OpenCode form ({server}_{tool})
    "agent_task_add_comment",
    "agent_task_update_status",
    "agent_task_create_task",
    "agent_task_create_subtask",
    "agent_task_get_details",
    "agent_task_list_tasks",
    "agent_task_handover_report",
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
        sdk: "claude-code" or "opencode".

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
        # Apply any MCP tool name remappings (currently none)
        if name in OPENCODE_MCP_TOOL_NAME_MAP:
            return OPENCODE_MCP_TOOL_NAME_MAP[name]
        # OpenCode built-ins are already lowercase
        return name

    # Unknown SDK — lowercase as fallback
    return name.lower() if name else name
