"""
Cinna artifacts — the `cinna` MCP server's ``artifacts_publish`` tool, engine-agnostic.

Served on the same `cinna` server as the task tools, by both bindings:

* Claude Code — the in-process SDK server built by ``cinna_tasks_claude.py``
  (building mode: artifacts only; conversation mode: tasks and artifacts);
* OpenCode — the stdio bridge ``mcp_bridge/cinna_server.py`` (both modes).

The tool's name, description and input schema are read from
``artifacts.schema.json`` (authored in cinna-core's
``docs/local_agent_kit/protocols/``, installed at
``/app/core/protocols/artifacts.schema.json``), with the loader of
``cinna_tasks_protocol``. A call posts to the backend's
``/api/v1/agent/artifacts/publish``, which pulls the path out of this
workspace and publishes it, private to the owner.

No engine SDK imports, and the sibling import works both as a package module
(Claude binding) and as a plain module next to the stdio bridge.
"""
from __future__ import annotations

import json
import logging
import os
import posixpath
import uuid
from pathlib import Path
from typing import Any

import httpx

try:  # package import (core.server.tools.*)
    from .cinna_tasks_protocol import WORKSPACE_DIR, _error_detail, load_tool_specs
except ImportError:  # plain module beside the stdio bridge
    from cinna_tasks_protocol import WORKSPACE_DIR, _error_detail, load_tool_specs  # type: ignore[no-redef]

logger = logging.getLogger(__name__)

# Where the files sit inside an environment's core directory.
SCHEMA_RELATIVE_PATH = Path("protocols") / "artifacts.schema.json"
SKILL_RELATIVE_PATH = Path("skills") / "cinna-artifacts"

_REPO_KIT_SNAPSHOT = (
    Path("platform-knowledge-env") / "app" / "workspace" / "knowledge" / "local-kit"
)
_HERE = Path(__file__).resolve()

PUBLISH_TOOL = "artifacts_publish"
_PUBLISH_PATH = "/api/v1/agent/artifacts/publish"
_WORKSPACE_PREFIX = "/app/workspace/"


def artifacts_schema_path() -> Path:
    """The ``artifacts.schema.json`` this environment serves."""
    override = os.getenv("CINNA_ARTIFACTS_SCHEMA_PATH")
    if override:
        return Path(override)
    core_dir = _HERE.parents[2]  # tools -> server -> core
    installed = core_dir / SCHEMA_RELATIVE_PATH
    if installed.is_file():
        return installed
    templates_dir = _HERE.parents[4]  # core -> app_core_base -> env-templates
    return templates_dir / _REPO_KIT_SNAPSHOT / SCHEMA_RELATIVE_PATH


def load_artifact_specs(path: Path | None = None) -> dict[str, dict[str, Any]]:
    """``{tool name: {"description", "inputSchema"}}`` from ``artifacts.schema.json``."""
    return load_tool_specs(path or artifacts_schema_path())


class CinnaArtifactsClient:
    """Forwards ``artifacts_publish`` to the backend. Returns ``(text, is_error)``.

    Synchronous on purpose, like ``CinnaTasksClient``. The backend copies the
    files out of the workspace, so a large folder takes a while: 120 s timeout.
    """

    def __init__(
        self,
        backend_url: str | None = None,
        auth_token: str | None = None,
        env_id: str | None = None,
        workspace_dir: Path = WORKSPACE_DIR,
        timeout: float = 120.0,
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

    def _workspace_relative(self, raw: str) -> str:
        path = raw.strip()
        if path.startswith(_WORKSPACE_PREFIX):
            path = path[len(_WORKSPACE_PREFIX):]
        while path.startswith("./"):
            path = path[2:]
        return path.rstrip("/")

    def call(self, name: str, args: dict[str, Any], session_id: str | None) -> tuple[str, bool]:
        if name != PUBLISH_TOOL:
            return f"Error: unknown tool {name}", True
        if not self.backend_url or not self.auth_token:
            return "Error: the artifact service is not configured in this environment", True

        payload = {key: value for key, value in (args or {}).items() if value is not None}
        path = self._workspace_relative(str(payload.get("path") or ""))
        if not path or path.startswith("/"):
            return "Error: `path` must be a file or folder inside the workspace", True
        if not (self.workspace_dir / path).exists():
            return f"Error: {path} was not found in the workspace. Nothing was published.", True
        payload["path"] = path
        payload = _drop_placeholder_args(payload, self.workspace_dir / path, path)
        if session_id:
            payload["source_session_id"] = session_id

        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.post(
                    f"{self.backend_url}{_PUBLISH_PATH}", json=payload, headers=self._headers(),
                )
        except httpx.TimeoutException:
            return (
                "Error: the publish timed out and may or may not have completed. Publishing the "
                "same path again is safe: it adds a revision to the same artifact.",
                True,
            )
        except httpx.RequestError as exc:
            return f"Error: could not reach the artifact service: {exc}", True

        if response.status_code == 200:
            return json.dumps(response.json(), ensure_ascii=False), False
        if response.status_code == 401:
            return "Error: authentication with the artifact service failed", True
        logger.warning("%s failed HTTP %s: %s", name, response.status_code, response.text[:500])
        if response.status_code == 404 and "artifact_id" in payload:
            return (
                f"Error: {_error_detail(response)}. `artifact_id` must be an id an earlier "
                "artifacts_publish returned; omit it to publish `path` (publishing the same "
                "path again updates the artifact it was published to).",
                True,
            )
        return f"Error: {_error_detail(response)}", True


def _drop_placeholder_args(payload: dict[str, Any], root: Path, path: str) -> dict[str, Any]:
    """Drop optional arguments no publish of ``root`` could ever accept.

    An engine that sends tools without ``"strict": false`` (OpenCode 1.14 does)
    gets OpenAI's strict default, which makes every property required: the
    model then fills ``artifact_id`` and ``entry`` with placeholders (``" "``,
    ``"/"``, the nil UUID, the published file itself) and loops on the
    rejections. An ``artifact_id`` that is not a UUID names no artifact, and an
    ``entry`` that is not an HTML file inside the published folder names no
    start page, so both mean "omitted". An entry written from the workspace
    root (``artifacts/site/index.html``) is made relative to the folder.
    """
    cleaned = dict(payload)
    artifact_id = cleaned.get("artifact_id")
    if artifact_id is not None:
        try:
            parsed = uuid.UUID(str(artifact_id).strip())
        except ValueError:
            parsed = None
        if parsed is None or parsed.int == 0:
            cleaned.pop("artifact_id")
        else:
            cleaned["artifact_id"] = str(parsed)
    entry = cleaned.get("entry")
    if entry is not None:
        entry = str(entry).strip()
        while entry.startswith("./"):
            entry = entry[2:]
        if entry.startswith(f"{path}/"):
            entry = entry[len(path) + 1:]
        is_page = posixpath.splitext(entry)[1].lower() in (".html", ".htm")
        if entry and is_page and root.is_dir() and (root / entry).is_file():
            cleaned["entry"] = entry
        else:
            cleaned.pop("entry")
    return cleaned
