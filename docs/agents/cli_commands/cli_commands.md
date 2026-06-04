# CLI Commands Sync and Discovery

Agents can expose a set of named shell commands that users and A2A clients invoke directly — without spending tokens on an LLM turn. These commands power the `/run:<name>` slash command in the chat UI, surface as A2A skills (`cinna.run.<name>`) in the authenticated agent card, appear in the slash-command autocomplete popup with a tooltip showing the resolved shell string, and feed output back into the LLM context on the next conversation turn. The popup also surfaces `/run-list` (discovery) whenever the agent has at least one CLI command configured.

---

## Overview

### What is CLI Commands Sync?

The platform provides a convention file `CLI_COMMANDS.yaml` that agents write to their workspace `docs/` folder to declare named shell commands. The backend reads, parses, and caches this file whenever the environment activates, after every agent-triggered session stream, and after scheduled script executions. The parsed list is stored on the `AgentEnvironment` DB row and served via the session commands autocomplete endpoint.

### Why is this useful?

Many agent workflows involve deterministic, repeatable operations — monthly data-quality checks, report generation, cache refreshes, reindex jobs. With `CLI_COMMANDS.yaml`, an agent owner declares these operations once. Users can then invoke them on demand via `/run:<name>` in any chat session without needing the LLM to figure out what script to run. This is faster, cheaper, and more reliable than a fully LLM-mediated invocation. External A2A callers discover the same commands through the agent card's `skills` array and invoke them by sending `/run:<name>` as a plain message.

---

## User Flow

### Writing CLI_COMMANDS.yaml

An agent (in building mode) or the agent's owner creates or edits `/app/workspace/docs/CLI_COMMANDS.yaml`:

```yaml
# /app/workspace/docs/CLI_COMMANDS.yaml
# Commands exposed via /run:<name> in chat, A2A skills, and webapp.

commands:
  - name: check
    description: Monthly data-quality check. Run after the month closes.
    command: uv run /app/workspace/scripts/check-data.py --month

  - name: report
    description: Generate the weekly report and upload it to S3.
    command: uv run /app/workspace/scripts/weekly_report.py
```

On save, the next post-action event (or sending `/run` with no args) triggers the backend to re-read, parse, and cache the file.

### Discovering Commands in Chat

In the session chat UI, typing `/` opens the slash-command autocomplete popup. Dynamic `/run:<name>` commands appear in the list alongside static commands. Hovering over a dynamic command shows a tooltip with the raw shell command string, so the user can verify what will run.

Typing `/run:` narrows the popup to only show dynamic subcommands. Once selected, the command executes inside the agent's environment without an LLM turn.

### Listing Commands in Chat (`/run-list`)

Selecting `/run-list` from the popup (or typing `/run-list`) returns a formatted markdown table of all declared commands — name, resolved shell string, and description. This serves as an in-chat discovery mechanism for users. `/run-list` only appears in the popup when the agent has at least one CLI command configured.

```
| Name | Command | Description |
|------|---------|-------------|
| `check` | `uv run /app/workspace/scripts/check-data.py --month` | Monthly data-quality check. |
| `report` | `uv run /app/workspace/scripts/weekly_report.py` | Generate the weekly report. |
```

`/run` (no args) still produces the same table and remains the convention A2A LLM-clients use to enumerate available commands. It is hidden from the autocomplete popup — it remains functional for manual typing and programmatic A2A invocation, but the popup exposes only `/run-list` (discovery) and `/run:<name>` (execution).

### Executing `/run:<name>`

Sending `/run:<name>` queues a pending message with `routing="command_stream"`. The `SessionStreamProcessor` dispatches it as a command batch to the agent-env's `POST /command/stream` endpoint. Stdout and stderr stream back as SSE events and are displayed in a terminal-style system message bubble. The exit code appears in the footer when execution completes.

The resolved shell command string is never editable by users — it executes exactly as declared in `CLI_COMMANDS.yaml`.

### Starter File

Every new agent environment includes a starter `CLI_COMMANDS.yaml` at `/app/workspace/docs/CLI_COMMANDS.yaml` with `commands: []`. Agents add entries as they write scripts they want to expose.

---

## File Format

### Location

`/app/workspace/docs/CLI_COMMANDS.yaml` (relative to workspace root: `docs/CLI_COMMANDS.yaml`) <!-- nocheck -->

