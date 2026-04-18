# Multi-SDK Support — Technical Reference

## File Locations

**Backend — Models:**
- `backend/app/models/users/user.py` — `User`, `UserUpdateMe`, `UserPublic` (SDK default fields), `AIServiceCredentials`, `AIServiceCredentialsUpdate`, `UserPublicWithAICredentials`
- `backend/app/models/environments/environment.py` — `AgentEnvironment`, `AgentEnvironmentCreate`, `AgentEnvironmentPublic` (SDK selection fields)

**Backend — Services:**
- `backend/app/services/environments/environment_service.py` — SDK constants, default cascade logic, credential validation
- `backend/app/services/environments/environment_lifecycle.py` — env file generation, settings file generation, rebuild regeneration

**Backend — Routes:**
- `backend/app/api/routes/users.py` — AI credential endpoints, SDK default update
- `backend/app/api/routes/agents.py` — environment creation with SDK fields

**Migrations:**
- `backend/app/alembic/versions/776395044d2b_add_agent_sdk_fields_to_environment.py`
- `backend/app/alembic/versions/c8d9e0f1a2b3_add_default_sdk_fields_to_user.py`
- `backend/app/alembic/versions/f0920ee2eeab_add_model_override_fields_to_environment.py`
- `backend/app/alembic/versions/a1b2c3d4e5f7_add_default_credential_fields_to_user.py`

**Frontend — Components:**
- `frontend/src/components/UserSettings/AICredentials.tsx`
- `frontend/src/components/Environments/AddEnvironment.tsx`
- `frontend/src/components/Environments/EnvironmentCard.tsx`

**Frontend — Client:**
- `frontend/src/client/` (auto-generated OpenAPI types and service classes)

**Agent Environment (inside container):**
- `backend/app/env-templates/app_core_base/core/server/sdk_manager.py`
- `backend/app/env-templates/app_core_base/core/server/sdk_utils.py` — `SessionEventLogger` (shared JSONL logger for all adapters)
- `backend/app/env-templates/app_core_base/core/server/adapters/base.py`
- `backend/app/env-templates/app_core_base/core/server/adapters/claude_code_sdk_adapter.py`
- `backend/app/env-templates/app_core_base/core/server/adapters/claude_code_event_transformer.py` — `ClaudeCodeEventTransformer`
- `backend/app/env-templates/app_core_base/core/server/adapters/opencode_sdk_adapter.py`
- `backend/app/env-templates/app_core_base/core/server/adapters/opencode_event_transformer.py` — `OpenCodeEventTransformer`
- `backend/app/env-templates/app_core_base/core/server/adapters/tool_name_registry.py` — unified lowercase tool name convention: maps, pre-approved set, `normalize_tool_name()`
- `backend/app/env-templates/app_core_base/core/server/tools/mcp_bridge/knowledge_server.py`
- `backend/app/env-templates/app_core_base/core/server/tools/mcp_bridge/task_server.py`
- `backend/app/env-templates/app_core_base/core/server/tools/mcp_bridge/collaboration_server.py` <!-- nocheck -->

**Dockerfiles (OpenCode PATH fix applied to all three):**
- `backend/app/env-templates/general-env/Dockerfile`
- `backend/app/env-templates/general-assistant-env/Dockerfile`
- `backend/app/env-templates/python-env-advanced/Dockerfile`

## Database Schema

**User table** (`backend/app/models/users/user.py`):
- `default_sdk_conversation` — nullable string, SDK ID for conversation mode default (e.g., `claude-code/anthropic`)
- `default_sdk_building` — nullable string, SDK ID for building mode default
- `ai_credentials_encrypted` — encrypted JSON blob containing all AI provider credentials (legacy; still used for backward compat when no named credential is set)
- `default_ai_credential_conversation_id` — UUID FK to `ai_credential.id` (nullable, `ondelete=SET NULL`); user's preferred named credential for new environments in conversation mode
- `default_ai_credential_building_id` — UUID FK to `ai_credential.id` (nullable, `ondelete=SET NULL`); user's preferred named credential for new environments in building mode
- `default_model_override_conversation` — nullable string (max 255); optional model override saved as part of user's conversation mode default preference
- `default_model_override_building` — nullable string (max 255); optional model override saved as part of user's building mode default preference

