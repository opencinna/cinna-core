# A2A Protocol Integration

## Purpose

Enables external agents and A2A-compatible tools to discover and communicate with platform agents through the standardized Agent-to-Agent (A2A) protocol.

## Feature Overview

1. External client requests AgentCard for discovery
2. Client sends JSON-RPC message to agent endpoint
3. Backend creates/retrieves session (task) and forwards message
4. Agent environment processes message and streams response
5. Events are mapped to A2A format and returned via SSE or synchronous response

## Architecture

```
A2A Client → A2A Router → A2A Request Handler → Session/Message Services → Agent Environment
                ↓
         A2A Service (AgentCard)
                ↓
         A2A Event Mapper (Internal → A2A events, centralized mapping)
                ↓
         A2A Task Store (Session → Task mapping via service layer)
```

### Service Layer Architecture

All A2A components access data exclusively through the service layer:

```
A2A Request Handler ──┬──→ SessionService.send_session_message() (creates session + message)
                      ├──→ SessionService.get_session() (scope validation)
                      ├──→ SessionService.list_environment_sessions() (task listing)
                      └──→ MessageService (message streaming)

A2A Task Store ───────┬──→ SessionService.get_session()
                      ├──→ MessageService.get_last_message()
                      ├──→ MessageService.get_last_n_messages()
                      └──→ A2AEventMapper (all A2A conversions)

A2A Event Mapper ─────────→ Centralized A2A protocol mapping logic
```

**Key Principle:** No direct database queries in A2A code. All data access goes through `SessionService` and `MessageService`.

## A2A Protocol v1.0 Support

The implementation supports both the current a2a library format (v0.3.0 draft) and the A2A Protocol v1.0 specification through an adapter layer.

### Protocol Version Selection

| Header | Behavior |
|--------|----------|
| (none) | v1.0 format (default) |
| `X-A2A-Stable: 1` | Legacy format (v0.3.0 draft) |

### Key Transformations

**Method Names (Inbound):**

| v1.0 Method | Internal Method |
|-------------|-----------------|
| `SendMessage` | `message/send` |
| `SendStreamingMessage` | `message/stream` |
| `GetTask` | `tasks/get` |
| `CancelTask` | `tasks/cancel` |
| `ListTasks` | `tasks/list` |

**AgentCard (Outbound):**

| v0.3.0 Field | v1.0 Field |
|--------------|------------|
| `protocolVersion` (string) | `protocolVersions` (array) |
| `url` (string) | `supportedInterfaces` (array of `{url, transport}`) |
| `supportsAuthenticatedExtendedCard` | `capabilities.extendedAgentCard` |

**Task/Message Responses:**
- v1.0 responses include `"kind": "task"` or `"kind": "message"` discriminator field

### Adapter Implementation

**File:** `backend/app/services/a2a_v1_adapter.py`

- `A2AV1Adapter.should_use_v1(request)` - Check header for protocol version
- `A2AV1Adapter.transform_request_inbound(body)` - Transform v1.0 method names
- `A2AV1Adapter.transform_agent_card_outbound(card)` - Transform AgentCard structure
- `A2AV1Adapter.transform_task_outbound(task)` - Add `kind` discriminator

See **[a2a_v1_support.md](./a2a_v1_support.md)** for detailed specification.

## Data/State Lifecycle

### A2A Concepts to Internal Model Mapping

| A2A Concept | Internal Model | Notes |
|-------------|----------------|-------|
| Task | Session | One-to-one mapping |
| Task.id | Session.id | Direct UUID mapping |
| Task.context_id | Session.id | Same as task_id (Phase 1) |
| Message | SessionMessage | Message within task/session |
| AgentCard.skills | Agent.a2a_config["skills"] | Pre-generated on workflow_prompt update |

### Task State Mapping

| A2A TaskState | Internal State |
|---------------|----------------|
| submitted | interaction_status='pending_stream' |
| working | interaction_status='running' |
| completed | status='completed' |
| input-required | tool_questions_status='unanswered' |
| canceled | status='error' + interrupted |
| failed | status='error' |

