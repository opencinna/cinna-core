"""What a plugin tree contains, read off its files. Pure: no DB, no network.

:func:`analyse_plugin_tree` walks one checked-out plugin root (a ``claude`` or
``codex`` marketplace entry) and returns its components in the README §3.1
``components`` shape, whether anything in it runs code, the file list the
archive is built from, and — when the tree cannot be snapshotted — the reason.

Nothing in the tree is executed. JSON files are read with a size cap; skill
folders are read through the platform's own SKILL.md parser.
"""
from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from app.services.agents.skill_manifest import (
    DEFAULT_MAX_TOTAL_BYTES,
    SKIP_DIR_NAMES,
    parse_frontmatter,
    parse_skill_dir,
)
from app.services.plugins.llm_plugin_service import LLMPluginService

#: Marketplace entry / manifest keys that declare components.
COMPONENT_KEYS = ("skills", "commands", "agents", "hooks", "mcpServers", "lspServers")

#: Largest JSON file (manifest, ``.mcp.json``, hooks) the analyser reads.
MAX_JSON_BYTES = 1024 * 1024

_PLACEHOLDER_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-[^}]*)?\}")
_SECRET_NAME_RE = re.compile(r"KEY|TOKEN|SECRET|PASSWORD", re.IGNORECASE)
#: Plugin-root variables the engine fills in itself; never a user need.
_ENGINE_VARS_PREFIX = "CLAUDE_PLUGIN_"

_RUNTIMES = {
    "npx": "node", "node": "node", "npm": "node", "bunx": "node",
    "uvx": "python", "uv": "python", "python": "python", "python3": "python",
    "pipx": "python",
    "docker": "docker",
}


@dataclass
class Analysis:
    components: dict[str, Any]
    runs_code: bool
    reason: str | None
    files: list[tuple[str, Path]] = field(default_factory=list)
    total_bytes: int = 0


def empty_components() -> dict[str, Any]:
    return {
        "skills": [],
        "commands": [],
        "agents": [],
        "hooks": None,
        "mcp_servers": [],
        "lsp_servers": [],
    }


class _Unsafe(Exception):
    pass


class _TooLarge(Exception):
    pass


# -- walk ---------------------------------------------------------------------


def walk_plugin_tree(root: Path) -> tuple[list[tuple[str, Path]], int]:
    """Every regular file under ``root``, sorted, and their total size.

    Raises :class:`_Unsafe` on any symlink (file or directory, wherever it
    points) and :class:`_TooLarge` as soon as the total passes the 16 MB cap.
    ``.git`` and the regenerated-junk directories are not walked.
    """
    files: list[tuple[str, Path]] = []
    total = 0
    for dir_path, dir_names, file_names in os.walk(root, followlinks=False):
        kept = []
        for name in dir_names:
            # Symlinks first: a skipped name (``node_modules -> /``) is still
            # a way out of the tree for a declared component path.
            if Path(dir_path, name).is_symlink():
                raise _Unsafe
            if name in SKIP_DIR_NAMES:
                continue
            kept.append(name)
        dir_names[:] = sorted(kept)
        for name in file_names:
            full = Path(dir_path, name)
            st = full.lstat()
            if stat.S_ISLNK(st.st_mode):
                raise _Unsafe
            if not stat.S_ISREG(st.st_mode):
                continue
            total += st.st_size
            if total > DEFAULT_MAX_TOTAL_BYTES:
                raise _TooLarge
            files.append((full.relative_to(root).as_posix(), full))
    files.sort(key=lambda item: item[0])
    return files, total


# -- helpers -------------------------------------------------------------------


