"""CatalogAccessService — who may reach a catalog item, and at what level.

One rule set for every kind (agent bundles, skill packages), made generic over
a :class:`CatalogKindSpec`. An item carries ``visibility`` + ``is_listed``; its
grant rows name a principal (a ``user``, every holder of a ``role`` — the
``admin`` role also matches superusers — or every member of a user ``group``,
resolved live) and a level
(:class:`~app.models.catalog.catalog_access.CatalogAccess`).

Effective access (plan P1):

- an inactive user has none;
- ``g`` = the max over grants matching the user;
- ``edit`` / ``maintain`` count at any visibility and listing state;
- ``use`` counts only while ``visibility='users'`` and listed;
- ``public`` and listed gives everyone ``use``; a superuser also sees a
  delisted public item;
- ``private`` gives nothing beyond edit/maintain grants.

Management through ``/catalog/items`` (grants) is ``maintain`` or superuser
(:meth:`CatalogAccessService.can_manage`). Only people can maintain, and at
least one person always does. Groups, like roles, are capped at ``edit``.
"""
from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, and_, col, select

from app.models.bundles.agent_bundle import AgentBundle
from app.models.bundles.bundle_access_grant import BundleAccessGrant
from app.models.catalog.catalog_access import (
    CatalogAccess,
    CatalogGrant,
    CatalogGrantIn,
    CatalogMaintainer,
    CatalogPerson,
    CatalogPrincipal,
    CatalogPrincipalGroup,
    CatalogPrincipalKind,
    CatalogPrincipalRole,
    CatalogPrincipalUser,
)
from app.models.events.security_event import CATALOG_GRANTS_CHANGED
from app.models.skills.skill_package import SkillPackage
from app.models.skills.skill_package_access_grant import SkillPackageAccessGrant
from app.models.users.user import VALID_USER_ROLES, User, UserRole
from app.models.users.user_group import UserGroup, UserGroupMember
from app.services.catalog.catalog_events import add_audit_event
from app.services.catalog.exceptions import (
    CatalogAccessDeniedError,
    CatalogConflictError,
    CatalogNotFoundError,
    CatalogValidationError,
)
from app.services.users.user_group_access import UserGroupAccess

NO_MAINTAINER_DETAIL = "This item needs at least one maintainer."
PEOPLE_ONLY_MAINTAIN_DETAIL = "Only a person can be a maintainer."
NOT_FOUND_DETAIL = "Catalog item not found"
NOT_MAINTAINER_DETAIL = "Only a maintainer of this item can do that."

# Visibility values; identical strings on ``BundleVisibility`` and
# ``SkillPackageVisibility``.
_USERS = "users"
_PUBLIC = "public"

# Sort order of grants in a listing: people, then groups, then roles.
_PRINCIPAL_ORDER = {
    CatalogPrincipalKind.USER: 0,
    CatalogPrincipalKind.GROUP: 1,
    CatalogPrincipalKind.ROLE: 2,
}


@dataclass(frozen=True)
class CatalogKindSpec:
    """The tables behind one catalog kind."""

    kind: Literal["bundle", "skill"]
    item_model: Any
    grant_model: Any
    # Grant column holding the item id.
    grant_fk: str
    # ``kind`` as clients see it in event meta (``agent`` / ``skill``).
    wire_kind: Literal["agent", "skill"]

    @property
    def fk_col(self) -> Any:
        return getattr(self.grant_model, self.grant_fk)


BUNDLE = CatalogKindSpec(
    kind="bundle",
    item_model=AgentBundle,
    grant_model=BundleAccessGrant,
    grant_fk="bundle_id",
    wire_kind="agent",
)
SKILL = CatalogKindSpec(
    kind="skill",
    item_model=SkillPackage,
    grant_model=SkillPackageAccessGrant,
    grant_fk="package_id",
    wire_kind="skill",
)


