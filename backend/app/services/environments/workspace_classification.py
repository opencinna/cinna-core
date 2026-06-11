"""Workspace classification — single source of truth for bundle/env copy.

Historically four divergent copy lists decided which parts of an agent env's
``app/workspace/`` tree are "bundle content" vs. "per-env runtime / per-user
state":

  1. ``PublishService._BUNDLE_FOLDERS`` / ``_BUNDLE_FILES``      (publish snapshot)
  2. ``workspace_copy._BUNDLE_FOLDERS_INTO_WORKSPACE`` / ...     (install seed + apply-update)
  3. ``workspace_copy._ENV_FOLDERS`` / ...                       (env-to-env / rebuild seed)
  4. ``environment_lifecycle.copy_workspace_between_environments`` (env-switch)

They drifted: list 1/2 silently dropped ``webapp/``, ``agent_api/`` and any
custom top-level dir the agent created, so publishing lost data. This module
replaces all four allowlists with **one denylist + two named profiles** so the
callers differ only by which profile they pick.

Design — *denylist, full-tree capture*:
A faithful copy of ``app/workspace/`` is "everything except the runtime /
per-user / secret state". Enumerating an allowlist is exactly how files went
missing; a denylist fails safe — a new agent-authored dir is captured by
default and only an explicitly excluded name is dropped.

Two profiles:

* ``BUNDLE_OWNED`` — used by publish, install-seed, apply-update. Everything
  under ``app/workspace/`` **except** :data:`BUNDLE_EXCLUDED_TOPLEVEL` and the
  runtime-name-denylisted entries. ``plugins/`` keeps its special handling
  (exclude derived files; merge on seed/update). ``uploads/`` is **excluded**
  (per-install runtime data, consistent with the App Data philosophy).
* ``ENV_MIGRATION`` — used by ``copy_env_to_env`` and
  ``copy_workspace_between_environments`` (same-user same-agent env switch /
  rebuild). Superset of ``BUNDLE_OWNED`` **plus** :data:`ENV_MIGRATION_EXTRA`
  (``credentials/`` + ``uploads/``), minus only the true runtime dirs
  ``logs/`` and ``databases/``. ``app-data/`` is a bind mount and follows the
  volume — never copied.

Runtime SDK session state (``opencode_sessions/``) lives at the instance-dir
root, **outside** ``app/workspace/``, so capturing the ``app/workspace/``
subtree excludes it for free.
"""
from __future__ import annotations

import logging
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)

# ``app/workspace/`` relative to ``ENV_INSTANCES_DIR/<env_id>/``.
WORKSPACE_ROOT_REL = "app/workspace"

# Top-level ``app/workspace/`` entries that are NEVER bundle content (denylist).
# These are per-user persistent data, runtime state, or template artifacts.
BUNDLE_EXCLUDED_TOPLEVEL: frozenset[str] = frozenset(
    {
        "app-data",  # per-user persistent volume (bind mount); follows the volume
        "credentials",  # synced separately every env start
        "logs",  # runtime session logs
        "databases",  # runtime SQLite DBs / session state
        "uploads",  # per-install runtime user-provided files (NOT bundle-owned)
        "__init__.py",  # template-created Python package marker, not agent content
    }
)

# Per-env runtime / dotfile state that must never be snapshotted, even when it
# appears as a top-level workspace entry. Verified against the env templates
# (``backend/app/env-templates/*/app/workspace/``) and the lifecycle setup code
# (``_setup_new_container`` / ``_materialize_opencode_config``):
#   - ``opencode_sessions/`` lives at the instance-dir ROOT, outside
#     ``app/workspace/`` — excluded for free by the subtree boundary.
#   - ``.opencode`` is materialised under ``app/core/.opencode`` (outside the
#     workspace) but a defensive entry guards against any workspace-level copy.
#   - ``.cache`` is generic tool cache.
# The per-plugin ``.cinna_plugin_ref`` marker is deliberately NOT denylisted —
# it is copied verbatim so the immutable snapshot faithfully records the
# publisher's plugin commit refs.
RUNTIME_NAME_DENYLIST: frozenset[str] = frozenset(
    {
        ".opencode",
        ".cache",
    }
)

# Plugins-root derived files: present in ``plugins/`` but regenerated per
# consumer by the container install routine — never snapshotted or seeded.
PLUGIN_DERIVED_FILES: frozenset[str] = frozenset({"settings.json", "manifest.json"})
PLUGINS_DIRNAME = "plugins"

# Extra top-level entries carried by the ENV_MIGRATION profile on top of
# BUNDLE_OWNED. ``credentials/`` and ``uploads/`` ARE copied on a same-user
# same-agent env migration (carrying them across is the desired behaviour) but
# are excluded from bundle content.
ENV_MIGRATION_EXTRA: frozenset[str] = frozenset({"credentials", "uploads"})


def is_runtime_denylisted(name: str) -> bool:
    """True when ``name`` is per-env runtime/dotfile state to never copy."""
    return name in RUNTIME_NAME_DENYLIST


def is_bundle_owned_toplevel(name: str) -> bool:
    """True when a top-level workspace entry named ``name`` is bundle content.

    Bundle-owned = not in :data:`BUNDLE_EXCLUDED_TOPLEVEL` and not
    runtime-name-denylisted. ``plugins/`` IS bundle-owned (it has special
    derived-file / merge handling at the call sites, not here).
    """
    if name in BUNDLE_EXCLUDED_TOPLEVEL:
        return False
    if is_runtime_denylisted(name):
        return False
    return True


