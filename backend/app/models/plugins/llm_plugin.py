"""
LLM Plugin marketplace and agent plugin management models.

This module defines models for:
- Plugin marketplaces (Git repositories containing plugin catalogs)
- Marketplace plugins (individual plugins within a marketplace)
- Agent plugin links (plugins installed for specific agents)
"""

import uuid
from datetime import datetime, UTC
from enum import Enum
from typing import Literal, Optional

import sqlalchemy as sa
from sqlalchemy import Column, Index, JSON
from sqlmodel import Field, Relationship, SQLModel

#: Repository formats this platform knows how to parse. Each value names a
#: catalog layout and a parser:
#:
#: * ``claude`` — ``.claude-plugin/marketplace.json``;
#: * ``codex``  — ``.agents/plugins/marketplace.json``;
#: * ``skills`` — no catalog file at all, a directory of ``SKILL.md`` folders.
#:
#: The API input schemas type ``type`` as this Literal so an unknown format is
#: a 422 at create/update time rather than a silent fall-back to the Claude
#: parser at sync time. The stored column stays a plain string: rows written
#: before this validation existed must still load, and the sync of such a row
#: is what reports the problem (``status=error``).
MarketplaceType = Literal["claude", "codex", "skills"]


class MarketplaceStatus(str, Enum):
    """Status of a plugin marketplace."""

    pending = "pending"
    connected = "connected"
    error = "error"
    disconnected = "disconnected"


# =============================================================================
# LLM Plugin Marketplace Models
# =============================================================================


class LLMPluginMarketplaceBase(SQLModel):
    """Base model for plugin marketplace."""

    name: str = Field(index=True)
    description: Optional[str] = None
    owner_name: Optional[str] = None
    owner_email: Optional[str] = None
    url: str  # Git repository URL
    git_branch: str = Field(default="main")
    ssh_key_id: Optional[uuid.UUID] = Field(default=None, foreign_key="user_ssh_keys.id")
    public_discovery: bool = Field(default=False)  # Indexed via idx_marketplace_public in __table_args__
    #: Repository format, one of :data:`MarketplaceType`. Stored as a plain
    #: string so a row written before the format was validated still loads.
    type: str = Field(default="claude")
    status: MarketplaceStatus = Field(default=MarketplaceStatus.pending, sa_type=sa.String())
    status_message: Optional[str] = None
    last_sync_at: Optional[datetime] = None
    sync_commit_hash: Optional[str] = None


