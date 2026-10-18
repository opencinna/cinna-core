"""Content-addressed snapshots of skills-marketplace entries, filesystem only.

Every sync of a ``skills`` marketplace archives each supported skill folder
with the shared deterministic builder and keeps the result at::

    <SKILL_STORAGE_DIR>/marketplace-skills/<marketplace_id>/<plugin_id>/<sha256>.tar.gz

The file name is the archive's own sha256, so identical folder content lands
on the same file and a resync of an unchanged entry writes nothing. Every
digest that was ever current is kept until the entry or the marketplace goes,
so a desktop pin to an older digest keeps resolving.

Archives are first built into ``marketplace-skills/<marketplace_id>/.staging/
<sync uuid>/`` on the same volume and promoted by ``os.replace`` just before
the sync commits, so a committed row never names a digest whose file is
missing. Staging inside the marketplace dir means a crashed sync's leftovers
go with the marketplace/user-delete ``rmtree``; ``.staging`` cannot collide
with the uuid-named plugin dirs beside it.

The catalog only ever creates ``<uuid>/`` children of ``SKILL_STORAGE_DIR``,
so the literal ``marketplace-skills`` cannot collide with it.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

from app.core.config import settings
from app.services.agents.skill_manifest import DEFAULT_MAX_TOTAL_BYTES
from app.services.common.data_dirs import remove_data_subdir
from app.services.skills.skill_archive import build_skill_archive, walk_skill_tree

logger = logging.getLogger(__name__)

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
STAGING_DIR_NAME = ".staging"


@dataclass(frozen=True)
class StagedArchive:
    path: Path
    sha256: str
    size: int


@dataclass(frozen=True)
class TooLarge:
    """The skill's uncompressed tree is over the catalog's 16 MB cap."""

    total_bytes: int


class MarketplaceSkillSnapshots:
    @staticmethod
    def root() -> Path:
        return Path(settings.SKILL_STORAGE_DIR) / "marketplace-skills"

    @staticmethod
    def marketplace_dir(marketplace_id: uuid.UUID) -> Path:
        return MarketplaceSkillSnapshots.root() / str(marketplace_id)

    @staticmethod
    def entry_dir(marketplace_id: uuid.UUID, plugin_id: uuid.UUID) -> Path:
        return MarketplaceSkillSnapshots.marketplace_dir(marketplace_id) / str(plugin_id)

    @staticmethod
    def archive_path(
        marketplace_id: uuid.UUID, plugin_id: uuid.UUID, sha256: str
    ) -> Path:
        if not SHA256_RE.match(sha256):
            raise ValueError("sha256 must be 64 lowercase hex characters")
        return (
            MarketplaceSkillSnapshots.entry_dir(marketplace_id, plugin_id)
            / f"{sha256}.tar.gz"
        )

    @staticmethod
    def new_staging_dir(marketplace_id: uuid.UUID) -> Path:
        """A fresh per-sync staging folder inside the marketplace's dir."""
        staging = (
            MarketplaceSkillSnapshots.marketplace_dir(marketplace_id)
            / STAGING_DIR_NAME
            / str(uuid.uuid4())
        )
        # A concurrent sync's cleanup may rmdir the empty ``.staging`` parent
        # between ``mkdir(parents=True)`` creating it and creating the child;
        # retry so that race never fails a sync.
        for attempt in range(3):
            try:
                staging.mkdir(parents=True, exist_ok=False)
                return staging
            except FileNotFoundError:
                if attempt == 2:
                    raise
        return staging

    @staticmethod
    def stage(
        staging_dir: Path, skill_dir: Path, skill_name: str
    ) -> StagedArchive | TooLarge:
        """Build one skill's archive into ``staging_dir``, or report it too large.

        The cap is checked over the uncompressed tree before any bytes are
        built; ``size`` is the compressed archive size.
        """
        files, total_bytes = walk_skill_tree(skill_dir)
        if total_bytes > DEFAULT_MAX_TOTAL_BYTES:
            return TooLarge(total_bytes=total_bytes)
        data = build_skill_archive(skill_name, files)
        path = staging_dir / f"{uuid.uuid4()}.tar.gz"
        path.write_bytes(data)
        return StagedArchive(
            path=path, sha256=hashlib.sha256(data).hexdigest(), size=len(data)
        )

    @staticmethod
    def promote(
        staged: StagedArchive, marketplace_id: uuid.UUID, plugin_id: uuid.UUID
    ) -> None:
        """Move a staged archive to its content-addressed final path.

        When the final file already exists its bytes are equal by construction,
        so the staged copy is dropped and nothing is rewritten.
        """
        final = MarketplaceSkillSnapshots.archive_path(
            marketplace_id, plugin_id, staged.sha256
        )
        if final.exists():
            staged.path.unlink(missing_ok=True)
            return
        final.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staged.path, final)

    @staticmethod
    def existing_archive(
        marketplace_id: uuid.UUID, plugin_id: uuid.UUID, sha256: str
    ) -> Path | None:
        try:
            path = MarketplaceSkillSnapshots.archive_path(
                marketplace_id, plugin_id, sha256
            )
        except ValueError:
            return None
        if path.is_symlink() or not path.is_file():
            return None
        return path

    @staticmethod
    def remove_entry(marketplace_id: uuid.UUID, plugin_id: uuid.UUID) -> None:
        """Best effort: drop every kept digest of one entry. Never raises."""
        remove_data_subdir(
            MarketplaceSkillSnapshots.marketplace_dir(marketplace_id),
            str(plugin_id),
            what="marketplace skill snapshot",
        )

    @staticmethod
    def remove_marketplace(marketplace_id: uuid.UUID) -> None:
        """Best effort: drop every snapshot of one marketplace. Never raises."""
        remove_data_subdir(
            MarketplaceSkillSnapshots.root(),
            str(marketplace_id),
            what="marketplace skill snapshots",
        )
