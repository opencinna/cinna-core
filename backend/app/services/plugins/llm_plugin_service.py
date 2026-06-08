"""
LLM Plugin marketplace and agent plugin management service.

This service handles:
- Plugin marketplace CRUD operations
- Marketplace synchronization (parsing git repos for plugins)
- Plugin discovery for users
- Agent plugin installation/uninstallation
- Plugin sync to agent environments
"""

import json
import logging
import os
import re
import shutil
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Optional

from fastapi import HTTPException
from sqlmodel import Session, select

from app.models.agents.agent import Agent
from app.models.users.user import User
from app.models.plugins.llm_plugin import (
    LLMPluginMarketplace,
    LLMPluginMarketplaceCreate,
    LLMPluginMarketplaceUpdate,
    LLMPluginMarketplacePublic,
    LLMPluginMarketplacePlugin,
    LLMPluginMarketplacePluginPublic,
    AgentPluginLink,
    AgentPluginLinkCreate,
    AgentPluginLinkUpdate,
    AgentPluginLinkPublic,
    AgentPluginLinkWithUpdateInfo,
    MarketplaceStatus,
    PluginSource,
    PluginSourceType,
    EnvironmentSyncStatus,
    PluginInstallResult,
    PluginSyncResponse,
)
from app.models.environments.environment import AgentEnvironment
from app.services.knowledge.git_operations import (
    clone_repository,
    get_current_commit_hash,
    create_ssh_key_file,
    GitOperationError,
)
from app.services.users.ssh_key_service import SSHKeyService

logger = logging.getLogger(__name__)


