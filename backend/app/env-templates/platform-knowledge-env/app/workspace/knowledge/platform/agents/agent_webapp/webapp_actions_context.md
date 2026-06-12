# Webapp Actions Context & Session Instructions

## Purpose

Enable both building and conversation agents to understand that a session is webapp-connected — once, at the start of each such session — without burdening the system prompt of every session. The mechanism uses a one-time orientation injection triggered by the first message with page context. The two modes receive different levels of detail because they access the actions documentation through different paths.

## Core Concepts

### WEB_APP_ACTIONS.md — Actions Registry

A Markdown file placed in the agent workspace (`/app/workspace/webapp/WEB_APP_ACTIONS.md`) that documents every webapp action the agent can use. It is seeded from an env-template at environment initialization and is designed to be edited by the building agent as it adds custom event handlers to the webapp JavaScript.

The file serves two audiences:

- **Building agent** — reads it on demand when actively working on webapp files. It is not loaded into the building mode system prompt. Instead, `WEBAPP_BUILDING.md` instructs the agent to update this file whenever it adds custom `webapp_action_*` event listeners.
- **Conversation agent** — receives the file contents exactly once per session, via one-time instruction injection on the first message that arrives with page context (proving the session is webapp-connected).

### One-Time Session Instruction Injection

On the first message in a session that carries `page_context` in its metadata, the backend detects that the session is webapp-connected and triggers a one-time injection. The injection is **mode-aware**:

1. A session flag (`webapp_actions_context_sent`) is set on the session, committed to the database immediately to prevent race conditions.
2. A static orientation text is always included, explaining the page_context/context_update blocks and the `<webapp_action>` concept.
3. **Conversation mode only**: the message payload also includes the absolute path to `WEB_APP_ACTIONS.md`. Agent-core reads the file and inlines its contents alongside the orientation text. This is the only way the conversation agent learns about available actions.
4. **Building mode**: only the orientation text is sent (no file path). The building agent already has full access to the actions documentation through `WEBAPP_BUILDING.md` → `ACTIONS_REFERENCE.md` → `WEB_APP_ACTIONS.md` in its system prompt and on-demand file reads.
5. Agent-core assembles an `<extra_instructions>` block and prepends it to the user message before the SDK call.

On all subsequent messages in the same session, the flag is already set and no injection occurs.

### Session Flag

The flag `webapp_actions_context_sent` lives in `Session.session_metadata` (an existing JSON column). It is set to `True` on first injection and never cleared. The pattern follows the established `recovery_pending` flag precedent.

## User Stories

### Conversation Agent Receives Webapp Context on First Message

1. A viewer opens the webapp and types a message in the chat widget.
2. The chat widget sends the message with `page_context` metadata.
3. The backend detects this is the first `page_context` message for the session and the session is in conversation mode.
4. The backend includes the orientation text and the contents of `WEB_APP_ACTIONS.md` as a one-time `<extra_instructions>` block prepended to the message.
5. The agent receives the full actions registry and the orientation text alongside the user's message and page context.
6. The agent can now use `<webapp_action>` tags in its responses, referencing the documented action types and any custom actions the building agent added.

### Building Agent Receives Webapp Context on First Message

1. A user opens the webapp chat widget while the session is in building mode.
2. The chat widget sends the message with `page_context` metadata.
3. The backend detects this is the first `page_context` message for the session and the session is in building mode.
4. The backend includes only the orientation text as a one-time `<extra_instructions>` block — no file contents are inlined.
5. The building agent learns that the session is webapp-connected and that messages carry page context, but does not receive the actions registry inline — it already has the full documentation via `WEBAPP_BUILDING.md` and can read `WEB_APP_ACTIONS.md` on demand.

### Subsequent Messages Have No Overhead

1. The viewer sends a second message in the same session.
2. The backend checks `session_metadata["webapp_actions_context_sent"]` — it is already `True`.
3. No extra instructions are included. The message is sent as-is.
4. The agent already knows about available actions from the first injection.

### Building Agent Updates the Registry

1. The building agent adds a custom `webapp_action_my_filter` event listener in the webapp JavaScript.
2. The building agent reads `WEBAPP_BUILDING.md` which instructs it to update `./WEB_APP_ACTIONS.md`.
3. The building agent appends an entry to the `## Custom Actions` section of `WEB_APP_ACTIONS.md`.
4. In the next conversation session, the first page_context message delivers the updated registry to the conversation agent.

### Building Agent Makes the Webapp Action-Ready

1. The building agent creates a form in the webapp HTML.
2. `WEBAPP_BUILDING.md` instructs the agent to add `id` attributes to all `<form>` elements and `name` attributes to all form fields — without these, the `update_form` action cannot target them.
3. The agent evaluates interactive elements that use JS framework state (Alpine.js `x-data`, filter button groups, tabs) and recognizes that `update_form` will not work on them.
4. The agent creates custom `webapp_action_*` event listeners for these elements and documents them in `WEB_APP_ACTIONS.md`.
5. The conversation agent later uses the correct custom actions instead of trying `update_form` on non-form elements.

