"""UserGroupService — superuser management of user groups and their members.

Groups are a sharing principal (see :mod:`app.models.users.user_group`). Only
superusers create, rename, delete groups and manage members (D2); the routes
enforce that. :meth:`UserGroupService.search` backs the share pickers and is
open to every authenticated user, so it returns name and size only.

Access through a group is live (D4), so adding a member has no side effects.
Removing one snapshots the group's coverage first and runs
:meth:`~app.services.users.user_group_access.UserGroupAccess.release` after the
commit so families holding materialised state can drop it for the users who
lost every path. Deleting a group lives in
:meth:`~app.services.users.user_group_shares_service.UserGroupSharesService.delete_group`.
"""
from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import NoReturn

from sqlalchemy import func, literal, or_, union_all
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, col, select

from app.models.agent_api.agent_api_access_group_grant import AgentApiAccessGroupGrant
from app.models.artifacts.artifact_grant import ArtifactGroupGrant
from app.models.bundles.bundle_access_grant import BundleAccessGrant
from app.models.catalog.catalog_access import CatalogPerson
from app.models.credentials.ai_credential_group_share import AICredentialGroupShare
from app.models.credentials.credential_group_share import CredentialGroupShare
from app.models.events.security_event import (
    USER_GROUP_CREATED,
    USER_GROUP_MEMBER_REMOVED,
    USER_GROUP_MEMBERS_ADDED,
    USER_GROUP_UPDATED,
)
from app.models.knowledge.knowledge import AIKnowledgeGitRepoGroupShare
from app.models.skills.skill_package_access_grant import SkillPackageAccessGrant
from app.models.users.user import User
from app.models.users.user_group import (
    UserGroup,
    UserGroupCreate,
    UserGroupMember,
    UserGroupMemberPublic,
    UserGroupMembersPublic,
    UserGroupPublic,
    UserGroupSearchResult,
    UserGroupsPublic,
    UserGroupsSearchPublic,
    UserGroupUpdate,
)
from app.services.catalog.catalog_events import add_audit_event
from app.services.users.user_group_access import UserGroupAccess

logger = logging.getLogger(__name__)

SEARCH_LIMIT_MIN = 1
SEARCH_LIMIT_MAX = 25


# ── Domain errors (mapped to HTTP in the routes) ─────────────────────


class UserGroupError(Exception):
    """Base class for user group errors."""


class UserGroupNotFoundError(UserGroupError):
    """404: the group, or the membership, does not exist."""


class UserGroupConflictError(UserGroupError):
    """409: the change collides with existing or concurrent state."""


class UserGroupNameConflictError(UserGroupConflictError):
    """409: another group already has this name (case-insensitive)."""


class UserGroupValidationError(UserGroupError):
    """422: the request names something invalid."""


class UserGroupUnknownUsersError(UserGroupValidationError):
    """422: some user ids are unknown or inactive."""

    def __init__(self, user_ids: list[uuid.UUID]) -> None:
        self.user_ids = user_ids
        super().__init__(
            "Unknown or inactive users: " + ", ".join(str(u) for u in user_ids)
        )


# ── Helpers ──────────────────────────────────────────────────────────


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _clean_name(name: str) -> str:
    cleaned = (name or "").strip()
    if not cleaned:
        raise UserGroupValidationError("Group name must not be empty")
    return cleaned


def _clean_description(description: str | None) -> str | None:
    if description is None:
        return None
    cleaned = description.strip()
    return cleaned or None


def _like_pattern(term: str) -> str:
    """``%term%`` with LIKE wildcards escaped (use with ``escape='\\\\'``)."""
    safe = (
        term.lower()
        .replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )
    return f"%{safe}%"


