# Webapp Chat Widget

## Purpose

Allow webapp viewers to chat with the agent directly from within the shared webapp page via an embedded chat widget. The chat appears as a floating icon at the bottom-right corner; clicking it opens a compact overlay panel. The main webapp stays in focus while the chat provides a communication channel to the agent.

## Core Concepts

### Chat Mode

Per-agent setting in the interface configuration that controls whether and how chat is available. Three states:

- **Disabled** (`chat_mode = null`) — no chat widget shown, chat endpoints return 403. Default for all agents.
- **Conversation** (`chat_mode = "conversation"`) — compact chat for interacting with existing UI (filters, views, data queries). Uses simplified tool display and hidden thinking.
- **Building** (`chat_mode = "building"`) — full chat for creating/modifying widgets. Shows full tool calls and file operations.

Chat mode is per-agent, not per-share — all share links for the same agent use the same chat mode.

### Session Scoping

Chat sessions are scoped by `webapp_share_id` — one active session per share token. This means:
- Multiple viewers on the same share URL share the same session
- Different share links for the same agent have separate sessions
- Sessions persist across page reloads — session ID and messages are cached in localStorage under `webapp_chat_{webappToken}` and restored instantly on mount, with a background API verify to detect stale sessions

### Session Persistence

The chat widget caches the active session ID and message history in localStorage (key: `webapp_chat_{webappToken}`) so that page refreshes are seamless. On mount, cached data is restored immediately before any API call, giving instant message display. A background verify confirms the session is still active on the server and refreshes the message list silently (no loading spinner). If the background verify fails or finds no active session, the cached state is intentionally preserved — the viewer retains their message history across page refreshes even when the verify cannot confirm the session. The cache is only cleared when the widget performs an explicit (foreground) session load and finds no active session. Opening an incognito window starts a fresh session naturally since incognito localStorage is isolated.

The intended workflow is: user chats → agent makes changes → user refreshes page to see changes → continues chatting in the same session without losing context.

Sessions use `user_id = agent.owner_id` (runs in the owner's environment), matching the guest share pattern.

### Auth Reuse

Chat endpoints reuse the existing webapp share JWT (`role: "webapp-viewer"`). The JWT already contains `agent_id`, `owner_id`, and `webapp_share_id`. No separate auth mechanism needed — the same token used for viewing the webapp is used for chat.

## User Stories

### Viewer Opens Chat for First Time

1. Viewer sees chat FAB icon at bottom-right of webapp page
2. Clicks the icon — chat panel slides in from the right
3. Panel shows empty state with contextual prompt based on mode
4. Viewer types a message and presses Enter
5. Frontend creates a chat session (idempotent — returns existing if one exists)
6. Message sent via streaming endpoint; agent response renders in real-time
7. Viewer can continue chatting in the same session

### Viewer Returns to Webapp (Page Reload)

1. Viewer reloads the webapp page
2. After auth (JWT from localStorage), webapp page renders
3. Chat widget mounts and immediately restores session ID and messages from localStorage (instant, no spinner)
4. FAB badge appears signaling that a prior conversation exists
5. In the background, the widget verifies the cached session is still active on the server and refreshes messages; the loading spinner is suppressed so cached messages remain visible
6. If the background verify cannot reach the server or the session is not found, the cached state is preserved so the viewer can keep reading their history — the cache is only cleared on an explicit (non-background) session load
7. Only when the viewer explicitly opens the chat without a cached session (e.g., first open after incognito or after a manual cache clear) will the widget query the server and reset if no session exists
8. Viewer clicks FAB and sees their previous conversation

### Owner Configures Chat Mode

1. Owner navigates to Integrations tab, Web App card, Interface button
2. Opens interface modal with Chat section showing three radio options
3. Selects "Conversation" or "Building"
4. All active share links for the agent now show the chat widget
5. Owner can switch modes or disable at any time

### Viewer Interrupts Agent Response

1. During an active streaming response, viewer clicks the interrupt button
2. Frontend sends interrupt request to the chat interrupt endpoint
3. If stream is active, interrupt is forwarded to the agent environment
4. If stream is pending (not yet started), interrupt is queued
5. Agent stops generating and the partial response is preserved

## Business Rules

### Chat Enablement

- Chat is enabled when `chat_mode IS NOT NULL` in the agent's interface config
- All chat endpoints validate this on every request; return 403 if disabled
- Existing sessions continue with their original mode if owner changes `chat_mode`
- New sessions use the current `chat_mode` value

### Session Lifecycle

- Sessions are created lazily on first message (not on widget open)
- `POST /sessions` is idempotent — returns existing active session if one exists
- One active session per `webapp_share_id` at a time
- Session mode is determined by the agent's `chat_mode` at creation time

### Environment Keep-Alive

- All chat requests (session creation, messages, polling) update `last_activity_at` on the environment
- This prevents suspension while viewers are actively chatting
- Same mechanism as regular webapp traffic

### Access Control

| Action | Webapp viewer (chat enabled) | Webapp viewer (no chat) | Owner |
|--------|------------------------------|------------------------|-------|
| View webapp content | Yes | Yes | Yes |
| Create chat session | Yes | No | N/A (uses regular sessions) |
| Send/receive messages | Yes (own session only) | No | N/A |
| Interrupt streaming | Yes (own session only) | No | N/A |

### Message Streaming

- Chat uses the same streaming infrastructure as regular sessions (WebSocket rooms)
- Room name: `session_{session_id}_stream`
- In-memory streaming events are merged with persisted messages when polling for message history
- Streaming event deduplication uses `event_seq` to avoid gaps or duplicates
- Streaming enrichment, interrupt orchestration, and response building are shared with regular session routes via `MessageService` — no chat-specific duplication of these flows

## Architecture Overview

```
Webapp Viewer clicks chat icon
        |
        v
Chat panel opens (React component, outside iframe)
        |
        v
First message sent
        |
        v
POST /webapp/{token}/chat/sessions  ->  Creates session
        |                                 (user_id = owner_id,
        |                                  webapp_share_id on session,
        |                                  mode from interface config)
        v
POST /webapp/{token}/chat/sessions/{id}/messages/stream
        |
        v
WebSocket stream -> chat panel renders messages in real time
        |
        v
Subsequent messages use same session
        |
        v
On page reload: GET /webapp/{token}/chat/sessions
                -> returns existing session (if any) -> resumes chat
```

## Integration Points

- **[Agent Webapp](agent_webapp.md)** — chat widget is part of the webapp viewer experience; shares the same token auth and environment keep-alive mechanism
- **[Agent Sessions](../../application/agent_sessions/agent_sessions.md)** — chat sessions reuse the full session infrastructure with `webapp_share_id` scoping
- **[Guest Sharing](../agent_sharing/guest_sharing.md)** — parallel pattern for `webapp_share_id` on sessions (mirrors `guest_share_id`)
- **[Streaming Architecture](../../application/realtime_events/frontend_backend_agentenv_streaming.md)** — WebSocket streaming for real-time chat responses
- **[Agent Prompts](../agent_prompts/agent_prompts.md)** — building mode chat uses the building prompt; conversation mode uses conversation prompt

---

*Last updated: 2026-03-08 — background verify preserves cache on failure; explicit load clears on no-session*
