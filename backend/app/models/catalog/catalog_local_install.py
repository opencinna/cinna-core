"""Local installs of catalog items (README U10, §5.1).

Cinna Desktop reports each agent it got and each catalog skill it pinned into
a local agent, so install counts, the "N people use this" delete guard and
"who is behind" work for every kind. Best effort: the desktop's own state is
the source of truth for itself.

One row per (person, device, item, host agent). ``host_manifest_id`` is the
``cinna-agent.json`` id of the local agent a skill was pinned into; agents
leave it NULL, and NULLs compare equal in the unique constraint (PG15+).
``item_uuid`` has no FK because it points at one of two tables
(``agent_bundle`` / ``skill_package``); the item delete removes the rows.
``device_id`` is opaque client data, stored as sent (plan P10).
"""
import uuid
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import Index, UniqueConstraint
from sqlmodel import Field, SQLModel

LocalInstallKind = Literal["agent", "skill"]


class CatalogLocalInstall(SQLModel, table=True):
    __tablename__ = "catalog_local_install"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "device_id",
            "item_kind",
            "item_uuid",
            "host_manifest_id",
            name="uq_catalog_local_install",
            postgresql_nulls_not_distinct=True,
        ),
        Index("ix_catalog_local_install_item", "item_kind", "item_uuid"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: uuid.UUID = Field(foreign_key="user.id", ondelete="CASCADE", nullable=False)
    device_id: str = Field(max_length=128, nullable=False)
    #: ``agent`` or ``skill`` (the wire kind).
    item_kind: str = Field(max_length=16, nullable=False)
    item_uuid: uuid.UUID = Field(nullable=False)
    revision_number: int = Field(nullable=False)
    host_manifest_id: uuid.UUID | None = Field(default=None, nullable=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class LocalInstallIn(SQLModel):
    """Body of ``POST /catalog/items/{uuid}/local-installs``."""

    device_id: str = Field(min_length=1, max_length=128)
    revision_number: int = Field(ge=1)
    #: The local agent a skill was pinned into; required for skills, refused
    #: for agents.
    host_manifest_id: uuid.UUID | None = None
