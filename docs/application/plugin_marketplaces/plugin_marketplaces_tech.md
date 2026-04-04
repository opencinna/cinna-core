# Plugin Marketplaces — Technical Reference

## File Locations

**Backend**
- `backend/app/models/plugins/llm_plugin.py` — `LLMPluginMarketplace`, `LLMPluginMarketplacePlugin`, and all related schemas
- `backend/app/api/routes/llm_plugins.py` — Marketplace CRUD + sync endpoints
- `backend/app/services/plugins/llm_plugin_service.py` — `create_marketplace()`, `sync_marketplace()`, `_parse_claude_marketplace()`, `_upsert_plugins()`, `discover_plugins()`
- `backend/app/services/knowledge/git_operations.py` — Shared git clone/pull utilities used during sync

**Frontend**
- `frontend/src/routes/_layout/admin/marketplaces.tsx` — Marketplace list page
- `frontend/src/routes/_layout/admin/marketplace/$marketplaceId.tsx` — Marketplace detail page (tabbed)
- `frontend/src/components/Admin/AddMarketplace.tsx` — Create marketplace dialog
- `frontend/src/components/Admin/marketplaceColumns.tsx` — Column definitions for the list table
- `frontend/src/components/Admin/MarketplaceActionsMenu.tsx` — Row-level actions (view, sync, delete) in the list table
- `frontend/src/components/Admin/MarketplaceConfigurationTab.tsx` — Configuration tab on the detail page
- `frontend/src/components/Admin/MarketplacePluginsTab.tsx` — Plugins tab on the detail page
- `frontend/src/client/sdk.gen.ts` — Auto-generated `LlmPluginsService`

## Database Schema

**Table: `llmpluginmarketplace`** (`LLMPluginMarketplace`, `backend/app/models/plugins/llm_plugin.py:52`)

Defined via `LLMPluginMarketplaceBase` (line 34):

| Field | Purpose |
|-------|---------|
| `url` | Git repository URL (HTTPS or SSH) |
| `git_branch` | Branch to clone (default: `main`) |
| `ssh_key_id` | FK → `user_ssh_keys` (nullable, for private repos) |
| `public_discovery` | Whether all users can discover this marketplace's plugins |
| `status` | `pending` / `connected` / `error` / `disconnected` |
| `status_message` | Human-readable error or status detail |
| `sync_commit_hash` | HEAD commit at last successful sync |
| `last_sync_at` | Timestamp of last sync |
| `type` | Marketplace format (`"claude"` only currently) |
| `name`, `description` | Extracted from `marketplace.json` during sync |
| `owner_name`, `owner_email` | Extracted from `author` field in `marketplace.json` |
| `plugin_count` | Cached count of plugins (updated on sync) |

**Table: `llmpluginmarketplaceplugin`** (`LLMPluginMarketplacePlugin`, line 164)

Defined via `LLMPluginMarketplacePluginBase` (line 145):

| Field | Purpose |
|-------|---------|
| `marketplace_id` | FK → `llmpluginmarketplace` |
| `name`, `description`, `version`, `category` | Plugin metadata |
| `author_name`, `author_email` | Plugin author from `plugin.json` |
| `source_type` | `local` or `url` |
| `source_path` | Relative path (local plugins) |
| `source_url`, `source_branch` | External repo details (url plugins) |
| `commit_hash` | Commit hash when this plugin config was last parsed |
| `homepage` | Optional external link |
| `config` | Full `plugin.json` stored as JSON blob |

**API Schemas** (non-table, in `llm_plugin.py`)
- `LLMPluginMarketplaceCreate` (line 97) — `url`, `ssh_key_id` (create only; name/branch extracted from repo)
- `LLMPluginMarketplaceUpdate` (line 112) — `public_discovery` (only editable field post-creation)
- `LLMPluginMarketplacePublic` (line 74) — Full public representation including `plugin_count`, `last_sync_at`, `owner_name/email`
- `LLMPluginMarketplacesPublic` (line 126) — Paginated list wrapper
- `LLMPluginMarketplacePluginPublic` (line 190) — Public plugin representation
- `LLMPluginMarketplacePluginsPublic` (line 216) — Paginated plugins list wrapper

## API Endpoints

All in `backend/app/api/routes/llm_plugins.py`, admin-only (superuser guard):

