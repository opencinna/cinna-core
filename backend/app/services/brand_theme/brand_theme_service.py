"""Brand theme storage: drafts, publish/unpublish, the public projection.

The validator (``validator.check_package``) is the only judge of a package;
this module never stores anything it has not passed. Publishing re-runs the
validator because the rules may have tightened since the upload, and stores
its output (re-sanitised SVG bytes, normalised theme) if it differs — the
only content change a row ever sees.

Every write (create, delete, publish, unpublish) holds one transaction-level
advisory lock, so asset reuse and asset garbage collection never interleave
and there is never more than one publish in flight.
"""
from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlmodel import Session, col, select

from app.models import User
from app.models.brand_theme.brand_theme import (
    BRAND_THEME_MAX_DRAFTS,
    BRAND_THEME_REVISION_SEQUENCE,
    BRAND_THEME_STATUS_DRAFT,
    BRAND_THEME_STATUS_PUBLISHED,
    BrandTheme,
    BrandThemeAsset,
    BrandThemeDraftPublic,
    BrandThemeIssue,
    BrandThemeUploadResult,
)
from app.models.events.event import EventType
from app.services.brand_theme.served import AssetMeta, served_document
from app.services.brand_theme.validator import PackageResult, ValidationError, check_package
from app.services.events.event_service import event_service

logger = logging.getLogger(__name__)

# Arbitrary constant key for pg_advisory_xact_lock ("brandthm" in ASCII).
_ADVISORY_LOCK_KEY = 0x6272616E64746D


class BrandThemeError(Exception):
    """A request the service refuses. ``status`` maps to the HTTP status."""

    def __init__(self, status: int, detail: Any) -> None:
        super().__init__(str(detail))
        self.status = status
        self.detail = detail


def _issues(items: list[ValidationError]) -> list[BrandThemeIssue]:
    return [BrandThemeIssue(path=e.path, code=e.code, message=e.message) for e in items]


def _now() -> datetime:
    return datetime.now(UTC)


