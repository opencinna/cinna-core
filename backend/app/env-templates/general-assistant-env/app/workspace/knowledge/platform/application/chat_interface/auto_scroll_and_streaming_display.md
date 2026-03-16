# Auto-Scroll & Streaming Display

## Purpose

The auto-scroll and streaming display system ensures that users see the latest agent output during real-time streaming while retaining the ability to scroll up and review previous content. It manages the visual layout of messages during active streaming, splitting the display into zones that prevent ordering artifacts.

## Core Concepts

- **Stick-to-Bottom** — Default behavior where the chat viewport stays pinned to the latest content. New streaming events automatically scroll into view
- **User Scroll Detection** — When the user manually scrolls up (>100px from bottom), auto-scroll pauses. A floating "scroll down" button appears for manual re-engagement
- **Scroll-Down Button** — Floating arrow button in bottom-right corner, visible when user has scrolled away from bottom. Clicking it scrolls to bottom and re-enables auto-scroll
- **Streaming Zone Split** — During active streaming, MessageList partitions messages into three zones: before streaming, live streaming, and after streaming. This prevents system/delegation messages from appearing before the streaming response

## Auto-Scroll Behavior

### Session Page (Full Tracking)

1. **On mount** — `scrollToBottom()` called via useEffect on `[]`
2. **New content** — useEffect on `[messages, streamingEvents, userHasScrolled]` calls `scrollToBottom()` only when `!userHasScrolled`
3. **User scrolls up** — `handleScroll()` checks `scrollHeight - scrollTop - clientHeight < 100`:
   - If beyond 100px from bottom: `userHasScrolled = true`, `showScrollButton = true`
   - If within 100px: `userHasScrolled = false`, `showScrollButton = false`
4. **Scroll button click** — `scrollToBottom()` uses `scrollIntoView({ behavior: "smooth" })`, resets `userHasScrolled = false`
5. **User scrolls back to bottom** — Same threshold check re-engages auto-scroll automatically

### Webapp Widget (Simple)

- Always scrolls to bottom on `[messages, streamingEvents]` changes
- No user scroll tracking — simpler UX for smaller viewport
- No scroll-down button

## Streaming Zone Split

During active streaming, `MessageList` avoids rendering artifacts by splitting messages:

1. **Find streaming message** — scans for message with `message_metadata.streaming_in_progress === true`
2. **Get pivot** — `streamingSeq = streamingMsg.sequence_number` (falls back to `Infinity` if not found)
3. **Partition**:
   - `beforeStreaming` — messages where `!streaming_in_progress && sequence_number < streamingSeq`
   - `afterStreaming` — messages where `!streaming_in_progress && sequence_number > streamingSeq`
4. **Render order**: `beforeStreaming` → `StreamingMessage` (live events) → `afterStreaming`

### Why Zone Split Exists

During streaming, tool calls may create system messages (e.g., task delegation, session state updates) that get their own `sequence_number` higher than the streaming message. Without splitting, these would render below the StreamingMessage, creating visual jumps. The zone split defers them to after the streaming display.

When streaming ends (`isStreaming = false`), all messages render as a flat list of MessageBubbles — no splitting needed.

## Streaming Message Display

The `StreamingMessage` component renders live events from the `useSessionStreaming` hook:
- Receives `streamingEvents[]` as prop
- Shows pulsing loader dots while events arrive
- Delegates event rendering to `StreamEventRenderer`
- Uses `event_seq` as React key for stable DOM across re-renders
- Disappears when streaming completes (replaced by persisted MessageBubble)

## Dynamic Polling Intervals

The session page adjusts data fetching frequency based on streaming state:

| Query | During Streaming | Idle |
|-------|-----------------|------|
| Session | 3s refetch | 10s refetch |
| Messages | 2s refetch | Disabled |

`session_interaction_status_changed` WebSocket events trigger immediate query invalidation for sub-second state transitions, independent of polling.

## Business Rules

- Auto-scroll threshold is 100px from bottom — provides some buffer for small scroll adjustments
- Smooth scroll behavior (`behavior: "smooth"`) used for visual continuity
- The streaming zone split is only active when `isStreaming` is true
- Message polling during streaming (2s) supplements WebSocket events — ensures data completeness even if WebSocket drops
- The webapp widget skips user-scroll tracking to keep the implementation lightweight for embedded contexts

## Integration Points

- **[Chat Windows](chat_windows.md)** — Auto-scroll and zone split are core behaviors of the MessageList component
- **[Streaming Architecture](../realtime_events/frontend_backend_agentenv_streaming.md)** — Event deduplication, derived streaming state, and WebSocket transport that drives the display
