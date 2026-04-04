# Webapp Chat Actions — Technical Details

## File Locations

### Backend

- `backend/app/services/sessions/message_service.py` — `_WEBAPP_ACTION_TAG_RE` regex, `_extract_webapp_actions()`, `_emit_webapp_action_events()`, mid-stream scanning in `stream_message_with_events()`, post-processing of `streaming_events` after stream completion

### Frontend

- `frontend/src/components/Chat/WebappActionBlock.tsx` — Renders a single `webapp_action` event as a visual block in the chat message
- `frontend/src/components/Chat/StreamEventRenderer.tsx` — Routes `webapp_action` events to `WebappActionBlock`; imports `StreamEvent` type from `useSessionStreaming`
- `frontend/src/hooks/useSessionStreaming.ts` — `StreamEvent` type definition; includes `"webapp_action"` in the `type` union and `action`/`data` fields in the `metadata` interface
- `frontend/src/components/Webapp/WebappChatWidget.tsx` — `stream_event` handler branch for `webapp_action` event type; forwards action to iframe via `postMessage`

### Webapp (Agent Environment)

- `backend/app/env-templates/app_core_base/core/prompts/WEBAPP_BUILDING.md` — brief action reference (compact table of 5 action types) and pointer to the full reference file for the building agent
- `backend/app/env-templates/app_core_base/core/webapp-framework/context-bridge.js` — production `context-bridge.js` script with `handleWebappAction()` dispatcher; auto-served as fallback at `./assets/context-bridge.js` when the agent has not created a custom version
- `backend/app/env-templates/app_core_base/core/webapp-framework/ACTIONS_REFERENCE.md` — full action type documentation with field specs, custom event listener examples, and usage examples for the building agent

### Tests

- `backend/tests/api/agents/agents_webapp_chat_actions_test.py` — Integration tests for action emission and tag stripping
- `backend/tests/api/agents/agents_webapp_chat_test.py` — Section K: webapp action tests and unit test for `_extract_webapp_actions()`

## Backend: Tag Parsing

### `_extract_webapp_actions(content: str) -> tuple[list[dict], str]`

Location: `backend/app/services/sessions/message_service.py`

Scans `content` for complete `<webapp_action>` tags using `_WEBAPP_ACTION_TAG_RE` (compiled regex with `re.DOTALL`). For each tag:
- Parses JSON payload
- If valid with `"action"` field: adds to returned actions list
- If malformed JSON or missing `"action"`: logs warning, skips emission
- Always strips the tag from `cleaned_content`

Returns `(actions, cleaned_content)`.

### `_emit_webapp_action_events(session_id, actions) -> None`

Location: `backend/app/services/sessions/message_service.py`

Calls `event_service.emit_stream_event()` once per action:
- `event_type="webapp_action"`
- `event_data={"action": ..., "data": ..., "session_id": str(session_id)}`
- Room: `session_{session_id}_stream`

Errors are caught and logged — never crashes the stream.

### Mid-Stream Scanning in `stream_message_with_events()`

Two tracking variables at the start of the streaming loop:
- `accumulated_assistant_content: str = ""`
- `webapp_action_scan_offset: int = 0`

After each `assistant` event:
1. Append event content to `accumulated_assistant_content`
2. Scan suffix starting at `max(0, offset - 2048)` (lookback window for tags straddling chunk boundaries)
3. Emit any new complete actions found
4. Advance `webapp_action_scan_offset`

After streaming loop, before DB save:
1. Scan remaining portion beyond last scan offset for any un-emitted tags
2. Emit them
3. Strip ALL tags from `agent_content` using `_extract_webapp_actions()` before persisting

### Post-Processing `streaming_events` After Stream Completion

After the streaming loop ends and `agent_content` is stripped, the accumulated `streaming_events` list is post-processed if any assistant event still contains `<webapp_action>` tags. This separates the raw assistant chunks (which may span multiple streaming SSE events) into a correctly ordered, renderable sequence:

**Condition**: at least one event has `type == "assistant"` and its `content` matches `_WEBAPP_ACTION_TAG_RE`.

**Algorithm** (inside `stream_message_with_events()`, before writing `response_metadata["streaming_events"]`):

For each event in `streaming_events`:
- If the event is an `assistant` event containing one or more tags:
  1. Walk through the content using `_WEBAPP_ACTION_TAG_RE.finditer()`.
  2. For text before each tag: emit a new assistant event (same type/metadata, stripped content).
  3. For each tag: attempt `json.loads()` on the inner content.
     - If valid JSON with `"action"` field: emit a `webapp_action` event with `content = action_name` and `metadata = {"action": action_name, "data": payload.get("data", {})}`.
     - If malformed JSON or missing `"action"`: log DEBUG warning, skip the event (tag is still consumed).
  4. For text after the last tag: emit a final assistant event if non-empty.
