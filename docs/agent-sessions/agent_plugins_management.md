# Agent Plugins Management

## Overview

This document outlines the implementation plan for a plugin management system that allows agents to extend their capabilities through installable plugins. The system is designed to be format-agnostic while initially supporting Claude-format plugins from configurable marketplaces.

## Business Concept

### Problem Statement

Agents need to be extensible with additional capabilities (commands, skills, hooks, MCP servers) without modifying core code. Users should be able to discover, install, and manage plugins from curated marketplaces.

### Solution

A two-tier plugin system:
1. **Marketplaces**: Git repositories containing plugin catalogs (e.g., `https://github.com/anthropics/claude-plugins-official`)
2. **Plugins**: Individual extensions that can be installed into agent environments

### Key Features

- **Multi-format support**: Parser architecture allows supporting Claude format initially, with extensibility for future formats
- **Public/Private marketplaces**: Admins can create public marketplaces for all users; private marketplaces for personal use
- **Mode-specific activation**: Plugins can be enabled for conversation mode, building mode, or both
- **Automatic sync**: Plugin files are synced to agent environments and activated in SDK initialization

## Architecture

### Data Flow

```
1. Admin creates marketplace → Stored in database (LLMPluginMarketplace)
2. Service parses marketplace repo → Extracts plugins (LLMPluginMarketplacePlugin)
3. User browses/installs plugin for agent → Creates link (AgentPluginLink)
4. Environment starts → Plugins synced to /app/workspace/plugins/
5. SDK initialized → Plugins loaded from local paths based on mode
```

### Workspace Structure

