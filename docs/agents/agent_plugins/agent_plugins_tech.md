# Agent Plugins — Technical Reference

## File Locations

**Backend**
- `backend/app/models/plugins/llm_plugin.py` — All database models and API schemas (`LLMPluginMarketplace`, `LLMPluginMarketplacePlugin`, `AgentPluginLink`, `PluginSource`, `PluginInstallResult`, `PluginSyncResponse`, `EnvironmentSyncStatus`)
- `backend/app/services/plugins/llm_plugin_service.py` — Main business logic service (`build_plugin_manifest`, `sync_plugins_to_agent_environments`, marketplace CRUD + sync)
- `backend/app/services/bundles/plugin_sync.py` — Bundle plugin snapshot/merge helpers (`snapshot_plugin_specs`, `materialise`, `merge`)
- `backend/app/services/bundles/publish_service.py` — `_collect_plugin_specs`, `_copy_plugins_tree` (bundle snapshot)
- `backend/app/services/bundles/install_service.py` — `_materialise_plugin_links` (bundle install), `apply_update` merge wiring
- `backend/app/services/environments/workspace_copy.py` — `_seed_plugins_tree` (overlay snapshot plugins onto consumer workspace)
- `backend/app/services/knowledge/git_operations.py` — Git clone/pull shared utilities (used by marketplace sync)
- `backend/app/services/environments/environment_lifecycle.py` — `_sync_plugins_to_environment`, `_surface_plugin_failures`, `_setup_new_container` wiring
- `backend/app/services/environments/adapters/docker_adapter.py` — `set_plugins(manifest)` — HTTP transport to agent-env `/config/plugins`
- `backend/app/services/notifications/notification_catalog.py` — `NotificationType.PLUGIN_SYNC_FAILED` entry
- `backend/app/api/routes/llm_plugins.py` — All API endpoints
- `backend/app/alembic/versions/2ca38822e945_resilient_plugins_plugin_source_bundle_.py` — Migration

**Agent-Env (inside Docker container)**
- `backend/app/env-templates/app_core_base/core/server/routes.py` — `POST /config/plugins` (repurposed: manifest + install, not base64)
- `backend/app/env-templates/app_core_base/core/server/agent_env_service.py` — `install_plugins()`, `_ensure_marketplace_plugin()`, `_ensure_bundle_plugin()`, `_prune_plugin_dirs()`, `_regenerate_plugin_settings()`, `get_active_plugins_for_mode()`, `get_opencode_plugin_artifacts()`
- `backend/app/env-templates/app_core_base/core/server/sdk_manager.py` — Plugin loading into Claude Code SDK at session start
- `backend/app/env-templates/app_core_base/core/server/adapters/opencode_sdk_adapter.py` — `_materialize_opencode_config()`, `_copy_plugin_commands()`, `plugin_capability_warning` system event

**Frontend**
- `frontend/src/routes/_layout/admin/marketplaces.tsx` — Marketplace list page (admin)
- `frontend/src/routes/_layout/admin/marketplace/$marketplaceId.tsx` — Marketplace detail with tabs (admin)
- `frontend/src/components/Admin/AddMarketplace.tsx` — Create marketplace dialog
- `frontend/src/components/Admin/MarketplaceConfigurationTab.tsx` — Edit marketplace settings
- `frontend/src/components/Admin/MarketplacePluginsTab.tsx` — View plugins in a marketplace
- `frontend/src/components/Agents/AgentPluginsTab.tsx` — Agent plugins tab (installed + discover; source badge; `plugin_results` error surface)
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
| `source_commit_hash` | Pinned commit for external (url) plugins |
| `commit_hash` | Commit at which plugin config was last parsed |
| `config` | Full `plugin.json` stored as JSON for reference |

**Table: `agent_plugin_link`** (`AgentPluginLink`) — migration `2ca38822e945`