### Skills Generation Lifecycle

1. User updates agent's `workflow_prompt`
2. `AgentService.update_agent()` detects change
3. Calls `generate_a2a_skills()` with new prompt
4. AI extracts skills and stores in `Agent.a2a_config`
5. Version is auto-incremented

## Database Schema

**Migration:** `backend/app/alembic/versions/e5f6a7b8c9d0_add_a2a_config_field.py`

**Model:** `backend/app/models/agent.py`

**New Field:**
- `Agent.a2a_config` (JSON) - Stores skills, version, generated_at

**a2a_config Structure:**
- `enabled`: Boolean flag to enable/disable A2A for this agent (default: false)
- `skills`: List of AgentSkill objects (id, name, description, tags, examples)
- `version`: Semantic version string (auto-incremented)
- `generated_at`: ISO timestamp of last generation

## Frontend Configuration

### Enabling A2A for an Agent

A2A integration is configured in the agent's **Integrations** tab:

1. Navigate to the agent detail page
2. Select the **Integrations** tab (between Configuration and Credentials)
3. Toggle the **A2A Integration** switch to enable/disable

When enabled, the Agent Card URL is displayed with a copy button for easy sharing with external A2A clients.

### UI Components

**File:** `frontend/src/components/Agents/AgentIntegrationsTab.tsx`

- Toggle switch to enable/disable A2A (`a2a_config.enabled`)
- Read-only input displaying the Agent Card URL
- Copy button to copy URL to clipboard

**Route:** `frontend/src/routes/_layout/agent/$agentId.tsx`

- Integrations tab registered between Configuration and Credentials tabs

### Agent Card URL Format

```
{VITE_API_URL}/api/v1/a2a/{agent_id}/
```

Example: `https://api.example.com/api/v1/a2a/123e4567-e89b-12d3-a456-426614174000/`

## Backend Implementation

### Routes

**File:** `backend/app/api/routes/a2a.py`

| Endpoint | Method | Auth Required | Description |
|----------|--------|---------------|-------------|
| `/api/v1/a2a/{agent_id}/` | GET | Optional* | Returns AgentCard (public or extended) |
| `/api/v1/a2a/{agent_id}/.well-known/agent-card.json` | GET | Optional* | AgentCard (standard location) |
| `/api/v1/a2a/{agent_id}/` | POST | Required | JSON-RPC router |

\* Auth optional only when A2A is enabled - returns public card without auth, extended card with auth

**JSON-RPC Methods:**

| v1.0 Method | Internal Method | Description |
|-------------|-----------------|-------------|
| `SendMessage` | `message/send` | Synchronous message, returns Task |
| `SendStreamingMessage` | `message/stream` | SSE streaming response |
| `GetTask` | `tasks/get` | Get task status and history |
| `CancelTask` | `tasks/cancel` | Cancel running task |
| `ListTasks` | `tasks/list` | List tasks for agent (custom extension) |

Note: v1.0 method names are used by default. Use `X-A2A-Stable: 1` header for internal method names.

### Services

**A2A Service:** `backend/app/services/a2a_service.py`
- `A2AService.build_agent_card()` - Generates full (extended) AgentCard from Agent model
- `A2AService.build_public_agent_card()` - Generates minimal public AgentCard (name only)
- `A2AService.get_agent_card_dict()` - Returns full card as JSON-serializable dict
- `A2AService.get_public_agent_card_dict()` - Returns public card as JSON-serializable dict

**A2A Request Handler:** `backend/app/services/a2a_request_handler.py`
- `A2ARequestHandler.handle_message_send()` - Non-streaming message handling
- `A2ARequestHandler.handle_message_stream()` - SSE streaming handler
- `A2ARequestHandler.handle_tasks_get()` - Task query
- `A2ARequestHandler.handle_tasks_cancel()` - Task cancellation
- `A2ARequestHandler.handle_tasks_list()` - List tasks (custom extension)
- `A2ARequestHandler._parse_and_validate_session_id()` - Parse task_id and validate A2A scope
- Uses `SessionService` and `MessageService` for all data access (no direct DB queries)

