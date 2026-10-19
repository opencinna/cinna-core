"""SkillPackageAccessGrant — one principal's access to one skill package.

A row names a principal (one ``user``, or every holder of a ``role``) and a
level (``use`` < ``edit`` < ``maintain``; see
:mod:`app.models.catalog.catalog_access`). ``use`` rows count only while the
package is ``visibility='users'`` and listed. The publisher holds ``maintain``.

Field-for-field mirror of
:class:`~app.models.bundles.bundle_access_grant.BundleAccessGrant`, because the
two answer the same question about two different artifacts and a publisher who
has learned one sharing model should not have to learn a second.

The grant confers **catalog visibility only**. Whether a container may download
a revision's archive is keyed on "this environment holds an install of this
revision" (``SkillCatalogService.env_may_download``), never on this table — so
revoking a grant hides the skill from someone's catalog without breaking the
agent they already installed it into.
"""
import uuid
from datetime import UTC, datetime

from sqlalchemy import CheckConstraint, Index, UniqueConstraint
from sqlmodel import Field, SQLModel


class SkillPackageAccessGrant(SQLModel, table=True):
    """Database model for one principal's access to one skill package."""

    __tablename__ = "skill_package_access_grant"
    __table_args__ = (
        UniqueConstraint(
            "package_id",
            "principal_kind",
            "principal_id",
            name="uq_skill_grant_principal",
        ),
        CheckConstraint(
            "(principal_kind = 'user') = (user_id IS NOT NULL)",
            name="ck_skill_grant_user_fk",
        ),
        CheckConstraint(
            "(principal_kind = 'group') = (group_id IS NOT NULL)",
            name="ck_skill_grant_group_fk",
        ),
        Index("ix_skill_grant_package", "package_id"),
        Index("ix_skill_grant_user", "user_id"),
        Index("ix_skill_grant_group", "group_id"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    package_id: uuid.UUID = Field(
        foreign_key="skill_package.id", nullable=False, ondelete="CASCADE"
    )
    principal_kind: str = Field(
        default="user", max_length=16, sa_column_kwargs={"server_default": "user"}
    )
    # User uuid as text, the role value, or the group uuid as text.
    principal_id: str = Field(max_length=64, nullable=False)
    # Set only for ``principal_kind='user'`` so deleting the user drops the grant.
    user_id: uuid.UUID | None = Field(
        default=None, foreign_key="user.id", nullable=True, ondelete="CASCADE"
    )
    # Set only for ``principal_kind='group'`` (``principal_id`` is then the
    # group uuid as text) so deleting the group drops the grant.
    group_id: uuid.UUID | None = Field(
        default=None, foreign_key="user_group.id", nullable=True, ondelete="CASCADE"
    )
    access: str = Field(
        default="use", max_length=16, sa_column_kwargs={"server_default": "use"}
    )
    # SET NULL rather than CASCADE: the granter leaving the instance must not
    # silently revoke access they handed out.
    granted_by_user_id: uuid.UUID | None = Field(
        default=None, foreign_key="user.id", ondelete="SET NULL"
    )
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
