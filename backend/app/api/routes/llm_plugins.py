"""
LLM Plugin marketplace and agent plugin management API routes.

This module provides endpoints for:
- Marketplace management (admin/superuser only)
- Plugin discovery
- Agent plugin installation/management
"""

import uuid
import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from sqlmodel import select

from app.api.deps import CurrentUser, SessionDep
from app.models import (
    Agent,
    Message,
)
from app.models.llm_plugin import (
    LLMPluginMarketplace,
    LLMPluginMarketplaceCreate,
    LLMPluginMarketplaceUpdate,
    LLMPluginMarketplacePublic,
    LLMPluginMarketplacesPublic,
    LLMPluginMarketplacePlugin,
    LLMPluginMarketplacePluginPublic,
    LLMPluginMarketplacePluginsPublic,
    AgentPluginLink,
    AgentPluginLinkCreate,
    AgentPluginLinkUpdate,
    AgentPluginLinkPublic,
    AgentPluginLinkWithUpdateInfo,
    AgentPluginLinksPublic,
)
from app.services.llm_plugin_service import LLMPluginService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/llm-plugins", tags=["llm-plugins"])


# =============================================================================
# Marketplace Routes (Admin only)
# =============================================================================


@router.post("/marketplaces", response_model=LLMPluginMarketplacePublic)
def create_marketplace(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    data: LLMPluginMarketplaceCreate,
) -> Any:
    """
    Create a new plugin marketplace.

    Only superusers can create marketplaces.
    """
    if not current_user.is_superuser:
        raise HTTPException(status_code=403, detail="Not enough permissions")

    try:
        marketplace = LLMPluginService.create_marketplace(
            session=session,
            data=data,
            user_id=current_user.id,
        )
        return LLMPluginService.get_marketplace_public(session, marketplace)
    except Exception as e:
        logger.error(f"Failed to create marketplace: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/marketplaces", response_model=LLMPluginMarketplacesPublic)
def list_marketplaces(
    session: SessionDep,
    current_user: CurrentUser,
    include_public: bool = True,
) -> Any:
    """
    List marketplaces accessible to the current user.

    - Returns user's own marketplaces
    - If include_public=True (default), also returns public marketplaces
    """
    marketplaces = LLMPluginService.list_marketplaces(
        session=session,
        user_id=current_user.id,
        include_public=include_public,
    )
    public_marketplaces = [
        LLMPluginService.get_marketplace_public(session, m)
        for m in marketplaces
    ]
    return LLMPluginMarketplacesPublic(data=public_marketplaces, count=len(public_marketplaces))


@router.get("/marketplaces/{marketplace_id}", response_model=LLMPluginMarketplacePublic)
def get_marketplace(
    session: SessionDep,
    current_user: CurrentUser,
    marketplace_id: uuid.UUID,
) -> Any:
    """
    Get marketplace details by ID.

    Users can only access:
    - Their own marketplaces
    - Public marketplaces
    """
    marketplace = LLMPluginService.get_marketplace(session, marketplace_id)
    if not marketplace:
        raise HTTPException(status_code=404, detail="Marketplace not found")

    # Check access: own marketplace or public
    if marketplace.user_id != current_user.id and not marketplace.public_discovery:
        if not current_user.is_superuser:
            raise HTTPException(status_code=403, detail="Not enough permissions")

    return LLMPluginService.get_marketplace_public(session, marketplace)


