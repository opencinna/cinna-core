# Agent Commands — Technical Details

## File Locations

### Backend — Framework
- `backend/app/services/command_service.py` — Core framework: `CommandContext`, `CommandResult`, `CommandHandler` (ABC), `CommandService` (static registry)
- `backend/app/services/commands/__init__.py` — Handler registration (imported by session service to ensure handlers are loaded)
- `backend/app/services/commands/files_command.py` — `/files` and `/files-all` handlers
- `backend/app/services/commands/session_recover_command.py` — `/session-recover` handler
- `backend/app/services/commands/session_reset_command.py` — `/session-reset` handler
- `backend/app/services/commands/webapp_command.py` — `/webapp` handler
- `backend/app/services/commands/rebuild_env_command.py` — `/rebuild-env` handler

### Backend — Integration Points
- `backend/app/services/session_service.py` — `send_session_message()` — command detection at Phase 1.5, between session validation and file handling; takes optional `backend_base_url` param for A2A callers
- `backend/app/api/routes/messages.py` — `send_message_stream()` — handles `"command_executed"` action result
- `backend/app/services/a2a_request_handler.py` — `handle_message_send()` and `handle_message_stream()` — handle `"command_executed"` action
- `backend/app/api/routes/a2a.py` — `handle_jsonrpc()` — extracts `backend_base_url` from request (handles `X-Forwarded-Proto` for reverse proxies)

### Backend — Workspace View Tokens
- `backend/app/services/agent_workspace_token_service.py` — `AgentWorkspaceTokenService`: `create_workspace_view_token()`, `verify_workspace_view_token()`
- `backend/app/api/routes/shared_workspace.py` — `GET /api/v1/shared/workspace/{env_id}/view/{path}` — public file view endpoint (no `CurrentUser` dependency)
- `backend/app/api/main.py` — router registration for `shared_workspace` under prefix `/shared/workspace` with tag `shared-workspace`

### Frontend
- No frontend changes — `MarkdownRenderer` already renders standard markdown links as clickable links

## Database Schema

No new database tables. Commands use existing session and message tables:
- Session metadata fields: `external_session_id`, `sdk_type`, `last_sdk_message_id`, `recovery_pending`, `status` — modified by session recovery/reset commands
- Message field: `sent_to_agent_status` — reset to `"pending"` by recovery command for auto-resend
- Message metadata: `{"command": true, "command_name": "/name"}` — JSON field on agent message records identifying command responses

## API Endpoints

- `GET /api/v1/shared/workspace/{env_id}/view/{path:path}?token={workspace_view_token}` — Public file content endpoint (`shared_workspace.py`)
  - No auth required; validates workspace view token and checks `env_id` match
  - Streams file content as `text/plain; charset=utf-8` via `adapter.download_workspace_item(path)`

## Services & Key Methods

### CommandService (`command_service.py`)
- `CommandService.register(handler)` — Registers a handler in the static registry
- `CommandService.is_command(content)` — Returns bool; fast check with no overhead for non-commands
- `CommandService.parse_command(content)` — Returns `(name, args)` tuple
- `CommandService.execute(content, context)` — Dispatches to the matching handler; returns `CommandResult`

### AgentWorkspaceTokenService (`agent_workspace_token_service.py`)
- `create_workspace_view_token(env_id, agent_id)` — Creates a 1-hour HS256 JWT with `type="workspace_view"`, `env_id`, `agent_id`, `exp`
- `verify_workspace_view_token(token)` — Decodes and validates; returns payload dict or `None`; no exceptions exposed

### Session Service Integration (`session_service.py`)
- `send_session_message(..., backend_base_url)` — Phase 1.5 command detection; builds `CommandContext`, creates messages, emits WebSocket events, auto-generates session title for new sessions

## Frontend Components

None — command responses are markdown strings rendered by the existing `MarkdownRenderer` component. File links use standard markdown link syntax already handled.

## Configuration

- `settings.SECRET_KEY` — Used to sign workspace view tokens (HS256)
- `settings.FRONTEND_HOST` — Used in UI-context link generation for file links

## Security

- **Workspace view tokens** — 1-hour HS256 JWTs; bound to a specific `env_id`; self-contained (no DB lookup); expired/invalid tokens return `None`
- **Public file endpoint** — No `CurrentUser` dependency; token validated before any file access; `env_id` in URL must match token's `env_id`
- **Command messages** — Set `sent_to_agent_status="sent"` immediately to prevent LLM pipeline pickup
- **Access control** — Commands execute within the existing session authorization context; `send_session_message()` already validates session ownership before Phase 1.5
