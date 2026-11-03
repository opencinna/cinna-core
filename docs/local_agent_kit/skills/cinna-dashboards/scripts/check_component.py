#!/usr/bin/env python3
"""Check a Cinna dashboard component (`cinna_protocol: dashboards/1`) before you offer it.

Validates `component.json`, the scripts it names and the web view's entry page, by the
rules in the kit's `protocols/dashboards.schema.json`. With `--run` it also runs the
source with the default params, the way a host does, and checks the result's shape
against the view. Standard library only, Python 3.10+.

Usage:
    uv run python <this skill>/scripts/check_component.py dashboards/<name> [--run]
    uv run python <this skill>/scripts/check_component.py skills/<skill>/dashboards/<name>
    uv run python <this skill>/scripts/check_component.py .          # every component of the agent

Run it from the agent folder. Each problem is printed as one `path: message` line
(warnings as `path: warning: message`), and the exit status is 1 when there is at least
one error. `--json` prints `{ok, problems: [{path, code, field, severity, message}]}`.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path, PurePosixPath

PROTOCOL = "dashboards/1"
SCHEMA_VERSION = 1
COMPONENT_FILE = "component.json"
DASHBOARDS_DIR = "dashboards"
MANIFEST_NAME = "cinna-agent.json"
SKILL_MD_NAME = "SKILL.md"
COMMAND_CATALOG = "docs/CLI_COMMANDS.yaml"

COMPONENT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
ENDPOINT_NAME_RE = re.compile(r"^[a-zA-Z0-9_]+$")
PARAM_NAME_RE = re.compile(r"^[a-zA-Z0-9_]+$")
DURATION_RE = re.compile(r"^([1-9][0-9]*)(s|m|h|d)$")
RUN_COMMAND_RE = re.compile(r"^/run:([A-Za-z0-9][A-Za-z0-9_-]*)$")
MONTH_RE = re.compile(r"^[0-9]{4}-(0[1-9]|1[0-2])$")
DATE_RE = re.compile(r"^[0-9]{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])$")

VIEWS = ("markdown", "table", "list", "metrics", "html")
SIZES = ("s", "m", "l", "wide", "tall")
PARAM_TYPES = ("enum", "month", "date", "date_range", "text", "number", "boolean")
# The runtime comes from the extension; component.json has no `runtime` field.
ENDPOINT_RUNTIMES = {".py": "python", ".js": "node", ".mjs": "node", ".cjs": "node"}
SOURCE_FILE_ROOTS = ("app-data/", "files/")
SOURCE_FILE_EXTENSIONS = (".md", ".json", ".csv")
# Extensions are matched as written: lowercase only, like the schema's patterns.
ENTRY_EXTENSIONS = (".html",)
DEFAULT_ENTRY = "index.html"
# An executable that runs a script the way `node` does, passed by a host; read before PATH.
# The check only ever runs `[CINNA_NODE, script]`: never `--version`, a flag or any probe.
NODE_ENV_VAR = "CINNA_NODE"

DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
MIN_WHILE_OPEN_SECONDS = 60
DEFAULT_TIMEOUT_S = 60
MAX_TIMEOUT_S = 300
MAX_STDOUT_BYTES = 5 * 1024 * 1024
STDERR_TAIL_CHARS = 2000
_READ_CHUNK = 64 * 1024

TITLE_MAX = 120
DESCRIPTION_MAX = 1000
CONTEXT_MAX = 4000
PROMPTS_MAX = 10
PROMPT_LABEL_MAX = 60
PROMPT_TEXT_MAX = 1000

TABLE_COLUMN_TYPES = ("text", "number", "money", "percent", "date", "badge")
LIST_PRIORITIES = ("high", "medium", "low")
METRIC_DELTA_GOOD = ("up", "down")
METRIC_STATUSES = ("ok", "warning", "error")
LINK_SCHEMES = ("https://", "http://", "mailto:")

SKIP_DIRS = {".git", ".venv", "__pycache__", "node_modules", ".mypy_cache", ".ruff_cache"}

# A web view gets no network (README D9): these references would be blocked.
_HTML_NETWORK_RE = re.compile(r"""\b(?:src|href)\s*=\s*["'](?:https?:)?//""", re.IGNORECASE)
_CSS_NETWORK_RE = re.compile(r"""(?:@import\s+|url\(\s*)["']?(?:https?:)?//""", re.IGNORECASE)
_JS_NETWORK_RE = re.compile(
    r"""(?:\bfrom\s+|\bimport\s*\(\s*|\bfetch\s*\(\s*|\bnew\s+WebSocket\s*\(\s*)["'`](?:https?:|wss?:)?//"""
)

# The page files a web view's content scans read (api/ is never served, so it is skipped).
PAGE_SCAN_EXTENSIONS = (".html", ".htm", ".css", ".js", ".mjs")
_HTML_EXTENSIONS = (".html", ".htm")

