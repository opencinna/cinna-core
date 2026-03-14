# Dashboard Prompt Actions

## Purpose

Dashboard prompt actions are one-click chat shortcuts embedded directly inside dashboard blocks. They let users send a pre-configured message to an agent without leaving the dashboard — the streaming response appears in-place in a compact overlay at the bottom of the block. This is the third hosting context for the platform's chat infrastructure, distinct from the full session page and the webapp chat widget.

For the dashboard feature itself (blocks, layout, view types), see **[User Dashboards](../user_dashboards/user_dashboards.md)**.

---

## Core Concepts

- **Prompt Action** — A stored message template attached to a block. Has a required `prompt_text` (the actual message sent) and an optional `label` (short button caption shown in the UI).
- **In-Place Execution** — Clicking an action sends the message and shows the streaming result directly inside the block, with no navigation to the session page.
- **Session Reuse** — Each block reuses its most recent session (within a 12-hour window) rather than creating a new session on every click. Repeated actions accumulate in a single ongoing conversation.
- **Overlay** — The `PromptActionsOverlay` component renders as an absolute-positioned bar at the bottom of the block content area. It is visible on hover or while streaming; it hides when neither condition applies.
- **Session Indicator** — A small icon on the left side of the action bar shows whether an active session exists for this block. It is clickable to navigate to the full session page.

---

## User Flow

### Configuring Prompt Actions

1. Enter edit mode on the dashboard ("Edit Layout").
2. Open a block's kebab menu and click "Edit".
3. Scroll to the "Prompt Actions" section at the bottom of the edit dialog.
4. Click "+ Add", fill in the Prompt Text (required, 1–2000 chars) and optionally a Button Label (max 100 chars).
5. Click "Save" on the action row — the action is saved immediately to the API.
6. Repeat to add more actions. Click the trash icon on any saved action row to delete it immediately.
7. Close the dialog. Actions are now active in view mode.

### Using Prompt Actions

1. In view mode, hover over a block that has prompt actions configured.
2. A semi-transparent overlay bar appears at the bottom of the block.
3. The action bar shows:
   - A session indicator icon on the left (see Session Indicator below).
   - One pill button per configured action (showing the label, or a truncated 28-char version of the prompt text).
4. Click a button:
   - The button shows a spinner while the request is in flight.
   - The overlay resolves (or creates) a session for the block.
   - For webapp blocks, page context is collected from the iframe concurrently.
   - The prompt text is sent via the regular authenticated messages API.
5. While the agent is processing:
   - A compact `StreamingMessage` appears above the action bar, showing live streaming content.
   - If no streaming events have arrived yet, a "Processing..." indicator with a spinner is shown.
   - All action buttons are disabled during streaming.
6. When the stream completes, the streaming display clears and buttons return to normal state.

### Navigating to the Full Session

Once a session exists for a block, the session indicator becomes a clickable link that navigates to the `/session/{id}` page where the full conversation history is visible.

---

## Session Indicator

The left-side icon in the action bar communicates session state:

| State | Display |
|-------|---------|
| No session | `MessageCircle` icon (muted/grey, not clickable) |
| Session exists | `MessageCircle` icon (primary color, clickable) |

Clicking the active session icon navigates to the full session page (`/session/{sessionId}`).

---

## Session Persistence and Reuse

Each block maintains its own session context across hover events and page refreshes:

- On **component mount**, `PromptActionsOverlay` calls `GET /api/v1/dashboards/{id}/blocks/{block_id}/latest-session`. If a session with messages within the last 12 hours is found, the session indicator turns active immediately — without any user interaction.
- On **action click**, if a `sessionId` is already known in component state, it is reused directly (no API call). If it is not yet known, the same latest-session endpoint is called first, and a new session is created only when no qualifying session is found.
- New sessions are always created in `conversation` mode, tagged with `dashboard_block_id`.
- The 12-hour window applies to `last_message_at` — sessions with no messages (never had a reply) are excluded from the window check.
- The `dashboard_block_id` FK on the `session` table uses `ondelete="SET NULL"`, so deleting the block sets the column to NULL rather than deleting the session. The full session and its history remain accessible via the session page.

---

