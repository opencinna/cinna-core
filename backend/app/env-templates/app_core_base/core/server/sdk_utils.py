"""
Utility functions for SDK management, logging, and debugging.

This module provides shared utilities used by SDK adapters:
- SessionEventLogger: JSONL-based bidirectional event logger used by all adapters
- format_message_for_debug: Formats SDK messages for debug output
- format_sdk_message: DEPRECATED - use adapter-specific formatting

All adapters use SessionEventLogger for unified JSONL logging controlled by
the DUMP_LLM_SESSION environment variable. Each log line records a timestamp,
direction (recv/send), and the raw event data — enabling offline replay and
cross-adapter debugging with a single format.
"""
import logging
import json
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional
import warnings

logger = logging.getLogger(__name__)


class SessionEventLogger:
    """
    Records bi-directional SDK communication to a JSONL file.

    Each line is a JSON object with:
    - ts: ISO timestamp
    - dir: "recv" (event from SDK) or "send" (request to SDK)
    - event: the raw data

    This unified format is used by all adapters (Claude Code, OpenCode, etc.)
    so session logs are consistent and can be replayed or compared across SDKs.

    Enabled when DUMP_LLM_SESSION=true.
    """

    def __init__(self, logs_dir: Path, prefix: str, enabled: bool = False):
        """
        Args:
            logs_dir: Directory to store log files
            prefix: Filename prefix (e.g. "claude_code_session", "opencode_session")
            enabled: Whether logging is active
        """
        self.enabled = enabled
        self._log_file: Optional[Path] = None
        if enabled:
            logs_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
            self._log_file = logs_dir / f"{prefix}_{ts}.jsonl"
            logger.info("Session logging enabled: %s", self._log_file)

    def _write(self, direction: str, event_data: dict) -> None:
        if not self._log_file:
            return
        try:
            record = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "dir": direction,
                "event": event_data,
            }
            with open(self._log_file, "a") as f:
                f.write(json.dumps(record, default=str) + "\n")
        except Exception as exc:
            logger.debug("Failed to write session log: %s", exc)

    def log_recv(self, event_data: dict) -> None:
        """Log an event received from the SDK."""
        self._write("recv", event_data)

    def log_send(self, action: str, data: dict) -> None:
        """Log a request sent to the SDK."""
        self._write("send", {"action": action, **data})


def format_message_for_debug(message_obj) -> str:
    """
    Format SDK message object for debug logging.

    Args:
        message_obj: SDK message object

    Returns:
        Formatted string representation with all fields
    """
    from claude_agent_sdk import AssistantMessage, ResultMessage, SystemMessage, UserMessage

    try:
        msg_type = type(message_obj).__name__

        if isinstance(message_obj, SystemMessage):
            return json.dumps({
                "type": "SystemMessage",
                "subtype": message_obj.subtype,
                "data": message_obj.data
            }, indent=2)

        elif isinstance(message_obj, AssistantMessage):
            # Format content blocks
            content_blocks = []
            for block in message_obj.content:
                block_type = type(block).__name__
                if hasattr(block, '__dict__'):
                    content_blocks.append({
                        "type": block_type,
                        "data": block.__dict__
                    })
                else:
                    content_blocks.append({
                        "type": block_type,
                        "data": str(block)
                    })

            return json.dumps({
                "type": "AssistantMessage",
                "model": getattr(message_obj, "model", None),
                "content_blocks": content_blocks,
                "num_blocks": len(message_obj.content)
            }, indent=2)

        elif isinstance(message_obj, ResultMessage):
            return json.dumps({
                "type": "ResultMessage",
                "subtype": message_obj.subtype,
                "session_id": message_obj.session_id,
                "duration_ms": message_obj.duration_ms,
                "duration_api_ms": message_obj.duration_api_ms,
                "is_error": message_obj.is_error,
                "num_turns": message_obj.num_turns,
                "total_cost_usd": message_obj.total_cost_usd,
                "usage": message_obj.usage,
                "result": message_obj.result
            }, indent=2)

        elif isinstance(message_obj, UserMessage):
            return json.dumps({
                "type": "UserMessage",
                "content": str(message_obj.content)[:200] + "..." if len(str(message_obj.content)) > 200 else str(message_obj.content)
            }, indent=2)

        else:
            # Fallback for unknown types
            if hasattr(message_obj, '__dict__'):
                return json.dumps({
                    "type": msg_type,
                    "data": message_obj.__dict__
                }, indent=2, default=str)
            else:
                return f"{msg_type}: {str(message_obj)[:500]}"

    except Exception as e:
        return f"Error formatting message: {e}, raw: {str(message_obj)[:500]}"


