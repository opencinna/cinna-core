# Agent Handover — Technical Reference

## File Locations

### Backend

**Models:**
- `backend/app/models/agents/agent_handover.py` — `AgentHandoverConfig` (table), request/response schemas
- `backend/app/models/__init__.py` — model exports
- `backend/app/models/agents/agent.py` — `Agent.handover_configs` relationship

**API Routes:**
- `backend/app/api/routes/agents.py` — handover and task-creation endpoints (thin controllers)

**Services:**
- `backend/app/services/agents/agent_handover_service.py` — `AgentHandoverService` with handover config CRUD, access verification, prompt generation; exception hierarchy (`HandoverError`, `HandoverNotFoundError`, `AgentNotFoundError`, `PermissionDeniedError`)
- `backend/app/services/agents/agent_service.py` — `sync_agent_handover_config()`, `create_agent_task()`
- `backend/app/services/tasks/input_task_service.py` — `create_task()`, `create_task_with_auto_refine()`, `execute_task()`, `link_session()`
- `backend/app/services/sessions/session_service.py` — `create_session()`, `send_session_message()`
- `backend/app/services/sessions/message_service.py` — system message creation with task metadata
- `backend/app/services/ai_functions/ai_functions_service.py` — `generate_handover_prompt()`, `refine_task()`
- `backend/app/services/environments/environment_lifecycle.py` — `_sync_dynamic_data()` (syncs handover config on env activation)
- `backend/app/services/environments/adapters/base.py` — `set_agent_handover_config()` abstract method
- `backend/app/services/environments/adapters/docker_adapter.py` — `set_agent_handover_config()` implementation

**AI Functions:**
- `backend/app/agents/handover_generator.py` — handover prompt generation agent
- `backend/app/agents/prompts/handover_generator_prompt.md` — generation prompt template

**Database Migration:**
- `backend/app/alembic/versions/b26f2c36507c_add_agent_handover_config_table.py`

### Agent-Env

**Configuration Endpoints:**
- `backend/app/env-templates/app_core_base/core/server/routes.py` — `GET /config/agent-handovers`, `POST /config/agent-handovers`
- `backend/app/env-templates/app_core_base/core/server/models.py` — `ChatRequest` (includes `backend_session_id`)
- `backend/app/env-templates/app_core_base/core/server/agent_env_service.py` — `get_agent_handover_config()`, `update_agent_handover_config()`

**Runtime:**
- `backend/app/env-templates/app_core_base/core/server/adapters/claude_code_sdk_adapter.py` — tool registration (conversation mode only), global session state with async lock, helper functions
- `backend/app/env-templates/app_core_base/core/server/prompt_generator.py` — `_load_handover_prompt()` reads the `handover_prompt` field from `{workspace}/docs/agent_handover_config.json`; appended to the conversation mode system prompt by `generate_conversation_mode_prompt()` after the task context section
- `backend/app/env-templates/app_core_base/core/server/tools/agent_task_create_task.py` — create_agent_task tool implementation (split from the former single-file tool)

**Workspace Storage:**
- `{workspace}/docs/agent_handover_config.json` — runtime config: array of handovers (id, name, prompt) + consolidated `handover_prompt`

### Frontend

**Components:**
- `frontend/src/components/Agents/AgentHandovers.tsx` — handover configuration UI (add, generate, edit, toggle, delete)
- `frontend/src/components/Agents/AgentConfigTab.tsx` — integration point (renders `AgentHandovers`)
- `frontend/src/components/Chat/MessageBubble.tsx` — renders task creation system messages with session/task links

**Generated Client:**
- `frontend/src/client/sdk.gen.ts` — `AgentsService` methods for handover CRUD + task creation
- `frontend/src/client/types.gen.ts` — TypeScript types for request/response models

## Database Schema

**Table:** `agent_handover_config`

| Field | Type | Notes |
|-------|------|-------|
| `id` | UUID | Primary key |
| `source_agent_id` | UUID FK | Agent that performs the handover; cascade delete |
| `target_agent_id` | UUID FK | Agent that receives the handover |
| `handover_prompt` | Text | 2-3 sentence trigger/context/format instructions |
| `enabled` | Boolean | Whether this handover is active |
| `created_at` | Timestamp | |
| `updated_at` | Timestamp | Updated on each prompt or enabled change |

**Relationships:**
- Many `AgentHandoverConfig` → one source `Agent`
- Many `AgentHandoverConfig` → one target `Agent`
- Cascade delete on source agent deletion

## API Endpoints

