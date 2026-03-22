"""
Unit tests for the tool_name_registry module.

Tests cover:
- normalize_tool_name() for Claude Code PascalCase → lowercase mapping
- normalize_tool_name() for OpenCode mcp__collaboration__* → mcp__task__* remapping
- normalize_tool_name() passthrough for already-lowercase names
- normalize_tool_name() fallback for unknown SDK
- normalize_tool_input() camelCase → snake_case conversion
- PRE_APPROVED_TOOLS set completeness and convention
- CLAUDE_CODE_TOOL_NAME_MAP and OPENCODE_MCP_TOOL_NAME_MAP contents

Run:
    cd backend && python -m pytest tests/unit/test_tool_name_registry.py -v
"""

import pytest

# sys.path setup is handled by tests/unit/conftest.py
from core.server.adapters.tool_name_registry import (
    CLAUDE_CODE_TOOL_NAME_MAP,
    OPENCODE_MCP_TOOL_NAME_MAP,
    PRE_APPROVED_TOOLS,
    normalize_tool_name,
    normalize_tool_input,
    _camel_to_snake,
)


# ---------------------------------------------------------------------------
# normalize_tool_name — Claude Code SDK
# ---------------------------------------------------------------------------


class TestNormalizeToolNameClaudeCode:
    """normalize_tool_name with sdk='claude-code' maps PascalCase to lowercase."""

    def test_read_normalized(self):
        assert normalize_tool_name("Read", sdk="claude-code") == "read"

    def test_write_normalized(self):
        assert normalize_tool_name("Write", sdk="claude-code") == "write"

    def test_edit_normalized(self):
        assert normalize_tool_name("Edit", sdk="claude-code") == "edit"

    def test_bash_normalized(self):
        assert normalize_tool_name("Bash", sdk="claude-code") == "bash"

    def test_glob_normalized(self):
        assert normalize_tool_name("Glob", sdk="claude-code") == "glob"

    def test_grep_normalized(self):
        assert normalize_tool_name("Grep", sdk="claude-code") == "grep"

    def test_webfetch_normalized(self):
        assert normalize_tool_name("WebFetch", sdk="claude-code") == "webfetch"

    def test_websearch_normalized(self):
        assert normalize_tool_name("WebSearch", sdk="claude-code") == "websearch"

    def test_todowrite_normalized(self):
        assert normalize_tool_name("TodoWrite", sdk="claude-code") == "todowrite"

    def test_task_normalized(self):
        assert normalize_tool_name("Task", sdk="claude-code") == "task"

    def test_skill_normalized(self):
        assert normalize_tool_name("Skill", sdk="claude-code") == "skill"

    def test_ask_user_question_normalized(self):
        assert normalize_tool_name("AskUserQuestion", sdk="claude-code") == "askuserquestion"

    def test_enter_plan_mode_normalized(self):
        assert normalize_tool_name("EnterPlanMode", sdk="claude-code") == "enterplanmode"

    def test_exit_plan_mode_normalized(self):
        assert normalize_tool_name("ExitPlanMode", sdk="claude-code") == "exitplanmode"

    def test_notebook_edit_normalized(self):
        assert normalize_tool_name("NotebookEdit", sdk="claude-code") == "notebookedit"

    def test_kill_shell_normalized(self):
        assert normalize_tool_name("KillShell", sdk="claude-code") == "killshell"

    def test_task_output_normalized(self):
        assert normalize_tool_name("TaskOutput", sdk="claude-code") == "taskoutput"

    def test_all_pascal_case_tools_covered(self):
        """Every entry in CLAUDE_CODE_TOOL_NAME_MAP normalizes correctly."""
        for pascal, lower in CLAUDE_CODE_TOOL_NAME_MAP.items():
            assert normalize_tool_name(pascal, sdk="claude-code") == lower, (
                f"normalize_tool_name('{pascal}') should return '{lower}'"
            )

    def test_mcp_tool_passes_through_unchanged(self):
        """MCP tools (already lowercase) pass through unmodified for Claude Code."""
        mcp_name = "mcp__task__create_agent_task"
        assert normalize_tool_name(mcp_name, sdk="claude-code") == mcp_name

    def test_mcp_knowledge_tool_passes_through(self):
        mcp_name = "mcp__knowledge__query_integration_knowledge"
        assert normalize_tool_name(mcp_name, sdk="claude-code") == mcp_name

    def test_unknown_tool_passes_through_unchanged(self):
        """Unknown tool names (e.g. plugin tools) pass through as-is for Claude Code."""
        custom = "mcp__plugin_context7__resolve-library-id"
        assert normalize_tool_name(custom, sdk="claude-code") == custom

    def test_already_lowercase_known_tool_passes_through(self):
        """If a known tool is already lowercase (shouldn't happen but safe)."""
        # 'read' is NOT in CLAUDE_CODE_TOOL_NAME_MAP (only 'Read' is).
        # So it passes through as-is, which is already correct.
        assert normalize_tool_name("read", sdk="claude-code") == "read"

    def test_default_sdk_is_claude_code(self):
        """SDK defaults to 'claude-code' when not specified."""
        assert normalize_tool_name("Bash") == "bash"
        assert normalize_tool_name("Read") == "read"


