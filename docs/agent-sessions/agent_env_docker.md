# Docker Agent Environments - Build Architecture

## Overview

Agent environments run in isolated Docker containers with a dual-layer architecture: **core** (system files) and **workspace** (user files). This separation enables updating system code without losing user data.

## Core Concepts

### Two-Layer Architecture

**Core Layer** (`/app/core/`)
- System code (FastAPI server, SDK manager, API routes)
- Baked into Docker image during build
- Updated via rebuild operation
- Immutable at runtime

**Workspace Layer** (`/app/workspace/`)
- User-generated content (scripts, files, docs, credentials, databases, logs)
- Integration knowledge base (updated during rebuilds, user files preserved)
- Mounted as Docker volume
- Persists across rebuilds and restarts
- Writable by agent at runtime

**Template Files** (`/app/`)
- Static configuration files (BUILDING_AGENT_EXAMPLE.md, AGENT_EXAMPLE.md, etc.)
- Copied to instance during initialization
- Not updated during rebuilds

### Why This Separation?

**Problem**: Updating system code (bug fixes, new features) required destroying and recreating environments, losing all user work.

**Solution**: Separate system code (core) from user data (workspace). System updates only affect core layer; workspace remains untouched.

## File Structure

### Template Directory
```
backend/app/env-templates/python-env-advanced/
├── app/
│   ├── core/                          # System code (baked into image)
│   │   ├── __init__.py
│   │   ├── main.py                    # FastAPI entry point
│   │   └── server/                    # API server code (modular architecture)
│   │       ├── routes.py              # HTTP endpoints
│   │       ├── models.py              # Pydantic request/response models
│   │       ├── sdk_manager.py         # SDK session orchestration
│   │       ├── prompt_generator.py    # System prompt generation
│   │       ├── agent_env_service.py   # Business logic (file operations)
│   │       └── sdk_utils.py           # Logging, debugging, formatting
│   │
│   ├── workspace/                     # User data (volume mounted)
│   │   ├── scripts/                   # Agent-created Python scripts
│   │   │   └── README.md              # Scripts catalog (maintained by agent)
│   │   ├── files/                     # Uploaded/generated files
│   │   ├── docs/                      # Workflow documentation
│   │   │   ├── WORKFLOW_PROMPT.md     # Conversation mode system prompt
│   │   │   └── ENTRYPOINT_PROMPT.md   # Trigger message examples
│   │   ├── credentials/               # API keys, tokens (encrypted)
│   │   ├── databases/                 # SQLite/other local DBs
│   │   ├── knowledge/                 # Integration knowledge base
│   │   │   ├── odoo-erp/              # Example: Odoo integration docs
│   │   │   │   ├── general_info.md    # API guides, best practices
│   │   │   │   └── vendor_bills.md    # Data schemas, field definitions
│   │   │   └── [other-topics]/        # Additional integration topics
│   │   ├── logs/                      # Execution logs, session dumps
│   │   └── workspace_requirements.txt # Agent-installed Python packages (persists across rebuilds)
│   │
│   ├── BUILDING_AGENT_EXAMPLE.md      # Template for building mode prompt
│   └── ENTRYPOINT_EXAMPLE.md          # Template for trigger message (deprecated)
│
├── Dockerfile                         # Image definition
├── docker-compose.template.yml        # Container configuration template
└── pyproject.toml                     # Python dependencies
```

### Instance Directory
```
backend/data/environments/{env_id}/
├── app/
│   ├── core/                          # Copied from template, baked into image
│   ├── workspace/                     # User data, volume mounted
│   ├── BUILDING_AGENT.md              # Instance-specific (from template)
│   └── .env                           # Environment variables
├── docker-compose.yml                 # Generated from template
└── .env                               # Docker compose variables
```

## Core Server Architecture

### Modular Design (Refactored)

The core server follows a **modular architecture** with clear separation of concerns:

**modules**:
- **routes.py**: HTTP API endpoints, request validation, response formatting
- **sdk_manager.py**: SDK session orchestration, streaming coordination (~ 220 lines, down from 600)
- **prompt_generator.py**: System prompt generation for building/conversation modes
- **agent_env_service.py**: Business logic for workspace file operations
- **sdk_utils.py**: Logging, debugging, message formatting utilities
- **models.py**: Pydantic request/response models

