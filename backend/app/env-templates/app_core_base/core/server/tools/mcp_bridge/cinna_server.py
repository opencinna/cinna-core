"""
The `cinna` MCP server for OpenCode (stdio bridge).

Serves the Cinna Task Protocol tools (``tasks_targets`` … ``tasks_reply``)
and ``artifacts_publish`` with the exact names, descriptions and input schemas
of ``tasks.schema.json`` and ``artifacts.schema.json`` — which is why this uses
the low-level MCP server rather than FastMCP, whose schemas come from Python
signatures. Calls go through ``CinnaTasksClient`` / ``CinnaArtifactsClient``,
shared with the Claude Code binding. The bridge runs in both OpenCode modes,
so artifacts are available while building and in conversation.

The dashboard tile tools (``dashboards-tools.schema.json``) are served in
conversation mode only. OpenCode lists an MCP server's tools once per
connection (again only on a ``tools/list_changed`` notification), so they are
listed in every conversation chat; outside a chat opened from a dashboard the
backend answers ``no_dashboard``. Their results carry ``structuredContent``
beside the same JSON as text.

The capability-request tools (``api-requests-tools.schema.json``) are served
in building mode only, and only when the workspace has ``agent_api/`` (the
agent serves a REST API); the backend scopes them to the agent's own requests.

Session context (backend_session_id, dashboard_id) is read at call time from
the file the OpenCode adapter writes before each message
(``CINNA_SESSION_CONTEXT_PATH``); the mode from ``CINNA_OPENCODE_MODE``. That
file is one per mode, so the latest message wins. A dashboard tool call
instead carries the calling OpenCode session id (``SESSION_ARG``, stamped by
``cinna_session_plugin.js``), and its context is read from the per-session copy
``session_context/<id>.json`` beside it. Arguments are validated here, after
that key is removed, rather than by the low-level server.

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

import jsonschema  # noqa: E402
import mcp.types as types  # noqa: E402
from mcp.server.lowlevel import Server  # noqa: E402

from cinna_api_requests_protocol import (  # noqa: E402
    CinnaApiRequestsClient,
    load_api_request_specs,
    serves_agent_api,
)
from cinna_artifacts_protocol import CinnaArtifactsClient, load_artifact_specs  # noqa: E402
from cinna_dashboards_protocol import CinnaDashboardsClient, load_dashboard_specs  # noqa: E402
from cinna_tasks_protocol import SERVER_NAME, CinnaTasksClient, load_tool_specs  # noqa: E402

logger = logging.getLogger(__name__)

SESSION_CONTEXT_PATH = Path(os.getenv("CINNA_SESSION_CONTEXT_PATH") or "session_context.json")


# The argument cinna_session_plugin.js adds to a dashboard tool call.
SESSION_ARG = "_cinna_opencode_session_id"
SESSION_CONTEXT_DIRNAME = "session_context"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not read %s: %s", path.name, exc)
    return {}


def _read_session_context() -> dict[str, Any]:
    return _read_json(SESSION_CONTEXT_PATH)


def _read_backend_session_id() -> str | None:
    return _read_session_context().get("backend_session_id") or None


def _is_safe_session_id(session_id: Any) -> bool:
    return (
        isinstance(session_id, str)
        and 0 < len(session_id) <= 128
        and all((ch.isascii() and ch.isalnum()) or ch in "_-" for ch in session_id)
    )


def _backend_session_id_for(opencode_session_id: Any) -> str | None:
    """The backend session of the OpenCode session that made the call.

    No stamped id (the plugin is not loaded) falls back to the shared file. A
    stamped id with no context of its own resolves to no session, never to
    another chat's.
    """
    if opencode_session_id is None:
        return _read_backend_session_id()
    if not _is_safe_session_id(opencode_session_id):
        return None
    path = SESSION_CONTEXT_PATH.parent / SESSION_CONTEXT_DIRNAME / f"{opencode_session_id}.json"
    return _read_json(path).get("backend_session_id") or None


def _dashboard_tools_offered() -> bool:
    """Conversation mode lists the tile tools (Q1: OpenCode caches the list).

    An adapter that predates ``CINNA_OPENCODE_MODE`` sets no mode; then the
    current chat's ``dashboard_id`` decides.
    """
    mode = os.getenv("CINNA_OPENCODE_MODE")
    if mode:
        return mode == "conversation"
    return bool(_read_session_context().get("dashboard_id"))


def _artifact_specs_if_installed() -> dict[str, dict[str, Any]]:
    """The artifact tool's specs; none when an older core lacks the schema."""
    try:
        return load_artifact_specs()
    except FileNotFoundError as exc:
        logger.warning("artifacts.schema.json not installed; serving task tools only: %s", exc)
        return {}


