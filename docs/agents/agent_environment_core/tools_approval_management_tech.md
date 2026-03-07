# Tools Approval Management - Technical Details

## File Locations

### Backend

| File | Purpose |
|------|---------|
| `backend/app/models/agent.py` | `agent_sdk_config` JSON field, `AgentSdkConfig` Pydantic schema (`sdk_tools`, `allowed_tools`) |
| `backend/app/services/agent_service.py` | `get_sdk_config()`, `add_allowed_tools()`, `get_pending_tools()`, `update_sdk_tools()`, `sync_allowed_tools_to_environment()` |
| `backend/app/services/llm_plugin_service.py` | `prepare_plugins_for_environment()` — accepts optional `allowed_tools`, includes in returned `settings_json` |
| `backend/app/services/environment_lifecycle.py` | `_sync_plugins_to_environment()` — reads agent's `allowed_tools`, passes to plugin service |
| `backend/app/api/routes/agents.py` | Tool management endpoints: `sdk-config`, `allowed-tools`, `pending-tools` |
| `backend/app/api/routes/messages.py` | Filters `tools_needing_approval` against current `allowed_tools` on message fetch (response-only) |
| `backend/app/services/message_service.py` | Stores `tools_needing_approval` in message metadata during streaming |

### Agent-Env (inside Docker container)

**Base path**: `backend/app/env-templates/app_core_base/core/server/`

| File | Purpose |
|------|---------|
| `agent_env_service.py` | `get_plugins_settings()` — full `settings.json` content; `get_allowed_tools()` — returns `allowed_tools` array |
| `sdk_manager.py` | `send_message_stream()` — defines pre-allowed list, merges with `get_allowed_tools()`, passes to `ClaudeAgentOptions` |

### Frontend

| File | Purpose |
|------|---------|
| `frontend/src/hooks/useToolApproval.ts` | Approval state management; reads `tools_needing_approval` from message metadata; calls `PATCH /agents/{id}/allowed-tools` |
| `frontend/src/components/Chat/MessageBubble.tsx` | Detects tool messages requiring approval, renders action |
| `frontend/src/components/Chat/MessageActions.tsx` | "Approve Tools" button with mutation loading state |

## Database Schema

**Table**: `agent` — `backend/app/models/agent.py`

`agent_sdk_config` is a JSON column storing an `AgentSdkConfig` object:

| Key | Type | Purpose |
|-----|------|---------|
| `sdk_tools` | `list[str]` | All tools discovered from agent environments (tracking/display) |
| `allowed_tools` | `list[str]` | Tools approved by user for automatic SDK permission |

## API Endpoints

**Route file**: `backend/app/api/routes/agents.py`

| Endpoint | Method | Request Body | Response | Purpose |
|----------|--------|--------------|----------|---------|
| `/agents/{id}/sdk-config` | GET | — | `AgentSdkConfig` | Returns current discovered and approved tools |
| `/agents/{id}/allowed-tools` | PATCH | `AllowedToolsUpdate { tools: list[str] }` | `AgentSdkConfig` | Adds to allowed list, syncs to active environment |
| `/agents/{id}/pending-tools` | GET | — | `PendingToolsResponse { pending_tools: list[str] }` | Returns `sdk_tools` minus `allowed_tools` |

## Services & Key Methods

**Agent Service**: `backend/app/services/agent_service.py`
- `get_sdk_config(agent_id)` — Returns `AgentSdkConfig`
- `add_allowed_tools(agent_id, tools)` — Appends to `allowed_tools` (deduplicates)
- `get_pending_tools(agent_id)` — Returns `sdk_tools - allowed_tools`
- `update_sdk_tools(agent_id, tools)` — Incrementally adds newly discovered tools to `sdk_tools`
- `sync_allowed_tools_to_environment(agent_id)` — Triggers plugin sync with updated `allowed_tools`

**Plugin Service**: `backend/app/services/llm_plugin_service.py`
- `prepare_plugins_for_environment(agent, allowed_tools=None)` — Builds `settings_json` dict; includes `allowed_tools` key when provided

**Environment Lifecycle**: `backend/app/services/environment_lifecycle.py`
- `_sync_plugins_to_environment(agent)` — Reads `agent.agent_sdk_config.allowed_tools`; passes to `prepare_plugins_for_environment()`

**Agent Env Service** (container): `backend/app/env-templates/app_core_base/core/server/agent_env_service.py`
- `get_plugins_settings()` — Reads `/app/workspace/plugins/settings.json` and returns full content
- `get_allowed_tools()` — Returns `settings["allowed_tools"]` list

**SDK Manager** (container): `backend/app/env-templates/app_core_base/core/server/sdk_manager.py`
- `send_message_stream()` — Defines hardcoded pre-allowed list; calls `get_allowed_tools()`; merges both into `ClaudeAgentOptions(allowed_tools=...)`

## Workspace Structure

**File**: `/app/workspace/plugins/settings.json` (inside container)

Contains both `active_plugins` and `allowed_tools` keys. Example structure:
- `active_plugins` — list of enabled plugin configurations
- `allowed_tools` — list of approved tool names (e.g., `mcp__plugin_context7__resolve-library-id`)

## Security

- Approval mutations are scoped to the agent owner — standard `CurrentUser` dependency injection enforces ownership
- Pre-allowed tools list is hardcoded in container server code (`:ro` mount) — cannot be modified by users or agents
- `sdk_tools` is populated from agent-env discovery only (tracking); `allowed_tools` is modified by user API calls only
- Message-level filtering (`tools_needing_approval` vs `allowed_tools`) is response-only — no DB writes during message fetch, preventing race conditions