# A single leading `/` resolves outside the tile's per-tile path prefix, so it is blocked.
# `(?![/*])` leaves a protocol-relative `//host` to network_reference; a root-relative api/
# URL is fetch_endpoint's alone. Each is (field, pattern).
_ROOT = r"""/(?![/*])"""
_QUOTED_ROOT = r"""["'`]/(?![/*])"""
_HTML_ATTR_ROOT_RES = (  # an attribute value, quoted or not: .html/.htm only
    ("src", re.compile(r"""(?<![\w$])src\s*=\s*["']?""" + _ROOT, re.IGNORECASE)),
    ("href", re.compile(r"""(?<![\w$])href\s*=\s*["']?""" + _ROOT, re.IGNORECASE)),
)
_SCRIPT_ATTR_ROOT_RES = (  # a script assigning a quoted string: `img.src = "/x.png"`
    ("src", re.compile(r"""(?<![\w$])src\s*=\s*""" + _QUOTED_ROOT)),
    ("href", re.compile(r"""(?<![\w$])href\s*=\s*""" + _QUOTED_ROOT)),
)
_SHARED_ROOT_RES = (  # CSS url( and imports, in every page file
    ("url", re.compile(r"""(?<![\w$.-])url\(\s*["']?""" + _ROOT)),
    ("import", re.compile(r"""(?:\bfrom\s+|\bimport\s*\(\s*|\bimport\s+|@import\s+)""" + _QUOTED_ROOT)),
)
_REQUEST_ROOT_RES = (  # a request by root-relative URL, outside api/ (that is fetch_endpoint)
    ("fetch", re.compile(r"""\bfetch\s*\(\s*""" + _QUOTED_ROOT + r"""(?!api/)""")),
    ("XMLHttpRequest", re.compile(
        r"""\.open\s*\(\s*["'`][A-Za-z]+["'`]\s*,\s*""" + _QUOTED_ROOT + r"""(?!api/)""")),
)
_ROOT_RELATIVE_RES = {
    ".html": _HTML_ATTR_ROOT_RES + _SHARED_ROOT_RES + _REQUEST_ROOT_RES,
    ".htm": _HTML_ATTR_ROOT_RES + _SHARED_ROOT_RES + _REQUEST_ROOT_RES,
    ".js": _SCRIPT_ATTR_ROOT_RES + _SHARED_ROOT_RES + _REQUEST_ROOT_RES,
    ".mjs": _SCRIPT_ATTR_ROOT_RES + _SHARED_ROOT_RES + _REQUEST_ROOT_RES,
    ".css": _SHARED_ROOT_RES,
}
# `<base` changes how every relative path resolves, and base-uri 'none' blocks it.
_BASE_TAG_RE = re.compile(r"<base\b", re.IGNORECASE)
# Endpoint data never comes through fetch: api/ is not served. (field, pattern)
_FETCH_ENDPOINT_RES = (
    ("fetch", re.compile(r"""\bfetch\s*\(\s*["'`](?:\.?/)?api/""")),
    ("XMLHttpRequest", re.compile(
        r"""\.open\s*\(\s*["'`][A-Za-z]+["'`]\s*,\s*["'`](?:\.?/)?api/""")),
)
# What the sandbox refuses (CSP and an opaque origin): it throws or does nothing. (field, pattern)
_SANDBOX_UNSUPPORTED_RES = (
    ("Worker", re.compile(r"\bnew\s+Worker\s*\(")),
    ("iframe", re.compile(r"<iframe\b", re.IGNORECASE)),
    ("eval", re.compile(r"(?<![\w$])eval\s*\(")),
    ("Function", re.compile(r"\bnew\s+Function\s*\(")),
    ("WebAssembly", re.compile(r"(?<![\w$])WebAssembly\.")),
    ("localStorage", re.compile(r"(?<![\w$])localStorage(?![\w$])")),
    ("sessionStorage", re.compile(r"(?<![\w$])sessionStorage(?![\w$])")),
    ("indexedDB", re.compile(r"(?<![\w$])indexedDB(?![\w$])")),
    ("document.cookie", re.compile(r"(?<![\w$])document\.cookie(?![\w$])")),
    ("window.open", re.compile(r"(?<![\w$])window\.open\s*\(")),
)
_SANDBOX_REASONS = {
    "Worker": "Web Workers are blocked (worker-src 'none')",
    "iframe": "frames are blocked (frame-src 'none')",
    "eval": "there is no 'unsafe-eval', so eval throws",
    "Function": "there is no 'unsafe-eval', so new Function throws",
    "WebAssembly": "there is no 'wasm-unsafe-eval', so WebAssembly does not compile",
    "localStorage": "the origin is opaque, so localStorage throws SecurityError",
    "sessionStorage": "the origin is opaque, so sessionStorage throws SecurityError",
    "indexedDB": "the origin is opaque, so IndexedDB throws SecurityError",
    "document.cookie": "the origin is opaque, so cookies throw SecurityError",
    "window.open": "popups and top navigation are blocked; use cinna.openLink(url)",
}


@dataclass
class Problem:
    """One finding. `code` is stable across hosts; `message` is for people."""

    path: str
    code: str
    message: str
    field: str | None = None
    severity: str = "error"

    def line(self) -> str:
        prefix = "warning: " if self.severity == "warning" else ""
        return f"{self.path}: {prefix}{self.message}"


class _Findings:
    def __init__(self) -> None:
        self.problems: list[Problem] = []

    def error(self, path: str, code: str, message: str, field: str | None = None) -> None:
        self.problems.append(Problem(path, code, message, field, "error"))

    def warn(self, path: str, code: str, message: str, field: str | None = None) -> None:
        self.problems.append(Problem(path, code, message, field, "warning"))

    @property
    def has_errors(self) -> bool:
        return any(p.severity == "error" for p in self.problems)


# --------------------------------------------------------------------------- #
# Where a component sits
# --------------------------------------------------------------------------- #


def find_agent_root(start: Path) -> Path | None:
    """The nearest folder at or above `start` holding `cinna-agent.json`."""
    for candidate in (start, *start.parents):
        if (candidate / MANIFEST_NAME).is_file():
            return candidate
    return None


def component_owner(component_dir: Path) -> tuple[str, str | None]:
    """(`agent` | `skill` | `unknown`, skill name) for a component folder."""
    if component_dir.parent.name != DASHBOARDS_DIR:
        return "unknown", None
    owner = component_dir.parent.parent
    if owner.parent.name == "skills" or (owner / SKILL_MD_NAME).is_file():
        return "skill", owner.name
    return "agent", None


def find_components(agent_root: Path) -> list[Path]:
    """Every component folder of an agent: `dashboards/*/` and `skills/*/dashboards/*/`.
    A symlinked component folder is never followed (`symlinked_components` lists them)."""
    roots = [agent_root / DASHBOARDS_DIR]
    skills = agent_root / "skills"
    if skills.is_dir():
        roots.extend(
            child / DASHBOARDS_DIR
            for child in sorted(skills.iterdir(), key=lambda p: p.name)
            if child.is_dir() and not child.name.startswith(".")
        )
    found: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        found.extend(
            child
            for child in sorted(root.iterdir(), key=lambda p: p.name)
            if child.is_dir() and not child.is_symlink()
            and not child.name.startswith(".") and child.name not in SKIP_DIRS
        )
    return found


