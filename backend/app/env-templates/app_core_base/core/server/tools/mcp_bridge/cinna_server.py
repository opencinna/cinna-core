"""
The `cinna` MCP server for OpenCode (stdio bridge).

Serves the Cinna Task Protocol tools (``tasks_targets`` … ``tasks_reply``)
with the exact names, descriptions and input schemas of ``tasks.schema.json``
— which is why this uses the low-level MCP server rather than FastMCP, whose
schemas come from Python signatures. Calls go through ``CinnaTasksClient``,
shared with the Claude Code binding.

Session context (backend_session_id) is read at call time from the file the
OpenCode adapter writes before each message (``CINNA_SESSION_CONTEXT_PATH``).

Run with:
    python3 /app/core/server/tools/mcp_bridge/cinna_server.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

# The shared implementation sits one directory up; this file runs as a script.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import mcp.types as types  # noqa: E402
from mcp.server.lowlevel import Server  # noqa: E402

from cinna_tasks_protocol import SERVER_NAME, CinnaTasksClient, load_tool_specs  # noqa: E402

logger = logging.getLogger(__name__)

SESSION_CONTEXT_PATH = Path(os.getenv("CINNA_SESSION_CONTEXT_PATH") or "session_context.json")


def _read_backend_session_id() -> str | None:
    try:
        if SESSION_CONTEXT_PATH.exists():
            data = json.loads(SESSION_CONTEXT_PATH.read_text(encoding="utf-8"))
            return data.get("backend_session_id") or None
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not read session_context.json: %s", exc)
    return None


def build_server(
    specs: dict[str, dict[str, Any]] | None = None,
    client: CinnaTasksClient | None = None,
) -> Server:
    specs = specs if specs is not None else load_tool_specs()
    client = client or CinnaTasksClient()
    server = Server(SERVER_NAME)

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [
            types.Tool(name=name, description=spec["description"], inputSchema=spec["inputSchema"])
            for name, spec in specs.items()
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any] | None) -> list[types.TextContent]:
        if name not in specs:
            raise ValueError(f"Unknown tool: {name}")
        text, is_error = await asyncio.to_thread(
            client.call, name, arguments or {}, _read_backend_session_id(),
        )
        if is_error:
            # The low-level server turns a raised error into an isError result.
            raise RuntimeError(text)
        return [types.TextContent(type="text", text=text)]

    return server


async def _main() -> None:
    from mcp.server.stdio import stdio_server

    server = build_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(_main())
