"""Agent skills — ``SKILL.md`` parsing, validation and tree hashing.

An *agent skill* is a folder shaped like the open Agent Skills standard::

    skills/<name>/
    ├── SKILL.md            # required; YAML frontmatter + markdown body
    ├── scripts/            # optional
    ├── references/         # optional
    └── assets/             # optional

This module is the single source of truth for what a valid skill folder is.
It is consumed on **both sides of the container boundary**:

* the host backend imports it as ``app.services.agents.skill_manifest``;
* env-core imports the vendored copy at
  ``backend/app/env-templates/app_core_base/core/server/skill_manifest.py``.

env-core runs inside the agent container from ``/app/core`` and cannot import
backend modules, so the file is **vendored byte-identically** — the same
host ⇄ container mirror precedent as ``OPENCODE_RUNTIME_DIR_TEMPLATE``. A unit
test asserts the two files are identical; keep them that way by editing this
file and copying it over, never by editing one side.

Consequences of the mirror, which shape every choice below:

* **stdlib only** — no pydantic, no PyYAML, no project imports. The container
  image does not declare PyYAML, so frontmatter is parsed by the restricted
  subset parser in this module rather than ``yaml.safe_load``: one parser, one
  behaviour, host and container alike.
* **pure** — no I/O beyond reading the tree it is pointed at, no logging
  configuration, no globals that outlive a call.

How the frontmatter parser diverges from real YAML
--------------------------------------------------

:func:`parse_frontmatter` covers the shapes the Agent Skills standard actually
uses — top-level ``key: value`` scalars, flow (``[a, b]``) and block
(``- item``) sequences, block scalars (``|`` / ``>`` and their ``-`` / ``+``
variants), one level of nested mapping, a closing fence of ``---`` or ``...``.
Everything below is a deliberate difference from ``yaml.safe_load``. Read them
before widening what a ``SKILL.md`` may contain:

* **Inline comments are not stripped.** Only a line whose stripped form
  *starts* with ``#`` is a comment, so ``name: foo # note`` yields the string
  ``foo # note``.
* **Flow sequences split naively on commas.** ``[ "a, b", c ]`` becomes three
  items, not two — a comma inside quotes still splits.
* **Block-scalar chomping indicators are accepted but not honoured.** ``|``,
  ``|-`` and ``|+`` behave identically, as do ``>``, ``>-`` and ``>+``. Every
  line is stripped, so indentation inside a block scalar is lost, and the
  folded (``>``) form joins blank lines with a single space like any other
  line, so paragraph breaks do not survive.
* **Nesting is one level deep, and depth is not tracked.** A nested mapping's
  values are coerced as scalars. Deeper structure is not dropped, it is
  mangled: a grandchild ``key: value`` is hoisted into the *same* mapping,
  where it can silently overwrite a sibling, and any ``- item`` under a nested
  key is collected into a sequence that replaces the mapping outright.
* **Unclassifiable lines are skipped, never raised on.** A malformed line
  simply disappears; only a missing or unterminated fence raises.
* **Scalar coercion uses a fixed, narrow token set.** ``yes`` / ``no`` are
  booleans (YAML 1.1, not 1.2) but ``on`` / ``off`` are not; ``null`` and
  ``~`` are null; a number is an optional minus followed by digits, optionally
  a dot and more digits — so ``1e3``, ``0x1f``, ``+5`` and ``.5`` stay
  strings, while a non-ASCII decimal digit does coerce to an int, because the
  digit class is Python's Unicode-aware one. Everything unrecognised stays a
  string, which is the safe posture for the unknown keys this module passes
  through untouched.
* **No anchors, aliases, tags, complex keys or multi-document streams.**
* **Keys are restricted to ASCII letters, digits, underscore, dot and
  hyphen.** Any other key shape is skipped along with its value.
* **Quote stripping does no escape processing.** Only a matching leading and
  trailing ``'`` or ``"`` pair is removed; a backslash-n inside the quotes
  stays two literal characters.
* **Duplicate keys: the last one wins**, silently.
* **``SKILL.md`` is decoded as utf-8-sig**, so an editor-written file that
  carries a byte-order mark still parses — the mark would otherwise sit in
  front of the opening ``---`` and fail the fence test.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Contract constants
# ---------------------------------------------------------------------------

#: Skill directory / frontmatter ``name`` shape.
SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

MAX_NAME_LENGTH = 64
MAX_DESCRIPTION_LENGTH = 1024

#: ``SKILL.md`` bodies above this are still projected, but flagged ``oversized``.
MAX_BODY_BYTES = 64 * 1024

#: Per-agent caps. Overflow entries are reported with ``error="budget"`` and
#: never projected, so a runaway tree cannot flood the engine's skill index.
DEFAULT_MAX_SKILLS = 50
DEFAULT_MAX_TOTAL_BYTES = 16 * 1024 * 1024

#: Platform slash-command names a skill may not take, because ``/<name>`` would
#: resolve to the command instead of the skill.
#:
#: The authority is ``app/services/agents/commands/__init__.py``; this tuple is
#: a vendored mirror of it for the same reason the whole module is vendored
#: (env-core cannot import the command registry). Adding a platform command
#: means adding it here too.
RESERVED_SKILL_NAMES: frozenset[str] = frozenset(
    {
        "files",
        "files-all",
        "run",
        "run-list",
        "skills",
        "session-recover",
        "session-reset",
        "session-improve",
        "webapp",
        "rebuild-env",
        "agent-status",
    }
)

#: Directories never walked when scanning a skill tree (regenerated junk).
SKIP_DIR_NAMES: frozenset[str] = frozenset(
    {".git", ".venv", "__pycache__", ".mypy_cache", ".ruff_cache", "node_modules"}
)

#: Basenames that can hold a credential value and must never travel.
#:
#: Content-identical to ``docs/local_agent_kit/layout.json`` →
#: ``secret_files.rules``, which is the contract's authority. Mirrored here
#: (rather than read from the file) because this module runs inside the
#: container, where the kit contract is not present.
SECRET_FILE_RULES: tuple[dict[str, Any], ...] = (
    {
        "id": "dotenv",
        "match": {
            "basename_equals": (".env",),
            "basename_prefix": (".env.",),
            "basename_suffix": (".env",),
        },
        "unless": {"basename_suffix": (".example", ".sample", ".template")},
    },
)

#: Names and globs that are almost always key material. Reported, never opened.
SECRET_FILENAMES: tuple[str, ...] = (
    "credentials.json",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
)
SECRET_GLOBS: tuple[str, ...] = ("*.pem", "*.p12", "*.pfx", "*.key")


# ---------------------------------------------------------------------------
# Issue vocabulary
# ---------------------------------------------------------------------------

#: Every condition a skill can be flagged with, and the sentence that explains
#: it. Codes are the stable contract — clients pick a status tone from the code
#: and never parse the message; the message is what a human reads.
#:
#: Keeping both in one table (rather than a bare code on one side and copy on
#: the other) is what stops the container, the cache and the UI from disagreeing
#: about what "budget" means.
ISSUE_MESSAGES: dict[str, str] = {
    # Errors — the skill is excluded from the projection.
    "not_a_directory": "Only folders directly under skills/ are read as skills.",
    "missing_skill_md": "This folder has no SKILL.md.",
    "unreadable": "SKILL.md could not be read.",
    "invalid_frontmatter": "SKILL.md has no valid frontmatter block.",
    "missing_name": "SKILL.md frontmatter has no name.",
    "invalid_name": (
        "The name must be lowercase words joined by single hyphens, "
        "at most 64 characters."
    ),
    "name_mismatch": "The name in SKILL.md does not match the folder name.",
    "reserved_name": "This name is reserved by a platform command.",
    "missing_description": "SKILL.md frontmatter has no description.",
    "description_too_long": "The description is longer than 1024 characters.",
    "budget": "Excluded — the agent is over its skill count or size budget.",
    "projection_error": "This skill could not be copied to the engine.",
    # Warnings — the skill still works.
    "oversized": "The SKILL.md body is larger than 64 KB.",
    "shadowed": "Another installed skill uses this name.",
    "secrets": "This skill holds files that look like credentials.",
}

#: Warning precedence, most severe first. ``secrets`` outranks the rest because
#: it is the only warning that blocks publishing; ``shadowed`` outranks
#: ``oversized`` because a shadowed skill may not be the one that runs.
WARNING_PRECEDENCE: tuple[str, ...] = ("secrets", "shadowed", "oversized")


@dataclass
class SkillIssue:
    """One flagged condition on a skill: a machine code plus a human sentence.

    ``paths`` is populated only for ``secrets`` — the relative paths of the
    files that tripped the predicate, which is what the publish rejection lists
    and what the card shows before publish. Every other code leaves it empty.
    """

    code: str
    message: str = ""
    paths: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.message:
            self.message = ISSUE_MESSAGES.get(self.code, self.code)

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "paths": list(self.paths)}


def issue_from_dict(value: Any) -> "SkillIssue | None":
    """Rebuild a :class:`SkillIssue` from its JSON form.

    Tolerates the shapes a cached row can legitimately hold: ``None``, the
    mapping :meth:`SkillIssue.to_dict` writes, and a bare code string (what a
    pre-``{code, message}`` cache row carries) — the cache is refreshed from
    the container, never migrated, so the old shape has to read cleanly until
    the next refresh replaces it.
    """
    if value is None:
        return None
    if isinstance(value, SkillIssue):
        return value
    if isinstance(value, str):
        return SkillIssue(value)
    if isinstance(value, dict):
        code = value.get("code")
        if not isinstance(code, str) or not code:
            return None
        message = value.get("message")
        paths = value.get("paths")
        return SkillIssue(
            code=code,
            message=message if isinstance(message, str) else "",
            paths=[p for p in paths if isinstance(p, str)]
            if isinstance(paths, list)
            else [],
        )
    return None


def pick_warning(*candidates: "SkillIssue | None") -> "SkillIssue | None":
    """Return the most severe of ``candidates`` per :data:`WARNING_PRECEDENCE`.

    A skill can trip several warnings at once (an oversized body that also
    carries a stray ``.env``), and the index shows one. Deciding here — rather
    than at each call site — is what keeps env-core, the cache and the card
    showing the same flag.
    """
    present = [c for c in candidates if c is not None]
    if not present:
        return None
    ranked = sorted(
        present,
        key=lambda issue: (
            WARNING_PRECEDENCE.index(issue.code)
            if issue.code in WARNING_PRECEDENCE
            else len(WARNING_PRECEDENCE)
        ),
    )
    return ranked[0]


# ---------------------------------------------------------------------------
# Index entry
# ---------------------------------------------------------------------------


@dataclass
class SkillEntry:
    """One row of the parsed skill index.

    The same shape is returned by env-core's ``GET /config/skills``, cached on
    the environment row, derived into a bundle revision's ``skills_summary``
    and rendered on the agent page — so it is deliberately flat and JSON-safe.
    """

    name: str
    description: str = ""
    source: str = "local"  # "local" | "plugin" | "catalog"
    plugin_ref: str | None = None
    path: str = ""
    has_scripts: bool = False
    user_invocable: bool = True
    model_invocable: bool = True
    size_bytes: int = 0
    error: SkillIssue | None = None
    warning: SkillIssue | None = None
    #: Relative paths inside the skill folder that tripped the secret
    #: predicate. Always present (empty = clean) so a consumer can test the
    #: scan result without having to first work out whether the scan ran.
    secret_paths: list[str] = field(default_factory=list)
    frontmatter: dict[str, Any] = field(default_factory=dict)

    @property
    def is_valid(self) -> bool:
        """True when the skill may be projected to the engine."""
        return self.error is None

    @property
    def is_publishable(self) -> bool:
        """True when this skill could be published as-is.

        Publishing needs more than projecting: an invalid skill is rejected and
        so is a clean one that carries key material. This is the predicate both
        the publish hard-block and the entry's ``can_publish`` flag read, so the
        card can never offer a verb the server would refuse.
        """
        return self.error is None and not self.secret_paths

    def to_dict(self, *, include_frontmatter: bool = False) -> dict[str, Any]:
        """JSON-safe projection. Frontmatter is opt-in (it is only needed by
        the catalog publish path, never by the index the UI renders)."""
        data: dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "source": self.source,
            "plugin_ref": self.plugin_ref,
            "path": self.path,
            "has_scripts": self.has_scripts,
            "user_invocable": self.user_invocable,
            "model_invocable": self.model_invocable,
            "size_bytes": self.size_bytes,
            "error": self.error.to_dict() if self.error else None,
            "warning": self.warning.to_dict() if self.warning else None,
            "secret_paths": list(self.secret_paths),
        }
        if include_frontmatter:
            data["frontmatter"] = self.frontmatter
        return data


# ---------------------------------------------------------------------------
# Frontmatter parsing (restricted YAML subset)
# ---------------------------------------------------------------------------


def _coerce_scalar(raw: str) -> Any:
    """Turn one scalar token into a Python value.

    Handles quoted strings, booleans, null and numbers; everything else stays
    a string. Unknown frontmatter keys are passed through untouched, so a
    conservative coercion is the right posture — misreading a publisher's
    custom key as a number would be worse than keeping it as text.
    """
    value = raw.strip()
    if not value:
        return ""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    lowered = value.lower()
    if lowered in ("true", "yes"):
        return True
    if lowered in ("false", "no"):
        return False
    if lowered in ("null", "~"):
        return None
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    if re.fullmatch(r"-?\d+\.\d+", value):
        return float(value)
    return value


def _parse_flow_sequence(raw: str) -> list[Any]:
    """``[a, b, "c"]`` → ``["a", "b", "c"]``."""
    inner = raw.strip()[1:-1].strip()
    if not inner:
        return []
    return [_coerce_scalar(part) for part in inner.split(",")]


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split a ``SKILL.md`` into (frontmatter mapping, markdown body).

    Supports the shapes the Agent Skills standard actually uses: top-level
    ``key: value`` scalars, block (``- item``) and flow (``[a, b]``) sequences,
    block scalars (``|`` / ``>``) and one level of nested mapping. Anything it
    cannot classify is kept as text rather than dropped.

    Raises:
        ValueError: when the document has no closing frontmatter fence.
    """
    if not text.startswith("---"):
        raise ValueError("missing frontmatter fence")

    lines = text.splitlines()
    end_index: int | None = None
    for index in range(1, len(lines)):
        if lines[index].strip() in ("---", "..."):
            end_index = index
            break
    if end_index is None:
        raise ValueError("unterminated frontmatter fence")

    body = "\n".join(lines[end_index + 1 :]).lstrip("\n")
    mapping: dict[str, Any] = {}

    index = 1
    while index < end_index:
        line = lines[index]
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or _indent_of(line) > 0:
            index += 1
            continue

        match = re.match(r"^([A-Za-z0-9_.\-]+)\s*:\s*(.*)$", stripped)
        if match is None:
            index += 1
            continue

        key, raw_value = match.group(1), match.group(2).strip()

        # Block scalar: `key: |` / `key: >` followed by an indented block.
        if raw_value in ("|", ">", "|-", ">-", "|+", ">+"):
            block: list[str] = []
            index += 1
            while index < end_index and (
                not lines[index].strip() or _indent_of(lines[index]) > 0
            ):
                block.append(lines[index].strip())
                index += 1
            joiner = "\n" if raw_value.startswith("|") else " "
            mapping[key] = joiner.join(block).strip()
            continue

        if raw_value.startswith("[") and raw_value.endswith("]"):
            mapping[key] = _parse_flow_sequence(raw_value)
            index += 1
            continue

        if raw_value:
            mapping[key] = _coerce_scalar(raw_value)
            index += 1
            continue

        # Empty value: a block sequence or a nested mapping follows.
        index += 1
        items: list[Any] = []
        nested: dict[str, Any] = {}
        while index < end_index:
            child = lines[index]
            if not child.strip():
                index += 1
                continue
            if _indent_of(child) == 0 and not child.strip().startswith("- "):
                break
            child_stripped = child.strip()
            if child_stripped.startswith("- "):
                items.append(_coerce_scalar(child_stripped[2:]))
            else:
                child_match = re.match(
                    r"^([A-Za-z0-9_.\-]+)\s*:\s*(.*)$", child_stripped
                )
                if child_match is not None:
                    nested[child_match.group(1)] = _coerce_scalar(
                        child_match.group(2)
                    )
            index += 1
        if items:
            mapping[key] = items
        elif nested:
            mapping[key] = nested
        else:
            mapping[key] = ""

    return mapping, body


