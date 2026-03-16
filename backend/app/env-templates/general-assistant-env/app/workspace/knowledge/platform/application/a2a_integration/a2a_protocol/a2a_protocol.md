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

1. Client requests AgentCard via GET to the agent's A2A URL
2. Without auth: receives public card (name, URL, auth requirements)
3. With auth: receives extended card (name, description, skills, capabilities)
4. Client inspects skills and capabilities to determine interaction approach

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
4. Agent response streamed as SSE events (status updates with messages)
5. Stream ends with final event (`completed`, `canceled`, or `failed`)

### 5. Task Management

1. `GetTask` - Retrieve task status and message history
2. `CancelTask` - Request cancellation of a running task
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

| Header | Behavior |
|--------|----------|
| (none) | v1.0 format (default) |
| `X-A2A-Stable: 1` | Legacy format (v0.3.0 draft) |

See [A2A v1.0 Support](./a2a_v1_support.md) for detailed specification.

### SSE Streaming Event Flow

| Internal Event Type | A2A TaskState | final | Notes |
|---------------------|---------------|-------|-------|
| stream_started | working | false | Stream initialization |
| assistant | working | false | Agent text response (with message) |
| tool | working | false | Tool execution (with message) |
| thinking | working | false | Agent thinking (with message) |
| stream_completed | completed | true | Used by internal event service |
| error | failed | true | Error occurred (with message) |
| interrupted | canceled | true | User requested cancellation |
| done | completed/canceled | true | Final stream event from MessageService |

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

*Last updated: 2026-03-02*
