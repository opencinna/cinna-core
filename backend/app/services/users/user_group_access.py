"""UserGroupAccess — the one seam every resource family uses to resolve groups.

Membership is live (D4): no family ever writes per-member rows for a group
share. Instead each family's access query ORs in "a group share whose group the
caller belongs to", using :meth:`UserGroupAccess.member_group_ids_subquery`.

Loss of access is computed, not assumed (D8). Before a member leaves a group
or a group is deleted, the caller snapshots the group's :class:`GroupCoverage`
(its credential / AI credential shares × the affected members). After the
commit, :meth:`UserGroupAccess.release` hands exactly those pairs to the
per-family release hooks, so families that hold **materialised** state
(credential links in agent environments, AI credential columns on environments)
drop it for the users who lost every path, and nothing the group never covered
is touched. Catalog, artifacts and knowledge are resolved at query time and need
no release.
"""
from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass

from sqlalchemy import func
from sqlmodel import Session, col, select
from sqlmodel.sql.expression import SelectOfScalar

from app.models.credentials.ai_credential_group_share import AICredentialGroupShare
from app.models.credentials.credential_group_share import CredentialGroupShare
from app.models.users.user import User
from app.models.users.user_group import UserGroupMember

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GroupCoverage:
    """What one group's shares gave a set of members, snapshotted before a
    membership removal or a group delete. The pairs released afterwards are
    ``credential_ids × user_ids`` and ``ai_credential_ids × user_ids``."""

    group_id: uuid.UUID
    user_ids: frozenset[uuid.UUID]
    credential_ids: frozenset[uuid.UUID]
    ai_credential_ids: frozenset[uuid.UUID]


ReleaseHook = Callable[[Session, GroupCoverage], Awaitable[None]]


async def _release_lost_credentials(session: Session, coverage: GroupCoverage) -> None:
    """Credential release hook: unlink + re-sync for users who lost every path."""
    if not coverage.credential_ids:
        return
    # Lazy: the credential services import this module.
    from app.services.credentials.credential_share_service import (
        CredentialShareService,
    )

    await CredentialShareService.release_lost(
        session, credential_ids=coverage.credential_ids, user_ids=coverage.user_ids
    )


async def _release_lost_ai_credentials(
    session: Session, coverage: GroupCoverage
) -> None:
    """AI credential release hook: NULL env columns + restart running envs (D10)."""
    if not coverage.ai_credential_ids:
        return
    # Lazy: the credential services import this module.
    from app.services.credentials.ai_credentials_service import (
        ai_credentials_service,
    )

    await ai_credentials_service.release_lost(
        session,
        credential_ids=coverage.ai_credential_ids,
        user_ids=coverage.user_ids,
    )


class UserGroupAccess:
    """Static entry points; no instance state."""

    # Order matters only for log readability; each hook is independent.
    _RELEASE_HOOKS: tuple[tuple[str, ReleaseHook], ...] = (
        ("credentials", _release_lost_credentials),
        ("ai_credentials", _release_lost_ai_credentials),
    )

    @staticmethod
    def member_group_ids_subquery(user_id: uuid.UUID) -> SelectOfScalar[uuid.UUID]:
        """``SELECT group_id FROM user_group_member WHERE user_id = :user_id``.

        Use as ``col(X.group_id).in_(UserGroupAccess.member_group_ids_subquery(uid))``.
        """
        return select(UserGroupMember.group_id).where(
            UserGroupMember.user_id == user_id
        )

    @staticmethod
    def active_member_ids(
        session: Session, group_ids: Iterable[uuid.UUID]
    ) -> set[uuid.UUID]:
        """Ids of the **active** users who belong to any of ``group_ids``."""
        ids = set(group_ids)
        if not ids:
            return set()
        statement = (
            select(UserGroupMember.user_id)
            .join(User, col(User.id) == col(UserGroupMember.user_id))
            .where(col(UserGroupMember.group_id).in_(ids))
            .where(User.is_active == True)  # noqa: E712
            .distinct()
        )
        return set(session.exec(statement).all())

    @staticmethod
    def member_counts(
        session: Session, group_ids: Iterable[uuid.UUID]
    ) -> dict[uuid.UUID, int]:
        """**Active** member count per group (search, list and detail)."""
        ids = set(group_ids)
        if not ids:
            return {}
        rows = session.exec(
            select(UserGroupMember.group_id, func.count())
            .join(User, col(User.id) == col(UserGroupMember.user_id))
            .where(col(UserGroupMember.group_id).in_(ids))
            .where(User.is_active == True)  # noqa: E712
            .group_by(UserGroupMember.group_id)
        ).all()
        return {gid: n for gid, n in rows}

    @staticmethod
    def member_ids(session: Session, group_id: uuid.UUID) -> set[uuid.UUID]:
        """Every member of the group, active or not (an inactive one may still
        own agents and environments holding materialised access)."""
        return set(
            session.exec(
                select(UserGroupMember.user_id).where(
                    UserGroupMember.group_id == group_id
                )
            ).all()
        )

    @staticmethod
    def coverage(
        session: Session,
        group_id: uuid.UUID,
        user_ids: Iterable[uuid.UUID] | None = None,
    ) -> GroupCoverage:
        """Snapshot the group's credential / AI credential shares for
        ``user_ids`` (every member when None). Read-only; take it **before**
        the change that removes access."""
        targets = (
            UserGroupAccess.member_ids(session, group_id)
            if user_ids is None
            else {uid for uid in user_ids if uid is not None}
        )
        credential_ids = session.exec(
            select(CredentialGroupShare.credential_id).where(
                CredentialGroupShare.group_id == group_id
            )
        ).all()
        ai_credential_ids = session.exec(
            select(AICredentialGroupShare.ai_credential_id).where(
                AICredentialGroupShare.group_id == group_id
            )
        ).all()
        return GroupCoverage(
            group_id=group_id,
            user_ids=frozenset(targets),
            credential_ids=frozenset(credential_ids),
            ai_credential_ids=frozenset(ai_credential_ids),
        )

    @staticmethod
    async def release(session: Session, coverage: GroupCoverage) -> None:
        """Run every family's release hook for a snapshotted coverage.

        Call **after** the membership / group change has been committed, so
        each hook sees the post-change state when it computes who actually
        lost access. Only the snapshotted pairs are considered. One family's
        failure is logged and does not stop the others.
        """
        if not coverage.user_ids:
            return
        for family, hook in UserGroupAccess._RELEASE_HOOKS:
            try:
                await hook(session, coverage)
            except Exception:
                session.rollback()
                logger.exception(
                    "user group release hook %s failed (group=%s, users=%d)",
                    family,
                    coverage.group_id,
                    len(coverage.user_ids),
                )
