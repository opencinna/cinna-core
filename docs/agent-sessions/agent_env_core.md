# Agent Environment Core - Architecture & Business Logic

## Overview

The **Agent Environment Core** is the server-side component that runs inside each Docker agent environment container. It provides:

1. **HTTP API** for communication with the backend
2. **SDK Integration** for routing to different AI SDKs (Claude, OpenAI, etc.)
3. **Prompt Management** for building and conversation modes
4. **Business Logic** for handling agent prompts, workspace files, and configuration

**Location**: `backend/app/env-templates/python-env-advanced/app/core/server/`

**Purpose**: Execute AI agent sessions in isolated environments with proper separation between system code and user workspace.

## Architecture

### Module Structure

The core is organized into focused modules with clear separation of concerns:

```
app/core/server/
├── main.py                 # FastAPI entry point
├── routes.py               # HTTP API endpoints
├── models.py               # Pydantic request/response models
├── sdk_manager.py          # Multi-adapter SDK manager
├── prompt_generator.py     # System prompt generation
├── agent_env_service.py    # Business logic for workspace operations
├── sdk_utils.py            # Logging, debugging utilities
├── active_session_manager.py  # Session interrupt tracking
└── adapters/               # SDK adapter implementations
    ├── __init__.py         # Package exports
    ├── base.py             # SDKEvent, SDKConfig, BaseSDKAdapter, AdapterRegistry
    ├── claude_code.py      # ClaudeCodeAdapter for claude-code/* variants
    └── google_adk.py       # GoogleADKAdapter placeholder for google-adk-wr/*
```

### Design Principles

1. **Single Responsibility**: Each module has one clear purpose
2. **Dependency Injection**: Components receive dependencies in constructors
3. **Service Layer**: Business logic separated from HTTP layer
4. **Strategy Pattern**: Prompt generation varies by mode
5. **Testability**: Pure functions and mockable dependencies

## Core Modules

### 1. SDK Manager (`sdk_manager.py`)

**Purpose**: Multi-adapter SDK orchestration and routing

**Architecture**: Uses a pluggable adapter system to support multiple SDK providers:

```
SDKManager
    ├── _get_adapter(mode) → selects adapter based on SDK_ADAPTER_* ENV vars
    ├── send_message_stream() → delegates to adapter, converts SDKEvent to dict
    └── AdapterRegistry → dynamic adapter registration and instantiation
```

**Responsibilities**:
- Route requests to appropriate adapter based on environment configuration
- Convert unified `SDKEvent` objects to dict format for backward compatibility
- Manage adapter lifecycle and caching per mode

**Key Classes**:
- `SDKManager`: Main orchestrator, reads ENV config, delegates to adapters
- `SDKConfig`: Configuration from environment variables (`SDKConfig.from_env(mode)`)
- `AdapterRegistry`: Dynamic adapter registration and instantiation

**Environment Variables**:
- `SDK_ADAPTER_BUILDING`: Adapter ID for building mode (e.g., `claude-code/anthropic`)
- `SDK_ADAPTER_CONVERSATION`: Adapter ID for conversation mode

**Adapter Selection Flow**:
1. `SDKManager.send_message_stream()` receives request with `mode` parameter
2. `_get_adapter(mode)` reads `SDK_ADAPTER_{MODE}` from ENV
3. `SDKConfig.from_env(mode)` parses adapter ID (e.g., `claude-code/minimax`)
4. `AdapterRegistry.create_adapter(config)` instantiates correct adapter
5. Adapter streams `SDKEvent` objects, manager converts to dicts

**Unified Event Format** (`SDKEvent`):
```python
@dataclass
class SDKEvent:
    type: SDKEventType  # SESSION_CREATED, ASSISTANT, TOOL_USE, DONE, ERROR, etc.
    content: str        # Human-readable message
    session_id: str     # SDK session ID
    metadata: dict      # Additional event-specific data
```

