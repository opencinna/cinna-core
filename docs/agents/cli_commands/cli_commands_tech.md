# CLI Commands — Technical Reference

## Architecture

The CLI Commands feature is modeled on `AgentStatusService` / `STATUS.md`. The key components span four implementation areas: sync and discovery (plan #1), execution and streaming (plan #2), A2A skill surfacing (plan #3), and LLM context bridging (plan #4).

```
[AgentEnvironment DB row]          [CLICommandsService]
  cli_commands_raw     TEXT    ←── fetch_commands(environment) ───▶ EnvironmentAdapter
  cli_commands_parsed  JSON         parse_commands_file(raw)
  cli_commands_fetched_at TIMESTAMPTZ  get_cached_commands(environment)
  cli_commands_error   VARCHAR(256)  refresh_after_action(environment)
                                     handle_post_action_event(event_data)

[GET /api/v1/sessions/{id}/commands]
  (messages.py: list_session_commands — thin: ownership check + service call)
  → CommandService.list_for_session(db, chat_session)
       applies display rules:
         - hides /run
         - hides /run-list when cli_commands_parsed is empty
         - marks /rebuild-env unavailable when co-tenant is streaming
         - appends dynamic /run:<name> from CLICommandsService.get_cached_commands(environment)

[SlashCommandPopup.tsx]
  → tooltip on dynamic entries via shadcn Tooltip

─── Execution and Streaming ─────────────────────────────────────────────────

[POST /api/v1/sessions/{id}/messages/stream with content="/run:check"]
  Phase 1.5 → RunCommandHandler.execute()
    → lookup in cli_commands_parsed
    → returns CommandResult(routing="command_stream", resolved_command=...)
  Phase 1.5 creates user message (pending, routing="command_stream")
  → initiate_stream() → SessionStreamProcessor._process_inner()
  → collect_pending_batches() partitions by routing
  → stream_command_via_agent_env() (MessageService)
       → AgentEnvConnector.stream_command() → POST /command/stream (agent-env)
       → SSE: tool / tool_result_delta / done events
       → flushed to DB every 2s
       → system message finalized with exec_exit_code / duration / streaming_events

[POST /api/v1/sessions/{id}/messages/interrupt]
  interrupt_stream() → ActiveStreamingManager.request_interrupt()
  → if stream_type="command": forward to AgentEnvConnector.interrupt_command()
  → POST /command/interrupt/{exec_id} (agent-env) → SIGTERM/SIGKILL

[agent-env: POST /command/stream]
  asyncio.create_subprocess_shell()
  stdout/stderr → asyncio.Queue → SSE tool_result_delta events
  done/interrupted/error → SSE done event

[MessageBubble.tsx]
  role="system" + message_metadata.routing="command_stream"
  → terminal-style header, scrollable output area, exit code footer

─── A2A Skill Surfacing ────────────────────────────────────────────────────

[A2AService.build_agent_card(environment)]
  → _build_cli_command_skills(environment)
       reads environment.cli_commands_parsed
       maps each entry → AgentSkill(id="cinna.run.<name>", ...)
  → appended after user-defined skills

─── LLM Context Bridging ────────────────────────────────────────────────────

[SessionStreamProcessor._process_inner()]
  collect_pending_batches()
    └─ MessageService.build_non_llm_prefix(db, session_id)
         → queries eligible command pairs since last agent message
         → formats as <prior_commands> XML with per-block + total caps
         → prefix prepended to first LLM batch's content
  Step 5b: mark_command_messages_as_forwarded(db, included_ids)
```

---

## Service: `CLICommandsService`

**File:** `backend/app/services/agents/cli_commands_service.py`

### Exception Classes

```python
class CLICommandsUnavailableError(Exception):
    """Env not running, file missing, or adapter error."""
    reason: str  # "file_missing" | "adapter_error: <msg>"

class CLICommandsParseError(Exception):
    """yaml.safe_load raised on malformed YAML."""
```

### Dataclass

```python
@dataclass
class ParsedCLICommand:
    name: str           # slug, e.g. "check"
    command: str        # raw shell string
    description: str | None
```

### Public Methods

| Method | Signature | Notes |
|---|---|---|
| `fetch_commands` | `async (environment, db_session=None) -> list[ParsedCLICommand]` | Adapter call + parse + DB persist + rate-limit + event |
| `get_cached_commands` | `(environment) -> list[ParsedCLICommand]` | Cache-only, no adapter call |
| `parse_commands_file` | `static (raw: str) -> list[ParsedCLICommand]` | Pure; raises `CLICommandsParseError` on bad YAML |
| `is_rate_limited` | `(environment_id: UUID) -> bool` | 30 s TTL per env |
| `refresh_after_action` | `async (environment, db_session=None, force=False) -> None` | Best-effort; checks rate limit (skipped when `force=True`); swallows all errors |
| `handle_post_action_event` | `async (event_data: dict) -> None` | Reads `meta.environment_id`; passes `force=True` to `refresh_after_action` when `meta.changed_files` contains `docs/CLI_COMMANDS.yaml` |

### Module-Level Constants

```python
CLI_COMMANDS_FILE_PATH = "docs/CLI_COMMANDS.yaml"
MAX_RAW_BYTES = 64 * 1024
MAX_COMMANDS = 50
MAX_COMMAND_LENGTH = 1024
MAX_DESCRIPTION_LENGTH = 512
FORCE_REFRESH_TTL_SECONDS = 30
NAME_REGEX = re.compile(r'^[a-z][a-z0-9_-]{0,31}$')
```

---

## Model Changes

### `AgentEnvironment` (new columns)

**File:** `backend/app/models/environments/environment.py`

```python
cli_commands_raw: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
cli_commands_parsed: list | None = Field(default=None, sa_column=Column(JSON, nullable=True))
cli_commands_fetched_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True), nullable=True))
cli_commands_error: str | None = Field(default=None, sa_column=Column(sa.String(256), nullable=True))
```

These fields are intentionally NOT exposed in `AgentEnvironmentPublic` — they are internal cache fields used by `CLICommandsService.get_cached_commands()` and execution logic.

### `SessionCommandPublic` (new field)

**File:** `backend/app/models/sessions/session.py`

```python
class SessionCommandPublic(SQLModel):
    name: str
    description: str
    is_available: bool
    resolved_command: str | None = None  # Raw shell command for /run:<name>; None for static commands
```

### `EventType` (new entry)

**File:** `backend/app/models/events/event.py`

```python
CLI_COMMANDS_UPDATED = "cli_commands_updated"
```

---

## Database Migrations

### Migration 1 — CLI commands cache columns

**File:** `backend/app/alembic/versions/c1d2e3f4a5b6_add_cli_commands_cache_to_environment.py`

```
Revision: c1d2e3f4a5b6
Revises: 34322f866173
```

Adds four nullable columns to `agent_environment`:
- `cli_commands_raw TEXT`
- `cli_commands_parsed JSON`
- `cli_commands_fetched_at TIMESTAMP WITH TIME ZONE`
- `cli_commands_error VARCHAR(256)`

### Migration 2 — Bulk-mark existing command messages as forwarded

**File:** `backend/app/alembic/versions/d1e2f3a4b5c6_mark_existing_command_messages_forwarded.py`

Bulk-marks all pre-deploy command system messages with `forwarded_to_llm_at` to prevent them flooding the first LLM turn after deployment. See [non_llm_context_bridging_tech.md](../agent_commands/non_llm_context_bridging_tech.md) for the migration SQL.

---

## Event Registration

**File:** `backend/app/main.py`

```python
from app.services.agents.cli_commands_service import CLICommandsService

event_service.register_handler(
    event_type=EventType.ENVIRONMENT_ACTIVATED,
    handler=CLICommandsService.handle_post_action_event,
)
for _event_type in (
    EventType.STREAM_COMPLETED,
    EventType.STREAM_ERROR,
    EventType.CRON_COMPLETED_OK,
    EventType.CRON_TRIGGER_SESSION,
    EventType.CRON_ERROR,
    EventType.WORKSPACE_FILES_CHANGED,
):
    event_service.register_handler(
        event_type=_event_type,
        handler=CLICommandsService.handle_post_action_event,
    )
```

`handle_post_action_event` reads `meta.changed_files` (populated by `WORKSPACE_FILES_CHANGED` from the env-core file watcher) and passes `force=True` to `refresh_after_action` when `docs/CLI_COMMANDS.yaml` is in the list. This bypasses the 30 s rate limit for that fetch — direct evidence that the cache is stale outweighs the de-dup heuristic.

---

## API Endpoint

**File:** `backend/app/api/routes/messages.py`

`list_session_commands` (GET `/sessions/{session_id}/commands`) is a thin route that does the HTTP-layer work only — session lookup, ownership check, and delegation to `CommandService.list_for_session`.

**File:** `backend/app/services/agents/command_service.py`

`CommandService.list_for_session(db, chat_session)` owns the display-rule logic:

| Rule | Behavior |
|---|---|
| `/run` | Always hidden from the popup (still invokable as a message) |
| `/run-list` | Hidden when `environment.cli_commands_parsed` is empty/null; shown otherwise |
| `/rebuild-env` | `is_available=false` if any session on the same environment is actively streaming (via `ActiveStreamingManager.is_any_session_streaming`) |
| Other static handlers | Always `is_available=true` |
| Dynamic `/run:<name>` | Appended after static handlers; always `is_available=true`; `resolved_command` populated (powers the tooltip) |

Static commands retain `resolved_command=None`. Dynamic entries have `resolved_command=cmd.command`.

---

## Rebuild Integration

**File:** `backend/app/services/agents/commands/rebuild_env_command.py`

After `EnvironmentService.rebuild_environment()` completes successfully, a best-effort CLI commands refresh is triggered:

```python
from app.services.agents.cli_commands_service import CLICommandsService

with create_db_session() as db:
    env = db.get(AgentEnvironment, env_id)
    if env:
        await CLICommandsService.refresh_after_action(env, db_session=db)
```

---

## Frontend Changes

**File:** `frontend/src/components/Chat/SlashCommandPopup.tsx`

- Imports `Tooltip`, `TooltipContent`, `TooltipProvider`, `TooltipTrigger` from `@/components/ui/tooltip`
- Wraps the popup root with `<TooltipProvider>`
- Dynamic command rows (where `resolved_command != null`) are wrapped with `<Tooltip>` showing the shell command in a `<code>` element truncated at 120 characters
- Accessibility: dynamic rows gain `aria-describedby="cmd-tooltip-{name}"` pointing to a hidden `<span>` with the resolved command
- Unavailable dynamic commands do not show the tooltip

---

## Dependency

**File:** `backend/pyproject.toml`

`pyyaml>=6.0.1` added to project dependencies. The service always uses `yaml.safe_load` (never bare `yaml.load`) for security.

---

## Env-Templates

Starter `CLI_COMMANDS.yaml` files (with `commands: []`) were added to:
- `backend/app/env-templates/general-env/app/workspace/docs/CLI_COMMANDS.yaml`
- `backend/app/env-templates/general-assistant-env/app/workspace/docs/CLI_COMMANDS.yaml`
- `backend/app/env-templates/python-env-advanced/app/workspace/docs/CLI_COMMANDS.yaml`

The `app_core_base` template includes an "Exposed CLI Commands" section in `core/prompts/COMPLEX_AGENT_DESIGN.md` covering the file format, security hygiene, A2A skill surfacing, and when to maintain the file.

---

## Execution and Streaming — `RunCommandHandler` / `RunListCommandHandler`

**File:** `backend/app/services/agents/commands/run_command.py`

Two `CommandHandler` subclasses share a helper:

- `_list_cli_commands(context)` — module-level async helper that reads `AgentEnvironment.cli_commands_parsed` and returns the markdown-table `CommandResult`. Shared to avoid one handler reaching into the other's privates.
- `RunCommandHandler` — name `/run`, `streams = True`, `include_in_llm_context = True`. Both list mode (no args) and exec mode (`/run:<name>` / `/run <name>`). Stays registered so existing manual typing and A2A callers keep working; hidden from the autocomplete popup.
- `RunListCommandHandler` — name `/run-list`, `streams = False`. Delegates to `_list_cli_commands`. Rejects non-empty args with an inline error. Surfaced in the popup only when the agent has CLI commands configured.

**Exec mode** (`/run:<name>` or `/run <name>`): validates the name with `^[a-zA-Z0-9_-]{1,64}$`, looks up the command in the cache (case-insensitive), blocks guest sessions, and returns `CommandResult(routing="command_stream", resolved_command=..., exec_command_short_name=...)`.

**Stale cache handling**: if the name is not found but the cache is non-empty, `CLICommandsService.fetch_commands` is called once as a refresh before returning "not found".

### `CommandResult` and `CommandHandler` extensions

**File:** `backend/app/services/agents/command_service.py`

`CommandResult` gains three new fields: `routing: str | None`, `resolved_command: str | None`, `exec_command_short_name: str | None`.

`CommandHandler` gains `streams: bool = False` (class attribute). `RunCommandHandler` sets `streams = True`.

`CommandService.is_command` and `parse_command` extended to recognize the colon form (`/run:name`).

### Phase 1.5 in `session_service.py`

When `result.routing == "command_stream"`:
- Creates a user message with `sent_to_agent_status="pending"` and `message_metadata` containing `routing`, `command_name`, `resolved_command`, `exec_command_short_name`.
- Calls `initiate_stream()` (same as normal LLM messages).
- Returns `{"action": "queued", ...}`.

### `collect_pending_batches`

**File:** `backend/app/services/sessions/message_service.py`

New static method that queries all pending messages and partitions them into contiguous same-routing batches. Returns:
- `{"routing": None, "messages": [...], "content": "..."}` — LLM batch with concatenated content
- `{"routing": "command_stream", "messages": [...], "resolved_command": "...", "command_name": "..."}` — command batch

Also calls `build_non_llm_prefix` and attaches `_included_command_ids` to the first batch.

### `SessionStreamProcessor._process_inner` batch loop

**File:** `backend/app/services/sessions/stream_processor.py`

Replaced the single `stream_message_with_events` call with a batch loop that dispatches LLM batches to `stream_message_with_events` and command batches to `stream_command_via_agent_env`. Recovery context and webapp context injection only apply to LLM batches. `on_complete` is called once at the end of all batches.

Step 5b (after `mark_messages_as_sent`, before streaming): calls `MessageService.mark_command_messages_as_forwarded(db, included_command_ids)` if any command IDs were collected by `build_non_llm_prefix`.

### `stream_command_via_agent_env`

**File:** `backend/app/services/sessions/message_service.py`

1. Resolves environment URL and auth headers.
2. Creates a system message with `streaming_in_progress=True`, `command=True`, `routing="command_stream"`, `synthesized=True`.
3. Registers with `ActiveStreamingManager` (stream_type="command", exec_id=UUID).
4. Emits a `tool` event (bash, synthesized=True) as the invocation marker.
5. Streams SSE from agent-env via `AgentEnvConnector.stream_command()`.
6. Flushes events to DB every 2 seconds.
7. Enforces `RUN_COMMAND_MAX_OUTPUT_BYTES` (256 KB).
8. Finalizes system message with `exec_exit_code`, `exec_duration_seconds`, `exec_timed_out`, `exec_interrupted`, `exec_truncated`, and formatted content.
9. Emits a `system` done event (command_done=True) at completion.
10. Emits one of `STREAM_COMPLETED` / `STREAM_INTERRUPTED` / `STREAM_ERROR` on the event bus (via `_emit_activity_event`), matching the LLM streaming path. Subscribers: `session_service.handle_stream_completed` (clears `interaction_status`, emits `session_interaction_status_changed` WS event to the user room), `activity_service.handle_stream_completed` (activity log), `input_task_service.handle_stream_completed` (task status sync), `CLICommandsService.handle_post_action_event` (cache refresh), `AgentStatusService.handle_post_action_event` (cache refresh), `environment_service.handle_stream_completed_event`.
11. Unregisters the stream from `ActiveStreamingManager` (emit-before-unregister mirrors the LLM path ordering).

**`message.status`** is set to:
- `""` — normal completion, including non-zero exit codes
- `"error"` — timed out or infrastructure failure
- `"user_interrupted"` — user sent interrupt

**Event routing**:
- `was_interrupted` → `STREAM_INTERRUPTED`
- `was_error` or `exec_timed_out` → `STREAM_ERROR` (with `error_type` = `CommandTimedOut` / `CommandError`)
- otherwise → `STREAM_COMPLETED` with `was_interrupted=False`

### `AgentEnvConnector` additions

**File:** `backend/app/services/environments/agent_env_connector.py`

- `stream_command(base_url, auth_headers, exec_id, resolved_command, timeout, max_output_bytes)` — SSE iterator for `/command/stream`
- `interrupt_command(base_url, auth_headers, exec_id)` — fire-and-forget POST to `/command/interrupt/{exec_id}`

### `ActiveStreamingManager` additions

**File:** `backend/app/services/sessions/active_streaming_manager.py`

- `ActiveStream` gains `stream_type: str = "llm"` and `exec_id: Optional[str] = None` fields
- New `update_stream_type(session_id, stream_type, exec_id)` method
- `request_interrupt` returns `stream_type` and `exec_id` in result dict; command streams are identified by `stream_type == "command"` and interrupt is propagated immediately without requiring `external_session_id`

### Configuration

**File:** `backend/app/core/config.py`

```python
RUN_COMMAND_TIMEOUT_SECONDS: int = 300
RUN_COMMAND_MAX_OUTPUT_BYTES: int = 262144  # 256 KB
```

### Agent-env Endpoints

**File:** `backend/app/env-templates/app_core_base/core/server/routes.py`

- `POST /command/stream` — `asyncio.create_subprocess_shell`, concurrent stdout/stderr reading via `asyncio.Queue`, SSE emission of `tool` / `tool_result_delta` / `done` events, timeout via `asyncio.wait_for`, byte cap enforcement, exec_id tracking in module-level dict `_active_execs`
- `POST /command/interrupt/{exec_id}` — SIGTERM then SIGKILL with 2 s grace period

Request model:

```python
class CommandStreamRequest(BaseModel):
    command: str
    exec_id: str
    timeout: int = 300
    max_output_bytes: int = 262144
```

### Frontend

**`useSessionStreaming.ts`**: `tool_result_delta` added to `StreamEventType`; new metadata fields: `synthesized`, `stream`, `exit_code`, `command_done`.

**`StreamEventRenderer.tsx`**: `tool_result_delta` case renders stdout (default) and stderr (amber) in monospace `pre` blocks.

**`MessageBubble.tsx`**: command response system messages (`message.role === "system"` AND `message_metadata.command === true`) skip the generic centered-notification early-return and reach the command-rendering branches:
- `routing === "command_stream"` (i.e. `/run:<name>`): terminal-style block.
  - Header: command name, resolved command (truncated at 80 chars), copy button
  - While streaming: dark background, monospace `<pre>`, stdout default / stderr amber, blinking cursor
  - When complete: output rendered via `MarkdownRenderer` (so scripts emitting markdown tables, lists, headings render properly); empty output shows an italic placeholder
  - Footer: exit code badge (green check / red X), `Timed out` / `Interrupted` / `[truncated]` labels
- Other command responses (`/run-list`, `/files`, `/agent-status`, etc.): `message.content` rendered via `MarkdownRenderer` with the shared `prose` classes (GFM tables, lists, fenced code blocks supported).

---

## A2A Skill Surfacing

### `A2AService._build_cli_command_skills`

**File:** `backend/app/services/a2a/a2a_service.py`

```python
@staticmethod
def _build_cli_command_skills(environment: AgentEnvironment | None) -> list[AgentSkill]:
    """Build AgentSkill list from environment's cached CLI commands."""
    if not environment or not environment.cli_commands_parsed:
        return []
    skills = []
    for cmd in environment.cli_commands_parsed:
        try:
            skills.append(A2AService._build_single_cli_skill(cmd))
        except Exception as e:
            logger.warning(f"Failed to build CLI skill for command {cmd}: {e}")
    return skills
```

### Skill Field Mapping

For a command `{name: "check", command: "uv run /app/workspace/scripts/check-data.py", description: "Monthly data check"}`:

| `AgentSkill` field | Value |
|---|---|
| `id` | `cinna.run.check` |
| `name` | `Monthly data check` (first line of description) or `Run: check` if no description |
| `description` | Full description + `\n\nInvoke by sending message: /run:check` + fenced command block |
| `tags` | `["cinna-run", "command"]` |
| `examples` | `["/run:check"]` |

### Integration in `build_agent_card`

```python
# existing: build user-defined skills from agent.a2a_config
skills = [...]  # user-defined

# CLI-generated skills appended after user-defined skills
skills.extend(A2AService._build_cli_command_skills(environment))
```

`build_public_agent_card()` is unchanged — it always returns `skills=[]`.

### Protocol Adapter Compatibility

`A2AV1Adapter.transform_agent_card_outbound()` passes `skills` through unchanged. No v1.0 / v0.3 differences apply to CLI skills.

### A2A Event Flow for `/run:<name>`

Command-stream events emitted by the agent-env's `/command/stream` are handled by `A2AEventMapper.map_stream_event()` without modification:

| Agent-env event | `A2AEventMapper` output |
|---|---|
| `tool` (stdout/stderr chunk) | `status-update (working, final=false, cinna.content_kind="tool", cinna.tool_name="bash")` |
| `done` (not interrupted) | `status-update (completed, final=true)` |
| `done` (interrupted) | `status-update (canceled, final=true)` |
| `error` | `status-update (failed, final=true)` |

The `synthesized: true` flag on `tool` events is informational only — `A2AEventMapper` does not inspect it.

### `TaskState` for Non-Zero Exit Codes

A shell command returning exit code 1 maps to `TaskState.completed`, not `TaskState.failed`. Only infrastructure failures (connection refused, unhandled exception) produce `TaskState.failed`.

---

## LLM Context Bridging — Integration Points

For the full specification see [non_llm_context_bridging_tech.md](../agent_commands/non_llm_context_bridging_tech.md).

Key touch points in this feature:

- `RunCommandHandler.include_in_llm_context = True` — `/run:*` output is eligible for the `<prior_commands>` block.
- System messages produced by `stream_command_via_agent_env` carry `message_metadata["command"] = True` and `message_metadata["command_name"] = "/run:<name>"` — these are the keys used by `build_non_llm_prefix` to identify eligible messages.
- `streaming_in_progress` flag on the system message: `build_non_llm_prefix` skips any system message where this is `True` (command still executing). The message is picked up on the next LLM turn once the stream finalizes.
- The compiled `message.content` (invocation header + accumulated stdout/stderr + exit code line) is used as the `<output>` element — not the raw `streaming_events`.

---

## Testing

### Unit Tests

**File:** `backend/tests/unit/test_cli_commands_service.py`

Covers `parse_commands_file` (valid, empty, malformed, missing fields, duplicate names, slug validation, command length, description truncation, 50-command limit, unknown keys), rate-limit helpers, `get_cached_commands`, `refresh_after_action`, and `handle_post_action_event`.

No HTTP, no database.

### Integration Tests — Discovery

**File:** `backend/tests/api/agents/agents_cli_commands_test.py`

Covers the API-observable behavior:
- No dynamic entries when cache is empty
- Dynamic entries appear when cache is populated (via EnvironmentTestAdapter.workspace_files)
- Static entries have `resolved_command=None`
- Dynamic entries are `is_available=False` when environment is not `running`

### Integration Tests — Execution

**File:** `backend/tests/api/agents/agents_run_command_test.py`

Tests:
- `/run` list mode returns markdown table
- `/run` with empty cache returns inline error
- `/run:<name>` queues message and streams via stub; system message has correct metadata
- `/run <name>` (space form) behaves identically
- Unknown command returns inline error without queuing
- Invalid name format returns inline error
- Non-zero exit code does not set `message.status="error"`
- Tool event in `streaming_events` has `synthesized=True`
