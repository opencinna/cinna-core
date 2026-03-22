"""
Claude Code Event Transformer

Translates raw Claude Agent SDK message objects into the unified SDKEvent format
used by all adapters. This module mirrors the OpenCodeEventTransformer pattern —
a dedicated stateful translator that normalizes SDK-specific events into the
common format consumed by the backend streaming pipeline.

Claude SDK message types (from claude_agent_sdk):

    SystemMessage          — SDK lifecycle events (init, etc.)
    AssistantMessage       — LLM responses containing content blocks:
        TextBlock          — assistant text content
        ThinkingBlock      — chain-of-thought (extended thinking)
        ToolUseBlock       — tool invocation (name, id, input)
        ToolResultBlock    — tool execution result
    ResultMessage          — session completion signal (subtype, duration, cost)
    UserMessage            — user messages (including interrupt notifications)
"""

import json
import logging
from typing import Optional

from .base import SDKEvent, SDKEventType
from .tool_name_registry import normalize_tool_name

logger = logging.getLogger(__name__)


class ClaudeCodeEventTransformer:
    """
    Stateful translator from raw Claude Agent SDK messages to SDKEvent objects.

    Handles all Claude SDK message types and normalizes tool names to the
    unified lowercase convention via tool_name_registry.

    Usage:
        transformer = ClaudeCodeEventTransformer()
        event = transformer.translate(message_obj, session_id)
        if event is not None:
            yield event
    """

    def translate(
        self,
        message_obj,
        session_id: str,
        interrupt_initiated: bool = False,
    ) -> Optional[SDKEvent]:
        """
        Translate a single Claude SDK message into an SDKEvent.

        Args:
            message_obj: Claude SDK message object (AssistantMessage, ResultMessage, etc.)
            session_id: Current session ID
            interrupt_initiated: Whether an interrupt was requested for this session

        Returns:
            SDKEvent or None to skip the message
        """
        from claude_agent_sdk import (
            AssistantMessage,
            TextBlock,
            ToolUseBlock,
            ToolResultBlock,
            ResultMessage,
            ThinkingBlock,
            SystemMessage,
            UserMessage,
        )

        # -- SystemMessage --------------------------------------------------
        if isinstance(message_obj, SystemMessage):
            return self._handle_system_message(message_obj, session_id)

        # -- AssistantMessage -----------------------------------------------
        if isinstance(message_obj, AssistantMessage):
            return self._handle_assistant_message(message_obj, session_id)

        # -- ResultMessage --------------------------------------------------
        if isinstance(message_obj, ResultMessage):
            return self._handle_result_message(message_obj, session_id, interrupt_initiated)

        # -- UserMessage (interrupt notifications) --------------------------
        if isinstance(message_obj, UserMessage):
            return self._handle_user_message(message_obj, session_id)

        logger.warning(f"Unknown message type: {type(message_obj)}")
        return None

    # ------------------------------------------------------------------
    # Message type handlers
    # ------------------------------------------------------------------

    @staticmethod
    def _handle_system_message(message_obj, session_id: str) -> Optional[SDKEvent]:
        """Handle SystemMessage — skip init, forward others."""
        if message_obj.subtype == "init":
            return None

        return SDKEvent(
            type=SDKEventType.SYSTEM,
            content=f"System: {message_obj.subtype}",
            session_id=session_id,
            metadata={"subtype": message_obj.subtype},
        )

    @staticmethod
    def _handle_assistant_message(message_obj, session_id: str) -> Optional[SDKEvent]:
        """
        Handle AssistantMessage — extract text, thinking, and tool use blocks.

        For ToolUseBlock, returns a TOOL_USE event immediately (first tool wins).
        For text/thinking, accumulates and returns as ASSISTANT or THINKING.
        """
        from claude_agent_sdk import (
            TextBlock,
            ToolUseBlock,
            ToolResultBlock,
            ThinkingBlock,
        )

        content_parts = []
        event_type = SDKEventType.ASSISTANT

        for block in message_obj.content:
            if isinstance(block, TextBlock):
                content_parts.append(block.text)

            elif isinstance(block, ThinkingBlock):
                event_type = SDKEventType.THINKING
                content_parts.append(f"[Thinking] {block.thinking}")

            elif isinstance(block, ToolUseBlock):
                # Normalize tool name to unified lowercase convention
                unified_name = normalize_tool_name(block.name, sdk="claude-code")
                tool_input_str = _format_tool_input(block.input)

                return SDKEvent(
                    type=SDKEventType.TOOL_USE,
                    tool_name=unified_name,
                    content=f"Using tool: {unified_name}{tool_input_str}",
                    session_id=session_id,
                    metadata={
                        "tool_id": block.id,
                        "tool_input": block.input,
                    },
                )

            elif isinstance(block, ToolResultBlock):
                continue

        content = "\n".join(content_parts) if content_parts else ""

        metadata = {}
        if hasattr(message_obj, "model"):
            metadata["model"] = message_obj.model

        return SDKEvent(
            type=event_type,
            content=content,
            session_id=session_id,
            metadata=metadata,
        )

    @staticmethod
    def _handle_result_message(
        message_obj, session_id: str, interrupt_initiated: bool,
    ) -> SDKEvent:
        """Handle ResultMessage — detect interrupts, emit DONE or INTERRUPTED."""
        is_interrupted = (
            interrupt_initiated
            and message_obj.subtype == "error_during_execution"
        )

        if is_interrupted:
            logger.info("Detected interrupted session from ResultMessage")

        return SDKEvent(
            type=SDKEventType.INTERRUPTED if is_interrupted else SDKEventType.DONE,
            content="Request interrupted by user" if is_interrupted else "",
            session_id=session_id,
            metadata={
                "subtype": message_obj.subtype,
                "duration_ms": message_obj.duration_ms,
                "is_error": message_obj.is_error,
                "num_turns": message_obj.num_turns,
                "total_cost_usd": message_obj.total_cost_usd,
                "session_id": message_obj.session_id,
            },
        )

    @staticmethod
    def _handle_user_message(message_obj, session_id: str) -> Optional[SDKEvent]:
        """Handle UserMessage — forward interrupt notifications, skip others."""
        content_str = str(message_obj.content)
        if "[Request interrupted by user" in content_str:
            return SDKEvent(
                type=SDKEventType.SYSTEM,
                content="⚠️ Request interrupted by user",
                session_id=session_id,
                metadata={"interrupt_notification": True},
            )
        return None


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _format_tool_input(tool_input) -> str:
    """Format tool input for display, truncating to 200 chars."""
    if not tool_input:
        return ""
    try:
        input_json = json.dumps(tool_input, indent=2)
        if len(input_json) > 200:
            input_json = input_json[:200] + "..."
        return f"\nInput: {input_json}"
    except Exception:
        return f"\nInput: {str(tool_input)[:200]}"