### Non-Webapp Session — No Impact

1. A user starts a regular chat session (no webapp, no chat widget).
2. Messages are sent without `page_context` metadata.
3. The `has_page_context` check in the backend is never true.
4. The session flag is never set and no extra instructions are ever injected.
5. Zero overhead for non-webapp sessions.

## Business Rules

### When Injection Fires

- Injection fires only on messages where at least one pending message has `page_context` in `message_metadata`.
- Injection fires at most once per session lifetime — the flag is never reset.
- The flag is committed immediately to the database before the payload is sent, preventing duplicate injection if two messages arrive concurrently.
- The injection is mode-aware: conversation mode includes both orientation text and `WEB_APP_ACTIONS.md` contents; building mode includes only the orientation text.

### What Is Never Changed

- Neither building mode nor conversation mode system prompts are modified. `WEB_APP_ACTIONS.md` is never loaded into the system prompt for any session type.
- The `PromptGenerator` inside agent-core has no changes for this feature.
- Non-webapp sessions (email, A2A, MCP) are not affected — they never send `page_context`.

### Actions Registry File

- The file is seeded from the env-template and placed in `/app/workspace/webapp/WEB_APP_ACTIONS.md` at environment initialization.
- The file documents five built-in action types and provides a `## Custom Actions` section for the building agent to fill in.
- The file is agent-writable. If the agent deletes it, the session instruction block still fires on the next new session (the flag is session-scoped), but the file content will be absent. Agent-core logs a warning and includes only the prepend orientation text — the agent retains basic orientation about `<webapp_action>` tags.
- For full action field specifications, the file points the agent to `/app/core/webapp-framework/ACTIONS_REFERENCE.md`.

### Generic Injection Mechanism

The `include_extra_instructions` / `extra_instructions_prepend` payload fields are fully generic — they carry only an absolute path and static text. No webapp-specific logic lives in agent-core. Any future feature can reuse the same mechanism by passing a different file path and prepend text.

## Architecture Overview

```
First message with page_context arrives
        |
        v
message_service detects has_page_context = True
        |
        v
session_service.activate_webapp_context()
  reads session_metadata["webapp_actions_context_sent"]
        |
        ├── Already True → return False (no injection)
        |
        └── Absent → set True, commit, return True
                |
                v
        message_service builds payload (mode-aware):
          extra_instructions_prepend = "This session is connected to a webapp..."
                |
                ├── conversation mode:
                |     include_extra_instructions = "/app/workspace/webapp/WEB_APP_ACTIONS.md"
                |     (file inlined — only way conversation agent learns available actions)
                |
                └── building mode:
                      include_extra_instructions = None
                      (no file — agent already has WEBAPP_BUILDING.md → ACTIONS_REFERENCE.md)
                |
                v
        agent-core._build_extra_instructions_block()
          assembles <extra_instructions> block (prepend text + file contents if path provided)
                |
                v
        effective_message = <extra_instructions>...</extra_instructions>

        <user message + page_context>
                |
                v
        SDK call — agent receives orientation on first message only
```

## Integration Points

- **[Webapp Chat Actions](webapp_chat_actions.md)** — this feature ensures the conversation agent knows which `<webapp_action>` types are available to emit; the actual action detection and delivery pipeline is documented there
- **[Webapp Chat Context](webapp_chat_context.md)** — `page_context` presence in message metadata is the trigger condition for instruction injection; the context collection and diff mechanism is documented there
- **[Webapp Chat](webapp_chat.md)** — the chat widget is the entry point where page_context is attached to messages
- **[Agent Prompts](../agent_prompts/agent_prompts.md)** — `WEBAPP_BUILDING.md` instructs the building agent to maintain `WEB_APP_ACTIONS.md`; this is where the registry integration with building mode lives
- **[Agent Sessions](../../application/agent_sessions/agent_sessions.md)** — `session_metadata` on the `Session` model stores the flag; session-scoped behavior is defined by the session lifecycle

## Edge Cases

| Scenario | Behavior |
|----------|----------|
| Multiple pending messages, first has `page_context` | Injection prepended to first message only; batch processes normally |
| `WEB_APP_ACTIONS.md` deleted from workspace | Agent-core logs warning, includes prepend text only; agent retains basic orientation |
| DB commit fails when setting flag | On the next message, flag is still absent — injection fires again (at most one duplicate) |
| Session not found in `activate_webapp_context()` | Returns `False` (safe default — no injection, no error) |
| Email / A2A / MCP sessions | Never have `page_context` — injection condition never met |
| Webapp enabled mid-session after session started | First message with `page_context` still triggers injection normally |

---

*Last updated: 2026-03-10*
