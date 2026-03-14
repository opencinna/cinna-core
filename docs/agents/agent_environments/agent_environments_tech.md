# Agent Environments - Technical Details

## File Locations

### Environment Templates (source of truth)

- `backend/app/env-templates/app_core_base/` - **Shared core** — single source of truth for `app/core/` across all templates
- `backend/app/env-templates/python-env-advanced/` - Python template (lightweight, `python:3.11-slim`)
- `backend/app/env-templates/general-env/` - General purpose template (full Debian, `python:3.11-bookworm`)

**Shared core** (`backend/app/env-templates/app_core_base/core/`):
- `server/routes.py` - HTTP endpoints, `_store_session_context()` helper, webapp endpoints
- `server/models.py` - Pydantic request/response models
- `server/sdk_manager.py` - Multi-adapter SDK orchestration
- `server/prompt_generator.py` - System prompt generation for building/conversation modes
- `server/agent_env_service.py` - Business logic for workspace file operations
- `server/sdk_utils.py` - Logging, debugging, message formatting
- `server/active_session_manager.py` - Session tracking, HMAC-verified per-session context store
- `server/adapters/base.py` - `SDKEvent`, `SDKConfig`, `BaseSDKAdapter`, `AdapterRegistry`
- `server/adapters/claude_code.py` - `ClaudeCodeAdapter` for `claude-code/*` variants
- `server/adapters/google_adk.py` - `GoogleADKAdapter` placeholder for `google-adk-wr/*`
- `scripts/get_session_context.py` - Stdlib-only helper for agent scripts to query session context
- `prompts/BUILDING_AGENT.md` - Building mode system prompt
- `prompts/WEBAPP_BUILDING.md` - Webapp building instructions (read by agent on demand)
- `main.py` - FastAPI entry point

**Per-template files** (template-specific, NOT shared):
- `backend/app/env-templates/<template-name>/` - Template root <!-- nocheck -->
  - `app/workspace/` - Workspace template (scripts/, files/, docs/, credentials/, databases/, knowledge/, logs/)
  - `app/BUILDING_AGENT_EXAMPLE.md` - Template for building mode prompt
  - `Dockerfile` - Image definition (different base image per template)
  - `docker-compose.template.yml` - Container configuration template
  - `pyproject.toml` - Python dependencies (system-level)

### Environment Instances (per-environment)

- `backend/data/environments/{env_id}/` - Instance root
  - `app/core/` - Copied from template, baked into image
  - `app/workspace/` - User data, Docker volume mounted
  - `app/BUILDING_AGENT.md` - Instance-specific building prompt
  - `app/.env` - Application environment variables
  - `docker-compose.yml` - Generated from template
  - `.env` - Docker compose variables

### Backend - Models

- `backend/app/models/environment.py` - `AgentEnvironment`, `AgentEnvironmentBase`, `AgentEnvironmentPublic`, `AgentEnvironmentCreate`, `AgentEnvironmentUpdate`
- `backend/app/models/agent.py` - `Agent` model (includes `inactivity_period_limit` field)

### Backend - Routes

- `backend/app/api/routes/environments.py` - Environment CRUD, rebuild, suspend endpoints
- `backend/app/api/routes/workspace.py` - Workspace tree and download proxy endpoints

### Backend - Services

- `backend/app/services/environment_lifecycle.py` - `EnvironmentLifecycleManager` - core lifecycle operations
- `backend/app/services/environment_service.py` - `EnvironmentService` - route-level orchestration
- `backend/app/services/environment_suspension_scheduler.py` - APScheduler background job (inactivity suspension)
- `backend/app/services/environment_status_scheduler.py` - APScheduler background job (health monitoring)
- `backend/app/services/adapters/base.py` - `EnvironmentAdapter` abstract interface, `LocalFilesAccessInterface` optional mixin
- `backend/app/services/adapters/docker_adapter.py` - `DockerEnvironmentAdapter` - Docker-specific implementation (implements `LocalFilesAccessInterface`)
- `backend/app/services/session_context_signer.py` - HMAC signing/verification for session context

