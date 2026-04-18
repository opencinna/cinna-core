# A2A Protocol Integration - Technical Details

## File Locations

### Backend - API Layer
- `backend/app/api/routes/a2a.py` - A2A endpoints (GET AgentCard, POST JSON-RPC)
- `backend/app/api/main.py` - Router registration

### Backend - A2A Services
- `backend/app/services/a2a/a2a_service.py` - AgentCard generation + `apply_protocol()` v1.0 finalizer shared with `ExternalA2AService`
- `backend/app/services/a2a/a2a_request_handler.py` - Shared JSON-RPC dispatch + session/stream/task orchestration with hook methods for subclasses (default hooks enforce A2A access-token scope)
- `backend/app/services/a2a/a2a_event_mapper.py` - Centralized A2A protocol mapping logic
- `backend/app/services/a2a/a2a_task_store.py` - Task store adapter (uses service layer)
- `backend/app/services/a2a/a2a_v1_adapter.py` - A2A Protocol v1.0 adapter
- `backend/app/services/a2a/jsonrpc_utils.py` - Shared `resolve_protocol()`, `jsonrpc_success()`, `jsonrpc_error()` helpers used by both the `/a2a/` and `/external/a2a/` surfaces

### Backend - Core Services (used by A2A)
- `backend/app/services/sessions/session_service.py` - Session operations
- `backend/app/services/sessions/message_service.py` - Message operations
- `backend/app/services/sessions/stream_processor.py` - `SessionStreamProcessor` unified streaming pipeline
- `backend/app/services/sessions/stream_event_handlers.py` - `A2AStreamEventHandler` for A2A SSE event mapping
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

Three routers each expose the same endpoint patterns. Each router is registered separately in `api/main.py`.

| Endpoint | Method | Auth Required | Protocol | Description |
|----------|--------|---------------|----------|-------------|
| `/api/v1/a2a/{agent_id}/` | GET | Optional* | v1.0 (latest) | AgentCard in v1.0 format with versioned `supportedInterfaces` |
| `/api/v1/a2a/{agent_id}/.well-known/agent-card.json` | GET | Optional* | v1.0 (latest) | AgentCard (standard location) |
| `/api/v1/a2a/{agent_id}/` | POST | Required | v1.0 (latest) | JSON-RPC, PascalCase method names |
| `/api/v1/a2a/v1.0/{agent_id}/` | GET | Optional* | v1.0 (explicit) | Identical to base URL |
| `/api/v1/a2a/v1.0/{agent_id}/.well-known/agent-card.json` | GET | Optional* | v1.0 (explicit) | AgentCard (standard location) |
| `/api/v1/a2a/v1.0/{agent_id}/` | POST | Required | v1.0 (explicit) | JSON-RPC, PascalCase method names |
| `/api/v1/a2a/v0.3/{agent_id}/` | GET | Optional* | v0.3.0 (legacy) | AgentCard in v0.3 library-native format |
| `/api/v1/a2a/v0.3/{agent_id}/.well-known/agent-card.json` | GET | Optional* | v0.3.0 (legacy) | AgentCard (standard location) |
| `/api/v1/a2a/v0.3/{agent_id}/` | POST | Required | v0.3.0 (legacy) | JSON-RPC, slash-case method names, no transformation |

\* Auth optional only when A2A is enabled - returns public card without auth, extended card with auth

**JSON-RPC Methods:**

| v1.0 Method (PascalCase) | v0.3 Method (slash-case) | Description |
|--------------------------|--------------------------|-------------|
| `SendMessage` | `message/send` | Synchronous message, returns Task |
| `SendStreamingMessage` | `message/stream` | SSE streaming response |
| `GetTask` | `tasks/get` | Get task status and history |
| `CancelTask` | `tasks/cancel` | Cancel running task |
| `ListTasks` | `tasks/list` | List tasks for agent (custom extension) |

Use PascalCase methods on `/a2a/` and `/a2a/v1.0/` URLs. Use slash-case methods on `/a2a/v0.3/` URLs.

## Services & Key Methods

### A2A Service
**File:** `backend/app/services/a2a/a2a_service.py`

- `A2AService.build_agent_card()` - Generates full (extended) AgentCard from Agent model
- `A2AService.build_public_agent_card()` - Generates minimal public AgentCard (name only)
- `A2AService.get_agent_card_dict(..., protocol="v0.3" | "v1.0")` - Returns full card as JSON-serializable dict, applying the v1.0 adapter when `protocol="v1.0"`
- `A2AService.get_public_agent_card_dict(..., protocol="v0.3" | "v1.0")` - Public card variant
- `A2AService.apply_protocol(card_dict, protocol)` - Public helper that runs the v1.0 outbound adapter; reused by `ExternalA2AService` for its synthesized identity card

### A2A Request Handler
**File:** `backend/app/services/a2a/a2a_request_handler.py`