# ---------------------------------------------------------------------------
# normalize_tool_name — OpenCode SDK
# ---------------------------------------------------------------------------


class TestNormalizeToolNameOpenCode:
    """normalize_tool_name with sdk='opencode' remaps collaboration prefix."""

    def test_create_collaboration_remapped(self):
        assert normalize_tool_name(
            "mcp__collaboration__create_collaboration", sdk="opencode"
        ) == "mcp__task__create_collaboration"

    def test_post_finding_remapped(self):
        assert normalize_tool_name(
            "mcp__collaboration__post_finding", sdk="opencode"
        ) == "mcp__task__post_finding"

    def test_get_collaboration_status_remapped(self):
        assert normalize_tool_name(
            "mcp__collaboration__get_collaboration_status", sdk="opencode"
        ) == "mcp__task__get_collaboration_status"

    def test_all_opencode_mcp_remappings_covered(self):
        """Every entry in OPENCODE_MCP_TOOL_NAME_MAP normalizes correctly."""
        for original, unified in OPENCODE_MCP_TOOL_NAME_MAP.items():
            assert normalize_tool_name(original, sdk="opencode") == unified, (
                f"normalize_tool_name('{original}', sdk='opencode') "
                f"should return '{unified}'"
            )

    def test_builtin_bash_passes_through(self):
        """OpenCode built-ins are already lowercase and pass through unchanged."""
        assert normalize_tool_name("bash", sdk="opencode") == "bash"

    def test_builtin_read_passes_through(self):
        assert normalize_tool_name("read", sdk="opencode") == "read"

    def test_builtin_list_passes_through(self):
        assert normalize_tool_name("list", sdk="opencode") == "list"

    def test_builtin_patch_passes_through(self):
        assert normalize_tool_name("patch", sdk="opencode") == "patch"

    def test_mcp_task_tools_pass_through(self):
        """mcp__task__* tools that are already unified pass through unchanged."""
        mcp_task = "mcp__task__create_agent_task"
        assert normalize_tool_name(mcp_task, sdk="opencode") == mcp_task

    def test_knowledge_tool_passes_through(self):
        mcp_knowledge = "mcp__knowledge__query_integration_knowledge"
        assert normalize_tool_name(mcp_knowledge, sdk="opencode") == mcp_knowledge

    def test_plugin_tool_passes_through(self):
        """Plugin MCP tools with custom prefixes pass through unchanged."""
        plugin_tool = "mcp__plugin_context7__resolve-library-id"
        assert normalize_tool_name(plugin_tool, sdk="opencode") == plugin_tool


