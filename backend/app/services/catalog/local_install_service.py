"""LocalInstallService — installs Cinna Desktop reports (README U10, §5.1).

The desktop reports each agent it got and each catalog skill it pinned into a
local agent (``POST``) and each removal (``DELETE``). Core keeps one
``catalog_local_install`` row per (person, device, item, host agent) so that
``install_count``, ``my_installs``, the "N people use this" delete guard and
the ``catalog.updated`` audience cover every kind. Best effort: the desktop's
own state stays the source of truth for itself.

Also home of :meth:`LocalInstallService.update_audience` — who holds an
item, cloud or local, and so who hears ``catalog.updated`` (P9).
"""
from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete as sa_delete
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlmodel import Session, col, select

from app.models.agents.agent import Agent
from app.models.bundles.agent_bundle import BundleRunsOn
from app.models.bundles.agent_bundle_revision import (
    CATALOG_REVISION_ORIGINS,
    AgentBundleRevision,
)
from app.models.catalog.catalog_access import CatalogAccess
from app.models.catalog.catalog_item import CatalogInstall
from app.models.catalog.catalog_local_install import CatalogLocalInstall, LocalInstallIn
from app.models.plugins.llm_plugin import AgentPluginLink
from app.models.skills.skill_package import SkillPackage
from app.models.skills.skill_package_revision import SkillPackageRevision
from app.models.users.user import User
from app.services.catalog.catalog_access_service import (
    BUNDLE,
    NOT_FOUND_DETAIL,
    SKILL,
    CatalogAccessService,
    CatalogKindSpec,
)
from app.services.catalog.exceptions import (
    CatalogNotFoundError,
    CatalogValidationError,
    CloudOnlyError,
)


