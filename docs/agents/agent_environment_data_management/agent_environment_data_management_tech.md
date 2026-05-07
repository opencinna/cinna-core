# Environment Data Management - Technical Details

## File Locations

### Backend - Models

- `backend/app/models/agents/agent.py` - `Agent` model with clone fields (`is_clone`, `parent_agent_id`, `workflow_prompt`, `entrypoint_prompt`, etc.)
- `backend/app/models/environments/environment.py` - `AgentEnvironment` model (`status`, `config`, `conversation_ai_credential_id`, `building_ai_credential_id`)
- `backend/app/models/sessions/session.py` - `Session` model (`environment_id`, `updated_at`) used for source environment detection
- `backend/app/models/credentials/credential.py` - `Credential` model (`allow_sharing`, `is_placeholder`)
- `backend/app/models/credentials/link_models.py` - `AgentCredentialLink`, `AgentPluginLink` junction tables

### Backend - Services

- `backend/app/services/environments/environment_lifecycle.py` - `EnvironmentLifecycleManager` - core lifecycle and sync operations
- `backend/app/services/environments/environment_service.py` - `EnvironmentService` - route-level orchestration, activation, workspace copy coordination
- `backend/app/services/environments/workspace_copy.py` - `copy_env_to_env`, `seed_workspace_from_bundle_snapshot`, `replace_bundle_content` helpers (extracted from the retired `AgentCloneService`)
- `backend/app/services/bundles/install_service.py` - `InstallService` - bundle install / apply-update flows that drive the snapshot-based copies
- `backend/app/services/credentials/credentials_service.py` - `CredentialsService` - credential preparation for environments
- `backend/app/services/plugins/llm_plugin_service.py` - `LLMPluginService` - plugin preparation for environments
- `backend/app/services/environments/adapters/docker_adapter.py` - `DockerEnvironmentAdapter` - HTTP proxy to agent-env config endpoints

### Agent-Env Internal (inside Docker container)

- `backend/app/env-templates/app_core_base/core/server/routes.py` - Config HTTP endpoints
- `backend/app/env-templates/app_core_base/core/server/agent_env_service.py` - Workspace file operations

### Configuration

- `backend/app/core/config.py` - `ENV_INSTANCES_DIR`, `ENV_TEMPLATES_DIR` settings

## Database Schema

### Agent model (`backend/app/models/agents/agent.py`)

Clone-related fields:
- `is_clone` (bool) - Whether this agent is a clone
- `parent_agent_id` (UUID, nullable, FK) - Original agent reference
- `workflow_prompt` (str, nullable) - Workflow prompt text (synced to environment)
- `entrypoint_prompt` (str, nullable) - Entrypoint prompt text (synced to environment)

### AgentEnvironment model (`backend/app/models/environments/environment.py`)

Data management fields:
- `conversation_ai_credential_id` (UUID, nullable, FK) - AI credential for conversation mode
- `building_ai_credential_id` (UUID, nullable, FK) - AI credential for building mode
- `config` (JSON) - Runtime configuration including `auth_token`

### Junction tables (`backend/app/models/credentials/link_models.py`)

- `AgentCredentialLink` - Links agents to integration credentials
- `AgentPluginLink` - Links agents to LLM plugins

## API Endpoints

### Agent-Env Internal Config Endpoints (inside container)

- `GET /config/agent-prompts` - Fetch current prompts from workspace/docs/
- `POST /config/agent-prompts` - Update prompts in workspace/docs/
- `POST /config/credentials` - Update credentials in workspace/credentials/
- `POST /config/plugins` - Update plugins in workspace/plugins/

## Services & Key Methods

### EnvironmentLifecycleManager (`backend/app/services/environments/environment_lifecycle.py`)

- `create_environment_instance()` - Copy template, build image (no data sync)
- `start_environment()` - Start container, detect new vs existing, sync data
- `activate_suspended_environment()` - Activate from suspended (skip setup, sync dynamic data only)
- `rebuild_environment()` - Update core, rebuild image (preserve workspace)
- `_sync_dynamic_data()` - Sync prompts, credentials, plugins to running container
- `_sync_plugins_to_environment()` - Sync installed plugins via HTTP API
- `_setup_new_container()` - One-time setup for new containers (install workspace packages)
- `copy_workspace_between_environments()` - Copy workspace folders between environment instance directories
- `_update_environment_config()` - Regenerate auth token, resolve AI credentials, generate .env

