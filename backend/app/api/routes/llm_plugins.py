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

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from app.api.deps import CurrentUser, SessionDep
from app.models import (
    Message,
)
from app.models.plugins.llm_plugin import (
    LLMPluginMarketplaceCreate,
    LLMPluginMarketplaceUpdate,
    LLMPluginMarketplacePublic,
    LLMPluginMarketplacesPublic,
    LLMPluginMarketplacePluginPublic,
    LLMPluginMarketplacePluginsPublic,
    AgentPluginLinkCreate,
    AgentPluginLinkUpdate,
    AgentPluginLinksPublic,
    PluginSource,
    PluginSyncResponse,
)
from app.services.credentials.credentials_service import CredentialsService
from app.services.plugins.llm_plugin_service import (
    LLMPluginService,
    MarketplaceFormatError,
)
from app.services.plugins.marketplace_skill_snapshots import (
    MarketplaceSkillSnapshots,
)
from app.services.skills.exceptions import SkillCatalogError, http_error_for

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
    marketplace = LLMPluginService.get_marketplace_with_access_check(
        session, marketplace_id, current_user
    )
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
    marketplace = LLMPluginService.get_marketplace_with_access_check(
        session, marketplace_id, current_user, require_write=True
    )

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
    marketplace = LLMPluginService.get_marketplace_with_access_check(
        session, marketplace_id, current_user, require_write=True
    )

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
    marketplace = LLMPluginService.get_marketplace_with_access_check(
        session, marketplace_id, current_user, require_write=True
    )

    try:
        synced = LLMPluginService.sync_marketplace(
            session=session,
            marketplace_id=marketplace_id,
            user_id=marketplace.user_id,  # Use marketplace owner for SSH key access
        )
        return LLMPluginService.get_marketplace_public(session, synced)
    except MarketplaceFormatError as e:
        # Nothing was read: either the marketplace declares a format this
        # platform has no parser for, or the repository's catalog is missing /
        # unreadable (`MarketplaceCatalogError`, which carries its own code).
        # Either way the row already holds `status=error` carrying this
        # sentence, and not one plugin was touched — 422 rather than the 404 below, which
        # means "no such marketplace". Coded so a client can branch without
        # matching prose.
        raise HTTPException(
            status_code=422,
            detail={"code": e.code, "message": str(e)},
        )
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
    plugin_type: str | None = None,
    marketplace_id: uuid.UUID | None = None,
    skip: int = 0,
    limit: int = 30,
) -> Any:
    """
    Discover available plugins.

    Returns plugins from:
    - User's private marketplaces
    - Public marketplaces

    Entries this platform cannot install are included, last, carrying
    `supported=false` and a `unsupported_reason` code — hiding them would make
    a half-broken marketplace look empty. Installing one is refused with a 409.

    Optional filters:
    - search: Search in name/description/author/category
    - category: Filter by category
    - plugin_type: Filter by marketplace format (claude | codex | skills)
    - marketplace_id: Scope to one marketplace (an id the caller cannot see
      returns an empty page rather than an error)
    - skip: Pagination offset (default 0)
    - limit: Maximum items per page (default 30)
    """
    plugins, total_count = LLMPluginService.discover_plugins(
        session=session,
        user_id=current_user.id,
        search=search,
        category=category,
        plugin_type=plugin_type,
        marketplace_id=marketplace_id,
        skip=skip,
        limit=limit,
    )
    return LLMPluginMarketplacePluginsPublic(data=plugins, count=total_count)


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

    # Check marketplace access (read level: owner OR public OR superuser).
    marketplace = plugin.marketplace
    if not marketplace:
        raise HTTPException(status_code=404, detail="Plugin marketplace not found")
    LLMPluginService.get_marketplace_with_access_check(
        session, marketplace.id, current_user
    )

    return LLMPluginService.get_plugin_public(plugin, marketplace_name=marketplace.name)


#: Most ids one batch lookup accepts.
MAX_BATCH_PLUGIN_IDS = 100


