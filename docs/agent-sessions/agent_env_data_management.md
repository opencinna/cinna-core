# Agent Environment Data Management

## Purpose

Defines how data flows between agents, environments, and clones, ensuring consistent behavior across lifecycle operations (create, activate, rebuild, clone, sync updates, environment switch).

## Feature Overview

**Flow:**
1. Agent creates/modifies workspace data (scripts, docs, files) during sessions
2. Data ownership determines sync behavior (Original Agent, Clone/Instance, User, Runtime)
3. Dynamic data (prompts, credentials, plugins) syncs on every container start
4. Static data (workspace files) syncs on clone creation, updates, or environment switch
5. Runtime data (logs, databases) never syncs between environments

## Architecture

```
Agent Model → Environment Lifecycle → Docker Container → Workspace Files
(DB config)   (sync operations)       (filesystem)       (/app/workspace/)
```

**Data Locations:**
- **agent_config** - Database `Agent` model fields and related tables (persistent, survives environment deletion)
- **environment** - Docker container filesystem `/app/workspace/` (environment-specific, lost if deleted)

## Data Ownership Levels

| Ownership Level | Description | Examples |
|-----------------|-------------|----------|
| **Original Agent** | Data defined by parent agent, clones receive copies and updates | Workflow prompt, scripts, docs, knowledge, files, plugins |
| **Clone/Instance Agent** | Data owned independently by each agent instance | Integration credentials links, agent-specific settings |
| **User** | Data owned by user, shared across their agents | AI credentials, user workspace files |
| **Environment Runtime** | Data generated during execution, not synced | Logs, databases |

## Data Classification Matrix

### Agent Definition Data (Original Agent Ownership)

| Data | Storage | Sync Behavior | Notes |
|------|---------|---------------|-------|
| `workflow_prompt` | agent_config | Dynamic | Synced to `/app/workspace/docs/WORKFLOW_PROMPT.md` |
| `entrypoint_prompt` | agent_config | Dynamic | Synced to `/app/workspace/docs/ENTRYPOINT_PROMPT.md` |
| `scripts/` folder | environment | On-Demand | Agent-created Python scripts |
| `docs/` folder | environment | On-Demand | Prompt files and workflow docs |
| `knowledge/` folder | environment | On-Demand | Integration docs, API guides |
| `files/` folder | environment | On-Demand | Reports, CSV files, SQLite DBs, caches |
| `uploads/` folder | environment | On-Demand | User-uploaded files |
| `workspace_requirements.txt` | environment | On-Demand | Agent-installed Python packages |
| Plugins (LLM tools) | agent_config | Dynamic | Synced via `_sync_plugins_to_environment()` |

### Agent Instance Data (Clone/Instance Ownership)

| Data | Storage | Sync Behavior | Notes |
|------|---------|---------------|-------|
| Integration credentials | agent_config | Dynamic | Links via `AgentCredentialLink`, synced to `/app/workspace/credentials/` |
| AI credentials | environment | On Start | Resolved via `conversation_ai_credential_id` / `building_ai_credential_id` on environment |
| Agent SDK config | agent_config | Static (Clone) | Copied during clone, not updated |
| A2A config | agent_config | Static (Clone) | Agent-to-agent communication settings |

### AI Credential Resolution

AI credentials (Anthropic, MiniMax, OpenAI Compatible) are resolved during environment start/rebuild:

| Scenario | Resolution Behavior |
|----------|---------------------|
| Credentials assigned on environment | Use **only** assigned credentials (supports shared via `AICredentialShare`) |
| Assigned credential not accessible | Warning logged, no fallback (environment may fail to start) |
| No credentials assigned | Fall back to user's default profile credentials |

**Key Point**: When credentials are specifically assigned (e.g., shared credentials for cloned agents), the system does **not** fall back to the user's own credentials. This ensures cloned agents always use the credentials the owner shared.

**Implementation**: `backend/app/services/environment_lifecycle.py:_update_environment_config()`

### Environment Runtime Data (Not Synced)

| Data | Storage | Notes |
|------|---------|-------|
| `logs/` folder | environment | Session logs, debug output |
| `databases/` folder | environment | Runtime SQLite DBs, session state |

## Sync Operations by Lifecycle Event

### Environment Creation

- `environment_service.py:create_environment()` - Entry point
- `environment_lifecycle.py:create_environment_instance()` - Copy template, build image
- **No data sync** - dynamic data synced on first start

### Environment Start

- `environment_lifecycle.py:start_environment()` - Start container, setup, sync data
- `environment_lifecycle.py:_container_exists()` - Check if container exists
- `environment_lifecycle.py:_setup_new_container()` - One-time setup for new containers
- `environment_lifecycle.py:_sync_dynamic_data()` - Sync prompts, credentials, plugins

