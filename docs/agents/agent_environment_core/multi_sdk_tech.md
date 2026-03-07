# Multi-SDK Support — Technical Reference

## File Locations

**Backend — Models:**
- `backend/app/models/user.py` — `User`, `UserUpdateMe`, `UserPublic` (SDK default fields), `AIServiceCredentials`, `AIServiceCredentialsUpdate`, `UserPublicWithAICredentials`
- `backend/app/models/environment.py` — `AgentEnvironment`, `AgentEnvironmentCreate`, `AgentEnvironmentPublic` (SDK selection fields)

**Backend — Services:**
- `backend/app/services/environment_service.py` — SDK constants, default cascade logic, credential validation
- `backend/app/services/environment_lifecycle.py` — env file generation, settings file generation, rebuild regeneration

**Backend — Routes:**
- `backend/app/api/routes/users.py` — AI credential endpoints, SDK default update
- `backend/app/api/routes/agents.py` — environment creation with SDK fields

**Migrations:**
- `backend/app/alembic/versions/776395044d2b_add_agent_sdk_fields_to_environment.py`
- `backend/app/alembic/versions/c8d9e0f1a2b3_add_default_sdk_fields_to_user.py`

**Frontend — Components:**
- `frontend/src/components/UserSettings/AICredentials.tsx`
- `frontend/src/components/Environments/AddEnvironment.tsx`
- `frontend/src/components/Environments/EnvironmentCard.tsx`

**Frontend — Client:**
- `frontend/src/client/` (auto-generated OpenAPI types and service classes)

**Agent Environment (inside container):**
- `backend/app/env-templates/app_core_base/core/server/sdk_manager.py`
- `backend/app/env-templates/app_core_base/core/server/adapters/base.py`
- `backend/app/env-templates/app_core_base/core/server/adapters/claude_code.py`
- `backend/app/env-templates/app_core_base/core/server/adapters/google_adk.py`

## Database Schema

**User table** (`backend/app/models/user.py`):
- `default_sdk_conversation` — nullable string, SDK ID for conversation mode default
- `default_sdk_building` — nullable string, SDK ID for building mode default
- `ai_credentials_encrypted` — encrypted JSON blob containing all AI provider credentials (no migration needed for new credential fields)

**AgentEnvironment table** (`backend/app/models/environment.py`):
- `agent_sdk_conversation` — string, SDK ID selected at creation, immutable
- `agent_sdk_building` — string, SDK ID selected at creation, immutable

**Schema constants** (`backend/app/models/user.py`):
- `SDK_ANTHROPIC`, `SDK_MINIMAX`, `SDK_OPENAI_COMPATIBLE`, `VALID_SDK_OPTIONS`

## API Endpoints

**User credentials and SDK defaults** (`backend/app/api/routes/users.py`):
- `GET /api/v1/users/me` — returns `default_sdk_conversation`, `default_sdk_building`
- `PATCH /api/v1/users/me` — updates SDK default fields; validates against `VALID_SDK_OPTIONS`
- `GET /api/v1/users/me/ai-credentials/status` — boolean key presence flags + SDK defaults
- `GET /api/v1/users/me/ai-credentials` — full credentials including `openai_compatible_base_url`, `openai_compatible_model`
- `PATCH /api/v1/users/me/ai-credentials` — updates credential fields (partial update, non-empty fields only)

**Environment creation** (`backend/app/api/routes/agents.py`):
- `POST /api/v1/agents/{id}/environments` — accepts `agent_sdk_conversation`, `agent_sdk_building`

## Services & Key Methods

**`backend/app/services/environment_service.py`:**
- `SDK_API_KEY_MAP` — maps SDK ID to required credential field name
- `create_environment()` — applies default SDK cascade, validates SDK values, checks user credentials, passes all credential params to background task

**`backend/app/services/environment_lifecycle.py`:**
- `create_environment_instance()` — accepts all credential params including OpenAI Compatible fields
- `_update_environment_config()` — fetches user credentials and triggers env file generation
- `_generate_env_file()` — writes `.env`; conditionally includes `ANTHROPIC_API_KEY`; calls settings generators for MiniMax and OpenAI Compatible
- `_generate_minimax_settings_files()` — writes JSON settings to `app/core/.claude/`
- `_generate_openai_compatible_settings_files()` — writes JSON settings to `app/core/.google-adk/`
- `rebuild_environment()` — after core replacement, regenerates settings files for both adapter types

## Frontend Components

