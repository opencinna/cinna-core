"""Where artifact files live on disk.

Layout under ``settings.ARTIFACT_STORAGE_DIR``::

    <artifact_id>/<revision_id>/<relative paths>       immutable revisions
    <artifact_id>/<revision_id>.zip                    cached download (built once)
    .tmp/<uuid>/                                       ingest scratch

The scratch area sits on the same filesystem as the revisions, so promoting
an ingested tree is a single atomic ``os.rename``: a revision folder either
exists complete or not at all. Scratch folders older than an hour (a crashed
ingest) are swept at the start of every ingest.

Revision folders are keyed by the revision **id**, not its number: the DB
row is written last, so a folder promoted for a commit that then failed is an
orphan nobody will ever reuse, instead of a folder squatting on the next
revision number (the caller removes it anyway).
"""
import logging
import os
import shutil
import time
import uuid
from pathlib import Path

from app.core.config import settings
from app.services.common.data_dirs import remove_data_subdir

logger = logging.getLogger(__name__)

TMP_DIRNAME = ".tmp"
STALE_TMP_SECONDS = 60 * 60


class ArtifactStorage:
    @staticmethod
    def root() -> Path:
        return Path(settings.ARTIFACT_STORAGE_DIR)

    @classmethod
    def artifact_dir(cls, artifact_id: uuid.UUID) -> Path:
        return cls.root() / str(artifact_id)

    @classmethod
    def revision_dir(cls, artifact_id: uuid.UUID, revision_id: uuid.UUID) -> Path:
        return cls.artifact_dir(artifact_id) / str(revision_id)

    @classmethod
    def revision_zip(cls, artifact_id: uuid.UUID, revision_id: uuid.UUID) -> Path:
        """Cached zip of a revision, beside its folder (``<artifact>/<revision_id>.zip``)."""
        return cls.artifact_dir(artifact_id) / f"{revision_id}.zip"

    @classmethod
    def tmp_root(cls) -> Path:
        return cls.root() / TMP_DIRNAME

    @classmethod
    def tmp_dir(cls) -> Path:
        """Create and return a fresh scratch folder."""
        path = cls.tmp_root() / uuid.uuid4().hex
        path.mkdir(parents=True, mode=0o755)
        return path

    @classmethod
    def promote(cls, tmp: Path, artifact_id: uuid.UUID, revision_id: uuid.UUID) -> Path:
        """Move an ingested scratch tree into place as ``<artifact>/<revision_id>/``.

        Raises ``FileExistsError`` when that revision folder already exists.
        """
        target = cls.revision_dir(artifact_id, revision_id)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        if target.exists():
            raise FileExistsError(str(target))
        os.rename(tmp, target)
        return target

    @staticmethod
    def discard_tmp(tmp: Path | None) -> None:
        """Remove a scratch folder; never raises."""
        if tmp is None:
            return
        shutil.rmtree(tmp, ignore_errors=True)

    @classmethod
    def remove_artifact(cls, artifact_id: uuid.UUID) -> bool:
        return remove_data_subdir(cls.root(), str(artifact_id), what="artifact")

    @classmethod
    def remove_revision(cls, artifact_id: uuid.UUID, revision_id: uuid.UUID) -> bool:
        """Remove a revision's folder and its cached zip; never raises."""
        try:
            cls.revision_zip(artifact_id, revision_id).unlink(missing_ok=True)
        except OSError as exc:
            logger.error("Failed to remove artifact revision zip %s: %s", revision_id, exc)
        return remove_data_subdir(
            cls.artifact_dir(artifact_id), str(revision_id), what="artifact revision"
        )

    @classmethod
    def sweep_stale_tmp(cls, max_age_seconds: int = STALE_TMP_SECONDS) -> int:
        """Remove scratch folders older than ``max_age_seconds``; returns how many."""
        tmp_root = cls.tmp_root()
        if not tmp_root.is_dir():
            return 0
        cutoff = time.time() - max_age_seconds
        removed = 0
        for entry in tmp_root.iterdir():
            try:
                if entry.is_symlink() or entry.stat().st_mtime >= cutoff:
                    continue
            except OSError:
                continue
            if remove_data_subdir(tmp_root, entry.name, what="artifact scratch"):
                removed += 1
        return removed
