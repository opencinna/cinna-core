# Dashboard Prompt Actions — Technical Reference

## File Locations

| File | Purpose |
|------|---------|
| `frontend/src/components/Dashboard/UserDashboards/PromptActionsOverlay.tsx` | Main overlay component: session resolution, streaming state, webapp action forwarding |
| `frontend/src/components/Dashboard/UserDashboards/DashboardBlock.tsx` | Parent component: hover tracking, iframeRef creation, overlay mounting |
| `frontend/src/components/Dashboard/UserDashboards/views/WebAppView.tsx` | Webapp iframe renderer; accepts `iframeRef` and attaches it to the `<iframe>` element |
| `frontend/src/components/Dashboard/UserDashboards/EditBlockDialog.tsx` | Prompt action CRUD UI (save/delete per-item, no batch save) |
| `frontend/src/utils/webappContext.ts` | Shared `buildPageContext()` + `collectIframeContext()` utilities (also used by `WebappChatWidget`) |
| `frontend/src/services/eventService.ts` | Socket.IO event bus: `subscribe`, `subscribeToRoom`, `unsubscribe`, `unsubscribeFromRoom` |
| `frontend/src/components/Chat/StreamingMessage.tsx` | Streaming display component reused inside the overlay |
| `backend/app/api/routes/user_dashboards.py` | Dashboard/block/prompt-action CRUD routes + `/latest-session` endpoint |
| `backend/app/services/session_service.py` | `get_recent_block_session()` + `create_session()` with `dashboard_block_id` param |
| `backend/app/models/user_dashboard.py` | `UserDashboardBlockPromptAction` model and schema classes |

---

## Component Architecture

### `DashboardBlock` (parent)

`DashboardBlock` owns hover state and the iframe ref:

```typescript
const [isHovered, setIsHovered] = useState(false)
const webappIframeRef = useRef<HTMLIFrameElement | null>(null)
```

`webappIframeRef` is created unconditionally regardless of view type. It is:
- Passed to `WebAppView` as `iframeRef` when `view_type === "webapp"` (attached to the `<iframe>` DOM element).
- Passed to `PromptActionsOverlay` as `iframeRef` only when `view_type === "webapp"`.

The overlay is mounted only when not in edit mode, when the agent is present, and when the block has at least one prompt action:

```typescript
{!isEditMode && agent && block.prompt_actions && block.prompt_actions.length > 0 && (
  <PromptActionsOverlay
    actions={block.prompt_actions}
    agentId={agent.id}
    blockId={block.id}
    dashboardId={dashboardId}
    isVisible={isHovered}
    isWebApp={block.view_type === "webapp"}
    iframeRef={block.view_type === "webapp" ? webappIframeRef : undefined}
  />
)}
```

### `PromptActionsOverlay` — Props

```typescript
interface PromptActionsOverlayProps {
  actions: UserDashboardBlockPromptActionPublic[]
  agentId: string
  blockId: string
  dashboardId: string
  isVisible: boolean        // hover state from parent
  isWebApp: boolean         // controls session indicator icon + page context collection
  iframeRef?: RefObject<HTMLIFrameElement | null>  // only passed for webapp blocks
}
```

### `PromptActionsOverlay` — State

| State | Type | Purpose |
|-------|------|---------|
| `pendingActions` | `Record<string, boolean>` | Tracks which action IDs are currently sending (one spinner per button) |
| `sessionId` | `string \| null` | Active session for this block, set on mount or first action click |
| `isStreaming` | `boolean` | Streaming in progress; controls overlay visibility and button disable |
| `streamingEvents` | `StreamEvent[]` | Accumulated events for `StreamingMessage` display |

### `PromptActionsOverlay` — Refs

| Ref | Type | Purpose |
|-----|------|---------|
| `lastKnownSeqRef` | `number` | Highest seen `event_seq`; used to deduplicate stream events |
| `streamSubscriptionRef` | `string \| null` | `eventService` subscription ID for `stream_event` |
| `streamRoomRef` | `string \| null` | Currently subscribed WebSocket room name (`session_{id}_stream`) |
| `sessionStatusSubRef` | `string \| null` | `eventService` subscription ID for `session_interaction_status_changed` |

---

## Session Resolution Logic

### On Mount

```typescript
useEffect(() => {
  let cancelled = false
  DashboardsService.getBlockLatestSession({ dashboardId, blockId })
    .then((session) => {
      if (!cancelled && session?.id) setSessionId(session.id)
    })
    .catch(() => { /* no recent session — fine */ })
  return () => { cancelled = true }
}, [dashboardId, blockId])
```

The effect uses a cancellation flag to prevent state updates on unmounted components.

### Module-Level `resolveSession` Helper

Called on action click when `sessionId` is not yet known in component state:

```typescript
async function resolveSession(agentId, blockId, dashboardId): Promise<{ id: string }> {
  try {
    const recent = await DashboardsService.getBlockLatestSession({ dashboardId, blockId })
    return { id: recent.id }
  } catch {
    const session = await SessionsService.createSession({
      requestBody: {
        agent_id: agentId,
        mode: "conversation",
        dashboard_block_id: blockId,
      },
    })
    return { id: session.id }
  }
}
```

