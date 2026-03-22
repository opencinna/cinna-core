"""
Google ADK Event Transformer

Translates Google ADK runner events into the unified SDKEvent format used by
all adapters. This mirrors the pattern of ClaudeCodeEventTransformer and
OpenCodeEventTransformer.

Google ADK event structure (from google.adk.runners):

    Each event has:
    - content.parts[] — list of parts, each can be:
        - function_call   — tool invocation (name, args)
        - function_response — tool result (response dict)
        - text            — assistant text content
    - author             — "assistant" or other role
"""

import json
import logging
from typing import Any

from .base import SDKEvent, SDKEventType
from .tool_name_registry import normalize_tool_name

logger = logging.getLogger(__name__)


class GoogleADKEventTransformer:
    """
    Translator from Google ADK runner events to SDKEvent objects.

    Handles:
    - Function calls → TOOL_USE (with normalized lowercase tool names)
    - Function responses → TOOL_RESULT (with formatted stdout/stderr)
    - Text responses → ASSISTANT
    """

    def translate(
        self,
        event: Any,
        session_id: str,
    ) -> list[SDKEvent]:
        """
        Translate a single Google ADK event into zero or more SDKEvents.

        Args:
            event: Google ADK runner event object
            session_id: Current session ID

        Returns:
            List of SDKEvent objects (may be empty if event has no content)
        """
        if not event.content or not event.content.parts:
            return []

        events: list[SDKEvent] = []

        for part in event.content.parts:
            # Handle function call (tool invocation)
            if hasattr(part, "function_call") and part.function_call:
                sdk_event = self._handle_function_call(
                    part.function_call, session_id,
                )
                if sdk_event:
                    events.append(sdk_event)

            # Handle function response (tool result)
            if hasattr(part, "function_response") and part.function_response:
                sdk_event = self._handle_function_response(
                    part.function_response, session_id,
                )
                if sdk_event:
                    events.append(sdk_event)

            # Handle text response
            if hasattr(part, "text") and part.text:
                author = getattr(event, "author", "assistant")
                logger.debug("Text from %s: %s...", author, part.text[:100])
                events.append(SDKEvent(
                    type=SDKEventType.ASSISTANT,
                    content=part.text,
                    session_id=session_id,
                    metadata={"author": author},
                ))

        return events

    # ------------------------------------------------------------------
    # Part handlers
    # ------------------------------------------------------------------

    @staticmethod
    def _handle_function_call(func_call, session_id: str) -> SDKEvent:
        """Handle a function_call part → TOOL_USE event."""
        unified_name = normalize_tool_name(func_call.name, sdk="google-adk")
        logger.info("Tool call: %s(%s)", unified_name, func_call.args)

        tool_input_str = _format_tool_input(func_call.args)

        return SDKEvent(
            type=SDKEventType.TOOL_USE,
            tool_name=unified_name,
            content=f"Using tool: {unified_name}{tool_input_str}",
            session_id=session_id,
            metadata={
                "tool_input": dict(func_call.args) if func_call.args else {},
            },
        )

    @staticmethod
    def _handle_function_response(func_resp, session_id: str) -> SDKEvent:
        """Handle a function_response part → TOOL_RESULT event."""
        response_data = func_resp.response

        if isinstance(response_data, dict):
            stdout = str(response_data.get("stdout", ""))[:500]
            stderr = str(response_data.get("stderr", ""))[:500]
            return_code = response_data.get("return_code")

            if return_code is not None:
                result_content = f"Return code: {return_code}"
                if stdout:
                    result_content += f"\nstdout: {stdout}"
                if stderr:
                    result_content += f"\nstderr: {stderr}"
            else:
                # For non-bash tools (like Read), just show the response
                result_content = str(response_data)[:500]
        else:
            result_content = str(response_data)[:500]

        return SDKEvent(
            type=SDKEventType.TOOL_RESULT,
            content=result_content,
            session_id=session_id,
            metadata={
                "result": response_data if isinstance(response_data, dict)
                else {"raw": str(response_data)},
            },
        )


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _format_tool_input(args) -> str:
    """Format tool input args for display, truncating to 200 chars."""
    if not args:
        return ""
    try:
        input_json = json.dumps(dict(args), indent=2)
        if len(input_json) > 200:
            input_json = input_json[:200] + "..."
        return f"\nInput: {input_json}"
    except Exception:
        return f"\nInput: {str(args)[:200]}"
