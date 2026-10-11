"""CatalogItemService — the unified catalog (``/api/v1/catalog/items``).

Projects agent bundles (cloud and desktop) and skill packages into one
``CatalogItem`` shape (README §5.1) and runs the item-level PATCH and DELETE.
Who sees what is :class:`CatalogAccessService` (rule P1); management is
``maintain`` or superuser (P2). Listing rules:

- ownerless **cloud** bundles (git imports) are never listed; a desktop agent
  whose owner is gone stays listed;
- an item with no revision is listed only to people with ``edit``/``maintain``
  (P4);
- ``scope=all`` (superuser only) lists every item with ``access='maintain'``
  (P3).

Every projection input is fetched in grouped queries per kind (no N+1).
"""
from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Iterable
from typing import Any

from sqlmodel import Session, col, select

from app.models.agents.agent import Agent
from app.models.bundles.agent_bundle import AgentBundle, AgentBundleUpdate, BundleRunsOn
from app.models.bundles.agent_bundle_revision import (
    CATALOG_REVISION_ORIGINS,
    AgentBundleRevision,
)
from app.models.catalog.catalog_access import CatalogAccess, CatalogPerson
from app.models.catalog.catalog_item import (
    CatalogDesktop,
    CatalogInstall,
    CatalogItem,
    CatalogItemDetail,
    CatalogItemSkill,
    CatalogItemUpdate,
    CatalogLatest,
    CatalogRevision,
    CatalogSkillContent,
)
from app.models.events.security_event import CATALOG_ITEM_DELETED
from app.models.library.git_repo import GitRepo
from app.models.plugins.llm_plugin import AgentPluginLink
from app.models.skills.schemas import SkillPackageUpdate
from app.models.skills.skill_package import SkillPackage
from app.models.skills.skill_package_revision import SkillPackageRevision
from app.models.users.user import User
from app.services.bundles.bundle_service import BundleService
from app.services.bundles.credential_spec import listing_credential_specs
from app.services.bundles.exceptions import BundleError
from app.services.catalog.catalog_access_service import (
    BUNDLE,
    NOT_FOUND_DETAIL,
    SKILL,
    CatalogAccessService,
    CatalogKindSpec,
    display_name,
)
from app.services.catalog.catalog_events import add_audit_event
from app.services.catalog.desktop_agent_service import DesktopAgentService
from app.services.catalog.local_install_service import LocalInstallService
from app.services.catalog.exceptions import (
    CatalogAccessDeniedError,
    CatalogConflictError,
    CatalogError,
    CatalogNotFoundError,
    CatalogValidationError,
)
from app.services.library.git_repo_storage import GitRepoStorage
from app.services.skills.exceptions import SkillCatalogError
from app.services.skills.skill_catalog_service import (
    SkillCatalogService,
    people_use_detail,
)

SCOPE_ALL = "all"
#: ``CatalogRevision.origin`` of a skill package revision.
SKILL_REVISION_ORIGIN = "publish"