def _read_json(path: Path) -> Any:
    try:
        if not path.is_file() or path.stat().st_size > MAX_JSON_BYTES:
            return None
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def _declared_paths(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [v for v in value if isinstance(v, str)]
    return []


def contained_path(base: Path, rel: str | os.PathLike[str]) -> Path | None:
    """``base / rel`` when it cannot lead out of ``base``, else None.

    No segment between ``base`` and the target may be a symlink (whatever it
    points at), no segment may be ``..``, and the resolved target must sit
    inside the resolved ``base``. The one containment rule for a plugin root
    inside its checkout and for a declared path inside a plugin root.
    """
    current = base
    for part in PurePosixPath(Path(rel).as_posix()).parts:
        if part in ("", ".", "/"):
            continue
        if part == "..":
            return None
        current = current / part
        if current.is_symlink():
            return None
    try:
        if not current.resolve().is_relative_to(base.resolve()):
            return None
    except (OSError, RuntimeError):
        return None
    return current


def _inside(root: Path, raw: str, entry_name: str) -> Path | None:
    """``root / raw`` for a declared path; raises :class:`_Unsafe` when the
    path escapes ``root`` (lexically or through a symlink)."""
    safe = LLMPluginService._safe_entry_path(raw, entry_name=entry_name)
    if safe is None:
        raise _Unsafe
    found = contained_path(root, safe.strip("/") or ".")
    if found is None:
        raise _Unsafe
    return found


def _has_scripts(skill_dir: Path) -> bool:
    scripts = skill_dir / "scripts"
    try:
        return scripts.is_dir() and any(scripts.iterdir())
    except OSError:
        return False


def _skill(skill_dir: Path, fallback_name: str) -> dict[str, Any]:
    entry = parse_skill_dir(skill_dir)
    if entry.error is None:
        return {"name": entry.name, "has_scripts": bool(entry.has_scripts)}
    # Listed even when the SKILL.md does not validate here (a root skill's
    # folder name is never its name): the desktop validates what it installs.
    name = fallback_name
    try:
        frontmatter, _ = parse_frontmatter(
            (skill_dir / "SKILL.md").read_text(encoding="utf-8-sig")
        )
        if isinstance(frontmatter.get("name"), str) and frontmatter["name"].strip():
            name = frontmatter["name"].strip()
    except (OSError, ValueError):
        pass
    return {"name": name, "has_scripts": _has_scripts(skill_dir)}


def _md_names(base: Path, *, recursive: bool) -> list[str]:
    """``*.md`` under ``base`` as names; nested ones as ``sub:stem``."""
    if base.is_file():
        return [base.stem] if base.suffix == ".md" else []
    if not base.is_dir():
        return []
    pattern = "**/*.md" if recursive else "*.md"
    names = []
    for path in sorted(base.glob(pattern)):
        if not path.is_file():
            continue
        rel = PurePosixPath(path.relative_to(base).as_posix())
        names.append(":".join([*rel.parent.parts, rel.stem]))
    return names


def _unique(values: list[Any]) -> list[Any]:
    seen: list[Any] = []
    for value in values:
        if value not in seen:
            seen.append(value)
    return seen


# -- component readers ---------------------------------------------------------


def _skills(root: Path, declared: list[Any], name: str) -> list[dict[str, Any]]:
    dirs: list[Path] = []
    skills_dir = root / "skills"
    if skills_dir.is_dir():
        dirs.extend(
            child for child in sorted(skills_dir.iterdir())
            if child.is_dir() and (child / "SKILL.md").is_file()
        )
    for value in declared:
        for raw in _declared_paths(value):
            base = _inside(root, raw, name)
            if base is None or not base.is_dir():
                continue
            if (base / "SKILL.md").is_file():
                dirs.append(base)
            else:
                dirs.extend(
                    child for child in sorted(base.iterdir())
                    if child.is_dir() and (child / "SKILL.md").is_file()
                )
    if not dirs and (root / "SKILL.md").is_file():
        dirs.append(root)
    unique_dirs = _unique([d.resolve() for d in dirs])
    found = [_skill(d, d.name if d != root.resolve() else name) for d in unique_dirs]
    return _unique(found)


def _md_component(
    root: Path, default_dir: str, declared: list[Any], name: str, *, recursive: bool
) -> list[str]:
    names = _md_names(root / default_dir, recursive=recursive)
    for value in declared:
        for raw in _declared_paths(value):
            base = _inside(root, raw, name)
            if base is not None:
                names.extend(_md_names(base, recursive=recursive))
    return sorted(set(names))


def _hook_events(value: Any) -> list[str]:
    if isinstance(value, dict):
        inner = value.get("hooks") if isinstance(value.get("hooks"), dict) else value
        return [k for k, v in inner.items() if isinstance(k, str) and isinstance(v, list)]
    return []


def _hooks(root: Path, declared: list[Any], name: str) -> dict[str, Any] | None:
    events: set[str] = set(_hook_events(_read_json(root / "hooks" / "hooks.json")))
    for value in declared:
        if isinstance(value, dict):
            events.update(_hook_events(value))
            continue
        for raw in _declared_paths(value):
            base = _inside(root, raw, name)
            if base is not None:
                events.update(_hook_events(_read_json(base)))
    return {"events": sorted(events)} if events else None


def _server_map(value: Any) -> dict[str, Any]:
    """``{mcpServers: {...}}`` or a flat ``{name: config}`` map."""
    if not isinstance(value, dict):
        return {}
    for key in ("mcpServers", "lspServers"):
        if isinstance(value.get(key), dict):
            return value[key]
    return {k: v for k, v in value.items() if isinstance(v, dict)}


def _collect_servers(
    root: Path, default_file: str, declared: list[Any], name: str
) -> dict[str, dict[str, Any]]:
    servers: dict[str, dict[str, Any]] = {}
    servers.update(_server_map(_read_json(root / default_file)))
    for value in declared:
        if isinstance(value, dict):
            servers.update(_server_map(value))
            continue
        items = value if isinstance(value, list) else [value]
        for item in items:
            if isinstance(item, dict):
                servers.update(_server_map(item))
            elif isinstance(item, str):
                base = _inside(root, item, name)
                if base is not None:
                    servers.update(_server_map(_read_json(base)))
    return {k: v for k, v in servers.items() if isinstance(k, str) and isinstance(v, dict)}


def _placeholders(value: Any) -> list[str]:
    if isinstance(value, str):
        return _PLACEHOLDER_RE.findall(value)
    if isinstance(value, list):
        return [n for item in value for n in _placeholders(item)]
    if isinstance(value, dict):
        return [n for item in value.values() for n in _placeholders(item)]
    return []


def _mcp_server(server_name: str, config: dict[str, Any]) -> dict[str, Any]:
    declared_type = config.get("type") if isinstance(config.get("type"), str) else None
    if declared_type == "streamable-http":
        declared_type = "http"
    if declared_type in ("stdio", "http", "sse"):
        transport = declared_type
    elif config.get("command"):
        transport = "stdio"
    else:
        transport = "http"

    runtime = None
    command = config.get("command")
    if isinstance(command, str) and command.strip():
        runtime = _RUNTIMES.get(PurePosixPath(command.strip().split()[0]).name)

    env: dict[str, bool] = {}
    raw_env = config.get("env") if isinstance(config.get("env"), dict) else {}
    for key, value in raw_env.items():
        if not isinstance(key, str) or key.startswith(_ENGINE_VARS_PREFIX):
            continue
        refs = _placeholders(value)
        if refs and all(var.startswith(_ENGINE_VARS_PREFIX) for var in refs):
            continue  # filled in by the engine, e.g. ``${CLAUDE_PLUGIN_ROOT}/x``
        is_placeholder = isinstance(value, str) and bool(_PLACEHOLDER_RE.fullmatch(value.strip()))
        env[key] = is_placeholder or bool(_SECRET_NAME_RE.search(key))
        # The placeholder's own variable is what the user supplies.
        for var in _placeholders(value):
            if not var.startswith(_ENGINE_VARS_PREFIX) and var != key:
                env.setdefault(var, True)
    headers = config.get("headers") if isinstance(config.get("headers"), dict) else {}
    for var in _placeholders(headers):
        if not var.startswith(_ENGINE_VARS_PREFIX):
            env[var] = True
    for source in (config.get("args"), config.get("url")):
        for var in _placeholders(source):
            if not var.startswith(_ENGINE_VARS_PREFIX):
                env.setdefault(var, bool(_SECRET_NAME_RE.search(var)))

    if transport == "stdio":
        auth = "env" if env else "none"
    elif config.get("oauth"):
        auth = "oauth"
    else:
        auth = "none"
        for header, value in headers.items():
            if not _placeholders(value):
                continue
            if (
                isinstance(header, str)
                and header.lower() == "authorization"
                and isinstance(value, str)
                and value.strip().lower().startswith("bearer")
            ):
                auth = "bearer"
                break
            auth = "env"

    return {
        "name": server_name,
        "transport": transport,
        "runtime": runtime,
        "auth": auth,
        "env": [{"name": n, "secret": s} for n, s in env.items()],
    }


def _manifest(root: Path, fmt: str) -> dict[str, Any]:
    order = (".codex-plugin", ".claude-plugin") if fmt == "codex" else (
        ".claude-plugin", ".codex-plugin"
    )
    for folder in order:
        data = _read_json(root / folder / "plugin.json")
        if isinstance(data, dict):
            return data
    return {}


# -- entry point ---------------------------------------------------------------


def analyse_plugin_tree(
    root: Path, entry: dict | None, fmt: str, *, checkout: Path | None = None
) -> Analysis:
    """Components, ``runs_code`` and archive files of the plugin at ``root``.

    ``entry`` is the marketplace entry (its ``config``): components it declares
    inline (``strict: false`` entries, or any of :data:`COMPONENT_KEYS`) are
    merged with the plugin's own manifest and default folders.
    ``checkout`` is the clone ``root`` lives in; when given, ``root`` must be
    :func:`contained_path` inside it.
    Reasons: ``unsafe_path`` (a symlink anywhere, including any folder between
    ``checkout`` and ``root``, or a path escaping), ``unknown_source`` (no
    such folder), ``too_large`` (over 16 MB uncompressed), ``no_components``.
    """
    entry = entry if isinstance(entry, dict) else {}
    name = str(entry.get("name") or root.name)
    try:
        if checkout is not None:
            try:
                rel = root.relative_to(checkout)
            except ValueError:
                raise _Unsafe
            if contained_path(checkout, rel) is None:
                raise _Unsafe
        if root.is_symlink():
            raise _Unsafe
        if not root.is_dir():
            return Analysis(
                components=empty_components(), runs_code=False, reason="unknown_source"
            )
        files, total = walk_plugin_tree(root)
        manifest = _manifest(root, fmt)
        declared = {
            key: [src[key] for src in (manifest, entry) if key in src]
            for key in COMPONENT_KEYS
        }
        components = empty_components()
        components["skills"] = _skills(root, declared["skills"], name)
        components["commands"] = _md_component(
            root, "commands", declared["commands"], name, recursive=True
        )
        components["agents"] = _md_component(
            root, "agents", declared["agents"], name, recursive=False
        )
        components["hooks"] = _hooks(root, declared["hooks"], name)
        mcp = _collect_servers(root, ".mcp.json", declared["mcpServers"], name)
        components["mcp_servers"] = [
            _mcp_server(server_name, config) for server_name, config in sorted(mcp.items())
        ]
        lsp = _collect_servers(root, ".lsp.json", declared["lspServers"], name)
        components["lsp_servers"] = sorted(lsp)
    except _Unsafe:
        return Analysis(components=empty_components(), runs_code=False, reason="unsafe_path")
    except _TooLarge:
        return Analysis(components=empty_components(), runs_code=False, reason="too_large")

    runs_code = (
        any(s["has_scripts"] for s in components["skills"])
        or components["hooks"] is not None
        or any(s["transport"] == "stdio" for s in components["mcp_servers"])
        or bool(components["lsp_servers"])
    )
    has_any = any(
        components[key] for key in ("skills", "commands", "agents", "mcp_servers", "lsp_servers")
    ) or components["hooks"] is not None
    return Analysis(
        components=components,
        runs_code=runs_code,
        reason=None if has_any else "no_components",
        files=files,
        total_bytes=total,
    )