## Webapp Action Forwarding

For blocks with `view_type = "webapp"`, the overlay subscribes to `webapp_action` stream events. These events are forwarded to the embedded iframe via `postMessage`, using the same pattern as the `WebappChatWidget`:

```
Agent streaming response
  → webapp_action stream event
  → PromptActionsOverlay receives event via eventService
  → postMessage to iframeRef.current.contentWindow
  → iframe-side JavaScript handles the action
```

This allows agent responses to trigger UI changes inside the webapp (e.g., highlighting a row, refreshing a view, showing a notification) directly from a prompt action response.

---

## Page Context Collection (Webapp Blocks)

When a prompt action is triggered on a webapp block, page context is collected from the iframe before sending the message. This follows the same pattern as the `WebappChatWidget`:

1. `buildPageContext(iframeRef)` is called concurrently with the WebSocket room subscription.
2. It captures the current text selection (max 2,000 chars) and sends a `request_page_context` postMessage to the iframe, waiting up to 500ms for a `page_context_response`.
3. The iframe's `context-bridge.js` responds with schema.org microdata, the page URL, and title.
4. The assembled context is attached as the `page_context` field on the message request body.
5. If the iframe does not respond within the timeout, the message sends without page context (silent degradation).

Non-webapp blocks do not collect page context.

---

## Comparison with Other Chat Contexts

| Aspect | Session Page | Webapp Chat Widget | Dashboard Prompt Actions |
|--------|-------------|-------------------|-------------------------|
| Entry point | Full session page | Floating FAB on webapp viewer | Hover overlay on dashboard block |
| Navigation on send | Already on session page | Stays on webapp viewer | Stays on dashboard |
| Message history shown | Full history | Full history (panel) | Streaming only (no history display) |
| Streaming hook | `useSessionStreaming` | Inline state | Inline state in overlay |
| Session creation | User-initiated | Lazy on first send | Lazy — reuses or creates per block |
| Session persistence | React Query | localStorage | Mount-time restore via latest-session API |
| Page context | Not applicable | From host iframe | From embedded webapp iframe (webapp blocks only) |
| Webapp action forwarding | Not applicable | Forwarded to iframe | Forwarded to iframe (webapp blocks only) |
| Auth | Standard JWT | Webapp viewer JWT | Standard JWT (same user, no special scoping) |
| File upload | Supported | Not supported | Not supported |
| CompactMode | User preference | Auto (conversation→compact) | Fixed: `"compact"` |
| Navigate to full session | n/a | Not available | Session indicator icon |

---

## Business Rules

- Prompt actions are only visible in view mode. They are never shown while the dashboard is in edit mode.
- A block can have any number of prompt actions (no enforced cap beyond practical UI constraints).
- While a response is streaming, all action buttons are disabled — no concurrent streams per block.
- Session state (active session ID, streaming status) lives in component memory. It persists across hover events within a page session but is restored from the API on mount, not from localStorage.
- Prompt actions are cascade-deleted when their parent block is deleted.
- Each action's `sort_order` controls button display order.

---

## Integration Points

- **[User Dashboards](../user_dashboards/user_dashboards.md)** — Dashboard and block structure, edit mode, block view types; prompt action configuration UX
- **[Chat Windows](chat_windows.md)** — Shared `StreamingMessage` component and `StreamEventRenderer` reused inside the overlay
- **[Webapp Chat Widget](webapp_chat_widget.md)** — Same page context collection (`buildPageContext`) and webapp action forwarding pattern; both use `eventService` for WebSocket subscriptions
- **[Streaming Architecture](../realtime_events/frontend_backend_agentenv_streaming.md)** — WebSocket event bus, `stream_event` and `session_interaction_status_changed` events, `event_seq` deduplication
- **[Agent Sessions](../agent_sessions/agent_sessions.md)** — Session lifecycle, `interaction_status`, session page navigation
- **[Webapp Chat Context](../../agents/agent_webapp/webapp_chat_context.md)** — Page context collection, storage, and injection into agent prompts
- **[Webapp Chat Actions](../../agents/agent_webapp/webapp_chat_actions.md)** — Agent-to-webapp action framework forwarded through the overlay
