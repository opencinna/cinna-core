# Agent Environments

## Purpose

Provide isolated Docker container runtimes where AI agents execute tasks, with a two-layer architecture that separates system code from user data so system updates never destroy user work.

## Core Concepts

### Two-Layer Architecture

Each agent environment is a Docker container with two distinct layers:

- **Core Layer** (`/app/core/`) - System code (FastAPI server, SDK adapters, API routes). Shared across all environment templates via `app_core_base`. Mounted read-only into every container at runtime — not baked into the image. Immutable at runtime. Updated only via rebuild
- **Workspace Layer** (`/app/workspace/`) - User-generated content (scripts, files, credentials, databases, logs, knowledge base). Mounted as a Docker volume. Persists across all lifecycle operations (restarts, rebuilds, suspensions)
- **Template Files** (`/app/`) - Static configuration files copied during initialization (e.g., `BUILDING_AGENT.md`). Not updated during rebuilds

### Why Separation Matters

**Problem**: Updating system code (bug fixes, new features) previously required destroying and recreating environments, losing all user work.

**Solution**: System updates only affect the core layer; workspace remains untouched. Users keep their scripts, files, credentials, and databases through any number of system updates.

### Environment Modes

- **Building Mode** - Agent uses Claude Sonnet for development tasks: creating scripts, configuring integrations, updating documentation
- **Conversation Mode** - Agent uses Claude Haiku for executing pre-built workflows: processing tasks, generating reports, interacting with APIs

### Multi-Image Templates

The platform supports multiple Docker base images for different agent use cases. The template is selected when creating an environment and determines the base OS, pre-installed tooling, and image size. See [Multi-Image Environments](./agent_multi_image_environments.md) for details on available templates and selection guidance.

### Three-Layer Dependencies

- **Template dependencies** (`pyproject.toml`) - System-level Python packages baked into the Docker image. Updated via rebuild
- **Workspace Python dependencies** (`workspace_requirements.txt`) - Integration-specific Python packages installed by the agent. Persist across rebuilds and auto-install on new container startup
- **Workspace system packages** (`workspace_system_packages.txt`) - OS-level packages (e.g., ffmpeg, imagemagick) installed via `apt-get`. Persist across rebuilds and auto-install on new container startup. Available in both templates but most useful with the `general-env` template which has the full Debian package ecosystem

## User Stories / Flows

### 1. Environment Creation

1. User creates a new agent
2. System copies the environment template to an instance directory (excludes `Dockerfile`, `pyproject.toml`, `uv.lock` — those stay in the template directory)
3. Shared template image built (or reused from cache) by `TemplateImageService`; tag injected into generated compose file
4. Instance-specific configuration files generated (docker-compose.yml, .env, auth tokens)
5. Environment is in `stopped` state, ready to start

### 2. Environment Start

1. User opens a session or sends a message
2. System checks if a Docker container already exists
3. Configuration files regenerated (fresh auth token, AI credentials resolved)
4. Docker container started (`docker-compose up`)
5. If **new container**: Install workspace dependencies from `workspace_requirements.txt` and system packages from `workspace_system_packages.txt`
6. **Always**: Sync dynamic data (agent prompts to `workspace/docs/`, credentials)
7. Environment status set to `running`
8. `ENVIRONMENT_ACTIVATED` event emitted to process any pending sessions

### 3. Environment Rebuild

1. User triggers rebuild (or admin pushes system update)
2. If running, container stopped first
3. Old container removed completely (`docker-compose down`)
4. Shared template image rebuilt (or reused from cache) by `TemplateImageService`; tag injected into regenerated compose file
5. Compose template overwritten from template dir; old core directory deleted, fresh core copied from shared `app_core_base`
6. Knowledge base files synced from template (add/update only, no deletions)
7. If was running: new container started with full setup (packages + dynamic data)
8. Environment returns to previous state (running or stopped)

### 4. Environment Suspension (Automatic)

1. Scheduler runs every 10 minutes, checks all `running` environments
2. For each environment, checks against agent's configured inactivity threshold
3. If inactive beyond threshold AND (user is offline OR environment is not the active one):
   - Container stopped but NOT removed (`docker-compose stop`)
   - Status set to `suspended`
   - `ENVIRONMENT_SUSPENDED` WebSocket event sent to user

### 5. Environment Activation (From Suspended)

1. User opens a session or sends a message to a suspended environment
2. `ENVIRONMENT_ACTIVATING` event emitted, status set to `activating`
3. Configuration files regenerated (fresh auth token)
4. Existing stopped container started (`docker-compose up`) - no rebuild
5. **Skip** package installation (container already has them)
6. **Only sync** dynamic data (prompts and credentials)
7. Status set to `running`, `ENVIRONMENT_ACTIVATED` event emitted
8. Typical activation time: < 10 seconds

### 6. Manual Suspension

1. User clicks "Suspend" button on environment card
2. Confirmation dialog shown
3. Same process as automatic suspension

### 7. Environment Health Monitoring (Automatic)

1. Scheduler runs every 10 minutes, checks all environments with `status == "running"`
2. For each environment, calls health check endpoint on the Docker container
3. If health check returns unhealthy, performs a second check on container status to guard against transient issues
4. If container is confirmed down (not just slow):
   - Status set to `error`
   - `status_message` set to describe the failure
   - `ENVIRONMENT_STATUS_CHANGED` WebSocket event sent to owner
5. `last_health_check` timestamp updated for every checked environment regardless of outcome

## Business Rules

### Environment Status Lifecycle