### Frontend - Components

- `frontend/src/components/Agents/AgentEnvironmentsTab.tsx` - Environments tab with inactivity period selector
- `frontend/src/components/Environments/EnvironmentCard.tsx` - Environment card with rebuild/suspend actions
- `frontend/src/components/Environment/EnvironmentPanel.tsx` - Workspace file browser panel
  - `TabHeader.tsx`, `WorkspaceTabContent.tsx`, `TreeItemRenderer.tsx`, `StateComponents.tsx`, `FileIcon.tsx`
  - `types.ts`, `utils.ts`
- `frontend/src/routes/_layout/session/$sessionId.tsx` - Session UI with activation status, WebSocket event listeners
- `frontend/src/services/eventService.ts` - `sendAgentUsageIntent()`, event subscriptions

### Migrations

- `backend/app/alembic/versions/813b0bf363af_add_last_activity_at_to_agent_.py` - Added `last_activity_at` and suspension statuses

## Database Schema

### AgentEnvironment model (`backend/app/models/environment.py`)

Key fields:
- `id` (UUID) - Primary key
- `agent_id` (UUID, FK) - Parent agent
- `status` (str) - One of: `stopped`, `creating`, `building`, `initializing`, `starting`, `running`, `rebuilding`, `suspended`, `activating`, `error`, `deprecated`
- `status_message` (str, nullable) - Human-readable status detail
- `last_activity_at` (datetime, nullable) - Last user activity timestamp
- `conversation_ai_credential_id` (UUID, nullable, FK) - AI credential for conversation mode
- `building_ai_credential_id` (UUID, nullable, FK) - AI credential for building mode
- `config` (JSON) - Runtime configuration including `auth_token`

### Agent model (`backend/app/models/agent.py`)

Relevant field:
- `inactivity_period_limit` (str, nullable) - Controls auto-suspension threshold. Values: `None` (10 min default), `"2_days"`, `"1_week"`, `"1_month"`, `"always_on"`

## API Endpoints

### Backend API (routes: `backend/app/api/routes/environments.py`)

- `POST /api/v1/environments/{id}/rebuild` - Trigger environment rebuild
- `POST /api/v1/environments/{id}/suspend` - Manually suspend environment
- `GET /api/v1/environments/{id}` - Get environment details
- `PUT /api/v1/environments/{id}` - Update environment settings

### Backend Workspace Proxy (routes: `backend/app/api/routes/workspace.py`)

- `GET /api/v1/environments/{env_id}/workspace/tree` - Proxy workspace tree request to agent-env
- `GET /api/v1/environments/{env_id}/workspace/download/{path}` - Stream file/folder downloads from agent-env

### Agent-Env Internal API (routes: `backend/app/env-templates/.../core/server/routes.py`)

- `POST /chat/stream` - Streaming chat (SSE) with AI agent
- `POST /chat` - Synchronous chat (non-streaming)
- `GET /config/agent-prompts` - Get current prompts from workspace
- `POST /config/agent-prompts` - Update prompts in workspace
- `GET /health` - Health check (Docker HEALTHCHECK, backend monitoring)
- `GET /session/context` - Get HMAC-verified session context metadata
- `GET /workspace/tree` - Complete workspace file tree
- `GET /workspace/download/{path}` - Download files or folders (ZIP for folders)

## Services & Key Methods

### LocalFilesAccessInterface (`backend/app/services/adapters/base.py`)

An optional mixin interface for adapters that can provide direct local filesystem access to workspace files without requiring the container to be running.

- `get_local_workspace_file_path(relative_path: str) -> Path | None` — returns absolute path to a workspace file, or None if not found or unsafe
- `list_local_workspace_files(subfolder: str = "files") -> list[str]` — lists files in a workspace subfolder, returns sorted relative paths
- Callers check `isinstance(adapter, LocalFilesAccessInterface)` to detect support — no configuration needed
- `DockerEnvironmentAdapter` implements this interface — workspace files are stored at `{env_dir}/app/workspace/` which is a Docker volume accessible directly on the host filesystem
- Adapters that do NOT implement it fall back to `download_workspace_item()`, which requires the container to be running
- Cloud/distributed adapters may implement this interface if they auto-sync workspace files to a local cache directory
- Used by `UserDashboardService` env-file methods: `list_env_files()` calls `list_local_workspace_files()`, `get_env_file_local_path()` calls `get_local_workspace_file_path()`
- Dashboard endpoints: `GET /api/v1/dashboards/{id}/blocks/{block_id}/env-files` (list) and `GET .../env-file` (stream)

