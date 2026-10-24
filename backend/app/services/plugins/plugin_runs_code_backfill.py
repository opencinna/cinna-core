"""Re-derive ``runs_code`` of ready plugin snapshots from their stored archives.

The analyser's ``runs_code`` rule grew (commands / agents that run shell).
Rows analysed by the old rule are corrected here without a re-fetch: a
resync never re-reads an unchanged source key. The archive bytes, digest and
``components`` are untouched; only a ``false`` that the current rule calls
``true`` flips. Raw SQL on purpose: this runs from a data migration and must
not depend on the ORM model's future columns.
"""
from __future__ import annotations

import logging
import tarfile
import tempfile
import uuid
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from app.services.plugins.marketplace_skill_snapshots import MarketplaceSkillSnapshots
from app.services.plugins.plugin_components import analyse_plugin_tree

logger = logging.getLogger(__name__)

_SNAPSHOT_FORMATS = ("claude", "codex")


def runs_code_from_archive(
    archive: Path, name: str, config: dict | None, fmt: str
) -> bool | None:
    """``runs_code`` of the plugin tree in ``archive`` (members ``<name>/...``).

    None when the archive cannot be read or no longer analyses cleanly.
    """
    entry = dict(config) if isinstance(config, dict) else {}
    entry["name"] = name
    try:
        with tempfile.TemporaryDirectory() as tmp:
            with tarfile.open(archive, mode="r:gz") as tar:
                tar.extractall(tmp, filter="data")
            checkout = Path(tmp)
            analysis = analyse_plugin_tree(checkout / name, entry, fmt, checkout=checkout)
    except (OSError, tarfile.TarError, ValueError) as exc:
        logger.warning("runs_code backfill: cannot read %s: %s", archive, exc)
        return None
    return analysis.runs_code if analysis.reason is None else None


def backfill_runs_code(conn: Connection) -> int:
    """Flip ``runs_code`` false -> true where the stored archive now says so.

    Idempotent; a missing archive is skipped. Bumps the addons directory
    version once when any row changed. Returns the number of rows flipped.
    """
    rows = conn.execute(
        sa.text(
            "SELECT id, marketplace_id, name, config, plugin_type, archive_sha256 "
            "FROM llm_plugin_marketplace_plugin "
            "WHERE snapshot_status = 'ready' AND runs_code IS FALSE "
            "AND archive_sha256 IS NOT NULL AND plugin_type IN :formats"
        ).bindparams(sa.bindparam("formats", expanding=True)),
        {"formats": list(_SNAPSHOT_FORMATS)},
    ).all()
    flipped = 0
    for row in rows:
        archive = MarketplaceSkillSnapshots.existing_archive(
            uuid.UUID(str(row.marketplace_id)), uuid.UUID(str(row.id)), row.archive_sha256
        )
        if archive is None:
            continue
        try:
            runs_code = runs_code_from_archive(
                archive, row.name, row.config, row.plugin_type
            )
        except Exception as exc:  # one bad archive must never abort the pass
            logger.warning("runs_code backfill: row %s skipped: %r", row.id, exc)
            continue
        if runs_code is not True:
            continue
        result = conn.execute(
            sa.text(
                "UPDATE llm_plugin_marketplace_plugin SET runs_code = TRUE "
                "WHERE id = :id AND archive_sha256 = :sha AND runs_code IS FALSE"
            ),
            {"id": row.id, "sha": row.archive_sha256},
        )
        flipped += result.rowcount or 0
    if flipped:
        conn.execute(
            sa.text(
                "UPDATE server_config SET addons_directory_version = "
                "COALESCE(addons_directory_version, 0) + 1"
            )
        )
    logger.info("runs_code backfill: %d of %d rows flipped", flipped, len(rows))
    return flipped