class UserGroupService:
    """Static entry points; callers own the session."""

    # ── Reads ────────────────────────────────────────────────────────

    @staticmethod
    def get_group(session: Session, group_id: uuid.UUID) -> UserGroup:
        group = session.get(UserGroup, group_id)
        if group is None:
            raise UserGroupNotFoundError("User group not found")
        return group

    @staticmethod
    def lock_group(session: Session, group_id: uuid.UUID) -> UserGroup:
        """The group under a row lock (``FOR UPDATE``), else 404."""
        group = session.exec(
            select(UserGroup).where(UserGroup.id == group_id).with_for_update()
        ).first()
        if group is None:
            raise UserGroupNotFoundError("User group not found")
        return group

    @staticmethod
    def get(session: Session, group_id: uuid.UUID) -> UserGroupPublic:
        group = UserGroupService.get_group(session, group_id)
        return UserGroupService._to_public(session, [group])[0]

    @staticmethod
    def list_groups(
        session: Session, q: str | None = None, skip: int = 0, limit: int = 100
    ) -> UserGroupsPublic:
        statement = select(UserGroup)
        count_statement = select(func.count()).select_from(UserGroup)
        term = (q or "").strip()
        if term:
            condition = func.lower(UserGroup.name).like(_like_pattern(term), escape="\\")
            statement = statement.where(condition)
            count_statement = count_statement.where(condition)
        count = session.exec(count_statement).one()
        groups = list(
            session.exec(
                statement.order_by(func.lower(UserGroup.name), col(UserGroup.id))
                .offset(skip)
                .limit(limit)
            ).all()
        )
        return UserGroupsPublic(
            data=UserGroupService._to_public(session, groups), count=count
        )

    @staticmethod
    def list_members(
        session: Session,
        group_id: uuid.UUID,
        q: str | None = None,
        skip: int = 0,
        limit: int = 100,
    ) -> UserGroupMembersPublic:
        UserGroupService.get_group(session, group_id)
        base = (
            select(UserGroupMember, User)
            .join(User, col(User.id) == col(UserGroupMember.user_id))
            .where(UserGroupMember.group_id == group_id)
        )
        count_base = (
            select(func.count())
            .select_from(UserGroupMember)
            .join(User, col(User.id) == col(UserGroupMember.user_id))
            .where(UserGroupMember.group_id == group_id)
        )
        term = (q or "").strip()
        if term:
            pattern = _like_pattern(term)
            condition = or_(
                func.lower(User.email).like(pattern, escape="\\"),
                func.lower(func.coalesce(User.full_name, "")).like(pattern, escape="\\"),
            )
            base = base.where(condition)
            count_base = count_base.where(condition)
        count = session.exec(count_base).one()
        rows = session.exec(
            base.order_by(func.lower(User.email), col(User.id)).offset(skip).limit(limit)
        ).all()
        return UserGroupMembersPublic(
            data=[UserGroupService._member_public(m, u) for m, u in rows], count=count
        )

    @staticmethod
    def search(
        session: Session, q: str | None = None, limit: int = 10
    ) -> UserGroupsSearchPublic:
        """Share-picker search: case-insensitive substring on name.

        Open to every authenticated user, so the result carries name and
        member count only, never who the members are.
        """
        limit = max(SEARCH_LIMIT_MIN, min(SEARCH_LIMIT_MAX, limit))
        statement = select(UserGroup)
        term = (q or "").strip()
        if term:
            statement = statement.where(
                func.lower(UserGroup.name).like(_like_pattern(term), escape="\\")
            )
        groups = list(
            session.exec(
                statement.order_by(func.lower(UserGroup.name), col(UserGroup.id)).limit(limit)
            ).all()
        )
        member_counts = UserGroupAccess.member_counts(session, [g.id for g in groups])
        return UserGroupsSearchPublic(
            data=[
                UserGroupSearchResult(
                    id=g.id, name=g.name, member_count=member_counts.get(g.id, 0)
                )
                for g in groups
            ]
        )

    # ── Writes ───────────────────────────────────────────────────────

    @staticmethod
    def create(
        session: Session, actor: User, data: UserGroupCreate
    ) -> UserGroupPublic:
        group = UserGroup(
            name=_clean_name(data.name),
            description=_clean_description(data.description),
            created_by_user_id=actor.id,
        )
        session.add(group)
        add_audit_event(
            session,
            actor.id,
            USER_GROUP_CREATED,
            {"group_id": str(group.id), "name": group.name},
        )
        UserGroupService._commit_name_change(session)
        session.refresh(group)
        return UserGroupService._to_public(session, [group])[0]

    @staticmethod
    def update(
        session: Session, actor: User, group_id: uuid.UUID, data: UserGroupUpdate
    ) -> UserGroupPublic:
        group = UserGroupService.get_group(session, group_id)
        changes = data.model_dump(exclude_unset=True)
        before = {"name": group.name, "description": group.description}
        if "name" in changes:
            if changes["name"] is None:
                raise UserGroupValidationError("Group name must not be empty")
            new_name = _clean_name(changes["name"])
        else:
            new_name = group.name
        if "description" in changes:
            new_description = _clean_description(changes["description"])
        else:
            new_description = group.description
        if new_name == group.name and new_description == group.description:
            # Empty or no-op PATCH: nothing changes, nothing is audited.
            return UserGroupService._to_public(session, [group])[0]
        group.name = new_name
        group.description = new_description
        group.updated_at = _utc_now()
        session.add(group)
        add_audit_event(
            session,
            actor.id,
            USER_GROUP_UPDATED,
            {
                "group_id": str(group.id),
                "name": group.name,
                "before": before,
                "after": {"name": group.name, "description": group.description},
            },
        )
        UserGroupService._commit_name_change(session)
        session.refresh(group)
        return UserGroupService._to_public(session, [group])[0]

    @staticmethod
    def add_members(
        session: Session, actor: User, group_id: uuid.UUID, user_ids: list[uuid.UUID]
    ) -> UserGroupMembersPublic:
        """Add active users to the group. Idempotent: existing members are
        skipped. Returns the rows actually added."""
        group = UserGroupService.get_group(session, group_id)
        requested = list(dict.fromkeys(user_ids))
        active_ids = set(
            session.exec(
                select(User.id).where(
                    col(User.id).in_(requested), User.is_active == True  # noqa: E712
                )
            ).all()
        )
        unknown = [uid for uid in requested if uid not in active_ids]
        if unknown:
            raise UserGroupUnknownUsersError(unknown)

        # ON CONFLICT DO NOTHING makes concurrent adds of the same member
        # idempotent; RETURNING yields only the rows this call inserted.
        now = _utc_now()
        try:
            added_ids = list(
                session.execute(
                    pg_insert(UserGroupMember)
                    .values(
                        [
                            {
                                "id": uuid.uuid4(),
                                "group_id": group_id,
                                "user_id": uid,
                                "created_at": now,
                            }
                            for uid in requested
                        ]
                    )
                    .on_conflict_do_nothing(constraint="uq_user_group_member")
                    .returning(UserGroupMember.user_id)
                )
                .scalars()
                .all()
            )
        except IntegrityError as exc:
            # FK violation: the group (or a user) was deleted mid-request.
            UserGroupService._raise_for_vanished(session, group_id, exc)
        if not added_ids:
            session.rollback()
            return UserGroupMembersPublic(data=[], count=0)

        group.updated_at = now
        session.add(group)
        add_audit_event(
            session,
            actor.id,
            USER_GROUP_MEMBERS_ADDED,
            {
                "group_id": str(group.id),
                "name": group.name,
                "user_ids": [str(uid) for uid in added_ids],
            },
        )
        try:
            session.commit()
        except IntegrityError as exc:
            UserGroupService._raise_for_vanished(session, group_id, exc)

        added = set(added_ids)
        rows = session.exec(
            select(UserGroupMember, User)
            .join(User, col(User.id) == col(UserGroupMember.user_id))
            .where(
                UserGroupMember.group_id == group_id,
                col(UserGroupMember.user_id).in_(added),
            )
        ).all()
        by_user = {u.id: (m, u) for m, u in rows}
        data = [
            UserGroupService._member_public(*by_user[uid])
            for uid in requested
            if uid in by_user
        ]
        return UserGroupMembersPublic(data=data, count=len(data))

    @staticmethod
    async def remove_member(
        session: Session, actor: User, group_id: uuid.UUID, user_id: uuid.UUID
    ) -> None:
        # Lock first so a share made concurrently cannot slip between the
        # coverage snapshot and the commit (share_with_group read-locks).
        group = UserGroupService.lock_group(session, group_id)
        member = session.exec(
            select(UserGroupMember).where(
                UserGroupMember.group_id == group_id,
                UserGroupMember.user_id == user_id,
            )
        ).first()
        if member is None:
            raise UserGroupNotFoundError("User is not a member of this group")
        # Snapshot before the delete: release only what this group covered.
        coverage = UserGroupAccess.coverage(session, group_id, {user_id})
        session.delete(member)
        group.updated_at = _utc_now()
        session.add(group)
        add_audit_event(
            session,
            actor.id,
            USER_GROUP_MEMBER_REMOVED,
            {"group_id": str(group.id), "name": group.name, "user_id": str(user_id)},
        )
        session.commit()
        await UserGroupAccess.release(session, coverage)

    @staticmethod
    def _raise_for_vanished(
        session: Session, group_id: uuid.UUID, exc: IntegrityError
    ) -> NoReturn:
        """Map a membership-insert FK violation: 404 when the group is gone,
        else 409 (a user was deleted concurrently)."""
        session.rollback()
        if session.get(UserGroup, group_id) is None:
            raise UserGroupNotFoundError("User group not found") from exc
        raise UserGroupConflictError("Membership changed concurrently; retry") from exc

    # ── Projections ──────────────────────────────────────────────────

    @staticmethod
    def _commit_name_change(session: Session) -> None:
        try:
            session.commit()
        except IntegrityError as exc:
            session.rollback()
            if "uq_user_group_name_lower" in str(exc.orig):
                raise UserGroupNameConflictError(
                    "A group with this name already exists"
                ) from exc
            raise

    @staticmethod
    def _member_public(member: UserGroupMember, user: User) -> UserGroupMemberPublic:
        return UserGroupMemberPublic(
            user_id=user.id,
            email=user.email,
            full_name=user.full_name,
            is_active=user.is_active,
            added_at=member.created_at,
        )

    @staticmethod
    def _share_counts(
        session: Session, group_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, int]:
        """Resources shared with each group, across every family, in one query."""
        if not group_ids:
            return {}
        sources = (
            (BundleAccessGrant, BundleAccessGrant.group_id),
            (SkillPackageAccessGrant, SkillPackageAccessGrant.group_id),
            (CredentialGroupShare, CredentialGroupShare.group_id),
            (AICredentialGroupShare, AICredentialGroupShare.group_id),
            (ArtifactGroupGrant, ArtifactGroupGrant.group_id),
            (AIKnowledgeGitRepoGroupShare, AIKnowledgeGitRepoGroupShare.group_id),
            (AgentApiAccessGroupGrant, AgentApiAccessGroupGrant.group_id),
        )
        union = union_all(
            *(
                select(col(group_col).label("group_id"), literal(1).label("one"))
                .select_from(model)
                .where(col(group_col).in_(group_ids))
                for model, group_col in sources
            )
        ).subquery()
        rows = session.exec(
            select(union.c.group_id, func.count()).group_by(union.c.group_id)
        ).all()
        return {gid: n for gid, n in rows}

    @staticmethod
    def _to_public(session: Session, groups: list[UserGroup]) -> list[UserGroupPublic]:
        ids = [g.id for g in groups]
        member_counts = UserGroupAccess.member_counts(session, ids)
        share_counts = UserGroupService._share_counts(session, ids)
        creator_ids = {g.created_by_user_id for g in groups if g.created_by_user_id}
        creators = (
            {
                u.id: u
                for u in session.exec(
                    select(User).where(col(User.id).in_(creator_ids))
                ).all()
            }
            if creator_ids
            else {}
        )
        result: list[UserGroupPublic] = []
        for g in groups:
            creator = creators.get(g.created_by_user_id) if g.created_by_user_id else None
            result.append(
                UserGroupPublic(
                    id=g.id,
                    name=g.name,
                    description=g.description,
                    member_count=member_counts.get(g.id, 0),
                    share_count=share_counts.get(g.id, 0),
                    created_at=g.created_at,
                    updated_at=g.updated_at,
                    created_by=(
                        CatalogPerson(
                            user_id=creator.id,
                            display_name=creator.full_name or creator.email,
                        )
                        if creator
                        else None
                    ),
                )
            )
        return result
