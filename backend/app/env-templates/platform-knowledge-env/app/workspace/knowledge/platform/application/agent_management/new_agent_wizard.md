# New Agent Creation Wizard

## Purpose

Multi-step wizard that guides users through creating an agent with environment setup, SDK configuration, and optional credential sharing, using SSE streaming for real-time progress updates.

## Feature Overview

**Flow:**
1. User clicks "+ New Agent" badge on dashboard → switches to building mode
2. User optionally clicks the cog (Settings) icon to open the environment pre-configuration Modal Dialog
3. Inside the dialog, user configures: environment template, SDK engine per mode, AI credential per mode, and optional model override per mode
4. User enters agent description and sends
5. Dashboard composes SDK IDs and forwards all configuration as URL search params to the creation wizard route
6. Backend creates agent, generates configuration via LLM
7. Backend builds and starts the default environment using the pre-configured settings
8. Frontend handles credential sharing and session creation
9. User redirected to new session

## Architecture

```
Dashboard UI → Agent Creation Route → Backend SSE → Environment Service → Agent-Env Container
(Env Config    (creating.tsx)         (create-flow)  (with all env       (SDK-specific config,
 Modal Dialog)                                         preconfig params)   model overrides)
```

**Configuration Flow:**
- Dashboard: env-config Modal Dialog (template + per-mode SDK/credential/model) → selections forwarded as URL search params
- Creating route: all env-preconfig params sent in SSE request body (camelCase → snake_case)
- Backend: all preconfig fields passed to `AgentEnvironmentCreate` for the default environment
- Environment: SDK-specific settings files generated with selected engine, credential, and model override

## SDK Pre-Configuration

### Dashboard SDK Selector

**Location:** `frontend/src/routes/_layout/index.tsx`

When "+ New Agent" is selected:
- Mode switch is replaced with a Settings (cog) icon
- Clicking the cog opens a Modal Dialog containing the full environment configuration form — the same shared component (`EnvironmentConfigForm`) used by `AddEnvironment.tsx` on an agent's Environments tab

**Form fields inside the dialog:**
- **Environment Template** — a 2-column card picker showing both templates side by side. Each card displays a lucide icon, a bold title, and a one-line description so the user can distinguish them without opening a dropdown. Cards: `python-env-advanced` "Python" (slim Python image, fast builds, pure-Python work) and `general-env` "General Purpose" (full Debian, supports system packages such as ffmpeg and sqlite). The selected card gets a highlighted border and background.
- **Conversation mode summary row** — shows selected engine and credential; clicking the edit (pencil) icon opens an `EnvModeEditDialog` sub-dialog
- **Building mode summary row** — same structure, independent settings; clicking edit opens its own `EnvModeEditDialog`