def symlinked_components(agent_root: Path) -> list[Path]:
    """Symlinks where a component folder would be; a host refuses them like the check does."""
    roots = [agent_root / DASHBOARDS_DIR]
    skills = agent_root / "skills"
    if skills.is_dir():
        roots.extend(child / DASHBOARDS_DIR for child in sorted(skills.iterdir(), key=lambda p: p.name)
                     if child.is_dir() and not child.name.startswith("."))
    return [child for root in roots if root.is_dir()
            for child in sorted(root.iterdir(), key=lambda p: p.name)
            if child.is_symlink() and child.is_dir() and not child.name.startswith(".")]


class _Paths:
    """Turns absolute paths into the agent-relative POSIX paths findings carry."""

    def __init__(self, agent_root: Path | None) -> None:
        self.base = agent_root or Path.cwd()

    def show(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.base.resolve()).as_posix()
        except ValueError:
            return path.as_posix()


# --------------------------------------------------------------------------- #
# Small readers
# --------------------------------------------------------------------------- #


def parse_duration(value: object) -> int | None:
    """Seconds for `30s`, `15m`, `6h`, `1d`; None when it does not parse."""
    if not isinstance(value, str):
        return None
    match = DURATION_RE.fullmatch(value)
    if match is None:
        return None
    return int(match.group(1)) * DURATION_UNITS[match.group(2)]


