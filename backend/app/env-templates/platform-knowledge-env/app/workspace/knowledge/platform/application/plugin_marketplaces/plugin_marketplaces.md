# Plugin Marketplaces

## Purpose

Allows platform admins to register Git-based plugin repositories (marketplaces) that make curated plugin catalogs available to users across the platform. Marketplaces are the supply side of the plugin system — they provide the plugin metadata (including Git coordinates) that agents use to install plugins. Agents consume plugins from them via the agent_plugins feature.

## Core Concepts

- **Marketplace** — A Git repository registered by an admin. The repository contains a `.claude-plugin/marketplace.json` catalog describing available plugins.
- **Sync** — The process of cloning the marketplace repo to a **throwaway temp directory**, parsing the catalog to extract plugin metadata and Git coordinates, writing those to Postgres, and then discarding the clone. No persistent backend cache is maintained.
- **Public Discovery** — Flag that controls whether a marketplace's plugins are visible to all users on the platform or only to the owner.
- **Marketplace Status** — Lifecycle state of the connection to the Git repo: `pending` → `connected` / `error` / `disconnected`.
- **SSH Key** — Optional reference to a stored SSH key for accessing private Git repositories.
- **Git Coordinates** — The `url`, `commit_hash`, `source_path`/`source_url`, and branch stored in Postgres for each plugin. These are what the container uses at install time to fetch plugin files — not a cached copy.

## What Changed (Resilient Plugin System)

Previously, marketplace sync populated a persistent cache directory on the backend container (`/app/data/marketplaces`). Plugin files were read from this cache and pushed as base64-encoded bytes to agent environments. When the backend container was recreated, the cache was lost; the DB still said `connected`; plugin file pushes silently failed; `settings.json` referenced directories that were never created.

Now, sync uses a **throwaway temp clone**: the repo is cloned to `tempfile.mkdtemp()`, parsed, Postgres is updated with git coordinates, and the clone is deleted in a `finally` block. There is no `/app/data/marketplaces` directory. Plugin files are never held by the backend. At install time, the agent container itself fetches plugin files directly from the Git URL at the pinned commit.

This means marketplace sync is now purely a metadata operation — fast, stateless, and with no persistent footprint.

## User Stories / Flows

### Admin: Register a Marketplace
1. Admin navigates to Admin → Marketplaces and clicks "Add Marketplace".
2. Enters Git repo URL, optional branch (defaults to main), and optionally selects an SSH key for private repos.
3. Backend creates a `LLMPluginMarketplace` record and immediately triggers an initial sync.
4. Sync clones the repo to a temp dir, reads `.claude-plugin/marketplace.json`, extracts name/description/author metadata and plugin records (with git coordinates), writes them to Postgres, and discards the clone.
5. Marketplace status changes to `connected` on success or `error` on failure.

### Admin: View Marketplace Plugins
1. Admin opens a marketplace detail page and clicks the Plugins tab.
2. A read-only list of all plugins parsed from the catalog is shown with source type, category, and version — all from Postgres.

### Admin: Re-sync a Marketplace
1. Admin clicks the sync button on the marketplace detail page.
2. Backend temp-clones latest commits, compares with `sync_commit_hash`, upserts changed plugins, removes deleted ones, discards the clone.
3. `sync_commit_hash` is updated to the current HEAD. This enables `has_update` detection for installed agent plugins.

### Admin: Control Plugin Visibility
1. Admin edits the marketplace configuration and toggles `Public Discovery`.
2. When disabled, only the marketplace owner can discover plugins from this marketplace.
3. When enabled, all platform users can browse and install plugins from this marketplace.

### Admin: Delete a Marketplace
1. Admin deletes a marketplace record.
2. Associated `LLMPluginMarketplacePlugin` records are removed.
3. There is no persistent cache to clean up — the delete comment in `delete_marketplace()` confirms this explicitly.
4. Agents that had plugins from this marketplace installed retain their `AgentPluginLink` records, but `plugin_id` will become NULL (via `ON DELETE SET NULL`) — the git coordinates are lost for upgrade purposes.

## Business Rules

- **Admin-only management**: Creating, updating, deleting, and syncing marketplaces requires superuser access.
- **Automatic initial sync**: Marketplace registration always triggers an immediate sync.
- **Temp-clone, no persistent cache**: Every sync (initial or manual) clones to a new throwaway directory and discards it. The backend holds only Postgres rows — no plugin files on disk.
- **Update detection**: `sync_commit_hash` stores the HEAD commit of the last sync. Comparing installed plugin commit hashes against `LLMPluginMarketplacePlugin.commit_hash` enables `has_update` detection on agent plugin links.
- **Upsert behavior**: Sync adds new plugins, updates changed plugin configs, and removes plugins that are no longer in `marketplace.json`. It does not uninstall plugins already installed by agents.
- **SSH key scope**: The SSH key is scoped to the marketplace owner (same `user_ssh_keys` table used by knowledge source Git repos).
- **Marketplace format**: Currently only the `"claude"` format (`.claude-plugin/marketplace.json`) is supported. The `type` field is reserved for future format extensions.
- **Plugin source types within a marketplace**:
  - `local` — Plugin files live inside the marketplace repo at a relative path (`source_path`). Git coordinates stored: `marketplace.url` + commit hash.
  - `url` — Plugin files live in an external Git repo (`source_url`). Git coordinates stored: `source_url` + `source_commit_hash` or branch.

## Architecture Overview

```
Admin UI → POST /api/v1/llm-plugins/marketplaces
         → LLMPluginMarketplace record created
         → sync_marketplace() triggered
               → tempfile.mkdtemp() → git clone repo to temp dir
               → _parse_claude_marketplace()
                     → reads .claude-plugin/marketplace.json
                     → _upsert_plugins() → LLMPluginMarketplacePlugin records (git coords)
               → status = "connected", sync_commit_hash = HEAD
               → shutil.rmtree(temp_dir)   # no persistent cache

User → GET /api/v1/llm-plugins/discover
      → Filters by public_discovery OR ownership (Postgres query — no disk)
      → Returns LLMPluginMarketplacePlugin list (with git coordinates)

Agent install → LLMPluginService.build_plugin_manifest()
              → reads git coords from Postgres
              → manifest pushed to container
              → container git-clones plugin files at pinned commit

Update detection → compare AgentPluginLink.installed_commit_hash
                              vs LLMPluginMarketplacePlugin.commit_hash
                 → sets has_update flag on GET agent plugins
```

## Integration Points

- **Agent Plugins** — Marketplace sync populates the Postgres rows (including git coordinates) that `build_plugin_manifest()` uses at plugin install time. Marketplace re-syncs propagate `has_update` to installed agent plugin links. See [agent_plugins](../../agents/agent_plugins/agent_plugins.md).
- **SSH Keys** — Uses the same `user_ssh_keys` table as knowledge source Git repos for private repository authentication. See [ssh_keys](../ssh_keys/ssh_keys.md).
- **Git Operations** — Shares the `backend/app/services/knowledge/git_operations.py` utility with knowledge sources for clone/pull operations.
