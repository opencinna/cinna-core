# Agent Sessions

## Purpose

A session is a persistent chat conversation between a user (or external system) and an agent environment. Sessions provide message history, streaming responses, and state tracking while the agent executes tasks inside an isolated Docker container.

## Core Concepts

- **Session** — A persistent conversation thread tied to a specific agent environment. Holds all messages, streaming state, and outcome metadata
- **Message** — A single communication unit within a session. Roles: `user`, `agent`, `system`. Messages are immutable once created
- **Session Mode** — Determines agent behavior and context window:
  - `building` — Development state. Agent uses Claude Sonnet, receives full workspace context, can create/modify files and configure integrations
  - `conversation` — Execution state. Agent uses Claude Haiku, receives lightweight workflow context, executes pre-built scripts
- **Session Status** — Overall lifecycle: `active`, `paused`, `completed`, `error`
- **Interaction Status** — Real-time streaming state: `""` (idle), `"running"` (stream active), `"pending_stream"` (waiting for environment to activate)
- **Result State** — Agent-declared outcome set via `update_session_state` tool: `completed`, `needs_input`, `error`. Auto-reset to `null` when user sends the next message
- **Result Summary** — Agent-provided description accompanying the result state (question, error message, completion note)
- **Integration Type** — How the session was initiated: `null` (manual), `"email"`, `"a2a"`, or `"mcp"` (tracked via `mcp_connector_id`)
- **Source Task** — Input task that spawned this session, tracked via `source_task_id` backlink
- **Todo Progress** — Real-time task completion progress captured from agent's TodoWrite tool calls
- **External Session ID** — SDK-level session identifier stored in `session_metadata`, used to resume conversation context across messages

## User Stories / Flows

### Flow 1: Manual Session Creation and Chat

1. User navigates to agent page, clicks "New Session"
2. System creates session against the agent's active environment
3. User is redirected to the session chat page
4. User types a message and sends it
5. Backend creates user message, triggers environment activation if needed
6. Agent processes message; response streams in real time via WebSocket
7. Streaming events render progressively: assistant text, tool use, thinking
8. After stream completes, final message persisted and displayed

### Flow 2: Session Created by Input Task Execution

1. User (or automated trigger) executes an Input Task
2. System creates a session with `source_task_id` linking it to the task
3. Refined task description is injected as the first message
4. Task status syncs with session state in real time
5. When agent calls `update_session_state(state, summary)` → task transitions to corresponding status

### Flow 3: Email-Initiated Session

1. IMAP polling detects new email for an email-enabled agent
2. System creates session with `integration_type="email"` and `email_thread_id`
3. Email body injected as first user message; agent auto-responds
4. Subsequent emails in the same thread are matched to the existing session via `email_thread_id`
5. Agent response is queued and delivered via SMTP to the original sender

### Flow 4: A2A-Initiated Session

1. External agent sends `SendMessage` JSON-RPC request to agent's A2A endpoint
2. System creates session with `integration_type="a2a"` and `access_token_id` for scope tracking
3. Agent processes message and returns response (synchronous or SSE streaming)
4. Task ID (= session ID) returned to caller for subsequent message routing

### Flow 5: MCP-Initiated Session

1. External MCP client calls `send_message` tool via authenticated MCP connector
2. System creates session with `mcp_connector_id` and `MCPSessionMeta` record
3. Session is isolated per `context_id` (platform session UUID) echoed back by the LLM
4. Multiple MCP clients can maintain independent conversations on the same connector

### Flow 6: Guest Session

1. Guest accesses shared agent via `/guest/{token}` URL
2. After security code verification, guest JWT is issued
3. Session created with `guest_share_id` and `user_id = agent.owner_id`
4. Guest chats in conversation mode only; owner sees guest sessions in their session list

### Flow 7: Environment Auto-Activation

1. User sends message to session whose environment is `suspended` or `stopped`
2. Backend sets `interaction_status = "pending_stream"` on session
3. Environment activation triggered in background
4. When environment reaches `running` → `ENVIRONMENT_ACTIVATED` event fires
5. Backend processes all pending sessions for that environment automatically
6. Streaming begins; frontend detects state change via WebSocket event

