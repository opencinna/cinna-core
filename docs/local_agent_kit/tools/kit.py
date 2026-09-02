#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Cinna local agent kit tool.

Standard library only, Python 3.10+. No install step, no third-party imports.
Run it with ``uv run .cinna-kit/tools/kit.py …``: the inline script metadata above
lets uv pick (and if needed download) a compatible interpreter, so the macOS system
``python3`` (3.9) is never a blocker. A bare ``python3`` works too when it is 3.10+.

Commands
--------
  new <slug> [--name N] [--root DIR]   scaffold Local/<slug>/ from templates/agent/
  validate <path> [--fix] [--json] [--cloud-ready]
                                       check an agent is coherent and cloud-ready
  list [--root DIR]                    table of local agents and their ladder rungs
  refresh [--check]                    compare / update the kit from the platform
  export <path> --to DIR [--force]     produce the cloud-import tree

Exit codes: 1 when the command failed or `validate` found errors, 0 otherwise
(warnings never fail a run; neither does an unreachable platform on `refresh`).

Never prints a credential value. Findings name the offending file or key only.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path, PurePosixPath

# --------------------------------------------------------------------------- #
# Kit layout
# --------------------------------------------------------------------------- #

KIT_DIR = Path(__file__).resolve().parent.parent
KIT_JSON_PATH = KIT_DIR / "kit.json"
SCHEMA_PATH = KIT_DIR / "schema" / "cinna-agent.schema.json"
TEMPLATE_DIR = KIT_DIR / "templates" / "agent"
VERSION_PATH = KIT_DIR / "VERSION"
LAST_REFRESH_CHECK = ".last_refresh_check"

MANIFEST_NAME = "cinna-agent.json"
SUPPORTED_SCHEMA_VERSION = 1

# --------------------------------------------------------------------------- #
# Manifest contract (subset of schema/cinna-agent.schema.json)
# --------------------------------------------------------------------------- #

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
ENV_PREFIX_RE = re.compile(r"^[A-Z][A-Z0-9_]*_$")
CRON_RE = re.compile(r"^\S+\s+\S+\s+\S+\s+\S+\s+\S+$")
COMMAND_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
UNRENDERED_TOKEN_RE = re.compile(r"^\{\{.*\}\}$")

CREDENTIAL_TYPES = (
    "email_imap",
    "email_smtp",
    "odoo",
    "gmail_oauth",
    "gmail_oauth_readonly",
    "gdrive_oauth",
    "gdrive_oauth_readonly",
    "gcalendar_oauth",
    "gcalendar_oauth_readonly",
    "google_service_account",
    "api_token",
    "ssh_key",
)
SCHEDULE_TYPES = ("static_prompt", "script_trigger")

PROMPT_KEYS = ("workflow", "entrypoint", "refiner")
DEFAULT_PROMPTS = {
    "workflow": "docs/WORKFLOW_PROMPT.md",
    "entrypoint": "docs/ENTRYPOINT_PROMPT.md",
    "refiner": "docs/REFINER_PROMPT.md",
}

# Ignore rules the scaffold ships under a dotless name; `new` restores the dot.
# A shipped `.gitignore` would be a live ignore rule inside the kit's own source
# tree and inside the synced snapshot, hiding scaffold files from the platform
# repository itself — so the kit a fresh clone publishes would differ from the
# one that was tested. Every rule that excludes a *path* is listed here.
#
# `credentials/.gitignore` deliberately keeps its dot: it names `.env` and
# `credentials.json`, which must not be committed to the platform repository
# either, so there it does the right thing rather than hiding content.
SCAFFOLD_IGNORE_TARGET = ".gitignore"
SCAFFOLD_IGNORE_FILES = (
    "gitignore",                 # -> .gitignore              (the agent root)
    "app-data/cache/gitignore",  # -> app-data/cache/.gitignore
)

# Missing these makes the agent structurally broken.
REQUIRED_FILES = (MANIFEST_NAME, SCAFFOLD_IGNORE_TARGET)
# Missing these is a cloud-readiness / convention problem, not a broken agent.
EXPECTED_FILES = (
    "README.md",
    "AGENTS.md",
    "Makefile",
    "pyproject.toml",
    "workspace_requirements.txt",
    "docs/CLI_COMMANDS.yaml",
    "scripts/README.md",
    "credentials/.env.example",
)

SHIPPED_SCRIPTS = ("cinna_credentials.py", "update_status.py")

# Directories never walked when scanning an agent tree.
SKIP_DIRS = {".git", ".venv", "__pycache__", "node_modules", ".mypy_cache", ".ruff_cache"}

# Every dotenv shape: `.env`, `prod.env`, `.env.local`, `.env.production`.
def is_env_filename(name: str) -> bool:
    return name == ".env" or name.endswith(".env") or name.startswith(".env.")


