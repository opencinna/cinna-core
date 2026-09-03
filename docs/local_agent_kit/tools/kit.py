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
  new <slug> [--name N] [--description S] [--root DIR] [--json]
                                       scaffold Local/<slug>/ from templates/agent/
  validate <path> [--fix] [--json] [--cloud-ready]
                                       check an agent is coherent and cloud-ready
  list [--root DIR]                    table of local agents and their ladder rungs
  refresh [--check]                    compare / update the kit from the platform
  export <path> --to DIR [--force] [--hash]
                                       produce the cloud-import tree
  chat <path> "<prompt>"               send one prompt to the agent through Cinna
                                       Desktop's local API, and print its answer

Exit codes: 1 when the command failed or `validate` found errors, 0 otherwise
(warnings never fail a run; neither does an unreachable platform on `refresh`).

Never prints a credential value. Findings name the offending file or key only.
"""

from __future__ import annotations

import argparse
import fnmatch
import functools
import hashlib
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
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

# --------------------------------------------------------------------------- #
# Kit layout
# --------------------------------------------------------------------------- #

KIT_DIR = Path(__file__).resolve().parent.parent
KIT_JSON_PATH = KIT_DIR / "kit.json"
LAYOUT_PATH = KIT_DIR / "layout.json"
SCHEMA_PATH = KIT_DIR / "schema" / "cinna-agent.schema.json"
TEMPLATE_DIR = KIT_DIR / "templates" / "agent"
VERSION_PATH = KIT_DIR / "VERSION"
CONTRACT_VERSION_PATH = KIT_DIR / "CONTRACT_VERSION"
LAST_REFRESH_CHECK = ".last_refresh_check"

MANIFEST_NAME = "cinna-agent.json"

# The three workshop directories, as the CONTRACT declares them (`layout.json`
# `workshop`). Like `DESKTOP_STATE_FILE` these are FALLBACKS for a kit whose
# contract cannot be read, not a second source of truth: `workshop_agents_dir()`
# and friends read the contract first. A folder name two hosts must agree on
# lives in contract data, never in a string literal at each use site — which is
# what `cmd_list` and `cmd_new` used to hold.
DEFAULT_KIT_DIR = ".cinna-kit"
DEFAULT_AGENTS_DIR = "Local"
DEFAULT_CLOUD_DIR = "Cloud"

# The cinna-cli account workspace, as IT lays itself out. Deliberately NOT in
# `layout.json`: that contract describes the folder model this kit and the
# desktop share, and `.cinna/account.json` plus `agents/` belong to a third tool
# that does not read it. Naming them here as constants of `cmd_list` says which
# host owns the shape; hoisting them into `layout.json` would claim an authority
# over cinna-cli's layout that this contract does not have.
CLOUD_ACCOUNT_DIR = ".cinna"
CLOUD_ACCOUNT_FILE = "account.json"
CLOUD_WORKSPACE_AGENTS_DIR = "agents"

# The publication ledger, a SIBLING of the manifest and never a key inside it.
# Every host hashes `cinna-agent.json` — it is in neither exclude list — so a
# `content_hash` of the exported tree stored in the manifest would be a value
# inside the file it is a hash of: writing it changes the bytes it describes, so
# the next scan disagrees with it and the folder reads "unpublished changes"
# the instant a publish SUCCEEDS. The ledger is also mutable per-instance state,
# which is not what a definitional manifest is for. Excluded from cloud import
# (`layout.json` names it as the plain string `publications.json`, root-anchored
# and deliberately unpaired), so it never travels and never needs clearing.
PUBLICATIONS_NAME = "publications.json"

# The desktop-owned state file, as the CONTRACT declares it (`layout.json`
# `desktop_owned`, D3). This constant is the FALLBACK for a kit that predates
# that block, not a second source of truth: `desktop_state_file()` reads the
# contract first, exactly as `agent_command_catalog()` does for the `agent`
# block. R4's rule — a filename two hosts must agree on lives in contract data,
# never in two hard-coded strings — is why the accessor, not this literal, is
# what `chat` calls.
DESKTOP_STATE_FILE = "app-data/desktop.json"

# The two keys D3 freezes inside that file, and the optional third. A tool reads
# these and no others; it never writes the file, never commits it, and never
# prints its contents — `api_base_url` included, which is why no error message
# on the `chat` path echoes the URL it called.
DESKTOP_BASE_URL_KEY = "api_base_url"
DESKTOP_TOKEN_KEY = "agent_token"
DESKTOP_CHAT_PATH_KEY = "chat_path"

# PROVISIONAL, deliberately, and recorded here as well as at `cmd_chat` so it
# cannot calcify by being forgotten. The desktop has not built the chat endpoint
# yet (D10), so the default path and the response-key preference order are a
# seam that lets them build it without a second round-trip to us. Neither is
# settled contract. When the endpoint ships, `chat_path` becomes a value the
# desktop declares in `desktop.json` and this tuple collapses to the one key it
# actually emits. Nothing else in this file may depend on either.
DEFAULT_CHAT_PATH = "/chat"
CHAT_TEXT_KEYS = ("text", "content", "delta")
# Only for a `"type": "error"` line, where the message may not be under a text
# key at all. Same seam, same impermanence.
CHAT_ERROR_TEXT_KEYS = CHAT_TEXT_KEYS + ("message", "error")
# How many distinct response-key names the "no recognised text key" diagnostic
# will name before it says so. Enough to identify the shape, bounded so a long
# stream of control frames cannot flood stderr.
SEEN_KEY_LIMIT = 12
# Per-read socket timeout for the chat call. Generous because the gap between
# two NDJSON lines is the agent thinking, not a stalled connection; finite
# because a wedged desktop must not hang a test session forever.
CHAT_TIMEOUT = 300

# The scaffold placeholders `new` fills, shared with every other host that
# scaffolds from `templates/agent/` (the desktop's `MANIFEST_TOKENS`). They are
# UPPER_SNAKE like the platform-render tokens, so the two classes cannot be told
# apart by shape — and this tuple does not tell them apart either. A list cannot
# classify a member it contains, and this one contains `KIT_VERSION`. What
# settles the question is PROVENANCE, and that is the next sentence.
# `KIT_VERSION` is in both sets on purpose: the platform fills it when it renders
# a kit for download, and `new` fills it when the kit was never rendered.
#
# Treat this comment as the upstream authority rather than as a note in passing.
# The shipped documentation had drifted away from it, and two doc files have been
# corrected back toward what it says; the desktop's `MANIFEST_TOKENS` enumerates
# `KIT_VERSION` because our contract documentation told them to. The provenance
# rule above is what those files are derived from, so an edit here is an edit to
# them.
SCAFFOLD_TOKENS = (
    "SLUG",
    "NAME",
    "DESCRIPTION",
    "ID",
    "CONTRACT_VERSION",
    "KIT_VERSION",
    "CREATED_AT",
)

# What the DESCRIPTION token becomes when `new` is run without `--description`,
# and therefore what `validate` recognises as "still the scaffold placeholder".
# The template carries the token, so this constant — not the template — is the
# text the K2 check compares against.
DEFAULT_DESCRIPTION = (
    "One sentence describing exactly what this agent does. "
    "Rewrite this last, from what you actually built."
)

# --------------------------------------------------------------------------- #
# Manifest contract (subset of schema/cinna-agent.schema.json)
# --------------------------------------------------------------------------- #

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
ENV_PREFIX_RE = re.compile(r"^[A-Z][A-Z0-9_]*_$")
CRON_RE = re.compile(r"^\S+\s+\S+\s+\S+\s+\S+\s+\S+$")
UNRENDERED_TOKEN_RE = re.compile(r"^\{\{.*\}\}$")

# Identity shapes. Character-identical to the contract's schema and to the
# desktop's `src/shared/kit/manifest.ts` / `src/shared/kit/contractVersion.ts`,
# so no two hosts can disagree about what a well-formed value looks like.
SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-([0-9A-Za-z-.]+))?$")
UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
# The shapes an API key takes. `runtime.credential` is a reference, never one of
# these; a value here has already been written to a file that travels.
SECRET_LOOKALIKE_RE = re.compile(r"^(sk-|sk_|ghp_|gho_|xox[baprs]-|AIza|AKIA)")
RUN_REFERENCE_RE = re.compile(r"^/run:([A-Za-z0-9][A-Za-z0-9_-]*)$")

# Command names have TWO thresholds on purpose, and the split is the whole point.
# The first is the desktop's pattern: a name it cannot turn into a `/run:` command
# is broken everywhere, so that is the error. The second is this kit's house
# style, which is stricter — a catalog entry named `Status`, or forty characters
# long, is perfectly runnable and only unconventional, so it is a warning. Making
# the stricter one the error was a false-positive generator: `kit.py` refused
# folders the desktop runs happily.
COMMAND_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
COMMAND_NAME_CONVENTION_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")

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
# Offline fallback for `scaffold_ignore_files()`. It MUST stay content-identical
# to `layout.json`'s `scaffold_ignore_files.agent` — same pairs, same order —
# for the same reason `DEFAULT_EXCLUDES` must: a fallback that diverges makes two
# hosts scaffold different trees from the same template.
DEFAULT_SCAFFOLD_IGNORE_FILES = [
    ["gitignore", ".gitignore"],                              # the agent root
    ["app-data/cache/gitignore", "app-data/cache/.gitignore"],
]

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

# Every dotenv shape, including `.env.example`. This is the WIDE reading, and it
# is the right one for `validate`: an `.env.example` sitting outside credentials/
# is still misplaced, whether or not it holds a value. What may travel is a
# different question, answered by the contract — see `is_secret_filename`.
def is_env_filename(name: str) -> bool:
    return name == ".env" or name.endswith(".env") or name.startswith(".env.")


# Offline fallback for `secret_file_rules()`. Like DEFAULT_EXCLUDES, it must stay
# content-identical to the shipped `layout.json` `secret_files.rules`.
DEFAULT_SECRET_FILE_RULES = [
    {
        "id": "dotenv",
        "description": "Every dotenv shape: `.env`, `.env.<suffix>` and `<name>.env`.",
        "match": {
            "basename_equals": [".env"],
            "basename_prefix": [".env."],
            "basename_suffix": [".env"],
        },
        "unless": {"basename_suffix": [".example", ".sample", ".template"]},
    }
]

# The clause vocabulary this build understands. A clause key that is missing from
# here is a newer contract talking to an older tool.
_SECRET_CLAUSE_TESTS = {
    "basename_equals": lambda name, value: name == value,
    "basename_prefix": lambda name, value: name.startswith(value),
    "basename_suffix": lambda name, value: name.endswith(value),
}


# Names that are almost always key material. Reported, never opened.
SECRET_FILENAMES = ("credentials.json", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519")
SECRET_GLOBS = ("*.pem", "*.p12", "*.pfx")

# Applied on export even if `layout.json` says otherwise — secrets and VCS state
# never travel to the cloud. Every pattern here is already covered by the shipped
# `cloud_import_excludes`, so the append is provably a no-op against an untampered
# contract; it only bites when the contract on disk has been edited or truncated.
# Keep it that way: a pattern that *adds* something would make the exported tree
# a different set from the hashed one, and the hash is what tells a host whether
# the cloud copy is current.
ALWAYS_EXCLUDE = (
    "credentials/",
    ".git/",
    ".venv/",
    "**/.env",
    "**/*.env",
    "**/credentials.json",
)

# Offline fallback for `cloud_import_excludes()`. It MUST stay content-identical
# to `layout.json`'s `cloud_import_excludes` — same patterns, same order. A
# fallback that diverges from the contract is a silent hash-parity break: the two
# hosts would hash different file sets and the drift indicator would never clear.
DEFAULT_EXCLUDES = [
    ".git/",
    "**/.git/",
    ".gitignore",
    "**/.gitignore",
    ".gitattributes",
    ".gitkeep",
    "**/.gitkeep",
    "AGENTS.md",
    "CLAUDE.md",
    "README.md",
    "Makefile",
    "publications.json",
    ".claude/",
    ".cursor/",
    ".vscode/",
    ".idea/",
    "app-data/",
    "temp/",
    "credentials/",
    "credentials.json",
    "**/credentials.json",
    "credentials/.env",
    "**/.env",
    "**/.env.local",
    "**/*.env",
    "**/*.pem",
    "**/*.key",
    "**/*.p12",
    "**/*.tmp",
    "**/*.pyc",
    "__pycache__/",
    "**/__pycache__/",
    ".mypy_cache/",
    "**/.mypy_cache/",
    ".ruff_cache/",
    "**/.ruff_cache/",
    ".venv/",
    "venv/",
    "node_modules/",
    "**/.DS_Store",
    "**/Thumbs.db",
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


def scaffold_token(name: str) -> str:
    """`{{` + name + `}}` — the literal placeholder for a token name.

    Built rather than written out, because `kit.py` is itself served through the
    platform renderer: a literal double-brace UPPER_SNAKE sequence in this file
    is indistinguishable from a platform placeholder, and would be substituted
    (or flagged as an unrendered one) on its way to the user.
    """
    return "{{" + name + "}}"


def read_text(path: Path) -> str:
    """Read a PROSE file, tolerating undecodable bytes.

    Lossy on purpose, and only safe because every caller here reads: a README, a
    Makefile, a prompt, a catalog, a version file. Substituting U+FFFD for a byte
    a scanner was going to grep anyway loses nothing recoverable.

    It is NOT the reader for anything this tool writes back — see `read_json`.
    """
    return path.read_text(encoding="utf-8", errors="replace")


def read_json(path: Path) -> object:
    """Parse a JSON file, decoding STRICTLY.

    Strict because JSON is defined to be UTF-8 (RFC 8259 §8.1), and because every
    JSON file this tool reads is one it may write back: `cinna-agent.json` and
    `publications.json` both make a round trip. Reading them with `read_text`'s
    replacement policy silently rewrote a `platform_url` of `https://café…` as
    `https://caf�…` — and since `platform_url` is the ledger's dedupe key,
    the next migration then added a SECOND entry for the same instance.

    It also made `read_publications`' third state unreachable: undecodable bytes
    never raised, so "this build cannot read the ledger" degraded into "the ledger
    parsed, with different bytes in it" — the exact conflation that function's
    docstring calls a data-loss bug.

    The failure is surfaced as `JSONDecodeError` rather than `UnicodeDecodeError`
    so that every existing handler catches it unchanged and reports it where it
    belongs. That is not a disguise: a file that is not UTF-8 is not valid JSON.
    """
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise json.JSONDecodeError(
            f"the file is not valid UTF-8 ({exc.reason} at byte {exc.start})", "", 0
        ) from None
    return json.loads(text)


def _serialise(document: object) -> str:
    """The one serialisation every host agrees on.

    2-space indent, one trailing newline, insertion order kept (no `sort_keys`),
    non-ASCII raw — matching JavaScript's `JSON.stringify(x, null, 2)`. Key
    order is part of the byte-identity contract with the desktop, so every
    writer in this file goes through here rather than calling `json.dumps`
    with its own arguments.
    """
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


def read_publications(agent_dir: Path) -> list[dict] | None:
    """Ledger entries; `[]` when there is no ledger; `None` when this build
    cannot read the one that is there.

    The three states are deliberately distinct, and conflating the last two is a
    data-loss bug: a caller that read an unparseable ledger as "empty" would
    overwrite it with whatever it migrated. `None` is also returned when a single
    entry is not an object, because silently dropping it on the next write is the
    same loss one row down.

    Never raises: `validate` is what reports a malformed ledger, and a reader
    that crashed here would take `list` and `new` down with it.
    """
    path = agent_dir / PUBLICATIONS_NAME
    if not path.is_file():
        return []
    try:
        document = read_json(path)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(document, dict):
        return None
    entries = document.get("publications")
    if entries is None:
        return []
    if not isinstance(entries, list):
        return None
    if any(not isinstance(entry, dict) for entry in entries):
        return None
    return list(entries)


# `publications.schema.json`'s per-entry rules, as data. Both the migration and
# `validate` read them from here, so the question "will this entry move?" and the
# question "is this entry valid?" can never be answered differently.
LEDGER_REQUIRED_KEYS = ("platform_url", "agent_id")
LEDGER_OPTIONAL_STRING_KEYS = (
    "workspace",
    "imported_at",
    "updated_at",
    "contract_version",
    "content_hash",
)


def ledger_entry_is_placeable(entry: object) -> bool:
    """Can this entry be written into `publications.json` exactly as it stands?

    The guarantee this predicate exists to provide: **this tool never writes a
    `publications.json` that its own `_validate_publications` then rejects.** A
    migration that produced a file the very next `validate` called broken — and
    that `cmd_export` then refused to export — would be a tool blaming a user for
    a file the tool had just written.

    The shape that broke it was a PARTIALLY populated legacy `cloud` block — a
    real `platform_url` string with `agent_id` null or absent — which the old
    migration absorbed verbatim into `"agent_id": null`, a value the schema
    (`required`, `type: "string"`, `minLength: 1`) and both validators reject.
    Measured, and worth recording precisely: a `cloud` block whose `platform_url`
    is ALSO null never reached the ledger at all, so it was the data-loss defect
    rather than this one. Nothing writes the partially populated shape today,
    which makes this latent rather than live — but the guard is right on the
    merits: `platform_url` was checked and its required partner `agent_id` was
    not, and the optional fields were not checked at all (`imported_at: 123`
    produced a rejected ledger by the same route).

    Refusing to place such an entry is chosen over repairing it because there is
    no repair: `agent_id` cannot be invented, and dropping the entry to make the
    file valid would discard the `platform_url` record — the unrecoverable
    direction. Left in the manifest the data is intact, `validate` names it, and
    the user can complete it.
    """
    if not isinstance(entry, dict):
        return False
    for key in LEDGER_REQUIRED_KEYS:
        value = entry.get(key)
        if not isinstance(value, str) or not value.strip():
            return False
    for key in LEDGER_OPTIONAL_STRING_KEYS:
        value = entry.get(key)
        if value is not None and not isinstance(value, str):
            return False
    return True


def is_published(agent_dir: Path, manifest: dict) -> bool:
    """Whether this folder has been published anywhere, in either shape.

    Reads the ledger FIRST and the deprecated `cloud` stamp second. Both are
    consulted because a migration (`write_manifest`) moves the answer from the
    manifest into `publications.json`, and a reader that knew only the old place
    would report a published agent as never published the moment it was
    re-stamped.
    """
    entries = read_publications(agent_dir)
    # A ledger this build cannot read still answers THIS question: the file only
    # exists because something published. Reporting "no" would be a confident
    # wrong answer where "yes" is merely imprecise, and `validate` names the
    # malformed file either way.
    if entries is None or entries:
        return True
    cloud = manifest.get("cloud")
    return isinstance(cloud, dict) and bool(cloud.get("agent_id"))


def write_manifest(agent_dir: Path, manifest: dict) -> None:
    """Write `cinna-agent.json`, migrating the legacy ledger keys on the way out.

    Migration happens HERE — at every write of the manifest — and nowhere else.
    In particular it never happens at export: an export that mutates the manifest
    is the defect class R4 removes, and it breaks byte-parity with a host that
    uploads the folder verbatim. A legacy folder exported without ever being
    re-stamped therefore carries its stale `cloud` block to the cloud. That is
    accepted: there is no secret in it, and the remedy is the re-stamp.

    Migrating moves the folder's `content_hash` exactly ONCE, which is correct —
    the folder genuinely changed, and a single visible, explainable move is the
    opposite of the perpetual drift that storing the hash inside the hashed file
    produced.

    Two keys move into `publications.json`:

    * `publications` — removed from the manifest schema by R4. A manifest still
      carrying it is reported by `validate`; this is where the report becomes
      actionable.
    * `cloud` — deprecated since contract 1.0.0, one entry's worth of the same
      information in the older shape.

    A ledger key this build cannot READ (not an object, not an array) is LEFT IN
    PLACE rather than dropped. The fail-safe direction is derived from the
    consequence, not copied from D15: discarding data a host cannot interpret is
    unrecoverable, while leaving a deprecated key is visible — `validate` names
    it on every run.

    A key this build cannot PLACE is left in place for the same reason, and that
    is the harder half. The `del`s below used to be conditional on the value's
    TYPE and never on the migration's OUTCOME, so a `cloud` block with no
    `platform_url` was deleted from the manifest while `absorb` declined to
    record it: the `agent_id` simply ceased to exist. Type is what the fail-safe
    was originally applied to; a value's *destination* is the thing that actually
    has to be reached before the source may be removed.

    Migration is therefore all-or-nothing per key. Absorbing the good half of a
    `publications` array and keeping the array for the bad half would duplicate
    the good entries across two files, so an array with one unplaceable entry
    moves nothing and stays whole.
    """
    migrated = read_publications(agent_dir)
    if migrated is None:
        # Loud, and the only alternative was silent. A ledger this build cannot
        # parse is never overwritten with what it managed to migrate, and the
        # manifest is not written either — a half-migration that reported success
        # is the failure shape this whole ruling exists to remove. `validate`
        # names the malformed file; fix it and write again.
        raise KitError(
            f"{agent_dir / PUBLICATIONS_NAME} could not be read as a publication "
            "ledger, so the manifest was not written — migrating into it would "
            "overwrite a file this tool does not understand. Run "
            "`kit.py validate` on the folder, fix the file, and retry."
        )

    # A migration may only ADD to a ledger this build would have written itself.
    # If what is already on disk holds an entry `_validate_publications` rejects,
    # appending to it and rewriting would make this tool the author of a file its
    # own validator fails. It stands down instead — the manifest keys stay, and
    # `validate` names both files.
    ledger_writable = all(ledger_entry_is_placeable(entry) for entry in migrated)

    known = {
        entry.get("platform_url")
        for entry in migrated
        if isinstance(entry.get("platform_url"), str)
    }
    ledger_changed = False

    # The caller's dict is left alone: a writer that silently emptied the object
    # it was handed would surprise every future call site.
    document = dict(manifest)

    def migrate(key: str, entries: list) -> None:
        """Move one manifest ledger key into the file — all of it, or none of it.

        The key is deleted only after every entry it holds has reached the
        ledger. Nothing is written to disk until this function has run for both
        keys, so a declined migration leaves no half-moved state anywhere.
        """
        nonlocal ledger_changed
        if not ledger_writable:
            return
        if not all(ledger_entry_is_placeable(entry) for entry in entries):
            return
        del document[key]
        for entry in entries:
            platform_url = entry["platform_url"]  # placeable ⇒ a non-blank str
            # The file already holds an entry for this instance and is the newer
            # authority: a migration adds instances, it never overwrites one.
            if platform_url in known:
                continue
            migrated.append(entry)
            known.add(platform_url)
            ledger_changed = True

    # Order matters: `publications[]` is the newer shape, so it is absorbed
    # first and a `cloud` stamp naming the same instance loses to it.
    stale = document.get("publications")
    if isinstance(stale, list):
        migrate("publications", stale)

    cloud = document.get("cloud")
    if isinstance(cloud, dict):
        migrate("cloud", [cloud])

    (agent_dir / MANIFEST_NAME).write_text(_serialise(document), encoding="utf-8")
    # Written only when something was actually absorbed, so a write that migrates
    # nothing never reformats a ledger somebody hand-edited.
    if ledger_changed:
        (agent_dir / PUBLICATIONS_NAME).write_text(
            _serialise({"publications": migrated}), encoding="utf-8"
        )


def kit_version() -> str | None:
    """The kit's own version, or None when it is absent / still a placeholder."""
    if not VERSION_PATH.is_file():
        return None
    value = read_text(VERSION_PATH).strip()
    if not value or UNRENDERED_TOKEN_RE.match(value):
        return None
    return value