# ---------------------------------------------------------------------------
# Secret predicate (§4 "No secrets in snapshots")
# ---------------------------------------------------------------------------


_SECRET_CLAUSE_TESTS = {
    "basename_equals": lambda name, value: name == value,
    "basename_prefix": lambda name, value: name.startswith(value),
    "basename_suffix": lambda name, value: name.endswith(value),
}


def _secret_clause_hits(
    name: str, clauses: dict[str, Any] | None, *, on_unknown: bool
) -> bool:
    """True when any clause of ``clauses`` matches ``name``.

    ``on_unknown`` is the fail-safe direction for a clause vocabulary this
    build does not understand: a ``match`` clause it cannot evaluate counts as
    a hit, an ``unless`` clause it cannot evaluate counts as a miss — so an
    unreadable rule withholds the file rather than releasing it.
    """
    if not clauses:
        return False
    for clause, values in clauses.items():
        test = _SECRET_CLAUSE_TESTS.get(clause)
        if test is None:
            return on_unknown
        usable = [value for value in values if isinstance(value, str) and value]
        if not usable:
            return on_unknown
        if any(test(name, value) for value in usable):
            return True
    return False


def is_secret_filename(name: str) -> bool:
    """True when this basename can hold a credential value and must not travel."""
    if name in SECRET_FILENAMES:
        return True
    if any(fnmatch.fnmatch(name, pattern) for pattern in SECRET_GLOBS):
        return True
    for rule in SECRET_FILE_RULES:
        if not _secret_clause_hits(name, rule.get("match"), on_unknown=True):
            continue
        if _secret_clause_hits(name, rule.get("unless"), on_unknown=False):
            continue
        return True
    return False


