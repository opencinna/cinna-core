"""ArtifactGrant / ArtifactGroupGrant — view access to one artifact for one
person or one user group.

v1 has a single level (``view``); the column is kept so edit-style levels can
come later. The API reuses the catalog principal shapes
(:class:`~app.models.catalog.catalog_access.CatalogPrincipalUser`,
:class:`~app.models.catalog.catalog_access.CatalogPrincipalGroup`) but only
accepts people and groups: a role principal fails validation (422).
"""
import uuid
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import DateTime, Index, UniqueConstraint
from sqlmodel import Field, SQLModel

from app.models.catalog.catalog_access import (
    CatalogPerson,
    CatalogPrincipalGroup,
    CatalogPrincipalUser,
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


class ArtifactGrantLevel:
    VIEW = "view"

    ALL = (VIEW,)


ArtifactGrantLevelLiteral = Literal["view"]


class ArtifactGrant(SQLModel, table=True):
    """Database model for one user's access to one artifact."""

    __tablename__ = "artifact_grant"
    __table_args__ = (
        UniqueConstraint("artifact_id", "user_id", name="uq_artifact_grant_user"),
        Index("ix_artifact_grant_user", "user_id"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    artifact_id: uuid.UUID = Field(
        foreign_key="artifact.id", nullable=False, ondelete="CASCADE"
    )
    user_id: uuid.UUID = Field(
        foreign_key="user.id", nullable=False, ondelete="CASCADE"
    )
    level: str = Field(
        default=ArtifactGrantLevel.VIEW,
        max_length=16,
        sa_column_kwargs={"server_default": ArtifactGrantLevel.VIEW},
    )
    granted_by: uuid.UUID | None = Field(
        default=None, foreign_key="user.id", nullable=True, ondelete="SET NULL"
    )
    created_at: datetime = Field(
        default_factory=_utc_now, sa_type=DateTime(timezone=True), nullable=False
    )


class ArtifactGroupGrant(SQLModel, table=True):
    """Database model for one user group's access to one artifact.

    Separate from :class:`ArtifactGrant` so a call site unaware of groups
    fails closed. Members are resolved live through ``user_group_member``.
    """

    __tablename__ = "artifact_group_grant"
    __table_args__ = (
        UniqueConstraint("artifact_id", "group_id", name="uq_artifact_group_grant"),
        Index("ix_artifact_group_grant_group", "group_id"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    artifact_id: uuid.UUID = Field(
        foreign_key="artifact.id", nullable=False, ondelete="CASCADE"
    )
    group_id: uuid.UUID = Field(
        foreign_key="user_group.id", nullable=False, ondelete="CASCADE"
    )
    level: str = Field(
        default=ArtifactGrantLevel.VIEW,
        max_length=16,
        sa_column_kwargs={"server_default": ArtifactGrantLevel.VIEW},
    )
    # Only the owner manages grants, so CASCADE shares the owner's fate.
    granted_by: uuid.UUID = Field(
        foreign_key="user.id", nullable=False, ondelete="CASCADE"
    )
    created_at: datetime = Field(
        default_factory=_utc_now, sa_type=DateTime(timezone=True), nullable=False
    )


# ── API schemas ─────────────────────────────────────────────────────


class ArtifactGrantIn(SQLModel):
    """One entry of ``PUT /artifacts/{id}/grants`` (full replace)."""

    principal: CatalogPrincipalUser | CatalogPrincipalGroup
    access: ArtifactGrantLevelLiteral = "view"


class ArtifactGrantPublic(SQLModel):
    principal: CatalogPrincipalUser | CatalogPrincipalGroup
    access: ArtifactGrantLevelLiteral = "view"
    # Person's full name or email; a group's name.
    display_name: str
    # The grantee's email (grants are only ever shown to the artifact's owner).
    # Always None for a group.
    email: str | None = None
    # Groups only: how many members the group has right now.
    member_count: int | None = None
    granted_by: CatalogPerson | None = None
    created_at: datetime
