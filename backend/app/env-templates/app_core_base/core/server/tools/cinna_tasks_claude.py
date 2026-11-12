"""
The `cinna` MCP server for Claude Code (in-process SDK server).

Registers one SDK tool per entry in the protocol schemas it is asked for —
``tasks.schema.json`` (toolset ``"tasks"``), ``artifacts.schema.json``
(toolset ``"artifacts"``), ``dashboards-tools.schema.json`` (toolset
``"dashboards"``) and ``api-requests-tools.schema.json`` (toolset
``"api_requests"``) — with the schema's own description and input schema, each
forwarding to its client (``CinnaTasksClient`` / ``CinnaArtifactsClient`` /
``CinnaDashboardsClient``). The OpenCode binding (``mcp_bridge/cinna_server.py``)
serves the same specs.

The SDK's tool result carries ``content`` and ``is_error`` only (no
``structuredContent``), so a dashboards call returns its JSON as text.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from typing import Any, Protocol

from claude_agent_sdk import create_sdk_mcp_server, tool

from ..adapters.claude_code_sdk_adapter import get_backend_session_id
from .cinna_api_requests_protocol import CinnaApiRequestsClient, load_api_request_specs
from .cinna_artifacts_protocol import CinnaArtifactsClient, load_artifact_specs
from .cinna_dashboards_protocol import CinnaDashboardsClient, load_dashboard_specs
from .cinna_tasks_protocol import SERVER_NAME, CinnaTasksClient, load_tool_specs

logger = logging.getLogger(__name__)

TOOLSETS = ("tasks", "artifacts", "dashboards", "api_requests")


class _Client(Protocol):
    def call(self, name: str, args: dict[str, Any], session_id: str | None) -> tuple[str, bool]: ...


def _handler(client: _Client, name: str):
    async def handle(args: dict[str, Any]) -> dict[str, Any]:
        # Read in the calling task: the backend session lives in its context.
        session_id = get_backend_session_id()
        text, is_error = await asyncio.to_thread(client.call, name, args or {}, session_id)
        result: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
        if is_error:
            result["is_error"] = True
        return result

    return handle


def _load(toolsets: Iterable[str]) -> list[tuple[str, dict[str, Any], _Client]]:
    """``(name, spec, client)`` per tool, toolsets in the order given."""
    entries: list[tuple[str, dict[str, Any], _Client]] = []
    for toolset in toolsets:
        if toolset == "tasks":
            specs, client = load_tool_specs(), CinnaTasksClient()
        elif toolset == "artifacts":
            try:
                specs = load_artifact_specs()
            except FileNotFoundError as exc:
                # A core whose kit snapshot predates artifacts: keep the task tools.
                logger.warning("artifacts.schema.json not installed; no artifacts tool: %s", exc)
                continue
            client = CinnaArtifactsClient()
        elif toolset == "dashboards":
            try:
                specs = load_dashboard_specs()
            except FileNotFoundError as exc:
                # A core whose kit snapshot predates dashboards: keep the rest.
                logger.warning("dashboards-tools.schema.json not installed; no dashboard tools: %s", exc)
                continue
            client = CinnaDashboardsClient(tool_names=frozenset(specs))
        elif toolset == "api_requests":
            try:
                specs = load_api_request_specs()
            except FileNotFoundError as exc:
                # A core whose kit snapshot predates capability requests.
                logger.warning("api-requests-tools.schema.json not installed; no api_requests tools: %s", exc)
                continue
            client = CinnaApiRequestsClient(tool_names=frozenset(specs))
        else:
            raise ValueError(f"Unknown cinna toolset: {toolset!r}")
        entries.extend((name, spec, client) for name, spec in specs.items())
    return entries


def build_cinna_tools(
    specs: dict[str, dict[str, Any]] | None = None,
    toolsets: Iterable[str] = ("tasks",),
) -> list[Any]:
    """One SDK tool per protocol tool, in the schemas' order.

    ``specs`` (task tools only) is kept for callers that already loaded them.
    """
    if specs is not None:
        client = CinnaTasksClient()
        entries = [(name, spec, client) for name, spec in specs.items()]
    else:
        entries = _load(toolsets)
    return _sdk_tools(entries)


def _sdk_tools(entries: list[tuple[str, dict[str, Any], _Client]]) -> list[Any]:
    return [
        tool(name, spec["description"], spec["inputSchema"])(_handler(client, name))
        for name, spec, client in entries
    ]


def build_cinna_server(toolsets: Iterable[str] = ("tasks",)) -> tuple[Any, list[str]]:
    """``(sdk server, engine-visible tool names to allow)`` for the given toolsets."""
    entries = _load(tuple(toolsets))
    server = create_sdk_mcp_server(name=SERVER_NAME, version="1.0.0", tools=_sdk_tools(entries))
    return server, [f"mcp__{SERVER_NAME}__{name}" for name, _spec, _client in entries]
