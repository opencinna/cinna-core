# New Agent Creation Wizard

## Overview

The New Agent Creation Wizard is a multi-step interface that guides users through creating an agent with environment setup and optional credential sharing. The wizard uses Server-Sent Events (SSE) streaming to provide real-time progress updates and allows users to select credentials to share with the agent before the first session starts.

## Architecture

### Backend Components

**Agent Model** (`backend/app/models/agent.py:83`)
- `AgentCreateFlowRequest`: Request schema with `description`, `mode`, and `auto_create_session` fields
- `auto_create_session` controls whether the flow stops after environment creation or continues to session creation

**Agent Service** (`backend/app/services/agent_service.py:113`)
- `create_agent_flow()`: Async generator that yields progress events
- Supports partial flows: when `auto_create_session=False`, stops after environment is ready
- Returns `agent_id` and `environment_id` in events for frontend state management

**Agent Routes** (`backend/app/api/routes/agents.py:93`)
- `POST /agents/create-flow`: SSE endpoint streaming creation progress
- `POST /agents/{id}/credentials`: Endpoint for sharing credentials with agent

**SSE Event Schema**
The service yields events with these fields:
- `step`: Event type (creating_agent, agent_created, environment_starting, environment_ready, completed, error)
- `message`: Human-readable progress message
- `current_step`: Which UI step is active (create_agent, start_environment, share_credentials, create_session, redirect)
- `agent_id`, `environment_id`, `session_id`: Resource identifiers (when available)

### Frontend Components

**Creation Wizard Route** (`frontend/src/routes/_layout/agent/creating.tsx`)
Main component managing the entire wizard flow with these responsibilities:
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

## Related Documentation

- Agent Environment Management: `docs/agent-sessions/agent_env_core.md`
- Credentials System: `docs/agent-sessions/agent_env_credentials_management.md`
- SSE Streaming: `docs/agent-sessions/frontend_backend_agentenv_streaming.md`
