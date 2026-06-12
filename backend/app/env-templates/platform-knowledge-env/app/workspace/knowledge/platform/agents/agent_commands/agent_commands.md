# Agent Commands

## Purpose

Slash commands (`/files`, `/session-recover`, etc.) are instant, deterministic actions users can invoke during an agent session. Unlike regular messages, commands execute locally on the backend without an LLM call, providing immediate responses.

## Core Concepts

- **Command** — A message starting with `/` that matches a registered handler (e.g., `/files`, `/session-recover`, `/run:check`)
- **CommandHandler** — Backend class implementing a specific command's logic
- **CommandContext** — Per-invocation context containing session ID, environment ID, user ID, caller type, and host URLs
- **CommandResult** — Handler output: a markdown string and an optional error flag; streaming commands also return `routing="command_stream"` and a `resolved_command`
- **Context-aware links** — The same command produces different link formats depending on whether the caller is a UI user or an A2A client
- **Static handler registry** — Handlers are registered at import time; no runtime configuration required
- **`include_in_llm_context`** — Class attribute on `CommandHandler`; controls whether the command's output is included in the `<prior_commands>` block on the next LLM turn (default `True`)
- **`streams`** — Class attribute on `CommandHandler`; when `True`, the command queues as a pending message and executes through `SessionStreamProcessor`'s command batch path instead of returning inline

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

- Most commands bypass the LLM pipeline entirely and return inline with no streaming. Agent-env activation is bypassed for most sync commands, but commands that declare `requires_running_environment = True` trigger an automatic synchronous wake-up when the environment is not running before the handler executes. The five handlers that opt in are: `/files`, `/files-all`, `/agent-status`, `/session-recover`, and `/session-reset`. For `/files` and `/files-all` this is a technical requirement (the workspace tree cannot be fetched from a stopped container). For `/agent-status` the wake-up ensures the command returns a fresh `STATUS.md` rather than a cached snapshot; the cached-snapshot fallback inside the handler still activates as a defensive last resort if `fetch_status` fails post-wake (e.g. the adapter is still unreachable). For `/session-recover` and `/session-reset` the wake-up is a UX choice: both commands are pure session-metadata DB operations on their own, but they almost always precede a fresh message, so pre-waking the environment removes friction on the user's next interaction. The `/run:<name>` command family is a separate exception: it queues as a pending message and streams output through `SessionStreamProcessor`.
- Command messages are marked `sent_to_agent_status="sent"` immediately for sync commands, or upon command-batch processing for streaming commands, to prevent them being picked up again.
- Command responses are created as **system messages** (`role="system"`), not agent messages — they appear as centered system notifications in the UI, visually distinct from LLM-generated responses.
- Command detection occurs after session validation but before file handling — all callers (UI, A2A send, A2A stream) benefit automatically.
- Link format differs by caller: UI links point to frontend routes; A2A links use public backend endpoints with workspace view tokens.
- Non-matching `/xyz` inputs (unregistered commands) are forwarded to the LLM as normal messages.
- Commands are registered at import time via `CommandService.register()` — adding a handler requires no changes to routes or session service.
- Command response messages carry metadata `{"command": true, "command_name": "/name"}` for downstream consumers including the LLM context bridging feature.
- A2A streaming callers (`message/stream`) receive an extra `notice` event ("Starting up the agent environment, this may take a moment...") before the command result when a `requires_running_environment` command wakes the env — matching the notice already emitted for `/run:<name>` and LLM messages. The notice fires when the pre-call env status is `"suspended"`, `"activating"`, or `"starting"`. On wake-up failure, the orchestrator short-circuits with `action="error"` and a friendly message; no DB rows are created.
- Command output with `include_in_llm_context=True` is automatically forwarded to the next LLM turn as a `<prior_commands>` XML block. This applies to `/files`, `/files-all`, `/agent-status`, and all `/run:*` commands.

## Architecture Overview