# ---------------------------------------------------------------------------
# normalize_tool_name — Unknown SDK fallback
# ---------------------------------------------------------------------------


class TestNormalizeToolNameUnknownSdk:
    """Unknown SDK falls back to lowercase conversion."""

    def test_unknown_sdk_converts_to_lowercase(self):
        assert normalize_tool_name("MyCustomTool", sdk="unknown-sdk") == "mycustomtool"

    def test_unknown_sdk_already_lowercase_unchanged(self):
        assert normalize_tool_name("bash", sdk="google-adk") == "bash"

    def test_empty_string_returns_empty(self):
        """Empty string is returned as-is for any SDK."""
        assert normalize_tool_name("", sdk="claude-code") == ""
        assert normalize_tool_name("", sdk="opencode") == ""
        assert normalize_tool_name("", sdk="unknown") == ""


# ---------------------------------------------------------------------------
# PRE_APPROVED_TOOLS set
# ---------------------------------------------------------------------------


class TestPreApprovedTools:
    """PRE_APPROVED_TOOLS contains all expected tools in lowercase convention."""

    # Core built-in SDK tools
    @pytest.mark.parametrize("tool", [
        "read", "write", "edit", "bash", "glob", "grep",
        "webfetch", "websearch", "todowrite",
        "task", "skill", "askuserquestion",
        "enterplanmode", "exitplanmode", "notebookedit",
        "killshell", "taskoutput",
    ])
    def test_builtin_sdk_tool_is_pre_approved(self, tool):
        assert tool in PRE_APPROVED_TOOLS, f"'{tool}' should be in PRE_APPROVED_TOOLS"

    # OpenCode-only built-ins
    @pytest.mark.parametrize("tool", ["list", "patch"])
    def test_opencode_only_builtin_is_pre_approved(self, tool):
        assert tool in PRE_APPROVED_TOOLS, f"'{tool}' should be in PRE_APPROVED_TOOLS"

    # MCP bridge tools
    @pytest.mark.parametrize("tool", [
        "mcp__knowledge__query_integration_knowledge",
        "mcp__task__create_agent_task",
        "mcp__task__update_session_state",
        "mcp__task__respond_to_task",
        "mcp__task__create_collaboration",
        "mcp__task__post_finding",
        "mcp__task__get_collaboration_status",
    ])
    def test_mcp_bridge_tool_is_pre_approved(self, tool):
        assert tool in PRE_APPROVED_TOOLS, f"'{tool}' should be in PRE_APPROVED_TOOLS"

    def test_all_tools_are_lowercase(self):
        """Convention: every tool in PRE_APPROVED_TOOLS must be lowercase."""
        for tool in PRE_APPROVED_TOOLS:
            assert tool == tool.lower(), (
                f"PRE_APPROVED_TOOLS entry '{tool}' is not lowercase"
            )

    def test_pascal_case_tools_not_present(self):
        """PascalCase variants are not in PRE_APPROVED_TOOLS."""
        pascal_tools = ["Read", "Write", "Bash", "Edit", "AskUserQuestion"]
        for tool in pascal_tools:
            assert tool not in PRE_APPROVED_TOOLS, (
                f"PascalCase '{tool}' should not be in PRE_APPROVED_TOOLS"
            )

    def test_old_collaboration_prefix_not_present(self):
        """Old mcp__collaboration__* names are not in PRE_APPROVED_TOOLS."""
        old_tools = [
            "mcp__collaboration__create_collaboration",
            "mcp__collaboration__post_finding",
            "mcp__collaboration__get_collaboration_status",
        ]
        for tool in old_tools:
            assert tool not in PRE_APPROVED_TOOLS, (
                f"Old collaboration prefix '{tool}' should not be in PRE_APPROVED_TOOLS; "
                f"use mcp__task__* instead"
            )

    def test_pre_approved_tools_is_frozenset(self):
        """PRE_APPROVED_TOOLS must be immutable (frozenset)."""
        assert isinstance(PRE_APPROVED_TOOLS, frozenset)

    def test_normalized_claude_code_tools_are_pre_approved(self):
        """All tools in CLAUDE_CODE_TOOL_NAME_MAP normalize to pre-approved names."""
        for pascal_name, lower_name in CLAUDE_CODE_TOOL_NAME_MAP.items():
            normalized = normalize_tool_name(pascal_name, sdk="claude-code")
            # Not every Claude Code tool is pre-approved (e.g. some are conditionally
            # added), but the normalization must at least produce lowercase names.
            assert normalized == lower_name, (
                f"normalize_tool_name('{pascal_name}') should produce '{lower_name}'"
            )

    def test_opencode_unified_collaboration_tools_are_pre_approved(self):
        """Unified mcp__task__ collaboration tool names are in PRE_APPROVED_TOOLS."""
        for _, unified in OPENCODE_MCP_TOOL_NAME_MAP.items():
            assert unified in PRE_APPROVED_TOOLS, (
                f"Unified tool name '{unified}' should be in PRE_APPROVED_TOOLS"
            )


