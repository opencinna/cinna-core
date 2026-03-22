# Create Agent Task Tool (Agent-Env Side)

## Purpose

MCP tool running inside agent Docker containers that enables agents to create tasks in two modes: delegating directly to another agent (direct handover) or creating an inbox task for user review. Available only in **conversation mode** — building mode agents cannot use this tool.

## How It Works

The tool makes an authenticated HTTP call to the backend task-creation endpoint:

1. Agent invokes `mcp__task__create_agent_task` with a `task_message` and optional `target_agent_id`
2. Tool retrieves the current SDK session ID via `get_current_sdk_session_id()` and the backend session ID via `get_backend_session_id()` (both from global state in `sdk_manager.py`)
3. Makes authenticated `POST /api/v1/agents/tasks/create` with `source_session_id`
4. Backend creates the task and (for direct handover) creates and executes a new agent session
5. Tool returns a success confirmation to the agent

### Two Modes

**Direct Handover** (`target_agent_id` provided):
- Tool validates `target_agent_id` against the configured handovers in `agent_handover_config.json`
- Backend creates `InputTask` with `agent_initiated=true`, `auto_execute=true`
- Backend optionally auto-refines the message using the target agent's `refiner_prompt`
- A new session is created for the target agent; the (possibly refined) message is sent immediately
- Source session receives a system message with a link to the new session

**Inbox Task** (`target_agent_id` omitted):
- No target validation — any task message is accepted
- Backend creates `InputTask` with `agent_initiated=true`, `auto_execute=false`
- No session is created; task appears in user's inbox for manual assignment and execution
- Source session receives a system message with a link to the created task

## Registration

- Registered in the Claude Code adapter only when `mode == "conversation"` — not available in building mode
- Full tool name: `mcp__task__create_agent_task`
- Listed in backend's pre-allowed tools — agents can invoke without per-call user approval

## Session ID Access

Python's `contextvars.ContextVar` does not propagate into tool execution contexts created by the Claude SDK. The tool accesses session IDs through global state:

- `get_current_sdk_session_id()` — returns the active SDK session ID
- `get_backend_session_id(sdk_session_id)` — looks up the backend session UUID from a global map
- An async lock (`_sdk_session_lock`) in `send_message_stream()` serializes SDK sessions to prevent race conditions when multiple requests arrive concurrently

## System Prompt Integration

Handover availability is injected into the conversation mode system prompt by `_load_task_creation_prompt()` in `prompt_generator.py`. This function reads `agent_handover_config.json` and provides the agent with:
- Named list of available direct handover targets (from enabled configs)
- Instructions on inbox task creation (always available)

## Authentication

The tool uses the same bearer token as other agent-env → backend calls:

- `Authorization: Bearer <AGENT_AUTH_TOKEN>` — token generated at environment creation
- Validated against the environment record in the backend database

## File References

- **Tool implementation**: `backend/app/env-templates/app_core_base/core/server/tools/create_agent_task.py`
- **Adapter registration**: `backend/app/env-templates/app_core_base/core/server/adapters/claude_code_sdk_adapter.py`
- **Global session state + helpers**: `backend/app/env-templates/app_core_base/core/server/adapters/claude_code_sdk_adapter.py` (`get_current_sdk_session_id()`, `get_backend_session_id()`, `_sdk_session_lock`)
- **Prompt injection**: `backend/app/env-templates/app_core_base/core/server/prompt_generator.py` — `_load_task_creation_prompt()`
- **Config storage**: `backend/app/env-templates/app_core_base/core/server/agent_env_service.py` — `get_agent_handover_config()`
- **Runtime config file**: `{workspace}/docs/agent_handover_config.json`
- **Backend endpoint**: `backend/app/api/routes/agents.py` — `POST /api/v1/agents/tasks/create`
- **Pre-allowed list**: `backend/app/services/message_service.py`
- **System message rendering**: `frontend/src/components/Chat/MessageBubble.tsx` — renders task creation notifications with session/task links

## Related Docs

- [Agent Handover](../agent_handover/agent_handover.md) — feature documentation: configuration management, UI, business rules, clone behavior
- [Agent Handover Tech](../agent_handover/agent_handover_tech.md) — backend services, database schema, API endpoints, sync flow
- [Agent Environment Core](agent_environment_core.md) — parent feature: server running inside Docker containers
- [Knowledge Query Tool](knowledge_tool.md) — sibling aspect: RAG queries from building mode