**File**: `backend/app/env-templates/python-env-advanced/app/core/server/sdk_manager.py`

### 1a. SDK Adapters (`adapters/`)

**Purpose**: SDK-specific implementations that produce unified events

**Base Adapter** (`adapters/base.py`):
- `BaseSDKAdapter`: Abstract base class with `send_message_stream()` and `interrupt_session()`
- `SDKEventType`: Enum of event types (SESSION_CREATED, ASSISTANT, TOOL_USE, etc.)
- `SDKEvent`: Unified event dataclass with `to_dict()` method
- `AdapterRegistry`: Decorator-based adapter registration

**Claude Code Adapter** (`adapters/claude_code.py`):
- Handles `claude-code/anthropic` and `claude-code/minimax` variants
- Configures Claude SDK client with appropriate settings files
- Converts Claude SDK messages to `SDKEvent` format
- Manages session tracking, interrupts, and error handling

**Google ADK Adapter** (`adapters/google_adk.py`):
- Placeholder for `google-adk-wr/gemini` and `google-adk-wr/vertex`
- Returns "not implemented" error (future implementation)

**Model Selection** (in ClaudeCodeAdapter):
- **Conversation Mode**: Uses `model="haiku"` (fast, cost-effective)
- **Building Mode**: Uses default model (Sonnet, better code generation)

### 2. Prompt Generator (`prompt_generator.py`)

**Purpose**: Generate system prompts for different modes and SDKs

**Responsibilities**:
- Load prompt files from workspace and templates
- Construct building mode prompts (Claude Code preset + docs)
- Construct conversation mode prompts (workflow-focused, lightweight)
- Cache static prompts for performance

**Key Methods**:
- `generate_building_mode_prompt()`: Returns SystemPromptPreset dict
  - Claude Code preset + BUILDING_AGENT.md + scripts README + workflow docs
- `generate_conversation_mode_prompt()`: Returns plain string
  - WORKFLOW_PROMPT.md + scripts README only
- `generate_prompt(mode)`: Factory method routing to mode-specific generators

**Loaded Files**:
- `/app/BUILDING_AGENT.md` - Static template for building mode
- `/app/workspace/scripts/README.md` - Dynamic scripts catalog
- `/app/workspace/docs/WORKFLOW_PROMPT.md` - Workflow system prompt
- `/app/workspace/docs/ENTRYPOINT_PROMPT.md` - Trigger message (building mode only)

**File**: `backend/app/env-templates/python-env-advanced/app/core/server/prompt_generator.py`

### 3. Agent Environment Service (`agent_env_service.py`)

**Purpose**: Handle business logic for workspace operations

**Responsibilities**:
- Read/write agent prompt files (WORKFLOW_PROMPT.md, ENTRYPOINT_PROMPT.md)
- Validate workspace structure
- Manage docs directory
- Provide workspace metadata

**Key Methods**:
- `get_agent_prompts()`: Returns tuple of (workflow_prompt, entrypoint_prompt)
- `update_agent_prompts()`: Write prompts to docs directory
  - Returns list of updated filenames
  - Raises IOError on failure
- `validate_workspace()`: Check workspace exists and is writable
- `get_workspace_info()`: Return workspace metadata dict

**File Paths**:
- Workspace root: `/app/workspace` (from `CLAUDE_CODE_WORKSPACE` env var)
- Docs directory: `/app/workspace/docs/`
- Prompt files: `WORKFLOW_PROMPT.md`, `ENTRYPOINT_PROMPT.md`

**File**: `backend/app/env-templates/python-env-advanced/app/core/server/agent_env_service.py`

### 4. SDK Utils (`sdk_utils.py`)

**Purpose**: Utility functions for logging, debugging, and message formatting

**Components**:

**SessionLogger Class**:
- Initialize session log files in `/app/workspace/logs/`
- Dump raw SDK messages for debugging
- Write session completion markers
- Enabled via `DUMP_LLM_SESSION=true` environment variable