class LocalInstallService:
    """All methods static and sync (routes run them in the threadpool)."""

    # ── Mutations ──────────────────────────────────────────────────

    @staticmethod
    def record(
        session: Session, user: User, item_uuid: uuid.UUID, data: LocalInstallIn
    ) -> tuple[CatalogKindSpec, Any, str]:
        """Upsert the caller's row for (device, item, host agent); a repeat
        moves ``revision_number``. Returns the item's kind, the item and
        the caller's access to project it with.

        Gate: ``use`` (a superuser always passes), or, for a skill, a desktop
        agent the caller can use pins this very revision (U7). 404 otherwise.
        """
        spec, item = CatalogAccessService.resolve_item(session, item_uuid)
        access = LocalInstallService._gate(session, user, spec, item, data.revision_number)
        if spec is BUNDLE and item.runs_on == BundleRunsOn.CLOUD:
            raise CloudOnlyError()

        if spec is SKILL and data.host_manifest_id is None:
            raise CatalogValidationError(
                "A skill is installed into a local agent: host_manifest_id is required."
            )
        if spec is BUNDLE and data.host_manifest_id is not None:
            raise CatalogValidationError("An agent is not installed into another agent.")
        if not LocalInstallService._revision_exists(session, spec, item, data.revision_number):
            raise CatalogValidationError(
                f"This item has no version {data.revision_number}."
            )

        # One statement, so two first reports racing on the same key both
        # land on the row instead of one failing the unique constraint.
        now = datetime.now(UTC)
        stmt = pg_insert(CatalogLocalInstall).values(
            id=uuid.uuid4(),
            user_id=user.id,
            device_id=data.device_id,
            item_kind=spec.wire_kind,
            item_uuid=item.id,
            revision_number=data.revision_number,
            host_manifest_id=data.host_manifest_id,
            created_at=now,
            updated_at=now,
        )
        stmt = stmt.on_conflict_do_update(
            constraint="uq_catalog_local_install",
            set_={"revision_number": stmt.excluded.revision_number, "updated_at": now},
        )
        session.exec(stmt)  # type: ignore[call-overload]
        session.commit()
        return spec, item, access

    @staticmethod
    def remove(
        session: Session,
        user: User,
        item_uuid: uuid.UUID,
        device_id: str,
        host_manifest_id: uuid.UUID | None,
    ) -> None:
        """Delete the caller's rows for this item on ``device_id`` — the one
        for ``host_manifest_id`` when given, else every row on that device.
        Idempotent, and it never touches another person's rows; an unknown
        item is a no-op too (the item may have been deleted meanwhile)."""
        stmt = sa_delete(CatalogLocalInstall).where(
            CatalogLocalInstall.user_id == user.id,
            CatalogLocalInstall.device_id == device_id,
            CatalogLocalInstall.item_uuid == item_uuid,
        )
        if host_manifest_id is not None:
            stmt = stmt.where(CatalogLocalInstall.host_manifest_id == host_manifest_id)
        session.exec(stmt)  # type: ignore[call-overload]
        session.commit()

    @staticmethod
    def delete_for_item(session: Session, kind: str, item_uuid: uuid.UUID) -> None:
        """Stage the removal of every local row of an item being deleted
        (the caller commits)."""
        session.exec(  # type: ignore[call-overload]
            sa_delete(CatalogLocalInstall).where(
                CatalogLocalInstall.item_kind == kind,
                CatalogLocalInstall.item_uuid == item_uuid,
            )
        )

    # ── Reads ──────────────────────────────────────────────────────

    @staticmethod
    def people_by_item(
        session: Session, kind: str, item_uuids: Iterable[uuid.UUID]
    ) -> dict[uuid.UUID, set[uuid.UUID]]:
        """Distinct people holding a local install, per item (one query)."""
        wanted = set(item_uuids)
        if not wanted:
            return {}
        rows = session.exec(
            select(CatalogLocalInstall.item_uuid, CatalogLocalInstall.user_id)
            .where(
                CatalogLocalInstall.item_kind == kind,
                col(CatalogLocalInstall.item_uuid).in_(wanted),
            )
            .distinct()
        ).all()
        result: dict[uuid.UUID, set[uuid.UUID]] = defaultdict(set)
        for item_uuid, user_id in rows:
            result[item_uuid].add(user_id)
        return dict(result)

    @staticmethod
    def other_people(
        session: Session, kind: str, item_uuid: uuid.UUID, user_id: uuid.UUID
    ) -> set[uuid.UUID]:
        """People other than ``user_id`` with a local install of the item."""
        people = LocalInstallService.people_by_item(session, kind, [item_uuid])
        return people.get(item_uuid, set()) - {user_id}

    @staticmethod
    def mine(
        session: Session, user_id: uuid.UUID, kind: str, item_uuids: Iterable[uuid.UUID]
    ) -> dict[uuid.UUID, list[CatalogInstall]]:
        """The caller's local installs as ``my_installs`` entries, per item,
        ordered by device then host agent."""
        wanted = set(item_uuids)
        if not wanted:
            return {}
        rows = session.exec(
            select(CatalogLocalInstall).where(
                CatalogLocalInstall.user_id == user_id,
                CatalogLocalInstall.item_kind == kind,
                col(CatalogLocalInstall.item_uuid).in_(wanted),
            )
        ).all()
        result: dict[uuid.UUID, list[CatalogInstall]] = defaultdict(list)
        for row in sorted(rows, key=lambda r: (r.device_id, str(r.host_manifest_id or ""))):
            result[row.item_uuid].append(
                CatalogInstall(
                    where="local",
                    device_id=row.device_id,
                    host_manifest_id=str(row.host_manifest_id) if row.host_manifest_id else None,
                    revision_number=row.revision_number,
                )
            )
        return dict(result)

    @staticmethod
    def cloud_installers(
        session: Session, spec: CatalogKindSpec, item: Any
    ) -> set[uuid.UUID]:
        """Owners of agents built from a bundle or linking a skill package."""
        if spec is BUNDLE:
            cloud = set(
                session.exec(select(Agent.owner_id).where(Agent.bundle_uuid == item.id)).all()
            )
        else:
            cloud = set(
                session.exec(
                    select(Agent.owner_id)
                    .select_from(AgentPluginLink)
                    .join(
                        SkillPackageRevision,
                        SkillPackageRevision.id == AgentPluginLink.skill_package_revision_id,
                    )
                    .join(Agent, Agent.id == AgentPluginLink.agent_id)
                    .where(SkillPackageRevision.package_id == item.id)
                ).all()
            )
        return cloud

    @staticmethod
    def update_audience(session: Session, spec: CatalogKindSpec, item: Any) -> set[uuid.UUID]:
        """Who hears ``catalog.updated``: users reached through the item's
        grants (role grants expanded), its cloud installers, and its local
        installers who may still reach it (``use``, superuser, or U7 for a
        skill at the latest revision) (P9). No fan-out to everyone for public
        items."""
        audience = CatalogAccessService.readers(
            session, spec, item
        ) | LocalInstallService.cloud_installers(session, spec, item)
        local = LocalInstallService.people_by_item(session, spec.wire_kind, [item.id]).get(
            item.id, set()
        ) - audience
        if not local:
            return audience
        latest_number = None
        if spec is SKILL and item.latest_revision_id:
            latest = session.get(SkillPackageRevision, item.latest_revision_id)
            latest_number = latest.revision_number if latest else None
        for person in session.exec(select(User).where(col(User.id).in_(local))).all():
            if LocalInstallService._may_reach(session, person, spec, item, latest_number):
                audience.add(person.id)
        return audience

    # ── Helpers ────────────────────────────────────────────────────

    @staticmethod
    def _may_reach(
        session: Session,
        user: User,
        spec: CatalogKindSpec,
        item: Any,
        revision_number: int | None,
    ) -> bool:
        if not user.is_active:
            return False
        if user.is_superuser:
            return True
        if CatalogAccessService.effective_access(session, spec, item, user) is not None:
            return True
        return (
            spec is SKILL
            and revision_number is not None
            and LocalInstallService.pinned_by_usable_desktop_agent(
                session, item, revision_number, user
            )
        )

    @staticmethod
    def _gate(
        session: Session, user: User, spec: CatalogKindSpec, item: Any, revision_number: int
    ) -> str:
        access = CatalogAccessService.effective_access(session, spec, item, user)
        if access is None and user.is_superuser and user.is_active:
            access = CatalogAccess.MAINTAIN
        if access is not None:
            return access
        if spec is SKILL and LocalInstallService.pinned_by_usable_desktop_agent(
            session, item, revision_number, user
        ):
            return CatalogAccess.USE
        raise CatalogNotFoundError(NOT_FOUND_DETAIL)

    @staticmethod
    def pinned_by_usable_desktop_agent(
        session: Session, package: SkillPackage, revision_number: int, user: User
    ) -> bool:
        """U7: some desktop agent ``user`` can use has a latest revision whose
        ``catalog_skills[]`` pins this package at ``revision_number``."""
        bundles = [
            bundle
            for bundle, _ in CatalogAccessService.accessible_items(session, BUNDLE, user).values()
            if bundle.runs_on == BundleRunsOn.DESKTOP and bundle.latest_revision_id
        ]
        if not bundles:
            return False
        manifests = session.exec(
            select(AgentBundleRevision.manifest).where(
                col(AgentBundleRevision.id).in_([b.latest_revision_id for b in bundles])
            )
        ).all()
        wanted = str(package.id)
        for manifest in manifests:
            pins = manifest.get("catalog_skills") if isinstance(manifest, dict) else None
            for pin in pins or []:
                if (
                    isinstance(pin, dict)
                    and str(pin.get("package_uuid")) == wanted
                    and pin.get("revision_number") == revision_number
                ):
                    return True
        return False

    @staticmethod
    def _revision_exists(
        session: Session, spec: CatalogKindSpec, item: Any, revision_number: int
    ) -> bool:
        if spec is BUNDLE:
            stmt = select(AgentBundleRevision.id).where(
                AgentBundleRevision.bundle_id == item.id,
                AgentBundleRevision.revision_number == revision_number,
                col(AgentBundleRevision.origin).in_(CATALOG_REVISION_ORIGINS),
            )
        else:
            stmt = select(SkillPackageRevision.id).where(
                SkillPackageRevision.package_id == item.id,
                SkillPackageRevision.revision_number == revision_number,
            )
        return session.exec(stmt).first() is not None


__all__ = ["LocalInstallService"]