**AgentEnvironment table** (`backend/app/models/environments/environment.py`):
- `agent_sdk_conversation` — string, SDK ID selected at creation, immutable
- `agent_sdk_building` — string, SDK ID selected at creation, immutable
- `model_override_conversation: str | None` — optional model override for conversation mode (e.g., `gpt-4o-mini`)
- `model_override_building: str | None` — optional model override for building mode (e.g., `claude-opus-4`)

**Schema constants** (`backend/app/services/environments/environment_service.py`):
- `SDK_ANTHROPIC` (`claude-code/anthropic`), `SDK_MINIMAX` (`claude-code/minimax`)
- `SDK_ENGINE_CLAUDE_CODE`, `SDK_ENGINE_OPENCODE` — engine-only prefix constants
- `VALID_SDK_ENGINES` — list of the two valid engine prefixes
- `SDK_CREDENTIAL_COMPATIBILITY` — dict mapping engine → list of compatible credential type strings
- `SDK_TO_CREDENTIAL_TYPE` — full SDK ID → `AICredentialType` mapping including all `opencode/*` variants

## API Endpoints

**User credentials and SDK defaults** (`backend/app/api/routes/users.py`):
- `GET /api/v1/users/me` — returns `default_sdk_conversation`, `default_sdk_building`, `default_ai_credential_conversation_id`, `default_ai_credential_building_id`, `default_model_override_conversation`, `default_model_override_building`
- `PATCH /api/v1/users/me` — updates SDK default fields including `default_ai_credential_*_id` and `default_model_override_*`; SDK ID values validated against `VALID_SDK_OPTIONS`
- `GET /api/v1/users/me/ai-credentials/status` — boolean key presence flags + all SDK default fields (used by frontend to pre-populate both the Settings panel and Add Environment dialog)
- `GET /api/v1/users/me/ai-credentials` — full credentials including `openai_compatible_base_url`, `openai_compatible_model`
- `PATCH /api/v1/users/me/ai-credentials` — updates credential fields (partial update, non-empty fields only)

**Environment creation** (`backend/app/api/routes/agents.py`):
- `POST /api/v1/agents/{id}/environments` — accepts `agent_sdk_conversation`, `agent_sdk_building`

## Services & Key Methods

**`backend/app/services/environments/environment_service.py`:**
- `SDK_API_KEY_MAP` — maps legacy SDK ID to required credential field name (for backward compat)
- `SDK_CREDENTIAL_COMPATIBILITY` — maps engine prefix to list of compatible credential type strings
- `_validate_sdk_credential_compatibility()` — raises `EnvironmentCredentialError` if SDK engine and credential type are incompatible
- `create_environment()` — applies default SDK cascade, validates SDK ↔ credential compatibility, passes credential params to background task

**`backend/app/services/environments/environment_lifecycle.py`:**
- `create_environment_instance()` — accepts all credential params for supported provider types
- `_update_environment_config()` — fetches user credentials and triggers env file generation
- `_generate_env_file()` — writes `.env`; conditionally includes `ANTHROPIC_API_KEY`; calls settings generators for MiniMax, OpenAI Compatible, and OpenCode
- `_generate_minimax_settings_files()` — writes JSON settings to `app/core/.claude/`
- `_generate_opencode_config_files()` — writes `opencode.json` to `app/core/.opencode/{mode}/` for each mode that uses `opencode/*`; embeds model selection, provider registration with API key, permission rules, tool flags, and MCP bridge server commands; called at environment creation and rebuild
- `rebuild_environment()` — after core replacement, regenerates settings files for all adapter types including OpenCode

## Frontend Components

**`frontend/src/components/UserSettings/AICredentials.tsx`:**
- Two-panel layout: AI Credentials list (left) and Default SDK Preferences (right)
- Default SDK Preferences panel renders two bordered sections (Conversation Mode, Building Mode), each with three cascading controls: SDK Engine select, Credential select, Model Override input
- SDK Engine options: `claude-code` ("Claude Code"), `opencode` ("OpenCode")
- Credential dropdown filtered via `SDK_CREDENTIAL_COMPATIBILITY` map; first option is "Use Default" (`__default__` sentinel)
- Model Override input uses `<datalist>` for type-specific suggestions (`SUGGESTED_MODELS` map)
- All three values per mode saved together via a single "Save Preferences" button (`handleSavePreferences`)
- Engine change cascades: resets credential to `__default__` and clears model override
- Initializes from `status.default_sdk_*`, `status.default_ai_credential_*_id`, `status.default_model_override_*` on first load
- `updateSdkMutation` sends `default_sdk_*`, `default_ai_credential_*_id`, and `default_model_override_*` in one `PATCH /users/me` call

