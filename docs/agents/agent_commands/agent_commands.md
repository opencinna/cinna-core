# Agent Commands

## Purpose

Slash commands (`/files`, `/session-recover`, etc.) are instant, deterministic actions users can invoke during an agent session. Unlike regular messages, commands execute locally on the backend without an LLM call, providing immediate responses.

## Core Concepts

- **Command** — A message starting with `/` that matches a registered handler (e.g., `/files`, `/session-recover`, `/session-reset`)
- **CommandHandler** — Backend class implementing a specific command's logic
- **CommandContext** — Per-invocation context containing session ID, environment ID, user ID, caller type, and host URLs
- **CommandResult** — Handler output: a markdown string and an optional error flag
- **Context-aware links** — The same command produces different link formats depending on whether the caller is a UI user or an A2A client
- **Static handler registry** — Handlers are registered at import time; no runtime configuration required

## User Stories / Flows

**User types `/files` in chat:**
1. User types `/files` in the session input
2. Backend detects the command and routes to `FilesCommandHandler`
3. Handler queries the workspace and returns a markdown list of files with clickable links
4. Response appears instantly in chat via WebSocket — no LLM involved

**A2A client sends `/files`:**
1. A2A client sends `/files` via `message/send` or `message/stream`
2. Backend executes the command, generates file links with short-lived workspace view tokens
3. A2A client receives a completed task with the markdown content
4. Client can open file links in a browser using the embedded tokens

**User sends an unrecognized `/xyz` command:**
1. User types `/xyz` — not a registered command
2. `CommandService.is_command()` returns false
3. Message passes through to the normal LLM flow unchanged

## Business Rules

- Commands bypass the LLM pipeline entirely — no streaming, no agent-env activation
- Command messages are marked `sent_to_agent_status="sent"` to prevent them being picked up by the streaming system
- Command detection occurs after session validation but before file handling — all callers (UI, A2A send, A2A stream) benefit automatically
- Link format differs by caller: UI links point to frontend routes; A2A links use public backend endpoints with workspace view tokens
- Non-matching `/xyz` inputs (unregistered commands) are forwarded to the LLM as normal messages
- Commands are registered at import time via `CommandService.register()` — adding a handler requires no changes to routes or session service
- Command response messages carry metadata `{"command": true, "command_name": "/name"}` for downstream consumers

## Architecture Overview

```
User/A2A Client types "/files"
         │
         ▼
SessionService.send_session_message()   ← single entry point for all callers
         │
         ├── CommandService.is_command(content)?
         │       YES → Build CommandContext
         │             Create user message (sent_to_agent_status="sent")
         │             CommandService.execute() → handler.execute()
         │             Create agent message with markdown response
         │             Emit WebSocket events (UI real-time update)
         │             Return {action: "command_executed", ...}
         │
         │       NO  → Normal LLM flow (unchanged)
         │
         ▼
Caller handles "command_executed":
  UI (messages.py)       → HTTP 200, frontend already received via WebSocket
  A2A send (a2a_request_handler.py) → Task(state=completed) with response text
  A2A stream (a2a_request_handler.py) → Single SSE status-update event, then done
```

## Available Commands

| Command | Purpose | Aspect Doc |
|---------|---------|------------|
| `/files` | List user-facing workspace files with clickable links | [files_command.md](files_command.md) |
| `/files-all` | List all workspace sections (files, scripts, logs, docs, uploads) | [files_command.md](files_command.md) |
| `/session-recover` | Recover from lost SDK connection; optionally auto-resend the failed message | [session_recovery_command.md](session_recovery_command.md) |
| `/session-reset` | Clear SDK session metadata for a clean-slate restart with no recovery context | [session_reset_command.md](session_reset_command.md) |
| `/webapp` | Return the shareable webapp URL for the agent (first active share link) | [webapp_command.md](webapp_command.md) |
| `/rebuild-env` | Rebuild the active environment (fails if any session is streaming) | [rebuild_env_command.md](rebuild_env_command.md) |

## Integration Points

- **[Agent Sessions](../../application/agent_sessions/agent_sessions.md)** — Commands are invoked within sessions; session state is read/modified by recovery and reset commands
- **[Agent Environments](../agent_environments/agent_environments.md)** — `/files` and `/files-all` require a running environment to query workspace files
- **[Agent File Management](../agent_file_management/agent_file_management.md)** — File listing reuses existing workspace tree API; workspace view tokens gate public file access
- **[A2A Protocol](../../application/a2a_integration/a2a_protocol/a2a_protocol.md)** — A2A callers send commands as regular messages and receive completed tasks in response
