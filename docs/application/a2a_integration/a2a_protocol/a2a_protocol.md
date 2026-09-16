---
feature: a2a_protocol
domain: application
one_liner: "Exposes platform agents as standard A2A protocol endpoints so external agents and A2A-compatible tools can discover and message them."
docs:
  tech: a2a_protocol_tech.md
  v1 support: a2a_v1_support.md
---
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

`GetTask`/`tasks/get` (and the `message/send` poll) derive `TaskState` from internal state in a fixed rule order — first match wins:

| # | Condition | A2A TaskState |
|---|-----------|---------------|
| 1 | Last message has an unanswered tool question | `input-required` |
| 2 | `interaction_status = 'running'` | `working` |
| 3 | `interaction_status = 'pending_stream'` | `submitted` |
| 4 | A turn is in flight in this process (session lock held, or a stream registered) — or undelivered user messages exist | `working` |
| 5 | `status = 'error'` | `failed` |
| 6 | Last agent message of the turn is `user_interrupted` | `canceled` |
| 6 | Last agent message of the turn is `aborted` (turn crashed or was torn down before it finished) | `failed` |
| 7 | `status` in `active`, `completed` | `completed` |
| 8 | anything else | `working` (conservative default) |

Rule 4 is what makes `GetTask` safe to poll immediately after a send returns: a message still queued behind another turn (or not yet picked up) reads as `working`, never as a stale terminal state left over from an earlier turn.

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

A client disconnect does not stop the turn: the agent keeps running to completion server-side, detached from the SSE connection; events queued after the client is gone are dropped rather than accumulated. Concurrent sends to the same task never stream in parallel — a second send queues behind the first (see Crash Recovery below). Every stream ends with exactly one final event (`final=true`), even when the turn produced no events of its own (e.g. it was already answered by an earlier batched turn).

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

A2A clients can inspect `TextPart.metadata` to distinguish the different kinds of content emitted during a stream — agent-generated content (text, thinking, tool, tool_result) and platform-generated content (notice, command_result). This metadata is placed on the `TextPart` (not on `Message`) because a single agent message can contain parts of mixed kinds.

| `cinna.content_kind` value | Meaning | Additional metadata keys |
|----------------------------|---------|-------------------------|
| `text` | Agent's final answer text (from `assistant` events) | — |
| `thinking` | Chain-of-thought reasoning (from `thinking` events) | — |
| `tool` | Tool-call narration (from `tool` events) | `cinna.tool_name` — name of the tool invoked; `cinna.tool_input` — structured tool arguments (JSON object); `cinna.tool_id` — opaque tool-call identifier string; `cinna.command_invocation` — present only when the tool part originated from a Cinna slash command (e.g. `"/run:rotate_status"`) |
| `tool_result` | Tool result output (from `tool_result_delta` events — CLI stdout/stderr from `/run:*` commands and LLM-side tool result chunks) | `cinna.tool_id` — pairs this result back to its originating `tool`-kind TextPart; `cinna.tool_stream` — `"stdout"` or `"stderr"`; `cinna.command_invocation` — present only when the tool_result part originated from a Cinna slash command (e.g. `"/run:rotate_status"`) |
| `notice` | Platform-emitted informational text — not part of the agent's response. Currently used for the environment-activation hint ("Starting up the agent environment…") yielded before the agent stream begins. Notices are ephemeral: they are not persisted to message history and do not appear in `GetTask` replay. | — |
| `command_result` | Output of a platform slash command (e.g. `/files`, `/agent-status`) executed synchronously via A2A. Carried on the terminal `completed` status event's message when the inbound request matched a slash command — the agent stream does not run in this case. Treat as plain text output produced by the platform, not by the LLM (often terminal-style / structured rather than prose). | `cinna.command_invocation` — the verbatim slash invocation the user typed (e.g. `"/files"`, `"/agent-status"`) |

- `cinna.tool_name`, `cinna.tool_input`, and `cinna.tool_id` are present on `TextPart.metadata` only when `cinna.content_kind` is `"tool"`.
- `cinna.tool_input` is included when the underlying SDK emits a dict of tool arguments; clients can use it to render the call without parsing the narration text.
- `cinna.tool_id` is included when the underlying SDK emits a non-empty identifier; clients can use it to pair a tool-call part with its later tool-result event and cross-reference the persisted streaming-event trace.
- Tool event text contains the raw tool-call narration; no prefix is added. Clients rely on `cinna.content_kind` and the additional tool metadata keys to identify and interpret tool parts.
- `cinna.tool_id` and `cinna.tool_stream` are present on `TextPart.metadata` only when `cinna.content_kind` is `"tool_result"`. `cinna.tool_stream` is always either `"stdout"` or `"stderr"`; it defaults to `"stdout"` when the underlying event omits a `stream` field, and unknown values are coerced to `"stdout"`.
- `cinna.command_invocation` is a cross-content-kind key: it appears on `tool`, `tool_result`, and `command_result` parts, but only when the part originated from a Cinna slash command. Its value is the verbatim invocation string the user typed (e.g. `"/files"`, `"/agent-status"`, `"/run:rotate_status"`, `"/run:check"`). Absence of the key means the part was LLM-initiated — the current default for all non-command tool and tool_result parts.