class CatalogItemService:
    """All methods static and sync (routes run them in the threadpool)."""

    # ── Reads ──────────────────────────────────────────────────────

    @staticmethod
    def list_items(
        session: Session,
        user: User,
        kind: str | None = None,
        runs_on: str | None = None,
        scope: str | None = None,
    ) -> list[CatalogItem]:
        """Every item ``user`` may list, agents first (by display name), then
        skills. ``runs_on`` narrows agents (and so leaves out skills)."""
        if scope == SCOPE_ALL and not user.is_superuser:
            raise CatalogAccessDeniedError("Only an administrator can list every item.")
        everything = scope == SCOPE_ALL

        items: list[CatalogItem] = []
        if kind in (None, "agent"):
            pairs = CatalogItemService._bundle_pairs(session, user, everything)
            if runs_on is not None:
                pairs = [(b, a) for b, a in pairs if b.runs_on == runs_on]
            items += CatalogItemService._project_bundles(session, user, pairs)
        if kind in (None, "skill") and runs_on is None:
            pairs = CatalogItemService._skill_pairs(session, user, everything)
            items += CatalogItemService._project_skills(session, user, pairs)
        return items

    @staticmethod
    def get(session: Session, user: User, item_uuid: uuid.UUID) -> CatalogItemDetail:
        """One item with its versions (newest first). 404 unless the caller
        has at least ``use``; a superuser sees any item as ``maintain``."""
        spec, item, access = CatalogItemService._visible_item(session, user, item_uuid)
        if spec is BUNDLE:
            base = CatalogItemService._project_bundles(session, user, [(item, access)])[0]
            revisions = CatalogItemService._bundle_revisions(session, item)
        else:
            base = CatalogItemService._project_skills(session, user, [(item, access)])[0]
            revisions = CatalogItemService._skill_revisions(session, item)
        return CatalogItemDetail(**base.model_dump(), revisions=revisions)

    @staticmethod
    def get_item(session: Session, user: User, item_uuid: uuid.UUID) -> CatalogItem:
        """One item without its versions."""
        spec, item, access = CatalogItemService._visible_item(session, user, item_uuid)
        return CatalogItemService.project(session, user, spec, item, access)

    @staticmethod
    def project(
        session: Session, user: User, spec: CatalogKindSpec, item: Any, access: str
    ) -> CatalogItem:
        """One item as ``user`` sees it, at an access level the caller has
        already established (e.g. the U7 ``use`` of a local skill install)."""
        if spec is BUNDLE:
            return CatalogItemService._project_bundles(session, user, [(item, access)])[0]
        return CatalogItemService._project_skills(session, user, [(item, access)])[0]

    # ── Mutations ──────────────────────────────────────────────────

    @staticmethod
    def update(
        session: Session, user: User, item_uuid: uuid.UUID, data: CatalogItemUpdate
    ) -> tuple[CatalogItem, CatalogKindSpec, set[uuid.UUID]]:
        """Rename, re-describe, change visibility or listing (P2). Returns the
        item, its kind and the users who just gained access through grants."""
        spec, item = CatalogAccessService.get_managed_item(session, item_uuid, user)
        fields = data.model_dump(exclude_unset=True)
        before = CatalogAccessService.readers(session, spec, item)
        if spec is BUNDLE:
            try:
                BundleService.update_bundle(session, item, AgentBundleUpdate(**fields))
            except BundleError as exc:
                raise CatalogValidationError(str(exc)) from exc
        else:
            try:
                SkillCatalogService.apply_update(session, item, SkillPackageUpdate(**fields))
            except SkillCatalogError as exc:
                raise CatalogValidationError(exc.message) from exc
        gained = CatalogAccessService.readers(session, spec, item) - before
        return CatalogItemService.get_item(session, user, item_uuid), spec, gained

    @staticmethod
    def delete(session: Session, user: User, item_uuid: uuid.UUID) -> None:
        """Remove an item while nobody else uses it (P2, P12)."""
        spec, item = CatalogAccessService.get_managed_item(session, item_uuid, user)
        audit = {
            "kind": spec.wire_kind,
            "uuid": str(item.id),
            "id": item.bundle_id if spec is BUNDLE else item.package_id,
        }
        if spec is SKILL:
            add_audit_event(session, user.id, CATALOG_ITEM_DELETED, audit)
            try:
                SkillCatalogService.delete_package(session, item, user)
            except SkillCatalogError as exc:
                session.rollback()
                raise CatalogConflictError(exc.message) from exc
            return

        if item.runs_on == BundleRunsOn.DESKTOP:
            holders = DesktopAgentService.other_holders(session, item, user)
            holders |= LocalInstallService.other_people(
                session, BUNDLE.wire_kind, item.id, user.id
            )
        else:
            # Every consumer install counts, the caller's own included, exactly
            # as ``BundleService.delete_bundle`` (``foreign_install_count``)
            # decides — so its refusal never surfaces with its old sentence.
            holders = set(
                session.exec(
                    select(Agent.owner_id).where(
                        Agent.bundle_uuid == item.id,
                        Agent.is_publisher_install == False,  # noqa: E712
                    )
                ).all()
            )
        if holders:
            raise CatalogConflictError(people_use_detail(len(holders)))
        add_audit_event(session, user.id, CATALOG_ITEM_DELETED, audit)
        LocalInstallService.delete_for_item(session, BUNDLE.wire_kind, item.id)
        if item.runs_on == BundleRunsOn.DESKTOP:
            DesktopAgentService.delete(session, item)
            return
        try:
            BundleService.delete_bundle(session, item)
        except BundleError as exc:
            session.rollback()
            raise CatalogConflictError(str(exc)) from exc

    # ── Access ─────────────────────────────────────────────────────

    @staticmethod
    def _visible_item(
        session: Session, user: User, item_uuid: uuid.UUID
    ) -> tuple[CatalogKindSpec, Any, str]:
        spec, item = CatalogAccessService.resolve_item(session, item_uuid)
        access = CatalogAccessService.effective_access(session, spec, item, user)
        if access is None and user.is_superuser and user.is_active:
            access = CatalogAccess.MAINTAIN
        if access is None:
            raise CatalogNotFoundError(NOT_FOUND_DETAIL)
        return spec, item, access

    @staticmethod
    def _bundle_pairs(
        session: Session, user: User, everything: bool
    ) -> list[tuple[AgentBundle, str]]:
        if everything:
            rows = session.exec(select(AgentBundle)).all()
            pairs = [(b, CatalogAccess.MAINTAIN) for b in rows]
        else:
            pairs = list(CatalogAccessService.accessible_items(session, BUNDLE, user).values())
        pairs = [
            (b, a)
            for b, a in pairs
            if not (b.publisher_user_id is None and b.runs_on == BundleRunsOn.CLOUD)
            and (b.latest_revision_id is not None or _can_edit(a))
        ]
        pairs.sort(key=lambda p: (p[0].display_name.lower(), str(p[0].id)))
        return pairs

    @staticmethod
    def _skill_pairs(
        session: Session, user: User, everything: bool
    ) -> list[tuple[SkillPackage, str]]:
        if everything:
            rows = session.exec(select(SkillPackage)).all()
            pairs = [(p, CatalogAccess.MAINTAIN) for p in rows]
        else:
            pairs = list(CatalogAccessService.accessible_items(session, SKILL, user).values())
        pairs = [(p, a) for p, a in pairs if p.latest_revision_id is not None or _can_edit(a)]
        pairs.sort(key=lambda p: (p[0].display_name.lower(), str(p[0].id)))
        return pairs

    # ── Projection: bundles ────────────────────────────────────────

    @staticmethod
    def _project_bundles(
        session: Session, user: User, pairs: list[tuple[AgentBundle, str]]
    ) -> list[CatalogItem]:
        if not pairs:
            return []
        bundles = [b for b, _ in pairs]
        ids = [b.id for b in bundles]
        revisions = _by_id(
            session,
            AgentBundleRevision,
            [b.latest_revision_id for b in bundles if b.latest_revision_id],
        )
        repos = _by_id(session, GitRepo, [b.git_repo_id for b in bundles if b.git_repo_id])
        maintainers = _maintainers(session, BUNDLE, ids)
        people = _people(
            session,
            [b.publisher_user_id for b in bundles]
            + [r.published_by_user_id for r in revisions.values()],
        )
        # Distinct people over cloud installs (the publisher's working install
        # excluded, as before) and local rows (the publisher excluded).
        installers: dict[uuid.UUID, set[uuid.UUID]] = defaultdict(set)
        for bundle_uuid, owner_id in session.exec(
            select(Agent.bundle_uuid, Agent.owner_id)
            .where(
                col(Agent.bundle_uuid).in_(ids),
                Agent.is_publisher_install == False,  # noqa: E712
            )
            .distinct()
        ).all():
            installers[bundle_uuid].add(owner_id)
        publishers = {b.id: b.publisher_user_id for b in bundles}
        for bundle_uuid, people_ids in LocalInstallService.people_by_item(
            session, BUNDLE.wire_kind, ids
        ).items():
            installers[bundle_uuid] |= people_ids - {publishers.get(bundle_uuid)}
        install_counts = {k: len(v) for k, v in installers.items()}
        my_agents = session.exec(
            select(Agent.id, Agent.bundle_uuid, Agent.installed_revision_id).where(
                col(Agent.bundle_uuid).in_(ids), Agent.owner_id == user.id
            )
        ).all()
        installed_numbers = dict(
            session.exec(
                select(AgentBundleRevision.id, AgentBundleRevision.revision_number).where(
                    col(AgentBundleRevision.id).in_(
                        [rev_id for _, _, rev_id in my_agents if rev_id]
                    )
                )
            ).all()
        ) if any(rev_id for _, _, rev_id in my_agents) else {}
        my_installs: dict[uuid.UUID, list[CatalogInstall]] = defaultdict(list)
        for agent_id, bundle_uuid, rev_id in sorted(my_agents, key=lambda r: str(r[0])):
            my_installs[bundle_uuid].append(
                CatalogInstall(
                    where="cloud",
                    install_id=agent_id,
                    revision_number=installed_numbers.get(rev_id) if rev_id else None,
                )
            )
        for bundle_uuid, local in LocalInstallService.mine(
            session, user.id, BUNDLE.wire_kind, ids
        ).items():
            my_installs[bundle_uuid].extend(local)

        items: list[CatalogItem] = []
        for bundle, access in pairs:
            latest = revisions.get(bundle.latest_revision_id) if bundle.latest_revision_id else None
            repo = repos.get(bundle.git_repo_id) if bundle.git_repo_id else None
            desktop = bundle.runs_on == BundleRunsOn.DESKTOP
            items.append(
                CatalogItem(
                    kind="agent",
                    runs_on=bundle.runs_on,
                    uuid=bundle.id,
                    id=bundle.bundle_id,
                    name=(bundle.slug or bundle.display_name) if desktop else bundle.display_name,
                    display_name=bundle.display_name,
                    description=bundle.description,
                    publisher=_person(people, bundle.publisher_user_id),
                    maintainers=maintainers.get(bundle.id, []),
                    access=access,
                    visibility=bundle.visibility,
                    is_listed=bundle.is_listed,
                    latest=_latest(latest, people) if latest else None,
                    install_count=install_counts.get(bundle.id, 0),
                    my_installs=my_installs.get(bundle.id, []),
                    required_credential_specs=listing_credential_specs(
                        latest.required_credential_specs
                    )
                    if latest
                    else [],
                    skills=_skills(latest.skills_summary if latest else None),
                    desktop=_desktop(bundle, repo) if desktop and repo else None,
                )
            )
        return items

    @staticmethod
    def _bundle_revisions(session: Session, bundle: AgentBundle) -> list[CatalogRevision]:
        rows = session.exec(
            select(AgentBundleRevision)
            .where(
                AgentBundleRevision.bundle_id == bundle.id,
                col(AgentBundleRevision.origin).in_(CATALOG_REVISION_ORIGINS),
            )
            .order_by(col(AgentBundleRevision.revision_number).desc())
        ).all()
        people = _people(session, [r.published_by_user_id for r in rows])
        return [
            CatalogRevision(
                revision_number=r.revision_number,
                version=r.version,
                published_at=r.published_at,
                published_by=_person(people, r.published_by_user_id),
                release_notes=r.release_notes,
                origin=r.origin,
                git_commit_sha=r.git_commit_sha,
            )
            for r in rows
        ]

    # ── Projection: skills ─────────────────────────────────────────

    @staticmethod
    def _project_skills(
        session: Session, user: User, pairs: list[tuple[SkillPackage, str]]
    ) -> list[CatalogItem]:
        if not pairs:
            return []
        packages = [p for p, _ in pairs]
        ids = [p.id for p in packages]
        ctx = SkillCatalogService._entry_context(session, packages, user)
        maintainers = _maintainers(session, SKILL, ids)
        people = _people(
            session,
            [p.publisher_user_id for p in packages]
            + [r.published_by_user_id for r in ctx.revisions.values()],
        )
        links = session.exec(
            select(
                AgentPluginLink.agent_id,
                SkillPackageRevision.package_id,
                SkillPackageRevision.revision_number,
            )
            .join(
                SkillPackageRevision,
                SkillPackageRevision.id == AgentPluginLink.skill_package_revision_id,
            )
            .join(Agent, Agent.id == AgentPluginLink.agent_id)
            .where(col(SkillPackageRevision.package_id).in_(ids), Agent.owner_id == user.id)
        ).all()
        my_installs: dict[uuid.UUID, list[CatalogInstall]] = defaultdict(list)
        for agent_id, package_id, number in sorted(links, key=lambda r: str(r[0])):
            my_installs[package_id].append(
                CatalogInstall(where="cloud", install_id=agent_id, revision_number=number)
            )
        for package_id, local in LocalInstallService.mine(
            session, user.id, SKILL.wire_kind, ids
        ).items():
            my_installs[package_id].extend(local)

        items: list[CatalogItem] = []
        for package, access in pairs:
            latest = (
                ctx.revisions.get(package.latest_revision_id)
                if package.latest_revision_id
                else None
            )
            items.append(
                CatalogItem(
                    kind="skill",
                    uuid=package.id,
                    id=package.package_id,
                    name=package.name,
                    display_name=package.display_name,
                    description=package.description,
                    publisher=_person(people, package.publisher_user_id),
                    maintainers=maintainers.get(package.id, []),
                    access=access,
                    visibility=package.visibility,
                    is_listed=package.is_listed,
                    latest=_latest(latest, people) if latest else None,
                    install_count=ctx.install_counts.get(package.id, 0),
                    my_installs=my_installs.get(package.id, []),
                    required_credential_specs=listing_credential_specs(
                        latest.required_credential_specs
                    )
                    if latest
                    else [],
                    skill=CatalogSkillContent(
                        frontmatter=dict(latest.frontmatter or {}),
                        archive_sha256=latest.archive_sha256,
                        size_bytes=latest.size_bytes,
                    )
                    if latest
                    else None,
                )
            )
        return items

    @staticmethod
    def _skill_revisions(session: Session, package: SkillPackage) -> list[CatalogRevision]:
        rows = SkillCatalogService.list_revisions(session, package)
        people = _people(session, [r.published_by_user_id for r in rows])
        return [
            CatalogRevision(
                revision_number=r.revision_number,
                version=r.version,
                published_at=r.published_at,
                published_by=_person(people, r.published_by_user_id),
                release_notes=r.release_notes,
                origin=SKILL_REVISION_ORIGIN,
            )
            for r in rows
        ]


