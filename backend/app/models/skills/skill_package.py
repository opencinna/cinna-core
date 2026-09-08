"""SkillPackage — one publisher-owned entry in the server skills catalog.

A *skill package* is the catalog identity of a single agent skill
(``skills/<name>/``) that a publisher has shared with the instance. Its content
lives in append-only :class:`~app.models.skills.skill_package_revision.SkillPackageRevision`
snapshots; this row carries the identity, the publisher, the visibility and the
pointer to the latest revision — exactly the split
:class:`~app.models.bundles.agent_bundle.AgentBundle` uses for bundles.

Consumers never install this row directly. Installing creates an
``AgentPluginLink(source=catalog)`` that points at a *revision*, so an install
is pinned to the content it was made against and an upgrade is a link edit
(§5.3). That is why there is no per-package install table here.
"""
import uuid
from datetime import UTC, datetime

from sqlalchemy import Index, Text, UniqueConstraint
from sqlmodel import Column, Field, SQLModel


class SkillPackageVisibility:
    """Catalog visibility levels.

    Deliberately only two: a skill package is either the publisher's own
    (``private``) or offered to everyone on the instance (``public``). The
    bundle catalog's third level (``users``, an explicit allowlist) is listed
    in the plan's out-of-scope section and would need its own grant table.
    """

    PRIVATE = "private"
    PUBLIC = "public"


#: Marketplace dir segment every catalog-installed skill lands under, both on
#: disk (``plugins/cinna-skills/<package name>/``) and in the link's
#: ``snapshot_marketplace_name``. A constant rather than a literal because the
#: install dedupe rule, the manifest builder and the container installer all
#: have to agree on it.
CATALOG_MARKETPLACE_NAME = "cinna-skills"


class SkillPackage(SQLModel, table=True):
    """Database model for a catalog skill package."""

    __tablename__ = "skill_package"
    __table_args__ = (
        UniqueConstraint("package_id", name="uq_skill_package_package_id"),
        UniqueConstraint(
            "publisher_user_id", "name", name="uq_skill_package_publisher_name"
        ),
        Index("ix_skill_package_publisher", "publisher_user_id"),
        Index("ix_skill_package_visibility", "visibility"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)

    # Reverse-DNS identifier, unique on this instance and stable across
    # re-publishes — the same contract ``AgentBundle.bundle_id`` carries, and
    # the string the publish dialog offers to copy.
    package_id: str = Field(max_length=255, nullable=False)

    # The skill's folder name (``skills/<name>/``). Unique per publisher, NOT
    # globally: two people may both publish a ``pdf-report`` skill, and
    # ``package_id`` is what disambiguates them (§9).
    name: str = Field(max_length=64, nullable=False)

    display_name: str = Field(max_length=255, nullable=False)

    # Copied from the latest revision's frontmatter at publish, then editable —
    # a publisher may want a catalog blurb that differs from the description
    # the model reads.
    description: str | None = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )

    # NULL after the publisher's account is deleted: the package becomes
    # ownerless rather than disappearing, so existing installs keep resolving
    # their revision. ``SET NULL`` (not ``RESTRICT``) because a skill package is
    # a leaf artifact — unlike a bundle, nothing else depends on the publisher
    # row staying alive.
    publisher_user_id: uuid.UUID | None = Field(
        default=None, foreign_key="user.id", nullable=True, ondelete="SET NULL"
    )

    # The agent this was published from. Provenance only — deleting the agent
    # must not take the catalog entry with it.
    source_agent_id: uuid.UUID | None = Field(
        default=None, foreign_key="agent.id", nullable=True, ondelete="SET NULL"
    )

    latest_revision_id: uuid.UUID | None = Field(
        default=None,
        foreign_key="skill_package_revision.id",
        nullable=True,
        ondelete="SET NULL",
    )

    visibility: str = Field(
        default=SkillPackageVisibility.PRIVATE, max_length=16
    )

    # A superuser may delist a package (hide it from the catalog) without
    # deleting it; the publisher can still see and manage their own row.
    is_listed: bool = Field(default=True)

    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    # NOTE — there is deliberately no ``install_count`` column. §3.4 sketches
    # one "maintained by the install service", but the install itself is an
    # ``AgentPluginLink`` row that disappears with its agent through
    # ``ON DELETE CASCADE``, which a stored counter cannot observe: it would
    # drift upward forever. The bundle precedent the plan cites for the
    # exclusion rule (``CatalogService._bundle_to_entry``) COUNTs at projection
    # time for the same reason, so the count is derived in
    # ``SkillCatalogService`` instead and this row stays a pure identity.
