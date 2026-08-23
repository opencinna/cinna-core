"""ServerAutoInstallBundle — the server-wide auto-install catalog list.

Pass 2 of channel routing: when none of a sender's *installed* agents match
an inbound message, the router classifies it against the bundles on this
list and installs the winner for that sender.

Deliberately server-wide (not per-channel): restricting *which people* may
get a given agent is a bundle-permissions concern, enforced by
``CatalogService.user_can_install`` at routing time. Membership on this list
is NOT an implicit grant — a non-public, ungranted bundle simply never
becomes a candidate for that user.
"""
import uuid
from datetime import UTC, datetime

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel


class ServerAutoInstallBundle(SQLModel, table=True):
    """A bundle eligible for channel auto-install."""

    __tablename__ = "server_auto_install_bundle"
    __table_args__ = (
        UniqueConstraint(
            "bundle_uuid", name="uq_server_auto_install_bundle_bundle_uuid"
        ),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    # Bundle deletion removes the list entry; existing installs are untouched.
    bundle_uuid: uuid.UUID = Field(foreign_key="agent_bundle.id", ondelete="CASCADE")
    added_by: uuid.UUID | None = Field(
        default=None, foreign_key="user.id", ondelete="SET NULL"
    )
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class AutoInstallBundleAdd(SQLModel):
    """Admin request body for adding a bundle to the auto-install list."""

    bundle_uuid: uuid.UUID


class AutoInstallBundlePublic(SQLModel):
    """Joined projection for the admin list.

    ``has_trigger_prompt`` is False when the bundle's latest revision carries
    no ``router_trigger_prompt`` — such a bundle can never win Pass 2, so the
    admin UI flags it.
    """

    bundle_uuid: uuid.UUID
    # Reverse-DNS identifier, e.g. "com.example.helpdesk".
    bundle_id: str
    display_name: str
    visibility: str
    has_trigger_prompt: bool = False
    added_by: uuid.UUID | None = None
    created_at: datetime
