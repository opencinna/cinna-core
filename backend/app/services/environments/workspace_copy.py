"""Workspace copy utilities — shared between environment service & bundles.

These functions move bundle revision content into install env workspaces and
copy workspace state between two envs of the same install. They all defer the
"what counts as workspace content" decision to
:mod:`app.services.environments.workspace_classification` (the single source of
truth that replaced four divergent allowlists).

Snapshot layouts (see ``workspace_classification.snapshot_layout``):

* **v2 (schema_version 2)** — the snapshot has a ``workspace/`` subtree holding
  a verbatim copy of ``app/workspace/`` minus the denylist. Seeding copies each
  top-level entry into the install workspace; apply-update additionally prunes
  stale bundle-owned top-level entries the new snapshot no longer ships.
* **v1 (legacy)** — the allowlisted folders (``scripts/``, ``docs/`` …) sit
  directly at the snapshot root. Handled by the ``_V1_FLAT_*`` tuples below;
  apply-update keeps the legacy no-delete (overwrite-only) behaviour.

``plugins/`` is always **merged** (never delete-swept) so a consumer's own
``source=marketplace`` plugin dirs survive a bundle update.
"""
import logging
import shutil
from pathlib import Path
from uuid import UUID

from app.core.config import settings
from app.services.environments.workspace_classification import (
    BUNDLE_EXCLUDED_TOPLEVEL,
    PLUGIN_DERIVED_FILES,
    PLUGINS_DIRNAME,
    WORKSPACE_ROOT_REL,
    is_runtime_denylisted,
    iter_env_migration_toplevel,
    safe_copytree,
    snapshot_layout,
)

logger = logging.getLogger(__name__)


# Legacy v1-flat snapshot allowlist — the EXACT folders/files that
# schema_version 1 revisions wrote at the snapshot root. Used ONLY by the v1
# branch of the seed routine; never extended (v2 uses the full-tree workspace/
# subtree). The first element is the snapshot-relative path, the second the env
# workspace-relative path.
_V1_FLAT_FOLDERS: tuple[tuple[str, str], ...] = (
    ("scripts", f"{WORKSPACE_ROOT_REL}/scripts"),
    ("docs", f"{WORKSPACE_ROOT_REL}/docs"),
    ("knowledge", f"{WORKSPACE_ROOT_REL}/knowledge"),
    ("files", f"{WORKSPACE_ROOT_REL}/files"),
)
_V1_FLAT_FILES: tuple[tuple[str, str], ...] = (
    ("workspace_requirements.txt", f"{WORKSPACE_ROOT_REL}/workspace_requirements.txt"),
    ("workspace_system_packages.txt", f"{WORKSPACE_ROOT_REL}/workspace_system_packages.txt"),
)


def copy_env_to_env(
    source_env_id: UUID, dest_env_id: UUID, *, include_uploads_folder: bool = True
) -> None:
    """Copy workspace state from one env to another (same install).

    Uses the ENV_MIGRATION profile: the full bundle-owned set **plus**
    ``credentials/`` + ``uploads/``, minus the runtime dirs ``logs/`` /
    ``databases/`` and the ``app-data/`` bind mount. This now also carries
    ``webapp/``, ``agent_api/``, ``plugins/`` and any custom top-level dir the
    agent created — closing the gaps in the old static folder list.

    ``include_uploads_folder`` is retained for call-site compatibility; when
    ``False`` the per-install ``uploads/`` dir is skipped (``files/`` is part of
    the bundle-owned set and always copied).

    Symlinks are never followed or copied: top-level symlinks are skipped by
    ``iter_env_migration_toplevel`` and nested ones by ``safe_copytree`` (the
    source workspace is agent-controlled).
    """
    instances_dir = Path(settings.ENV_INSTANCES_DIR)
    src_root = instances_dir / str(source_env_id)
    dst_root = instances_dir / str(dest_env_id)

    if not src_root.exists():
        logger.warning("Source env workspace not found: %s", src_root)
        return
    if not dst_root.exists():
        logger.warning("Destination env workspace not found: %s", dst_root)
        return

    src_workspace = src_root / WORKSPACE_ROOT_REL
    dst_workspace = dst_root / WORKSPACE_ROOT_REL

    for src in iter_env_migration_toplevel(src_workspace):
        if not include_uploads_folder and src.name == "uploads":
            continue
        dst = dst_workspace / src.name
        try:
            if src.is_dir():
                if dst.exists():
                    shutil.rmtree(dst)
                safe_copytree(src, dst)
            else:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
        except OSError as e:
            logger.error("Failed to copy %s: %s", src.name, e)


