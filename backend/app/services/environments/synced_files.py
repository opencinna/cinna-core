"""Synced Workspace File Registry — single declarative source of truth.

Every workspace file that the platform auto-syncs between the agent-env
container and the backend is declared here exactly once. Two sync classes
coexist under one registry:

- ``bidirectional`` — the three prompt docs (env ⇄ DB). These flow through
  ``EnvironmentService.reconcile_agent_prompts`` (three-way reconcile + LWW).
- ``pull_only`` — env-authoritative caches (env → DB only). These flow through
  their cache-refresh service's ``handle_post_action_event`` and never get the
  bidirectional reconcile.

The backend derives its event-handler wiring from this registry (see
``app/main.py``); env-core mirrors the ``rel_path`` set in its watched-file
list (``app_core_base/core/main.py:_WATCHED_FILES``). A drift-detecting unit
test asserts the two lists agree, so adding a synced file is a one-line change
here.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class SyncedFile:
    """One auto-synced workspace file.

    Attributes:
        key: Stable logical key (e.g. ``"workflow_prompt"``, ``"status"``).
        rel_path: Path relative to the workspace root, matching what env-core
            watches and what the cache services fetch.
        sync_class: ``"bidirectional"`` (reconcile + LWW) or ``"pull_only"``
            (env → DB cache, content-hash short-circuit).
    """

    key: str
    rel_path: str
    sync_class: Literal["bidirectional", "pull_only"]


SYNCED_FILES: tuple[SyncedFile, ...] = (
    # Bidirectional prompt docs — reconcile + LWW.
    SyncedFile("workflow_prompt", "docs/WORKFLOW_PROMPT.md", "bidirectional"),
    SyncedFile("entrypoint_prompt", "docs/ENTRYPOINT_PROMPT.md", "bidirectional"),
    SyncedFile("refiner_prompt", "docs/REFINER_PROMPT.md", "bidirectional"),
    # Pull-only env-authoritative caches.
    SyncedFile("cli_commands", "docs/CLI_COMMANDS.yaml", "pull_only"),
    SyncedFile("status", "app-data/storage/STATUS.md", "pull_only"),
)


def watched_rel_paths() -> tuple[str, ...]:
    """All registered ``rel_path``s — the env-core watched-file list mirror."""
    return tuple(f.rel_path for f in SYNCED_FILES)


def bidirectional_files() -> tuple[SyncedFile, ...]:
    """Registry entries that flow through the prompt reconcile."""
    return tuple(f for f in SYNCED_FILES if f.sync_class == "bidirectional")


def pull_only_files() -> tuple[SyncedFile, ...]:
    """Registry entries that are env-authoritative pull-only caches."""
    return tuple(f for f in SYNCED_FILES if f.sync_class == "pull_only")