class CatalogAccessService:
    """Grant evaluation and replacement for catalog items. All methods static."""

    # ── Evaluation ─────────────────────────────────────────────────

    @staticmethod
    def user_roles(user: User) -> set[str]:
        """Role values a user's grants match: ``user.role``, plus ``admin`` for
        a superuser."""
        roles = {user.role} if user.role else set()
        if user.is_superuser:
            roles.add(UserRole.ADMIN.value)
        return roles

    @staticmethod
    def grant_matches_user(spec: CatalogKindSpec, user: User) -> Any:
        """SQL predicate: a grant row whose principal covers ``user``."""
        grant = spec.grant_model
        return or_(
            and_(
                col(grant.principal_kind) == CatalogPrincipalKind.USER,
                col(grant.user_id) == user.id,
            ),
            and_(
                col(grant.principal_kind) == CatalogPrincipalKind.ROLE,
                col(grant.principal_id).in_(
                    sorted(CatalogAccessService.user_roles(user))
                ),
            ),
            and_(
                col(grant.principal_kind) == CatalogPrincipalKind.GROUP,
                col(grant.group_id).in_(
                    UserGroupAccess.member_group_ids_subquery(user.id)
                ),
            ),
        )

    @staticmethod
    def grant_level(
        session: Session, spec: CatalogKindSpec, item_id: uuid.UUID, user: User
    ) -> str | None:
        """Max level over the grants of ``item_id`` matching ``user`` (raw,
        before visibility is applied)."""
        if not user.is_active:
            return None
        levels = session.exec(
            select(spec.grant_model.access).where(
                spec.fk_col == item_id,
                CatalogAccessService.grant_matches_user(spec, user),
            )
        ).all()
        return CatalogAccess.max(*levels)

    @staticmethod
    def level_from(
        item: Any, grant: str | None, user: User, *, listing: bool = False
    ) -> str | None:
        """Rule P1 over an already-resolved grant level.

        ``listing`` drops the superuser's view of a delisted public item: it is
        reachable by direct lookup, never offered in a list.
        """
        level: str | None = None
        if grant in (CatalogAccess.EDIT, CatalogAccess.MAINTAIN):
            level = grant
        elif grant == CatalogAccess.USE and item.visibility == _USERS and item.is_listed:
            level = CatalogAccess.USE
        if item.visibility == _PUBLIC and (
            item.is_listed or (user.is_superuser and not listing)
        ):
            level = CatalogAccess.max(level, CatalogAccess.USE)
        return level

    @staticmethod
    def effective_access(
        session: Session, spec: CatalogKindSpec, item: Any, user: User
    ) -> str | None:
        """``use`` / ``edit`` / ``maintain`` or ``None`` (rule P1)."""
        if not user.is_active:
            return None
        grant = CatalogAccessService.grant_level(session, spec, item.id, user)
        return CatalogAccessService.level_from(item, grant, user)

    @staticmethod
    def accessible(
        session: Session, spec: CatalogKindSpec, user: User
    ) -> dict[uuid.UUID, str]:
        """Every item ``user`` may list, with their level."""
        return {
            item_id: level
            for item_id, (_, level) in CatalogAccessService.accessible_items(
                session, spec, user
            ).items()
        }

    @staticmethod
    def accessible_items(
        session: Session, spec: CatalogKindSpec, user: User
    ) -> dict[uuid.UUID, tuple[Any, str]]:
        """Every item ``user`` may list, with the loaded row and their level.
        Two queries: the items joined to matching grants, and the public
        listed ones."""
        if not user.is_active:
            return {}
        item_model = spec.item_model
        rows = session.exec(
            select(item_model, spec.grant_model.access)
            .join(spec.grant_model, spec.fk_col == item_model.id)
            .where(CatalogAccessService.grant_matches_user(spec, user))
        ).all()
        grants: dict[uuid.UUID, tuple[Any, str | None]] = {}
        for item, access in rows:
            current = grants.get(item.id)
            grants[item.id] = (item, CatalogAccess.max(current[1] if current else None, access))

        result: dict[uuid.UUID, tuple[Any, str]] = {}
        for item_id, (item, grant) in grants.items():
            level = CatalogAccessService.level_from(item, grant, user, listing=True)
            if level is not None:
                result[item_id] = (item, level)

        public_items = session.exec(
            select(item_model).where(
                item_model.visibility == _PUBLIC,
                item_model.is_listed == True,  # noqa: E712
            )
        ).all()
        for item in public_items:
            current = result.get(item.id)
            level = CatalogAccess.max(current[1] if current else None, CatalogAccess.USE)
            result[item.id] = (item, level or CatalogAccess.USE)
        return result

    @staticmethod
    def can_manage(access: str | None, user: User) -> bool:
        """Grants / visibility / delete through ``/catalog/items``: a
        maintainer, or any superuser (P2)."""
        return access == CatalogAccess.MAINTAIN or bool(user.is_superuser)

    @staticmethod
    def at_least(access: str | None, level: str) -> bool:
        return CatalogAccess.rank(access) >= CatalogAccess.rank(level)

    @staticmethod
    def grant_rows(session: Session, spec: CatalogKindSpec, item_id: uuid.UUID) -> list[Any]:
        return list(
            session.exec(select(spec.grant_model).where(spec.fk_col == item_id)).all()
        )

    @staticmethod
    def readers(session: Session, spec: CatalogKindSpec, item: Any) -> set[uuid.UUID]:
        """Ids of active users reached through ``item``'s grants (role grants
        expanded). Public reach is not included: there is no fan-out to
        everyone (P9)."""
        rows = CatalogAccessService.grant_rows(session, spec, item.id)
        return CatalogAccessService._readers_for(session, item, rows)

    @staticmethod
    def maintainers(
        session: Session, spec: CatalogKindSpec, item: Any
    ) -> list[CatalogMaintainer]:
        """Active users granted ``maintain`` directly, by display name."""
        grant = spec.grant_model
        users = session.exec(
            select(User)
            .join(grant, col(grant.user_id) == col(User.id))
            .where(
                spec.fk_col == item.id,
                grant.principal_kind == CatalogPrincipalKind.USER,
                grant.access == CatalogAccess.MAINTAIN,
                col(User.is_active).is_(True),
            )
        ).all()
        result = [
            CatalogMaintainer(user_id=u.id, display_name=display_name(u)) for u in users
        ]
        return sorted(result, key=lambda m: m.display_name.lower())

    # ── Replacement ────────────────────────────────────────────────

    @staticmethod
    def validate_grants(
        session: Session,
        spec: CatalogKindSpec,
        item: Any,
        grants: list[CatalogGrantIn],
        actor: User,
    ) -> list[Any]:
        """``grants`` as unsaved rows of ``item``. A principal listed twice
        folds to its highest level. Raises 422 for a role or group
        maintainer, an unknown role, an unknown group or an unknown/inactive
        user, and 409 when no person maintains."""
        user_ids = {
            g.principal.user_id
            for g in grants
            if isinstance(g.principal, CatalogPrincipalUser)
        }
        active_ids: set[uuid.UUID] = set()
        if user_ids:
            active_ids = set(
                session.exec(
                    select(User.id).where(
                        col(User.id).in_(user_ids), col(User.is_active).is_(True)
                    )
                ).all()
            )
        group_ids = {
            g.principal.group_id
            for g in grants
            if isinstance(g.principal, CatalogPrincipalGroup)
        }
        known_group_ids: set[uuid.UUID] = set()
        if group_ids:
            known_group_ids = set(
                session.exec(
                    select(UserGroup.id).where(col(UserGroup.id).in_(group_ids))
                ).all()
            )
        folded: dict[tuple[str, str], tuple[uuid.UUID | None, uuid.UUID | None, str]] = {}
        for grant in grants:
            kind, principal_id, user_id, group_id = _principal_key(grant.principal)
            _check_principal_rule(
                kind, principal_id, user_id, group_id, grant.access,
                active_ids, known_group_ids,
            )
            previous = folded.get((kind, principal_id))
            access = CatalogAccess.max(previous[2] if previous else None, grant.access)
            folded[(kind, principal_id)] = (user_id, group_id, access or grant.access)

        rows = [
            spec.grant_model(
                **{spec.grant_fk: item.id},
                principal_kind=kind,
                principal_id=principal_id,
                user_id=user_id,
                group_id=group_id,
                access=access,
                granted_by_user_id=actor.id,
            )
            for (kind, principal_id), (user_id, group_id, access) in folded.items()
        ]
        if not any(
            row.principal_kind == CatalogPrincipalKind.USER
            and row.access == CatalogAccess.MAINTAIN
            for row in rows
        ):
            raise CatalogConflictError(NO_MAINTAINER_DETAIL)
        return rows

    @staticmethod
    def replace_grants(
        session: Session,
        spec: CatalogKindSpec,
        item: Any,
        grants: list[CatalogGrantIn],
        actor: User,
    ) -> tuple[set[uuid.UUID], set[uuid.UUID]]:
        """Full replace of ``item``'s grants. Flushes, never commits.

        Returns ``(before_readers, after_readers)`` so the caller can notify
        the users who just gained access once it has committed.
        """
        rows = CatalogAccessService.validate_grants(session, spec, item, grants, actor)
        CatalogAccessService._lock_item(session, spec, item.id)
        before = CatalogAccessService.readers(session, spec, item)
        existing = CatalogAccessService.grant_rows(session, spec, item.id)
        # A principal kept at the same level keeps who granted it and when.
        previous = {(r.principal_kind, r.principal_id): r for r in existing}
        for row in rows:
            old = previous.get((row.principal_kind, row.principal_id))
            if old is not None and old.access == row.access:
                row.granted_by_user_id = old.granted_by_user_id
                row.created_at = old.created_at
        for row in existing:
            session.delete(row)
        session.flush()
        for row in rows:
            session.add(row)
        session.flush()
        after = CatalogAccessService._readers_for(session, item, rows)
        return before, after

    @staticmethod
    def _lock_item(session: Session, spec: CatalogKindSpec, item_id: uuid.UUID) -> None:
        """Row lock on the item serialising grant changes: a concurrent
        replacement or revoke waits instead of racing past the checks."""
        session.exec(
            select(spec.item_model.id)
            .where(spec.item_model.id == item_id)
            .with_for_update()
        ).first()

    @staticmethod
    def ensure_maintainer(
        session: Session, spec: CatalogKindSpec, item: Any, user: User
    ) -> Any:
        """Upsert ``user/maintain`` on ``item``. Does not commit."""
        grant = spec.grant_model
        row = session.exec(
            select(grant).where(
                spec.fk_col == item.id,
                grant.principal_kind == CatalogPrincipalKind.USER,
                grant.user_id == user.id,
            )
        ).first()
        if row is None:
            row = grant(
                **{spec.grant_fk: item.id},
                principal_kind=CatalogPrincipalKind.USER,
                principal_id=str(user.id),
                user_id=user.id,
                access=CatalogAccess.MAINTAIN,
                granted_by_user_id=user.id,
            )
        else:
            row.access = CatalogAccess.MAINTAIN
        session.add(row)
        return row

    # ── Single-person helpers (old per-kind grant routes) ──────────

    @staticmethod
    def find_user_grant(
        session: Session, spec: CatalogKindSpec, item_id: uuid.UUID, user_id: uuid.UUID
    ) -> Any | None:
        grant = spec.grant_model
        return session.exec(
            select(grant).where(
                spec.fk_col == item_id,
                grant.principal_kind == CatalogPrincipalKind.USER,
                grant.user_id == user_id,
            )
        ).first()

    @staticmethod
    def add_user_use(
        session: Session,
        spec: CatalogKindSpec,
        item: Any,
        target: User,
        actor: User | uuid.UUID,
    ) -> tuple[Any, bool]:
        """Upsert ``user/use`` for ``target``; an existing row (at any level)
        is returned untouched, never downgraded. Returns ``(row, created)``.
        Does not commit."""
        existing = CatalogAccessService.find_user_grant(session, spec, item.id, target.id)
        if existing is not None:
            return existing, False
        actor_id = actor.id if isinstance(actor, User) else actor
        row = spec.grant_model(
            **{spec.grant_fk: item.id},
            principal_kind=CatalogPrincipalKind.USER,
            principal_id=str(target.id),
            user_id=target.id,
            access=CatalogAccess.USE,
            granted_by_user_id=actor_id,
        )
        session.add(row)
        return row, True

    @staticmethod
    def assert_removable(session: Session, spec: CatalogKindSpec, row: Any) -> None:
        """409 when deleting ``row`` would leave its item without a person
        holding ``maintain``."""
        if not (
            row.principal_kind == CatalogPrincipalKind.USER
            and row.access == CatalogAccess.MAINTAIN
        ):
            return
        grant = spec.grant_model
        item_id = getattr(row, spec.grant_fk)
        CatalogAccessService._lock_item(session, spec, item_id)
        # Only active people count. A user deletion cascading away the last
        # maintainer row is accepted: superusers can still manage (P2).
        others = session.exec(
            select(grant.id)
            .join(User, col(User.id) == col(grant.user_id))
            .where(
                spec.fk_col == item_id,
                grant.principal_kind == CatalogPrincipalKind.USER,
                grant.access == CatalogAccess.MAINTAIN,
                grant.id != row.id,
                col(User.is_active).is_(True),
            )
        ).first()
        if others is None:
            raise CatalogConflictError(NO_MAINTAINER_DETAIL)

    # ── /catalog/items/{uuid}/grants ───────────────────────────────

    @staticmethod
    def resolve_item(session: Session, item_uuid: uuid.UUID) -> tuple[CatalogKindSpec, Any]:
        """The item behind a catalog uuid: a bundle, else a skill package
        (README §5 deviation 1), else 404."""
        for spec in (BUNDLE, SKILL):
            item = session.get(spec.item_model, item_uuid)
            if item is not None:
                return spec, item
        raise CatalogNotFoundError(NOT_FOUND_DETAIL)

    @staticmethod
    def get_managed_item(
        session: Session, item_uuid: uuid.UUID, user: User
    ) -> tuple[CatalogKindSpec, Any]:
        """The item when ``user`` may manage it; 404 when they cannot see it,
        403 when they see it without managing it."""
        spec, item = CatalogAccessService.resolve_item(session, item_uuid)
        access = CatalogAccessService.effective_access(session, spec, item, user)
        if CatalogAccessService.can_manage(access, user):
            return spec, item
        if access is None:
            raise CatalogNotFoundError(NOT_FOUND_DETAIL)
        raise CatalogAccessDeniedError(NOT_MAINTAINER_DETAIL)

    @staticmethod
    def list_item_grants(
        session: Session, item_uuid: uuid.UUID, user: User
    ) -> list[CatalogGrant]:
        spec, item = CatalogAccessService.get_managed_item(session, item_uuid, user)
        return CatalogAccessService.grants_public(session, spec, item)

    @staticmethod
    def put_item_grants(
        session: Session,
        item_uuid: uuid.UUID,
        grants: list[CatalogGrantIn],
        actor: User,
    ) -> tuple[list[CatalogGrant], CatalogKindSpec, uuid.UUID, set[uuid.UUID]]:
        """Replace an item's grants, audit, commit. Returns the new grants,
        the kind, the item id and the users who just gained access."""
        spec, item = CatalogAccessService.get_managed_item(session, item_uuid, actor)
        try:
            before, after = CatalogAccessService.replace_grants(
                session, spec, item, grants, actor
            )
            add_audit_event(
                session,
                actor.id,
                CATALOG_GRANTS_CHANGED,
                {
                    "kind": spec.wire_kind,
                    "uuid": str(item.id),
                    "grants": [
                        {"principal": principal_label(g), "access": g.access}
                        for g in grants
                    ],
                },
            )
            session.commit()
        except IntegrityError as exc:
            session.rollback()
            raise CatalogConflictError(
                "Sharing for this item was changed at the same time; try again"
            ) from exc
        except Exception:
            session.rollback()
            raise
        return (
            CatalogAccessService.grants_public(session, spec, item),
            spec,
            item.id,
            after - before,
        )

    # ── Single-group add / remove (Bundle tab permissions card) ────

    @staticmethod
    def find_group_grant(
        session: Session, spec: CatalogKindSpec, item_id: uuid.UUID, group_id: uuid.UUID
    ) -> Any | None:
        grant = spec.grant_model
        return session.exec(
            select(grant).where(
                spec.fk_col == item_id,
                grant.principal_kind == CatalogPrincipalKind.GROUP,
                grant.group_id == group_id,
            )
        ).first()

    @staticmethod
    def add_group_grant(
        session: Session,
        spec: CatalogKindSpec,
        item: Any,
        group_id: uuid.UUID,
        access: str,
        actor: User,
    ) -> tuple[Any, UserGroup, set[uuid.UUID]]:
        """Add one group grant to ``item`` under the same rules, lock and audit
        as a full replace; commits. Returns the row, the group (loaded before
        the commit, so a concurrent group deletion cannot fail the caller) and
        the users who just gained access. 404 for an unknown group, 422 for ``maintain`` (D6),
        409 when the group already holds a grant on ``item``."""
        group = session.get(UserGroup, group_id)
        if group is None:
            raise CatalogNotFoundError("Group not found")
        _check_principal_rule(
            CatalogPrincipalKind.GROUP, str(group_id), None, group_id, access,
            set(), {group_id},
        )
        try:
            CatalogAccessService._lock_item(session, spec, item.id)
            if CatalogAccessService.find_group_grant(session, spec, item.id, group_id):
                raise CatalogConflictError("This group already has access to this item")
            before = CatalogAccessService.readers(session, spec, item)
            row = spec.grant_model(
                **{spec.grant_fk: item.id},
                principal_kind=CatalogPrincipalKind.GROUP,
                principal_id=str(group_id),
                group_id=group_id,
                access=access,
                granted_by_user_id=actor.id,
            )
            session.add(row)
            session.flush()
            after = CatalogAccessService.readers(session, spec, item)
            _audit_grants_changed(session, spec, item, actor)
            # Detach both rows before the commit so their loaded values
            # survive it: no post-commit reload that a concurrent group
            # deletion (cascading the grant) could fail.
            session.expunge(row)
            session.expunge(group)
            session.commit()
        except IntegrityError as exc:
            session.rollback()
            raise CatalogConflictError(
                "Sharing for this item was changed at the same time; try again"
            ) from exc
        except Exception:
            session.rollback()
            raise
        return row, group, after - before

    @staticmethod
    def remove_group_grant(
        session: Session,
        spec: CatalogKindSpec,
        item: Any,
        grant_id: uuid.UUID,
        actor: User,
    ) -> None:
        """Delete one group grant of ``item`` under the item lock, audit,
        commit. 404 when ``grant_id`` is not a group grant of ``item``."""
        try:
            CatalogAccessService._lock_item(session, spec, item.id)
            row = session.get(spec.grant_model, grant_id)
            if (
                row is None
                or getattr(row, spec.grant_fk) != item.id
                or row.principal_kind != CatalogPrincipalKind.GROUP
            ):
                raise CatalogNotFoundError("Group grant not found for this item")
            session.delete(row)
            session.flush()
            _audit_grants_changed(session, spec, item, actor)
            session.commit()
        except Exception:
            session.rollback()
            raise

    @staticmethod
    def grants_public(
        session: Session, spec: CatalogKindSpec, item: Any
    ) -> list[CatalogGrant]:
        """Every grant of ``item`` on the wire (at most three queries),
        highest level first, then people, groups, roles."""
        rows = CatalogAccessService.grant_rows(session, spec, item.id)
        people_ids = {r.user_id for r in rows if r.user_id} | {
            r.granted_by_user_id for r in rows if r.granted_by_user_id
        }
        people: dict[uuid.UUID, User] = {}
        if people_ids:
            people = {
                u.id: u
                for u in session.exec(select(User).where(col(User.id).in_(people_ids))).all()
            }
        groups = _groups_with_member_counts(
            session,
            {
                r.group_id
                for r in rows
                if r.principal_kind == CatalogPrincipalKind.GROUP and r.group_id
            },
        )
        result: list[CatalogGrant] = []
        for row in rows:
            granted_by = people.get(row.granted_by_user_id) if row.granted_by_user_id else None
            member_count: int | None = None
            principal: CatalogPrincipal
            if row.principal_kind == CatalogPrincipalKind.USER and row.user_id:
                person = people.get(row.user_id)
                principal = CatalogPrincipalUser(user_id=row.user_id)
                name = display_name(person) if person else str(row.user_id)
            elif row.principal_kind == CatalogPrincipalKind.GROUP and row.group_id:
                principal = CatalogPrincipalGroup(group_id=row.group_id)
                group_name, member_count = groups.get(row.group_id, (str(row.group_id), 0))
                name = group_name
            elif row.principal_kind == CatalogPrincipalKind.ROLE:
                principal = CatalogPrincipalRole(role=row.principal_id)
                name = row.principal_id
            else:
                # A row the check constraints should make impossible; never
                # guess its principal.
                continue
            result.append(
                CatalogGrant(
                    principal=principal,
                    access=row.access,
                    display_name=name,
                    member_count=member_count,
                    granted_by=CatalogPerson(
                        user_id=granted_by.id, display_name=display_name(granted_by)
                    )
                    if granted_by
                    else None,
                    created_at=row.created_at,
                )
            )
        result.sort(
            key=lambda g: (
                -CatalogAccess.rank(g.access),
                _PRINCIPAL_ORDER[_wire_kind(g.principal)],
                g.display_name.lower(),
            )
        )
        return result

    # ── Internals ──────────────────────────────────────────────────

    @staticmethod
    def _readers_for(
        session: Session, item: Any, grants: Iterable[Any]
    ) -> set[uuid.UUID]:
        """Active users to whom ``grants`` give effective access to ``item``
        (role and group grants expanded to their current holders)."""
        use_counts = item.visibility == _USERS and item.is_listed
        user_ids: set[uuid.UUID] = set()
        roles: set[str] = set()
        group_ids: set[uuid.UUID] = set()
        for grant in grants:
            if grant.access == CatalogAccess.USE and not use_counts:
                continue
            if CatalogAccess.rank(grant.access) == 0:
                continue
            if grant.principal_kind == CatalogPrincipalKind.ROLE:
                roles.add(grant.principal_id)
            elif grant.principal_kind == CatalogPrincipalKind.USER and grant.user_id:
                user_ids.add(grant.user_id)
            elif grant.principal_kind == CatalogPrincipalKind.GROUP and grant.group_id:
                group_ids.add(grant.group_id)

        # Active members only; the role/user query below filters on activity too.
        user_ids |= UserGroupAccess.active_member_ids(session, group_ids)
        predicates = []
        if user_ids:
            predicates.append(col(User.id).in_(user_ids))
        if roles:
            predicates.append(col(User.role).in_(roles))
            if UserRole.ADMIN.value in roles:
                predicates.append(col(User.is_superuser).is_(True))
        if not predicates:
            return set()
        return set(
            session.exec(
                select(User.id).where(col(User.is_active).is_(True), or_(*predicates))
            ).all()
        )