def _read_manifest(agent_root: Path | None) -> dict | None:
    if agent_root is None:
        return None
    try:
        document = json.loads((agent_root / MANIFEST_NAME).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return document if isinstance(document, dict) else None


def _names(manifest: dict | None, key: str) -> set[str] | None:
    if manifest is None:
        return None
    entries = manifest.get(key)
    if not isinstance(entries, list):
        return set()
    return {e["name"] for e in entries if isinstance(e, dict) and isinstance(e.get("name"), str)}


def _command_names(agent_root: Path | None) -> set[str] | None:
    """`- name:` entries of docs/CLI_COMMANDS.yaml, by line scan; None if unreadable."""
    if agent_root is None:
        return None
    try:
        text = (agent_root / COMMAND_CATALOG).read_text(encoding="utf-8-sig")
    except OSError:
        return None
    names = set()
    for line in text.splitlines():
        match = re.match(r"^\s*-\s+name:\s*['\"]?([A-Za-z0-9][A-Za-z0-9_-]*)['\"]?\s*$", line)
        if match:
            names.add(match.group(1))
    return names


def _safe_relative(value: str) -> PurePosixPath | None:
    """A relative POSIX path with no `..`, no backslash and no drive; None otherwise."""
    if not value or "\\" in value or value.startswith("/") or re.match(r"^[A-Za-z]:", value):
        return None
    path = PurePosixPath(value)
    if any(part == ".." for part in path.parts):
        return None
    return path


def _inside(path: Path, root: Path) -> bool:
    """True when `path`, with every symlink resolved, stays within `root`."""
    try:
        path.resolve().relative_to(root.resolve())
    except (ValueError, OSError, RuntimeError):
        return False
    return True


def _is_str(value: object, max_length: int, min_length: int = 1) -> bool:
    return isinstance(value, str) and min_length <= len(value.strip()) and len(value) <= max_length


# --------------------------------------------------------------------------- #
# Static checks
# --------------------------------------------------------------------------- #


def _check_endpoints(component_dir: Path, raw: object, at: str, out: _Findings) -> dict[str, Path]:
    """Validated `endpoints`; returns the name → script of each usable one."""
    usable: dict[str, Path] = {}
    if raw is None:
        return usable
    if not isinstance(raw, dict):
        out.error(at, "invalid_type", "`endpoints` must be an object of name → {script}.", "endpoints")
        return usable
    for name, spec in raw.items():
        field = f"endpoints.{name}"
        if not ENDPOINT_NAME_RE.fullmatch(name):
            out.error(at, "endpoint_name_invalid",
                      f"endpoint `{field}`: names use letters, digits and `_` only.", field)
            continue
        if not isinstance(spec, dict):
            out.error(at, "invalid_type", f"`{field}` must be an object with a `script`.", field)
            continue
        script = spec.get("script")
        if not isinstance(script, str) or not script:
            out.error(at, "required", f"`{field}.script` is required.", f"{field}.script")
            continue
        relative = _safe_relative(script)
        if relative is None:
            out.error(at, "script_outside_component",
                      f"`{field}.script` must be a path inside the component folder, like api/{name}.py.",
                      f"{field}.script")
            continue
        if relative.suffix not in ENDPOINT_RUNTIMES:
            out.error(at, "script_extension",
                      f"`{field}.script` {script}: an endpoint is .py, .js, .mjs or .cjs (lowercase).",
                      f"{field}.script")
            continue
        target = component_dir / relative
        if not _inside(target, component_dir):
            out.error(at, "symlink_escape",
                      f"`{field}.script` {script} leads outside the component folder through a symlink.",
                      f"{field}.script")
            continue
        if not target.is_file():
            out.error(at, "script_missing", f"`{field}.script` names {script}, which does not exist.",
                      f"{field}.script")
            continue
        if relative.parts[0] != "api":
            out.warn(at, "script_location", f"`{field}.script`: keep endpoints in api/ ({script}).",
                     f"{field}.script")
        if "description" in spec and not _is_str(spec["description"], DESCRIPTION_MAX, 0):
            out.error(at, "invalid_value", f"`{field}.description` must be a short text.",
                      f"{field}.description")
        if "timeout_s" in spec:
            timeout = spec["timeout_s"]
            whole = isinstance(timeout, int) or (isinstance(timeout, float) and timeout.is_integer())
            if isinstance(timeout, bool) or not whole or not 1 <= timeout <= MAX_TIMEOUT_S:
                out.error(at, "invalid_value",
                          f"`{field}.timeout_s` must be a whole number of seconds from 1 to {MAX_TIMEOUT_S}.",
                          f"{field}.timeout_s")
                continue
        usable[name] = target
    return usable


def _timeout_of(spec: dict) -> int:
    return int(spec.get("timeout_s", DEFAULT_TIMEOUT_S))


def _check_source(doc: dict, view: object, raw_endpoints: object, agent_root: Path | None,
                  at: str, out: _Findings) -> None:
    source = doc.get("source")
    if source is None:
        out.error(at, "required", "`source` is required: {\"endpoint\": name} or {\"file\": path}.", "source")
        return
    if not isinstance(source, dict) or set(source) not in ({"endpoint"}, {"file"}):
        out.error(at, "source_invalid",
                  "`source` names exactly one of `endpoint` or `file`, and nothing else.", "source")
        return
    if "endpoint" in source:
        name = source["endpoint"]
        declared = raw_endpoints if isinstance(raw_endpoints, dict) else {}
        if not isinstance(name, str) or name not in declared:
            out.error(at, "unknown_endpoint",
                      f"`source.endpoint` {name!r} is not one of `endpoints`.", "source.endpoint")
        return
    value = source["file"]
    relative = _safe_relative(value) if isinstance(value, str) else None
    if relative is None:
        out.error(at, "source_file_location",
                  "`source.file` must be an agent-relative path under app-data/ or files/.", "source.file")
        return
    posix = relative.as_posix()
    if not posix.startswith(SOURCE_FILE_ROOTS):
        out.error(at, "source_file_location",
                  f"`source.file` {posix} must be under app-data/ or files/ "
                  "(write data to app-data/dashboards/<name>/, never into dashboards/).", "source.file")
    suffix = relative.suffix
    if suffix not in SOURCE_FILE_EXTENSIONS:
        out.error(at, "source_file_extension",
                  f"`source.file` {posix} must be .md, .json or .csv (lowercase).", "source.file")
    elif (suffix == ".csv" and view != "table") or (suffix == ".md" and view != "markdown"):
        out.error(at, "source_file_view",
                  f"`source.file` {posix}: a .csv feeds a table view and a .md a markdown view.",
                  "source.file")
    if agent_root is not None and not _inside(agent_root / relative, agent_root):
        out.error(at, "symlink_escape",
                  f"`source.file` {posix} leads outside the agent folder through a symlink.", "source.file")


def _check_entry(component_dir: Path, doc: dict, view: object, at: str, out: _Findings) -> None:
    if view != "html":
        if "entry" in doc:
            out.warn(at, "entry_ignored", "`entry` is read only for an html view.", "entry")
        return
    entry = doc.get("entry", DEFAULT_ENTRY)
    relative = _safe_relative(entry) if isinstance(entry, str) else None
    if relative is None or relative.suffix not in ENTRY_EXTENSIONS:
        out.error(at, "entry_invalid", "`entry` must be an .html file inside the component folder.", "entry")
        return
    if not _inside(component_dir / relative, component_dir):
        out.error(at, "symlink_escape",
                  f"`entry` {relative.as_posix()} leads outside the component folder through a symlink.",
                  "entry")
        return
    if not (component_dir / relative).is_file():
        out.error(at, "entry_missing", f"`entry` {relative.as_posix()} does not exist.", "entry")


def _check_params(raw: object, at: str, out: _Findings) -> None:
    if raw is None:
        return
    if not isinstance(raw, dict):
        out.error(at, "invalid_type", "`params` must be an object of name → {type, …}.", "params")
        return
    for name, spec in raw.items():
        field = f"params.{name}"
        if not PARAM_NAME_RE.fullmatch(name):
            out.error(at, "param_name_invalid", f"param `{field}`: names use letters, digits and `_` only.",
                      field)
            continue
        if not isinstance(spec, dict) or spec.get("type") not in PARAM_TYPES:
            out.error(at, "invalid_value",
                      f"`{field}.type` must be one of {', '.join(PARAM_TYPES)}.", f"{field}.type")
            continue
        kind = spec["type"]
        if "label" in spec and not _is_str(spec["label"], TITLE_MAX):
            out.error(at, "invalid_value", f"`{field}.label` must be a short text.", f"{field}.label")
        values = spec.get("values")
        if kind == "enum" and (
            not isinstance(values, list) or not values or not all(_is_str(v, 200) for v in values)
        ):
            out.error(at, "invalid_value", f"`{field}.values` must list the choices as strings.",
                      f"{field}.values")
            continue
        if "default" in spec and not _default_ok(kind, spec["default"], values):
            out.error(at, "invalid_value", f"`{field}.default` does not fit type {kind}.",
                      f"{field}.default")


def _default_ok(kind: str, value: object, values: object) -> bool:
    if value is None:
        return True
    if kind == "enum":
        return isinstance(values, list) and value in values
    if kind == "month":
        return value == "current" or (isinstance(value, str) and bool(MONTH_RE.fullmatch(value)))
    if kind == "date":
        return value == "today" or (isinstance(value, str) and bool(DATE_RE.fullmatch(value)))
    if kind == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "text":
        return isinstance(value, str)
    return True  # date_range: host-defined presets


def _check_refresh(raw: object, agent_root: Path | None, manifest: dict | None,
                   at: str, out: _Findings) -> None:
    if raw is None:
        return
    if not isinstance(raw, dict):
        out.error(at, "invalid_type", "`refresh` must be an object.", "refresh")
        return
    # `max_age` has no null: leave it out for the 15m default (null would mean nothing the
    # schema defines, and a tile always needs some age at which it is recomputed).
    if "max_age" in raw and parse_duration(raw["max_age"]) is None:
        out.error(at, "duration_invalid",
                  f"`refresh.max_age` {raw['max_age']!r} is not a duration like 30s, 15m, 6h or 1d "
                  "(leave it out for the 15m default).", "refresh.max_age")
    every = raw.get("while_open_every")
    if every is not None:
        seconds = parse_duration(every)
        if seconds is None:
            out.error(at, "duration_invalid",
                      f"`refresh.while_open_every` {every!r} is not a duration like 5m.",
                      "refresh.while_open_every")
        elif seconds < MIN_WHILE_OPEN_SECONDS:
            out.error(at, "while_open_every_too_short",
                      f"`refresh.while_open_every` {every} is shorter than 1m.", "refresh.while_open_every")
    for flag in ("after_turn", "on_app_start"):
        if flag in raw and not isinstance(raw[flag], bool):
            out.error(at, "invalid_type", f"`refresh.{flag}` must be true or false.", f"refresh.{flag}")
    command = raw.get("command")
    if command is not None:
        match = RUN_COMMAND_RE.fullmatch(command) if isinstance(command, str) else None
        if match is None:
            out.error(at, "refresh_command_invalid",
                      "`refresh.command` must be `/run:<command name>` or null.", "refresh.command")
        else:
            known = _command_names(agent_root)
            if known is not None and match.group(1) not in known:
                out.warn(at, "refresh_command_unknown",
                         f"`refresh.command` {command}: no such command in {COMMAND_CATALOG}.",
                         "refresh.command")
    schedule = raw.get("schedule")
    if schedule is not None:
        if not _is_str(schedule, 255):
            out.error(at, "invalid_type", "`refresh.schedule` must be a schedule name or null.",
                      "refresh.schedule")
        else:
            known = _names(manifest, "schedules")
            if known is not None and schedule not in known:
                out.warn(at, "refresh_schedule_unknown",
                         f"`refresh.schedule` {schedule!r} is not in {MANIFEST_NAME} `schedules`.",
                         "refresh.schedule")


def _check_text_fields(doc: dict, at: str, out: _Findings) -> None:
    if "title" not in doc:
        out.error(at, "required", "`title` is required.", "title")
    elif not _is_str(doc["title"], TITLE_MAX):
        out.error(at, "invalid_value", f"`title` must be text of 1 to {TITLE_MAX} characters.", "title")
    if "description" in doc and not _is_str(doc["description"], DESCRIPTION_MAX, 0):
        out.error(at, "invalid_value", f"`description` must be text of at most {DESCRIPTION_MAX} characters.",
                  "description")
    if "context" in doc and not _is_str(doc["context"], CONTEXT_MAX, 0):
        out.error(at, "invalid_value", f"`context` must be text of at most {CONTEXT_MAX} characters.",
                  "context")
    if "size" in doc and doc["size"] not in SIZES:
        out.error(at, "invalid_value", f"`size` must be one of {', '.join(SIZES)}.", "size")
    prompts = doc.get("prompts")
    if prompts is None:
        return
    if not isinstance(prompts, list) or len(prompts) > PROMPTS_MAX:
        out.error(at, "invalid_value", f"`prompts` must be a list of at most {PROMPTS_MAX}.", "prompts")
        return
    for index, prompt in enumerate(prompts):
        if not (
            isinstance(prompt, dict)
            and _is_str(prompt.get("label"), PROMPT_LABEL_MAX)
            and _is_str(prompt.get("prompt"), PROMPT_TEXT_MAX)
        ):
            out.error(at, "invalid_value",
                      f"`prompts[{index}]` needs a `label` (≤ {PROMPT_LABEL_MAX}) and a `prompt` "
                      f"(≤ {PROMPT_TEXT_MAX}).", "prompts")
            return


def _check_credentials(raw: object, manifest: dict | None, at: str, out: _Findings) -> None:
    if raw is None:
        return
    if not isinstance(raw, list) or not all(isinstance(n, str) and n for n in raw):
        out.error(at, "invalid_type", "`requires_credentials` must be a list of credential names.",
                  "requires_credentials")
        return
    known = _names(manifest, "credentials")
    if known is None:
        out.warn(at, "manifest_missing",
                 f"no {MANIFEST_NAME} above this component, so `requires_credentials` was not checked; "
                 "pass --agent <agent folder>.", "requires_credentials")
        return
    for name in raw:
        if name not in known:
            out.error(at, "credential_unknown",
                      f"`requires_credentials` names {name!r}, which is not in {MANIFEST_NAME} `credentials`.",
                      "requires_credentials")


def _page_files(component_dir: Path, doc: dict) -> list[Path]:
    """The entry plus every page file (.html, .htm, .css, .js, .mjs) outside api/."""
    found: list[Path] = []
    for root, dirs, files in os.walk(component_dir):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
        if Path(root) == component_dir and "api" in dirs:
            dirs.remove("api")
        for name in sorted(files):
            if Path(name).suffix.lower() in PAGE_SCAN_EXTENSIONS:
                found.append(Path(root) / name)
    entry = doc.get("entry", DEFAULT_ENTRY)
    relative = _safe_relative(entry) if isinstance(entry, str) else None
    if relative is not None:
        entry_path = component_dir / relative
        if entry_path.is_file() and _inside(entry_path, component_dir) and entry_path not in found:
            found.append(entry_path)
    return found


def _scan_hits(text: str, rules: tuple) -> list[str]:
    return [field for field, pattern in rules if pattern.search(text)]


def _scan_page(path: Path, shown: str, out: _Findings) -> None:
    """One file's content scans: each (code, field) is reported once per file."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    suffix = path.suffix.lower()
    network = {".html": _HTML_NETWORK_RE, ".htm": _HTML_NETWORK_RE, ".css": _CSS_NETWORK_RE,
               ".js": _JS_NETWORK_RE, ".mjs": _JS_NETWORK_RE}[suffix]
    if network.search(text):
        out.warn(shown, "network_reference",
                 "loads something from the network; a web view gets none. Bundle it into the "
                 "folder and use a relative path.")
    for field in _scan_hits(text, _ROOT_RELATIVE_RES[suffix]):
        out.error(shown, "root_relative_path",
                  f"a `{field}` path starts with `/`, which resolves outside the component's own "
                  "folder and is blocked. Write `./app.js` or `app.js`.", field)
    if suffix in _HTML_EXTENSIONS and _BASE_TAG_RE.search(text):
        out.error(shown, "base_tag",
                  "has a `<base>` tag; the sandbox refuses it (base-uri 'none'). Remove it and use "
                  "relative paths.")
    for field in _scan_hits(text, _FETCH_ENDPOINT_RES):
        out.error(shown, "fetch_endpoint",
                  f"`{field}` asks for api/, which is never served to a page. Get the source through "
                  "cinna.onData and other endpoints through cinna.call.", field)
    for field in _scan_hits(text, _SANDBOX_UNSUPPORTED_RES):
        out.warn(shown, "sandbox_unsupported",
                 f"uses `{field}`: {_SANDBOX_REASONS[field]} in a web view.", field)


def _scan_pages(component_dir: Path, doc: dict, paths: _Paths, out: _Findings) -> None:
    """Content scans of a web view's page files: what its sandbox (schema `x-web-view`) blocks."""
    for path in _page_files(component_dir, doc):
        _scan_page(path, paths.show(path), out)


# --------------------------------------------------------------------------- #
# --run
# --------------------------------------------------------------------------- #


def default_params(raw: object, today: date | None = None) -> dict:
    """The params object a host sends with nothing chosen yet: each declared default."""
    today = today or date.today()
    params: dict = {}
    if not isinstance(raw, dict):
        return params
    for name, spec in raw.items():
        if not isinstance(spec, dict) or "default" not in spec or spec["default"] is None:
            continue
        value = spec["default"]
        if spec.get("type") == "month" and value == "current":
            value = today.strftime("%Y-%m")
        elif spec.get("type") == "date" and value == "today":
            value = today.isoformat()
        params[name] = value
    return params


def _node_executable() -> str | None:
    """`CINNA_NODE` when it names an executable file, else the first `node` on PATH.

    Never runs it: whatever `CINNA_NODE` names may not be a real node (a host's shim), so
    it is not probed with `--version` or anything else; endpoint_command runs it with the
    script as its only argument."""
    override = os.environ.get(NODE_ENV_VAR, "").strip()
    if override and os.path.isfile(override) and os.access(override, os.X_OK):
        return override
    return shutil.which("node")


def endpoint_command(script: Path, agent_root: Path) -> tuple[list[str] | None, str | None]:
    """How a host runs this endpoint (§5.3), and a caveat when the local run may differ.

    Returns (None, reason) when no runtime is found to run it with."""
    runtime = ENDPOINT_RUNTIMES.get(script.suffix)
    try:
        shown = script.resolve().relative_to(agent_root.resolve()).as_posix()
    except ValueError:
        shown = str(script.resolve())
    if runtime == "python":
        has_project = (agent_root / "pyproject.toml").is_file()
        uv = shutil.which("uv")
        if uv and has_project:
            return [uv, "run", "--quiet", "python", shown], None
        if has_project:
            return [sys.executable, shown], (
                "the agent has a pyproject.toml but no `uv` is on PATH, so this ran with "
                f"{sys.executable} instead of `uv run python`; it may fail on imports a host would have."
            )
        return [sys.executable, shown], None
    if runtime == "node":
        node = _node_executable()
        if node:
            return [node, shown], None
        return None, (
            f"not run: no `node` ({NODE_ENV_VAR} or PATH). A host falls back to its own Node "
            f"runtime; set {NODE_ENV_VAR} to an executable that runs a script the way `node` does, "
            "or install node, to run it here."
        )
    return None, "not run: no runtime for this extension."


def _kill_group(proc: subprocess.Popen) -> None:
    """Kill the endpoint and everything it started (its own process group / session)."""
    if os.name == "posix":
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        return
    # Windows has no killpg: taskkill /T takes the tree, kill() the process itself.
    try:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       capture_output=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        proc.kill()
    except OSError:
        pass


def _spawn(command: list[str], cwd: Path) -> subprocess.Popen:
    kwargs: dict = {"stdin": subprocess.PIPE, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE,
                    "cwd": cwd}
    if os.name == "posix":
        kwargs["start_new_session"] = True
    else:
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    return subprocess.Popen(command, **kwargs)


class _RunResult:
    def __init__(self) -> None:
        self.stdout = bytearray()
        self.stderr_tail = b""
        self.overflow = False
        self.timed_out = False
        self.returncode: int | None = None


def _read_stdout(proc: subprocess.Popen, result: _RunResult) -> None:
    """Read stdout in chunks up to the cap; past it, stop reading and kill the group."""
    stream = proc.stdout
    assert stream is not None
    while True:
        chunk = stream.read1(_READ_CHUNK) if hasattr(stream, "read1") else stream.read(_READ_CHUNK)
        if not chunk:
            return
        if len(result.stdout) + len(chunk) > MAX_STDOUT_BYTES:
            result.overflow = True
            _kill_group(proc)
            return
        result.stdout.extend(chunk)


def _read_stderr(proc: subprocess.Popen, result: _RunResult) -> None:
    """Keep only the tail of stderr (a few bytes per kept character, for UTF-8)."""
    stream = proc.stderr
    assert stream is not None
    keep = STDERR_TAIL_CHARS * 4
    while True:
        chunk = stream.read1(_READ_CHUNK) if hasattr(stream, "read1") else stream.read(_READ_CHUNK)
        if not chunk:
            return
        result.stderr_tail = (result.stderr_tail + chunk)[-keep:]


def _execute(command: list[str], stdin: bytes, cwd: Path, timeout: int) -> _RunResult:
    """Run `command` like a host: own process group, stdout capped, killed at the timeout."""
    result = _RunResult()
    proc = _spawn(command, cwd)
    readers = [threading.Thread(target=_read_stdout, args=(proc, result), daemon=True),
               threading.Thread(target=_read_stderr, args=(proc, result), daemon=True)]
    for reader in readers:
        reader.start()
    try:
        assert proc.stdin is not None
        proc.stdin.write(stdin)
        proc.stdin.close()
    except (BrokenPipeError, OSError):
        pass  # the endpoint does not read its params; that is allowed
    try:
        result.returncode = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        result.timed_out = True
        _kill_group(proc)
        proc.wait()
    if os.name == "posix":
        # A process the endpoint left behind would hold the pipes open: the run is over, end
        # its group. (Not on Windows, where a reused pid could name an unrelated tree.)
        _kill_group(proc)
    for reader in readers:
        reader.join(timeout=5)
    for stream in (proc.stdout, proc.stderr):
        try:
            if stream is not None:
                stream.close()
        except OSError:
            pass
    return result


def run_endpoint(script: Path, params: dict, agent_root: Path, timeout: int,
                 at: str, out: _Findings) -> tuple[bool, object]:
    """Run one endpoint like a host: params on stdin, one JSON value on stdout."""
    command, caveat = endpoint_command(script, agent_root)
    if command is None:
        out.warn(at, "runtime_missing", caveat or "not run: no runtime for this endpoint.")
        return False, None
    if caveat:
        out.warn(at, "uv_missing", caveat)
    try:
        result = _execute(command, json.dumps(params).encode("utf-8"), agent_root, timeout)
    except OSError as exc:
        out.error(at, "run_failed", f"could not start ({exc.strerror}).")
        return False, None
    if result.overflow:
        out.error(at, "run_output_invalid", "printed more than 5 MB; return less, or pre-aggregate.")
        return False, None
    if result.timed_out:
        out.error(at, "run_failed", f"did not finish within {timeout}s.")
        return False, None
    if result.returncode != 0:
        tail = result.stderr_tail.decode("utf-8", errors="replace")[-STDERR_TAIL_CHARS:].strip()
        out.error(at, "run_failed", f"exited with {result.returncode}: {tail or '(no stderr)'}")
        return False, None
    stdout = bytes(result.stdout)
    try:
        return True, json.loads(stdout.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        head = stdout.decode("utf-8", errors="replace")[:200].strip()
        out.error(at, "run_output_invalid",
                  f"stdout is not one JSON value (it starts with {head!r}). Print only the result; "
                  "send logs to stderr.")
        return False, None


def shape_problems(view: str, value: object) -> list[tuple[str, str, str]]:
    """(severity, field, message) for a result that does not fit its view (§5.2)."""
    found: list[tuple[str, str, str]] = []
    seen: set[str] = set()

    def add(severity: str, field: str, message: str) -> None:
        if field not in seen:  # one finding per field, the first index that breaks it
            seen.add(field)
            found.append((severity, field, message))

    if view == "html":
        return found
    if not isinstance(value, dict):
        add("error", "", f"a {view} source returns a JSON object.")
        return found
    if "generated_at" in value and not isinstance(value["generated_at"], str):
        add("error", "generated_at", "`generated_at` must be an ISO 8601 string.")
    if "notice" in value and (not isinstance(value["notice"], str) or "\n" in value["notice"]):
        add("error", "notice", "`notice` must be one line of text.")

    if view == "markdown":
        if not isinstance(value.get("markdown"), str):
            add("error", "markdown", "a markdown source returns {\"markdown\": \"…\"}.")
        return found

    if view == "table":
        columns, rows = value.get("columns"), value.get("rows")
        if not isinstance(columns, list) or not columns:
            add("error", "columns", "a table returns a non-empty `columns` list.")
        else:
            for i, column in enumerate(columns):
                if not isinstance(column, dict) or not isinstance(column.get("key"), str):
                    add("error", "columns.key", f"`columns[{i}]` needs a `key`.")
                elif "type" in column and column["type"] not in TABLE_COLUMN_TYPES:
                    add("error", "columns.type",
                        f"`columns[{i}].type` must be one of {', '.join(TABLE_COLUMN_TYPES)}.")
        if not isinstance(rows, list):
            add("error", "rows", "a table returns a `rows` list.")
        else:
            for i, row in enumerate(rows):
                if not isinstance(row, dict):
                    add("error", "rows", f"`rows[{i}]` must be an object.")
                elif "id" not in row:
                    add("warning", "rows.id", f"`rows[{i}]` has no `id`, so it cannot be selected.")
        return found

    items = value.get("items")
    if not isinstance(items, list):
        add("error", "items", f"a {view} returns an `items` list.")
        return found
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            add("error", "items", f"`items[{i}]` must be an object.")
            continue
        if "id" not in item:
            add("warning", "items.id", f"`items[{i}]` has no `id`, so it cannot be selected.")
        if view == "list":
            if not _is_str(item.get("title"), 10_000):
                add("error", "items.title", f"`items[{i}].title` is required.")
            if "priority" in item and item["priority"] not in LIST_PRIORITIES:
                add("error", "items.priority", f"`items[{i}].priority` must be high, medium or low.")
            if "url" in item and not (isinstance(item["url"], str) and item["url"].startswith(LINK_SCHEMES)):
                add("error", "items.url", f"`items[{i}].url` must be an https, http or mailto link.")
        else:  # metrics
            if not _is_str(item.get("label"), 10_000):
                add("error", "items.label", f"`items[{i}].label` is required.")
            metric = item.get("value")
            if isinstance(metric, bool) or not isinstance(metric, (int, float, str)):
                add("error", "items.value", f"`items[{i}].value` must be a number or a string.")
            if "delta_good" in item and item["delta_good"] not in METRIC_DELTA_GOOD:
                add("error", "items.delta_good", f"`items[{i}].delta_good` must be up or down.")
            if "status" in item and item["status"] not in METRIC_STATUSES:
                add("error", "items.status", f"`items[{i}].status` must be ok, warning or error.")
            trend = item.get("trend")
            if trend is not None and not (
                isinstance(trend, list)
                and all(isinstance(n, (int, float)) and not isinstance(n, bool) for n in trend)
            ):
                add("error", "items.trend", f"`items[{i}].trend` must be a list of numbers.")
    return found


def _run_source(component_dir: Path, doc: dict, endpoints: dict[str, Path], agent_root: Path,
                paths: _Paths, out: _Findings) -> None:
    view = doc["view"]
    source = doc["source"]
    if "endpoint" in source:
        script = endpoints.get(source["endpoint"])
        if script is None:
            return
        spec = doc["endpoints"][source["endpoint"]]
        timeout = _timeout_of(spec)
        at = paths.show(script)
        ok, value = run_endpoint(script, default_params(doc.get("params")), agent_root, timeout, at, out)
        if not ok:
            return
    else:
        target = agent_root / source["file"]
        at = paths.show(target)
        if not _inside(target, agent_root):
            return  # reported by the static check (symlink_escape); never read outside the agent
        if not target.is_file():
            out.warn(at, "source_file_unwritten",
                     "the source file does not exist yet; run the refresh command or schedule that writes it.")
            return
        suffix = target.suffix
        try:
            text = target.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError):
            out.error(at, "run_output_invalid", "the source file could not be read as UTF-8 text.")
            return
        if suffix == ".md":
            return
        if suffix == ".csv":
            if not text.strip().splitlines()[:1] or not text.strip().splitlines()[0].strip():
                out.error(at, "shape_invalid", "a .csv source needs a header row.", "columns")
            return
        try:
            value = json.loads(text)
        except ValueError:
            out.error(at, "run_output_invalid", "the source file is not valid JSON.")
            return
    for severity, field, message in shape_problems(view, value):
        if severity == "error":
            out.error(at, "shape_invalid", message, field or None)
        else:
            out.warn(at, "id_missing", message, field or None)


# --------------------------------------------------------------------------- #
# Entry points
# --------------------------------------------------------------------------- #


def check_component(component_dir: Path, *, agent_root: Path | None = None,
                    run: bool = False) -> list[Problem]:
    """Every problem with one component folder. `agent_root` defaults to the nearest
    folder above it holding `cinna-agent.json` (a skill cache outside any agent has none)."""
    given = Path(component_dir)
    if given.is_symlink():
        link = given.parent.resolve() / given.name
        root = agent_root or find_agent_root(link.parent)
        try:
            shown = link.relative_to(root.resolve()).as_posix() if root else link.as_posix()
        except ValueError:
            shown = link.as_posix()
        return [Problem(shown, "component_symlink",
                        "a component folder may not be a symlink; copy the folder in instead.")]
    component_dir = given.resolve()
    if agent_root is None:
        agent_root = find_agent_root(component_dir)
    paths = _Paths(agent_root)
    out = _Findings()
    shown_dir = paths.show(component_dir)
    if not component_dir.is_dir():
        out.error(shown_dir, "not_a_directory", "a component is a folder, dashboards/<name>/.")
        return out.problems
    owner, _skill = component_owner(component_dir)
    if owner == "unknown":
        out.error(shown_dir, "not_in_dashboards",
                  "a component lives in dashboards/<name>/ or skills/<skill>/dashboards/<name>/.")
    if not COMPONENT_NAME_RE.fullmatch(component_dir.name):
        out.error(shown_dir, "component_name_invalid",
                  "the folder name is the component name: lowercase letters, digits and '-', "
                  "starting with a letter or digit, at most 64 characters.")

    component_json = component_dir / COMPONENT_FILE
    at = paths.show(component_json)
    if not component_json.is_file():
        out.error(at, "component_json_missing", f"every component folder starts with {COMPONENT_FILE}.")
        return out.problems
    try:
        doc = json.loads(component_json.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        out.error(at, "component_json_invalid", f"is not valid JSON ({exc}).")
        return out.problems
    if not isinstance(doc, dict):
        out.error(at, "component_json_invalid", "must hold one JSON object.")
        return out.problems

    if "schema_version" not in doc:
        out.error(at, "required", "`schema_version` is required (1).", "schema_version")
    elif isinstance(doc["schema_version"], bool) or doc["schema_version"] != SCHEMA_VERSION:
        out.error(at, "invalid_value",
                  f"`schema_version` {doc['schema_version']!r} is not {SCHEMA_VERSION} ({PROTOCOL}).",
                  "schema_version")
    _check_text_fields(doc, at, out)
    view = doc.get("view")
    if view is None:
        out.error(at, "required", f"`view` is required: one of {', '.join(VIEWS)}.", "view")
    elif view not in VIEWS:
        out.error(at, "invalid_value", f"`view` {view!r} must be one of {', '.join(VIEWS)}.", "view")

    manifest = _read_manifest(agent_root)
    endpoints = _check_endpoints(component_dir, doc.get("endpoints"), at, out)
    _check_source(doc, view, doc.get("endpoints"), agent_root, at, out)
    _check_entry(component_dir, doc, view, at, out)
    _check_params(doc.get("params"), at, out)
    _check_refresh(doc.get("refresh"), agent_root, manifest, at, out)
    _check_credentials(doc.get("requires_credentials"), manifest, at, out)
    if view == "html":
        _scan_pages(component_dir, doc, paths, out)

    if run and not out.has_errors:
        if agent_root is None:
            out.warn(at, "manifest_missing",
                     f"not run: --run needs the agent folder (no {MANIFEST_NAME} above); "
                     "pass --agent <folder>.")
        else:
            _run_source(component_dir, doc, endpoints, agent_root, paths, out)
    return out.problems


def check_agent(agent_root: Path, *, run: bool = False) -> list[Problem]:
    """Every component of one agent, its own and those its skills ship."""
    agent_root = Path(agent_root).resolve()
    problems: list[Problem] = []
    dashboards = agent_root / DASHBOARDS_DIR
    if dashboards.is_dir():
        for child in sorted(dashboards.iterdir(), key=lambda p: p.name):
            if child.is_file() and not child.name.startswith("."):
                problems.append(Problem(
                    f"{DASHBOARDS_DIR}/{child.name}", "stray_file",
                    "only component folders belong in dashboards/; data goes to app-data/dashboards/<name>/.",
                    None, "warning",
                ))
    for link in symlinked_components(agent_root):
        problems.append(Problem(
            link.relative_to(agent_root).as_posix(), "component_symlink",
            "a component folder may not be a symlink; copy the folder in instead.",
        ))
    for component in find_components(agent_root):
        problems.extend(check_component(component, agent_root=agent_root, run=run))
    return problems


def check_path(path: Path, *, run: bool = False, agent_root: Path | None = None) -> list[Problem]:
    """A component folder, or an agent folder (every component in it)."""
    if Path(path).is_symlink():
        return check_component(Path(path), agent_root=agent_root, run=run)  # refused there
    path = Path(path).resolve()
    is_agent = (path / MANIFEST_NAME).is_file() and not (path / COMPONENT_FILE).is_file()
    if is_agent and agent_root is None:
        return check_agent(path, run=run)
    return check_component(path, agent_root=agent_root, run=run)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="check_component.py",
        description="Check a Cinna dashboard component before you offer it.",
    )
    parser.add_argument("path", nargs="?", default=".",
                        help="dashboards/<name>, skills/<skill>/dashboards/<name>, or an agent folder")
    parser.add_argument("--run", action="store_true",
                        help="also run the source with the default params and check the result's shape")
    parser.add_argument("--agent", help="the agent folder, when the component is not inside one")
    parser.add_argument("--json", action="store_true", help="print the findings as JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    agent_root = Path(args.agent).resolve() if args.agent else None
    problems = check_path(Path(args.path), run=args.run, agent_root=agent_root)
    ok = not any(p.severity == "error" for p in problems)
    if args.json:
        print(json.dumps({"ok": ok, "problems": [asdict(p) for p in problems]}, indent=2,
                         ensure_ascii=False))
    else:
        for problem in problems:
            print(problem.line())
        if ok:
            warnings = sum(1 for p in problems if p.severity == "warning")
            print(f"{args.path}: ok" + (f" ({warnings} warning(s))" if warnings else ""))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
