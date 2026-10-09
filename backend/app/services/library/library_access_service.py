"""LibraryAccessService — who may reach a team library repo, and at what level.

Access comes only from :class:`~app.models.library.git_repo.GitRepoGrant` rows.
A grant names a principal — one user (``user``), every user holding a role
(``role``; ``admin`` also matches superusers) or every user (``everyone``) — and
a level (``read`` < ``write`` < ``maintain``). Effective access is the maximum
over the grants matching the caller.

Superusers *manage* every repo (edit grants, delete, admin listing) through
:meth:`LibraryAccessService.can_manage` and may clone any of them (see
``GitHttpService.authorize``), but pushing and ``GET /library`` follow grants
only.
"""
from __future__ import annotations

import uuid
from collections.abc import Iterable

from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, and_, col, select

from app.models.library.git_repo import (
    EVERYONE_PRINCIPAL_ID,
    GitRepo,
    GitRepoGrant,
    LibraryAccess,
    LibraryGrantIn,
    LibraryMaintainer,
    LibraryPrincipalKind,
    LibraryPrincipalRole,
    LibraryPrincipalUser,
)
from app.models.users.user import VALID_USER_ROLES, User, UserRole
from app.services.library.exceptions import (
    LibraryConflictError,
    LibraryValidationError,
)

NO_MAINTAINER_DETAIL = "An agent in the team library needs at least one maintainer"