```
/app/workspace/
├── plugins/
│   ├── settings.json                    # Active plugins configuration
│   └── [marketplace_name]/
│       └── [plugin_name]/               # Plugin files from repository
│           └── .claude-plugin/
│               └── plugin.json
├── scripts/
├── files/
├── docs/
└── credentials/
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

---

## Implementation Plan

### Phase 1: Database Models

#### New File: `backend/app/models/llm_plugin.py`

**LLMPluginMarketplace** (table)
| Field | Type | Description |
|-------|------|-------------|
| id | UUID | Primary key |
| name | str | Marketplace name (unique, indexed) |
| description | str | Optional description |
| owner_name | str | Marketplace owner name |
| owner_email | str | Marketplace owner email |
| url | str | Git repository URL |
| git_branch | str | Branch to use (default: "main") |
| ssh_key_id | UUID | FK to user_ssh_keys (optional) |
| public_discovery | bool | Whether other users can discover plugins |
| type | str | Marketplace type (default: "claude") |
| status | enum | pending/connected/error/disconnected |
| status_message | str | Error or status details |
| last_sync_at | datetime | Last successful parse |
| sync_commit_hash | str | Latest commit hash from branch HEAD after sync |
| user_id | UUID | FK to user (creator) |
| created_at | datetime | |
| updated_at | datetime | |

**Marketplace Sync Commit Hash**: The `sync_commit_hash` stores the HEAD commit of the configured branch at last sync time. This is used to:
- Detect if repository has new commits (compare local hash vs remote HEAD)
- Provide reference point for plugin update detection

**LLMPluginMarketplacePlugin** (table)
| Field | Type | Description |
|-------|------|-------------|
| id | UUID | Primary key |
| marketplace_id | UUID | FK to marketplace |
| name | str | Plugin name (from config) |
| description | str | Plugin description |
| version | str | Plugin version (from plugin.json) |
| author_name | str | Author name |
| author_email | str | Author email |
| category | str | Plugin category (development, etc.) |
| homepage | str | Plugin homepage URL |
| source_path | str | Path within repository (for local source_type) |
| source_type | enum | local or url (default: local) |
| source_url | str | External git URL (for url source_type) |
| source_branch | str | Git branch for external repo (default: main) |
| source_commit_hash | str | Commit hash from external repo (for url source_type) |
| plugin_type | str | Type inherited from marketplace |
| config | JSON | Original plugin configuration |
| commit_hash | str | Commit hash when this plugin config was parsed |
| created_at | datetime | |
| updated_at | datetime | |

**Unique constraint**: (marketplace_id, name)

**Plugin Source Types**:
- `local`: Plugin files are in the marketplace repository at `source_path`
- `url`: Plugin files are in an external repository at `source_url`

**Plugin Commit Hash**: Each plugin stores the `commit_hash` from which its configuration was parsed. When marketplace is re-synced:
- If plugin's source files changed (detected via git diff on `source_path`), update `commit_hash`
- Agents with `installed_commit_hash` != plugin's current `commit_hash` have updates available

**AgentPluginLink** (table)
| Field | Type | Description |
|-------|------|-------------|
| id | UUID | Primary key |
| agent_id | UUID | FK to agent |
| plugin_id | UUID | FK to marketplace plugin |
| installed_version | str | Version string at time of installation (for display) |
| installed_commit_hash | str | Git commit hash from which plugin was extracted |
| conversation_mode | bool | Enable in conversation mode |
| building_mode | bool | Enable in building mode |
| created_at | datetime | |
| updated_at | datetime | |

**Unique constraint**: (agent_id, plugin_id)

**Git-Based Version Tracking**: When a plugin is installed, we store both the version string (for display) and the git commit hash (for reproducibility). This enables:
- **Exact reproducibility**: On environment rebuild, we checkout the exact `installed_commit_hash` to get identical plugin files
- **Update detection**: Compare `installed_commit_hash` vs marketplace's `sync_commit_hash` to detect updates
- **Stable installations**: Plugin files don't change unless user explicitly upgrades
- **Upgrade workflow**: When upgrading, we pull latest commit and update both `installed_version` and `installed_commit_hash`

#### Schema Models (same file)

- `LLMPluginMarketplaceCreate`, `LLMPluginMarketplaceUpdate`, `LLMPluginMarketplacePublic`
- `LLMPluginMarketplacePluginPublic`
- `AgentPluginLinkCreate`, `AgentPluginLinkPublic`
- `AgentPluginLinkWithUpdateInfo` - Extended schema including:
  - All fields from `AgentPluginLinkPublic`
  - `has_update: bool` - True if plugin's current version > installed_version
  - `latest_version: str` - Current version in marketplace (for display)

#### Migration

**New file**: `backend/app/alembic/versions/xxx_add_plugin_marketplace_tables.py`
- Create `llm_plugin_marketplace` table
- Create `llm_plugin_marketplace_plugin` table
- Create `agent_plugin_link` table
- Add indexes for common queries

---

### Phase 2: Backend Services

#### New File: `backend/app/services/llm_plugin_service.py`

**LLMPluginService** class with methods:

**Marketplace Management:**
- `create_marketplace(data: LLMPluginMarketplaceCreate, user_id: UUID) -> LLMPluginMarketplace`
- `update_marketplace(id: UUID, data: LLMPluginMarketplaceUpdate) -> LLMPluginMarketplace`
- `delete_marketplace(id: UUID)` - Cascade delete plugins and agent links
- `get_marketplace(id: UUID) -> LLMPluginMarketplace`
- `list_marketplaces(user_id: UUID, include_public: bool) -> list[LLMPluginMarketplace]`

**Marketplace Parsing:**
- `sync_marketplace(id: UUID)` - Main parsing method
  1. Clone/pull repository using git URL and optional SSH key
  2. Get current HEAD commit hash for the branch
  3. Locate `.claude-plugin/marketplace.json` (for claude type)
  4. Parse plugin definitions
  5. For each plugin, check if `source_path` files changed since last sync (git diff)
  6. Upsert plugins (add new, update existing with new `commit_hash` if changed, remove deleted)
  7. Update marketplace `sync_commit_hash` and `last_sync_at`
- `_parse_claude_marketplace(repo_path: str) -> list[dict]` - Claude format parser
- `_get_parser_for_type(type: str) -> Callable` - Parser factory for extensibility
- `_get_commit_hash_for_path(repo_path: str, source_path: str) -> str` - Get last commit that modified the plugin's source path

**Plugin Discovery:**
- `discover_plugins(user_id: UUID, search: str, category: str) -> list[LLMPluginMarketplacePluginPublic]`
  - Returns plugins from user's private marketplaces + public marketplaces
  - Supports search by name/description and category filter

**Agent Plugin Management:**
- `install_plugin_for_agent(agent_id: UUID, plugin_id: UUID, conversation_mode: bool, building_mode: bool)`
  - Stores plugin's current `version` as `installed_version`
  - Stores plugin's current `commit_hash` as `installed_commit_hash`
  - Extracts plugin files from that specific commit
- `uninstall_plugin_from_agent(agent_id: UUID, plugin_id: UUID)`
- `get_agent_plugins(agent_id: UUID) -> list[AgentPluginLinkWithUpdateInfo]`
  - Returns plugins with `has_update` flag (compares `installed_commit_hash` vs plugin's current `commit_hash`)
  - Includes `latest_version` from plugin table for display
- `update_plugin_modes(link_id: UUID, conversation_mode: bool, building_mode: bool)`
- `upgrade_agent_plugin(link_id: UUID)` - Upgrades to latest version
  - Extracts plugin files from plugin's current `commit_hash`
  - Updates `installed_version` and `installed_commit_hash` to match plugin's current values
  - Triggers sync to running environments

**Plugin Sync to Environment:**
- `prepare_plugins_for_environment(agent_id: UUID) -> dict`
  - Returns plugin data for syncing to agent-env
  - Includes list of plugins with paths and mode flags
- `sync_plugins_to_agent_environments(agent_id: UUID)`
  - Syncs plugins to all running environments for the agent
  - Similar pattern to `CredentialsService.sync_credentials_to_agent_environments()`

**Git Operations (internal):**
- `_clone_repository(url: str, branch: str, ssh_key: str | None, target_dir: str)`
- `_pull_repository(repo_dir: str, ssh_key: str | None)`
- `_get_current_commit_hash(repo_dir: str) -> str` - Get HEAD commit hash
- `_get_commit_hash_for_path(repo_dir: str, path: str) -> str` - Get last commit that modified a path
- `_checkout_commit(repo_dir: str, commit_hash: str)` - Checkout specific commit (for extracting pinned version)
- `_get_plugin_files(repo_dir: str, source_path: str, commit_hash: str | None) -> dict[str, bytes]`
  - Extract plugin directory files for syncing
  - If `commit_hash` provided, checkout that commit first (for rebuild without upgrade)
  - Returns to branch HEAD after extraction

#### Modify: `backend/app/services/environment_lifecycle.py`

**In `_sync_dynamic_data()` method:**
- Add call to sync plugins after credentials sync
- `await self._sync_plugins_to_environment(environment, agent)`

**New method `_sync_plugins_to_environment()`:**
1. Get installed plugins for agent via `LLMPluginService.get_agent_plugins()`
2. For each plugin:
   - Clone/checkout marketplace repo if not cached
   - Checkout the `installed_commit_hash` (ensures exact version reproducibility)
   - Extract plugin files from `source_path` at that commit
   - Send to environment via adapter
3. Generate and send `settings.json`

**Note**: Using `installed_commit_hash` ensures that environment rebuilds get the exact same plugin files, even if the marketplace has newer commits. User must explicitly upgrade to get new versions.

#### Modify: `backend/app/services/adapters/docker_adapter.py`

**New method `set_plugins()`:**
- HTTP POST to agent-env `/config/plugins` endpoint
- Payload: plugin files and settings.json content

**New method `get_plugins_settings()`:**
- HTTP GET from agent-env `/config/plugins/settings`
- Returns current plugins configuration

---

### Phase 3: Agent Environment (agent-env) Changes

#### Modify: `backend/app/env-templates/python-env-advanced/app/core/server/routes.py`

**New endpoints:**
- `POST /config/plugins` - Receive and store plugin files from backend
- `GET /config/plugins/settings` - Return current settings.json

#### Modify: `backend/app/env-templates/python-env-advanced/app/core/server/agent_env_service.py`

**New methods:**
- `update_plugins(plugins_data: dict)` - Write plugin files to workspace
  1. Create `/app/workspace/plugins/` if not exists
  2. For each plugin: create `[marketplace]/[plugin]/` directory
  3. Write plugin files maintaining structure
  4. Update `settings.json` with active plugins
- `get_plugins_settings() -> dict` - Read settings.json
- `get_active_plugins_for_mode(mode: str) -> list[dict]` - Filter by mode

#### Modify: `backend/app/env-templates/python-env-advanced/app/core/server/sdk_manager.py`

**In `send_message_stream()` method:**
1. Load plugins settings via `AgentEnvService.get_active_plugins_for_mode(mode)`
2. Build plugins array for SDK options:
   ```python
   plugins = [
       {"type": "local", "path": plugin["path"]}
       for plugin in active_plugins
   ]
   ```
3. Pass to ClaudeAgentOptions:
   ```python
   options = ClaudeAgentOptions(
       plugins=plugins,
       # ... other options
   )
   ```

---

### Phase 4: Backend API Routes

#### New File: `backend/app/api/routes/llm_plugins.py`

**Marketplace Routes (Admin only):**
- `POST /api/v1/llm-plugins/marketplaces` - Create marketplace
- `GET /api/v1/llm-plugins/marketplaces` - List marketplaces
- `GET /api/v1/llm-plugins/marketplaces/{id}` - Get marketplace details
- `PUT /api/v1/llm-plugins/marketplaces/{id}` - Update marketplace
- `DELETE /api/v1/llm-plugins/marketplaces/{id}` - Delete marketplace
- `POST /api/v1/llm-plugins/marketplaces/{id}/sync` - Trigger re-parsing

**Plugin Discovery Routes:**
- `GET /api/v1/llm-plugins/discover` - Discover available plugins (with search/filter)
- `GET /api/v1/llm-plugins/plugins/{id}` - Get plugin details

**Agent Plugin Routes:**
- `GET /api/v1/agents/{agent_id}/plugins` - List installed plugins (includes `has_update` flag)
- `POST /api/v1/agents/{agent_id}/plugins` - Install plugin
- `DELETE /api/v1/agents/{agent_id}/plugins/{link_id}` - Uninstall plugin
- `PUT /api/v1/agents/{agent_id}/plugins/{link_id}` - Update mode flags
- `POST /api/v1/agents/{agent_id}/plugins/{link_id}/upgrade` - Upgrade to latest version

#### Modify: `backend/app/api/main.py`

- Register new router: `api_router.include_router(llm_plugins.router, prefix="/llm-plugins", tags=["llm-plugins"])`

---

### Phase 5: Frontend - Admin Marketplace Management

#### New File: `frontend/src/routes/_layout/admin/marketplaces.tsx`

**MarketplacesAdmin** component:
- Table listing all marketplaces
- Columns: Name, URL, Type, Status, Last Sync, Actions
- Actions: Edit, Sync, Delete

#### New File: `frontend/src/components/Admin/MarketplaceForm.tsx`

**MarketplaceForm** component:
- Form fields: name, description, owner_name, owner_email, url, git_branch, ssh_key_id (dropdown), public_discovery, type
- SSH key dropdown populated from user's SSH keys
- Validation for required fields

#### New File: `frontend/src/components/Admin/MarketplaceCard.tsx`

**MarketplaceCard** component:
- Display marketplace details
- Sync button with loading state
- Status indicator (connected/error/pending)
- Plugin count badge

---

### Phase 6: Frontend - Agent Plugins Tab

#### New File: `frontend/src/components/Agents/AgentPluginsTab.tsx`

**AgentPluginsTab** component with two sections:

**Section 1: Installed Plugins**
- Table of installed plugins for this agent
- Columns: Name, Version, Category, Conversation Mode, Building Mode, Actions
- Version column shows `installed_version` with "Update Available" badge if `has_update` is true
- Mode toggles (checkboxes)
- Action buttons: Upgrade (if update available), Uninstall

**Section 2: Discover Plugins**
- Search input field
- Category filter dropdown
- Grid/List of available plugins (not yet installed)
- Each plugin card shows: name, description, category, marketplace name
- Install button per plugin
  - On click: modal to select conversation_mode and building_mode
  - Then triggers install API

#### Modify: `frontend/src/routes/_layout/agent/$agentId.tsx`

**Add new tab:**
```typescript
import { AgentPluginsTab } from "@/components/Agents/AgentPluginsTab"

