"""SkillPackageAccessGrant — explicit per-user access for ``visibility='users'``.

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

from sqlalchemy import Index, UniqueConstraint
from sqlmodel import Field, SQLModel


class SkillPackageAccessGrant(SQLModel, table=True):
    """Database model for one user's explicit access to one skill package."""

    __tablename__ = "skill_package_access_grant"
    __table_args__ = (
        UniqueConstraint(
            "package_id", "user_id", name="uq_skill_grant_package_user"
        ),
        Index("ix_skill_grant_package", "package_id"),
        Index("ix_skill_grant_user", "user_id"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    package_id: uuid.UUID = Field(
        foreign_key="skill_package.id", nullable=False, ondelete="CASCADE"
    )
    user_id: uuid.UUID = Field(
        foreign_key="user.id", nullable=False, ondelete="CASCADE"
    )
    # SET NULL rather than CASCADE: the granter leaving the instance must not
    # silently revoke access they handed out.
    granted_by_user_id: uuid.UUID | None = Field(
        default=None, foreign_key="user.id", ondelete="SET NULL"
    )
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