def seed_workspace_from_bundle_snapshot(
    snapshot_path: Path, env_id: UUID
) -> None:
    """Copy a bundle revision snapshot into a fresh install env workspace.

    Used by ``InstallService.install_bundle`` immediately after the env
    instance directory has been created by ``EnvironmentLifecycleManager``.
    Dispatches on the snapshot layout (v1 flat vs v2 ``workspace/`` subtree).
    Preserves anything already present that isn't part of the snapshot (e.g.
    the empty ``app/workspace/credentials/`` dir created by the template,
    ``opencode_sessions``, etc.). ``plugins/`` is merged so the consumer's own
    marketplace plugins survive.
    """
    instances_dir = Path(settings.ENV_INSTANCES_DIR)
    dst_root = instances_dir / str(env_id)
    if not dst_root.exists():
        logger.warning("Target env workspace does not exist: %s", dst_root)
        return
    if not snapshot_path.exists():
        logger.warning("Bundle snapshot path missing: %s", snapshot_path)
        return

    _seed_overwrite_pass(snapshot_path, dst_root)


def _seed_overwrite_pass(snapshot_path: Path, dst_root: Path) -> None:
    """Copy/overwrite snapshot content into the install workspace.

    Shared by ``seed_workspace_from_bundle_snapshot`` and the
    ``replace_bundle_content`` apply-update path (which adds a prune pass on
    top for v2 snapshots). Dispatches on the snapshot layout.
    """
    if snapshot_layout(snapshot_path) == "v2_workspace":
        _seed_v2(snapshot_path / "workspace", dst_root)
    else:
        _seed_v1_flat(snapshot_path, dst_root)


def _seed_v2(snapshot_workspace: Path, dst_root: Path) -> None:
    """Copy each top-level entry of a v2 ``workspace/`` subtree into the install.

    The snapshot can only contain bundle-owned entries (the publisher snapshot
    already applied the denylist), so we copy everything present. ``plugins/``
    is routed through the merge helper; all other dirs/files overwrite.
    """
    dst_workspace = dst_root / WORKSPACE_ROOT_REL
    if not snapshot_workspace.exists() or not snapshot_workspace.is_dir():
        return
    for child in snapshot_workspace.iterdir():
        # Defence in depth: the publisher snapshot already strips symlinks, but
        # never follow/copy one if a legacy/hand-built snapshot contains it.
        if child.is_symlink():
            logger.warning("Skipping symlink in snapshot workspace: %s", child)
            continue
        dst = dst_workspace / child.name
        if child.name == PLUGINS_DIRNAME and child.is_dir():
            _seed_plugins_tree(child, dst)
            continue
        try:
            if child.is_dir():
                if dst.exists():
                    shutil.rmtree(dst)
                safe_copytree(child, dst)
            else:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(child, dst)
        except OSError as e:
            logger.error("Failed to seed bundle entry %s: %s", child.name, e)


def _seed_v1_flat(snapshot_path: Path, dst_root: Path) -> None:
    """Legacy v1-flat allowlist seed (unchanged behaviour for old revisions)."""
    for snap_rel, ws_rel in _V1_FLAT_FOLDERS:
        src = snapshot_path / snap_rel
        dst = dst_root / ws_rel
        if not src.exists():
            continue
        try:
            if dst.exists():
                shutil.rmtree(dst)
            safe_copytree(src, dst)
        except OSError as e:
            logger.error("Failed to seed bundle folder %s: %s", snap_rel, e)

    for snap_rel, ws_rel in _V1_FLAT_FILES:
        src = snapshot_path / snap_rel
        dst = dst_root / ws_rel
        if not src.exists():
            continue
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        except OSError as e:
            logger.error("Failed to seed bundle file %s: %s", snap_rel, e)

    # Plugins tree: MERGE the snapshot's plugin dirs into the consumer workspace
    # so the consumer's own source=marketplace plugins survive. Derived files
    # (settings.json / manifest.json) are never seeded.
    _seed_plugins_tree(
        snapshot_path / PLUGINS_DIRNAME,
        dst_root / WORKSPACE_ROOT_REL / PLUGINS_DIRNAME,
    )


