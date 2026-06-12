# Webapp Chat Widget

## Purpose

The webapp chat widget is an embedded floating chat panel that appears alongside agent webapp dashboards. It provides a lightweight, self-contained chat interface within the webapp viewer page — separate from the full session page chat — with unique behaviors like localStorage caching, page context collection from the host iframe, webapp action forwarding, and a collapsible FAB (floating action button) design.

For the full webapp chat feature (session scoping, auth, mode configuration, backend endpoints), see **[Webapp Chat](../../agents/agent_webapp/webapp_chat.md)**.

## Core Concepts

- **FAB (Floating Action Button)** — Circular button fixed to bottom-right corner. Shows chat icon when collapsed, unread badge when new messages arrive while closed
- **Chat Panel** — 460px-wide overlay panel that expands from the FAB. Contains header, scrollable message area, and input
- **LocalStorage Cache** — Session ID and messages cached under `webapp_chat_{webappToken}` for instant display across page reloads
- **Page Context** — Contextual data collected from the host iframe (schema.org microdata, selected text) and attached to each message
- **Webapp Actions** — Agent commands forwarded from streaming events to the iframe via `postMessage` for bi-directional interaction
- **Self-Contained Streaming** — Unlike the session page (which uses `useSessionStreaming` hook), the widget manages its own streaming state, WebSocket subscriptions, and event handling inline

## User Flow

1. Viewer sees chat FAB icon at bottom-right of webapp page
2. Clicks FAB → panel slides open, cached messages restore instantly (if any)
3. Empty state shows contextual prompt: "Ask questions about the data" (conversation) or "Request new widgets" (building)
4. Viewer types message → widget ensures session exists (lazy creation on first message)
5. Before sending, widget concurrently:
   - Collects page context from iframe (500ms timeout, silent failure)
   - Captures any text selection from the viewer (max 2,000 chars)
6. Message sent with optional `page_context` JSON payload
7. Streaming events render in real-time via WebSocket subscription
8. Webapp action events forwarded to iframe via `postMessage`
9. On page reload: cache restores instantly, background verify checks session validity without showing spinner

## Widget vs Session Page Chat

| Aspect | Session Page | Webapp Widget |
|--------|-------------|---------------|
| Streaming hook | `useSessionStreaming` (shared hook) | Inline state management |
| Auto-scroll | User-scroll tracking with 100px threshold | Always scroll to bottom |
| Message caching | React Query cache | localStorage persistence |
| File upload | Supported (drag-drop, 100MB) | Not supported |
| Mode switching | User-controlled toggle | Fixed from agent config |
| Page context | Not applicable | iframe microdata + selection |
| Webapp actions | Not applicable | Forwarded to iframe |
| Compact mode | User preference | Auto: conversation→compact, building→detailed |
| Sub-tasks panel | Available | Not available |
| Environment panel | Available | Not available |

## LocalStorage Caching

### Cache Structure

Key: `webapp_chat_{webappToken}`

Value:
- `sessionId: string` — Active chat session ID
- `messages: MessagePublic[]` — Full message array
- `cachedAt: number` — Timestamp for cache age tracking

### Cache Lifecycle

1. **Mount** — Reads cache immediately, restores `sessionId` and `messages` state
2. **Background verify** — If session was restored from cache, a silent API call verifies session validity (no loading spinner). Failure preserves cached state
3. **Updates** — Cache written on every `[sessionId, messages]` state change
4. **Clear** — Only cleared on explicit foreground load that finds no active session (not on background verify failure)
5. **Unread badge** — If cache has messages on mount, FAB shows unread indicator

### Design Intent

The cache ensures seamless page-reload experience: user chats → agent modifies webapp → user refreshes to see changes → continues chatting without losing context. Cache is intentionally resilient to background failures to preserve this flow.

## Page Context Collection

Before each message send, the widget collects contextual data:

1. **Text selection** — `window.getSelection()` captured and truncated to 2,000 chars
2. **Iframe context** — `postMessage({ type: "request_page_context" })` sent to iframe
   - Waits up to 500ms for `page_context_response` reply
   - Extracts: `url`, `title`, and `microdata` (schema.org items from the webapp page)
   - Silent timeout — message sends regardless
3. **Payload assembly** — JSON with `selected_text`, `page.url`, `page.title`, `microdata`
4. **Sent as** — `page_context` field in the message request body

For full context management details (storage, injection into prompts, diff optimization), see **[Webapp Chat Context](../../agents/agent_webapp/webapp_chat_context.md)**.

## Webapp Action Forwarding

When a `webapp_action` stream event arrives:

1. Event handler extracts `action` name and `data` payload
2. Forwards via `iframeRef.current.contentWindow.postMessage({ type: "webapp_action", action, data }, "*")`
3. Iframe-side JavaScript handles the action (e.g., `refresh_page`, `update_form`, `show_notification`)
4. These events are NOT rendered as messages — they're invisible to the viewer

For full action framework details, see **[Webapp Chat Actions](../../agents/agent_webapp/webapp_chat_actions.md)**.

## Streaming State Management

Unlike the session page which derives `isStreaming` from the session query, the widget manages streaming locally:

- `isStreaming` — local state set from:
  - `session.interaction_status` on initial session load
  - `session_interaction_status_changed` WebSocket events
- Subscribes to `stream_event` events with `event_seq` deduplication (same logic as `useSessionStreaming`)
- On stream complete: clears events, resets seq counter, refreshes messages from API
- If widget is closed during streaming: sets `hasUnread = true` for FAB badge

## Business Rules

- Widget only appears when agent has `chat_mode` configured (not null)
- Session created lazily on first message (not on widget open)
- One active session per `webapp_share_id` — shared across all viewers of the same share URL
- Auth uses existing webapp viewer JWT (`role: "webapp-viewer"`)
- All API calls go through `chatFetch()` helper scoped to `/webapp/{token}/chat/` base path
- `Enter` sends message, `Shift+Enter` for newline
- Textarea auto-grows up to 100px height
- Input disabled during send (optimistic message shown immediately)
- Failed sends remove the optimistic message and show error banner

## Integration Points

- **[Chat Windows](chat_windows.md)** — Widget reuses core chat components: `MessageBubble`, `StreamingMessage`, `StreamEventRenderer`
- **[Webapp Chat](../../agents/agent_webapp/webapp_chat.md)** — Full feature documentation: session scoping, auth, mode config, backend endpoints
- **[Webapp Chat Context](../../agents/agent_webapp/webapp_chat_context.md)** — Context collection, storage, prompt injection, diff optimization
- **[Webapp Chat Actions](../../agents/agent_webapp/webapp_chat_actions.md)** — Agent-to-webapp action framework
- **[Auto-Scroll & Streaming Display](auto_scroll_and_streaming_display.md)** — Widget uses simplified always-scroll variant
