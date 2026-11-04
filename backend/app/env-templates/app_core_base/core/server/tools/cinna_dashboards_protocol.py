"""
Cinna dashboards — the `cinna` MCP server's tile tools, engine-agnostic.

``dashboards_tiles`` and ``dashboards_update_tile`` (names, descriptions and
input schemas read from ``dashboards-tools.schema.json``, authored in
cinna-core's ``docs/local_agent_kit/protocols/`` and installed at
``/app/core/protocols/dashboards-tools.schema.json``, with the loader of
``cinna_tasks_protocol``). Served on the same `cinna` server as the task and
artifact tools, by both bindings:

* Claude Code — the in-process SDK server built by ``cinna_tasks_claude.py``,
  only in a conversation whose HMAC-verified session context carries a
  ``dashboard_id`` (toolset ``"dashboards"``);
* OpenCode — the stdio bridge ``mcp_bridge/cinna_server.py``. OpenCode lists
  an MCP server's tools once per connection, so in conversation mode the bridge
  always lists them; outside a dashboard chat the backend answers
  ``no_dashboard``.

A call posts to the backend's ``/api/v1/agent/dashboards/<route>`` with the
backend session id. The backend re-reads the scope on every call (the session's
dashboard, the calling agent's tiles), so nothing here is trusted for access.
It answers 200 with the result object, or 200 ``{"error": {...}}`` with one of
the schema's error codes, which becomes an error result.

No engine SDK imports, and the sibling import works both as a package module
(Claude binding) and as a plain module next to the stdio bridge.
"""
from __future__ import annotations

import hashlib
import hmac
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

# Where the files sit inside an environment's core directory.
SCHEMA_RELATIVE_PATH = Path("protocols") / "dashboards-tools.schema.json"
SKILL_RELATIVE_PATH = Path("skills") / "cinna-dashboards"

_REPO_KIT_SNAPSHOT = (
    Path("platform-knowledge-env") / "app" / "workspace" / "knowledge" / "local-kit"
)
_HERE = Path(__file__).resolve()

# Every tool name in the schema starts with this; the rest names its route
# (``dashboards_update_tile`` -> ``update-tile``).
TOOL_PREFIX = "dashboards_"
_ROUTE_BASE = "/api/v1/agent/dashboards/"

# The schema's error code for "this chat has no dashboard".
NO_DASHBOARD = "no_dashboard"


def dashboards_schema_path() -> Path:
    """The ``dashboards-tools.schema.json`` this environment serves."""
    override = os.getenv("CINNA_DASHBOARDS_SCHEMA_PATH")
    if override:
        return Path(override)
    core_dir = _HERE.parents[2]  # tools -> server -> core
    installed = core_dir / SCHEMA_RELATIVE_PATH
    if installed.is_file():
        return installed
    templates_dir = _HERE.parents[4]  # core -> app_core_base -> env-templates
    return templates_dir / _REPO_KIT_SNAPSHOT / SCHEMA_RELATIVE_PATH


def load_dashboard_specs(path: Path | None = None) -> dict[str, dict[str, Any]]:
    """``{tool name: {"description", "inputSchema"}}`` from ``dashboards-tools.schema.json``."""
    specs = load_tool_specs(path or dashboards_schema_path())
    stray = [name for name in specs if not name.startswith(TOOL_PREFIX)]
    if stray:
        raise ValueError(f"dashboards-tools.schema.json tools must start with {TOOL_PREFIX!r}: {stray}")
    return specs


def route_for(name: str) -> str:
    """The backend route a tool posts to, derived from its schema name."""
    return name[len(TOOL_PREFIX):].replace("_", "-")


# ── The verified dashboard of a chat ─────────────────────────────────────


def verified_session_context(
    session_state: dict | None, signing_key: str | None = None,
) -> dict | None:
    """``session_state["session_context"]`` when its HMAC signature checks out, else None.

    Same canonical form as ``ActiveSessionManager._verify_hmac`` and the
    backend's ``session_context_signer``. Stricter than that store on purpose:
    a missing signature or a missing key (``AGENT_AUTH_TOKEN``) means
    unverified, because what it gates is the agent's reach into a dashboard.
    """
    if not isinstance(session_state, dict):
        return None
    context = session_state.get("session_context")
    signature = session_state.get("session_context_signature")
    key = signing_key if signing_key is not None else os.getenv("AGENT_AUTH_TOKEN", "")
    if not isinstance(context, dict) or not isinstance(signature, str) or not signature or not key:
        return None
    canonical = json.dumps(context, sort_keys=True, separators=(",", ":")).encode("utf-8")
    expected = hmac.new(key.encode("utf-8"), canonical, hashlib.sha256).hexdigest()
    return context if hmac.compare_digest(expected, signature) else None


