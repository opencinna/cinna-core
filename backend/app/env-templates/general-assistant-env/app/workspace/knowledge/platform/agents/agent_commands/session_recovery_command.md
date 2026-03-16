# Session Recovery

## Purpose

Allows users to recover a session after a lost SDK connection (e.g., after an agent environment container rebuild), continuing the conversation with full context. Recovery can be triggered by a UI button or the `/session-recover` command.

## Core Concepts

- **Lost SDK connection** — When an agent-env Docker container is rebuilt, the `external_session_id` is gone. The next message fails with a system error.
- **`recovery_pending` flag** — A session metadata flag set during recovery. Consumed once on the next message to prepend conversation history to the agent.
- **Recovery context** — The last 20 user/agent messages (no system messages) formatted as a block and prepended to the next message sent to the agent environment. Not stored in DB; never shown in the UI.
- **Auto-resend** — When the failed-message pattern is detected (trailing system errors followed by a user message), the existing failed user message is reset to `pending` and re-sent automatically — no duplicate message is created.
- **UI trigger** — "Recover Session" button appears on all system error messages
- **Command trigger** — `/session-recover` slash command; equivalent to the REST endpoint, useful for A2A clients

## User Stories / Flows

**Typical recovery (auto-resend — UI):**
1. SDK session is lost (container rebuild, crash)
2. User sends a message → system error appears in chat
3. User clicks "Recover Session" button on the error message
4. Confirmation modal shows "Recover & Resend" — last message will be automatically resent
5. User confirms → backend clears SDK metadata, sets `recovery_pending`, resets failed message to `pending`
6. Backend calls `initiate_stream()` — re-processes the existing user message with recovery context prepended
7. Agent-env creates a fresh SDK session; agent responds with awareness of previous context
8. "Session recovered" system message appears in chat

**Manual recovery (no failed message detected):**
1. User clicks "Recover Session" on any system error
2. Modal shows "Recover Session" — user must send a new message manually
3. User confirms → backend clears SDK metadata, sets `recovery_pending`
4. User sends a new message → recovery context prepended → fresh SDK session created

**A2A client recovery:**
1. A2A client detects a session error
2. Client sends `/session-recover` via `message/send`
3. Backend executes recovery: clears SDK metadata, sets `recovery_pending`, detects and resends failed message if applicable
4. Client receives a completed task: `"Session recovered. Resending last message."` or `"Session recovered. Send a new message to continue."`
5. If auto-resend triggered: streaming resumes automatically for the re-sent message

## Business Rules

- Recovery is idempotent — triggering multiple times is harmless
- Recovery context is injected into the message content sent to agent-env only — the original DB message is never modified
- The UI shows only the original user message; recovery context is invisible to users
- `recovery_pending` is consumed exactly once, then cleared
- Auto-resend is backend-driven: the backend detects the failed-message pattern, resets the message, and triggers `initiate_stream()` — the frontend only adapts its UI cosmetically
- A "Session recovered" system message is always added to the chat history
- `/session-recover` command cannot reuse `mark_session_for_recovery()` directly because the command framework has already created the command's own user message in DB, which would break the trailing-error detection pattern. The command handler implements its own detection logic that skips the command message before scanning.

## Recovery Context Format

Prepended to the user message content (sent to agent-env only):

```
[SESSION RECOVERY]
Previous conversation history:
User: <full message content>
Assistant: <full message content>
...
[END SESSION RECOVERY]
Please continue the conversation. The user's new message follows:

<actual user message>
```

- Last 20 messages, full content (no truncation)
- System messages excluded
- User/agent messages only

## Session Metadata Lifecycle

| Field | Before Recovery | After `mark_session_for_recovery()` | After Next Message |
|-------|----------------|--------------------------------------|--------------------|
| `external_session_id` | `"sdk-123"` | removed | new value from agent-env |
| `sdk_type` | `"claude"` | removed | new value from agent-env |
| `last_sdk_message_id` | `"msg-456"` | removed | new value from agent-env |
| `recovery_pending` | absent | `true` | removed |
| `status` | `"error"` / any | `"active"` | `"active"` |

**Failed user message field:**

| Field | Before Recovery | After `mark_session_for_recovery()` |
|-------|----------------|--------------------------------------|
| `sent_to_agent_status` | `"sent"` | `"pending"` |

## Auto-Resend Detection

Both backend and frontend independently detect the failed-message pattern by walking backwards from the end of the messages list:
1. Skip all trailing system error messages (`role="system"`, `status="error"`)
2. If the next message is a user message (`role="user"`), it is the "failed message"
3. **Backend**: resets `sent_to_agent_status` to `"pending"` and calls `initiate_stream()`
4. **Frontend**: adapts UI — shows "Recover & Resend" button text and resend toast (cosmetic only, no message creation)

## Architecture Overview

```
Error Message  →  Recover Button / /session-recover command
(frontend UI)     (frontend / A2A)
                        │
                        ▼ POST /sessions/{id}/recover  OR  CommandService.execute()
                   Backend: mark_session_for_recovery() + create system message
                        │
                        ├── Failed message detected?
                        │       YES → reset sent_to_agent_status="pending"
                        │             initiate_stream()  → process_pending_messages()
                        │             → prepend recovery context → send to agent-env
                        │       NO  → next user message will carry recovery context
                        │
                        ▼
                   agent-env creates fresh SDK session
                   Agent responds with conversation history awareness
```

## Integration Points

- **[Agent Sessions](../../application/agent_sessions/agent_sessions.md)** — Session status and metadata modified by recovery
- **[Agent Environments](../agent_environments/agent_environments.md)** — Container rebuild is the primary trigger; fresh SDK session created in agent-env on next message
- **[A2A Protocol](../../application/a2a_integration/a2a_protocol/a2a_protocol.md)** — A2A clients use `/session-recover` command as the programmatic recovery path

## Technical Reference

**Backend:**
- `backend/app/services/session_service.py` — `mark_session_for_recovery()` (clears SDK metadata, sets `recovery_pending`, detects and resets failed message)
- `backend/app/services/message_service.py` — `build_recovery_context()` (builds history block), `process_pending_messages()` (injects context, clears flag)
- `backend/app/api/routes/sessions.py` — `POST /api/v1/sessions/{id}/recover`
- `backend/app/services/commands/session_recover_command.py` — `/session-recover` command handler

**Frontend:**
- `frontend/src/components/Chat/RecoverSessionModal.tsx` — Confirmation dialog; detects auto-resend pattern; calls `SessionsService.recoverSession()`
- `frontend/src/components/Chat/MessageBubble.tsx` — Shows "Recover Session" button on system error messages
- `frontend/src/components/Chat/MessageList.tsx` — Passes `sessionId` to `MessageBubble`
- `frontend/src/routes/_layout/session/$sessionId.tsx` — Passes `sessionId` from route params to `MessageList`