### EnvironmentLifecycleManager (`backend/app/services/environment_lifecycle.py`)

- `create_environment_instance()` - Copy template + shared core, generate configs, build Docker image
- `start_environment()` - UP operation with smart container detection (new vs existing)
- `stop_environment()` - STOP operation, keep container
- `suspend_environment()` - STOP operation with `suspended` status
- `activate_suspended_environment()` - UP operation optimized for existing containers
- `rebuild_environment()` - DOWN → build → UP with full setup; core replaced from shared `app_core_base`
- `delete_environment_instance()` - DOWN operation with volume cleanup
- `_container_exists()` - Check if Docker container exists (stopped or running)
- `_sync_dynamic_data()` - Sync prompts and credentials to running environment
- `_setup_new_container()` - Install workspace Python packages and system packages (only for new containers)
- `_update_environment_config()` - Regenerate auth token, docker-compose.yml, .env
- `_generate_auth_token()` - Create 10-year JWT with user ID as subject
- `_generate_env_file()` - Generate .env with AI credential auto-detection by prefix

### DockerEnvironmentAdapter (`backend/app/services/adapters/docker_adapter.py`)

- `initialize()` - Build Docker image (`docker-compose build`)
- `start()` - UP operation (`docker-compose up -d`), wait for health check
- `stop()` - STOP operation (`docker-compose stop`)
- `rebuild()` - DOWN + build + optional UP
- `delete()` - DOWN with volumes (`docker-compose down -v --remove-orphans`)
- `install_custom_packages()` - Install workspace Python dependencies from `workspace_requirements.txt`
- `install_system_packages()` - Install OS-level packages from `workspace_system_packages.txt` via `apt-get`
- `set_agent_prompts()` - Sync prompts via HTTP API to agent-env
- `set_credentials()` - Sync credentials via HTTP API to agent-env
- `get_container()` - Get Docker container object for existence check
- `get_workspace_tree()` - HTTP proxy to agent-env `/workspace/tree`
- `download_workspace_item()` - HTTP streaming proxy to agent-env `/workspace/download/{path}`
- `get_local_workspace_file_path(relative_path)` - Returns the absolute local filesystem path for a workspace file, or None if not found or unsafe. Rejects `..` and absolute paths. Resolves symlinks and validates the result stays within `{env_dir}/app/workspace/`. (`LocalFilesAccessInterface` implementation)
- `list_local_workspace_files(subfolder)` - Lists files under `{env_dir}/app/workspace/{subfolder}/` recursively. Returns sorted relative paths from the subfolder root. (`LocalFilesAccessInterface` implementation)

### EnvironmentSuspensionScheduler (`backend/app/services/environment_suspension_scheduler.py`)

- `start_scheduler()` - Initialize APScheduler background job (10-minute interval)
- `shutdown_scheduler()` - Clean shutdown
- `run_suspension_check()` - Check all running environments against inactivity thresholds

### EnvironmentStatusScheduler (`backend/app/services/environment_status_scheduler.py`)

- `start_scheduler()` - Initialize APScheduler background job (10-minute interval)
- `shutdown_scheduler()` - Clean shutdown
- `run_status_check()` - Check all running environments via health check; mark crashed ones as `error`
- `_check_environment_statuses()` - Async implementation: queries running envs, calls `health_check()` + `get_status()`, updates `last_health_check`, emits `ENVIRONMENT_STATUS_CHANGED` event on failure

### EventService (`backend/app/services/event_service.py`)

