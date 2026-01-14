# Multi-Agent SDK Management

## Purpose

Enable users to select different AI SDK providers (Anthropic Claude or MiniMax M2) per agent environment, with automatic configuration, API key management, and user-level default preferences.

## Feature Overview

**Flow:**
1. User saves API keys (Anthropic, MiniMax) in User Settings
2. User optionally sets default SDK preferences for conversation and building modes
3. User creates environment → SDK defaults populated from user preferences (can override per environment)
4. Backend validates user has required API keys for selected SDKs
5. Backend generates environment with SDK-specific configuration files
6. Agent-env detects settings files at runtime and configures SDK client accordingly

## Architecture

```
User Settings → Default SDK Prefs → Environment Creation → Env Generation → Agent-Env Runtime
(API Keys)      (User Defaults)     (SDK Selection)        (Settings Files)  (SDK Client Config)
```

**Configuration Locations:**
- **User API Keys:** `ai_service_credentials` table (encrypted)
- **User Default SDKs:** `user` table fields (`default_sdk_conversation`, `default_sdk_building`)
- **Environment SDK Selection:** `agent_environment` table fields
- **SDK Settings Files:** `{instance_dir}/app/core/.claude/` (auto-generated)

## Supported SDKs

| SDK ID | Display Name | Required User Key | Default |
|--------|-------------|-------------------|---------|
| `claude-code/anthropic` | Anthropic Claude | `anthropic_api_key` | Yes |
| `claude-code/minimax` | MiniMax M2 | `minimax_api_key` | No |

## SDK Configuration Strategy

### Anthropic SDK (Default)
- Uses standard `ANTHROPIC_API_KEY` environment variable in `.env` file
- No additional settings files required

### MiniMax SDK
- Settings files generated in `/app/core/.claude/` folder
- Files contain Anthropic-compatible API configuration pointing to MiniMax endpoint
- `ANTHROPIC_API_KEY` is NOT added to `.env` when MiniMax is selected (prevents conflicts)

**Settings File Structure:**
- `building_settings.json` - Used when agent is in building mode
- `conversation_settings.json` - Used when agent is in conversation mode
- Contains: `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`, model mappings

## Database Schema

**Migrations:**
- Phase 1: (minimax_api_key added to ai_service_credentials - existing)
- Phase 2: `backend/app/alembic/versions/776395044d2b_add_agent_sdk_fields_to_environment.py`
- Phase 3: `backend/app/alembic/versions/c8d9e0f1a2b3_add_default_sdk_fields_to_user.py`

**Models:**

**User:** `backend/app/models/user.py`
- `User` - Added `default_sdk_conversation`, `default_sdk_building` fields
- `UserUpdateMe` - Added SDK preference fields for user updates
- `UserPublic` - Exposes SDK defaults in API responses
- SDK Constants: `SDK_ANTHROPIC`, `SDK_MINIMAX`, `VALID_SDK_OPTIONS`

**User AI Credentials:** `backend/app/models/user.py`
- `AIServiceCredentials` - Added `minimax_api_key: str | None`
- `AIServiceCredentialsUpdate` - Added `minimax_api_key: str | None`
- `UserPublicWithAICredentials` - Added `has_minimax_api_key: bool`

**Environment:** `backend/app/models/environment.py`
- `AgentEnvironment` - Added `agent_sdk_conversation`, `agent_sdk_building`
- `AgentEnvironmentCreate` - Added SDK selection fields
- `AgentEnvironmentPublic` - Exposes SDK fields to frontend

## Backend Implementation

### API Routes

**User Settings:** `backend/app/api/routes/users.py`
- `GET /api/v1/users/me` - Returns user with `default_sdk_conversation`, `default_sdk_building`
- `PATCH /api/v1/users/me` - Updates user SDK defaults (validates against `VALID_SDK_OPTIONS`)
- `GET /api/v1/users/me/ai-credentials/status` - Returns `has_minimax_api_key` flag + SDK defaults
- `PATCH /api/v1/users/me/ai-credentials` - Accepts `minimax_api_key` for update

**Environment Creation:** `backend/app/api/routes/agents.py`
- `POST /api/v1/agents/{id}/environments` - Accepts `agent_sdk_conversation`, `agent_sdk_building`

### Services

**Environment Service:** `backend/app/services/environment_service.py`
- SDK Constants: `SDK_ANTHROPIC`, `SDK_MINIMAX`, `DEFAULT_SDK`, `VALID_SDK_OPTIONS`
- `SDK_API_KEY_MAP` - Maps SDK IDs to required API key field names
- `create_environment()` - Uses user's default SDK preferences, validates values, checks API keys

**Environment Lifecycle:** `backend/app/services/environment_lifecycle.py`
- `create_environment_instance()` - Accepts `minimax_api_key` parameter
- `_update_environment_config()` - Fetches API keys, calls env generation
- `_generate_env_file()` - Conditionally includes `ANTHROPIC_API_KEY`, calls settings generation
- `_generate_minimax_settings_files()` - Creates JSON settings in `app/core/.claude/`
- `rebuild_environment()` - Regenerates settings files after core replacement

### Configuration

**SDK Constants:** `backend/app/services/environment_service.py`
- `SDK_ANTHROPIC = "claude-code/anthropic"`
- `SDK_MINIMAX = "claude-code/minimax"`
- `VALID_SDK_OPTIONS` - List of allowed SDK values

## Frontend Implementation

### Components

