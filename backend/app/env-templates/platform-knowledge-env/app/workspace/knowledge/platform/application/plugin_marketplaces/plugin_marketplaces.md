---
feature: plugin_marketplaces
domain: application
one_liner: "Lets admins register Git-based marketplaces of plugins and skills that users install into their agents, syncing metadata into Postgres with no persistent file cache."
docs:
  tech: plugin_marketplaces_tech.md
---
# Addon Marketplaces

> The admin surface is called **Addon Marketplaces** (sidebar, page title, create dialog). The feature id, the route (`/admin/marketplaces`) and every table name stay `marketplace` / `plugin`. "Addon" is the presentation-layer umbrella over plugins and skills — see [agent_addons](../../agents/agent_addons/agent_addons.md).

## Purpose

Allows platform admins to register Git-based repositories (marketplaces) that make curated catalogs of **plugins and skills** available to users across the platform. Marketplaces are the supply side of the plugin system — they provide the metadata (including Git coordinates) that agents use to install entries. Agents consume them via the agent_plugins feature.

## Core Concepts

- **Marketplace** — A Git repository registered by an admin, in one of **three formats** (see [Marketplace formats](#marketplace-formats)). A `claude` repository contains a `.claude-plugin/marketplace.json` catalog; a `codex` repository an `.agents/plugins/marketplace.json`; a `skills` repository has no catalog file at all and is walked as a directory of `SKILL.md` folders.
- **Supported / unsupported entry** — A sync-time verdict on whether *this platform's containers* can install an entry. Recorded as `supported` plus a stable `unsupported_reason` code. Unsupported entries are still listed — hiding them would make a half-broken marketplace look empty — but installing one is refused.
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
1. Admin navigates to Admin → **Addon Marketplaces** and clicks "Add addon marketplace".
2. Enters Git repo URL, picks the **Format** — "Claude plugins" (default) · "Codex plugins" · "Skills repository", with a help line naming the file each expects — optional branch (defaults to main), and optionally selects an SSH key for private repos.
3. Backend creates a `LLMPluginMarketplace` record and immediately triggers an initial sync.
4. Sync clones the repo to a temp dir, runs the parser **for that format**, extracts metadata and entry records (with git coordinates and a `supported` verdict per entry), writes them to Postgres, and discards the clone.
5. Marketplace status changes to `connected` on success or `error` on failure. A catalog that cannot be read fails **without deleting any existing entry**.

### Admin: View Marketplace Plugins
1. Admin opens a marketplace detail page and clicks the Plugins tab.
2. A read-only list of all entries parsed from the catalog is shown with source type, category, version, a **Supported** cell (tone dot + label, with the `unsupported_reason` sentence in a tooltip) and a **Skills** count — all from Postgres.

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
4. Agents that had plugins from this marketplace installed retain their `AgentPluginLink` records, and `plugin_id` becomes NULL via `ON DELETE SET NULL` — the git coordinates are lost for upgrade purposes.
5. **This only started working when the ORM cascade was fixed.** `LLMPluginMarketplacePlugin.agent_links` used to declare `cascade="all, delete-orphan"`, which deleted the links in Python before the database rule could apply — so an admin deleting a marketplace silently *uninstalled* every user's plugins. It is now `passive_deletes=True`, deferring to the FK. See [agent_addons_tech](../../agents/agent_addons/agent_addons_tech.md).
6. The orphaned links keep their names, because the install path snapshots `snapshot_marketplace_name` / `snapshot_plugin_name` on every source. If the entry reappears upstream, the next sync **re-attaches** them — but only when the marketplace names the **same repository** the install was made from, already existed when the link was created, and the link actually recorded a repository URL. All three are required: a reused *name* is not enough (the unique name index frees a name exactly when its holder is deleted, so a rename could otherwise hand an unrelated repository somebody else's installs), and a marketplace you delete and re-register from the same repository is younger than the links, so it does not adopt them either — those re-install by hand.

## Business Rules

- **Admin-only management**: Creating, updating, deleting, and syncing marketplaces requires superuser access.
- **Automatic initial sync**: Marketplace registration always triggers an immediate sync.
- **Temp-clone, no persistent cache**: Every sync (initial or manual) clones to a new throwaway directory and discards it. The backend holds only Postgres rows — no plugin files on disk.
- **Update detection**: `sync_commit_hash` stores the HEAD commit of the last sync. Comparing installed plugin commit hashes against `LLMPluginMarketplacePlugin.commit_hash` enables `has_update` detection on agent plugin links.
- **Upsert behavior**: Sync adds new plugins, updates changed plugin configs, and removes plugins that are no longer in the catalog. It does not uninstall plugins already installed by agents — a removed entry orphans its links instead, and re-attaches them if it comes back.
- **SSH key scope**: The SSH key is scoped to the marketplace owner (same `user_ssh_keys` table used by knowledge source Git repos).
- **Marketplace format**: `claude`, `codex` or `skills`, chosen at create time. The set is closed — an unknown `type` is a **422** on create or update, never a silent fallback to the Claude parser, and the parser registry raises rather than defaulting.
- **Format is a property of the marketplace, not of an entry**: every entry inherits `plugin_type` from its marketplace on each upsert.
- **Unsupported entries are flagged at sync, not at install**: `supported` and `unsupported_reason` are re-derived on every sync (never sticky). Discovery returns unsupported rows so the user can see *why*; `install_plugin_for_agent` refuses them with **409 `plugin_unsupported`** carrying the reason sentence.
- **A failed parse deletes nothing**: an unreadable catalog raises before the upsert step, and stale-row deletion lives only inside it. A temporarily broken upstream repo leaves the marketplace `status=error` with every existing entry intact — it cannot empty an admin's list or orphan anybody's installs. The API answer is **422 `marketplace_catalog_unreadable`** (or `unsupported_marketplace_type`).
- **A `skills` repository never renames its own marketplace**: the parser deliberately returns no name. `marketplace.name` is not a label — it is the on-disk directory segment every install writes into `plugins/<marketplace>/`, so letting an upstream README edit change it would move every install's directory on the next sync.
- **Plugin source types within a marketplace** (`claude` and `codex`; a `skills` repo is local-only):
  - `local` — Plugin files live inside the marketplace repo at a relative path (`source_path`). Git coordinates stored: `marketplace.url` + commit hash.
  - `url` — Plugin files live in an external Git repo (`source_url`). Git coordinates stored: `source_url` + `source_commit_hash` or branch.

## Marketplace formats

Chosen at create time from a **Format** select, and validated as a closed set.

| Format (`type`) | Catalog file | One entry is | Plugin manifest | Skills inside | Source kinds accepted |
|-----------------|--------------|--------------|-----------------|---------------|-----------------------|
| `claude` (default) | `.claude-plugin/marketplace.json` | a plugin | `.claude-plugin/plugin.json` | `<plugin>/skills/*/SKILL.md` | string path, `{source: url}` |
| `codex` | `.agents/plugins/marketplace.json` | a plugin | `.codex-plugin/plugin.json` | `<plugin>/skills/*/SKILL.md` | `local`, `url`, `git-subdir`; `npm` → unsupported |
| `skills` | **none** — a directory walk | **one skill** | none (synthesised in the container) | `skills/<name>/SKILL.md`, or `<name>/SKILL.md` for a repo that is nothing but skills | local only |

**Codex is a format, not an engine.** Containers keep running Claude Code and
OpenCode. A Codex plugin is normalised container-side into a synthesised
`.claude-plugin/plugin.json`; its `.app.json` connectors are ignored, and its
`policy` block is stored verbatim in the entry's `config` but **not enforced**.

**A `skills` marketplace produces one entry per skill folder.** The container
copies the tree to `plugins/<marketplace>/<name>/skills/<name>/`, the same layout
a skills-catalog install produces, and synthesises the plugin manifest locally.

### The five unsupported reasons

| Code | Meaning |
|------|---------|
| `npm_source` | published to npm, which agent environments cannot install from |
| `app_connector_only` | declares app connectors only, which this platform does not run |
| `no_skill_md` | no valid `SKILL.md`, so there is nothing to install |
| `unknown_source` | declares a source kind this platform cannot fetch from |
| `unsafe_path` | its `path` / `source_path` is absolute or contains `..`, so it points outside its repository |

`unsafe_path` is applied **per entry**: one bad path flags that entry rather than
failing the whole sync, mirroring the container's own `Unsafe plugin subdir`
guard so a bad entry fails visibly at sync instead of silently at every install.
Each code has exactly one sentence, owned by the server and mirrored client-side
so the create dialog, the admin table, the entries table and the Add-addon dialog
cannot print four different words for one row.

## Architecture Overview

```
Admin UI → POST /api/v1/llm-plugins/marketplaces
         → LLMPluginMarketplace record created
         → sync_marketplace() triggered
               → tempfile.mkdtemp() → git clone repo to temp dir
               → _get_parser_for_type(marketplace.type)   # claude | codex | skills
                     #  raises MarketplaceFormatError on an unknown type — no fallback
                     → reads the format's catalog file (or walks the skill folders)
                     → flags each entry supported / unsupported_reason
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
