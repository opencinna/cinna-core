# Agent Session Commands

## Overview

Agent session commands are quick, deterministic slash commands (e.g., `/files`) that users can invoke during agent sessions. Unlike regular messages, commands are executed locally on the backend without making an LLM call, providing instant responses.

Commands work across both connection types:
- **UI users**: Responses appear in real-time via WebSocket, with links pointing to frontend routes
- **A2A clients**: Responses are returned as completed A2A tasks, with links using short-lived tokens for browser access

## Architecture

### Request Flow

```
User types "/files"
    │
    ▼
SessionService.send_session_message()  ← common entry point (UI + A2A)
    │
    ├─ CommandService.is_command(content)?
    │   YES → CommandService.execute(content, context)
    │         │
    │         ├─ Creates user message (sent_to_agent_status="sent")
    │         ├─ Calls handler.execute() (e.g., FilesCommandHandler)
    │         ├─ Creates agent message with markdown response
    │         ├─ Emits WebSocket events (for UI real-time update)
    │         └─ Returns {"action": "command_executed", ...}
    │
    │   NO → Normal LLM flow (unchanged)
    │
    ▼
Callers handle "command_executed" action:
  - messages.py: Returns HTTP response (frontend already received via WS)
  - a2a_request_handler.py: Returns A2A Task/SSE with completed status
```

### Key Design Decisions

1. **No LLM involvement**: Commands bypass the entire streaming/LLM pipeline. Messages are created with `sent_to_agent_status="sent"` to prevent them from being picked up by the streaming system.

2. **Single integration point**: Command detection is inserted in `send_session_message()` between Phase 1 (session validation) and Phase 2 (file handling), so all callers (UI, A2A send, A2A stream) benefit automatically.

3. **Context-aware links**: The same command produces different link formats depending on whether the caller is a UI user or an A2A client, using `access_token_id` presence as the discriminator.

4. **Static handler registry**: Commands are registered at import time via `CommandService.register()`, making the framework zero-config for callers.

## Command Framework

### Core Components

**File:** `backend/app/services/command_service.py`

| Class | Purpose |
|-------|---------|
| `CommandContext` | Dataclass with session/environment/user IDs, `access_token_id`, `frontend_host`, `backend_base_url` |
| `CommandResult` | Dataclass with `content` (markdown string) and `is_error` flag |
| `CommandHandler` | ABC with `name`, `description` properties and `execute(context, args)` method |
| `CommandService` | Static registry with `register()`, `is_command()`, `parse_command()`, `execute()` |

### Handler Registration

**File:** `backend/app/services/commands/__init__.py`

All handlers are imported and registered here. The module is imported in `session_service.py` before command detection to ensure handlers are available:

```python
import app.services.commands  # ensures handlers registered
```

### Adding a New Command

1. Create a handler file in `backend/app/services/commands/`:

```python
from app.services.command_service import CommandHandler, CommandContext, CommandResult

class MyCommandHandler(CommandHandler):
    @property
    def name(self) -> str:
        return "/mycommand"

    @property
    def description(self) -> str:
        return "Short description"

    async def execute(self, context: CommandContext, args: str) -> CommandResult:
        # args contains everything after the command name
        return CommandResult(content="Response markdown")
```

2. Register in `backend/app/services/commands/__init__.py`:

```python
from app.services.commands.my_command import MyCommandHandler
CommandService.register(MyCommandHandler())
```

No changes needed to routes, session service, or A2A handler.

## `/files` and `/files-all` Commands

### Purpose

List workspace files with clickable links. Two variants:

| Command | Sections shown | Use case |
|---------|---------------|----------|
| `/files` | `files` only | User-facing data files (reports, CSVs, exports) |
| `/files-all` | `files`, `scripts`, `logs`, `docs`, `uploads` | Full workspace overview |

Both commands share the same execution logic via `_execute_files_listing()`, differing only in which sections are included.

**File:** `backend/app/services/commands/files_command.py`

### Execution Flow

