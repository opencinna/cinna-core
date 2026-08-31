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
- `backend/app/env-templates/platform-knowledge-env/Dockerfile`
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

**Schema constants & helpers** (`backend/app/services/environments/sdk_constants.py`):
- `SDK_ANTHROPIC` (`claude-code/anthropic`), `SDK_MINIMAX` (`claude-code/minimax`)
- `SDK_ENGINE_CLAUDE_CODE`, `SDK_ENGINE_OPENCODE` — engine-only prefix constants
- `VALID_SDK_ENGINES` — list of the two valid engine prefixes
- `SDK_CREDENTIAL_COMPATIBILITY` — engine → list of credential types. Used only for two non-validating purposes: forward-compat fallback when an SDK id isn't in the strict map, and as the candidate set for `resolve_default_credential_for_sdk`'s priority ranking
- `SDK_TO_CREDENTIAL_TYPE` — full SDK ID → `AICredentialType` mapping. Single source of truth for strict provider matching
- `sdk_expected_credential_type(sdk_id)` — returns the exact `AICredentialType` a full SDK id requires, or `None` for SDK strings outside the strict map
- `is_credential_compatible_with_sdk(sdk_id, cred_type)` — strict boolean check using the helper above; returns `True` when the SDK id is unmapped so callers can pre-validate the SDK with `is_valid_sdk` if they want hard rejection

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
- `_validate_sdk_credential_compatibility(sdk_id, credential)` — strict full-SDK-id match via `sdk_expected_credential_type`. Raises `EnvironmentCredentialError` with a "SDK 'X' requires a 'Y' credential, got 'Z'" detail when types don't match. Falls back to the engine-level `SDK_CREDENTIAL_COMPATIBILITY` list only when the SDK id is unmapped (forward-compat)
- `create_environment()` — applies default SDK cascade, validates SDK ↔ credential compatibility, passes credential params to background task. `POST /agents/{id}/environments` wraps the call with `try/except AgentEnvironmentError` so the validation surfaces as HTTP 400 (not 500)

**`backend/app/services/environments/environment_lifecycle.py`:**
- `create_environment_instance()` — accepts all credential params for supported provider types
- `_update_environment_config()` — fetches user credentials and triggers env file generation
- `_generate_env_file()` — writes `.env`; conditionally includes `ANTHROPIC_API_KEY`; calls settings generators for MiniMax, OpenAI Compatible, and OpenCode
- `_generate_minimax_settings_files()` — writes JSON settings to `app/core/.claude/`
- `_generate_opencode_config_files()` — writes `opencode.json` to `app/core/.opencode/{mode}/` for each mode that uses `opencode/*`; embeds model selection, provider registration with API key, permission rules, tool flags, MCP bridge server commands, and the absolute `instructions` AGENTS.md path; called at environment creation and rebuild. Uses the `OPENCODE_RUNTIME_DIR_TEMPLATE` / `OPENCODE_AGENTS_MD_FILENAME` constants mirrored from the adapter
- `rebuild_environment()` — after core replacement, regenerates settings files for all adapter types including OpenCode

### Credential resolution into the environment

Which AI key reaches each mode is resolved in two phases. Both fill a **credential bag** (`make_empty_credential_bag` / `apply_credential_to_bag` in `sdk_constants.py`, keyed by `CREDENTIAL_TYPE_TO_BAG_KEY`).

**Create-time (one-shot)** — `environment_service.py:create_environment()`:
- `use_default_ai_credentials=True`: seeds the bag from the legacy profile, then per mode applies the user's per-mode default credential (`default_ai_credential_conversation_id` / `default_ai_credential_building_id`) when type-compatible, else the type-level default (`get_default_for_type`).
- `use_default_ai_credentials=False`: applies the explicit `conversation_ai_credential_id` / `building_ai_credential_id`, else the type-level default for the mode's SDK.
- A credential id is **persisted on the environment only when it came from an explicit pick or a per-mode default**. Type-level-default fallbacks fill the bag value but persist NO id (so `building_ai_credential_id` / `conversation_ai_credential_id` stay `null`).
- Validates every required bag key for the chosen SDKs is present, else raises `EnvironmentCredentialError` (HTTP 400).
- The computed bag is passed through `create_environment_instance()` → `_update_environment_config()` → `_generate_env_file()`.

**Reconfigure (start / restart / rebuild)** — re-resolves with NO bag passed in, using only the ids stored on the environment plus a per-mode fallback. Two code paths share the same helpers:
- `_update_environment_config()` — runs on every `start_environment()` (and restart).
- `rebuild_environment()` — runs on rebuild.