**Utility Functions**:
- `format_message_for_debug()`: Format SDK messages for logs
- `format_sdk_message()`: Transform SDK messages to API response format

**File**: `backend/app/env-templates/python-env-advanced/app/core/server/sdk_utils.py`

## Session Modes

### Building Mode

**Purpose**: Develop and configure workflow capabilities

**System Prompt Structure**:
```
Claude Code Preset (with all standard tools)
+
BUILDING_AGENT.md (development guidelines)
+
scripts/README.md (existing scripts catalog)
+
docs/WORKFLOW_PROMPT.md (current workflow configuration)
+
docs/ENTRYPOINT_PROMPT.md (trigger message examples)
```

**Model**: Default (Claude Sonnet) - better code generation

**Tools Available**: `Read`, `Edit`, `Glob`, `Grep`, `Bash`, `Write`

**Use Cases**:
- Create new Python scripts for workflow automation
- Configure integrations and credentials
- Update workflow documentation
- Define entry points for scheduled execution

**Generated By**: `PromptGenerator.generate_building_mode_prompt()`

### Conversation Mode

**Purpose**: Execute pre-built workflows and tasks

**System Prompt Structure**:
```
docs/WORKFLOW_PROMPT.md (main system prompt)
+
scripts/README.md (available scripts)
```

**Model**: Haiku - faster and more cost-effective

**Tools Available**: `Read`, `Edit`, `Glob`, `Grep`, `Bash`, `Write`

**Use Cases**:
- Execute workflow tasks using existing scripts
- Process user requests based on workflow capabilities
- Generate reports and summaries
- Interact with APIs and data sources

**Characteristics**:
- Lightweight (no Claude Code preset overhead)
- Focused on execution, not development
- Faster responses with Haiku model
- Lower cost per interaction

**Generated By**: `PromptGenerator.generate_conversation_mode_prompt()`

## SDK Support

### Agent SDK Parameter

**Purpose**: Support multiple AI SDKs in the same environment

**Current Implementation**: `agent_sdk="claude"` (only option currently)

**Future Extensibility**: Can add `"openai"`, `"google-adk"`, `"anthropic-api"`, etc.

**Flow**:
1. Backend creates session with `agent_sdk` parameter
2. Session stores SDK preference in database
3. Message requests include `agent_sdk` in payload
4. Environment routes to appropriate SDK manager
5. SDK-specific configuration applied (model, tools, prompts)

**Request Model** (`models.py::ChatRequest`):
```python
class ChatRequest:
    message: str
    session_id: str | None
    mode: str  # "building" | "conversation"
    agent_sdk: str = "claude"
    system_prompt: str | None
```

## Request Flow

### Message Request Flow

**1. Backend Request** (`POST /chat/stream`)
```
Backend → HTTP POST → Environment Container
Payload: {
  "message": "user message",
  "mode": "conversation",
  "agent_sdk": "claude",
  "session_id": "existing_session_id" or null
}
```

**2. Route Handler** (`routes.py::chat_stream()`)
- Validates `agent_sdk` parameter
- Logs request details
- Routes to SDK manager

**3. SDK Manager** (`sdk_manager.py::send_message_stream()`)
- Validates SDK support
- Sets model based on mode (Haiku or Sonnet)
- Generates system prompt via PromptGenerator
- Creates SDK client with options
- Sends message and streams responses

**4. Prompt Generation** (`prompt_generator.py`)
- Building mode: Comprehensive prompt with all docs
- Conversation mode: Lightweight prompt with workflow context

**5. Response Stream**
```
SDK → SDK Manager → Route Handler → Backend
Events: {
  "type": "assistant" | "tool" | "done",
  "content": "response text",
  "session_id": "sdk_session_id",
  "metadata": {...}
}
```

### Session Resumption Flow