**`frontend/src/components/Environments/AddEnvironment.tsx`:**
- Cascading SDK Engine → Credential → Model Override per mode (same three-step pattern as User Settings)
- Credential dropdown is always visible inline (no "Use Default AI Credentials" toggle)
- First credential option is "Default (use account default)" (`__default__` sentinel); text hint shown below: "Will use your default credential for this provider type."
- Pre-populated on dialog open from `credentialsStatus.default_sdk_*`, `default_ai_credential_*_id`, `default_model_override_*`
- `composeSDKId(engine, credential)` builds the full SDK ID sent to the backend (`engine/credentialType`)
- Submit logic: if both modes use `__default__` sentinel → sends `use_default_ai_credentials: true`; otherwise sends explicit credential IDs, with `undefined` for modes still on "Default"

**`frontend/src/components/Environments/EnvironmentCard.tsx`:**
- SDK badges with MessageCircle (conversation) and Wrench (building) icons
- `getSDKDisplayName()` converts SDK ID to display label

**React Query hooks:**
- `useQuery(["aiCredentialsStatus"])` — boolean flags + SDK defaults including `default_ai_credential_*_id` and `default_model_override_*`; used by AICredentials and AddEnvironment
- `useQuery(["aiCredentialsList"])` — list of `AICredentialPublic` objects for credential dropdowns in both components
- `useMutation` in AICredentials — SDK preference update: sends `default_sdk_*`, `default_ai_credential_*_id`, `default_model_override_*` together via `PATCH /users/me`; invalidates `aiCredentialsStatus` + `currentUser`

## Adapter Architecture (inside container)

### SDKEvent — Unified Event Format

`SDKEvent` dataclass (`adapters/base.py`) — the only event format the backend processes:
- `type: SDKEventType` — see event type table below
- `content: str` — human-readable text (assistant reply, tool description, error message)
- `session_id: str | None` — SDK session identifier; `None` only before session creation
- `metadata: dict` — event-specific additional data
- `tool_name: str | None` — populated only for `TOOL_USE` events
- `error_type: str | None` — populated only for `ERROR` events
- `session_corrupted: bool` — `True` signals backend should treat session as unrecoverable
- `stderr_lines: list[str]` — captured stderr from subprocess (Claude Code adapter)
- `data: dict | None` — payload for `SYSTEM` events with `subtype="tools_init"`
- `subtype: str | None` — secondary classification for `SYSTEM` events (e.g., `"tools_init"`, `"permission_asked"`)

### SDKEventType Values

| Value | When Emitted |
|-------|-------------|
| `session_created` | New SDK session was created; carries the `session_id` |
| `session_resumed` | Existing SDK session was resumed |
| `system` | Infrastructure events: tools list initialization, permission requests |
| `assistant` | Text chunk from the LLM |
| `thinking` | Chain-of-thought / reasoning text (e.g., OpenCode reasoning parts, Claude extended thinking) |
| `tool` | Tool invocation started — carries `tool_name` and input in `metadata` |
| `tool_result` | Tool completed — carries result or error in `metadata` |
| `done` | Session completed successfully |
| `interrupted` | Session was interrupted by user request |
| `error` | Fatal error; `error_type` field describes the category |

### SDKManager — Adapter Routing

`sdk_manager.py` (`SDKManager` class):
- On first `send_message_stream()` call for a mode, reads `SDK_ADAPTER_{MODE}` env var
- Splits adapter ID into `adapter_type` / `provider` (e.g., `opencode` / `anthropic`)
- Looks up adapter class in `AdapterRegistry` by `adapter_type`
- Instantiates and caches one adapter per mode; subsequent calls reuse the cached instance
- If adapter type is unknown, falls back to `claude-code/anthropic`
- Converts each `SDKEvent` to a dict via `event.to_dict()` for backward compatibility with the backend streaming protocol
- `ClaudeCodeSDKManager` is a deprecated alias for `SDKManager`

### AdapterRegistry — Decorator-Based Registration

`AdapterRegistry` (`adapters/base.py`):
- Class-level dict mapping `adapter_type` string → adapter class
- `@AdapterRegistry.register` decorator registers a class at import time
- `create_adapter(config)` instantiates the correct class; logs a warning (not error) if the provider is not in `SUPPORTED_PROVIDERS` (allows forward-compatible extensions)

