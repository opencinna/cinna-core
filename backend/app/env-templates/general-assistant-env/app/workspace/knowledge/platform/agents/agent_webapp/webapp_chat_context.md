# Webapp Chat — Context Management

## Purpose

Allow the agent to give smarter, context-aware responses by silently collecting information about what the viewer is currently looking at in the webapp and bundling it with each chat message. The viewer never has to describe the current state of the dashboard — the context is captured automatically.

## Core Concepts

### Page Context

A JSON payload assembled on every message send that describes the current state of the webapp from the viewer's perspective. Contains two sources of information: structured DOM data (schema.org microdata) and any text the viewer has selected.

### Context Bridge

A small JavaScript file (`context-bridge.js`) included in every webapp page by the building agent. It listens for `postMessage` requests from the parent page, walks the DOM for schema.org-annotated elements, and responds with a structured JSON payload. This is the mechanism that makes context collection possible without the agent needing to implement a custom API.

### Context Diff

An optimization that avoids sending the full context payload on every message. Instead, only the first message in a session carries the full context; subsequent messages carry either nothing (if unchanged) or a compact diff (if changed). This keeps the agent's conversation context window lean.

## User Stories

### Viewer Sends Context-Aware Message

1. Viewer is looking at a sales dashboard in the webapp
2. Viewer types "Why did revenue drop?" in the chat widget
3. On send, the widget silently collects the current dashboard state (table data, metrics, filter values) via schema.org microdata
4. If the viewer had selected a specific cell or value, that selection is also captured
5. The message is sent with the context payload attached — the viewer sees only their typed text in the chat
6. The agent receives the message with the full dashboard context injected and can reference specific data points in its response

### Viewer Sends Multiple Messages Without Changing View

1. Viewer sends a first message — full context is attached
2. Viewer sends a follow-up question without changing the dashboard
3. The backend detects that context is identical to the previous message and omits it entirely — zero overhead
4. Viewer sends a third message — still identical, still omitted
5. The agent conversation stays lean; only the first message carries the context payload

### Viewer Changes View Between Messages

1. Viewer sends a message about the Q3 revenue table — full context attached
2. Viewer applies a filter to show Q4 data instead
3. Viewer sends another message — the backend computes a diff showing which fields changed, added, or were removed
4. Only the diff is sent to the agent as a `<context_update>` block, not the full payload

## Business Rules

### Context Collection

- Context is collected automatically on every message send — no user action required
- No UI indication is shown that context was collected
- If collection fails or times out (500ms), the message sends without context — never blocks the user
- Selected text is truncated at 2,000 characters
- Total context payload is truncated at 10,000 characters on the backend before storage

### Context Storage

- Context is stored in `message_metadata` (a JSON column), NOT in `message.content`
- `message.content` always contains only the viewer's typed text — the chat UI never renders context
- Context is injected into agent-bound content at dispatch time, wrapped in XML tags that only the agent environment sees

### Context Diff Optimization (Three-Tier Behavior)

| Situation | Block injected into agent-bound content |
|---|---|
| First message in session (no prior sent context) | Full `<page_context>` block with complete JSON payload |
| Subsequent message — context identical to previous | Nothing — block omitted entirely |
| Subsequent message — context changed | Compact `<context_update>` block containing only the JSON diff |

- If either the current or previous context cannot be parsed as JSON, the system falls back to sending the full `<page_context>` block
- The diff captures which fields changed, which were added, and which were removed
- In a batch of multiple pending messages, each message diffs against the immediately preceding one (not always against the last sent message)

### Schema.org Microdata Requirements

- The building agent must annotate all significant elements (tables, cards, metrics, filters, form inputs) with schema.org microdata attributes (`itemscope`, `itemtype`, `itemprop`)
- Every webapp HTML page must include the context bridge script (`<script src="./assets/context-bridge.js"></script>`) — the script is auto-available and does not need to be created by the agent
- Instructions for the building agent are defined in `WEBAPP_BUILDING.md`; full HTML markup examples are available at `/app/core/webapp-framework/SCHEMA_EXAMPLES.md`

## Architecture Overview

```
Viewer types message in chat widget
        |
        v
buildPageContext() collects:
  - schema.org microdata (via postMessage to iframe)
  - selected text (via window.getSelection)
        |
        v
POST /messages/stream { content: "...", page_context: "{...}" }
        |
        v
Route truncates page_context (max 10,000 chars)
        |
        v
SessionService stores page_context in message_metadata
(message.content stays clean)
        |
        v
collect_pending_messages() at dispatch time:
  - Looks up previous sent context
  - Computes diff (or uses full / omits)
  - Injects <page_context> or <context_update> into agent-bound content
        |
        v
Agent environment receives augmented content
```

## Integration Points

- **[Webapp Chat](webapp_chat.md)** — context management is an aspect of the chat widget; context is collected on message send and injected at message dispatch
- **[Agent Prompts](../agent_prompts/agent_prompts.md)** — `WEBAPP_BUILDING.md` contains schema.org markup and context bridge script instructions for the building agent
- **[Agent Sessions](../../application/agent_sessions/agent_sessions.md)** — context is stored in `message_metadata` on `SessionMessage`, using the existing JSON column

---

*Last updated: 2026-03-08 — extracted as standalone aspect document from webapp_chat.md*