@router.put("/marketplaces/{marketplace_id}", response_model=LLMPluginMarketplacePublic)
def update_marketplace(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    marketplace_id: uuid.UUID,
    data: LLMPluginMarketplaceUpdate,
) -> Any:
    """
    Update a marketplace.

    Only the marketplace owner or superuser can update.
    """
    marketplace = LLMPluginService.get_marketplace(session, marketplace_id)
    if not marketplace:
        raise HTTPException(status_code=404, detail="Marketplace not found")

    # Check ownership
    if marketplace.user_id != current_user.id and not current_user.is_superuser:
        raise HTTPException(status_code=403, detail="Not enough permissions")

    updated = LLMPluginService.update_marketplace(
        session=session,
        marketplace_id=marketplace_id,
        data=data,
        user_id=current_user.id if not current_user.is_superuser else marketplace.user_id,
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Marketplace not found")

    return LLMPluginService.get_marketplace_public(session, updated)


@router.delete("/marketplaces/{marketplace_id}")
def delete_marketplace(
    session: SessionDep,
    current_user: CurrentUser,
    marketplace_id: uuid.UUID,
) -> Message:
    """
    Delete a marketplace and all its plugins.

    Only the marketplace owner or superuser can delete.
    """
    marketplace = LLMPluginService.get_marketplace(session, marketplace_id)
    if not marketplace:
        raise HTTPException(status_code=404, detail="Marketplace not found")

    # Check ownership
    if marketplace.user_id != current_user.id and not current_user.is_superuser:
        raise HTTPException(status_code=403, detail="Not enough permissions")

    deleted = LLMPluginService.delete_marketplace(
        session=session,
        marketplace_id=marketplace_id,
        user_id=current_user.id if not current_user.is_superuser else marketplace.user_id,
    )
    if not deleted:
        raise HTTPException(status_code=404, detail="Marketplace not found")

    return Message(message="Marketplace deleted successfully")


@router.post("/marketplaces/{marketplace_id}/sync", response_model=LLMPluginMarketplacePublic)
def sync_marketplace(
    session: SessionDep,
    current_user: CurrentUser,
    marketplace_id: uuid.UUID,
) -> Any:
    """
    Trigger marketplace re-sync (clone/pull and parse plugins).

    Only the marketplace owner or superuser can sync.
    """
    marketplace = LLMPluginService.get_marketplace(session, marketplace_id)
    if not marketplace:
        raise HTTPException(status_code=404, detail="Marketplace not found")

    # Check ownership
    if marketplace.user_id != current_user.id and not current_user.is_superuser:
        raise HTTPException(status_code=403, detail="Not enough permissions")

    try:
        synced = LLMPluginService.sync_marketplace(
            session=session,
            marketplace_id=marketplace_id,
            user_id=marketplace.user_id,  # Use marketplace owner for SSH key access
        )
        return LLMPluginService.get_marketplace_public(session, synced)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to sync marketplace {marketplace_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Sync failed: {str(e)}")


# =============================================================================
# Plugin Discovery Routes
# =============================================================================


@router.get("/discover", response_model=LLMPluginMarketplacePluginsPublic)
def discover_plugins(
    session: SessionDep,
    current_user: CurrentUser,
    search: str | None = None,
    category: str | None = None,
) -> Any:
    """
    Discover available plugins.

    Returns plugins from:
    - User's private marketplaces
    - Public marketplaces

    Optional filters:
    - search: Search in name/description
    - category: Filter by category
    """
    plugins = LLMPluginService.discover_plugins(
        session=session,
        user_id=current_user.id,
        search=search,
        category=category,
    )
    return LLMPluginMarketplacePluginsPublic(data=plugins, count=len(plugins))


@router.get("/plugins/{plugin_id}", response_model=LLMPluginMarketplacePluginPublic)
def get_plugin(
    session: SessionDep,
    current_user: CurrentUser,
    plugin_id: uuid.UUID,
) -> Any:
    """
    Get plugin details by ID.

    Users can only access plugins from accessible marketplaces.
    """
    plugin = LLMPluginService.get_plugin(session, plugin_id)
    if not plugin:
        raise HTTPException(status_code=404, detail="Plugin not found")

    # Check marketplace access
    marketplace = plugin.marketplace
    if not marketplace:
        raise HTTPException(status_code=404, detail="Plugin marketplace not found")

    if marketplace.user_id != current_user.id and not marketplace.public_discovery:
        if not current_user.is_superuser:
            raise HTTPException(status_code=403, detail="Not enough permissions")

    return LLMPluginMarketplacePluginPublic(
        id=plugin.id,
        marketplace_id=plugin.marketplace_id,
        name=plugin.name,
        description=plugin.description,
        version=plugin.version,
        author_name=plugin.author_name,
        author_email=plugin.author_email,
        category=plugin.category,
        homepage=plugin.homepage,
        source_path=plugin.source_path,
        source_type=plugin.source_type,
        source_url=plugin.source_url,
        source_branch=plugin.source_branch,
        source_commit_hash=plugin.source_commit_hash,
        plugin_type=plugin.plugin_type,
        commit_hash=plugin.commit_hash,
        config=plugin.config,
        created_at=plugin.created_at,
        updated_at=plugin.updated_at,
        marketplace_name=marketplace.name,
    )


# =============================================================================
# Agent Plugin Routes
# =============================================================================


@router.get("/agents/{agent_id}/plugins", response_model=AgentPluginLinksPublic)
def list_agent_plugins(
    session: SessionDep,
    current_user: CurrentUser,
    agent_id: uuid.UUID,
) -> Any:
    """
    List installed plugins for an agent.

    Returns plugins with update availability info:
    - has_update: True if newer version available
    - latest_version: Current version in marketplace
    """
    # Verify agent access
    agent = session.get(Agent, agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if agent.owner_id != current_user.id and not current_user.is_superuser:
        raise HTTPException(status_code=403, detail="Not enough permissions")

    plugins = LLMPluginService.get_agent_plugins(session, agent_id)
    return AgentPluginLinksPublic(data=plugins, count=len(plugins))


@router.post("/agents/{agent_id}/plugins", response_model=AgentPluginLinkPublic)
async def install_agent_plugin(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    agent_id: uuid.UUID,
    data: AgentPluginLinkCreate,
) -> Any:
    """
    Install a plugin for an agent.

    The plugin will be:
    - Locked to current version (installed_version, installed_commit_hash)
    - Enabled for conversation/building mode as specified
    - Synced to running environments
    """
    # Verify agent access
    agent = session.get(Agent, agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if agent.owner_id != current_user.id and not current_user.is_superuser:
        raise HTTPException(status_code=403, detail="Not enough permissions")

    # Verify plugin access
    plugin = LLMPluginService.get_plugin(session, data.plugin_id)
    if not plugin:
        raise HTTPException(status_code=404, detail="Plugin not found")

    marketplace = plugin.marketplace
    if marketplace:
        if marketplace.user_id != current_user.id and not marketplace.public_discovery:
            if not current_user.is_superuser:
                raise HTTPException(status_code=403, detail="Not enough permissions to access this plugin")

    try:
        link = LLMPluginService.install_plugin_for_agent(
            session=session,
            agent_id=agent_id,
            data=data,
        )

        # Sync to running environments
        await LLMPluginService.sync_plugins_to_agent_environments(
            session=session,
            agent_id=agent_id,
            user_id=current_user.id,
        )

        return AgentPluginLinkPublic(
            id=link.id,
            agent_id=link.agent_id,
            plugin_id=link.plugin_id,
            installed_version=link.installed_version,
            installed_commit_hash=link.installed_commit_hash,
            conversation_mode=link.conversation_mode,
            building_mode=link.building_mode,
            created_at=link.created_at,
            updated_at=link.updated_at,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/agents/{agent_id}/plugins/{link_id}")
async def uninstall_agent_plugin(
    session: SessionDep,
    current_user: CurrentUser,
    agent_id: uuid.UUID,
    link_id: uuid.UUID,
) -> Message:
    """
    Uninstall a plugin from an agent.

    The plugin will be removed and environments will be synced.
    """
    # Verify agent access
    agent = session.get(Agent, agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if agent.owner_id != current_user.id and not current_user.is_superuser:
        raise HTTPException(status_code=403, detail="Not enough permissions")

    deleted = LLMPluginService.uninstall_plugin_from_agent(
        session=session,
        agent_id=agent_id,
        link_id=link_id,
    )
    if not deleted:
        raise HTTPException(status_code=404, detail="Plugin link not found")

    # Sync to running environments
    await LLMPluginService.sync_plugins_to_agent_environments(
        session=session,
        agent_id=agent_id,
        user_id=current_user.id,
    )

    return Message(message="Plugin uninstalled successfully")


@router.put("/agents/{agent_id}/plugins/{link_id}", response_model=AgentPluginLinkPublic)
async def update_agent_plugin(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    agent_id: uuid.UUID,
    link_id: uuid.UUID,
    data: AgentPluginLinkUpdate,
) -> Any:
    """
    Update plugin mode flags (conversation_mode, building_mode).

    Changes will be synced to running environments.
    """
    # Verify agent access
    agent = session.get(Agent, agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if agent.owner_id != current_user.id and not current_user.is_superuser:
        raise HTTPException(status_code=403, detail="Not enough permissions")

    link = LLMPluginService.update_plugin_modes(
        session=session,
        agent_id=agent_id,
        link_id=link_id,
        data=data,
    )
    if not link:
        raise HTTPException(status_code=404, detail="Plugin link not found")

    # Sync to running environments
    await LLMPluginService.sync_plugins_to_agent_environments(
        session=session,
        agent_id=agent_id,
        user_id=current_user.id,
    )

    return AgentPluginLinkPublic(
        id=link.id,
        agent_id=link.agent_id,
        plugin_id=link.plugin_id,
        installed_version=link.installed_version,
        installed_commit_hash=link.installed_commit_hash,
        conversation_mode=link.conversation_mode,
        building_mode=link.building_mode,
        created_at=link.created_at,
        updated_at=link.updated_at,
    )


@router.post("/agents/{agent_id}/plugins/{link_id}/upgrade", response_model=AgentPluginLinkWithUpdateInfo)
async def upgrade_agent_plugin(
    session: SessionDep,
    current_user: CurrentUser,
    agent_id: uuid.UUID,
    link_id: uuid.UUID,
) -> Any:
    """
    Upgrade a plugin to the latest version.

    Updates installed_version and installed_commit_hash to match
    the current values in the marketplace.
    """
    # Verify agent access
    agent = session.get(Agent, agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if agent.owner_id != current_user.id and not current_user.is_superuser:
        raise HTTPException(status_code=403, detail="Not enough permissions")

    link = LLMPluginService.upgrade_agent_plugin(
        session=session,
        agent_id=agent_id,
        link_id=link_id,
    )
    if not link:
        raise HTTPException(status_code=404, detail="Plugin link not found")

    # Sync to running environments
    await LLMPluginService.sync_plugins_to_agent_environments(
        session=session,
        agent_id=agent_id,
        user_id=current_user.id,
    )

    # Get full info for response
    plugins = LLMPluginService.get_agent_plugins(session, agent_id)
    upgraded_plugin = next((p for p in plugins if p.id == link_id), None)

    if upgraded_plugin:
        return upgraded_plugin

    # Fallback if not found in list
    return AgentPluginLinkWithUpdateInfo(
        id=link.id,
        agent_id=link.agent_id,
        plugin_id=link.plugin_id,
        installed_version=link.installed_version,
        installed_commit_hash=link.installed_commit_hash,
        conversation_mode=link.conversation_mode,
        building_mode=link.building_mode,
        created_at=link.created_at,
        updated_at=link.updated_at,
        has_update=False,
    )
