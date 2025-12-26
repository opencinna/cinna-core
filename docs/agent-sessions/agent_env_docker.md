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
│   │   └── server/                    # API server code
│   │       ├── routes.py              # HTTP endpoints
│   │       ├── sdk_manager.py         # Claude Code SDK integration
│   │       └── models.py              # Pydantic models
│   │
│   ├── workspace/                     # User data (volume mounted)
│   │   ├── scripts/                   # Agent-created Python scripts
│   │   ├── files/                     # Uploaded/generated files
│   │   ├── docs/                      # WORKFLOW_PROMPT.md, ENTRYPOINT_PROMPT.md
│   │   ├── credentials/               # API keys, tokens (encrypted)
│   │   ├── databases/                 # SQLite/other local DBs
│   │   └── logs/                      # Execution logs
│   │
│   ├── BUILDING_AGENT_EXAMPLE.md      # Template for building mode prompt
│   ├── AGENT_EXAMPLE.md               # Template for conversation mode prompt
│   └── ENTRYPOINT_EXAMPLE.md          # Template for trigger message
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

## Operations

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

**Process**:
1. Run `docker-compose up -d`
2. Mount volumes:
   - `{instance_dir}/app/core:/app/core` (read-only in practice)
   - `{instance_dir}/app/workspace:/app/workspace` (read-write)
3. Wait for health check at `/health` endpoint
4. Sync prompts to `workspace/docs/` via `DockerEnvironmentAdapter.set_agent_prompts()`
5. Update database status to `running`

**Note**: Core files are both baked into image AND volume-mounted for easier development. Production could use image-only.

### Environment Rebuild

**Entry Point**: `backend/app/services/environment_lifecycle.py::rebuild_environment()`

**Purpose**: Update core system files from template while preserving workspace data

**Process**:
1. Check if environment is running → stop if needed
2. Delete old core directory: `{instance_dir}/app/core`
3. Copy fresh core from template: `{template_dir}/app/core` → `{instance_dir}/app/core`
4. Rebuild Docker image via `DockerEnvironmentAdapter.rebuild()`
   - Runs `docker-compose build` (uses cache for speed)
   - New core files baked into image
5. Restart container if it was running before
6. Update status to `running` or `stopped`

**Preserved**:
- All workspace data (scripts, files, docs, credentials, databases, logs)
- Docker volumes
- Environment configuration
- Agent prompts

**Updated**:
- Core server code
- API routes
- SDK manager logic
- Docker image layers

## Docker Configuration

### Dockerfile

**Location**: `backend/app/env-templates/python-env-advanced/Dockerfile`

**Key Steps**:
- Install system dependencies (curl, git, Node.js for Claude Code)
- Install uv package manager
- Install Claude Code CLI globally
- Copy `pyproject.toml` and install Python dependencies
- **Copy `app/core` into image** (line 50)
- Set CMD to run `fastapi run core/main.py`

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

## Database Schema

**Status Values**: `stopped`, `creating`, `building`, `initializing`, `starting`, `running`, `rebuilding`, `error`, `deprecated`

**Model**: `backend/app/models/environment.py::AgentEnvironment`

**Rebuild Status Flow**:
1. `running` or `stopped` → trigger rebuild
2. `rebuilding` → during rebuild operation
3. `running` (if was running) or `stopped` (if was stopped) → after rebuild

## Implementation References

**Environment Lifecycle Manager**:
- `backend/app/services/environment_lifecycle.py`
- Methods: `create_environment_instance()`, `start_environment()`, `stop_environment()`, `rebuild_environment()`

**Docker Adapter**:
- `backend/app/services/adapters/docker_adapter.py`
- Methods: `initialize()`, `start()`, `stop()`, `rebuild()`

**Base Adapter Interface**:
- `backend/app/services/adapters/base.py::EnvironmentAdapter.rebuild()`

**Frontend**:
- Component: `frontend/src/components/Environments/EnvironmentCard.tsx`
- Action: Rebuild button with confirmation dialog

## Benefits

1. **Zero Data Loss**: Workspace preserved across system updates
2. **Fast Updates**: Rebuild only updates core, uses Docker cache
3. **Version Control**: Core code tracks template version
4. **Isolation**: Each environment has independent workspace
5. **Flexibility**: Can rebuild multiple times without risk
6. **Development**: Core updates tested without recreating environments