**Per-mode sub-dialog (`EnvModeEditDialog`) fields:**
- SDK Engine — `claude-code` (Anthropic's CLI agent SDK) or `opencode` (multi-provider, 75+ providers)
- AI Credential — filtered by `SDK_CREDENTIAL_COMPATIBILITY`; "Default" option uses the account default resolved at environment build time; explicit credential selection pins a specific credential UUID
- Model Override — optional free-text field (with browser-native datalist suggestions per credential type); left empty means the SDK uses its own default for that mode

**State:**
- `envConfigOpen`: Controls Modal Dialog visibility
- `envConfig: EnvConfigValue`: Full configuration value holding `envName`, `sdkEngineConversation`, `conversationCredentialId`, `modelOverrideConversation`, `sdkEngineBuilding`, `buildingCredentialId`, and `modelOverrideBuilding`
- `envNameTouched`: Boolean (default `false`) that records whether the user explicitly clicked a template card in the current "+ New Agent" flow. Reset to `false` in `handleAgentClick` each time the user enters a fresh flow. Only when this is `true` does the dashboard include `envName` in the navigation search params.
- The form is seeded from `aiCredentialsStatus` defaults each time the dialog is opened (re-seeds on every open, not just the first)

**Credential Filtering:** Credential dropdown is filtered by `SDK_CREDENTIAL_COMPATIBILITY` — only credentials whose type is compatible with the selected engine are shown. Compatible types: `claude-code` → `anthropic`, `minimax`; `opencode` → `anthropic`, `openai`, `openai_compatible`, `google`.

**Model Override:** Optional text field per mode for explicit model selection (e.g., `gpt-4o-mini`, `claude-opus-4`). Left empty means the adapter uses its default for that mode.

**SDK ID composition:** On Send, `composeSDKId(engine, credential)` combines the selected engine and credential type into the wire format (e.g., `claude-code/anthropic`, `opencode/openai`) that the backend stores as the environment's SDK setting for each mode.

### Search Params

**Route:** `frontend/src/routes/_layout/agent/creating.tsx`
- `description`: Agent description text
- `mode`: "conversation" | "building"
- `sdkConversation`: Optional SDK ID for conversation mode (composed via `composeSDKId`)
- `sdkBuilding`: Optional SDK ID for building mode
- `envName`: Optional environment template name (e.g., `general-env`). The dashboard only forwards this param when the user has explicitly clicked a template card in the cog Dialog — internally tracked via `envNameTouched` state that starts `false` and resets to `false` each time the user enters a fresh "+ New Agent" flow. When the param is absent (user never touched the picker), TanStack Router drops it from the URL entirely and the backend falls back to `settings.DEFAULT_AGENT_ENV_NAME` as the single source of truth. The `AddEnvironment.tsx` consumer of the shared form does not use this mechanism and always sends `envName`.
- `modelOverrideConversation`: Optional model override string for conversation mode
- `modelOverrideBuilding`: Optional model override string for building mode
- `useDefaultAiCredentials`: Boolean — when `true`, the backend resolves the user's account defaults; when `false`, the explicit credential ID params below govern. `validateSearch` uses a `coerceBool` helper that accepts both native boolean values (from programmatic navigation) and `"true"`/`"false"` strings (from URL reloads)
- `conversationAiCredentialId`: Optional explicit AI credential UUID for conversation mode; omitted when `useDefaultAiCredentials` is `true` or the conversation credential is left at "Default"
- `buildingAiCredentialId`: Optional explicit AI credential UUID for building mode; same omission rule

## Backend Components

**Agent Model:** `backend/app/models/agents/agent.py`
- `AgentCreateFlowRequest`: Request schema with fields:
  - `description`, `mode`, `auto_create_session`, `user_workspace_id`
  - `agent_sdk_conversation`: SDK for conversation mode (e.g., `"claude-code/anthropic"`)
  - `agent_sdk_building`: SDK for building mode
  - `env_name: str | None = None` — environment template name (e.g., `"general-env"`); `None` causes the service to fall back to `settings.DEFAULT_AGENT_ENV_NAME`
  - `model_override_conversation: str | None = None` — optional per-mode model override (e.g., `"claude-haiku-4-5"`); `None` or empty leaves the SDK default in place
  - `model_override_building: str | None = None` — same for building mode
  - `use_default_ai_credentials: bool = True` — when `True` (the default), the environment uses the user's account-default AI credentials; when `False`, the explicit `*_ai_credential_id` fields below pin specific credentials
  - `conversation_ai_credential_id: uuid.UUID | None = None` — explicit AI credential UUID for conversation mode
  - `building_ai_credential_id: uuid.UUID | None = None` — explicit AI credential UUID for building mode

**Agent Service:** `backend/app/services/agents/agent_service.py`
- `create_agent_flow()`: Async generator that yields progress events
- Accepts `agent_sdk_conversation`, `agent_sdk_building`, `env_name`, `model_override_conversation`, `model_override_building`, `use_default_ai_credentials`, `conversation_ai_credential_id`, and `building_ai_credential_id` parameters
- All new kwargs are passed directly into the `AgentEnvironmentCreate` it constructs for the default environment; `env_name` falls back to `settings.DEFAULT_AGENT_ENV_NAME` when `None`
- Supports partial flows: when `auto_create_session=False`, stops after environment is ready
- Returns `agent_id` and `environment_id` in events for frontend state management

**Agent Routes:** `backend/app/api/routes/agents.py`
- `POST /agents/create-flow`: SSE endpoint streaming creation progress
  - Extracts all fields from `AgentCreateFlowRequest` — including all six new env-preconfig fields — and forwards them to `AgentService.create_agent_flow()`
  - Restricted to `agent-developer` and `admin` roles (`require_developer` dependency)
- `POST /agents/{id}/credentials`: Endpoint for sharing credentials with agent

**SSE Event Schema**
The service yields events with these fields:
- `step`: Event type (creating_agent, agent_created, environment_starting, environment_ready, completed, error)
- `message`: Human-readable progress message
- `current_step`: Which UI step is active (create_agent, start_environment, share_credentials, create_session, redirect)
- `agent_id`, `environment_id`, `session_id`: Resource identifiers (when available)

### Frontend Components

**Dashboard:** `frontend/src/routes/_layout/index.tsx`
- New Agent badge triggers building mode with env-config UI
- Cog (Settings) icon opens a Modal Dialog hosting `EnvironmentConfigForm`; dialog state is `envConfigOpen` / `envConfig`
- `handleAgentClick()`: Manages agent selection and env-config dialog visibility
- `handleSend()`: Composes SDK IDs via `composeSDKId`, resolves credential flags, and navigates to creation wizard with all env-preconfig search params; gates `envName` on `envNameTouched` (omits it when the user never explicitly picked a template, deferring to the backend default)

**Creation Wizard Route:** `frontend/src/routes/_layout/agent/creating.tsx`
Main component managing the entire wizard flow:
- Extracts `sdkConversation`, `sdkBuilding`, `envName`, `modelOverrideConversation`, `modelOverrideBuilding`, `useDefaultAiCredentials`, `conversationAiCredentialId`, and `buildingAiCredentialId` from search params
- Sends all params in SSE request body to backend (camelCase search params → snake_case JSON fields)
- SSE event consumption and state updates
- Credential selection UI
- Post-environment flow orchestration (credential sharing, session creation)
- Countdown/manual start logic

**State Management**
The wizard maintains several pieces of state:
- `steps`: Array of step objects with id, label, status, and optional message
- `selectedCredentialIds`: Set of credential IDs to share
- `agentId`, `environmentReady`, `sessionId`: Flow control flags
- `countdown`, `isCountingDown`: Redirect timer state

**Service Integration**
- `CredentialsService.readCredentials()`: Fetch user's available credentials
- `AgentsService.addCredentialToAgent()`: Share selected credentials
- `SessionsService.createSession()`: Create session after credential sharing
- `UsersService.getAiCredentialsStatus()`: Check available API keys for SDK validation

## Flow Architecture

### Phase 1: Backend-Controlled (SSE Stream)
1. User submits description and mode
2. Backend creates agent and generates configuration
3. Backend builds and starts environment
4. Backend yields "environment_ready" event with agent_id
5. SSE stream completes

### Phase 2: Frontend-Controlled (Post-Environment)
Triggered when `environmentReady=true` and `agentId` is set:

1. **Credential Sharing** (if credentials selected)
   - Iterates through `selectedCredentialIds`
   - Calls `AgentsService.addCredentialToAgent()` for each
   - Updates step status with count of shared credentials

2. **Session Creation**
   - Calls `SessionsService.createSession()` with agent_id and mode
   - Sets `sessionId` state

3. **Redirect Logic**
   - No credentials selected → 5-second countdown with "Start Now" skip option
   - Credentials selected → "Start Session" button (no countdown)

### Phase 3: Redirect
Navigate to `/session/$sessionId` with `initialMessage` query parameter

## UI Components

### Progress Steps
Visual indicator showing 5 steps:
- Creating agent
- Starting default environment
- Sharing selected credentials
- Creating conversation session
- Redirecting to session

Each step has status: pending, in_progress, completed, error

### Credential Selection Panel
Shown only when:
- User has credentials (`credentialsData?.data.length > 0`)
- Environment is not yet ready (`!environmentReady`)

Features:
- Checkbox list of available credentials
- Shows credential name, notes, and type
- Counter showing number of selected credentials
- Selection state persists until environment is ready

### Redirect Controls
Adaptive button behavior based on credential selection:
- **Auto-countdown mode**: When no credentials selected, shows "Starting session in X seconds..." with "Start Now" button
- **Manual mode**: When credentials selected, shows "Start Session" button with confirmation message

## Extension Points for LLMs

### Adding New Wizard Steps

**Backend Extension**
To add steps between environment creation and session creation:
1. Modify `create_agent_flow()` to yield additional events before session creation
2. Add new event types to the switch statement in the frontend SSE handler
3. Update `steps` initial state array with new step definitions

**Frontend Extension**
To add UI elements or validation before session start:
1. Add new state variables for validation/data collection
2. Insert new conditional rendering blocks between credential selection and redirect
3. Update `handlePostEnvironmentFlow()` to include new async operations
4. Modify redirect logic conditions to account for new requirements

### Adding Pre-Flight Validations

Add checks in `handlePostEnvironmentFlow()` before credential sharing:
- Check agent configuration requirements
- Validate user permissions
- Verify environment health

### Customizing Credential Sharing

Current implementation shares all selected credentials sequentially. To modify:
- Change iteration in credential sharing loop
- Add filtering based on credential type or agent requirements
- Implement batched sharing or parallel API calls
- Add credential validation before sharing

### Modifying Redirect Behavior

The countdown vs. manual button logic can be customized:
- Change countdown duration by modifying initial `countdown` state
- Add additional conditions for auto-redirect (e.g., agent type, user preferences)
- Implement skip-countdown preference storage
- Add intermediate confirmation steps

### Adding Rollback Support

To add error recovery:
1. Track created resources (agent_id, environment_id) in state
2. Add cleanup handlers in error catch blocks
3. Call appropriate deletion endpoints for created resources
4. Update error UI to show rollback status

## Key Design Patterns

### Separation of Concerns
- Backend controls resource creation (agent, environment)
- Frontend controls user interaction (credentials, session timing)
- Clean handoff at environment_ready event

### Progressive Enhancement
- Wizard works without credentials (original flow)
- Credentials are optional enhancement
- No breaking changes to existing flows

### State-Driven UI
- UI sections conditionally render based on state flags
- No imperative DOM manipulation
- Clear dependencies between phases

### Error Resilience
- Credential sharing failures don't block session creation
- Individual credential errors logged but flow continues
- User can manually retry failed steps

## Common Customization Scenarios

### Scenario 1: Add Environment Variable Configuration
**Location**: After credential selection, before session creation
**Implementation**:
- Add environment variable input form to UI
- Store in new state variable
- Pass to session creation or environment update endpoint

### Scenario 2: Agent Template Selection
**Location**: Replace description-based generation
**Implementation**:
- Add template selection UI before wizard starts
- Pass template_id instead of description to create-flow
- Backend uses template to populate agent configuration

### Scenario 3: Team/Permission Assignment
**Location**: After agent creation, before environment start
**Implementation**:
- Backend pauses after agent_created event
- Frontend shows team selection UI
- Call agent update endpoint with team assignments
- Resume environment creation

### Scenario 4: Custom Welcome Message
**Location**: Replace countdown/button with chat interface
**Implementation**:
- Show mini-chat widget during countdown
- Let user type custom first message
- Replace `initialMessage` query param with typed message
- Skip countdown entirely

## Testing Considerations

When extending the wizard, test these scenarios:
1. User with no credentials (should work as before)
2. User with credentials who selects none (5-second countdown)
3. User with credentials who selects some (manual button)
4. Credential sharing API failures (should continue)
5. Session creation failures (should show error)
6. Browser refresh during creation (SSE will fail - handle gracefully)
7. Network interruptions (SSE timeout handling)
8. SDK selection with missing API key (should show warning)
9. Different SDK combinations for conversation vs building modes
10. Env-config dialog opens with defaults seeded from `aiCredentialsStatus` (re-seeds on every open)
11. Explicit AI credential IDs are omitted from search params when `useDefaultAiCredentials` is `true`
12. `useDefaultAiCredentials` round-trips correctly across a page reload (string `"true"`/`"false"` via `coerceBool`)
13. `general-env` template selected in dialog results in correct `env_name` passed to backend
14. Model override fields left empty result in backend using SDK defaults (not empty strings)

## File Locations Reference

**Backend:**
- Models: `backend/app/models/agents/agent.py` (`AgentCreateFlowRequest` with all env-preconfig fields)
- Service: `backend/app/services/agents/agent_service.py` (`create_agent_flow()` with all new kwargs)
- Routes: `backend/app/api/routes/agents.py` (create-flow endpoint — `create_agent_with_flow`)
- Environment: `backend/app/models/environments/environment.py` (`AgentEnvironmentCreate` with SDK, credential, and model override fields)

**Frontend:**
- Dashboard: `frontend/src/routes/_layout/index.tsx` (env-config Modal Dialog, NEW_AGENT_ID handling)
- Creation Wizard: `frontend/src/routes/_layout/agent/creating.tsx` (SSE consumption, all env-preconfig param extraction)
- Shared env-config form: `frontend/src/components/Environments/EnvironmentConfigForm.tsx` (exports `EnvironmentConfigForm`, `EnvModeEditDialog`, `EnvConfigValue`, `composeSDKId`, constants)
- Env tab consumer: `frontend/src/components/Environments/AddEnvironment.tsx` (agent Environments tab — uses shared form; no behavior change)
- Client: Auto-generated from OpenAPI (`frontend/src/client/*`)

**Related SDK Configuration:**
- Environment Service: `backend/app/services/environments/environment_service.py` (SDK validation, defaults)
- Environment Lifecycle: `backend/app/services/environments/environment_lifecycle.py` (SDK settings file generation)
- User Settings: `frontend/src/components/UserSettings/AICredentials.tsx` (API key management)

## Related Documentation

- [Agent Environments](../../agents/agent_environments/agent_environments.md)
- [Agent Credentials](../../agents/agent_credentials/agent_credentials.md)
- [Multi-SDK](../../agents/agent_environment_core/multi_sdk.md)
- [Real-time Streaming](../../application/realtime_events/frontend_backend_agentenv_streaming.md)