### BaseSDKAdapter — Contract

All adapters must implement:
- `send_message_stream(message, session_id, backend_session_id, system_prompt, mode, session_state) -> AsyncIterator[SDKEvent]`
- `interrupt_session(session_id) -> bool`

Class-level declarations required:
- `ADAPTER_TYPE: str` — must match the prefix used in SDK IDs
- `SUPPORTED_PROVIDERS: list[str]` — providers this adapter handles

### ClaudeCodeAdapter (`adapters/claude_code_sdk_adapter.py`)

- Handles `claude-code/*` variants (anthropic, minimax)
- Settings file detection: checks `/app/core/.claude/{mode}_settings.json`; if present, passes path via `options.settings`
- Falls back to `ANTHROPIC_API_KEY` env var if no settings file found
- Uses Claude SDK Python library directly (subprocess-based streaming)
- Delegates all message-to-`SDKEvent` translation to `ClaudeCodeEventTransformer`
- Logs all sent/received events via `SessionEventLogger` (JSONL format, same as OpenCode)

### ClaudeCodeEventTransformer (`adapters/claude_code_event_transformer.py`)

Stateful translator from raw Claude Agent SDK messages to `SDKEvent` objects. Mirrors the `OpenCodeEventTransformer` pattern — a dedicated translator class that can be instantiated and tested in isolation.

- `translate(message_obj, session_id, interrupt_initiated)` — maps Claude SDK message types to `SDKEvent`
- `_handle_system_message()` — skips `init` subtype; forwards other system events
- `_handle_assistant_message()` — extracts `TextBlock`, `ThinkingBlock`, `ToolUseBlock`; normalizes tool names via `tool_name_registry`
- `_handle_result_message()` — emits `DONE` or `INTERRUPTED` based on subtype and interrupt flag
- `_handle_user_message()` — forwards interrupt notifications, skips other user messages

### OpenCodeAdapter (`adapters/opencode_sdk_adapter.py`)

The most complex adapter. Runs `opencode serve` as a managed subprocess and communicates over HTTP + SSE.

**Per-mode server isolation:**

| | Building | Conversation |
|---|---|---|
| Port | 4096 | 4097 |
| Config source | `/app/core/.opencode/building/opencode.json` | `/app/core/.opencode/conversation/opencode.json` |
| Runtime dir | `/tmp/.opencode_building/` | `/tmp/.opencode_conversation/` |
| Adapter instance | Separate (cached per mode by SDKManager) | Separate |

Each mode has its own `opencode serve` process. Model is baked into the config — no runtime config changes between sessions, no race conditions.

**Server lifecycle:**
- `_ensure_server_running()` — starts the process if not alive; uses `asyncio.Lock` to prevent concurrent starts; clears stale session ID on restart
- `_start_opencode_server()` — creates the runtime dir, symlinks static config files from the read-only `/app/core/.opencode/{mode}/` into the writable `/tmp/.opencode_{mode}/`, launches `opencode serve --port {port} --hostname 127.0.0.1` with `cwd={runtime_dir}` so opencode finds `opencode.json` and `AGENTS.md` in one place
- `_wait_for_server_health()` — polls `GET /health` then `GET /doc` every 1s up to `OPENCODE_STARTUP_TIMEOUT` (30s)

**Message flow per `send_message_stream()` call:**
1. Resolve per-mode port/dir via `_resolve_mode(mode)` (no-op if already resolved)
2. Ensure server is running
3. Create or resume session via `POST /session`; yield `SESSION_CREATED` or `SESSION_RESUMED`
4. Register session with `active_session_manager` for interrupt support
5. Write `session_context.json` to the runtime dir so MCP bridge servers can read `backend_session_id`
6. Resolve and write system prompt as `AGENTS.md` to the runtime dir
7. Build plugin MCP config; yield `SYSTEM` event with `subtype="tools_init"` and full tool list
8. Open SSE stream on `GET /global/event` first
9. On first SSE event (any type), fire `POST /session/{id}/message` as a background `asyncio.Task` — this avoids missing events from fast models and prevents deadlock (POST blocks until LLM completes)
10. For each SSE chunk: check for interrupt flag, check progress timeout, parse and translate via `OpenCodeEventTransformer`
11. Yield translated `SDKEvent` objects; stop on `DONE`, `ERROR`, or `INTERRUPTED`
12. In `finally`: cancel pending POST task if needed; unregister session from `active_session_manager`