**A2A Event Mapper:** `backend/app/services/a2a_event_mapper.py`

Centralized A2A protocol mapping logic:
- `A2AEventMapper.map_stream_event()` - Internal streaming event → A2A event
- `A2AEventMapper.map_session_status_to_task_state()` - Session status → TaskState
- `A2AEventMapper.convert_session_messages_to_a2a()` - SessionMessage list → A2A Message list

**A2A Task Store:** `backend/app/services/a2a_task_store.py`
- `DatabaseTaskStore.get()` - Get task by ID (via SessionService)
- `DatabaseTaskStore.get_task_with_limited_history()` - Get task with message limit
- Delegates all A2A conversions to `A2AEventMapper`

**Skills Generator:** `backend/app/agents/skills_generator.py`
- `generate_a2a_skills()` - AI-based skill extraction from workflow_prompt

### Integration with Existing Services

**SessionService:** `backend/app/services/session_service.py`
- `SessionService.get_session()` - Get session by ID
- `SessionService.create_session()` - Creates sessions (used internally)
- `SessionService.send_session_message()` - Creates session (if agent_id provided) + message, initiates streaming
- `SessionService.list_environment_sessions()` - List sessions for environment with pagination and access token filter
- `SessionService.update_interaction_status()` - Update interaction_status and pending_messages_count
- `SessionService.auto_generate_session_title()` - AI-generated session titles from first message

**MessageService:** `backend/app/services/message_service.py`
- `MessageService.create_message()` - Create messages
- `MessageService.get_last_message()` - Get last message (for tool_questions_status check)
- `MessageService.get_last_n_messages()` - Get message history
- `MessageService.stream_message_with_events()` - Streams responses

**Agent Update Hook:** `backend/app/services/agent_service.py:update_agent()`
- Detects workflow_prompt changes
- Triggers skills regeneration
- Updates a2a_config with new skills and incremented version

**Interrupt Handling:**
- `active_streaming_manager.request_interrupt()` - Cancels tasks

### Configuration

**Prompt Template:** `backend/app/agents/prompts/skills_generator_prompt.md`

**Dependencies:** a2a-sdk (v0.3.22 in pyproject.toml)

## Security Features

- JWT authentication required for JSON-RPC endpoints (message/send, message/stream, etc.)
- Agent ownership validation (user must own agent or be superuser)
- Environment validation (agent must have active environment)
- JSON-RPC error codes for authorization failures (-32004)

### Public vs Extended Agent Card (A2A Protocol Compliant)

The AgentCard endpoint supports two access levels following the A2A protocol specification:

**Public Card (No Authentication):**
- Available only when A2A is enabled for the agent (`a2a_config.enabled = true`)
- Returns minimal card with only:
  - `name`: Agent name
  - `url`: A2A endpoint URL
  - `supportsAuthenticatedExtendedCard`: `true` (indicates full card available with auth)
  - `securitySchemes`: Bearer JWT authentication scheme
  - Basic protocol fields (version, protocolVersion, defaultInputModes, defaultOutputModes)
- No skills, description (shows generic "AI Agent"), or extensions exposed
- Allows external clients to discover agent existence and authentication requirements without credentials

**Extended Card (Authenticated):**
- Requires valid JWT token or A2A access token
- Returns full card with all details:
  - Agent name and description
  - All skills extracted from workflow_prompt
  - Extensions (SDK type information)
  - Full capabilities
  - `securitySchemes`: Bearer JWT authentication scheme
  - `supportsAuthenticatedExtendedCard`: based on A2A enabled status
- Same access control as before (owner or superuser, or valid A2A token for the agent)

**Security Schemes:**

Both card types include `securitySchemes` following the A2A protocol specification:

```json
{
  "securitySchemes": {
    "bearerAuth": {
      "type": "http",
      "scheme": "Bearer",
      "bearerFormat": "JWT",
      "description": "JWT Bearer token for authentication. Use either a user JWT token or an A2A access token."
    }
  },
  "security": [{"bearerAuth": []}]
}
```