**AI Credentials Settings:** `frontend/src/components/UserSettings/AICredentials.tsx`
- Two-column layout: API credentials (left) + Default SDK Preferences (right)
- Left card: Anthropic and MiniMax API key inputs with save/delete mutations
- Right card: Dropdowns for default conversation and building mode SDKs
- UI-level validation: Shows alert when selected SDK is missing required API key
- SDK options show "(API key required)" indicator when key not configured
- Helper: `SDK_OPTIONS` array, `getSDKDisplayName()`, `hasRequiredKey()`

**Add Environment Dialog:** `frontend/src/components/Environments/AddEnvironment.tsx`
- Dropdown selects for `agent_sdk_conversation` and `agent_sdk_building`
- Defaults populated from user's SDK preferences
- Validates user has required API keys before enabling create button
- Shows warning if keys are missing with link to settings

**Environment Card:** `frontend/src/components/Environments/EnvironmentCard.tsx`
- Displays SDK badges with icons: MessageCircle (conversation), Wrench (building)
- Shows "Anthropic" or "MiniMax" labels
- Helper: `getSDKDisplayName()` - Converts SDK ID to display name

### State Management

**AI Credentials Query:** `useQuery(["aiCredentialsStatus"])`
- Fetches `has_anthropic_api_key`, `has_minimax_api_key` flags
- Fetches `default_sdk_conversation`, `default_sdk_building` preferences
- Used by AICredentials for SDK preference display and AddEnvironment for defaults

**SDK Update Mutation:** `useMutation` in AICredentials
- Calls `UsersService.updateUserMe()` with SDK preference changes
- Invalidates both `aiCredentialsStatus` and `currentUser` queries

## Agent-Env Implementation

**SDK Manager:** `backend/app/env-templates/python-env-advanced/app/core/server/sdk_manager.py`
- Detects settings file based on mode (`building_settings.json` or `conversation_settings.json`)
- Settings file path: `/app/core/.claude/{mode}_settings.json`
- Sets `options.settings` property in `ClaudeAgentOptions` if file exists
- Falls back to default behavior (env var) if no settings file

**Detection Logic:**
1. Determine mode from `send_message_stream()` parameter
2. Build settings file path: `/app/core/.claude/{mode}_settings.json`
3. Check `Path.exists()` for settings file
4. If exists: set `options.settings = str(settings_file_path)`

## Security Features

**Validation:**
- SDK values validated against `VALID_SDK_OPTIONS` list
- User must have required API key before environment creation
- API keys stored encrypted in `ai_service_credentials` table

**Access Control:**
- API keys only accessible to owning user
- Settings files generated per-environment with user's own keys
- SDK selection immutable after environment creation

## Key Integration Points

**Environment Creation Flow:** `backend/app/services/environment_service.py:create_environment()`
1. Get SDK values from request, or fall back to user's defaults, or global default
2. Validate SDK values in allowed list
3. Check user has required API keys via `SDK_API_KEY_MAP`
4. Create environment record with SDK fields
5. Pass API keys to background task

**Env Generation Flow:** `backend/app/services/environment_lifecycle.py:_generate_env_file()`
1. Determine which SDKs are used (conversation, building)
2. If Anthropic used: include `ANTHROPIC_API_KEY` in `.env`
3. If MiniMax used: call `_generate_minimax_settings_files()`
4. Write SDK identifiers to `.env` for reference

**Rebuild Flow:** `backend/app/services/environment_lifecycle.py:rebuild_environment()`
1. Core files replaced from template (deletes `.claude/` folder)
2. After rebuild: regenerate settings files if MiniMax is used
3. Fetch API key from user credentials
4. Call `_generate_minimax_settings_files()`

**Runtime Detection:** `sdk_manager.py:send_message_stream()`
1. Build `ClaudeAgentOptions` with standard config
2. Check for settings file at `/app/core/.claude/{mode}_settings.json`
3. If exists: set `options.settings` property
4. SDK uses settings file to override base URL and auth

## File Locations Reference

**Backend:**
- Models: `backend/app/models/user.py`, `backend/app/models/environment.py`
- Services: `backend/app/services/environment_service.py`, `backend/app/services/environment_lifecycle.py`
- Routes: `backend/app/api/routes/users.py`, `backend/app/api/routes/agents.py`
- Migrations:
  - `backend/app/alembic/versions/776395044d2b_add_agent_sdk_fields_to_environment.py`
  - `backend/app/alembic/versions/c8d9e0f1a2b3_add_default_sdk_fields_to_user.py`

**Frontend:**
- Components: `frontend/src/components/UserSettings/AICredentials.tsx`, `frontend/src/components/Environments/AddEnvironment.tsx`, `frontend/src/components/Environments/EnvironmentCard.tsx`
- Client: Auto-generated from OpenAPI (`frontend/src/client/*`)

**Agent-Env:**
- SDK Manager: `backend/app/env-templates/python-env-advanced/app/core/server/sdk_manager.py`
- Settings Location: `/app/core/.claude/building_settings.json`, `/app/core/.claude/conversation_settings.json`

## Constraints

- SDK selection is **immutable** after environment creation
- Empty SDK fields default to user's preferences, then `claude-code/anthropic` for backward compatibility
- User must have valid API key before creating environment with that SDK
- MiniMax uses Anthropic-compatible API format (same client, different base URL)
- Settings files are regenerated after environment rebuild

---

**Document Version:** 1.1
**Last Updated:** 2026-01-14
**Status:** Fully Implemented (Phase 1 + Phase 2 + Phase 3: User Default SDK Preferences)