def kit_config() -> dict:
    """`kit.json` — the kit's own identity and index, as data.

    **This function and `layout_config()` fail in OPPOSITE directions, on
    purpose. Do not harmonise them.** Here, a `kit.json` that cannot be parsed
    REFUSES, by name: it is the file that says which kit this is and where it
    came from, an identity has no built-in stand-in, and a broken one means a
    broken install the user has to be told about. There, `layout.json` DEGRADES
    to the built-in defaults, because every value it supplies has a correct
    fallback shipped in this file — the layout it describes is the layout this
    contract already knows. Met cold the pair reads as an inconsistency and is
    not one: **the safe direction is a property of what the file supplies, not a
    house style.** An edit that makes the two agree does not tidy anything, it
    silently reverses one of them, and the one it reverses is the one whose
    failure is a user running a kit whose identity nobody could check. Same
    shape, and the same warning, as `contract_exclude_patterns()` and
    `secret_file_rules()` further down.

    Two states are NOT that failure and both return `{}`: the file is absent, or
    it parses and is not an object. The distinction is what can be known. A file
    that parses tells us what it declares, and one declaring nothing this tool
    can use is answered exactly as one that is not there — the state
    `contract_version()`'s `CONTRACT_VERSION` fallback is scoped to. A file that
    will not parse tells us nothing at all, INCLUDING whether it declared the
    thing we came for, so falling back there would be inventing an answer rather
    than reaching for a known one.
    """
    if not KIT_JSON_PATH.is_file():
        return {}
    try:
        data = read_json(KIT_JSON_PATH)
    except (json.JSONDecodeError, OSError) as exc:
        # NAMES THE FILE, and that is the whole point of the clause. Without it
        # the parser's own message reached the top level bare — `Expecting
        # property name enclosed in double quotes: line 1 column 3` — killing
        # `kit.py new` and `kit.py validate --json` from a tool whose every other
        # contract reader refuses or degrades by name, leaving the user to guess
        # which of the kit's JSON files it meant. Safe to interpolate for the
        # reason `main()` states: a JSONDecodeError carries a position, an
        # OSError an errno and a filename, and `kit.json` holds no secret.
        raise KitError(
            f"{KIT_JSON_PATH} could not be read as JSON ({exc}). It is installed "
            "by the kit rather than hand-edited, and `kit.py refresh` reads it "
            "too, so refresh cannot repair it — re-install the kit from your "
            "Cinna instance."
        ) from None
    return data if isinstance(data, dict) else {}


def layout_config() -> dict:
    """`layout.json` — the folder model as data, shared with every other host.

    Never raises: a contract that cannot be read degrades to the built-in
    defaults rather than stopping the command, exactly as the desktop's
    `parseLayout` degrades to an empty layout.

    **This function and `kit_config()` fail in OPPOSITE directions, on purpose.
    Do not harmonise them.** Every value this file supplies has a correct
    built-in fallback in this tool, so degrading costs nothing that was not
    already known; `kit.json` supplies the kit's identity, which has no stand-in,
    so it refuses and names the file instead. The direction follows from what
    each file supplies. The note there says this from the other side, and the
    pair is written down twice deliberately: an asymmetry explained at only one
    of its two ends is one a reader still meets cold at the other, and this
    project has the scar to prove it.
    """
    if not LAYOUT_PATH.is_file():
        return {}
    try:
        data = read_json(LAYOUT_PATH)
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def contract_exclude_patterns() -> list[str] | None:
    """`layout.json` `cloud_import_excludes` as the CONTRACT states it, or None
    when this build cannot evaluate the contract's own list.

    Two callers want different things from the same data and must not share an
    answer. What TRAVELS may fall back to `DEFAULT_EXCLUDES` (below). What is
    HASHED may not: an unevaluable exclude list means the file set the hash
    covers is unevaluable, and the rule for that is absolute — unevaluable ⇒
    refuse to emit a `content_hash` at all, never emit one computed a different
    way. A missing drift number is visible and recoverable; a plausible wrong one
    is neither.

    "But the fallback is the same list" is a claim about THIS build's contract,
    not about the contract sitting in the folder being read — which is the only
    one that matters, because the other host is reading that one. It is exactly
    why the refusal cannot be argued away from here.

    A list holding an entry that is not a usable string returns None too, rather
    than the usable subset. A silently shortened exclude list is a narrower
    secret gate reported as a healthy one, and it is the same shape we are asking
    the desktop to remove from `asStringArray` (`src/main/kit/layout.ts`). Whole
    or nothing, as `scaffold_ignore_files()` already does with its pairs.

    **This function and `secret_file_rules()` fail in OPPOSITE directions, on
    purpose. Do not harmonise them.** There, a contract rule this build cannot
    evaluate withholds the FILE, because a leaked credential is unrecoverable.
    Here, an exclude list this build cannot evaluate withholds the
    `content_hash`, because a confidently wrong hash is worse than no hash. Met
    cold the pair reads as an inconsistency and is not one: **the safe direction
    is a property of the consequence, not a house style.** An edit that makes the
    two agree does not tidy anything — it silently reverses one of them, and the
    one it reverses is the one whose failure is a secret leaving the machine.
    """
    patterns = layout_config().get("cloud_import_excludes")
    if not isinstance(patterns, list) or not patterns:
        return None
    if any(not isinstance(p, str) or not p.strip() for p in patterns):
        return None
    # Stray surrounding whitespace is stripped and SAID OUT LOUD. Silent
    # acceptance is the one option that is neither of the two sanctioned fixes:
    # a pattern read differently from how it was written, with nothing telling
    # the author. cinna-cli's `_clean_patterns` does the same, deliberately —
    # both hosts hash the file set these patterns select, so a difference of one
    # file makes the two hashes disagree forever.
    cleaned = []
    for pattern in patterns:
        stripped = pattern.strip()
        if stripped != pattern:
            print(
                f"warning: layout.json: exclude pattern {pattern!r} has stray "
                f"whitespace - reading it as {stripped!r}.",
                file=sys.stderr,
            )
        cleaned.append(stripped)
    return cleaned


def cloud_import_excludes() -> list[str]:
    """The one exclude list, for deciding what travels.

    Falls back to `DEFAULT_EXCLUDES` when the contract is missing or unusable —
    and that fallback is content-identical to the shipped list, so a folder still
    exports with the right files left behind. Callers that go on to EMIT a
    `content_hash` must ask `contract_exclude_patterns()` instead and honour a
    None; this function cannot tell them the difference, which is why it is not
    the function `cmd_export` hashes from.

    It has no caller in this file today, for that exact reason — `cmd_export` is
    the only consumer and it needs the tristate. It stays because it is the
    accessor the contract decisions name, and because the next consumer that does
    not hash must find the fallback here rather than re-deriving it.
    """
    patterns = contract_exclude_patterns()
    return patterns if patterns is not None else list(DEFAULT_EXCLUDES)


def contract_version() -> str | None:
    """The folder contract this kit implements, or None when it is unavailable.

    `kit.json` is the primary read because that is the file the platform renders
    and `refresh` swaps, and because it is half the identity pair a consumer
    detects a contract tree by (D2). It travels in the contract tarball as well
    as in the full kit — `INDEX_MEMBER` is one of `CONTRACT_MEMBERS` in
    `backend/app/services/cli/local_agent_kit_service.py`, and the packer's
    coherence guard refuses to serve an archive without it. The file the contract
    tarball genuinely omits is `VERSION`, which carries the KIT version and not
    this one. **Do not justify the fallback below by claiming the tarball lacks
    `kit.json`** — an earlier revision of this docstring did, which is a false
    statement about the contract's membership shipping inside the contract.

    So the `CONTRACT_VERSION` fallback covers something narrower, and still worth
    having: a `.cinna-kit/` whose `kit.json` is absent, or present without a
    usable `contract_version` (key missing, not a string, or blank). The serving
    path 503s on exactly those states — the guard above requires `CONTRACT_VERSION`,
    `kit.json` and `layout.json` to be present, parseable, and to declare the same
    version — so a tree the platform served should never reach the fallback, while
    a hand-assembled or half-refreshed one can. D2 puts the DESKTOP's fallback on
    this same file, so both hosts answer the version question from the same two
    files in the same order rather than each picking a second source.

    What it does NOT cover, said plainly because the claim is easy to over-state:
    a `kit.json` that is present but malformed raises out of `kit_config()` and
    never arrives here.
    """
    value = kit_config().get("contract_version")
    if isinstance(value, str) and value.strip():
        return value.strip()
    if CONTRACT_VERSION_PATH.is_file():
        try:
            text = read_text(CONTRACT_VERSION_PATH).strip()
        except OSError:
            return None
        if text and not UNRENDERED_TOKEN_RE.match(text):
            return text
    return None


def _layout_workshop_name(key: str, fallback: str) -> str:
    """One declared workshop directory name from `layout.json` `workshop`.

    Falls back to the built-in name, which is the same name this contract ships,
    and the direction is derived rather than borrowed from `desktop_state_file()`
    next door — that one refuses. What is at stake here is which folder `list`
    enumerates and `new` scaffolds into. Guessing wrong costs an empty listing or
    a folder in the wrong place, both immediately visible and both fixed by
    moving a directory; refusing costs every verb in the tool for a contract that
    is merely unreadable. Nothing secret and no hash rides on it, so the loud
    option buys nothing the fallback does not.
    """
    workshop = layout_config().get("workshop")
    if isinstance(workshop, dict):
        value = workshop.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return fallback


def workshop_kit_dir() -> str:
    return _layout_workshop_name("kit_dir", DEFAULT_KIT_DIR)


def workshop_agents_dir() -> str:
    return _layout_workshop_name("agents_dir", DEFAULT_AGENTS_DIR)


def workshop_cloud_dir() -> str:
    return _layout_workshop_name("cloud_dir", DEFAULT_CLOUD_DIR)


def _layout_agent_path(key: str, fallback: str) -> str:
    """One declared agent-relative path from `layout.json` `agent`, or its fallback."""
    agent = layout_config().get("agent")
    if isinstance(agent, dict):
        value = agent.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return fallback


def agent_command_catalog() -> str:
    return _layout_agent_path("command_catalog", "docs/CLI_COMMANDS.yaml")


def agent_status_file() -> str:
    return _layout_agent_path("status_file", "app-data/storage/STATUS.md")