def validate_tree(root: str | Path) -> list[str]:
    """Return the relative paths under ``root`` that look like key material.

    An empty list means the tree is safe to snapshot or publish. Used by the
    agent-page card to warn before publish and by the publish path to reject
    (422) — same predicate on both ends so the warning and the refusal cannot
    disagree.
    """
    root_path = Path(root)
    hits: list[str] = []
    if not root_path.is_dir():
        return hits
    for dir_path, dir_names, file_names in os.walk(root_path):
        dir_names[:] = sorted(d for d in dir_names if d not in SKIP_DIR_NAMES)
        for file_name in sorted(file_names):
            if not is_secret_filename(file_name):
                continue
            relative = Path(dir_path, file_name).relative_to(root_path)
            hits.append(relative.as_posix())
    return hits


# ---------------------------------------------------------------------------
# Parsing / scanning
# ---------------------------------------------------------------------------


def is_reserved_name(name: str) -> bool:
    """True when ``name`` collides with a platform slash command."""
    return name in RESERVED_SKILL_NAMES


def _walk_files(root: Path):
    """Yield ``(relative_posix_path, os.stat_result)`` for every regular file.

    Symlinks are skipped at both the directory and the file level: the
    projector refuses to follow them, so hashing or sizing them would report a
    tree that is not the tree that gets copied.
    """
    for dir_path, dir_names, file_names in os.walk(root, followlinks=False):
        dir_names[:] = sorted(
            d
            for d in dir_names
            if d not in SKIP_DIR_NAMES and not Path(dir_path, d).is_symlink()
        )
        for file_name in sorted(file_names):
            full = Path(dir_path, file_name)
            if full.is_symlink():
                continue
            try:
                stat_result = full.stat()
            except OSError:
                continue
            yield full.relative_to(root).as_posix(), stat_result