# ── Helpers ────────────────────────────────────────────────────────


def _can_edit(access: str | None) -> bool:
    return CatalogAccessService.at_least(access, CatalogAccess.EDIT)


def _by_id(session: Session, model: Any, ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, Any]:
    wanted = set(ids)
    if not wanted:
        return {}
    return {row.id: row for row in session.exec(select(model).where(col(model.id).in_(wanted))).all()}


def _people(session: Session, ids: Iterable[uuid.UUID | None]) -> dict[uuid.UUID, User]:
    return _by_id(session, User, [i for i in ids if i])


def _person(people: dict[uuid.UUID, User], user_id: uuid.UUID | None) -> CatalogPerson | None:
    person = people.get(user_id) if user_id else None
    if person is None:
        return None
    return CatalogPerson(user_id=person.id, display_name=display_name(person))


def _maintainers(
    session: Session, spec: CatalogKindSpec, item_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list]:
    """Active people holding ``maintain`` directly, per item (one query)."""
    from app.models.catalog.catalog_access import CatalogMaintainer, CatalogPrincipalKind

    grant = spec.grant_model
    rows = session.exec(
        select(spec.fk_col, User)
        .join(User, col(User.id) == col(grant.user_id))
        .where(
            spec.fk_col.in_(item_ids),
            grant.principal_kind == CatalogPrincipalKind.USER,
            grant.access == CatalogAccess.MAINTAIN,
            col(User.is_active).is_(True),
        )
    ).all()
    result: dict[uuid.UUID, list] = defaultdict(list)
    for item_id, person in rows:
        result[item_id].append(
            CatalogMaintainer(user_id=person.id, display_name=display_name(person))
        )
    for people in result.values():
        people.sort(key=lambda m: m.display_name.lower())
    return dict(result)