All routes are in `backend/app/api/routes/agents.py`:

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/v1/agents/{id}/handovers` | List handover configs for source agent (includes target agent names) |
| `POST` | `/api/v1/agents/{id}/handovers` | Create handover config (`target_agent_id`, `handover_prompt`); validates ownership, no self-handover, no duplicates |
| `PUT` | `/api/v1/agents/{id}/handovers/{handover_id}` | Update prompt or `enabled` flag |
| `DELETE` | `/api/v1/agents/{id}/handovers/{handover_id}` | Remove config permanently |
| `POST` | `/api/v1/agents/{id}/handovers/generate` | AI-generate draft handover prompt (`target_agent_id` body) |
| `POST` | `/api/v1/agents/tasks/create` | Primary task creation endpoint (called by agent-env tool) |
| `POST` | `/api/v1/agents/handover/execute` | Deprecated alias for tasks/create |

## Services & Key Methods

### AgentHandoverService (`backend/app/services/agents/agent_handover_service.py`)

- `verify_agent_access(agent_id, user_id)` — checks agent exists and user has ownership; raises domain exceptions
- `list_configs(agent_id, user_id)` — lists all handover configs for source agent with target agent names resolved
- `create_config(agent_id, user_id, data)` — creates handover config; validates target agent access, prevents self-handover, syncs to agent-env
- `update_config(agent_id, handover_id, user_id, data)` — updates prompt/enabled fields, syncs to agent-env
- `delete_config(agent_id, handover_id, user_id)` — deletes config permanently, syncs to agent-env
- `generate_handover_prompt(agent_id, target_agent_id, user_id)` — validates both agents, delegates to `AIFunctionsService`

### AgentService (`backend/app/services/agents/agent_service.py`)

- `sync_agent_handover_config(agent_id)` — queries all enabled handover configs, formats JSON with targets and consolidated prompt, pushes to agent-env via adapter
- `create_agent_task(task_message, source_session_id, target_agent_id?, target_agent_name?)` — orchestrates direct handover or inbox task creation; delegates to `InputTaskService`
- `delete_agent(agent_id)` — handles clone relationship cleanup (detaches clones from parent, updates share records for deleted clones) before deleting environments and agent

### InputTaskService (`backend/app/services/tasks/input_task_service.py`)

- `create_task(...)` — creates `InputTask` with `agent_initiated=true`, `auto_execute=false` (inbox task)
- `create_task_with_auto_refine(...)` — creates `InputTask` with `auto_execute=true`; if target has `refiner_prompt`, calls `AIFunctionsService.refine_task()` before returning message
- `execute_task(task, message)` — creates session via `SessionService`, links session to task, sends message
- `link_session(task_id, session_id)` — updates task with `session_id` reference

### AIFunctionsService (`backend/app/services/ai_functions/ai_functions_service.py`)

- `generate_handover_prompt(source_agent, target_agent)` — invokes handover generator agent to produce draft prompt
- `refine_task(task_message, refiner_prompt)` — refines handover message for direct handover mode

### Environment Lifecycle (`backend/app/services/environments/environment_lifecycle.py`)

- `_sync_dynamic_data()` — called on every environment start/activation; re-syncs handover config from DB, ensuring clones receive empty config rather than stale parent workspace data

### DockerAdapter (`backend/app/services/environments/adapters/docker_adapter.py`)

- `set_agent_handover_config(env, config)` — calls `POST /config/agent-handovers` on the agent-env container

## Frontend Components

### AgentHandovers.tsx (`frontend/src/components/Agents/AgentHandovers.tsx`)

**Local state:**
- `editingPrompts` — map of handover ID → current textarea value (unsaved)
- `dirtyPrompts` — set of handover IDs with unsaved changes
- `selectedTargetAgent` — currently selected target in add dropdown
- `isAddingHandover` — controls add-handover form visibility

**Server state (TanStack Query):**
- `agentHandovers` query — list of configs for the current agent
- `agents` query — all agents for dropdown population
- Mutations: create, update (prompt/enabled), delete, generate prompt

**Renders:** agent selector dropdown, handover cards with generate button, prompt textarea, apply/enable/delete controls

### MessageBubble.tsx (`frontend/src/components/Chat/MessageBubble.tsx`)

- Detects `message_metadata.task_created === true` to render task creation notifications with blue styling
- **Direct handover**: renders **View session** link using `message_metadata.session_id`
- **Inbox task**: renders **View task** link using `message_metadata.task_id` (when `inbox_task=true`)

## Configuration Sync Flow

```
CRUD operation (create/update/delete/enable-toggle)
    │
    ↓
sync_agent_handover_config() in AgentService
    │
    ↓
DockerAdapter.set_agent_handover_config()
    │
    ↓
POST /config/agent-handovers on agent-env container
    │
    ↓
agent_env_service.update_agent_handover_config()
    → writes {workspace}/docs/agent_handover_config.json
```

Also synced automatically by `_sync_dynamic_data()` on each environment start.

## System Message Metadata

Logged to source session after task creation:

**Direct Handover:**
```
task_created: true
task_id: <uuid>
session_id: <uuid>
target_agent_id: <uuid>
target_agent_name: <string>
```

**Inbox Task:**
```
task_created: true
task_id: <uuid>
inbox_task: true
```

## Security

- Access control: user must own both source and target agents (enforced in `AgentHandoverService` via `verify_agent_access()`)
- No self-handover enforced at API level
- No duplicate targets enforced at API level
- Cascade delete ensures no orphaned handover configs
- Clone isolation: handover configs excluded from workspace file syncs and push updates

