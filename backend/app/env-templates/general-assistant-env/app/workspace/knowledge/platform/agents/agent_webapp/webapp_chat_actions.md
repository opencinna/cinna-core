# Webapp Chat — Agent-to-Webapp Actions

## Purpose

Enable bi-directional communication between the agent and the webapp frontend. While context flows from webapp to agent (see [Context Management](webapp_chat_context.md)), actions flow in the opposite direction — agents send commands that manipulate the webapp UI without requiring the viewer to reload or interact manually.

## Core Concepts

### Webapp Action

A JSON command embedded in the agent's text response using `<webapp_action>` XML tags. Each action has an `action` type and optional `data` payload. The raw XML tags are stripped from the persisted message content and the final text bubble. However, actions are rendered as visual blocks in the chat UI (alongside the surrounding text) so the viewer can see which actions the agent issued during the conversation.

### Action Pipeline

1. Agent includes `<webapp_action>` tags in its streaming response
2. Backend detects tags mid-stream and at stream completion
3. Backend emits a `webapp_action` WebSocket event to the session stream room (for real-time delivery to the iframe)
4. After streaming completes, the backend post-processes the saved `streaming_events`: assistant events that contain `<webapp_action>` tags are split into interleaved text chunks and `webapp_action` events, preserving the order in which they appeared. All events are re-numbered sequentially.
5. Backend strips the tags from the persisted message text (`message.content`)
6. Frontend chat renders the `webapp_action` events as `WebappActionBlock` components — full block with icon and data fields in detailed mode, inline one-liner in compact mode
7. Frontend chat widget (in webapp sessions) also forwards the action to the webapp iframe via `postMessage`
8. The `context-bridge.js` script inside the iframe dispatches the action to the appropriate handler

### Action Types

| Action | Description | Typical Mode |
|--------|-------------|--------------|
| `refresh_page` | Reload the entire webapp iframe | Building — after code changes |
| `reload_data` | Signal webapp to refetch data from a specific API endpoint | Conversation — after data updates |
| `update_form` | Set field values on a named form element | Conversation — for filter changes |
| `show_notification` | Display a notification toast in the webapp | Both modes |
| `navigate` | Navigate to a different path within the SPA | Both modes |

Unknown action types are dispatched as custom events (`webapp_action_{action}`) so the webapp's own JS can handle them without modifying the bridge script.

## User Stories

### Agent Refreshes Page After Code Change (Building Mode)

1. Viewer asks the agent to update the dashboard layout
2. Agent writes new code to the workspace files
3. Agent includes `<webapp_action>{"action": "refresh_page"}</webapp_action>` in its response
4. Viewer sees the agent's reply ("I've updated the layout") with a `refresh_page` action block rendered inline — the raw XML tag is never shown as text
5. The webapp iframe reloads automatically, showing the new layout

### Agent Updates Form Filters (Conversation Mode)

1. Viewer says "filter to Q4 2024"
2. Agent determines the correct form and field values
3. Agent responds with text ("I've updated the date range") plus an embedded `update_form` action
4. The form fields update automatically — no page reload needed

### Agent Shows Notification

1. Agent completes a background data operation
2. Agent includes a `show_notification` action with a message
3. The webapp displays a toast notification to the viewer

## Business Rules

### Tag Processing

- Tags are detected both mid-stream (for real-time action delivery to the iframe) and at stream completion (for any remaining tags)
- After streaming completes, `streaming_events` are post-processed: any assistant event whose content contains one or more `<webapp_action>` tags is split into interleaved text and `webapp_action` events. The split preserves relative order — text before a tag, the action event, then text after. All resulting events are re-numbered sequentially.
- Malformed JSON inside tags is silently skipped during both mid-stream emission and post-processing (logged as warning at DEBUG level) — the tag is still stripped from the message
- Tags missing the `"action"` field are skipped — the tag is still stripped
- All `<webapp_action>` tags are always removed from the stored `message.content`, regardless of JSON validity
- Valid actions with a parseable `action` field produce a `webapp_action` event in `streaming_events` with `content = action_name` and `metadata = {action, data}`

### Mode Applicability

- Actions work in both conversation and building modes — no mode restriction on which actions can be used
- In practice, `refresh_page` is most natural for building mode (after code changes), while `update_form`, `reload_data`, and `navigate` suit conversation mode (UI state changes)
- The agent prompt for each mode guides which actions the agent uses — no backend enforcement

### Error Resilience

- Failed WebSocket event emissions are caught and logged — they never crash the streaming response
- If the iframe is not available or the postMessage fails, the action is silently dropped on the frontend side
- Actions are fire-and-forget — no acknowledgment or retry mechanism

## Architecture Overview

```
Agent response with <webapp_action> tags
        |
        v
Backend: mid-stream scan via _extract_webapp_actions()
        |
        ├──> Actions emitted as WebSocket events (session stream room)
        |     |
        |     v
        |   Frontend: WebappChatWidget (webapp sessions) receives webapp_action event
        |     |
        |     v
        |   postMessage to webapp iframe
        |     |
        |     v
        |   context-bridge.js handleWebappAction()
        |     |
        |     v
        |   Action dispatched: reload, form update, navigate, notification, etc.
        |
        └──> Stream completion: post-process streaming_events
              |
              ├── Split assistant events containing tags into interleaved
              |   text chunks + webapp_action events (preserves position)
              ├── Re-number all events sequentially
              └── Strip tags from message.content before DB save
                        |
                        v
              Frontend: StreamEventRenderer renders webapp_action events
              via WebappActionBlock (full block or compact inline)
```

## Integration Points

- **[Webapp Chat](webapp_chat.md)** — actions are part of the chat streaming pipeline; the widget handles action events alongside regular stream events
- **[Webapp Chat Context](webapp_chat_context.md)** — complementary direction: context flows webapp→agent, actions flow agent→webapp
- **[Streaming Architecture](../../application/realtime_events/frontend_backend_agentenv_streaming.md)** — action events use the same WebSocket event bus and session stream room
- **[Agent Prompts](../agent_prompts/agent_prompts.md)** — `WEBAPP_BUILDING.md` instructs the building agent on available actions and the context-bridge.js script
- **[Agent Sessions](../../application/agent_sessions/agent_sessions.md)** — `streaming_events` in `message_metadata` is where post-processed events (including `webapp_action` type) are persisted and later replayed on page load

---

*Last updated: 2026-03-10*
