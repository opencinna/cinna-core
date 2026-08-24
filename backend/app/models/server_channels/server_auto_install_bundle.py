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
    #: The latest revision's ``router_trigger_prompt`` itself, not just whether
    #: there is one. Widened deliberately for the Auto Routing Tuning card
    #: (plan §6): a Pass-2 ``no_match`` is diagnosed by comparing the message
    #: against the wording that failed to claim it, and a boolean cannot be
    #: compared against anything. ``None`` when the revision carries none —
    #: exactly when ``has_trigger_prompt`` is False, because the builder derives
    #: one from the other rather than computing them separately.
    #:
    #: Not a new exposure class: superuser-only, and it is the bundle
    #: publisher's own routing configuration — the same standard §7 applies to
    #: ``candidates[].trigger_prompt`` on a routing trace.
    #:
    #: **A field here is inert until the builder sets it.** This one is
    #: populated in ``ServerChannelService.list_auto_install_bundles``, which is
    #: the only place that constructs this model, and pinned by a test that
    #: asserts the *text* comes back rather than that the attribute exists —
    #: this codebase has shipped a widened projection whose builder never
    #: learned about the new field, and the model read as though it worked.
    router_trigger_prompt: str | None = None
    added_by: uuid.UUID | None = None
    created_at: datetime
