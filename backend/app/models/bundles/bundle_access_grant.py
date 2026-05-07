"""BundleAccessGrant — explicit per-user access for ``visibility='users'`` bundles.

When a publisher sets a bundle's visibility to ``"users"``, only users with
a matching ``BundleAccessGrant`` row may see the bundle in the catalog and
install it. ``visibility='public'`` ignores this table; ``visibility='private'``
hides the bundle from everyone except the publisher.
"""
import uuid
from datetime import datetime, UTC

from sqlmodel import Field, SQLModel
from sqlalchemy import UniqueConstraint, Index


class BundleAccessGrant(SQLModel, table=True):
    """Database model for explicit per-user catalog access grants."""

    __tablename__ = "bundle_access_grant"
    __table_args__ = (
        UniqueConstraint(
            "bundle_id",
            "user_id",
            name="uq_bundle_grant_bundle_user",
        ),
        Index("ix_bundle_grant_bundle", "bundle_id"),
        Index("ix_bundle_grant_user", "user_id"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    bundle_id: uuid.UUID = Field(
        foreign_key="agent_bundle.id", nullable=False, ondelete="CASCADE"
    )
    user_id: uuid.UUID = Field(
        foreign_key="user.id", nullable=False, ondelete="CASCADE"
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
    granted_by_user_id: uuid.UUID | None = None
    created_at: datetime


class BundleAccessGrantsPublic(SQLModel):
    data: list[BundleAccessGrantPublic]
    count: int


class BundleAccessGrantCreate(SQLModel):
    """Body of ``POST /bundles/{bundle_id}/grants``."""
    email: str