### EnvironmentService (`backend/app/services/environments/environment_service.py`)

- `create_environment()` - Entry point for environment creation
- `activate_environment()` - Activate environment for agent. Resolves the workspace copy source synchronously here (before flipping `agent.active_environment_id`) and passes `source_env_id` to the background task to avoid the race
- `rebuild_environment()` - Entry point for rebuild
- `_activate_environment_background()` - Background task for activation; receives `source_env_id` from the caller and uses it directly
- `_find_source_environment_for_workspace_copy()` - Pick best source env by priority: (1) current active env if `!= target`, (2) most-recent non-target env in `running`/`suspended`/`stopped`, (3) most-recent session env

### Workspace Copy Helpers (`backend/app/services/environments/workspace_copy.py`)

- `copy_env_to_env(source_env_id, dest_env_id, *, include_files_folder=True)` - Used by `EnvironmentService._create_environment_background` when a `source_environment_id` is supplied (blue-green / "duplicate environment" flows). Copies bundle-style folders + the two workspace requirements files.
- `seed_workspace_from_bundle_snapshot(snapshot_path, env_id)` - Used by `InstallService.install_bundle` to drop a bundle revision snapshot into a fresh install workspace.
- `replace_bundle_content(snapshot_path, env_id)` - Used by `InstallService.apply_update` to swap bundle-owned folders with a new revision. Preserves `credentials/` and `app-data/`.

### Supporting Services

- `backend/app/services/credentials/credentials_service.py` - `prepare_credentials_for_environment()` - Gather and format credentials for sync
- `backend/app/services/plugins/llm_plugin_service.py` - `prepare_plugins_for_environment()` - Gather and format plugins for sync

### DockerEnvironmentAdapter (`backend/app/services/environments/adapters/docker_adapter.py`)

- `set_agent_prompts()` - HTTP POST to agent-env `/config/agent-prompts`
- `set_credentials()` - HTTP POST to agent-env `/config/credentials`
- `set_plugins()` - HTTP POST to agent-env `/config/plugins`

## Workspace Copy Specifications

### Environment Switch Copy

Folders copied by `copy_workspace_between_environments()` (in `environment_lifecycle.py`):
- `app/workspace/scripts/`
- `app/workspace/docs/`
- `app/workspace/knowledge/`
- `app/workspace/files/`
- `app/workspace/uploads/` (legacy bundle-owned folder; new uploads land in `app-data/uploads/` instead — see [Agent App Data](../agent_app_data/agent_app_data.md))
- `app/workspace/credentials/`
- `app/workspace/plugins/`
- `app/workspace/webapp/`
- `app/workspace/workspace_requirements.txt`
- `app/workspace/workspace_system_packages.txt`

Excluded: `app/workspace/logs/`, `app/workspace/databases/`, `app/workspace/app-data/` (`app-data` is preserved automatically — for bundle agents the bind-mount source is `(user × bundle)` and shared across envs of the same install; for legacy fallback agents it lives outside the env instance dir)

### New Env From Source (`copy_env_to_env`)

Used by `EnvironmentService._create_environment_background` when a `source_environment_id` is supplied:
- Always: `app/workspace/scripts/`, `app/workspace/docs/`, `app/workspace/knowledge/`, `app/workspace/webapp/`
- Optional (`include_files_folder=True`, default): `app/workspace/files/`, `app/workspace/uploads/`
- Files: `app/workspace/workspace_requirements.txt`, `app/workspace/workspace_system_packages.txt`

Excluded: `credentials/` (handled via dynamic sync separately), `app-data/`, `logs/`, `databases/`

### Bundle Snapshot Seed / Apply-Update (`seed_workspace_from_bundle_snapshot` / `replace_bundle_content`)

Used by `InstallService.install_bundle` (initial seed) and `InstallService.apply_update` (revision push). Copies bundle-owned folders from the snapshot into the install env workspace:
- `scripts/`, `docs/`, `knowledge/`, `files/`
- `workspace_requirements.txt`, `workspace_system_packages.txt`

Excluded (preserved): `credentials/` (dynamic sync), `app-data/` (per-user persistent), `logs/`, `databases/`

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