Shared helpers on `EnvironmentLifecycleManager` (in `environment_lifecycle.py`):
- `_usable_assigned_credential(db_session, user, credential_id, sdk_id)` — an id counts as "assigned" only if it exists AND is type-compatible with the mode's SDK; a mismatched/poisoned id is ignored so the fallback can self-heal the slot.
- `_resolve_assigned_credential_into_bag(db_session, user, environment, bag, credential_id, sdk_id, label)` — files an assigned credential into the bag by its ACTUAL type, skipping incompatible ids, filling empty slots only.
- `_fallback_fill_bag_for_sdk(db_session, user, bag, sdk_id)` — for a mode with no usable assigned id, fills its slot from the user's named type-default, then the legacy encrypted profile (anthropic / minimax / openai_compatible only; openai / google are named-credential-only).

**Per-mode fallback rule:** the fallback is gated PER MODE (`if not conv_assigned: ...; if not build_assigned: ...`), never all-or-nothing. A mode whose id was never persisted (resolved from a type-level default at create time) still re-resolves on every reconfigure, even when the OTHER mode pins a credential. An earlier all-or-nothing gate dropped the unpinned mode's key — e.g. left `ANTHROPIC_API_KEY=` empty for a `claude-code/anthropic` building mode paired with a pinned `opencode/openai` conversation mode.

