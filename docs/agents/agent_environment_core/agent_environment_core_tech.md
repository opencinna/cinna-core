# Agent Environment Core - Technical Details

## File Locations

### Core Server (inside Docker container)

**Base path**: `backend/app/env-templates/app_core_base/core/server/`

| File | Purpose |
|------|---------|
| `routes.py` | HTTP API endpoints, `_store_session_context()` helper, SSE streaming |
| `sdk_manager.py` | Multi-adapter SDK orchestrator, adapter selection and caching |
| `prompt_generator.py` | System prompt construction for building and conversation modes |
| `agent_env_service.py` | Business logic for workspace file operations (read/write prompts) |
| `active_session_manager.py` | Per-session context store with HMAC verification, TTL cleanup |
| `models.py` | Pydantic request/response models (`ChatRequest`, `ChatResponse`, `AgentPromptsResponse`, etc.) |
| `sdk_utils.py` | `SessionEventLogger` class (shared JSONL logger for all adapters), `format_message_for_debug`, deprecated `format_sdk_message` |

### SDK Adapters

**Base path**: `backend/app/env-templates/app_core_base/core/server/adapters/`

| File | Purpose |
|------|---------|
| `base.py` | `SDKEvent`, `SDKEventType`, `SDKConfig`, `BaseSDKAdapter`, `AdapterRegistry` |
| `claude_code_sdk_adapter.py` | `ClaudeCodeAdapter` - handles `claude-code/anthropic` and `claude-code/minimax` variants |
| `claude_code_event_transformer.py` | `ClaudeCodeEventTransformer` - translates raw Claude SDK messages to `SDKEvent` objects |
| `opencode_sdk_adapter.py` | `OpenCodeAdapter` - handles all `opencode/*` variants via HTTP client to `opencode serve` |
| `opencode_event_transformer.py` | `OpenCodeEventTransformer` - stateful translator from raw OpenCode SSE events to `SDKEvent` objects; uses shared `SessionEventLogger` for JSONL logging |
| `tool_name_registry.py` | Unified lowercase tool name convention: `CLAUDE_CODE_TOOL_NAME_MAP`, `OPENCODE_MCP_TOOL_NAME_MAP`, `PRE_APPROVED_TOOLS`, `normalize_tool_name()`, `normalize_tool_input()` |
| `sqlite_session_service.py` | SQLite-based session persistence for adapters |

### Custom Tools (SDK — Claude Code adapter)

**Base path**: `backend/app/env-templates/app_core_base/core/server/tools/`

| File | Tool Name | Purpose |
|------|-----------|---------|
| `knowledge_query.py` | `query_integration_knowledge` | RAG-based knowledge source query tool |
| `agent_task_add_comment.py` | `add_comment` | Post comment on task — see [Session State Tools](session_state_tools.md) |
| `agent_task_update_status.py` | `update_status` | Edge-case status update — see [Session State Tools](session_state_tools.md) |
| `agent_task_create_task.py` | `create_task` | Standalone task creation — see [Create Agent Task Tool](create_agent_task_tool.md) |
| `agent_task_create_subtask.py` | `create_subtask` | Team subtask delegation — see [Create Agent Task Tool](create_agent_task_tool.md) |
| `agent_task_get_details.py` | `get_details` | Full task detail with comments (capped at 3000 chars each) and subtask progress |
| `agent_task_list_tasks.py` | `list_tasks` | List tasks by scope (assigned/created/team) |

**Schema convention**: All SDK tools use explicit JSON Schema dicts (`{"type": "object", "properties": {...}, "required": [...]}`) rather than simple `{name: type}` dicts. This ensures the `claude_agent_sdk`'s `create_sdk_mcp_server()` correctly marks optional parameters as non-required in the generated MCP tool schema.

### MCP Bridge Servers (OpenCode adapter)

**Base path**: `backend/app/env-templates/app_core_base/core/server/tools/mcp_bridge/`

Lightweight stdio MCP servers that expose the same tools for OpenCode agents. Referenced in the generated `opencode.json` under the `mcp` key.