def desktop_state_file() -> str | None:
    """Agent-relative path of the desktop-owned state file, per `layout.json`.

    Tristate, and the third state is the whole point:

    * the contract declares it .............. that path;
    * `layout.json` carries no `desktop_owned`
      block at all .......................... `DESKTOP_STATE_FILE`, because a kit
      from before the block is a kit from before the file could have moved, and
      the built-in value is what that contract said;
    * the block is THERE and this build
      cannot read it ........................ None, and the caller refuses.

    The third state's direction is DERIVED, not copied from a neighbour. Two
    can't-evaluate answers already live in this file and point opposite ways on
    purpose — `contract_exclude_patterns` withholds a hash, and
    `secret_file_rules` (with `is_secret_filename`, which acts on what it returns)
    withholds a file — and those two contract readers carry cross-reference notes
    naming each other and saying not to harmonise them. Here:
    guessing means reading a file the contract no longer names and POSTing a
    bearer token to an address found inside it; refusing means one loud line and
    a `kit.py refresh`. Loud and recoverable wins — and refusing still honours
    the rule that matters most for this verb, which is that it never falls back
    to role-play.

    Which entry, when a future contract lists several, is decided by the
    CONTRACT rather than by position: the one whose `contract_keys` declares
    both frozen keys (D3). Ambiguous, or any entry unreadable, is the third
    state — whole-or-nothing, as `scaffold_ignore_files()` already is, because
    quietly skipping the entry we could not parse is how the wrong file gets
    picked with no one noticing.

    A bare string entry is accepted as well as D3's object form. That is not
    laxity: it is the same string-or-object tolerance we are asking the desktop
    to add to `parseLayout` (`src/main/kit/layout.ts:228`, §0 Bucket 5a), and
    reading both shapes here is what lets either host ship first.
    """
    layout = layout_config()
    if "desktop_owned" not in layout:
        return DESKTOP_STATE_FILE
    entries = layout.get("desktop_owned")
    if not isinstance(entries, list) or not entries:
        return None

    every: list[str] = []
    declares_the_frozen_keys: list[str] = []
    for entry in entries:
        keys: object = None
        if isinstance(entry, str):
            raw_path: object = entry
        elif isinstance(entry, dict):
            raw_path = entry.get("path")
            keys = entry.get("contract_keys")
        else:
            return None
        if not isinstance(raw_path, str) or not raw_path.strip():
            return None
        relative = normalize_rel_path(raw_path.strip())
        if not relative:
            return None
        every.append(relative)
        if (
            isinstance(keys, dict)
            and DESKTOP_BASE_URL_KEY in keys
            and DESKTOP_TOKEN_KEY in keys
        ):
            declares_the_frozen_keys.append(relative)

    if len(declares_the_frozen_keys) == 1:
        return declares_the_frozen_keys[0]
    if not declares_the_frozen_keys and len(every) == 1:
        return every[0]
    return None


# --------------------------------------------------------------------------- #
# The contract-version gate
# --------------------------------------------------------------------------- #


def parse_semver(value: object) -> tuple[int, int, int, str | None, str] | None:
    """Parse a semantic version; None for anything that is not one.

    A port of the desktop's `parseSemver` (`src/shared/kit/contractVersion.ts`),
    pattern included and identical to the schema's, so the two hosts cannot
    disagree about which strings parse.
    """
    if not isinstance(value, str):
        return None
    match = SEMVER_RE.match(value.strip())
    if not match:
        return None
    return (
        int(match.group(1)),
        int(match.group(2)),
        int(match.group(3)),
        match.group(4),
        value.strip(),
    )


def check_contract_compatibility(
    agent_version: object, tool_version: object
) -> tuple[str, str]:
    """May a tool bundling `tool_version` operate a folder recording `agent_version`?

    A port of the desktop's `checkContractCompatibility`. Returns
    `(status, reason)` where status is one of `ok`, `app_too_old`, `migratable`,
    `unknown`, and reason is one sentence a UI can show as written.

    **Only the major version decides.** A minor or patch difference is not a
    compatibility question at all: the contract's minor releases are additive by
    definition, so a folder and a tool that disagree there still understand each
    other. The remedy differs by direction — a folder from a newer major needs a
    newer tool, a folder from an older major needs migrating — which is why the
    two are not one symmetric "mismatch".
    """
    agent = parse_semver(agent_version)
    tool = parse_semver(tool_version)

    if agent is None:
        return (
            "unknown",
            "this folder does not record a usable `contract_version`. It was created "
            "before contract 1.0.0 and should be re-stamped.",
        )
    if tool is None:
        # Divergence from the desktop, deliberate: they report an unusable *tool*
        # version through `manifest.contract_version.invalid`, which blames the
        # folder for the host's problem. The folder parsed; the kit is what cannot
        # answer, so this is information about the kit, not a defect in the agent.
        return (
            "unknown",
            f"this folder records contract {agent[4]}, and this kit records no usable "
            "contract version to compare it against — the compatibility gate did not run.",
        )
    if agent[0] == tool[0]:
        return ("ok", f"contract {agent[4]} runs on this kit's contract {tool[4]}.")
    if agent[0] > tool[0]:
        return (
            "app_too_old",
            f"this folder needs contract {agent[0]}.x and this kit has {tool[4]}. "
            "Run `kit.py refresh` to get a kit that understands it.",
        )
    return (
        "migratable",
        f"this folder was built against contract {agent[4]} and this kit is on "
        f"{tool[4]}. It can be migrated — read CHANGELOG.md's Breaking entries.",
    )


def scaffold_ignore_files() -> list[tuple[str, str]]:
    """`layout.json` `scaffold_ignore_files.agent` — the dotless→dotted renames.

    Pairs of [path in the template tree, path in the created folder]. Read from
    the contract so every host restores the same names; falls back to
    `DEFAULT_SCAFFOLD_IGNORE_FILES`, which is the same list, so an unreadable
    contract cannot quietly stop restoring the dot on `.gitignore`.

    `templates/agent/credentials/.gitignore` is deliberately absent from this
    list on both sides: it keeps its dot, because it names files no repository
    should track — including the one the kit itself lives in.
    """
    block = layout_config().get("scaffold_ignore_files")
    pairs: list[tuple[str, str]] = []
    if isinstance(block, dict):
        entries = block.get("agent")
        if isinstance(entries, list):
            for entry in entries:
                if (
                    isinstance(entry, (list, tuple))
                    and len(entry) == 2
                    and all(isinstance(part, str) and part.strip() for part in entry)
                ):
                    pairs.append((entry[0], entry[1]))
                else:
                    # An entry this tool cannot read means the contract restructured
                    # the block. Fall back whole rather than restoring a subset:
                    # a partially applied rename ships an agent whose `.gitignore`
                    # is missing, which is the one failure this list exists to stop.
                    pairs = []
                    break
    if pairs:
        return pairs
    return [(source, target) for source, target in DEFAULT_SCAFFOLD_IGNORE_FILES]


def secret_file_rules() -> list:
    """`layout.json` `secret_files.rules` — what may never travel, as data.

    Falls back to `DEFAULT_SECRET_FILE_RULES`, which is the same rule, so an
    unreadable contract cannot quietly turn the secret gate off.

    A declared list is returned **as declared**, entries this build cannot read
    included. It used to filter them out, which disabled a fail-safe one function
    away: `is_secret_filename` treats an unreadable rule as "assume it protects
    something" and withholds everything, and its docstring says so — but no
    unreadable rule could ever reach it, because this function had already
    dropped it. A shortened rule list is a NARROWER secret gate, reported as a
    healthy one, on the one list whose failure mode is a credential leaving the
    machine. Two guards on the same data, one lifted for the caller's benefit and
    lowered here: the filter goes, and the consumer's fail-safe becomes reachable.

    **This function and `contract_exclude_patterns()` fail in OPPOSITE
    directions, on purpose. Do not harmonise them.** Here, a rule this build
    cannot evaluate withholds the FILE (`is_secret_filename` treats it as
    protecting something), because a leaked credential is unrecoverable. There,
    an exclude list this build cannot evaluate withholds the `content_hash`,
    because a confidently wrong hash is worse than no hash. Met cold the pair
    reads as an inconsistency and is not one: **the safe direction is a property
    of the consequence, not a house style.** An edit that makes the two agree
    does not tidy anything — it silently reverses one of them, and reversing this
    one ships a credential to the cloud.
    """
    block = layout_config().get("secret_files")
    if isinstance(block, dict):
        rules = block.get("rules")
        if isinstance(rules, list) and rules:
            return list(rules)
    return [dict(rule) for rule in DEFAULT_SECRET_FILE_RULES]


def _secret_clause_hits(name: str, clause: object, *, on_unknown: bool) -> bool:
    """Does any test in one `match` / `unless` clause fire for this basename?

    `on_unknown` is the fail-safe direction for anything this build cannot
    evaluate, and it differs by position on purpose: an unevaluable `match`
    counts as a hit (True) and an unevaluable `unless` counts as a miss (False),
    so both resolve toward "treat the path as secret".

    "Cannot evaluate" is deliberately broad — it is not just an unknown clause
    key. A clause that is absent, is not an object, is an empty object, or whose
    values hold no usable string is equally unevaluable, and every one of those
    is a shape a future contract could introduce. Resolving any of them the other
    way turns the secret gate into a silent no-op, which is the one direction
    this rule may never fail in: `cloud_import_excludes` does not cover
    `.env.production`, `.env.prod` or `config/.env.staging`, so an off gate ships
    a credential to the cloud.

    Note the pleasant accident that makes the broad reading safe: an absent
    `unless` is unevaluable *and* means "no exception", and `on_unknown` is
    False there — so the general rule gives the right answer without a special
    case for the common shape.
    """
    if not isinstance(clause, dict) or not clause:
        return on_unknown
    for key, values in clause.items():
        test = _SECRET_CLAUSE_TESTS.get(key)
        if test is None:
            return on_unknown
        if isinstance(values, str):
            values = [values]
        usable = (
            [value for value in values if isinstance(value, str) and value]
            if isinstance(values, list)
            else []
        )
        if not usable:
            # A key we know carrying values we cannot use: same unevaluable
            # shape as an unknown key, so the same fail-safe direction.
            return on_unknown
        if any(test(name, value) for value in usable):
            return True
    return False


def is_secret_filename(name: str, rules: list | None = None) -> bool:
    """True when the contract says this basename can hold a credential value.

    The rule lives in `layout.json` `secret_files`, not here, for the same reason
    the exclude list does: a folder rule each host re-derives from prose is a
    rule the hosts will eventually disagree about, and here the disagreement is a
    secret leaving the machine. A glob list cannot express it — enumerating
    dotenv suffixes leaks the first one nobody thought of (`.env.prod`), and no
    glob can say "any `.env.<suffix>` except `.example`".

    A caller may pass `rules=[]` to mean "no secret filtering at all" — that is
    an explicit local choice, not a contract defect, and is the one way the gate
    is off. A contract that declares rules this build cannot read withholds
    everything instead, which is loud and recoverable (`kit.py refresh`) rather
    than silent and not.
    """
    if rules is None:
        rules = secret_file_rules()
    for rule in rules:
        if not isinstance(rule, dict):
            # A rule this build cannot even open is a rule it must assume
            # protects something.
            return True
        if not _secret_clause_hits(name, rule.get("match"), on_unknown=True):
            continue
        if _secret_clause_hits(name, rule.get("unless"), on_unknown=False):
            continue
        return True
    return False


def resolve_root(explicit: str | None) -> Path:
    """The workshop root that holds the agents, cloud and kit directories."""
    if explicit:
        return Path(explicit).expanduser().resolve()
    if KIT_DIR.name == workshop_kit_dir():
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


def iter_files(root: Path, on_error=None):
    """Yield every file under `root`, skipping noise directories.

    `on_error` receives the `OSError` for any directory this walk could not read.
    The default is `None`, which is `os.walk`'s own default of ignoring such
    errors, so every caller that does not pass it behaves exactly as before.

    **The parameter exists so there is ONE walker, not two.** `validate` has to
    know which directories it could not enter, and the honest answer is "the ones
    this walk skipped" — because this walk is the widest thing `validate` does
    (`_validate_secrets` runs it over the whole agent folder hunting stray key
    material). A separate scanner written to answer that question would be a
    second definition of `validate`'s scope, and the two would drift the first
    time someone adjusted `SKIP_DIRS` on one of them. Same move as making one
    function the sole writer of the manifest: a rule someone must remember
    ("keep the scan's scope equal to the walk's") becomes a property of there
    being only one walk. See `unreadable_directories` below, its only user.
    """
    for dirpath, dirnames, filenames in os.walk(root, onerror=on_error):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for filename in sorted(filenames):
            yield Path(dirpath) / filename


def unreadable_directories(root: Path) -> list[str]:
    """Root-relative directories under `root` that `iter_files` could not enter.

    `"."` means `root` itself. Returned sorted, deduplicated, POSIX-separated.

    **Why this is a THIRD condition and not the export's `unreadable[]`.** There
    are three ways this tool can be blind to part of an agent folder, and they
    are deliberately kept apart because they fail at different stages, with
    different consequences, and only two of them are about the export at all:

    1. **A file whose bytes will not read** — `hash_export_files`' list. The file
       IS in the export set and travels; per D7 the digest folds it in as
       `UNREADABLE_MARKER`, so the number is stable and comparable but does not
       describe the bytes. Settled by the HASH, over a finished file list.
    2. **A directory the export walk cannot scan** — `collect_export_tree`'s
       list. Files that should travel are ABSENT from the set entirely, so the
       upload is short a subtree and the hash covers a smaller list than any
       other host's. Settled by the WALK, before a file list exists.
       (1) and (2) cannot share one list: a single list could not be acted on
       until after the hash, which is later than the point at which (2) is
       already known to make the answer unusable, and later than `cmd_export`'s
       decision not to `mkdir`. Nor can they share one message — "the hash does
       not describe the bytes" and "files are missing from the upload with
       nothing saying so" are different facts, and one sentence cannot state
       both truthfully. Their POLICY is identical, and that is the part that must
       not diverge: refuse, and `--force` waives neither.
    3. **A directory `validate` reads that the export never touches** — this
       function. It cannot join (2), and the reason is load-bearing rather than
       incidental: (2)'s walk never descends into an EXCLUDED directory, which is
       exactly what makes an unreadable `credentials/` cost the export nothing.
       Widening it to cover `credentials/` would break that property to fix a
       problem the export does not have. But `validate` DOES read there —
       `credentials/.env.example` is a required file and `app-data/storage/`
       holds the status file — and `Path.is_file()` swallows ENOENT while
       PROPAGATING EACCES, so before this existed those two reads left the whole
       command on `main()`'s `(OSError, json.JSONDecodeError)` backstop, printing
       a bare `error: [Errno 13] Permission denied: …/credentials/.env.example`.
       That backstop turns a traceback into a tidy line; it is not a diagnosis,
       and a tidy line is the thing most easily mistaken for one.

    So: three conditions, three lists, three messages, one policy. This one lands
    on `validate` (`export` inherits it, because `cmd_export` validates first).

    Not covered here, deliberately: an entry whose TYPE cannot be stat'd. `os.walk`
    treats it as a file, so it reaches `_validate_secrets` as a name — which is all
    that check reads — and the export walk records it separately in (2).
    """
    failures: list[str] = []

    def record(error: OSError) -> None:
        filename = getattr(error, "filename", None) or str(root)
        try:
            relative = Path(filename).relative_to(root).as_posix()
        except ValueError:
            relative = str(filename)
        failures.append(relative)

    for _ in iter_files(root, on_error=record):
        pass
    return sorted(set(failures))


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


def _check_required_str(report: Report, value: object, label: str, *, max_length: int) -> str | None:
    """A required string field, reported the way the desktop reports one."""
    if value is None:
        report.error(f"{MANIFEST_NAME}: `{label}` is required.")
        return None
    if not isinstance(value, str):
        report.error(f"{MANIFEST_NAME}: `{label}` must be a string.")
        return None
    if not value.strip():
        report.error(f"{MANIFEST_NAME}: `{label}` must not be empty.")
        return None
    if len(value) > max_length:
        report.error(f"{MANIFEST_NAME}: `{label}` is longer than {max_length} characters.")
        return None
    return value