# ---------------------------------------------------------------------------
# CLAUDE_CODE_TOOL_NAME_MAP structure
# ---------------------------------------------------------------------------


class TestClaudeCodeToolNameMap:
    """Validate map structure and values."""

    def test_all_keys_are_pascal_case(self):
        """Keys must start with uppercase (PascalCase as emitted by Claude SDK)."""
        for key in CLAUDE_CODE_TOOL_NAME_MAP:
            assert key[0].isupper(), f"Key '{key}' should start with uppercase"

    def test_all_values_are_lowercase(self):
        """Values must be fully lowercase."""
        for key, value in CLAUDE_CODE_TOOL_NAME_MAP.items():
            assert value == value.lower(), (
                f"Value '{value}' for key '{key}' should be lowercase"
            )

    def test_values_equal_key_lowercased(self):
        """Values should be the simple lowercase of the key (no transformations)."""
        for key, value in CLAUDE_CODE_TOOL_NAME_MAP.items():
            assert value == key.lower(), (
                f"Expected '{key.lower()}' but got '{value}' for key '{key}'"
            )


# ---------------------------------------------------------------------------
# OPENCODE_MCP_TOOL_NAME_MAP structure
# ---------------------------------------------------------------------------


class TestOpenCodeMcpToolNameMap:
    """Validate collaboration prefix remapping."""

    def test_all_keys_have_collaboration_prefix(self):
        """All source keys use mcp__collaboration__* prefix."""
        for key in OPENCODE_MCP_TOOL_NAME_MAP:
            assert key.startswith("mcp__collaboration__"), (
                f"Key '{key}' should start with 'mcp__collaboration__'"
            )

    def test_all_values_have_task_prefix(self):
        """All target values use mcp__task__* prefix."""
        for value in OPENCODE_MCP_TOOL_NAME_MAP.values():
            assert value.startswith("mcp__task__"), (
                f"Value '{value}' should start with 'mcp__task__'"
            )

    def test_tool_suffix_preserved(self):
        """The tool name suffix (after server prefix) is preserved in remapping."""
        for key, value in OPENCODE_MCP_TOOL_NAME_MAP.items():
            key_suffix = key.replace("mcp__collaboration__", "")
            value_suffix = value.replace("mcp__task__", "")
            assert key_suffix == value_suffix, (
                f"Tool suffix mismatch: '{key}' → '{value}' "
                f"('{key_suffix}' != '{value_suffix}')"
            )

    def test_no_collaboration_server_prefix_in_values(self):
        """Remapped values must use mcp__task__ prefix, not mcp__collaboration__."""
        for value in OPENCODE_MCP_TOOL_NAME_MAP.values():
            assert not value.startswith("mcp__collaboration__"), (
                f"Value '{value}' should not use 'mcp__collaboration__' prefix after remapping"
            )