**New Session**:
- `session_id=None` in request
- SDK creates new session
- `session_created` event emitted with new `session_id`
- Backend stores `session_id` for future resumption

**Resume Session**:
- `session_id="existing_id"` in request
- SDK loads previous conversation context
- Continues from last state
- Same `session_id` throughout conversation

## API Endpoints

### Chat Endpoints

**POST /chat** - Synchronous chat (non-streaming)
- Request: `ChatRequest` with message, mode, agent_sdk
- Response: `ChatResponse` with complete response text
- Use case: Simple request-response interactions

**POST /chat/stream** - Streaming chat (SSE)
- Request: `ChatRequest` with message, mode, agent_sdk
- Response: Server-Sent Events stream
- Events: `session_created`, `assistant`, `tool`, `done`, `error`
- Use case: Real-time streaming with tool visibility

### Configuration Endpoints

**GET /config/agent-prompts** - Get current prompts
- Returns: `AgentPromptsResponse` with workflow_prompt and entrypoint_prompt
- Source: Reads from `/app/workspace/docs/` files
- Use case: Backend syncing prompts after building sessions

**POST /config/agent-prompts** - Update prompts
- Request: `AgentPromptsUpdate` with optional workflow_prompt and entrypoint_prompt
- Response: Status and list of updated files
- Use case: Backend pushing manually edited prompts to environment

### Utility Endpoints

**GET /health** - Health check
- Returns: Status, timestamp, uptime, message
- Use case: Docker health checks, monitoring

**GET /sdk/sessions** - List SDK sessions (debugging)
- Returns: Message indicating sessions are per-request
- Note: Claude SDK doesn't maintain persistent server-side sessions

**DELETE /sdk/sessions/{session_id}** - Close SDK session
- Returns: Confirmation message
- Note: Sessions auto-cleanup, no explicit close needed

## Backend Integration

### Backend Services Using Agent Env Core

**Message Service** (`backend/app/services/message_service.py`):
- `send_message_to_environment_stream()`: Sends HTTP request to `/chat/stream`
- Includes `agent_sdk` parameter in payload
- Parses SSE events and yields to frontend

**Session Service** (`backend/app/services/session_service.py`):
- Creates sessions with `agent_sdk` parameter
- Stores external SDK session IDs in metadata
- Manages session mode and SDK type

**Environment Lifecycle** (`backend/app/services/environment_lifecycle.py`):
- Creates environment instances with core and workspace separation
- Syncs prompts to/from environment via HTTP endpoints
- Manages rebuild operations (updates core, preserves workspace)
- Regenerates configuration files on create/rebuild/start operations:
  - `_update_environment_config()`: Generates fresh JWT token, updates docker-compose.yml and .env
  - `_generate_auth_token()`: Creates 10-year JWT token with user ID as subject
  - Fetches `ANTHROPIC_API_KEY` from user settings via `crud.get_user_ai_credentials()`

### Backend Routes Using Environment

**Messages Route** (`backend/app/api/routes/messages.py`):
- `POST /sessions/{session_id}/messages/stream`
- Extracts `mode` and `agent_sdk` from session
- Streams events from environment to frontend

**Agents Route** (`backend/app/api/routes/agents.py`):
- Sync prompts between backend Agent model and environment docs

## Configuration

### Environment Variables

**Required**:
- `ANTHROPIC_API_KEY`: API key for Claude SDK (fetched from user AI credentials)
- `CLAUDE_CODE_WORKSPACE`: Path to workspace (`/app/workspace`)
- `ENV_ID`: Environment UUID
- `AGENT_ID`: Agent UUID
- `AGENT_AUTH_TOKEN`: JWT bearer token for backend API authentication (10-year expiration)

**SDK Adapter Configuration**:
- `SDK_ADAPTER_BUILDING`: Adapter ID for building mode (default: `claude-code/anthropic`)
- `SDK_ADAPTER_CONVERSATION`: Adapter ID for conversation mode (default: `claude-code/anthropic`)