def _latest(revision: Any, people: dict[uuid.UUID, User]) -> CatalogLatest:
    return CatalogLatest(
        revision_number=revision.revision_number,
        version=revision.version,
        published_at=revision.published_at,
        published_by=_person(people, revision.published_by_user_id),
        release_notes=revision.release_notes,
    )


def _skills(summary: list | None) -> list[CatalogItemSkill]:
    result: list[CatalogItemSkill] = []
    for entry in summary or []:
        if not isinstance(entry, dict) or not entry.get("name"):
            continue
        source = "catalog" if entry.get("source") == "catalog" else "authored"
        package_uuid = None
        if entry.get("package_uuid"):
            try:
                package_uuid = uuid.UUID(str(entry["package_uuid"]))
            except ValueError:
                package_uuid = None
        result.append(
            CatalogItemSkill(
                name=str(entry["name"]),
                description=entry.get("description") if isinstance(entry.get("description"), str) else None,
                source=source,
                package_id=entry.get("package_id") if isinstance(entry.get("package_id"), str) else None,
                package_uuid=package_uuid,
            )
        )
    return result


def _desktop(bundle: AgentBundle, repo: GitRepo) -> CatalogDesktop:
    manifest = repo.manifest_cache if isinstance(repo.manifest_cache, dict) else {}
    runtime = manifest.get("runtime") if isinstance(manifest.get("runtime"), dict) else {}
    engine = runtime.get("engine")
    contract = manifest.get("contract_version")
    return CatalogDesktop(
        repo_id=repo.id,
        clone_url=GitRepoStorage.clone_url(repo.id),
        default_branch=repo.default_branch,
        head_sha=repo.head_sha,
        manifest_id=bundle.manifest_id,
        slug=bundle.slug or "",
        engine=engine if isinstance(engine, str) else None,
        contract_version=contract if isinstance(contract, str) else None,
    )


__all__ = ["CatalogItemService", "CatalogError", "SCOPE_ALL"]
