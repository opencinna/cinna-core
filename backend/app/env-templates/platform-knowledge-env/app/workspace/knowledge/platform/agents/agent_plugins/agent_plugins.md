# Agent Plugins

## Purpose

Extends agent capabilities by installing plugins from curated Git-based marketplaces or from published bundles. Plugins provide additional slash commands, MCP servers, skills, hooks, and agents that are loaded into the Claude SDK at runtime based on the active session mode.

## Core Concepts

- **Marketplace** — A Git repository containing a plugin catalog (`marketplace.json`). Admins register marketplaces by URL; the backend syncs and parses the catalog to record plugin metadata (including Git coordinates) in Postgres. Syncs use a throwaway temp clone — no backend-side persistent cache.
- **Plugin** — An individual capability extension defined within a marketplace. Has a source type (`local` or `url`), version, category, and author.
- **AgentPluginLink** — The installed relationship between an agent and a plugin. Carries version pinning, per-mode activation flags, and a `source` indicating whether it came from a marketplace (`git`-fetched by the container) or a bundle (files seeded from the snapshot).
- **Plugin Manifest** — `/app/workspace/plugins/manifest.json` (bind-mounted, persistent) — the SSOT living in the env workspace. The backend writes git coordinates and per-mode flags; the container's install routine reads it to materialize files and regenerate `settings.json`. Mirrors how `workspace_requirements.txt` works for Python deps.
- **Container Install Routine** — `agent_env_service.install_plugins(manifest)` — runs inside the container at every setup (new container, post-rebuild) and on every plugin change. Fetches/ensures files, prunes removed plugins, regenerates `settings.json`. Returns a per-plugin `PluginInstallResult` — errors are results, not exceptions.
- **Conversation Mode** — Plugin is active during workflow execution.
- **Building Mode** — Plugin is active during agent configuration and development.
- **Disabled State** — Plugin directory remains on disk but is excluded from `settings.json` and from the SDK; enables quick toggling without re-downloading files.

## Plugin Source Abstraction

Every `AgentPluginLink` has a `source`:

- **`marketplace`** — files are fetched by the container at install time via `git clone` at the pinned commit. Git coordinates come from the Postgres marketplace rows, not a backend file cache. On rebuild the container re-fetches from the persisted manifest.
- **`bundle`** — files were seeded into the env workspace from a bundle revision snapshot. `plugin_id` is NULL (no marketplace needed). Identity comes from `snapshot_marketplace_name` / `snapshot_plugin_name`.

Both sources produce identical on-disk layout (`/app/workspace/plugins/<mkt>/<plugin>/`) and identical `settings.json` entries, so the adapters are source-agnostic.

## Failure Mode This Feature Fixes

The old system kept a per-marketplace git cache on the backend container filesystem (`/app/data/marketplaces`). Backend container recreation wiped it; the DB still said `connected`; plugin sync then silently wrote a `settings.json` referencing dirs it had never created. Claude Code received a `{type: local, path: <missing>}` entry and plugins appeared broken with no warning.

The resilient system removes the backend cache entirely from the hot path. Plugin files live in the bind-mounted env workspace, which survives backend container recreation, container stop/start, and rebuild.

## User Stories / Flows

### Admin: Register a Marketplace
1. Admin navigates to Admin → Marketplaces and clicks "Add Marketplace".
2. Enters a Git repo URL and optionally selects an SSH key for private repos.
3. Backend clones to a throwaway temp dir, parses `.claude-plugin/marketplace.json`, stores plugin metadata (name, version, git coordinates) in Postgres, then discards the clone. No persistent cache on disk.
4. Marketplace becomes visible to users with the appropriate discovery setting.

### User: Discover and Install a Plugin
1. User opens an agent's Plugins tab and browses the "Discover Plugins" grid.
2. Clicks Install on a plugin; selects Conversation Mode and/or Building Mode.
3. Backend creates an `AgentPluginLink` (source=marketplace) pinned to the current version and commit hash; builds the manifest with git coordinates; pushes it to running/suspended environments via `POST /config/plugins`.
4. Each receiving container's install routine clones the plugin at the pinned commit into `/app/workspace/plugins/<mkt>/<plugin>/`, regenerates `settings.json`, and returns per-plugin results.

