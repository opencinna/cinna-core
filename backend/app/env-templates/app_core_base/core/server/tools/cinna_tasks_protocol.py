"""
Cinna Task Protocol (CTP) v1 — the `cinna` MCP server's tools, engine-agnostic.

One implementation behind both bindings in this environment:

* Claude Code — the in-process SDK server built by ``cinna_tasks_claude.py``;
* OpenCode — the stdio bridge ``mcp_bridge/cinna_server.py``.

Tool names, descriptions and input schemas are NOT written here: they are read
from ``tasks.schema.json``, the protocol's one source of truth (authored in
cinna-core's ``docs/local_agent_kit/protocols/``, copied into every environment
at ``/app/core/protocols/tasks.schema.json`` when its core files are laid down).
``$ref`` pointers into the schema's ``$defs`` are inlined, because an MCP tool's
``inputSchema`` must stand alone.

This module imports no engine SDK and uses no relative imports, so the stdio
bridge can load it as a plain module from its own directory's parent.
"""
from __future__ import annotations

import copy
import json
import logging
import os
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

SERVER_NAME = "cinna"
PROTOCOL_VERSION = 1

# Where the protocol files sit inside an environment's core directory.
SCHEMA_RELATIVE_PATH = Path("protocols") / "tasks.schema.json"
SKILL_RELATIVE_PATH = Path("skills") / "cinna-tasks"

# A repository checkout has no core/protocols/ (it is installed per environment);
# the kit snapshot beside app_core_base is the same file.
_REPO_KIT_SNAPSHOT = (
    Path("platform-knowledge-env") / "app" / "workspace" / "knowledge" / "local-kit"
)

_HERE = Path(__file__).resolve()
WORKSPACE_DIR = Path("/app/workspace")


def schema_path() -> Path:
    """The ``tasks.schema.json`` this environment serves."""
    override = os.getenv("CINNA_TASKS_SCHEMA_PATH")
    if override:
        return Path(override)
    core_dir = _HERE.parents[2]  # tools -> server -> core
    installed = core_dir / SCHEMA_RELATIVE_PATH
    if installed.is_file():
        return installed
    templates_dir = _HERE.parents[4]  # core -> app_core_base -> env-templates
    return templates_dir / _REPO_KIT_SNAPSHOT / SCHEMA_RELATIVE_PATH


def _inline_refs(node: Any, defs: dict[str, Any]) -> Any:
    """Replace every ``{"$ref": "#/$defs/x", ...}`` with ``$defs.x`` merged with its siblings."""
    if isinstance(node, list):
        return [_inline_refs(item, defs) for item in node]
    if not isinstance(node, dict):
        return node
    if "$ref" in node:
        ref = node["$ref"]
        prefix = "#/$defs/"
        if not ref.startswith(prefix) or ref[len(prefix):] not in defs:
            raise ValueError(f"Unresolvable $ref in tasks.schema.json: {ref}")
        merged = copy.deepcopy(defs[ref[len(prefix):]])
        merged.update({key: value for key, value in node.items() if key != "$ref"})
        return _inline_refs(merged, defs)
    return {key: _inline_refs(value, defs) for key, value in node.items()}


def load_tool_specs(path: Path | None = None) -> dict[str, dict[str, Any]]:
    """``{tool name: {"description", "inputSchema"}}`` in the schema's order."""
    document = json.loads((path or schema_path()).read_text(encoding="utf-8"))
    if document.get("server") != SERVER_NAME:
        raise ValueError(f"tasks.schema.json declares server {document.get('server')!r}, expected {SERVER_NAME!r}")
    defs = document.get("$defs") or {}
    return {
        name: {
            "description": spec["description"],
            "inputSchema": _inline_refs(spec["inputSchema"], defs),
        }
        for name, spec in document["tools"].items()
    }


# ── Calls ─────────────────────────────────────────────────────────────────

_GET_ROUTES = {
    "tasks_targets": "targets",
    "tasks_list": "list",
    "tasks_get": "get",
}
_POST_ROUTES = {
    "tasks_create": "create",
    "tasks_update": "update",
    "tasks_comment": "comment",
    "tasks_report": "report",
    "tasks_reply": "reply",
}


