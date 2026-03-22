# Agent Environment Core

## Purpose

Server-side application running inside each Docker agent environment container. Provides the HTTP API, SDK integration, prompt management, and business logic that enables agent sessions to execute in isolated environments.

## Core Concepts

### Agent Environment Server

A FastAPI application deployed inside each Docker container at `/app/core/server/`. It receives requests from the backend, routes them through the appropriate SDK adapter, generates system prompts, and streams responses back.

### SDK Adapters

Pluggable adapter system supporting multiple AI providers. Each adapter converts SDK-specific messages into a unified `SDKEvent` format. Adapters are registered via a decorator-based registry and selected at runtime based on environment variables.

- **Adapter ID format**: `<adapter-type>/<provider>` (e.g., `claude-code/anthropic`, `opencode/openai`)
- **Three SDK engines:**
  - **Claude Code** — Anthropic's CLI agent SDK, communicates via Python subprocess (`claude_agent_sdk`). Supports `anthropic`, `minimax`.
  - **OpenCode** — Open-source multi-provider agent running as an HTTP server (`opencode serve`). Supports `anthropic`, `openai`, `openai_compatible`, `google`. Custom tools are bridged via local MCP servers.
  - **Google ADK** — Google's Agent Development Kit, runs in-process. Supports `openai_compatible`, `google`.

### Session Modes

- **Building Mode** - Development state. Uses Claude Sonnet for creating scripts, configuring integrations, updating documentation. Full Claude Code preset with all workspace docs
- **Conversation Mode** - Execution state. Uses Claude Haiku for running pre-built workflows. Lightweight prompt with workflow context only

### Custom Tools

Extension tools that give agents additional capabilities beyond built-in SDK tools:
- **Knowledge Query** - RAG-based queries against indexed knowledge sources
- **Create Agent Task** - Handover delegation to other agents
- **Respond to Task** - Reply to tasks received from other agents
- **Update Session State** - Modify session metadata during execution

### Unified Event Format

All SDK adapters emit `SDKEvent` objects with a common structure:
- **type** - Event category: SESSION_CREATED, ASSISTANT, TOOL_USE, DONE, ERROR
- **content** - Human-readable message text
- **session_id** - SDK session identifier for resumption
- **metadata** - Additional event-specific data

### Session Context

Server-verified metadata injected into system prompts for integration-aware behavior. Includes integration type, sender email, subject, agent ID, and clone info. HMAC-signed by the backend to prevent tampering.

## User Stories / Flows

### 1. Building Mode Session

1. Backend creates session with `mode="building"`
2. User sends message (e.g., "Create a script to fetch emails and detect invoices")
3. Backend sends HTTP POST to `/chat/stream` on the environment container
4. Prompt generator loads: BUILDING_AGENT.md + scripts/README.md + WORKFLOW_PROMPT.md + ENTRYPOINT_PROMPT.md
5. SDK manager creates Claude client with Sonnet model and Claude Code preset
6. Agent creates scripts, updates docs, maintains catalog
7. Response events streamed back to backend, then to frontend
8. After session, backend syncs updated prompts from environment

### 2. Conversation Mode Session

1. Backend creates session with `mode="conversation"`
2. User sends message (e.g., "Process my inbox for invoices")
3. Backend sends HTTP POST to `/chat/stream` on the environment container
4. Prompt generator loads: WORKFLOW_PROMPT.md + scripts/README.md + optional session context
5. SDK manager creates Claude client with Haiku model and lightweight prompt
6. Agent executes using pre-built scripts
7. Response events streamed to user in real-time

### 3. SDK Session Resumption

1. First message: `session_id=None` - SDK creates new session, returns `session_id` in response
2. Backend stores `session_id` in `session_metadata["external_session_id"]`
3. Follow-up messages: `session_id="existing_id"` - SDK loads conversation history, maintains context

### 4. Prompt Synchronization

**Backend to Environment** (manual edit):
1. User edits prompts in backend UI
2. Backend calls `POST /config/agent-prompts`
3. Agent env service writes files to `/app/workspace/docs/`
4. Next session uses updated prompts

**Environment to Backend** (building session):
1. Building session completes
2. Backend calls `GET /config/agent-prompts`
3. Reads current prompts from environment
4. Updates Agent model with new prompts
5. Prompts available in UI and for other environments

## Business Rules

### Adapter Selection

- Adapter determined by `SDK_ADAPTER_{MODE}` environment variable per mode
- Adapter ID parsed as `<adapter-type>/<provider>` - registry instantiates the correct adapter
- Adapters are cached per mode after first instantiation

### Prompt Loading

- **Static prompts** (BUILDING_AGENT.md) cached at server initialization, reused across sessions
- **Dynamic prompts** (scripts/README.md, WORKFLOW_PROMPT.md, ENTRYPOINT_PROMPT.md) loaded per request since they may change
- Missing prompt files degrade gracefully - logs warning, continues with available prompts

### Model Selection