**Benefits**:
- **Testability**: Each module can be tested independently
- **Maintainability**: Changes isolated to relevant modules
- **Extensibility**: Easy to add new SDKs or prompt strategies
- **Clarity**: Clear module boundaries and responsibilities

**Key Changes from Previous Architecture**:
- Prompt loading extracted to `PromptGenerator` (was in `sdk_manager.py`)
- Business logic moved to `AgentEnvService` (was in `routes.py`)
- Debugging utilities extracted to `sdk_utils.py` (was in `sdk_manager.py`)
- SDK manager now focused on orchestration only

### Session Modes

**Building Mode**:
- **Model**: Claude Sonnet (default) - better code generation
- **System Prompt**: Claude Code preset + comprehensive docs
- **Purpose**: Develop workflows, create scripts, configure integrations

**Conversation Mode**:
- **Model**: Claude Haiku - faster and cheaper
- **System Prompt**: Workflow-specific instructions (no preset)
- **Purpose**: Execute tasks using pre-built workflows

**SDK Support**:
- Current: `agent_sdk="claude"` (Claude SDK via ClaudeAgentOptions)
- Future: Can add OpenAI, Google, etc.

### Integration Knowledge Base

**Purpose**: Provide agents with integration-specific documentation without bloating system prompts

**Location**: `/app/workspace/knowledge/` (workspace, persists across rebuilds)

**Organization**:
- Topic-based subdirectories (e.g., `odoo-erp/`, `salesforce/`, `stripe/`)
- Markdown files containing API guides, data schemas, best practices
- Minimal footprint: Only topic folder names included in prompts

**Prompt Integration**:
- `PromptGenerator._get_knowledge_topics()` scans for topic folders
- Returns comma-separated list (e.g., "odoo-erp, salesforce, stripe")
- Added to both building and conversation mode prompts
- Agent reads specific files on-demand when needed

**Rebuild Behavior**:
- Template knowledge files are synced to environment (add/update only)
- User-created knowledge files are preserved
- No deletions during rebuild
- Enables distributing new integration docs to all environments

**Example**:
```
workspace/knowledge/
├── odoo-erp/
│   ├── general_info.md      # Connection methods, architecture
│   ├── vendor_bills.md       # Field schemas, workflows
│   └── sales_orders.md       # Operations, best practices
└── custom-integration/       # User-created, won't be deleted
    └── api_notes.md
```

### Workspace Data Access

**Purpose**: Expose workspace file structure to external systems (backend, frontend) for browsing and downloading agent-generated artifacts

**API Endpoints** (Agent-Env Core):
- `GET /workspace/tree` - Returns complete file tree for files, logs, scripts, docs folders
- `GET /workspace/download/{path}` - Downloads individual files or folders (as ZIP)

**Implementation**: `backend/app/env-templates/python-env-advanced/app/core/server/`
- **routes.py**: HTTP endpoints with auth validation, streaming responses
- **agent_env_service.py**: Business logic with security-critical path validation
- **models.py**: Pydantic schemas (`FileNode`, `FolderSummary`, `WorkspaceTreeResponse`)

**Security** (`AgentEnvService.validate_workspace_path()`):
- Rejects absolute paths and parent directory references (`..`)
- Resolves to absolute path and validates workspace boundary
- Validates symlinks don't point outside workspace
- Prevents directory traversal attacks

**Backend Proxy Layer** (`backend/app/api/routes/workspace.py`):
- `GET /api/v1/environments/{env_id}/workspace/tree` - Proxies tree request to agent-env
- `GET /api/v1/environments/{env_id}/workspace/download/{path}` - Streams downloads directly to browser
- Permission checks: User must own agent, environment must be running
- Streaming: No buffering, 64KB chunks, direct proxy from agent-env to client

**Adapter Pattern** (`backend/app/services/adapters/`):
- **base.py**: Abstract methods `get_workspace_tree()`, `download_workspace_item()`
- **docker_adapter.py**: HTTP proxy implementation with auth headers, streaming support
- Universal interface works for Docker, future SSH/K8s/HTTP adapters

**File Operations**:
- Tree building: Recursive traversal with unlimited depth, metadata extraction (size, modified date)
- Downloads: Single files streamed directly, folders zipped on-the-fly in `/tmp`, auto-cleanup
- Folder summaries: Calculated fileCount and totalSize for each workspace section

