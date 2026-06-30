"""AppDataService — manages per-(user, bundle, catalog_type) persistent storage volumes.

Every agent install (``Agent`` row) maps to exactly one ``AppDataVolume``
row keyed by ``(user_id, bundle_id, catalog_type)``. The ``catalog_type``
discriminator separates the publisher install (``NULL`` slot — the
publisher's dev copy) from consumer installs of the same bundle by the
same user (``"server"`` slot today, with future values like
``"marketplace"`` or ``"remote:<host>"`` planned). The backing data lives
on disk under
``settings.APP_DATA_STORAGE_DIR/<user>/<bundle>[/<catalog_type>]/`` with
four sub-directories — ``storage/``, ``uploads/``, ``cache/``, ``memory/`` —
bind-mounted into the agent environment at ``/app/workspace/app-data``.

Phase 1 contract:
- ``get_or_create_volume`` — idempotent creation; reused on reinstall after
  the row was marked orphaned. Lookup is scoped by ``catalog_type`` so a
  consumer reinstall reattaches the previous consumer volume rather than
  the publisher's NULL-slot one.
- ``wipe_volume`` — destroys row + on-disk tree; refuses while an install
  still references the volume.
- ``recompute_size`` — walks the tree with ``os.scandir`` (cheap stat()s)
  and persists the byte total.
- ``list_user_volumes`` — joined with ``Agent.name`` for display.
- ``find_orphans_older_than`` — used by the daily APScheduler reporter.
"""
import logging
import os
import re
import shutil
import uuid
from datetime import datetime, timedelta, UTC
from pathlib import Path

from sqlmodel import Session, select

from app.core.config import settings
from app.models.agents.agent import Agent
from app.models.bundles.app_data_volume import AppDataVolume
from app.models.users.user import User

logger = logging.getLogger(__name__)

# A candidate orphan directory must be untouched for at least this long before
# the GC reclaims it. Guards against deleting a tree whose ``AppDataVolume``
# row is being created in another in-flight request (directory written, row
# not yet committed when the GC snapshots the DB).
ORPHAN_DIR_GRACE = timedelta(days=1)


# Bundle ids contain dots, dashes, and digits — all safe for directory names
# on POSIX. We still sanitize for paranoia in case a future migration relaxes
# the input contract.
_VOLUME_NAME_SAFE = re.compile(r"[^a-zA-Z0-9_.-]")

APP_DATA_SUBDIRS: tuple[str, ...] = ("storage", "uploads", "cache", "memory")