**Behavior:**
| A2A Enabled | Auth Provided | Result |
|-------------|---------------|--------|
| No | No | 401 Unauthorized |
| No | Yes | Full card (if authorized) |
| Yes | No | Public card (name only) |
| Yes | Yes | Full card (if authorized) |

### A2A Access Tokens

For external A2A clients that need to access agents without full user credentials, see **[agent_integration_access_tokens.md](./agent_integration_access_tokens.md)**.

This feature allows:
- Creating scoped JWT tokens for specific agents
- Mode control: `conversation` (chat only) or `building` (full access)
- Scope control: `limited` (own sessions) or `general` (all sessions)
- 5-year token expiration
- Revoke/restore without deletion

Access tokens are managed in the agent's **Integrations** tab alongside the A2A toggle.

## Key Integration Points

### AgentCard Generation
- **Public Card** (unauthenticated, A2A enabled): Returns name, URL, securitySchemes, and `supportsAuthenticatedExtendedCard=true`
- **Extended Card** (authenticated): Includes all details below
- Reads skills from `Agent.a2a_config["skills"]`
- Reads SDK type from `AgentEnvironment.agent_sdk_conversation`
- Constructs URL from request base URL
- Sets `supportsAuthenticatedExtendedCard` based on `a2a_config.enabled`
- Includes `securitySchemes` with Bearer JWT authentication in both card types

### Message Flow
1. A2A Message parts → extracted text content via `_extract_text_from_parts()`
2. Task ID parsed and scope validated via `_parse_and_validate_session_id()`
3. Session created (if new) + message created via `SessionService.send_session_message()`
4. For new sessions, title generation triggered via `SessionService.auto_generate_session_title()`
5. **Environment activation check** via `SessionService.ensure_environment_ready_for_streaming()`
   - If environment is suspended: activates synchronously and waits for completion
   - If environment is already running: proceeds immediately
   - If environment is in error or other non-ready state: returns error
6. Streaming handled by `MessageService.stream_message_with_events()` (SSE) or `SessionService.initiate_stream()` (sync)
7. Internal events mapped to A2A format via `A2AEventMapper.map_stream_event()`

### Task State Resolution
- Gets session via `SessionService.get_session()`
- Gets last message via `MessageService.get_last_message()` for tool_questions_status
- Maps status via `A2AEventMapper.map_session_status_to_task_state()`
- Converts history via `A2AEventMapper.convert_session_messages_to_a2a()`

### SSE Streaming Event Flow

The `message/stream` JSON-RPC method returns Server-Sent Events (SSE) that comply with A2A 1.0 protocol.

**Stream Lifecycle:**

1. Client sends `message/stream` JSON-RPC request
2. `A2ARequestHandler.handle_message_stream()` parses task_id via `_parse_and_validate_session_id()`
3. `SessionService.send_session_message()` creates session (if new) and message (with `initiate_streaming=False`)
4. For new sessions, title generation triggered in background
5. Yields initial `working` status update (`final=false`)
6. **Environment activation via `SessionService.ensure_environment_ready_for_streaming()`**
   - Activates suspended environments synchronously (waits for container to start)
   - Returns error if environment cannot be activated
7. Iterates over `MessageService.stream_message_with_events()` events
8. Each internal event mapped via `A2AEventMapper.map_stream_event()`
9. Final `done` event mapped to `completed` or `canceled` status (`final=true`)

**Internal Event to A2A TaskState Mapping:**

| Internal Event Type | A2A TaskState | final | Notes |
|---------------------|---------------|-------|-------|
| `stream_started` | working | false | Stream initialization |
| `assistant` | working | false | Agent text response (with message) |
| `tool` | working | false | Tool execution (with message) |
| `thinking` | working | false | Agent thinking (with message) |
| `stream_completed` | completed | true | Used by internal event service |
| `error` | failed | true | Error occurred (with message) |
| `interrupted` | canceled | true | User requested cancellation |
| `done` | completed/canceled | true | Final stream event from MessageService |

**A2A Protocol Compliance:**