def _dashboard_specs_if_installed() -> dict[str, dict[str, Any]]:
    """The tile tools' specs; none when an older core lacks the schema."""
    try:
        return load_dashboard_specs()
    except FileNotFoundError as exc:
        logger.warning("dashboards-tools.schema.json not installed; no dashboard tools: %s", exc)
        return {}


def _api_request_tools_offered() -> bool:
    """Building mode in an agent that serves a REST API (``agent_api/``)."""
    return os.getenv("CINNA_OPENCODE_MODE") == "building" and serves_agent_api()


def _api_request_specs_if_installed() -> dict[str, dict[str, Any]]:
    """The capability-request tools' specs; none when an older core lacks the schema."""
    try:
        return load_api_request_specs()
    except FileNotFoundError as exc:
        logger.warning("api-requests-tools.schema.json not installed; no api_requests tools: %s", exc)
        return {}


def build_server(
    specs: dict[str, dict[str, Any]] | None = None,
    client: CinnaTasksClient | None = None,
    artifact_specs: dict[str, dict[str, Any]] | None = None,
    artifacts_client: CinnaArtifactsClient | None = None,
    dashboard_specs: dict[str, dict[str, Any]] | None = None,
    dashboards_client: CinnaDashboardsClient | None = None,
    api_request_specs: dict[str, dict[str, Any]] | None = None,
    api_requests_client: CinnaApiRequestsClient | None = None,
) -> Server:
    task_specs = specs if specs is not None else load_tool_specs()
    artifact_specs = artifact_specs if artifact_specs is not None else _artifact_specs_if_installed()
    dashboard_specs = dashboard_specs if dashboard_specs is not None else _dashboard_specs_if_installed()
    task_client = client or CinnaTasksClient()
    artifacts_client = artifacts_client or CinnaArtifactsClient()
    dashboards_client = dashboards_client or CinnaDashboardsClient(tool_names=frozenset(dashboard_specs))
    if api_request_specs is None:
        api_request_specs = _api_request_specs_if_installed() if _api_request_tools_offered() else {}
    api_requests_client = api_requests_client or CinnaApiRequestsClient(
        tool_names=frozenset(api_request_specs)
    )
    specs = {**task_specs, **artifact_specs}
    server = Server(SERVER_NAME)

    def _offered() -> dict[str, dict[str, Any]]:
        offered = dict(specs)
        if dashboard_specs and _dashboard_tools_offered():
            offered.update(dashboard_specs)
        if api_request_specs:
            offered.update(api_request_specs)
        return offered

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [
            types.Tool(name=name, description=spec["description"], inputSchema=spec["inputSchema"])
            for name, spec in _offered().items()
        ]

    @server.call_tool(validate_input=False)
    async def call_tool(
        name: str, arguments: dict[str, Any] | None,
    ) -> list[types.TextContent] | types.CallToolResult:
        offered = _offered()
        if name not in offered:
            raise ValueError(f"Unknown tool: {name}")
        arguments = dict(arguments or {})
        opencode_session_id = arguments.pop(SESSION_ARG, None)
        try:
            jsonschema.validate(instance=arguments, schema=offered[name]["inputSchema"])
        except jsonschema.ValidationError as exc:
            # The message the low-level server gave when it validated.
            raise ValueError(f"Input validation error: {exc.message}") from exc
        if name in dashboard_specs:
            result = await asyncio.to_thread(
                dashboards_client.call_result, name, arguments,
                _backend_session_id_for(opencode_session_id),
            )
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=result.text)],
                structuredContent=result.structured,
                isError=result.is_error,
            )
        if name in api_request_specs:
            api_result = await asyncio.to_thread(
                api_requests_client.call_result, name, arguments,
            )
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=api_result.text)],
                structuredContent=api_result.structured,
                isError=api_result.is_error,
            )
        target = artifacts_client if name in artifact_specs else task_client
        text, is_error = await asyncio.to_thread(
            target.call, name, arguments, _read_backend_session_id(),
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