**Progress timeout (`OPENCODE_PROGRESS_TIMEOUT = 120s`):**
- Tracks the last time any meaningful SSE event arrived (not heartbeats)
- If only heartbeats come for 120s after the message was posted, the session is considered hung (e.g., `read` tool given a directory)
- Calls `DELETE /session/{id}` to clean up the OpenCode process, then yields an `ERROR` event with `error_type="ProgressTimeout"`

**Interrupt handling:**
- `interrupt_session()` calls `active_session_manager.request_interrupt(session_id)` to set a flag
- The SSE loop checks this flag between chunks; when set, calls `_delete_session()` and yields `INTERRUPTED`
- Fallback: if session is not registered (already finishing), calls `DELETE /session/{id}` directly

### OpenCodeEventTransformer (`adapters/opencode_event_transformer.py`)

Stateful translator from raw OpenCode SSE events to `SDKEvent` objects. Instantiated once per `OpenCodeAdapter` instance and shared across sessions.

**OpenCode SSE event types handled:**

| OpenCode Event | SDKEvent Output |
|---------------|----------------|
| `session.idle` | Flush all text buffers → `DONE` |
| `message.part.updated` (type=text, end) | Flush buffer → `ASSISTANT` |
| `message.part.updated` (type=reasoning, end) | Flush buffer → `THINKING` |
| `message.part.updated` (type=tool, running) | `TOOL_USE` (with truncated input) |
| `message.part.updated` (type=tool, completed) | `TOOL_RESULT` (with truncated output) |
| `message.part.updated` (type=tool, error) | `TOOL_RESULT` (with error flag in metadata) |
| `message.part.delta` (type=text) | Buffer delta, flush on newline → `ASSISTANT` |
| `message.part.delta` (type=reasoning) | Buffer delta, flush on newline → `THINKING` |
| `permission.asked` | `SYSTEM` with `subtype="permission_asked"` and human-readable `content` |
| `question.asked` | `TOOL_USE` (tool_name `askuserquestion`, Claude-Code-compatible input) followed by `DONE`; `opencode_question_request_id` is attached to metadata so the adapter can call `POST /question/{requestID}/reject` to release the suspended session |
| `message.part.updated` (type=tool, tool=`question`) | Silently suppressed — handled by `question.asked` |
| `message.updated`, `session.updated`, `session.status`, `session.diff`, `server.connected`, `server.heartbeat`, `project.updated` | Silently skipped (no events emitted) |
| Any event with `error` in type or `error` in properties | `ERROR` |

**Text/reasoning buffering strategy:**
- Deltas are accumulated per `partID` in `_text_buffers`
- When the buffer contains a newline, everything up to and including the last newline is flushed as an event; the remainder stays buffered
- When the part finishes (`time.end` present), the buffer remainder is flushed
- This produces natural streaming without extra paragraph spacing from many small deltas

**SSE envelope unwrapping:**
- OpenCode wraps SSE events in `{"payload": {...}}`; `_parse_sse_event()` unwraps this so callers always see the inner event dict with `type` and `properties` at the top level

**State management:**
- `reset()` — clears `_part_types` and `_text_buffers` between messages (called before each new SSE event sequence)

**Raw event logging (`SessionEventLogger` from `sdk_utils.py`):**
- Enabled when `DUMP_LLM_SESSION=true`
- Shared JSONL logger used by all adapters (Claude Code and OpenCode); each adapter passes a prefix (`"claude_code_session"` or `"opencode_session"`) to distinguish log files
- Writes JSONL to `{workspace_dir}/logs/{prefix}_{timestamp}.jsonl`
- Each line: `{"ts": "...", "dir": "recv"|"send", "event": {...}}`
- Used for test development and offline debugging; cross-adapter format enables side-by-side comparison

### MCP Bridge Servers (OpenCode only)

Located in `tools/mcp_bridge/`. Each is a standalone Python MCP stdio server registered in `opencode.json`. OpenCode spawns them as child processes when needed.

All bridge servers read `session_context.json` from their cwd (the mode runtime dir, where `opencode serve` is run) to get `backend_session_id` at call time.

- `knowledge_server.py` — exposes `query_integration_knowledge` tool; calls backend knowledge API
- `task_server.py` — exposes `add_comment`, `update_status`, `create_task`, `create_subtask`, `get_details`, `list_tasks` tools