const tabs = [
  { value: "configuration", title: "Configuration", content: <AgentPromptsTab agent={agent} /> },
  { value: "credentials", title: "Credentials", content: <AgentCredentialsTab agentId={agent.id} /> },
  { value: "plugins", title: "Plugins", content: <AgentPluginsTab agentId={agent.id} /> }, // NEW
  { value: "environments", title: "Environments", content: <AgentEnvironmentsTab agentId={agent.id} /> },
  { value: "interface", title: "Interface", content: <AgentConfigurationTab agent={agent} /> },
]
```

#### New File: `frontend/src/components/Agents/PluginCard.tsx`

**PluginCard** component:
- Plugin name, description, version
- Author info
- Category badge
- Install/Uninstall button

#### New File: `frontend/src/components/Agents/InstallPluginModal.tsx`

**InstallPluginModal** component:
- Plugin details display
- Checkboxes: "Enable for Conversation Mode", "Enable for Building Mode"
- Install/Cancel buttons

---

### Phase 7: Frontend Client Regeneration

After backend API routes are complete:
```bash
bash scripts/generate-client.sh
```

This generates TypeScript types and service classes in `frontend/src/client/`:
- `LlmPluginsService` (marketplace and plugin operations)
- Types: `LLMPluginMarketplacePublic`, `LLMPluginMarketplacePluginPublic`, `AgentPluginLinkPublic`, etc.

---

## File Reference Summary

### New Backend Files
| File | Purpose |
|------|---------|
| `backend/app/models/llm_plugin.py` | Database models and schemas |
| `backend/app/services/llm_plugin_service.py` | Business logic |
| `backend/app/api/routes/llm_plugins.py` | API endpoints |
| `backend/app/alembic/versions/xxx_add_plugin_marketplace_tables.py` | Migration |

### Modified Backend Files
| File | Changes |
|------|---------|
| `backend/app/api/main.py` | Register new router |
| `backend/app/services/environment_lifecycle.py` | Add plugin sync to `_sync_dynamic_data()` |
| `backend/app/services/adapters/docker_adapter.py` | Add `set_plugins()` method |
| `backend/app/services/adapters/base.py` | Add abstract `set_plugins()` method |
| `backend/app/env-templates/.../routes.py` | Add `/config/plugins` endpoint |
| `backend/app/env-templates/.../agent_env_service.py` | Add plugin file management |
| `backend/app/env-templates/.../sdk_manager.py` | Load plugins into SDK options |

### New Frontend Files
| File | Purpose |
|------|---------|
| `frontend/src/routes/_layout/admin/marketplaces.tsx` | Admin marketplace management page |
| `frontend/src/components/Admin/MarketplaceForm.tsx` | Marketplace create/edit form |
| `frontend/src/components/Admin/MarketplaceCard.tsx` | Marketplace display card |
| `frontend/src/components/Agents/AgentPluginsTab.tsx` | Agent plugins configuration tab |
| `frontend/src/components/Agents/PluginCard.tsx` | Plugin display card |
| `frontend/src/components/Agents/InstallPluginModal.tsx` | Plugin installation modal |

### Modified Frontend Files
| File | Changes |
|------|---------|
| `frontend/src/routes/_layout/agent/$agentId.tsx` | Add Plugins tab |

---

## Claude Marketplace Format Reference

### Marketplace File Location
`.claude-plugin/marketplace.json` at repository root

### Marketplace JSON Structure
```json
{
  "name": "marketplace-name",
  "description": "Marketplace description",
  "version": "1.0.0",
  "plugins": [
    {
      "name": "plugin-name",
      "description": "Plugin description",
      "version": "1.0.0",
      "author": {
        "name": "Author Name",
        "email": "author@example.com"
      },
      "source": "./plugins/plugin-name",
      "category": "development",
      "strict": false,
      "lspServers": { ... },
      "commands": [ ... ],
      "hooks": { ... }
    }
  ]
}
```

### Plugin Source Types

The marketplace supports two types of plugin sources:

#### 1. Local Source (default)
Plugin files are located within the marketplace repository itself.

```json
{
  "name": "plugin-name",
  "source": "./plugins/plugin-name",
  ...
}
```

#### 2. URL Source (external repository)
Plugin files are in an external git repository. The repository should contain a `.claude-plugin/plugin.json` file.

```json
{
  "name": "atlassian",
  "description": "Connect to Atlassian products including Jira and Confluence.",
  "category": "productivity",
  "source": {
    "source": "url",
    "url": "https://github.com/atlassian/atlassian-mcp-server.git",
    "branch": "main"
  },
  "homepage": "https://github.com/atlassian/atlassian-mcp-server"
}
```

**URL Source Fields:**
- `source.source`: Must be `"url"` to indicate an external repository
- `source.url`: Git repository URL (HTTPS or SSH)
- `source.branch`: Optional, defaults to `"main"`

When a URL-based plugin is installed:
1. The external repository is cloned to a local cache
2. The `.claude-plugin/plugin.json` is read for plugin configuration
3. All repository files are synced to the agent environment (excluding `.git`)

### Plugin Directory Structure

#### Local Plugins (in marketplace repo)
```
plugins/plugin-name/
├── .claude-plugin/
│   └── plugin.json
├── commands/
├── agents/
├── skills/
├── hooks/
└── .mcp.json
```

#### URL-Based Plugins (external repo)
The external repository should follow the same structure:
```
repository-root/
├── .claude-plugin/
│   └── plugin.json     # Required: Plugin configuration
├── commands/
├── agents/
├── skills/
├── hooks/
├── .mcp.json
└── README.md
```

The `.claude-plugin/plugin.json` in an external repository:
```json
{
  "name": "plugin-name",
  "description": "Plugin description",
  "version": "1.0.0",
  "author": {
    "name": "Author Name",
    "email": "author@example.com"
  },
  "category": "productivity",
  "commands": [...],
  "hooks": {...},
  "mcpServers": [...]
}
```

---

## SDK Plugin Activation

### Python Example (agent-env)
```python
from claude_agent_sdk import query, ClaudeAgentOptions