def is_env_migration_toplevel(name: str) -> bool:
    """True when a top-level entry is carried by the ENV_MIGRATION profile.

    Superset of :func:`is_bundle_owned_toplevel` plus
    :data:`ENV_MIGRATION_EXTRA` (``credentials`` + ``uploads``). The runtime
    dirs ``logs`` / ``databases`` and the bind mount ``app-data`` are still
    excluded; the template marker ``__init__.py`` is still excluded.
    """
    if is_runtime_denylisted(name):
        return False
    if name in ENV_MIGRATION_EXTRA:
        return True
    return is_bundle_owned_toplevel(name)


# ── Symlink safety ───────────────────────────────────────────────────────────
#
# The agent env ``app/workspace/`` is AGENT-CONTROLLED. A symlink there is a
# denylist-bypass + host-file-exfiltration vector: an entry like
# ``mystuff -> ../../credentials`` or ``leak -> /etc/passwd`` is a directory as
# far as ``is_dir()`` is concerned (it follows the link), so without a guard it
# would be classified as bundle-owned and ``shutil.copytree`` would dereference
# it into the immutable published snapshot — leaking excluded/host content to
# every installer. Nested symlinks inside a captured dir leak the same way.
#
# Policy (defence in depth, simplest safe default): **SKIP symlinks** — never
# follow, never copy the link itself. This is the single source of truth used by
# every copy walk (publish snapshot, install seed, apply-update, env migration).


def _skip_symlink_toplevel(child: Path) -> bool:
    """True (and log a warning) when a top-level workspace entry is a symlink.

    Top-level symlinks are skipped wholesale: the workspace is agent-controlled
    and a symlink can point at excluded (``credentials/``) or host (``/etc``)
    content, so it is never part of bundle/env-migration copy.
    """
    if child.is_symlink():
        logger.warning(
            "Skipping symlink in workspace (never followed/copied): %s", child
        )
        return True
    return False


def _ignore_symlinks(directory: str, names: list[str]) -> set[str]:
    """``shutil.copytree(ignore=...)`` callback that drops nested symlinks.

    Called once per directory in the source tree with that dir's child names;
    returns the names that are symlinks so ``copytree`` neither follows nor
    recreates them. Guarantees no nested symlink inside a captured tree can
    dereference excluded/host content into the destination.
    """
    base = Path(directory)
    skipped: set[str] = set()
    for name in names:
        if (base / name).is_symlink():
            logger.warning(
                "Skipping nested symlink in workspace copy: %s", base / name
            )
            skipped.add(name)
    return skipped


def safe_copytree(src: Path, dst: Path, *, dirs_exist_ok: bool = False) -> None:
    """``shutil.copytree`` that never follows or copies symlinks (any depth).

    The single safe copy primitive for the agent-controlled workspace: top-level
    callers already skip symlinked entries via :func:`_skip_symlink_toplevel`,
    and this guards every NESTED symlink inside the captured tree via the
    :func:`_ignore_symlinks` callback (``symlinks=False`` would otherwise
    dereference them).
    """
    shutil.copytree(src, dst, ignore=_ignore_symlinks, dirs_exist_ok=dirs_exist_ok)


def iter_bundle_toplevel(workspace_root: Path) -> Iterator[Path]:
    """Yield each bundle-owned top-level entry under ``workspace_root``.

    Skips :data:`BUNDLE_EXCLUDED_TOPLEVEL`, runtime-denylisted names, and
    **symlinks** (see :func:`_skip_symlink_toplevel`). Returns nothing when
    ``workspace_root`` is missing or not a directory (the caller decides whether
    that is an error — see ``_assert_workspace_readable``).
    """
    if not workspace_root.exists() or not workspace_root.is_dir():
        return
    for child in sorted(workspace_root.iterdir(), key=lambda p: p.name):
        if _skip_symlink_toplevel(child):
            continue
        if is_bundle_owned_toplevel(child.name):
            yield child


def iter_env_migration_toplevel(workspace_root: Path) -> Iterator[Path]:
    """Yield each ENV_MIGRATION top-level entry under ``workspace_root``.

    Symlinks are skipped (see :func:`_skip_symlink_toplevel`).
    """
    if not workspace_root.exists() or not workspace_root.is_dir():
        return
    for child in sorted(workspace_root.iterdir(), key=lambda p: p.name):
        if _skip_symlink_toplevel(child):
            continue
        if is_env_migration_toplevel(child.name):
            yield child


def snapshot_layout(snapshot_path: Path) -> Literal["v1_flat", "v2_workspace"]:
    """Detect a bundle revision snapshot's on-disk layout by shape.

    * ``"v2_workspace"`` — a ``workspace/`` subtree directory exists at the
      snapshot root (schema_version 2: the verbatim ``app/workspace/`` copy
      lives under ``workspace/``).
    * ``"v1_flat"`` — legacy layout where the allowlisted folders
      (``scripts/``, ``docs/`` …) sit directly at the snapshot root.

    Prefer dispatching on the manifest ``schema_version`` when the manifest is
    loaded; this directory-shape fallback is for callers that only have the
    path (``seed_workspace_from_bundle_snapshot`` / ``replace_bundle_content``).
    """
    workspace_dir = snapshot_path / "workspace"
    if workspace_dir.exists() and workspace_dir.is_dir():
        return "v2_workspace"
    return "v1_flat"