```
User/A2A Client sends a message
         │
         ▼
SessionService.send_session_message()   ← single entry point for all callers
         │
         ├── CommandService.is_command(content)?
         │       YES → Build CommandContext
         │
         │             handler.streams == False (sync path):
         │               Create user message (sent_to_agent_status="sent")
         │               handler.execute() → CommandResult
         │               Create system message with markdown response (role="system")
         │               Emit WebSocket events (UI real-time update)
         │               Return {action: "command_executed", ...}
         │
         │             handler.streams == True (streaming path, e.g. /run:<name>):
         │               Create user message (sent_to_agent_status="pending",
         │                 message_metadata.routing="command_stream")
         │               initiate_stream() → queued for SessionStreamProcessor
         │               Return {action: "queued", ...}
         │               SessionStreamProcessor routes command batch →
         │                 agent-env POST /command/stream → stdout/stderr SSE
         │                 finalized system message with exit code + output
         │
         │       NO  → Normal LLM flow (unchanged)
         │
         ▼
Before each LLM turn, SessionStreamProcessor prepends <prior_commands> XML
(command outputs with include_in_llm_context=True since last agent response)
```

## Available Commands

| Command | Purpose | Streams | LLM ctx | Aspect Doc |
|---------|---------|---------|---------|------------|
| `/files` | List user-facing workspace files with clickable links | no | yes | [files_command.md](files_command.md) |
| `/files-all` | List all workspace sections (files, scripts, logs, docs, uploads) | no | yes | [files_command.md](files_command.md) |
| `/session-recover` | Recover from lost SDK connection; optionally auto-resend the failed message | no | no | [session_recovery_command.md](session_recovery_command.md) |
| `/session-reset` | Clear SDK session metadata for a clean-slate restart with no recovery context | no | no | [session_reset_command.md](session_reset_command.md) |
| `/webapp` | Return the shareable webapp URL for the agent (first active share link) | no | no | [webapp_command.md](webapp_command.md) |
| `/rebuild-env` | Rebuild the active environment (fails if any session is streaming) | no | no | [rebuild_env_command.md](rebuild_env_command.md) |
| `/agent-status` | Show the agent's self-reported status from `STATUS.md` — severity, summary, timestamp, and full body | no | yes | [agent_status_command.md](agent_status_command.md) |
| `/run` | List all declared CLI commands (from `CLI_COMMANDS.yaml`) as a markdown table | no | — | [cli_commands](../../agents/cli_commands/cli_commands.md) |
| `/run:<name>` | Execute a declared CLI command — streams stdout/stderr; appears as a terminal-style system message | yes | yes | [cli_commands](../../agents/cli_commands/cli_commands.md) |

**Column legend:**
- **Streams**: command queues as a pending message and streams output through `SessionStreamProcessor`
- **LLM ctx**: command output is forwarded to the next LLM turn via `<prior_commands>` block; `—` for `/run` (list only, no output to forward)

## Integration Points

- **[Agent Sessions](../../application/agent_sessions/agent_sessions.md)** — Commands are invoked within sessions; session state is read/modified by recovery and reset commands
- **[Agent Environments](../agent_environments/agent_environments.md)** — `/files`, `/files-all`, and `/run:*` require a running environment
- **[Agent File Management](../agent_file_management/agent_file_management.md)** — File listing reuses existing workspace tree API; workspace view tokens gate public file access
- **[A2A Protocol](../../application/a2a_integration/a2a_protocol/a2a_protocol.md)** — A2A callers send commands as regular messages and receive completed tasks in response; `/run:<name>` streams via the same SSE pipeline; all command-originated TextParts carry `cinna.command_invocation` metadata with the original slash invocation, letting A2A clients distinguish slash-command output from LLM-initiated tool calls
- **[Agent Status Tracking](agent_status_command.md)** — `/agent-status` reads `STATUS.md` from the workspace; the feature also exposes a REST endpoint and a real-time WebSocket event
- **[CLI Commands](../../agents/cli_commands/cli_commands.md)** — `/run` and `/run:<name>` are backed by `CLI_COMMANDS.yaml`; commands surface as `cinna.run.*` A2A skills in the extended agent card
- **[Non-LLM Context Bridging](non_llm_context_bridging_tech.md)** — Technical spec for how command output is forwarded to the next LLM turn via `<prior_commands>` XML