| Field | Purpose |
|-------|---------|
| `agent_id` | FK → `agent` (CASCADE) |
| `plugin_id` | FK → `llm_plugin_marketplace_plugin` (`ON DELETE SET NULL`); **nullable** — NULL for bundle-sourced links |
| `source` | `PluginSource` enum: `marketplace` \| `bundle` |
| `snapshot_marketplace_name` | Bundle: on-disk dir segment + manifest label (also used for display) |
| `snapshot_plugin_name` | Bundle: plugin dir name |
| `snapshot_config` | Bundle: frozen `plugin.json` JSON (for UI display when marketplace row unavailable) |
| `installed_version`, `installed_commit_hash` | Version pinning at install time |
| `conversation_mode`, `building_mode` | Per-mode activation flags |
| `disabled` | Files on disk but excluded from `settings.json` when true |

Uniqueness: `idx_agent_plugin_unique(agent_id, plugin_id, UNIQUE)` covers marketplace links (plugin_id NOT NULL). Bundle links (plugin_id NULL) are deduped at service layer by `(agent_id, snapshot_marketplace_name, snapshot_plugin_name)` — Postgres treats NULLs as distinct in a unique index.

**Table: `agent_bundle_revision`** — new column, migration `2ca38822e945`

| Field | Purpose |
|-------|---------|
| `plugin_specs` | JSON list of plugin snapshots (NOT NULL, default `[]`). Shape: `{marketplace_name, plugin_name, version, commit_hash, conversation_mode, building_mode, disabled, config, snapshot_subdir}` |

**`PluginSource` enum** (`backend/app/models/plugins/llm_plugin.py`)
```python
class PluginSource(str, Enum):
    marketplace = "marketplace"
    bundle = "bundle"
```

**`PluginInstallResult`** (response model, not a table)

| Field | Values |
|-------|--------|
| `plugin_name`, `marketplace_name`, `source` | Identity |
| `status` | `"installed"` \| `"skipped"` \| `"failed"` |
| `error_message` | Populated on `"failed"` |

`"skipped"` means the `.cinna_plugin_ref` marker already matches the pinned commit — re-clone was unnecessary.

**`EnvironmentSyncStatus`** — extended (non-table)

| Field | Purpose |
|-------|---------|
| `plugin_results` | `list[PluginInstallResult]` from the container install for this env |
| `partial_failures` | True when any `plugin_results` entry is `"failed"` |

**`PluginSyncResponse`** — extended (non-table)

| Field | Purpose |
|-------|---------|
| `plugin_results` | Aggregated, deduplicated `failed` entries across all synced envs |
| `partial_failures` | True when any plugin failed (env transport may still have succeeded) |

## API Endpoints

All routes in `backend/app/api/routes/llm_plugins.py`:

**Marketplace (admin)**
- `POST /api/v1/llm-plugins/marketplaces` — Create marketplace (temp-clone sync triggered immediately)
- `GET /api/v1/llm-plugins/marketplaces` — List marketplaces
- `GET /api/v1/llm-plugins/marketplaces/{id}` — Get marketplace detail
- `PUT /api/v1/llm-plugins/marketplaces/{id}` — Update marketplace
- `DELETE /api/v1/llm-plugins/marketplaces/{id}` — Delete marketplace (no cache to clean up)
- `POST /api/v1/llm-plugins/marketplaces/{id}/sync` — Trigger re-sync (temp-clone, parse, discard)

**Plugin Discovery**
- `GET /api/v1/llm-plugins/discover` — Discover plugins (search/filter across accessible marketplaces — reads Postgres, not disk)
- `GET /api/v1/llm-plugins/marketplaces/{id}/plugins` — List plugins in a marketplace

**Agent Plugin Management** (all return `PluginSyncResponse` except GET)
- `GET /api/v1/llm-plugins/agents/{agent_id}/plugins` → `AgentPluginLinksPublic` (includes `has_update`, `disabled` flags, `source`, snapshot fields)
- `POST /api/v1/llm-plugins/agents/{agent_id}/plugins` — Install plugin
- `DELETE /api/v1/llm-plugins/agents/{agent_id}/plugins/{link_id}` — Uninstall plugin
- `PUT /api/v1/llm-plugins/agents/{agent_id}/plugins/{link_id}` — Update mode/disabled flags
- `POST /api/v1/llm-plugins/agents/{agent_id}/plugins/{link_id}/upgrade` — Upgrade to latest version