```
creating → building → stopped ──→ starting → running
                                                │
                                    running ←── ┤
                                                │
                        rebuild ← running/stopped
                        rebuilding → running/stopped
                                                │
                               running → suspended
                          suspended → activating → running
                                                │
                                     Any → error
                                     Any → deprecated
```

**Status values**: `stopped`, `creating`, `building`, `initializing`, `starting`, `running`, `rebuilding`, `suspended`, `activating`, `error`, `deprecated`

### Inactivity Period Configuration

Each agent has a configurable inactivity threshold (agent-level setting, not per-environment):

| Setting | Behavior |
|---------|----------|
| `None` (default) | Suspend after 10 minutes of inactivity |
| `"2_days"` | Suspend after 2 days |
| `"1_week"` | Suspend after 1 week |
| `"1_month"` | Suspend after 30 days |
| `"always_on"` | Never auto-suspend |

### Suspension Criteria (ALL must be true)

1. Environment status is `running`
2. Agent's `inactivity_period_limit` is not `"always_on"`
3. Last activity exceeds the agent's configured threshold
4. EITHER: user is offline (no active WebSocket) OR environment is not the active one for its agent

### Activity Tracking

`last_activity_at` timestamp updated when:
- User sends a message to the environment
- User opens a session with the environment
- User sends `agent_usage_intent` WebSocket event (opens session in UI)

### Data Preservation Rules

**Always preserved** (across all operations including rebuild):
- All workspace data (scripts, files, docs, credentials, databases, logs)
- User-created knowledge files (not in template)
- Workspace dependencies (`workspace_requirements.txt`)
- System packages (`workspace_system_packages.txt`)
- Docker volumes
- Environment configuration and agent prompts

**Updated during rebuild**:
- Docker compose template (overwritten from template dir)
- Core server code (all modules, replaced from `app_core_base`)
- Template image (rebuilt by `TemplateImageService` if build inputs changed)
- Knowledge base files from template (add/update only, never delete)

### Docker Operations Mapping

| Operation | Docker Command | Container Effect |
|-----------|---------------|-----------------|
| **Start/Activate** (UP) | `docker-compose up` | Creates new or starts existing container |
| **Stop/Suspend** (STOP) | `docker-compose stop` | Stops container, keeps it intact |
| **Rebuild** (DOWN → UP) | `docker-compose down` then `up` | Removes container completely, new one created; image built/reused by `TemplateImageService` before compose runs |
| **Delete** (DOWN -v) | `docker-compose down -v` | Removes container and volumes |

### Container Setup Logic

- **New container** (first start or after rebuild): Install workspace Python packages + system packages + sync dynamic data
- **Existing container** (restart or activation from suspended): Only sync dynamic data (prompts, credentials)
- This optimization makes restarts and activations significantly faster

### AI Credential Resolution

1. If `conversation_ai_credential_id` or `building_ai_credential_id` set on environment → use those credentials only (supports owned and shared credentials via `AICredentialShare`)
2. If no credentials assigned → fall back to user's default profile credentials
3. Anthropic credential type auto-detected by prefix: `sk-ant-api*` → API key, `sk-ant-oat*` → OAuth token

## Architecture Overview

```
User → Frontend → Backend API → Environment Lifecycle Manager → Docker Adapter → Docker Container
                                        │                                            │
                                        ├── Configuration Generator                  ├── /app/core/ (system code, read-only)
                                        ├── Credential Resolver                      ├── /app/workspace/ (user data, read-write)
                                        ├── Suspension Scheduler                     └── FastAPI Server → SDK Adapters → AI Provider
                                        └── Status Scheduler (health checks)
```

## Integration Points

- **[Agent Sessions](../../application/agent_sessions/agent_sessions.md)** - Sessions connect users to environments; environment must be running for message streaming; `ENVIRONMENT_ACTIVATED` event triggers processing of pending sessions
- **[Agent Prompts](../agent_prompts/agent_prompts.md)** - Prompts synced to `workspace/docs/` on every start; building mode reads comprehensive prompt set, conversation mode reads workflow prompt
- **[Session Recovery](../agent_commands/session_recovery_command.md)** - SDK session IDs stored for resumption after container restarts; recovery handles lost connections after rebuilds
- **[Multi SDK](../agent_environment_core/multi_sdk.md)** - Environment adapter configuration determines which SDK (Claude, OpenAI, etc.) is used per mode
- **[Agent Environment Data Management](../agent_environment_data_management/agent_environment_data_management.md)** - Workspace data cloning, syncing, and file transfer operations between environments
- **[Credential Management](../agent_credentials/agent_credentials.md)** - Credentials synced to `workspace/credentials/` on environment start
- **[AI Credentials](../../application/ai_credentials/ai_credentials.md)** - AI provider keys resolved and injected as environment variables
- **[Affected Environments Rebuild](./affected_environments_rebuild.md)** - Credential-triggered rebuild flow: how environments are detected and rebuilt after AI credential changes
- **[File Upload](../agent_file_management/agent_file_management.md)** - Files uploaded to backend transferred into workspace via Docker adapter
- **[Event Bus](../../application/realtime_events/event_bus_system.md)** - WebSocket events for environment status changes (activating, activated, suspended, error)
- **[Knowledge Management](../../application/knowledge_sources/knowledge_sources.md)** - Knowledge base files in `workspace/knowledge/` synced from template during rebuild
- **[Multi-Image Environments](./agent_multi_image_environments.md)** - Template selection guidance: `python-env-advanced` (lightweight) vs `general-env` (full Debian with system package support)

