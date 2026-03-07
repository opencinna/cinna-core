# Environment Data Management - Technical Details

## File Locations

### Backend - Models

- `backend/app/models/agent.py` - `Agent` model with clone fields (`is_clone`, `parent_agent_id`, `workflow_prompt`, `entrypoint_prompt`, etc.)
- `backend/app/models/environment.py` - `AgentEnvironment` model (`status`, `config`, `conversation_ai_credential_id`, `building_ai_credential_id`)
- `backend/app/models/session.py` - `Session` model (`environment_id`, `updated_at`) used for source environment detection
- `backend/app/models/credential.py` - `Credential` model (`allow_sharing`, `is_placeholder`)
- `backend/app/models/link_models.py` - `AgentCredentialLink`, `AgentPluginLink` junction tables

### Backend - Services

- `backend/app/services/environment_lifecycle.py` - `EnvironmentLifecycleManager` - core lifecycle and sync operations
- `backend/app/services/environment_service.py` - `EnvironmentService` - route-level orchestration, activation, workspace copy coordination
- `backend/app/services/agent_clone_service.py` - `AgentCloneService` - clone creation, workspace copy, push updates
- `backend/app/services/credentials_service.py` - `CredentialsService` - credential preparation for environments
- `backend/app/services/llm_plugin_service.py` - `LLMPluginService` - plugin preparation for environments
- `backend/app/services/adapters/docker_adapter.py` - `DockerEnvironmentAdapter` - HTTP proxy to agent-env config endpoints

### Agent-Env Internal (inside Docker container)

- `backend/app/env-templates/app_core_base/core/server/routes.py` - Config HTTP endpoints
- `backend/app/env-templates/app_core_base/core/server/agent_env_service.py` - Workspace file operations

### Configuration

- `backend/app/core/config.py` - `ENV_INSTANCES_DIR`, `ENV_TEMPLATES_DIR` settings

## Database Schema

### Agent model (`backend/app/models/agent.py`)

Clone-related fields:
- `is_clone` (bool) - Whether this agent is a clone
- `parent_agent_id` (UUID, nullable, FK) - Original agent reference
- `workflow_prompt` (str, nullable) - Workflow prompt text (synced to environment)
- `entrypoint_prompt` (str, nullable) - Entrypoint prompt text (synced to environment)

### AgentEnvironment model (`backend/app/models/environment.py`)

Data management fields:
- `conversation_ai_credential_id` (UUID, nullable, FK) - AI credential for conversation mode
- `building_ai_credential_id` (UUID, nullable, FK) - AI credential for building mode
- `config` (JSON) - Runtime configuration including `auth_token`

### Junction tables (`backend/app/models/link_models.py`)

- `AgentCredentialLink` - Links agents to integration credentials
- `AgentPluginLink` - Links agents to LLM plugins

## API Endpoints

### Agent-Env Internal Config Endpoints (inside container)

- `GET /config/agent-prompts` - Fetch current prompts from workspace/docs/
- `POST /config/agent-prompts` - Update prompts in workspace/docs/
- `POST /config/credentials` - Update credentials in workspace/credentials/
- `POST /config/plugins` - Update plugins in workspace/plugins/

## Services & Key Methods

### EnvironmentLifecycleManager (`backend/app/services/environment_lifecycle.py`)

- `create_environment_instance()` - Copy template, build image (no data sync)
- `start_environment()` - Start container, detect new vs existing, sync data
- `activate_suspended_environment()` - Activate from suspended (skip setup, sync dynamic data only)
- `rebuild_environment()` - Update core, rebuild image (preserve workspace)
- `_sync_dynamic_data()` - Sync prompts, credentials, plugins to running container
- `_sync_plugins_to_environment()` - Sync installed plugins via HTTP API
- `_setup_new_container()` - One-time setup for new containers (install workspace packages)
- `copy_workspace_between_environments()` - Copy workspace folders between environment instance directories
- `_update_environment_config()` - Regenerate auth token, resolve AI credentials, generate .env

### EnvironmentService (`backend/app/services/environment_service.py`)

- `create_environment()` - Entry point for environment creation
- `activate_environment()` - Activate environment for agent, orchestrate workspace copy
- `rebuild_environment()` - Entry point for rebuild
- `_activate_environment_background()` - Background task for activation with workspace copy
- `_find_source_environment_for_workspace_copy()` - Find best source environment by priority (active → recent suspended → recent session)

### AgentCloneService (`backend/app/services/agent_clone_service.py`)

- `create_clone()` - Create clone agent record and environment
- `copy_workspace()` - Copy workspace files from original to clone (scripts, docs, knowledge, files, uploads, workspace_requirements.txt)
- `setup_clone_credentials()` - Link shared credentials or create placeholders
- `push_updates()` - Push updates to all clones of an agent
- `_apply_update_internal()` - Apply update to a single clone
- `apply_update()` - Manual update apply from UI
- `sync_workspace_from_parent()` - Sync workspace files from parent agent

### Supporting Services

- `backend/app/services/credentials_service.py` - `prepare_credentials_for_environment()` - Gather and format credentials for sync
- `backend/app/services/llm_plugin_service.py` - `prepare_plugins_for_environment()` - Gather and format plugins for sync

### DockerEnvironmentAdapter (`backend/app/services/adapters/docker_adapter.py`)

- `set_agent_prompts()` - HTTP POST to agent-env `/config/agent-prompts`
- `set_credentials()` - HTTP POST to agent-env `/config/credentials`
- `set_plugins()` - HTTP POST to agent-env `/config/plugins`

## Workspace Copy Specifications

### Environment Switch Copy

Folders copied by `copy_workspace_between_environments()`:
- `app/workspace/scripts/`
- `app/workspace/docs/`
- `app/workspace/knowledge/`
- `app/workspace/files/`
- `app/workspace/uploads/`
- `app/workspace/credentials/`
- `app/workspace/plugins/`
- `app/workspace/workspace_requirements.txt`

Excluded: `app/workspace/logs/`, `app/workspace/databases/`

### Clone Creation Copy

Folders copied by `copy_workspace()`:
- `app/workspace/scripts/`
- `app/workspace/docs/`
- `app/workspace/knowledge/`
- `app/workspace/files/`
- `app/workspace/uploads/`
- `app/workspace/workspace_requirements.txt`

Excluded: `logs/`, `databases/` (runtime), `credentials/` (handled via dynamic sync separately)

### Clone Push Update Copy

Folders synced by `_apply_update_internal()`:
- `scripts/`, `docs/`, `knowledge/`, `files/`, `uploads/`, `workspace_requirements.txt`

Excluded: Integration credentials, runtime data

## Dynamic Sync Implementation

The `_sync_dynamic_data()` method runs on every container start:

1. Fetch agent prompts from DB → send via `adapter.set_agent_prompts()`
2. Fetch integration credentials via `credentials_service.prepare_credentials_for_environment()` → send via `adapter.set_credentials()`
3. Fetch plugins via `llm_plugin_service.prepare_plugins_for_environment()` → send via `adapter.set_plugins()`

AI credential resolution happens earlier in `_update_environment_config()`:
1. Check `conversation_ai_credential_id` / `building_ai_credential_id` on environment
2. If set → use those credentials only (no fallback)
3. If not set → fall back to user's default profile credentials
4. Auto-detect credential type by prefix → set appropriate env var