Shared dispatch methods (used by both the `/a2a/` surface and, via `ExternalA2AContextHandler`, the `/external/a2a/` surface):

- `A2ARequestHandler.handle_message_send()` - Non-streaming message handling (polls for completion)
- `A2ARequestHandler.handle_message_stream()` - SSE streaming handler; delegates core streaming to `SessionStreamProcessor` with `A2AStreamEventHandler`
- `A2ARequestHandler.handle_tasks_get()` - Task query
- `A2ARequestHandler.handle_tasks_cancel()` - Task cancellation
- `A2ARequestHandler.handle_tasks_list()` - List tasks (custom extension)

Hook methods — subclasses override to customize access control and session stamping. Default implementations enforce A2A-access-token scope (used by the core `/a2a/` route):

- `_parse_session_scope(task_id)` - Parse task_id UUID and enforce scope on existing sessions
- `_authorize_existing_session(session)` - Guard tasks/get and tasks/cancel
- `_stamp_new_session(session_id)` - Post-create hook (no-op default; external surface writes caller_id / metadata)
- `_integration_type_for_new_session()` - Integration type passed to `SessionService.send_session_message`
- `_session_access_token_id()` - Threaded into `SessionService` for both session lineage and `CommandContext`
- `_task_list_access_token_filter()` - DB-level `access_token_id` filter for tasks/list
- `_task_list_filter(session)` - In-memory filter for tasks/list results
- `_wrap_env_error(exc)` - Shape env-readiness errors for the caller
- `_stream_scope_error(exc, request_id)` - Optionally surface scope violations as inline SSE errors (default: propagate)

Uses `SessionService` for session operations (no direct DB queries); streaming delegated to unified `SessionStreamProcessor`.

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

- `A2AV1Adapter.transform_request_inbound(body)` - Transform v1.0 method names to internal (PascalCase → slash-case)
- `A2AV1Adapter.transform_agent_card_outbound(card)` - Transform AgentCard to v1.0 structure; builds distinct versioned URLs for `supportedInterfaces` by inserting `v1.0/` or `v0.3/` after `/a2a/` in the base URL
- `A2AV1Adapter.transform_task_outbound(task)` - Add `kind` discriminator
- `A2AV1Adapter.transform_message_outbound(message)` - Add `kind` discriminator
- `A2AV1Adapter.transform_sse_event_outbound(event)` - Pass-through (already formatted)

The adapter is only applied for v1.0 and latest endpoints. The v0.3 endpoint bypasses the adapter entirely — the library speaks v0.3 natively.

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

## Protocol Reference & Discovery

How to inspect A2A protocol types, required fields, and card structure when doing protocol analysis or upgrading to a newer spec version.

### Authoritative Sources

| Source | What it tells you | How to access |
|--------|-------------------|---------------|
| A2A v1 spec | Normative field definitions, required/optional, semantics | https://a2a-protocol.org/latest/specification/ — sections 4.4.x cover Agent Discovery Objects |
| `a2a-sdk` Python library | Pydantic models used at runtime; `Field(...)` = required, `Field(default=...)` = optional | Installed at `backend/.venv/lib/python3.13/site-packages/a2a/types.py` |
| Our v1 adapter | What we actually transform and emit for v1 clients | `backend/app/services/a2a/a2a_v1_adapter.py` |
| Live card output | The JSON a client actually receives | `curl http://localhost:8000/api/v1/a2a/{agent_id}/` (v1.0) or `curl http://localhost:8000/api/v1/a2a/v0.3/{agent_id}/` (v0.3) |

### Inspecting the Library Models

The `a2a-sdk` package (pinned `>=0.3.22` in `backend/pyproject.toml`) ships Pydantic v2 models in `a2a.types`. These are the source of truth for what our code can construct.

**List all fields and required status for any A2A type:**

```bash
source backend/.venv/bin/activate
python3 -c "
from a2a.types import AgentCard  # or AgentInterface, AgentProvider, AgentSkill, etc.
for name, field in AgentCard.model_fields.items():
    req = field.is_required()
    print(f'{name:40s} required={req}  default={field.default if not req else \"---\"}')
"
```

**Read the full class source (docstrings, field descriptions):**

```bash
python3 -c "
from a2a.types import AgentCard
import inspect
print(inspect.getsource(AgentCard))
"
```