#### History Replay (GetTask)

When a client calls `GetTask`, agent messages in `history` are returned with **multiple TextParts** — one per persisted streaming event (assistant, thinking, tool, tool_result). Each part carries its own `cinna.content_kind` metadata so clients replaying the history can reconstruct the full content breakdown. `tool_result_delta` events are expanded into their own TextParts with `cinna.tool_id` and `cinna.tool_stream` metadata, exactly mirroring the live-stream shape. `cinna.command_invocation` is preserved through replay — replay TextParts include the key whenever the persisted streaming events stored it, so live SSE and `GetTask` history produce the same shape. Messages without a persisted streaming trace fall back to a single TextPart built from the stored message content.

Each agent message in `history` also carries `cinna.message_state` on `Message.metadata`: `complete`, `streaming` (still generating — the currently open row), `aborted` (the turn crashed or the process was torn down before it finished — see Crash Recovery below), or `canceled` (the user requested cancellation). A user message carries `cinna.client_message_id` on `Message.metadata` when the caller supplied a `messageId` on send (see Idempotency below).

For the in-progress row specifically, `GetTask` merges the persisted streaming events with the in-memory live buffer of this process's active stream, so a client that reconnects mid-turn sees the same content a live SSE consumer would — not just what has already been flushed to the database (flushes happen roughly every 2 seconds).

#### Crash Recovery

The agent turn is detached from the SSE connection carrying `SendStreamingMessage`/`message/stream`: if the client disconnects, the turn keeps running to completion server-side rather than being cancelled. Recovery is by polling, not by reconnecting to the same stream — call `GetTask`/`tasks/get` until the state is terminal (`completed`, `failed`, `canceled`) or `input-required`. `SubscribeToTask`/`tasks/resubscribe` is not implemented.

Two sends to the same task never stream concurrently. The second `SendStreamingMessage` yields its own initial `working` acknowledgement immediately, then queues behind the session's turn lock (wait mode — it is never rejected outright) until the first turn's teardown completes. That lock is per backend worker: with several workers running, two sends to the same session that land on different workers can still stream concurrently instead of queuing behind each other — a documented multi-worker gap, not a fix.

If the backend process itself is killed mid-turn (not just the client disconnecting), the in-progress agent message is left `streaming` until a process restart. The status-repair background sweep's orphan-stream pass (see [Status Repair](../../../system/status_repair/status_repair.md)) then seals it as `aborted` — a few minutes after the crash by default — so `GetTask` eventually reports `failed` rather than `working` forever. A cancel the user actually requested (the interrupt/stop button, or `CancelTask`) is sealed as `canceled` instead.

The same seal-on-exit also covers a stream that ends in an **error** before it finished writing: the partial agent message is sealed `aborted` rather than left `streaming` forever, exactly like a disconnect or a crash. The error itself is still recorded separately (a `system` message with an error status), and `GetTask` still reports `failed` for the turn — only the partial agent row's own status changes.

#### Idempotency (`messageId`)

`SendMessage`/`SendStreamingMessage` may include the caller's own `message.messageId`. The platform stores it on the user message row and, for an existing task, dedupes a resend before any command runs or any row is written:

- If the earlier message with that `messageId` was never delivered to the agent (its turn failed before collecting it — e.g. the environment never came up), the resend re-drives delivery. No new row is written.
- Otherwise the resend returns the status of the turn the original message opened — `message/send` polls it like a fresh send; `message/stream` returns a single status event and closes without re-running the turn: the turn's final state if it has one, or a **non-final `working`** event if it's still open, including the edge case of a resend that races the original send before it has even been stored (an in-flight duplicate claim, no message row yet). Either way, a client that gets a non-final event back from `message/stream` must fall back to polling `GetTask`/`tasks/get` for the outcome — the same recovery contract as any other in-progress turn.
- Dedupe is scoped to the task: a first message that creates a new session is never deduped (there is no `taskId` yet). Callers should key resend attempts on the `taskId` returned by the first response.
- A blank or overlong (over 255 characters) `messageId` is treated as absent and never takes part in dedupe.
- The in-flight duplicate claim (the edge case above, before the first row is stored) is also per backend worker: two resends racing each other in that narrow window can both run if they land on different workers. Every resend after the first row exists is deduped by the stored `messageId` regardless of which worker handles it, so this exposure is limited to that first race.

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

*Last updated: 2026-09-16 — A2A crash recovery (C1–C4): detached producer, tasks/get in-flight state, cancel finalize + orphan-stream seal, client messageId dedupe*
