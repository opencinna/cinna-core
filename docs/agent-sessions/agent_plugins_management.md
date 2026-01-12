# Agent Plugins Management

## Overview

Agent environments can be extended with plugins from configurable marketplaces. Plugins provide additional capabilities (commands, skills, hooks, MCP servers) that are loaded into the Claude SDK at runtime based on the session mode.

## Core Concepts

### Two-Tier Architecture

**Marketplaces** (Git repositories containing plugin catalogs)
- Admin creates marketplace by providing a Git repo URL
- Backend clones/syncs repository and parses `.claude-plugin/marketplace.json`
- Supports public/private visibility via `public_discovery` flag
- Marketplace metadata (name, description, owner) extracted from repo during sync

**Plugins** (Individual extensions within marketplaces)
- Defined in marketplace's `marketplace.json` with source path or URL
- Two source types: `local` (in marketplace repo) or `url` (external repo)
- Each plugin has version, category, author, and configuration

### Mode-Specific Activation

Plugins can be enabled per agent for:
- **Conversation Mode**: Active during workflow execution (Haiku model)
- **Building Mode**: Active during development/configuration (Sonnet model)

Toggle independently allows different plugins for different use cases.

### Git-Based Version Tracking

When installing plugins, the system stores:
- `installed_version`: Version string at installation time (for display)
- `installed_commit_hash`: Git commit hash for exact reproducibility

This enables:
- **Reproducible builds**: Environment rebuilds get exact same plugin files
- **Update detection**: Compare installed vs latest commit hash
- **Explicit upgrades**: Plugins don't auto-update; user controls upgrades

## Data Flow

```
1. Admin creates marketplace → LLMPluginMarketplace record created
2. Backend syncs repo → Parses marketplace.json → Creates LLMPluginMarketplacePlugin records
3. User installs plugin for agent → Creates AgentPluginLink with version/commit pinning
4. Environment starts → _sync_plugins_to_environment() copies plugin files to /app/workspace/plugins/
5. SDK initialized → Loads plugins from local paths based on session mode
```

## Database Models

**File**: `backend/app/models/llm_plugin.py`

### LLMPluginMarketplace

Stores marketplace configuration and sync status.

| Field | Purpose |
|-------|---------|
| `url`, `git_branch` | Git repository location |
| `ssh_key_id` | FK to user_ssh_keys for private repos |
| `public_discovery` | Whether other users can see plugins |
| `status` | pending/connected/error/disconnected |
| `sync_commit_hash` | HEAD commit at last sync |
| `type` | Marketplace format (default: "claude") |

### LLMPluginMarketplacePlugin

Individual plugins parsed from marketplace.

| Field | Purpose |
|-------|---------|
| `source_type` | `local` (in marketplace) or `url` (external repo) |
| `source_path` | Relative path for local plugins |
| `source_url`, `source_branch` | Git URL for external plugins |
| `commit_hash` | Commit when plugin config was parsed |
| `config` | Full plugin.json stored for reference |

### AgentPluginLink

Links installed plugins to agents.

| Field | Purpose |
|-------|---------|
| `agent_id`, `plugin_id` | Links agent to plugin |
| `installed_version`, `installed_commit_hash` | Version pinning |
| `conversation_mode`, `building_mode` | Per-mode activation |

## Backend Service

**File**: `backend/app/services/llm_plugin_service.py`

### LLMPluginService

Main service class with methods organized by responsibility:

**Marketplace Management**:
- `create_marketplace()` - Creates record, generates temp name from URL
- `sync_marketplace()` - Clones/pulls repo, parses plugins, updates metadata
- `_parse_claude_marketplace()` - Parses `.claude-plugin/marketplace.json`
- `_upsert_plugins()` - Add new, update existing, remove deleted plugins

**Plugin Discovery**:
- `discover_plugins()` - Returns plugins from accessible marketplaces with search/filter

**Agent Plugin Management**:
- `install_plugin_for_agent()` - Creates AgentPluginLink with version pinning
- `uninstall_plugin_from_agent()` - Removes plugin link
- `get_agent_plugins()` - Returns plugins with `has_update` flag
- `upgrade_agent_plugin()` - Updates to latest version/commit