# Build plugins list from settings.json
plugins = [
    {"type": "local", "path": p["path"]}
    for p in settings["active_plugins"]
    if p.get("conversation_mode") or p.get("building_mode")  # based on current mode
]

options = ClaudeAgentOptions(
    plugins=plugins,
    # ... other options
)

async for message in query(prompt=user_message, options=options):
    # Process response
    pass
```

---

## Similar Patterns in Codebase

### Credentials Sync Pattern
Reference: `docs/agent-sessions/agent_env_credentials_management.md`

The plugin sync follows the same pattern:
1. Backend prepares data (`prepare_plugins_for_environment`)
2. Lifecycle manager calls sync during environment start/rebuild
3. Adapter sends HTTP request to agent-env
4. Agent-env writes files to workspace
5. SDK reads configuration at runtime

### Knowledge Sources SSH Key Pattern
Reference: `backend/app/models/knowledge.py` - `AIKnowledgeGitRepo`

The marketplace model follows the same SSH key reference pattern:
- `ssh_key_id` foreign key to `user_ssh_keys.id`
- Optional field for private repositories
- Same git clone/pull logic with SSH authentication

---

## Security Considerations

1. **SSH Key Access**: Only marketplace creator can use their SSH keys
2. **Public Discovery**: Admin controls which marketplaces are publicly visible
3. **Plugin Isolation**: Plugins run within agent-env sandbox
4. **Repository Validation**: Validate git URLs and branch names
5. **File Path Validation**: Prevent directory traversal in plugin source paths

---

## Future Extensibility

### Additional Marketplace Types
The parser factory pattern allows adding new marketplace formats:
```python
def _get_parser_for_type(self, type: str) -> Callable:
    parsers = {
        "claude": self._parse_claude_marketplace,
        "openai": self._parse_openai_marketplace,  # Future
        "custom": self._parse_custom_marketplace,   # Future
    }
    return parsers.get(type, self._parse_claude_marketplace)
```

### User-Created Marketplaces
Currently admin-only; future enhancement to allow regular users to create private marketplaces.

### Plugin Versioning (Partially Implemented)
The current design includes version locking at installation time (`installed_version` in AgentPluginLink). Future enhancements:
- Version history tracking (what versions were previously installed)
- Rollback capability (revert to previous version)
- Changelog display (show what changed between versions)
- Bulk upgrade across all agents using a plugin