- `is_user_online(user_id)` - Check active WebSocket connections
- `agent_usage_intent` handler - Updates `last_activity_at`, triggers background activation if suspended
- `_activate_environment_sync()` - Synchronous wrapper for async activation (runs in ThreadPoolExecutor)

### AgentEnvService (inside container: `core/server/agent_env_service.py`)

- `get_agent_prompts()` - Read WORKFLOW_PROMPT.md and ENTRYPOINT_PROMPT.md
- `update_agent_prompts()` - Write prompts to workspace/docs/
- `validate_workspace()` - Verify workspace exists and is writable
- `validate_workspace_path()` - Security: prevent directory traversal, validate symlinks

### SDKManager (inside container: `core/server/sdk_manager.py`)

- `send_message_stream()` - Route to appropriate adapter based on ENV config, convert SDKEvent to dict
- `_get_adapter(mode)` - Select adapter from `SDK_ADAPTER_{MODE}` environment variable

## Frontend Components

### AgentEnvironmentsTab (`frontend/src/components/Agents/AgentEnvironmentsTab.tsx`)

- Inactivity period selector (Select dropdown)
- Environment list with cards

### AddEnvironment (`frontend/src/components/Environments/AddEnvironment.tsx`)

- Environment template selector (Python / General Purpose)
- SDK selectors for conversation and building modes
- AI credential configuration (default or custom)

### EnvironmentCard (`frontend/src/components/Environments/EnvironmentCard.tsx`)

- Template badge (Python / General Purpose) alongside SDK badges
- Active + Running → "Suspend" button (Pause icon)
- Inactive → "Delete" button
- Rebuild button for all environments

### Session UI (`frontend/src/routes/_layout/session/$sessionId.tsx`)

- `isEnvActivating` state tracks activation progress
- Suspended/Activating → "Activating..." button with spinner
- Running → normal "App" button
- WebSocket event listeners: `ENVIRONMENT_ACTIVATING`, `ENVIRONMENT_ACTIVATED`, `ENVIRONMENT_ACTIVATION_FAILED`, `ENVIRONMENT_SUSPENDED`

### EnvironmentPanel (`frontend/src/components/Environment/EnvironmentPanel.tsx`)

- Workspace file browser with tree view
- React Query with 5-second cache, conditional fetching (panel open + env ID available)
- Download via Axios interceptor with blob handling

## Configuration

### Environment Variables (inside Docker container)

**Required**:
- `ENV_ID` - Environment UUID
- `AGENT_ID` - Agent UUID
- `CLAUDE_CODE_WORKSPACE` - Path to workspace (`/app/workspace`)
- `AGENT_AUTH_TOKEN` - JWT bearer token for backend API authentication

**AI Credentials** (one set, auto-detected by prefix):
- `ANTHROPIC_API_KEY` - Set when credential prefix is `sk-ant-api*`
- `CLAUDE_CODE_OAUTH_TOKEN` - Set when credential prefix is `sk-ant-oat*`

**SDK Adapter Configuration**:
- `SDK_ADAPTER_BUILDING` - Adapter ID for building mode (default: `claude-code/anthropic`)
- `SDK_ADAPTER_CONVERSATION` - Adapter ID for conversation mode (default: `claude-code/anthropic`)
- Adapter ID format: `<adapter-type>/<provider>` (e.g., `claude-code/minimax`, `google-adk-wr/gemini`)

**Optional**:
- `CLAUDE_CODE_PERMISSION_MODE` - Permission mode for SDK (default: `acceptEdits`)
- `DUMP_LLM_SESSION` - Enable session logging (`true`/`false`)
- `ENV_NAME` - Human-readable environment name
- `PYTHONPATH` - Set to `/app` for imports

### Docker Configuration

Each template has its own Dockerfile and docker-compose template. Both share the same build structure:

**Dockerfile** (`backend/app/env-templates/<template>/Dockerfile`): <!-- nocheck -->
- Base image: `python:3.11-slim` (python-env-advanced) or `python:3.11-bookworm` (general-env)
- Installs system deps (curl, git, Node.js for Claude Code)
- Installs uv package manager and Claude Code CLI globally
- Copies `pyproject.toml`, installs template dependencies
- Copies `app/core` into image (sourced from shared `app_core_base` at instance creation)
- CMD: `fastapi run core/main.py`