| Method | Path | Request | Response | Purpose |
|--------|------|---------|----------|---------|
| `POST` | `/api/v1/llm-plugins/marketplaces` | `LLMPluginMarketplaceCreate` | `LLMPluginMarketplacePublic` | Create + trigger initial sync |
| `GET` | `/api/v1/llm-plugins/marketplaces` | `?includePublic=bool` | `LLMPluginMarketplacesPublic` | List (owner's + optionally public) |
| `GET` | `/api/v1/llm-plugins/marketplaces/{id}` | — | `LLMPluginMarketplacePublic` | Get single marketplace |
| `PUT` | `/api/v1/llm-plugins/marketplaces/{id}` | `LLMPluginMarketplaceUpdate` | `LLMPluginMarketplacePublic` | Update `public_discovery` flag |
| `DELETE` | `/api/v1/llm-plugins/marketplaces/{id}` | — | `Message` | Delete marketplace and plugins |
| `POST` | `/api/v1/llm-plugins/marketplaces/{id}/sync` | — | `LLMPluginMarketplacePublic` | Trigger re-sync |
| `GET` | `/api/v1/llm-plugins/discover` | `?search=&category=` | `LLMPluginMarketplacePluginsPublic` | Discover plugins from accessible marketplaces |
| `GET` | `/api/v1/llm-plugins/marketplaces/{id}/plugins` | — | `LLMPluginMarketplacePluginsPublic` | List plugins for a specific marketplace |

## Services & Key Methods

**`backend/app/services/plugins/llm_plugin_service.py` — `LLMPluginService`**

Marketplace lifecycle:
- `create_marketplace()` — Creates `LLMPluginMarketplace` record, immediately calls `sync_marketplace()` in background; generates temp name from URL until repo metadata is read
- `sync_marketplace()` — Calls `git_operations.clone_or_pull()`, updates `status`, reads `marketplace.json`, calls `_parse_claude_marketplace()`, writes `sync_commit_hash` and `last_sync_at`
- `_parse_claude_marketplace()` — Reads `.claude-plugin/marketplace.json`, extracts `name`/`description`/`author` metadata into the marketplace record, calls `_upsert_plugins()`
- `_upsert_plugins()` — Compares parsed plugin list with existing DB records: inserts new, updates changed (by `name` key), deletes removed; updates `plugin_count`

Discovery:
- `discover_plugins(search, category)` — Queries `LLMPluginMarketplacePlugin` joined with `LLMPluginMarketplace` where `public_discovery=true` OR `owner_id = current_user`; supports text search on name/description and category filter

## Frontend Components

### Marketplace List Page (`marketplaces.tsx`)

- Route: `/_layout/admin/marketplaces`
- Query key: `["marketplaces"]` via `LlmPluginsService.listMarketplaces({ includePublic: true })`
- Renders `DataTable` with `marketplaceColumns` + `AddMarketplace` button in page header
- Uses `Suspense` + `PendingItems` fallback

### `marketplaceColumns.tsx`

Column definitions for `LLMPluginMarketplacePublic` list:
- **Name** — Linked to `/admin/marketplace/$marketplaceId`
- **Repository URL** — Monospace, truncated
- **Type** — Badge (`claude`)
- **Status** — `StatusBadge` with icon (connected / pending / error / disconnected)
- **Plugins** — `plugin_count`, muted if zero
- **Visibility** — Badge (Public / Private from `public_discovery`)
- **Actions** — `MarketplaceActionsMenu` (view details, sync now, delete with confirm dialog)

### `AddMarketplace.tsx`

Dialog triggered from page header. Fields:
- `url` — Required, validated against HTTPS or SSH git URL pattern (`/^(https?:\/\/.+|git@[^:]+:.+)$/`)
- `ssh_key_id` — Optional select from `SshKeysService.readSshKeys()` (loaded lazily when dialog opens); "none" value mapped to `undefined` in submit

Mutations: `LlmPluginsService.createMarketplace()` → invalidates `["marketplaces"]` on settled.

### Marketplace Detail Page (`$marketplaceId.tsx`)

- Route: `/_layout/admin/marketplace/$marketplaceId`
- Query key: `["marketplace", marketplaceId]` via `LlmPluginsService.getMarketplace()`
- Page header: marketplace name + dropdown menu (Sync Now / Delete Marketplace)
- Tabs: `HashTabs` with `configuration` (default) and `plugins`
- Delete: `AlertDialog` confirm → `LlmPluginsService.deleteMarketplace()` → navigate back to list

### `MarketplaceConfigurationTab.tsx`

Read-only display of all marketplace fields: URL, branch, SSH key presence, type, `public_discovery` switch, plugin count, last sync timestamp, last commit hash (first 8 chars), owner name/email, description, status message.

Editable:
- `public_discovery` — Inline `Switch` → `LlmPluginsService.updateMarketplace({ public_discovery })` → invalidates `["marketplace", id]` and `["marketplaces"]`

Actions:
- **Sync Marketplace** button → `LlmPluginsService.syncMarketplace()` → invalidates `["marketplace", id]`

### `MarketplacePluginsTab.tsx`

Displays plugins for this marketplace. Only renders when `marketplace.status === "connected"`.

Data: calls `LlmPluginsService.discoverPlugins({})` (all accessible plugins), then filters client-side by `plugin.marketplace_id === marketplaceId`. Query key: `["marketplace-plugins", marketplaceId]`.

Table columns: Name (linked to plugin detail `/admin/marketplace/plugin/$pluginId`), Description (truncated to 80 chars), Author, Type (`Local` / `Remote` badge based on `source_type`).

Empty states:
- Marketplace not connected → instruction to sync in Configuration tab
- Connected but no plugins → instruction to sync

## React Query Keys

| Key | Purpose |
|-----|---------|
| `["marketplaces"]` | Full marketplace list |
| `["marketplace", marketplaceId]` | Single marketplace detail |
| `["marketplace-plugins", marketplaceId]` | Plugin list for a marketplace (filtered from discover) |
| `["ssh-keys"]` | SSH keys for AddMarketplace dialog (lazy) |

## Security

- All marketplace mutation endpoints (`POST`, `PUT`, `DELETE`, `sync`) are guarded by `get_current_active_superuser` dependency — non-admin users cannot create or modify marketplaces
- `GET /marketplaces` and `GET /discover` filter by ownership or `public_discovery=true` so users only see their own or explicitly shared marketplaces
- SSH key IDs reference the `user_ssh_keys` table; private key material is never included in API responses