**Adapter ID Format**: `<adapter-type>/<provider>`
- `claude-code/anthropic` - Claude Code SDK with Anthropic
- `claude-code/minimax` - Claude Code SDK with MiniMax
- `google-adk-wr/gemini` - Google ADK with Gemini (placeholder)

**Optional**:
- `CLAUDE_CODE_PERMISSION_MODE`: Permission mode for SDK (default: `acceptEdits`)
- `DUMP_LLM_SESSION`: Enable session logging (`true`/`false`, default: `false`)
- `ENV_NAME`: Human-readable environment name

**Configuration File Management**:
- `.env` and `docker-compose.yml` are regenerated on every `rebuild` and `start` operation
- Fresh JWT tokens generated automatically to replace expired or invalid tokens
- `ANTHROPIC_API_KEY` always fetched from user settings via `crud.get_user_ai_credentials()`
- Configuration updates handled by `EnvironmentLifecycleManager._update_environment_config()`

### File Locations

**Static Templates** (Version Controlled):
- `/app/BUILDING_AGENT_EXAMPLE.md` → Copied to `/app/BUILDING_AGENT.md` during init
- `/app/core/` → System code, baked into Docker image

**Instance Files** (Per Environment):
- `/app/BUILDING_AGENT.md` → Instance-specific building prompt
- `/app/workspace/` → User workspace (volume mounted)
- `/app/workspace/scripts/` → Python scripts created by agent
- `/app/workspace/docs/` → WORKFLOW_PROMPT.md, ENTRYPOINT_PROMPT.md
- `/app/workspace/logs/` → Session logs (if enabled)

## Separation of Concerns

### Why Refactored?

**Before**: Monolithic `sdk_manager.py` with ~600 lines
- Mixed prompt loading, SDK coordination, logging, business logic
- Hard to test, maintain, and extend

**After**: Modular architecture with focused components
- `sdk_manager.py`: ~220 lines, focused on SDK coordination
- `prompt_generator.py`: Prompt construction logic
- `agent_env_service.py`: Business logic for file operations
- `sdk_utils.py`: Logging and debugging utilities

### Benefits

1. **Testability**: Each module can be tested independently
2. **Maintainability**: Changes isolated to relevant modules
3. **Extensibility**: Easy to add new SDKs or prompt strategies
4. **Clarity**: Clear module boundaries and responsibilities
5. **Reusability**: Services can be used outside HTTP context

## Key Workflows

### Building Mode Session

**Scenario**: User creates workflow automation scripts

1. Backend creates session with `mode="building"`, `agent_sdk="claude"`
2. User sends message: "Create a script to fetch emails and detect invoices"
3. Environment receives request at `/chat/stream`
4. `PromptGenerator` loads:
   - BUILDING_AGENT.md
   - scripts/README.md
   - WORKFLOW_PROMPT.md
   - ENTRYPOINT_PROMPT.md
5. SDK Manager creates Claude SDK client with:
   - Model: Default (Sonnet)
   - System prompt: Claude Code preset + all docs
   - Tools: Read, Edit, Glob, Grep, Bash, Write
6. Agent creates scripts, updates docs, maintains catalog
7. After session, backend syncs prompts from environment

### Conversation Mode Session

**Scenario**: User executes workflow task

1. Backend creates session with `mode="conversation"`, `agent_sdk="claude"`
2. User sends message: "Process my inbox for invoices"
3. Environment receives request at `/chat/stream`
4. `PromptGenerator` loads:
   - WORKFLOW_PROMPT.md (main system prompt)
   - scripts/README.md (available scripts)
5. SDK Manager creates Claude SDK client with:
   - Model: Haiku (fast, cheap)
   - System prompt: Workflow-focused string
   - Tools: Read, Edit, Glob, Grep, Bash, Write
6. Agent executes using pre-built scripts
7. Response streamed to user in real-time

