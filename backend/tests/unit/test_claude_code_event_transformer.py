"""
Unit tests for ClaudeCodeEventTransformer.

Tests event translation from Claude Agent SDK message objects to SDKEvent objects
using mock SDK types (the real claude_agent_sdk is only available inside Docker
containers, so we mock the message/block classes here).

Real event data is derived from captured JSONL session logs.

Run: cd backend && python -m pytest tests/unit/test_claude_code_event_transformer.py -v
"""

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Mock claude_agent_sdk types — mirrors the real SDK interfaces so the
# transformer can be tested without the actual SDK installed.
# ---------------------------------------------------------------------------

@dataclass
class TextBlock:
    text: str
    type: str = "text"


@dataclass
class ThinkingBlock:
    thinking: str
    type: str = "thinking"


@dataclass
class ToolUseBlock:
    id: str
    name: str
    input: dict
    type: str = "tool_use"


@dataclass
class ToolResultBlock:
    tool_use_id: str
    content: str
    type: str = "tool_result"


@dataclass
class SystemMessage:
    subtype: str
    data: Any = None


@dataclass
class AssistantMessage:
    content: list
    model: str = "claude-haiku-4-5-20251001"


@dataclass
class ResultMessage:
    subtype: str
    session_id: str
    duration_ms: int = 0
    duration_api_ms: int = 0
    is_error: bool = False
    num_turns: int = 1
    total_cost_usd: float = 0.0
    usage: dict = field(default_factory=dict)
    result: str = ""


@dataclass
class UserMessage:
    content: Any = ""


# Install mock module before importing transformer
_mock_sdk = MagicMock()
_mock_sdk.AssistantMessage = AssistantMessage
_mock_sdk.TextBlock = TextBlock
_mock_sdk.ToolUseBlock = ToolUseBlock
_mock_sdk.ToolResultBlock = ToolResultBlock
_mock_sdk.ResultMessage = ResultMessage
_mock_sdk.ThinkingBlock = ThinkingBlock
_mock_sdk.SystemMessage = SystemMessage
_mock_sdk.UserMessage = UserMessage
sys.modules["claude_agent_sdk"] = _mock_sdk

# sys.path setup is handled by tests/unit/conftest.py
from core.server.adapters.claude_code_event_transformer import ClaudeCodeEventTransformer
from core.server.adapters.base import SDKEvent, SDKEventType


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SESSION_ID = "1070f9d3-bee1-4918-9b99-b795059447ab"


@pytest.fixture
def transformer():
    """Create a fresh transformer instance."""
    return ClaudeCodeEventTransformer()


# ---------------------------------------------------------------------------
# Raw message fixtures (derived from real captured session logs)
# ---------------------------------------------------------------------------

# -- SystemMessage: init (should be skipped) --------------------------------

MSG_SYSTEM_INIT = SystemMessage(
    subtype="init",
    data={
        "type": "system",
        "subtype": "init",
        "cwd": "/app/workspace",
        "session_id": SESSION_ID,
        "tools": ["Bash", "Read", "Write", "Edit", "Glob", "Grep"],
        "model": "claude-haiku-4-5-20251001",
        "permissionMode": "acceptEdits",
        "claude_code_version": "2.0.72",
    },
)

# -- SystemMessage: non-init (should be forwarded) --------------------------

MSG_SYSTEM_OTHER = SystemMessage(
    subtype="result",
    data={"info": "some result"},
)

# -- AssistantMessage: text only --------------------------------------------

MSG_ASSISTANT_TEXT = AssistantMessage(
    content=[TextBlock(text="2 + 2 = **4**\n\nThe sum of 2 and 2 equals 4.")],
    model="claude-haiku-4-5-20251001",
)

# -- AssistantMessage: thinking block ---------------------------------------

MSG_ASSISTANT_THINKING = AssistantMessage(
    content=[ThinkingBlock(thinking="Let me think about this carefully...")],
    model="claude-haiku-4-5-20251001",
)

# -- AssistantMessage: tool use (Bash) --------------------------------------

MSG_ASSISTANT_TOOL_USE_BASH = AssistantMessage(
    content=[
        ToolUseBlock(
            id="toolu_017piqFhG5ss8t7h241QgDoN",
            name="Bash",
            input={
                "command": "ls -la ./scripts/",
                "description": "List files in scripts directory",
            },
        )
    ],
    model="claude-haiku-4-5-20251001",
)

