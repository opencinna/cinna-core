"""Dashboard components inside the agent environment (kit protocol ``dashboards/1``).

The backend reads an agent's components, their page files and their data
through these routes, then serves the pages from its own content origin.
Everything travels in POST bodies so env-core's access log only ever sees the
four fixed paths below — a component name, a file path or params never land in
a log line. This module never logs params, stdout, file paths or file bytes.

Routes (mounted by ``routes.py`` behind ``verify_auth_token``):

* ``POST /dashboards/components`` ``{}`` → every component of the agent and of
  its resolved skills, with ``check_component.py``'s static findings.
* ``POST /dashboards/file`` ``{component, skill?, path}`` → one file of the
  component folder (raw bytes + content-type).
* ``POST /dashboards/run`` ``{component, skill?, endpoint, params, timeout?}``
  → ``{ok: true, value}`` | ``{ok: false, code, message, stderr_tail?}``.
* ``POST /dashboards/source-file`` ``{component, skill?}`` → the bytes of the
  component's ``source.file`` (``app-data/`` or ``files/`` of the agent).

Errors on the byte routes (``file``, ``source-file``) carry the same body as a
failed run, ``{ok: false, code, message}``, with status 400 (``invalid_input``)
or 404 (``not_found``). ``run`` always answers 200; ``ok`` tells the outcome.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import mimetypes
import os
import re
import sys
from pathlib import Path, PurePosixPath
from types import ModuleType
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from .endpoint_runner import (
    DEFAULT_TIMEOUT_S,
    MAX_STDOUT_BYTES,
    RUNTIMES,
    UnsupportedScriptError,
    clamp_timeout,
    run_script,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/dashboards", tags=["dashboards"])

#: The agent root. Read at call time (``_workspace()``) so tests can point it
#: at a temporary folder.
WORKSPACE_DIR = Path(os.getenv("CLAUDE_CODE_WORKSPACE", "/app/workspace"))

#: The platform skill's checker, installed by ``platform_protocol_files.py``.
CHECK_COMPONENT_PATH = Path(
    os.getenv(
        "CINNA_CHECK_COMPONENT_PATH",
        "/app/core/skills/cinna-dashboards/scripts/check_component.py",
    )
)

DASHBOARDS_DIR = "dashboards"
COMPONENT_FILE = "component.json"
COMPONENT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
SOURCE_FILE_ROOTS = ("app-data", "files")
_SKIP_DIRS = frozenset({".git", ".venv", "__pycache__", "node_modules", ".mypy_cache", ".ruff_cache"})

_SOURCE_CONTENT_TYPES = {
    ".md": "text/markdown; charset=utf-8",
    ".json": "application/json",
    ".csv": "text/csv; charset=utf-8",
}
_NO_STORE = {"Cache-Control": "no-store"}


# --------------------------------------------------------------------------- #
# Request bodies
# --------------------------------------------------------------------------- #


class ComponentsRequest(BaseModel):
    pass


class ComponentRef(BaseModel):
    component: str
    skill: str | None = None


class FileRequest(ComponentRef):
    path: str


class RunRequest(ComponentRef):
    endpoint: str
    params: dict[str, Any] = Field(default_factory=dict)
    timeout: int | None = None


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class DashboardError(Exception):
    """A refusal with a stable ``code`` (``not_found`` | ``invalid_input``)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    @property
    def status_code(self) -> int:
        return 404 if self.code == "not_found" else 400

    def body(self) -> dict[str, Any]:
        return {"ok": False, "code": self.code, "message": self.message}


def _not_found(message: str) -> DashboardError:
    return DashboardError("not_found", message)


def _invalid(message: str) -> DashboardError:
    return DashboardError("invalid_input", message)


def _error_response(error: DashboardError) -> JSONResponse:
    return JSONResponse(error.body(), status_code=error.status_code, headers=_NO_STORE)


# --------------------------------------------------------------------------- #
# Resolution
# --------------------------------------------------------------------------- #


def _workspace() -> Path:
    return Path(WORKSPACE_DIR)