def _error_detail(response: httpx.Response) -> str:
    try:
        detail = response.json().get("detail")
    except Exception:  # noqa: BLE001 — a non-JSON error body
        return response.text or f"HTTP {response.status_code}"
    if isinstance(detail, list):  # FastAPI request validation
        return "; ".join(
            f"{'.'.join(str(p) for p in item.get('loc', ())[1:]) or 'request'}: {item.get('msg', '')}"
            for item in detail
            if isinstance(item, dict)
        ) or str(detail)
    return str(detail or response.text or f"HTTP {response.status_code}")


class CinnaTasksClient:
    """Forwards a ``tasks_*`` call to ``/api/v1/agent/tasks/protocol/*``.

    Synchronous on purpose: the Claude binding runs it in a worker thread, the
    OpenCode bridge is its own process. Returns ``(text, is_error)``.
    """

    def __init__(
        self,
        backend_url: str | None = None,
        auth_token: str | None = None,
        env_id: str | None = None,
        workspace_dir: Path = WORKSPACE_DIR,
        timeout: float = 30.0,
    ) -> None:
        self.backend_url = backend_url if backend_url is not None else os.getenv("BACKEND_URL", "http://backend:8000")
        self.auth_token = auth_token if auth_token is not None else os.getenv("AGENT_AUTH_TOKEN", "")
        self.env_id = env_id if env_id is not None else os.getenv("ENV_ID", "")
        self.workspace_dir = workspace_dir
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.auth_token}",
            "X-Agent-Env-Id": self.env_id,
            "Content-Type": "application/json",
        }

    def _resolve_files(self, files: list[str]) -> tuple[list[str], list[str]]:
        """Workspace-relative paths -> absolute ones; also the ones that do not exist."""
        resolved, missing = [], []
        for name in files:
            path = Path(name)
            if not path.is_absolute():
                path = self.workspace_dir / path
            if path.exists():
                resolved.append(str(path))
            else:
                missing.append(name)
        return resolved, missing

    def call(self, name: str, args: dict[str, Any], session_id: str | None) -> tuple[str, bool]:
        if name not in _GET_ROUTES and name not in _POST_ROUTES:
            return f"Error: unknown tool {name}", True
        if not self.backend_url or not self.auth_token:
            return "Error: the task service is not configured in this environment", True
        if not session_id:
            return "Error: no active conversation; task tools work only inside a conversation", True

        payload = {key: value for key, value in (args or {}).items() if value is not None}
        if name == "tasks_comment" and payload.get("files"):
            resolved, missing = self._resolve_files(list(payload["files"]))
            if missing:
                listed = "\n".join(f"  - {item}" for item in missing)
                return (
                    f"Error: these files were not found in the workspace:\n{listed}\n"
                    "Nothing was posted. Fix the paths and try again.",
                    True,
                )
            payload["files"] = resolved

        url = f"{self.backend_url}/api/v1/agent/tasks/protocol/"
        try:
            with httpx.Client(timeout=self.timeout) as client:
                if name in _GET_ROUTES:
                    params = {"source_session_id": session_id}
                    for key, value in payload.items():
                        params[key] = str(value).lower() if isinstance(value, bool) else value
                    response = client.get(url + _GET_ROUTES[name], params=params, headers=self._headers())
                else:
                    response = client.post(
                        url + _POST_ROUTES[name],
                        json={**payload, "source_session_id": session_id},
                        headers=self._headers(),
                    )
        except httpx.TimeoutException:
            if name in _POST_ROUTES:
                return (
                    "Error: the request timed out and may or may not have been applied; "
                    "check with tasks_get or tasks_list before repeating it",
                    True,
                )
            return "Error: the request timed out", True
        except httpx.RequestError as exc:
            return f"Error: could not reach the task service: {exc}", True

        if response.status_code == 200:
            return json.dumps(response.json(), ensure_ascii=False), False
        if response.status_code == 401:
            return "Error: authentication with the task service failed", True
        logger.warning("%s failed HTTP %s: %s", name, response.status_code, response.text[:500])
        return f"Error: {_error_detail(response)}", True
