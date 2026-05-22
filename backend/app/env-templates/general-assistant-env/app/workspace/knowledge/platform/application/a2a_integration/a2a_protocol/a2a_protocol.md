# A2A Protocol Integration

## Purpose

Enables external agents and A2A-compatible tools to discover and communicate with platform agents through the standardized Agent-to-Agent (A2A) protocol. Agents are exposed as A2A-compliant endpoints supporting discovery, messaging, streaming, and task management.

## Core Concepts

| Concept | Description |
|---------|-------------|
| **AgentCard** | Discovery document describing agent capabilities, skills, and connection details |
| **Public Card** | Minimal AgentCard (name + URL) returned without authentication when A2A is enabled |
| **Extended Card** | Full AgentCard with skills, description, and extensions - requires authentication |
| **JSON-RPC** | Communication protocol for all A2A operations (send message, get task, cancel, etc.) |
| **A2A Task** | Maps to an internal Session - represents a conversation between external client and agent |
| **A2A Message** | Maps to an internal SessionMessage - a single communication within a task |
| **Skills** | AI-extracted capabilities from agent's workflow_prompt, stored in `a2a_config` |
| **A2A Protocol v1.0** | Latest protocol version with PascalCase methods and restructured AgentCard (default) |

## User Stories / Flows

### 1. Enabling A2A for an Agent

1. User navigates to agent detail page
2. Selects the **Integrations** tab
3. Toggles **A2A Integration** switch to enable
4. Agent Card URL is displayed with a copy button
5. URL format: `{API_URL}/api/v1/a2a/{agent_id}/`

### 2. External Client Discovery

1. Client requests AgentCard via GET to the agent's A2A URL (`/api/v1/a2a/{agent_id}/`)
2. Without auth: receives public card (name, URL, auth requirements)
3. With auth: receives extended card (name, description, skills, capabilities)
4. Client reads `supportedInterfaces` to find the versioned URL matching its protocol version
5. Client connects to that versioned URL for all subsequent requests

### 3. Sending a Message (Synchronous)

1. Client sends `SendMessage` JSON-RPC request with message content
2. Backend creates or retrieves session (task) based on provided task_id
3. For new sessions: title auto-generated from first message
4. If environment is suspended: activated and waited for before proceeding
5. Agent processes message and returns complete response as Task object

### 4. Streaming a Message (SSE)

1. Client sends `SendStreamingMessage` JSON-RPC request
2. Backend creates/retrieves session, yields initial `working` status
3. Environment activated if suspended
4. Agent response streamed as SSE events in the same chunked cadence the backend receives them from the agent-env — `assistant`, `tool`, and `thinking` events arrive incrementally, not in a burst at the end
5. Stream ends with final event (`completed`, `canceled`, or `failed`)

### 5. Task Management

1. `GetTask` - Retrieve task status and message history
2. `CancelTask` - Request cancellation of a running task; forwards the interrupt to the agent-env via HTTP (same path as the UI interrupt button); idempotent if the task is already terminal
3. `ListTasks` - List all tasks for the agent (custom extension)

### 6. Skills Generation Lifecycle

1. User updates agent's `workflow_prompt`
2. System detects change and triggers AI-based skill extraction
3. Extracted skills stored in `Agent.a2a_config` with auto-incremented version
4. Updated skills reflected in AgentCard on next discovery request

## Business Rules

### A2A Concept to Internal Model Mapping

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

### Public vs Extended Agent Card

| A2A Enabled | Auth Provided | Result |
|-------------|---------------|--------|
| No | No | 401 Unauthorized |
| No | Yes | Full card (if authorized) |
| Yes | No | Public card (name only) |
| Yes | Yes | Full card (if authorized) |

- Public card exposes: name, URL, securitySchemes, `supportsAuthenticatedExtendedCard=true`
- Extended card exposes: full skills, description, extensions, capabilities
- Both card types include `securitySchemes` with Bearer JWT authentication

### a2a_config Structure

- `enabled` - Boolean flag to enable/disable A2A for this agent (default: false)
- `skills` - List of AgentSkill objects (id, name, description, tags, examples)
- `version` - Semantic version string (auto-incremented)
- `generated_at` - ISO timestamp of last generation

### Protocol Version Selection

Protocol version is selected by the URL the client connects to:

| URL | Protocol | Notes |
|-----|----------|-------|
| `/api/v1/a2a/{agent_id}/` | v1.0 (latest) | Entry point for new clients |
| `/api/v1/a2a/v1.0/{agent_id}/` | v1.0 (explicit) | Identical behavior to base URL |
| `/api/v1/a2a/v0.3/{agent_id}/` | v0.3.0 (legacy) | Use slash-case method names |

Clients discover all supported versioned URLs via `supportedInterfaces` in the AgentCard. The `X-A2A-Stable` header is no longer supported.

See [A2A v1.0 Support](./a2a_v1_support.md) for detailed specification.

### SSE Streaming Event Flow

Events are delivered incrementally. `A2AStreamEventHandler` pushes each mapped event into an `asyncio.Queue` the moment `on_event` fires; the SSE generator drains the queue and yields to the client immediately. Clients receive `assistant`, `tool`, and `thinking` events in the same chunked cadence that the backend receives them from the agent-env — there is no end-of-stream burst.

| Internal Event Type | A2A TaskState | final | Notes |
|---------------------|---------------|-------|-------|
| stream_started | working | false | Stream initialization |
| assistant | working | false | Agent text response (with message) |
| tool | working | false | Tool execution (with message) |
| thinking | working | false | Agent thinking (with message) |
| tool_result_delta | working | false | Tool result chunk; carries a TextPart with `cinna.content_kind = "tool_result"`, `cinna.tool_id`, and `cinna.tool_stream` |
| stream_completed | completed | true | Used by internal event service |
| error | failed | true | Error occurred (with message) |
| interrupted | canceled | true | User requested cancellation |
| done | completed/canceled | true | Final stream event from MessageService |