def display_name(user: User) -> str:
    """A person as the catalog shows them: full name, else email."""
    return user.full_name or user.email


def _wire_kind(principal: CatalogPrincipal) -> str:
    """``principal_kind`` of a wire principal."""
    if isinstance(principal, CatalogPrincipalUser):
        return CatalogPrincipalKind.USER
    if isinstance(principal, CatalogPrincipalGroup):
        return CatalogPrincipalKind.GROUP
    return CatalogPrincipalKind.ROLE


def _principal_key(
    principal: CatalogPrincipal,
) -> tuple[str, str, uuid.UUID | None, uuid.UUID | None]:
    """``(principal_kind, principal_id, user_id, group_id)`` for a wire principal."""
    if isinstance(principal, CatalogPrincipalUser):
        return CatalogPrincipalKind.USER, str(principal.user_id), principal.user_id, None
    if isinstance(principal, CatalogPrincipalGroup):
        return CatalogPrincipalKind.GROUP, str(principal.group_id), None, principal.group_id
    return CatalogPrincipalKind.ROLE, principal.role, None, None


def _check_principal_rule(
    kind: str,
    principal_id: str,
    user_id: uuid.UUID | None,
    group_id: uuid.UUID | None,
    access: str,
    active_ids: set[uuid.UUID],
    known_group_ids: set[uuid.UUID],
) -> None:
    """The per-principal rules of a grant (422): roles and groups never
    maintain (D6), a role must be known, a group must exist and a person must
    be an active user."""
    if kind == CatalogPrincipalKind.ROLE:
        if access == CatalogAccess.MAINTAIN:
            raise CatalogValidationError(PEOPLE_ONLY_MAINTAIN_DETAIL)
        if principal_id not in VALID_USER_ROLES:
            raise CatalogValidationError(f"Unknown role '{principal_id}'")
    elif kind == CatalogPrincipalKind.GROUP:
        if group_id not in known_group_ids:
            raise CatalogValidationError("Unknown group")
        if access == CatalogAccess.MAINTAIN:
            raise CatalogValidationError(PEOPLE_ONLY_MAINTAIN_DETAIL)
    elif user_id not in active_ids:
        raise CatalogValidationError("That person is not an active user")