### Structure

```yaml
commands:
  - name: <slug>          # required; lowercase, [a-z][a-z0-9_-]{0,31}
    description: <text>   # optional; up to 512 chars
    command: <shell>      # required; single-line shell string, up to 1024 chars
```

### Validation Rules

| Field | Rules |
|---|---|
| `name` | Required. Slug: `^[a-z][a-z0-9_-]{0,31}$`. Unique within the file (first occurrence wins). |
| `command` | Required. Single-line shell string, 1–1024 characters after trim. |
| `description` | Optional. Up to 512 characters; truncated silently if longer. |

### Limits

- Maximum 50 commands per file; entries beyond 50 are silently discarded.
- File size cap: 64 KB; content beyond 64 KB is truncated before parsing.
- Unknown top-level keys and unknown per-command keys are silently ignored (forward-compatibility).

---

## Sync Behavior

### Refresh Triggers

The backend refreshes the CLI commands cache at five points:

1. **Environment activation** — immediately after the environment comes online (`ENVIRONMENT_ACTIVATED` event). `CLICommandsService.handle_post_action_event` is registered for this event via the Synced Workspace File Registry in `backend/app/main.py`.
2. **Post-action** — after every session stream (`STREAM_COMPLETED`, `STREAM_ERROR`) including `/run:*` command streams, and after every scheduled execution (`CRON_COMPLETED_OK`, `CRON_TRIGGER_SESSION`, `CRON_ERROR`).
3. **After rebuild** — after a successful `/rebuild-env` operation completes.
4. **File-watcher signal** — the env-core watcher polls `docs/CLI_COMMANDS.yaml` (along with prompt files and `STATUS.md`) every ~5 s and fires `WORKSPACE_FILES_CHANGED` with the changed paths whenever the file stabilises after a write. The CLI commands handler treats the file's presence in `changed_files` as direct evidence the cache is stale and **bypasses the rate limit** for that fetch — same auto-sync semantics the prompt files already use.
5. **Explicit `/run` or `/run-list`** — falls back to the cached list when invoked; the watcher-driven refresh above is the primary source of truth.
6. **Start-time sweep** — `_sync_dynamic_data` (run on every env start, activation, and rebuild) now also calls `CLICommandsService.refresh_after_action(force=True)` directly as part of the start sweep, so the cache is current at the moment the environment becomes active (independent of the `ENVIRONMENT_ACTIVATED` event handler).

### Rate Limiting

A 30-second per-environment rate limit prevents redundant fetches when multiple speculative events fire in quick succession (e.g., a CRON event followed immediately by a STREAM_COMPLETED). The rate limit is shared with `AgentStatusService`, but they maintain independent rate-limit buckets.

`WORKSPACE_FILES_CHANGED` events that explicitly name `docs/CLI_COMMANDS.yaml` in `meta.changed_files` bypass this rate limit — that signal is debounced at the watcher level (≥5 s) and means the file demonstrably changed, so dropping the fetch would leave the cache stale.

### Cache Storage

The parsed command list is stored on the `AgentEnvironment` DB row:
- `cli_commands_raw` — last successfully fetched raw file content
- `cli_commands_parsed` — parsed JSON list: `[{name, command, description}]`
- `cli_commands_fetched_at` — timestamp of last successful fetch
- `cli_commands_error` — last error reason (`file_missing` / `adapter_error` / `parse_error`); `null` on success

---

## Autocomplete UI

The `GET /api/v1/sessions/{session_id}/commands` endpoint returns the ordered list consumed by `SlashCommandPopup`. The service layer (`CommandService.list_for_session`) applies the display rules so the route stays thin:

- Static handlers are included by default.
- **`/run` is hidden** — it remains a valid command for manual typing and A2A callers but is never surfaced in the popup.
- **`/run-list` appears only when the agent has at least one CLI command configured** (i.e., `environment.cli_commands_parsed` is non-empty).
- `/rebuild-env` is marked unavailable when any session on the same environment is actively streaming.
- Dynamic `/run:<name>` entries from the cache are appended. Each dynamic entry includes `resolved_command` — the raw shell string — which the popup renders as a tooltip on hover.

### Availability