#### Content-Kind Metadata on TextParts

A2A clients can inspect `TextPart.metadata` to distinguish the three kinds of agent content emitted during a stream. This metadata is placed on the `TextPart` (not on `Message`) because a single agent message can contain parts of mixed kinds.

| `cinna.content_kind` value | Meaning | Additional metadata keys |
|----------------------------|---------|-------------------------|
| `text` | Agent's final answer text (from `assistant` events) | — |
| `thinking` | Chain-of-thought reasoning (from `thinking` events) | — |
| `tool` | Tool-call narration (from `tool` events) | `cinna.tool_name` — name of the tool invoked; `cinna.tool_input` — structured tool arguments (JSON object); `cinna.tool_id` — opaque tool-call identifier string |
| `tool_result` | Tool result output (from `tool_result_delta` events — CLI stdout/stderr from `/run:*` commands and LLM-side tool result chunks) | `cinna.tool_id` — pairs this result back to its originating `tool`-kind TextPart; `cinna.tool_stream` — `"stdout"` or `"stderr"` |

- `cinna.tool_name`, `cinna.tool_input`, and `cinna.tool_id` are present on `TextPart.metadata` only when `cinna.content_kind` is `"tool"`.
- `cinna.tool_input` is included when the underlying SDK emits a dict of tool arguments; clients can use it to render the call without parsing the narration text.
- `cinna.tool_id` is included when the underlying SDK emits a non-empty identifier; clients can use it to pair a tool-call part with its later tool-result event and cross-reference the persisted streaming-event trace.
- Tool event text contains the raw tool-call narration; no prefix is added. Clients rely on `cinna.content_kind` and the additional tool metadata keys to identify and interpret tool parts.
- `cinna.tool_id` and `cinna.tool_stream` are present on `TextPart.metadata` only when `cinna.content_kind` is `"tool_result"`. `cinna.tool_stream` is always either `"stdout"` or `"stderr"`; it defaults to `"stdout"` when the underlying event omits a `stream` field, and unknown values are coerced to `"stdout"`.

#### History Replay (GetTask)

When a client calls `GetTask`, agent messages in `history` are returned with **multiple TextParts** — one per persisted streaming event (assistant, thinking, tool, tool_result). Each part carries its own `cinna.content_kind` metadata so clients replaying the history can reconstruct the full content breakdown. `tool_result_delta` events are expanded into their own TextParts with `cinna.tool_id` and `cinna.tool_stream` metadata, exactly mirroring the live-stream shape. Messages without a persisted streaming trace fall back to a single TextPart built from the stored message content.

### Environment Activation

- If environment is suspended: activates synchronously and waits for completion
- If environment is already running: proceeds immediately
- If environment is in error or other non-ready state: returns error

### Security Rules

- JWT authentication required for JSON-RPC endpoints (message/send, message/stream, etc.)
- Agent ownership validation (user must own agent or be superuser)
- Environment validation (agent must have active environment)
- JSON-RPC error codes for authorization failures (-32004)
- Supports both user JWT tokens and A2A access tokens

## Architecture Overview

```
A2A Client --> A2A Router --> A2A Request Handler --> Session/Message Services --> Agent Environment
                  |
           A2A Service (AgentCard)
                  |
           A2A Event Mapper (Internal --> A2A events)
                  |
           A2A Task Store (Session --> Task mapping)
```

### Service Layer Architecture

```
A2A Request Handler --+--> SessionService.send_session_message() (creates session + message)
                      +--> SessionService.get_session() (scope validation)
                      +--> SessionService.list_environment_sessions() (task listing)
                      +--> MessageService (message streaming)

A2A Task Store -------+--> SessionService.get_session()
                      +--> MessageService.get_last_message()
                      +--> MessageService.get_last_n_messages()
                      +--> A2AEventMapper (all A2A conversions)

A2A Event Mapper ---------> Centralized A2A protocol mapping logic
```

**Key Principle:** No direct database queries in A2A code. All data access goes through `SessionService` and `MessageService`.

### Message Flow

1. A2A Message parts extracted to text content
2. Task ID parsed and scope validated
3. Session created (if new) + message created via SessionService
4. For new sessions, title generation triggered in background
5. Environment activation check (activate suspended environments)
6. Streaming or synchronous response via MessageService
7. Internal events mapped to A2A format via A2AEventMapper

### Session Creation Flow (A2A)

1. Client sends message without task_id (or with invalid task_id)
2. Parser returns None (no existing session)
3. SessionService creates new session with `access_token_id` for scope tracking
4. Message created and associated with new session
5. Title generation triggered in background
6. Session ID returned in response for subsequent messages

## Integration Points

- **[A2A Access Tokens](../a2a_access_tokens/a2a_access_tokens.md)** - Scoped JWT tokens for external A2A client authentication
- **[A2A v1.0 Support](./a2a_v1_support.md)** - Protocol version adapter layer
- **[Agent Sessions](../../agent_sessions/agent_sessions.md)** - Session lifecycle and management
- **[Agent Environments](../../../agents/agent_environments/agent_environments.md)** - Docker container architecture
- **[MCP Integration](../../mcp_integration/agent_mcp_architecture.md)** - Comparable protocol integration (MCP)
- **[Agent Prompts](../../../agents/agent_prompts/agent_prompts.md)** - `workflow_prompt` is the source document for A2A skills extraction; changes to it (via UI edit or building session sync) trigger automatic skill regeneration

---

*Last updated: 2026-05-21*