## Services & Key Methods

**`backend/app/services/plugins/llm_plugin_service.py` — `LLMPluginService`**

Marketplace Management:
- `create_marketplace()` — Creates record, generates temp name from URL
- `sync_marketplace()` — Temp-clones to `tempfile.mkdtemp()`, parses plugins, updates metadata, writes git coordinates to Postgres, discards clone in `finally` block. No `MARKETPLACE_CACHE_DIR`.
- `_parse_claude_marketplace()` — Parses `.claude-plugin/marketplace.json`, handles local and URL source types
- `_upsert_plugins()` — Adds new, updates changed, removes deleted plugins from DB

Access & Route Helpers (added with route refactor):
- `verify_agent_access(session, agent_id, user)` — Verifies agent exists and caller is owner or superuser; raises `HTTPException(404)` / `HTTPException(403)`. Used by all agent-plugin routes as a thin shared guard, replacing inline ownership checks.
- `get_marketplace_with_access_check(session, marketplace_id, user, require_write=False)` — Fetches a marketplace and enforces access: read level (owner, public, or superuser) when `require_write=False`; write level (owner or superuser only — public does not grant write) when `require_write=True`. Raises `HTTPException(404)` / `HTTPException(403)`. Used by marketplace read routes (GET detail, GET plugins) and mutation routes (PUT, DELETE, sync).
- `get_plugin_public(session, plugin, marketplace_name=None)` — Projects a `LLMPluginMarketplacePlugin` to its `LLMPluginMarketplacePluginPublic` schema. When `marketplace_name` is omitted it is read from the plugin's relationship. Shared by `discover_plugins()` and the `GET /plugins/{id}` route to guarantee consistent projection.

Plugin Discovery:
- `discover_plugins()` — Returns plugins from accessible marketplaces with search/filter. Reads Postgres only — no disk access.

Agent Plugin Management:
- `install_plugin_for_agent()` — Creates `AgentPluginLink` with `source=marketplace`, version and commit pinning
- `uninstall_plugin_from_agent()` — Removes plugin link (prune happens at next manifest apply)
- `get_agent_plugins()` — Returns plugins with computed `has_update` and display fields. Bundle plugins: `has_update=False` (updates arrive via bundle apply-update); display resolves from snapshot fields. Marketplace plugins: display resolves from live plugin row.
- `update_plugin_modes()` — Updates `conversation_mode`, `building_mode`, `disabled`
- `upgrade_agent_plugin()` — Updates link to latest version and commit hash

Manifest Builder (v2 — replaces old file-payload builder):
- `build_plugin_manifest(session, agent_id, allowed_tools)` → `{"plugins": [...], "allowed_tools": [...]}`. For `source=marketplace` links: resolves git URL + ref + subdir from live plugin/marketplace rows. For `source=bundle` links: emits `git: null`, identity from snapshot fields.
- `_resolve_plugin_git_coords(link, plugin, marketplace)` — Resolves `{url, ref, subdir}` per source type:
  - `local` plugin: `{url: _normalize_public_git_url(marketplace.url), ref: link.installed_commit_hash or plugin.commit_hash or marketplace.git_branch, subdir: source_path}`.
  - `url` plugin: `{url: _normalize_public_git_url(plugin.source_url), ref: plugin.source_commit_hash or plugin.source_branch, subdir: ""}`. The ref chain deliberately uses the **external repo's own** commit/branch fields. `plugin.commit_hash` is NOT used here — for `url` plugins that field holds the marketplace repo's commit, which does not exist in the external `source_url` repo and would cause `git checkout` to fail. Known limitation: until `source_commit_hash` is captured at install time (a documented follow-up), `url` plugins track the external branch tip rather than being commit-pinned.