- **Building mode**: Default model (Sonnet/larger) for better code generation quality
- **Conversation mode**: Haiku/smaller model for faster responses and lower cost
- **Model Override**: Each environment can specify an explicit model per mode (`model_override_building`, `model_override_conversation`). When set, this overrides the adapter's default. Examples: `claude-opus-4`, `gpt-4o`, `gemini-2.5-pro`.

### Session Context Verification

- Backend HMAC-signs `session_context` with `AGENT_AUTH_TOKEN` using HMAC-SHA256 before sending
- Environment verifies signature before storing - forged context is rejected
- Per-session context store keyed by `backend_session_id` supports parallel sessions
- Cleanup on stream end + TTL-based cleanup (24h) as fallback
- Session context injected as a "Server-Verified, Read-Only" section in system prompts
- Scripts bypass LLM and query context directly via `GET /session/context?session_id=X`

### Authentication

- All HTTP endpoints require `Authorization: Bearer {token}` header
- Token is a signed JWT (HS256) with user ID as subject, 10-year expiration
- Regenerated on every environment rebuild and start operation
- Bypassed only when `AGENT_AUTH_TOKEN` not configured (development mode)

## Architecture Overview

```
Backend ──HTTP POST──→ Environment Container (FastAPI)
                           │
                           ├──→ Routes (routes.py) ── validates, stores context
                           │        │
                           ├──→ SDK Manager ── selects adapter, manages lifecycle
                           │        │
                           ├──→ Prompt Generator ── builds mode-specific system prompts
                           │        │
                           ├──→ SDK Adapter ── sends to LLM, streams SDKEvents
                           │        │   ├── ClaudeCodeAdapter (subprocess, port-less)
                           │        │   ├── OpenCodeAdapter (HTTP → opencode serve :4096)
                           │        │   │       └── MCP bridge servers (custom tools)
                           │        │   └── GoogleADKAdapter (in-process library)
                           │        │
                           └──→ Agent Env Service ── workspace file operations

                    ←──SSE stream──┘
```

## Integration Points

- **Agent Environments** - Core runs inside the Docker container defined by [Agent Environments](../agent_environments/agent_environments.md)
- **Agent Sessions** - Backend session service creates/manages sessions that invoke environment core endpoints. See [Agent Sessions](../../application/agent_sessions/agent_sessions.md)
- **Agent Prompts** - Prompt generator loads and constructs prompts for both modes. See [Agent Prompts](../agent_prompts/agent_prompts.md) <!-- TODO: create agent_prompts docs -->
- **Multi SDK** - Adapter system enables multi-provider support. See [Multi SDK](multi_sdk.md)
- **Streaming** - Response events flow through the backend streaming pipeline. See [Streaming](../../application/realtime_events/frontend_backend_agentenv_streaming.md)
- **Agent Environment Data Management** - Prompt sync and credential sync operations. See [Data Management](../agent_environment_data_management/agent_environment_data_management.md)
- **Knowledge Query Tool** - RAG-based knowledge queries from building mode. See [Knowledge Tool](knowledge_tool.md) aspect and [Knowledge Sources](../../application/knowledge_sources/knowledge_sources.md)
- **Agent Handover** - Create/respond task tools enable agent-to-agent delegation. See [Agent Handover](../agent_handover/agent_handover.md) and [Create Agent Task Tool](create_agent_task_tool.md)
- **Input Tasks** - Session state tools let agents report task outcomes and exchange clarifications. See [Session State Tools](session_state_tools.md) and [Input Tasks](../../application/input_tasks/input_tasks.md)
- **Tools Approval** - Plugin-provided tools require explicit user approval before autonomous use; approval state synced to environments. See [Tools Approval Management](tools_approval_management.md)
- **Agent Webapp** - Three dedicated endpoints serve static files, execute Python data scripts, and report webapp metadata from `/app/workspace/webapp/`. See [Agent Webapp](../agent_webapp/agent_webapp.md)

## API Endpoints

### Chat

- `POST /chat/stream` - Streaming chat (SSE). Accepts message, mode, agent_sdk, session_id. Returns server-sent events: session_created, assistant, tool, done, error
- `POST /chat` - Synchronous chat (non-streaming). Same request model, returns complete response text

### Configuration

- `GET /config/agent-prompts` - Get current prompts (workflow_prompt, entrypoint_prompt) from workspace docs
- `POST /config/agent-prompts` - Update prompts in workspace docs directory

### Webapp

- `GET /webapp/status` - Webapp metadata: exists, total_size_bytes, file_count, has_index, api_endpoints
- `GET /webapp/{path:path}` - Serve static file from `webapp/` with ETag/Last-Modified/304 caching headers
- `POST /webapp/api/{endpoint}` - Execute Python data script (`webapp/api/{endpoint}.py`) via stdin/stdout, timeout up to 300s

### Utility

- `GET /health` - Health check (status, timestamp, uptime). Used by Docker HEALTHCHECK and backend monitoring
- `GET /session/context` - Get session context metadata. Optional `?session_id=` for per-session HMAC-verified context
- `GET /sdk/sessions` - List SDK sessions (debugging)
- `DELETE /sdk/sessions/{session_id}` - Close SDK session (sessions auto-cleanup)

