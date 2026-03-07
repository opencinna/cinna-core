# Agent Plugins — Technical Reference

## File Locations

**Backend**
- `backend/app/models/llm_plugin.py` — All database models and API schemas (LLMPluginMarketplace, LLMPluginMarketplacePlugin, AgentPluginLink, PluginSyncResponse, EnvironmentSyncStatus)
- `backend/app/services/llm_plugin_service.py` — Main business logic service
- `backend/app/services/git_operations.py` — Git clone/pull shared utilities
- `backend/app/api/routes/llm_plugins.py` — All API endpoints
- `backend/app/services/environment_lifecycle.py` — Plugin sync during environment start (`_sync_plugins_to_environment`, `_sync_dynamic_data`)
- `backend/app/services/adapters/docker_adapter.py` — `set_plugins()` method

**Agent-Env (inside Docker container)**
- `backend/app/env-templates/app_core_base/core/server/routes.py` — `/config/plugins` endpoints
- `backend/app/env-templates/app_core_base/core/server/agent_env_service.py` — Plugin file management
- `backend/app/env-templates/app_core_base/core/server/sdk_manager.py` — Plugin loading into SDK at session start

**Frontend**
- `frontend/src/routes/_layout/admin/marketplaces.tsx` — Marketplace list page (admin)
- `frontend/src/routes/_layout/admin/marketplace/$marketplaceId.tsx` — Marketplace detail with tabs (admin)
- `frontend/src/components/Admin/AddMarketplace.tsx` — Create marketplace dialog
- `frontend/src/components/Admin/MarketplaceConfigurationTab.tsx` — Edit marketplace settings
- `frontend/src/components/Admin/MarketplacePluginsTab.tsx` — View plugins in a marketplace
- `frontend/src/components/Agents/AgentPluginsTab.tsx` — Agent plugins tab (installed + discover sections)
- `frontend/src/components/Agents/PluginCard.tsx` — Plugin card in the discovery grid
- `frontend/src/components/Agents/InstallPluginModal.tsx` — Mode selection dialog during install
- `frontend/src/client/sdk.gen.ts` — Auto-generated `LlmPluginsService`

## Database Schema

**Table: `llmpluginmarketplace`** (`LLMPluginMarketplace`)

| Field | Purpose |
|-------|---------|
| `url`, `git_branch` | Git repository location |
| `ssh_key_id` | FK → `user_ssh_keys` for private repos |
| `public_discovery` | Whether other users can discover plugins |
| `status` | `pending` / `connected` / `error` / `disconnected` |
| `sync_commit_hash` | HEAD commit at last sync (for update detection) |
| `type` | Marketplace format (`"claude"` default) |
| `name`, `description` | Extracted from `marketplace.json` during sync |

**Table: `llmpluginmarketplaceplugin`** (`LLMPluginMarketplacePlugin`)

| Field | Purpose |
|-------|---------|
| `marketplace_id` | FK → marketplace |
| `source_type` | `local` (in marketplace repo) or `url` (external repo) |
| `source_path` | Relative path for local plugins |
| `source_url`, `source_branch` | Git URL/branch for external plugins |
| `commit_hash` | Commit at which plugin config was last parsed |
| `config` | Full `plugin.json` stored as JSON for reference |

**Table: `agentpluginlink`** (`AgentPluginLink`)

| Field | Purpose |
|-------|---------|
| `agent_id`, `plugin_id` | Agent ↔ plugin relationship |
| `installed_version`, `installed_commit_hash` | Version pinning at install time |
| `conversation_mode`, `building_mode` | Per-mode activation flags |
| `disabled` | Files synced but not loaded into SDK when true |

**Response Models** (non-table, in `llm_plugin.py`)

- `PluginSyncResponse` — `success`, `message`, `plugin_link`, `environments_synced`, `total_environments`, `successful_syncs`, `failed_syncs`
- `EnvironmentSyncStatus` — `environment_id`, `instance_name`, `status` (`"success"` / `"error"` / `"activated_and_synced"` / `"skipped"`), `error_message`, `was_suspended`

## API Endpoints

All routes in `backend/app/api/routes/llm_plugins.py`:

**Marketplace (admin)**
- `POST /api/v1/llm-plugins/marketplaces` — Create marketplace
- `GET /api/v1/llm-plugins/marketplaces` — List marketplaces
- `GET /api/v1/llm-plugins/marketplaces/{id}` — Get marketplace detail
- `PUT /api/v1/llm-plugins/marketplaces/{id}` — Update marketplace
- `DELETE /api/v1/llm-plugins/marketplaces/{id}` — Delete marketplace
- `POST /api/v1/llm-plugins/marketplaces/{id}/sync` — Trigger re-sync

**Plugin Discovery**
- `GET /api/v1/llm-plugins/discover` — Discover plugins (search/filter across accessible marketplaces)
- `GET /api/v1/llm-plugins/marketplaces/{id}/plugins` — List plugins in a marketplace

**Agent Plugin Management** (all return `PluginSyncResponse` except GET)
- `GET /api/v1/llm-plugins/agents/{agent_id}/plugins` → `AgentPluginLinksPublic` (includes `has_update`, `disabled` flags)
- `POST /api/v1/llm-plugins/agents/{agent_id}/plugins` — Install plugin
- `DELETE /api/v1/llm-plugins/agents/{agent_id}/plugins/{link_id}` — Uninstall plugin
- `PUT /api/v1/llm-plugins/agents/{agent_id}/plugins/{link_id}` — Update mode/disabled flags
- `POST /api/v1/llm-plugins/agents/{agent_id}/plugins/{link_id}/upgrade` — Upgrade to latest version

