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

from app.core.config import settings
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
    PluginSkillSummary,
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
from app.services.agents.skill_manifest import parse_skill_dir
from app.services.users.ssh_key_service import SSHKeyService

logger = logging.getLogger(__name__)

#: ``unsupported_reason`` code → the sentence a refused install answers with.
#: The row badge's copy belongs to the client (it renders the code); this map
#: exists because a 409 has to say something, and "unsupported" alone does not
#: tell the caller which of the five different problems they hit.
UNSUPPORTED_REASON_SENTENCES: dict[str, str] = {
    "npm_source": (
        "This entry is published to npm, which agent environments cannot "
        "install from."
    ),
    "app_connector_only": (
        "This entry only declares app connectors, which this platform does "
        "not run."
    ),
    "no_skill_md": (
        "This entry has no valid SKILL.md, so there is nothing to install."
    ),
    "unknown_source": (
        "This entry declares a source this platform cannot fetch from."
    ),
    "unsafe_path": (
        "This entry points outside its repository, so it cannot be fetched "
        "safely."
    ),
}

_UNSUPPORTED_FALLBACK = (
    "This marketplace entry cannot be installed by this platform."
)


class MarketplaceFormatError(ValueError):
    """The repository cannot be read as the format the marketplace declares.

    A ``ValueError`` subclass so that :meth:`LLMPluginService.sync_marketplace`
    keeps recording it on the row exactly like any other parse failure, and a
    named one so the sync route can answer "this repository is unusable" (422)
    rather than the "no such marketplace" (404) its plain-``ValueError`` branch
    means.

    Raising is the whole point: a sync that cannot read the repository must
    leave the plugin rows alone. Answering with an empty catalog instead would
    send ``_upsert_plugins`` down its "remove plugins no longer in marketplace"
    pass and delete every row — and since a link now *survives* its plugin row
    (``ON DELETE SET NULL``), that turns one bad sync into a permanent orphan
    on every install of it.
    """

    #: Stable code the sync route answers with, so a client can branch without
    #: matching prose.
    code = "unsupported_marketplace_type"