# -- AssistantMessage: tool use (Read) --------------------------------------

MSG_ASSISTANT_TOOL_USE_READ = AssistantMessage(
    content=[
        ToolUseBlock(
            id="toolu_read_001",
            name="Read",
            input={"file_path": "/app/workspace/scripts/README.md"},
        )
    ],
    model="claude-haiku-4-5-20251001",
)

# -- AssistantMessage: MCP tool use -----------------------------------------

MSG_ASSISTANT_TOOL_USE_MCP = AssistantMessage(
    content=[
        ToolUseBlock(
            id="toolu_mcp_001",
            name="mcp__task__create_agent_task",
            input={"task": "Process invoices", "agent_id": "abc-123"},
        )
    ],
    model="claude-haiku-4-5-20251001",
)

# -- AssistantMessage: tool result (should be skipped) ----------------------

MSG_ASSISTANT_TOOL_RESULT = AssistantMessage(
    content=[
        ToolResultBlock(
            tool_use_id="toolu_017piqFhG5ss8t7h241QgDoN",
            content="total 4\ndrwxr-xr-x  4 root root 128 Dec 25 20:53 .",
        )
    ],
    model="claude-haiku-4-5-20251001",
)

# -- AssistantMessage: mixed text + tool use --------------------------------

MSG_ASSISTANT_TEXT_THEN_TOOL = AssistantMessage(
    content=[
        TextBlock(text="Let me check what files are in the scripts folder."),
        ToolUseBlock(
            id="toolu_mixed_001",
            name="Bash",
            input={"command": "ls scripts/"},
        ),
    ],
    model="claude-haiku-4-5-20251001",
)

# -- AssistantMessage: empty content ----------------------------------------

MSG_ASSISTANT_EMPTY = AssistantMessage(
    content=[],
    model="claude-haiku-4-5-20251001",
)

# -- ResultMessage: success -------------------------------------------------

MSG_RESULT_SUCCESS = ResultMessage(
    subtype="success",
    session_id=SESSION_ID,
    duration_ms=2274,
    duration_api_ms=4483,
    is_error=False,
    num_turns=1,
    total_cost_usd=0.0090991,
    usage={
        "input_tokens": 2,
        "cache_creation_input_tokens": 4990,
        "cache_read_input_tokens": 12836,
        "output_tokens": 37,
    },
    result="2 + 2 = **4**\n\nThe sum of 2 and 2 equals 4.",
)

# -- ResultMessage: multi-turn success -------------------------------------

MSG_RESULT_MULTI_TURN = ResultMessage(
    subtype="success",
    session_id=SESSION_ID,
    duration_ms=3346,
    duration_api_ms=5504,
    is_error=False,
    num_turns=2,
    total_cost_usd=0.00657395,
    result="The scripts folder is currently empty.",
)

# -- ResultMessage: error during execution (interrupt) ----------------------

MSG_RESULT_INTERRUPTED = ResultMessage(
    subtype="error_during_execution",
    session_id=SESSION_ID,
    duration_ms=1000,
    is_error=True,
    num_turns=1,
    total_cost_usd=0.005,
)

# -- UserMessage: interrupt notification ------------------------------------

MSG_USER_INTERRUPT = UserMessage(
    content="[Request interrupted by user at 2026-03-22T18:50:00Z]",
)

# -- UserMessage: normal (should be skipped) --------------------------------

MSG_USER_NORMAL = UserMessage(
    content="What files are in the scripts folder?",
)

# -- AssistantMessage: large tool input (should be truncated) ---------------

MSG_ASSISTANT_TOOL_LARGE_INPUT = AssistantMessage(
    content=[
        ToolUseBlock(
            id="toolu_large_001",
            name="Write",
            input={
                "file_path": "/app/workspace/scripts/long_script.py",
                "content": "x = 1\n" * 100,  # ~600 chars
            },
        )
    ],
    model="claude-haiku-4-5-20251001",
)


# ===========================================================================
# Tests: SystemMessage handling
# ===========================================================================