The function is defined at module level (outside the component) to keep the action handler clean.

### Session ID Optimization

In `handleActionClick`, if `sessionId` is already in component state it is used directly — skipping the extra API call:

```typescript
const sid = sessionId ?? (await resolveSession(agentId, blockId, dashboardId)).id
setSessionId(sid)
```

---

## Streaming State Management

Three `useEffect` hooks manage WebSocket subscriptions, each self-cleaning.

### Effect 1: `session_interaction_status_changed`

Dependency: `[sessionId]`

- Subscribes when `sessionId` is set.
- On event: checks `event.model_id !== sessionId && event.meta?.session_id !== sessionId` to filter to this session.
- Sets `isStreaming = true` on `"running"` or `"pending_stream"` status.
- Sets `isStreaming = false` and clears `streamingEvents` + `lastKnownSeqRef` on empty/undefined status.

### Effect 2: `stream_event`

Dependency: `[sessionId, isStreaming]`

- Active only when both `sessionId` is set and `isStreaming` is true.
- Subscribes to room `session_{sessionId}_stream` via `eventService.subscribeToRoom()`.
- On `stream_completed` event: stops streaming, clears events.
- On `webapp_action` event: forwards to iframe via `postMessage` (if `iframeRef` is present); does NOT append to `streamingEvents`.
- On other events: deduplicates by `event_seq` (skips if `seq <= lastKnownSeqRef.current`), appends typed `StreamEvent` to state.
- Cleanup: unsubscribes from event + room.

### Effect 3: Unmount Cleanup

Dependency: `[]`

Runs only on component unmount. Unsubscribes all three refs (stream subscription, stream room, session status subscription) to prevent memory leaks when the block is removed from the DOM.

---

## WebSocket Room Subscription on Action Click

The stream room is subscribed before the message is sent, so no events are missed:

```typescript
await Promise.all([
  isWebApp ? buildPageContext(iframeRef) : Promise.resolve(undefined),
  streamRoomRef.current
    ? Promise.resolve()
    : (async () => {
        const streamRoom = `session_${sid}_stream`
        streamRoomRef.current = streamRoom
        await eventService.subscribeToRoom(streamRoom)
      })(),
])
```

If the room is already subscribed (from a previous action that is still streaming), the subscription step is skipped.

---

## API Endpoints Used

| Operation | Service / Endpoint | Description |
|-----------|-------------------|-------------|
| Mount restore | `DashboardsService.getBlockLatestSession({ dashboardId, blockId })` | `GET /api/v1/dashboards/{id}/blocks/{block_id}/latest-session` |
| Session creation | `SessionsService.createSession({ requestBody: { agent_id, mode: "conversation", dashboard_block_id } })` | `POST /api/v1/sessions/` |
| Send message | `MessagesService.sendMessageStream({ sessionId, requestBody: { content, file_ids, page_context? } })` | `POST /api/v1/messages/{session_id}/stream` |
| Prompt action CRUD | `DashboardsService.createPromptAction / updatePromptAction / deletePromptAction` | `/api/v1/dashboards/{id}/blocks/{block_id}/prompt-actions` |

`MessagesService.sendMessageStream()` uses the standard user authentication (Bearer JWT from `localStorage["access_token"]`). There is no special scoping — it is the same endpoint used by the full session page.

---

## Backend: Latest-Session Endpoint

**Route:** `GET /api/v1/dashboards/{dashboard_id}/blocks/{block_id}/latest-session`
**File:** `backend/app/api/routes/user_dashboards.py`

- Validates dashboard ownership, then delegates to `SessionService.get_recent_block_session()`.
- Returns `SessionPublic` if found; raises 404 otherwise.

**Service method:**

```python
def get_recent_block_session(db_session, block_id, user_id, max_age_hours=12) -> Session | None:
    cutoff = datetime.now(UTC) - timedelta(hours=max_age_hours)
    statement = (
        select(Session)
        .where(Session.dashboard_block_id == block_id)
        .where(Session.user_id == user_id)
        .where(Session.last_message_at >= cutoff)
        .order_by(Session.last_message_at.desc())
        .limit(1)
    )
    return db_session.exec(statement).first()
```

Sessions with `last_message_at IS NULL` (never received a reply) do not satisfy the `>= cutoff` condition and are excluded.

---

## Backend: Session Creation with `dashboard_block_id`

**Route:** `POST /api/v1/sessions/`

The `SessionCreate` schema includes a nullable `dashboard_block_id` field. When the frontend creates a session for a prompt action, it passes:

```typescript
{
  agent_id: agentId,
  mode: "conversation",
  dashboard_block_id: blockId,
}
```

`SessionService.create_session()` stores the value in the `session.dashboard_block_id` column (FK → `user_dashboard_block.id`, `ondelete="SET NULL"`). This column is indexed (`ix_session_dashboard_block_id`) for efficient latest-session lookups.

---

## Page Context Collection

Uses the shared `buildPageContext` utility from `frontend/src/utils/webappContext.ts`:

