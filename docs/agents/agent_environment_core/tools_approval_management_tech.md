# Tools Approval Management - Technical Details

## File Locations

### Backend

| File | Purpose |
|------|---------|
| `backend/app/models/agents/agent.py` | `agent_sdk_config` JSON field, `AgentSdkConfig` Pydantic schema (`sdk_tools`, `allowed_tools`) |
| `backend/app/services/agents/agent_service.py` | `get_sdk_config()`, `add_allowed_tools()`, `get_pending_tools()`, `update_sdk_tools()`, `sync_allowed_tools_to_environment()` |
| `backend/app/services/plugins/llm_plugin_service.py` | `prepare_plugins_for_environment()` — accepts optional `allowed_tools`, includes in returned `settings_json` |
| `backend/app/services/environments/environment_lifecycle.py` | `_sync_plugins_to_environment()` — reads agent's `allowed_tools`, passes to plugin service |
| `backend/app/api/routes/agents.py` | Tool management endpoints: `sdk-config`, `allowed-tools`, `pending-tools` |
| `backend/app/api/routes/messages.py` | Filters `tools_needing_approval` against current `allowed_tools` on message fetch (response-only) |
| `backend/app/services/sessions/message_service.py` | Stores `tools_needing_approval` in message metadata during streaming |

### Agent-Env (inside Docker container)

**Base path**: `backend/app/env-templates/app_core_base/core/server/`

| File | Purpose |
|------|---------|
| `agent_env_service.py` | `get_plugins_settings()` — full `settings.json` content; `get_allowed_tools()` — returns `allowed_tools` array |
| `sdk_manager.py` | `send_message_stream()` — defines pre-allowed list, merges with `get_allowed_tools()`, passes to `ClaudeAgentOptions` |
| `adapters/tool_name_registry.py` | Single source of truth for tool naming: `CLAUDE_CODE_TOOL_NAME_MAP`, `OPENCODE_MCP_TOOL_NAME_MAP`, `PRE_APPROVED_TOOLS`, `normalize_tool_name()` |

### Frontend

| File | Purpose |
|------|---------|
| `frontend/src/hooks/useToolApproval.ts` | Approval state management; reads `tools_needing_approval` from message metadata; calls `PATCH /agents/{id}/allowed-tools` |
| `frontend/src/components/Chat/MessageBubble.tsx` | Detects tool messages requiring approval, renders action |
| `frontend/src/components/Chat/MessageActions.tsx` | "Approve Tools" button with mutation loading state |

## Database Schema

**Table**: `agent` — `backend/app/models/agents/agent.py`

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

**Agent Service**: `backend/app/services/agents/agent_service.py`
- `get_sdk_config(agent_id)` — Returns `AgentSdkConfig`
- `add_allowed_tools(agent_id, tools)` — Appends to `allowed_tools` (deduplicates)
- `get_pending_tools(agent_id)` — Returns `sdk_tools - allowed_tools`
- `update_sdk_tools(agent_id, tools)` — Incrementally adds newly discovered tools to `sdk_tools`
- `sync_allowed_tools_to_environment(agent_id)` — Triggers plugin sync with updated `allowed_tools`

**Plugin Service**: `backend/app/services/plugins/llm_plugin_service.py`
- `prepare_plugins_for_environment(agent, allowed_tools=None)` — Builds `settings_json` dict; includes `allowed_tools` key when provided

**Environment Lifecycle**: `backend/app/services/environments/environment_lifecycle.py`
- `_sync_plugins_to_environment(agent)` — Reads `agent.agent_sdk_config.allowed_tools`; passes to `prepare_plugins_for_environment()`

**Agent Env Service** (container): `backend/app/env-templates/app_core_base/core/server/agent_env_service.py`
- `get_plugins_settings()` — Reads `/app/workspace/plugins/settings.json` and returns full content
- `get_allowed_tools()` — Returns `settings["allowed_tools"]` list

**SDK Manager** (container): `backend/app/env-templates/app_core_base/core/server/sdk_manager.py`
- `send_message_stream()` — Defines hardcoded pre-allowed list; calls `get_allowed_tools()`; merges both into `ClaudeAgentOptions(allowed_tools=...)`

**Tool Name Registry** (container): `backend/app/env-templates/app_core_base/core/server/adapters/tool_name_registry.py`
- `CLAUDE_CODE_TOOL_NAME_MAP` — PascalCase → lowercase mapping for all Claude Code built-in tools
- `OPENCODE_MCP_TOOL_NAME_MAP` — reserved for future MCP tool name remapping (currently empty)
- `PRE_APPROVED_TOOLS` — canonical frozenset of all pre-approved tool names (unified lowercase)
- `normalize_tool_name(name, sdk)` — normalizes any tool name to the unified lowercase convention; used by all adapters when emitting `tools_init` and `TOOL_USE` events

## Tool Naming Convention

All tool names throughout the system use **unified lowercase**:
- Claude Code natively emits PascalCase names (`Read`, `Bash`, `WebFetch`). The `ClaudeCodeAdapter` normalizes these to lowercase (`read`, `bash`, `webfetch`) before emitting `SDKEvent` objects.
- OpenCode natively emits lowercase names — no normalization needed for built-ins.
- Both Claude Code and OpenCode register agent task tools on the `agent_task` MCP server, producing unified `mcp__agent_task__*` names.
- Google ADK tools are defined as lowercase Python functions (`bash`, `read`).

The backend `message_service.py` maintains a `PRE_ALLOWED_TOOLS` set that mirrors `tool_name_registry.PRE_APPROVED_TOOLS`. Tool comparison uses `.lower()` on both sides for backward compatibility with legacy PascalCase data that may exist in the database from before this convention was established.

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