Dynamic `/run:<name>` entries are always `is_available=true`. If the environment is not in `running` status, invoking the command activates it the same way a regular user message does. The tooltip always shows the resolved command for reference.

---

## Execution Behavior

### Queue and Stream

Sending `/run:<name>` does not start an LLM turn. Instead:

1. A pending user message is created with `message_metadata.routing="command_stream"` and `sent_to_agent_status="pending"`.
2. `SessionStreamProcessor` picks it up on the next processing cycle, recognizes the `command_stream` routing, and routes it as a command batch (not an LLM batch).
3. The agent-env's `POST /command/stream` endpoint runs `asyncio.create_subprocess_shell`, reads stdout and stderr concurrently, and streams output as SSE events.
4. The backend flushes events to the DB every 2 seconds. A system message is finalized at completion with exit code, duration, and accumulated output.
5. One of `STREAM_COMPLETED`, `STREAM_INTERRUPTED`, or `STREAM_ERROR` is emitted on the event bus. This fires the standard post-action pipeline: `session_interaction_status_changed` WS event (frontend invalidates the messages query immediately — no page refresh needed), activity log entry, task status sync, CLI commands cache refresh, agent status cache refresh.

### Output Display

The resulting system message is rendered in the chat with two modes:
- **While streaming**: terminal-style block — dark background, monospace, stdout in default color, stderr in amber, blinking cursor.
- **When complete**: the accumulated stdout/stderr is rendered through `MarkdownRenderer`, so scripts that emit markdown (tables, lists, headings, code blocks) render properly. Falls back to the raw text layout for plain-text output.

Every command message also has:
- **Header**: command name and truncated resolved command string (with a copy button)
- **Footer**: exit code badge (green check / red X), plus `Timed out` / `Interrupted` / `[truncated]` labels when applicable

### Limits and Safety

| Setting | Value |
|---|---|
| Execution timeout | 300 seconds (configurable) |
| Output cap | 256 KB — truncation marker appended, then stream interrupted |
| Output persisted to | `message.streaming_events` + compiled `message.content` |

### Interrupt

The user can interrupt a running `/run:<name>` command via the same interrupt button used for LLM streams. The backend sends a SIGTERM to the agent-env subprocess (SIGKILL after 2 seconds). The system message is finalized with `exec_interrupted=true`.

### Error / Exit Codes

A non-zero exit code is a normal command outcome — the system message status is `""` (not `"error"`). Only infrastructure failures (adapter unreachable, unhandled exception) produce `status="error"`. This distinction matters for A2A callers: a non-zero exit still maps to `TaskState.completed`.

---

## A2A Integration

### Message-Level Parity

`/run` and `/run:<name>` behave identically from the A2A layer's perspective: they queue as pending messages and flow through `SessionStreamProcessor`. The existing `A2AStreamEventHandler` and `A2AEventMapper` handle the synthesized `tool` events that the command stream produces.

| A2A method | Behavior |
|---|---|
| `message/send` + `/run` | Returns `completed` Task containing the markdown command table |
| `message/send` + `/run:<name>` | Blocks until exec completes; returns `completed` Task with assembled output |
| `message/stream` + `/run:<name>` | Streams stdout/stderr as `status-update (working)` events, then `completed` |
| `tasks/cancel` during `/run:<name>` | Routes to `/command/interrupt` on agent-env; produces `TaskState.canceled` |

### CLI Commands as A2A Skills (Extended Card)

The authenticated extended agent card (`agent/getAuthenticatedExtendedCard`) includes CLI commands as named `AgentSkill` entries. Each command maps to one skill:

| `AgentSkill` field | Value |
|---|---|
| `id` | `cinna.run.<name>` — namespaced to avoid collision with user-defined skills |
| `name` | First line of description, or `Run: <name>` if no description |
| `description` | Full description + invocation instruction + fenced resolved command |
| `tags` | `["cinna-run", "command"]` |
| `examples` | `["/run:<name>"]` |

CLI skills are appended after user-defined skills. The public minimal card never includes CLI skills. Skills reflect the current DB cache — updated automatically after each `CLICommandsService` refresh.

### Discovery Convention

Two discovery options coexist for external callers:

- **Agent card skills (Option A)**: A2A-native clients read the `skills` array (ids prefixed `cinna.run.*`) before sending any message.
- **Convention-based (Option C)**: LLM-driven agents send `/run` (no args) and parse the markdown table response. Zero additional client code needed.