```typescript
export async function buildPageContext(
  iframeRef?: RefObject<HTMLIFrameElement | null>
): Promise<string | undefined>
```

- Called only for webapp blocks (`isWebApp === true`).
- Sends `{ type: "request_page_context" }` postMessage to `iframeRef.current.contentWindow`.
- Waits up to `PAGE_CONTEXT_TIMEOUT_MS = 500ms` for `page_context_response`.
- Also captures `window.getSelection()` (truncated to `MAX_SELECTED_TEXT_CHARS = 2000`).
- Returns a JSON string with `selected_text?`, `page.url`, `page.title`, `microdata?`, or `undefined` when there is nothing to attach.

The same function is used by `WebappChatWidget` (public webapp share chat).

---

## Webapp Action Forwarding

```typescript
if (eventType === "webapp_action") {
  const action = data?.action ?? event.action
  const actionData = data?.data ?? event.data ?? {}
  if (action && iframeRef?.current?.contentWindow) {
    iframeRef.current.contentWindow.postMessage(
      { type: "webapp_action", action, data: actionData },
      "*"
    )
  }
  return  // do NOT append to streamingEvents
}
```

`webapp_action` events are consumed silently (not rendered in the streaming display). The iframe's JavaScript is responsible for handling the action payload.

---

## Streaming Display

While `isStreaming` is true and `streamingEvents.length > 0`, the overlay renders `StreamingMessage`:

```typescript
<StreamingMessage events={streamingEvents} conversationModeUi="compact" />
```

The `"compact"` mode is fixed — it matches the compact display used in the webapp chat widget and is appropriate for the constrained space of the dashboard block overlay. The streaming area is capped at `max-h-[120px] overflow-y-auto`.

While streaming but before any events arrive (`streamingEvents.length === 0`), a minimal "Processing..." indicator is shown:

```typescript
<Loader2 className="h-3 w-3 animate-spin text-muted-foreground" />
<span className="text-xs text-muted-foreground">Processing...</span>
```

---

## Overlay Visibility Logic

The overlay uses CSS opacity transitions, not conditional rendering. It stays mounted while streaming even if the user stops hovering:

```typescript
className={cn(
  "absolute inset-x-0 bottom-0 flex flex-col gap-1.5",
  "bg-background/85 backdrop-blur-sm border-t border-border/50",
  "transition-opacity duration-150",
  isVisible || isStreaming ? "opacity-100 pointer-events-auto" : "opacity-0 pointer-events-none",
)}
```

The component returns `null` early only when `actions.length === 0 && !isStreaming`.

---

## Button Label Truncation

```typescript
const getDisplayLabel = (action: UserDashboardBlockPromptActionPublic): string => {
  if (action.label) return action.label
  const text = action.prompt_text
  return text.length > 28 ? text.slice(0, 26) + "…" : text
}
```

If no explicit label is set, the prompt text is truncated to 28 characters with an ellipsis. The full `prompt_text` is always shown in the button's `title` attribute as a tooltip.

---

## WebApp Owner Status Polling

Before the iframe can render, `WebAppView` polls the owner status endpoint to handle agent startup:

**Route:** `GET /api/v1/agents/{agent_id}/webapp/owner-status`
**File:** `backend/app/api/routes/webapp.py`

Auth: Bearer token, `?token=` query param, or `webapp_owner_token` cookie (same three-source resolution as `serve_webapp_file`).

The component polls every 2 seconds while `activationStatus` is `"checking"` or `"activating"`, stopping when the status becomes `"running"` or `"error"`. On `"running"`, the iframe is rendered:

```typescript
const webappUrl = `${baseUrl}/api/v1/agents/${agentId}/webapp/?token=${encodeURIComponent(accessToken)}`
```

The `?token=` parameter on the initial request causes the backend to set the `webapp_owner_token` cookie, so subsequent sub-resource requests authenticate via cookie without needing the query parameter on every URL.

---

## React Query Keys Relevant to Prompt Actions

| Key | Used by | Notes |
|-----|---------|-------|
| `["userDashboard", dashboardId]` | `DashboardGrid`, `DashboardBlock` | Contains `block.prompt_actions` in the response; invalidated on block update |

The prompt actions themselves are fetched as part of the dashboard/block response (via `selectinload` in the service layer). There is no separate query key for prompt actions — they are embedded in the block payload.

---

## Shared Infrastructure Reuse

`PromptActionsOverlay` reuses the following platform-wide components and utilities without modification:

| Component/Utility | Source | What it provides |
|-------------------|--------|-----------------|
| `StreamingMessage` | `src/components/Chat/StreamingMessage.tsx` | Live streaming event rendering in compact mode |
| `buildPageContext` | `src/utils/webappContext.ts` | Page context collection from webapp iframe |
| `eventService` | `src/services/eventService.ts` | WebSocket room subscription + event dispatch |
| `MessagesService.sendMessageStream` | Auto-generated from OpenAPI | Standard authenticated message send |
| `DashboardsService.getBlockLatestSession` | Auto-generated from OpenAPI | Latest-session lookup |
| `SessionsService.createSession` | Auto-generated from OpenAPI | New session creation |