@router.get(
    "/plugins/{plugin_id}/skill-archive",
    response_class=FileResponse,
    responses={
        200: {"content": {"application/gzip": {}}},
        404: {"description": "`not_found` or `snapshot_missing`"},
        409: {"description": "`not_a_skill`"},
    },
)
def get_plugin_skill_archive(
    session: SessionDep,
    current_user: CurrentUser,
    plugin_id: uuid.UUID,
    sha256: str | None = Query(default=None, pattern="^[0-9a-f]{64}$"),
) -> Any:
    """
    Download a skills-format entry's archive snapshot.

    Visible exactly when the entry is listed by `/discover` for the caller (own
    or publicly discoverable marketplace; no superuser bypass). Without
    `sha256` the current snapshot is served; with it, any digest that was ever
    current for this entry. The response carries the digest in
    `X-Archive-Sha256`; the client verifies the bytes against it.

    Errors (`detail.code`): 404 `not_found` (unknown or not visible),
    409 `not_a_skill` (no installable snapshot: another format, or an
    unsupported entry such as `too_large`), 404 `snapshot_missing` (that
    digest is not kept).
    """
    found = LLMPluginService.get_discoverable_plugin(
        session, plugin_id, current_user.id
    )
    if found is None:
        raise HTTPException(status_code=404, detail={"code": "not_found"})
    plugin, marketplace = found
    if plugin.skill_archive_sha256 is None:
        raise HTTPException(status_code=409, detail={"code": "not_a_skill"})

    want = sha256 or plugin.skill_archive_sha256
    path = MarketplaceSkillSnapshots.existing_archive(marketplace.id, plugin.id, want)
    try:
        if path is None:
            raise FileNotFoundError(want)
        # One stat, handed to FileResponse: a delete racing the existence
        # check answers snapshot_missing rather than a 500.
        stat_result = path.stat()
    except OSError:
        raise HTTPException(status_code=404, detail={"code": "snapshot_missing"})

    logger.info(
        "marketplace_skill_archive_download user_id=%s plugin_id=%s sha=%s bytes=%s",
        current_user.id,
        plugin.id,
        want,
        stat_result.st_size,
    )
    return FileResponse(
        path,
        stat_result=stat_result,
        media_type="application/gzip",
        filename=f"{plugin.name}.tar.gz",
        headers={"X-Archive-Sha256": want, "X-Content-Type-Options": "nosniff"},
    )


@router.get("/plugins", response_model=LLMPluginMarketplacePluginsPublic)
def get_plugins_by_ids(
    session: SessionDep,
    current_user: CurrentUser,
    ids: str = Query(description="Comma-separated plugin ids, at most 100"),
) -> Any:
    """
    Look up several marketplace plugins by id.

    Uses the `/discover` visibility rule: unknown and invisible ids are simply
    absent. Duplicates are returned once, in request order; `count` is the
    number of rows returned.

    Errors (422, `detail.code`): `too_many_ids` (more than 100), `invalid_id`
    (a value that is not a uuid).
    """
    raw_ids = [part.strip() for part in ids.split(",") if part.strip()]
    if len(raw_ids) > MAX_BATCH_PLUGIN_IDS:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "too_many_ids",
                "message": f"At most {MAX_BATCH_PLUGIN_IDS} ids per request.",
            },
        )
    plugin_ids: list[uuid.UUID] = []
    for raw in raw_ids:
        try:
            plugin_id = uuid.UUID(raw)
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail={"code": "invalid_id", "message": f"Not a valid id: {raw[:64]!r}"},
            )
        if plugin_id not in plugin_ids:
            plugin_ids.append(plugin_id)

    plugins = LLMPluginService.get_discoverable_plugins(
        session, plugin_ids, current_user.id
    )
    return LLMPluginMarketplacePluginsPublic(data=plugins, count=len(plugins))


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
    LLMPluginService.verify_agent_access(session, agent_id, current_user)

    plugins = LLMPluginService.get_agent_plugins(session, agent_id)
    return AgentPluginLinksPublic(data=plugins, count=len(plugins))


