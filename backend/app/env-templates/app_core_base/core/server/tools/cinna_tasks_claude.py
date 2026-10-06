"""
The `cinna` MCP server for Claude Code (in-process SDK server).

Registers one SDK tool per entry in ``tasks.schema.json`` with the schema's own
description and input schema, all forwarding to ``CinnaTasksClient``. The
OpenCode binding (``mcp_bridge/cinna_server.py``) serves the same specs.
"""
from __future__ import annotations

import asyncio
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from ..adapters.claude_code_sdk_adapter import get_backend_session_id
from .cinna_tasks_protocol import SERVER_NAME, CinnaTasksClient, load_tool_specs


def _handler(client: CinnaTasksClient, name: str):
    async def handle(args: dict[str, Any]) -> dict[str, Any]:
        # Read in the calling task: the backend session lives in its context.
        session_id = get_backend_session_id()
        text, is_error = await asyncio.to_thread(client.call, name, args or {}, session_id)
        result: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
        if is_error:
            result["is_error"] = True
        return result

    return handle


def build_cinna_tools(specs: dict[str, dict[str, Any]] | None = None) -> list[Any]:
    """One SDK tool per protocol tool, in the schema's order."""
    specs = specs if specs is not None else load_tool_specs()
    client = CinnaTasksClient()
    return [
        tool(name, spec["description"], spec["inputSchema"])(_handler(client, name))
        for name, spec in specs.items()
    ]


def build_cinna_server() -> tuple[Any, list[str]]:
    """``(sdk server, engine-visible tool names to allow)``."""
    specs = load_tool_specs()
    server = create_sdk_mcp_server(
        name=SERVER_NAME, version="1.0.0", tools=build_cinna_tools(specs),
    )
    return server, [f"mcp__{SERVER_NAME}__{name}" for name in specs]