## Services & Key Methods

**`backend/app/services/llm_plugin_service.py` — `LLMPluginService`**

Marketplace Management:
- `create_marketplace()` — Creates record, generates temp name from URL
- `sync_marketplace()` — Clones/pulls repo, parses plugins, updates metadata
- `_parse_claude_marketplace()` — Parses `.claude-plugin/marketplace.json`
- `_upsert_plugins()` — Adds new, updates changed, removes deleted plugins from DB

Plugin Discovery:
- `discover_plugins()` — Returns plugins from accessible marketplaces with search/filter

Agent Plugin Management:
- `install_plugin_for_agent()` — Creates `AgentPluginLink` with version and commit pinning
- `uninstall_plugin_from_agent()` — Removes plugin link
- `get_agent_plugins()` — Returns plugins with computed `has_update` and `disabled` flags
- `update_plugin_modes()` — Updates `conversation_mode`, `building_mode`, `disabled`
- `upgrade_agent_plugin()` — Updates link to latest version and commit hash

Plugin File Operations:
- `get_plugin_files()` — Dispatches to local or URL source handler
- `_get_local_plugin_files()` — Reads from marketplace repo cache on disk
- `_get_url_plugin_files()` — Reads from external plugin repo cache on disk

Environment Sync:
- `prepare_plugins_for_environment()` — Returns `all_plugins` (for file sync) and `active_plugins` (enabled only, for settings.json)
- `sync_plugins_to_agent_environments()` — Queries running/suspended environments, activates suspended ones, syncs files, returns `PluginSyncResponse`

**`backend/app/services/environment_lifecycle.py`**
- `_sync_plugins_to_environment()` — Called in `_sync_dynamic_data()` during environment start; encodes plugin files as base64 and sends via adapter

**`backend/app/services/adapters/docker_adapter.py`**
- `set_plugins()` — HTTP POST to `/config/plugins` in agent-env with base64-encoded files and `settings.json` content

**Agent-Env: `agent_env_service.py`**
- `update_plugins()` — Writes plugin files to `/app/workspace/plugins/`
- `get_plugins_settings()` — Reads `settings.json`
- `get_active_plugins_for_mode(mode)` — Filters by `conversation_mode` or `building_mode` flag

**Agent-Env: `sdk_manager.py`**
- In `send_message_stream()`: calls `get_active_plugins_for_mode(mode)`, builds `[{"type": "local", "path": ...}]` array, passes to `ClaudeAgentOptions(plugins=...)`

## Frontend Components

**Admin**
- `marketplaces.tsx` — Marketplace list with sync status indicators
- `marketplace/$marketplaceId.tsx` — Tabbed detail: Configuration tab + Plugins tab
- `AddMarketplace.tsx` — Create dialog (URL + SSH key)
- `MarketplaceConfigurationTab.tsx` — Edit name, URL, branch, SSH key, visibility
- `MarketplacePluginsTab.tsx` — Read-only plugin list from the marketplace

**Agent Plugins Tab (`AgentPluginsTab.tsx`)**

Two sections:
1. **Installed Plugins** — Table with: enable/disable switch, name, version, category, description, Conversation Mode toggle, Building Mode toggle, Upgrade button (when update available), Uninstall button. Disabled plugins render at reduced opacity; mode toggles are disabled when plugin is disabled.
2. **Discover Plugins** — Searchable card grid; each card has an Install button opening `InstallPluginModal.tsx` for mode selection.

Sync feedback:
- Success → toast notification
- Error → modal dialog with `EnvironmentSyncStatus` list showing per-environment details

## Workspace Structure (Inside Agent-Env)

```
/app/workspace/plugins/
├── settings.json                        # Active plugins configuration
└── {marketplace_name}/
    └── {plugin_name}/                   # Plugin files from repository
        ├── .claude-plugin/
        │   └── plugin.json
        ├── commands/
        ├── skills/
        └── .mcp.json
```

`settings.json` contains only enabled plugins (`disabled=false`). Disabled plugins keep their directory but are excluded from `active_plugins`.

## Marketplace File Format

Repository root: `.claude-plugin/marketplace.json`

Fields: `name`, `description`, `author` (name, email), `plugins` (array)

**Local plugin source** (default): `{"name": "plugin-name", "source": "./plugins/plugin-name", ...}`

**URL plugin source**: `{"name": "...", "source": {"source": "url", "url": "https://github.com/org/repo.git", "branch": "main"}, ...}`

External repos (URL type) must contain `.claude-plugin/plugin.json` with plugin configuration.

## Configuration

- `ssh_key_id` — References `user_ssh_keys` table; same SSH auth pattern used by knowledge source Git repos
- Marketplace `type` field — Reserved for future marketplace format parsers; currently only `"claude"` format is implemented

## Security

- Admin-only access for marketplace CRUD operations (superuser guard on routes)
- `public_discovery` flag gates plugin visibility to non-owners
- SSH key references use the shared `user_ssh_keys` table; private key material is never exposed via API
- Plugin files are written to a scoped workspace directory (`/app/workspace/plugins/`) inside the isolated Docker container
