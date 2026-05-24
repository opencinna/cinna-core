"""AppDataVolume — per-user, per-bundle, per-source persistent storage record.

Represents a Docker-managed bind-mount directory keyed by
``(user_id, bundle_id, catalog_type)``. The volume backs
``/app/workspace/app-data`` inside the agent environment and **survives
Install (Agent) deletion** — that is the whole point of the bundle/install
split. When the install is removed, the row is marked ``is_orphaned=true``
and the data on disk is preserved; reinstall of the same ``bundle_id``
from the same source reattaches the row.

The ``catalog_type`` discriminator separates the publisher's working / dev
copy (``catalog_type IS NULL``) from consumer installs of the same bundle
made by the same user (``catalog_type='server'`` today; reserved values
like ``'marketplace'`` or ``'remote:<host>'`` are anticipated). Postgres
treats NULLs as distinct in unique constraints, so a publisher who installs
their own bundle as a consumer ends up with two coexisting volumes — a
NULL-slot publisher volume and a ``'server'``-slot consumer volume — both
under ``(user_id, bundle_id)``.

Rows are created lazily by ``AppDataService.get_or_create_volume`` when an
environment is provisioned for an agent that has a ``bundle_id``. The
``bundle_id`` column is the reverse-DNS string (NOT the bundle UUID) so
the row stays stable across bundle row deletion.
"""
import uuid
from datetime import datetime, UTC

from sqlmodel import Field, SQLModel
from sqlalchemy import UniqueConstraint, Index, text


class AppDataVolume(SQLModel, table=True):
    """Per-user, per-bundle, per-source persistent storage volume."""

    __tablename__ = "app_data_volume"
    __table_args__ = (
        # Per source: the publisher install (NULL) and a consumer install
        # ("server") of the same bundle by the same user are distinct rows
        # — Postgres treats NULLs as distinct in unique constraints.
        UniqueConstraint(
            "user_id",
            "bundle_id",
            "catalog_type",
            name="uq_app_data_user_bundle_catalog",
        ),
        Index("ix_app_data_volume_user_id", "user_id"),
        Index("ix_app_data_volume_bundle_id", "bundle_id"),
        Index(
            "ix_app_data_volume_orphaned",
            "is_orphaned",
            postgresql_where=text("is_orphaned = true"),
        ),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: uuid.UUID = Field(foreign_key="user.id", nullable=False, ondelete="CASCADE")

    # Reverse-DNS bundle identifier (NOT the bundle UUID — that may be missing
    # for unpublished agents or removed when a bundle is deleted).
    bundle_id: str = Field(max_length=255, nullable=False)

    # Source discriminator. Open-ended plain string so we can introduce
    # future values (``"marketplace"``, ``"remote:<host>"``) without an
    # enum migration. Today only two values are written:
    #   - ``NULL``    → publisher install (the publisher's dev / source copy)
    #   - ``"server"`` → consumer install from this server's local catalog
    catalog_type: str | None = Field(default=None, max_length=64, nullable=True)

    # Docker volume / bind-mount metadata
    volume_name: str = Field(max_length=255, nullable=False, unique=True)
    host_path: str = Field(max_length=1024, nullable=False)

    # Lazy size accounting (recomputed on demand by the API)
    size_bytes: int = Field(default=0)
    last_size_check_at: datetime | None = Field(default=None)

    # Lifecycle
    current_install_id: uuid.UUID | None = Field(
        default=None,
        foreign_key="agent.id",
        ondelete="SET NULL",
    )
    is_orphaned: bool = Field(default=False)

    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


# ── Public schemas ─────────────────────────────────────────────────────


class AppDataVolumePublic(SQLModel):
    """Response schema for ``GET /users/me/app-data``."""

    id: uuid.UUID
    bundle_id: str
    catalog_type: str | None
    volume_name: str
    size_bytes: int
    last_size_check_at: datetime | None
    current_install_id: uuid.UUID | None
    current_install_name: str | None = None  # Resolved from Agent.name when joined
    is_orphaned: bool
    created_at: datetime
    updated_at: datetime


class AppDataVolumesPublic(SQLModel):
    data: list[AppDataVolumePublic]
    count: int