def _inside(path: Path, root: Path) -> bool:
    """True when ``path``, every symlink resolved, stays within ``root``."""
    try:
        path.resolve().relative_to(root.resolve())
    except (ValueError, OSError, RuntimeError):
        return False
    return True


def resolve_skill_dirs(workspace: Path) -> list[tuple[str, Path]]:
    """``(name, folder)`` of every valid skill, in the ``/config/skills`` order.

    The same resolution the skill index uses: the agent's own ``skills/`` and
    each active plugin's ``skills/``. On a shadowed name the first row (the
    agent's own copy) wins, the way a request naming ``skill`` is resolved.
    """
    from .agent_env_service import AgentEnvService

    try:
        index = AgentEnvService(str(workspace)).build_skills_index()
    except Exception as exc:  # noqa: BLE001 — a broken index lists no skills
        logger.warning("dashboards: could not build the skill index (%s)", type(exc).__name__)
        return []
    seen: set[str] = set()
    resolved: list[tuple[str, Path]] = []
    for entry in index.get("skills", []):
        name = entry.get("name") or ""
        raw_path = entry.get("path") or ""
        if not name or not raw_path or entry.get("error") or name in seen:
            continue
        folder = Path(raw_path)
        if not folder.is_absolute():
            folder = workspace / folder
        if folder.is_dir():
            seen.add(name)
            resolved.append((name, folder))
    return resolved


def _owner_dir(workspace: Path, skill: str | None) -> Path:
    """The folder whose ``dashboards/`` holds the component: agent root or skill."""
    if skill is None:
        return workspace
    for name, folder in resolve_skill_dirs(workspace):
        if name == skill:
            return folder
    raise _not_found("no such skill")


def _component_dir(workspace: Path, component: str, skill: str | None) -> Path:
    if not COMPONENT_NAME_RE.fullmatch(component):
        raise _invalid("invalid component name")
    owner = _owner_dir(workspace, skill)
    dashboards = owner / DASHBOARDS_DIR
    candidate = dashboards / component
    if (
        dashboards.is_symlink()
        or candidate.is_symlink()
        or not candidate.is_dir()
        or not _inside(candidate, dashboards)
    ):
        raise _not_found("no such component")
    if not (candidate / COMPONENT_FILE).is_file():
        raise _not_found("no such component")
    return candidate


def _read_component_json(component_dir: Path) -> dict[str, Any]:
    try:
        doc = json.loads((component_dir / COMPONENT_FILE).read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, ValueError):
        raise _invalid("component.json is not valid JSON")
    if not isinstance(doc, dict):
        raise _invalid("component.json must hold one JSON object")
    return doc


def safe_relative(value: str, *, refuse_api: bool) -> PurePosixPath:
    """A component-relative POSIX path, or ``invalid_input``.

    Refused: empty, a leading ``/``, a backslash, a drive letter, an empty
    segment, any segment starting with ``.`` (covers ``.``, ``..`` and hidden
    files) and, when ``refuse_api``, a first segment ``api`` — endpoint scripts
    are run, never served.
    """
    if not isinstance(value, str) or not value or value.startswith("/") or "\\" in value:
        raise _invalid("invalid path")
    if re.match(r"^[A-Za-z]:", value):
        raise _invalid("invalid path")
    segments = value.split("/")
    if any(not segment or segment.startswith(".") for segment in segments):
        raise _invalid("invalid path")
    if refuse_api and segments[0] == "api":
        raise _invalid("invalid path")
    return PurePosixPath(value)


def _contained_file(root: Path, relative: PurePosixPath, *, refuse_api: bool = False) -> Path:
    """``root/relative`` when its realpath stays inside ``root`` and is a file.

    With ``refuse_api`` the resolved path is checked again, so a symlink inside
    the component (``www -> api``) cannot serve an endpoint script either.
    """
    target = root / relative
    if not _inside(target, root):
        raise _invalid("invalid path")
    if refuse_api:
        resolved = target.resolve().relative_to(root.resolve())
        if resolved.parts and resolved.parts[0] == "api":
            raise _invalid("invalid path")
    if not target.is_file():
        raise _not_found("no such file")
    return target


# --------------------------------------------------------------------------- #
# Findings (check_component.py, static checks only)
# --------------------------------------------------------------------------- #

