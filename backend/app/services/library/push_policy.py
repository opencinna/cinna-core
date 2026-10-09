"""Team library push policy — the logic behind the ``pre-receive`` hook.

Pure on purpose: stdlib + ``jsonschema`` only, no ``app.core`` import and no
database, so the hook starts fast inside git's environment and unit tests can
drive :func:`main` directly. Everything it needs arrives through the
environment the smart-HTTP handler hands ``git receive-pack`` (which passes it
on to the hook):

- ``CINNA_REPO_KIND`` — ``agent`` or ``mirror`` (falls back to the bare repo's
  ``cinna.kind`` git config).
- ``CINNA_KIT_DIR`` — the Local Agent Kit snapshot (``layout.json``,
  ``CONTRACT_VERSION``, ``schema/cinna-agent.schema.json``).
- ``CINNA_GIT_MAX_BLOB_BYTES`` / ``CINNA_GIT_MAX_TREE_BYTES`` — size caps.

The pushed objects are only visible through git's quarantine variables
(``GIT_OBJECT_DIRECTORY`` / ``GIT_ALTERNATE_OBJECT_DIRECTORIES``), so every git
plumbing call runs with the hook's own environment. Nothing from the pushed tree
is ever executed or checked out.

A refusal prints one sentence on stderr (git relays it to the client as
``remote: ...``) and exits 1. The sentences follow the desktop's S9 vocabulary:
they never say branch, commit, push, pull, merge, rebase, remote, repository,
conflict or SHA. Cinna Desktop shows them verbatim under "Couldn't share".
"""
from __future__ import annotations

import json
import subprocess
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import IO, Any

MAIN_REF = "refs/heads/main"
MANIFEST_PATH = "cinna-agent.json"
ZERO_SHA_CHARS = {"0"}
GIT_TIMEOUT_SECONDS = 120
DETAIL_MAX_CHARS = 160
MIB = 1024 * 1024
SYMLINK_MODE = "120000"

# Defaults used only when the handler did not pass a cap (never in production).
DEFAULT_MAX_BLOB_BYTES = 10 * MIB
DEFAULT_MAX_TREE_BYTES = 200 * MIB

# ── Refusal sentences (S9) ───────────────────────────────────────────

MSG_UNAVAILABLE = (
    "The team library can't check this change right now. "
    "Ask your admin to look at the Cinna server."
)
MSG_OTHER_REF = "Only the agent's main version can be shared to the team library."
MSG_SECRET = "This change includes {path}, a file that can hold a password or key, so it can't be shared."
MSG_CREDENTIALS = "This change includes the agent's credentials folder, which never leaves your computer."
MSG_APP_DATA = (
    "This change includes the agent's app-data folder (its private working files), "
    "which can't be shared."
)
MSG_NEVER_TRAVELS = "This change includes {path}, which never leaves your computer."
MSG_SYMLINK = (
    "This change includes {path}, a shortcut (symlink) to another file, "
    "which can't be shared. Replace it with the file itself."
)
MSG_BLOB_TOO_LARGE = "The file {path} is larger than {n} MB, too big to share with the team."
MSG_TREE_TOO_LARGE = "This agent is larger than {n} MB in total, too big to share with the team."
MSG_MANIFEST_MISSING = "This agent has no cinna-agent.json, so it can't be shared."
MSG_MANIFEST_INVALID = "This agent's cinna-agent.json isn't valid ({detail}), so it can't be shared."
MSG_CONTRACT_NEWER = (
    "This agent was made with a newer version of Cinna than this server. "
    "Ask your admin to update Cinna."
)
MSG_CONTRACT_OLDER = (
    "This agent was made with an older version of Cinna. "
    "Open it in Cinna Desktop to update it, then share again."
)
WARN_CONTRACT_MINOR_NEWER = (
    "This agent was made with a slightly newer version of Cinna than this server. "
    "It was shared, but settings this server doesn't know yet are kept as they are."
)

# Paths of the kit's ``never_travels`` roots with a dedicated sentence.
_NEVER_TRAVELS_SENTENCES = {
    "credentials/": MSG_CREDENTIALS,
    "app-data/": MSG_APP_DATA,
}


class PushRefused(Exception):
    """A rule refused the change; ``str(exc)`` is the sentence to show."""


class PolicyUnavailable(Exception):
    """The policy cannot be evaluated (missing kit file, git failure)."""


# ── Kit rules ────────────────────────────────────────────────────────