## Environment Variables

**Required:**
- `CLAUDE_CODE_WORKSPACE` - Path to workspace (`/app/workspace`)
- `ENV_ID` - Environment UUID
- `AGENT_ID` - Agent UUID
- `AGENT_AUTH_TOKEN` - JWT bearer token for backend API authentication

**Anthropic Credentials** (auto-detected by prefix):
- `ANTHROPIC_API_KEY` - Traditional API key (prefix: `sk-ant-api*`)
- `CLAUDE_CODE_OAUTH_TOKEN` - OAuth token (prefix: `sk-ant-oat*`)

**SDK Adapter Configuration:**
- `SDK_ADAPTER_BUILDING` - Adapter ID for building mode (default: `claude-code/anthropic`)
- `SDK_ADAPTER_CONVERSATION` - Adapter ID for conversation mode (default: `claude-code/anthropic`)

**OpenCode Credentials** (injected into `.opencode/` config files, not env vars):
- All OpenCode provider credentials (Anthropic, OpenAI, Google, OpenAI-compatible) are written to per-mode config JSON files by `_generate_opencode_config_files()` in `environment_lifecycle.py`

**Optional:**
- `CLAUDE_CODE_PERMISSION_MODE` - Permission mode for SDK (default: `acceptEdits`)
- `DUMP_LLM_SESSION` - Enable session logging to `/app/workspace/logs/` (`true`/`false`)
- `ENV_NAME` - Human-readable environment name

## File Layout (Inside Container)

```
/app/
├── BUILDING_AGENT.md              # Instance-specific building prompt (copied from template on init)
├── core/                          # System code (from shared app_core_base, read-only mount, baked into Docker image)
│   ├── prompts/
│   │   ├── BUILDING_AGENT.md      # Building mode system prompt
│   │   └── WEBAPP_BUILDING.md     # Webapp building instructions (read by agent on demand)
│   ├── server/                    # FastAPI server application
│   │   ├── routes.py              # HTTP endpoints + session context helper + webapp endpoints
│   │   ├── sdk_manager.py         # Multi-adapter SDK orchestrator
│   │   ├── prompt_generator.py    # System prompt construction
│   │   ├── agent_env_service.py   # Workspace file operations
│   │   ├── active_session_manager.py  # Per-session context store (HMAC-verified)
│   │   ├── models.py              # Pydantic request/response models
│   │   ├── sdk_utils.py           # Logging and debugging utilities
│   │   ├── adapters/              # SDK adapter implementations
│   │   │   ├── base.py            # SDKEvent, SDKConfig, BaseSDKAdapter, AdapterRegistry
│   │   │   ├── claude_code.py     # ClaudeCodeAdapter (claude-code/* variants)
│   │   │   ├── opencode_adapter.py  # OpenCodeAdapter (opencode/* variants, HTTP client)
│   │   │   ├── opencode_event_adapter.py  # OpenCodeEventAdapter — stateful SSE event translator
│   │   │   ├── google_adk.py      # GoogleADKAdapter (google-adk-wr/* variants)
│   │   │   ├── tool_name_registry.py  # Unified lowercase tool name convention: maps, PRE_APPROVED_TOOLS, normalize_tool_name()
│   │   │   └── sqlite_session_service.py  # SQLite-based session persistence
│   │   └── tools/                 # Custom agent tools
│   │       ├── knowledge_query.py # RAG knowledge source queries
│   │       ├── create_agent_task.py  # Agent-to-agent task creation
│   │       ├── respond_to_task.py    # Task response tool
│   │       ├── update_session_state.py  # Session state modification
│   │       └── mcp_bridge/        # MCP stdio servers for OpenCode custom tools
│   │           ├── knowledge_server.py   # Wraps knowledge_query tool
│   │           ├── task_server.py        # Wraps create_agent_task, respond_to_task, update_session_state
│   │           └── collaboration_server.py  # Wraps create_collaboration, post_finding, get_collaboration_status
│   └── scripts/
│       └── get_session_context.py # Stdlib-only helper for agent scripts
└── workspace/                     # User workspace (volume-mounted, persists across rebuilds)
    ├── scripts/                   # Python scripts created by agent
    │   └── README.md              # Scripts catalog (auto-maintained)
    ├── docs/                      # Agent prompts
    │   ├── WORKFLOW_PROMPT.md      # Workflow system prompt
    │   └── ENTRYPOINT_PROMPT.md   # Trigger message definition
    ├── credentials/               # API keys and service accounts
    ├── logs/                      # Session logs (when enabled)
    └── webapp/                    # Web app files (HTML/CSS/JS + Python data scripts)
        ├── index.html             # Entry point
        ├── assets/                # CSS, JS, images
        └── api/                   # Python data endpoint scripts
```

**Source of truth**: All `app/core/` files live in `backend/app/env-templates/app_core_base/core/` and are shared across all environment templates. Individual templates (`general-env`, `python-env-advanced`) only contain template-specific files (Dockerfile, docker-compose template, workspace structure).