def _audit_grants_changed(
    session: Session, spec: CatalogKindSpec, item: Any, actor: User
) -> None:
    """``CATALOG_GRANTS_CHANGED`` carrying ``item``'s full grant list as it
    stands now — the meta shape of a full replace."""
    rows = CatalogAccessService.grant_rows(session, spec, item.id)
    add_audit_event(
        session,
        actor.id,
        CATALOG_GRANTS_CHANGED,
        {
            "kind": spec.wire_kind,
            "uuid": str(item.id),
            "grants": [
                {"principal": f"{r.principal_kind}:{r.principal_id}", "access": r.access}
                for r in rows
            ],
        },
    )


def principal_label(grant: CatalogGrantIn) -> str:
    """``user:<id>`` / ``group:<id>`` / ``role:<value>`` for audit meta."""
    kind, principal_id, _, _ = _principal_key(grant.principal)
    return f"{kind}:{principal_id}"


def _groups_with_member_counts(
    session: Session, group_ids: set[uuid.UUID]
) -> dict[uuid.UUID, tuple[str, int]]:
    """``{group_id: (name, active_member_count)}`` in one query.

    Only active members count; a group with none still appears with 0.
    """
    if not group_ids:
        return {}
    rows = session.exec(
        select(UserGroup.id, UserGroup.name, func.count(col(User.id)))
        .outerjoin(UserGroupMember, col(UserGroupMember.group_id) == col(UserGroup.id))
        .outerjoin(
            User,
            and_(
                col(User.id) == col(UserGroupMember.user_id),
                User.is_active == True,  # noqa: E712
            ),
        )
        .where(col(UserGroup.id).in_(group_ids))
        .group_by(col(UserGroup.id), col(UserGroup.name))
    ).all()
    return {group_id: (name, count) for group_id, name, count in rows}