def verified_dashboard_id(session_state: dict | None, signing_key: str | None = None) -> str | None:
    """The chat's ``dashboard_id`` from a verified session context, else None."""
    context = verified_session_context(session_state, signing_key)
    dashboard_id = (context or {}).get("dashboard_id")
    return str(dashboard_id) if dashboard_id else None


# ── Calls ─────────────────────────────────────────────────────────────────


@dataclass
class DashboardToolResult:
    """One tool call's outcome, for both bindings.

    ``text`` is the JSON of ``structured`` whenever the backend answered (the
    result object, or ``{"error": {...}}``), so a binding that cannot carry
    ``structuredContent`` still gives the model the same data. A transport
    failure has no schema error code: plain text and ``structured`` None.
    """

    text: str
    is_error: bool
    structured: dict[str, Any] | None = field(default=None)


def _error_code(response: httpx.Response) -> str | None:
    """``error.code`` of a response body, if it has one (never anything else from it)."""
    try:
        error = response.json().get("error")
    except Exception:  # noqa: BLE001 — a non-JSON or non-object body
        return None
    return error.get("code") if isinstance(error, dict) else None


def _error_result(code: str, message: str) -> DashboardToolResult:
    body = {"error": {"code": code, "message": message}}
    return DashboardToolResult(json.dumps(body, ensure_ascii=False), True, body)


class CinnaDashboardsClient:
    """Forwards a ``dashboards_*`` call to ``/api/v1/agent/dashboards/*``.

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
            self._tool_names = frozenset(load_dashboard_specs())
        return self._tool_names

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.auth_token}",
            "X-Agent-Env-Id": self.env_id,
            "Content-Type": "application/json",
        }

    def call(self, name: str, args: dict[str, Any], session_id: str | None) -> tuple[str, bool]:
        """``(text, is_error)``: the shape every `cinna` client shares."""
        result = self.call_result(name, args, session_id)
        return result.text, result.is_error

    def call_result(self, name: str, args: dict[str, Any], session_id: str | None) -> DashboardToolResult:
        if name not in self.tool_names:
            return DashboardToolResult(f"Error: unknown tool {name}", True)
        if not self.backend_url or not self.auth_token:
            return DashboardToolResult("Error: the dashboard service is not configured in this environment", True)
        if not session_id:
            return _error_result(NO_DASHBOARD, "This chat has no dashboard: it was not opened from one.")

        # Top-level None means "omitted"; a None inside `params` resets that
        # param to its default, so `params` itself is passed through as given.
        payload = {key: value for key, value in (args or {}).items() if value is not None}
        payload["session_id"] = session_id

        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.post(
                    f"{self.backend_url}{_ROUTE_BASE}{route_for(name)}",
                    json=payload,
                    headers=self._headers(),
                )
        except httpx.TimeoutException:
            if "params" in payload:
                return DashboardToolResult(
                    "Error: the request timed out and the params change may or may not have been "
                    "applied; list the tiles again to see the tile's current params before repeating it",
                    True,
                )
            return DashboardToolResult("Error: the request timed out", True)
        except httpx.RequestError as exc:
            return DashboardToolResult(f"Error: could not reach the dashboard service: {exc}", True)

        if response.status_code == 401:
            return DashboardToolResult("Error: authentication with the dashboard service failed", True)
        if response.status_code != 200:
            # Status and error code only: a validation error body echoes the
            # input (params, highlight ids), which stays out of the logs.
            logger.warning("%s failed HTTP %s (code %s)", name, response.status_code, _error_code(response))
            return DashboardToolResult(f"Error: {_error_detail(response)}", True)

        try:
            body = response.json()
        except ValueError:
            return DashboardToolResult("Error: the dashboard service sent an unreadable answer", True)
        if not isinstance(body, dict):
            return DashboardToolResult("Error: the dashboard service sent an unexpected answer", True)
        text = json.dumps(body, ensure_ascii=False)
        return DashboardToolResult(text, isinstance(body.get("error"), dict), body)
