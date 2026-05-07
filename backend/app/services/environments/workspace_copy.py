"""Workspace copy utilities — shared between environment service & bundles.

Phase 2: when ``InstallService.install_bundle`` seeds a new install env, it
needs to drop the bundle revision content into the install's env workspace.
The previous home of this logic (``AgentCloneService.copy_workspace``) is
being retired with the rest of the clone code; the bytes-level copy moves
here so the env service can keep its current "seed from a source env" code
path while pointing at a different source (the bundle revision tree).
"""
import logging
import shutil
from pathlib import Path
from uuid import UUID

from app.core.config import settings

logger = logging.getLogger(__name__)


# Standard bundle folders + workspace files. Mirrors PublishService's
# snapshot folders. The first element is the snapshot relative path; the
# second is the env workspace relative path.
_BUNDLE_FOLDERS_INTO_WORKSPACE: tuple[tuple[str, str], ...] = (
    ("scripts", "app/workspace/scripts"),
    ("docs", "app/workspace/docs"),
    ("knowledge", "app/workspace/knowledge"),
    ("files", "app/workspace/files"),
)
_BUNDLE_FILES_INTO_WORKSPACE: tuple[tuple[str, str], ...] = (
    ("workspace_requirements.txt", "app/workspace/workspace_requirements.txt"),
    ("workspace_system_packages.txt", "app/workspace/workspace_system_packages.txt"),
)

# When copying between two existing env workspaces (publisher install →
# foreign install seed at install time, or env-to-env copy on rebuild) we
# use the env-workspace-relative paths on both ends.
_ENV_FOLDERS: tuple[str, ...] = (
    "app/workspace/scripts",
    "app/workspace/docs",
    "app/workspace/knowledge",
    "app/workspace/webapp",
)
_ENV_OPTIONAL_FOLDERS: tuple[str, ...] = (
    "app/workspace/files",
    "app/workspace/uploads",
)
_ENV_FILES: tuple[str, ...] = (
    "app/workspace/workspace_requirements.txt",
    "app/workspace/workspace_system_packages.txt",
)


def copy_env_to_env(
    source_env_id: UUID, dest_env_id: UUID, *, include_files_folder: bool = True
) -> None:
    """Copy bundle-style folders from one env workspace to another.

    Replaces ``AgentCloneService.copy_workspace`` for the env-service hook
    that seeds a new env from an existing one.
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

    folders = list(_ENV_FOLDERS)
    if include_files_folder:
        folders.extend(_ENV_OPTIONAL_FOLDERS)

    for rel in folders:
        src = src_root / rel
        dst = dst_root / rel
        if not src.exists():
            continue
        try:
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(src, dst)
        except OSError as e:
            logger.error("Failed to copy %s: %s", rel, e)

    for rel in _ENV_FILES:
        src = src_root / rel
        dst = dst_root / rel
        if not src.exists():
            continue
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        except OSError as e:
            logger.error("Failed to copy file %s: %s", rel, e)


def seed_workspace_from_bundle_snapshot(
    snapshot_path: Path, env_id: UUID
) -> None:
    """Copy a bundle revision snapshot into a fresh install env workspace.

    Used by ``InstallService.install_bundle`` immediately after the env
    instance directory has been created by ``EnvironmentLifecycleManager``.
    Preserves anything already present that isn't part of the snapshot
    (e.g. the empty ``app/workspace/credentials/`` directory created by the
    template, ``opencode_sessions``, etc.).
    """
    instances_dir = Path(settings.ENV_INSTANCES_DIR)
    dst_root = instances_dir / str(env_id)
    if not dst_root.exists():
        logger.warning("Target env workspace does not exist: %s", dst_root)
        return
    if not snapshot_path.exists():
        logger.warning("Bundle snapshot path missing: %s", snapshot_path)
        return

    for snap_rel, ws_rel in _BUNDLE_FOLDERS_INTO_WORKSPACE:
        src = snapshot_path / snap_rel
        dst = dst_root / ws_rel
        if not src.exists():
            continue
        try:
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(src, dst)
        except OSError as e:
            logger.error("Failed to seed bundle folder %s: %s", snap_rel, e)

    for snap_rel, ws_rel in _BUNDLE_FILES_INTO_WORKSPACE:
        src = snapshot_path / snap_rel
        dst = dst_root / ws_rel
        if not src.exists():
            continue
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        except OSError as e:
            logger.error("Failed to seed bundle file %s: %s", snap_rel, e)


def replace_bundle_content(snapshot_path: Path, env_id: UUID) -> None:
    """Replace bundle-owned folders in an env workspace with snapshot content.

    Preserves ``credentials/`` (managed by the credentials sync) and
    ``app-data/`` (per-user persistent data; never touched by updates).
    Same logic as ``seed_workspace_from_bundle_snapshot`` — the distinct
    name documents the lifecycle method called by ``InstallService.apply_update``.
    """
    seed_workspace_from_bundle_snapshot(snapshot_path, env_id)