**Dynamic data synced:**
- Agent prompts → `/app/workspace/docs/`
- Integration credentials → `/app/workspace/credentials/`
- Plugins → `/app/workspace/plugins/`

### Environment Activation (from Suspended)

- `environment_lifecycle.py:activate_suspended_environment()` - Activate from suspended state
- Container exists, skip setup, only sync dynamic data

### Environment Switch (Same Agent)

When activating a different environment for the same agent:

- `environment_service.py:_find_source_environment_for_workspace_copy()` - Find best source environment
- `environment_lifecycle.py:copy_workspace_between_environments()` - Copy workspace data
- `environment_service.py:_activate_environment_background()` - Orchestrates the switch

**Source Environment Detection Priority:**
1. Current active environment (if set and different from target)
2. Most recently updated suspended environment
3. Environment from the most recent session for this agent (via `Session.updated_at`)

**Copied between environments:**
- `app/workspace/scripts/`
- `app/workspace/docs/`
- `app/workspace/knowledge/`
- `app/workspace/files/`
- `app/workspace/uploads/`
- `app/workspace/credentials/`
- `app/workspace/plugins/`
- `app/workspace/workspace_requirements.txt`

**NOT copied (Environment Runtime):**
- `app/workspace/logs/`
- `app/workspace/databases/`

### Environment Rebuild

- `environment_service.py:rebuild_environment()` - Entry point
- `environment_lifecycle.py:rebuild_environment()` - Update core, rebuild image

**Updated from template:** Infrastructure files (Dockerfile, pyproject.toml), core server code

**Preserved:** All workspace data (scripts, files, docs, credentials, databases, logs)

### Clone Creation

- `agent_clone_service.py:create_clone()` - Create clone agent and environment
- `agent_clone_service.py:copy_workspace()` - Copy workspace files to clone
- `agent_clone_service.py:setup_clone_credentials()` - Setup credentials for clone

**Copied to clone:**
- `app/workspace/scripts/`
- `app/workspace/docs/`
- `app/workspace/knowledge/`
- `app/workspace/files/`
- `app/workspace/uploads/`
- `app/workspace/workspace_requirements.txt`

**NOT copied:**
- `logs/`, `databases/` - Runtime data
- `credentials/` - Handled separately via dynamic sync

### Clone Update (Push Updates)

- `agent_clone_service.py:push_updates()` - Push updates to all clones
- `agent_clone_service.py:_apply_update_internal()` - Apply update to single clone
- `agent_clone_service.py:apply_update()` - Manual update apply

**Applied during update:** `scripts/`, `docs/`, `knowledge/`, `files/`, `uploads/`, `workspace_requirements.txt`

**Not applied:** Integration credentials, runtime data

## Database Schema

**Models:**

| File | Purpose |
|------|---------|
| `backend/app/models/agent.py` | Agent model with clone fields (`is_clone`, `parent_agent_id`, `workflow_prompt`, etc.) |
| `backend/app/models/environment.py` | AgentEnvironment model (`status`, `config`, `agent_sdk_*`) |
| `backend/app/models/session.py` | Session model (`environment_id`, `updated_at`) for source env detection |
| `backend/app/models/credential.py` | Credential model (`allow_sharing`, `is_placeholder`) |
| `backend/app/models/link_models.py` | AgentCredentialLink, AgentPluginLink junction tables |

## Backend Implementation

### Services

**Environment Lifecycle:** `backend/app/services/environment_lifecycle.py`
- `create_environment_instance()` - Copy template, build image
- `start_environment()` - Start container, setup, sync data
- `activate_suspended_environment()` - Activate from suspended state
- `rebuild_environment()` - Update core, rebuild image
- `_sync_dynamic_data()` - Sync prompts, credentials, plugins
- `_sync_plugins_to_environment()` - Sync installed plugins
- `_setup_new_container()` - One-time setup for new containers
- `copy_workspace_between_environments()` - Copy workspace when switching environments

**Environment Service:** `backend/app/services/environment_service.py`
- `create_environment()` - Entry point for environment creation
- `activate_environment()` - Activate environment for agent
- `rebuild_environment()` - Entry point for rebuild
- `_activate_environment_background()` - Background task for activation with workspace copy
- `_find_source_environment_for_workspace_copy()` - Find best source environment for copy

**Agent Clone Service:** `backend/app/services/agent_clone_service.py`
- `create_clone()` - Create clone agent and environment
- `copy_workspace()` - Copy workspace files to clone
- `setup_clone_credentials()` - Setup credentials for clone
- `push_updates()` - Push updates to all clones
- `_apply_update_internal()` - Apply update to single clone
- `apply_update()` - Manual update apply
- `sync_workspace_from_parent()` - Sync workspace files