| File | Exposes |
|------|---------|
| `knowledge_server.py` | `query_integration_knowledge` tool |
| `task_server.py` | `add_comment`, `update_status`, `create_task`, `create_subtask`, `get_details`, `list_tasks` tools |

The MCP bridge tools use Python type annotations with defaults for optional params (e.g., `files: list[str] | None = None`). FastMCP generates correct JSON Schema from these signatures automatically.

### Scripts

| File | Purpose |
|------|---------|
| `backend/app/env-templates/app_core_base/core/scripts/get_session_context.py` | Stdlib-only helper for agent scripts to query session context via HTTP |

### Backend Integration (main backend)

| File | Purpose |
|------|---------|
| `backend/app/services/sessions/message_service.py` | `send_message_to_environment_stream()` - HTTP POST to `/chat/stream`, SSE parsing |
| `backend/app/services/sessions/session_service.py` | Session creation with `agent_sdk` parameter, external session ID storage |
| `backend/app/services/environments/environment_lifecycle.py` | `EnvironmentLifecycleManager` - create, rebuild, start; config regeneration |
| `backend/app/services/sessions/session_context_signer.py` | HMAC-SHA256 signing/verification for session context |
| `backend/app/api/routes/messages.py` | `POST /sessions/{session_id}/messages/stream` - routes messages to environment |
| `backend/app/api/routes/agents.py` | Prompt sync between Agent model and environment docs |
| `backend/app/models/sessions/session.py` | Session model with `agent_sdk`, `session_metadata` fields |

## Database Schema

No dedicated database tables - the environment core runs inside Docker containers and uses the filesystem for state. Session tracking relies on:

- `Session` model (`backend/app/models/sessions/session.py`) - stores `agent_sdk`, `session_metadata["external_session_id"]` for SDK session resumption
- `Agent` model (`backend/app/models/agents/agent.py`) - stores `workflow_prompt`, `entrypoint_prompt` synced from environment
- `AgentEnvironment` model (`backend/app/models/environments/environment.py`) - stores `model_override_conversation` and `model_override_building` (nullable strings); these are passed to adapters at runtime to override the default model selection

## API Endpoints

### Environment-side (inside container)