def _measure_tree(root: Path) -> tuple[int, list[str]]:
    """One walk, two answers: total bytes and the secret-looking files.

    Fused deliberately — the size is needed for the budget cap and the secret
    hits for the publish gate, and walking a skill tree twice per index build
    is the kind of cost that shows up on an agent with fifty skills.
    """
    total = 0
    secrets: list[str] = []
    for relative, stat_result in _walk_files(root):
        total += stat_result.st_size
        if is_secret_filename(relative.rsplit("/", 1)[-1]):
            secrets.append(relative)
    return total, secrets


def parse_skill_dir(
    path: str | Path,
    *,
    source: str = "local",
    plugin_ref: str | None = None,
    rel_path: str | None = None,
) -> SkillEntry:
    """Parse and validate one ``skills/<name>/`` directory.

    Never raises: an unreadable or malformed skill comes back as a
    :class:`SkillEntry` carrying an ``error`` code, because the index has to
    render the problem rather than disappear on it.

    ``error`` and ``warning`` are :class:`SkillIssue` values — a stable ``code``
    plus a human sentence — so a client picks its status tone from the code and
    never matches on text. Error codes: ``not_a_directory``,
    ``missing_skill_md``, ``unreadable``, ``invalid_frontmatter``,
    ``missing_name``, ``invalid_name``, ``name_mismatch``, ``reserved_name``,
    ``missing_description``, ``description_too_long``. Warning codes:
    ``secrets``, ``oversized`` (``shadowed`` is added by the index builder,
    which is the only layer that can see two sources at once).
    """
    skill_dir = Path(path)
    dir_name = skill_dir.name
    entry_path = rel_path if rel_path is not None else f"skills/{dir_name}"

    def _fail(code: str) -> SkillEntry:
        return SkillEntry(
            name=dir_name,
            source=source,
            plugin_ref=plugin_ref,
            path=entry_path,
            error=SkillIssue(code),
        )

    if not skill_dir.is_dir() or skill_dir.is_symlink():
        return _fail("not_a_directory")

    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        return _fail("missing_skill_md")

    try:
        # utf-8-sig, not utf-8: an editor-written SKILL.md may carry a BOM, and
        # a leading \ufeff would make the `---` fence test fail and exclude an
        # otherwise perfectly good skill.
        raw = skill_md.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return _fail("unreadable")

    try:
        frontmatter, body = parse_frontmatter(raw)
    except ValueError:
        return _fail("invalid_frontmatter")

    name = frontmatter.get("name")
    if not isinstance(name, str) or not name.strip():
        return _fail("missing_name")
    name = name.strip()

    if len(name) > MAX_NAME_LENGTH or not SKILL_NAME_RE.match(name):
        return _fail("invalid_name")
    if name != dir_name:
        return _fail("name_mismatch")
    if is_reserved_name(name):
        return _fail("reserved_name")

    description = frontmatter.get("description")
    if not isinstance(description, str) or not description.strip():
        return _fail("missing_description")
    description = description.strip()
    if len(description) > MAX_DESCRIPTION_LENGTH:
        return _fail("description_too_long")

    scripts_dir = skill_dir / "scripts"
    size_bytes, secret_paths = _measure_tree(skill_dir)
    warning = pick_warning(
        SkillIssue("secrets", paths=secret_paths) if secret_paths else None,
        SkillIssue("oversized")
        if len(body.encode("utf-8")) > MAX_BODY_BYTES
        else None,
    )

    # `disable-model-invocation` and `user-invocable` are the only optional
    # keys the platform interprets: they decide whether the skill shows up in
    # the slash-command popup and whether the model may reach for it itself.
    return SkillEntry(
        name=name,
        description=description,
        source=source,
        plugin_ref=plugin_ref,
        path=entry_path,
        has_scripts=scripts_dir.is_dir() and any(scripts_dir.iterdir()),
        user_invocable=frontmatter.get("user-invocable", True) is not False,
        model_invocable=frontmatter.get("disable-model-invocation", False) is not True,
        size_bytes=size_bytes,
        error=None,
        warning=warning,
        secret_paths=secret_paths,
        frontmatter=frontmatter,
    )