**Supporting Services:**
- `backend/app/services/credentials_service.py` - `prepare_credentials_for_environment()`
- `backend/app/services/llm_plugin_service.py` - `prepare_plugins_for_environment()`
- `backend/app/services/adapters/docker_adapter.py` - `set_agent_prompts()`, `set_credentials()`, `set_plugins()`

### Configuration

**Settings:** `backend/app/core/config.py`
- `ENV_INSTANCES_DIR` - Directory for environment instances
- `ENV_TEMPLATES_DIR` - Directory for environment templates

## Agent-Env Implementation (Inside Docker Container)

**Routes:** `backend/app/env-templates/python-env-advanced/app/core/server/routes.py`
- `GET/POST /config/agent-prompts` - Fetch/update prompts
- `POST /config/credentials` - Update credentials
- `POST /config/plugins` - Update plugins

**Service:** `backend/app/env-templates/python-env-advanced/app/core/server/agent_env_service.py`
- Business logic for workspace file operations

## Workspace Directory Structure

```
/app/workspace/                          # Inside Docker container
├── scripts/                             # Agent scripts (Original Agent)
├── docs/                                # Documentation (Original Agent)
│   ├── WORKFLOW_PROMPT.md
│   └── ENTRYPOINT_PROMPT.md
├── knowledge/                           # Integration docs (Original Agent)
├── files/                               # Reports & caches (Original Agent)
├── uploads/                             # User-uploaded files (Original Agent)
├── credentials/                         # Integration credentials (Clone/Instance)
├── plugins/                             # LLM plugins (Original Agent)
├── logs/                                # Session logs (Runtime)
├── databases/                           # Runtime databases (Runtime)
└── workspace_requirements.txt           # Python packages (Original Agent)
```

## Key Integration Points

### Environment Switch Flow

1. `activate_environment()` called with target env_id
2. `_find_source_environment_for_workspace_copy()` determines source:
   - Checks `agent.active_environment_id`
   - Falls back to most recent suspended environment
   - Falls back to environment from most recent session
3. `copy_workspace_between_environments()` copies workspace data
4. Old environments stopped, target started
5. Dynamic data synced to target

### Clone Creation Flow

1. `create_clone()` creates Agent record with clone fields
2. `create_environment()` creates environment for clone
3. `copy_workspace()` copies workspace files from original
4. `setup_clone_credentials()` links shared or creates placeholder credentials

### Dynamic Data Sync Flow

1. Container starts (new or existing)
2. `_sync_dynamic_data()` called
3. Prompts sent via `adapter.set_agent_prompts()`
4. Credentials sent via `adapter.set_credentials()`
5. Plugins sent via `adapter.set_plugins()`

## Extending the Framework

When adding new data types, determine:

1. **Ownership** - Original agent, clone/instance, user, or runtime
2. **Storage** - agent_config (DB) or environment (filesystem)
3. **Sync timing** - Dynamic (every start), on-demand (clone/switch), static (clone only), never
4. **Conflict resolution** - Overwrite, append, rename, skip

**Implementation locations:**
- Dynamic sync → `_sync_dynamic_data()`
- Clone sync → `copy_workspace()`
- Environment switch → `copy_workspace_between_environments()`

## File Locations Reference

**Backend Services:**
- `backend/app/services/environment_lifecycle.py`
- `backend/app/services/environment_service.py`
- `backend/app/services/agent_clone_service.py`
- `backend/app/services/credentials_service.py`
- `backend/app/services/llm_plugin_service.py`
- `backend/app/services/adapters/docker_adapter.py`

**Database Models:**
- `backend/app/models/agent.py`
- `backend/app/models/environment.py`
- `backend/app/models/session.py`
- `backend/app/models/credential.py`
- `backend/app/models/link_models.py`

**Agent-Env Core:**
- `backend/app/env-templates/python-env-advanced/app/core/server/routes.py`
- `backend/app/env-templates/python-env-advanced/app/core/server/agent_env_service.py`
- `backend/app/env-templates/python-env-advanced/app/workspace/`

**Configuration:**
- `backend/app/core/config.py`

---

**Document Version:** 2.0
**Last Updated:** 2026-01-17
**Status:** Implemented
**Related Documents:**
- `docs/agent-sessions/agent_env_docker.md` - Docker architecture and lifecycle
- `docs/agent-sessions/agent_env_core.md` - Agent environment core server
- `docs/business-domain/shared_agents_management.md` - Clone and sharing features