## Business Rules

### Session Creation

- Session is always created against a specific environment (not directly against an agent)
- Agent must have an active environment; environment does not need to be `running` at creation time
- Session mode defaults to `conversation`; building mode requires explicit selection
- Guest share sessions are forced to `conversation` mode regardless of user preference

### Session Lifecycle

| Status | Description |
|--------|-------------|
| `active` | Default state; session can receive messages |
| `paused` | Not currently used operationally |
| `completed` | Agent or user explicitly marked as done |
| `error` | Unrecoverable error occurred |

### Interaction Status Transitions

```
idle ("") → running → idle ("")
        → pending_stream → running → idle ("")
```

- `pending_stream` is set when a message is sent but the environment is not yet ready
- `running` is set when the backend begins streaming from the agent environment
- Cleared back to `""` on stream completion, error, or interruption
- `streaming_started_at` is set on `running` and cleared on completion

### Result State Rules

- Set by agent via `update_session_state(state, summary)` tool inside the environment
- Transitions linked task to `PENDING_INPUT`, `COMPLETED`, or `ERROR` status
- Auto-reset to `null` when user sends a new message (enables task to return to `RUNNING`)
- Previous state passed to agent environment as `session_state` context on next message

### Message Rules

- Messages are immutable once created (audit trail)
- Sequence numbers are unique per session and monotonically incrementing
- User messages start with `sent_to_agent_status = "pending"`, set to `"sent"` after delivery
- Agent messages accumulate content via incremental DB flushes every ~2 seconds during streaming
- `message_metadata.streaming_events` stores all streaming events with `event_seq` for deduplication

### Cascade Delete

- Deleting a session removes all its messages
- Deleting an **environment** detaches its sessions (`environment_id` is SET NULL); on the next message the session auto-rebinds to the agent's current active environment (see [Detaching and Rebinding](#detaching-and-rebinding) below). Messages on detached sessions are preserved.
- Deleting an **agent** removes all of that agent's sessions explicitly before its environments are cascade-deleted (preserves the "delete agent → delete everything" business rule)
- `source_task_id` is SET NULL on session delete; task status is recomputed from remaining sessions
- `guest_share_id` is SET NULL on guest share delete; sessions are preserved

### Detaching and Rebinding

A session is considered **detached** when `environment_id IS NULL` — typically because the environment it was originally created against has been deleted (e.g., an old non-active env was cleaned up). On the next message send, `SessionService.resolve_session_environment` rebinds the session to the agent's current `active_environment_id`:

- If the agent has an active environment → `environment_id` is updated, the message proceeds as normal.
- If the agent has no active environment → the send fails with `"Agent has no active environment"`.

The same helper also handles the stale-but-not-detached case: a session pointing at a non-active environment is switched to the active one so the user doesn't accidentally chat with an older replica.

### Authorization

- Users can only access sessions belonging to their agents or explicitly shared with them
- Guest access scoped by `guest_share_id` — guests see only sessions matching their share token
- A2A sessions scoped by `access_token_id` — external clients access only their own sessions
- Building mode blocked for guest share sessions and MCP sessions

## Architecture Overview

```
Session Initiators:
  User (manual)        → Frontend → POST /api/v1/sessions
  Input Task execution → InputTaskService → SessionService.create_session()
  Email polling        → EmailProcessingService → SessionService
  A2A client           → A2ARequestHandler → SessionService.send_session_message()
  MCP client           → MCPRequestHandler → SessionService.get_or_create_mcp_session()
  Agent handover tool  → create_agent_task → InputTaskService → SessionService

Message Flow:
  User sends message
    → POST /api/v1/sessions/{id}/messages/stream
    → SessionService.initiate_stream()
        → Check environment status
        → If not running: activate environment (background task)
        → Mark session as pending_stream
        → When environment activates: process_pending_messages()
    → MessageService.stream_message_with_events()
        → SSE stream from Agent Environment container
        → Events assigned event_seq, buffered in ActiveStreamingManager
        → Flushed to DB every ~2s
        → Emitted to Frontend via WebSocket room: session_{id}_stream
    → Stream completes → session interaction_status cleared → frontend notified
```

