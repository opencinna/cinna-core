"""
LLM Plugin marketplace and agent plugin management models.

This module defines models for:
- Plugin marketplaces (Git repositories containing plugin catalogs)
- Marketplace plugins (individual plugins within a marketplace)
- Agent plugin links (plugins installed for specific agents)
"""

import uuid
from datetime import datetime
from enum import Enum
from typing import Optional

from sqlalchemy import Column, Index, JSON
from sqlmodel import Field, Relationship, SQLModel


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
    public_discovery: bool = Field(default=False, index=True)
    type: str = Field(default="claude")  # Marketplace type (claude, openai, custom)
    status: MarketplaceStatus = Field(default=MarketplaceStatus.pending)
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
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

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
    type: str = "claude"


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
    type: Optional[str] = None


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
    source_type: PluginSourceType = Field(default=PluginSourceType.local)  # local or url
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
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    # Relationships
    marketplace: Optional[LLMPluginMarketplace] = Relationship(back_populates="plugins")
    agent_links: list["AgentPluginLink"] = Relationship(
        back_populates="plugin",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"}
    )


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
    # Additional fields for discovery
    marketplace_name: Optional[str] = None


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


class AgentPluginLink(AgentPluginLinkBase, table=True):
    """Agent plugin link table - links installed plugins to agents."""

    __tablename__ = "agent_plugin_link"
    __table_args__ = (
        Index("idx_agent_plugin_unique", "agent_id", "plugin_id", unique=True),
        Index("idx_agent_plugin_agent", "agent_id"),
        Index("idx_agent_plugin_plugin", "plugin_id"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    agent_id: uuid.UUID = Field(foreign_key="agent.id", ondelete="CASCADE", index=True)
    plugin_id: uuid.UUID = Field(
        foreign_key="llm_plugin_marketplace_plugin.id", ondelete="CASCADE", index=True
    )
    installed_version: Optional[str] = None  # Version string at installation time
    installed_commit_hash: Optional[str] = None  # Git commit hash for reproducibility
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    # Relationships
    plugin: Optional[LLMPluginMarketplacePlugin] = Relationship(back_populates="agent_links")


class AgentPluginLinkCreate(SQLModel):
    """Schema for creating an agent plugin link."""

    plugin_id: uuid.UUID
    conversation_mode: bool = True
    building_mode: bool = True


class AgentPluginLinkUpdate(SQLModel):
    """Schema for updating an agent plugin link."""

    conversation_mode: Optional[bool] = None
    building_mode: Optional[bool] = None


class AgentPluginLinkPublic(SQLModel):
    """Public schema for agent plugin link."""

    id: uuid.UUID
    agent_id: uuid.UUID
    plugin_id: uuid.UUID
    installed_version: Optional[str]
    installed_commit_hash: Optional[str]
    conversation_mode: bool
    building_mode: bool
    created_at: datetime
    updated_at: datetime


class AgentPluginLinkWithPlugin(AgentPluginLinkPublic):
    """Agent plugin link with plugin details."""

    plugin: Optional[LLMPluginMarketplacePluginPublic] = None


class AgentPluginLinkWithUpdateInfo(AgentPluginLinkPublic):
    """Extended schema including update availability info."""

    has_update: bool = False
    latest_version: Optional[str] = None
    latest_commit_hash: Optional[str] = None
    plugin_name: Optional[str] = None
    plugin_description: Optional[str] = None
    plugin_category: Optional[str] = None
    marketplace_name: Optional[str] = None


class AgentPluginLinksPublic(SQLModel):
    """List response for agent plugin links."""

    data: list[AgentPluginLinkWithUpdateInfo]
    count: int
