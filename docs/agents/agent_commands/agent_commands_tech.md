# Agent Commands — Technical Details

## File Locations

### Backend — Framework
- `backend/app/services/agents/command_service.py` — Core framework: `CommandContext`, `CommandResult`, `CommandHandler` (ABC), `CommandService` (static registry); includes `include_in_llm_context` and `requires_running_environment` class attributes and `CommandService.get_handler()` method
- `backend/app/services/agents/commands/__init__.py` — Handler registration (imported by session service to ensure handlers are loaded)
- `backend/app/services/agents/commands/files_command.py` — `/files` and `/files-all` handlers
- `backend/app/services/agents/commands/session_recover_command.py` — `/session-recover` handler
- `backend/app/services/agents/commands/session_reset_command.py` — `/session-reset` handler
- `backend/app/services/agents/commands/webapp_command.py` — `/webapp` handler
- `backend/app/services/agents/commands/rebuild_env_command.py` — `/rebuild-env` handler
- `backend/app/services/agents/commands/run_command.py` — `/run` and `/run:<name>` handlers; streaming path with `streams=True`

### Backend — Integration Points
- `backend/app/services/sessions/session_service.py` — `send_session_message()` — command detection at Phase 1.5, between session validation and file handling; takes optional `backend_base_url` param for A2A callers
- `backend/app/api/routes/messages.py` — `send_message_stream()` — handles `"command_executed"` action result
- `backend/app/services/a2a/a2a_request_handler.py` — `handle_message_send()` and `handle_message_stream()` — handle `"command_executed"` action; `handle_message_stream()` yields a `notice` SSE event via `A2AEventMapper.create_notice_event` when `result["env_wake_initiated"]` is `True` (the service layer owns the wake-up decision; the handler reads the result key rather than snapshotting `environment.status` itself)
- `backend/app/api/routes/a2a.py` — `handle_jsonrpc()` — extracts `backend_base_url` from request (handles `X-Forwarded-Proto` for reverse proxies)

### Backend — Workspace View Tokens
- `backend/app/services/environments/agent_workspace_token_service.py` — `AgentWorkspaceTokenService`: `create_workspace_view_token()`, `verify_workspace_view_token()`
- `backend/app/api/routes/shared_workspace.py` — `GET /api/v1/shared/workspace/{env_id}/view/{path}` — public file view endpoint (no `CurrentUser` dependency)
- `backend/app/api/main.py` — router registration for `shared_workspace` under prefix `/shared/workspace` with tag `shared-workspace`

### Frontend
- No frontend changes — `MarkdownRenderer` already renders standard markdown links as clickable links

## Database Schema

No new tables. Commands use existing session and message tables:
- Session metadata fields: `external_session_id`, `sdk_type`, `last_sdk_message_id`, `recovery_pending`, `status` — modified by session recovery/reset commands
- Message field: `sent_to_agent_status` — reset to `"pending"` by recovery command for auto-resend
- Message metadata on command system messages (`role="system"`):
  - `{"command": true, "command_name": "/name"}` — identifies command responses; read by `build_non_llm_prefix`
  - `{"forwarded_to_llm_at": "<iso_ts>"}` — written by `mark_command_messages_as_forwarded` after inclusion in a `<prior_commands>` block; absence means eligible for inclusion
  - `/run:*` specific: `{"routing": "command_stream", "resolved_command": "...", "streaming_in_progress": bool, "exec_exit_code": int, ...}` — set by `stream_command_via_agent_env`

## API Endpoints

- `GET /api/v1/shared/workspace/{env_id}/view/{path:path}?token={workspace_view_token}` — Public file content endpoint (`shared_workspace.py`)
  - No auth required; validates workspace view token and checks `env_id` match
  - Streams file content as `text/plain; charset=utf-8` via `adapter.download_workspace_item(path)`

## Services & Key Methods