### SDK Session Resumption

**Scenario**: Continue conversation context

1. First message: `session_id=None`
   - SDK creates new session
   - Returns `session_id` in response
   - Backend stores in `session_metadata["external_session_id"]`

2. Follow-up message: `session_id="existing_id"`
   - SDK loads conversation history
   - Maintains context across turns
   - Efficient multi-turn conversations

### Prompt Synchronization

**Backend → Environment** (Manual Edit):
1. User edits prompts in backend UI
2. Backend calls `POST /config/agent-prompts`
3. `AgentEnvService` writes to `/app/workspace/docs/`
4. Next session uses updated prompts

**Environment → Backend** (Building Session):
1. Building session completes
2. Backend calls `GET /config/agent-prompts`
3. Reads current prompts from environment
4. Updates `Agent.workflow_prompt` and `Agent.entrypoint_prompt`
5. Prompts available in UI and other environments

## Error Handling

### SDK Errors

**Import Error**: Claude SDK not installed
- Returns `{"type": "error", "error_type": "ImportError"}`
- Logged and yielded to frontend

**Invalid Mode**: Unsupported mode parameter
- Returns `{"type": "error", "error_type": "ValueError"}`
- Logged with details

**Connection Error**: SDK connection fails
- Logged with full stack trace
- Returns generic error to frontend
- Session marked as "error" in backend

### File Operation Errors

**Prompt Loading**: File not found or unreadable
- Logs warning, continues with available prompts
- Building mode degrades gracefully
- Conversation mode may have minimal context

**Prompt Writing**: Permission denied or disk full
- Raises `IOError` from `AgentEnvService`
- Returns HTTP 500 to backend
- Logged with specific error details

## Performance Considerations

### Prompt Caching

**Static Prompts** (Cached at Initialization):
- `BUILDING_AGENT.md` loaded once in `__init__`
- Reused across all building mode sessions

**Dynamic Prompts** (Loaded per Request):
- `scripts/README.md` - May change as agent creates scripts
- `WORKFLOW_PROMPT.md` - Updated during building sessions
- `ENTRYPOINT_PROMPT.md` - Updated during building sessions

### Model Selection Impact

**Haiku (Conversation Mode)**:
- Faster response times (~2-5s for typical requests)
- Lower cost (~1/10th of Sonnet)
- Suitable for execution tasks

**Sonnet (Building Mode)**:
- Better code generation quality
- Deeper reasoning for complex tasks
- Worth the cost for development sessions

### Session Logging Overhead

**Disabled** (default):
- No disk I/O overhead
- Minimal memory usage
- Production mode

**Enabled** (`DUMP_LLM_SESSION=true`):
- Each message written to `/app/workspace/logs/`
- File per session run
- Useful for debugging but increases I/O

## Security

### Authentication

**JWT Bearer Token** (Required):
- All HTTP endpoints require `Authorization: Bearer {token}` header
- Token is a signed JWT containing user ID as subject
- Generated by `EnvironmentLifecycleManager._generate_auth_token()` with 10-year expiration
- Regenerated on every environment rebuild and start operation
- Validated by backend using `get_current_user()` dependency in `backend/app/api/deps.py`
- Stored in environment config (`environment.config["auth_token"]`) and `.env` file
- Backend includes token in all requests to agent environment

**Token Lifecycle**:
- **Create**: Fresh JWT generated with agent owner's user ID
- **Rebuild**: New JWT generated, old token replaced in `.env` and docker-compose.yml
- **Start**: New JWT generated to ensure valid authentication
- **Verification**: JWT decoded and validated against `settings.SECRET_KEY` using HS256 algorithm

**Bypassed When**:
- `AGENT_AUTH_TOKEN` not configured (development/backward compatibility only)

### File Access

**Workspace Isolation**:
- Agent operates in `/app/workspace` only
- Cannot access `/app/core` (system files)
- Tools restricted to workspace directory

