"""Best-effort removal of a per-row folder under one of the data roots.

Rows such as a bundle, a user or an environment own a folder named after
them under a configured root (``BUNDLE_STORAGE_DIR/<bundle_id>``,
``APP_DATA_STORAGE_DIR/<user_id>``, ...). When the row is deleted the folder
goes with it, after the commit, without ever failing the request.

The guard is deliberately strict: the name must be one plain path segment
and the resolved target must sit directly under the resolved root, so an
empty id, ``..``, an absolute path or a symlink can never
turn a cleanup into removing something outside the data root.
"""
import logging
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)


def _child_of_root(root: str | Path, name: str) -> Path | None:
    """The folder ``root/name`` when ``name`` is one safe segment, else None."""
    if not root or not isinstance(name, str):
        return None
    if not name or name != name.strip() or name in (".", "..") or "/" in name or "\\" in name or "\x00" in name:
        return None
    root_path = Path(root).resolve()
    candidate = root_path / name
    if candidate.is_symlink():
        return None
    target = candidate.resolve()
    if target.parent != root_path or target == root_path:
        return None
    return target


def remove_data_subdir(root: str | Path, name: str, *, what: str) -> bool:
    """Remove ``root/name`` if it exists; log, never raise.

    Returns True when the folder is gone afterwards (removed, or never
    there), False when the name was refused or the removal failed.
    ``what`` names the folder kind in the log lines (e.g. "bundle storage").
    """
    target = _child_of_root(root, name)
    if target is None:
        logger.warning("Refusing to remove %s folder %r under %s", what, name, root)
        return False
    try:
        if target.exists():
            shutil.rmtree(target)
            logger.info("Removed %s folder %s", what, target)
        return True
    except OSError as exc:
        logger.error("Failed to remove %s folder %s: %s", what, target, exc)
        return False