def _validate_identity(manifest: dict, report: Report) -> None:
    """`contract_version`, `id`, `created_at` — and the compatibility gate.

    This replaced the old `schema_version` gate wholesale. `schema_version` is now
    legacy: tolerated, not required, and **nothing branches on its value** — the
    contract, not an integer, decides whether this kit may operate the folder.
    """
    has_schema_version = "schema_version" in manifest
    has_contract = "contract_version" in manifest
    has_id = "id" in manifest

    # The one tolerated absence, and the reason `contract_version` and `id` are not
    # in the schema's top-level `required`: a pre-1.0.0 folder carries the integer
    # `schema_version` and neither identity key. It is read, reported and
    # re-stamped, never rejected. Skipping the rest mirrors the early return in the
    # desktop's `checkIdentity()`; without it, a legacy folder would collect two
    # "required" errors for the very keys this branch exists to excuse.
    if has_schema_version and not has_contract and not has_id:
        report.warn(
            f"{MANIFEST_NAME}: this agent predates contract 1.0.0 — it has `schema_version` "
            "but no `contract_version` or `id`. Re-stamp it so moves, renames and "
            "publications stay attached to it."
        )
        return

    contract = _check_required_str(
        report, manifest.get("contract_version"), "contract_version", max_length=64
    )
    if contract is not None:
        if parse_semver(contract) is None:
            report.error(
                f"{MANIFEST_NAME}: `contract_version` must be a semantic version like "
                f"1.0.0, got {contract!r}."
            )
        else:
            status, reason = check_contract_compatibility(contract, contract_version())
            if status == "app_too_old":
                report.error(f"{MANIFEST_NAME}: {reason}")
            elif status == "migratable":
                report.warn(f"{MANIFEST_NAME}: {reason}")
            elif status == "unknown":
                # The folder's version parsed, so only the kit's own can be missing.
                report.info(f"{MANIFEST_NAME}: {reason}")

    agent_id = _check_required_str(report, manifest.get("id"), "id", max_length=64)
    if agent_id is not None and not UUID_RE.match(agent_id):
        report.error(f"{MANIFEST_NAME}: `id` must be a UUID.")

    if has_schema_version and has_contract:
        report.info(
            f"{MANIFEST_NAME}: `schema_version` is legacy and is ignored — "
            "`contract_version` is the gate."
        )

    # Deliberately loose: the desktop does not validate `created_at` at all, so a
    # format check here would refuse folders it accepts. Type only.
    created_at = manifest.get("created_at")
    if created_at is not None and not isinstance(created_at, str):
        report.error(f"{MANIFEST_NAME}: `created_at` must be a string or null.")


def validate_manifest(manifest: dict, report: Report) -> None:
    """Validate the manifest against the subset of the schema stdlib can express."""
    _validate_identity(manifest, report)

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
        # The schema declares both bounds and the desktop enforces both; this
        # validator used to declare them and check neither.
        if len(example_prompts) > 20:
            report.error(f"{MANIFEST_NAME}: `example_prompts` holds at most 20 entries.")
        for index, prompt in enumerate(example_prompts):
            if not isinstance(prompt, str) or not prompt.strip():
                report.error(f"{MANIFEST_NAME}: `example_prompts[{index}]` must be a non-empty string.")
            elif len(prompt) > 500:
                report.error(
                    f"{MANIFEST_NAME}: `example_prompts[{index}]` is longer than 500 characters."
                )

    router_trigger = manifest.get("router_trigger_prompt")
    if router_trigger is not None and not isinstance(router_trigger, str):
        report.error(f"{MANIFEST_NAME}: `router_trigger_prompt` must be a string or null.")

    status_command = manifest.get("status_refresh_command")
    if status_command is not None and not isinstance(status_command, str):
        report.error(f"{MANIFEST_NAME}: `status_refresh_command` must be a string or null.")

    # Routing has two independent inputs and needs at least one of them. Each is
    # separately optional, so neither field's own check can see this: an agent with
    # neither reaches the cloud and nothing can ever hand it a request.
    has_trigger = isinstance(router_trigger, str) and bool(router_trigger.strip())
    has_examples = isinstance(example_prompts, list) and bool(example_prompts)
    if not has_trigger and not has_examples:
        report.warn(
            f"{MANIFEST_NAME}: with neither `router_trigger_prompt` nor `example_prompts`, "
            "nothing can route a request to this agent once it reaches the cloud."
        )

    _validate_runtime(manifest.get("runtime"), report)
    _validate_credentials(manifest.get("credentials", []), report)
    _validate_schedules(manifest.get("schedules", []), report)
    _validate_handovers(manifest.get("handovers", []), manifest.get("slug"), report)
    _validate_manifest_ledger_keys(manifest, report)

    features = manifest.get("features", {})
    if not isinstance(features, dict):
        report.error(f"{MANIFEST_NAME}: `features` must be an object.")
    else:
        # Two severities for one condition, and both are the desktop's: `features`
        # not being an object at all is an error, a member that is not a boolean is
        # a warning. Unknown feature keys are preserved by every host, so a value
        # this build cannot read is reported, never rejected.
        for key, value in features.items():
            if value is not None and not isinstance(value, bool):
                report.warn(f"{MANIFEST_NAME}: `features.{key}` should be a boolean.")


def _validate_runtime(runtime: object, report: Report) -> None:
    if runtime is None:
        return
    if not isinstance(runtime, dict):
        report.error(f"{MANIFEST_NAME}: `runtime` must be an object or null.")
        return
    for key in ("model", "credential"):
        value = runtime.get(key)
        if value is not None and not isinstance(value, str):
            report.error(f"{MANIFEST_NAME}: `runtime.{key}` must be a string or null.")

    # An ERROR, and the one place in this file where a manifest field is judged by
    # its shape rather than its type: `runtime.credential` names a credential, and
    # a folder whose manifest holds the key itself has already published it into
    # every copy, export and hash of that folder. Naming it is the only way the
    # user learns to rotate it.
    credential = runtime.get("credential")
    if isinstance(credential, str) and (
        SECRET_LOOKALIKE_RE.match(credential) or len(credential) > 200
    ):
        report.error(
            f"{MANIFEST_NAME}: `runtime.credential` looks like a secret value. It must be a "
            "reference — a credential type, or the name of a credential configured in the "
            "host — never a key. Remove this value and rotate it."
        )

    if "permissions" in runtime and not isinstance(runtime.get("permissions"), dict):
        report.error(f"{MANIFEST_NAME}: `runtime.permissions` must be an object.")


def _validate_manifest_ledger_keys(manifest: dict, report: Report) -> None:
    """The two ledger keys that no longer belong in the manifest.

    Both are reported, neither is repaired: `validate` is read-only, and the
    migration belongs to `write_manifest`, where the folder's hash is allowed to
    move once.

    Every message here PREDICTS what the next write will do, so each one is
    guarded by `ledger_entry_is_placeable` — the same predicate `write_manifest`
    migrates by. The two branches used to be guarded differently (`cloud`
    type-checked, `publications` not) and both promised a move unconditionally,
    so `"publications": "foo"` was told "it moves across on the next write" and
    `"cloud": null` was told it "moves into publications.json" — neither of which
    would ever happen. A wrong prediction is worse than no prediction: it sends
    the user away to wait for a migration that is never coming.
    """
    if "cloud" in manifest:
        cloud = manifest.get("cloud")
        if cloud is None:
            report.warn(
                f"{MANIFEST_NAME}: `cloud` is null — the key records nothing and will "
                "never migrate. Remove it."
            )
        elif not isinstance(cloud, dict):
            report.error(f"{MANIFEST_NAME}: `cloud` must be an object.")
        elif not ledger_entry_is_placeable(cloud):
            report.warn(
                f"{MANIFEST_NAME}: the `cloud` stamp is deprecated, and this one cannot "
                f"move into {PUBLICATIONS_NAME}: an entry there needs `platform_url` and "
                "`agent_id` as non-empty strings, and the optional timestamps as strings "
                "or null. Until it does, it stays in the manifest — nothing is discarded."
            )
        else:
            # Info, not a warning: the block still reads, and nothing about it is
            # wrong today. The contract still accepts it; the only ask is a re-stamp.
            report.info(
                f"{MANIFEST_NAME}: the `cloud` stamp is deprecated in favour of "
                f"`publications[]`. It still reads, and moves into {PUBLICATIONS_NAME} "
                "on the next write."
            )

    if "publications" not in manifest:
        return

    publications = manifest.get("publications")
    if not isinstance(publications, list):
        # A warning and not an error for the same parity reason as below; the
        # difference from the schema-shaped `cloud` case is that the manifest
        # schema no longer carries this property at all, so there is no schema
        # rule to be in breach of — only a value that will sit here forever.
        report.warn(
            f"{MANIFEST_NAME}: `publications` must be an array to be migrated, and this "
            f"one is not, so it stays in the manifest. The ledger lives in "
            f"{PUBLICATIONS_NAME}; move the entries by hand or remove the key."
        )
        return
    if not all(ledger_entry_is_placeable(entry) for entry in publications):
        report.warn(
            f"{MANIFEST_NAME}: `publications` no longer belongs in the manifest, but it "
            f"cannot move into {PUBLICATIONS_NAME} yet: every entry needs `platform_url` "
            "and `agent_id` as non-empty strings. The array moves whole or not at all, so "
            "one incomplete entry holds all of them here — nothing is discarded."
        )
        return
    # A WARNING, and the severity is argued rather than picked. It cannot be an
    # ERROR: no other host's validator has this check, and §9.2 forbids a
    # kit-only error — a folder this kit calls broken that the desktop happily
    # runs is the parity failure in the worse direction.
    #
    # It is not an INFO either, which is where the deprecated `cloud` block sits.
    # `cloud` is RETAINED: the contract still describes it and it still reads
    # correctly. `publications` here is REMOVED — the manifest schema no longer
    # carries the property, no host reads it from this file, and a host that
    # WRITES a content_hash into it reproduces the unreachable fixed point the
    # split exists to remove (the manifest is inside the tree the hash covers).
    # Filing a removed-and-harmful key at the same level as a retained-and-inert
    # one would be the wrong signal.
    report.warn(
        f"{MANIFEST_NAME}: `publications` no longer belongs in the manifest — the "
        f"ledger lives in {PUBLICATIONS_NAME}, because a content_hash of the exported "
        "tree cannot be stored inside a file that tree contains. It moves across on "
        "the next write."
    )