- `_normalize_public_git_url(url)` — Rewrites SSH-form URLs for well-known public hosts (`github.com`, `gitlab.com`, `bitbucket.org`) to their HTTPS equivalents. The container has no SSH key for these hosts, so a URL like `git@github.com:org/repo.git` would fail with `Permission denied (publickey)` even for a fully public repo. Only the exact host set is rewritten — unknown or private hosts are returned unchanged so the private-marketplace SSH-key path (deferred follow-up) is never disrupted. Applied in `_resolve_plugin_git_coords` (primary, manifest build path).

Environment Sync:
- `sync_plugins_to_agent_environments(session, agent_id, user_id, plugin_link, message_prefix)` — Queries running/suspended environments, activates suspended ones, calls `adapter.set_plugins(manifest)`, collects `PluginInstallResult` lists, deduplicates failures across environments for the top-level `PluginSyncResponse`. The optional `message_prefix` param is prepended to the response `message` string so callers (e.g. the uninstall and upgrade routes) do not need to post-process it.
- `_coerce_install_results()` — Normalizes raw adapter output (list of dicts) into `PluginInstallResult` objects.
- `_add_unique_failure()` — Dedup key: `(marketplace_name, plugin_name, source)`.

**`backend/app/services/environments/environment_lifecycle.py`**
- `_sync_plugins_to_environment()` — Builds manifest, calls `adapter.set_plugins(manifest)`, returns raw result dicts. Transport failures are swallowed (logged) — never blocks env start.
- `_surface_plugin_failures()` — Best-effort: emits `PLUGIN_SYNC_WARNING` event and dispatches `PLUGIN_SYNC_FAILED` notification. Every sub-call is individually try/except so surfacing itself can never break env start.
- `_setup_new_container()` — After `install_custom_packages()` and `install_system_packages()`, calls `_sync_plugins_to_environment()` — the "like libraries" reinstall step for new/rebuilt containers.

**`backend/app/services/environments/adapters/docker_adapter.py`**
- `set_plugins(manifest)` — `POST /config/plugins` with manifest JSON; `timeout=300.0` (git clones can take time). Returns `body["results"]` (per-plugin dicts).

**`backend/app/services/bundles/plugin_sync.py`** (new module)
- `snapshot_plugin_specs(links)` — Projects `AgentPluginLink` rows into revision `plugin_specs` shape.
- `materialise(session, install, revision)` — Creates `source=bundle` links from `revision.plugin_specs` at install time. Collision-guards against existing `source=marketplace` links of the same `(mkt_name, plugin_name)`.
- `merge(session, install, revision)` — Reconciles bundle links on apply-update: unchanged signature → keep row and consumer toggles, refresh frozen metadata; signature gone → delete link; new/changed signature → create link. Never touches `source=marketplace` links. Commits at end.
- `sig(source)` — Behavioral signature `(snapshot_marketplace_name, snapshot_plugin_name)` for both rows and spec dicts.

**`backend/app/services/bundles/publish_service.py`**
- `_collect_plugin_specs(session, install)` — Calls `snapshot_plugin_specs` on publisher's `AgentPluginLink` rows; result stored in `AgentBundleRevision.plugin_specs`.
- `_copy_plugins_tree(src, dest)` — Copies publisher's `app/workspace/plugins/<mkt>/<plugin>/` file trees into the snapshot; skips top-level `manifest.json` and `settings.json` (regenerated per consumer).

**`backend/app/services/environments/workspace_copy.py`**
- `_seed_plugins_tree(src_plugins, dst_plugins)` — Overlays snapshot `plugins/` onto the consumer workspace: copies top-level marketplace dirs while leaving the consumer's own marketplace dirs untouched.