### User: Manage Installed Plugins
- **Enable/Disable**: Toggle the switch on the Installed Plugins table. The manifest is updated; on the next install the plugin dir stays but the plugin is excluded from `settings.json`.
- **Mode toggles**: Enable per-mode (Conversation / Building) independently.
- **Upgrade**: When a newer commit is available (for marketplace plugins), an Upgrade button appears. Explicit action required — plugins never auto-update.
- **Uninstall**: Removes the plugin link, rebuilds the manifest, and on the next container install the directory is pruned.
- **Bundle plugins**: Source badge shows "From bundle". Upgrade/Uninstall buttons are hidden; enable/disable and per-mode toggles still work (consumer-local, survive bundle apply-update).

### Plugin Sync on Changes
1. Any install, uninstall, upgrade, or enable/disable action triggers a sync.
2. Backend builds the manifest (git coordinates + flags, no file bytes) once and delivers it to all **running** and **suspended** environments.
3. Suspended environments are activated first, then synced.
4. A `PluginSyncResponse` is returned with per-environment status plus `plugin_results` (per-plugin install outcomes) and `partial_failures` (true when any plugin failed).
5. Sync transport errors show a detailed dialog; per-plugin install failures are shown inline in the same dialog and also surface as a live amber banner (via `PLUGIN_SYNC_WARNING` realtime event).

### Reinstall on Rebuild / Self-Heal
When a container is rebuilt (or newly created), `_setup_new_container` calls `_sync_plugins_to_environment` after installing Python deps and system packages — exactly like the library install step. The container reads the persisted `manifest.json`, re-ensures every plugin at its pinned commit (skipping those already present via `.cinna_plugin_ref` idempotency marker), and regenerates `settings.json`. A rebuilt container deterministically ends with correct plugin files, sourced from the bind-mounted manifest.

## Business Rules

- **Two marketplace visibility levels**: `public_discovery=true` makes plugins discoverable by all users; private marketplaces are only accessible to the owner.
- **Version pinning at install time**: `installed_version` (display) and `installed_commit_hash` (exact reproducibility) are stored. Container installs use the pinned commit, not the latest.
- **No auto-updates**: Marketplace plugins require explicit user action to upgrade. Bundle plugins are updated when the user applies a bundle update.
- **Disabled ≠ Uninstalled**: Disabled plugins keep their files on disk but are excluded from `settings.json`. Enables quick toggling without re-cloning.
- **Mode independence**: A plugin can be active in Conversation Mode only, Building Mode only, both, or neither (disabled).
- **Sync to suspended environments**: Plugin changes always reach suspended environments (by activating them first) so they are up to date when resumed.
- **Non-blocking errors**: A failed plugin is excluded from `settings.json` (so the SDK never gets a missing path) and reported via `PluginInstallResult`. It never silently remains listed as active while absent on disk.
- **Bundle plugin rules**: Bundle plugins carry `plugin_id=NULL`. Upgrade/Uninstall are not available; the user's `disabled`/per-mode toggles survive bundle apply-update. The publisher must be able to resolve all declared plugins at publish time — a publish hard-blocks on an unresolvable plugin.
- **Collision guard**: A `source=bundle` plugin with the same `(marketplace_name, plugin_name)` as a consumer's existing `source=marketplace` plugin is skipped (not silently overwritten), logged, and reported.
- **Source types for marketplace plugins** (within a plugin catalog):
  - `local` — Plugin files are in the marketplace repo itself at `source_path`.
  - `url` — Plugin files are in an external Git repository (`source_url`).

## Architecture Overview

