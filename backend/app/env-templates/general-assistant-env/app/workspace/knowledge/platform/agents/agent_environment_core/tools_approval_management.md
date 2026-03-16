# Tools Approval Management

## Purpose

Controls which tools agent environments are permitted to use autonomously. Plugin-provided tools (e.g., MCP server tools) require explicit user approval before the agent can use them without pausing execution.

## Core Concepts

### Two-Tier Tool Authorization

**Pre-Allowed Tools** (no approval required)
- Core SDK tools always permitted: Read, Edit, Glob, Grep, Bash, Write, WebFetch, WebSearch, TodoWrite
- Custom tools added per mode: knowledge query tool (building mode), agent handover tools (conversation mode)
- Hardcoded in `sdk_manager.py`, always included in SDK initialization options

**User-Approved Tools** (require explicit approval)
- Tools introduced by installed plugins (MCP servers, custom commands)
- Stored in the agent's `agent_sdk_config.allowed_tools` field
- Synced to agent environments via `settings.json`

### Approval Persistence

Tool approvals are stored at the agent level, not the session level:
- Persist across all sessions
- Shared between building and conversation modes
- Propagated to all active environments when updated

### Settings.json Integration

Tool approvals are bundled with plugin configuration in a single settings file:
- `settings.json` contains both `active_plugins` and `allowed_tools`
- Shares the same sync mechanism as plugin configuration
- Environment reads settings on each SDK session initialization

## User Stories / Flows

### 1. Approving a Plugin Tool

1. User installs a plugin that provides MCP tools (e.g., Context7)
2. Agent executes and encounters an unapproved tool → SDK pauses execution
3. Message stored with `tools_needing_approval` in metadata
4. Frontend shows "Approve Tools" button on the message
5. User clicks approve → backend updates `agent.agent_sdk_config.allowed_tools`
6. Backend syncs updated `settings.json` to running environment
7. On next SDK session initialization, merged tool list (pre-allowed + user-approved) is used
8. Subsequent executions proceed without interruption

### 2. Tool Approval Display on Page Reload

1. Message created with `tools_needing_approval` stored as a metadata snapshot
2. User approves tools → `agent.agent_sdk_config.allowed_tools` updated in DB
3. User reloads page → `GET /sessions/{id}/messages` called
4. Backend filters `tools_needing_approval` against current `allowed_tools` (response-only, no DB write)
5. Frontend receives filtered list — button only shows for genuinely pending tools

This prevents stale approval buttons appearing after tool approval.

## Business Rules

### Pre-Allowed vs User-Approved

- Core SDK tools are always pre-allowed regardless of plugin state
- Every new tool from a plugin requires at least one explicit user approval
- Approval is permanent — no per-session re-approval needed once granted

### Approval Storage

- Approvals stored in `agent.agent_sdk_config.allowed_tools` (JSON field on agent model)
- No duplicates stored — approving an already-approved tool is idempotent
- Agent-level storage means one approval covers all environments for that agent

### Sync Mechanism

- Same pattern as plugin configuration sync: backend updates DB, then syncs `settings.json` to environment
- Only `settings.json` needs updating — no plugin file re-sync required
- Running environments receive approvals immediately upon user action

### Message-Level Filtering

- `tools_needing_approval` in message metadata is a point-in-time snapshot
- On message fetch, backend filters this field against current `allowed_tools` (response-only, no DB write)
- Ensures approval UI is accurate regardless of when tools were approved relative to page load

## Architecture Overview

```
User approves tools
        │
        ▼
Backend API ──→ agent_service.add_allowed_tools() ──→ Agent.agent_sdk_config.allowed_tools (DB)
        │
        ▼
environment_lifecycle._sync_plugins_to_environment()
        │
        ▼
llm_plugin_service.prepare_plugins_for_environment(allowed_tools=...)
        │
        ▼
Environment Container: /app/workspace/plugins/settings.json
        │
        ▼
sdk_manager.send_message_stream()
        │
        ├── pre-allowed tools (hardcoded)
        ├── user-approved tools (from settings.json)
        └── merged list → ClaudeAgentOptions(allowed_tools=...)
```

## Integration Points

- **Agent Plugins** — Plugin installation introduces new MCP tools that enter the approval queue. See [Agent Plugins](../agent_plugins/agent_plugins.md)
- **Multi SDK** — Pre-allowed tool merging and `ClaudeAgentOptions` construction happen in the SDK manager. See [Multi SDK](multi_sdk.md)
- **Agent Credentials** — Same environment HTTP sync pattern used for credential and plugin updates. See [Agent Credentials](../agent_credentials/agent_credentials.md)
- **Agent Sessions** — `tools_needing_approval` is attached to messages during streaming; filtered on fetch. See [Agent Sessions](../../application/agent_sessions/agent_sessions.md)
