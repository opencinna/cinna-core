"""
Shared tool contracts — single source of truth for canonical tool definitions.

The ``send_message`` tool name, description, and input schema are defined here
once and reused everywhere they need to be advertised:

  - ``app/mcp/tools.py`` registers it on each per-connector FastMCP server
    (the stateless MCP variant — keeps ``context_id`` so callers can maintain
    conversation continuity themselves).
  - ``app/services/a2a/a2a_service.py`` folds it into the ``cinna.mcp``
    descriptor on the external A2A surface (the desktop-facing variant — drops
    ``context_id`` because the desktop injects continuity itself).

Defining the contract once prevents the cinna-core and cinna-desktop repos from
drifting on the canonical tool shape.
"""
from __future__ import annotations

from typing import Any

# Canonical tool name. Both the MCP connector and the desktop descriptor expose
# the agent's primary action under this name.
SEND_MESSAGE_TOOL_NAME = "send_message"

# Canonical description for the *stateless* MCP connector variant. It instructs
# the caller to round-trip ``context_id`` for conversation continuity.
SEND_MESSAGE_DESCRIPTION = (
    "Send a message to the AI agent and receive a response. "
    "The agent can use tools, write code, and perform tasks based on your message.\n\n"
    "Returns a JSON object with 'response' and 'context_id' fields. "
    "IMPORTANT: Always pass back the 'context_id' from the previous response "
    "to maintain conversation continuity. On the first message in a new conversation, "
    "pass an empty string for context_id."
)

# Canonical description for the *stateful* desktop variant. The desktop persists
# its own A2A session continuity, so the context_id round-trip guidance is
# omitted — the descriptor only documents how to phrase the task.
SEND_MESSAGE_DESKTOP_DESCRIPTION = (
    "Send a self-contained task or question to the AI agent and receive a response. "
    "The agent can use tools, write code, and perform tasks based on your message."
)

# Human-readable description for the single ``message`` argument. Shared by both
# variants so the property documentation never drifts.
SEND_MESSAGE_ARG_DESCRIPTION = "The task or question for the agent."


def build_send_message_input_schema(*, include_context_id: bool) -> dict[str, Any]:
    """Build the JSON Schema for the ``send_message`` tool input.

    Args:
        include_context_id: When True, include the optional ``context_id``
            property (the stateless MCP connector variant). When False, expose
            only ``message`` (the stateful desktop variant).

    Returns:
        A JSON Schema dict describing the tool input object.
    """
    properties: dict[str, Any] = {
        "message": {
            "type": "string",
            "description": SEND_MESSAGE_ARG_DESCRIPTION,
        },
    }
    if include_context_id:
        properties["context_id"] = {
            "type": "string",
            "description": (
                "Opaque conversation identifier. Pass back the 'context_id' "
                "returned by the previous response to continue a conversation; "
                "pass an empty string to start a new one."
            ),
        }
    return {
        "type": "object",
        "properties": properties,
        "required": ["message"],
    }