**Agent-Env: `agent_env_service.py`** (container-side)
- `install_plugins(manifest)` — Main install routine: write `manifest.json` → for each entry call `_ensure_marketplace_plugin` or `_ensure_bundle_plugin` → prune removed dirs → regenerate `settings.json` (files-present only) → return result dicts.
- `_normalize_public_git_url(url)` — Duplicated helper (mirrors `LLMPluginService._normalize_public_git_url`; the container cannot import backend code). Rewrites SSH-form URLs for `github.com`, `gitlab.com`, and `bitbucket.org` to HTTPS. The host set is kept in sync with the backend copy manually.
- `_ensure_marketplace_plugin(plugin_dir, git)` — Checks `.cinna_plugin_ref` marker for idempotency (commit-hash refs only; branch refs always re-fetch). Calls `_normalize_public_git_url` on the incoming URL as a defensive measure — the backend manifest builder already normalizes, but a stale manifest or a direct caller could still carry an SSH-form URL. Runs `git clone --no-checkout --depth 1`, fetches pinned ref, checks out, copies subdir into `plugin_dir`, writes marker. Returns `(status, error_message)`.
- `_ensure_bundle_plugin(plugin_dir)` — Verifies snapshot-seeded files exist; returns `("installed", None)` or `("failed", "Bundle plugin files missing from workspace snapshot")`.
- `_prune_plugin_dirs(wanted)` — Removes `<mkt>/<plugin>/` dirs under `plugins_dir` not in the manifest; safe-segment validation guards against traversal.
- `_regenerate_plugin_settings(entries, allowed_tools)` — Builds `settings.json` only from entries where `disabled=False` AND `plugin_dir.exists()`. The existence guard is the belt-and-suspenders guarantee that the SDK never receives a missing path.
- `get_active_plugins_for_mode(mode)` — Reads `settings.json`, filters by `conversation_mode` or `building_mode`.
- `get_opencode_plugin_artifacts(mode)` — For each active plugin in the mode: reads `.mcp.json` or `.claude-plugin/plugin.json` for declared MCP servers; collects `commands/*.md` paths; detects unsupported capabilities (skills/agents/hooks dirs). Returns `{mcp_servers, command_files, unsupported}`.

**Agent-Env: `opencode_sdk_adapter.py`** (container-side)
- `_materialize_opencode_config(mode_config_dir)` — Calls `get_opencode_plugin_artifacts(mode)`, merges plugin MCP servers into `opencode.json` (namespaced `plugin_<mkt>_<plugin>_<server>` keys so they never clobber user-configured MCP servers), copies plugin `commands/*.md` into the runtime command dir.
- `plugin_capability_warning` SYSTEM event — Emitted when any plugin has unsupported capabilities (skills/agents/hooks). Non-blocking; tells the owner rather than silently dropping.

**Agent-Env: `sdk_manager.py`** (container-side)
- In `send_message_stream()`: calls `get_active_plugins_for_mode(mode)`, builds `[{"type": "local", "path": ...}]` array, passes to `ClaudeAgentOptions(plugins=...)`.

## Workspace Structure (Inside Agent-Env)

```
/app/workspace/plugins/
├── manifest.json          # Backend-authored SSOT (git coords + flags). Persisted in bind-mount.
├── settings.json          # Derived by install routine (active + files-present plugins only)
└── <marketplace_name>/
    └── <plugin_name>/     # Files materialized by install routine (or seeded from bundle snapshot)
        ├── .cinna_plugin_ref   # Idempotency marker: records the git ref at checkout
        ├── .claude-plugin/
        │   └── plugin.json
        ├── .mcp.json           # Plugin-declared MCP servers (optional)
        ├── commands/           # Slash commands (*.md) — copied into OpenCode runtime dir
        ├── skills/             # Agent skills (OpenCode: unsupported, reported)
        └── agents/             # Composed agents (OpenCode: unsupported, reported)
```

`settings.json` structure:
```json
{
  "active_plugins": [
    {
      "marketplace_name": "...",
      "plugin_name": "...",
      "path": "/app/workspace/plugins/<mkt>/<plugin>",
      "conversation_mode": true,
      "building_mode": false,
      "version": "...",
      "commit_hash": "..."
    }
  ],
  "allowed_tools": ["..."]
}
```

## Bundle Snapshot Layout

```
<BUNDLE_STORAGE_DIR>/<bundle_id>/<rev>/
├── manifest.json            # plugin_specs[] included
├── scripts/ docs/ knowledge/ files/
├── workspace_requirements.txt  workspace_system_packages.txt
└── plugins/                 # Plugin file trees (no settings.json or manifest.json)
    └── <marketplace_name>/<plugin_name>/
```