_checker_cache: dict[str, Any] = {"key": None, "module": None}


def _load_checker() -> ModuleType | None:
    """Import ``check_component.py`` from the platform skill, cached by mtime."""
    path = Path(CHECK_COMPONENT_PATH)
    try:
        key = (str(path), path.stat().st_mtime_ns)
    except OSError:
        return None
    if _checker_cache["key"] == key:
        return _checker_cache["module"]
    try:
        spec = importlib.util.spec_from_file_location("cinna_check_component", path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        # Its dataclasses resolve their module through sys.modules while the
        # file executes, so it has to be registered before exec_module.
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001 — no checker means no findings, not a 500
        sys.modules.pop("cinna_check_component", None)
        logger.warning("dashboards: could not load check_component (%s)", type(exc).__name__)
        return None
    _checker_cache["key"] = key
    _checker_cache["module"] = module
    return module


def _findings(checker: ModuleType | None, component_dir: Path, workspace: Path) -> list[dict]:
    if checker is None:
        return []
    try:
        problems = checker.check_component(component_dir, agent_root=workspace, run=False)
    except Exception as exc:  # noqa: BLE001
        logger.warning("dashboards: check_component raised (%s)", type(exc).__name__)
        return []
    return [
        {
            "path": problem.path,
            "code": problem.code,
            "field": problem.field,
            "severity": problem.severity,
            "message": problem.message,
        }
        for problem in problems
    ]


# --------------------------------------------------------------------------- #
# Listing
# --------------------------------------------------------------------------- #


def _folder_size(folder: Path) -> int:
    total = 0
    for current, dirs, files in os.walk(folder, followlinks=False):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for name in files:
            try:
                path = Path(current) / name
                if not path.is_symlink():
                    total += path.stat().st_size
            except OSError:
                continue
    return total


def _component_folders(owner: Path) -> list[Path]:
    """``<owner>/dashboards/*/`` holding a ``component.json``; symlinks never followed."""
    dashboards = owner / DASHBOARDS_DIR
    if dashboards.is_symlink() or not dashboards.is_dir():
        return []
    return [
        child
        for child in sorted(dashboards.iterdir(), key=lambda p: p.name)
        if child.is_dir()
        and not child.is_symlink()
        and not child.name.startswith(".")
        and child.name not in _SKIP_DIRS
        and (child / COMPONENT_FILE).is_file()
    ]


def _describe(component_dir: Path, skill: str | None, checker: ModuleType | None,
              workspace: Path) -> dict[str, Any]:
    try:
        doc = json.loads((component_dir / COMPONENT_FILE).read_text(encoding="utf-8-sig"))
        component_json = doc if isinstance(doc, dict) else None
    except (OSError, UnicodeDecodeError, ValueError):
        component_json = None
    return {
        "name": component_dir.name,
        "skill": skill,
        "component_json": component_json,
        "findings": _findings(checker, component_dir, workspace),
        "size_bytes": _folder_size(component_dir),
    }


def list_components(workspace: Path) -> list[dict[str, Any]]:
    checker = _load_checker()
    owners: list[tuple[str | None, Path]] = [(None, workspace)]
    owners.extend((name, folder) for name, folder in resolve_skill_dirs(workspace))
    return [
        _describe(component_dir, skill, checker, workspace)
        for skill, owner in owners
        for component_dir in _component_folders(owner)
    ]


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #


@router.post("/components")
async def list_dashboard_components(_: ComponentsRequest | None = None) -> JSONResponse:
    """Every component of the agent and of its resolved skills, with findings."""
    components = await asyncio.to_thread(list_components, _workspace())
    return JSONResponse({"components": components}, headers=_NO_STORE)


@router.post("/file")
async def get_dashboard_file(body: FileRequest) -> Response:
    """One file of a component folder, by component-relative path."""
    workspace = _workspace()
    try:
        relative = safe_relative(body.path, refuse_api=True)
        component_dir = await asyncio.to_thread(_component_dir, workspace, body.component, body.skill)
        target = _contained_file(component_dir, relative, refuse_api=True)
        content = await asyncio.to_thread(target.read_bytes)
    except DashboardError as error:
        return _error_response(error)
    except OSError:
        return _error_response(_not_found("no such file"))
    content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    return Response(content=content, media_type=content_type, headers=_NO_STORE)


def _endpoint_script(component_dir: Path, doc: dict[str, Any], endpoint: str) -> tuple[Path, dict]:
    endpoints = doc.get("endpoints")
    if not isinstance(endpoints, dict) or endpoint not in endpoints:
        raise _not_found("no such endpoint")
    spec = endpoints[endpoint]
    script = spec.get("script") if isinstance(spec, dict) else None
    if not isinstance(script, str):
        raise _invalid("the endpoint names no script")
    relative = safe_relative(script, refuse_api=False)
    if relative.suffix not in RUNTIMES:
        raise _invalid("an endpoint script is .py, .js, .mjs or .cjs")
    return _contained_file(component_dir, relative), spec


@router.post("/run")
async def run_dashboard_endpoint(body: RunRequest) -> JSONResponse:
    """Run one declared endpoint of a component with ``params`` on stdin."""
    workspace = _workspace()
    try:
        component_dir = await asyncio.to_thread(_component_dir, workspace, body.component, body.skill)
        doc = _read_component_json(component_dir)
        script, spec = _endpoint_script(component_dir, doc, body.endpoint)
        timeout = clamp_timeout(
            body.timeout if body.timeout is not None else spec.get("timeout_s"),
            DEFAULT_TIMEOUT_S,
        )
        result = await run_script(script, body.params, timeout_s=timeout, cwd=workspace)
    except DashboardError as error:
        return JSONResponse(error.body(), headers=_NO_STORE)
    except UnsupportedScriptError:
        return JSONResponse(_invalid("an endpoint script is .py, .js, .mjs or .cjs").body(),
                            headers=_NO_STORE)

    if result.reason == "ok":
        value = json.loads(result.value_bytes.decode("utf-8"))
        return JSONResponse({"ok": True, "value": value}, headers=_NO_STORE)

    if result.start_error is not None:
        message = f"could not start the script ({result.start_error})"
    elif result.reason == "timeout":
        message = f"did not finish within {timeout}s"
    elif result.reason == "too_large":
        message = f"printed more than {MAX_STDOUT_BYTES} bytes"
    elif result.reason == "bad_json":
        message = "stdout is not one JSON value"
    else:
        message = f"exited with code {result.exit_code}"
    payload: dict[str, Any] = {"ok": False, "code": "call_failed", "message": message}
    if result.stderr_tail:
        payload["stderr_tail"] = result.stderr_tail
    return JSONResponse(payload, headers=_NO_STORE)


def _source_file(workspace: Path, doc: dict[str, Any]) -> Path:
    source = doc.get("source")
    value = source.get("file") if isinstance(source, dict) else None
    if not isinstance(value, str):
        raise _invalid("the component has no source.file")
    relative = safe_relative(value, refuse_api=False)
    if relative.parts[0] not in SOURCE_FILE_ROOTS or len(relative.parts) < 2:
        raise _invalid("source.file must be under app-data/ or files/")
    # Contained to the named root, not the whole workspace: a symlink under
    # files/ must not reach the rest of the agent folder.
    root = workspace / relative.parts[0]
    if root.is_symlink():
        raise _invalid("invalid path")
    return _contained_file(root, PurePosixPath(*relative.parts[1:]))


@router.post("/source-file")
async def get_dashboard_source_file(body: ComponentRef) -> Response:
    """The bytes of the component's ``source.file`` (agent-relative)."""
    workspace = _workspace()
    try:
        component_dir = await asyncio.to_thread(_component_dir, workspace, body.component, body.skill)
        doc = _read_component_json(component_dir)
        target = _source_file(workspace, doc)
        content = await asyncio.to_thread(target.read_bytes)
    except DashboardError as error:
        return _error_response(error)
    except OSError:
        return _error_response(_not_found("no such file"))
    content_type = (
        _SOURCE_CONTENT_TYPES.get(target.suffix)
        or mimetypes.guess_type(target.name)[0]
        or "application/octet-stream"
    )
    return Response(content=content, media_type=content_type, headers=_NO_STORE)
