# Addon Marketplaces — Technical Reference

> The admin surface was renamed to **Addon Marketplaces**; the feature id, the route and every table name are unchanged. The `codex` / `skills` formats and the `supported` verdict came in with [agent_addons](../../agents/agent_addons/agent_addons_tech.md), which carries the parser details.

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

**Table: `llm_plugin_marketplace`** (`LLMPluginMarketplace`, `backend/app/models/plugins/llm_plugin.py`)

Defined via `LLMPluginMarketplaceBase`:

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
| `type` | Marketplace format: `claude` (default) \| `codex` \| `skills`. Stored as a plain string; typed as a `Literal` on create/update, so an unknown value is a **422** |
| `name`, `description` | Extracted from `marketplace.json` during sync |
| `owner_name`, `owner_email` | Extracted from `author` field in `marketplace.json` |
| `plugin_count` | Cached count of plugins (updated on sync) |

**Table: `llm_plugin_marketplace_plugin`** (`LLMPluginMarketplacePlugin`)

Defined via `LLMPluginMarketplacePluginBase`:

| Field | Purpose |
|-------|---------|
| `marketplace_id` | FK → `llm_plugin_marketplace` |
| `name`, `description`, `version`, `category` | Plugin metadata |
| `author_name`, `author_email` | Plugin author from `plugin.json` |
| `source_type` | `local` or `url` |
| `plugin_type` | Inherited from the marketplace `type` on every upsert: `claude` \| `codex` \| `skills` |
| `supported` | **New.** `bool`, not null, default `true`. Sync-time verdict: can our containers install this entry? Re-derived on every sync, never sticky |
| `unsupported_reason` | **New.** `String(64)`, nullable. One of `npm_source`, `app_connector_only`, `no_skill_md`, `unknown_source`, `unsafe_path` |
| `source_path` | Relative path (local plugins) |
| `source_url`, `source_branch` | External repo details (url plugins) |
| `source_commit_hash` | Pinned commit from external repo (url plugins) |
| `commit_hash` | Commit hash when this plugin config was last parsed |
| `homepage` | Optional external link |
| `config` | Full `plugin.json` stored as JSON blob. `codex`: the whole entry verbatim, including `policy` (stored, **not enforced**) and the `.codex-plugin/plugin.json` `interface` block, plus a `skills` list of the plugin's own skill folders. `skills` format: `{"skill": {name, description, has_scripts}}` |

These rows are the source of git coordinates consumed by `LLMPluginService.build_plugin_manifest()` at agent plugin install time. There is no companion cache directory on disk.

**API Schemas** (non-table, in `llm_plugin.py`)
- `LLMPluginMarketplaceCreate` — `url`, `ssh_key_id`, `git_branch`, `public_discovery`, `type` (name/description extracted from repo)
- `LLMPluginMarketplaceUpdate` — All fields optional; `public_discovery` is the most commonly edited field post-creation
- `LLMPluginMarketplacePublic` — Full public representation including `plugin_count`, `last_sync_at`, `owner_name/email`
- `LLMPluginMarketplacesPublic` — Paginated list wrapper
- `LLMPluginMarketplacePluginPublic` — Public plugin representation (includes `source_url`, `source_commit_hash`, and now `supported`, `unsupported_reason`, `skill_summary: PluginSkillSummary | None`)
- `LLMPluginMarketplacePluginsPublic` — Paginated plugins list wrapper

## API Endpoints

All in `backend/app/api/routes/llm_plugins.py`, admin-only (superuser guard):