class TestSystemMessage:
    def test_init_message_skipped(self, transformer):
        """SystemMessage with subtype 'init' should be skipped."""
        result = transformer.translate(MSG_SYSTEM_INIT, SESSION_ID)
        assert result is None

    def test_non_init_message_forwarded(self, transformer):
        """Non-init SystemMessages should produce a SYSTEM event."""
        result = transformer.translate(MSG_SYSTEM_OTHER, SESSION_ID)
        assert result is not None
        assert result.type == SDKEventType.SYSTEM
        assert result.session_id == SESSION_ID
        assert "result" in result.content
        assert result.metadata["subtype"] == "result"


# ===========================================================================
# Tests: AssistantMessage handling
# ===========================================================================

class TestAssistantMessage:
    def test_text_block_produces_assistant_event(self, transformer):
        """TextBlock should produce an ASSISTANT event with the text content."""
        result = transformer.translate(MSG_ASSISTANT_TEXT, SESSION_ID)
        assert result is not None
        assert result.type == SDKEventType.ASSISTANT
        assert result.session_id == SESSION_ID
        assert "2 + 2 = **4**" in result.content
        assert result.metadata["model"] == "claude-haiku-4-5-20251001"

    def test_thinking_block_produces_thinking_event(self, transformer):
        """ThinkingBlock should produce a THINKING event."""
        result = transformer.translate(MSG_ASSISTANT_THINKING, SESSION_ID)
        assert result is not None
        assert result.type == SDKEventType.THINKING
        assert "think about this carefully" in result.content

    def test_tool_use_block_produces_tool_event(self, transformer):
        """ToolUseBlock should produce a TOOL_USE event with normalized name."""
        result = transformer.translate(MSG_ASSISTANT_TOOL_USE_BASH, SESSION_ID)
        assert result is not None
        assert result.type == SDKEventType.TOOL_USE
        # Tool name should be normalized to lowercase
        assert result.tool_name == "bash"
        assert "bash" in result.content
        assert result.metadata["tool_id"] == "toolu_017piqFhG5ss8t7h241QgDoN"
        assert result.metadata["tool_input"]["command"] == "ls -la ./scripts/"

    def test_read_tool_normalized(self, transformer):
        """Read tool name should be normalized to lowercase."""
        result = transformer.translate(MSG_ASSISTANT_TOOL_USE_READ, SESSION_ID)
        assert result.tool_name == "read"
        assert result.metadata["tool_input"]["file_path"] == "/app/workspace/scripts/README.md"

    def test_mcp_tool_name_preserved(self, transformer):
        """MCP tool names (already lowercase) should pass through."""
        result = transformer.translate(MSG_ASSISTANT_TOOL_USE_MCP, SESSION_ID)
        assert result.tool_name == "mcp__task__create_agent_task"

    def test_tool_result_block_skipped(self, transformer):
        """ToolResultBlock should be skipped (produces empty ASSISTANT)."""
        result = transformer.translate(MSG_ASSISTANT_TOOL_RESULT, SESSION_ID)
        assert result is not None
        assert result.type == SDKEventType.ASSISTANT
        assert result.content == ""

    def test_text_then_tool_returns_tool_event(self, transformer):
        """When text precedes tool use, the tool event should be returned
        (tool use returns immediately, cutting off remaining blocks)."""
        result = transformer.translate(MSG_ASSISTANT_TEXT_THEN_TOOL, SESSION_ID)
        assert result is not None
        # ToolUseBlock causes immediate return
        assert result.type == SDKEventType.TOOL_USE
        assert result.tool_name == "bash"

    def test_empty_content_produces_empty_assistant(self, transformer):
        """Empty content list should produce an ASSISTANT event with empty content."""
        result = transformer.translate(MSG_ASSISTANT_EMPTY, SESSION_ID)
        assert result is not None
        assert result.type == SDKEventType.ASSISTANT
        assert result.content == ""

    def test_large_tool_input_truncated(self, transformer):
        """Tool input longer than 200 chars should be truncated in content."""
        result = transformer.translate(MSG_ASSISTANT_TOOL_LARGE_INPUT, SESSION_ID)
        assert result is not None
        assert result.type == SDKEventType.TOOL_USE
        # Content should be truncated
        assert "..." in result.content
        # But metadata should contain full input
        assert len(result.metadata["tool_input"]["content"]) > 200