- Each SSE event is a JSON-RPC response containing a `TaskStatusUpdateEvent`
- Events include `taskId`, `contextId`, `status` (with `state` and `timestamp`), and `final` flag
- Status updates may include a `message` with agent content
- Stream MUST end with a final event where `final=true`
- Format: `A2ARequestHandler._format_sse_event()` wraps events in JSON-RPC response structure

**SSE Response Format:**

```
data: {"jsonrpc":"2.0","id":"<request_id>","result":{"kind":"status-update","taskId":"...","contextId":"...","status":{"state":"working","timestamp":"..."},"final":false}}

data: {"jsonrpc":"2.0","id":"<request_id>","result":{"kind":"status-update","taskId":"...","contextId":"...","status":{"state":"completed","timestamp":"..."},"final":true}}
```

**Key Implementation Files:**

- `A2ARequestHandler.handle_message_stream()` - SSE stream orchestration
- `A2AEventMapper.map_stream_event()` - Event type to TaskState mapping
- `A2AEventMapper._create_status_update()` - TaskStatusUpdateEvent construction
- `MessageService.stream_message_with_events()` - Internal event generation

## File Locations Reference

### Backend - API Layer
- `backend/app/api/routes/a2a.py` - A2A endpoints
- `backend/app/api/main.py` - Router registration

### Backend - A2A Services
- `backend/app/services/a2a_service.py` - AgentCard generation
- `backend/app/services/a2a_request_handler.py` - Request handling (uses service layer)
- `backend/app/services/a2a_event_mapper.py` - Centralized A2A mapping logic
- `backend/app/services/a2a_task_store.py` - Task store adapter (uses service layer)
- `backend/app/services/a2a_v1_adapter.py` - A2A Protocol v1.0 adapter

### Backend - Core Services (used by A2A)
- `backend/app/services/session_service.py` - Session operations
- `backend/app/services/message_service.py` - Message operations
- `backend/app/services/agent_service.py` - Skills generation integration

### Backend - AI Functions
- `backend/app/agents/skills_generator.py` - Skills extraction
- `backend/app/agents/prompts/skills_generator_prompt.md` - Prompt template

### Backend - Models
- `backend/app/models/agent.py` - Agent model with a2a_config field

### Backend - Migrations
- `backend/app/alembic/versions/e5f6a7b8c9d0_add_a2a_config_field.py`

### Frontend
- `frontend/src/components/Agents/AgentIntegrationsTab.tsx` - A2A configuration UI
- `frontend/src/components/Agents/AccessTokensCard.tsx` - Access token management UI
- `frontend/src/routes/_layout/agent/$agentId.tsx` - Agent detail page with Integrations tab

### Documentation
- `docs/a2a/a2a_basic_implementation.md` - Implementation plan
- `docs/a2a/agent_integration_access_tokens.md` - Access tokens documentation
- `docs/a2a/a2a.json` - A2A protocol specification

### Test Client
- `backend/clients/a2a/run_a2a_agent.py` - Interactive A2A client for testing
- `backend/clients/a2a/utils.py` - A2A connection utilities and session logging
- `backend/clients/a2a/logs/` - Session log files (JSON format)

### Session Creation Flow (A2A)

When an A2A client sends a message without a task_id (or with invalid task_id):

1. `_parse_and_validate_session_id()` returns `None` (no existing session)
2. `SessionService.send_session_message()` receives `agent_id` parameter
3. New session created via `SessionService.create_session()` with `access_token_id` for scope tracking
4. Message created and associated with new session
5. Title generation triggered in background via `SessionService.auto_generate_session_title()`
6. Session ID returned in response for subsequent messages

**Key Methods:**
- `A2ARequestHandler._parse_and_validate_session_id()` - Returns UUID or None
- `SessionService.send_session_message(session_id=None, agent_id=...)` - Creates session when session_id is None
- `SessionService.auto_generate_session_title()` - AI-generated title from first message content

---

**Document Version:** 1.7
**Last Updated:** 2026-01-17
**Status:** Phase 1 Implementation Complete (with Access Tokens, SSE Streaming, Refactored Session Creation, Public/Extended Agent Card, A2A Protocol v1.0 Support)
