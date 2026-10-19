"""UserGroup — a superuser-defined set of people that can be a sharing principal.

A group is a name plus a member list. Resources are shared with a group in each
resource family's own share dialog (catalog grants, ``credential_group_share``,
``ai_credential_group_share``, ``artifact_group_grant``,
``ai_knowledge_git_repo_group_share``). Membership is **live**: access is
resolved at check time through
:meth:`~app.services.users.user_group_access.UserGroupAccess.member_group_ids_subquery`,
so no per-member rows are ever written for a group share.

Membership rows carry no ``added_by`` column (D11): a self-added admin would
otherwise be reached by CASCADE (``user_id``) and SET NULL (``added_by``) in one
user delete. The audit event records the actor instead.
"""
import uuid
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Index,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlmodel import Field, SQLModel

from app.models.catalog.catalog_access import CatalogPerson

USER_GROUP_NAME_MAX = 100
USER_GROUP_DESCRIPTION_MAX = 500


def _utc_now() -> datetime:
    return datetime.now(UTC)


class UserGroup(SQLModel, table=True):
    """Database model for one user group."""

    __tablename__ = "user_group"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    name: str = Field(max_length=USER_GROUP_NAME_MAX, nullable=False)
    description: str | None = Field(
        default=None, max_length=USER_GROUP_DESCRIPTION_MAX, nullable=True
    )
    # SET NULL DEFERRABLE INITIALLY DEFERRED: the group outlives its creator,
    # and a creator who is also a member is reached by two cascade paths in one
    # user delete (member CASCADE, creator SET NULL).
    created_by_user_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(
            Uuid,
            ForeignKey(
                "user.id",
                ondelete="SET NULL",
                deferrable=True,
                initially="DEFERRED",
            ),
            nullable=True,
        ),
    )
    created_at: datetime = Field(
        default_factory=_utc_now, sa_type=DateTime(timezone=True), nullable=False
    )
    updated_at: datetime = Field(
        default_factory=_utc_now, sa_type=DateTime(timezone=True), nullable=False
    )


# Names are unique case-insensitively.
Index(
    "uq_user_group_name_lower",
    func.lower(UserGroup.__table__.c.name),
    unique=True,
)


class UserGroupMember(SQLModel, table=True):
    """Database model for one person's membership of one group."""

    __tablename__ = "user_group_member"
    __table_args__ = (
        UniqueConstraint("group_id", "user_id", name="uq_user_group_member"),
        Index("ix_user_group_member_user", "user_id"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    group_id: uuid.UUID = Field(
        foreign_key="user_group.id", nullable=False, ondelete="CASCADE"
    )
    user_id: uuid.UUID = Field(
        foreign_key="user.id", nullable=False, ondelete="CASCADE"
    )
    created_at: datetime = Field(
        default_factory=_utc_now, sa_type=DateTime(timezone=True), nullable=False
    )


# ── API schemas ─────────────────────────────────────────────────────


class UserGroupCreate(SQLModel):
    """Body of ``POST /admin/user-groups``. ``name`` is trimmed before storage."""

    name: str = Field(min_length=1, max_length=USER_GROUP_NAME_MAX)
    description: str | None = Field(default=None, max_length=USER_GROUP_DESCRIPTION_MAX)


class UserGroupUpdate(SQLModel):
    """Body of ``PATCH /admin/user-groups/{id}``; omitted fields are unchanged."""

    name: str | None = Field(default=None, min_length=1, max_length=USER_GROUP_NAME_MAX)
    description: str | None = Field(default=None, max_length=USER_GROUP_DESCRIPTION_MAX)


class UserGroupPublic(SQLModel):
    id: uuid.UUID
    name: str
    description: str | None = None
    member_count: int = 0
    # Resources shared with the group across every family.
    share_count: int = 0
    created_at: datetime
    updated_at: datetime
    # None once the creating admin has been deleted.
    created_by: CatalogPerson | None = None


class UserGroupsPublic(SQLModel):
    data: list[UserGroupPublic]
    count: int


class UserGroupMemberPublic(SQLModel):
    user_id: uuid.UUID
    email: str
    full_name: str | None = None
    is_active: bool
    added_at: datetime


class UserGroupMembersPublic(SQLModel):
    data: list[UserGroupMemberPublic]
    count: int


class UserGroupMembersAdd(SQLModel):
    """Body of ``POST /admin/user-groups/{id}/members``."""

    user_ids: list[uuid.UUID] = Field(min_length=1, max_length=100)


class UserGroupRef(SQLModel):
    """A group as other families' share lists name it."""

    id: uuid.UUID
    name: str


class UserGroupSearchResult(SQLModel):
    """One share-picker hit: name and size only, never the members."""

    id: uuid.UUID
    name: str
    member_count: int = 0


class UserGroupsSearchPublic(SQLModel):
    data: list[UserGroupSearchResult]


# ── Group page: shares, revoke, impact (P5) ─────────────────────────

UserGroupShareResourceType = Literal[
    "agent_bundle",
    "desktop_agent",
    "skill_package",
    "credential",
    "ai_credential",
    "artifact",
    "knowledge_source",
    "agent_api_scopes",
]


class UserGroupSharePublic(SQLModel):
    """One resource shared with a group, as the group page lists it.

    ``level`` is the family's own word: the catalog grant level, ``read`` for
    credentials, ``use`` for AI credentials, the artifact grant level,
    ``query`` for knowledge sources, and the comma-joined scope names (or
    ``"no scopes"``) for producer API scopes (``resource_id`` = producer agent).
    """

    resource_type: UserGroupShareResourceType
    resource_id: uuid.UUID
    resource_name: str
    level: str
    # None when the resource is ownerless or the person has been deleted.
    owner: CatalogPerson | None = None
    granted_by: CatalogPerson | None = None
    granted_at: datetime


class UserGroupSharesPublic(SQLModel):
    data: list[UserGroupSharePublic]
    count: int


class UserGroupImpact(SQLModel):
    """What deleting a group, removing members or revoking one share would cost.

    Read-only preview. Users and counts are those left with **no other path**
    (owner, direct share, another group) once this group's shares stop
    applying to them.
    """

    # Active members among those affected (inactive ones are still released).
    member_count: int = 0
    shares_by_type: dict[str, int] = Field(default_factory=dict)
    # Distinct users losing at least one credential or AI credential.
    users_losing_credential_access: int = 0
    credential_links_removed: int = 0
    ai_environments_cleared: int = 0
    ai_environments_restarted: int = 0