# ===========================================================================
# Tests: ResultMessage handling
# ===========================================================================

class TestResultMessage:
    def test_success_result_emits_done(self, transformer):
        """Successful ResultMessage should produce a DONE event."""
        result = transformer.translate(MSG_RESULT_SUCCESS, SESSION_ID)
        assert result is not None
        assert result.type == SDKEventType.DONE
        assert result.session_id == SESSION_ID
        assert result.metadata["subtype"] == "success"
        assert result.metadata["duration_ms"] == 2274
        assert result.metadata["is_error"] is False
        assert result.metadata["num_turns"] == 1
        assert result.metadata["total_cost_usd"] == 0.0090991
        assert result.metadata["session_id"] == SESSION_ID

    def test_multi_turn_result(self, transformer):
        """Multi-turn ResultMessage should have correct turn count."""
        result = transformer.translate(MSG_RESULT_MULTI_TURN, SESSION_ID)
        assert result.type == SDKEventType.DONE
        assert result.metadata["num_turns"] == 2

    def test_interrupt_without_flag_emits_done(self, transformer):
        """error_during_execution without interrupt flag should still be DONE."""
        result = transformer.translate(MSG_RESULT_INTERRUPTED, SESSION_ID)
        assert result.type == SDKEventType.DONE
        assert result.metadata["is_error"] is True

    def test_interrupt_with_flag_emits_interrupted(self, transformer):
        """error_during_execution WITH interrupt flag should produce INTERRUPTED."""
        result = transformer.translate(
            MSG_RESULT_INTERRUPTED, SESSION_ID, interrupt_initiated=True
        )
        assert result.type == SDKEventType.INTERRUPTED
        assert "interrupted" in result.content.lower()


# ===========================================================================
# Tests: UserMessage handling
# ===========================================================================

class TestUserMessage:
    def test_interrupt_notification_forwarded(self, transformer):
        """Interrupt notification UserMessage should produce a SYSTEM event."""
        result = transformer.translate(MSG_USER_INTERRUPT, SESSION_ID)
        assert result is not None
        assert result.type == SDKEventType.SYSTEM
        assert "interrupted" in result.content.lower()
        assert result.metadata.get("interrupt_notification") is True

    def test_normal_user_message_skipped(self, transformer):
        """Normal UserMessages should be skipped."""
        result = transformer.translate(MSG_USER_NORMAL, SESSION_ID)
        assert result is None


# ===========================================================================
# Tests: unknown message types
# ===========================================================================

class TestUnknownMessages:
    def test_unknown_type_skipped(self, transformer):
        """Unknown message types should return None."""

        class UnknownMessage:
            pass

        result = transformer.translate(UnknownMessage(), SESSION_ID)
        assert result is None


# ===========================================================================
# Tests: full conversation replay (from real captured JSONL session)
# ===========================================================================

