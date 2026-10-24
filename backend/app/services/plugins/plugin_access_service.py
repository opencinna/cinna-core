"""Marketplace access mode and per-entry allow / ban (K5).

Which of a marketplace's entries users see:

- ``allow_all``: every entry except ``banned``;
- ``allowlist``: only ``allowed`` entries (new ones from a sync wait).

The verdict lives in ``llm_plugin_entry_access``, keyed by
``(marketplace_id, entry_name)`` so it survives a resync and an entry removed
from the repository and re-added (which gets a new plugin row id). No row is
``default``. The rule hides entries from discover, batch, archive and the
addons directory for everyone, superusers included; snapshots, cloud installs
and existing agent links are deliberately not gated (plan A5).
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import and_, func, literal, or_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlmodel import Session, select

from app.models.plugins.llm_plugin import (
    LLMPluginEntryAccess,
    LLMPluginMarketplace,
    LLMPluginMarketplacePlugin,
    MarketplaceAccessModeResult,
)
from app.models.users.user import User
from app.services.addons.directory_version import AddonsDirectoryVersion


class PluginAccessService:
    # ── Visibility rule ────────────────────────────────────────────────

    @staticmethod
    def join_condition():
        """Outer-join condition from a plugin row to its access row."""
        return and_(
            LLMPluginEntryAccess.marketplace_id
            == LLMPluginMarketplacePlugin.marketplace_id,
            LLMPluginEntryAccess.entry_name == LLMPluginMarketplacePlugin.name,
        )

    @staticmethod
    def _access_expr():
        return func.coalesce(LLMPluginEntryAccess.access, "default")

    @staticmethod
    def _visible_under(mode_expr):
        access = PluginAccessService._access_expr()
        return or_(
            and_(mode_expr == "allow_all", access != "banned"),
            and_(mode_expr == "allowlist", access == "allowed"),
        )

    @staticmethod
    def entry_visible_clause():
        """SQL form of :meth:`is_entry_visible`.

        Needs ``LLMPluginMarketplace`` joined and ``LLMPluginEntryAccess``
        outer-joined on :meth:`join_condition`.
        """
        return PluginAccessService._visible_under(LLMPluginMarketplace.access_mode)

    @staticmethod
    def is_entry_visible(
        marketplace: LLMPluginMarketplace, access_row: LLMPluginEntryAccess | None
    ) -> bool:
        """Python form of :meth:`entry_visible_clause`. An unknown stored mode
        shows nothing, as the SQL form does."""
        access = access_row.access if access_row is not None else "default"
        if marketplace.access_mode == "allow_all":
            return access != "banned"
        if marketplace.access_mode == "allowlist":
            return access == "allowed"
        return False

    @staticmethod
    def access_row_for(
        session: Session, plugin: LLMPluginMarketplacePlugin
    ) -> LLMPluginEntryAccess | None:
        return session.exec(
            select(LLMPluginEntryAccess)
            .where(LLMPluginEntryAccess.marketplace_id == plugin.marketplace_id)
            .where(LLMPluginEntryAccess.entry_name == plugin.name)
        ).first()

    @staticmethod
    def visible_count(
        session: Session, marketplace: LLMPluginMarketplace, mode: str
    ) -> int:
        """Entries of ``marketplace`` users would see under ``mode``."""
        return session.exec(
            select(func.count())
            .select_from(LLMPluginMarketplacePlugin)
            .outerjoin(LLMPluginEntryAccess, PluginAccessService.join_condition())
            .where(LLMPluginMarketplacePlugin.marketplace_id == marketplace.id)
            .where(PluginAccessService._visible_under(literal(mode)))
        ).one()

    # ── Writes (superuser routes) ──────────────────────────────────────

    @staticmethod
    def _upsert(
        session: Session,
        marketplace_id: uuid.UUID,
        names: list[str],
        access: str,
        reason: str | None,
        user: User,
    ) -> None:
        if not names:
            return
        now = datetime.now(UTC)
        reason = (reason or "").strip() or None
        statement = pg_insert(LLMPluginEntryAccess).values(
            [
                {
                    "id": uuid.uuid4(),
                    "marketplace_id": marketplace_id,
                    "entry_name": name,
                    "access": access,
                    "reason": reason,
                    "changed_by_id": user.id,
                    "changed_at": now,
                }
                for name in names
            ]
        )
        statement = statement.on_conflict_do_update(
            index_elements=["marketplace_id", "entry_name"],
            set_={
                "access": statement.excluded.access,
                "reason": statement.excluded.reason,
                "changed_by_id": statement.excluded.changed_by_id,
                "changed_at": statement.excluded.changed_at,
            },
        )
        session.execute(statement)

    @staticmethod
    def set_access(
        session: Session,
        plugin_id: uuid.UUID,
        access: str,
        reason: str | None,
        user: User,
    ) -> tuple[LLMPluginMarketplacePlugin, LLMPluginEntryAccess | None]:
        """Record ``access`` for the entry behind ``plugin_id``.

        Raises:
            HTTPException(404, ``not_found``): unknown plugin id.
        """
        plugin = session.get(LLMPluginMarketplacePlugin, plugin_id)
        if plugin is None:
            raise HTTPException(status_code=404, detail={"code": "not_found"})
        PluginAccessService._upsert(
            session, plugin.marketplace_id, [plugin.name], access, reason, user
        )
        AddonsDirectoryVersion.bump(session)
        session.commit()
        return plugin, PluginAccessService.access_row_for(session, plugin)

    @staticmethod
    def set_access_bulk(
        session: Session,
        marketplace: LLMPluginMarketplace,
        ids: list[uuid.UUID],
        access: str,
        reason: str | None,
        user: User,
    ) -> int:
        """One verdict for many entries of one marketplace; all or nothing.

        Returns the number of distinct entries written.

        Raises:
            HTTPException(422, ``foreign_ids``): an id that is unknown or
                belongs to another marketplace (listed in ``detail.ids``).
        """
        wanted = list(dict.fromkeys(ids))
        names = dict(
            session.exec(
                select(LLMPluginMarketplacePlugin.id, LLMPluginMarketplacePlugin.name)
                .where(LLMPluginMarketplacePlugin.marketplace_id == marketplace.id)
                .where(LLMPluginMarketplacePlugin.id.in_(wanted))
            ).all()
        )
        foreign = [str(pid) for pid in wanted if pid not in names]
        if foreign:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "foreign_ids",
                    "message": "Every id must be an entry of this marketplace.",
                    "ids": foreign[:50],
                },
            )
        entry_names = sorted(set(names.values()))
        PluginAccessService._upsert(
            session, marketplace.id, entry_names, access, reason, user
        )
        AddonsDirectoryVersion.bump(session)
        session.commit()
        return len(entry_names)

    @staticmethod
    def set_mode(
        session: Session,
        marketplace: LLMPluginMarketplace,
        mode: str,
        *,
        dry_run: bool,
    ) -> MarketplaceAccessModeResult:
        """Switch the marketplace's access mode, or (``dry_run``) only report
        what users would see. Entry verdicts are never touched."""
        previous = marketplace.access_mode
        result = MarketplaceAccessModeResult(
            access_mode=mode,
            previous_mode=previous,
            visible_before=PluginAccessService.visible_count(
                session, marketplace, previous
            ),
            visible_after=PluginAccessService.visible_count(session, marketplace, mode),
            applied=not dry_run,
        )
        if dry_run:
            return result
        if mode != previous:
            marketplace.access_mode = mode
            marketplace.updated_at = datetime.now(UTC)
            session.add(marketplace)
            AddonsDirectoryVersion.bump(session)
            session.commit()
        return result