### AgentCard Required Fields (a2a-sdk 0.3.22)

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `name` | `str` | Yes | Agent display name |
| `description` | `str` | Yes | Human-readable purpose description |
| `url` | `str` | Yes | Preferred endpoint URL (v0.3 top-level; v1 moved to `supportedInterfaces`) |
| `version` | `str` | Yes | Agent's own version number |
| `capabilities` | `AgentCapabilities` | Yes | Streaming, push notifications, extensions |
| `skills` | `list[AgentSkill]` | Yes | Agent capabilities (can be empty `[]`) |
| `defaultInputModes` | `list[str]` | Yes | MIME types for input |
| `defaultOutputModes` | `list[str]` | Yes | MIME types for output |
| `provider` | `AgentProvider` | No | Organization name + URL (sub-fields `organization` and `url` are both required when present) |
| `protocolVersion` | `str` | No | Default `"0.3.0"` |
| `preferredTransport` | `str` | No | Default `"JSONRPC"` |
| `securitySchemes` | `dict[str, SecurityScheme]` | No | OpenAPI 3.0 security scheme objects |
| `security` | `list[dict]` | No | Security requirement objects |
| `additionalInterfaces` | `list[AgentInterface]` | No | Extra transport/URL combos |
| `supportsAuthenticatedExtendedCard` | `bool` | No | Signals extended card availability |
| `documentationUrl` | `str` | No | Link to agent docs |
| `iconUrl` | `str` | No | Agent icon |
| `signatures` | `list[AgentCardSignature]` | No | Card signing (JWS) |

### Key Nested Types

**AgentInterface** — declares a transport + URL pair:
- `url` (str, required) — endpoint URL
- `transport` (str, required) — e.g. `"JSONRPC"`, `"GRPC"`, `"HTTP+JSON"`

**AgentProvider** — service provider info:
- `organization` (str, required) — provider org name
- `url` (str, required) — provider website

**AgentSkill** — agent capability:
- `id` (str, required), `name` (str, required), `description` (str, required)
- `tags` (list[str], optional), `examples` (list[str], optional)

### v0.3 vs v1 Card Output

Each protocol version has its own dedicated URL. The format is determined by the URL, not any request header.

**v0.3 (legacy / library native) — GET `/api/v1/a2a/v0.3/{agent_id}/`:**
- `protocolVersion`: `"0.3.0"` (string)
- `url`: points to `/api/v1/a2a/v0.3/{agent_id}/` (v0.3-specific URL)
- `supportsAuthenticatedExtendedCard`: top-level bool

**v1.0 (default, via `A2AV1Adapter.transform_agent_card_outbound`) — GET `/api/v1/a2a/{agent_id}/` or `/api/v1/a2a/v1.0/{agent_id}/`:**
- `protocolVersions`: `["1.0", "0.3.0"]` (array)
- `supportedInterfaces`: distinct versioned URLs per protocol:
  ```json
  [
    {"url": "http://host/api/v1/a2a/v1.0/{id}/", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"},
    {"url": "http://host/api/v1/a2a/v0.3/{id}/", "protocolBinding": "JSONRPC", "protocolVersion": "0.3.0"}
  ]
  ```
- `capabilities.extendedAgentCard`: `true` (replaces `supportsAuthenticatedExtendedCard`)

### Quick Protocol Check Commands

```bash
# Fetch v1 card (default / latest)
curl -s http://localhost:8000/api/v1/a2a/{agent_id}/ | python3 -m json.tool

# Fetch explicit v1.0 card
curl -s http://localhost:8000/api/v1/a2a/v1.0/{agent_id}/ | python3 -m json.tool

# Fetch v0.3 card (legacy)
curl -s http://localhost:8000/api/v1/a2a/v0.3/{agent_id}/ | python3 -m json.tool

# Compare our output against spec field list
source backend/.venv/bin/activate
python3 -c "
from a2a.types import AgentCard
spec_required = {n for n, f in AgentCard.model_fields.items() if f.is_required()}
print('Required by library:', sorted(spec_required))
"
```

## Configuration

- **Prompt Template:** `backend/app/agents/prompts/skills_generator_prompt.md`
- **Dependencies:** a2a-sdk (`>=0.3.22` in `backend/pyproject.toml`, installed as `a2a-sdk`)
- **Agent Card URL:** `{VITE_API_URL}/api/v1/a2a/{agent_id}/`
- **Library types location:** `backend/.venv/lib/python3.13/site-packages/a2a/types.py`

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

## Sharing the Runtime with External Agent Access

`A2ARequestHandler`'s dispatch bodies (`handle_message_send`, `handle_message_stream`, `handle_tasks_*`) are reused by the first-party `/api/v1/external/a2a/` surface via the `ExternalA2AContextHandler` subclass in `backend/app/services/external/external_a2a_context_handler.py`. That subclass overrides the hook methods listed above to:

- Enforce caller-scope per `TargetContext.integration_type` (`external`, `app_mcp`, `identity_mcp`) instead of A2A-token scope
- Stamp `caller_id` / `identity_caller_id` / `session_metadata` on new sessions
- Re-check identity-binding validity on resume
- Raise `app.services.external.errors` domain exceptions instead of `ValueError`

The two feature surfaces keep their own routes, auth contexts, card builders, and access policies; only the protocol runtime is shared. See [External Agent Access](../../external_agent_access/external_agent_access.md) for the per-target-type rules on top of this shared dispatch.

---

*Last updated: 2026-04-18*