class LibraryAccessService:
    """Grant evaluation and replacement for library repos. All methods static."""

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
    def _grant_matches_user(user: User):
        """SQL predicate: a grant row whose principal covers ``user``."""
        return or_(
            and_(
                col(GitRepoGrant.principal_kind) == LibraryPrincipalKind.USER,
                col(GitRepoGrant.user_id) == user.id,
            ),
            and_(
                col(GitRepoGrant.principal_kind) == LibraryPrincipalKind.ROLE,
                col(GitRepoGrant.principal_id).in_(
                    sorted(LibraryAccessService.user_roles(user))
                ),
            ),
            col(GitRepoGrant.principal_kind) == LibraryPrincipalKind.EVERYONE,
        )

    @staticmethod
    def effective_access(session: Session, repo: GitRepo, user: User) -> str | None:
        """``read`` / ``write`` / ``maintain`` or ``None`` — max over matching grants."""
        if not user.is_active:
            return None
        levels = session.exec(
            select(GitRepoGrant.access).where(
                GitRepoGrant.repo_id == repo.id,
                LibraryAccessService._grant_matches_user(user),
            )
        ).all()
        return LibraryAccess.max(*levels)

    @staticmethod
    def accessible_repos(
        session: Session, user: User, kind: str | None = None
    ) -> list[tuple[GitRepo, str]]:
        """Every repo ``user`` holds at least ``read`` on, with the max level.

        One query (repo join matching grants); the per-repo maximum is folded in
        Python because levels are ordered strings, not a sortable column.
        """
        if not user.is_active:
            return []
        stmt = (
            select(GitRepo, GitRepoGrant.access)
            .join(GitRepoGrant, col(GitRepoGrant.repo_id) == col(GitRepo.id))
            .where(LibraryAccessService._grant_matches_user(user))
        )
        if kind is not None:
            stmt = stmt.where(GitRepo.kind == kind)
        best: dict[uuid.UUID, tuple[GitRepo, str]] = {}
        for repo, access in session.exec(stmt).all():
            current = best.get(repo.id)
            if current is None or LibraryAccess.rank(access) > LibraryAccess.rank(current[1]):
                best[repo.id] = (repo, access)
        return sorted(best.values(), key=lambda pair: (pair[0].name.lower(), str(pair[0].id)))

    @staticmethod
    def can_manage(repo: GitRepo, user: User, access: str | None) -> bool:
        """Edit grants / delete: a maintainer of ``repo``, or any superuser."""
        del repo  # management is decided by the caller's level and role only
        return access == LibraryAccess.MAINTAIN or bool(user.is_superuser)

    @staticmethod
    def readers(session: Session, repo: GitRepo) -> set[uuid.UUID]:
        """Ids of active users holding at least ``read`` on ``repo``."""
        grants = session.exec(
            select(GitRepoGrant).where(GitRepoGrant.repo_id == repo.id)
        ).all()
        return LibraryAccessService._readers_for(session, grants)

    @staticmethod
    def maintainers(session: Session, repo: GitRepo) -> list[LibraryMaintainer]:
        """Users granted ``maintain`` directly, by display name."""
        rows = session.exec(
            select(User)
            .join(GitRepoGrant, col(GitRepoGrant.user_id) == col(User.id))
            .where(
                GitRepoGrant.repo_id == repo.id,
                GitRepoGrant.principal_kind == LibraryPrincipalKind.USER,
                GitRepoGrant.access == LibraryAccess.MAINTAIN,
                col(User.is_active).is_(True),
            )
        ).all()
        result = [
            LibraryMaintainer(user_id=user.id, display_name=display_name(user))
            for user in rows
        ]
        return sorted(result, key=lambda m: m.display_name.lower())

    # ── Replacement ────────────────────────────────────────────────

    @staticmethod
    def replace_grants(
        session: Session,
        repo: GitRepo,
        grants: list[LibraryGrantIn],
        actor: User,
    ) -> tuple[set[uuid.UUID], set[uuid.UUID]]:
        """Replace every grant of ``repo`` with ``grants`` in one transaction.

        Validates first (role values, users exist and are active, no principal
        twice; :class:`LibraryValidationError`) and refuses a result without a
        ``maintain`` user grant (:class:`LibraryConflictError`, 409).
        Returns ``(before_readers, after_readers)`` so the caller can notify the
        users who just gained access.
        """
        rows = LibraryAccessService.validate_grants(session, repo, grants, actor)

        try:
            # Serialize concurrent replacements of the same repo's grants: the
            # second waits on the row lock instead of colliding on
            # ``uq_git_repo_grant_principal``.
            session.exec(
                select(GitRepo.id).where(GitRepo.id == repo.id).with_for_update()
            ).first()
            before = LibraryAccessService.readers(session, repo)
            existing = session.exec(
                select(GitRepoGrant).where(GitRepoGrant.repo_id == repo.id)
            ).all()
            for row in existing:
                session.delete(row)
            session.flush()
            for row in rows:
                session.add(row)
            session.commit()
        except IntegrityError as exc:
            session.rollback()
            raise LibraryConflictError(
                "Sharing for this agent was changed at the same time; try again"
            ) from exc
        except Exception:
            session.rollback()
            raise

        after = LibraryAccessService._readers_for(session, rows)
        return before, after

    @staticmethod
    def validate_grants(
        session: Session,
        repo: GitRepo,
        grants: list[LibraryGrantIn],
        actor: User,
    ) -> list[GitRepoGrant]:
        """``grants`` as unsaved rows of ``repo``, or a validation error (role
        values, users exist and are active, no principal twice, at least one
        person maintains)."""
        seen: set[tuple[str, str]] = set()
        rows: list[GitRepoGrant] = []
        for grant in grants:
            kind, principal_id, user_id = _principal_key(grant.principal)
            if kind == LibraryPrincipalKind.ROLE and principal_id not in VALID_USER_ROLES:
                raise LibraryValidationError(f"Unknown role '{principal_id}'")
            if kind == LibraryPrincipalKind.USER:
                user = session.get(User, user_id)
                if user is None or not user.is_active:
                    raise LibraryValidationError("That person is not an active user")
            if (kind, principal_id) in seen:
                raise LibraryValidationError("The same person or group is listed twice")
            seen.add((kind, principal_id))
            rows.append(
                GitRepoGrant(
                    repo_id=repo.id,
                    principal_kind=kind,
                    principal_id=principal_id,
                    user_id=user_id,
                    access=grant.access,
                    granted_by_user_id=actor.id,
                )
            )
        has_maintainer = any(
            row.principal_kind == LibraryPrincipalKind.USER
            and row.access == LibraryAccess.MAINTAIN
            for row in rows
        )
        if not has_maintainer:
            raise LibraryConflictError(NO_MAINTAINER_DETAIL)
        return rows

    @staticmethod
    def _readers_for(
        session: Session, grants: Iterable[GitRepoGrant]
    ) -> set[uuid.UUID]:
        """Active users covered by ``grants`` (every grant level includes read)."""
        user_ids: set[uuid.UUID] = set()
        roles: set[str] = set()
        everyone = False
        for grant in grants:
            if LibraryAccess.rank(grant.access) == 0:
                continue
            if grant.principal_kind == LibraryPrincipalKind.EVERYONE:
                everyone = True
            elif grant.principal_kind == LibraryPrincipalKind.ROLE:
                roles.add(grant.principal_id)
            elif grant.principal_kind == LibraryPrincipalKind.USER and grant.user_id:
                user_ids.add(grant.user_id)

        stmt = select(User.id).where(col(User.is_active).is_(True))
        if not everyone:
            predicates = []
            if user_ids:
                predicates.append(col(User.id).in_(user_ids))
            if roles:
                predicates.append(col(User.role).in_(roles))
                if UserRole.ADMIN.value in roles:
                    predicates.append(col(User.is_superuser).is_(True))
            if not predicates:
                return set()
            stmt = stmt.where(or_(*predicates))
        return set(session.exec(stmt).all())


def display_name(user: User) -> str:
    """A person as the library shows them: full name, else email."""
    return user.full_name or user.email


def _principal_key(
    principal: LibraryPrincipalUser | LibraryPrincipalRole | str,
) -> tuple[str, str, uuid.UUID | None]:
    """``(principal_kind, principal_id, user_id)`` for a wire principal."""
    if isinstance(principal, LibraryPrincipalUser):
        return LibraryPrincipalKind.USER, str(principal.user_id), principal.user_id
    if isinstance(principal, LibraryPrincipalRole):
        return LibraryPrincipalKind.ROLE, principal.role, None
    if principal == LibraryPrincipalKind.EVERYONE:
        return LibraryPrincipalKind.EVERYONE, EVERYONE_PRINCIPAL_ID, None
    raise LibraryValidationError("Unknown principal")