class LLMPluginMarketplace(LLMPluginMarketplaceBase, table=True):
    """Plugin marketplace table."""

    __tablename__ = "llm_plugin_marketplace"
    __table_args__ = (
        Index("idx_marketplace_name_unique", "name", unique=True),
        Index("idx_marketplace_user", "user_id"),
        Index("idx_marketplace_public", "public_discovery"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: uuid.UUID = Field(foreign_key="user.id", ondelete="CASCADE", index=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    # Relationships
    plugins: list["LLMPluginMarketplacePlugin"] = Relationship(
        back_populates="marketplace",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"}
    )


class LLMPluginMarketplacePublic(SQLModel):
    """Public schema for plugin marketplace."""

    id: uuid.UUID
    name: str
    description: Optional[str]
    owner_name: Optional[str]
    owner_email: Optional[str]
    url: str
    git_branch: str
    ssh_key_id: Optional[uuid.UUID]
    public_discovery: bool
    type: str
    status: MarketplaceStatus
    status_message: Optional[str]
    last_sync_at: Optional[datetime]
    sync_commit_hash: Optional[str]
    user_id: uuid.UUID
    created_at: datetime
    updated_at: datetime
    plugin_count: int = 0


class LLMPluginMarketplaceCreate(SQLModel):
    """Schema for creating a plugin marketplace.

    Only url is required. Other fields like name, description, owner_name, and
    owner_email will be automatically extracted from the repository's
    marketplace.json during sync.
    """

    url: str
    git_branch: str = "main"
    ssh_key_id: Optional[uuid.UUID] = None
    public_discovery: bool = False
    #: Repository format — see :data:`MarketplaceType`. An unknown value is a
    #: 422 here, never a marketplace that syncs with the wrong parser.
    type: MarketplaceType = "claude"


class LLMPluginMarketplaceUpdate(SQLModel):
    """Schema for updating a plugin marketplace."""

    name: Optional[str] = None
    description: Optional[str] = None
    owner_name: Optional[str] = None
    owner_email: Optional[str] = None
    url: Optional[str] = None
    git_branch: Optional[str] = None
    ssh_key_id: Optional[uuid.UUID] = None
    public_discovery: Optional[bool] = None
    type: Optional[MarketplaceType] = None


class LLMPluginMarketplacesPublic(SQLModel):
    """List response for plugin marketplaces."""

    data: list[LLMPluginMarketplacePublic]
    count: int


# =============================================================================
# LLM Plugin Marketplace Plugin Models
# =============================================================================


class PluginSourceType(str, Enum):
    """Type of plugin source."""

    local = "local"  # Plugin files are in the marketplace repo (source_path is relative path)
    url = "url"  # Plugin files are in an external repo (source_url is the git URL)


class PluginSource(str, Enum):
    """Origin of an installed agent plugin link.

    - ``marketplace`` — files fetched by the container via ``git clone`` of the
      plugin's marketplace source at its pinned commit. ``plugin_id`` references
      the resolvable marketplace plugin row.
    - ``bundle`` — files delivered inside the install's bundle revision snapshot
      and seeded into the env workspace. ``plugin_id`` is NULL (no marketplace
      needed); identity/coordinates come from the snapshot fields.
    - ``catalog`` — a skill package from this instance's skills catalog. The
      container fetches a signed archive of the pinned
      ``SkillPackageRevision`` from the backend and extracts it under
      ``plugins/cinna-skills/<package name>/``. ``plugin_id`` is NULL;
      identity comes from the snapshot fields and the revision FK.

    Only ``marketplace`` links resolve a live ``plugin`` row — every other
    source is snapshot-identified, which is why identity resolution branches on
    "is this marketplace?" rather than on each source in turn.
    """

    marketplace = "marketplace"
    bundle = "bundle"
    catalog = "catalog"


class LLMPluginMarketplacePluginBase(SQLModel):
    """Base model for marketplace plugin."""

    name: str = Field(index=True)
    description: Optional[str] = None
    version: Optional[str] = None
    author_name: Optional[str] = None
    author_email: Optional[str] = None
    category: Optional[str] = None
    homepage: Optional[str] = None  # Plugin homepage URL
    source_path: str = Field(default="")  # Path within repository (for local source_type)
    source_type: PluginSourceType = Field(default=PluginSourceType.local, sa_type=sa.String())  # local or url
    source_url: Optional[str] = None  # External git URL (for url source_type)
    source_branch: str = Field(default="main")  # Git branch for external repo
    source_commit_hash: Optional[str] = None  # Commit hash from external repo (for url source_type)
    plugin_type: str = Field(default="claude")  # Type inherited from marketplace
    commit_hash: Optional[str] = None  # Commit hash when plugin config was parsed


class LLMPluginMarketplacePlugin(LLMPluginMarketplacePluginBase, table=True):
    """Marketplace plugin table."""

    __tablename__ = "llm_plugin_marketplace_plugin"
    __table_args__ = (
        Index("idx_plugin_marketplace_name_unique", "marketplace_id", "name", unique=True),
        Index("idx_plugin_marketplace", "marketplace_id"),
        Index("idx_plugin_category", "category"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    marketplace_id: uuid.UUID = Field(
        foreign_key="llm_plugin_marketplace.id", ondelete="CASCADE", index=True
    )
    config: Optional[dict] = Field(default=None, sa_column=Column(JSON))
    # Sync-time verdict: can our containers install this entry at all? Written
    # by the marketplace parser, never by an install. An unsupported entry is
    # still listed — an admin whose marketplace is half-broken needs to see the
    # rows and the reason, not an empty catalog.
    supported: bool = Field(default=True, nullable=False)
    # Stable code, not a sentence: the copy belongs to the client. One of
    # ``npm_source`` | ``app_connector_only`` | ``no_skill_md`` |
    # ``unknown_source`` | ``unsafe_path``.
    unsupported_reason: Optional[str] = Field(default=None, max_length=64)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    # Relationships
    marketplace: Optional[LLMPluginMarketplace] = Relationship(back_populates="plugins")
    # NO delete cascade, and passive deletes on purpose. ``plugin_id`` is
    # declared ``ON DELETE SET NULL`` so that deleting a marketplace plugin
    # orphans (but KEEPS) every agent's install — an admin's action on a
    # catalog must never uninstall somebody else's skill. An ORM-side cascade
    # deletes those rows in Python before the database rule is ever consulted,
    # which is exactly what used to happen here; ``passive_deletes`` leaves the
    # nulling to the constraint that documents it. Consumers of a link
    # therefore have to tolerate ``plugin_id IS NULL`` — the addons projection
    # reports such a row as ``source_unavailable``.
    agent_links: list["AgentPluginLink"] = Relationship(
        back_populates="plugin",
        sa_relationship_kwargs={"passive_deletes": True},
    )


class PluginSkillSummary(SQLModel):
    """The one skill a marketplace entry ships, when it ships exactly one.

    A ``skills``-format marketplace publishes one plugin per skill folder, so
    its rows have a skill to describe before anything is installed. Read off
    the parsed ``config["skill"]`` block rather than recomputed, because the
    repository is not on disk any more by the time anybody asks.
    """

    name: str
    description: str = ""
    has_scripts: bool = False


class LLMPluginMarketplacePluginPublic(SQLModel):
    """Public schema for marketplace plugin."""

    id: uuid.UUID
    marketplace_id: uuid.UUID
    name: str
    description: Optional[str]
    version: Optional[str]
    author_name: Optional[str]
    author_email: Optional[str]
    category: Optional[str]
    homepage: Optional[str]
    source_path: str
    source_type: PluginSourceType
    source_url: Optional[str]
    source_branch: str
    source_commit_hash: Optional[str]
    plugin_type: str
    commit_hash: Optional[str]
    config: Optional[dict]
    created_at: datetime
    updated_at: datetime
    #: Sync-time verdict: can our containers install this entry? An
    #: unsupported entry is listed (with its reason) and refused at install.
    supported: bool = True
    #: Stable code — ``npm_source`` | ``app_connector_only`` | ``no_skill_md``
    #: | ``unknown_source`` | ``unsafe_path``. The sentence belongs to the
    #: client.
    unsupported_reason: Optional[str] = None
    #: Present only for a one-skill entry (``skills`` format).
    skill_summary: Optional[PluginSkillSummary] = None
    # Additional fields for discovery
    marketplace_name: Optional[str] = None
    #: The marketplace's ``owner`` (name, else email). The Add addon list
    #: badges an entry with its author and falls back to this when the
    #: manifest names none — the official marketplace leaves ``author`` blank
    #: on most entries and says "Anthropic" once, at the top.
    marketplace_owner: Optional[str] = None
    #: Where the source lives, browser-openable: ``homepage``, else a
    #: ``url``-sourced entry's repository, else the marketplace repository
    #: (SSH form rewritten to HTTPS for the public hosts, dropped otherwise).
    repository_url: Optional[str] = None


class LLMPluginMarketplacePluginsPublic(SQLModel):
    """List response for marketplace plugins."""

    data: list[LLMPluginMarketplacePluginPublic]
    count: int


# =============================================================================
# Agent Plugin Link Models
# =============================================================================


class AgentPluginLinkBase(SQLModel):
    """Base model for agent plugin link."""

    conversation_mode: bool = Field(default=True)
    building_mode: bool = Field(default=True)
    disabled: bool = Field(default=False)


class AgentPluginLink(AgentPluginLinkBase, table=True):
    """Agent plugin link table - links installed plugins to agents."""

    __tablename__ = "agent_plugin_link"
    __table_args__ = (
        # NOTE: this uniqueness guard only covers marketplace-sourced links
        # (plugin_id NOT NULL). Bundle-sourced links carry plugin_id=NULL and
        # are deduped by (agent_id, snapshot_marketplace_name, snapshot_plugin_name)
        # at the service layer instead. Postgres treats NULLs as distinct in a
        # unique index, so multiple bundle rows per agent do not collide here.
        Index("idx_agent_plugin_unique", "agent_id", "plugin_id", unique=True),
        Index("idx_agent_plugin_agent", "agent_id"),
        Index("idx_agent_plugin_plugin", "plugin_id"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    agent_id: uuid.UUID = Field(foreign_key="agent.id", ondelete="CASCADE", index=True)
    # Nullable: bundle-sourced links have no resolvable marketplace plugin row.
    # ON DELETE SET NULL so deleting a marketplace plugin orphans (but keeps)
    # the link rather than cascading the install away.
    plugin_id: Optional[uuid.UUID] = Field(
        default=None,
        foreign_key="llm_plugin_marketplace_plugin.id",
        ondelete="SET NULL",
        index=True,
        nullable=True,
    )
    # Origin of this link: marketplace (git-fetched), bundle (snapshot-seeded)
    # or catalog (archive fetched from this instance's skills catalog).
    source: PluginSource = Field(default=PluginSource.marketplace, sa_type=sa.String())
    # Catalog-sourced identity (NULL for every other source): the pinned skill
    # package revision. ``SET NULL`` so deleting a package orphans the link
    # (rendered as "source unavailable") instead of silently uninstalling a
    # skill from someone's agent. Also the update signal: an update is
    # available when ``package.latest_revision_id != skill_package_revision_id``.
    skill_package_revision_id: Optional[uuid.UUID] = Field(
        default=None,
        foreign_key="skill_package_revision.id",
        ondelete="SET NULL",
        index=True,
        nullable=True,
    )
    # The install's on-disk directory identity — ``plugins/<marketplace
    # name>/<plugin name>/`` — plus a frozen copy of plugin.json for the UI.
    # Written by EVERY source: bundle and catalog links have nothing else to
    # resolve through, and a marketplace link (which resolves its names live)
    # needs them the moment its plugin row is gone, which ``ON DELETE SET
    # NULL`` makes a supported state. They are what names an orphaned row, what
    # folds its skills onto it, and what lets a returning entry re-adopt it.
    snapshot_marketplace_name: Optional[str] = None
    snapshot_plugin_name: Optional[str] = None
    #: The git URL of the repository this install was actually made from —
    #: **security identity**, not display. A marketplace *name* is globally
    #: unique only at any one instant: the unique index frees it the moment its
    #: holder is deleted, which is exactly the event that orphans links, and a
    #: sync reassigns ``marketplace.name`` from the repository's own
    #: ``marketplace.json``. So a name can move to another row, and re-adopting
    #: an orphaned link on the name alone would let a repository the agent's
    #: owner never chose deliver code into their container. This is the identity
    #: no rename can transfer, and ``_reattach_orphaned_links`` requires it to
    #: match. Written by the marketplace install path only; bundle and catalog
    #: links have no upstream repository of their own. NULL on links installed
    #: before this column existed and already orphaned by then — they can never
    #: be re-adopted, deliberately.
    snapshot_repository_url: Optional[str] = None
    snapshot_config: Optional[dict] = Field(default=None, sa_column=Column(JSON))
    installed_version: Optional[str] = None  # Version string at installation time
    installed_commit_hash: Optional[str] = None  # Git commit hash for reproducibility
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    # Relationships
    plugin: Optional[LLMPluginMarketplacePlugin] = Relationship(back_populates="agent_links")


class AgentPluginLinkCreate(SQLModel):
    """Schema for creating an agent plugin link."""

    plugin_id: uuid.UUID
    conversation_mode: bool = True
    building_mode: bool = True
    disabled: bool = False


class AgentPluginLinkUpdate(SQLModel):
    """Schema for updating an agent plugin link."""

    conversation_mode: Optional[bool] = None
    building_mode: Optional[bool] = None
    disabled: Optional[bool] = None


class AgentPluginLinkPublic(SQLModel):
    """Public schema for agent plugin link."""

    id: uuid.UUID
    agent_id: uuid.UUID
    plugin_id: Optional[uuid.UUID]
    source: PluginSource = PluginSource.marketplace
    snapshot_marketplace_name: Optional[str] = None
    snapshot_plugin_name: Optional[str] = None
    snapshot_config: Optional[dict] = None
    skill_package_revision_id: Optional[uuid.UUID] = None
    installed_version: Optional[str]
    installed_commit_hash: Optional[str]
    conversation_mode: bool
    building_mode: bool
    disabled: bool
    created_at: datetime
    updated_at: datetime


class AgentPluginLinkWithPlugin(AgentPluginLinkPublic):
    """Agent plugin link with plugin details."""

    plugin: Optional[LLMPluginMarketplacePluginPublic] = None


class AgentPluginLinkWithUpdateInfo(AgentPluginLinkPublic):
    """Extended schema including update availability info.

    The display fields are **projections**, not columns, and each source has to
    fill all of them or the row renders nameless:

    * ``marketplace`` — the live ``LLMPluginMarketplacePlugin`` +
      ``LLMPluginMarketplace`` rows; ``has_update`` compares commit hashes.
    * ``bundle`` — the frozen ``snapshot_*`` fields; never has an update of its
      own (bundle apply-update is the only path).
    * ``catalog`` — the ``SkillPackage`` behind the pinned revision: its
      ``display_name``/``name``, its ``description``, category ``"skill"``, and
      ``latest_version`` from the package's latest revision; ``has_update`` is
      ``package.latest_revision_id != link.skill_package_revision_id``.
    """

    has_update: bool = False
    latest_version: Optional[str] = None
    latest_commit_hash: Optional[str] = None
    plugin_name: Optional[str] = None
    plugin_description: Optional[str] = None
    plugin_category: Optional[str] = None
    marketplace_name: Optional[str] = None
    #: Catalog links only — the package this skill came from, so the Plugins
    #: tab can link a row to its catalog page without a second lookup.
    skill_package_id: Optional[uuid.UUID] = None


class AgentPluginLinksPublic(SQLModel):
    """List response for agent plugin links."""

    data: list[AgentPluginLinkWithUpdateInfo]
    count: int


# =============================================================================
# Plugin Sync Response Models
# =============================================================================


class PluginInstallResult(SQLModel):
    """Per-plugin result returned by the container install routine.

    Errors are surfaced as results, not exceptions: a ``failed`` plugin is
    excluded from ``settings.json`` (so the SDK never gets a missing path) and
    reported, never silently listed-but-absent.
    """

    plugin_name: str
    marketplace_name: str
    source: str = "marketplace"  # PluginSource value as a plain string
    status: str  # "installed" | "failed" | "skipped"
    error_message: Optional[str] = None


class EnvironmentSyncStatus(SQLModel):
    """Status of plugin sync for a single environment."""

    environment_id: uuid.UUID
    instance_name: str
    # "unsupported" is not a flavour of "error": the container answered, it
    # simply has no plugin endpoint because its core predates one. The write to
    # the link row succeeded either way — what differs is the remedy, and a
    # client that cannot tell them apart can only guess (and did: it told
    # pre-feature containers to restart, which can never add the route).
    status: str  # "success", "error", "unsupported", "activated_and_synced", "skipped"
    error_message: Optional[str] = None
    was_suspended: bool = False
    # Per-plugin install results from the container install routine for THIS
    # environment. A `failed` entry means the plugin could not be fetched/seeded
    # and was excluded from settings.json — the env sync itself still succeeded.
    plugin_results: list[PluginInstallResult] = []
    # True when any entry in `plugin_results` is `failed` (the env transport
    # succeeded but one or more plugins did not install).
    partial_failures: bool = False


class PluginSyncResponse(SQLModel):
    """Response model for plugin sync operations."""

    success: bool
    message: str
    plugin_link: Optional[AgentPluginLinkPublic] = None
    environments_synced: list[EnvironmentSyncStatus] = []
    total_environments: int = 0
    successful_syncs: int = 0
    failed_syncs: int = 0
    # Aggregated per-plugin install failures across all synced environments.
    # Distinct from `failed_syncs` (which counts environments the transport
    # could not reach): a `plugin_results` failure means the env was reached but
    # a specific plugin did not install. Drives the FE warning surface.
    plugin_results: list[PluginInstallResult] = []
    # True when any environment reported a `failed` plugin result. The overall
    # operation can still be `success=True` (env-level) while having
    # partial_failures=True (plugin-level).
    partial_failures: bool = False
    # Environments that could not take the manifest because their core predates
    # the endpoint. Counted apart from `failed_syncs` so a client can name the
    # only remedy that works (rebuild) instead of the one that never could
    # (restart) — and so it can say the link write itself was fine.
    unsupported_syncs: int = 0
