# Agent Plugins

## Purpose

Extends agent capabilities by installing plugins from curated Git-based marketplaces. Plugins provide additional commands, skills, hooks, and MCP servers that are loaded into the Claude SDK at runtime based on the active session mode.

## Core Concepts

- **Marketplace** — A Git repository containing a plugin catalog (`marketplace.json`). Admins register marketplaces by URL; the backend syncs and parses the catalog.
- **Plugin** — An individual capability extension defined within a marketplace. Has a source type (`local` or `url`), version, category, and author.
- **AgentPluginLink** — The installed relationship between an agent and a plugin, including version pinning and per-mode activation flags.
- **Conversation Mode** — Plugin is active during workflow execution (Haiku model sessions).
- **Building Mode** — Plugin is active during agent configuration and development (Sonnet model sessions).
- **Disabled State** — Plugin files remain synced to environments but are not loaded into the SDK; enables quick toggling without re-downloading files.

## User Stories / Flows

### Admin: Register a Marketplace
1. Admin navigates to Admin → Marketplaces and clicks "Add Marketplace".
2. Enters a Git repo URL and optionally selects an SSH key for private repos.
3. Backend clones the repo, parses `.claude-plugin/marketplace.json`, and creates plugin records.
4. Marketplace becomes visible to users with the appropriate discovery setting.

### User: Discover and Install a Plugin
1. User opens an agent's Plugins tab and browses the "Discover Plugins" grid.
2. Clicks Install on a plugin; selects Conversation Mode and/or Building Mode.
3. Backend creates an `AgentPluginLink` pinned to the current plugin version and commit hash.
4. Plugin files are immediately synced to all running and suspended environments for that agent.

### User: Manage Installed Plugins
- **Enable/Disable**: Toggle the switch on the Installed Plugins table. Files remain synced; the plugin is removed from or added to the SDK context.
- **Mode toggles**: Enable per-mode (Conversation / Building) independently.
- **Upgrade**: When a newer commit is available, an Upgrade button appears. Explicit action required — plugins never auto-update.
- **Uninstall**: Removes the plugin link and syncs removal to all environments.

### Plugin Sync on Changes
1. Any install, uninstall, upgrade, or enable/disable action triggers a sync.
2. Backend targets all **running** and **suspended** environments for the agent.
3. Suspended environments are activated first, then synced.
4. A `PluginSyncResponse` is returned with per-environment status: success, error, activated-and-synced, or skipped.
5. Sync errors display a detailed dialog; successful syncs show a toast notification.

## Business Rules

- **Two marketplace visibility levels**: `public_discovery=true` makes plugins discoverable by all users; private marketplaces are only accessible to the owner.
- **Version pinning at install time**: `installed_version` (display) and `installed_commit_hash` (exact reproducibility) are stored. Environment rebuilds use the pinned commit, not the latest.
- **No auto-updates**: Plugins require explicit user action to upgrade. This prevents unexpected behavior changes.
- **Disabled ≠ Uninstalled**: Disabled plugins keep their files on disk but are excluded from the SDK `plugins` array at session start.
- **Mode independence**: A plugin can be active in Conversation Mode only, Building Mode only, both, or neither (disabled).
- **Sync to suspended environments**: Plugin changes always reach suspended environments (by activating them first) so they are up to date when resumed.
- **Source types**:
  - `local` — Plugin files are in the marketplace repo itself.
  - `url` — Plugin files are in an external Git repository, cloned separately.

## Architecture Overview

```
Admin → Marketplace Registration
      → Backend clones Git repo → Parses marketplace.json → Plugin records

User → Discover Plugins (Filtered view of accessible marketplaces)
     → Install Plugin → AgentPluginLink (version + commit pinned)
                      → sync_plugins_to_agent_environments()
                            → Running/Suspended Environments
                                  → docker_adapter.set_plugins()
                                        → agent-env /config/plugins
                                              → files written to /app/workspace/plugins/

Session Start → environment_lifecycle._sync_plugins_to_environment()
             → Reads AgentPluginLinks → Sends all plugin files to env

SDK Init → agent_env_service.get_active_plugins_for_mode(mode)
         → Returns enabled plugins matching current mode
         → ClaudeAgentOptions(plugins=[{"type": "local", "path": ...}])
```

## Integration Points

- **Agent Environments** — Plugin files are synced as part of environment data management. See [agent_environment_data_management](../agent_environment_data_management/agent_environment_data_management.md).
- **Agent Credentials** — Follows the same sync-on-change pattern: lifecycle hook on start + targeted sync on modification. See [agent_credentials](../agent_credentials/agent_credentials.md).
- **Agent Environment Core** — The agent-env SDK manager reads active plugins from `settings.json` at session start. See [agent_environment_core](../agent_environment_core/agent_environment_core.md).
- **SSH Keys** — Private marketplace repos use the same `ssh_key_id` pattern as knowledge source Git repos.