def scan_skills_root(
    root: str | Path,
    *,
    source: str = "local",
    plugin_ref: str | None = None,
    rel_root: str = "skills",
    max_skills: int = DEFAULT_MAX_SKILLS,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
) -> list[SkillEntry]:
    """Parse every ``<root>/<name>/`` directory into an index, name-sorted.

    A missing ``root`` yields an empty list — an agent with no ``skills/``
    directory behaves exactly as it did before the feature existed.

    ``rel_root`` is the workspace-relative location of ``root``, which is what
    each entry's ``path`` is built from. It must be passed for a plugin's skill
    folder (``plugins/<mkt>/<plugin>/skills``): the path is how a caller reads
    the ``SKILL.md`` back, so a default of ``skills`` there would point at the
    agent's own folder and read the wrong file.

    Caps are applied after parsing, in name order, so which skills survive a
    budget overflow is deterministic rather than filesystem-order luck.
    Overflow entries stay in the list with ``error="budget"`` so the UI can
    explain the exclusion instead of silently losing a skill.
    """
    root_path = Path(root)
    if not root_path.is_dir():
        return []

    try:
        children = sorted(root_path.iterdir(), key=lambda p: p.name)
    except OSError:
        return []

    entries: list[SkillEntry] = []
    for child in children:
        # A plain file at the skills root is skipped silently, not reported as
        # ``not_a_directory``: the Local Agent Kit ships ``skills/README.md`` as
        # scaffolding, so a file here is the normal case, not a mistake worth
        # putting a red row on the card for. ``not_a_directory`` stays reachable
        # through :func:`parse_skill_dir` for a caller that names one directly.
        if child.name.startswith(".") or not child.is_dir():
            continue
        entries.append(
            parse_skill_dir(
                child,
                source=source,
                plugin_ref=plugin_ref,
                rel_path=f"{rel_root}/{child.name}",
            )
        )

    entries.sort(key=lambda e: e.name)
    apply_budget(entries, max_skills=max_skills, max_total_bytes=max_total_bytes)
    return entries