**docker-compose.template.yml** (`backend/app/env-templates/<template>/docker-compose.template.yml`): <!-- nocheck -->
- Volume mounts: `core:/app/core:ro` (read-only), `workspace:/app/workspace` (read-write)
- Networks: `agent-bridge` (shared with backend), `agent-env-${ENV_ID}` (isolated)
- Variables substituted: `${ENV_ID}`, `${AGENT_ID}`, `${ENV_VERSION}`, `${AGENT_PORT}`, `${AGENT_AUTH_TOKEN}`

### Rebuild Overwrite Files

Infrastructure files overwritten from template during rebuild (defined in `REBUILD_OVERWRITE_FILES`):
- `uv.lock`, `pyproject.toml`, `Dockerfile`, `docker-compose.template.yml`

### Shared Core Directory

The `app/core/` directory is maintained in a single shared location (`backend/app/env-templates/app_core_base/core/`) and is:
- Copied into environment instances during creation (overlaid after template-specific files)
- Used as the source during rebuild (replaces the instance's `app/core/` entirely)
- Defined by `APP_CORE_BASE_DIR_NAME` constant in `backend/app/services/environment_lifecycle.py`

### Agent-Level Settings

- `inactivity_period_limit` on `Agent` model - Updated via `PUT /api/v1/agents/{id}` (`AgentUpdate` schema)
- UI: Select dropdown in Agent Config → Environments tab

## Security

### JWT Authentication

- All agent-env HTTP endpoints require `Authorization: Bearer {token}` header
- Token: 10-year JWT with agent owner's user ID as subject, signed with `settings.SECRET_KEY` (HS256)
- Regenerated on every rebuild and start operation
- Stored in `environment.config["auth_token"]` and `.env` file

### Session Context HMAC Verification

- Backend signs `session_context` with `AGENT_AUTH_TOKEN` using HMAC-SHA256 before sending
- Agent-env verifies signature before storing context - forged context rejected
- Canonical JSON (`sort_keys=True, separators=(',',':')`) for deterministic signing
- Per-session context store keyed by `backend_session_id` supports parallel sessions
- Cleanup: explicit on stream end + TTL-based (24h) fallback
- Signing module: `backend/app/services/session_context_signer.py`

### Workspace Isolation

- Agent operates in `/app/workspace` only
- `/app/core` mounted read-only (`:ro`) - LLM cannot modify server code or HMAC verification logic
- `AgentEnvService.validate_workspace_path()` prevents directory traversal: rejects absolute paths, `..` references, validates symlinks stay within workspace boundary

### Credential Security

- API keys stored in `/app/workspace/credentials/` (encrypted)
- Not logged or exposed in responses
- Agent can read but frontend cannot access directly

### Backend Proxy Authorization

- Workspace proxy endpoints (`/api/v1/environments/{env_id}/workspace/*`) require user to own the agent
- Environment must be running for proxy requests to succeed
- Streaming downloads: 64KB chunks, no buffering, direct proxy from agent-env to client

## WebSocket Events

| Event | Direction | Purpose |
|-------|-----------|---------|
| `ENVIRONMENT_ACTIVATING` | Backend → Frontend | Activation started |
| `ENVIRONMENT_ACTIVATED` | Backend → Frontend | Activation successful, ready for use |
| `ENVIRONMENT_ACTIVATION_FAILED` | Backend → Frontend | Activation failed |
| `ENVIRONMENT_SUSPENDED` | Backend → Frontend | Environment suspended |
| `ENVIRONMENT_STATUS_CHANGED` | Backend → Frontend | Environment status changed (e.g., health check detected crash → error) |
| `agent_usage_intent` | Frontend → Backend | User opened session, triggers activity tracking and potential activation |

Event types defined in `backend/app/models/event.py`

### Thread Pool Isolation

Background activation runs in `ThreadPoolExecutor` (4 workers) in `event_service.py` to prevent blocking the Socket.IO event loop during Docker operations.