| Method | Path | Request | Response | Purpose |
|--------|------|---------|----------|---------|
| `POST` | `/api/v1/llm-plugins/marketplaces` | `LLMPluginMarketplaceCreate` | `LLMPluginMarketplacePublic` | Create + trigger initial sync |
| `GET` | `/api/v1/llm-plugins/marketplaces` | `?includePublic=bool` | `LLMPluginMarketplacesPublic` | List (owner's + optionally public) |
| `GET` | `/api/v1/llm-plugins/marketplaces/{id}` | — | `LLMPluginMarketplacePublic` | Get single marketplace |
| `PUT` | `/api/v1/llm-plugins/marketplaces/{id}` | `LLMPluginMarketplaceUpdate` | `LLMPluginMarketplacePublic` | Update fields |
| `DELETE` | `/api/v1/llm-plugins/marketplaces/{id}` | — | `Message` | Delete marketplace and plugins (no cache cleanup) |
| `POST` | `/api/v1/llm-plugins/marketplaces/{id}/sync` | — | `LLMPluginMarketplacePublic` | Trigger re-sync (temp-clone, parse, discard) |
| `GET` | `/api/v1/llm-plugins/discover` | `?search=&category=` | `LLMPluginMarketplacePluginsPublic` | Discover plugins from accessible marketplaces |
| `GET` | `/api/v1/llm-plugins/marketplaces/{id}/plugins` | — | `LLMPluginMarketplacePluginsPublic` | List plugins for a specific marketplace |

## Services & Key Methods

**`backend/app/services/plugins/llm_plugin_service.py` — `LLMPluginService`**

Marketplace lifecycle:
- `create_marketplace()` — Creates `LLMPluginMarketplace` record, immediately calls `sync_marketplace()` in background; generates temp name from URL until repo metadata is read.
- `sync_marketplace()` — Core sync method:
  1. Sets status to `pending`.
  2. Optionally loads SSH key via `SSHKeyService.get_decrypted_key_for_git()`.
  3. `temp_dir = tempfile.mkdtemp(prefix="cinna_marketplace_")`.
  4. `clone_repository(url, temp_dir, branch, ssh_key_path)`.
  5. `get_current_commit_hash(repo)`.
  6. `_parse_claude_marketplace(temp_dir)` → metadata + plugin data dicts.
  7. `_upsert_plugins()` → writes to Postgres.
  8. Updates marketplace `status`, `sync_commit_hash`, `last_sync_at`.
  9. `finally: shutil.rmtree(temp_dir, ignore_errors=True)` — discard clone.
- `_get_parser_for_type(marketplace_type)` — dict lookup over `claude` / `codex` / `skills`. **Raises `MarketplaceFormatError` on a miss** — it no longer falls back to the Claude parser. `MarketplaceCatalogError` subclasses it (`marketplace_catalog_unreadable`); `routes/llm_plugins.py` maps both to **422** with `{code, message}`.
- `_parse_claude_marketplace(repo_path)` — Reads `.claude-plugin/marketplace.json`, parses local and URL source types, returns `{"metadata": {...}, "plugins": [...]}`. Now also runs `_safe_entry_path` on each declared path (`unsafe_path` on escape).
- `_parse_codex_marketplace(repo_path)` / `_parse_skills_marketplace(repo_path)` — the two new parsers. Details in [agent_addons_tech](../../agents/agent_addons/agent_addons_tech.md#marketplace-parsers--backendappservicespluginsllm_plugin_servicepy).
- `_rename_link_snapshots()` / `_reattach_orphaned_links()` — keep an installed link's directory identity true across a marketplace rename and across an entry disappearing and coming back. Re-attaching requires the returning marketplace to name the **same repository** as the install's `snapshot_repository_url` (NULL fails closed), on top of the name snapshots and the `created_at` guard. Both wrapped so neither can fail a sync.

**Deletion safety:** because the parser raises *before* `_upsert_plugins` runs, and stale-row deletion lives only inside `_upsert_plugins`, an unreadable catalog **deletes nothing**.
- `_upsert_plugins()` — Compares parsed plugin list with existing DB records: inserts new, updates changed (by `name` key), deletes removed; updates `plugin_count`.
- `delete_marketplace()` — Deletes DB record + cascades to plugin rows. Comment in code confirms: "No persistent cache to clean up — marketplace sync uses a throwaway temp clone that is discarded immediately after parsing."

Discovery:
- `discover_plugins(search, category, plugin_type)` — Queries `LLMPluginMarketplacePlugin` joined with `LLMPluginMarketplace` where `public_discovery=true` OR `owner_id = current_user`; supports text search on name/description/author/category. Reads Postgres only.

## Frontend Components

### Marketplace List Page (`marketplaces.tsx`)

- Route: `/_layout/admin/marketplaces`
- Query key: `["marketplaces"]` via `LlmPluginsService.listMarketplaces({ includePublic: true })`
- Page `h1` / subtitle: **"Addon Marketplaces"** / "Manage plugin and skill repositories and sync their entries"; `Sidebar/AdminMenu.tsx` label follows (route and `Store` icon unchanged)
- Renders `DataTable` with `marketplaceColumns` + `AddMarketplace` button in page header
- Uses `Suspense` + `PendingItems` fallback

### `marketplaceColumns.tsx`

Column definitions for `LLMPluginMarketplacePublic` list:
- **Name** — Linked to `/admin/marketplace/$marketplaceId`
- **Repository URL** — Monospace, truncated
- **Format** — `Badge variant="secondary"`: "Claude plugins" / "Codex plugins" / "Skills repository", labels shared with the create dialog via `frontend/src/utils/marketplace.ts` so the two cannot drift
- **Status** — repainted on touch onto the `--success` / `--warning` / `--destructive` tokens (it hard-coded `green` / `yellow` / `red` classes)
- **Status** — `StatusBadge` with icon (connected / pending / error / disconnected)
- **Plugins** — `plugin_count`, muted if zero
- **Visibility** — Badge (Public / Private from `public_discovery`)
- **Actions** — `MarketplaceActionsMenu` (view details, sync now, delete with confirm dialog)

### `AddMarketplace.tsx`

Dialog triggered from page header. Fields:
- `url` — Required, validated against HTTPS or SSH git URL pattern (`/^(https?:\/\/.+|git@[^:]+:.+)$/`)
- `type` — **Format** `Select` (three options, default `claude`) with a `text-xs text-muted-foreground` help line naming the file each format expects. One dialog, one section — the format changes only the help sentence, never the field set
- `ssh_key_id` — Optional select from `SshKeysService.readSshKeys()` (loaded lazily when dialog opens); "none" value mapped to `undefined` in submit

Dialog title: **"Add addon marketplace"**.

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

Table columns: Name (linked to plugin detail `/admin/marketplace/plugin/$pluginId`), Description (truncated to 80 chars), Author, Type (`Local` / `Remote` badge based on `source_type`), **Supported** (tone dot + label on `--success` / `--destructive`, with the `unsupported_reason` sentence in a `Tooltip`) and **Skills** (the entry's skill count from `config`).

An error branch (`QueryErrorAlert` with Retry) was added on touch — the tab previously had none, so a failed query was indistinguishable from an empty catalog.

> **Known gaps here, accepted in review rather than fixed:** this table is a hand-rolled `Table` rather than the shared `DataTable` the UI specification named, `Common/StatusDot` still is not extracted although the tone dot now has three consumers, and the per-plugin **detail** route still has no query-error branch.

Empty states:
- Marketplace not connected → instruction to sync in Configuration tab
- Connected but no plugins → instruction to sync

## React Query Keys

| Key | Purpose |
|-----|---------|
| `["marketplaces"]` | Full marketplace list |
| `["marketplace", marketplaceId]` | Single marketplace detail |
| `["marketplace-plugins", marketplaceId]` | Plugin list for a marketplace (filtered from discover) |

All three sync call sites (the Configuration tab's button, the detail page's header menu, the list's row menu) go through the single `frontend/src/hooks/useMarketplaceSync.ts` mutation. They previously each carried their own `onSuccess` and had drifted to three different invalidation sets, so a re-sync of an already-connected marketplace left the entries table showing the previous sync's Supported reasons and Skills counts until a page reload.
| `["ssh-keys"]` | SSH keys for AddMarketplace dialog (lazy) |

## Security

- All marketplace mutation endpoints (`POST`, `PUT`, `DELETE`, `sync`) are guarded by `get_current_active_superuser` dependency — non-admin users cannot create or modify marketplaces
- `GET /marketplaces` and `GET /discover` filter by ownership or `public_discovery=true` so users only see their own or explicitly shared marketplaces
- SSH key IDs reference the `user_ssh_keys` table; private key material is never included in API responses
- No plugin files are ever stored on the backend filesystem — only git coordinates in Postgres
- **SSH URLs for public hosts are auto-normalized**: A marketplace (or an external `url`-type plugin) may be registered with an SSH-form Git URL (e.g. `git@github.com:org/repo.git`). When the git coordinates are later resolved for the plugin manifest, `LLMPluginService._normalize_public_git_url` rewrites such URLs for `github.com`, `gitlab.com`, and `bitbucket.org` to their HTTPS equivalents so the agent-env container (which has no SSH key for these hosts) can clone public repos without authentication. URLs for unrecognized or private hosts are left unchanged.