def apply_budget(
    entries: list[SkillEntry],
    *,
    max_skills: int = DEFAULT_MAX_SKILLS,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
) -> list[SkillEntry]:
    """Stamp ``error="budget"`` on everything past the caps. Mutates in place.

    Separate from :func:`scan_skills_root` because the caps are **per agent**
    (§3.1) while a scan only ever sees one root: an index that merges the
    workspace's skills with every plugin's has to re-apply them across the
    merged list, or a plugin-heavy agent would quietly get several times the
    budget.

    Order decides who survives, so callers sort first — the list is walked as
    given. Entries that already carry an error are skipped and do not consume
    budget: a broken skill is not projected, so charging it would exclude a
    working one for nothing.

    Re-application is safe — the merged pass sees the UNION of the per-root
    passes, not a subset, so the reason is per-entry rather than per-set: an
    entry already carrying an error is skipped and charges nothing, so a second
    pass can only add exclusions, never resurrect one. And because one root
    that saturates its own cap also saturates the shared one, the merged result
    is exactly "the first N of the merged list" — which is the non-obvious part
    of why applying the caps twice does not double-count.
    """
    accepted = 0
    total_bytes = 0
    for entry in entries:
        if entry.error is not None:
            continue
        if accepted >= max_skills or total_bytes + entry.size_bytes > max_total_bytes:
            entry.error = SkillIssue("budget")
            continue
        accepted += 1
        total_bytes += entry.size_bytes
    return entries


def tree_hash(root: str | Path) -> str:
    """Stable SHA-256 over a skills tree: relative path, size and mtime_ns.

    Cheap by design — it is computed before every message to decide whether the
    projection needs redoing, so it must never read file contents. mtime is
    part of the digest so an in-place edit of a same-size ``SKILL.md`` still
    invalidates the projection.

    A missing root hashes the empty string, so "no skills" is a stable value
    rather than an error the callers have to special-case.
    """
    digest = hashlib.sha256()
    root_path = Path(root)
    if root_path.is_dir():
        for relative, stat_result in _walk_files(root_path):
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            digest.update(str(stat_result.st_size).encode("ascii"))
            digest.update(b"\0")
            digest.update(str(stat_result.st_mtime_ns).encode("ascii"))
            digest.update(b"\n")
    return digest.hexdigest()
