# Plugin Marketplaces

## Purpose

Allows platform admins to register Git-based plugin repositories (marketplaces) that make curated plugin catalogs available to users across the platform. Marketplaces are the supply side of the plugin system — agents consume plugins from them via the agent_plugins feature.

## Core Concepts

- **Marketplace** — A Git repository registered by an admin. The repository contains a `.claude-plugin/marketplace.json` catalog describing available plugins.
- **Sync** — The process of cloning or pulling the marketplace repo and parsing the catalog to update plugin records in the database.
- **Public Discovery** — Flag that controls whether a marketplace's plugins are visible to all users on the platform or only to the owner.
- **Marketplace Status** — Lifecycle state of the connection to the Git repo: `pending` → `connected` / `error` / `disconnected`.
- **SSH Key** — Optional reference to a stored SSH key for accessing private Git repositories.

## User Stories / Flows

### Admin: Register a Marketplace
1. Admin navigates to Admin → Marketplaces and clicks "Add Marketplace".
2. Enters Git repo URL, optional branch (defaults to main), and optionally selects an SSH key for private repos.
3. Backend creates a `LLMPluginMarketplace` record and immediately triggers an initial sync.
4. Sync clones the repo, reads `.claude-plugin/marketplace.json`, extracts name/description/author metadata, and creates `LLMPluginMarketplacePlugin` records.
5. Marketplace status changes to `connected` on success or `error` on failure.

### Admin: View Marketplace Plugins
1. Admin opens a marketplace detail page and clicks the Plugins tab.
2. A read-only list of all plugins parsed from the catalog is shown with source type, category, and version.

### Admin: Re-sync a Marketplace
1. Admin clicks the sync button on the marketplace detail page.
2. Backend pulls latest commits, compares with `sync_commit_hash`, upserts changed plugins, removes deleted ones.
3. `sync_commit_hash` is updated to the current HEAD.

### Admin: Control Plugin Visibility
1. Admin edits the marketplace configuration and toggles `Public Discovery`.
2. When disabled, only the marketplace owner can discover plugins from this marketplace.
3. When enabled, all platform users can browse and install plugins from this marketplace.

### Admin: Delete a Marketplace
1. Admin deletes a marketplace record.
2. Associated `LLMPluginMarketplacePlugin` records are removed.
3. Agents that had plugins from this marketplace installed retain their `AgentPluginLink` records but the plugin source is no longer resolvable for upgrades.

## Business Rules

- **Admin-only management**: Creating, updating, deleting, and syncing marketplaces requires superuser access.
- **Automatic initial sync**: Marketplace registration always triggers an immediate sync.
- **Update detection**: `sync_commit_hash` stores the HEAD commit of the last sync. Comparing installed plugin commit hashes against this enables `has_update` detection on agent plugin links.
- **Upsert behavior**: Sync adds new plugins, updates changed plugin configs, and removes plugins that are no longer in `marketplace.json`. It does not uninstall plugins already installed by agents.
- **SSH key scope**: The SSH key is scoped to the marketplace owner (same `user_ssh_keys` table used by knowledge source Git repos).
- **Marketplace format**: Currently only the `"claude"` format (`.claude-plugin/marketplace.json`) is supported. The `type` field is reserved for future format extensions.
- **Plugin source types within a marketplace**:
  - `local` — Plugin files live inside the marketplace repo at a relative path.
  - `url` — Plugin files live in an external Git repo, cached separately.

## Architecture Overview

```
Admin UI → POST /api/v1/llm-plugins/marketplaces
         → LLMPluginMarketplace record created
         → sync_marketplace() triggered
               → git_operations.clone_or_pull(repo_url, ssh_key)
               → _parse_claude_marketplace()
                     → reads .claude-plugin/marketplace.json
                     → _upsert_plugins() → LLMPluginMarketplacePlugin records
               → status = "connected", sync_commit_hash = HEAD

User → GET /api/v1/llm-plugins/discover
      → Filters by public_discovery OR ownership
      → Returns LLMPluginMarketplacePlugin list

Update detection → compare AgentPluginLink.installed_commit_hash
                              vs LLMPluginMarketplacePlugin.commit_hash
                 → sets has_update flag on GET agent plugins
```

## Integration Points

- **Agent Plugins** — Plugins discovered from marketplaces are installed per-agent via `AgentPluginLink`. Marketplace re-syncs propagate `has_update` to installed agent plugins. See [agent_plugins](../../agents/agent_plugins/agent_plugins.md).
- **SSH Keys** — Uses the same `user_ssh_keys` table as knowledge source Git repos for private repository authentication. See [ssh_keys](../ssh_keys/ssh_keys.md).
- **Git Operations** — Shares the `backend/app/services/knowledge/git_operations.py` utility with knowledge sources for clone/pull operations.