# Names that are almost always key material. Reported, never opened.
SECRET_FILENAMES = ("credentials.json", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519")
SECRET_GLOBS = ("*.pem", "*.p12", "*.pfx")

# Applied on export even if kit.json says otherwise — secrets and VCS state
# never travel to the cloud.
ALWAYS_EXCLUDE = (
    "credentials/",
    ".git/",
    ".venv/",
    ".env",
    ".env.*",
    "*.env",
    "credentials.json",
)
DEFAULT_EXCLUDES = [
    "credentials/",
    ".venv/",
    ".claude/",
    "AGENTS.md",
    "CLAUDE.md",
    "app-data/",
    "temp/",
    "__pycache__/",
    "*.pyc",
    ".git/",
    ".DS_Store",
]

WORKSPACE_REQUIREMENTS_HEADER = (
    "# Runtime dependencies for the cloud workspace, one requirement specifier per line.\n"
    "# Generated from [project.dependencies] in pyproject.toml by\n"
    "#   kit.py validate . --fix\n"
    "# Edit pyproject.toml, then regenerate — do not hand-edit this file.\n"
)


class KitError(Exception):
    """A condition that stops the command."""


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #


class Report:
    """Collected findings for one validated agent."""

    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.infos: list[str] = []
        self.fixed: list[str] = []

    def error(self, message: str) -> None:
        self.errors.append(message)

    def warn(self, message: str) -> None:
        self.warnings.append(message)

    def info(self, message: str) -> None:
        self.infos.append(message)

    def fix(self, message: str) -> None:
        self.fixed.append(message)

    @property
    def ok(self) -> bool:
        return not self.errors


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


def read_text(path: Path) -> str:
    """Read a text file, tolerating undecodable bytes."""
    return path.read_text(encoding="utf-8", errors="replace")


def read_json(path: Path) -> object:
    return json.loads(read_text(path))


def kit_version() -> str | None:
    """The kit's own version, or None when it is absent / still a placeholder."""
    if not VERSION_PATH.is_file():
        return None
    value = read_text(VERSION_PATH).strip()
    if not value or UNRENDERED_TOKEN_RE.match(value):
        return None
    return value


def kit_config() -> dict:
    if not KIT_JSON_PATH.is_file():
        return {}
    data = read_json(KIT_JSON_PATH)
    return data if isinstance(data, dict) else {}


def cloud_import_excludes() -> list[str]:
    config = kit_config()
    block = config.get("cloud_import")
    if isinstance(block, dict):
        patterns = block.get("exclude")
        if isinstance(patterns, list):
            values = [p for p in patterns if isinstance(p, str) and p]
            if values:
                return values
    return list(DEFAULT_EXCLUDES)


def resolve_root(explicit: str | None) -> Path:
    """The workshop root that holds Local/, Cloud/ and .cinna-kit/."""
    if explicit:
        return Path(explicit).expanduser().resolve()
    if KIT_DIR.name == ".cinna-kit":
        return KIT_DIR.parent
    return Path.cwd().resolve()


def relative_kit_tool(from_dir: Path) -> str:
    """`kit.py` path to print in hints, relative to `from_dir` when sensible."""
    tool = Path(__file__).resolve()
    try:
        rel = os.path.relpath(tool, from_dir)
    except ValueError:  # different drives (Windows)
        return str(tool)
    return rel if len(rel) < len(str(tool)) else str(tool)


def iter_files(root: Path):
    """Yield every file under `root`, skipping noise directories."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for filename in sorted(filenames):
            yield Path(dirpath) / filename


def normalize_requirement(spec: str) -> str:
    """PEP 503 style name of a requirement specifier (`Foo_Bar[x]>=1` -> `foo-bar`)."""
    name = re.split(r"[<>=!~;\[\s@]", spec.strip(), maxsplit=1)[0]
    return re.sub(r"[-_.]+", "-", name).strip().lower()


# --------------------------------------------------------------------------- #
# git interrogation (tolerates git being absent or the tree not being a repo)
# --------------------------------------------------------------------------- #


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def git_available(cwd: Path) -> bool:
    result = _git(cwd, "rev-parse", "--is-inside-work-tree")
    return bool(result and result.returncode == 0 and result.stdout.strip() == "true")


def git_tracked(cwd: Path, relative_path: str) -> bool:
    result = _git(cwd, "ls-files", "--error-unmatch", "--", relative_path)
    return bool(result and result.returncode == 0)


def git_ignored(cwd: Path, relative_path: str) -> bool:
    result = _git(cwd, "check-ignore", "-q", "--", relative_path)
    return bool(result and result.returncode == 0)


def gitignore_covers_env(agent_dir: Path) -> bool:
    """Textual fallback for `git check-ignore` when git cannot answer."""
    candidates = {
        agent_dir / ".gitignore": {
            ".env",
            "*.env",
            "credentials/.env",
            "/credentials/.env",
            "credentials/",
            "/credentials/",
        },
        agent_dir / "credentials" / ".gitignore": {".env", "/.env", "*.env"},
    }
    for gitignore, patterns in candidates.items():
        if not gitignore.is_file():
            continue
        for raw_line in read_text(gitignore).splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or line.startswith("!"):
                continue
            if line in patterns:
                return True
    return False


# --------------------------------------------------------------------------- #
# Manifest validation (pragmatic subset of the JSON Schema)
# --------------------------------------------------------------------------- #


def _check_str(report: Report, value: object, label: str, *, max_length: int, min_length: int = 1) -> bool:
    if not isinstance(value, str):
        report.error(f"{MANIFEST_NAME}: `{label}` must be a string.")
        return False
    if len(value) < min_length:
        report.error(f"{MANIFEST_NAME}: `{label}` must not be empty.")
        return False
    if len(value) > max_length:
        report.error(f"{MANIFEST_NAME}: `{label}` is longer than {max_length} characters.")
        return False
    return True


def validate_manifest(manifest: dict, report: Report) -> None:
    """Validate the manifest against the subset of the schema stdlib can express."""
    schema_version = manifest.get("schema_version")
    if not isinstance(schema_version, int) or isinstance(schema_version, bool):
        report.error(f"{MANIFEST_NAME}: `schema_version` must be an integer.")
    elif schema_version > SUPPORTED_SCHEMA_VERSION:
        report.error(
            f"{MANIFEST_NAME}: `schema_version` {schema_version} is newer than this tool "
            f"understands ({SUPPORTED_SCHEMA_VERSION}). Run `kit.py refresh` first."
        )
    elif schema_version != SUPPORTED_SCHEMA_VERSION:
        report.error(
            f"{MANIFEST_NAME}: `schema_version` must be {SUPPORTED_SCHEMA_VERSION}, got {schema_version}."
        )

    _check_str(report, manifest.get("name"), "name", max_length=255)
    _check_str(report, manifest.get("description"), "description", max_length=2000)

    slug = manifest.get("slug")
    if not isinstance(slug, str):
        report.error(f"{MANIFEST_NAME}: `slug` must be a string.")
    elif not SLUG_RE.match(slug):
        report.error(f"{MANIFEST_NAME}: `slug` must match ^[a-z0-9][a-z0-9-]{{1,62}}$, got {slug!r}.")

    prompts = manifest.get("prompts", {})
    if not isinstance(prompts, dict):
        report.error(f"{MANIFEST_NAME}: `prompts` must be an object.")
    else:
        for key, value in prompts.items():
            if key not in PROMPT_KEYS:
                report.error(f"{MANIFEST_NAME}: unknown prompt key `{key}`.")
            elif not isinstance(value, str) or not value:
                report.error(f"{MANIFEST_NAME}: `prompts.{key}` must be a non-empty path.")

    example_prompts = manifest.get("example_prompts", [])
    if not isinstance(example_prompts, list):
        report.error(f"{MANIFEST_NAME}: `example_prompts` must be an array.")
    else:
        for index, prompt in enumerate(example_prompts):
            if not isinstance(prompt, str) or not prompt.strip():
                report.error(f"{MANIFEST_NAME}: `example_prompts[{index}]` must be a non-empty string.")

    router_trigger = manifest.get("router_trigger_prompt")
    if router_trigger is not None and not isinstance(router_trigger, str):
        report.error(f"{MANIFEST_NAME}: `router_trigger_prompt` must be a string or null.")

    status_command = manifest.get("status_refresh_command")
    if status_command is not None and not isinstance(status_command, str):
        report.error(f"{MANIFEST_NAME}: `status_refresh_command` must be a string or null.")

    _validate_credentials(manifest.get("credentials", []), report)
    _validate_schedules(manifest.get("schedules", []), report)
    _validate_handovers(manifest.get("handovers", []), report)

    features = manifest.get("features", {})
    if not isinstance(features, dict):
        report.error(f"{MANIFEST_NAME}: `features` must be an object.")

    cloud = manifest.get("cloud", {})
    if not isinstance(cloud, dict):
        report.error(f"{MANIFEST_NAME}: `cloud` must be an object.")


def _validate_credentials(credentials: object, report: Report) -> None:
    if not isinstance(credentials, list):
        report.error(f"{MANIFEST_NAME}: `credentials` must be an array.")
        return
    seen: set[str] = set()
    for index, slot in enumerate(credentials):
        label = f"credentials[{index}]"
        if not isinstance(slot, dict):
            report.error(f"{MANIFEST_NAME}: `{label}` must be an object.")
            continue
        name = slot.get("name")
        if not isinstance(name, str) or not name:
            report.error(f"{MANIFEST_NAME}: `{label}.name` is required.")
        elif name in seen:
            report.error(f"{MANIFEST_NAME}: duplicate credential slot name `{name}`.")
        else:
            seen.add(name)
        credential_type = slot.get("type")
        if credential_type not in CREDENTIAL_TYPES:
            report.error(
                f"{MANIFEST_NAME}: `{label}.type` must be one of: {', '.join(CREDENTIAL_TYPES)}."
            )
        env_prefix = slot.get("env_prefix")
        if env_prefix is not None:
            if not isinstance(env_prefix, str) or not ENV_PREFIX_RE.match(env_prefix):
                report.error(
                    f"{MANIFEST_NAME}: `{label}.env_prefix` must match ^[A-Z][A-Z0-9_]*_$."
                )
        fields = slot.get("fields")
        if fields is not None and (
            not isinstance(fields, list) or not all(isinstance(f, str) and f for f in fields)
        ):
            report.error(f"{MANIFEST_NAME}: `{label}.fields` must be an array of names.")
        # Guard against a value having been pasted into the manifest.
        for forbidden in (
            "value",
            "values",
            "secret",
            "password",
            "token",
            "api_key",
            "apikey",
            "client_secret",
            "private_key",
            "credential_data",
        ):
            if forbidden in slot:
                report.error(
                    f"{MANIFEST_NAME}: `{label}` contains a `{forbidden}` key — the manifest "
                    "never holds credential values. Remove the key and put the value in "
                    "credentials/.env."
                )


def _validate_schedules(schedules: object, report: Report) -> None:
    if not isinstance(schedules, list):
        report.error(f"{MANIFEST_NAME}: `schedules` must be an array.")
        return
    for index, schedule in enumerate(schedules):
        label = f"schedules[{index}]"
        if not isinstance(schedule, dict):
            report.error(f"{MANIFEST_NAME}: `{label}` must be an object.")
            continue
        name = schedule.get("name")
        if not isinstance(name, str) or not name:
            report.error(f"{MANIFEST_NAME}: `{label}.name` is required.")
        cron_string = schedule.get("cron_string")
        if not isinstance(cron_string, str) or not CRON_RE.match(cron_string.strip()):
            report.error(
                f"{MANIFEST_NAME}: `{label}.cron_string` must be a five-field cron expression."
            )
        schedule_type = schedule.get("schedule_type")
        if schedule_type not in SCHEDULE_TYPES:
            report.error(
                f"{MANIFEST_NAME}: `{label}.schedule_type` must be one of: "
                f"{', '.join(SCHEDULE_TYPES)}."
            )
        elif schedule_type == "static_prompt":
            prompt = schedule.get("prompt")
            if not isinstance(prompt, str) or not prompt.strip():
                report.error(f"{MANIFEST_NAME}: `{label}.prompt` is required for static_prompt.")
        elif schedule_type == "script_trigger":
            command = schedule.get("command")
            if not isinstance(command, str) or not command.strip():
                report.error(f"{MANIFEST_NAME}: `{label}.command` is required for script_trigger.")
        timezone = schedule.get("timezone")
        if timezone is not None and not isinstance(timezone, str):
            report.error(f"{MANIFEST_NAME}: `{label}.timezone` must be a string or null.")
        enabled = schedule.get("enabled")
        if enabled is not None and not isinstance(enabled, bool):
            report.error(f"{MANIFEST_NAME}: `{label}.enabled` must be a boolean.")


def _validate_handovers(handovers: object, report: Report) -> None:
    if not isinstance(handovers, list):
        report.error(f"{MANIFEST_NAME}: `handovers` must be an array.")
        return
    for index, handover in enumerate(handovers):
        label = f"handovers[{index}]"
        if not isinstance(handover, dict):
            report.error(f"{MANIFEST_NAME}: `{label}` must be an object.")
            continue
        target = handover.get("target_slug")
        if not isinstance(target, str) or not SLUG_RE.match(target):
            report.error(f"{MANIFEST_NAME}: `{label}.target_slug` must be a valid slug.")


# --------------------------------------------------------------------------- #
# Project files
# --------------------------------------------------------------------------- #


def read_pyproject_dependencies(pyproject: Path) -> tuple[list[str], str | None]:
    """`[project].dependencies` plus a parse-failure reason (never silently empty)."""
    text = read_text(pyproject)
    try:
        import tomllib  # Python 3.11+
    except ImportError:
        tomllib = None  # type: ignore[assignment]
    if tomllib is not None:
        try:
            data = tomllib.loads(text)
        except Exception as exc:
            return [], f"pyproject.toml could not be parsed: {exc}"
        project = data.get("project")
        if isinstance(project, dict):
            dependencies = project.get("dependencies")
            if isinstance(dependencies, list):
                return [d for d in dependencies if isinstance(d, str) and d.strip()], None
        return [], None
    match = re.search(r"^\s*dependencies\s*=\s*\[(.*?)\]", text, re.DOTALL | re.MULTILINE)
    if not match:
        return [], None
    items = [item for item in re.findall(r"[\"']([^\"']+)[\"']", match.group(1)) if item.strip()]
    return items, None


def read_workspace_requirements(path: Path) -> list[str]:
    requirements = []
    for raw_line in read_text(path).splitlines():
        line = raw_line.strip()
        if line and not line.startswith("#"):
            requirements.append(line)
    return requirements


def write_workspace_requirements(path: Path, dependencies: list[str]) -> None:
    body = "".join(f"{dependency}\n" for dependency in dependencies)
    path.write_text(WORKSPACE_REQUIREMENTS_HEADER + body, encoding="utf-8")


def _strip_yaml_scalar(value: str) -> str:
    """Unquote a single-line YAML scalar and drop a trailing comment."""
    value = value.strip()
    if len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0]:
        return value[1:-1]
    if value[:1] in "\"'":
        closing = value.find(value[0], 1)
        if closing != -1:
            return value[1:closing]
    return re.sub(r"\s+#.*$", "", value).strip()


def cli_command_names(cli_commands_yaml: Path) -> list[str]:
    """Command names from CLI_COMMANDS.yaml — line scan only, no YAML parser.

    Only `- name:` items that are direct entries of the top-level `commands:` list
    are harvested, so a nested `name:` (under `args:`, say) is never mistaken for a
    command. A value that is not a valid command name is still returned, so the
    caller reports it rather than silently dropping it.
    """
    names: list[str] = []
    in_commands = False
    item_indent: int | None = None
    for raw_line in read_text(cli_commands_yaml).splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        if re.match(r"^commands\s*:", raw_line):
            in_commands = True
            continue
        if not in_commands:
            continue
        if re.match(r"^\S", raw_line):
            break  # a new top-level key ends the commands list
        match = re.match(r"^(\s*)-\s+name\s*:\s*(.+?)\s*$", raw_line)
        if not match:
            continue
        indent = len(match.group(1))
        if item_indent is None:
            item_indent = indent
        if indent != item_indent:
            continue  # a nested list item, not a command
        value = _strip_yaml_scalar(match.group(2))
        if value:
            names.append(value)
    return names


def makefile_targets(makefile: Path) -> set[str]:
    targets = set()
    for raw_line in read_text(makefile).splitlines():
        match = re.match(r"^([A-Za-z0-9_][A-Za-z0-9_.-]*)\s*:(?!=)", raw_line)
        if match:
            targets.add(match.group(1))
    return targets


# --------------------------------------------------------------------------- #
# Safe tar extraction
# --------------------------------------------------------------------------- #


def safe_extract(tar: tarfile.TarFile, destination: Path) -> None:
    """Extract `tar` into `destination`, refusing anything that escapes it.

    Rejects absolute member paths, `..` segments, members that resolve outside
    the destination, and every non regular-file/directory member (symlinks,
    hardlinks, devices, fifos). `tarfile`'s own default behaviour is not relied on.
    """
    destination = destination.resolve()
    safe_members = []
    for member in tar.getmembers():
        name = member.name
        if not name or name.strip("/") in ("", "."):
            continue  # the archive root itself carries nothing to extract
        if member.issym() or member.islnk():
            raise KitError(f"unsafe archive: link member {name!r}")
        if member.isdev() or member.ischr() or member.isblk() or member.isfifo():
            raise KitError(f"unsafe archive: special member {name!r}")
        if not (member.isfile() or member.isdir()):
            raise KitError(f"unsafe archive: unsupported member type {name!r}")
        if name.startswith("/") or name.startswith("\\") or re.match(r"^[A-Za-z]:[\\/]", name):
            raise KitError(f"unsafe archive: absolute member path {name!r}")
        parts = PurePosixPath(name).parts
        if any(part == ".." for part in parts):
            raise KitError(f"unsafe archive: parent-directory segment in {name!r}")
        target = (destination / name).resolve()
        if target != destination and destination not in target.parents:
            raise KitError(f"unsafe archive: member escapes the destination: {name!r}")
        # Drop setuid/setgid/sticky and group/other write; keep owner+read bits.
        member.mode = ((member.mode or 0o644) & 0o755) | 0o644
        safe_members.append(member)
    try:
        tar.extractall(destination, members=safe_members, filter="data")
    except TypeError:  # Python < 3.11.4 has no extraction filters
        tar.extractall(destination, members=safe_members)


# --------------------------------------------------------------------------- #
# Command: new
# --------------------------------------------------------------------------- #


def substitute_tokens(root: Path, replacements: dict[str, str]) -> None:
    """Rewrite the scaffold's `{{name}}` / `{{slug}}` tokens in place."""
    for path in iter_files(root):
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # binary or unreadable: nothing to substitute
        replaced = text
        for token, value in replacements.items():
            replaced = replaced.replace(token, value)
        if replaced != text:
            path.write_text(replaced, encoding="utf-8")


def restore_scaffold_ignore_files(agent_dir: Path) -> None:
    """Rename the scaffold's dotless `gitignore` files to `.gitignore`.

    The templates ship them dotless so they are inert where the kit is stored;
    the created agent needs the real names, because the root one is what keeps
    `credentials/.env` out of the user's history.
    """
    for relative in SCAFFOLD_IGNORE_FILES:
        source = agent_dir / relative
        target = source.with_name(SCAFFOLD_IGNORE_TARGET)
        if not source.is_file():
            # An older kit already shipped it dotted; nothing to rename.
            if target.is_file():
                continue
            raise KitError(
                f"scaffold template is missing {relative} (and its dotted "
                f"form): {TEMPLATE_DIR}"
            )
        if target.exists():
            target.unlink()
        source.rename(target)


def cmd_new(args: argparse.Namespace) -> int:
    slug = args.slug.strip()
    if not SLUG_RE.match(slug):
        raise KitError(f"slug must match ^[a-z0-9][a-z0-9-]{{1,62}}$, got {slug!r}")
    if not TEMPLATE_DIR.is_dir():
        raise KitError(f"scaffold template missing: {TEMPLATE_DIR}")

    name = (args.name or slug.replace("-", " ").title()).strip()
    if not name:
        raise KitError("--name must not be empty")

    root = resolve_root(args.root)
    target = root / "Local" / slug
    if target.exists():
        raise KitError(f"{target} already exists — pick another slug or remove the folder")

    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(TEMPLATE_DIR, target)
    restore_scaffold_ignore_files(target)

    substitute_tokens(target, {"{{name}}": name, "{{slug}}": slug})

    manifest_path = target / MANIFEST_NAME
    manifest = read_json(manifest_path)
    if not isinstance(manifest, dict):
        raise KitError(f"scaffold {MANIFEST_NAME} is not a JSON object")
    manifest["slug"] = slug
    manifest["name"] = name
    version = kit_version()
    if version:
        manifest["kit_version"] = version
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    file_count = sum(1 for _ in iter_files(target))
    tool = relative_kit_tool(target)
    print(f"Created {target} ({file_count} files) from the kit scaffold.")
    print()
    print("Next steps:")
    print(f"  1. cd {target}")
    print("  2. Interview the user, then build one capability at a time —")
    print("     read .cinna-kit/guides/01-first-agent.md")
    print("  3. Write docs/WORKFLOW_PROMPT.md, the description and example_prompts last")
    print(f"  4. python3 {tool} validate .")
    print()
    print("Run the ladder check in .cinna-kit/README.md after every substantive change.")
    return 0


# --------------------------------------------------------------------------- #
# Command: validate
# --------------------------------------------------------------------------- #


def _validate_files(agent_dir: Path, manifest: dict, report: Report) -> None:
    for relative in REQUIRED_FILES:
        if not (agent_dir / relative).is_file():
            report.error(f"missing required file: {relative}")
    for relative in EXPECTED_FILES:
        if not (agent_dir / relative).is_file():
            report.warn(f"missing expected file: {relative}")

    # A manifest may declare a subset of the prompts (the schema requires none of
    # them individually); only what it declares is required on disk, plus the
    # workflow prompt, which every agent needs.
    prompts = manifest.get("prompts")
    if isinstance(prompts, dict) and prompts:
        prompt_paths = {k: v for k, v in prompts.items() if isinstance(v, str) and v}
    else:
        prompt_paths = dict(DEFAULT_PROMPTS)
    prompt_paths.setdefault("workflow", DEFAULT_PROMPTS["workflow"])
    for key, relative in prompt_paths.items():
        if not (agent_dir / relative).is_file():
            report.error(f"missing {key} prompt: {relative}")

    workflow_relative = prompt_paths["workflow"]
    workflow = agent_dir / workflow_relative
    if workflow.is_file():
        text = read_text(workflow)
        if not text.strip():
            report.error(f"{workflow_relative} is empty — it is the agent's real instructions.")
        if "{{" in text:
            report.error(
                f"{workflow_relative} still contains an unfilled `{{{{...}}}}` placeholder."
            )


def _validate_secrets(agent_dir: Path, report: Report) -> None:
    """Secret hygiene. Findings name files, never their contents."""
    env_relative = "credentials/.env"
    if git_available(agent_dir):
        tracked = git_tracked(agent_dir, env_relative)
        if tracked:
            # `git check-ignore` always says "not ignored" for a tracked path, so the
            # .gitignore check below would fire a second, false finding.
            report.error(
                f"{env_relative} is tracked by git — remove it from the index before sharing "
                "this agent."
            )
        elif not git_ignored(agent_dir, env_relative) and not gitignore_covers_env(agent_dir):
            report.error(f"{env_relative} is not covered by .gitignore.")
    else:
        if not gitignore_covers_env(agent_dir):
            report.error(f"{env_relative} is not covered by .gitignore.")
        report.info("git is unavailable here — .gitignore was checked textually only.")

    for path in iter_files(agent_dir):
        relative = path.relative_to(agent_dir)
        if relative.parts[:1] == ("credentials",):
            continue
        if is_env_filename(path.name):
            report.error(
                f"{relative.as_posix()} is an env file outside credentials/ — move it to "
                "credentials/.env so it stays git-ignored and out of the cloud import."
            )
        elif path.name in SECRET_FILENAMES or any(
            fnmatch.fnmatch(path.name, glob) for glob in SECRET_GLOBS
        ):
            report.warn(
                f"{relative.as_posix()} looks like key material — credentials belong in "
                "credentials/, which is never copied to the cloud."
            )


def _validate_requirements(agent_dir: Path, report: Report, fix: bool) -> None:
    pyproject = agent_dir / "pyproject.toml"
    requirements_file = agent_dir / "workspace_requirements.txt"
    if not pyproject.is_file():
        return
    dependencies, parse_error = read_pyproject_dependencies(pyproject)
    if parse_error:
        report.warn(f"{parse_error} — dependency mirroring could not be checked.")
        return
    if not dependencies:
        return
    if not requirements_file.is_file():
        # The cloud installs from this file; without it the workspace gets no deps.
        if fix:
            write_workspace_requirements(requirements_file, dependencies)
            report.fix("workspace_requirements.txt created from [project.dependencies].")
        else:
            report.error(
                "workspace_requirements.txt is missing but pyproject.toml declares "
                f"{len(dependencies)} dependency(ies) — the cloud workspace would install "
                "none of them. Run with --fix to generate it."
            )
        return
    declared = {normalize_requirement(item) for item in read_workspace_requirements(requirements_file)}
    missing = [item for item in dependencies if normalize_requirement(item) not in declared]
    if not missing:
        return
    if fix:
        write_workspace_requirements(requirements_file, dependencies)
        report.fix("workspace_requirements.txt regenerated from [project.dependencies].")
        return
    report.error(
        "workspace_requirements.txt is missing dependencies declared in pyproject.toml: "
        f"{', '.join(missing)}. Run with --fix to regenerate it."
    )


def _validate_commands(agent_dir: Path, manifest: dict, report: Report) -> None:
    cli_commands = agent_dir / "docs" / "CLI_COMMANDS.yaml"
    makefile = agent_dir / "Makefile"
    if not cli_commands.is_file():
        return
    names = cli_command_names(cli_commands)
    for name in names:
        if not COMMAND_NAME_RE.match(name):
            report.error(
                f"docs/CLI_COMMANDS.yaml: command name {name!r} must match "
                "^[a-z][a-z0-9_-]{0,31}$."
            )
    duplicates = {name for name in names if names.count(name) > 1}
    for name in sorted(duplicates):
        report.error(f"docs/CLI_COMMANDS.yaml: duplicate command name {name!r}.")
    if makefile.is_file():
        targets = makefile_targets(makefile)
        for name in names:
            if name not in targets:
                report.warn(
                    f"docs/CLI_COMMANDS.yaml command {name!r} has no matching Makefile target — "
                    "add the local mirror in the same change."
                )
    schedules = manifest.get("schedules")
    if isinstance(schedules, list) and schedules:
        has_status = "status" in names or bool(manifest.get("status_refresh_command"))
        if not has_status:
            report.warn(
                "the agent has schedules but no `status` command — unattended runs should "
                "report state (guides/06-status-reporting.md)."
            )


def _validate_scripts_catalog(agent_dir: Path, report: Report) -> None:
    scripts_dir = agent_dir / "scripts"
    catalog = scripts_dir / "README.md"
    if not scripts_dir.is_dir() or not catalog.is_file():
        return
    catalog_text = read_text(catalog)
    for path in iter_files(scripts_dir):
        if path.suffix != ".py":
            continue
        relative = path.relative_to(scripts_dir).as_posix()
        if relative not in catalog_text and path.name not in catalog_text:
            report.warn(
                f"scripts/{relative} is not described in scripts/README.md — the catalog must "
                "never fall behind reality."
            )


def template_description() -> str | None:
    """The scaffold's placeholder description, so validate can spot an unedited one."""
    template_manifest = TEMPLATE_DIR / MANIFEST_NAME
    if not template_manifest.is_file():
        return None
    try:
        data = read_json(template_manifest)
    except json.JSONDecodeError:
        return None
    if isinstance(data, dict):
        description = data.get("description")
        if isinstance(description, str):
            return description
    return None


def _validate_cloud_readiness(
    agent_dir: Path, manifest: dict, report: Report, cloud_ready: bool
) -> None:
    slug = manifest.get("slug")
    if isinstance(slug, str) and slug != agent_dir.name:
        report.error(
            f"{MANIFEST_NAME}: `slug` is {slug!r} but the folder is named {agent_dir.name!r} — "
            "they must match."
        )

    # `--cloud-ready` is the go-cloud gate (guides/11-go-cloud.md step 7): what is
    # advice while the agent is still being built becomes blocking before import.
    blocking = report.error if cloud_ready else report.warn

    example_prompts = manifest.get("example_prompts")
    if isinstance(example_prompts, list):
        if not example_prompts:
            blocking(
                "`example_prompts` is empty — a cloud-ready agent needs at least one; it is "
                "also a routing input (guides/02-prompts-and-description.md)."
            )
        elif len(example_prompts) < 2:
            report.warn("only one `example_prompt` — a second one measurably improves routing.")

    description = manifest.get("description")
    if isinstance(description, str) and description.strip() == (template_description() or "").strip():
        blocking(
            "`description` is still the scaffold placeholder — rewrite it from what the "
            "agent actually does (guides/02-prompts-and-description.md)."
        )

    handovers = manifest.get("handovers")
    if isinstance(handovers, list):
        for handover in handovers:
            if not isinstance(handover, dict):
                continue
            target = handover.get("target_slug")
            if isinstance(target, str) and not (agent_dir.parent / target).is_dir():
                report.error(
                    f"{MANIFEST_NAME}: handover target {target!r} does not exist next to this "
                    f"agent ({agent_dir.parent})."
                )

    manifest_kit_version = manifest.get("kit_version")
    current = kit_version()
    if current and isinstance(manifest_kit_version, str) and manifest_kit_version != current:
        report.info(
            f"scaffolded with kit {manifest_kit_version}, current kit is {current} — "
            "read CHANGELOG.md for convention changes."
        )


def validate_agent(agent_dir: Path, fix: bool, cloud_ready: bool = False) -> Report:
    report = Report()
    if not agent_dir.is_dir():
        report.error(f"{agent_dir} is not a directory.")
        return report

    manifest_path = agent_dir / MANIFEST_NAME
    if not manifest_path.is_file():
        report.error(
            f"missing {MANIFEST_NAME} — this folder was not created by `kit.py new`."
        )
        return report
    try:
        manifest = read_json(manifest_path)
    except json.JSONDecodeError as exc:
        report.error(f"{MANIFEST_NAME} is not valid JSON: {exc.msg} (line {exc.lineno}).")
        return report
    if not isinstance(manifest, dict):
        report.error(f"{MANIFEST_NAME} must contain a JSON object.")
        return report

    validate_manifest(manifest, report)
    _validate_cloud_readiness(agent_dir, manifest, report, cloud_ready)
    _validate_files(agent_dir, manifest, report)
    _validate_secrets(agent_dir, report)
    _validate_requirements(agent_dir, report, fix)
    _validate_commands(agent_dir, manifest, report)
    _validate_scripts_catalog(agent_dir, report)
    return report


def cmd_validate(args: argparse.Namespace) -> int:
    agent_dir = Path(args.path).expanduser().resolve()
    report = validate_agent(agent_dir, fix=args.fix, cloud_ready=args.cloud_ready)

    if args.json:
        payload = {
            "agent": str(agent_dir),
            "slug": agent_dir.name,
            "ok": report.ok,
            "errors": report.errors,
            "warnings": report.warnings,
            "info": report.infos,
            "fixed": report.fixed,
            "schema": str(SCHEMA_PATH),
            "kit_version": kit_version(),
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0 if report.ok else 1

    print(f"Validating {agent_dir}")
    for message in report.fixed:
        print(f"  FIXED  {message}")
    for message in report.errors:
        print(f"  ERROR  {message}")
    for message in report.warnings:
        print(f"  WARN   {message}")
    for message in report.infos:
        print(f"  INFO   {message}")
    print()
    if report.ok:
        print(
            f"OK — {len(report.warnings)} warning(s). "
            "Warnings are advice: read them, then decide."
        )
    else:
        print(f"FAILED — {len(report.errors)} error(s), {len(report.warnings)} warning(s).")
    print(f"Full JSON Schema for the manifest: {SCHEMA_PATH}")
    return 0 if report.ok else 1


# --------------------------------------------------------------------------- #
# Command: list
# --------------------------------------------------------------------------- #


def _rungs_present(agent_dir: Path, manifest: dict) -> list[str]:
    """Which ladder rungs this agent has actually adopted."""
    rungs = []
    example_prompts = manifest.get("example_prompts")
    if isinstance(example_prompts, list) and example_prompts:
        rungs.append("prompts")

    scripts_dir = agent_dir / "scripts"
    if scripts_dir.is_dir():
        own_scripts = [
            path
            for path in iter_files(scripts_dir)
            if path.suffix == ".py" and path.name not in SHIPPED_SCRIPTS
        ]
        if own_scripts:
            rungs.append("scripts")

    if manifest.get("credentials"):
        rungs.append("credentials")
    if manifest.get("schedules"):
        rungs.append("schedules")
    if (agent_dir / "app-data" / "storage" / "STATUS.md").is_file():
        rungs.append("status")

    cli_commands = agent_dir / "docs" / "CLI_COMMANDS.yaml"
    if cli_commands.is_file():
        names = [name for name in cli_command_names(cli_commands) if name != "status"]
        if names:
            rungs.append("cli_commands")

    knowledge_dir = agent_dir / "knowledge"
    if knowledge_dir.is_dir():
        knowledge_files = [p for p in iter_files(knowledge_dir) if p.name != "README.md"]
        if knowledge_files:
            rungs.append("knowledge")

    if manifest.get("handovers"):
        rungs.append("multi_agent")

    cloud = manifest.get("cloud")
    if isinstance(cloud, dict) and cloud.get("agent_id"):
        rungs.append("go_cloud")
    return rungs


def _print_table(headers: list[str], rows: list[list[str]]) -> None:
    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    line = "  ".join(header.ljust(widths[index]) for index, header in enumerate(headers))
    print(line.rstrip())
    print("  ".join("-" * width for width in widths))
    for row in rows:
        print("  ".join(cell.ljust(widths[index]) for index, cell in enumerate(row)).rstrip())


def cmd_list(args: argparse.Namespace) -> int:
    root = resolve_root(args.root)
    local_dir = root / "Local"
    print(f"Root: {root}")
    print()

    rows: list[list[str]] = []
    if local_dir.is_dir():
        for agent_dir in sorted(p for p in local_dir.iterdir() if p.is_dir()):
            manifest_path = agent_dir / MANIFEST_NAME
            if not manifest_path.is_file():
                rows.append([agent_dir.name, "(no manifest)", "-", "-"])
                continue
            try:
                manifest = read_json(manifest_path)
            except json.JSONDecodeError:
                rows.append([agent_dir.name, "(invalid manifest)", "-", "-"])
                continue
            if not isinstance(manifest, dict):
                rows.append([agent_dir.name, "(invalid manifest)", "-", "-"])
                continue
            cloud = manifest.get("cloud")
            imported = "yes" if isinstance(cloud, dict) and cloud.get("agent_id") else "no"
            rows.append(
                [
                    agent_dir.name,
                    str(manifest.get("name") or "-"),
                    ", ".join(_rungs_present(agent_dir, manifest)) or "-",
                    imported,
                ]
            )

    if rows:
        _print_table(["SLUG", "NAME", "RUNGS", "CLOUD"], rows)
    else:
        print(f"No agents under {local_dir}. Create one with `kit.py new <slug>`.")

    account_file = root / "Cloud" / ".cinna" / "account.json"
    if account_file.is_file():
        cloud_agents_dir = root / "Cloud" / "agents"
        names = (
            sorted(p.name for p in cloud_agents_dir.iterdir() if p.is_dir())
            if cloud_agents_dir.is_dir()
            else []
        )
        print()
        print("Cloud workspace (Cloud/.cinna/account.json present):")
        if names:
            for name in names:
                print(f"  - {name}")
        else:
            print("  (no agents synced yet — `cinna agent sync <slug>` from Cloud/)")
    return 0


# --------------------------------------------------------------------------- #
# Command: refresh
# --------------------------------------------------------------------------- #


def _touch_last_refresh_check(directory: Path) -> None:
    try:
        (directory / LAST_REFRESH_CHECK).write_text(
            time.strftime("%Y-%m-%dT%H:%M:%S%z") + "\n", encoding="utf-8"
        )
    except OSError:
        pass


def _http_get(url: str, timeout: int = 30) -> bytes:
    if not url.startswith(("http://", "https://")):
        raise KitError(f"refusing a non-HTTP kit URL: {url}")
    request = urllib.request.Request(url, headers={"User-Agent": "cinna-kit/kit.py"})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - http(s) only
        return response.read()


def _parse_remote_version(payload: bytes) -> str:
    text = payload.decode("utf-8", errors="replace").strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return text
    if isinstance(data, dict):
        for key in ("kit_version", "version"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    if isinstance(data, str):
        return data.strip()
    return text


def _locate_extracted_kit(extract_dir: Path) -> Path:
    """The directory inside the extracted tarball that is the kit root."""
    candidates = [extract_dir]
    candidates.extend(path for path in sorted(extract_dir.iterdir()) if path.is_dir())
    for candidate in candidates:
        if (candidate / "kit.json").is_file() and (candidate / "VERSION").is_file():
            return candidate
    raise KitError("downloaded archive does not look like a kit (kit.json / VERSION missing)")


def _swap_kit_tree(new_tree: Path) -> None:
    """Replace KIT_DIR with `new_tree`; the old tree goes only after success."""
    backup = KIT_DIR.parent / f"{KIT_DIR.name}.old-{int(time.time())}"
    KIT_DIR.rename(backup)
    try:
        shutil.move(str(new_tree), str(KIT_DIR))
    except Exception:
        if not KIT_DIR.exists():
            backup.rename(KIT_DIR)
            raise
        # A half-written kit is in place: keep the backup and say where it is.
        raise KitError(
            f"the new kit could not be put in place. The previous kit is intact at "
            f"{backup} — move it back over {KIT_DIR} to recover."
        ) from None
    shutil.rmtree(backup, ignore_errors=True)


def cmd_refresh(args: argparse.Namespace) -> int:
    local_version = kit_version()
    config = kit_config()
    base_url = config.get("kit_base_url")
    if not isinstance(base_url, str) or not base_url or "{{" in base_url:
        # Stamp the check anyway, so an offline machine is not nagged every session.
        _touch_last_refresh_check(KIT_DIR)
        print("warning: kit.json has no usable kit_base_url — cannot check for updates.")
        return 0
    base_url = base_url.rstrip("/")

    try:
        remote_version = _parse_remote_version(_http_get(f"{base_url}/version"))
    except (KitError, urllib.error.URLError, OSError, ValueError) as exc:
        _touch_last_refresh_check(KIT_DIR)
        print(f"warning: could not reach {base_url}/version ({exc}). Continuing with the kit you have.")
        return 0

    _touch_last_refresh_check(KIT_DIR)

    if not remote_version:
        print(f"warning: {base_url}/version returned nothing usable. Continuing with the kit you have.")
        return 0
    if local_version and remote_version == local_version:
        print(f"Kit is up to date ({local_version}).")
        return 0

    print(f"Kit update available: local {local_version or 'unknown'} -> remote {remote_version}")
    if args.check:
        print("Run `kit.py refresh` to install it, then read CHANGELOG.md.")
        return 0

    temp_dir = Path(tempfile.mkdtemp(prefix=".cinna-kit-refresh-", dir=str(KIT_DIR.parent)))
    try:
        archive = temp_dir / "kit.tar.gz"
        try:
            archive.write_bytes(_http_get(f"{base_url}/kit.tar.gz", timeout=120))
        except (KitError, urllib.error.URLError, OSError, ValueError) as exc:
            print(f"warning: could not download {base_url}/kit.tar.gz ({exc}). Kit unchanged.")
            return 0
        extract_dir = temp_dir / "extract"
        extract_dir.mkdir()
        with tarfile.open(archive, "r:*") as tar:
            safe_extract(tar, extract_dir)
        new_tree = _locate_extracted_kit(extract_dir)
        _swap_kit_tree(new_tree)
    except (KitError, OSError, tarfile.TarError) as exc:
        # A stale kit is workable; a blocked session is not.
        print(f"warning: refresh aborted ({exc}). Kit unchanged.")
        return 0
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

    _touch_last_refresh_check(KIT_DIR)
    print(f"Kit updated to {remote_version}.")
    print("Read .cinna-kit/CHANGELOG.md — the top entry says what changed for you.")
    return 0


# --------------------------------------------------------------------------- #
# Command: export
# --------------------------------------------------------------------------- #


def is_excluded(relative: PurePosixPath, patterns: list[str]) -> bool:
    """gitignore-shaped matching for `kit.json`'s cloud_import.exclude list.

    `dir/`            excludes that directory wherever it appears
    `outer/inner/`    excludes that path, anchored at the agent root
    `docs/notes.md`   anchored at the agent root
    `AGENTS.md`, `*.pyc`   matched against the file name at any depth
    """
    parts = relative.parts
    ancestors = parts[:-1]  # directory components only — never the file itself
    text = relative.as_posix()
    for pattern in patterns:
        cleaned = pattern[2:] if pattern.startswith("./") else pattern
        if cleaned.endswith("/"):
            directory = cleaned.strip("/")
            if not directory:
                continue
            if "/" in directory:
                if text == directory or text.startswith(f"{directory}/"):
                    return True
            elif directory in ancestors:
                return True
            continue
        if "/" in cleaned:
            if fnmatch.fnmatch(text, cleaned) or text.startswith(f"{cleaned}/"):
                return True
        elif fnmatch.fnmatch(parts[-1], cleaned):
            return True
    return False


def cmd_export(args: argparse.Namespace) -> int:
    source = Path(args.path).expanduser().resolve()
    destination = Path(args.to).expanduser().resolve()
    if not (source / MANIFEST_NAME).is_file():
        raise KitError(f"{source} has no {MANIFEST_NAME} — not an agent folder")
    if destination == source or source in destination.parents:
        raise KitError("--to must point outside the agent folder")
    if destination in source.parents:
        raise KitError(
            f"--to must not be a folder that contains the agent ({destination}) — "
            "the export would scatter its files next to it"
        )

    # This tree is what gets pushed to the platform, so the §10 secret gate has to
    # sit here and not only on a `validate` the user may never have run.
    report = validate_agent(source, fix=False, cloud_ready=True)
    if report.errors and not args.force:
        for message in report.errors:
            print(f"  ERROR  {message}", file=sys.stderr)
        raise KitError(
            "the agent does not validate — fix the errors above before exporting "
            "(or pass --force if you know what you are doing)"
        )

    patterns = cloud_import_excludes()
    patterns += [pattern for pattern in ALWAYS_EXCLUDE if pattern not in patterns]
    destination.mkdir(parents=True, exist_ok=True)
    if any(destination.iterdir()):
        print(f"note: {destination} is not empty — merging the export into it.")

    copied = 0
    excluded: list[str] = []
    for path in iter_files(source):
        relative = PurePosixPath(path.relative_to(source).as_posix())
        if is_excluded(relative, patterns) or is_env_filename(path.name):
            excluded.append(relative.as_posix())
            continue
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        copied += 1

    pyproject = source / "pyproject.toml"
    if pyproject.is_file():
        dependencies, _ = read_pyproject_dependencies(pyproject)
        write_workspace_requirements(destination / "workspace_requirements.txt", dependencies)

    manifest_path = destination / MANIFEST_NAME
    if not manifest_path.is_file():
        raise KitError(
            f"{MANIFEST_NAME} was not copied — check kit.json cloud_import.exclude"
        )
    manifest = read_json(manifest_path)
    if isinstance(manifest, dict):
        manifest["cloud"] = {"platform_url": None, "agent_id": None, "imported_at": None}
        manifest_path.write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )

    print(f"Exported {source.name} -> {destination}")
    print(f"  {copied} file(s) copied, {len(excluded)} left behind:")
    for relative in excluded:
        print(f"    - {relative}")
    print("  workspace_requirements.txt regenerated, `cloud` block cleared")
    print("  credentials/ and every .env file are never copied")
    if report.warnings:
        print(f"  {len(report.warnings)} validation warning(s) — run `kit.py validate` to see them")
    return 0


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kit.py",
        description="Scaffold, validate and export locally built Cinna agents.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    new_parser = subparsers.add_parser("new", help="scaffold Local/<slug>/ from the kit template")
    new_parser.add_argument("slug", help="folder name and cloud reference, ^[a-z0-9][a-z0-9-]{1,62}$")
    new_parser.add_argument("--name", help="display name (defaults to a title-cased slug)")
    new_parser.add_argument("--root", help="workshop root that holds Local/ and Cloud/")
    new_parser.set_defaults(func=cmd_new)

    validate_parser = subparsers.add_parser("validate", help="check an agent is coherent and cloud-ready")
    validate_parser.add_argument("path", help="path to the agent folder")
    validate_parser.add_argument(
        "--fix", action="store_true", help="regenerate what can be regenerated safely"
    )
    validate_parser.add_argument("--json", action="store_true", help="machine-readable report")
    validate_parser.add_argument(
        "--cloud-ready",
        action="store_true",
        help="apply the go-cloud gate: cloud-readiness advice becomes blocking",
    )
    validate_parser.set_defaults(func=cmd_validate)

    list_parser = subparsers.add_parser("list", help="table of local agents and their ladder rungs")
    list_parser.add_argument("--root", help="workshop root that holds Local/ and Cloud/")
    list_parser.set_defaults(func=cmd_list)

    refresh_parser = subparsers.add_parser("refresh", help="compare / update the kit from the platform")
    refresh_parser.add_argument(
        "--check", action="store_true", help="only report whether an update exists"
    )
    refresh_parser.set_defaults(func=cmd_refresh)

    export_parser = subparsers.add_parser("export", help="produce the cloud-import tree")
    export_parser.add_argument("path", help="path to the agent folder")
    export_parser.add_argument("--to", required=True, help="destination directory")
    export_parser.add_argument(
        "--force",
        action="store_true",
        help="export even though the agent has validation errors",
    )
    export_parser.set_defaults(func=cmd_export)

    return parser


def main(argv: list[str] | None = None) -> int:
    if sys.version_info < (3, 10):
        found = ".".join(str(part) for part in sys.version_info[:3])
        print(
            f"kit.py needs Python 3.10 or newer; this interpreter is {found}.\n"
            "Run it through uv instead — uv provisions a compatible Python by itself:\n"
            "  uv run .cinna-kit/tools/kit.py <command> …\n"
            "No uv yet? Install it with one of:\n"
            "  curl -LsSf https://astral.sh/uv/install.sh | sh   # macOS / Linux\n"
            "  brew install uv                                   # Homebrew\n"
            "  powershell -ExecutionPolicy ByPass -c \"irm https://astral.sh/uv/install.ps1 | iex\"  # Windows\n"
            "then open a new shell (or add ~/.local/bin to PATH) and retry.",
            file=sys.stderr,
        )
        return 1
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KitError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