class BrandThemeService:
    # ── drafts ─────────────────────────────────────────────────────────

    @staticmethod
    def create_draft(
        session: Session, user: User, theme_json: bytes, assets: dict[str, bytes]
    ) -> BrandThemeUploadResult:
        """Validate and, only if valid, store a new draft (B3)."""
        BrandThemeService._lock(session)
        count = session.exec(select(func.count()).select_from(BrandTheme)).one()
        if count >= BRAND_THEME_MAX_DRAFTS:
            raise BrandThemeError(
                409,
                f"This server already holds {BRAND_THEME_MAX_DRAFTS} brand themes; delete an unused draft first.",
            )
        result = check_package(theme_json, assets)
        if not result.ok:
            return BrandThemeUploadResult(id=None, errors=_issues(result.errors), warnings=_issues(result.warnings))
        assert result.theme is not None
        BrandThemeService._store_assets(session, result)
        theme = BrandTheme(
            status=BRAND_THEME_STATUS_DRAFT,
            name=result.theme["name"],
            theme_json=result.theme,
            asset_map={path: asset.sha256 for path, asset in result.assets.items()},
            created_by_id=user.id,
        )
        session.add(theme)
        session.commit()
        session.refresh(theme)
        return BrandThemeUploadResult(id=theme.id, errors=[], warnings=_issues(result.warnings))

    @staticmethod
    def _store_assets(session: Session, result: PackageResult) -> None:
        # Content-addressed and immutable: an existing row already holds these
        # exact bytes, so a conflict is a no-op.
        for asset in result.assets.values():
            session.execute(
                pg_insert(BrandThemeAsset)
                .values(
                    sha256=asset.sha256,
                    mime=asset.mime,
                    bytes=asset.size,
                    width=asset.width,
                    height=asset.height,
                    data=asset.data,
                    created_at=_now(),
                )
                .on_conflict_do_nothing(index_elements=["sha256"])
            )
        session.flush()

    @staticmethod
    def list_drafts(session: Session) -> list[BrandThemeDraftPublic]:
        rows = session.exec(select(BrandTheme).order_by(col(BrandTheme.created_at).desc())).all()
        return [BrandThemeService.to_public(session, row) for row in rows]

    @staticmethod
    def get_draft(session: Session, theme_id: uuid.UUID) -> BrandThemeDraftPublic:
        return BrandThemeService.to_public(session, BrandThemeService._get(session, theme_id))

    @staticmethod
    def _get(session: Session, theme_id: uuid.UUID) -> BrandTheme:
        row = session.get(BrandTheme, theme_id)
        if row is None:
            raise BrandThemeError(404, "Brand theme not found")
        return row

    @staticmethod
    def delete_draft(session: Session, theme_id: uuid.UUID) -> None:
        BrandThemeService._lock(session)
        row = BrandThemeService._get(session, theme_id)
        if row.status == BRAND_THEME_STATUS_PUBLISHED:
            raise BrandThemeError(409, "This theme is published; unpublish it before deleting it.")
        candidates = set(row.asset_map.values())
        session.delete(row)
        session.flush()
        BrandThemeService._drop_orphan_assets(session, candidates)
        session.commit()

    @staticmethod
    def _drop_orphan_assets(session: Session, candidates: set[str]) -> None:
        if not candidates:
            return
        still_used: set[str] = set()
        for asset_map in session.exec(select(BrandTheme.asset_map)).all():
            still_used.update(asset_map.values())
        for sha in candidates - still_used:
            asset = session.get(BrandThemeAsset, sha)
            if asset is not None:
                session.delete(asset)

    # ── publish ────────────────────────────────────────────────────────

    @staticmethod
    def _lock(session: Session) -> None:
        """Serialise every brand-theme write for the rest of the transaction.
        Row locks would not do: an empty table has nothing to lock, and a new
        draft reusing an asset is invisible to a concurrent delete's GC."""
        session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _ADVISORY_LOCK_KEY})

    @staticmethod
    def publish(
        session: Session, user: User, theme_id: uuid.UUID
    ) -> tuple[BrandThemeDraftPublic, bool]:
        """Re-validate, then make this the server's one published theme.

        Returns the theme and whether anything changed: publishing the theme
        that is already live with unchanged bytes is a no-op (no new
        revision, so desktops keep their 304s).

        Raises ``BrandThemeError(409, BrandThemePublishErrors-shaped dict)``
        when the draft no longer passes the rules.
        """
        BrandThemeService._lock(session)
        row = BrandThemeService._get(session, theme_id)
        assets = {
            path: asset.data
            for path, sha in row.asset_map.items()
            if (asset := session.get(BrandThemeAsset, sha)) is not None
        }
        result = check_package(row.theme_json, assets)
        if not result.ok:
            session.rollback()
            raise BrandThemeError(409, {"errors": [i.model_dump() for i in _issues(result.errors)]})
        assert result.theme is not None
        # A tightened sanitiser may now produce different bytes for the same
        # SVG; what is served must always be the current sanitiser's output.
        new_map = {path: asset.sha256 for path, asset in result.assets.items()}
        unchanged = new_map == row.asset_map and result.theme == row.theme_json
        if unchanged and row.status == BRAND_THEME_STATUS_PUBLISHED:
            session.rollback()
            return BrandThemeService.to_public(session, row), False
        if not unchanged:
            BrandThemeService._store_assets(session, result)
            old = set(row.asset_map.values()) - set(new_map.values())
            row.asset_map = new_map
            row.theme_json = result.theme
            session.add(row)
            session.flush()
            BrandThemeService._drop_orphan_assets(session, old)

        current = session.exec(
            select(BrandTheme).where(BrandTheme.status == BRAND_THEME_STATUS_PUBLISHED)
        ).first()
        if current is not None and current.id != row.id:
            current.status = BRAND_THEME_STATUS_DRAFT
            current.updated_at = _now()
            session.add(current)
            # Release the unique slot before claiming it below.
            session.flush()
        revision = session.execute(text(f"SELECT nextval('{BRAND_THEME_REVISION_SEQUENCE}')")).scalar_one()
        row.status = BRAND_THEME_STATUS_PUBLISHED
        row.revision = int(revision)
        row.published_at = _now()
        row.published_by_id = user.id
        row.updated_at = row.published_at
        session.add(row)
        session.commit()
        session.refresh(row)
        logger.info("Brand theme %s published as revision %s by %s", row.id, row.revision, user.id)
        return BrandThemeService.to_public(session, row), True

    @staticmethod
    def unpublish(session: Session) -> int | None:
        """Return the published theme to draft. Returns the revision that was
        live, or None when nothing was published (idempotent)."""
        BrandThemeService._lock(session)
        current = session.exec(
            select(BrandTheme).where(BrandTheme.status == BRAND_THEME_STATUS_PUBLISHED)
        ).first()
        if current is None:
            session.rollback()
            return None
        revision = current.revision
        current.status = BRAND_THEME_STATUS_DRAFT
        current.updated_at = _now()
        session.add(current)
        session.commit()
        logger.info("Brand theme %s (revision %s) unpublished", current.id, revision)
        return revision

    # ── public reads ───────────────────────────────────────────────────

    @staticmethod
    def published(session: Session) -> BrandTheme | None:
        return session.exec(
            select(BrandTheme).where(BrandTheme.status == BRAND_THEME_STATUS_PUBLISHED)
        ).first()

    @staticmethod
    def document(session: Session, row: BrandTheme, revision: int | None = None) -> dict[str, Any]:
        """The served projection (README §3) of any stored theme."""
        metas: dict[str, AssetMeta] = {}
        shas = set(row.asset_map.values())
        if shas:
            found = session.exec(
                select(
                    BrandThemeAsset.sha256,
                    BrandThemeAsset.mime,
                    BrandThemeAsset.size,
                    BrandThemeAsset.width,
                    BrandThemeAsset.height,
                ).where(col(BrandThemeAsset.sha256).in_(shas))
            ).all()
            by_sha = {r[0]: AssetMeta(sha256=r[0], mime=r[1], size=r[2], width=r[3], height=r[4]) for r in found}
            metas = {path: by_sha[sha] for path, sha in row.asset_map.items() if sha in by_sha}
        rev = revision if revision is not None else (row.revision or 0)
        return served_document(row.theme_json, metas, rev)

    @staticmethod
    def get_asset(session: Session, sha256: str) -> BrandThemeAsset | None:
        return session.get(BrandThemeAsset, sha256)

    # ── projection ─────────────────────────────────────────────────────

    @staticmethod
    def to_public(session: Session, row: BrandTheme) -> BrandThemeDraftPublic:
        emails: dict[uuid.UUID, str] = {}
        ids = {i for i in (row.created_by_id, row.published_by_id) if i is not None}
        if ids:
            for uid, email in session.exec(select(User.id, User.email).where(col(User.id).in_(ids))).all():
                emails[uid] = email
        return BrandThemeDraftPublic(
            id=row.id,
            status=row.status,
            name=row.name,
            revision=row.revision,
            created_at=row.created_at,
            created_by_email=emails.get(row.created_by_id) if row.created_by_id else None,
            published_at=row.published_at,
            published_by_email=emails.get(row.published_by_id) if row.published_by_id else None,
            document=BrandThemeService.document(session, row),
        )


class BrandThemeEvents:
    @staticmethod
    async def updated(revision: int | None) -> None:
        """``brand_theme.updated`` to every connected client. Never raises:
        the publish or unpublish has already committed."""
        try:
            await event_service.emit_event(
                event_type=EventType.BRAND_THEME_UPDATED,
                meta={"published": revision is not None, "revision": revision},
            )
        except Exception:
            logger.exception("Failed to emit brand_theme.updated")