**`frontend/src/components/UserSettings/AICredentials.tsx`:**
- Three-section layout: Cloud AI Services (Anthropic + MiniMax keys), Default SDK Preferences (dropdowns), OpenAI Compatible (base URL + key + model)
- `SDK_OPTIONS` array, `getSDKDisplayName()`, `hasRequiredKey()` helpers
- Shows "(API key required)" indicator when key not configured for a given option

**`frontend/src/components/Environments/AddEnvironment.tsx`:**
- SDK dropdowns for conversation and building modes
- Pre-filled from `aiCredentialsStatus.default_sdk_*`
- Disables create button if required keys are missing; shows link to settings

**`frontend/src/components/Environments/EnvironmentCard.tsx`:**
- SDK badges with MessageCircle (conversation) and Wrench (building) icons
- `getSDKDisplayName()` converts SDK ID to display label

**React Query hooks:**
- `useQuery(["aiCredentialsStatus"])` — boolean flags + SDK defaults; used by AICredentials and AddEnvironment
- `useQuery(["aiCredentials"])` — full credentials for pre-populating OpenAI Compatible fields
- `useMutation` in AICredentials — SDK preference update (invalidates `aiCredentialsStatus` + `currentUser`)
- `useMutation` in AICredentials — OpenAI Compatible update (sends non-empty fields only; invalidates `aiCredentialsStatus` + `aiCredentials`)

## Adapter Architecture (inside container)

**`adapters/base.py`:**
- `SDKConfig` — parses `SDK_ADAPTER_{MODE}` env var; splits provider/variant (e.g., `claude-code/anthropic`)
- `BaseSDKAdapter` — abstract base; all adapters must implement `send_message_stream()`
- `AdapterRegistry` — maps SDK prefix to adapter class; `create_adapter(config)` instantiates correct adapter
- `SDKEvent` + `SDKEventType` — unified event format produced by all adapters

**`adapters/claude_code.py` — `ClaudeCodeAdapter`:**
- Handles `claude-code/*` variants (anthropic, minimax)
- Settings file detection: checks `/app/core/.claude/{mode}_settings.json`; if present, passes path via `options.settings`
- Falls back to `ANTHROPIC_API_KEY` env var if no settings file found

**`adapters/google_adk.py` — `GoogleADKAdapter`:**
- Handles `google-adk-wr/*` variants (openai-compatible, gemini, vertex)
- `_load_settings_for_mode(mode)` — reads `/app/core/.google-adk/{mode}_settings.json`
- `_get_openai_compatible_config(mode)` — extracts `api_key`, `base_url`, `model` from settings
- Gemini and Vertex providers are placeholders

### SDKEvent Fields

`SDKEvent` dataclass (`adapters/base.py`):
- `type: SDKEventType` — `SESSION_CREATED`, `ASSISTANT`, `TOOL_USE`, `THINKING`, `DONE`, `INTERRUPTED`, `ERROR`
- `content: str` — human-readable message
- `session_id: str` — SDK session identifier
- `metadata: dict` — event-specific additional data
- `tool_name: str` — populated for `TOOL_USE` events
- `error_type: str` — populated for `ERROR` events

## Configuration

**Environment variables injected into container:**
- `SDK_ADAPTER_BUILDING` — SDK ID for building mode (e.g., `claude-code/anthropic`)
- `SDK_ADAPTER_CONVERSATION` — SDK ID for conversation mode

**Settings file locations inside container:**
- `app/core/.claude/building_settings.json` — MiniMax building mode config
- `app/core/.claude/conversation_settings.json` — MiniMax conversation mode config
- `app/core/.google-adk/building_settings.json` — OpenAI Compatible building mode config
- `app/core/.google-adk/conversation_settings.json` — OpenAI Compatible conversation mode config

**MiniMax settings file fields:** `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`, model mappings

**OpenAI Compatible settings file fields:** `providers.openai-compatible.api_key`, `providers.openai-compatible.base_url`, `providers.openai-compatible.model`

## Security

- SDK values validated against `VALID_SDK_OPTIONS` before database write
- User must have required credentials before environment creation — checked via `SDK_API_KEY_MAP`
- AI credentials stored encrypted in `ai_credentials_encrypted` JSON blob; decrypted only during env generation
- Settings files are generated per-environment with the owning user's keys
- SDK selection is immutable post-creation — no runtime SDK switching
- API keys not accessible across users; credentials fetched by user ID in service layer