class MarketplaceCatalogError(MarketplaceFormatError):
    """The format is known, but the repository's catalog cannot be read.

    A missing ``marketplace.json``, an unreadable one, a ``skills`` repository
    with no skill folders anywhere: the file (or the directory layout that
    stands in for it) is *expected* to be there, so its absence is corrupt
    state, not an empty catalog. An upstream rename, a branch where the file is
    briefly absent, a permissions accident — each is transient, and each would
    otherwise be recorded as a successful sync of zero plugins.
    """

    code = "marketplace_catalog_unreadable"


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

        Agents that installed one of those plugins keep their install rows:
        ``agent_plugin_link.plugin_id`` is ``ON DELETE SET NULL``, so the link
        is orphaned rather than deleted. An administrator removing a catalog
        must not silently uninstall a plugin from somebody else's agent — the
        owner sees the row flagged ``source_unavailable`` and decides.

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

        Owner OR superuser. A caller who is neither gets the same 404 as a
        caller naming an id that does not exist: agent ids are guessable and
        every verb behind this helper is a write, so a 403 here would confirm
        that somebody else's agent exists. This is
        :meth:`AgentService.assert_can_build`'s no-existence-leak rule, which
        these routes reach only after this check, applied at the check itself.

        Raises:
            HTTPException(404): agent not found, or not the caller's.
        """
        agent = session.get(Agent, agent_id)
        if not agent or (agent.owner_id != user.id and not user.is_superuser):
            raise HTTPException(status_code=404, detail="Agent not found")
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
                previous_name = marketplace.name
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

                # The marketplace name is half of every install's directory
                # identity, so a rename upstream moves the directory — and the
                # frozen copy on each install has to move with it. Guarded like
                # the re-attach: a bookkeeping repair never fails a sync whose
                # plugin rows are already committed.
                try:
                    LLMPluginService._rename_link_snapshots(
                        session, marketplace, previous_name
                    )
                except Exception as e:
                    session.rollback()
                    logger.exception(
                        f"Could not move install snapshots from "
                        f"'{previous_name}' to '{marketplace.name}': {e}"
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
        """The parser for ``marketplace_type``, or raise.

        No fall-back. A marketplace whose ``type`` this platform does not know
        is a repository nobody has read: parsing it as Claude would either
        report zero plugins (looking like an empty catalog) or, worse, parse a
        file that happens to be there and present entries the container cannot
        install. ``sync_marketplace`` turns the raise into
        ``status=error`` with this message, which is the honest answer.

        Create and update validate ``type`` against
        :data:`~app.models.plugins.llm_plugin.MarketplaceType`, so the only way
        to reach this is a row written before that validation existed.
        """
        parsers = {
            "claude": LLMPluginService._parse_claude_marketplace,
            "codex": LLMPluginService._parse_codex_marketplace,
            "skills": LLMPluginService._parse_skills_marketplace,
        }
        parser = parsers.get(marketplace_type)
        if parser is None:
            raise MarketplaceFormatError(
                f"unsupported marketplace type: {marketplace_type!r}"
            )
        return parser

    @staticmethod
    def _safe_entry_path(raw: str, *, entry_name: str) -> str | None:
        """Normalise a repo-relative path from a marketplace entry.

        Returns the normalised path, or ``None`` when it escapes the
        repository — an absolute path, or one containing ``..``. This mirrors
        the container's own ``Unsafe plugin subdir`` guard one layer earlier,
        so a bad entry is caught while an administrator is looking at the sync
        rather than at every install of it, on somebody else's agent, with no
        way to fix it.

        The refusal is per **entry**, not per marketplace: an unsafe path is
        listed like any other entry this platform cannot install
        (``unsafe_path``), because an upstream author must not be able to take
        an admin's other twenty entries down with one bad line.
        """
        candidate = (raw or "").strip()
        if not candidate:
            return ""
        if candidate.startswith("/") or ".." in Path(candidate).parts:
            logger.warning(
                f"Unsafe plugin path in marketplace entry {entry_name!r}: "
                f"{candidate!r}"
            )
            return None
        # Only the leading "./" is dropped — never a leading dot, which is a
        # legitimate first character of a directory name.
        return candidate[2:] if candidate.startswith("./") else candidate

    @staticmethod
    def _read_catalog_file(path: str, label: str) -> dict:
        """The catalog JSON at ``path``, or raise :class:`MarketplaceCatalogError`.

        Absent, unreadable, malformed and "not an object" are one answer: this
        repository has no catalog we can read *right now*. None of them is an
        empty catalog, and the difference is not cosmetic — an empty catalog is
        a valid parse result that deletes every plugin row of the marketplace,
        which orphans every install of them permanently. A repository whose
        entry list is genuinely empty says so with ``"plugins": []``, and that
        still parses.
        """
        if not os.path.exists(path):
            logger.error(f"No {label} found at {path}")
            raise MarketplaceCatalogError(
                f"{label} is missing from this repository — nothing was read, "
                f"so no plugin was changed."
            )
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            # UnicodeDecodeError is a ValueError but *not* a JSONDecodeError,
            # so letting it past here would reach the sync route's plain
            # ``except ValueError`` branch and answer 404 "no such
            # marketplace" for a repository that is plainly there.
            logger.error(f"Invalid JSON in {label}: {e}")
            raise MarketplaceCatalogError(f"Invalid {label}: {e}")
        except OSError as e:
            logger.error(f"Unreadable {label} at {path}: {e}")
            raise MarketplaceCatalogError(f"Could not read {label}: {e}")
        if not isinstance(data, dict):
            raise MarketplaceCatalogError(
                f"{label} does not hold a JSON object."
            )
        return data

    @staticmethod
    def _mark_unsupported(plugin: dict, reason: str) -> dict:
        """Flag a parsed entry as one this platform cannot install."""
        plugin["supported"] = False
        plugin["unsupported_reason"] = reason
        return plugin

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
        marketplace_data = LLMPluginService._read_catalog_file(
            marketplace_file, ".claude-plugin/marketplace.json"
        )

        # Extract marketplace metadata. The Claude Code marketplace schema
        # names the maintainer under ``owner`` (the official marketplace does);
        # ``author`` is the older spelling this parser grew up on, and a bare
        # string under either is a name.
        owner = marketplace_data.get("owner")
        if not isinstance(owner, (dict, str)) or not owner:
            owner = marketplace_data.get("author")
        if isinstance(owner, dict):
            owner_name, owner_email = owner.get("name"), owner.get("email")
        elif isinstance(owner, str):
            owner_name, owner_email = owner, None
        else:
            owner_name, owner_email = None, None
        metadata = {
            "name": marketplace_data.get("name"),
            "description": marketplace_data.get("description"),
            "owner_name": owner_name,
            "owner_email": owner_email,
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

            name = plugin.get("name", "")
            parsed_plugin = {
                "name": name,
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
            # The declared path becomes the container's clone subdir, so it is
            # checked here like every other format's. An entry that escapes the
            # repository is listed with its reason and refused at install,
            # rather than being handed to the container to fail on at every
            # sync of every agent that has it.
            safe_path = LLMPluginService._safe_entry_path(
                source_path, entry_name=name
            )
            if safe_path is None:
                LLMPluginService._mark_unsupported(parsed_plugin, "unsafe_path")
                parsed_plugin["source_path"] = ""
            elif source_type == PluginSourceType.local and not safe_path:
                # A local entry that names no path would resolve to the whole
                # marketplace repository. Saying so here is what keeps the row
                # honest: the manifest skips an entry it cannot fetch, and a
                # skip nobody was told about is an install that quietly stops
                # working. (An entry whose root really is the plugin writes
                # ``"."``, which survives the check above.)
                LLMPluginService._mark_unsupported(parsed_plugin, "unknown_source")
            else:
                parsed_plugin["source_path"] = safe_path
            if parsed_plugin["name"]:
                parsed_plugins.append(parsed_plugin)

        return {"metadata": metadata, "plugins": parsed_plugins}

    # -- Codex format ------------------------------------------------------

    @staticmethod
    def _parse_codex_marketplace(repo_path: str) -> dict:
        """Parse a Codex-format marketplace repository.

        Expected structure::

            .agents/plugins/marketplace.json     # the catalog
            <plugin>/.codex-plugin/plugin.json   # per-plugin manifest (local)
            <plugin>/skills/<name>/SKILL.md      # the skills it ships

        Four source kinds appear in the wild; three of them map onto the two
        source types this platform already fetches:

        * ``local`` — a subdirectory of the marketplace repo;
        * ``url`` — an external repo whose root *is* the plugin;
        * ``git-subdir`` — an external repo with the plugin in a subdirectory,
          which is the same ``url`` source type carrying a ``source_path``
          (:meth:`_resolve_plugin_git_coords` turns that into the clone's
          ``subdir``);
        * ``npm`` — recorded as an entry, marked unsupported: the container
          installs from git and archives, never from a package registry.

        Anything else is listed too, as ``unknown_source``. An entry the
        platform cannot install is never dropped: an administrator whose
        repository is half-usable needs to see which half and why, and a
        silently shorter list says neither.

        Nothing here executes repository content — two JSON files are read and
        a directory is listed. ``policy`` is stored verbatim and never
        evaluated.
        """
        marketplace_file = os.path.join(
            repo_path, ".agents", "plugins", "marketplace.json"
        )
        marketplace_data = LLMPluginService._read_catalog_file(
            marketplace_file, ".agents/plugins/marketplace.json"
        )

        interface = marketplace_data.get("interface")
        interface = interface if isinstance(interface, dict) else {}
        author = marketplace_data.get("author")
        author = author if isinstance(author, dict) else {}
        metadata = {
            "name": marketplace_data.get("name") or interface.get("displayName"),
            "description": (
                marketplace_data.get("description") or interface.get("description")
            ),
            "owner_name": author.get("name"),
            "owner_email": author.get("email"),
        }

        parsed_plugins: list[dict] = []
        for entry in marketplace_data.get("plugins", []) or []:
            if not isinstance(entry, dict):
                continue
            name = (entry.get("name") or "").strip()
            if not name:
                continue
            parsed_plugins.append(
                LLMPluginService._parse_codex_entry(repo_path, entry, name)
            )

        return {"metadata": metadata, "plugins": parsed_plugins}

    @staticmethod
    def _parse_codex_entry(repo_path: str, entry: dict, name: str) -> dict:
        """One ``.agents/plugins/marketplace.json`` entry as plugin data."""
        source = entry.get("source")
        source = source if isinstance(source, dict) else {}
        kind = (source.get("source") or "").strip()

        plugin: dict = {
            "name": name,
            "description": entry.get("description") or "",
            "version": entry.get("version") or "",
            "author_name": "",
            "author_email": "",
            "category": entry.get("category") or "",
            "homepage": entry.get("homepage") or "",
            "source_path": "",
            "source_type": PluginSourceType.local,
            "source_url": None,
            "source_branch": "main",
            "source_commit_hash": None,
            # The entry verbatim — ``policy`` and everything else the catalog
            # author wrote. Stored, never evaluated.
            "config": dict(entry),
            "supported": True,
            "unsupported_reason": None,
        }

        if kind == "local":
            path = LLMPluginService._safe_entry_path(
                source.get("path") or "", entry_name=name
            )
            if path is None:
                return LLMPluginService._mark_unsupported(plugin, "unsafe_path")
            if not path:
                # No path at all: the entry would resolve to the marketplace
                # repository itself. Installing a whole catalog as one plugin
                # is never what the author meant.
                return LLMPluginService._mark_unsupported(plugin, "unknown_source")
            plugin["source_path"] = path
            LLMPluginService._apply_codex_plugin_manifest(repo_path, plugin)
        elif kind in ("url", "git-subdir"):
            url = source.get("url")
            if not url:
                return LLMPluginService._mark_unsupported(plugin, "unknown_source")
            plugin["source_type"] = PluginSourceType.url
            plugin["source_url"] = url
            plugin["source_branch"] = source.get("ref") or "main"
            plugin["source_commit_hash"] = source.get("sha")
            if kind == "git-subdir":
                # The plugin lives in a subdirectory of the external repo; the
                # coordinate builder passes it to the container as the clone's
                # ``subdir``.
                path = LLMPluginService._safe_entry_path(
                    source.get("path") or "", entry_name=name
                )
                if path is None:
                    return LLMPluginService._mark_unsupported(
                        plugin, "unsafe_path"
                    )
                plugin["source_path"] = path
        elif kind == "npm":
            return LLMPluginService._mark_unsupported(plugin, "npm_source")
        else:
            return LLMPluginService._mark_unsupported(plugin, "unknown_source")

        return plugin

    @staticmethod
    def _apply_codex_plugin_manifest(repo_path: str, plugin: dict) -> None:
        """Fill a local Codex entry from its ``.codex-plugin/plugin.json``.

        The catalog entry names the plugin; the plugin's own manifest describes
        it. Mutates ``plugin`` in place, and is a no-op when the manifest is
        absent or unreadable — a plugin without one is still installable, it is
        just thinner in the list.

        Two verdicts come from the manifest:

        * a manifest declaring only ``apps`` is an app connector, not an agent
          plugin — this platform does not register those, so the entry is
          listed as ``app_connector_only`` rather than offered and then failing
          in the container;
        * the skill folders the plugin ships are listed into ``config["skills"]``
          at sync time, because the repository is a throwaway clone and nobody
          can look again later. That key is *derived*, and it replaces whatever
          the catalog author wrote under the same name. It is written only for
          ``local`` entries — a ``url`` / ``git-subdir`` plugin lives in a repo
          this sync never clones, so its absence means "not known", never
          "ships no skills".
        """
        repo_root = Path(repo_path).resolve()
        plugin_dir = repo_root / plugin["source_path"]
        # The declared path is already free of ``..``; a *symlink* inside the
        # clone could still point out of it, and the two files read below would
        # then surface content from outside the repository in the admin's list.
        try:
            if not plugin_dir.resolve().is_relative_to(repo_root):
                logger.warning(
                    f"Plugin directory for '{plugin['name']}' escapes the "
                    f"marketplace clone; not reading its manifest"
                )
                LLMPluginService._mark_unsupported(plugin, "unsafe_path")
                return
        except OSError:
            return
        manifest_file = plugin_dir / ".codex-plugin" / "plugin.json"

        manifest: dict = {}
        if manifest_file.is_file():
            try:
                manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
                # Unreadable, not unsupported: a plugin with a broken manifest
                # still installs, it is just thinner in the list. Only an
                # actual verdict (an apps-only manifest) marks an entry.
                logger.warning(
                    f"Unreadable .codex-plugin/plugin.json for "
                    f"'{plugin['name']}': {e}"
                )
                manifest = {}
        if not isinstance(manifest, dict):
            manifest = {}

        if manifest:
            author = manifest.get("author")
            if isinstance(author, dict):
                plugin["author_name"] = author.get("name") or ""
                plugin["author_email"] = author.get("email") or ""
            elif author:
                plugin["author_name"] = str(author)
            plugin["description"] = (
                manifest.get("description") or plugin["description"]
            )
            plugin["version"] = manifest.get("version") or plugin["version"]
            plugin["homepage"] = manifest.get("homepage") or plugin["homepage"]

            config = plugin["config"]
            if isinstance(manifest.get("interface"), dict):
                config["interface"] = manifest["interface"]
            if manifest.get("policy") is not None and "policy" not in config:
                config["policy"] = manifest["policy"]

            declares_apps = bool(manifest.get("apps"))
            declares_anything_else = any(
                manifest.get(key)
                for key in ("skills", "mcpServers", "hooks", "commands", "agents")
            )
            if declares_apps and not declares_anything_else:
                plugin["supported"] = False
                plugin["unsupported_reason"] = "app_connector_only"

        plugin["config"]["skills"] = LLMPluginService._list_skill_folders(plugin_dir)

    @staticmethod
    def _list_skill_folders(plugin_dir: Path) -> list[str]:
        """Names of ``<plugin>/skills/<name>/`` folders that hold a SKILL.md.

        Names only: this is the count and the labels the admin table shows, not
        a validated index. Validation is the environment's job, on the copy it
        actually installed.
        """
        skills_dir = plugin_dir / "skills"
        # ``is_dir()`` follows a symlink, and the children of the *target* are
        # ordinary directories that a per-child symlink check would never
        # catch — so the root is where this has to be refused.
        if skills_dir.is_symlink() or not skills_dir.is_dir():
            return []
        try:
            children = sorted(skills_dir.iterdir(), key=lambda p: p.name)
        except OSError:
            return []
        return [
            child.name
            for child in children
            # Symlinks are skipped, not followed: the clone is untrusted.
            if not child.is_symlink()
            and child.is_dir()
            and (child / "SKILL.md").is_file()
        ]

    # -- Bare-skills format ------------------------------------------------

    @staticmethod
    def _parse_skills_marketplace(repo_path: str) -> dict:
        """Parse a repository that is simply a collection of skills.

        There is no catalog file to read — the directory layout *is* the
        catalog, in one of the two shapes public skill collections use::

            skills/<name>/SKILL.md      # the conventional layout
            <name>/SKILL.md             # a repo that is nothing but skills

        Each skill folder becomes one plugin entry, because that is the unit a
        user installs: an agent gets the one skill it asked for, not the whole
        repository. The environment lands such an entry at
        ``plugins/<marketplace>/<name>/skills/<name>/`` so the on-disk shape
        matches a catalog install (``plugin_type="skills"`` is what tells it
        to).

        A folder whose ``SKILL.md`` does not validate is still listed, marked
        ``no_skill_md``: the administrator needs to see that the repository is
        half-broken, and an upstream fix re-syncs it into a supported row with
        no further action here. Finding **no folder at all** is the other
        answer entirely — the catalog could not be read — and raises
        :class:`MarketplaceCatalogError` rather than returning an empty list
        that would delete every row of the marketplace.

        Validation reuses :mod:`app.services.agents.skill_manifest` — the same
        parser the environment and the publish path use, so "valid skill" means
        one thing on this platform, not three.

        No marketplace name is returned. A repository like this carries nothing
        authoritative to name itself with (only a README heading), and
        ``marketplace.name`` is not a label: it is the on-disk directory every
        install resolves through (``plugins/<marketplace>/<plugin>/``). Letting
        an upstream README edit rename it would move every install's plugin
        directory on the next sync. The name chosen when the marketplace was
        registered stands.
        """
        repo_root = Path(repo_path)
        skills_root = repo_root / "skills"
        # A symlinked ``skills`` is not the nested layout: ``is_dir()`` follows
        # it, and everything inside the target is an ordinary directory that
        # the per-child symlink guard below would happily walk — in the nested
        # layout a child needs no SKILL.md to be listed, so ``skills -> /``
        # would enumerate the host's top-level directories into the admin's
        # list. Falling through to the flat layout skips it entirely.
        nested = skills_root.is_dir() and not skills_root.is_symlink()
        root = skills_root if nested else repo_root
        rel_prefix = "skills/" if nested else ""

        try:
            children = sorted(root.iterdir(), key=lambda p: p.name)
        except OSError as e:
            logger.error(f"Unreadable skills repository at {root}: {e}")
            raise MarketplaceCatalogError(
                f"The repository could not be listed: {e}"
            )

        parsed_plugins: list[dict] = []
        for child in children:
            # A symlink is skipped rather than followed: the clone is
            # untrusted content, and ``skills/x -> /`` would otherwise have the
            # parser read — and surface in a description — files from outside
            # the repository. (``parse_skill_dir`` refuses one too; this keeps
            # the flat layout's own ``SKILL.md`` probe below from following it
            # either.)
            if (
                child.name.startswith(".")
                or child.is_symlink()
                or not child.is_dir()
            ):
                continue
            # In the flat layout every top-level directory is a candidate, and
            # most of them (docs/, images/, scripts/) are not skills at all —
            # so a directory only counts there when it actually holds a
            # SKILL.md. Under ``skills/`` the intent is explicit: every child
            # is meant to be a skill, and one without a SKILL.md is a broken
            # skill worth reporting rather than an unrelated folder.
            if not nested and not (child / "SKILL.md").is_file():
                continue

            rel_path = f"{rel_prefix}{child.name}"
            entry = parse_skill_dir(child, rel_path=rel_path)
            valid = entry.error is None
            parsed_plugins.append(
                {
                    "name": child.name,
                    "description": entry.description if valid else "",
                    "version": "",
                    "author_name": "",
                    "author_email": "",
                    "category": "skill",
                    "homepage": "",
                    "source_path": rel_path,
                    "source_type": PluginSourceType.local,
                    "source_url": None,
                    "source_branch": "main",
                    "config": {
                        "skill": {
                            "name": child.name,
                            "description": entry.description if valid else "",
                            "has_scripts": bool(entry.has_scripts),
                        }
                    },
                    "supported": valid,
                    "unsupported_reason": None if valid else "no_skill_md",
                }
            )

        if not parsed_plugins:
            # This format has no catalog file; the directory layout *is* the
            # catalog, so "not one skill folder anywhere" is the same finding a
            # missing marketplace.json would be — the repository moved, was
            # renamed, or was never a skills repository. Refusing here is what
            # keeps a transient upstream rename from deleting every row (and
            # permanently orphaning every install of them).
            #
            # A repository that is half-broken is a *different* answer and
            # still syncs: a folder whose SKILL.md does not validate is listed
            # as ``no_skill_md``, which is a row, not an empty list. Zero
            # supported rows is a connected marketplace; zero rows at all is
            # not.
            where = "under skills/" if nested else "at the repository root"
            raise MarketplaceCatalogError(
                f"No skill folders found {where} — nothing was read, so no "
                f"plugin was changed."
            )

        return {"metadata": {}, "plugins": parsed_plugins}

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
                plugin.source_commit_hash = plugin_data.get("source_commit_hash")
                plugin.config = plugin_data.get("config")
                plugin.commit_hash = commit_hash
                # The verdict is re-derived on every sync, so an upstream fix
                # (a real SKILL.md, a source kind we can fetch) flips the row
                # back to installable without anybody re-adding the
                # marketplace.
                plugin.plugin_type = marketplace.type
                plugin.supported = bool(plugin_data.get("supported", True))
                plugin.unsupported_reason = plugin_data.get("unsupported_reason")
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
                    source_commit_hash=plugin_data.get("source_commit_hash"),
                    plugin_type=marketplace.type,
                    config=plugin_data.get("config"),
                    commit_hash=commit_hash,
                    supported=bool(plugin_data.get("supported", True)),
                    unsupported_reason=plugin_data.get("unsupported_reason"),
                )
                session.add(plugin)

        # Remove plugins no longer in marketplace. Any agent that installed one
        # keeps its link (``plugin_id`` becomes NULL, per the column's
        # ``ON DELETE SET NULL``): an upstream repo dropping an entry is not
        # consent to uninstall it from somebody's agent.
        for name, plugin in existing_plugins.items():
            if name not in new_plugin_names:
                logger.info(f"Removing plugin '{name}' from marketplace")
                session.delete(plugin)

        session.commit()

        # ...and the other direction: an entry that comes back adopts the
        # installs it left behind. Opportunistic repair, never part of the
        # sync contract: the plugin rows above are committed, and a failure
        # here (a link installed between the read and the write, taking the
        # (agent, plugin) index with it) must not turn a good sync into a
        # failed one.
        try:
            LLMPluginService._reattach_orphaned_links(session, marketplace)
        except Exception as e:
            session.rollback()
            logger.exception(
                f"Could not re-attach orphaned links for marketplace "
                f"'{marketplace.name}': {e}"
            )

    @staticmethod
    def _rename_link_snapshots(
        session: Session, marketplace: LLMPluginMarketplace, previous_name: str
    ) -> int:
        """Move live installs' frozen marketplace name with the marketplace's.

        ``snapshot_marketplace_name`` is written once at install and is the
        install's *directory* identity — ``plugins/<marketplace>/<plugin>/`` —
        which the environment resolves through ``marketplace.name`` live. A
        sync rewrites that name from repository metadata, so an upstream rename
        moves the directory while every install still names the old one. Two
        things then quietly break: the addons projection folds an orphan's
        skills on the stale pair and splits one directory into two rows, and
        :meth:`_reattach_orphaned_links` can no longer recognise its own
        installs.

        Only **live** links are moved — the ones whose ``plugin_id`` still
        points into this marketplace, which is what proves they came from it.
        A link already orphaned under the old name carries no proof of origin
        and is left alone rather than adopted on a name that is now free.

        Returns the number of links moved.
        """
        if not previous_name or previous_name == marketplace.name:
            return 0

        plugin_ids = [
            plugin.id
            for plugin in session.exec(
                select(LLMPluginMarketplacePlugin).where(
                    LLMPluginMarketplacePlugin.marketplace_id == marketplace.id
                )
            ).all()
        ]
        if not plugin_ids:
            return 0

        links = session.exec(
            select(AgentPluginLink).where(
                AgentPluginLink.plugin_id.in_(plugin_ids),
                AgentPluginLink.snapshot_marketplace_name == previous_name,
            )
        ).all()
        for link in links:
            link.snapshot_marketplace_name = marketplace.name
            session.add(link)
        if links:
            session.commit()
            logger.info(
                f"Moved {len(links)} install(s) from marketplace directory "
                f"'{previous_name}' to '{marketplace.name}'"
            )
        return len(links)

    @staticmethod
    def _reattach_orphaned_links(
        session: Session, marketplace: LLMPluginMarketplace
    ) -> int:
        """Re-point orphaned installs at a plugin row that has reappeared.

        A link outlives its plugin row (``plugin_id`` is ``ON DELETE SET
        NULL``), which is what keeps an admin's catalog delete — or an upstream
        entry dropped for one sync — from uninstalling somebody else's plugin.
        The half that was missing is the way back: without it the row is
        ``source_unavailable`` **forever**, its files pruned on the next sync,
        and re-installing only stacks a second link beside the dead one.

        One of the things matched on is the install's *directory* identity,
        ``<marketplace>/<plugin>`` — the same pair
        :meth:`~app.services.agents.addons_service.AddonsService._link_ref`
        folds skills on, written by the install path and backfilled by
        migration ``c8d2e5b71a04``. It is not sufficient on its own, and was
        never meant to be read that way: the full rule is the three numbered
        conditions below. A link carrying no identity to match — no names, or no
        repository — stays orphaned; nothing else can be said about it
        truthfully.

        An agent that already holds a live link to the returning plugin keeps
        its dead one untouched: the unique index on ``(agent_id, plugin_id)``
        forbids the second row, and the owner — not a sync — decides which of
        the two to uninstall. (Re-attaching to an entry the last sync marked
        ``supported=false`` is allowed and inert: the manifest still omits it
        and the projection still reports ``source_unavailable`` — the row is
        named and upgradable again the moment upstream fixes it.)

        **The name is not proof that an install came from this marketplace**, so
        it is not what adoption turns on. A name is globally unique at any
        instant but freely transferable: ``idx_marketplace_name_unique`` frees it
        exactly when its holder is deleted — the state that produced these
        orphans — and ``sync_marketplace`` reassigns ``marketplace.name`` from
        the repository's own ``marketplace.json`` on every sync. So a name can
        move to another row, in either age direction.

        Adoption therefore requires **all three** conditions. They are not
        ranked: condition 2 is the only one that can defeat a *rename* — 1 and 3
        both pass in that case — while condition 3 does most of the excluding in
        practice, as the closing paragraph works through.

        1. the directory identity matches — ``snapshot_marketplace_name`` and
           ``snapshot_plugin_name``, which is what makes the row nameable and
           what the environment has on disk;
        2. ``snapshot_repository_url`` names the **same repository** as this
           marketplace (:meth:`_same_repository`) — the identity a rename cannot
           transfer, because it is where the code would actually be fetched from;
        3. ``link.created_at >= marketplace.created_at`` — kept, not replaced. An
           install cannot predate the marketplace it was made from, and dropping
           a cheap independent condition because a stronger one arrived is how a
           single mistake in the stronger one becomes total.

        **Null fails closed.** A link with no ``snapshot_repository_url`` is
        never adopted
        (``test_an_install_with_no_recorded_repository_is_never_adopted``). The
        shape that reaches this code is a marketplace install made before the
        column existed *and* already orphaned before migration
        ``d7b41e0c9a35`` ran, so there was no live marketplace row left to
        backfill a URL from. (An install whose marketplace was unresolvable at
        install time also stores no URL, but it stores no marketplace *name*
        either, so it fails condition 1 first and never reaches the comparison.)
        Such links stay orphaned permanently: the row is named, reported
        ``source_unavailable``, and its remedy is uninstall and re-install, which
        re-creates the link with a URL. Guessing a repository for them from a
        name is precisely the confusion the column ends.

        **All three hold at once, and condition 3 is stricter than it reads.**
        An install is created after the marketplace it was made from, so only a
        marketplace row *older than the link* can ever adopt it — the URL is not
        consulted at all until that is true. Two paths therefore lead to an
        adoption, and both are pinned:

        * an entry that disappeared upstream and came back, on a marketplace row
          that was never deleted
          (``test_an_entry_that_comes_back_adopts_the_install_it_left_behind``,
          Scenario 6);
        * an older marketplace row renamed onto the freed name **and** pointing
          at the same repository
          (``test_the_same_repository_under_the_freed_name_adopts_its_orphans``).

        What is **not** adopted, each deliberately:

        * a delete-and-re-add of the very same repository. The replacement row
          postdates the link, so condition 3 excludes it before the URL is ever
          compared — the matching URL does not save it
          (``test_a_marketplace_registered_after_the_install_never_adopts_it``);
        * a *different* repository under a reused name, older row or younger —
          the takeover this whole guard exists for
          (``test_an_older_marketplace_taking_the_freed_name_does_not_capture_the_install``);
        * any spelling of the URL that :meth:`_same_repository` will not
          normalise together: host casing, ``http`` vs ``https``, a private
          host's SSH form. That last one is a property of that method rather
          than a scenario, and is pinned only where it overlaps a scenario — the
          adoption test above spells the two URLs with and without ``.git``; the
          refusals are documented on :meth:`_same_repository` itself.

        Every one of those re-installs by hand, which is the safe direction to
        fail. The named tests live in
        ``backend/tests/api/agents/core/agents_addons_projection_test.py``
        (Scenario 9 unless noted), and every claim above about what *is* or *is
        not* adopted is one of them — because the bug that produced this guard
        was a docstring promising more than its guard delivered.

        Returns the number of links re-attached.
        """
        plugins = {
            plugin.name: plugin
            for plugin in session.exec(
                select(LLMPluginMarketplacePlugin).where(
                    LLMPluginMarketplacePlugin.marketplace_id == marketplace.id
                )
            ).all()
        }
        if not plugins or not marketplace.name:
            return 0

        orphans = session.exec(
            select(AgentPluginLink).where(
                AgentPluginLink.plugin_id.is_(None),
                # Only a marketplace that already existed when the install was
                # made can be the one it was installed from.
                AgentPluginLink.created_at >= marketplace.created_at,
                # Bundle and catalog links carry ``plugin_id IS NULL`` by
                # design — they are snapshot-identified and have no marketplace
                # row to point at. Only a marketplace link can be orphaned.
                AgentPluginLink.source == PluginSource.marketplace,
                AgentPluginLink.snapshot_marketplace_name == marketplace.name,
                AgentPluginLink.snapshot_plugin_name.in_(list(plugins)),
                # Fail closed on a link with no recorded repository: there is
                # nothing to verify it against, and it must not be adopted on the
                # strength of its name. Asserted in SQL as well as below so the
                # candidate set never even contains one.
                AgentPluginLink.snapshot_repository_url.is_not(None),
            )
        ).all()
        # The repository comparison itself is done here rather than in SQL: it
        # normalises (SSH → HTTPS, trailing ``/`` and ``.git``), which no ``=``
        # in Postgres would do, and the candidate set is already narrowed to one
        # marketplace's names.
        orphans = [
            link
            for link in orphans
            if LLMPluginService._same_repository(
                link.snapshot_repository_url, marketplace.url
            )
        ]
        if not orphans:
            return 0

        taken = {
            (agent_id, plugin_id)
            for agent_id, plugin_id in session.exec(
                select(AgentPluginLink.agent_id, AgentPluginLink.plugin_id).where(
                    AgentPluginLink.plugin_id.in_(
                        [plugin.id for plugin in plugins.values()]
                    )
                )
            ).all()
        }

        reattached = 0
        for link in orphans:
            plugin = plugins.get(link.snapshot_plugin_name)
            if plugin is None or (link.agent_id, plugin.id) in taken:
                continue
            link.plugin_id = plugin.id
            link.updated_at = datetime.now(UTC)
            session.add(link)
            taken.add((link.agent_id, plugin.id))
            reattached += 1
            logger.info(
                f"Re-attached orphaned plugin link {link.id} to "
                f"'{marketplace.name}/{plugin.name}'"
            )

        if reattached:
            session.commit()
        return reattached

    # ==========================================================================
    # Plugin Discovery
    # ==========================================================================

    @staticmethod
    def discover_plugins(
        session: Session,
        user_id: uuid.UUID,
        search: str | None = None,
        category: str | None = None,
        plugin_type: str | None = None,
        marketplace_id: uuid.UUID | None = None,
        skip: int = 0,
        limit: int = 30
    ) -> tuple[list[LLMPluginMarketplacePluginPublic], int]:
        """
        Discover available plugins for a user.

        Unsupported entries are returned as they are, ordered last: the picker
        shows them with the reason so a user understands why the plugin they
        came for is not offered, instead of searching for something that looks
        absent. The install route is what refuses them.

        Args:
            session: Database session
            user_id: User ID
            search: Optional search term for name/description/author/category
            category: Optional category filter
            plugin_type: Optional format filter, compared against the raw
                ``marketplace.type`` value copied onto the row — normally
                ``claude`` | ``codex`` | ``skills``, but a legacy row can hold a
                word from before that vocabulary (the addons projection clamps
                such a row to ``claude`` for display, which this filter does
                not, so the two disagree about it). No shipped client sends the
                filter: the Add Addon dialog lists every format in one result
                set and labels each row from the format it comes back with.
            marketplace_id: Optional scope to one marketplace — the admin
                marketplace detail page lists exactly that marketplace's
                entries and their ``supported`` verdict, a question the
                server-wide list can only answer by paging through everything
                else. A marketplace this caller cannot see (or that does not
                exist) is an empty page, not an error: discovery never reports
                on the existence of a private marketplace.
            skip: Number of items to skip (pagination offset)
            limit: Maximum number of items to return

        Returns:
            Tuple of (list of discoverable plugins, total count)
        """
        # Get accessible marketplaces
        marketplaces = LLMPluginService.list_marketplaces(session, user_id, include_public=True)
        marketplace_ids = [m.id for m in marketplaces]
        marketplace_names = {m.id: m.name for m in marketplaces}
        marketplace_rows = {m.id: m for m in marketplaces}

        if not marketplace_ids:
            return [], 0

        if marketplace_id is not None:
            if marketplace_id not in set(marketplace_ids):
                return [], 0
            marketplace_ids = [marketplace_id]

        # Query plugins from accessible marketplaces
        statement = select(LLMPluginMarketplacePlugin).where(
            LLMPluginMarketplacePlugin.marketplace_id.in_(marketplace_ids)
        )

        if category:
            statement = statement.where(LLMPluginMarketplacePlugin.category == category)
        if plugin_type:
            statement = statement.where(
                LLMPluginMarketplacePlugin.plugin_type == plugin_type
            )

        plugins = list(session.exec(statement).all())

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

        # Installable first, then by name. Ordering is applied before the page
        # is cut so an unsupported entry never pushes an installable one off
        # the first page.
        plugins.sort(key=lambda p: (not p.supported, (p.name or "").lower()))

        # Apply pagination
        plugins = plugins[skip:skip + limit]

        # Convert to public schema with marketplace name
        result = [
            LLMPluginService.get_plugin_public(
                p,
                marketplace_name=marketplace_names.get(p.marketplace_id),
                marketplace=marketplace_rows.get(p.marketplace_id),
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
        marketplace: LLMPluginMarketplace | None = None,
    ) -> LLMPluginMarketplacePluginPublic:
        """Project a marketplace plugin to its public schema.

        Parity with ``get_marketplace_public`` / ``_link_to_public``. When
        ``marketplace_name`` is omitted it is read from the plugin's marketplace
        relationship; ``marketplace`` lets a list caller pass the row it already
        holds so the owner and repository fields do not lazy-load per plugin.
        """
        if marketplace is None:
            marketplace = plugin.marketplace
        if marketplace_name is None:
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
            supported=plugin.supported,
            unsupported_reason=plugin.unsupported_reason,
            skill_summary=LLMPluginService._skill_summary(plugin),
            marketplace_name=marketplace_name,
            marketplace_owner=LLMPluginService.marketplace_owner_label(marketplace),
            repository_url=LLMPluginService.plugin_repository_url(
                plugin, marketplace.url if marketplace else None
            ),
        )

    @staticmethod
    def marketplace_owner_label(
        marketplace: LLMPluginMarketplace | None,
    ) -> str | None:
        """The marketplace's ``owner`` as one label: name, else email."""
        if marketplace is None:
            return None
        return marketplace.owner_name or marketplace.owner_email or None

    @staticmethod
    def plugin_repository_url(
        plugin: LLMPluginMarketplacePlugin | None,
        marketplace_url: str | None,
    ) -> str | None:
        """Where an entry's source lives, as a URL a browser can open.

        The most specific thing on record wins: the manifest's ``homepage``
        (the official marketplace points each entry at its own folder), then a
        ``url``-sourced entry's own repository, then the marketplace repository
        itself. Only ``http(s)`` comes back — the marketplace URL is often the
        SSH form it was registered with, which is rewritten for the public
        hosts by the same helper the clone path uses and dropped otherwise, so
        a private host's ``git@`` address is never handed to an anchor tag.
        """
        candidates: list[str | None] = []
        if plugin is not None:
            candidates.append(plugin.homepage)
            if plugin.source_type == PluginSourceType.url:
                candidates.append(plugin.source_url)
        candidates.append(marketplace_url)
        for candidate in candidates:
            if not candidate:
                continue
            normalized = (
                LLMPluginService._normalize_public_git_url(candidate) or ""
            ).strip()
            if normalized.startswith(("https://", "http://")):
                return normalized
        return None

    @staticmethod
    def _skill_summary(
        plugin: LLMPluginMarketplacePlugin,
    ) -> PluginSkillSummary | None:
        """The entry's single skill, when it has exactly one.

        Only a ``skills``-format entry does: its parser writes the block. A
        Codex plugin ships a *list* of skill folders (``config["skills"]``),
        which is a count rather than a description, so it has no summary.
        Config is repository-authored JSON, so every field is validated here
        rather than trusted.
        """
        config = plugin.config or {}
        block = config.get("skill") if isinstance(config, dict) else None
        if not isinstance(block, dict):
            return None
        name = block.get("name")
        if not isinstance(name, str) or not name:
            return None
        description = block.get("description")
        return PluginSkillSummary(
            name=name,
            description=description if isinstance(description, str) else "",
            has_scripts=bool(block.get("has_scripts")),
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
            HTTPException(409): the marketplace entry is not installable by
                this platform (``plugin_unsupported``).
        """
        # Check if plugin exists
        plugin = LLMPluginService.get_plugin(session, data.plugin_id)
        if not plugin:
            raise ValueError(f"Plugin {data.plugin_id} not found")

        # Refuse before the link exists, not after. An unsupported entry is
        # visible in discovery on purpose (with its reason), so the click is
        # reachable; what must not happen is a row in somebody's install list
        # that the container will fail on at every sync from here on.
        if not plugin.supported:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "plugin_unsupported",
                    "message": UNSUPPORTED_REASON_SENTENCES.get(
                        plugin.unsupported_reason or "", _UNSUPPORTED_FALLBACK
                    ),
                    "reason": plugin.unsupported_reason,
                },
            )

        # Check if already installed
        existing = session.exec(
            select(AgentPluginLink).where(
                AgentPluginLink.agent_id == agent_id,
                AgentPluginLink.plugin_id == data.plugin_id
            )
        ).first()

        if existing:
            raise ValueError(f"Plugin {plugin.name} is already installed for this agent")

        # Create link. The two snapshot names are written even though this
        # source resolves its identity live: they are the *directory* identity
        # (``plugins/<marketplace>/<plugin>/``), and the day the plugin row is
        # gone — a deleted marketplace, an entry dropped upstream — they are
        # all that is left to name the row and to match it against what the
        # environment still has on disk. Without them such a link renders
        # nameless and its skills land on a second, orphan row: one directory,
        # two rows. The catalog install path writes the same pair for the same
        # reason.
        marketplace = plugin.marketplace
        link = AgentPluginLink(
            agent_id=agent_id,
            plugin_id=data.plugin_id,
            source=PluginSource.marketplace,
            snapshot_marketplace_name=marketplace.name if marketplace else None,
            snapshot_plugin_name=plugin.name,
            # The repository this install is actually made from, snapshotted for
            # the same reason as the names but answering a different question:
            # the names say which directory, this says whose code. A name is
            # transferable — freed by a delete, reassigned by a sync from the
            # repo's own marketplace.json — so re-adopting an orphaned link on
            # the name alone would let an unrelated repository deliver code into
            # this agent's container. ``_reattach_orphaned_links`` requires this
            # to match, and refuses a link that has none.
            snapshot_repository_url=marketplace.url if marketplace else None,
            # The frozen plugin.json alongside the frozen names, for the same
            # reason: when the live row is gone this is the only description of
            # the plugin left — and it is what a bundle published from this
            # agent ships to consumers for display.
            snapshot_config=plugin.config,
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
            # Only marketplace links resolve a live plugin row; every other
            # source is snapshot-identified.
            is_marketplace = link.source == PluginSource.marketplace
            plugin = link.plugin if is_marketplace else None
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
            skill_package_id = None
            # ``or None``: a manifest with no version syncs as ``""``, and an
            # empty string is a version to a client that only checks for null.
            latest_version = (plugin.version or None) if plugin else None

            # Catalog links have no marketplace row at all, so the fields above
            # would leave the row nameless. Project them from the live package
            # (its display name and blurb, which the publisher can edit without
            # re-publishing) and take the update signal from the package's
            # latest revision rather than from a commit hash it does not have.
            if link.source == PluginSource.catalog:
                from app.models.skills.skill_package_revision import (
                    SkillPackageRevision,
                )
                from app.services.skills.skill_catalog_service import (
                    SkillCatalogService,
                )

                package = SkillCatalogService.package_of_link(session, link)
                if package is not None:
                    skill_package_id = package.id
                    display_name = package.display_name or package.name
                    display_desc = package.description or display_desc
                    has_update = (
                        package.latest_revision_id is not None
                        and package.latest_revision_id
                        != link.skill_package_revision_id
                    )
                    if package.latest_revision_id:
                        latest = session.get(
                            SkillPackageRevision, package.latest_revision_id
                        )
                        latest_version = (latest.version or None) if latest else None
                # "skill" rather than the package's own (absent) category: the
                # tab groups rows by what they are, and a catalog row is always
                # one skill.
                display_cat = display_cat or "skill"

            result.append(AgentPluginLinkWithUpdateInfo(
                id=link.id,
                agent_id=link.agent_id,
                plugin_id=link.plugin_id,
                source=link.source,
                snapshot_marketplace_name=link.snapshot_marketplace_name,
                snapshot_plugin_name=link.snapshot_plugin_name,
                snapshot_config=link.snapshot_config,
                skill_package_revision_id=link.skill_package_revision_id,
                installed_version=link.installed_version,
                installed_commit_hash=link.installed_commit_hash,
                conversation_mode=link.conversation_mode,
                building_mode=link.building_mode,
                disabled=link.disabled,
                created_at=link.created_at,
                updated_at=link.updated_at,
                has_update=has_update,
                latest_version=latest_version,
                latest_commit_hash=plugin.commit_hash if plugin else None,
                plugin_name=display_name,
                plugin_description=display_desc,
                plugin_category=display_cat,
                marketplace_name=display_mkt,
                skill_package_id=skill_package_id,
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

        # Catalog links upgrade by re-pinning to the package's latest revision;
        # they have no marketplace commit to chase. Delegating keeps the one
        # upgrade route working for every source without the route knowing
        # which it is holding.
        if link.source == PluginSource.catalog:
            from app.services.skills.skill_catalog_service import (
                SkillCatalogService,
            )

            # ``SkillCatalogError`` is allowed to propagate: the route maps its
            # code to a status. Collapsing it to None here would answer "Plugin
            # link not found" for a link that plainly exists — the caller needs
            # to hear that the catalog entry behind it is gone, which is a
            # different problem with a different fix.
            return SkillCatalogService.upgrade_link(session, link)

        plugin = link.plugin
        if not plugin:
            # The link exists; what it pointed at does not. Answering "Plugin
            # link not found" here would send the caller looking for a row
            # they can plainly see. There is nothing to upgrade *to* — the
            # only remaining action is to uninstall it.
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "source_unavailable",
                    "message": (
                        "This plugin's marketplace entry no longer exists, so "
                        "there is no newer version to move to. Uninstall it to "
                        "remove it from this agent."
                    ),
                },
            )

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

        Every entry carries ``plugin_type`` (``claude`` | ``codex`` |
        ``skills``) — the shape of the files it is about to fetch. env-core
        keys exactly one decision on it: a ``skills`` entry's tree is a single
        skill folder and lands at ``<plugin_dir>/skills/<plugin>/``, everything
        else IS the plugin directory. Keep the key name and the three values;
        they are the contract with the container.

        A marketplace entry also carries the marketplace row's ``description``,
        which env-core copies into a ``.claude-plugin/plugin.json`` it has to
        synthesise (a Codex or bare-skill tree that ships none of its own).
        Without it every such plugin loads under the placeholder
        ``Skill '<name>'``.

        Per entry:
          - ``source=marketplace``: resolve git coords from the linked
            ``LLMPluginMarketplacePlugin`` (+ its marketplace):
              * ``local`` plugin -> {url: marketplace.url,
                ref: link.installed_commit_hash or plugin.commit_hash,
                subdir: plugin.source_path}
              * ``url`` plugin -> {url: plugin.source_url,
                ref: plugin.source_commit_hash or plugin.commit_hash,
                subdir: plugin.source_path} (branch as fallback ref; the
                subdir is empty unless the entry is a Codex ``git-subdir``)
          - ``source=bundle``: ``git=null``; identity from the link's snapshot
            fields (files are seeded from the bundle snapshot, no fetch).
          - ``source=catalog``: ``git=null`` + ``archive`` coordinates (URL of
            the pinned skill revision's tarball, its sha256, and the revision
            id as the idempotency ref). An orphaned link — its revision or
            package deleted — still emits an entry with ``archive=null`` so the
            container reports ``catalog_revision_missing`` as a per-plugin
            failure rather than the skill silently vanishing (§9).

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
                    # A bundle ships its plugins already shaped: the
                    # publisher's workspace tree is copied verbatim into the
                    # install, so the files are in the layout both engines read
                    # and there is nothing for env-core to re-shape. The value
                    # is constant because the fact is.
                    "plugin_type": "claude",
                    "git": None,
                    "conversation_mode": link.conversation_mode,
                    "building_mode": link.building_mode,
                    "disabled": link.disabled,
                    "version": link.installed_version,
                    "commit_hash": link.installed_commit_hash,
                }
            elif link.source == PluginSource.catalog:
                entry = LLMPluginService._build_catalog_entry(session, link)
                if entry is None:
                    continue
            else:
                # Marketplace-sourced: resolve git coordinates from DB rows.
                plugin = link.plugin
                if not plugin:
                    # ``plugin_id`` is NULL: the marketplace entry behind this
                    # install was deleted. There are no coordinates left to
                    # fetch from, so the entry is omitted and the environment
                    # prunes the directory on this sync. The link itself
                    # survives — the addons projection reports it as
                    # ``source_unavailable`` so the owner can uninstall it
                    # deliberately instead of finding it gone.
                    logger.warning(
                        f"Skipping marketplace plugin link {link.id} — its "
                        f"marketplace entry no longer exists"
                    )
                    continue
                if not plugin.supported:
                    # The last sync re-derived the verdict onto an entry that
                    # agents already have linked (upstream changed the source
                    # kind, dropped the SKILL.md, wrote a path that escapes the
                    # repo…). The 409 on install only guards NEW installs;
                    # this is the same refusal applied to an install that
                    # already exists. Omitted rather than fetched, and the
                    # addons projection reports the row as
                    # ``source_unavailable`` so the files disappearing is
                    # something the owner is told about, not something they
                    # discover.
                    #
                    # Deliberately broader than the hazard that motivated it: a
                    # row that turns ``no_skill_md`` or ``app_connector_only``
                    # upstream loses its files too, though the installed copy
                    # still worked. Refusing new installs while quietly
                    # re-fetching existing ones would be the incoherent half —
                    # the platform has said it cannot install this entry, and
                    # the projection tells the owner rather than letting the
                    # files vanish unannounced.
                    logger.warning(
                        f"Skipping plugin '{plugin.name}' — the marketplace "
                        f"entry is not installable "
                        f"({plugin.unsupported_reason or 'unsupported'})"
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
                    "plugin_type": LLMPluginService._manifest_plugin_type(
                        plugin.plugin_type
                    ),
                    # Read by env-core's manifest normaliser when the fetched
                    # tree carries no manifest of its own (a bare-skill or
                    # ``skills``-format entry): the description the marketplace
                    # declared is the only honest one available there, and
                    # without it every such plugin gets the placeholder
                    # ``Skill '<name>'``. Not retroactive — the normaliser never
                    # overwrites a manifest it already wrote, so a plugin
                    # normalised before this field existed keeps its placeholder
                    # until it is re-fetched. Nothing else consumes the key, and
                    # the addon row's description still comes from the DB row.
                    "description": plugin.description,
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

    #: The formats a manifest entry may declare. Anything else would be a value
    #: env-core has no branch for.
    _MANIFEST_PLUGIN_TYPES = ("claude", "codex", "skills")

    @staticmethod
    def _manifest_plugin_type(declared: str | None) -> str:
        """``declared`` if env-core has a branch for it, else ``claude``.

        ``plugin_type`` decides where a fetched tree lands in the plugin
        directory, so an unrecognised value is not a label the container can
        ignore — it is a shape nothing handles. The column is a plain string
        copied from ``marketplace.type``, whose documented values predate the
        ``Literal`` that constrains it today (``custom`` was one), so a legacy
        row can still hold a word from before this vocabulary existed.
        ``claude`` — the tree *is* the plugin directory — is the reading that
        was always applied to those rows.
        """
        if declared in LLMPluginService._MANIFEST_PLUGIN_TYPES:
            return declared
        return "claude"

    @staticmethod
    def _build_catalog_entry(session: Session, link: AgentPluginLink) -> dict | None:
        """Manifest entry for a ``source=catalog`` link, or None if unusable.

        The archive URL is built from ``AGENT_ENV_BACKEND_URL`` — the
        container-visible address of this backend, the same one written into
        every agent's ``.env`` as ``BACKEND_URL`` — because the consumer of
        this URL is the container, not a browser.
        """
        from app.models.skills.skill_package import CATALOG_MARKETPLACE_NAME
        from app.models.skills.skill_package_revision import SkillPackageRevision
        from app.services.skills.skill_catalog_service import SkillCatalogService

        plugin_name = link.snapshot_plugin_name
        if not plugin_name:
            logger.warning(
                f"Skipping catalog plugin link {link.id} with no package name"
            )
            return None

        revision = (
            session.get(SkillPackageRevision, link.skill_package_revision_id)
            if link.skill_package_revision_id
            else None
        )

        archive: dict | None = None
        if revision is not None:
            sha256 = SkillCatalogService.archive_sha256(revision)
            if sha256:
                base = (settings.AGENT_ENV_BACKEND_URL or "").rstrip("/")
                archive = {
                    "url": (
                        f"{base}/api/v1/skills/packages/{revision.package_id}"
                        f"/revisions/{revision.revision_number}/archive"
                    ),
                    "sha256": sha256,
                    # The idempotency marker: the container skips the download
                    # when the on-disk `.cinna_plugin_ref` already names this
                    # revision. A revision is immutable, so unlike a git branch
                    # the marker can be trusted without re-fetching.
                    "ref": str(revision.id),
                    "description": (link.snapshot_config or {}).get("description"),
                }

        return {
            "marketplace_name": link.snapshot_marketplace_name
            or CATALOG_MARKETPLACE_NAME,
            "plugin_name": plugin_name,
            "source": PluginSource.catalog.value,
            # A catalog install *is* one skill. The value is honest, and it is
            # inert here: env-core reads ``plugin_type`` only in the git-clone
            # branch, to decide where a fetched tree lands. The archive branch
            # already extracts the finished ``skills/<name>/`` layout.
            "plugin_type": "skills",
            "git": None,
            "archive": archive,
            "conversation_mode": link.conversation_mode,
            "building_mode": link.building_mode,
            "disabled": link.disabled,
            "version": link.installed_version,
            "commit_hash": None,
        }

    @staticmethod
    def _resolve_plugin_git_coords(
        link: AgentPluginLink,
        plugin: LLMPluginMarketplacePlugin,
        marketplace: LLMPluginMarketplace,
    ) -> dict | None:
        """Resolve {url, ref, subdir} for a marketplace plugin.

        For ``local`` plugins the files live in the marketplace repo at
        ``source_path``; for ``url`` plugins they live in an external repo —
        at its root, or, for a Codex ``git-subdir`` entry, at ``source_path``
        inside it. Both cases are the same coordinate: the subdirectory of the
        cloned repo the plugin lives in, empty meaning "the repo root".
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
                # Empty for a plain ``url`` entry (the repo root is the
                # plugin); the declared path for a Codex ``git-subdir`` entry,
                # whose whole point is that the plugin sits inside a larger
                # repository. The container's clone step already honours
                # ``subdir`` for any git entry, so this needed no change there.
                "subdir": LLMPluginService._normalize_subdir(plugin.source_path),
            }

        # local plugin — files are a subdir of the marketplace repo
        subdir = LLMPluginService._normalize_subdir(plugin.source_path)
        if not subdir:
            # No subdirectory means the whole marketplace repository would be
            # cloned in as one plugin — never what a catalog author meant, and
            # the shape a refused path would otherwise fall into. The caller
            # skips an entry with no resolvable coordinates. (A repo whose root
            # really is the plugin says so with ``"."``, which survives this.)
            return None
        return {
            "url": LLMPluginService._normalize_public_git_url(marketplace.url),
            # Pinned to the install-time commit for reproducibility; fall back to
            # the plugin's parsed commit, then the marketplace branch.
            "ref": link.installed_commit_hash or plugin.commit_hash or marketplace.git_branch,
            "subdir": subdir,
        }

    @staticmethod
    def _normalize_subdir(source_path: str | None) -> str:
        """``source_path`` as the container's clone-relative subdirectory."""
        subdir = (source_path or "").strip()
        return subdir[2:] if subdir.startswith("./") else subdir

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
    def _same_repository(left: str | None, right: str | None) -> bool:
        """Do two configured git URLs name the same repository?

        Used for one decision only: whether an orphaned install may be re-adopted
        by a marketplace row (:meth:`_reattach_orphaned_links`). That makes the
        two failure directions unequal, and the comparison is tuned accordingly.
        Matching two URLs that are *not* the same repository would let somebody
        else's code into an agent's container; failing to match two spellings of
        the same one costs an administrator a manual re-install, which is the
        cost this feature already documents. So it normalises only what this
        codebase already treats as insignificant, and nothing else.

        Normalised, each with a precedent in this module:

        * SSH → HTTPS for the three well-known public hosts, through
          :meth:`_normalize_public_git_url` — which exists because the platform
          *clones* both forms from the same place, so it has already ruled them
          one repository.
        * A trailing ``/`` and a trailing ``.git``, which
          :meth:`_generate_name_from_url` already strips when it derives a
          marketplace's identity from its URL. (It does so with
          ``rstrip(".git")``, a character-class strip that also eats a trailing
          ``t``, ``i``, ``g`` or ``.`` from a repo name — not copied here.)

        Deliberately **not** normalised, so a difference here means "no match"
        and an honest re-registration is re-installed by hand: host casing and
        ``http`` vs ``https`` on an HTTPS URL, a ``user@`` prefix inside one, and
        any SSH URL on a host outside the public three (a private host's SSH flow
        is untouched by design elsewhere in this class). Nothing was invented for
        this comparison; widening it later is a decision, not a tidy-up.
        """
        if not left or not right:
            # Fail closed: an install with no recorded repository has no identity
            # to verify, and "unknown" must never read as "matches".
            return False
        return (
            LLMPluginService._canonical_repository_url(left)
            == LLMPluginService._canonical_repository_url(right)
        )

    @staticmethod
    def _canonical_repository_url(url: str) -> str:
        """``url`` reduced to the form :meth:`_same_repository` compares."""
        candidate = (
            LLMPluginService._normalize_public_git_url(url.strip()) or ""
        ).strip().rstrip("/")
        if candidate.endswith(".git"):
            candidate = candidate[: -len(".git")].rstrip("/")
        return candidate

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
            skill_package_revision_id=link.skill_package_revision_id,
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