```
Admin → Marketplace Registration
      → Backend temp-clones Git repo → parses marketplace.json → Plugin records (git coords) in Postgres
      → temp clone discarded (no persistent cache)

User → Discover Plugins (filtered view of accessible marketplaces, live Postgres)
     → Install Plugin → AgentPluginLink (source=marketplace, version + commit pinned)
                      → build_plugin_manifest() (git coords + flags, no bytes)
                      → sync_plugins_to_agent_environments()
                            → Running/Suspended Environments
                                  → adapter.set_plugins(manifest)
                                        → POST /config/plugins (manifest JSON)
                                              → agent_env_service.install_plugins()
                                                    → git clone@commit → /app/workspace/plugins/<mkt>/<plugin>/
                                                    → prune removed dirs
                                                    → regenerate settings.json (files-present only)
                                                    → return PluginInstallResult[]

Container Setup (new / post-rebuild) → _setup_new_container()
  ├ install_custom_packages()   (existing — Python deps)
  ├ install_system_packages()   (existing — system packages)
  └ _sync_plugins_to_environment()   (new — re-ensure from persisted manifest)

Session Start → sdk_manager.get_active_plugins_for_mode(mode)
             → reads settings.json, filters by mode
             → ClaudeAgentOptions(plugins=[{"type": "local", "path": ...}])
             → OpenCode: merges plugin MCP servers + copies commands/ into runtime dir

Bundle Publish → PublishService._collect_plugin_specs() → plugin_specs[] in AgentBundleRevision
              → _copy_plugins_tree() → snapshot/plugins/<mkt>/<plugin>/ (no settings/manifest)

Bundle Install → workspace_copy seeds snapshot/plugins/ → env workspace/plugins/
              → InstallService._materialise_plugin_links() → source=bundle AgentPluginLinks

Bundle Apply-Update → plugin_sync.merge() → preserves consumer toggles for unchanged plugins
                   → re-seeds files from new snapshot → next setup re-ensures
```

## Non-Blocking Error Surfacing

A failed plugin install triggers two side-effects (both best-effort, never block env start):

1. **Realtime event** `PLUGIN_SYNC_WARNING` — emitted to the agent owner; frontend shows a live amber banner on the Plugins tab and invalidates the plugin query. Mirrors the `model_freshness` amber banner pattern.
2. **System notification** `PLUGIN_SYNC_FAILED` — emails the agent owner; deduped per `environment_id` by the notification throttle so a flaky marketplace doesn't spam on every start/rebuild. Copy shown in Settings → Notifications.

## Integration Points

- **Agent Environment Data Management** — Plugin files are part of the bind-mounted workspace. `_sync_plugins_to_environment` is called during `_sync_dynamic_data` (env start) and `_setup_new_container` (new/rebuilt). See [agent_environment_data_management](../agent_environment_data_management/agent_environment_data_management.md).
- **Agent Environment Core / Multi-SDK** — `sdk_manager.get_active_plugins_for_mode()` reads `settings.json` (files-present guard included) and passes active plugins to the Claude Code SDK. The OpenCode adapter materializes plugin MCP servers and copies `commands/*.md` into the runtime command dir. See [agent_environment_core](../agent_environment_core/agent_environment_core.md) and [multi_sdk](../agent_environment_core/multi_sdk.md).
- **Agent Bundles** — Bundle revisions carry `plugin_specs` and a `plugins/` file tree in the snapshot. `plugin_sync.materialise` creates `source=bundle` links at install time; `plugin_sync.merge` reconciles them on apply-update. See [agent_bundles](../agent_bundles/agent_bundles.md).
- **Agent Credentials** — Follows the same sync-on-change pattern: lifecycle hook on env start + targeted sync on modification. See [agent_credentials](../agent_credentials/agent_credentials.md).
- **Realtime Events** — `PLUGIN_SYNC_WARNING` event (amber banner) is emitted by `_surface_plugin_failures` using the same event bus pattern as `model_freshness`. See [realtime_events](../../application/realtime_events/event_bus_system.md).
- **System Notifications** — `PLUGIN_SYNC_FAILED` in the Notification Catalog mirrors `MODEL_DEPRECATED` (same dedup / throttle / email pattern). See [system_notifications](../../application/system_notifications/system_notifications.md).
- **Model Freshness** — The amber-banner + notification pattern is established here and mirrored by the plugin system. See [model_freshness](../agent_environments/model_freshness.md).
- **SSH Keys** — Private marketplace repos use the same `ssh_key_id` pattern as knowledge source Git repos.
- **Plugin Marketplaces** — Marketplace sync populates the Postgres rows the manifest builder reads; no persistent git cache involved at plugin-use time. See [plugin_marketplaces](../../application/plugin_marketplaces/plugin_marketplaces.md).