def _validate_publications(agent_dir: Path, report: Report) -> None:
    """`publications.json`: the ledger's own file.

    Severities match the desktop's `manifest.publications.*`, which are errors.
    R4 moved WHERE these entries live; it did not make checking them a kit-only
    concern, so the error severity carries across with the data.
    """
    path = agent_dir / PUBLICATIONS_NAME
    if not path.is_file():
        return
    try:
        document = read_json(path)
    except json.JSONDecodeError as exc:
        report.error(
            f"{PUBLICATIONS_NAME} is not valid JSON: {exc.msg} (line {exc.lineno})."
        )
        return
    except OSError as exc:
        report.error(f"{PUBLICATIONS_NAME} could not be read: {exc}.")
        return
    if not isinstance(document, dict):
        report.error(
            f"{PUBLICATIONS_NAME} must contain a JSON object with a `publications` array."
        )
        return

    publications = document.get("publications")
    if publications is None:
        # An object with no `publications` key is a file that says nothing, not a
        # file that says something wrong. The schema requires the key; a folder
        # missing it publishes nothing and loses nothing.
        report.warn(f"{PUBLICATIONS_NAME}: no `publications` array — nothing is recorded.")
        return
    if not isinstance(publications, list):
        report.error(f"{PUBLICATIONS_NAME}: `publications` must be an array.")
        return
    for index, entry in enumerate(publications):
        label = f"publications[{index}]"
        if not isinstance(entry, dict):
            report.error(f"{PUBLICATIONS_NAME}: `{label}` must be an object.")
            continue
        # The same two key lists `ledger_entry_is_placeable` reads. Shared rather
        # than repeated so that "what this tool will write" and "what this tool
        # accepts" cannot drift apart — a drift whose symptom is the tool writing
        # a file it then refuses to export.
        for key in LEDGER_REQUIRED_KEYS:
            value = entry.get(key)
            if not isinstance(value, str) or not value.strip():
                report.error(f"{PUBLICATIONS_NAME}: `{label}.{key}` is required.")
        for key in LEDGER_OPTIONAL_STRING_KEYS:
            value = entry.get(key)
            if value is not None and not isinstance(value, str):
                report.error(f"{PUBLICATIONS_NAME}: `{label}.{key}` must be a string or null.")


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
            report.warn(f"{MANIFEST_NAME}: two credential slots are both named `{name}`.")
        else:
            seen.add(name)
        credential_type = slot.get("type")
        if not isinstance(credential_type, str) or not credential_type.strip():
            report.error(f"{MANIFEST_NAME}: `{label}.type` is required.")
        elif credential_type not in CREDENTIAL_TYPES:
            # A WARNING, and the schema agrees: the list below is what this kit
            # knows at contract 1.0.0, and the platform's list grows independently
            # of any bundled contract. A closed enum here would retroactively
            # invalidate every folder using the first type the platform adds, on a
            # machine that may be offline and cannot learn about it.
            report.warn(
                f"{MANIFEST_NAME}: `{label}.type` is {credential_type!r}, which this kit does "
                "not recognise. It will be sent to the platform as written."
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
        # Guard against a value having been pasted into the manifest. This stays an
        # ERROR, and it is the one place this validator is deliberately stricter
        # than the desktop's, which has no equivalent check: demoting a
        # secret-leak guard to buy severity parity is the wrong trade, because the
        # cost of the false positive is an edit and the cost of the false negative
        # is a published key. The answer-back asks the desktop to add it.
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


def _validate_handovers(handovers: object, slug: object, report: Report) -> None:
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
            continue
        if target == slug:
            report.warn(f"{MANIFEST_NAME}: `{label}` hands over to this same agent.")


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


def cli_command_entries(cli_commands_yaml: Path) -> tuple[list[str], list[tuple[int, str]]]:
    """Command names from CLI_COMMANDS.yaml, plus the entries that could not be read.

    Line scan only, no YAML parser. Only `- name:` items that are direct entries
    of the top-level `commands:` list are harvested, so a nested `name:` (under
    `args:`, say) is never mistaken for a command.

    The second return value is what this reader used to throw away. An entry it
    cannot take a name from is a command that exists in the file and that every
    check downstream is then blind to — the Makefile mirror, the duplicate check
    and the `/run:` resolution all silently agree it is not there. It is reported
    and refused rather than guessed at: a command read as something shorter than
    what is written runs a different program.
    """
    names: list[str] = []
    unreadable: list[tuple[int, str]] = []
    in_commands = False
    item_indent: int | None = None
    # The entry currently open: its first line, the indent its map keys sit at,
    # and whether a name has been taken from it yet.
    entry_line: int | None = None
    entry_key_indent = 0
    entry_named = False

    def close_entry() -> None:
        nonlocal entry_line, entry_named
        if entry_line is not None and not entry_named:
            unreadable.append(
                (
                    entry_line,
                    "this `commands:` entry has no readable `name:`, so the command it "
                    "declares cannot be seen at all",
                )
            )
        entry_line = None
        entry_named = False

    def take_name(line_number: int, raw_value: str) -> None:
        nonlocal entry_named
        value = _strip_yaml_scalar(raw_value)
        if not value:
            unreadable.append(
                (line_number, "this entry's `name:` reads as empty, so the command cannot be named")
            )
            entry_named = True  # reported once; do not report it again as nameless
            return
        names.append(value)
        entry_named = True

    for line_number, raw_line in enumerate(read_text(cli_commands_yaml).splitlines(), start=1):
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        if re.match(r"^commands\s*:", raw_line):
            in_commands = True
            continue
        if not in_commands:
            continue
        if re.match(r"^\S", raw_line):
            break  # a new top-level key ends the commands list

        indent = len(raw_line) - len(raw_line.lstrip())
        item = re.match(r"^(\s*)(-\s+)(.*)$", raw_line)
        if item is not None and (item_indent is None or indent == item_indent):
            if item_indent is None:
                item_indent = indent
            close_entry()
            entry_line = line_number
            entry_key_indent = indent + len(item.group(2))
            key = re.match(r"^name\s*:\s*(.+?)\s*$", item.group(3))
            if key is not None:
                take_name(line_number, key.group(1))
            continue
        # A continuation line of the open entry. `name:` may legitimately sit
        # below `description:` — a real YAML reader sees it there, so refusing
        # such an entry would reject a catalog the desktop runs. Only keys at the
        # entry's own indent count, which is what keeps a nested `args:` list's
        # `- name:` from being mistaken for a command.
        if entry_line is None or entry_named or indent != entry_key_indent:
            continue
        key = re.match(r"^name\s*:\s*(.+?)\s*$", raw_line.lstrip())
        if key is not None:
            take_name(line_number, key.group(1))
    close_entry()
    return names, unreadable


def cli_command_names(cli_commands_yaml: Path) -> list[str]:
    """Just the names — the reading half of `cli_command_entries`."""
    return cli_command_entries(cli_commands_yaml)[0]


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


def substitute_tokens(root: Path, replacements: dict[str, str | None]) -> None:
    """Fill the scaffold's UPPER_SNAKE double-brace tokens in place.

    Keys are bare token names (`NAME`, `SLUG`, …); a name whose value is None is
    left unsubstituted, so a kit that never learned its own version ships a
    visible KIT_VERSION token rather than a plausible wrong one.

    Substitution into a `.json` member escapes the value as a JSON string body.
    Every token inside `cinna-agent.json` sits between quotes and the values are
    free-form user text — `--description "he said \"no\""` would otherwise
    write a manifest no parser accepts. This mirrors the platform renderer's
    `json_mode` in `LocalAgentKitService._render_bytes`, and it is contextual
    escaping, not templating.
    """
    for path in iter_files(root):
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # binary or unreadable: nothing to substitute
        json_mode = path.suffix.lower() == ".json"
        replaced = text
        for token, value in replacements.items():
            if value is None:
                continue
            # json.dumps quotes and escapes; strip the outer quotes since the
            # template already supplies them.
            emitted = json.dumps(value)[1:-1] if json_mode else value
            replaced = replaced.replace(scaffold_token(token), emitted)
        if replaced != text:
            path.write_text(replaced, encoding="utf-8")


def restore_scaffold_ignore_files(agent_dir: Path) -> None:
    """Rename the scaffold's dotless `gitignore` files to `.gitignore`.

    The templates ship them dotless so they are inert where the kit is stored;
    the created agent needs the real names, because the root one is what keeps
    `credentials/.env` out of the user's history.

    The pairs come from `layout.json` (`scaffold_ignore_files.agent`) so this
    tool and the desktop restore exactly the same names.
    `templates/agent/credentials/.gitignore` is not in that list and keeps its
    dot — it must stay a live rule wherever the contract is stored.
    """
    for relative, restored in scaffold_ignore_files():
        source = agent_dir / relative
        target = agent_dir / restored
        if not source.is_file():
            # An older kit already shipped it dotted; nothing to rename.
            if target.is_file():
                continue
            raise KitError(
                f"scaffold template is missing {relative} (and its restored "
                f"form {restored}): {TEMPLATE_DIR}"
            )
        if target.exists():
            target.unlink()
        target.parent.mkdir(parents=True, exist_ok=True)
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

    description = (args.description or "").strip() or DEFAULT_DESCRIPTION

    root = resolve_root(args.root)
    # Same accessor `cmd_list` enumerates from. Two literals would let `new`
    # scaffold into a folder `list` does not look in, which presents as a
    # scaffold that silently produced nothing.
    target = root / workshop_agents_dir() / slug
    if target.exists():
        raise KitError(f"{target} already exists — pick another slug or remove the folder")

    # Identity is written once, here, and never rewritten: `id` survives folder
    # moves and slug renames, which is the whole reason it is not the slug.
    agent_id = str(uuid.uuid4())
    created_at = (
        datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    )
    contract = contract_version()

    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(TEMPLATE_DIR, target)
    restore_scaffold_ignore_files(target)

    # One substitution pass fills the whole tree, manifest included. Nothing is
    # patched onto the parsed dict afterwards: two writers of the same field
    # would only disagree eventually.
    substitute_tokens(
        target,
        {
            "NAME": name,
            "SLUG": slug,
            "DESCRIPTION": description,
            "ID": agent_id,
            "CREATED_AT": created_at,
            "CONTRACT_VERSION": contract,
            "KIT_VERSION": kit_version(),
        },
    )

    manifest_path = target / MANIFEST_NAME
    manifest = read_json(manifest_path)
    if not isinstance(manifest, dict):
        raise KitError(f"scaffold {MANIFEST_NAME} is not a JSON object")
    # An ASSERTION about the KIT, standing where `write_manifest`'s repair would
    # otherwise stand. That repair is right for a user's folder — a legacy `cloud`
    # block earns a quiet migration — and wrong as the only thing between a stale
    # template and every scaffold this kit produces: it would land silently, on
    # every `new`, for as long as the template stayed wrong, and nothing would
    # ever say so. It also would not help the hosts that DON'T route through
    # `write_manifest`: the desktop parses this same template and sets the seven
    # identity fields on it, deliberately preserving "unknown template keys and
    # key order ... exactly as `kit.py new` leaves them" (`buildManifest`,
    # src/main/services/localAgents/scaffoldService.ts). A ledger key left in the
    # template is therefore one this repair strips and their scaffold keeps —
    # two hosts writing different key sets into the one file whose byte shape is
    # contractual, and a divergence against their own stated intent.
    ledger_keys = [key for key in ("publications", "cloud") if key in manifest]
    if ledger_keys:
        raise KitError(
            f"the kit's own {MANIFEST_NAME} template carries "
            f"{', '.join(repr(key) for key in ledger_keys)} — a publication-ledger key "
            f"that no new agent may start life with (the ledger lives in "
            f"{PUBLICATIONS_NAME}, and is absent until the first publish). This is a "
            f"defect in the kit at {TEMPLATE_DIR}, not in anything you did: re-fetch it "
            "with `kit.py refresh`, or remove the key from the template."
        )
    # Re-written only to guarantee the serialisation every host agrees on.
    # It goes through `write_manifest` rather than a local `json.dumps` so the
    # scaffold shares the one writer, and so the ledger migration is structural:
    # every manifest write in this file migrates, and a write path added later
    # cannot forget to.
    write_manifest(target, manifest)

    if args.json:
        # Machine mode: this object and nothing else on stdout, so a conformance
        # harness can drive `new` without scraping the human output.
        print(
            json.dumps(
                {
                    "path": str(target),
                    "slug": slug,
                    "id": agent_id,
                    "contract_version": contract,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return 0

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
        if (agent_dir / relative).is_file():
            continue
        if relative == SCAFFOLD_IGNORE_TARGET:
            # Kit-only, so a warning: the desktop's validator has no "missing
            # .gitignore" check at all, and a folder it runs happily must not be
            # one this tool calls broken. What actually matters — that nothing
            # secret is committable — is checked by name in `_validate_secrets`,
            # which still errors, so demoting this weakens no secret guard.
            report.warn(
                f"missing {relative} — without it nothing stops `credentials/.env` and "
                "app-data/ from being committed."
            )
        else:
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

    # Both findings below are warnings, not errors, and the reason is the same in
    # each case: the desktop treats an empty prompt as a warning and has no
    # placeholder check at all. An agent whose workflow prompt is still a stub is
    # unfinished, not broken — it loads and runs — and calling it broken here
    # would refuse a folder the desktop opens without comment.
    workflow_relative = prompt_paths["workflow"]
    workflow = agent_dir / workflow_relative
    if workflow.is_file():
        text = read_text(workflow)
        if not text.strip():
            report.warn(f"{workflow_relative} is empty — it is the agent's real instructions.")
        if "{{" in text:
            report.warn(
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
            #
            # A warning, not an error, because it is kit-only: the desktop reads
            # `.gitignore` files in-process and never shells out to git, so it has
            # no concept of a tracked path and cannot reach this verdict. Note it
            # is still the more useful of the two checks — git's index is the fact,
            # the ignore file is only the intent — which is why it is kept rather
            # than dropped, and why the answer-back mentions it.
            report.warn(
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
    """pyproject ⇄ workspace_requirements.txt reconciliation. Advisory only.

    Kit-only: the desktop's validator deliberately does not port this check, so
    every finding here is a warning. It is the one check with a repair — `--fix`
    regenerates the file — which is why demoting it costs nothing: the user is
    told what is wrong and handed the command that fixes it, and a folder the
    desktop opens without complaint is not failed here.
    """
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
            report.warn(
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
    report.warn(
        "workspace_requirements.txt is missing dependencies declared in pyproject.toml: "
        f"{', '.join(missing)}. Run with --fix to regenerate it."
    )


def _validate_commands(agent_dir: Path, manifest: dict, report: Report) -> None:
    catalog_relative = agent_command_catalog()
    cli_commands = agent_dir / catalog_relative
    makefile = agent_dir / "Makefile"

    # An absent catalog is not an early return. `status_refresh_command` can name
    # a `/run:` command, and a reference into a file that is not there resolves to
    # nothing just as surely as one into a file that lacks the entry — the host
    # offers a button that runs no command. Returning here left that case unseen.
    if cli_commands.is_file():
        names, unreadable = cli_command_entries(cli_commands)
    else:
        names, unreadable = [], []

    for line_number, message in unreadable:
        # An ERROR, and the desktop agrees: a host would otherwise offer a `/run:`
        # button for a command this reader saw differently from what is written,
        # and a command read as something shorter runs a different program.
        report.error(f"{catalog_relative} line {line_number}: {message}.")

    for name in names:
        if not COMMAND_NAME_RE.match(name):
            report.error(
                f"{catalog_relative}: command name {name!r} cannot be used as a `/run:` "
                "command — it must match ^[A-Za-z0-9][A-Za-z0-9_-]*$."
            )
        elif not COMMAND_NAME_CONVENTION_RE.match(name):
            report.warn(
                f"{catalog_relative}: command name {name!r} runs, but the kit's convention "
                "is lower case and at most 32 characters."
            )
    duplicates = {name for name in names if names.count(name) > 1}
    for name in sorted(duplicates):
        report.error(f"{catalog_relative}: duplicate command name {name!r}.")
    if makefile.is_file():
        targets = makefile_targets(makefile)
        for name in names:
            if name not in targets:
                report.warn(
                    f"{catalog_relative} command {name!r} has no matching Makefile target — "
                    "add the local mirror in the same change."
                )

    refresh_command = manifest.get("status_refresh_command")
    if isinstance(refresh_command, str) and refresh_command.strip():
        reference = RUN_REFERENCE_RE.match(refresh_command.strip())
        if reference is not None and reference.group(1) not in names:
            report.error(
                f"{MANIFEST_NAME}: `status_refresh_command` refers to /run:{reference.group(1)}, "
                f"which is not a command in {catalog_relative}."
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
    if not scripts_dir.is_dir():
        return
    scripts = [
        path
        for path in iter_files(scripts_dir)
        if path.suffix == ".py" and path.name != "__init__.py"
    ]
    if not scripts:
        return

    catalog = scripts_dir / "README.md"
    if not catalog.is_file():
        # Previously an early return, so an agent with scripts and no catalog at
        # all — the worst case, not the mildest — was the one case that reported
        # nothing while every partially-catalogued folder was warned about.
        report.warn(
            "scripts/ holds scripts but there is no scripts/README.md cataloguing them."
        )
        return

    catalog_text = read_text(catalog)
    for path in scripts:
        relative = path.relative_to(scripts_dir).as_posix()
        if relative not in catalog_text and path.name not in catalog_text:
            report.warn(
                f"scripts/{relative} is not described in scripts/README.md — the catalog must "
                "never fall behind reality."
            )


def _parse_frontmatter(text: str) -> dict[str, str] | None:
    """Top-level scalars of a leading `---` YAML frontmatter block, or None.

    A port of the desktop's `parseFrontmatter`, narrowed to what the status check
    asks of it: does the block open, does it close, and what plain `key: value`
    pairs does it hold.
    """
    normalized = text.lstrip("\ufeff")
    if not re.match(r"^---[ \t]*\r?\n", normalized):
        return None
    lines = normalized.split("\n")
    for index in range(1, len(lines)):
        if lines[index].strip() not in ("---", "..."):
            continue
        data: dict[str, str] = {}
        for raw_line in lines[1:index]:
            match = re.match(r"^([A-Za-z0-9_][A-Za-z0-9_.-]*)\s*:\s*(.*)$", raw_line)
            if match:
                data[match.group(1)] = _strip_yaml_scalar(match.group(2))
        return data
    return None


def _validate_status_file(agent_dir: Path, report: Report) -> None:
    """STATUS.md, when the agent writes one, must be readable as frontmatter.

    Only checked when the file exists: an agent that reports no status is a
    perfectly valid agent, and `_validate_commands` already nudges an unattended
    one toward a `status` command.
    """
    relative = agent_status_file()
    status_file = agent_dir / relative
    if not status_file.is_file():
        return
    frontmatter = _parse_frontmatter(read_text(status_file))
    if frontmatter is None:
        report.warn(
            f"{relative} has no YAML frontmatter, so no host can read a status out of it."
        )
    elif not frontmatter.get("status"):
        report.warn(f"{relative} has no `status` field.")


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
            # The template carries the DESCRIPTION token; `new` substitutes
            # the default when no `--description` was given, so what lands on
            # disk is that sentence. Compare against the substituted form or
            # this check silently stops firing.
            return description.replace(scaffold_token("DESCRIPTION"), DEFAULT_DESCRIPTION)
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
    #
    # THIS PROMOTION IS KIT-ONLY AND HAS NO DESKTOP ANALOGUE. It is not a severity
    # divergence to be "aligned away": every check it rebinds is a warning at the
    # same severity the desktop reports, and the flag only exists because this tool
    # has a moment the desktop does not — the user asking to publish, when advice
    # that has been ignored for a week becomes the last chance to act on it.
    # A reader tidying `kit.py` toward the desktop's severity table should leave
    # this alone; without the flag the two tables already agree.
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
                # A warning, matching the desktop: the sibling may simply not have
                # been built yet, or may live in another workshop, and neither
                # makes this agent broken.
                report.warn(
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

    # Before ANY check reads a path underneath this folder, and before the
    # manifest: a directory the walk cannot scan is not a finding this function
    # can record and then carry on past. `Path.is_file()` swallows ENOENT and
    # PROPAGATES EACCES, so the next check to touch such a path exits the whole
    # command with a bare `[Errno 13] Permission denied: .../scripts/README.md`
    # and no story. Reproduced with `chmod 000` on a scaffolded agent's
    # `scripts/` followed by `kit.py export`, which reaches this function first.
    #
    # An error and an immediate return, not a warning and a continue: every
    # remaining check would be answering about a folder it cannot see, and a
    # "missing required file" that is really "unreadable directory" is a filter
    # standing where an assertion belongs.
    #
    # The scan covers VALIDATE's scope, which is the whole agent folder less
    # `SKIP_DIRS`, and NOT the subtree that travels. It was scoped to the
    # travelling subtree once, on the reasoning that an unreadable `credentials/`
    # is excluded from the export and so "fails nothing". That reasoning was
    # about the wrong command. `credentials/.env.example` is a REQUIRED file and
    # `app-data/storage/` holds the status file, so `_validate_files` and
    # `_validate_status_file` stat inside both — and `chmod 000` on either still
    # ended the whole command on `main()`'s backstop with a bare
    # `error: [Errno 13] Permission denied: …`, which is the same defect one
    # directory to the left. Worse for `.claude/` and friends, which nothing
    # required reads but `_validate_secrets` walks hunting stray key material:
    # `os.walk` yields nothing for a directory it cannot enter, so that check
    # reported a clean bill of health over a subtree it never saw.
    #
    # `unreadable_directories` shares `iter_files` with `_validate_secrets` so
    # the scan's scope cannot drift from the widest walk it is standing in front
    # of; see its docstring for why this is a third condition rather than the
    # export's `unreadable[]`.
    unscannable = unreadable_directories(agent_dir)
    if unscannable:
        for relative in unscannable:
            label = "the agent folder itself" if relative == "." else f"{relative}/"
            report.error(
                f"{label} could not be read — nothing inside it could be checked. "
                "Fix its permissions and validate again."
            )
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
    except OSError as exc:
        # Refuse-early rather than report-and-continue, and it is the ONE read on
        # this path that earns the difference: every check below is a statement
        # about the manifest, so carrying on would answer nine questions about a
        # file nobody read. The two branches beside it already return for the
        # same reason.
        report.error(
            f"{MANIFEST_NAME} could not be read ({exc.strerror}) — nothing else "
            "about this folder can be checked until that is fixed."
        )
        return report
    if not isinstance(manifest, dict):
        report.error(f"{MANIFEST_NAME} must contain a JSON object.")
        return report

    # Report-and-continue for an unreadable FILE, at CHECK granularity, and this
    # is the only place in the file where that costs one edit instead of seven.
    #
    # Seven of the scaffold's twenty-six files took the whole command down when
    # `chmod 000`, and every one of them raised inside `read_text` or `read_json`
    # — no validator reads bytes any other way. Fixing it in those two helpers
    # was the tempting move and is the wrong one twice over: returning `""` for a
    # file that could not be read invents findings (an unreadable
    # WORKFLOW_PROMPT.md would be reported as an EMPTY one, which moves the
    # reader from neutral to wrong — worse than the bare errno it replaces), and
    # raising a different exception type out of two helpers used all over this
    # file would silently escape every `except OSError` already standing around
    # them. So the handler goes where the CHECK boundary already is.
    #
    # Missing and unreadable are the same class of fact — the content is not
    # available — but NOT the same class structurally, which is why they are
    # handled in different places. A missing file is knowable before the read, by
    # a total predicate every caller already uses (`is_file()`), so those callers
    # skip the read and report one finding. An unreadable file is knowable ONLY
    # by attempting the read, from inside whichever helper attempted it, so the
    # nearest boundary that can both name it and carry on is the check itself.
    #
    # The granularity is the CHECK, not the file, and the difference is real:
    # one check is lost per failure and the other eight still run, but two
    # unreadable files read by the SAME check yield one ERROR naming the first,
    # and the second appears on the next run. Verified: with `pyproject.toml`,
    # `Makefile`, `docs/WORKFLOW_PROMPT.md` and `docs/CLI_COMMANDS.yaml` all
    # unreadable, three ERRORs are reported — `Makefile` is masked because
    # `_validate_commands` reaches the YAML first. Do not describe this as
    # "one pass names every permission"; it names one per failing check.
    #
    # `OSError` only: a check that raises anything else is a genuine bug, and a
    # traceback for that is information. No sub-validator raises `KitError`, so
    # nothing here is being swallowed. This does NOT lean on `main()`'s
    # `(OSError, json.JSONDecodeError)` backstop — that clause turns a traceback
    # into a tidy line, which is the thing most easily mistaken for a diagnosis.
    checks = (
        lambda: validate_manifest(manifest, report),
        lambda: _validate_cloud_readiness(agent_dir, manifest, report, cloud_ready),
        lambda: _validate_files(agent_dir, manifest, report),
        lambda: _validate_secrets(agent_dir, report),
        lambda: _validate_requirements(agent_dir, report, fix),
        lambda: _validate_commands(agent_dir, manifest, report),
        lambda: _validate_scripts_catalog(agent_dir, report),
        lambda: _validate_status_file(agent_dir, report),
        lambda: _validate_publications(agent_dir, report),
    )
    for check in checks:
        try:
            check()
        except OSError as exc:
            filename = getattr(exc, "filename", None)
            try:
                label = Path(filename).relative_to(agent_dir).as_posix()
            except (TypeError, ValueError):
                label = str(filename) if filename else "a file in this folder"
            report.error(
                f"{label} could not be read ({exc.strerror}) — the check that reads "
                "it was skipped. Fix its permissions and validate again."
            )
    return report


def _manifest_contract_version(agent_dir: Path) -> str | None:
    """The folder's own `contract_version`, for the JSON report. Never raises."""
    try:
        manifest = read_json(agent_dir / MANIFEST_NAME)
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(manifest, dict):
        value = manifest.get("contract_version")
        if isinstance(value, str):
            return value
    return None


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
            # Both sides of the compatibility gate, so a conformance harness can
            # see what it was decided from rather than inferring it from a message.
            "contract_version": _manifest_contract_version(agent_dir),
            "tool_contract_version": contract_version(),
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

    if is_published(agent_dir, manifest):
        rungs.append("go_cloud")
    return rungs


def _print_table(headers: list[str], rows: list[list[str]]) -> None:
    for row in rows:
        # An ASSERTION, and cheap: a row shorter than the headers prints fewer
        # cells and still looks like a table, so a column that was forgotten on
        # one row reads as a blank answer rather than as the bug it is. This
        # caught nothing today only because the check went in with the column.
        if len(row) != len(headers):
            raise KitError(
                f"internal: table row has {len(row)} cell(s) for {len(headers)} column(s)"
            )
    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    line = "  ".join(header.ljust(widths[index]) for index, header in enumerate(headers))
    print(line.rstrip())
    print("  ".join("-" * width for width in widths))
    for row in rows:
        print("  ".join(cell.ljust(widths[index]) for index, cell in enumerate(row)).rstrip())


def _cloud_workspaces(cloud_root: Path) -> tuple[list[tuple[str, Path]], bool]:
    """`(workspaces, readable)` — every cinna-cli account workspace under `Cloud/`.

    A workspace is any directory holding `.cinna/account.json`, and there are two
    shapes to find. `Cloud/<host>/` is the one A6 settled: one workspace per
    Cinna instance, named by host, because an account on two instances is two
    accounts. `Cloud/` itself carrying `.cinna/account.json` is the FLAT LEGACY
    layout, and it is still read for the reason it always is — a workshop that
    silently stopped being listed would look like data loss to the person whose
    workshop it is. Both can be present at once, and both are reported.

    `readable` is False when some part of `Cloud/` could not be enumerated. That
    is a second return value rather than an empty list because the two are
    different answers: "no workspaces" and "I could not look" print differently,
    and a caller that could not tell them apart would print the confident one.
    """
    workspaces: list[tuple[str, Path]] = []
    cloud_name = cloud_root.name
    readable = True

    def has_account(directory: Path) -> bool:
        """`Path.is_file()` swallows ENOENT and PROPAGATES EACCES — see the guard
        at the top of `validate_agent`, which is the same trap one command over.
        Here the honest answer to "could not look" is the `readable` flag, so an
        OSError must reach it rather than escaping as a bare errno that takes the
        whole listing down."""
        return (directory / CLOUD_ACCOUNT_DIR / CLOUD_ACCOUNT_FILE).is_file()

    try:
        if has_account(cloud_root):
            workspaces.append((f"{cloud_name}/", cloud_root))
        if not cloud_root.is_dir():
            return workspaces, True
        children = sorted(p for p in cloud_root.iterdir() if p.is_dir())
    except OSError:
        # Whatever was found before the failure is still true and is still
        # returned; `readable` is what says the list may be short. Returning an
        # empty list instead would be the filter-as-assertion shape: "no cloud
        # workspaces" printed for "I could not finish looking".
        return workspaces, False

    for child in children:
        if child.name == CLOUD_ACCOUNT_DIR:
            continue
        # PER-CHILD, and this is the whole reason the loop sits outside the try
        # above: `has_account` raises EACCES on a workspace directory the user
        # cannot descend into, and catching that around the WHOLE loop made one
        # such directory take every workspace SORTED AFTER IT off the listing.
        # Measured, not reasoned about: with `acme.example.io` at mode 000,
        # `other.example.io` and `zulu.example.io` both vanished and the only
        # thing printed blamed `Cloud/` itself. That is the failure this function
        # exists to prevent — a workshop that stops being listed reads as data
        # loss to the person whose workshop it is — arriving through permissions
        # instead of through layout, which is why the layout half being right did
        # not save it. The unreadable child is skipped and `readable` carries the
        # news; its siblings are still reported, because they are still there.
        try:
            found = has_account(child)
        except OSError:
            readable = False
            continue
        if found:
            workspaces.append((f"{cloud_name}/{child.name}/", child))
    return workspaces, readable


def cmd_list(args: argparse.Namespace) -> int:
    root = resolve_root(args.root)
    local_dir = root / workshop_agents_dir()
    print(f"Root: {root}")
    print()

    # Asked ONCE, before the loop: this is contract state, not per-agent state,
    # so it is the same answer for every row. None is `desktop_state_file()`'s
    # third state — `layout.json` declares a `desktop_owned` block this build
    # cannot read — and the column shows "?" there rather than "no".
    #
    # "no" would be defensible on the letter of it, because `chat` does refuse in
    # that state. It is the wrong cell anyway. "no" in this column is a per-agent
    # fact whose remedy is "turn Connected on in Cinna Desktop"; the truth here is
    # a per-INSTALL fact whose remedy is `kit.py refresh`. Printed as "no" it is
    # indistinguishable from an agent that is simply not connected, which hides a
    # broken contract behind an ordinary-looking answer — a filter standing where
    # an assertion belongs, in the one place a table makes that invisible. "?"
    # plus the line below says what is actually known.
    desktop_declared = desktop_state_file() is not None

    rows: list[list[str]] = []
    if local_dir.is_dir():
        for agent_dir in sorted(p for p in local_dir.iterdir() if p.is_dir()):
            # Evaluated for every row, manifest or not: `chat` needs a folder and
            # a `desktop.json`, never a manifest, so a folder with a broken
            # manifest can still be connected and the column must not imply
            # otherwise.
            desktop = (
                ("yes" if _desktop_connected(agent_dir) else "no")
                if desktop_declared
                else "?"
            )
            manifest_path = agent_dir / MANIFEST_NAME
            if not manifest_path.is_file():
                rows.append([agent_dir.name, "(no manifest)", "-", "-", desktop])
                continue
            try:
                manifest = read_json(manifest_path)
            except json.JSONDecodeError:
                rows.append([agent_dir.name, "(invalid manifest)", "-", "-", desktop])
                continue
            if not isinstance(manifest, dict):
                rows.append([agent_dir.name, "(invalid manifest)", "-", "-", desktop])
                continue
            # `is_published` reads `publications.json` first and the deprecated
            # `cloud` stamp second. It is the ONE reader of that question, shared
            # with `_rungs_present`'s `go_cloud` rung, so the RUNGS and CLOUD
            # cells of a row can never contradict each other.
            imported = "yes" if is_published(agent_dir, manifest) else "no"
            rows.append(
                [
                    agent_dir.name,
                    str(manifest.get("name") or "-"),
                    ", ".join(_rungs_present(agent_dir, manifest)) or "-",
                    imported,
                    desktop,
                ]
            )

    if rows:
        _print_table(["SLUG", "NAME", "RUNGS", "CLOUD", "DESKTOP"], rows)
        if not desktop_declared:
            print()
            print(
                "  DESKTOP is `?` for every agent: layout.json declares a "
                "`desktop_owned` block this build cannot read, so this tool does not "
                "know which file to look in. Run `kit.py refresh`."
            )
    else:
        print(f"No agents under {local_dir}. Create one with `kit.py new <slug>`.")

    cloud_root = root / workshop_cloud_dir()
    workspaces, cloud_readable = _cloud_workspaces(cloud_root)
    if not cloud_readable:
        print()
        # Deliberately says "in full": `_cloud_workspaces` returns False both for
        # a `Cloud/` it could not open at all and for one workspace inside it that
        # it could not probe, and the line has to be true of both. Naming only the
        # top directory would send someone to check permissions on the one folder
        # that is fine.
        print(
            f"{cloud_root.name}/ could not be read in full — a cloud workspace under "
            "it may be missing from this list. Check permissions under "
            f"{cloud_root.name}/."
        )
    for label, workspace in workspaces:
        agents_dir = workspace / CLOUD_WORKSPACE_AGENTS_DIR
        try:
            names = (
                sorted(p.name for p in agents_dir.iterdir() if p.is_dir())
                if agents_dir.is_dir()
                else []
            )
        except OSError:
            names = None
        print()
        print(f"Cloud workspace {label} ({CLOUD_ACCOUNT_DIR}/{CLOUD_ACCOUNT_FILE} present):")
        if names is None:
            print(f"  ({CLOUD_WORKSPACE_AGENTS_DIR}/ could not be read — check its permissions)")
        elif names:
            for name in names:
                print(f"  - {name}")
        else:
            print(f"  (no agents synced yet — `cinna agent sync <slug>` from {label})")
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


def _parse_remote_version(
    payload: bytes, keys: "str | tuple[str, ...]" = ("kit_version", "version")
) -> str:
    """A version out of a `/version`-shaped response: JSON object, JSON string or bare text.

    `keys` names which member to read, in order, so a contract-version poll
    (`{"contract_version": …, "kit_version": …}`) reuses this parser instead of
    growing a second one that drifts from it.
    """
    if isinstance(keys, str):
        keys = (keys,)
    text = payload.decode("utf-8", errors="replace").strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return text
    if isinstance(data, dict):
        for key in keys:
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


def normalize_rel_path(rel_path: str) -> str:
    """POSIX, root-relative, no `./`, no leading or trailing `/`.

    Byte-for-byte the desktop's `normalizeRelPath` (`src/main/kit/layout.ts`).
    Both the pattern and the path go through it before any matching.
    """
    text = rel_path.replace("\\", "/")
    if text.startswith("./"):
        text = text[2:]
    return text.lstrip("/").rstrip("/")


def _to_code_units(text: str) -> str:
    """The string as UTF-16 code units, one Python character each.

    JavaScript indexes strings by code unit, so its `[^/]` — what `?` compiles
    to — consumes ONE unit and therefore only half of a non-BMP character.
    Python's `[^/]` consumes a whole code point. Matching both sides in this
    representation makes `?` mean the same thing on both hosts; `*` and literals
    are unaffected, since a surrogate pair matches itself either way.

    This is the same UTF-16 seam D7 closed for the *sort*. The sort was proven;
    segment matching never was, and the two are independent.
    """
    if text.isascii():
        return text
    units = []
    for character in text:
        code_point = ord(character)
        if code_point > 0xFFFF:
            offset = code_point - 0x10000
            units.append(chr(0xD800 + (offset >> 10)))
            units.append(chr(0xDC00 + (offset & 0x3FF)))
        else:
            units.append(character)
    return "".join(units)


@functools.lru_cache(maxsize=512)
def _segment_matcher(pattern: str) -> "re.Pattern[str]":
    """One pattern segment as a regex: `*` -> `[^/]*`, `?` -> `[^/]`, rest literal.

    Compiled unanchored and used with `fullmatch`, NOT `^…$` with `match`:
    Python's `$` also matches just before a trailing newline, JavaScript's does
    not. A file called `notes.md\n` is legal on macOS and Linux, and with `$`
    the pattern `README.md` would drop `README.md\n` here while the desktop kept
    it — different file sets, different `content_hash`, drift that no publish
    can clear.
    """
    parts = []
    for character in _to_code_units(pattern):
        if character == "*":
            parts.append("[^/]*")
        elif character == "?":
            parts.append("[^/]")
        else:
            parts.append(re.escape(character))
    return re.compile("".join(parts))


def _match_segments(pattern_segments: tuple[str, ...], path_segments: tuple[str, ...]) -> bool:
    """`**` spans zero or more segments; every other segment matches exactly one."""
    if not pattern_segments:
        return not path_segments
    if pattern_segments[0] == "**":
        rest = pattern_segments[1:]
        for skip in range(len(path_segments) + 1):
            if _match_segments(rest, path_segments[skip:]):
                return True
        return False
    if not path_segments:
        return False
    if not _segment_matcher(pattern_segments[0]).fullmatch(_to_code_units(path_segments[0])):
        return False
    return _match_segments(pattern_segments[1:], path_segments[1:])


def matches_pattern(pattern: str, rel_path: str) -> bool:
    """One `cloud_import_excludes` pattern against one agent-root-relative path.

    A port of `matchesPattern` in the desktop's `src/main/kit/layout.ts`, and it
    has to stay one: both hosts hash the file set this function selects, and a
    difference of a single file makes the two hashes disagree forever while
    every individual step still looks like it worked.

    Semantics, as `layout.json`'s `cloud_import_excludes_notes` documents them:

    - trailing `/`   the directory itself and everything beneath it
    - `*`            any run of characters inside one segment
    - `?`            one character inside one segment
    - `**`           zero or more whole segments
    - no leading `**`  anchored at the agent root, and the pattern must consume
      the whole path — so `README.md` drops the agent's own README and never
      `docs/README.md` or `scripts/README.md`
    """
    path = normalize_rel_path(rel_path)
    # Strip BEFORE anything reads the pattern, so the branch test below and the
    # body below are derived from the same text. They were not: the branch test
    # was whitespace-tolerant (`pattern.rstrip()`) while `normalize_rel_path`
    # strips only "/", so a trailing space took the directory branch and then
    # survived into the body as a segment of its own, and the pattern matched
    # nothing — a directory silently dropped from the exclude set, with every
    # individual step looking like it worked. `contract_exclude_patterns()`
    # already strips and warns at load, so a shipped list cannot reach here
    # dirty; this keeps the function honest for a direct caller, and keeps the
    # two hosts' matchers observably identical, which is the property that
    # actually matters (both hash the file set this selects).
    pattern = pattern.strip()
    body = normalize_rel_path(pattern)
    if not body or not path:
        return False

    pattern_segments = tuple(body.split("/"))
    path_segments = tuple(path.split("/"))

    if pattern.endswith("/"):
        for end in range(len(pattern_segments), len(path_segments) + 1):
            if _match_segments(pattern_segments, path_segments[:end]):
                return True
        return False
    return _match_segments(pattern_segments, path_segments)


def is_excluded(relative: "str | PurePosixPath", patterns: list[str]) -> bool:
    """True when any `cloud_import_excludes` pattern drops this path.

    `relative` is agent-root-relative and names a file *or* a directory: the
    export walk asks about directories before descending, so an excluded
    directory is never opened.
    """
    text = relative if isinstance(relative, str) else relative.as_posix()
    return any(matches_pattern(pattern, text) for pattern in patterns)


def _with_always_excluded(patterns: list[str]) -> list[str]:
    """`patterns` plus the entries no contract may turn off, added once.

    The one place the two lists are joined. Both walkers need the combined list
    and they must never combine it differently: a folder validated against one
    set and exported against another can pass a check the export then breaks.
    """
    combined = list(patterns)
    combined += [pattern for pattern in ALWAYS_EXCLUDE if pattern not in combined]
    return combined


def collect_export_tree(
    agent_dir: Path, patterns: list[str], secret_rules: list | None = None
) -> tuple[list[str], list[str]]:
    """`(files, unreadable)` — the desktop's walk, and what it could not see.

    `files` is the agent-root-relative POSIX paths that travel, sorted.
    `unreadable` is every path the walk could not read *as a directory entry*:
    a directory it could not scan, or an entry whose type it could not
    determine. It is the directory-level twin of `hash_export_files`' file-level
    list, and it exists because the two failures have the SAME consequence —
    part of the tree was never seen — at two levels of one walk.

    This list used to be thrown away. `except OSError: return` sat on the
    `scandir` below with a comment saying a transient EACCES must not abort a
    scan, which is true and is not the whole rule: not aborting is one thing,
    reporting nothing is another. A `chmod 000 scripts/` produced a complete-
    looking export whose `content_hash` covered a tree with a subtree missing
    from it — a number that looks authoritative, is not comparable to any other
    host's, and recorded on a publication reads "up to date" forever. That is
    the same defect the file-level list exists to stop; the guard had simply
    been written where the check is rather than where the consequence is.

    **Fail-safe direction, derived here rather than copied from a neighbour.**
    Guessing that an unscannable directory is empty costs an export that
    silently omits a subtree, plus a hash nobody can trace back to it. Refusing
    costs one loud line and a `chmod`. Loud and recoverable wins, so the walk
    RECORDS and the callers refuse — `collect_export_files` and every hash built
    on it unconditionally, `cmd_export` with the listing. An EXCLUDED directory
    is never descended into, so an unreadable `credentials/` costs nothing: the
    list only ever names a subtree that would have travelled.

    Three details of the walk are load-bearing and none of them is what
    `iter_files` does:

    - **Symlinks are never followed and never listed**, files and directories
      alike: a folder that travels must not be able to reach outside itself.
    - **Excluded directories are never descended into**, so `credentials/` costs
      one match rather than one per file.
    - **Dotfiles are walked.** They are dropped by the patterns, not by the walk.
      `iter_files` hard-skips `SKIP_DIRS`, which is a *different* set from the
      contract's and would make this host's hash disagree with every other
      host's the moment the two lists drift apart.

    On top of the patterns it applies the contract's `secret_files` rule, which
    globs cannot express. That filter belongs HERE, in the one walk, rather than
    at copy time: `content_hash` must describe the set that actually travels, or
    editing a withheld secret moves the hash for a change that can never be
    published — "unpublished changes" that no publish can ever clear.
    """
    if secret_rules is None:
        secret_rules = secret_file_rules()
    files: list[str] = []
    unreadable: list[str] = []

    def walk(rel_dir: str) -> None:
        absolute = agent_dir if rel_dir == "" else agent_dir / rel_dir
        try:
            with os.scandir(absolute) as scan:
                entries = sorted(scan, key=lambda entry: entry.name)
        except OSError:
            # Not aborted — a transient EACCES must not take the scan down — but
            # not swallowed either. The caller decides; see the docstring.
            unreadable.append(f"{rel_dir}/" if rel_dir else ".")
            return
        for entry in entries:
            relative = entry.name if rel_dir == "" else f"{rel_dir}/{entry.name}"
            try:
                is_link = entry.is_symlink()
                is_dir = not is_link and entry.is_dir(follow_symlinks=False)
                is_file = not is_link and not is_dir and entry.is_file(follow_symlinks=False)
            except OSError:
                # An entry whose type cannot be determined is a third way for the
                # walk to be blind to part of the tree, and it resolves the same
                # way: recorded, never guessed. Guessing "not a directory" drops
                # a subtree; guessing "not a file" drops a file that travels.
                unreadable.append(relative)
                continue
            if is_link:
                continue
            if is_excluded(relative, patterns):
                continue
            # Applied to directories too: a `secrets.env/` folder is as much a
            # place to keep a value as a file of that name.
            if is_secret_filename(entry.name, secret_rules):
                continue
            if is_dir:
                walk(relative)
            elif is_file:
                files.append(relative)

    walk("")
    return sorted(files, key=_utf16_sort_key), sorted(unreadable, key=_utf16_sort_key)


def collect_export_files(
    agent_dir: Path, patterns: list[str], secret_rules: list | None = None
) -> list[str]:
    """The paths that travel, or `KitError` when the walk could not see the tree.

    The policy wrapper over `collect_export_tree`, and the policy is the one
    already settled for a file whose bytes cannot be read: there is no honest
    answer to "what travels?" when a subtree could not be scanned, so this
    function does not invent one. Everything built on it — `content_hash` above
    all — inherits the refusal, which is the D11-amendment rule reaching the
    directory level: **unevaluable ⇒ refuse to emit a `content_hash` at all,
    never emit one computed a different way.**

    A caller that wants to phrase the refusal itself — `cmd_export` prints the
    paths and says `--force` does not waive them — calls `collect_export_tree`
    and inspects the second element. That is the ONE way past this guard, and it
    is a caller taking the decision, not a caller escaping it. Compare
    `secret_file_rules`, whose docstring records what happened when a guard was
    lifted one function away for a caller's convenience.

    See `collect_export_tree` for what the walk does and why; it is the port of
    `collectExportFiles` (`src/main/kit/exportTree.ts`).
    """
    files, unreadable = collect_export_tree(agent_dir, patterns, secret_rules)
    if unreadable:
        listing = "\n".join(f"    - {relative}" for relative in unreadable)
        raise KitError(
            "these paths are inside the export but could not be read, so the set "
            "of files that travels — and any content_hash over it — would describe "
            "a tree that was never fully seen:\n"
            f"{listing}\n"
            "Fix their permissions and try again."
        )
    return files


def _utf16_sort_key(path: str) -> bytes:
    """JavaScript's default string ordering, which is UTF-16 code-unit order.

    The desktop sorts the file list with `Array.prototype.sort`, and the sort
    order is part of the hash: the lines are fed into one running digest. Plain
    `sorted()` agrees for ASCII and diverges for non-BMP characters — an emoji in
    a filename would reorder two lines and change the digest. The only symptom
    would be a host reporting "unpublished changes" forever, which nobody would
    trace back to a filename.

    `surrogatepass` is what keeps a filename that is not valid UTF-8 from taking
    the whole command down: `os.scandir` decodes such a name with
    `surrogateescape`, leaving lone surrogates that a plain encode rejects. Not
    reproducible on APFS, which refuses the name at creation, but live on Linux —
    where a cinna-cli import may well run. See `hash_export_files` for the parity
    limit this cannot fix.
    """
    return path.encode("utf-16-be", "surrogatepass")


# The desktop substitutes this for the hex digest of a file it could not read
# (`UNREADABLE_MARKER` in `exportTree.ts`). It opens with a NUL of its own, so the
# emitted line carries TWO: `<relpath>\0\0unreadable\n`. Verified against their
# source; do not "tidy" the leading NUL away.
UNREADABLE_MARKER = "\0unreadable"


def hash_export_files(agent_dir: Path, files: list[str]) -> tuple[str, list[str]]:
    """`(content_hash, unreadable)` for one file list — the desktop's `hashExportFiles`.

    One line per file, `<relpath>` NUL `<sha256 hex of the bytes>` LF, in sorted
    order, fed into one running SHA-256. No mtimes, sizes, modes or directory
    entries: two machines holding the same files must produce the same string.
    An empty file list yields `sha256:` plus the digest of empty input.

    A file whose bytes cannot be read is folded in as `UNREADABLE_MARKER` rather
    than aborting — a race with a running agent must not fail a scan — **and is
    returned in the second element**. That list is not a diagnostic nicety: the
    digest is stable and comparable, but it no longer describes the bytes that
    would be uploaded, so recorded on a publication it reads as "up to date"
    forever. Every caller must decide what to do about a non-empty list;
    `cmd_export` refuses. This mirrors `ExportTree.unreadable` and the refusal
    its own docstring demands.

    Parity limit worth knowing: for a filename that is not valid UTF-8 this host
    and the desktop hash *different strings* — Python decodes the name with
    `surrogateescape`, Node with lossy U+FFFD replacement. Nothing in this file
    can reconcile that; `surrogatepass` here only guarantees we stay
    deterministic and do not crash.
    """
    digest = hashlib.sha256()
    unreadable: list[str] = []
    for relative in sorted(files, key=_utf16_sort_key):
        try:
            entry = hashlib.sha256((agent_dir / relative).read_bytes()).hexdigest()
        except OSError:
            unreadable.append(relative)
            entry = UNREADABLE_MARKER
        digest.update(f"{relative}\0{entry}\n".encode("utf-8", "surrogatepass"))
    return f"sha256:{digest.hexdigest()}", unreadable


def content_hash(
    agent_dir: Path,
    patterns: list[str],
    files: list[str] | None = None,
    secret_rules: list | None = None,
) -> str:
    """`sha256:<hex>` over the file set that travels, or `KitError` — content only.

    The str-returning convenience over `hash_export_files`, and — like
    `collect_export_files` over `collect_export_tree` — the POLICY wrapper, not a
    thinner spelling of the primitive. D11's amendment: **unevaluable ⇒ refuse to
    emit a `content_hash` at all, never emit one computed a different way.** A
    digest that folded in `UNREADABLE_MARKER` is comparable to nothing and, once
    recorded on a publication, reads "up to date" forever.

    This used to return `hash_export_files(...)[0]`, dropping the unreadable list
    on the floor while its own docstring told callers they "should call
    `hash_export_files` instead and inspect the second element". That is an
    intention, and the one reader who most needs it — someone reaching for the
    function whose name and return type say "give me the number" — is exactly the
    reader who will not go looking for it. Verified silent before the change:
    `chmod 000` one travelling file, then `content_hash(...)` returned a digest
    with nothing said. The refusal is now the structure rather than the advice;
    the signature is unchanged, so a caller wanting the D7-parity digest over a
    partly-unreadable tree still has `hash_export_files`, which is where that
    (correct, desktop-matching) behaviour belongs.

    `files` lets a caller that already walked the tree hash exactly the list it
    used, instead of walking twice and risking a different answer. Passing it
    does not waive the refusal: an explicit list is a claim about which files
    travel, never a claim that they could be read.
    """
    if files is None:
        files = collect_export_files(agent_dir, patterns, secret_rules)
    digest, unreadable = hash_export_files(agent_dir, files)
    if unreadable:
        listing = "\n".join(f"    - {relative}" for relative in unreadable)
        raise KitError(
            "these files are part of the export but could not be read, so a "
            "content_hash over them would describe a tree that was never fully "
            "read:\n"
            f"{listing}\n"
            "Fix their permissions and try again."
        )
    return digest


def _excluded_for_report(
    agent_dir: Path, patterns: list[str], secret_rules: list | None = None
) -> list[str]:
    """What the export left behind, named file by file, for the human reading it.

    Deliberately NOT the complement of `collect_export_files`. That walk stops at
    an excluded directory, so its complement could only say `credentials/` — and
    `credentials/.env` is precisely the line a user wants to see confirmed. This
    walk therefore descends into excluded directories, pruning only `SKIP_DIRS`,
    whose contents are machine-generated and unbounded (a listed `.venv/` would
    bury the summary). It reports; it never decides what travels.

    It asks about the secret rule as well as the patterns: a withheld
    `.env.production` matches no pattern, and silently vanishing from the summary
    is the one outcome a secret gate must not have.

    It also names **symlinks**, which the export walk drops for parity with the
    desktop and which therefore match no rule at all. Dropping them is right; a
    user who symlinked `knowledge/shared` into place and is told nothing is not.
    This is a kit-only line — the desktop has no summary to print it in — so it
    carries a reason rather than pretending to be an exclude.

    It does not walk `SKIP_DIRS`, whose contents are machine-generated and
    unbounded; a listed `.venv/` would bury the summary. Those are named by the
    contract's own patterns, so their absence here surprises nobody.
    """
    if secret_rules is None:
        secret_rules = secret_file_rules()
    reported: list[str] = []
    for dirpath, dirnames, filenames in os.walk(agent_dir):
        parent = Path(dirpath).relative_to(agent_dir).as_posix()
        parent = "" if parent == "." else parent
        kept = []
        for name in sorted(dirnames):
            relative = f"{parent}/{name}" if parent else name
            if (Path(dirpath) / name).is_symlink():
                reported.append(f"{relative}/ (symlink — never travels)")
            elif name not in SKIP_DIRS:
                kept.append(name)
        dirnames[:] = kept
        for name in sorted(filenames):
            relative = f"{parent}/{name}" if parent else name
            if (Path(dirpath) / name).is_symlink():
                reported.append(f"{relative} (symlink — never travels)")
            elif is_excluded(relative, patterns) or is_secret_filename(name, secret_rules):
                reported.append(relative)
    return reported


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

    # Read the contract's exclude list ONCE, and keep the two questions it
    # answers apart. `contract` is None when this build cannot evaluate the list;
    # the copy still proceeds on the identical built-in fallback, but no
    # `content_hash` is emitted — see `contract_exclude_patterns`.
    contract = contract_exclude_patterns()
    hashable = contract is not None
    if args.hash and not hashable:
        # Refused HERE, before a single file is copied: `--hash` asks for the one
        # thing this export cannot honestly produce, and half an export plus a
        # non-zero exit would be a worse answer than none.
        raise KitError(
            f"--hash cannot be answered: {LAYOUT_PATH} does not provide a usable "
            "`cloud_import_excludes`, so the set of files a content_hash would "
            "cover is not the set this contract declares. Emitting a hash computed "
            "from the built-in fallback would be a number that looks authoritative "
            "and is not comparable to another host's. Repair or re-fetch the kit "
            "(`kit.py refresh`) and export again; without --hash the export itself "
            "still works."
        )
    patterns = _with_always_excluded(
        contract if contract is not None else list(DEFAULT_EXCLUDES)
    )
    # The tree that travels and the hash every host computes over it come from
    # ONE walk of the SOURCE — never from the destination, which this command
    # then edits (workspace_requirements.txt regenerated).
    #
    # Both happen BEFORE anything is written, so a refusal below leaves no
    # half-written export behind.
    secret_rules = secret_file_rules()
    # `collect_export_tree`, not `collect_export_files`: this command phrases the
    # refusal itself, in the same terms as the file-level one below. The two
    # findings are one condition at two levels of the same walk — part of the
    # tree was never read — so they read the same and waive the same, which is
    # to say not at all.
    travelling, unscannable = collect_export_tree(source, patterns, secret_rules)
    if unscannable:
        listing = "\n".join(f"    - {relative}" for relative in unscannable)
        raise KitError(
            "these paths are inside the export but could not be read, so neither "
            "the set of files that travels nor a content_hash over it describes a "
            "tree that was fully seen — and the files under them would go missing "
            "from the upload with nothing saying so:\n"
            f"{listing}\n"
            "Fix their permissions and export again. --force does not cover this: "
            "it waives validation findings, not a tree this host could not read."
        )
    digest: str | None = None
    if hashable:
        digest, unreadable = hash_export_files(source, travelling)
        if unreadable:
            listing = "\n".join(f"    - {relative}" for relative in unreadable)
            raise KitError(
                "these files are part of the export but could not be read, so the "
                "content_hash would describe a tree that was never fully read — and "
                "recorded on a publication it would report 'up to date' forever:\n"
                f"{listing}\n"
                "Fix their permissions (or remove them) and export again. --force "
                "does not cover this: it waives validation findings, not a hash that "
                "does not describe the bytes."
            )

    destination.mkdir(parents=True, exist_ok=True)
    if any(destination.iterdir()):
        print(f"note: {destination} is not empty — merging the export into it.")

    copied = 0
    for relative in travelling:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(source / relative, target)
        except OSError as exc:
            # Readable a moment ago, gone or locked now: a race with a running
            # agent. Say which file, never a traceback.
            raise KitError(f"{relative} could not be copied: {exc}") from None
        copied += 1

    excluded = _excluded_for_report(source, patterns, secret_rules)

    pyproject = source / "pyproject.toml"
    if pyproject.is_file():
        dependencies, _ = read_pyproject_dependencies(pyproject)
        write_workspace_requirements(destination / "workspace_requirements.txt", dependencies)

    # An ASSERTION, not a filter, and the distinction is the whole point of the
    # line: `cloud_import_excludes` is contract DATA a later version can change,
    # and a list that swallowed the manifest would otherwise produce a cheerful
    # 0-error export of a tree no host can identify. Three separate defects in
    # this contract's history were a filter standing where an assertion belonged,
    # each degrading to a plausible-looking success. This one stays.
    manifest_path = destination / MANIFEST_NAME
    if not manifest_path.is_file():
        raise KitError(
            f"{MANIFEST_NAME} was not copied — check layout.json cloud_import_excludes"
        )

    # Export does NOT write the manifest, and must not be made to. It used to
    # clear the publication links in the exported copy; the ledger now lives in
    # `publications.json`, which the exclude list drops, so there is nothing left
    # to clear and the exported manifest is byte-identical to the source.
    #
    # Do not reintroduce a rewrite to strip a deprecated `cloud` key from the
    # copy either. An export that mutates the manifest is exactly the defect
    # class this deletion removes, and it breaks byte-parity with a host that
    # uploads the folder verbatim. A legacy `cloud` block is migrated where the
    # manifest is WRITTEN (`write_manifest`); a folder exported before its next
    # re-stamp carries the stale block to the cloud, which is accepted — nothing
    # in it is a secret.

    print(f"Exported {source.name} -> {destination}")
    print(f"  {copied} file(s) copied, {len(excluded)} left behind:")
    for relative in excluded:
        print(f"    - {relative}")
    print("  workspace_requirements.txt regenerated; the manifest is copied unchanged")
    print(f"  {PUBLICATIONS_NAME} is excluded — publication links never travel")
    print("  credentials/ and every .env file are never copied")
    if digest is not None:
        print(f"  content_hash {digest}")
    else:
        # Named, not omitted: a summary that simply stopped printing a line the
        # user has seen before is how "no hash" becomes "I did not notice".
        print(
            f"  content_hash WITHHELD — {LAYOUT_PATH} does not provide a usable "
            "`cloud_import_excludes`, so this build cannot say which files a hash "
            "would cover. The files above still travel (built-in fallback list). "
            "Repair or re-fetch the kit to get a comparable hash."
        )
    if report.warnings:
        print(f"  {len(report.warnings)} validation warning(s) — run `kit.py validate` to see them")
    if args.hash:
        # Bare, last line, nothing else on it: a conformance harness compares this
        # against the other host's hash without parsing the summary. Unreachable
        # with `digest is None` — `--hash` refuses up front, before any copying.
        print(digest)
    return 0


# --------------------------------------------------------------------------- #
# Command: chat
# --------------------------------------------------------------------------- #


class _NoChatRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect on the chat call. This is a token-egress guard.

    `urllib`'s default redirect handler copies the request headers onto the
    redirected request — it drops only `Content-Length` and `Content-Type` — so
    a 302 from the loopback API to any other host would carry
    `Authorization: Bearer <agent_token>` with it, to that host. D10 has no
    redirect in it, so the safe answer is not "strip the header and follow" but
    "do not follow at all": returning None makes urllib fall through to
    `HTTPDefaultErrorHandler`, which raises the 3xx as an `HTTPError`, and
    `cmd_chat` then reports it as the non-2xx it is.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ARG002 - see class docstring
        return None


@functools.lru_cache(maxsize=1)
def _chat_opener() -> urllib.request.OpenerDirector:
    """The opener the chat call uses: no proxies, no redirects.

    `ProxyHandler({})` is the second token-egress guard. A default opener honours
    `http_proxy` / `https_proxy` / `all_proxy` from the environment, and
    `no_proxy` does not reliably name loopback, so on a machine with a corporate
    proxy configured the agent's bearer token and the user's prompt would travel
    through it on their way to `127.0.0.1`. A loopback API has no business
    behind a proxy.
    """
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}), _NoChatRedirect()
    )


def _network_reason(exc: BaseException) -> str:
    """A one-line reason for a failed call that CANNOT carry the URL or the token.

    Deliberately not `str(exc)`. Several exception types on this path stringify
    an attribute of their own that holds the request URL, and `api_base_url` is
    `desktop.json` content, which D3 says a tool never prints. Structured fields
    only: a status line, or the underlying socket error, neither of which
    embeds a URL, a header or a body.
    """
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code} {exc.reason}"
    if isinstance(exc, urllib.error.URLError):
        return str(exc.reason)
    if isinstance(exc, OSError) and exc.strerror:
        return exc.strerror
    return exc.__class__.__name__


def _read_desktop_connection(agent_dir: Path) -> tuple[str, str, str]:
    """`(api_base_url, chat_path, agent_token)` from the desktop-owned state file.

    Raises `KitError` with ONE line whenever the desktop is not connected (D10
    step 1: file absent, `api_base_url` missing or empty, `agent_token` missing
    or empty).

    Every message here names the FILE and at most the KEY that was unusable —
    never a value, never a parse position, never the exception that was raised
    while reading. That is not caution about this particular build: the file
    holds a bearer token, and the cheapest way to leak one is an error string
    that interpolates the object it came from. Nothing in this function formats
    `data`, an exception raised from it, or the token.
    """
    relative = desktop_state_file()
    if relative is None:
        raise KitError(
            "cannot locate the desktop state file: layout.json carries a "
            "`desktop_owned` block this build cannot read. Run `kit.py refresh`."
        )
    path = agent_dir / relative

    def not_connected(reason: str) -> KitError:
        """The one D10 step-1 line. `reason` is a literal from this function."""
        return KitError(
            f"Cinna Desktop is not connected for this agent ({relative}: {reason}). "
            "Open the agent in Cinna Desktop, turn Connected on, then retry."
        )

    if not path.is_file():
        raise not_connected("no such file")
    try:
        data = read_json(path)
    except (json.JSONDecodeError, OSError):
        # Not interpolated, on purpose — see the docstring.
        raise not_connected("unreadable, or not valid JSON") from None
    if not isinstance(data, dict):
        raise not_connected("not a JSON object")

    base_url = data.get(DESKTOP_BASE_URL_KEY)
    token = data.get(DESKTOP_TOKEN_KEY)
    # The two frozen keys get the IDENTICAL test. An asymmetry here — one
    # checked for emptiness and the other only for presence — is exactly how a
    # blank token becomes a live request carrying `Authorization: Bearer `,
    # which a permissive server may well accept.
    if not isinstance(base_url, str) or not base_url.strip():
        raise not_connected(f"no usable `{DESKTOP_BASE_URL_KEY}`")
    if not isinstance(token, str) or not token.strip():
        raise not_connected(f"no usable `{DESKTOP_TOKEN_KEY}`")

    # D3 makes `chat_path` optional and defaulted. `null` is read as "absent",
    # because that is how a writer says "unset" in JSON; anything else that is
    # present but unusable is an assertion failure rather than a quiet default —
    # defaulting there would send the prompt to a path the desktop did not name,
    # and a 200 from the wrong endpoint is not a failure anyone would notice.
    chat_path = DEFAULT_CHAT_PATH
    declared_path = data.get(DESKTOP_CHAT_PATH_KEY)
    if declared_path is not None:
        if not isinstance(declared_path, str) or not declared_path.strip():
            raise KitError(
                f"`{DESKTOP_CHAT_PATH_KEY}` in {relative} is present but not a usable "
                "path; remove the key to use the default, or give it a string."
            )
        chat_path = declared_path.strip()

    return base_url.strip(), chat_path, token.strip()


def _chat_url(base_url: str, chat_path: str) -> str:
    """`api_base_url` + `chat_path`, with the two joining details settled once.

    The scheme check is load-bearing, not hygiene: `urllib` will happily open a
    `file://` URL, and `Request()` raises a `ValueError` that quotes the URL
    back for anything it cannot classify — and the URL is `desktop.json`
    content. Refusing here keeps both problems out of reach, and the refusal
    names the key rather than the value.
    """
    if not base_url.startswith(("http://", "https://")):
        raise KitError(
            f"refusing to call the desktop: `{DESKTOP_BASE_URL_KEY}` is not an "
            "http:// or https:// URL."
        )
    if not chat_path.startswith("/"):
        chat_path = "/" + chat_path
    return base_url.rstrip("/") + chat_path


def _desktop_connected(agent_dir: Path) -> bool:
    """Would `kit.py chat` get as far as the network for this folder?

    The DESKTOP column of `kit.py list` is this function, and this function is
    `cmd_chat`'s own preflight — `_read_desktop_connection` for the file and the
    two frozen keys, `_chat_url` for the scheme check — run for its verdict
    instead of its values. It lives HERE, beside the code it shares, rather than
    next to its one caller, because the property that matters is structural: a
    column that answered the question a second way would drift from the verb it
    describes, and a column that silently disagrees about whether `chat` works is
    worse than no column at all. Every `KitError` those two raise is a way `chat`
    exits non-zero without sending anything, which is exactly what "no" means.

    It stops at the last check that touches no network, and that boundary is the
    honest one: whether the desktop is actually listening cannot be known without
    a request, and `list` must not POST anything anywhere. "yes" therefore means
    "connected as far as this folder can say", which is the same claim the file
    itself makes.

    The token is discarded on the spot and never returned, printed or logged —
    the same rule the whole `chat` path follows.
    """
    try:
        base_url, chat_path, _token = _read_desktop_connection(agent_dir)
        _chat_url(base_url, chat_path)
    except KitError:
        return False
    return True


def _stream_chat_response(response) -> int:
    """Print an NDJSON chat stream as it arrives; return the verb's exit code.

    Four rules, and each is an ASSERTION rather than a filter, because each has
    a silent-success shape sitting right next to it:

    * a line that is not UTF-8, not JSON, or not an object is REPORTED and fails
      the run. Skipping it prints a shorter answer that still looks complete.
    * `{"type": "error"}` prints to stderr and fails the run (D10 step 3).
    * a line carrying no recognised text key is counted and its KEY NAMES (never
      its values) are collected, so one run tells the desktop team what to
      rename.
    * a stream in which NO line carried a recognised text key fails the run.
      This is the tolerance seam's own alarm. If the desktop names its key
      `message`, the alternative to failing here is printing nothing and exiting
      0 — a silent empty success, indistinguishable from an agent that had
      nothing to say, and the single most misleading thing this verb could do.

    Text is written verbatim with no newline added, because a `delta` producer
    emits fragments that must concatenate; one closing newline is added at the
    end if the stream did not supply one. Provisional along with the rest of the
    seam.
    """
    failed = False
    recognised = 0
    unrecognised = 0
    wrote_text = False
    ends_with_newline = True
    seen_keys: list[str] = []
    keys_truncated = False
    lines = 0

    for raw_line in response:
        lines += 1
        try:
            line = raw_line.decode("utf-8").strip()
        except UnicodeDecodeError:
            print(f"error: response line {lines} is not UTF-8.", file=sys.stderr)
            failed = True
            continue
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            print(f"error: response line {lines} is not JSON.", file=sys.stderr)
            failed = True
            continue
        if not isinstance(payload, dict):
            print(f"error: response line {lines} is not a JSON object.", file=sys.stderr)
            failed = True
            continue

        if payload.get("type") == "error":
            message = next(
                (payload[key] for key in CHAT_ERROR_TEXT_KEYS if isinstance(payload.get(key), str)),
                None,
            )
            print(
                f"error: {message or 'the desktop reported an error with no message.'}",
                file=sys.stderr,
            )
            failed = True
            continue

        text = next(
            (payload[key] for key in CHAT_TEXT_KEYS if isinstance(payload.get(key), str)),
            None,
        )
        if text is None:
            unrecognised += 1
            for key in payload:
                if not isinstance(key, str) or key in seen_keys:
                    continue
                if len(seen_keys) >= SEEN_KEY_LIMIT:
                    keys_truncated = True
                    break
                seen_keys.append(key)
            continue
        recognised += 1
        if text:
            sys.stdout.write(text)
            sys.stdout.flush()
            wrote_text = True
            ends_with_newline = text.endswith("\n")

    # The two lines below look like mismatched siblings — one guarded, one not —
    # and they are deliberately not siblings. The newline is conditional because
    # adding a second one to a stream that already ended in `\n` puts a blank
    # line under every answer; the flush is unconditional because a stream whose
    # last chunk DID end in `\n` still has to reach the terminal.
    if wrote_text and not ends_with_newline:
        sys.stdout.write("\n")
    sys.stdout.flush()

    # `truncated` rather than a silent cap: a key list that stopped early while
    # still reading as complete is the one thing this note must not be, since its
    # whole job is telling the desktop team which key we failed to recognise.
    key_note = (
        f" keys seen: {', '.join(seen_keys)}{' (truncated)' if keys_truncated else ''}."
        if seen_keys
        else ""
    )
    if recognised == 0 and not failed:
        raise KitError(
            f"the desktop returned {lines} line(s), none carrying a recognised text key "
            f"with a string value ({', '.join(CHAT_TEXT_KEYS)}).{key_note} Nothing was "
            f"printed, so this run "
            "proved nothing about the agent. The key set is the provisional seam D10 "
            "names — if the desktop now emits a different one, that name belongs in "
            "CHAT_TEXT_KEYS."
        )
    if unrecognised:
        print(
            f"warning: {unrecognised} response line(s) carried no recognised text key "
            f"with a string value ({', '.join(CHAT_TEXT_KEYS)}).{key_note}",
            file=sys.stderr,
        )
    return 1 if failed else 0


def cmd_chat(args: argparse.Namespace) -> int:
    """Send one prompt to a locally running agent through Cinna Desktop (D10).

    PROVISIONAL SURFACE, recorded here rather than only in the answer-back so it
    cannot calcify by being forgotten. The desktop has not built this endpoint
    yet. Two things below are a seam, not settled contract, and both are marked
    at their own site as well:

    * `chat_path` defaults to `/chat` when `desktop.json` declares none;
    * a line's text is the first present of `CHAT_TEXT_KEYS` — `text`, then
      `content`, then `delta`.

    When the endpoint ships, both collapse to the one shape it emits. Until
    then they are what spares us a round-trip, and they are the two things a
    reader should expect to change.

    What is NOT provisional: this verb never falls back to role-play. Every
    failure below exits non-zero with one line and prints nothing that could be
    mistaken for the agent's answer. A tester who cannot tell whether they were
    talking to the agent or to an assistant imitating it has learned nothing,
    which is the entire reason the verb exists.

    Nothing on any path prints the token or any of `desktop.json`'s contents —
    including the URL, and including the text of every exception, which is why
    `_network_reason` exists instead of `str(exc)`.
    """
    agent_dir = Path(args.path).expanduser().resolve()
    if not agent_dir.is_dir():
        raise KitError(f"not an agent folder: {agent_dir}")
    if not args.prompt.strip():
        raise KitError("nothing to send — the prompt is empty.")

    base_url, chat_path, token = _read_desktop_connection(agent_dir)
    request = urllib.request.Request(
        _chat_url(base_url, chat_path),
        data=json.dumps({"prompt": args.prompt}, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/x-ndjson",
            "User-Agent": "cinna-kit/kit.py",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )

    try:
        with _chat_opener().open(request, timeout=CHAT_TIMEOUT) as response:
            status = getattr(response, "status", None)
            # A backstop, and reachable only if the opener above is ever built
            # without `HTTPErrorProcessor` — which normally raises `HTTPError`
            # for anything outside 2xx before this line runs. Kept because the
            # handler list is OURS to change and this file has already been bitten
            # once by a guard that a change upstream made unreachable; named as a
            # backstop so nobody later reads it as the primary check.
            if not isinstance(status, int) or not 200 <= status < 300:
                raise KitError(
                    f"the desktop's chat endpoint answered HTTP {status}; not a chat stream."
                )
            return _stream_chat_response(response)
    except urllib.error.HTTPError as exc:
        exc.close()
        # The 401 hint is attached to 401 ONLY. A remedy sentence printed under
        # every status is worse than none: it is advice that is wrong most of
        # the times it appears, and a reader who follows it once and gets
        # nowhere stops reading the whole line.
        hint = (
            " The token in the state file is no longer the one the desktop issued —"
            " toggle Connected off and on in Cinna Desktop to reissue it."
            if exc.code == 401
            else ""
        )
        raise KitError(
            f"the desktop's chat endpoint refused the call ({_network_reason(exc)})."
            + hint
        ) from None
    except (urllib.error.URLError, OSError) as exc:
        raise KitError(
            f"could not reach the desktop's local API ({_network_reason(exc)}). "
            "Cinna Desktop must be running with this agent connected."
        ) from None


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
    new_parser.add_argument(
        "--description",
        help="one sentence describing what the agent does (defaults to the scaffold placeholder)",
    )
    new_parser.add_argument("--root", help="workshop root that holds Local/ and Cloud/")
    new_parser.add_argument(
        "--json",
        action="store_true",
        help="print only {path, slug, id, contract_version} as JSON",
    )
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
    export_parser.add_argument(
        "--hash",
        action="store_true",
        help="also print the exported tree's content_hash on its own final line",
    )
    export_parser.set_defaults(func=cmd_export)

    chat_parser = subparsers.add_parser(
        "chat",
        help="send one prompt to a locally running agent through Cinna Desktop",
    )
    chat_parser.add_argument("path", help="path to the agent folder, e.g. Local/<slug>")
    chat_parser.add_argument("prompt", help="the message to send, quoted")
    chat_parser.set_defaults(func=cmd_chat)

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
    except (OSError, json.JSONDecodeError) as exc:
        # `KitError` alone left a network or filesystem failure to reach the top
        # level as a traceback — predicted while `chat` was still unwritten, and
        # `chat` is the command that makes it reachable. `urllib.error.URLError`
        # (and `HTTPError` beneath it) are `OSError` subclasses, so one clause
        # covers both families, and a JSON file that will not parse is the other
        # thing an ordinary user can hand this tool.
        #
        # A traceback is still the right answer for a genuine bug, so nothing
        # broader is caught here: a stack trace for a defect is information, a
        # stack trace for a refused connection is noise the user cannot act on.
        #
        # Safe to interpolate: an `OSError` stringifies its errno, message and
        # (when set) filename, and a `JSONDecodeError` its position — none of
        # them a header, a body, or a request object. The `chat` path does not
        # rely on that, and converts its own network failures to `KitError`
        # through `_network_reason` before they can reach here.
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