**Credential Security**:
- API keys stored in `/app/workspace/credentials/`
- Not logged or exposed in responses
- Agent can read but frontend cannot access directly

## Monitoring & Debugging

### Logging Levels

**INFO**: Major operations
- SDK client creation
- Prompt loading success
- Session ID capture
- Model selection

**DEBUG**: Detailed execution
- Raw SDK message structures
- Formatted message outputs
- File read/write operations

**ERROR**: Failures
- Import errors
- Connection failures
- File operation errors
- SDK exceptions

### Health Checks

**Endpoint**: `GET /health`

**Checked By**:
- Docker HEALTHCHECK directive
- Backend monitoring (`EnvironmentAdapter.health_check()`)
- Frontend status displays

**Indicates**:
- FastAPI server running
- Environment reachable
- Ready to accept requests

## Future Enhancements

### Multi-SDK Support (Partially Implemented)

**Current Status**: Multi-adapter architecture is in place with pluggable adapters.

**Implemented Adapters**:
- `claude-code/anthropic` - Claude Code SDK with Anthropic (default)
- `claude-code/minimax` - Claude Code SDK with MiniMax

**Placeholder Adapters** (not yet implemented):
- `google-adk-wr/gemini` - Google ADK with Gemini
- `google-adk-wr/vertex` - Google ADK with Vertex AI

**Adding New Adapters**:
1. Create new adapter class in `adapters/` directory
2. Inherit from `BaseSDKAdapter`
3. Implement `send_message_stream()` and `interrupt_session()` methods
4. Use `@AdapterRegistry.register` decorator
5. Convert SDK-specific messages to `SDKEvent` format
6. Add required API key validation in backend `environment_service.py`

### Prompt Versioning

**Need**: Track prompt changes over time

**Approach**:
- Version prompts in git
- Store version hash with sessions
- Reproduce exact prompt for debugging

### Advanced Logging

**Structured Logging**:
- JSON format for better parsing
- Include trace IDs for request correlation
- Send to centralized logging (e.g., Datadog, CloudWatch)

**Metrics**:
- Session duration
- Token usage per session
- Error rates by SDK
- Model performance comparison

## References

### Core Files

- `backend/app/env-templates/python-env-advanced/app/core/server/main.py`
- `backend/app/env-templates/python-env-advanced/app/core/server/routes.py`
- `backend/app/env-templates/python-env-advanced/app/core/server/sdk_manager.py`
- `backend/app/env-templates/python-env-advanced/app/core/server/prompt_generator.py`
- `backend/app/env-templates/python-env-advanced/app/core/server/agent_env_service.py`
- `backend/app/env-templates/python-env-advanced/app/core/server/sdk_utils.py`
- `backend/app/env-templates/python-env-advanced/app/core/server/models.py`
- `backend/app/env-templates/python-env-advanced/app/core/server/active_session_manager.py`
- `backend/app/env-templates/python-env-advanced/app/core/server/adapters/`
  - `base.py` - SDKEvent, SDKEventType, SDKConfig, BaseSDKAdapter, AdapterRegistry
  - `claude_code.py` - ClaudeCodeAdapter for claude-code/* variants
  - `google_adk.py` - GoogleADKAdapter placeholder for google-adk-wr/*

### Backend Integration

- `backend/app/services/message_service.py`
- `backend/app/services/session_service.py`
- `backend/app/services/environment_lifecycle.py`
- `backend/app/api/routes/messages.py`
- `backend/app/models/session.py`

### Related Documentation

- `docs/agent-sessions/agent_env_docker.md` - Docker architecture and rebuild operations
- `docs/agent-sessions/agent_env_building_prompt.md` - Building mode prompt construction
- `docs/agent-sessions/business_logic.md` - Overall system architecture
- `docs/file-management/remote_database_viewer.md` - SQLite database viewing/querying in UI (agent-env exposes `/database/*` endpoints for schema and query execution)