1. Verify environment is running (returns error if not)
2. Get workspace tree via `adapter.get_workspace_tree()` (reuses existing `DockerAdapter` method)
3. If A2A context (`access_token_id` is present): generate a workspace view token
4. Recursively collect files from the requested sections only
5. Build markdown with `[filename](link)` entries and human-readable sizes
6. Return `CommandResult` with the markdown content

### Link Generation

**UI context** (`access_token_id` is None):
```
{FRONTEND_HOST}/environment/{envId}/file?path={encoded_path}
```
Uses existing frontend FileViewer route. User's browser session handles authentication.

**A2A context** (`access_token_id` is present):
```
{backend_base_url}/api/v1/shared/workspace/{env_id}/view/{path}?token={workspace_view_token}
```
Uses the public shared workspace endpoint. One token is generated per command invocation and reused across all links in the response.

### Example Output

`/files`:
```markdown
**Files** (3)
- [data.csv](/environment/abc-123/file?path=files%2Fdata.csv) (45.2 KB)
- [report.xlsx](/environment/abc-123/file?path=files%2Freport.xlsx) (1.2 MB)
- [config.json](/environment/abc-123/file?path=files%2Fconfig.json) (512 B)
```

`/files-all`:
```markdown
**Files** (3)
- [data.csv](/environment/abc-123/file?path=files%2Fdata.csv) (45.2 KB)
- [report.xlsx](/environment/abc-123/file?path=files%2Freport.xlsx) (1.2 MB)
- [config.json](/environment/abc-123/file?path=files%2Fconfig.json) (512 B)

**Scripts** (1)
- [process_data.py](/environment/abc-123/file?path=scripts%2Fprocess_data.py) (3.4 KB)

**Logs** (2)
- [session_20260222.log](/environment/abc-123/file?path=logs%2Fsession_20260222.log) (12.8 KB)
- [errors.log](/environment/abc-123/file?path=logs%2Ferrors.log) (1.1 KB)
```

## Agent Workspace View Tokens

### Purpose

Short-lived JWTs that allow A2A clients to open agent workspace file links in a browser without regular user authentication.

**File:** `backend/app/services/agent_workspace_token_service.py`

### Token Structure

```json
{
  "type": "workspace_view",
  "env_id": "uuid-string",
  "agent_id": "uuid-string",
  "exp": "timestamp (now + 1 hour)"
}
```

- Signed with `settings.SECRET_KEY` using HS256 algorithm
- Self-contained: no database lookup needed for validation
- One token per `/files` command invocation, shared across all file links

### Methods

| Method | Purpose |
|--------|---------|
| `create_workspace_view_token(env_id, agent_id)` | Creates a 1-hour JWT |
| `verify_workspace_view_token(token)` | Decodes and validates, returns payload or None |

### Security Considerations

- Tokens expire after 1 hour (sufficient for browsing file links)
- Token is bound to a specific `env_id` — cannot be used to access other environments
- Expired or invalid tokens return `None` (no exceptions exposed)
- The public endpoint verifies `env_id` in URL matches the token's `env_id`

## Public File Endpoint

### Purpose

Serves workspace file content without requiring user authentication, using workspace view tokens instead.

**File:** `backend/app/api/routes/shared_workspace.py`

### Endpoint

```
GET /api/v1/shared/workspace/{env_id}/view/{path:path}?token={workspace_view_token}
```

- **No `CurrentUser` dependency** — public endpoint
- Validates workspace view token via `AgentWorkspaceTokenService.verify_workspace_view_token()`
- Verifies `env_id` in URL matches token's `env_id`
- Checks environment is running
- Streams file content via `adapter.download_workspace_item(path)`
- Returns `text/plain; charset=utf-8`

### Registration

Registered in `backend/app/api/main.py` with prefix `/shared/workspace` under the `shared-workspace` tag.

## Integration with Session Service

### Modified Method

**File:** `backend/app/services/session_service.py` — `send_session_message()`

**New parameter:** `backend_base_url: str | None = None` — passed by A2A callers for generating public file links.

**Insertion point:** Phase 1.5, after session validation (Phase 1) and before file handling (Phase 2).

### Flow

