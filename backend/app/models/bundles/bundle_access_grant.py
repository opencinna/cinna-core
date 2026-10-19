"""BundleAccessGrant — one principal's access to one agent bundle.

A row names a principal (one ``user``, or every holder of a ``role``) and a
level (``use`` < ``edit`` < ``maintain``; see
:mod:`app.models.catalog.catalog_access`). ``use`` rows count only while the
bundle is ``visibility='users'`` and listed; ``edit`` / ``maintain`` count at
any visibility. The publisher holds ``maintain``.
"""
import uuid
from datetime import datetime, UTC

from sqlmodel import Field, SQLModel
from sqlalchemy import CheckConstraint, UniqueConstraint, Index


class BundleAccessGrant(SQLModel, table=True):
    """Database model for one principal's catalog access to one bundle."""

    __tablename__ = "bundle_access_grant"
    __table_args__ = (
        UniqueConstraint(
            "bundle_id",
            "principal_kind",
            "principal_id",
            name="uq_bundle_grant_principal",
        ),
        CheckConstraint(
            "(principal_kind = 'user') = (user_id IS NOT NULL)",
            name="ck_bundle_grant_user_fk",
        ),
        CheckConstraint(
            "(principal_kind = 'group') = (group_id IS NOT NULL)",
            name="ck_bundle_grant_group_fk",
        ),
        Index("ix_bundle_grant_bundle", "bundle_id"),
        Index("ix_bundle_grant_user", "user_id"),
        Index("ix_bundle_grant_group", "group_id"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    bundle_id: uuid.UUID = Field(
        foreign_key="agent_bundle.id", nullable=False, ondelete="CASCADE"
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
    granted_by_user_id: uuid.UUID | None = Field(
        default=None, foreign_key="user.id", ondelete="SET NULL"
    )
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


# ── Public schemas ──────────────────────────────────────────────────


class BundleAccessGrantPublic(SQLModel):
    """Response schema for a bundle access grant."""
    id: uuid.UUID
    bundle_id: uuid.UUID
    user_id: uuid.UUID
    user_email: str | None = None  # Resolved from User row
    access: str = "use"
    granted_by_user_id: uuid.UUID | None = None
    created_at: datetime


class BundleAccessGrantsPublic(SQLModel):
    data: list[BundleAccessGrantPublic]
    count: int


class BundleAccessGrantCreate(SQLModel):
    """Body of ``POST /bundles/{bundle_id}/grants``."""
    email: str