class TestConversationReplay:
    """
    Replays real captured conversations through the transformer and verifies
    the full event stream. Events are derived from
    claude_code_session_20260322_184632_282031.jsonl.
    """

    def test_simple_text_conversation(self, transformer):
        """Replay: user asks '2+2?' -> text response -> done."""
        messages = [
            MSG_SYSTEM_INIT,
            MSG_ASSISTANT_TEXT,
            MSG_RESULT_SUCCESS,
        ]

        events = []
        for msg in messages:
            event = transformer.translate(msg, SESSION_ID)
            if event is not None:
                events.append(event)

        # init should be skipped
        assert len(events) == 2

        # First event: assistant text
        assert events[0].type == SDKEventType.ASSISTANT
        assert "2 + 2 = **4**" in events[0].content

        # Second event: done
        assert events[1].type == SDKEventType.DONE
        assert events[1].metadata["num_turns"] == 1

    def test_tool_use_conversation(self, transformer):
        """Replay: user asks about files -> text -> tool use -> text -> done.

        Based on the second message in the captured session where the agent
        uses Bash to list files in the scripts folder.
        """
        messages = [
            # Init (skipped)
            MSG_SYSTEM_INIT,
            # "Let me check what files are in the scripts folder."
            AssistantMessage(
                content=[TextBlock(text="Let me check what files are in the scripts folder.")],
                model="claude-haiku-4-5-20251001",
            ),
            # Tool use: Bash ls
            AssistantMessage(
                content=[
                    ToolUseBlock(
                        id="toolu_017piqFhG5ss8t7h241QgDoN",
                        name="Bash",
                        input={
                            "command": "ls -la ./scripts/",
                            "description": "List files in scripts directory",
                        },
                    )
                ],
                model="claude-haiku-4-5-20251001",
            ),
            # Tool result echo (UserMessage with ToolResultBlock content — skipped)
            UserMessage(
                content="[ToolResultBlock(tool_use_id='toolu_017piqFhG5ss8t7h241QgDoN', content='total 4...')]",
            ),
            # Final text response
            AssistantMessage(
                content=[
                    TextBlock(
                        text="The **scripts folder is currently empty** except for:\n"
                             "- `.gitkeep` - a placeholder file\n"
                             "- `README.md` - a readme file"
                    )
                ],
                model="claude-haiku-4-5-20251001",
            ),
            # Done
            ResultMessage(
                subtype="success",
                session_id=SESSION_ID,
                duration_ms=3346,
                is_error=False,
                num_turns=2,
                total_cost_usd=0.00657395,
            ),
        ]

        events = []
        for msg in messages:
            event = transformer.translate(msg, SESSION_ID)
            if event is not None:
                events.append(event)

        # Verify event types
        types = [e.type for e in events]
        assert SDKEventType.ASSISTANT in types
        assert SDKEventType.TOOL_USE in types
        assert SDKEventType.DONE in types

        # Text events
        assistant_events = [e for e in events if e.type == SDKEventType.ASSISTANT]
        assert len(assistant_events) == 2
        assert "check what files" in assistant_events[0].content
        assert "scripts folder is currently empty" in assistant_events[1].content

        # Tool event
        tool_events = [e for e in events if e.type == SDKEventType.TOOL_USE]
        assert len(tool_events) == 1
        assert tool_events[0].tool_name == "bash"
        assert tool_events[0].metadata["tool_input"]["command"] == "ls -la ./scripts/"

        # Done
        done_events = [e for e in events if e.type == SDKEventType.DONE]
        assert len(done_events) == 1
        assert done_events[0].metadata["num_turns"] == 2

    def test_event_ordering(self, transformer):
        """Verify events come in the correct order: text -> tool -> text -> done."""
        messages = [
            MSG_SYSTEM_INIT,
            AssistantMessage(
                content=[TextBlock(text="Let me check.")],
                model="claude-haiku-4-5-20251001",
            ),
            AssistantMessage(
                content=[
                    ToolUseBlock(id="t1", name="Glob", input={"pattern": "*.py"})
                ],
                model="claude-haiku-4-5-20251001",
            ),
            AssistantMessage(
                content=[TextBlock(text="Found 3 files.")],
                model="claude-haiku-4-5-20251001",
            ),
            ResultMessage(
                subtype="success",
                session_id=SESSION_ID,
                num_turns=2,
            ),
        ]

        events = []
        for msg in messages:
            event = transformer.translate(msg, SESSION_ID)
            if event is not None:
                events.append(event)

        types = [e.type for e in events]
        assert types == [
            SDKEventType.ASSISTANT,   # "Let me check."
            SDKEventType.TOOL_USE,    # Glob
            SDKEventType.ASSISTANT,   # "Found 3 files."
            SDKEventType.DONE,
        ]

    def test_interrupted_session(self, transformer):
        """Replay an interrupted session: text -> interrupt -> error result."""
        messages = [
            MSG_SYSTEM_INIT,
            AssistantMessage(
                content=[TextBlock(text="Starting long operation...")],
                model="claude-haiku-4-5-20251001",
            ),
            UserMessage(
                content="[Request interrupted by user at 2026-03-22T18:50:00Z]",
            ),
            ResultMessage(
                subtype="error_during_execution",
                session_id=SESSION_ID,
                duration_ms=5000,
                is_error=True,
            ),
        ]

        events = []
        for msg in messages:
            event = transformer.translate(msg, SESSION_ID, interrupt_initiated=True)
            if event is not None:
                events.append(event)

        types = [e.type for e in events]
        assert SDKEventType.ASSISTANT in types
        assert SDKEventType.SYSTEM in types  # interrupt notification
        assert SDKEventType.INTERRUPTED in types

        # INTERRUPTED should be last
        assert events[-1].type == SDKEventType.INTERRUPTED

    def test_multi_tool_conversation(self, transformer):
        """Multiple sequential tool calls in one conversation."""
        messages = [
            MSG_SYSTEM_INIT,
            # First tool: Glob
            AssistantMessage(
                content=[
                    ToolUseBlock(id="t1", name="Glob", input={"pattern": "*.py"})
                ],
                model="claude-haiku-4-5-20251001",
            ),
            UserMessage(content="[ToolResult]"),
            # Second tool: Read
            AssistantMessage(
                content=[
                    ToolUseBlock(
                        id="t2",
                        name="Read",
                        input={"file_path": "/app/workspace/scripts/main.py"},
                    )
                ],
                model="claude-haiku-4-5-20251001",
            ),
            UserMessage(content="[ToolResult]"),
            # Final text
            AssistantMessage(
                content=[TextBlock(text="Here's what I found in the scripts.")],
                model="claude-haiku-4-5-20251001",
            ),
            ResultMessage(subtype="success", session_id=SESSION_ID, num_turns=3),
        ]

        events = []
        for msg in messages:
            event = transformer.translate(msg, SESSION_ID)
            if event is not None:
                events.append(event)

        tool_events = [e for e in events if e.type == SDKEventType.TOOL_USE]
        assert len(tool_events) == 2
        assert tool_events[0].tool_name == "glob"
        assert tool_events[1].tool_name == "read"

        assistant_events = [e for e in events if e.type == SDKEventType.ASSISTANT]
        assert len(assistant_events) == 1
        assert "what I found" in assistant_events[0].content

    def test_thinking_then_text_response(self, transformer):
        """Model with extended thinking: ThinkingBlock -> TextBlock."""
        messages = [
            MSG_SYSTEM_INIT,
            AssistantMessage(
                content=[ThinkingBlock(thinking="The user is asking about math...")],
                model="claude-haiku-4-5-20251001",
            ),
            AssistantMessage(
                content=[TextBlock(text="The answer is 42.")],
                model="claude-haiku-4-5-20251001",
            ),
            ResultMessage(subtype="success", session_id=SESSION_ID),
        ]

        events = []
        for msg in messages:
            event = transformer.translate(msg, SESSION_ID)
            if event is not None:
                events.append(event)

        thinking = [e for e in events if e.type == SDKEventType.THINKING]
        assistant = [e for e in events if e.type == SDKEventType.ASSISTANT]

        assert len(thinking) == 1
        assert "asking about math" in thinking[0].content
        assert len(assistant) == 1
        assert "42" in assistant[0].content