The snapshot `plugins/` is copied into the consumer's bind-mounted workspace by `workspace_copy._seed_plugins_tree`. The consumer's own `source=marketplace` plugin directories are left untouched (the overlay only writes top-level marketplace dirs from the snapshot).

## Plugin Manifest Format

`/app/workspace/plugins/manifest.json`:

```json
{
  "plugins": [
    {
      "marketplace_name": "claude-plugins-official",
      "plugin_name": "frontend-design",
      "source": "marketplace",
      "git": {
        "url": "https://github.com/anthropics/claude-plugins-official.git",
        "ref": "f1be96f0...",
        "subdir": "plugins/frontend-design"
      },
      "conversation_mode": true,
      "building_mode": true,
      "disabled": false,
      "version": "1.2.0",
      "commit_hash": "f1be96f0..."
    },
    {
      "marketplace_name": "acme-tools",
      "plugin_name": "pdf-helper",
      "source": "bundle",
      "git": null,
      "conversation_mode": true,
      "building_mode": false,
      "disabled": false,
      "version": "2.0.1",
      "commit_hash": null
    }
  ],
  "allowed_tools": null
}
```

## Marketplace File Format

Repository root: `.claude-plugin/marketplace.json`

Fields: `name`, `description`, `author` (name, email), `plugins` (array)

**Local plugin source**: `{"name": "plugin-name", "source": "./plugins/plugin-name", ...}`

**URL plugin source**: `{"name": "...", "source": {"source": "url", "url": "https://github.com/org/repo.git", "branch": "main"}, ...}`

External repos (URL type) must contain `.claude-plugin/plugin.json` with plugin configuration.

## Notification Catalog Entry

`NotificationType.PLUGIN_SYNC_FAILED` (`notification_catalog.py`):
- Label: "Plugin install failures"
- Template: `plugin_sync_failed.html`
- Default: email enabled
- Dedup scope: `environment_id` (prevents spam on flaky marketplace per start/rebuild within throttle window)
- Subject: `{PROJECT_NAME} — Plugin install failed for {instance_name}`

## Realtime Event

`EventType.PLUGIN_SYNC_WARNING = "plugin_sync_warning"` (`models/events/event.py`):
- Emitted by `_surface_plugin_failures` when any plugin result is `"failed"`
- Payload: `{agent_id, environment_id, instance_name, failures: [{marketplace_name, plugin_name, source, error_message}]}`
- Frontend: subscribes → shows amber banner on Plugins tab + invalidates plugin query

## Security

- Admin-only access for marketplace CRUD operations (superuser guard on routes)
- `public_discovery` flag gates plugin visibility to non-owners
- SSH key references use the shared `user_ssh_keys` table; private key material is never exposed via API
- Path safety: `_is_safe_plugin_segment(segment)` validates marketplace/plugin names are simple alphanumeric path segments (no separators, no traversal). Applied at manifest apply, plugin dir resolution, and prune enumeration.
- `_safe_plugin_dir()` confirms the resolved path is strictly contained within `plugins_dir` (defence in depth via `relative_to`).
- `settings.json` only contains plugins whose directories exist on disk — no missing paths ever reach the SDK.
- Plugin files never contain secret material; file contents are never logged.
- Container git clone is scoped to `/tmp` temp dirs; files are moved atomically after checkout.
- **Git fetch — public SSH URL normalization**: Marketplaces and external plugin repos may be registered with SSH-form URLs (`git@github.com:org/repo.git`). The agent-env container carries no SSH key for public hosts, so such a URL would fail with `Permission denied (publickey)` even for a fully public repo. `_normalize_public_git_url` in both `LLMPluginService` (backend, manifest build) and `AgentEnvService` (container, defensive) rewrites SSH URLs for `github.com`, `gitlab.com`, and `bitbucket.org` to their HTTPS equivalents. Only exact host-set membership triggers the rewrite — lookalikes and private hosts are left unchanged, preserving the (deferred) private-marketplace SSH-key injection path.