def format_sdk_message(message_obj, session_id: str, interrupt_initiated: bool = False) -> Optional[dict]:
    """
    Format SDK message object into standard dictionary for API responses.

    DEPRECATED: This function is deprecated. Each SDK adapter now handles
    message formatting internally via their _format_message method.
    See adapters/claude_code_event_transformer.py:ClaudeCodeEventTransformer.translate() for
    the current implementation.

    Args:
        message_obj: SDK message object (AssistantMessage, ResultMessage, SystemMessage, etc.)
        session_id: Current session ID
        interrupt_initiated: Whether interrupt() was called for this session

    Returns:
        Formatted message dict, or None to skip this message
    """
    warnings.warn(
        "format_sdk_message is deprecated. Use adapter-specific formatting instead. "
        "See adapters/claude_code_event_transformer.py:ClaudeCodeEventTransformer.translate()",
        DeprecationWarning,
        stacklevel=2
    )
    from claude_agent_sdk import (
        AssistantMessage,
        TextBlock,
        ToolUseBlock,
        ToolResultBlock,
        ResultMessage,
        ThinkingBlock,
        SystemMessage,
        UserMessage
    )

    formatted = {
        "session_id": session_id,
        "content": "",
        "metadata": {},
    }

    # Handle SystemMessage (metadata messages from SDK)
    if isinstance(message_obj, SystemMessage):
        # Extract useful metadata but don't show init messages to user
        if message_obj.subtype == "init":
            # Store metadata but return empty content (will be filtered out)
            data = message_obj.data
            formatted["type"] = "system"
            formatted["content"] = ""  # Don't show init message to user
            formatted["metadata"] = {
                "subtype": "init",
                "model": data.get("model"),
                "claude_code_version": data.get("claude_code_version"),
                "cwd": data.get("cwd"),
                "permission_mode": data.get("permissionMode"),
            }
            # Return None to skip this message (don't send to frontend)
            return None
        else:
            # Other system messages - log subtype for debugging
            formatted["type"] = "system"
            formatted["content"] = f"System: {message_obj.subtype}"
            formatted["metadata"]["subtype"] = message_obj.subtype
            return formatted

    # Handle AssistantMessage
    elif isinstance(message_obj, AssistantMessage):
        content_parts = []

        for block in message_obj.content:
            if isinstance(block, TextBlock):
                # Regular text content
                formatted["type"] = "assistant"
                content_parts.append(block.text)

            elif isinstance(block, ThinkingBlock):
                # Thinking block (extended thinking models)
                formatted["type"] = "thinking"
                content_parts.append(f"[Thinking] {block.thinking}")

            elif isinstance(block, ToolUseBlock):
                # Tool use request - send as separate event
                tool_input_str = ""
                if block.input:
                    # Format tool input for display
                    try:
                        # Try to format as readable JSON, but limit size
                        input_json = json.dumps(block.input, indent=2)
                        if len(input_json) > 200:
                            # Truncate long inputs
                            input_json = input_json[:200] + "..."
                        tool_input_str = f"\nInput: {input_json}"
                    except:
                        tool_input_str = f"\nInput: {str(block.input)[:200]}"

                formatted["type"] = "tool"
                formatted["tool_name"] = block.name
                formatted["content"] = f"🔧 Using tool: {block.name}{tool_input_str}"
                formatted["metadata"]["tool_id"] = block.id
                formatted["metadata"]["tool_input"] = block.input
                # Return immediately for tool use to preserve event granularity
                return formatted

            elif isinstance(block, ToolResultBlock):
                # Tool result - don't show to user, just metadata
                # Skip tool results as they're internal
                continue

        # Set default type if not set by blocks
        if "type" not in formatted:
            formatted["type"] = "assistant"

        formatted["content"] = "\n".join(content_parts) if content_parts else ""

        # Extract model metadata from AssistantMessage
        if hasattr(message_obj, "model"):
            formatted["metadata"]["model"] = message_obj.model

    # Handle ResultMessage
    elif isinstance(message_obj, ResultMessage):
        # Check if this was interrupted based on subtype and interrupt_initiated flag
        # When interrupt() is called, SDK may return with subtype "error_during_execution"
        # instead of raising exit code -9
        is_interrupted = interrupt_initiated and message_obj.subtype == "error_during_execution"

        formatted["type"] = "interrupted" if is_interrupted else "done"
        formatted["content"] = "Request interrupted by user" if is_interrupted else ""
        formatted["metadata"] = {
            "subtype": message_obj.subtype,
            "duration_ms": message_obj.duration_ms,
            "is_error": message_obj.is_error,
            "num_turns": message_obj.num_turns,
            "total_cost_usd": message_obj.total_cost_usd,
            "session_id": message_obj.session_id,
        }

        if is_interrupted:
            logger.info(f"Detected interrupted session from ResultMessage subtype: {message_obj.subtype}")

    # Handle UserMessage (SDK sends these for interrupt notifications)
    elif isinstance(message_obj, UserMessage):
        # Check if this is an interrupt notification
        content_str = str(message_obj.content)
        if "[Request interrupted by user" in content_str:
            # This is an interrupt notification - show it to the user
            formatted["type"] = "system"
            formatted["content"] = "⚠️ Request interrupted by user"
            formatted["metadata"]["interrupt_notification"] = True
            return formatted
        else:
            # Other UserMessages - skip them (internal SDK messages)
            logger.debug(f"Skipping UserMessage: {content_str[:100]}")
            return None

    # Handle other message types
    else:
        logger.warning(f"Unknown message type: {type(message_obj)}")
        formatted["type"] = "unknown"
        formatted["content"] = ""
        return None

    return formatted
