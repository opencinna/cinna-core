"""
Cinna API capability requests — the `cinna` MCP server's producer tools, engine-agnostic.

``api_requests_list``, ``api_requests_get`` and ``api_requests_update_status``
(names, descriptions and input schemas read from
``api-requests-tools.schema.json``, authored in cinna-core's
``docs/local_agent_kit/protocols/`` and installed at
``/app/core/protocols/api-requests-tools.schema.json``, with the loader of
``cinna_tasks_protocol``). Served on the same `cinna` server as the task and
artifact tools, in **building mode only** and only when the workspace has an
``agent_api/`` folder (the agent serves a REST API), by both bindings:

* Claude Code — the in-process SDK server built by ``cinna_tasks_claude.py``
  (toolset ``"api_requests"``);
* OpenCode — the stdio bridge ``mcp_bridge/cinna_server.py``.

A call posts to the backend's ``/api/v1/agent/api-requests/<route>`` with the
environment's token. The backend scopes every call to the calling agent's own
requests and enforces ``agent_api_enabled``, so nothing here is trusted for
access. It answers 200 with the result object, or 200 ``{"error": {...}}`` with
one of the schema's error codes, which becomes an error result.

No engine SDK imports, and the sibling import works both as a package module
(Claude binding) and as a plain module next to the stdio bridge.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

try:  # package import (core.server.tools.*)
    from .cinna_tasks_protocol import _error_detail, load_tool_specs
except ImportError:  # plain module beside the stdio bridge
    from cinna_tasks_protocol import _error_detail, load_tool_specs  # type: ignore[no-redef]

logger = logging.getLogger(__name__)

# Where the schema sits inside an environment's core directory.
SCHEMA_RELATIVE_PATH = Path("protocols") / "api-requests-tools.schema.json"

_REPO_KIT_SNAPSHOT = (
    Path("platform-knowledge-env") / "app" / "workspace" / "knowledge" / "local-kit"
)
_HERE = Path(__file__).resolve()

# Every tool name in the schema starts with this; the rest names its route
# (``api_requests_update_status`` -> ``update-status``).
TOOL_PREFIX = "api_requests_"
_ROUTE_BASE = "/api/v1/agent/api-requests/"

# The folder whose presence means "this agent serves a REST API" (D10).
AGENT_API_DIR = Path(os.getenv("CINNA_AGENT_API_DIR", "/app/workspace/agent_api"))


def api_requests_schema_path() -> Path:
    """The ``api-requests-tools.schema.json`` this environment serves."""
    override = os.getenv("CINNA_API_REQUESTS_SCHEMA_PATH")
    if override:
        return Path(override)
    core_dir = _HERE.parents[2]  # tools -> server -> core
    installed = core_dir / SCHEMA_RELATIVE_PATH
    if installed.is_file():
        return installed
    templates_dir = _HERE.parents[4]  # core -> app_core_base -> env-templates
    return templates_dir / _REPO_KIT_SNAPSHOT / SCHEMA_RELATIVE_PATH


def load_api_request_specs(path: Path | None = None) -> dict[str, dict[str, Any]]:
    """``{tool name: {"description", "inputSchema"}}`` from ``api-requests-tools.schema.json``."""
    specs = load_tool_specs(path or api_requests_schema_path())
    stray = [name for name in specs if not name.startswith(TOOL_PREFIX)]
    if stray:
        raise ValueError(
            f"api-requests-tools.schema.json tools must start with {TOOL_PREFIX!r}: {stray}"
        )
    return specs


def route_for(name: str) -> str:
    """The backend route a tool posts to, derived from its schema name."""
    return name[len(TOOL_PREFIX):].replace("_", "-")


def serves_agent_api(agent_api_dir: Path | None = None) -> bool:
    """True when the workspace has an ``agent_api/`` folder (the tools apply)."""
    return (agent_api_dir or AGENT_API_DIR).is_dir()


@dataclass
class ApiRequestToolResult:
    """One tool call's outcome, for both bindings (same shape as the dashboards one)."""

    text: str
    is_error: bool
    structured: dict[str, Any] | None = field(default=None)


class CinnaApiRequestsClient:
    """Forwards an ``api_requests_*`` call to ``/api/v1/agent/api-requests/*``.

    Synchronous on purpose, like ``CinnaTasksClient``: the Claude binding runs
    it in a worker thread, the OpenCode bridge is its own process.
    """

    def __init__(
        self,
        backend_url: str | None = None,
        auth_token: str | None = None,
        env_id: str | None = None,
        tool_names: frozenset[str] | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.backend_url = backend_url if backend_url is not None else os.getenv("BACKEND_URL", "http://backend:8000")
        self.auth_token = auth_token if auth_token is not None else os.getenv("AGENT_AUTH_TOKEN", "")
        self.env_id = env_id if env_id is not None else os.getenv("ENV_ID", "")
        self._tool_names = tool_names
        self.timeout = timeout

    @property
    def tool_names(self) -> frozenset[str]:
        if self._tool_names is None:
            self._tool_names = frozenset(load_api_request_specs())
        return self._tool_names

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.auth_token}",
            "X-Agent-Env-Id": self.env_id,
            "Content-Type": "application/json",
        }

    def call(self, name: str, args: dict[str, Any], session_id: str | None = None) -> tuple[str, bool]:
        """``(text, is_error)``: the shape every `cinna` client shares."""
        result = self.call_result(name, args)
        return result.text, result.is_error

    def call_result(self, name: str, args: dict[str, Any]) -> ApiRequestToolResult:
        if name not in self.tool_names:
            return ApiRequestToolResult(f"Error: unknown tool {name}", True)
        if not self.backend_url or not self.auth_token:
            return ApiRequestToolResult(
                "Error: the capability request service is not configured in this environment", True
            )

        payload = {key: value for key, value in (args or {}).items() if value is not None}
        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.post(
                    f"{self.backend_url}{_ROUTE_BASE}{route_for(name)}",
                    json=payload,
                    headers=self._headers(),
                )
        except httpx.TimeoutException:
            return ApiRequestToolResult("Error: the request timed out", True)
        except httpx.RequestError as exc:
            return ApiRequestToolResult(f"Error: could not reach the capability request service: {exc}", True)

        if response.status_code == 401:
            return ApiRequestToolResult("Error: authentication with the capability request service failed", True)
        if response.status_code != 200:
            # Status only: a validation error body echoes the input.
            logger.warning("%s failed HTTP %s", name, response.status_code)
            return ApiRequestToolResult(f"Error: {_error_detail(response)}", True)

        try:
            body = response.json()
        except ValueError:
            return ApiRequestToolResult("Error: the capability request service sent an unreadable answer", True)
        if not isinstance(body, dict):
            return ApiRequestToolResult("Error: the capability request service sent an unexpected answer", True)
        return ApiRequestToolResult(
            json.dumps(body, ensure_ascii=False), isinstance(body.get("error"), dict), body
        )