**Plugin File Operations**:
- `get_plugin_files()` - Extracts plugin directory for sync
- `_get_local_plugin_files()` - From marketplace repo cache
- `_get_url_plugin_files()` - From external plugin repo cache

## Environment Integration

### Plugin Sync on Start

**File**: `backend/app/services/environment_lifecycle.py`

Method `_sync_plugins_to_environment()` called during `_sync_dynamic_data()`:
1. Gets installed plugins via `LLMPluginService.prepare_plugins_for_environment()`
2. For each plugin, gets files via `LLMPluginService.get_plugin_files()`
3. Encodes files as base64 for JSON transport
4. Sends to environment via `adapter.set_plugins()`

### Docker Adapter

**File**: `backend/app/services/adapters/docker_adapter.py`

Method `set_plugins()`:
- HTTP POST to `/config/plugins` endpoint in agent-env
- Sends plugin files and settings.json content

### Agent-Env Server

**File**: `backend/app/env-templates/python-env-advanced/app/core/server/routes.py`

Endpoints:
- `POST /config/plugins` - Receives and stores plugin files
- `GET /config/plugins/settings` - Returns current settings.json

**File**: `backend/app/env-templates/python-env-advanced/app/core/server/agent_env_service.py`

Methods:
- `update_plugins()` - Writes plugin files to workspace
- `get_plugins_settings()` - Reads settings.json
- `get_active_plugins_for_mode()` - Filters plugins by conversation/building mode

### SDK Loading

**File**: `backend/app/env-templates/python-env-advanced/app/core/server/sdk_manager.py`

In `send_message_stream()`:
1. Calls `agent_env_service.get_active_plugins_for_mode(mode)`
2. Builds plugins array: `[{"type": "local", "path": plugin["path"]}]`
3. Passes to `ClaudeAgentOptions(plugins=plugins, ...)`

## Workspace Structure

```
/app/workspace/plugins/
├── settings.json                        # Active plugins configuration
└── [marketplace_name]/
    └── [plugin_name]/                   # Plugin files from repository
        ├── .claude-plugin/
        │   └── plugin.json
        ├── commands/
        ├── skills/
        └── .mcp.json
```

### settings.json Format

```json
{
  "active_plugins": [
    {
      "marketplace_name": "claude-plugins-official",
      "plugin_name": "pyright-lsp",
      "path": "/app/workspace/plugins/claude-plugins-official/pyright-lsp",
      "conversation_mode": true,
      "building_mode": true
    }
  ]
}
```

## API Routes

**File**: `backend/app/api/routes/llm_plugins.py`

### Marketplace Routes (Admin)

| Endpoint | Purpose |
|----------|---------|
| `POST /api/v1/llm-plugins/marketplaces` | Create marketplace |
| `GET /api/v1/llm-plugins/marketplaces` | List marketplaces |
| `GET /api/v1/llm-plugins/marketplaces/{id}` | Get marketplace details |
| `PUT /api/v1/llm-plugins/marketplaces/{id}` | Update marketplace |
| `DELETE /api/v1/llm-plugins/marketplaces/{id}` | Delete marketplace |
| `POST /api/v1/llm-plugins/marketplaces/{id}/sync` | Trigger re-sync |

### Plugin Routes

| Endpoint | Purpose |
|----------|---------|
| `GET /api/v1/llm-plugins/discover` | Discover available plugins (with search/filter) |
| `GET /api/v1/llm-plugins/marketplaces/{id}/plugins` | List marketplace plugins |

### Agent Plugin Routes

| Endpoint | Purpose |
|----------|---------|
| `GET /api/v1/agents/{agent_id}/plugins` | List installed plugins (includes `has_update` flag) |
| `POST /api/v1/agents/{agent_id}/plugins` | Install plugin |
| `DELETE /api/v1/agents/{agent_id}/plugins/{link_id}` | Uninstall plugin |
| `PUT /api/v1/agents/{agent_id}/plugins/{link_id}` | Update mode flags |
| `POST /api/v1/agents/{agent_id}/plugins/{link_id}/upgrade` | Upgrade to latest version |

## Frontend Components

### Admin Marketplace Management