**Frontend Integration**:
- Main component: `frontend/src/components/Environment/EnvironmentPanel.tsx`
- Subcomponents: `TabHeader.tsx`, `WorkspaceTabContent.tsx`, `TreeItemRenderer.tsx`, `StateComponents.tsx`, `FileIcon.tsx`
- Utilities: `types.ts`, `utils.ts` (formatting, conversion, interfaces)
- React Query: Fetches tree data when panel opens, 5-second cache
- State management: Loading, error, empty, no-environment states via dedicated components
- Downloads: Axios interceptor for blob handling, authenticated requests, browser download trigger
- Conditional fetching: Queries only when panel open and environment ID available

### Python Dependencies

**Two-Layer System**: Template dependencies (system-level) + Workspace dependencies (integration-specific)

**Template Dependencies** (`pyproject.toml`):
- Pre-installed packages: `fastapi`, `uvicorn`, `pydantic`, `httpx`, `requests`, `claude-agent-sdk`
- Baked into Docker image during build
- Updated via environment rebuild

**Workspace Dependencies** (`workspace/workspace_requirements.txt`):
- Integration-specific packages: `odoo-rpc-client`, `salesforce-api`, `stripe`, etc.
- Installed by agent using `uv pip install <package>`
- Added to `workspace_requirements.txt` for persistence
- Auto-installed on container startup (after health check)
- **Persists across rebuilds**

**Installation Flow**:
1. Agent runs: `uv pip install <package>` (immediate use)
2. Agent adds to: `workspace_requirements.txt` (persistence)
3. On next start/rebuild: Auto-installed via `DockerEnvironmentAdapter.install_custom_packages()`

**Implementation**: `backend/app/services/adapters/docker_adapter.py::install_custom_packages()`

## Operations

### Docker Terminology

The environment lifecycle uses standard Docker operations:

- **UP** (`docker-compose up`) - Creates and starts container
  - If container doesn't exist: creates new container from image
  - If container exists but stopped: starts existing container
- **STOP** (`docker-compose stop`) - Stops container but keeps it
  - Container still exists and can be restarted quickly
  - All data and configuration preserved
- **DOWN** (`docker-compose down`) - Removes container completely
  - Container deleted, new container will be created on next UP
  - Volumes preserved (workspace data not lost)

### Data Sync Strategy

**Dynamic Data** (synced on every container start):
- Agent prompts (workflow_prompt, entrypoint_prompt)
- Credentials files
- Applied when: Container starts (new or existing), backend updates agent config

**Container Setup** (only for NEW containers):
- Installing custom Python packages from `workspace_requirements.txt`
- One-time container initialization
- Applied when: New container created (first start or after rebuild)
- NOT applied when: Restarting existing stopped/suspended container

### Environment Creation

**Entry Point**: `backend/app/services/environment_lifecycle.py::create_environment_instance()`

**Process**:
1. Copy entire template → instance directory
2. Create `BUILDING_AGENT.md` from `BUILDING_AGENT_EXAMPLE.md`
3. Generate `docker-compose.yml` (replace variables: `ENV_ID`, `AGENT_ID`, `HOST_INSTANCE_DIR`)
4. Generate `.env` files (ports, auth tokens, resource limits)
5. Build Docker image via `DockerEnvironmentAdapter.initialize()`
   - Copies `app/core` into image (Dockerfile line 50)
   - Installs Python dependencies
   - Sets CMD to run `core/main.py`

**Result**: Stopped environment ready to start

### Environment Start

**Entry Point**: `backend/app/services/environment_lifecycle.py::start_environment()`

**Docker Operation**: `UP` (docker-compose up) - creates or starts container

**Process**:
1. Check if container exists (using `_container_exists()` helper)
2. Update configuration files (regenerate auth token, docker-compose.yml, .env)
3. Run `docker-compose up -d`:
   - If container doesn't exist: creates new container from image
   - If container exists but stopped: starts existing container
4. Mount volumes:
   - `{instance_dir}/app/core:/app/core` (read-only in practice)
   - `{instance_dir}/app/workspace:/app/workspace` (read-write)
