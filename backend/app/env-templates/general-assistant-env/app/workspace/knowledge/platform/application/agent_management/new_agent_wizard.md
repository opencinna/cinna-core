# New Agent Creation Wizard

## Purpose

Multi-step wizard that guides users through creating an agent with environment setup, SDK configuration, and optional credential sharing, using SSE streaming for real-time progress updates.

## Feature Overview

**Flow:**
1. User clicks "+ New Agent" badge on dashboard → switches to building mode
2. User configures SDK providers via cog icon dropdown (optional)
3. User enters agent description and sends
4. Backend creates agent, generates configuration via LLM
5. Backend builds and starts environment with selected SDKs
6. Frontend handles credential sharing and session creation
7. User redirected to new session

## Architecture

```
Dashboard UI → Agent Creation Route → Backend SSE → Environment Service → Agent-Env Container
(SDK Config)   (creating.tsx)         (create-flow)  (with SDK params)    (SDK-specific config)
```

**Configuration Flow:**
- Dashboard: SDK selection via dropdown → passed as search params
- Creating route: SDK params sent in SSE request body
- Backend: SDK params passed to environment creation
- Environment: SDK-specific settings files generated

## SDK Pre-Configuration

### Dashboard SDK Selector

**Location:** `frontend/src/routes/_layout/index.tsx`

When "+ New Agent" is selected:
- Mode switch replaced with Settings (cog) icon
- Cog opens dropdown with SDK configuration
- User can select different SDKs for conversation and building modes
- Selections passed to creation wizard via URL search params

**State:**
- `showSdkConfig`: Controls dropdown visibility
- `sdkConversation`: Selected SDK for conversation mode (default: `claude-code/anthropic`)
- `sdkBuilding`: Selected SDK for building mode (default: `claude-code/anthropic`)

**SDK Options:** Defined in `SDK_OPTIONS` constant
- `claude-code/anthropic` - Anthropic Claude
- `claude-code/minimax` - MiniMax M2
- `google-adk-wr/openai-compatible` - OpenAI Compatible

**API Key Validation:** Uses `getKeyStatus()` helper with `aiCredentialsStatus` query to show warnings for unconfigured SDKs

### Search Params

**Route:** `frontend/src/routes/_layout/agent/creating.tsx`
- `description`: Agent description text
- `mode`: "conversation" | "building"
- `sdkConversation`: Optional SDK for conversation mode
- `sdkBuilding`: Optional SDK for building mode

## Backend Components

**Agent Model:** `backend/app/models/agent.py:107`
- `AgentCreateFlowRequest`: Request schema with fields:
  - `description`, `mode`, `auto_create_session`, `user_workspace_id`
  - `agent_sdk_conversation`: SDK for conversation mode
  - `agent_sdk_building`: SDK for building mode

**Agent Service:** `backend/app/services/agent_service.py:119`
- `create_agent_flow()`: Async generator that yields progress events
- Accepts `agent_sdk_conversation` and `agent_sdk_building` parameters
- Passes SDK params to `AgentEnvironmentCreate` for environment setup
- Supports partial flows: when `auto_create_session=False`, stops after environment is ready
- Returns `agent_id` and `environment_id` in events for frontend state management

**Agent Routes:** `backend/app/api/routes/agents.py:150`
- `POST /agents/create-flow`: SSE endpoint streaming creation progress
  - Extracts SDK params from request and passes to service
- `POST /agents/{id}/credentials`: Endpoint for sharing credentials with agent

**SSE Event Schema**
The service yields events with these fields:
- `step`: Event type (creating_agent, agent_created, environment_starting, environment_ready, completed, error)
- `message`: Human-readable progress message
- `current_step`: Which UI step is active (create_agent, start_environment, share_credentials, create_session, redirect)
- `agent_id`, `environment_id`, `session_id`: Resource identifiers (when available)

### Frontend Components

**Dashboard:** `frontend/src/routes/_layout/index.tsx`
- New Agent badge triggers building mode with SDK config UI
- `handleAgentClick()`: Manages agent selection and SDK config visibility
- `handleSend()`: Navigates to creation wizard with SDK params

**Creation Wizard Route:** `frontend/src/routes/_layout/agent/creating.tsx`
Main component managing the entire wizard flow:
- Extracts `sdkConversation` and `sdkBuilding` from search params
- Sends SDK params in SSE request body to backend
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

## File Locations Reference

**Backend:**
- Models: `backend/app/models/agent.py` (AgentCreateFlowRequest with SDK fields)
- Service: `backend/app/services/agent_service.py:create_agent_flow()`
- Routes: `backend/app/api/routes/agents.py` (create-flow endpoint)
- Environment: `backend/app/models/environment.py` (AgentEnvironmentCreate with SDK fields)

**Frontend:**
- Dashboard: `frontend/src/routes/_layout/index.tsx` (SDK config dropdown, NEW_AGENT_ID handling)
- Creation Wizard: `frontend/src/routes/_layout/agent/creating.tsx` (SSE consumption, SDK param extraction)
- Client: Auto-generated from OpenAPI (`frontend/src/client/*`)

**Related SDK Configuration:**
- Environment Service: `backend/app/services/environment_service.py` (SDK validation, defaults)
- Environment Lifecycle: `backend/app/services/environment_lifecycle.py` (SDK settings file generation)
- User Settings: `frontend/src/components/UserSettings/AICredentials.tsx` (API key management)

## Related Documentation

- [Agent Environments](../../agents/agent_environments/agent_environments.md)
- [Agent Credentials](../../agents/agent_credentials/agent_credentials.md)
- [Multi-SDK](../../agents/agent_environment_core/multi_sdk.md)
- [Real-time Streaming](../../application/realtime_events/frontend_backend_agentenv_streaming.md)