## Session Page UI Elements

### Header

- **Back button** — Navigates to sessions list
- **Session title** — Auto-generated from first message content; shows animated placeholder until set
- **Mode indicator** — Color-coded dot: orange for Building mode, blue for Conversation mode
- **Integration badges** — `Email` badge (indigo) when `integration_type="email"`, `A2A` badge (purple) when `integration_type="a2a"`
- **Tasks button** — Visible when session has sub-tasks (created via `create_agent_task` tool). Shows colored badge counts per status: violet (new), blue (running), amber (needs input), red (error), green (completed). Toggles sub-tasks side panel
- **App button** — Opens the Environment Panel showing agent workspace files. Displays "Activating..." spinner when environment is starting up
- **Options menu** — Edit session (rename, change mode) and Delete session

### Main Content Area

- **MessageList** — Scrollable chat history with `MessageBubble` per message. In-progress messages show streaming indicator and update every 2 seconds via polling. Completed messages render markdown with tool blocks, thinking sections, file attachments
- **StreamingMessage** — Real-time streaming event display beneath the in-progress message; events rendered by type (assistant text, tool use, thinking); deduplicated by `event_seq`
- **MessageInput** — Text area with send/stop buttons. Disabled with placeholder "Agent is responding..." while streaming. Supports file attachments

### Side Panels (mutually exclusive)

- **EnvironmentPanel** — Workspace file browser, file download, app logs. Triggered by "App" button. Linked to effective environment ID (resolved from `agent_usage_intent` or session's `environment_id`)
- **SubTasksPanel** — List of tasks spawned by this session via agent handover tool. Shows task status, progress, and quick navigation links

## Sessions List Page (`/sessions`)

- Displays all sessions grouped by agent in a card grid
- Each agent group shows the agent name with color preset and up to 10 recent sessions
- Sessions show: title, mode, last message preview, time since last activity
- Clicking a session navigates to its chat page

## Agent Sessions Table (`/sessions/agent/{agentId}`)

- Full table of all sessions for a specific agent (up to 500)
- Ordered by `last_message_at` descending
- Shows session title, mode, status, message count, and last activity
- Accessible from agent detail page

## Integration Points

- **[Agent Environment Core](../../agents/agent_environment_core/agent_environment_core.md)** — Server running inside Docker containers that processes messages via SDK adapters and streams responses
- **[Streaming Architecture](../realtime_events/frontend_backend_agentenv_streaming.md)** — WebSocket (frontend ↔ backend) and SSE (backend ↔ agent env) streaming pipeline, event sequencing, deduplication
- **[Input Tasks](../input_tasks/input_tasks.md)** — Tasks that execute by creating sessions with `source_task_id`; session result states sync back to task status
- **[Email Integration / Email Sessions](../email_integration/email_sessions.md)** — Email-initiated sessions with threading, auto-reply, and session context injection
- **[A2A Protocol](../a2a_integration/a2a_protocol/a2a_protocol.md)** — External A2A clients create and manage sessions via JSON-RPC; session maps 1:1 to A2A Task concept
- **[MCP Integration](../mcp_integration/agent_mcp_connector.md)** — MCP connector calls create platform sessions; `MCPSessionMeta` tracks authenticated MCP user identity
- **[Guest Sharing](../../agents/agent_sharing/guest_sharing.md)** — Guest sessions use owner's environment with `guest_share_id` scoping; conversation mode only
- **[Agent Handover](../../agents/agent_handover/agent_handover.md)** — Agent's `create_agent_task` tool creates sub-tasks that spawn child sessions; visible in Sub-tasks panel
- **[Agent Activities](../agent_activities/agent_activities.md)** — Session state changes (streaming completed, result state set) generate activity feed entries
- **[Agent Environment Data Management](../../agents/agent_environment_data_management/agent_environment_data_management.md)** — Prompt sync happens after building mode sessions complete