**Routes file**: `backend/app/env-templates/app_core_base/core/server/routes.py`

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/chat/stream` | POST | Streaming chat - SSE events (session_created, assistant, tool, done, error) |
| `/chat` | POST | Synchronous chat - complete response |
| `/config/agent-prompts` | GET | Read current prompts from workspace docs |
| `/config/agent-prompts` | POST | Write prompts to workspace docs |
| `/health` | GET | Health check with status, timestamp, uptime |
| `/session/context` | GET | Session context metadata (HMAC-verified, per-session or legacy) |
| `/sdk/sessions` | GET | List SDK sessions (debugging) |
| `/sdk/sessions/{session_id}` | DELETE | Close SDK session |

### Backend-side (routes that call environment)

| Endpoint | File |
|----------|------|
| `POST /api/v1/sessions/{session_id}/messages/stream` | `backend/app/api/routes/messages.py` |
| `GET/POST /api/v1/agents/{agent_id}/prompts` | `backend/app/api/routes/agents.py` |

## Services & Key Methods

### SDK Manager

**File**: `backend/app/env-templates/app_core_base/core/server/sdk_manager.py`

- `SDKManager.send_message_stream()` - Main entry point. Selects adapter, generates prompt, streams SDKEvents as dicts
- `SDKManager._get_adapter(mode)` - Reads `SDK_ADAPTER_{MODE}` env var, creates/caches adapter via AdapterRegistry

### Prompt Generator

**File**: `backend/app/env-templates/app_core_base/core/server/prompt_generator.py`

- `PromptGenerator.generate_prompt(mode, session_state)` - Factory method routing to mode-specific generators
- `PromptGenerator.generate_building_mode_prompt(session_context)` - Returns `SystemPromptPreset` dict (Claude Code preset + all docs)
- `PromptGenerator.generate_conversation_mode_prompt(session_context)` - Returns plain string (workflow prompt + scripts + context)
- `PromptGenerator.build_session_context_section(session_context)` - Static method, builds server-verified context markdown section

### Agent Env Service

**File**: `backend/app/env-templates/app_core_base/core/server/agent_env_service.py`

- `AgentEnvService.get_agent_prompts()` - Returns tuple of (workflow_prompt, entrypoint_prompt)
- `AgentEnvService.update_agent_prompts()` - Write prompts to docs directory, returns list of updated filenames
- `AgentEnvService.validate_workspace()` - Check workspace exists and is writable
- `AgentEnvService.get_workspace_info()` - Return workspace metadata dict

### Active Session Manager

**File**: `backend/app/env-templates/app_core_base/core/server/active_session_manager.py`

- `ActiveSessionManager.store_session_context()` - HMAC-verify and store per-session context
- `ActiveSessionManager.get_session_context()` - Retrieve context by backend_session_id
- `ActiveSessionManager.cleanup_session_context()` - Remove context when stream ends

### SDK Adapters

**Base**: `backend/app/env-templates/app_core_base/core/server/adapters/base.py`

- `BaseSDKAdapter.send_message_stream()` - Abstract method, streams `SDKEvent` objects
- `BaseSDKAdapter.interrupt_session()` - Abstract method, interrupts running session
- `AdapterRegistry.register(adapter_type)` - Decorator for adapter registration
- `AdapterRegistry.create_adapter(config)` - Instantiate adapter from `SDKConfig`
- `SDKConfig.from_env(mode)` - Parse adapter ID from environment variables

**Claude Code**: `backend/app/env-templates/app_core_base/core/server/adapters/claude_code_sdk_adapter.py`

- `ClaudeCodeAdapter.send_message_stream()` - Configures Claude SDK, streams messages via `ClaudeCodeEventTransformer`, logs events via `SessionEventLogger`
- `ClaudeCodeAdapter.interrupt_session()` - Interrupts running Claude session

**Claude Code Event Transformer**: `backend/app/env-templates/app_core_base/core/server/adapters/claude_code_event_transformer.py`

- `ClaudeCodeEventTransformer.translate()` - Translates a single Claude SDK message object into an `SDKEvent` (or `None` to skip)

**OpenCode**: `backend/app/env-templates/app_core_base/core/server/adapters/opencode_sdk_adapter.py`

- `OpenCodeAdapter.__init__()` - Starts `opencode serve` subprocess on port 4096, waits for health
- `OpenCodeAdapter.send_message_stream()` - Creates/resumes session via HTTP, writes AGENTS.md from system prompt, streams SSE events, yields `SDKEvent` objects
- `OpenCodeAdapter.interrupt_session()` - Deletes the running OpenCode session
- `OpenCodeAdapter._start_opencode_server()` - Launches `opencode serve`, manages process lifecycle
- `OpenCodeAdapter._ensure_server_running()` - Health check + restart if crashed
- `OpenCodeAdapter._setup_mcp_servers()` - Configures custom tool MCP bridge servers in `opencode.json`

Config files read at startup from `/app/core/.opencode/`:
- `building_config.json` — model, auth, mcp, permissions for building mode
- `conversation_config.json` — model, auth, mcp, permissions for conversation mode
- `AGENTS.md` — system prompt, written per request before message send
- `session_context.json` — session context written before each message for MCP bridge servers

### Backend Services

**Message Service**: `backend/app/services/sessions/message_service.py`
- `send_message_to_environment_stream()` - HTTP POST to `/chat/stream`, includes `agent_sdk` in payload, parses SSE events

**Session Context Signer**: `backend/app/services/sessions/session_context_signer.py`
- `sign_session_context()` - HMAC-SHA256 signing with canonical JSON serialization
- `verify_session_context()` - Signature verification

**Environment Lifecycle**: `backend/app/services/environments/environment_lifecycle.py`
- `EnvironmentLifecycleManager._update_environment_config()` - Regenerate JWT token, docker-compose.yml, .env
- `EnvironmentLifecycleManager._generate_auth_token()` - Create 10-year JWT with user ID
- `EnvironmentLifecycleManager._generate_opencode_config_files()` - Generates per-mode `building_config.json` and `conversation_config.json` in `app/core/.opencode/` with auth credentials and model selection; called during create and rebuild when any mode uses `opencode/*`

## Frontend Components

No dedicated frontend components - the environment core is a backend/container-side system. Frontend interacts indirectly through:

- Chat interface sends messages via backend message streaming endpoints
- Agent settings UI triggers prompt sync via agents API routes

## Configuration

### Environment Variables (container-side)

| Variable | Required | Default | Purpose |
|----------|----------|---------|---------|
| `CLAUDE_CODE_WORKSPACE` | Yes | `/app/workspace` | Workspace root path |
| `ENV_ID` | Yes | - | Environment UUID |
| `AGENT_ID` | Yes | - | Agent UUID |
| `AGENT_AUTH_TOKEN` | Yes | - | JWT bearer token for authentication |
| `ANTHROPIC_API_KEY` | One of | - | API key (prefix `sk-ant-api*`) |
| `CLAUDE_CODE_OAUTH_TOKEN` | these | - | OAuth token (prefix `sk-ant-oat*`) |
| `SDK_ADAPTER_BUILDING` | No | `claude-code/anthropic` | Adapter ID for building mode |
| `SDK_ADAPTER_CONVERSATION` | No | `claude-code/anthropic` | Adapter ID for conversation mode |
| `CLAUDE_CODE_PERMISSION_MODE` | No | `acceptEdits` | SDK permission mode |
| `DUMP_LLM_SESSION` | No | `false` | Enable session logging |
| `ENV_NAME` | No | - | Human-readable name |

### Configuration File Management

- `.env` and `docker-compose.yml` are regenerated on every `rebuild` and `start` operation
- Fresh JWT tokens generated by `EnvironmentLifecycleManager._generate_auth_token()` in `backend/app/services/environments/environment_lifecycle.py`
- Anthropic credential type auto-detected by key prefix
- Config updates handled by `EnvironmentLifecycleManager._update_environment_config()`

### Prompt Files (workspace)

| File | Location | Loaded By |
|------|----------|-----------|
| `BUILDING_AGENT.md` | `/app/BUILDING_AGENT.md` | `prompt_generator.py` (cached at init) |
| `WORKFLOW_PROMPT.md` | `/app/workspace/docs/WORKFLOW_PROMPT.md` | `prompt_generator.py` (per request) |
| `ENTRYPOINT_PROMPT.md` | `/app/workspace/docs/ENTRYPOINT_PROMPT.md` | `prompt_generator.py` (per request) |
| `scripts/README.md` | `/app/workspace/scripts/README.md` | `prompt_generator.py` (per request) |

### Agent Status File

`/app/workspace/docs/STATUS.md` — agent-maintained status snapshot. Read on demand by `AgentStatusService` via `fetch_workspace_item_with_meta()`; not loaded into system prompts. See [Agent Status Command](../agent_commands/agent_status_command.md) for parsing rules, frontmatter format, and caching behaviour.

## Security

### JWT Authentication

- All endpoints require `Authorization: Bearer {token}` - validated via `get_current_user()` in `backend/app/api/deps.py`
- Token generated by `EnvironmentLifecycleManager._generate_auth_token()` with HS256 algorithm
- 10-year expiration, regenerated on rebuild/start
- Stored in `environment.config["auth_token"]` and container `.env`

### Session Context HMAC

- Signing module: `backend/app/services/sessions/session_context_signer.py`
- Canonical JSON (`sort_keys=True, separators=(',',':')`) for deterministic signing
- Verification in `active_session_manager.py` before context storage
- Per-session isolation by `backend_session_id` key

### Workspace Isolation

- `/app/core` mounted read-only (`:ro`) - LLM cannot modify server code
- Agent restricted to `/app/workspace` for file operations
- Credentials in `/app/workspace/credentials/` - readable by agent, not exposed to frontend

### System Prompt Injection Resistance

- Server-verified metadata positioned as trusted "Session Context (Server-Verified, Read-Only)" section
- LLM instructed to trust system prompt values over message content
- Scripts query `GET /session/context` directly, bypassing LLM entirely