class LLMPluginService:
    """
    Service for managing LLM plugin marketplaces and agent plugins.

    Responsibilities:
    - Marketplace CRUD operations
    - Marketplace sync (parsing git repos)
    - Plugin discovery
    - Agent plugin management
    - Plugin sync to environments
    """

    # ==========================================================================
    # Marketplace Management
    # ==========================================================================

    @staticmethod
    def _generate_name_from_url(url: str) -> str:
        """Generate a temporary marketplace name from the git URL."""
        # Extract repo name from URL
        # Handle formats like:
        # - https://github.com/user/repo.git
        # - git@github.com:user/repo.git
        # - https://github.com/user/repo
        name = url.rstrip("/").rstrip(".git")
        if "/" in name:
            name = name.rsplit("/", 1)[-1]
        if ":" in name:
            name = name.rsplit(":", 1)[-1]
        return name or "marketplace"

    @staticmethod
    def create_marketplace(
        session: Session,
        data: LLMPluginMarketplaceCreate,
        user_id: uuid.UUID
    ) -> LLMPluginMarketplace:
        """
        Create a new plugin marketplace.

        Only the URL is required. Name, description, and owner info will be
        extracted from the repository's marketplace.json during sync.

        Args:
            session: Database session
            data: Marketplace creation data
            user_id: ID of the user creating the marketplace

        Returns:
            Created marketplace
        """
        # Generate temporary name from URL (will be updated during sync)
        temp_name = LLMPluginService._generate_name_from_url(data.url)

        marketplace = LLMPluginMarketplace(
            name=temp_name,
            description=None,
            owner_name=None,
            owner_email=None,
            url=data.url,
            git_branch=data.git_branch,
            ssh_key_id=data.ssh_key_id,
            public_discovery=data.public_discovery,
            type=data.type,
            user_id=user_id,
            status=MarketplaceStatus.pending,
        )
        session.add(marketplace)
        session.commit()
        session.refresh(marketplace)

        logger.info(f"Created marketplace '{marketplace.name}' (id={marketplace.id})")
        return marketplace

    @staticmethod
    def update_marketplace(
        session: Session,
        marketplace_id: uuid.UUID,
        data: LLMPluginMarketplaceUpdate,
        user_id: uuid.UUID
    ) -> LLMPluginMarketplace | None:
        """
        Update an existing marketplace.

        Args:
            session: Database session
            marketplace_id: ID of marketplace to update
            data: Update data
            user_id: ID of the user (for ownership verification)

        Returns:
            Updated marketplace or None if not found
        """
        marketplace = LLMPluginService.get_marketplace(session, marketplace_id, user_id)
        if not marketplace:
            return None

        update_data = data.model_dump(exclude_unset=True)
        for field, value in update_data.items():
            setattr(marketplace, field, value)

        marketplace.updated_at = datetime.now(UTC)
        session.add(marketplace)
        session.commit()
        session.refresh(marketplace)

        logger.info(f"Updated marketplace '{marketplace.name}' (id={marketplace.id})")
        return marketplace

    @staticmethod
    def delete_marketplace(
        session: Session,
        marketplace_id: uuid.UUID,
        user_id: uuid.UUID
    ) -> bool:
        """
        Delete a marketplace and all its plugins.

        Args:
            session: Database session
            marketplace_id: ID of marketplace to delete
            user_id: ID of the user (for ownership verification)

        Returns:
            True if deleted, False if not found
        """
        marketplace = LLMPluginService.get_marketplace(session, marketplace_id, user_id)
        if not marketplace:
            return False

        # No persistent cache to clean up — marketplace sync uses a throwaway
        # temp clone that is discarded immediately after parsing.
        session.delete(marketplace)
        session.commit()

        logger.info(f"Deleted marketplace '{marketplace.name}' (id={marketplace_id})")
        return True

    @staticmethod
    def get_marketplace(
        session: Session,
        marketplace_id: uuid.UUID,
        user_id: uuid.UUID | None = None
    ) -> LLMPluginMarketplace | None:
        """
        Get a marketplace by ID.

        Args:
            session: Database session
            marketplace_id: ID of marketplace
            user_id: If provided, verify ownership

        Returns:
            Marketplace or None if not found
        """
        statement = select(LLMPluginMarketplace).where(
            LLMPluginMarketplace.id == marketplace_id
        )
        if user_id:
            statement = statement.where(LLMPluginMarketplace.user_id == user_id)

        return session.exec(statement).first()

    @staticmethod
    def get_marketplace_with_access_check(
        session: Session,
        marketplace_id: uuid.UUID,
        user: User,
        *,
        require_write: bool = False,
    ) -> LLMPluginMarketplace:
        """Get a marketplace, enforcing access, or raise the right HTTPException.

        Two access levels:
          - read (``require_write=False``): owner OR public OR superuser.
          - write (``require_write=True``): owner OR superuser only (NOT public).

        Raises:
            HTTPException(404): marketplace not found.
            HTTPException(403): caller lacks the required access.
        """
        marketplace = session.get(LLMPluginMarketplace, marketplace_id)
        if not marketplace:
            raise HTTPException(status_code=404, detail="Marketplace not found")

        is_owner = marketplace.user_id == user.id
        if is_owner or user.is_superuser:
            return marketplace
        if not require_write and marketplace.public_discovery:
            return marketplace
        raise HTTPException(status_code=403, detail="Not enough permissions")

    @staticmethod
    def verify_agent_access(
        session: Session,
        agent_id: uuid.UUID,
        user: User,
    ) -> Agent:
        """Verify an agent exists and the caller may access it, or raise.

        Owner OR superuser. Raises:
            HTTPException(404): agent not found.
            HTTPException(403): caller is neither owner nor superuser.
        """
        agent = session.get(Agent, agent_id)
        if not agent:
            raise HTTPException(status_code=404, detail="Agent not found")
        if agent.owner_id != user.id and not user.is_superuser:
            raise HTTPException(status_code=403, detail="Not enough permissions")
        return agent

    @staticmethod
    def list_marketplaces(
        session: Session,
        user_id: uuid.UUID,
        include_public: bool = True
    ) -> list[LLMPluginMarketplace]:
        """
        List marketplaces accessible to a user.

        Args:
            session: Database session
            user_id: User ID
            include_public: Include public marketplaces from other users

        Returns:
            List of marketplaces
        """
        if include_public:
            statement = select(LLMPluginMarketplace).where(
                (LLMPluginMarketplace.user_id == user_id) |
                (LLMPluginMarketplace.public_discovery == True)  # noqa: E712
            )
        else:
            statement = select(LLMPluginMarketplace).where(
                LLMPluginMarketplace.user_id == user_id
            )

        return list(session.exec(statement).all())

    @staticmethod
    def get_marketplace_public(
        session: Session,
        marketplace: LLMPluginMarketplace
    ) -> LLMPluginMarketplacePublic:
        """
        Convert marketplace to public schema with plugin count.

        Args:
            session: Database session
            marketplace: Marketplace model

        Returns:
            Public schema with plugin count
        """
        # Count plugins
        statement = select(LLMPluginMarketplacePlugin).where(
            LLMPluginMarketplacePlugin.marketplace_id == marketplace.id
        )
        plugins = session.exec(statement).all()
        plugin_count = len(plugins)

        return LLMPluginMarketplacePublic(
            id=marketplace.id,
            name=marketplace.name,
            description=marketplace.description,
            owner_name=marketplace.owner_name,
            owner_email=marketplace.owner_email,
            url=marketplace.url,
            git_branch=marketplace.git_branch,
            ssh_key_id=marketplace.ssh_key_id,
            public_discovery=marketplace.public_discovery,
            type=marketplace.type,
            status=marketplace.status,
            status_message=marketplace.status_message,
            last_sync_at=marketplace.last_sync_at,
            sync_commit_hash=marketplace.sync_commit_hash,
            user_id=marketplace.user_id,
            created_at=marketplace.created_at,
            updated_at=marketplace.updated_at,
            plugin_count=plugin_count,
        )

    # ==========================================================================
    # Marketplace Parsing/Sync
    # ==========================================================================

    @staticmethod
    def sync_marketplace(
        session: Session,
        marketplace_id: uuid.UUID,
        user_id: uuid.UUID
    ) -> LLMPluginMarketplace:
        """
        Sync a marketplace by cloning/pulling and parsing its plugins.

        Args:
            session: Database session
            marketplace_id: ID of marketplace to sync
            user_id: User ID (for SSH key access)

        Returns:
            Updated marketplace

        Raises:
            ValueError: If marketplace not found
            GitOperationError: If git operations fail
        """
        marketplace = LLMPluginService.get_marketplace(session, marketplace_id, user_id)
        if not marketplace:
            raise ValueError(f"Marketplace {marketplace_id} not found")

        logger.info(f"Starting sync for marketplace '{marketplace.name}'")

        # Update status to pending
        marketplace.status = MarketplaceStatus.pending
        marketplace.status_message = "Syncing repository..."
        session.add(marketplace)
        session.commit()

        try:
            # Get SSH key if configured
            ssh_key_path = None
            ssh_key_context = None

            if marketplace.ssh_key_id:
                key_data = SSHKeyService.get_decrypted_key_for_git(
                    session, marketplace.ssh_key_id, user_id
                )
                if key_data:
                    private_key, passphrase = key_data
                    ssh_key_context = create_ssh_key_file(private_key, passphrase)
                    ssh_key_path = ssh_key_context.__enter__()

            # Clone to a throwaway temp dir, parse, then discard — no persistent
            # marketplace cache. Git coordinates land in Postgres; the container
            # re-fetches plugin files at install time from those coordinates.
            temp_dir = tempfile.mkdtemp(prefix="cinna_marketplace_")
            try:
                # Clone repository (fresh each sync)
                repo = clone_repository(
                    marketplace.url,
                    temp_dir,
                    marketplace.git_branch,
                    ssh_key_path,
                )

                # Get current commit hash
                commit_hash = get_current_commit_hash(repo)

                # Parse marketplace based on type
                parser = LLMPluginService._get_parser_for_type(marketplace.type)
                parse_result = parser(temp_dir)

                # Extract metadata and plugins from parse result
                metadata = parse_result.get("metadata", {})
                plugins_data = parse_result.get("plugins", [])

                # Update marketplace metadata from repository if available
                if metadata.get("name"):
                    marketplace.name = metadata["name"]
                if metadata.get("description"):
                    marketplace.description = metadata["description"]
                if metadata.get("owner_name"):
                    marketplace.owner_name = metadata["owner_name"]
                if metadata.get("owner_email"):
                    marketplace.owner_email = metadata["owner_email"]

                # Upsert plugins
                LLMPluginService._upsert_plugins(
                    session=session,
                    marketplace=marketplace,
                    plugins_data=plugins_data,
                    commit_hash=commit_hash
                )

                # Update marketplace status
                marketplace.status = MarketplaceStatus.connected
                marketplace.status_message = f"Synced {len(plugins_data)} plugins"
                marketplace.last_sync_at = datetime.now(UTC)
                marketplace.sync_commit_hash = commit_hash
                session.add(marketplace)
                session.commit()

                logger.info(f"Successfully synced marketplace '{marketplace.name}' - {len(plugins_data)} plugins")

            finally:
                # Discard the throwaway clone (no persistent cache).
                shutil.rmtree(temp_dir, ignore_errors=True)
                # Clean up SSH key file
                if ssh_key_context:
                    ssh_key_context.__exit__(None, None, None)

        except GitOperationError as e:
            logger.error(f"Git error syncing marketplace '{marketplace.name}': {e}")
            marketplace.status = MarketplaceStatus.error
            marketplace.status_message = str(e)
            session.add(marketplace)
            session.commit()
            raise

        except Exception as e:
            logger.error(f"Error syncing marketplace '{marketplace.name}': {e}")
            marketplace.status = MarketplaceStatus.error
            marketplace.status_message = f"Sync failed: {str(e)}"
            session.add(marketplace)
            session.commit()
            raise

        session.refresh(marketplace)
        return marketplace

    @staticmethod
    def _get_parser_for_type(marketplace_type: str):
        """Get parser function for marketplace type."""
        parsers = {
            "claude": LLMPluginService._parse_claude_marketplace,
        }
        return parsers.get(marketplace_type, LLMPluginService._parse_claude_marketplace)

    @staticmethod
    def _parse_claude_marketplace(repo_path: str) -> dict:
        """
        Parse a Claude-format marketplace repository.

        Expected structure:
        .claude-plugin/marketplace.json at repo root

        Supports two source types:
        1. Local sources: "source": "./plugins/plugin-name" (relative path in marketplace repo)
        2. URL sources: "source": {"source": "url", "url": "https://github.com/..."} (external repo)

        Returns:
            Dictionary containing:
            - metadata: marketplace name, description, owner info
            - plugins: list of plugin data dictionaries
        """
        marketplace_file = os.path.join(repo_path, ".claude-plugin", "marketplace.json")

        if not os.path.exists(marketplace_file):
            logger.warning(f"No marketplace.json found at {marketplace_file}")
            return {"metadata": {}, "plugins": []}

        try:
            with open(marketplace_file, "r") as f:
                marketplace_data = json.load(f)
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON in marketplace.json: {e}")
            raise ValueError(f"Invalid marketplace.json: {e}")

        # Extract marketplace metadata
        metadata = {
            "name": marketplace_data.get("name"),
            "description": marketplace_data.get("description"),
            "owner_name": marketplace_data.get("author", {}).get("name") if isinstance(marketplace_data.get("author"), dict) else marketplace_data.get("author"),
            "owner_email": marketplace_data.get("author", {}).get("email") if isinstance(marketplace_data.get("author"), dict) else None,
        }

        # Parse plugins
        plugins = marketplace_data.get("plugins", [])
        parsed_plugins = []

        for plugin in plugins:
            # Handle source field - can be a string (local path) or an object (URL-based)
            source = plugin.get("source", "")
            source_type = PluginSourceType.local
            source_path = ""
            source_url = None
            source_branch = "main"

            if isinstance(source, dict):
                # URL-based source: {"source": "url", "url": "https://github.com/..."}
                if source.get("source") == "url" and source.get("url"):
                    source_type = PluginSourceType.url
                    source_url = source.get("url")
                    source_branch = source.get("branch", "main")
                    # For URL sources, source_path is empty (files come from external repo)
                    source_path = ""
                else:
                    # Local source as object: {"path": "./plugins/..."}
                    source_path = source.get("path", "")
            else:
                # Local source as string: "./plugins/plugin-name"
                source_path = source

            # Extract author info - handle both string and object formats
            author = plugin.get("author", {})
            if isinstance(author, dict):
                author_name = author.get("name", "")
                author_email = author.get("email", "")
            else:
                author_name = str(author) if author else ""
                author_email = ""

            parsed_plugin = {
                "name": plugin.get("name", ""),
                "description": plugin.get("description", ""),
                "version": plugin.get("version", ""),
                "author_name": author_name,
                "author_email": author_email,
                "category": plugin.get("category", ""),
                "homepage": plugin.get("homepage", ""),
                "source_path": source_path,
                "source_type": source_type,
                "source_url": source_url,
                "source_branch": source_branch,
                "config": plugin,  # Store full config for reference
            }
            if parsed_plugin["name"]:
                parsed_plugins.append(parsed_plugin)

        return {"metadata": metadata, "plugins": parsed_plugins}

    @staticmethod
    def _upsert_plugins(
        session: Session,
        marketplace: LLMPluginMarketplace,
        plugins_data: list[dict],
        commit_hash: str
    ):
        """
        Upsert plugins for a marketplace.

        - Add new plugins
        - Update existing plugins
        - Remove plugins no longer in marketplace
        """
        # Get existing plugins
        statement = select(LLMPluginMarketplacePlugin).where(
            LLMPluginMarketplacePlugin.marketplace_id == marketplace.id
        )
        existing_plugins = {p.name: p for p in session.exec(statement).all()}

        new_plugin_names = set()

        for plugin_data in plugins_data:
            name = plugin_data["name"]
            new_plugin_names.add(name)

            # Get source type - default to local if not specified
            source_type = plugin_data.get("source_type", PluginSourceType.local)

            if name in existing_plugins:
                # Update existing plugin
                plugin = existing_plugins[name]
                plugin.description = plugin_data.get("description")
                plugin.version = plugin_data.get("version")
                plugin.author_name = plugin_data.get("author_name")
                plugin.author_email = plugin_data.get("author_email")
                plugin.category = plugin_data.get("category")
                plugin.homepage = plugin_data.get("homepage")
                plugin.source_path = plugin_data.get("source_path", "")
                plugin.source_type = source_type
                plugin.source_url = plugin_data.get("source_url")
                plugin.source_branch = plugin_data.get("source_branch", "main")
                plugin.config = plugin_data.get("config")
                plugin.commit_hash = commit_hash
                plugin.updated_at = datetime.now(UTC)
                session.add(plugin)
            else:
                # Create new plugin
                plugin = LLMPluginMarketplacePlugin(
                    marketplace_id=marketplace.id,
                    name=name,
                    description=plugin_data.get("description"),
                    version=plugin_data.get("version"),
                    author_name=plugin_data.get("author_name"),
                    author_email=plugin_data.get("author_email"),
                    category=plugin_data.get("category"),
                    homepage=plugin_data.get("homepage"),
                    source_path=plugin_data.get("source_path", ""),
                    source_type=source_type,
                    source_url=plugin_data.get("source_url"),
                    source_branch=plugin_data.get("source_branch", "main"),
                    plugin_type=marketplace.type,
                    config=plugin_data.get("config"),
                    commit_hash=commit_hash,
                )
                session.add(plugin)

        # Remove plugins no longer in marketplace
        for name, plugin in existing_plugins.items():
            if name not in new_plugin_names:
                logger.info(f"Removing plugin '{name}' from marketplace")
                session.delete(plugin)

        session.commit()

    # ==========================================================================
    # Plugin Discovery
    # ==========================================================================

    @staticmethod
    def discover_plugins(
        session: Session,
        user_id: uuid.UUID,
        search: str | None = None,
        category: str | None = None,
        skip: int = 0,
        limit: int = 30
    ) -> tuple[list[LLMPluginMarketplacePluginPublic], int]:
        """
        Discover available plugins for a user.

        Args:
            session: Database session
            user_id: User ID
            search: Optional search term for name/description/author/category
            category: Optional category filter
            skip: Number of items to skip (pagination offset)
            limit: Maximum number of items to return

        Returns:
            Tuple of (list of discoverable plugins, total count)
        """
        # Get accessible marketplaces
        marketplaces = LLMPluginService.list_marketplaces(session, user_id, include_public=True)
        marketplace_ids = [m.id for m in marketplaces]
        marketplace_names = {m.id: m.name for m in marketplaces}

        if not marketplace_ids:
            return [], 0

        # Query plugins from accessible marketplaces
        statement = select(LLMPluginMarketplacePlugin).where(
            LLMPluginMarketplacePlugin.marketplace_id.in_(marketplace_ids)
        )

        if category:
            statement = statement.where(LLMPluginMarketplacePlugin.category == category)

        plugins = session.exec(statement).all()

        # Filter by search term if provided (searches name, description, author, category)
        if search:
            search_lower = search.lower()
            plugins = [
                p for p in plugins
                if search_lower in (p.name or "").lower()
                or search_lower in (p.description or "").lower()
                or search_lower in (p.author_name or "").lower()
                or search_lower in (p.category or "").lower()
            ]

        # Get total count before pagination
        total_count = len(plugins)

        # Apply pagination
        plugins = plugins[skip:skip + limit]

        # Convert to public schema with marketplace name
        result = [
            LLMPluginService.get_plugin_public(
                p, marketplace_name=marketplace_names.get(p.marketplace_id)
            )
            for p in plugins
        ]
        return result, total_count

    @staticmethod
    def get_plugin(
        session: Session,
        plugin_id: uuid.UUID
    ) -> LLMPluginMarketplacePlugin | None:
        """Get a plugin by ID."""
        return session.get(LLMPluginMarketplacePlugin, plugin_id)

    @staticmethod
    def get_plugin_public(
        plugin: LLMPluginMarketplacePlugin,
        marketplace_name: str | None = None,
    ) -> LLMPluginMarketplacePluginPublic:
        """Project a marketplace plugin to its public schema.

        Parity with ``get_marketplace_public`` / ``_link_to_public``. When
        ``marketplace_name`` is omitted it is read from the plugin's marketplace
        relationship.
        """
        if marketplace_name is None:
            marketplace = plugin.marketplace
            marketplace_name = marketplace.name if marketplace else None
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
            marketplace_name=marketplace_name,
        )

    # ==========================================================================
    # Agent Plugin Management
    # ==========================================================================

    @staticmethod
    def install_plugin_for_agent(
        session: Session,
        agent_id: uuid.UUID,
        data: AgentPluginLinkCreate
    ) -> AgentPluginLink:
        """
        Install a plugin for an agent.

        Args:
            session: Database session
            agent_id: Agent ID
            data: Plugin link creation data

        Returns:
            Created agent plugin link

        Raises:
            ValueError: If plugin not found or already installed
        """
        # Check if plugin exists
        plugin = LLMPluginService.get_plugin(session, data.plugin_id)
        if not plugin:
            raise ValueError(f"Plugin {data.plugin_id} not found")

        # Check if already installed
        existing = session.exec(
            select(AgentPluginLink).where(
                AgentPluginLink.agent_id == agent_id,
                AgentPluginLink.plugin_id == data.plugin_id
            )
        ).first()

        if existing:
            raise ValueError(f"Plugin {plugin.name} is already installed for this agent")

        # Create link
        link = AgentPluginLink(
            agent_id=agent_id,
            plugin_id=data.plugin_id,
            installed_version=plugin.version,
            installed_commit_hash=plugin.commit_hash,
            conversation_mode=data.conversation_mode,
            building_mode=data.building_mode,
        )
        session.add(link)
        session.commit()
        session.refresh(link)

        logger.info(f"Installed plugin '{plugin.name}' for agent {agent_id}")
        return link

    @staticmethod
    def uninstall_plugin_from_agent(
        session: Session,
        agent_id: uuid.UUID,
        link_id: uuid.UUID
    ) -> bool:
        """
        Uninstall a plugin from an agent.

        Args:
            session: Database session
            agent_id: Agent ID
            link_id: Plugin link ID

        Returns:
            True if uninstalled, False if not found
        """
        link = session.exec(
            select(AgentPluginLink).where(
                AgentPluginLink.id == link_id,
                AgentPluginLink.agent_id == agent_id
            )
        ).first()

        if not link:
            return False

        session.delete(link)
        session.commit()

        logger.info(f"Uninstalled plugin link {link_id} from agent {agent_id}")
        return True

    @staticmethod
    def get_agent_plugins(
        session: Session,
        agent_id: uuid.UUID
    ) -> list[AgentPluginLinkWithUpdateInfo]:
        """
        Get installed plugins for an agent with update info.

        Args:
            session: Database session
            agent_id: Agent ID

        Returns:
            List of plugin links with update availability info
        """
        statement = select(AgentPluginLink).where(
            AgentPluginLink.agent_id == agent_id
        )
        links = session.exec(statement).all()

        result = []
        for link in links:
            is_bundle = link.source == PluginSource.bundle
            plugin = None if is_bundle else link.plugin
            marketplace = plugin.marketplace if plugin else None

            # Check for updates by comparing commit hashes. Bundle plugins never
            # carry a marketplace "latest" to compare against — updates arrive
            # via bundle apply-update, not marketplace upgrade.
            has_update = False
            if plugin and link.installed_commit_hash and plugin.commit_hash:
                has_update = link.installed_commit_hash != plugin.commit_hash

            # Display identity: marketplace plugins resolve from the live plugin
            # row; bundle plugins use the frozen snapshot fields.
            snapshot_cfg = link.snapshot_config or {}
            display_name = (
                plugin.name if plugin
                else (link.snapshot_plugin_name or snapshot_cfg.get("name"))
            )
            display_desc = (
                plugin.description if plugin else snapshot_cfg.get("description")
            )
            display_cat = (
                plugin.category if plugin else snapshot_cfg.get("category")
            )
            display_mkt = (
                marketplace.name if marketplace else link.snapshot_marketplace_name
            )

            result.append(AgentPluginLinkWithUpdateInfo(
                id=link.id,
                agent_id=link.agent_id,
                plugin_id=link.plugin_id,
                source=link.source,
                snapshot_marketplace_name=link.snapshot_marketplace_name,
                snapshot_plugin_name=link.snapshot_plugin_name,
                snapshot_config=link.snapshot_config,
                installed_version=link.installed_version,
                installed_commit_hash=link.installed_commit_hash,
                conversation_mode=link.conversation_mode,
                building_mode=link.building_mode,
                disabled=link.disabled,
                created_at=link.created_at,
                updated_at=link.updated_at,
                has_update=has_update,
                latest_version=plugin.version if plugin else None,
                latest_commit_hash=plugin.commit_hash if plugin else None,
                plugin_name=display_name,
                plugin_description=display_desc,
                plugin_category=display_cat,
                marketplace_name=display_mkt,
            ))

        return result

    @staticmethod
    def update_plugin_modes(
        session: Session,
        agent_id: uuid.UUID,
        link_id: uuid.UUID,
        data: AgentPluginLinkUpdate
    ) -> AgentPluginLink | None:
        """
        Update plugin mode flags.

        Args:
            session: Database session
            agent_id: Agent ID
            link_id: Plugin link ID
            data: Update data

        Returns:
            Updated link or None if not found
        """
        link = session.exec(
            select(AgentPluginLink).where(
                AgentPluginLink.id == link_id,
                AgentPluginLink.agent_id == agent_id
            )
        ).first()

        if not link:
            return None

        if data.conversation_mode is not None:
            link.conversation_mode = data.conversation_mode
        if data.building_mode is not None:
            link.building_mode = data.building_mode
        if data.disabled is not None:
            link.disabled = data.disabled

        link.updated_at = datetime.now(UTC)
        session.add(link)
        session.commit()
        session.refresh(link)

        return link

    @staticmethod
    def upgrade_agent_plugin(
        session: Session,
        agent_id: uuid.UUID,
        link_id: uuid.UUID
    ) -> AgentPluginLink | None:
        """
        Upgrade a plugin to the latest version.

        Args:
            session: Database session
            agent_id: Agent ID
            link_id: Plugin link ID

        Returns:
            Updated link or None if not found
        """
        link = session.exec(
            select(AgentPluginLink).where(
                AgentPluginLink.id == link_id,
                AgentPluginLink.agent_id == agent_id
            )
        ).first()

        if not link:
            return None

        plugin = link.plugin
        if not plugin:
            return None

        # Update to latest version
        link.installed_version = plugin.version
        link.installed_commit_hash = plugin.commit_hash
        link.updated_at = datetime.now(UTC)

        session.add(link)
        session.commit()
        session.refresh(link)

        logger.info(f"Upgraded plugin '{plugin.name}' for agent {agent_id} to version {plugin.version}")
        return link

    # ==========================================================================
    # Plugin Sync to Environment
    # ==========================================================================

    @staticmethod
    def build_plugin_manifest(
        session: Session,
        agent_id: uuid.UUID,
        allowed_tools: list[str] | None = None,
    ) -> dict:
        """Build the workspace plugin manifest for an agent.

        The manifest carries git coordinates + per-mode flags (NOT file bytes).
        The container install routine reads it to fetch/ensure plugin files and
        regenerate ``settings.json``. This is the v2 replacement for the old
        ``prepare_plugins_for_environment`` (which built base64 file payloads).

        Per entry:
          - ``source=marketplace``: resolve git coords from the linked
            ``LLMPluginMarketplacePlugin`` (+ its marketplace):
              * ``local`` plugin -> {url: marketplace.url,
                ref: link.installed_commit_hash or plugin.commit_hash,
                subdir: plugin.source_path}
              * ``url`` plugin -> {url: plugin.source_url,
                ref: plugin.source_commit_hash or plugin.commit_hash,
                subdir: ""} (branch as fallback ref)
          - ``source=bundle``: ``git=null``; identity from the link's snapshot
            fields (files are seeded from the bundle snapshot, no fetch).

        Args:
            session: Database session.
            agent_id: Agent ID.
            allowed_tools: Optional allowed-tools list (pass-through into the
                manifest; merged into ``settings.json`` by the install routine).

        Returns:
            ``{"plugins": [...], "allowed_tools": [...] | None}``.
        """
        links = session.exec(
            select(AgentPluginLink).where(AgentPluginLink.agent_id == agent_id)
        ).all()

        entries: list[dict] = []
        for link in links:
            entry: dict | None = None

            if link.source == PluginSource.bundle:
                # Bundle-sourced: identity from snapshot fields, no git coords.
                if not (link.snapshot_marketplace_name and link.snapshot_plugin_name):
                    logger.warning(
                        f"Skipping bundle plugin link {link.id} with missing snapshot names"
                    )
                    continue
                entry = {
                    "marketplace_name": link.snapshot_marketplace_name,
                    "plugin_name": link.snapshot_plugin_name,
                    "source": PluginSource.bundle.value,
                    "git": None,
                    "conversation_mode": link.conversation_mode,
                    "building_mode": link.building_mode,
                    "disabled": link.disabled,
                    "version": link.installed_version,
                    "commit_hash": link.installed_commit_hash,
                }
            else:
                # Marketplace-sourced: resolve git coordinates from DB rows.
                plugin = link.plugin
                if not plugin:
                    logger.warning(
                        f"Skipping marketplace plugin link {link.id} — plugin row not resolvable"
                    )
                    continue
                marketplace = plugin.marketplace
                if not marketplace:
                    logger.warning(
                        f"Skipping plugin '{plugin.name}' — marketplace not resolvable"
                    )
                    continue

                git = LLMPluginService._resolve_plugin_git_coords(link, plugin, marketplace)
                if git is None:
                    logger.warning(
                        f"Skipping plugin '{plugin.name}' — could not resolve git coordinates"
                    )
                    continue

                entry = {
                    "marketplace_name": marketplace.name,
                    "plugin_name": plugin.name,
                    "source": PluginSource.marketplace.value,
                    "git": git,
                    "conversation_mode": link.conversation_mode,
                    "building_mode": link.building_mode,
                    "disabled": link.disabled,
                    "version": link.installed_version,
                    "commit_hash": link.installed_commit_hash,
                }

            if entry:
                entries.append(entry)

        manifest: dict = {"plugins": entries}
        manifest["allowed_tools"] = allowed_tools
        return manifest

    @staticmethod
    def _resolve_plugin_git_coords(
        link: AgentPluginLink,
        plugin: LLMPluginMarketplacePlugin,
        marketplace: LLMPluginMarketplace,
    ) -> dict | None:
        """Resolve {url, ref, subdir} for a marketplace plugin.

        For ``local`` plugins the files live in the marketplace repo at
        ``source_path``; for ``url`` plugins they live in an external repo.
        Returns None when no usable URL is available.
        """
        if plugin.source_type == PluginSourceType.url:
            if not plugin.source_url:
                return None
            return {
                # Normalize public SSH URLs → HTTPS so the keyless container can
                # clone public repos (e.g. anthropics over git@github.com).
                "url": LLMPluginService._normalize_public_git_url(plugin.source_url),
                # Pin to the EXTERNAL repo's own commit (source_commit_hash) when
                # captured, else fall back to its branch. Do NOT fall back to
                # plugin.commit_hash here: for url plugins that field holds the
                # MARKETPLACE repo's commit (parsed from marketplace.json), which
                # does not exist in the external source_url repo and would make
                # the container's `git checkout <ref>` fail with "unable to read
                # tree". (Capturing the external HEAD into source_commit_hash at
                # install time is a documented follow-up — it needs a network
                # fetch the v2 design avoids, so url plugins track the branch tip.)
                "ref": plugin.source_commit_hash or plugin.source_branch,
                "subdir": "",
            }

        # local plugin — files are a subdir of the marketplace repo
        subdir = (plugin.source_path or "").strip().lstrip("./")
        return {
            "url": LLMPluginService._normalize_public_git_url(marketplace.url),
            # Pinned to the install-time commit for reproducibility; fall back to
            # the plugin's parsed commit, then the marketplace branch.
            "ref": link.installed_commit_hash or plugin.commit_hash or marketplace.git_branch,
            "subdir": subdir,
        }

    # Well-known PUBLIC git hosts whose SSH URLs clone keyless over HTTPS. Only
    # these are rewritten — unknown/private hosts are left untouched so the
    # private-marketplace SSH flow (deferred) is never broken.
    _PUBLIC_GIT_HOSTS = ("github.com", "gitlab.com", "bitbucket.org")
    # scp-like form: [user@]host:owner/repo(.git)  e.g. git@github.com:org/repo.git
    _SCP_GIT_RE = re.compile(r"^(?:[^@/]+@)?([^:/]+):(.+)$")
    # ssh:// form: ssh://[user@]host[:port]/owner/repo(.git)
    _SSH_GIT_RE = re.compile(r"^ssh://(?:[^@/]+@)?([^:/]+)(?::\d+)?/(.+)$")

    @staticmethod
    def _normalize_public_git_url(url: str | None) -> str | None:
        """Rewrite a well-known PUBLIC host's SSH git URL to its HTTPS form.

        The agent-env container has no GitHub/GitLab/Bitbucket SSH key, so an
        SSH-form URL (``git@github.com:org/repo.git`` or
        ``ssh://git@github.com/org/repo.git``) fails ``Permission denied
        (publickey)`` even for a PUBLIC repo that clones fine over HTTPS. We
        rewrite only the recognized public hosts; any other SSH URL (a genuinely
        private host) is returned unchanged so the deferred SSH-key-injection
        path is untouched.

        Examples:
          git@github.com:anthropics/x.git      → https://github.com/anthropics/x.git
          ssh://git@gitlab.com/org/x           → https://gitlab.com/org/x.git
          https://github.com/org/x.git         → unchanged (already HTTPS)
          git@my-private-host.internal:org/x   → unchanged (unknown host)
        """
        if not url:
            return url
        candidate = url.strip()

        # Already HTTP(S) / git protocol — nothing to do.
        if candidate.startswith(("https://", "http://", "git://")):
            return url

        host = path = None
        m = LLMPluginService._SSH_GIT_RE.match(candidate)
        if m:
            host, path = m.group(1), m.group(2)
        else:
            m = LLMPluginService._SCP_GIT_RE.match(candidate)
            if m:
                host, path = m.group(1), m.group(2)

        if not host or host.lower() not in LLMPluginService._PUBLIC_GIT_HOSTS:
            return url  # unknown/private host or not an SSH URL — leave as-is

        path = path.lstrip("/")
        if not path.endswith(".git"):
            path = f"{path}.git"
        return f"https://{host.lower()}/{path}"

    @staticmethod
    def _link_to_public(link: AgentPluginLink) -> AgentPluginLinkPublic:
        """Project an AgentPluginLink to its public schema (source-aware)."""
        return AgentPluginLinkPublic(
            id=link.id,
            agent_id=link.agent_id,
            plugin_id=link.plugin_id,
            source=link.source,
            snapshot_marketplace_name=link.snapshot_marketplace_name,
            snapshot_plugin_name=link.snapshot_plugin_name,
            snapshot_config=link.snapshot_config,
            installed_version=link.installed_version,
            installed_commit_hash=link.installed_commit_hash,
            conversation_mode=link.conversation_mode,
            building_mode=link.building_mode,
            disabled=link.disabled,
            created_at=link.created_at,
            updated_at=link.updated_at,
        )

    @staticmethod
    def _coerce_install_results(raw: object) -> list[PluginInstallResult]:
        """Normalize adapter `set_plugins` output into PluginInstallResult list.

        Adapters return a list of result dicts; defensively tolerate a non-list
        (e.g. an old stub) by treating it as no results.
        """
        if not isinstance(raw, list):
            return []
        results: list[PluginInstallResult] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            try:
                results.append(PluginInstallResult(
                    plugin_name=item.get("plugin_name", ""),
                    marketplace_name=item.get("marketplace_name", ""),
                    source=item.get("source", "marketplace"),
                    status=item.get("status", "failed"),
                    error_message=item.get("error_message"),
                ))
            except Exception:
                continue
        return results

    @staticmethod
    def _add_unique_failure(
        bucket: list[PluginInstallResult], result: PluginInstallResult
    ) -> None:
        """Append `result` to `bucket` unless an equivalent entry already exists.

        Dedup key = (marketplace_name, plugin_name, source) so the same plugin
        failing across multiple environments is reported once.
        """
        key = (result.marketplace_name, result.plugin_name, result.source)
        for existing in bucket:
            if (existing.marketplace_name, existing.plugin_name, existing.source) == key:
                return
        bucket.append(result)

    @staticmethod
    async def sync_plugins_to_agent_environments(
        session: Session,
        agent_id: uuid.UUID,
        user_id: uuid.UUID,
        plugin_link: AgentPluginLink | None = None,
        message_prefix: str | None = None,
    ) -> PluginSyncResponse:
        """
        Sync plugins to all running and suspended environments of an agent.

        For suspended environments, activates them first before syncing.

        Args:
            session: Database session
            agent_id: Agent ID
            user_id: User ID (for SSH key access)
            plugin_link: Optional plugin link that triggered the sync
            message_prefix: Optional verb prepended to the response ``message``
                (e.g. ``"Plugin uninstalled."``) so callers don't post-process it.

        Returns:
            PluginSyncResponse with detailed status per environment
        """
        def _prefixed(msg: str) -> str:
            return f"{message_prefix} {msg}" if message_prefix else msg
        from app.services.environments.environment_service import EnvironmentService
        from sqlalchemy import or_

        environments_synced = []
        successful_syncs = 0
        failed_syncs = 0
        # Plugin-level failures aggregated across all environments (deduped).
        aggregated_failures: list[PluginInstallResult] = []

        # Get all running and suspended environments
        statement = select(AgentEnvironment).where(
            AgentEnvironment.agent_id == agent_id,
            or_(
                AgentEnvironment.status == "running",
                AgentEnvironment.status == "suspended"
            )
        )
        environments = list(session.exec(statement).all())

        if not environments:
            logger.info(f"No running/suspended environments for agent {agent_id}, skipping plugin sync")
            plugin_link_public = None
            if plugin_link:
                plugin_link_public = LLMPluginService._link_to_public(plugin_link)
            return PluginSyncResponse(
                success=True,
                message=_prefixed("No environments to sync"),
                plugin_link=plugin_link_public,
                environments_synced=[],
                total_environments=0,
                successful_syncs=0,
                failed_syncs=0,
            )

        # Build the manifest once (git coordinates + flags, no file bytes). The
        # container install routine (triggered via adapter.set_plugins) fetches
        # the files at the pinned ref and regenerates settings.json.
        manifest = LLMPluginService.build_plugin_manifest(
            session=session,
            agent_id=agent_id,
        )

        # Get lifecycle manager
        lifecycle_manager = EnvironmentService.get_lifecycle_manager()

        # Sync to each environment
        for env in environments:
            was_suspended = env.status == "suspended"
            try:
                # Activate suspended environments first
                if was_suspended:
                    logger.info(f"Activating suspended environment {env.id} before plugin sync")
                    try:
                        await lifecycle_manager.activate_suspended_environment(env)
                        # Refresh environment status
                        session.refresh(env)
                    except Exception as activate_error:
                        logger.error(f"Failed to activate environment {env.id}: {activate_error}")
                        environments_synced.append(EnvironmentSyncStatus(
                            environment_id=env.id,
                            instance_name=env.instance_name or str(env.id),
                            status="error",
                            error_message=f"Failed to activate: {str(activate_error)}",
                            was_suspended=True,
                        ))
                        failed_syncs += 1
                        continue

                logger.info(f"Syncing plugins to environment {env.id}")
                adapter = lifecycle_manager.get_adapter(env)
                # Pushes the manifest + runs the container install routine in one
                # call; per-plugin failures are returned (not raised) so a single
                # bad plugin never fails the whole env sync.
                raw_results = await adapter.set_plugins(manifest)
                logger.info(f"Successfully synced plugins to environment {env.id}")

                env_results = LLMPluginService._coerce_install_results(raw_results)
                env_partial = any(r.status == "failed" for r in env_results)

                environments_synced.append(EnvironmentSyncStatus(
                    environment_id=env.id,
                    instance_name=env.instance_name or str(env.id),
                    status="activated_and_synced" if was_suspended else "success",
                    error_message=None,
                    was_suspended=was_suspended,
                    plugin_results=env_results,
                    partial_failures=env_partial,
                ))
                successful_syncs += 1
                # Aggregate plugin-level failures across environments for the
                # top-level response (deduped by marketplace/plugin/source).
                for r in env_results:
                    if r.status == "failed":
                        LLMPluginService._add_unique_failure(aggregated_failures, r)

            except Exception as e:
                logger.error(f"Failed to sync plugins to environment {env.id}: {e}")
                environments_synced.append(EnvironmentSyncStatus(
                    environment_id=env.id,
                    instance_name=env.instance_name or str(env.id),
                    status="error",
                    error_message=str(e),
                    was_suspended=was_suspended,
                ))
                failed_syncs += 1
                # Continue with other environments even if one fails

        # Build response
        plugin_link_public = None
        if plugin_link:
            plugin_link_public = LLMPluginService._link_to_public(plugin_link)

        partial_failures = len(aggregated_failures) > 0
        message = f"Synced to {successful_syncs}/{len(environments)} environments"
        if failed_syncs > 0:
            message += f" ({failed_syncs} failed)"
        if partial_failures:
            names = ", ".join(
                f"{r.marketplace_name}/{r.plugin_name}" for r in aggregated_failures
            )
            message += f" — {len(aggregated_failures)} plugin(s) failed to install: {names}"

        return PluginSyncResponse(
            success=failed_syncs == 0,
            message=_prefixed(message),
            plugin_link=plugin_link_public,
            environments_synced=environments_synced,
            total_environments=len(environments),
            successful_syncs=successful_syncs,
            failed_syncs=failed_syncs,
            plugin_results=aggregated_failures,
            partial_failures=partial_failures,
        )