class AppDataService:
    """CRUD + filesystem operations for ``AppDataVolume`` rows."""

    # ── Path helpers ──────────────────────────────────────────────────

    @staticmethod
    def storage_root() -> Path:
        """Return the configured app-data root as a ``Path``."""
        return Path(settings.APP_DATA_STORAGE_DIR)

    @classmethod
    def host_storage_root(cls) -> Path:
        """Return the host-side root used in docker-compose volume mounts.

        Falls back to ``APP_DATA_STORAGE_DIR`` for non-Docker-in-Docker setups.
        """
        if settings.HOST_APP_DATA_DIR:
            return Path(settings.HOST_APP_DATA_DIR)
        return cls.storage_root()

    @classmethod
    def host_path_for(
        cls,
        user_id: uuid.UUID,
        bundle_id: str,
        catalog_type: str | None = None,
    ) -> Path:
        """Compute the host-side path for ``(user, bundle[, catalog_type])``.

        The NULL slot (publisher install) lives at the legacy
        ``<root>/<user>/<bundle>/`` location so existing on-disk data is
        preserved verbatim. Non-NULL slots (consumer installs) nest under a
        ``_<catalog_type>/`` subdirectory so they cannot collide with the
        publisher's tree.
        """
        base = cls.host_storage_root() / str(user_id) / bundle_id
        if catalog_type is None:
            return base
        return base / f"_{cls._sanitize_slot(catalog_type)}"

    @classmethod
    def container_path_for(
        cls,
        user_id: uuid.UUID,
        bundle_id: str,
        catalog_type: str | None = None,
    ) -> Path:
        """Compute the backend-container-side path used for I/O."""
        base = cls.storage_root() / str(user_id) / bundle_id
        if catalog_type is None:
            return base
        return base / f"_{cls._sanitize_slot(catalog_type)}"

    @staticmethod
    def _sanitize_slot(catalog_type: str) -> str:
        """Sanitize a ``catalog_type`` value for use as a path segment.

        Defensive normalisation only — today's known values (``"server"``)
        are already safe, but future values like ``"remote:host.example"``
        carry path-unfriendly characters.
        """
        return _VOLUME_NAME_SAFE.sub("_", catalog_type)

    @staticmethod
    def _volume_name(
        user_id: uuid.UUID,
        bundle_id: str,
        catalog_type: str | None = None,
    ) -> str:
        """Compose a docker-volume-safe name.

        Dashes are fine; underscores keep PG/docker happy. The 240-char cap
        is on the *assembled* ``appdata_<8hex>_<slug>[_<catalog>]`` string —
        it leaves headroom under common filesystem name limits (e.g. ext4's
        255-byte filename limit). PG's 63-char identifier limit doesn't
        apply here: ``volume_name`` is a Docker-volume / bind-mount name,
        not a SQL identifier.

        The publisher (NULL) slot keeps the legacy 3-segment name so
        existing rows continue to match. Non-NULL slots append the
        catalog_type so the two coexisting slots get distinct Docker
        volume names (the ``volume_name`` column is globally unique).
        """
        slug = _VOLUME_NAME_SAFE.sub("_", bundle_id)
        if catalog_type is None:
            return f"appdata_{user_id.hex[:8]}_{slug}"[:240]
        slot = AppDataService._sanitize_slot(catalog_type)
        return f"appdata_{user_id.hex[:8]}_{slug}_{slot}"[:240]

    # ── Read / list ──────────────────────────────────────────────────

    @staticmethod
    def get_by_id(session: Session, volume_id: uuid.UUID) -> AppDataVolume | None:
        return session.get(AppDataVolume, volume_id)

    @staticmethod
    def get_for_user(
        session: Session, volume_id: uuid.UUID, user_id: uuid.UUID
    ) -> AppDataVolume | None:
        """Return the volume only when it exists AND belongs to ``user_id``.

        Combines lookup + ownership filter so route handlers don't have to
        repeat the 404-or-forbidden pattern. Returning ``None`` for
        wrong-owner rows (rather than 403) matches the rest of the codebase:
        we don't leak existence of resources owned by other users.
        """
        volume = session.get(AppDataVolume, volume_id)
        if not volume or volume.user_id != user_id:
            return None
        return volume

    @staticmethod
    def get_by_user_bundle(
        session: Session,
        user_id: uuid.UUID,
        bundle_id: str,
        catalog_type: str | None = None,
    ) -> AppDataVolume | None:
        """Lookup keyed on ``(user_id, bundle_id, catalog_type)``.

        ``catalog_type`` is matched explicitly (including its ``NULL``
        value via ``IS NULL``) — a SQL ``= NULL`` comparison would silently
        return zero rows and break the publisher-slot path.
        """
        stmt = select(AppDataVolume).where(
            AppDataVolume.user_id == user_id,
            AppDataVolume.bundle_id == bundle_id,
        )
        if catalog_type is None:
            stmt = stmt.where(AppDataVolume.catalog_type.is_(None))
        else:
            stmt = stmt.where(AppDataVolume.catalog_type == catalog_type)
        return session.exec(stmt).first()

    @staticmethod
    def get_install_name(
        session: Session, volume: AppDataVolume
    ) -> str | None:
        """Return the linked install's ``Agent.name`` or ``None``.

        Used by single-volume responses (recompute, GET) so the route doesn't
        need to re-run the full join in ``list_user_volumes`` to look up one
        name. Returns ``None`` when the volume is orphaned or the FK has
        been cleared.
        """
        if volume.current_install_id is None:
            return None
        install = session.get(Agent, volume.current_install_id)
        return install.name if install else None

    @staticmethod
    def list_user_volumes(
        session: Session, user_id: uuid.UUID
    ) -> list[tuple[AppDataVolume, str | None]]:
        """Return ``[(volume, install_name | None), ...]`` for the UI table.

        ``install_name`` is the linked ``Agent.name`` — None if no install
        currently references the volume (orphaned or dangling FK).
        """
        stmt = (
            select(AppDataVolume, Agent.name)
            .where(AppDataVolume.user_id == user_id)
            .join(
                Agent,
                Agent.id == AppDataVolume.current_install_id,
                isouter=True,
            )
            .order_by(AppDataVolume.created_at.desc())
        )
        return list(session.exec(stmt).all())

    # ── Lifecycle ────────────────────────────────────────────────────

    @classmethod
    def get_or_create_volume(
        cls,
        session: Session,
        user_id: uuid.UUID,
        bundle_id: str,
        current_install_id: uuid.UUID | None = None,
        catalog_type: str | None = None,
    ) -> AppDataVolume:
        """Idempotent: create the row + directory tree, or reuse if present.

        Lookup is keyed on ``(user_id, bundle_id, catalog_type)``. Reusing
        an existing row clears ``is_orphaned`` and updates
        ``current_install_id`` (so reinstalling reattaches the volume).

        ``catalog_type`` semantics:
          - ``None``     → publisher install (publisher's dev / source copy)
          - ``"server"`` → consumer install from this server's local catalog
          - other strings reserved for future sources (``"marketplace"``,
            ``"remote:<host>"``).

        For backfilled rows (existing on-disk data) the stored
        ``host_path`` is preserved on reuse — we deliberately do **not**
        overwrite it with the freshly computed path, otherwise a
        configuration drift on ``HOST_APP_DATA_DIR`` would silently strand
        users' data. New rows always store the freshly-computed path.
        """
        existing = cls.get_by_user_bundle(
            session, user_id, bundle_id, catalog_type=catalog_type
        )

        if existing:
            # Reuse the recorded on-disk path. ``container_path_from_volume``
            # translates the stored ``host_path`` back to the backend's
            # filesystem view so we ensure the directory tree at the path
            # the row actually points at (not the freshly-computed one).
            cls._ensure_directory_tree(cls._container_path_from_volume(existing))
            existing.is_orphaned = False
            if current_install_id is not None:
                existing.current_install_id = current_install_id
            existing.updated_at = datetime.now(UTC)
            session.add(existing)
            session.commit()
            session.refresh(existing)
            return existing

        host_path = cls.host_path_for(user_id, bundle_id, catalog_type=catalog_type)
        cls._ensure_directory_tree(
            cls.container_path_for(user_id, bundle_id, catalog_type=catalog_type)
        )

        volume = AppDataVolume(
            user_id=user_id,
            bundle_id=bundle_id,
            catalog_type=catalog_type,
            volume_name=cls._volume_name(user_id, bundle_id, catalog_type=catalog_type),
            host_path=str(host_path),
            current_install_id=current_install_id,
            is_orphaned=False,
        )
        session.add(volume)
        session.commit()
        session.refresh(volume)
        logger.info(
            "Created AppDataVolume id=%s user=%s bundle=%s catalog_type=%s host=%s",
            volume.id, user_id, bundle_id, catalog_type, host_path,
        )
        return volume

    @classmethod
    def mark_orphaned(
        cls, session: Session, volume: AppDataVolume
    ) -> AppDataVolume:
        """Mark a volume orphaned (Phase 2 uninstall hook).

        Phase 1 doesn't trigger this from any service, but the helper exists
        so the daily reporter and future ``InstallService.uninstall`` share
        a single code path.
        """
        volume.is_orphaned = True
        volume.current_install_id = None
        volume.updated_at = datetime.now(UTC)
        session.add(volume)
        session.commit()
        session.refresh(volume)
        return volume

    @classmethod
    def wipe_volume(cls, session: Session, volume: AppDataVolume) -> None:
        """Destroy the row + on-disk tree.

        Wipe is allowed **only** when the volume is explicitly orphaned
        (``is_orphaned = true``). The looser ``current_install_id IS NULL``
        check is unsafe: when an install row is hard-deleted, the FK
        ``SET NULL`` clears ``current_install_id`` without flipping
        ``is_orphaned`` — leaving a window where the user could wipe data
        the platform still considers "live".

        Phase 2 will set ``is_orphaned = true`` from
        ``InstallService.uninstall``. Until then, no wipe path exists for
        attached volumes — the App Data tab disables the action accordingly.

        Filesystem removal is best-effort: a failure to ``rmtree`` is logged
        and the row is still deleted, otherwise the user would be stuck with
        a ghost row they can never clear.
        """
        if not volume.is_orphaned:
            raise ValueError(
                "Cannot wipe app-data volume that is still attached to an install. "
                "Uninstall the agent first."
            )

        cls._best_effort_rmtree(cls._container_path_from_volume(volume))

        session.delete(volume)
        session.commit()
        logger.info("Wiped AppDataVolume id=%s bundle=%s", volume.id, volume.bundle_id)

    # ── Size accounting ─────────────────────────────────────────────

    @classmethod
    def recompute_size(cls, session: Session, volume: AppDataVolume) -> int:
        """Walk the volume tree and persist its byte total.

        Uses ``os.scandir`` to stat children without loading file contents.
        Symlinks are followed for size accounting (mirrors ``du -sb`` default).
        Missing trees report 0 — that handles the case where the volume row
        exists but the on-disk dir was removed out-of-band.
        """
        container_path = cls._container_path_from_volume(volume)
        size = cls._tree_size(container_path)

        volume.size_bytes = size
        volume.last_size_check_at = datetime.now(UTC)
        volume.updated_at = volume.last_size_check_at
        session.add(volume)
        session.commit()
        session.refresh(volume)
        return size

    @classmethod
    def _container_path_from_volume(cls, volume: AppDataVolume) -> Path:
        """Convert a stored ``host_path`` back to the backend-container path.

        ``host_path`` is the docker-compose-side view; on Docker-in-Docker
        setups (``HOST_APP_DATA_DIR`` set) it does not exist inside the
        backend container — translate it back via the relative position
        under the configured root. For plain dev (no ``HOST_APP_DATA_DIR``)
        host and container views are the same path.
        """
        host = Path(volume.host_path)
        if (
            settings.HOST_APP_DATA_DIR
            and str(host).startswith(settings.HOST_APP_DATA_DIR)
        ):
            rel = host.relative_to(settings.HOST_APP_DATA_DIR)
            return cls.storage_root() / rel
        # Fallback for the (theoretical) case of a stored relative path.
        if not host.is_absolute():
            return cls.storage_root() / host
        return host

    @staticmethod
    def _tree_size(root: Path) -> int:
        """Sum file sizes under ``root`` using ``os.scandir`` for speed."""
        if not root.exists():
            return 0
        total = 0
        stack: list[Path] = [root]
        while stack:
            current = stack.pop()
            try:
                with os.scandir(current) as it:
                    for entry in it:
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                stack.append(Path(entry.path))
                            elif entry.is_file(follow_symlinks=False):
                                total += entry.stat(follow_symlinks=False).st_size
                        except OSError:
                            # Race: file vanished between scandir and stat.
                            continue
            except OSError as e:
                logger.warning("scandir failed for %s: %s", current, e)
        return total

    # ── Filesystem helpers ─────────────────────────────────────────

    @classmethod
    def _ensure_directory_tree(cls, container_path: Path) -> None:
        """Create ``<root>/storage``, ``uploads``, ``cache``, ``memory`` with mode 0o755."""
        for sub in APP_DATA_SUBDIRS:
            target = container_path / sub
            target.mkdir(parents=True, exist_ok=True, mode=0o755)

    # ── Orphan reporting ───────────────────────────────────────────

    @staticmethod
    def find_orphans_older_than(
        session: Session, days: int
    ) -> list[AppDataVolume]:
        """Return orphaned volumes whose ``updated_at`` is older than ``days``.

        Used by the daily APScheduler reporter — the job logs results but does
        NOT delete; deletion is user-driven via the Settings → App Data tab.
        """
        cutoff = datetime.now(UTC) - timedelta(days=days)
        stmt = select(AppDataVolume).where(
            AppDataVolume.is_orphaned == True,  # noqa: E712
            AppDataVolume.updated_at < cutoff,
        )
        return list(session.exec(stmt).all())

    # ── Filesystem garbage collection ──────────────────────────────

    @classmethod
    def find_orphan_dirs(
        cls, session: Session, grace: timedelta = ORPHAN_DIR_GRACE
    ) -> list[Path]:
        """Return on-disk app-data directories with no DB representation.

        Deleting a user (or a single install) drops the ``AppDataVolume``
        rows via FK cascade, but the cascade never touches the filesystem —
        the per-user tree under ``APP_DATA_STORAGE_DIR/<user_id>/...`` is left
        behind. This diffs the directory tree against the DB and returns the
        container-side paths that should be reclaimed, at two granularities:

        - **User directory** (``<root>/<user_id>``) whose user no longer
          exists → the whole tree is orphaned (the account-deletion case).
          Top-level entries whose name is not a valid UUID are skipped, so an
          operator-placed directory under the root is never touched.
        - **Bundle subtree** (``<root>/<user_id>/<bundle_id>``) under a *live*
          user that no volume row maps to. A row whose path nests deeper (the
          ``_<catalog_type>`` consumer slot) keeps its parent bundle dir alive,
          so a half-populated bundle dir is conservatively retained.

        Directories modified within ``grace`` are skipped to avoid racing an
        in-flight install whose volume row is not yet committed. Returns paths
        only — it never deletes.

        Queries are scoped to the user-id directories actually present on disk
        (``WHERE id IN (...)``), so the cost tracks on-disk dirs rather than the
        full ``user`` / ``app_data_volume`` tables.
        """
        root = cls.storage_root()
        if not root.exists():
            return []

        cutoff = datetime.now(UTC).timestamp() - grace.total_seconds()

        # Only UUID-named top-level dirs are candidates; scope the DB lookups
        # to exactly those users so we never load whole tables.
        disk_dirs = cls._iter_subdirs(root)
        disk_uuids = [
            uuid.UUID(d.name) for d in disk_dirs if cls._looks_like_uuid(d.name)
        ]

        live_user_ids: set[str] = set()
        # Flatten each live volume path plus all its ancestors into one set so
        # "a row equals or nests under this bundle dir" is an O(1) membership
        # test (the ``_<catalog_type>`` consumer slot keeps its parent alive).
        live_dirs: set[Path] = set()
        if disk_uuids:
            live_user_ids = {
                str(uid)
                for uid in session.exec(
                    select(User.id).where(User.id.in_(disk_uuids))
                ).all()
            }
            volumes = session.exec(
                select(AppDataVolume).where(AppDataVolume.user_id.in_(disk_uuids))
            ).all()
            for v in volumes:
                p = cls._container_path_from_volume(v)
                live_dirs.add(p)
                live_dirs.update(p.parents)

        orphans: list[Path] = []
        for user_dir in disk_dirs:
            if not cls._looks_like_uuid(user_dir.name):
                continue  # operator-placed dir under the root — never touch
            if user_dir.name not in live_user_ids:
                # Deleted user → the whole tree is orphaned.
                if cls._older_than(user_dir, cutoff):
                    orphans.append(user_dir)
                continue
            # Live user: reclaim bundle subtrees no volume row maps to.
            for bundle_dir in cls._iter_subdirs(user_dir):
                if bundle_dir in live_dirs:
                    continue
                if cls._older_than(bundle_dir, cutoff):
                    orphans.append(bundle_dir)
        return orphans

    @classmethod
    def purge_orphan_dirs(
        cls, session: Session, grace: timedelta = ORPHAN_DIR_GRACE
    ) -> tuple[int, int]:
        """``rmtree`` every directory from :meth:`find_orphan_dirs`.

        Best-effort: a failed removal is logged and counted, never raised.
        Returns ``(removed_count, failed_count)``.
        """
        removed = failed = 0
        for path in cls.find_orphan_dirs(session, grace=grace):
            if cls._best_effort_rmtree(path):
                removed += 1
                logger.info("AppData GC removed orphaned dir: %s", path)
            else:
                failed += 1
        return removed, failed

    @staticmethod
    def _best_effort_rmtree(path: Path) -> bool:
        """Remove a directory tree, logging (not raising) on failure.

        Returns ``True`` when the tree is gone afterwards (removed, or already
        absent), ``False`` when removal failed. Shared by ``wipe_volume`` and
        the GC so the removal+logging contract lives in one place.
        """
        try:
            if path.exists():
                shutil.rmtree(path)
            return True
        except OSError as e:
            logger.error("Failed to remove app-data tree %s: %s", path, e)
            return False

    @staticmethod
    def _iter_subdirs(path: Path) -> list[Path]:
        """List immediate sub-directories, tolerating I/O errors."""
        try:
            return [p for p in path.iterdir() if p.is_dir()]
        except OSError as e:
            logger.warning("AppData GC: cannot list %s: %s", path, e)
            return []

    @staticmethod
    def _looks_like_uuid(name: str) -> bool:
        try:
            uuid.UUID(name)
            return True
        except ValueError:
            return False

    @staticmethod
    def _older_than(path: Path, cutoff_ts: float) -> bool:
        """True when ``path``'s mtime is at/older than ``cutoff_ts`` epoch secs."""
        try:
            return path.stat().st_mtime <= cutoff_ts
        except OSError:
            return False