- If the event does not contain tags: pass through unchanged.

After the loop, all events in `processed_events` are re-numbered with `event_seq = i` (1-based, contiguous). This replaces `streaming_events` before it is written to `response_metadata`.

**Event shape for a `webapp_action` event in `streaming_events`:**

```json
{
  "type": "webapp_action",
  "content": "refresh_page",
  "event_seq": 4,
  "metadata": {
    "action": "refresh_page",
    "data": {}
  }
}
```

Note that `event_seq` values in the stored list reflect the post-processed order, not the raw SSE sequence numbers from agent-core.

## WebSocket Event Structure

```json
{
  "session_id": "<session-uuid>",
  "event_type": "webapp_action",
  "data": {
    "action": "refresh_page",
    "data": {},
    "session_id": "<session-uuid>"
  },
  "timestamp": "<iso8601>"
}
```

Room: `session_{session_id}_stream`

## Frontend: StreamEvent Type

Location: `frontend/src/hooks/useSessionStreaming.ts`

The canonical `StreamEvent` interface includes `"webapp_action"` in its `type` union:

```typescript
export interface StreamEvent {
  type: "assistant" | "tool" | "thinking" | "system" | "webapp_action"
  content: string       // For webapp_action events: the action name (e.g. "refresh_page")
  event_seq: number
  tool_name?: string
  metadata?: {
    tool_id?: string
    tool_input?: Record<string, any>
    model?: string
    interrupt_notification?: boolean
    needs_approval?: boolean
    tool_name?: string
    action?: string                    // action name (same as content for webapp_action)
    data?: Record<string, unknown>     // action payload
  }
}
```

`StreamEventRenderer` imports this type directly from `useSessionStreaming` rather than defining its own local interface.

## Frontend: WebappActionBlock Component

Location: `frontend/src/components/Chat/WebappActionBlock.tsx`

Props:

```typescript
interface WebappActionBlockProps {
  action: string
  data?: Record<string, unknown>
  isCompact?: boolean
}
```

**Normal mode** (`isCompact` is falsy or absent):
- Renders a block with `AppWindow` icon, "Webapp action: `{action}`" label, and a field-by-field listing of `data` entries below it (key in semibold, value in monospace)
- Background: `bg-slate-100 dark:bg-slate-800` with border

**Compact mode** (`isCompact = true`):
- Renders an inline `div` with `AppWindow` icon and "Webapp: `{action}`" in monospace badge
- Used when the session UI mode is set to `"compact"` (e.g. in conversation mode with compact setting)

## Frontend: StreamEventRenderer — webapp_action Branch

Location: `frontend/src/components/Chat/StreamEventRenderer.tsx`

The renderer dispatches on `event.type`. For `"webapp_action"`:

```typescript
} else if (event.type === "webapp_action") {
  return (
    <WebappActionBlock
      key={key}
      action={event.content}           // action name stored in content field
      data={event.metadata?.data}
      isCompact={conversationModeUi === "compact"}
    />
  )
}
```

`event.content` holds the action name (set by the backend post-processing step). `event.metadata.data` holds the action payload object.

## Frontend: iframe Forwarding (Webapp Sessions)

Location: `frontend/src/components/Webapp/WebappChatWidget.tsx`

The `stream_event` subscription callback has a branch for `webapp_action`:
- Extracts `action` and `data` from the event payload
- If `action` is present and `iframeRef.current.contentWindow` is available: sends `postMessage` with `{ type: "webapp_action", action, data }` to the iframe
- Returns early (no further processing for action events)

The `iframeRef` prop is already available on the component.

## Webapp: context-bridge.js Action Handler

Location: `backend/app/env-templates/app_core_base/core/webapp-framework/context-bridge.js`

The `context-bridge.js` message listener checks for `webapp_action` message type and calls `handleWebappAction(action, data)`. This file is auto-served at `./assets/context-bridge.js` by the environment server as a fallback when the agent has not placed a custom version in `webapp/assets/context-bridge.js`.

### Action Dispatch Table

| Action | Implementation |
|--------|---------------|
| `refresh_page` | `window.location.reload()` |
| `reload_data` | `window.dispatchEvent(new CustomEvent('webapp_reload_data', { detail: data }))` |
| `update_form` | Find form by `data.form_id`, set field values, fire `input`+`change` events |
| `show_notification` | `window.dispatchEvent(new CustomEvent('webapp_show_notification', { detail: data }))` |
| `navigate` | `history.pushState` + `popstate` event; fallback to `window.location.href` |
| unknown | `window.dispatchEvent(new CustomEvent('webapp_action_' + action, { detail: data }))` |

Custom events (`webapp_reload_data`, `webapp_show_notification`) allow the webapp's own JS to handle them without modifying the bridge script.

---

*Last updated: 2026-03-10*