MCP tool names visible to the agent follow the pattern `mcp__{server}__{tool}` (e.g., `mcp__agent_task__create_task`).

## Configuration

**Environment variables injected into container:**
- `SDK_ADAPTER_BUILDING` — SDK ID for building mode (e.g., `claude-code/anthropic`)
- `SDK_ADAPTER_CONVERSATION` — SDK ID for conversation mode
- `DUMP_LLM_SESSION` — set to `true` to enable JSONL event logging for all adapters (Claude Code and OpenCode); log files are written to `{workspace}/logs/` with adapter-specific prefixes
- `OPENCODE_SKIP_UPDATE` — always set to `1` in subprocess env to suppress update prompts

**Settings file locations inside container:**
- `app/core/.claude/building_settings.json` — MiniMax building mode config
- `app/core/.claude/conversation_settings.json` — MiniMax conversation mode config
- `app/core/.opencode/building/opencode.json` — OpenCode building mode server config (port 4096)
- `app/core/.opencode/conversation/opencode.json` — OpenCode conversation mode server config (port 4097)

**OpenCode `opencode.json` fields (generated by `_generate_opencode_config_files`):**
- `$schema` — `"https://opencode.ai/config.json"`
- `model` — provider-qualified model string (e.g., `anthropic/claude-sonnet-4-5`, `openai/gpt-4o`); set from `model_override_*` if provided, else from per-provider mode defaults
- `provider` — provider registration block; registers the selected model by ID so OpenCode accepts it even if it's not in OpenCode's built-in list; includes `options.apiKey` with the API key directly embedded (file permissions set to `0o600`)
- `permission` — wildcard allow `"*": "allow"` plus `external_directory` rules pre-approving `/app/workspace/**`, `/app/**`, `/tmp/**`
- `tools` — per-tool enable flags: `webfetch`, `websearch`, `bash`, `read`, `write`, `edit`, `glob`, `grep`, `list`, `patch`
- `mcp` — MCP bridge server entries (knowledge, task, collaboration); each has `type: "local"`, `command: ["python3", "..."]`, `enabled: true`
- `server` — `{"port": 4096, "hostname": "127.0.0.1"}` (building) or `{"port": 4097, ...}` (conversation)

**Default models per provider per mode (OpenCode):**

| Provider | Building Mode Default | Conversation Mode Default |
|----------|----------------------|--------------------------|
| `anthropic` | `anthropic/claude-sonnet-4-5` | `anthropic/claude-haiku-4-5-20251001` |
| `openai` | `openai/gpt-5.4-mini` | `openai/gpt-5.4-nano` |
| `openai_compatible` | from credential config | from credential config |
| `google` | `google/gemini-2.5-pro` | `google/gemini-2.5-flash` |

**MiniMax settings file fields:** `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`, model mappings

## Tests

- `backend/tests/unit/test_opencode_event_transformer.py` — unit tests for `OpenCodeEventTransformer` in isolation (no HTTP, no async, no Docker)
  - Informational events (10) — verify silent skipping
  - Session completion (2) — `session.idle` → DONE with buffer flush
  - Error events (2) — error in event type name or properties
  - Text streaming (5) — newline buffering, delta flush, reasoning as THINKING
  - Tool events (4) — pending skipped, running TOOL_USE, completed TOOL_RESULT, error
  - Permission events (3) — forwarded as SYSTEM with non-empty content
  - Conversation replays (10) — full event sequences
  - Real session replays (5) — from captured JSONL files
- `backend/tests/unit/test_opencode_mcp_bridge.py` — MCP bridge server tests
- `backend/tests/unit/test_phase5_advanced_providers.py` — provider config generation tests <!-- nocheck -->

Run without Docker:

    cd backend && source .venv/bin/activate
    python -m pytest tests/unit/test_opencode_event_transformer.py -v --noconftest

## Security

- SDK values validated against `VALID_SDK_OPTIONS` before database write
- User must have required credentials before environment creation — checked via `SDK_API_KEY_MAP`
- AI credentials stored encrypted in `ai_credentials_encrypted` JSON blob; decrypted only during env generation
- Settings files are generated per-environment with the owning user's keys
- SDK selection is immutable post-creation — no runtime SDK switching
- API keys not accessible across users; credentials fetched by user ID in service layer
- OpenCode `opencode.json` files containing API keys are written with `0o600` permissions (owner-read-only)
- OpenCode runtime dirs (`/tmp/.opencode_{mode}/`) are writable only by the container process
