"""Pure, dependency-free prompt-sync reconcile logic.

This module holds the hashing/normalisation helpers and the three-way
reconcile decision table for the bidirectional prompt files
(``WORKFLOW_PROMPT.md``, ``ENTRYPOINT_PROMPT.md``, ``REFINER_PROMPT.md``).

It mirrors the App Sync feature's ``content_fingerprint`` no-op short-circuit
plus last-write-wins (LWW) conflict resolution, but the comparison key is a
normalised content hash (the strip()'d body), and the common ancestor is a
per-environment "last synced" hash stored on ``AgentEnvironment``.

Nothing here touches the database, the adapter, or any IO — that keeps the
decision table trivially unit-testable. The orchestration lives in
``EnvironmentService.reconcile_agent_prompts``.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from enum import Enum


# Ordered mapping of Agent prompt field -> environment docs filename.
# The order is fixed so reconcile iterates deterministically (matters only
# for stable logging / test assertions).
PROMPT_FIELDS: tuple[tuple[str, str], ...] = (
    ("workflow_prompt", "WORKFLOW_PROMPT.md"),
    ("entrypoint_prompt", "ENTRYPOINT_PROMPT.md"),
    ("refiner_prompt", "REFINER_PROMPT.md"),
)

# Oldest-possible timestamp used as the "-∞" sentinel when a side has no
# logical clock (``None``). A populated timestamp always beats this, which is
# the safe direction: it preserves whichever side actually has a known edit.
_MIN_TS = datetime.min.replace(tzinfo=timezone.utc)


def normalise(content: str | None) -> str | None:
    """Return the comparison-normalised form of a prompt body.

    ``.strip()`` removes spurious leading/trailing whitespace (e.g. a trailing
    newline appended by an editor or by the env-core write path) so the same
    logical content does not register as a conflict. Empty / whitespace-only
    content collapses to ``None`` ("no content"), distinct from the
    empty-string hash. Content is still stored and written verbatim elsewhere;
    only this comparison key is normalised.
    """
    if content is None:
        return None
    stripped = content.strip()
    return stripped or None


def content_hash(content: str | None) -> str | None:
    """SHA-256 hex digest of the normalised content, or ``None`` if empty."""
    normalised = normalise(content)
    if normalised is None:
        return None
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


class ReconcileAction(str, Enum):
    """The decision for a single prompt field on one reconcile pass."""

    NOOP = "noop"               # identical content both sides → heal base only
    PULL = "pull"               # only env changed → env → DB
    PUSH = "push"               # only DB changed → DB → env
    CONFLICT_PULL = "conflict_pull"   # both changed, LWW → env wins
    CONFLICT_PUSH = "conflict_push"   # both changed, LWW → DB wins
    SEED_PUSH = "seed_push"     # never synced, DB authoritative
    SEED_PULL = "seed_pull"     # never synced, only env has content


# Actions that result in a DB-side write (used by callers to decide whether to
# emit AGENT_UPDATED and which fields changed).
PULL_ACTIONS: frozenset[ReconcileAction] = frozenset(
    {ReconcileAction.PULL, ReconcileAction.CONFLICT_PULL, ReconcileAction.SEED_PULL}
)
# Actions that result in an env-side write.
PUSH_ACTIONS: frozenset[ReconcileAction] = frozenset(
    {ReconcileAction.PUSH, ReconcileAction.CONFLICT_PUSH, ReconcileAction.SEED_PUSH}
)


def decide(
    db_content: str | None,
    env_content: str | None,
    base_hash: str | None,
    db_ts: datetime | None,
    env_ts: datetime | None,
) -> ReconcileAction:
    """Decide the reconcile action for a single prompt field.

    Three-way merge against ``base_hash`` (the last-reconciled hash stored on
    the environment), with an LWW tiebreak on logical timestamps for genuine
    both-sides-diverged conflicts.

    Args:
        db_content: Current DB prompt content (verbatim).
        env_content: Current env file content (verbatim, ``None`` if missing/empty).
        base_hash: Last-reconciled content hash for this field on this env
            (``None`` = never reconciled).
        db_ts: Logical clock for the DB side (``Agent.<field>_updated_at``).
        env_ts: Logical clock for the env side (file mtime, already clamped
            for clock skew by the caller).

    Returns:
        The :class:`ReconcileAction` to apply.
    """
    db_hash = content_hash(db_content)
    env_hash = content_hash(env_content)

    # 1. Identical content both sides → nothing to write, just heal the base.
    if db_hash == env_hash:
        return ReconcileAction.NOOP

    # From here db_hash != env_hash.

    # 2. Env side blank/missing while DB has content → restore the env file.
    #    Covers a deleted or emptied env file; never let a None env overwrite a
    #    non-empty DB side (preserves today's truthiness guard).
    if env_hash is None:
        # db_hash is non-None here (they differ).
        return ReconcileAction.PUSH

    # 3. Never reconciled before → seed.
    if base_hash is None:
        if db_hash is not None:
            return ReconcileAction.SEED_PUSH  # DB authoritative at first sync
        return ReconcileAction.SEED_PULL      # only env has content

    # 4. Only one side moved relative to the baseline.
    db_changed = db_hash != base_hash
    env_changed = env_hash != base_hash

    if not db_changed and env_changed:
        return ReconcileAction.PULL   # only env changed
    if db_changed and not env_changed:
        return ReconcileAction.PUSH   # only DB changed

    # 5. Both diverged from the baseline → LWW tiebreak.
    #    ``>=`` favours env on ties — env edits are the ones being lost today.
    env_wins = (env_ts or _MIN_TS) >= (db_ts or _MIN_TS)
    return ReconcileAction.CONFLICT_PULL if env_wins else ReconcileAction.CONFLICT_PUSH