# ===========================================================================
# Tests: tool name normalization
# ===========================================================================

class TestToolNameNormalization:
    """Verify that PascalCase Claude Code tools are normalized to lowercase."""

    @pytest.mark.parametrize("sdk_name,expected", [
        ("Bash", "bash"),
        ("Read", "read"),
        ("Write", "write"),
        ("Edit", "edit"),
        ("Glob", "glob"),
        ("Grep", "grep"),
        ("WebFetch", "webfetch"),
        ("WebSearch", "websearch"),
        ("TodoWrite", "todowrite"),
        ("NotebookEdit", "notebookedit"),
    ])
    def test_builtin_tools_normalized(self, transformer, sdk_name, expected):
        """Built-in Claude Code tools should be lowercased."""
        msg = AssistantMessage(
            content=[ToolUseBlock(id="t1", name=sdk_name, input={"x": 1})],
        )
        result = transformer.translate(msg, SESSION_ID)
        assert result.tool_name == expected

    def test_mcp_tools_pass_through(self, transformer):
        """MCP tools (already lowercase) should pass through unchanged."""
        msg = AssistantMessage(
            content=[
                ToolUseBlock(
                    id="t1",
                    name="mcp__task__create_agent_task",
                    input={"task": "test"},
                )
            ],
        )
        result = transformer.translate(msg, SESSION_ID)
        assert result.tool_name == "mcp__task__create_agent_task"
