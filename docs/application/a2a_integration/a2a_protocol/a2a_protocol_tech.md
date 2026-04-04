# A2A Protocol Integration - Technical Details

## File Locations

### Backend - API Layer
- `backend/app/api/routes/a2a.py` - A2A endpoints (GET AgentCard, POST JSON-RPC)
- `backend/app/api/main.py` - Router registration

### Backend - A2A Services
- `backend/app/services/a2a/a2a_service.py` - AgentCard generation
- `backend/app/services/a2a/a2a_request_handler.py` - Request handling (uses service layer)
- `backend/app/services/a2a/a2a_event_mapper.py` - Centralized A2A protocol mapping logic
- `backend/app/services/a2a/a2a_task_store.py` - Task store adapter (uses service layer)
- `backend/app/services/a2a/a2a_v1_adapter.py` - A2A Protocol v1.0 adapter

### Backend - Core Services (used by A2A)
- `backend/app/services/sessions/session_service.py` - Session operations
- `backend/app/services/sessions/message_service.py` - Message operations
- `backend/app/services/agents/agent_service.py` - Skills generation integration

### Backend - AI Functions
- `backend/app/agents/skills_generator.py` - Skills extraction from workflow_prompt
- `backend/app/agents/prompts/skills_generator_prompt.md` - Prompt template

### Backend - Models
- `backend/app/models/agents/agent.py` - Agent model with `a2a_config` JSON field

### Backend - Migrations
- `backend/app/alembic/versions/e5f6a7b8c9d0_add_a2a_config_field.py` - Adds a2a_config field to Agent

### Frontend
- `frontend/src/components/Agents/AgentIntegrationsTab.tsx` - A2A toggle + Agent Card URL display
- `frontend/src/components/Agents/AccessTokensCard.tsx` - Access token management UI
- `frontend/src/routes/_layout/agent/$agentId.tsx` - Agent detail page with Integrations tab

### Test Client
- `backend/clients/a2a/run_a2a_agent.py` - Interactive A2A client for testing
- `backend/clients/a2a/utils.py` - A2A connection utilities and session logging
- `backend/clients/a2a/logs/` - Session log files (JSON format)

### Tests
- `backend/tests/api/a2a_integration/` - A2A integration tests

## Database Schema

**Migration:** `backend/app/alembic/versions/e5f6a7b8c9d0_add_a2a_config_field.py`

**Model:** `backend/app/models/agents/agent.py`

**Field:** `Agent.a2a_config` (JSON) - Stores skills, version, generated_at, enabled flag

## API Endpoints

**File:** `backend/app/api/routes/a2a.py`

| Endpoint | Method | Auth Required | Description |
|----------|--------|---------------|-------------|
| `/api/v1/a2a/{agent_id}/` | GET | Optional* | Returns AgentCard (public or extended) |
| `/api/v1/a2a/{agent_id}/.well-known/agent-card.json` | GET | Optional* | AgentCard (standard location) |
| `/api/v1/a2a/{agent_id}/` | POST | Required | JSON-RPC router |

\* Auth optional only when A2A is enabled - returns public card without auth, extended card with auth

**JSON-RPC Methods (v1.0 / internal):**

| v1.0 Method | Internal Method | Description |
|-------------|-----------------|-------------|
| `SendMessage` | `message/send` | Synchronous message, returns Task |
| `SendStreamingMessage` | `message/stream` | SSE streaming response |
| `GetTask` | `tasks/get` | Get task status and history |
| `CancelTask` | `tasks/cancel` | Cancel running task |
| `ListTasks` | `tasks/list` | List tasks for agent (custom extension) |

## Services & Key Methods

### A2A Service
**File:** `backend/app/services/a2a/a2a_service.py`

- `A2AService.build_agent_card()` - Generates full (extended) AgentCard from Agent model
- `A2AService.build_public_agent_card()` - Generates minimal public AgentCard (name only)
- `A2AService.get_agent_card_dict()` - Returns full card as JSON-serializable dict
- `A2AService.get_public_agent_card_dict()` - Returns public card as JSON-serializable dict

### A2A Request Handler
**File:** `backend/app/services/a2a/a2a_request_handler.py`