class KitRules:
    """The parts of the kit the policy reads, loaded once per push."""

    def __init__(
        self,
        secret_rules: list[dict[str, Any]],
        never_travels: list[str],
        kit_dir: Path,
    ) -> None:
        self.secret_rules = secret_rules
        self.never_travels = never_travels
        self.kit_dir = kit_dir

    @classmethod
    def load(cls, kit_dir: str | None) -> KitRules:
        """Read ``layout.json``; any problem is :class:`PolicyUnavailable` (fail safe)."""
        if not kit_dir:
            raise PolicyUnavailable("CINNA_KIT_DIR is not set")
        root = Path(kit_dir)
        try:
            layout = json.loads((root / "layout.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise PolicyUnavailable(f"layout.json unreadable: {exc}") from exc
        if not isinstance(layout, dict):
            raise PolicyUnavailable("layout.json is not an object")

        secret_block = layout.get("secret_files")
        rules = secret_block.get("rules") if isinstance(secret_block, dict) else None
        if not isinstance(rules, list) or not rules:
            raise PolicyUnavailable("layout.json has no secret_files.rules")

        shared = layout.get("shared_remote")
        never = shared.get("never_travels") if isinstance(shared, dict) else None
        paths = never.get("paths") if isinstance(never, dict) else None
        if not isinstance(paths, list) or not all(isinstance(p, str) and p for p in paths):
            raise PolicyUnavailable("layout.json has no shared_remote.never_travels.paths")
        return cls(
            secret_rules=[r for r in rules if isinstance(r, dict)],
            never_travels=[_as_dir_prefix(p) for p in paths],
            kit_dir=root,
        )


def _as_dir_prefix(path: str) -> str:
    path = path.strip().removeprefix("./").lstrip("/")
    return path if path.endswith("/") else f"{path}/"


# Case-insensitive on purpose: ``.ENV`` or ``Credentials/`` lands on the same
# file on a case-insensitive disk (macOS, Windows) once a teammate gets it.
_SECRET_CLAUSE_TESTS = {
    "basename_equals": lambda name, value: name.casefold() == value.casefold(),
    "basename_prefix": lambda name, value: name.casefold().startswith(value.casefold()),
    "basename_suffix": lambda name, value: name.casefold().endswith(value.casefold()),
}


def _secret_clause_hits(name: str, clauses: Any, *, on_unknown: bool) -> bool:
    """Same semantics as ``skill_manifest._secret_clause_hits``: an unknown
    ``match`` clause counts as a hit, an unknown ``unless`` clause as a miss."""
    if not clauses:
        return False
    if not isinstance(clauses, dict):
        return on_unknown
    for clause, values in clauses.items():
        test = _SECRET_CLAUSE_TESTS.get(clause)
        if test is None or not isinstance(values, list):
            return on_unknown
        usable = [v for v in values if isinstance(v, str) and v]
        if not usable:
            return on_unknown
        if any(test(name, v) for v in usable):
            return True
    return False


def is_secret_path(path: str, rules: Iterable[dict[str, Any]]) -> bool:
    """True when the basename of ``path`` matches a kit ``secret_files`` rule."""
    name = path.rsplit("/", 1)[-1]
    for rule in rules:
        if not _secret_clause_hits(name, rule.get("match"), on_unknown=True):
            continue
        if _secret_clause_hits(name, rule.get("unless"), on_unknown=False):
            continue
        return True
    return False


def check_path(path: str, size: int | None, rules: KitRules, max_blob: int) -> None:
    """Refuse one blob path: never-travels root, secret file, oversized file.
    Path comparisons are case-insensitive (see ``_SECRET_CLAUSE_TESTS``)."""
    folded = path.casefold()
    for prefix in rules.never_travels:
        root = prefix.casefold()
        if folded == root.rstrip("/") or folded.startswith(root):
            sentence = _NEVER_TRAVELS_SENTENCES.get(prefix)
            raise PushRefused(sentence or MSG_NEVER_TRAVELS.format(path=prefix.rstrip("/")))
    if is_secret_path(path, rules.secret_rules):
        raise PushRefused(MSG_SECRET.format(path=path))
    if size is not None and size > max_blob:
        raise PushRefused(MSG_BLOB_TOO_LARGE.format(path=path, n=_mb(max_blob)))


def _mb(n_bytes: int) -> str:
    value = n_bytes / MIB
    return str(int(value)) if value == int(value) else f"{value:.1f}"


# ── Git plumbing ─────────────────────────────────────────────────────


class Git:
    """Plumbing calls in the hook's environment (quarantine included)."""

    def __init__(self, env: Mapping[str, str]) -> None:
        self.env = dict(env)
        self.tip_symlinks: list[str] = []

    def run(self, args: list[str], *, stdin: bytes | None = None, check: bool = True) -> bytes:
        try:
            result = subprocess.run(
                ["git", *args],
                input=stdin,
                env=self.env,
                capture_output=True,
                timeout=GIT_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise PolicyUnavailable(f"git {args[0]}: {exc}") from exc
        if check and result.returncode != 0:
            raise PolicyUnavailable(
                f"git {args[0]} failed: {result.stderr.decode('utf-8', 'replace').strip()}"
            )
        return result.stdout

    def new_blobs(self, new: str) -> list[tuple[str, int]]:
        """``(path, size)`` of every blob reachable from ``new`` but from no
        existing ref — every intermediate state, not only the tip."""
        listing = self.run(["rev-list", "--objects", new, "--not", "--all"])
        if not listing.strip():
            return []
        batch = self.run(
            [
                "cat-file",
                "--batch-check=%(objecttype) %(objectname) %(objectsize) %(rest)",
            ],
            stdin=listing,
        )
        blobs: list[tuple[str, int]] = []
        for line in batch.decode("utf-8", "surrogateescape").splitlines():
            parts = line.split(" ", 3)
            if len(parts) < 4 or parts[0] != "blob" or not parts[3]:
                continue
            blobs.append((parts[3], int(parts[2])))
        return blobs

    def tip_tree(self, new: str) -> list[tuple[str, int]]:
        """``(path, size)`` of every blob in the tip tree (``ls-tree -r -l``)."""
        out = self.run(["ls-tree", "-r", "-l", "-z", new])
        entries: list[tuple[str, int]] = []
        for record in out.decode("utf-8", "surrogateescape").split("\0"):
            if not record:
                continue
            meta, _, path = record.partition("\t")
            fields = meta.split()
            if len(fields) < 4 or fields[1] != "blob":
                continue
            size = int(fields[3]) if fields[3].isdigit() else 0
            entries.append((path, size))
            if fields[0] == SYMLINK_MODE:
                self.tip_symlinks.append(path)
        return entries

    def new_symlinks(self, new: str) -> list[str]:
        """Paths added or changed as a symlink (mode 120000) in any new commit."""
        out = self.run(
            [
                "log",
                "--format=",
                "--raw",
                "--no-abbrev",
                "--no-renames",
                "--root",
                "-m",
                "-z",
                new,
                "--not",
                "--all",
            ]
        )
        paths: list[str] = []
        fields = out.decode("utf-8", "surrogateescape").split("\0")
        i = 0
        while i < len(fields):
            meta = fields[i].strip()
            if meta.startswith(":") and i + 1 < len(fields):
                parts = meta[1:].split()
                if len(parts) >= 2 and parts[1] == SYMLINK_MODE:
                    paths.append(fields[i + 1])
                i += 2
                continue
            i += 1
        return paths

    def show_manifest(self, new: str) -> bytes | None:
        """``cinna-agent.json`` at the tip, or ``None`` when absent / not a file."""
        kind = self.run(["cat-file", "-t", f"{new}:{MANIFEST_PATH}"], check=False)
        if kind.strip() != b"blob":
            return None
        return self.run(["cat-file", "blob", f"{new}:{MANIFEST_PATH}"])

    def config_kind(self) -> str | None:
        out = self.run(["config", "--get", "cinna.kind"], check=False).strip()
        return out.decode("utf-8", "replace") or None


# ── Manifest ─────────────────────────────────────────────────────────


def _parse_version(value: Any) -> tuple[int, int] | None:
    if not isinstance(value, str):
        return None
    parts = value.split("-", 1)[0].split(".")
    if len(parts) < 2 or not parts[0].isdigit() or not parts[1].isdigit():
        return None
    return int(parts[0]), int(parts[1])


def check_manifest(raw: bytes | None, kit_dir: Path, warn: IO[str]) -> None:
    """Refuse a missing / invalid manifest or an incompatible contract major."""
    if raw is None:
        raise PushRefused(MSG_MANIFEST_MISSING)
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise PushRefused(MSG_MANIFEST_INVALID.format(detail="it isn't readable JSON"))
    if not isinstance(document, dict):
        raise PushRefused(MSG_MANIFEST_INVALID.format(detail="it isn't a JSON object"))

    try:
        import jsonschema

        schema = json.loads(
            (kit_dir / "schema" / "cinna-agent.schema.json").read_text(encoding="utf-8")
        )
        validator_cls = jsonschema.validators.validator_for(schema)
        validator = validator_cls(schema)
    except (ImportError, OSError, ValueError) as exc:
        raise PolicyUnavailable(f"agent schema unavailable: {exc}") from exc
    error = next(iter(sorted(validator.iter_errors(document), key=lambda e: list(e.path))), None)
    if error is not None:
        raise PushRefused(MSG_MANIFEST_INVALID.format(detail=_error_detail(error)))

    try:
        server = _parse_version((kit_dir / "CONTRACT_VERSION").read_text().strip())
    except OSError as exc:
        raise PolicyUnavailable(f"CONTRACT_VERSION unreadable: {exc}") from exc
    if server is None:
        raise PolicyUnavailable("CONTRACT_VERSION is not a version")
    agent = _parse_version(document.get("contract_version"))
    if agent is None:
        return  # legacy folder without a contract version: accepted
    if agent[0] > server[0]:
        raise PushRefused(MSG_CONTRACT_NEWER)
    if agent[0] < server[0]:
        raise PushRefused(MSG_CONTRACT_OLDER)
    if agent[1] > server[1]:
        print(WARN_CONTRACT_MINOR_NEWER, file=warn)


def _error_detail(error: Any) -> str:
    where = "/".join(str(p) for p in error.absolute_path)
    message = str(error.message)
    detail = f"{where}: {message}" if where else message
    if len(detail) > DETAIL_MAX_CHARS:
        detail = detail[: DETAIL_MAX_CHARS - 1] + "…"
    return detail


# ── Entry point ──────────────────────────────────────────────────────


def _int_env(environ: Mapping[str, str], key: str, default: int) -> int:
    try:
        return int(environ.get(key, ""))
    except ValueError:
        return default


def _is_zero(sha: str) -> bool:
    return bool(sha) and set(sha) <= ZERO_SHA_CHARS


def evaluate(
    updates: list[tuple[str, str, str]],
    environ: Mapping[str, str],
    warn: IO[str],
    git: Git | None = None,
) -> None:
    """Run every rule over ``updates``; raises PushRefused / PolicyUnavailable."""
    rules = KitRules.load(environ.get("CINNA_KIT_DIR"))
    git = git or Git(environ)
    max_blob = _int_env(environ, "CINNA_GIT_MAX_BLOB_BYTES", DEFAULT_MAX_BLOB_BYTES)
    max_tree = _int_env(environ, "CINNA_GIT_MAX_TREE_BYTES", DEFAULT_MAX_TREE_BYTES)
    kind = environ.get("CINNA_REPO_KIND") or git.config_kind()

    for _old, new, ref in updates:
        if ref != MAIN_REF:
            raise PushRefused(MSG_OTHER_REF)
        if _is_zero(new):
            continue  # a delete; receive.denyDeletes refuses it
        # Every new blob, so a secret added then removed is still caught.
        for path, size in git.new_blobs(new):
            check_path(path, size, rules, max_blob)
        # The tip, so a secret path whose content already exists is caught too.
        tip = git.tip_tree(new)
        for path, size in tip:
            check_path(path, size, rules, max_blob)
        # Symlinks never travel: a checkout would point outside the folder.
        for path in [*git.tip_symlinks, *git.new_symlinks(new)]:
            raise PushRefused(MSG_SYMLINK.format(path=path))
        if sum(size for _path, size in tip) > max_tree:
            raise PushRefused(MSG_TREE_TOO_LARGE.format(n=_mb(max_tree)))
        if kind == "agent":
            check_manifest(git.show_manifest(new), rules.kit_dir, warn)


def parse_updates(stdin: IO[str]) -> list[tuple[str, str, str]]:
    updates: list[tuple[str, str, str]] = []
    for line in stdin:
        parts = line.split()
        if len(parts) == 3:
            updates.append((parts[0], parts[1], parts[2]))
    return updates


def main(stdin: IO[str], environ: Mapping[str, str], stderr: IO[str] | None = None) -> int:
    """Hook entry: 0 accepts the change, 1 refuses it (sentence on stderr)."""
    import sys

    err = stderr or sys.stderr
    try:
        evaluate(parse_updates(stdin), environ, err)
    except PushRefused as refusal:
        print(str(refusal), file=err)
        return 1
    except PolicyUnavailable as exc:
        print(MSG_UNAVAILABLE, file=err)
        if environ.get("CINNA_PUSH_POLICY_DEBUG"):
            print(f"(policy: {exc})", file=err)
        return 1
    except Exception:  # noqa: BLE001 — fail safe: an unexpected error refuses
        print(MSG_UNAVAILABLE, file=err)
        return 1
    return 0