@router.post("/agents/{agent_id}/plugins", response_model=PluginSyncResponse)
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
    - Synced to running and suspended environments (suspended ones will be activated first)

    Returns sync status for all environments.
    """
    LLMPluginService.verify_agent_access(session, agent_id, current_user)

    # Verify plugin access
    plugin = LLMPluginService.get_plugin(session, data.plugin_id)
    if not plugin:
        raise HTTPException(status_code=404, detail="Plugin not found")

    # Plugin marketplace access (read level). Kept inline rather than calling
    # get_marketplace_with_access_check to preserve the existing behavior: a
    # plugin whose marketplace row is unresolvable is tolerated (no 404), and the
    # 403 detail is plugin-specific.
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

        # Sync to running/suspended environments
        sync_response = await LLMPluginService.sync_plugins_to_agent_environments(
            session=session,
            agent_id=agent_id,
            user_id=current_user.id,
            plugin_link=link,
        )

        return sync_response
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/agents/{agent_id}/plugins/{link_id}", response_model=PluginSyncResponse)
async def uninstall_agent_plugin(
    session: SessionDep,
    current_user: CurrentUser,
    agent_id: uuid.UUID,
    link_id: uuid.UUID,
) -> Any:
    """
    Uninstall a plugin from an agent.

    The plugin will be removed and environments will be synced.
    Returns sync status for all environments.
    """
    LLMPluginService.verify_agent_access(session, agent_id, current_user)

    result = LLMPluginService.uninstall_plugin_link(
        session=session,
        agent_id=agent_id,
        link_id=link_id,
    )
    if not result.deleted:
        raise HTTPException(status_code=404, detail="Plugin link not found")

    # A catalog skill's released placeholders leave the agent: plugin sync
    # does not carry credentials, so push them first.
    if result.credentials_changed:
        await CredentialsService.sync_credentials_to_agent_environments(
            session, agent_id
        )

    # Sync to running/suspended environments
    return await LLMPluginService.sync_plugins_to_agent_environments(
        session=session,
        agent_id=agent_id,
        user_id=current_user.id,
        message_prefix="Plugin uninstalled.",
    )


@router.put("/agents/{agent_id}/plugins/{link_id}", response_model=PluginSyncResponse)
async def update_agent_plugin(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    agent_id: uuid.UUID,
    link_id: uuid.UUID,
    data: AgentPluginLinkUpdate,
) -> Any:
    """
    Update plugin mode flags (conversation_mode, building_mode, disabled).

    Changes will be synced to running and suspended environments.
    Returns sync status for all environments.
    """
    LLMPluginService.verify_agent_access(session, agent_id, current_user)

    link = LLMPluginService.update_plugin_modes(
        session=session,
        agent_id=agent_id,
        link_id=link_id,
        data=data,
    )
    if not link:
        raise HTTPException(status_code=404, detail="Plugin link not found")

    # Sync to running/suspended environments
    sync_response = await LLMPluginService.sync_plugins_to_agent_environments(
        session=session,
        agent_id=agent_id,
        user_id=current_user.id,
        plugin_link=link,
    )

    return sync_response


@router.post("/agents/{agent_id}/plugins/{link_id}/upgrade", response_model=PluginSyncResponse)
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
    Returns sync status for all environments.
    """
    LLMPluginService.verify_agent_access(session, agent_id, current_user)

    try:
        link = LLMPluginService.upgrade_agent_plugin(
            session=session,
            agent_id=agent_id,
            link_id=link_id,
        )
    except SkillCatalogError as exc:
        # A catalog-sourced link upgrades by re-pinning to its package's latest
        # revision, and that can fail for reasons a marketplace link has no
        # word for ("the catalog entry is gone"). Map the code rather than
        # letting it read as a missing link.
        raise http_error_for(exc)
    if not link:
        raise HTTPException(status_code=404, detail="Plugin link not found")

    # A catalog re-pin may provision the credential slots the new revision
    # adds. Plugin sync does not carry credentials, so push them first.
    if link.source == PluginSource.catalog:
        await CredentialsService.sync_credentials_to_agent_environments(
            session, agent_id
        )

    # Sync to running/suspended environments
    return await LLMPluginService.sync_plugins_to_agent_environments(
        session=session,
        agent_id=agent_id,
        user_id=current_user.id,
        plugin_link=link,
        message_prefix="Plugin upgraded.",
    )
