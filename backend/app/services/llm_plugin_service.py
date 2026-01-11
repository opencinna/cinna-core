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
import shutil
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from sqlmodel import Session, select

from app.models.llm_plugin import (
    LLMPluginMarketplace,
    LLMPluginMarketplaceCreate,
    LLMPluginMarketplaceUpdate,
    LLMPluginMarketplacePublic,
    LLMPluginMarketplacePlugin,
    LLMPluginMarketplacePluginPublic,
    AgentPluginLink,
    AgentPluginLinkCreate,
    AgentPluginLinkUpdate,
    AgentPluginLinkWithUpdateInfo,
    MarketplaceStatus,
    PluginSourceType,
)
from app.models.environment import AgentEnvironment
from app.services.git_operations import (
    clone_repository,
    pull_repository,
    get_current_commit_hash,
    create_ssh_key_file,
    GitOperationError,
)
from app.services.ssh_key_service import SSHKeyService

logger = logging.getLogger(__name__)

# Cache directory for marketplace repositories
MARKETPLACE_CACHE_DIR = os.environ.get("MARKETPLACE_CACHE_DIR", "/app/data/marketplaces")

# Cache directory for external plugin repositories (URL-based sources)
PLUGIN_REPO_CACHE_DIR = os.environ.get("PLUGIN_REPO_CACHE_DIR", "/app/data/plugin_repos")


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

        marketplace.updated_at = datetime.utcnow()
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

        # Clean up cached repository
        cache_path = LLMPluginService._get_marketplace_cache_path(marketplace_id)
        if os.path.exists(cache_path):
            shutil.rmtree(cache_path, ignore_errors=True)

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

            try:
                # Clone or pull repository
                cache_path = LLMPluginService._get_marketplace_cache_path(marketplace_id)
                repo = LLMPluginService._clone_or_pull_repo(
                    url=marketplace.url,
                    branch=marketplace.git_branch,
                    cache_path=cache_path,
                    ssh_key_path=ssh_key_path
                )

                # Get current commit hash
                commit_hash = get_current_commit_hash(repo)

                # Parse marketplace based on type
                parser = LLMPluginService._get_parser_for_type(marketplace.type)
                parse_result = parser(cache_path)

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
                marketplace.last_sync_at = datetime.utcnow()
                marketplace.sync_commit_hash = commit_hash
                session.add(marketplace)
                session.commit()

                logger.info(f"Successfully synced marketplace '{marketplace.name}' - {len(plugins_data)} plugins")

            finally:
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
    def _get_marketplace_cache_path(marketplace_id: uuid.UUID) -> str:
        """Get cache path for a marketplace repository."""
        return os.path.join(MARKETPLACE_CACHE_DIR, str(marketplace_id))

    @staticmethod
    def _clone_or_pull_repo(
        url: str,
        branch: str,
        cache_path: str,
        ssh_key_path: str | None = None
    ):
        """Clone a new repo or pull existing one."""
        if os.path.exists(cache_path) and os.path.exists(os.path.join(cache_path, ".git")):
            logger.info(f"Pulling existing repository at {cache_path}")
            return pull_repository(cache_path, branch, ssh_key_path)
        else:
            logger.info(f"Cloning repository to {cache_path}")
            os.makedirs(os.path.dirname(cache_path), exist_ok=True)
            if os.path.exists(cache_path):
                shutil.rmtree(cache_path)
            return clone_repository(url, cache_path, branch, ssh_key_path)

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
                plugin.updated_at = datetime.utcnow()
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
        category: str | None = None
    ) -> list[LLMPluginMarketplacePluginPublic]:
        """
        Discover available plugins for a user.

        Args:
            session: Database session
            user_id: User ID
            search: Optional search term for name/description
            category: Optional category filter

        Returns:
            List of discoverable plugins
        """
        # Get accessible marketplaces
        marketplaces = LLMPluginService.list_marketplaces(session, user_id, include_public=True)
        marketplace_ids = [m.id for m in marketplaces]
        marketplace_names = {m.id: m.name for m in marketplaces}

        if not marketplace_ids:
            return []

        # Query plugins from accessible marketplaces
        statement = select(LLMPluginMarketplacePlugin).where(
            LLMPluginMarketplacePlugin.marketplace_id.in_(marketplace_ids)
        )

        if category:
            statement = statement.where(LLMPluginMarketplacePlugin.category == category)

        plugins = session.exec(statement).all()

        # Filter by search term if provided
        if search:
            search_lower = search.lower()
            plugins = [
                p for p in plugins
                if search_lower in (p.name or "").lower()
                or search_lower in (p.description or "").lower()
            ]

        # Convert to public schema with marketplace name
        return [
            LLMPluginMarketplacePluginPublic(
                id=p.id,
                marketplace_id=p.marketplace_id,
                name=p.name,
                description=p.description,
                version=p.version,
                author_name=p.author_name,
                author_email=p.author_email,
                category=p.category,
                homepage=p.homepage,
                source_path=p.source_path,
                source_type=p.source_type,
                source_url=p.source_url,
                source_branch=p.source_branch,
                source_commit_hash=p.source_commit_hash,
                plugin_type=p.plugin_type,
                commit_hash=p.commit_hash,
                config=p.config,
                created_at=p.created_at,
                updated_at=p.updated_at,
                marketplace_name=marketplace_names.get(p.marketplace_id),
            )
            for p in plugins
        ]

    @staticmethod
    def get_plugin(
        session: Session,
        plugin_id: uuid.UUID
    ) -> LLMPluginMarketplacePlugin | None:
        """Get a plugin by ID."""
        return session.get(LLMPluginMarketplacePlugin, plugin_id)

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
            plugin = link.plugin
            marketplace = plugin.marketplace if plugin else None

            # Check for updates by comparing commit hashes
            has_update = False
            if plugin and link.installed_commit_hash and plugin.commit_hash:
                has_update = link.installed_commit_hash != plugin.commit_hash

            result.append(AgentPluginLinkWithUpdateInfo(
                id=link.id,
                agent_id=link.agent_id,
                plugin_id=link.plugin_id,
                installed_version=link.installed_version,
                installed_commit_hash=link.installed_commit_hash,
                conversation_mode=link.conversation_mode,
                building_mode=link.building_mode,
                created_at=link.created_at,
                updated_at=link.updated_at,
                has_update=has_update,
                latest_version=plugin.version if plugin else None,
                latest_commit_hash=plugin.commit_hash if plugin else None,
                plugin_name=plugin.name if plugin else None,
                plugin_description=plugin.description if plugin else None,
                plugin_category=plugin.category if plugin else None,
                marketplace_name=marketplace.name if marketplace else None,
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

        link.updated_at = datetime.utcnow()
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
        link.updated_at = datetime.utcnow()

        session.add(link)
        session.commit()
        session.refresh(link)

        logger.info(f"Upgraded plugin '{plugin.name}' for agent {agent_id} to version {plugin.version}")
        return link

    # ==========================================================================
    # Plugin Sync to Environment
    # ==========================================================================

    @staticmethod
    def prepare_plugins_for_environment(
        session: Session,
        agent_id: uuid.UUID,
        mode: str | None = None
    ) -> dict:
        """
        Prepare plugin data for syncing to agent environment.

        Args:
            session: Database session
            agent_id: Agent ID
            mode: Optional filter by mode ("conversation" or "building")

        Returns:
            Dictionary with plugin data for environment
        """
        links = LLMPluginService.get_agent_plugins(session, agent_id)

        # Filter by mode if specified
        if mode == "conversation":
            links = [l for l in links if l.conversation_mode]
        elif mode == "building":
            links = [l for l in links if l.building_mode]

        active_plugins = []
        for link in links:
            # Get plugin and marketplace info
            plugin = session.get(LLMPluginMarketplacePlugin, link.plugin_id)
            if not plugin:
                continue

            marketplace = plugin.marketplace
            if not marketplace:
                continue

            active_plugins.append({
                "marketplace_name": marketplace.name,
                "plugin_name": plugin.name,
                "path": f"/app/workspace/plugins/{marketplace.name}/{plugin.name}",
                "conversation_mode": link.conversation_mode,
                "building_mode": link.building_mode,
                "version": link.installed_version,
                "commit_hash": link.installed_commit_hash,
            })

        return {
            "active_plugins": active_plugins,
            "settings_json": {"active_plugins": active_plugins},
        }

    # ==========================================================================
    # External Plugin Repository Handling (URL-based sources)
    # ==========================================================================

    @staticmethod
    def _get_plugin_repo_cache_path(plugin_id: uuid.UUID) -> str:
        """Get cache path for an external plugin repository."""
        return os.path.join(PLUGIN_REPO_CACHE_DIR, str(plugin_id))

    @staticmethod
    def _clone_or_pull_plugin_repo(
        url: str,
        branch: str,
        cache_path: str,
        ssh_key_path: str | None = None
    ):
        """Clone a new external plugin repo or pull existing one."""
        if os.path.exists(cache_path) and os.path.exists(os.path.join(cache_path, ".git")):
            logger.info(f"Pulling existing plugin repository at {cache_path}")
            return pull_repository(cache_path, branch, ssh_key_path)
        else:
            logger.info(f"Cloning plugin repository to {cache_path}")
            os.makedirs(os.path.dirname(cache_path), exist_ok=True)
            if os.path.exists(cache_path):
                shutil.rmtree(cache_path)
            return clone_repository(url, cache_path, branch, ssh_key_path)

    @staticmethod
    def _parse_plugin_json(repo_path: str) -> dict:
        """
        Parse .claude-plugin/plugin.json from a plugin repository.

        This is the standard location for plugin configuration in Claude-format plugins.

        Args:
            repo_path: Path to the cloned repository

        Returns:
            Dictionary with plugin configuration, or empty dict if not found
        """
        plugin_json_path = os.path.join(repo_path, ".claude-plugin", "plugin.json")

        if not os.path.exists(plugin_json_path):
            logger.warning(f"No plugin.json found at {plugin_json_path}")
            return {}

        try:
            with open(plugin_json_path, "r") as f:
                return json.load(f)
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON in plugin.json: {e}")
            return {}

    @staticmethod
    def sync_external_plugin_repo(
        session: Session,
        plugin_id: uuid.UUID,
        user_id: uuid.UUID | None = None
    ) -> dict:
        """
        Sync an external plugin repository (URL-based source).

        Clones/pulls the plugin's source_url repository and reads the
        .claude-plugin/plugin.json for additional metadata.

        Args:
            session: Database session
            plugin_id: Plugin ID
            user_id: User ID (for SSH key access if needed)

        Returns:
            Dictionary with plugin configuration from plugin.json

        Raises:
            ValueError: If plugin not found or not URL-based
            GitOperationError: If git operations fail
        """
        plugin = session.get(LLMPluginMarketplacePlugin, plugin_id)
        if not plugin:
            raise ValueError(f"Plugin {plugin_id} not found")

        if plugin.source_type != PluginSourceType.url:
            raise ValueError(f"Plugin {plugin_id} is not URL-based")

        if not plugin.source_url:
            raise ValueError(f"Plugin {plugin_id} has no source URL")

        logger.info(f"Syncing external plugin repo for '{plugin.name}' from {plugin.source_url}")

        # Clone or pull the plugin repository
        cache_path = LLMPluginService._get_plugin_repo_cache_path(plugin_id)

        try:
            # TODO: Support SSH keys for private plugin repos
            repo = LLMPluginService._clone_or_pull_plugin_repo(
                url=plugin.source_url,
                branch=plugin.source_branch,
                cache_path=cache_path,
                ssh_key_path=None
            )

            # Get current commit hash
            source_commit_hash = get_current_commit_hash(repo)

            # Update plugin with the commit hash
            plugin.source_commit_hash = source_commit_hash
            plugin.updated_at = datetime.utcnow()
            session.add(plugin)
            session.commit()

            # Parse plugin.json for configuration
            plugin_config = LLMPluginService._parse_plugin_json(cache_path)

            logger.info(f"Successfully synced plugin repo for '{plugin.name}' at commit {source_commit_hash}")
            return plugin_config

        except GitOperationError as e:
            logger.error(f"Git error syncing plugin repo for '{plugin.name}': {e}")
            raise

    @staticmethod
    def get_plugin_files(
        session: Session,
        plugin_id: uuid.UUID,
        commit_hash: str | None = None,
        user_id: uuid.UUID | None = None
    ) -> dict[str, bytes]:
        """
        Get plugin files for syncing to environment.

        Supports both local plugins (from marketplace repo) and URL-based plugins
        (from external repositories).

        Args:
            session: Database session
            plugin_id: Plugin ID
            commit_hash: Optional specific commit hash (for reproducibility)
            user_id: User ID (for SSH key access)

        Returns:
            Dictionary mapping file paths to contents
        """
        plugin = session.get(LLMPluginMarketplacePlugin, plugin_id)
        if not plugin:
            raise ValueError(f"Plugin {plugin_id} not found")

        # Handle URL-based plugins (external repositories)
        if plugin.source_type == PluginSourceType.url:
            return LLMPluginService._get_url_plugin_files(session, plugin, user_id)

        # Handle local plugins (from marketplace repo)
        return LLMPluginService._get_local_plugin_files(session, plugin)

    @staticmethod
    def _get_local_plugin_files(
        session: Session,
        plugin: LLMPluginMarketplacePlugin
    ) -> dict[str, bytes]:
        """Get files for a local plugin (from marketplace repo)."""
        marketplace = plugin.marketplace
        if not marketplace:
            raise ValueError(f"Marketplace not found for plugin {plugin.id}")

        # Get marketplace cache path
        cache_path = LLMPluginService._get_marketplace_cache_path(marketplace.id)
        if not os.path.exists(cache_path):
            raise ValueError(f"Marketplace repository not cached. Run sync first.")

        # Get plugin source path
        source_path = plugin.source_path.lstrip("./")
        plugin_path = os.path.join(cache_path, source_path)

        if not os.path.exists(plugin_path):
            raise ValueError(f"Plugin path {plugin_path} not found")

        # Collect all files in the plugin directory
        files = {}
        for root, dirs, filenames in os.walk(plugin_path):
            for filename in filenames:
                file_path = os.path.join(root, filename)
                rel_path = os.path.relpath(file_path, plugin_path)

                with open(file_path, "rb") as f:
                    files[rel_path] = f.read()

        return files

    @staticmethod
    def _get_url_plugin_files(
        session: Session,
        plugin: LLMPluginMarketplacePlugin,
        user_id: uuid.UUID | None = None
    ) -> dict[str, bytes]:
        """
        Get files for a URL-based plugin (from external repository).

        If the repository is not cached, it will be cloned first.
        """
        if not plugin.source_url:
            raise ValueError(f"Plugin {plugin.id} has no source URL")

        # Get plugin repo cache path
        cache_path = LLMPluginService._get_plugin_repo_cache_path(plugin.id)

        # Clone/pull if not cached
        if not os.path.exists(cache_path):
            logger.info(f"Plugin repo not cached, cloning {plugin.source_url}")
            try:
                LLMPluginService._clone_or_pull_plugin_repo(
                    url=plugin.source_url,
                    branch=plugin.source_branch,
                    cache_path=cache_path,
                    ssh_key_path=None
                )
            except GitOperationError as e:
                raise ValueError(f"Failed to clone plugin repository: {e}")

        # For URL-based plugins, the entire repo is the plugin
        # (no source_path subdir like local plugins)
        plugin_path = cache_path

        # Collect all files in the plugin directory
        files = {}
        for root, dirs, filenames in os.walk(plugin_path):
            # Skip .git directory
            if ".git" in root:
                continue

            for filename in filenames:
                file_path = os.path.join(root, filename)
                rel_path = os.path.relpath(file_path, plugin_path)

                # Skip .git files
                if rel_path.startswith(".git"):
                    continue

                with open(file_path, "rb") as f:
                    files[rel_path] = f.read()

        return files

    @staticmethod
    async def sync_plugins_to_agent_environments(
        session: Session,
        agent_id: uuid.UUID,
        user_id: uuid.UUID
    ):
        """
        Sync plugins to all running environments of an agent.

        Args:
            session: Database session
            agent_id: Agent ID
            user_id: User ID (for SSH key access)
        """
        from app.services.environment_service import EnvironmentService

        # Get all running environments
        statement = select(AgentEnvironment).where(
            AgentEnvironment.agent_id == agent_id,
            AgentEnvironment.status == "running"
        )
        running_environments = session.exec(statement).all()

        if not running_environments:
            logger.info(f"No running environments for agent {agent_id}, skipping plugin sync")
            return

        # Prepare plugin data once
        plugins_data = LLMPluginService.prepare_plugins_for_environment(
            session=session,
            agent_id=agent_id
        )

        # Get plugin files for each installed plugin
        plugin_files = {}
        for plugin_info in plugins_data.get("active_plugins", []):
            # Find the plugin link to get plugin_id
            link = session.exec(
                select(AgentPluginLink).where(
                    AgentPluginLink.agent_id == agent_id
                ).join(LLMPluginMarketplacePlugin).where(
                    LLMPluginMarketplacePlugin.name == plugin_info["plugin_name"]
                )
            ).first()

            if link:
                try:
                    files = LLMPluginService.get_plugin_files(
                        session=session,
                        plugin_id=link.plugin_id,
                        commit_hash=plugin_info.get("commit_hash"),
                        user_id=user_id
                    )
                    plugin_key = f"{plugin_info['marketplace_name']}/{plugin_info['plugin_name']}"
                    plugin_files[plugin_key] = files
                except Exception as e:
                    logger.error(f"Failed to get files for plugin {plugin_info['plugin_name']}: {e}")

        # Combine plugins data with files
        full_plugins_data = {
            **plugins_data,
            "plugin_files": plugin_files,
        }

        # Get lifecycle manager
        lifecycle_manager = EnvironmentService.get_lifecycle_manager()

        # Sync to each environment
        for env in running_environments:
            try:
                logger.info(f"Syncing plugins to environment {env.id}")
                adapter = lifecycle_manager.get_adapter(env)
                await adapter.set_plugins(full_plugins_data)
                logger.info(f"Successfully synced plugins to environment {env.id}")
            except Exception as e:
                logger.error(f"Failed to sync plugins to environment {env.id}: {e}")
                # Continue with other environments even if one fails