### CommandService (`command_service.py`)
- `CommandService.register(handler)` — Registers a handler in the static registry
- `CommandService.is_command(content)` — Returns bool; fast check with no overhead for non-commands; recognizes both `/run:name` and `/run name` forms
- `CommandService.parse_command(content)` — Returns `(name, args)` tuple
- `CommandService.execute(content, context)` — Dispatches to the matching handler; returns `CommandResult`
- `CommandService.get_handler(name)` — Returns the registered handler for a given command name, or `None` if not registered; used by `build_non_llm_prefix` to look up `include_in_llm_context` and by `_ensure_env_for_command_handler` to check `requires_running_environment`
- `CommandService.requires_running_environment(content)` — One-liner helper: calls `parse_command` + `get_handler` + reads the `requires_running_environment` attribute. Returns `False` for unrecognized commands. Used internally by `SessionService._ensure_env_for_command_handler` and by the A2A request handler.

**`CommandHandler` class attributes** (set on subclasses):
- `include_in_llm_context: bool = True` — controls whether command output is included in the `<prior_commands>` block on the next LLM turn
- `streams: bool = False` — when `True`, command queues as a pending message and runs through `SessionStreamProcessor` (e.g. `/run:<name>`)
- `requires_running_environment: bool = False` — when `True`, `send_session_message` Phase 1.5 auto-wakes the environment via `ensure_environment_ready_for_streaming(timeout_seconds=120)` before the handler executes. Set to `True` on `FilesCommandHandler`, `FilesAllCommandHandler`, `AgentStatusCommandHandler`, `SessionRecoverCommandHandler`, and `SessionResetCommandHandler`. See [agent_commands.md](agent_commands.md) Business Rules for the rationale per handler.

### AgentWorkspaceTokenService (`agent_workspace_token_service.py`)
- `create_workspace_view_token(env_id, agent_id)` — Creates a 1-hour HS256 JWT with `type="workspace_view"`, `env_id`, `agent_id`, `exp`
- `verify_workspace_view_token(token)` — Decodes and validates; returns payload dict or `None`; no exceptions exposed

### Session Service Integration (`session_service.py`)
- `send_session_message(..., backend_base_url)` — Phase 1.5 command detection; builds `CommandContext`, delegates environment wake-up decision to `_ensure_env_for_command_handler`, then dispatches to the handler. On wake-up failure the method returns `action="error"` with a friendly message and creates no DB rows. On success, creates user message + system response message (`role="system"`), emits WebSocket events, auto-generates session title for new sessions. Result dict carries `env_wake_initiated: True` when a wake-up was actually triggered; the key is omitted when no wake-up was needed. This key is present on `command_executed`, `queued`, and wake-up `error` short-circuit branches.
- `SessionService._ensure_env_for_command_handler(*, content, session_id, command_env_id, environment_status, get_fresh_db_session)` — Private helper extracted from the former inline Phase 1.5 gate. Calls `CommandService.requires_running_environment(content)`, and when `True` and the env is not `"running"`, calls `ensure_environment_ready_for_streaming(timeout_seconds=120)`. Returns `(env_was_woken: bool, error_short_circuit_or_None)`. Reuses the `environment_status` already resolved in Phase 1 — no extra DB round-trip.

## Frontend Components

None — command responses are markdown strings rendered by the existing `MarkdownRenderer` component. File links use standard markdown link syntax already handled.

## Configuration

- `settings.SECRET_KEY` — Used to sign workspace view tokens (HS256)
- `settings.FRONTEND_HOST` — Used in UI-context link generation for file links
- `settings.RUN_COMMAND_TIMEOUT_SECONDS` — Execution timeout for `/run:<name>` (default 300 s)
- `settings.RUN_COMMAND_MAX_OUTPUT_BYTES` — Output cap for `/run:<name>` (default 256 KB)

## Security

- **Workspace view tokens** — 1-hour HS256 JWTs; bound to a specific `env_id`; self-contained (no DB lookup); expired/invalid tokens return `None`
- **Public file endpoint** — No `CurrentUser` dependency; token validated before any file access; `env_id` in URL must match token's `env_id`
- **Command messages** — Set `sent_to_agent_status="sent"` immediately to prevent LLM pipeline pickup
- **Access control** — Commands execute within the existing session authorization context; `send_session_message()` already validates session ownership before Phase 1.5