def _seed_plugins_tree(src_plugins: Path, dst_plugins: Path) -> None:
    """Overlay a snapshot ``plugins/`` tree onto the consumer's plugins dir.

    For each top-level entry in the snapshot plugins dir:
      - Skip derived files (settings.json / manifest.json).
      - For a marketplace-name directory, replace the matching consumer dir
        wholesale (so a stale bundle plugin tree is overwritten) while leaving
        the consumer's OTHER marketplace dirs (their own plugins) untouched.

    NOTE: this overlays at the marketplace-name level. A consumer that has a
    same-named marketplace directory as a bundle marketplace would have that
    directory replaced — collision handling lives in the manifest/install layer
    (§6.4); the on-disk layout intentionally uses original names.
    """
    if not src_plugins.exists() or not src_plugins.is_dir():
        return
    try:
        dst_plugins.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.error("Failed to create plugins dir %s: %s", dst_plugins, e)
        return

    for child in src_plugins.iterdir():
        if child.is_symlink():
            logger.warning("Skipping symlink in plugins tree: %s", child)
            continue
        if child.is_file() and child.name in PLUGIN_DERIVED_FILES:
            continue
        target = dst_plugins / child.name
        try:
            if child.is_dir():
                if target.exists():
                    shutil.rmtree(target)
                safe_copytree(child, target)
            else:
                shutil.copy2(child, target)
        except OSError as e:
            logger.error("Failed to seed plugin entry %s: %s", child.name, e)


def replace_bundle_content(snapshot_path: Path, env_id: UUID) -> None:
    """Apply a new revision's snapshot onto an existing install env workspace.

    Unlike a fresh seed, apply-update must remove **stale bundle-owned**
    top-level entries — paths the old revision shipped but the new one dropped
    (D5: "the snapshot is authoritative for bundle-owned top-level entries").
    The sequence is:

    1. Copy/overwrite every snapshot top-level entry (shared seed pass).
    2. (v2 only) Prune: remove each install-workspace top-level entry that is
       NOT in the new snapshot, NOT in ``BUNDLE_EXCLUDED_TOPLEVEL``, NOT
       runtime-denylisted, and NOT ``plugins/``.

    Always preserved: ``app-data/``, ``credentials/``, ``logs/``,
    ``databases/``, ``uploads/`` (the denylist) and the consumer's own
    ``plugins/`` marketplace dirs (plugins are merged, never delete-swept).

    v1 (legacy) snapshots skip the prune pass to keep today's overwrite-only
    semantics — a v1→v1 apply-update never deletes; a v1→v2 apply-update prunes
    against the v2 snapshot, which is the desired forward behaviour.

    Pruning is best-effort per entry (a failed removal is logged, not raised)
    so one bad entry can't abort the whole update.
    """
    instances_dir = Path(settings.ENV_INSTANCES_DIR)
    dst_root = instances_dir / str(env_id)
    if not dst_root.exists():
        logger.warning("Target env workspace does not exist: %s", dst_root)
        return
    if not snapshot_path.exists():
        logger.warning("Bundle snapshot path missing: %s", snapshot_path)
        return

    _seed_overwrite_pass(snapshot_path, dst_root)

    if snapshot_layout(snapshot_path) != "v2_workspace":
        return  # legacy: no prune

    _prune_stale_bundle_content(snapshot_path / "workspace", dst_root)


def _prune_stale_bundle_content(snapshot_workspace: Path, dst_root: Path) -> None:
    """Remove install-workspace top-level entries the v2 snapshot no longer ships.

    Only genuinely stale **bundle-owned** paths are removed: anything in the
    denylist (``app-data/``, ``credentials/``, ``logs/``, ``databases/``,
    ``uploads/``, ``__init__.py``), any runtime dotfile, and ``plugins/`` are
    always preserved.
    """
    dst_workspace = dst_root / WORKSPACE_ROOT_REL
    if not dst_workspace.exists() or not dst_workspace.is_dir():
        return

    snapshot_toplevel: set[str] = set()
    if snapshot_workspace.exists() and snapshot_workspace.is_dir():
        snapshot_toplevel = {p.name for p in snapshot_workspace.iterdir()}

    for child in dst_workspace.iterdir():
        name = child.name
        if name in snapshot_toplevel:
            continue  # still shipped by the bundle
        if name == PLUGINS_DIRNAME:
            continue  # plugins are merged, never delete-swept
        if name in BUNDLE_EXCLUDED_TOPLEVEL:
            continue  # per-user / runtime / secret — never touched
        if is_runtime_denylisted(name):
            continue  # runtime dotfile state
        # Stale bundle-owned path: the bundle used to ship it but no longer
        # does. Remove it.
        try:
            # A symlink (even one pointing at a dir) must be unlinked, never
            # rmtree'd: rmtree raises on a symlinked dir (the OSError would be
            # swallowed and the stale link would linger forever), and following
            # it could delete the link's TARGET. Always remove the link itself.
            if child.is_symlink():
                child.unlink()
            elif child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
            logger.info("Pruned stale bundle-owned workspace entry: %s", name)
        except OSError as e:
            logger.error("Failed to prune stale workspace entry %s: %s", name, e)