# ---------------------------------------------------------------------------
# _camel_to_snake
# ---------------------------------------------------------------------------


class TestCamelToSnake:
    """Validate camelCase → snake_case conversion including acronyms."""

    @pytest.mark.parametrize("input_key, expected", [
        ("filePath", "file_path"),
        ("oldString", "old_string"),
        ("newString", "new_string"),
        ("command", "command"),
        ("pattern", "pattern"),
        ("query", "query"),
        ("file_path", "file_path"),
        ("articleIds", "article_ids"),
        ("taskMessage", "task_message"),
        # Acronym handling
        ("callID", "call_id"),
        ("sessionID", "session_id"),
        ("URLPath", "url_path"),
        ("htmlParser", "html_parser"),
        # Already snake_case
        ("old_string", "old_string"),
        # Single word
        ("name", "name"),
        # Empty string
        ("", ""),
    ])
    def test_conversion(self, input_key, expected):
        assert _camel_to_snake(input_key) == expected


# ---------------------------------------------------------------------------
# normalize_tool_input
# ---------------------------------------------------------------------------


class TestNormalizeToolInput:
    """Validate tool input key normalization from camelCase to snake_case."""

    def test_opencode_camel_keys_normalized(self):
        result = normalize_tool_input(
            {"filePath": "/app/foo.py", "oldString": "a", "newString": "b"},
            sdk="opencode",
        )
        assert result == {"file_path": "/app/foo.py", "old_string": "a", "new_string": "b"}

    def test_opencode_snake_keys_unchanged(self):
        """Keys already in snake_case pass through correctly."""
        inp = {"file_path": "/app/foo.py", "command": "ls"}
        result = normalize_tool_input(inp, sdk="opencode")
        assert result == inp

    def test_claude_code_not_transformed(self):
        """Claude Code inputs are returned as-is (already snake_case)."""
        inp = {"file_path": "/app/foo.py"}
        result = normalize_tool_input(inp, sdk="claude-code")
        assert result is inp  # Same object, not copied

    def test_non_dict_returns_as_is(self):
        assert normalize_tool_input("not a dict", sdk="opencode") == "not a dict"

    def test_empty_dict(self):
        assert normalize_tool_input({}, sdk="opencode") == {}

    def test_values_preserved(self):
        """Only keys are transformed, values are untouched."""
        inp = {"filePath": "/app/workspace/data.csv", "content": "hello\nworld"}
        result = normalize_tool_input(inp, sdk="opencode")
        assert result["file_path"] == "/app/workspace/data.csv"
        assert result["content"] == "hello\nworld"


# ---------------------------------------------------------------------------
# Cross-check: PRE_APPROVED_TOOLS in registry matches PRE_ALLOWED_TOOLS in
# message_service.py. These two sets must stay in sync but live in separate
# packages (agent-env vs backend) so they cannot share an import.
# ---------------------------------------------------------------------------


class TestPreApprovedToolsSync:
    """Ensure the two copies of pre-approved tools stay in sync."""

    def test_registry_matches_message_service(self):
        from app.services.message_service import PRE_ALLOWED_TOOLS

        missing_in_backend = PRE_APPROVED_TOOLS - PRE_ALLOWED_TOOLS
        missing_in_registry = PRE_ALLOWED_TOOLS - PRE_APPROVED_TOOLS

        assert not missing_in_backend, (
            f"Tools in registry PRE_APPROVED_TOOLS but missing from "
            f"message_service PRE_ALLOWED_TOOLS: {missing_in_backend}"
        )
        assert not missing_in_registry, (
            f"Tools in message_service PRE_ALLOWED_TOOLS but missing from "
            f"registry PRE_APPROVED_TOOLS: {missing_in_registry}"
        )