5. Wait for health check at `/health` endpoint
6. **If container is NEW** (didn't exist before):
   - Install workspace dependencies from `workspace_requirements.txt` via `_setup_new_container()`
   - One-time container initialization
7. **Always** sync dynamic data via `_sync_dynamic_data()`:
   - Sync prompts to `workspace/docs/` via `DockerEnvironmentAdapter.set_agent_prompts()`
   - Sync credentials via `DockerEnvironmentAdapter.set_credentials()`
8. Update database status to `running`

**Optimization**: Restarting an existing stopped container skips package installation (step 6), making restarts much faster.

**Note**: Core files are both baked into image AND volume-mounted for easier development. Production could use image-only.

### Environment Rebuild

**Entry Point**: `backend/app/services/environment_lifecycle.py::rebuild_environment()`

**Docker Operation**: `DOWN` → build → `UP` - removes old container, creates new one

**Purpose**: Update core system files and knowledge base from template while preserving workspace data

**Process**:
1. Check if environment is running → stop if needed (`STOP` - docker-compose stop)
2. **Remove old container** via `docker-compose down` (`DOWN` operation):
   - Old container completely deleted
   - Volumes kept (workspace data preserved)
   - New container will be created from new image
3. Delete old core directory: `{instance_dir}/app/core`
4. Copy fresh core from template: `{template_dir}/app/core` → `{instance_dir}/app/core`
5. Update knowledge files from template: `{template_dir}/app/workspace/knowledge` → `{instance_dir}/app/workspace/knowledge`
   - **Add/Update only**: New and updated knowledge files from template are copied
   - **Preserve user files**: User-created knowledge files not in template are kept
   - **No deletions**: Existing knowledge files are never deleted
6. Rebuild Docker image via `DockerEnvironmentAdapter.rebuild()`
   - Runs `docker-compose build` (uses cache for speed)
   - New core files baked into image
7. **If was running**: Start NEW container (`UP` - docker-compose up -d):
   - Creates completely new container from new image
   - Run full container setup via `_setup_new_container()` (install packages)
   - Sync dynamic data via `_sync_dynamic_data()` (prompts, credentials)
8. Update status to `running` or `stopped`

**Key Point**: Rebuild creates a NEW container (not restarting old one), so full container setup is required.

**Preserved**:
- All workspace data (scripts, files, docs, credentials, databases, logs)
- User-created knowledge files (not in template)
- **Workspace dependencies** (`workspace_requirements.txt`)
- Docker volumes
- Environment configuration
- Agent prompts

**Updated**:
- Core server code (modular architecture)
- API routes and request handling
- SDK manager orchestration
- Prompt generation logic
- Business logic services
- Utility functions
- Docker image layers
- **Template dependencies** (pyproject.toml packages)
- Knowledge base files from template (add/update only, no deletions)

### Environment Suspension & Activation

**Entry Points**:
- `backend/app/services/environment_lifecycle.py::suspend_environment()`
- `backend/app/services/environment_lifecycle.py::activate_suspended_environment()`

**Docker Operations**:
- Suspension: `STOP` (docker-compose stop) - keeps container
- Activation: `UP` (docker-compose up) - starts existing container

**Purpose**: Gracefully manage resource usage by suspending inactive environments and reactivating them on-demand

**Key Optimization**: Suspended containers are STOPPED (not removed), so activation is fast and skips package installation.

#### Automatic Suspension

**Scheduler**: `backend/app/services/environment_suspension_scheduler.py`
- Runs every 10 minutes (background APScheduler job)
- Checks all `running` environments for inactivity

**Suspension Criteria** (ALL must be true):
1. Environment status is `running`
2. Last activity > 10 minutes ago (tracked via `last_activity_at`)
3. EITHER:
   - User is offline (no active WebSocket connection), OR
   - Environment is not the active one for its agent

**Process**:
1. Stop Docker container via `DockerEnvironmentAdapter.stop()` → `docker-compose stop` (`STOP` operation)
2. Container is stopped but NOT removed (suspended state, not destroyed)
3. Container and all its configuration preserved (fast reactivation)
4. Set status to `suspended` (instead of `stopped`)
5. Set status message: "Environment suspended due to inactivity"
6. Emit `ENVIRONMENT_SUSPENDED` WebSocket event to user

**Activity Tracking**:
- `last_activity_at` updated when:
  - User sends a message to the environment
  - User opens a session with the environment
  - User sends `agent_usage_intent` WebSocket event (opens session in UI)

#### Manual Suspension

**API Endpoint**: `POST /api/v1/environments/{id}/suspend`

**UI Location**: Environment card "Suspend" button (replaces "Delete" for active environments)

**Confirmation Dialog**:
```
Suspend this environment?

This will stop the container to save resources.
The environment will automatically reactivate when you send a message or open a session.
```

**Process**: Same as automatic suspension, but user-initiated

#### Automatic Activation

**Triggers**:
1. **User opens session**: `agent_usage_intent` WebSocket event → triggers background activation
2. **User sends message**: Message service detects `suspended` status → activates synchronously before sending

**WebSocket Event Flow** (`agent_usage_intent` handler in `event_service.py`):
1. Frontend sends `agent_usage_intent` event with `environment_id`
2. Backend updates `last_activity_at` timestamp
3. If environment is `suspended`:
   - Spawns activation in background thread pool (non-blocking)
   - Returns `{status: "activating"}` immediately
   - Activation runs in separate thread to avoid blocking Socket.IO event loop
4. If environment is already `running` or other status:
   - Returns current status

**Message Service Activation** (`session_service.py::initiate_stream()`):
1. Check environment status before processing message
2. If `suspended`:
   - Call `activate_suspended_environment()` synchronously
   - Wait for activation to complete (emits events)
   - Proceed with message streaming
3. Update `last_activity_at` after activation

**Activation Process** (`activate_suspended_environment()`):
1. Emit `ENVIRONMENT_ACTIVATING` WebSocket event
2. Set status to `activating`, status_message: "Activating environment..."
3. Update configuration files (regenerate auth token, docker-compose.yml, .env)
4. Start Docker container via `DockerEnvironmentAdapter.start()` → `docker-compose up -d` (`UP` operation)
5. Existing stopped container starts (no rebuild, no new container created)
6. Wait for health check (up to 120 seconds)
7. **Skip container setup** (packages already installed in existing container)
8. **Only sync dynamic data** via `_sync_dynamic_data()`:
   - Sync prompts to `workspace/docs/`
   - Sync credentials files
9. Set status to `running`, status_message: "Environment activated"
10. Update `last_activity_at` to current time
11. Emit `ENVIRONMENT_ACTIVATED` WebSocket event

**Performance Optimization**:
- Container already exists with packages installed
- No package installation step (unlike new container or rebuild)
- Only syncs dynamic data (prompts and credentials)
- Typical activation time: < 10 seconds

**Error Handling**:
- On failure: Set status to `error`, emit `ENVIRONMENT_ACTIVATION_FAILED` event
- Errors include Docker issues, health check timeouts, configuration problems

#### Frontend Integration

**Session UI** (`frontend/src/routes/_layout/session/$sessionId.tsx`):

**State Management**:
- `isEnvActivating` state tracks activation in progress
- Updated via:
  - Environment query data (status === "suspended" | "activating")
  - WebSocket events (ENVIRONMENT_ACTIVATING, ENVIRONMENT_ACTIVATED, etc.)

**UI Behavior**:
- **Suspended/Activating**: Shows "Activating..." button with spinner (replaces "App" button)
- **Running**: Shows normal "App" button
- User can type messages during activation (queued, sent after activation completes)

**WebSocket Event Listeners**:
- `ENVIRONMENT_ACTIVATING` → Set activating state, invalidate environment query
- `ENVIRONMENT_ACTIVATED` → Clear activating state, show success toast, invalidate query
- `ENVIRONMENT_ACTIVATION_FAILED` → Clear activating state, show error toast, invalidate query
- `ENVIRONMENT_SUSPENDED` → Clear activating state, invalidate query

**Environment Panel** (`frontend/src/components/Environments/EnvironmentCard.tsx`):
- Active + Running → Shows "Suspend" button (Pause icon)
- Inactive → Shows "Delete" button (cannot delete active environments)

#### Implementation Details

**Thread Pool Isolation** (`event_service.py`):
- Background activation runs in `ThreadPoolExecutor` (4 workers)
- Prevents blocking Socket.IO event loop during Docker operations
- Uses `_activate_environment_sync()` method (synchronous wrapper for async activation)
- Ensures WebSocket connection remains responsive during activation

**Database Schema Updates**:
- Added `suspended` status to environment status enum
- Added `activating` status for activation in progress
- Added `last_activity_at` timestamp field (nullable datetime)

**Migration**: `backend/app/alembic/versions/813b0bf363af_add_last_activity_at_to_agent_.py`

**Event Types** (`backend/app/models/event.py`):
- `ENVIRONMENT_ACTIVATING` - Activation started
- `ENVIRONMENT_ACTIVATED` - Activation successful
- `ENVIRONMENT_ACTIVATION_FAILED` - Activation failed
- `ENVIRONMENT_SUSPENDED` - Environment suspended

**User Online Status**:
- Tracked via Socket.IO connections in `event_service.py`
- Method: `is_user_online(user_id)` checks active WebSocket connections
- Used by scheduler to prevent suspending environments when user is online and active

#### Benefits

1. **Resource Efficiency**: Inactive environments don't consume CPU/memory
2. **Seamless UX**: Users don't manage suspension manually (mostly automatic)
3. **Fast Reactivation**: Typically < 10 seconds (existing container starts, no package installation)
4. **Real-time Feedback**: WebSocket events provide instant status updates
5. **No Data Loss**: Workspace data fully preserved during suspension
6. **Cost Savings**: Reduces infrastructure costs for inactive users
7. **Intelligent Scheduling**: Doesn't suspend if user is online with active environment
8. **Container Preservation**: Suspended containers use `STOP` not `DOWN` - container kept intact with all packages installed
9. **Optimized Performance**: Activation only syncs dynamic data, skips container setup
10. **Clear Operations**: Uses standard Docker terminology (UP/STOP/DOWN) for predictable behavior

## Docker Configuration

### Dockerfile

**Location**: `backend/app/env-templates/python-env-advanced/Dockerfile`

**Key Steps**:
- Install system dependencies (curl, git, Node.js for Claude Code)
- Install uv package manager
- Install Claude Code CLI globally
- Copy `pyproject.toml` and install **template dependencies** (system-level packages)
- **Copy `app/core` into image** (line 50)
- Set CMD to run `fastapi run core/main.py`

**Note**: Workspace dependencies (`workspace_requirements.txt`) are installed after container startup, not during image build.

**Environment Variables**:
- `PYTHONPATH=/app` (enables `import core.server.routes`)
- `CLAUDE_CODE_WORKSPACE=/app/workspace` (agent works in workspace)

### docker-compose.template.yml

**Location**: `backend/app/env-templates/python-env-advanced/docker-compose.template.yml`

**Volume Mounts**:
- `${HOST_INSTANCE_DIR}/app/core:/app/core` - Core files (system code)
- `${HOST_INSTANCE_DIR}/app/workspace:/app/workspace` - Workspace files (user data)

**Networks**:
- `agent-bridge` (shared with backend for HTTP communication)
- `agent-env-${ENV_ID}` (isolated network for this environment)

**Variables** (substituted during creation):
- `${ENV_ID}`, `${AGENT_ID}`, `${ENV_VERSION}`, `${AGENT_PORT}`, `${AGENT_AUTH_TOKEN}`

## API Endpoints

**Rebuild**: `POST /api/v1/environments/{id}/rebuild`
- Route: `backend/app/api/routes/environments.py::rebuild_environment()`
- Service: `backend/app/services/environment_service.py::rebuild_environment()`
- Lifecycle: `backend/app/services/environment_lifecycle.py::rebuild_environment()`
- Adapter: `backend/app/services/adapters/docker_adapter.py::rebuild()`

**Suspend**: `POST /api/v1/environments/{id}/suspend`
- Route: `backend/app/api/routes/environments.py::suspend_environment()`
- Service: `backend/app/services/environment_service.py::suspend_environment()`
- Lifecycle: `backend/app/services/environment_lifecycle.py::suspend_environment()`
- Adapter: `backend/app/services/adapters/docker_adapter.py::stop()`

## Database Schema

**Status Values**: `stopped`, `creating`, `building`, `initializing`, `starting`, `running`, `rebuilding`, `suspended`, `activating`, `error`, `deprecated`

**Model**: `backend/app/models/environment.py::AgentEnvironment`

**Additional Fields**:
- `last_activity_at` (datetime, nullable) - Timestamp of last activity (message sent, session opened, usage intent)

**Rebuild Status Flow**:
1. `running` or `stopped` → trigger rebuild
2. `rebuilding` → during rebuild operation
3. `running` (if was running) or `stopped` (if was stopped) → after rebuild

**Suspension Status Flow**:
1. `running` → idle for 10+ minutes (or manual suspension)
2. `suspended` → environment suspended
3. User opens session or sends message
4. `activating` → activation in progress
5. `running` → environment ready for use

## Implementation References

**Environment Lifecycle Manager**:
- `backend/app/services/environment_lifecycle.py`
- Public Methods:
  - `create_environment_instance()` - Copy template, build image
  - `start_environment()` - UP operation, smart container setup
  - `stop_environment()` - STOP operation, keep container
  - `suspend_environment()` - STOP operation with suspended status
  - `activate_suspended_environment()` - UP operation, optimized for existing containers
  - `rebuild_environment()` - DOWN → build → UP, full setup
  - `delete_environment_instance()` - DOWN operation with cleanup
- Helper Methods:
  - `_container_exists()` - Check if container exists (stopped or running)
  - `_sync_dynamic_data()` - Sync prompts and credentials (always needed)
  - `_setup_new_container()` - Install packages and one-time setup (only for new containers)

**Docker Adapter**:
- `backend/app/services/adapters/docker_adapter.py`
- Methods:
  - `initialize()` - Build Docker image (docker-compose build)
  - `start()` - UP operation (docker-compose up -d), wait for health check
  - `stop()` - STOP operation (docker-compose stop)
  - `rebuild()` - DOWN + build + optional UP (docker-compose down, build, up)
  - `delete()` - DOWN operation with volumes (docker-compose down -v --remove-orphans)
  - `install_custom_packages()` - Install workspace dependencies (called by lifecycle manager)
  - `set_agent_prompts()` - Sync prompts via HTTP API
  - `set_credentials()` - Sync credentials via HTTP API
  - `get_container()` - Get Docker container object (for existence check)

**Base Adapter Interface**:
- `backend/app/services/adapters/base.py::EnvironmentAdapter.rebuild()`

**Suspension Scheduler**:
- `backend/app/services/environment_suspension_scheduler.py`
- Functions: `start_scheduler()`, `shutdown_scheduler()`, `run_suspension_check()`

**Event Service**:
- `backend/app/services/event_service.py`
- Methods: `is_user_online()`, `agent_usage_intent` event handler, `_activate_environment_sync()`

**Frontend Components**:
- Environment Card: `frontend/src/components/Environments/EnvironmentCard.tsx`
  - Actions: Rebuild button, Suspend button (for active environments)
- Session UI: `frontend/src/routes/_layout/session/$sessionId.tsx`
  - Features: Activation status UI, WebSocket event listeners, environment query
- Event Service: `frontend/src/services/eventService.ts`
  - Methods: `sendAgentUsageIntent()`, event subscriptions

## Benefits

1. **Zero Data Loss**: Workspace preserved across system updates and suspension/activation cycles
2. **Fast Updates**: Rebuild only updates core, uses Docker cache
3. **Version Control**: Core code tracks template version
4. **Isolation**: Each environment has independent workspace
5. **Flexibility**: Can rebuild multiple times without risk
6. **Development**: Core updates tested without recreating environments
7. **Knowledge Distribution**: Integration knowledge base updates distributed to all environments via rebuild, while preserving user customizations
8. **Dependency Management**: Two-layer system allows template dependency updates via rebuild while preserving agent-installed workspace dependencies
9. **Resource Efficiency**: Automatic suspension saves CPU/memory for inactive environments
10. **Seamless Reactivation**: Fast activation (< 10 seconds) with real-time WebSocket feedback
11. **Cost Optimization**: Reduces infrastructure costs by suspending idle resources
12. **Smart Scheduling**: Intelligent suspension logic prevents disrupting active users
13. **Optimized Container Lifecycle**: Suspended containers skip package installation on reactivation (only sync dynamic data)
14. **Clear Docker Operations**: Uses standard UP/STOP/DOWN terminology for predictable, maintainable behavior
15. **Intelligent Setup**: Automatically detects new vs existing containers and applies appropriate setup steps
16. **Separation of Concerns**: Dynamic data (prompts, credentials) synced separately from container setup (packages)
