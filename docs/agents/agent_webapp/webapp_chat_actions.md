# Webapp Chat — Agent-to-Webapp Actions

## Purpose

Enable bi-directional communication between the agent and the webapp frontend. While context flows from webapp to agent (see [Context Management](webapp_chat_context.md)), actions flow in the opposite direction — agents send commands that manipulate the webapp UI without requiring the viewer to reload or interact manually.

## Core Concepts

### Webapp Action

A JSON command embedded in the agent's text response using `<webapp_action>` XML tags. Each action has an `action` type and optional `data` payload. The tags are invisible to the viewer — they are stripped from the stored message and never displayed in the chat UI.

### Action Pipeline

1. Agent includes `<webapp_action>` tags in its streaming response
2. Backend detects tags mid-stream and at stream completion
3. Backend emits a `webapp_action` WebSocket event to the session stream room
4. Backend strips the tags from the persisted message content
5. Frontend chat widget receives the WebSocket event
6. Widget forwards the action to the webapp iframe via `postMessage`
7. The `context-bridge.js` script inside the iframe dispatches the action to the appropriate handler

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
4. Viewer sees the agent's reply ("I've updated the layout") — the action tag is invisible
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

- Tags are detected both mid-stream (for real-time action delivery) and at stream completion (for any remaining tags)
- Malformed JSON inside tags is silently skipped (logged as warning) — the tag is still stripped from the message
- Tags missing the `"action"` field are skipped — the tag is still stripped
- All `<webapp_action>` tags are always removed from the stored message, regardless of JSON validity

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
Backend: _extract_webapp_actions() parses tags from content
        |
        ├──> Actions emitted as WebSocket events (session stream room)
        |
        └──> Tags stripped from stored message content
                |
                v
Frontend: WebappChatWidget receives webapp_action event
        |
        v
postMessage to webapp iframe
        |
        v
context-bridge.js handleWebappAction()
        |
        v
Action dispatched: reload, form update, navigate, notification, etc.
```

## Integration Points

- **[Webapp Chat](webapp_chat.md)** — actions are part of the chat streaming pipeline; the widget handles action events alongside regular stream events
- **[Webapp Chat Context](webapp_chat_context.md)** — complementary direction: context flows webapp→agent, actions flow agent→webapp
- **[Streaming Architecture](../../application/realtime_events/frontend_backend_agentenv_streaming.md)** — action events use the same WebSocket event bus and session stream room
- **[Agent Prompts](../agent_prompts/agent_prompts.md)** — `WEBAPP_BUILDING.md` instructs the building agent on available actions and the context-bridge.js script

---

*Last updated: 2026-03-08*