- `A2ARequestHandler.handle_message_send()` - Non-streaming message handling
- `A2ARequestHandler.handle_message_stream()` - SSE streaming handler
- `A2ARequestHandler.handle_tasks_get()` - Task query
- `A2ARequestHandler.handle_tasks_cancel()` - Task cancellation
- `A2ARequestHandler.handle_tasks_list()` - List tasks (custom extension)
- `A2ARequestHandler._parse_and_validate_session_id()` - Parse task_id and validate A2A scope
- Uses `SessionService` and `MessageService` for all data access (no direct DB queries)

### A2A Event Mapper
**File:** `backend/app/services/a2a/a2a_event_mapper.py`

- `A2AEventMapper.map_stream_event()` - Internal streaming event to A2A event
- `A2AEventMapper.map_session_status_to_task_state()` - Session status to TaskState
- `A2AEventMapper.convert_session_messages_to_a2a()` - SessionMessage list to A2A Message list
- `A2AEventMapper._create_status_update()` - TaskStatusUpdateEvent construction

### A2A Task Store
**File:** `backend/app/services/a2a/a2a_task_store.py`

- `DatabaseTaskStore.get()` - Get task by ID (via SessionService)
- `DatabaseTaskStore.get_task_with_limited_history()` - Get task with message limit
- Delegates all A2A conversions to `A2AEventMapper`

### A2A v1.0 Adapter
**File:** `backend/app/services/a2a/a2a_v1_adapter.py`

- `A2AV1Adapter.should_use_v1(request)` - Check header for protocol version
- `A2AV1Adapter.transform_request_inbound(body)` - Transform v1.0 method names to internal
- `A2AV1Adapter.transform_agent_card_outbound(card)` - Transform AgentCard to v1.0 structure
- `A2AV1Adapter.transform_task_outbound(task)` - Add `kind` discriminator

### Skills Generator
**File:** `backend/app/agents/skills_generator.py`

- `generate_a2a_skills()` - AI-based skill extraction from workflow_prompt

### Integration with Existing Services

**SessionService:** `backend/app/services/sessions/session_service.py`
- `send_session_message()` - Creates session (if agent_id provided) + message, initiates streaming
- `get_session()` - Get session by ID
- `list_environment_sessions()` - List sessions for environment with pagination and access token filter
- `update_interaction_status()` - Update interaction_status and pending_messages_count
- `auto_generate_session_title()` - AI-generated session titles from first message
- `ensure_environment_ready_for_streaming()` - Activate suspended environments

**MessageService:** `backend/app/services/sessions/message_service.py`
- `stream_message_with_events()` - Streams responses as internal events
- `get_last_message()` - Get last message (for tool_questions_status check)
- `get_last_n_messages()` - Get message history

**AgentService:** `backend/app/services/agents/agent_service.py:update_agent()`
- Detects workflow_prompt changes, triggers skills regeneration
- Updates a2a_config with new skills and incremented version

**ActiveStreamingManager:**
- `active_streaming_manager.request_interrupt()` - Cancels tasks

## Frontend Components

**AgentIntegrationsTab:** `frontend/src/components/Agents/AgentIntegrationsTab.tsx`
- Toggle switch to enable/disable A2A (`a2a_config.enabled`)
- Read-only input displaying the Agent Card URL
- Copy button to copy URL to clipboard

**Agent detail page:** `frontend/src/routes/_layout/agent/$agentId.tsx`
- Integrations tab registered between Configuration and Credentials tabs

## Configuration

- **Prompt Template:** `backend/app/agents/prompts/skills_generator_prompt.md`
- **Dependencies:** a2a-sdk (v0.3.22 in pyproject.toml)
- **Agent Card URL:** `{VITE_API_URL}/api/v1/a2a/{agent_id}/`

## Security

- JWT authentication required for JSON-RPC endpoints
- Agent ownership validation (user must own agent or be superuser)
- Environment validation (agent must have active environment)
- JSON-RPC error codes for authorization failures (-32004)
- Public card exposes only name and URL (no skills/description)
- Security schemes included in both public and extended cards (Bearer JWT)
- Supports both user JWT tokens and A2A access tokens (see [A2A Access Tokens tech](../a2a_access_tokens/a2a_access_tokens_tech.md))

### SSE Response Format

Each SSE event is a JSON-RPC response containing a `TaskStatusUpdateEvent`:
- Events include `taskId`, `contextId`, `status` (with `state` and `timestamp`), and `final` flag
- Status updates may include a `message` with agent content
- Stream ends with a final event where `final=true`
- Format handled by `A2ARequestHandler._format_sse_event()` wrapping events in JSON-RPC response structure

---

*Last updated: 2026-03-02*
