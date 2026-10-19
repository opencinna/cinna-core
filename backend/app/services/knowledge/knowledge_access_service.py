"""
Consumer access resolution for knowledge sources.

Single source of truth for "which knowledge sources may this user query".
Every consumer path (agent knowledge query, CLI agent-scoped search, account
CLI search) goes through :func:`get_accessible_source_ids`, which is built on
:func:`accessible_sources_filter`.

Rule: the source is enabled AND connected AND
    (access_level == public
     OR the user is a superuser
     OR (access_level == shared AND (the user is on the source's share list
                                     OR a group the user belongs to is))),
and the user is active.

For agent-driven queries "the user" is the agent owner.
"""

import logging
import uuid

from sqlalchemy import ColumnElement, and_, false, or_, true
from sqlmodel import Session, select

from app.models.knowledge.knowledge import (
    AIKnowledgeGitRepo,
    AIKnowledgeGitRepoGroupShare,
    AIKnowledgeGitRepoUserShare,
    KnowledgeSourceAccessLevel,
    SourceStatus,
)
from app.models.users.user import User
from app.services.users.user_group_access import UserGroupAccess

logger = logging.getLogger(__name__)


def accessible_sources_filter(user: User) -> ColumnElement[bool]:
    """Return a SQL predicate on ``AIKnowledgeGitRepo`` selecting sources ``user`` may query.

    Inactive users can query nothing.
    """
    if not user.is_active:
        return false()
    if user.is_superuser:
        level_clause: ColumnElement[bool] = true()
    else:
        shared_with_user = or_(
            AIKnowledgeGitRepo.id.in_(
                select(AIKnowledgeGitRepoUserShare.git_repo_id).where(
                    AIKnowledgeGitRepoUserShare.user_id == user.id
                )
            ),
            AIKnowledgeGitRepo.id.in_(
                select(AIKnowledgeGitRepoGroupShare.git_repo_id).where(
                    AIKnowledgeGitRepoGroupShare.group_id.in_(
                        UserGroupAccess.member_group_ids_subquery(user.id)
                    )
                )
            ),
        )
        level_clause = or_(
            AIKnowledgeGitRepo.access_level == KnowledgeSourceAccessLevel.public,
            and_(
                AIKnowledgeGitRepo.access_level == KnowledgeSourceAccessLevel.shared,
                shared_with_user,
            ),
        )

    return and_(
        AIKnowledgeGitRepo.is_enabled == True,  # noqa: E712
        AIKnowledgeGitRepo.status == SourceStatus.connected,
        level_clause,
    )


def get_accessible_source_ids(
    *,
    session: Session,
    user_id: uuid.UUID,
) -> list[uuid.UUID]:
    """Return IDs of knowledge sources the given user may query.

    Unknown or inactive users resolve to no sources.
    """
    user = session.get(User, user_id)
    if user is None or not user.is_active:
        logger.warning(f"Knowledge access resolution for unknown/inactive user {user_id}")
        return []

    source_ids = list(
        session.exec(
            select(AIKnowledgeGitRepo.id).where(accessible_sources_filter(user))
        ).all()
    )
    logger.info(f"Found {len(source_ids)} accessible knowledge sources for user {user_id}")
    return source_ids