**Key delivery quirks:**
- `OPENAI_API_KEY` / `GOOGLE_API_KEY` are written to `.env` but the docker-compose templates do NOT forward them into the container — only `ANTHROPIC_API_KEY` / `CLAUDE_CODE_OAUTH_TOKEN` are passed through (see each template's `environment:` block). OpenCode reads its key from `opencode.json` (`provider.<id>.options.apiKey`), not an env var, so "no OpenAI env var in the container" is expected.
- `ai_credentials_service.py:_sync_default_to_user_profile()` mirrors only anthropic / minimax / openai_compatible defaults into the legacy `ai_credentials_encrypted` profile; openai / google live solely as named credentials.

Regression tests: `backend/tests/api/agents/core/agents_ai_credential_slot_mismatch_test.py` — mismatch self-heal plus mixed-SDK per-mode fallback on both rebuild and reconfigure paths.

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
- `_handle_assistant_message()` — extracts `TextBlock`, `ThinkingBlock`, `ToolUseBlock`; normalizes tool names via `tool_name_registry`. **Synthetic messages** (`model == "<synthetic>"`, emitted by the CLI for failures like "Invalid API key · Please run /login") are translated to `ERROR` events (not normal `ASSISTANT` replies), with the raw text humanized via `_humanize_error_text()`
- `_handle_result_message()` — emits `INTERRUPTED` (user interrupt), `ERROR`, or `DONE`, in that precedence. A `ResultMessage` with `is_error=True` maps to `ERROR` **even when `subtype == "success"`** (Claude Code reports invalid/expired key, billing, and max-turns failures this way); without this the run would otherwise be reported as a silent `DONE`. Error text comes from `_extract_result_error_text()`
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
- `_start_opencode_server()` — creates the runtime dir, symlinks static config files from the read-only `/app/core/.opencode/{mode}/` into the writable `/tmp/.opencode_{mode}/`, launches `opencode serve --port {port} --hostname 127.0.0.1` with `cwd={runtime_dir}`. The subprocess env sets `OPENCODE_CONFIG` and `CINNA_SESSION_CONTEXT_PATH` (both absolute paths under the runtime dir) — required because each session binds to `/app/workspace` as its project root (see **Workspace project binding** below)
- `_wait_for_server_health()` — polls `GET /health` then `GET /doc` every 1s up to `OPENCODE_STARTUP_TIMEOUT` (30s)
- The `/tmp/.opencode_{mode}` path and the `AGENTS.md` filename are the module constants `OPENCODE_RUNTIME_DIR_TEMPLATE` / `OPENCODE_AGENTS_MD_FILENAME`; the host-side config generator (`environment_lifecycle.py`) **mirrors** them (host/container boundary prevents a shared import — keep the two definitions in sync)

**Message flow per `send_message_stream()` call:**
1. Resolve per-mode port/dir via `_resolve_mode(mode)` (no-op if already resolved)
2. Ensure server is running
3. Create or resume session via `POST /session?directory=/app/workspace`; yield `SESSION_CREATED` or `SESSION_RESUMED`. The `directory` query param binds the session's project root to the agent workspace (see **Workspace project binding**)
4. Register session with `active_session_manager` for interrupt support
5. Write `session_context.json` to the runtime dir so MCP bridge servers can read `backend_session_id`
6. Resolve and write system prompt as `AGENTS.md` to the runtime dir (loaded via the config's `instructions` entry, not cwd auto-discovery)
7. Build plugin MCP config; yield `SYSTEM` event with `subtype="tools_init"` and full tool list
8. Open SSE stream on `GET /global/event` first
9. On first SSE event (any type — including foreign-session events, which still prove the socket is live), fire `POST /session/{id}/message` as a background `asyncio.Task` — this avoids missing events from fast models and prevents deadlock (POST blocks until LLM completes)
10. For each SSE chunk: demultiplex by session ID (see **Global event stream demultiplexing** below), check for interrupt flag, check progress timeout, parse and translate via `OpenCodeEventTransformer`
11. Yield translated `SDKEvent` objects; stop on `DONE`, `ERROR`, or `INTERRUPTED`
12. In `finally`: cancel pending POST task if needed; unregister session from `active_session_manager`

**Global event stream demultiplexing:**

`GET /global/event` is a **serve-wide** SSE stream shared by every session of the mode's `opencode serve` process (one process per mode, shared by all backend sessions). Each event must be matched against the session currently being streamed. The `_event_session_id(event_data)` static helper extracts the session ID from the event payload:

- `properties.sessionID` — most message / session events
- `properties.part.sessionID` — `message.part.updated` / `message.part.delta`
- `properties.info.sessionID` — some `session.*` snapshots

Events whose extracted ID differs from the current session's ID are **dropped before** the progress-timer reset and `translate()`. This prevents a concurrently running (or orphaned) session from bleeding its text or its `session.idle`→DONE into an unrelated stream. Server lifecycle events (`server.connected`, `server.heartbeat`, `project.updated`, etc.) carry no session ID and fall through unchanged — they are still valid triggers for the "first event → POST message" handshake.

**Orphan cancellation on stream teardown:**

When the adapter's SSE loop exits via `asyncio.CancelledError` (e.g., the user deleted the backend session while a slow model was still responding), the adapter fires a best-effort background `DELETE /session/{id}` on the OpenCode server. This stops the in-flight generation so it does not continue running as an orphan and later bleed its events into a different session's serve-wide stream. The `DELETE` is dispatched via `_spawn_background` because `await` is not possible inside a cancelled context; a `RuntimeError` (no running loop) is swallowed with a debug log.

**Progress timeout (`OPENCODE_PROGRESS_TIMEOUT = 120s`):**
- Tracks the last time any meaningful SSE event arrived (not heartbeats)
- If only heartbeats come for 120s after the message was posted, the session is considered hung (e.g., `read` tool given a directory)
- Calls `DELETE /session/{id}` to clean up the OpenCode process, then yields an `ERROR` event with `error_type="ProgressTimeout"`

**Interrupt handling:**
- `interrupt_session()` calls `active_session_manager.request_interrupt(session_id)` to set a flag
- The SSE loop checks this flag between chunks; when set, calls `_delete_session()` and yields `INTERRUPTED`
- Fallback: if session is not registered (already finishing), calls `DELETE /session/{id}` directly
- `_delete_session()` also passes `?directory=/app/workspace` so opencode resolves the right project instance

**Workspace project binding:**

By default `opencode serve` treats its launch cwd (the runtime dir under `/tmp`) as the project root, so agent file tools (`write` / `edit` / `apply_patch` / `bash`) resolve **relative** paths into `/tmp/.opencode_{mode}/…` instead of `/app/workspace`. That makes agent-created files invisible to the workspace, the `/files` browser, sync, and message attachments (see [agent_message_attachments_tech.md](../agent_file_management/agent_message_attachments_tech.md)). Claude Code is unaffected because its adapter launches with `cwd=workspace_dir`.

The OpenCode adapter fixes this **per session** via opencode's native `directory` query param (`POST /session?directory=/app/workspace`, also on `/session/{id}/message` and `DELETE /session/{id}`), which binds the session's project root to the workspace so file tools operate there. opencode v1.14 confirmed: relative writes land in the bound `directory`; `/global/event` SSE remains global (no `directory` needed). Three consequences this binding forces, each handled in the adapter launch env / config:

- **Config discovery** — opencode loads project `opencode.json` by walking up from the project `directory` + global dirs, NOT from cwd. With `directory=/app/workspace` it would no longer find the per-mode config under `/tmp`. Fix: `OPENCODE_CONFIG` env pins the absolute config path.
- **System prompt** — opencode no longer auto-discovers `AGENTS.md` from cwd. Fix: `opencode.json` carries an absolute `instructions` path to the runtime-dir `AGENTS.md` (verified to load only when the config itself is loaded via `OPENCODE_CONFIG`).
- **MCP bridge cwd** — opencode spawns local MCP servers with `cwd` = the session project dir (`/app/workspace`), so they can't read `session_context.json` from cwd. Fix: `CINNA_SESSION_CONTEXT_PATH` env gives the absolute path; the bridge servers read it with a cwd-relative fallback.

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
| `question.asked` | `TOOL_USE` (tool_name `askuserquestion`, Claude-Code-compatible input) followed by `DONE`; `opencode_question_request_id` is attached to metadata. The adapter relays the next user message for the session as the answer via `POST /question/{requestID}/reply?directory=/app/workspace` (parameter-free detection via `GET /question`); `reject` is teardown-only. (An earlier version rejected the question, aborting the turn and wedging the session — fixed.) See [OpenCode Interactive Questions](opencode_interactive_questions.md) |
| `message.part.updated` (type=tool, tool=`question`) | Silently suppressed — handled by `question.asked` |
| `message.updated`, `session.updated`, `session.status`, `session.diff`, `server.connected`, `server.heartbeat`, `project.updated`, `question.replied` | Silently skipped (no events emitted) — `question.replied` is the ack after `POST /question/{id}/reply`; the resumed turn streams via normal `message.part` events |
| Any event with `error` in type or `error` in properties | `ERROR` |

**Text/reasoning buffering strategy:**
- Deltas are accumulated per `partID` in `_text_buffers`
- When the buffer contains a newline, everything up to and including the last newline is flushed as an event; the remainder stays buffered
- When the part finishes (`time.end` present), the buffer remainder is flushed
- This produces natural streaming without extra paragraph spacing from many small deltas

**Assistant-event coalescing (finalize, host-side):**
- Because OpenCode flushes assistant text on every newline, a multi-line markdown block (code fence, table, list) arrives as one `assistant` event per line. The web chat renders each `assistant` streaming event as its own markdown block, so a code fence split across events shatters into "empty code block / plain text / empty code block".
- `backend/app/services/sessions/message_service.py:_coalesce_assistant_events()` merges runs of **consecutive** `assistant` events (stopping at any other event type to preserve tool / webapp_action / attachment interleaving) and renumbers `event_seq` contiguously. It runs as the first step of the `stream_message_with_events` finalize block (before webapp_action splitting and attachment processing).
- Adapter-agnostic and applied only to the **persisted** trace (web re-render + A2A replay); the live stream still carries per-line deltas (clients concatenate). Claude Code emits whole text blocks per turn, so coalescing is a no-op there. Tests: `backend/tests/unit/test_coalesce_assistant_events.py`.

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

Bridge servers read `session_context.json` to get `backend_session_id` at call time. They prefer the absolute path in the `CINNA_SESSION_CONTEXT_PATH` env var (set by the adapter on the serve subprocess and inherited by the spawned bridges), falling back to the cwd-relative `session_context.json` for older environments. This indirection is required because, once sessions bind to `/app/workspace`, opencode spawns the bridges with `cwd = /app/workspace` rather than the runtime dir (see **Workspace project binding**). `task_server.py` reads context; `knowledge_server.py` is env-var-configured and does not.

- `knowledge_server.py` — exposes `query_integration_knowledge` tool; calls backend knowledge API
- `task_server.py` — exposes `add_comment`, `update_status`, `create_task`, `create_subtask`, `get_details`, `list_tasks` tools

MCP tool names visible to the agent follow the pattern `mcp__{server}__{tool}` (e.g., `mcp__agent_task__create_task`).

## Configuration

**Environment variables injected into container:**
- `SDK_ADAPTER_BUILDING` — SDK ID for building mode (e.g., `claude-code/anthropic`)
- `SDK_ADAPTER_CONVERSATION` — SDK ID for conversation mode
- `MODEL_BUILDING` — resolved building-mode model string (tier word for claude-code/anthropic; concrete ID otherwise). Computed by the host via `model_catalog.resolve_model`. Read by the claude-code adapter.
- `MODEL_CONVERSATION` — resolved conversation-mode model string (same semantics as `MODEL_BUILDING`)
- `DUMP_LLM_SESSION` — set to `true` to enable JSONL event logging for all adapters (Claude Code and OpenCode); log files are written to `{workspace}/logs/` with adapter-specific prefixes
- `OPENCODE_SKIP_UPDATE` — always set to `1` in subprocess env to suppress update prompts
- `OPENCODE_CONFIG` — (opencode subprocess env) absolute path to the per-mode `opencode.json` in the runtime dir; pins config loading since sessions bind to `/app/workspace` and opencode no longer discovers config from cwd
- `CINNA_SESSION_CONTEXT_PATH` — (opencode subprocess env, inherited by spawned MCP bridges) absolute path to the runtime-dir `session_context.json`; lets the bridges resolve it regardless of their `cwd`

**Settings file locations inside container:**
- `app/core/.claude/building_settings.json` — MiniMax building mode config
- `app/core/.claude/conversation_settings.json` — MiniMax conversation mode config
- `app/core/.opencode/building/opencode.json` — OpenCode building mode server config (port 4096)
- `app/core/.opencode/conversation/opencode.json` — OpenCode conversation mode server config (port 4097)

**OpenCode `opencode.json` fields (generated by `_generate_opencode_config_files`):**
- `$schema` — `"https://opencode.ai/config.json"`
- `model` — provider-qualified model string (e.g., `anthropic/claude-sonnet-4-5`, `openai/gpt-4o`); set from `model_override_*` if provided, else from per-provider mode defaults
- `instructions` — list with the absolute runtime-dir `AGENTS.md` path (`/tmp/.opencode_{mode}/AGENTS.md`); delivers the system prompt now that sessions bind to `/app/workspace` and cwd-based `AGENTS.md` discovery no longer applies (see **Workspace project binding**)
- `provider` — provider registration block; registers the selected model by ID so OpenCode accepts it even if it's not in OpenCode's built-in list; includes `options.apiKey` with the API key directly embedded (file permissions set to `0o600`)
- `permission` — wildcard allow `"*": "allow"` plus `external_directory` rules pre-approving `/app/workspace/**`, `/app/**`, `/tmp/**`
- `tools` — per-tool enable flags: `webfetch`, `websearch`, `bash`, `read`, `write`, `edit`, `glob`, `grep`, `list`, `patch`
- `mcp` — MCP bridge server entries (knowledge, task, collaboration); each has `type: "local"`, `command: ["python3", "..."]`, `enabled: true`
- `server` — `{"port": 4096, "hostname": "127.0.0.1"}` (building) or `{"port": 4097, ...}` (conversation)

**Default models per engine/provider per mode:**

Defaults are managed by the central model catalog (`backend/app/services/environments/model_catalog.py`),
which is the single source of truth. The table below reflects the catalog's current values:

| Engine | Provider | Building (BALANCED) | Conversation (FAST) |
|--------|----------|---------------------|---------------------|
| `claude-code` | `anthropic` | `sonnet` *(tier word)* | `haiku` *(tier word)* |
| `claude-code` | `minimax` | `MiniMax-M2.1` | `MiniMax-M2.1-lightning` |
| `opencode` | `anthropic` | `anthropic/claude-sonnet-4-6` | `anthropic/claude-haiku-4-5` |
| `opencode` | `openai` | `openai/gpt-5.4-mini` | `openai/gpt-5.4-nano` |
| `opencode` | `openai_compatible` | from credential config | from credential config |
| `opencode` | `google` | `google/gemini-2.5-pro` | `google/gemini-2.5-flash` |

`claude-code/anthropic` stores tier **words** (`haiku`/`sonnet`) — the Claude Code CLI auto-resolves
these to the current model and they are never flagged as deprecated. See
[model_freshness_tech.md](../agent_environments/model_freshness_tech.md) for catalog details.

**Per-mode model env vars (injected by the backend, consumed by the claude-code adapter):**

- `MODEL_BUILDING` — resolved building-mode model (tier word or concrete ID)
- `MODEL_CONVERSATION` — resolved conversation-mode model

These are computed in `_generate_env_file` via `resolve_model(engine, provider, mode, override)`
and forwarded into the container through all three docker-compose templates
(`general-env`, `python-env-advanced`, `platform-knowledge-env`) with `${MODEL_BUILDING:-}`
/ `${MODEL_CONVERSATION:-}` syntax (empty-string default for backward compatibility).

The claude-code adapter (`claude_code_sdk_adapter.py`) reads `MODEL_{MODE.upper()}` and calls
`options.model = model_value`. This fixed a latent bug where `model_override_*` was silently
ignored by the claude-code engine. OpenCode bakes its model into `opencode.json` at config
generation time and does not use these env vars.

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
