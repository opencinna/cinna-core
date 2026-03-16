# `/session-reset` Command

## Purpose

Clears SDK session metadata so the next message starts a completely fresh conversation with the agent — no recovery context, no auto-resend, no conversation history prepended.

## When to Use

- The agent is in a bad state and you want to start fresh without carrying over context
- You want to force a new SDK session without triggering recovery logic
- An A2A client needs to reset the session via the message interface instead of calling `POST /sessions/{id}/reset-sdk`

## Execution Flow

1. `SessionService.clear_external_session()` — removes `external_session_id`, `sdk_type`, `last_sdk_message_id` from session metadata
2. A "Session reset" system message is created in the session
3. Returns `CommandResult` with reset confirmation

No `recovery_pending` flag is set. No message scanning. No `initiate_stream()`.

## Business Rules

- The next message after `/session-reset` starts a completely fresh SDK session with no conversation history
- No auto-resend is triggered regardless of whether a failed message exists
- Contrast with `/session-recover`: reset is an explicit "clean slate" operation, not a recovery operation

## Behavior

| Scenario | Response | Side Effect |
|----------|----------|-------------|
| Any state | "Session reset. Next message will start a fresh conversation with the agent." | SDK metadata cleared; next message starts fresh |

## Comparison with `/session-recover`

| Aspect | `/session-recover` | `/session-reset` |
|--------|-------------------|-----------------|
| Clears SDK metadata | Yes | Yes |
| Sets `recovery_pending` | Yes | No |
| Detects failed messages | Yes | No |
| Auto-resends failed message | Yes | No |
| System message | "Session recovered" | "Session reset" |
| Next message behavior | Includes conversation history | Clean slate |

## Integration Points

- **[Session Recovery](session_recovery_command.md)** — Compare and contrast with `/session-recover` behavior
- **[Agent Sessions](../../application/agent_sessions/agent_sessions.md)** — Session metadata modified by this command

## Technical Reference

- `backend/app/services/commands/session_reset_command.py` — Command handler
- `backend/app/services/session_service.py` — `clear_external_session()` method