---

## LLM Context Bridging

Command output is invisible to the LLM by default — it does not appear in the chat history sent to the agent. To close this gap, the platform automatically bridges non-LLM command outputs into the next LLM turn.

When an LLM-bound message is about to be processed, the backend collects all eligible command pairs (user invocation + system output) that occurred since the previous LLM response and prepends a `<prior_commands>` XML block to the user message content. This happens before the message reaches the agent environment.

`/run:*` commands have `include_in_llm_context=True` — their stdout/stderr output is included. Meta/infra commands (`/session-recover`, `/session-reset`, `/rebuild-env`, `/webapp`) are excluded. `/files`, `/files-all`, and `/agent-status` are also included.

After inclusion, each system message is marked `forwarded_to_llm_at` so it is not re-included on subsequent turns.

See [non_llm_context_bridging_tech.md](../agent_commands/non_llm_context_bridging_tech.md) for the technical specification.

---

## Security Notes

- The `resolved_command` string is visible to authenticated session owners in the API response, the UI tooltip, and the A2A agent card description. Do not embed credentials in command strings.
- Commands run with the agent's full environment access (inside the Docker container). No LLM mediation happens; execution is direct.
- A2A access to `/run:*` commands is gated at the agent level by the access token. No per-command ACL exists in the current implementation.

---

## Error Handling

| Scenario | Behavior |
|---|---|
| `CLI_COMMANDS.yaml` absent | `cli_commands_error = "file_missing"` persisted; cached list unchanged |
| Environment not running | `cli_commands_error = "adapter_error"` persisted; cached list unchanged |
| Empty file | Valid; `cli_commands_parsed = []` persisted; no error |
| Malformed YAML | `cli_commands_error = "parse_error"` persisted; `CLI_COMMANDS_UPDATED` event with `command_count=0`; parsed list not updated |
| Bad entry (missing name/command, invalid slug) | Entry skipped with WARNING; valid entries still parsed |
| File > 64 KB | Content truncated before parse; parse proceeds on truncated content |
| Unknown command name in `/run:<name>` | Inline error returned without queuing a message; stale cache is refreshed once before giving up |
| Output exceeds 256 KB | Truncation marker appended, command stream interrupted |
| Execution timeout (300 s) | System message finalized with `exec_timed_out=true`, `status="error"` |

---

## Integration Points

- **[Agent Commands](../agent_commands/agent_commands.md)** — `/run:*` is a slash command variant using the same `CommandHandler` framework with `streams=True`
- **[Agent Sessions](../../application/agent_sessions/agent_sessions.md)** — execution flows through `SessionStreamProcessor`'s batch loop
- **[A2A Protocol](../../application/a2a_integration/a2a_protocol/a2a_protocol.md)** — `/run:*` works end-to-end via the existing A2A pipeline; CLI skills appear in the extended agent card
- **[Non-LLM Context Bridging](../agent_commands/non_llm_context_bridging_tech.md)** — `/run:*` output is forwarded to the next LLM turn via `<prior_commands>` block
- **[Slash Command Autocomplete](../agent_commands/slash_command_autocomplete.md)** — `/run:<name>` entries appear dynamically in the popup with tooltips
- **[Agent Status Tracking](../agent_status_tracking/agent_status_tracking.md)** — uses the same post-action refresh pattern and rate-limit infrastructure; both are now classified as `pull_only` entries in the Synced Workspace File Registry
- **[Agent Prompts](../agent_prompts/agent_prompts.md)** — `docs/CLI_COMMANDS.yaml` is one of the five files in the Synced Workspace File Registry (`synced_files.py`); the registry drives the `WORKSPACE_FILES_CHANGED` / post-action / `ENVIRONMENT_ACTIVATED` handler registrations in `backend/app/main.py` for all five files together
- **[Realtime Events](../../application/realtime_events/event_bus_system.md)** — `CLI_COMMANDS_UPDATED` is now wired into `frontend/src/services/eventService.ts` `EventTypes`; `MessageInput.tsx` subscribes and invalidates `["sessionCommands", sessionId]` so the `/run:*` autocomplete popup updates live when the agent writes a new command without a page reload