| File | Purpose |
|------|---------|
| `frontend/src/routes/_layout/admin/marketplaces.tsx` | Marketplace list page |
| `frontend/src/routes/_layout/admin/marketplace/$marketplaceId.tsx` | Marketplace detail with tabs |
| `frontend/src/components/Admin/AddMarketplace.tsx` | Create marketplace dialog |
| `frontend/src/components/Admin/MarketplaceConfigurationTab.tsx` | Edit marketplace settings |
| `frontend/src/components/Admin/MarketplacePluginsTab.tsx` | View marketplace plugins |

### Agent Plugins Tab

| File | Purpose |
|------|---------|
| `frontend/src/components/Agents/AgentPluginsTab.tsx` | Main plugins configuration tab |
| `frontend/src/components/Agents/PluginCard.tsx` | Plugin display card in discovery |
| `frontend/src/components/Agents/InstallPluginModal.tsx` | Mode selection during install |

**AgentPluginsTab** has two sections:
1. **Installed Plugins**: Table with mode toggles, upgrade/uninstall actions
2. **Discover Plugins**: Searchable grid with install button per plugin

## Claude Marketplace Format

### Marketplace File

Location: `.claude-plugin/marketplace.json` at repository root

```json
{
  "name": "marketplace-name",
  "description": "Marketplace description",
  "author": {"name": "Author Name", "email": "author@example.com"},
  "plugins": [...]
}
```

### Plugin Source Types

**Local Source** (default): Plugin files in marketplace repo
```json
{"name": "plugin-name", "source": "./plugins/plugin-name", ...}
```

**URL Source**: Plugin files in external repository
```json
{
  "name": "atlassian",
  "source": {"source": "url", "url": "https://github.com/atlassian/atlassian-mcp-server.git", "branch": "main"},
  "homepage": "https://github.com/atlassian/atlassian-mcp-server"
}
```

For URL-based plugins, the external repo should contain `.claude-plugin/plugin.json` with plugin configuration.

## Implementation References

### Backend

| File | Purpose |
|------|---------|
| `backend/app/models/llm_plugin.py` | Database models and schemas |
| `backend/app/services/llm_plugin_service.py` | Main business logic |
| `backend/app/services/git_operations.py` | Git clone/pull operations |
| `backend/app/api/routes/llm_plugins.py` | API endpoints |
| `backend/app/services/environment_lifecycle.py` | Plugin sync during env start |
| `backend/app/services/adapters/docker_adapter.py` | `set_plugins()` method |

### Agent-Env

| File | Purpose |
|------|---------|
| `backend/app/env-templates/.../routes.py` | `/config/plugins` endpoints |
| `backend/app/env-templates/.../agent_env_service.py` | Plugin file management |
| `backend/app/env-templates/.../sdk_manager.py` | Plugin loading into SDK |

### Frontend

| File | Purpose |
|------|---------|
| `frontend/src/routes/_layout/admin/marketplaces.tsx` | Admin marketplace page |
| `frontend/src/components/Agents/AgentPluginsTab.tsx` | Agent plugins configuration |
| `frontend/src/client/sdk.gen.ts` | Auto-generated `LlmPluginsService` |

## Similar Patterns

### Credentials Sync

The plugin sync follows the same pattern as credentials (`docs/agent-sessions/agent_env_credentials_management.md`):
1. Backend prepares data (`prepare_plugins_for_environment`)
2. Lifecycle manager calls sync during environment start
3. Adapter sends HTTP request to agent-env
4. Agent-env writes files to workspace
5. SDK reads configuration at runtime

### SSH Key Pattern

The marketplace SSH key reference follows the same pattern as `AIKnowledgeGitRepo`:
- `ssh_key_id` foreign key to `user_ssh_keys.id`
- Optional field for private repositories
- Same git clone/pull logic with SSH authentication

## Benefits

1. **Extensibility**: Agents gain new capabilities without core code changes
2. **Centralized Discovery**: Curated marketplaces for plugin distribution
3. **Version Control**: Git-based tracking ensures reproducibility
4. **Mode Flexibility**: Different plugins for conversation vs building modes
5. **Update Control**: Explicit upgrades prevent unexpected behavior changes
6. **Multi-Format Support**: Parser architecture allows future marketplace formats