1. Import `CommandService` and `app.services.commands` (ensures handlers registered)
2. Check `CommandService.is_command(content)` — returns False for non-commands (no overhead)
3. If command detected:
   - Build `CommandContext` with session/environment IDs, `access_token_id`, host URLs
   - Create user message with `sent_to_agent_status="sent"`
   - Execute command via `CommandService.execute()`
   - Create agent message with result markdown and `{"command": True, "command_name": "/files"}` metadata
   - Emit WebSocket events (`assistant` + `stream_completed`) for real-time UI update
   - Auto-generate session title for new sessions
   - Return `{"action": "command_executed", "message": ..., "session_id": ..., "pending_count": 0}`

### Message Metadata

Command response messages are stored with metadata:
```json
{
  "command": true,
  "command_name": "/files"
}
```

This allows the frontend and other consumers to identify command responses.

## Caller Handling

### UI Route (`messages.py`)

**File:** `backend/app/api/routes/messages.py` — `send_message_stream()`

When `result["action"] == "command_executed"`:
- Returns `{"status": "ok", "command_executed": True, ...}` immediately
- Frontend already received the response via WebSocket events

### A2A `message/send` (`a2a_request_handler.py`)

**File:** `backend/app/services/a2a_request_handler.py` — `handle_message_send()`

When `result["action"] == "command_executed"`:
- Returns a `Task` object with `TaskState.completed` status
- The task status message contains the command response text
- Skips the entire polling/streaming flow

### A2A `message/stream` (`a2a_request_handler.py`)

**File:** `backend/app/services/a2a_request_handler.py` — `handle_message_stream()`

When `result["action"] == "command_executed"`:
- Yields a single `status-update` SSE event with `TaskState.completed` and the response text
- Returns immediately (no environment activation or streaming needed)

### A2A Route (`a2a.py`)

**File:** `backend/app/api/routes/a2a.py` — `handle_jsonrpc()`

Extracts `backend_base_url` from `request.base_url` (with `X-Forwarded-Proto` handling for reverse proxies) and passes it to `A2ARequestHandler.__init__()`.

## File Locations Reference

### New Files

| File | Purpose |
|------|---------|
| `backend/app/services/command_service.py` | Command framework (registry, context, result, dispatch) |
| `backend/app/services/agent_workspace_token_service.py` | Short-lived JWT for A2A agent workspace file access |
| `backend/app/services/commands/__init__.py` | Command handler registration |
| `backend/app/services/commands/files_command.py` | `/files` and `/files-all` command handlers |
| `backend/app/api/routes/shared_workspace.py` | Public file view endpoint |

### Modified Files

| File | Change |
|------|--------|
| `backend/app/services/session_service.py` | Added `backend_base_url` param, Phase 1.5 command detection |
| `backend/app/api/main.py` | Registered `shared_workspace.router` |
| `backend/app/api/routes/messages.py` | Handle `"command_executed"` action |
| `backend/app/services/a2a_request_handler.py` | Added `backend_base_url` param, handle `"command_executed"` in send/stream |
| `backend/app/api/routes/a2a.py` | Extract and pass `backend_base_url` |

### No Changes Needed

- **Frontend**: `MarkdownRenderer` already renders `[text](url)` as clickable links
- **Agent-env core**: Existing `GET /workspace/tree` endpoint reused via adapter

## Verification Checklist

1. **`/files` UI flow**: Type `/files` → shows only the `files` folder listing → click link → opens file viewer
2. **`/files-all` UI flow**: Type `/files-all` → shows all folders (files, scripts, logs, docs, uploads)
3. **A2A flow**: Send `/files` or `/files-all` via A2A → response contains links with `?token=`
4. **Token expiry**: Wait >1 hour → A2A file link returns 401
5. **Environment not running**: Type `/files` on suspended environment → error message about environment status
6. **Unknown command**: Type `/unknown` → passes through to normal LLM flow (not a registered command)
7. **Empty workspace**: Type `/files` on fresh environment → "No files found in workspace" message
8. **New session**: First message is `/files` → session created, title auto-generated, response returned

---

**Document Version:** 1.1
**Last Updated:** 2026-02-22
**Status:** Feature Implemented
