"""Agent skills projection — mirror ``skills/`` into the engine's skill root.

Skills are authored in the agent workspace at ``/app/workspace/skills/<name>/``
(D1: top-level and bundle-owned, never ``.claude/skills/``). Neither engine
looks there, so before every message env-core **projects** the valid skills
into ``/root/.claude/skills/`` — the per-instance ``claude_sessions/`` mount,
which is the user-scope skill source both engines already read.

Design constraints this module exists to satisfy:

* **Never break a message.** Every failure is caught and logged; ``refresh()``
  returns ``changed=False`` rather than raising into the chat path. A broken
  projection degrades to "the model does not see the new skill", never to a
  failed turn.
* **Cheap when nothing changed.** The tree hash (path/size/mtime, no file
  contents) and the projected name set are both persisted, so the common case —
  the overwhelming majority of messages — is one stat walk plus one
  ``iterdir``, with no ``SKILL.md`` parsed at all.
* **Confined writes.** Only ``<home>/skills/`` is ever written, and only
  directories whose name already passed the skill-name regex. Symlinks are
  refused on both sides.
* **Never eat a user's own file.** A projected directory carries a
  ``.cinna_projected`` marker; pruning only removes directories that have it.
  Nothing places skills under ``~/.claude/skills`` today, but the guard costs
  one file write and makes the prune safe forever.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .skill_manifest import parse_skill_dir, scan_skills_root, tree_hash

logger = logging.getLogger(__name__)

#: Engine-visible skill root. ``/root/.claude`` is the writable
#: ``claude_sessions/`` bind mount (every env template mounts it rw).
DEFAULT_CLAUDE_HOME = "/root/.claude"

#: Written into each projected directory so pruning can tell our copies from a
#: skill a user placed under ``~/.claude/skills`` by hand.
PROJECTION_MARKER = ".cinna_projected"

#: Projection state, next to the skills root:
#: ``{"hash": <tree hash>, "projected": [names], "failed": [names]}``.
#:
#: ``projected`` is the set of marker-bearing directories observed on disk at
#: the end of the last run — not the set we intended to write. Recording what
#: is actually there is what lets the fast path be a plain "has anything moved
#: since I last looked", and keeps a directory we could not remove from forcing
#: a full re-projection on every subsequent message.
#:
#: ``failed`` is what keeps a durable copy failure (a full disk, a file the
#: agent chmod'd 000) from re-copying the whole tree every turn: the hash
#: latches, and only the skills that did not land are retried.
STATE_FILENAME = ".cinna_skills_hash"


@dataclass
class ProjectionResult:
    """Outcome of one :func:`refresh` call.

    ``identity`` is what consumers latch on. It covers the workspace tree hash
    AND the names actually projected, so it moves in both directions that
    matter: a skill's content changing (hash) and a previously failed skill
    finally landing (names) — the latter of which the hash alone cannot see.
    Equally important, it holds STILL while a durable failure keeps retrying,
    so a broken skill cannot make every consumer rebuild on every message.

    ``changed`` is this run's own view ("I copied or pruned something") and is
    only useful for logging; a consumer that acted on it would rebuild forever
    on a durable failure. Compare ``identity`` against what you last saw.
    """

    changed: bool = False
    #: How many marker-bearing directories the engine can see, counted on disk
    #: after the run — NOT how many this run copied. A directory that resisted
    #: pruning is included, because the engine indexes it either way.
    projected: int = 0
    hash: str = ""
    identity: str = ""
    errors: list[str] = field(default_factory=list)


def _projected_root(home: str | Path) -> Path:
    return Path(home) / "skills"


def _identity(tree: str, names: list[str] | set[str]) -> str:
    """Stable token for "what the engine can currently see".

    ``json.dumps`` rather than a separator join so the encoding is injective:
    a directory name containing the separator cannot masquerade as two names.

    Known limitation, inherited from :func:`tree_hash`: content that changes
    while keeping both size and mtime_ns (an mtime-preserving restore) does not
    move this token, so the projection is not refreshed. The mirror case — an
    mtime-only touch moving it for byte-identical content — costs one needless
    re-copy. Closing both means folding a content digest into the tree hash.
    """
    payload = json.dumps([tree, sorted(set(names))], separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _read_state(home: Path) -> dict:
    """Load the projection state, tolerating anything unreadable.

    An absent, corrupt or truncated state file is not an error — it just means
    "nothing is known", which costs one re-projection and self-heals.
    """
    try:
        raw = (home / STATE_FILENAME).read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        "hash": data.get("hash") if isinstance(data.get("hash"), str) else "",
        "projected": _string_list(data.get("projected")),
        "failed": _string_list(data.get("failed")),
    }


def _string_list(value) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _write_state(
    home: Path, tree: str, projected: list[str], failed: list[str]
) -> None:
    try:
        home.mkdir(parents=True, exist_ok=True)
        (home / STATE_FILENAME).write_text(
            json.dumps(
                {
                    "hash": tree,
                    "projected": sorted(set(projected)),
                    "failed": sorted(set(failed)),
                }
            ),
            encoding="utf-8",
        )
    except OSError as exc:
        logger.warning("Could not persist skills projection state: %s", exc)


def _is_projected(directory: Path) -> bool:
    return (directory / PROJECTION_MARKER).is_file()


def _projected_names(projected_root: Path) -> set[str]:
    """Names of the directories this module owns under the skill root."""
    if not projected_root.is_dir():
        return set()
    try:
        return {
            child.name
            for child in projected_root.iterdir()
            if child.is_dir() and _is_projected(child)
        }
    except OSError:
        return set()


def _ignore_symlinks(directory: str, names: list[str]) -> set[str]:
    """``shutil.copytree`` ignore callback that drops every symlink.

    Skipped, not copied as links: the same rule
    ``workspace_classification.safe_copytree`` applies on the host. Following a
    link would let a workspace the agent controls pull arbitrary host-visible
    content into the projection, bypass the size budget (the manifest's walk
    skips links, so a linked payload is never counted) and, for a link loop,
    recurse until ``RecursionError`` — which is not an ``OSError`` and would
    escape the per-skill guard.
    """
    return {name for name in names if Path(directory, name).is_symlink()}


def _copy_skill(source: Path, destination: Path) -> None:
    """Replace ``destination`` with a fresh copy of ``source``.

    rm-then-copy rather than a merge: a skill folder is publisher-owned as a
    whole, so a file the publisher deleted must disappear from the projection
    too.
    """
    if destination.exists() or destination.is_symlink():
        if destination.is_dir() and not destination.is_symlink():
            shutil.rmtree(destination)
        else:
            destination.unlink()
    shutil.copytree(source, destination, symlinks=False, ignore=_ignore_symlinks)


def _prune_stale(projected_root: Path, keep: set[str]) -> list[str]:
    """Remove projected directories that are no longer valid skills."""
    removed: list[str] = []
    if not projected_root.is_dir():
        return removed
    for child in sorted(projected_root.iterdir(), key=lambda p: p.name):
        if child.name in keep:
            continue
        if not child.is_dir() or child.is_symlink():
            continue
        if not _is_projected(child):
            # Somebody else's skill — leave it exactly where it is.
            continue
        try:
            shutil.rmtree(child)
            removed.append(child.name)
        except OSError as exc:
            logger.warning("Could not prune projected skill %s: %s", child.name, exc)
    return removed


def _project_one(skills_root: Path, projected_root: Path, name: str) -> str | None:
    """Copy one skill into the projection. Returns an error message or None."""
    destination = projected_root / name
    try:
        _copy_skill(skills_root / name, destination)
        (destination / PROJECTION_MARKER).write_text("", encoding="utf-8")
        return None
    except (OSError, shutil.Error) as exc:
        # shutil.Error is copytree's per-file aggregate — not an OSError, so it
        # needs naming explicitly or one unreadable file would abort the whole
        # refresh instead of excluding one skill.
        #
        # Leave nothing behind. A half-written directory, or a complete one
        # whose marker write failed, is a directory `_prune_stale` will refuse
        # to touch (no marker = somebody else's skill) while both engines
        # happily index its SKILL.md — a transient disk error would otherwise
        # plant a broken skill in the index permanently.
        shutil.rmtree(destination, ignore_errors=True)
        message = f"{name}: {exc}"
        logger.warning("Failed to project skill %s", message)
        return message


def _retry_failed(
    result: ProjectionResult,
    skills_root: Path,
    projected_root: Path,
    home_path: Path,
    tree: str,
    state: dict,
) -> ProjectionResult:
    """Fast path: the tree is unchanged, so only last run's failures are retried.

    This is what makes latching the hash after a partial failure safe. Without
    it the choice would be between never retrying a transient failure and
    re-copying every skill on every message for as long as the failure lasts.
    """
    projected = set(_string_list(state.get("projected")))
    failed = _string_list(state.get("failed"))
    result.projected = len(projected)
    result.identity = _identity(tree, projected)
    if not failed:
        return result

    landed: list[str] = []
    still_failed: list[str] = []
    for name in sorted(set(failed)):
        # `parse_skill_dir`, not `scan_skills_root`: the caps were already
        # applied when this name was put on the failed list, and the unchanged
        # tree hash pins the tree it was applied to. If that pinning ever goes
        # away, this has to become cap-aware — a budget-excluded skill must not
        # get in through the retry door.
        if not parse_skill_dir(skills_root / name).is_valid:
            # No longer a projectable skill — drop it from the retry list
            # rather than retrying something that can never land.
            continue
        error = _project_one(skills_root, projected_root, name)
        if error is None:
            landed.append(name)
        else:
            still_failed.append(name)
            result.errors.append(error)

    if set(still_failed) != set(failed):
        projected = _projected_names(projected_root)
        result.projected = len(projected)
        result.changed = bool(landed)
        result.identity = _identity(tree, projected)
        _write_state(home_path, tree, sorted(projected), still_failed)
        if landed:
            logger.info(
                "Recovered %d previously failed skill(s): %s", len(landed), landed
            )
    return result


def projection_failures(home: str | Path = DEFAULT_CLAUDE_HOME) -> set[str]:
    """Names of skills the last :func:`refresh` could not copy to the engine.

    The index builder reports these as ``projection_error`` (plan §9): a skill
    that is perfectly valid on disk but is not in ``/root/.claude/skills`` is
    invisible to the model, and a card that showed it as healthy would be
    lying about the one thing the card exists to answer.

    Reads the latched state file only — no walk, no copy — so it is cheap
    enough to call on every index build. An absent or unreadable state file
    means "nothing known to have failed", which is the right answer both
    before the first projection and after a wiped ``claude_sessions/``.
    """
    return set(_string_list(_read_state(Path(home)).get("failed")))


def refresh(
    workspace_dir: str | Path = "/app/workspace",
    home: str | Path = DEFAULT_CLAUDE_HOME,
) -> ProjectionResult:
    """Mirror the workspace's valid skills into the engine skill root.

    Returns a :class:`ProjectionResult` whose ``identity`` is the token callers
    latch per engine to decide whether that engine must rebuild its skill list.
    Claude Code never needs to: a fresh CLI subprocess per message rescans the
    directory anyway.

    Never raises.
    """
    result = ProjectionResult()
    try:
        skills_root = Path(workspace_dir) / "skills"
        home_path = Path(home)
        current_hash = tree_hash(skills_root)
        result.hash = current_hash

        state = _read_state(home_path)
        projected_root = _projected_root(home_path)
        on_disk = _projected_names(projected_root)

        if state.get("hash") == current_hash and on_disk == set(
            _string_list(state.get("projected"))
        ):
            # The tree is unchanged AND the projection it describes is still on
            # disk. Checking both matters: a state file that outlived the
            # directory (a wiped ``claude_sessions/``, a half-failed prune)
            # would otherwise latch "up to date" forever while the engine sees
            # nothing at all.
            return _retry_failed(
                result, skills_root, projected_root, home_path, current_hash, state
            )

        entries = scan_skills_root(skills_root)
        valid = [entry for entry in entries if entry.is_valid]
        expected = {entry.name for entry in valid}

        if not valid and not projected_root.is_dir():
            # An agent with no skills stays exactly as it was before this
            # feature existed (D6): no directory is created and nothing is
            # copied.
            #
            # The identity is still a real (non-empty) digest, NOT the empty
            # string. Empty is reserved for "the projection could not determine
            # anything" — sdk_manager reads it as no evidence and refuses to
            # latch it — whereas "this agent has zero skills" is a fact worth
            # latching, and it has to compare unequal to the identity of the
            # first skill the agent later writes. So on the first message of a
            # mode, where nothing is latched yet, an empty tree does report
            # ``skills_changed=True``. That costs nothing: the only consumer is
            # OpenCode's instance dispose, which is additionally gated on
            # ``not server_just_started``, and a mode's first message is always
            # the one that starts its server.
            _write_state(home_path, current_hash, [], [])
            result.identity = _identity(current_hash, [])
            return result

        projected_root.mkdir(parents=True, exist_ok=True)

        landed: list[str] = []
        failed: list[str] = []
        for entry in valid:
            error = _project_one(skills_root, projected_root, entry.name)
            if error is None:
                landed.append(entry.name)
            else:
                failed.append(entry.name)
                result.errors.append(error)

        pruned = _prune_stale(projected_root, expected)

        # Record what is ACTUALLY on disk, not what we meant to write: a
        # directory that resisted removal has to be part of the recorded state,
        # or the fast-path comparison would mismatch on every later message and
        # re-project the whole tree forever.
        on_disk = _projected_names(projected_root)
        result.projected = len(on_disk)
        result.changed = bool(landed or pruned)
        result.identity = _identity(current_hash, on_disk)
        _write_state(home_path, current_hash, sorted(on_disk), failed)

        invalid = [entry.name for entry in entries if not entry.is_valid]
        logger.info(
            "Copied %d skill(s) into %s (pruned: %s, failed: %s, invalid: %s)",
            len(landed),
            projected_root,
            pruned or "none",
            failed or "none",
            invalid or "none",
        )
        return result

    except Exception as exc:  # noqa: BLE001 — must never break the message path
        logger.error("Skills projection failed: %s", exc, exc_info=True)
        result.changed = False
        result.errors.append(str(exc))
        return result


__all__ = [
    "DEFAULT_CLAUDE_HOME",
    "PROJECTION_MARKER",
    "STATE_FILENAME",
    "ProjectionResult",
    "projection_failures",
    "refresh",
]
