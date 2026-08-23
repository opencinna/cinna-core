# Agent Webhooks — Technical Details

## File Locations

### Backend

**Models:**
- `backend/app/models/agents/agent_webhook.py` — `AgentWebhookType`, `AgentWebhook` (table), `AgentWebhookCreateSession`, `AgentWebhookCreateScript`, `AgentWebhookUpdate`, `AgentWebhookPublic`, `AgentWebhookPublicWithToken`, `AgentWebhooksPublic`
- `backend/app/models/agents/agent_webhook_log.py` — `AgentWebhookLog` (table), `AgentWebhookLogPublic`, `AgentWebhookLogsPublic`

**Routes:**
- `backend/app/api/routes/agent_webhooks.py` — authenticated CRUD, logs, and regenerate-token endpoints (nested under `agents/{id}/webhooks`)
- `backend/app/api/routes/agent_hooks.py` — public execution endpoint (`/agent-hooks/{webhook_id}`, no JWT)

**Services:**
- `backend/app/services/agents/agent_webhook_service.py` — `AgentWebhookService` (CRUD, token generation/validation, `fire_webhook`, internal dispatchers, log helpers)
- `backend/app/services/agents/agent_webhook_errors.py` — exception hierarchy
- `backend/app/services/agents/environment_resolver.py` — `get_active_environment`, `ensure_environment_running` (shared with `AgentSchedulerService`)

**Environment Connector:**
- `backend/app/services/environments/agent_env_connector.py` — `exec_command()` extended with optional `env` dict and `stdin` parameters

**Agent-Env Server:**
- `backend/app/env-templates/app_core_base/core/server/routes.py` — `POST /exec` handler extended to accept `env` and `stdin` in the request body

**Migration:**
- `backend/app/alembic/versions/ba8f1f14621f_add_agent_webhook_and_agent_webhook_log_.py`

**Tests:**
- `backend/tests/api/agents/` — 38 agent-webhook-specific tests; 240 agents-domain tests pass

### Frontend

**Components:**
- `frontend/src/components/Agents/Webhooks/AgentWebhooksCard.tsx` — main card: list, query, mutation orchestration, all dialog state
- `frontend/src/components/Agents/Webhooks/WebhookTypeSelectorDialog.tsx` — Session vs Script type chooser
- `frontend/src/components/Agents/Webhooks/CreateSessionWebhookForm.tsx` — session webhook creation form + token reveal
- `frontend/src/components/Agents/Webhooks/CreateScriptWebhookForm.tsx` — script webhook creation form + token reveal
- `frontend/src/components/Agents/Webhooks/EditWebhookDialog.tsx` — edit dialog with type-conditional fields
- `frontend/src/components/Agents/Webhooks/WebhookCard.tsx` — per-webhook row with copy, toggle, logs, edit, regenerate, delete
- `frontend/src/components/Agents/Webhooks/WebhookTokenDisplay.tsx` — re-exports `WebhookTokenDisplay` from `frontend/src/components/Tasks/Triggers/WebhookTokenDisplay.tsx`
- `frontend/src/components/Agents/Webhooks/WebhookLogsModal.tsx` — expandable log rows with status badges and session links

**Integration:**
- `frontend/src/components/Agents/AgentIntegrationsTab.tsx` — renders `<AgentWebhooksCard agentId={agent.id} />` gated on `isOwner`

---

## Database Schema

### Table: `agent_webhook`

| Field | Type | Description |
|-------|------|-------------|
| `id` | UUID (PK) | Row identifier |
| `agent_id` | UUID (FK → `agent.id`, CASCADE) | Owning agent |
| `owner_id` | UUID (FK → `user.id`, CASCADE) | Agent owner; used as `user_id` when creating sessions |
| `type` | str | `"session"` or `"script"`. Immutable after creation. |
| `name` | str (1–255) | User-friendly label |
| `enabled` | bool, default `True` | Disabled webhooks return 404 on the public endpoint |
| `payload_template` | Text, nullable, max 10,000 | Static context prepended to the dynamic payload (session: included in prompt; script: stored in log) |
| `prompt` | Text, nullable | Session type only. Custom starting prompt; falls back to `agent.entrypoint_prompt`, then `"Start webhook-triggered execution."` |
| `session_mode` | str, nullable | Session type only. `"conversation"` or `"building"`. Null for script webhooks. |
| `command` | Text, nullable | Script type only. Shell command, max 2,000 chars. |
| `command_timeout_seconds` | int, nullable | Script type only. 1–300; default 120. Null for session webhooks. |
| `webhook_id` | str, UNIQUE | URL slug, `secrets.token_urlsafe(8)` (~11 chars). Collision-retry logic in service layer. |
| `webhook_token_encrypted` | str | Fernet-encrypted plaintext token via `encrypt_field()` |
| `webhook_token_prefix` | str, max 8 | First 8 chars of plaintext token for UI display |
| `last_execution` | datetime, nullable | UTC; updated on every fire (success or error) |
| `created_at` | datetime | UTC |
| `updated_at` | datetime | UTC |

**Indexes:**
- `ix_agent_webhook_agent_id` on `(agent_id)` — list webhooks by agent
- `ix_agent_webhook_owner_id` on `(owner_id)`
- `ix_agent_webhook_webhook_id` UNIQUE on `(webhook_id)` — hot path for public endpoint lookup

**Cascade:** Deleting an agent removes all its webhooks and their logs. Deleting a webhook removes its logs.

### Table: `agent_webhook_log`

| Field | Type | Description |
|-------|------|-------------|
| `id` | UUID (PK) | Log entry identifier |
| `webhook_id_fk` | UUID (FK → `agent_webhook.id`, CASCADE) | Parent webhook row. Column named `_fk` to avoid clash with the `webhook_id` slug on the parent. |
| `agent_id` | UUID (FK → `agent.id`, CASCADE) | Denormalized for agent-wide queries |
| `webhook_type` | str | Snapshot of `agent_webhook.type` at execution time |
| `status` | str | `"session_started"`, `"success"`, `"script_error"`, or `"error"` |
| `remote_ip` | str, nullable, max 64 | First hop of `X-Forwarded-For`, fallback to `request.client.host` |
| `headers_subset` | JSON, nullable | Allowlisted headers only. Returns `{}` (empty dict) when none of the allowlisted headers are present in the request. |
| `payload_received` | Text, nullable | Raw body, truncated to 10,000 chars |
| `payload_content_type` | str, nullable | Incoming `Content-Type` header |
| `prompt_used` | Text, nullable | Assembled prompt (session type only) |
| `command_executed` | Text, nullable | Raw command string (script type only) |
| `command_output` | Text, nullable | stdout, truncated to 10,000 chars (script type only) |
| `command_stderr` | Text, nullable | stderr, truncated to 10,000 chars (script type only) |
| `command_exit_code` | int, nullable | Process exit code (script type only); `-1` for timeout |
| `session_id` | UUID (FK → `session.id`, SET NULL), nullable | Session created by this invocation. SET NULL when the session is later deleted — log is preserved. |
| `error_message` | Text, nullable | Human-readable error detail when `status="error"` |
| `duration_ms` | int, nullable | End-to-end handler wall time in milliseconds |
| `executed_at` | datetime | UTC; when the invocation was processed |

**Indexes:**
- `ix_agent_webhook_log_webhook_fk` on `(webhook_id_fk)` — list logs per webhook
- `ix_agent_webhook_log_agent_id` on `(agent_id)`
- `ix_agent_webhook_log_executed_at` on `(executed_at)` — ordering

**Lifecycle:** Append-only. Logs are never updated. UI shows the last 50 per webhook.

#### Status Semantics

| Status | Type | Meaning |
|--------|------|---------|
| `session_started` | session | Session successfully created and user message queued |
| `success` | script | Command exited with code 0 |
| `script_error` | script | Command exited with non-zero code (normal operational outcome; output preserved in log) |
| `error` | both | Infrastructure failure: no active environment, activation timeout, session-creation failure, or unhandled exception |

---

## API Endpoints

### Authenticated CRUD — `backend/app/api/routes/agent_webhooks.py`

All endpoints require JWT (`CurrentUser` + `SessionDep`). The service layer verifies `agent.owner_id == current_user.id` on every call.

Router registered in `backend/app/api/main.py` with prefix `/api/v1`.

| Method | Path | Request Body | Response | Notes |
|--------|------|-------------|----------|-------|
| POST | `/api/v1/agents/{agent_id}/webhooks/session` | `AgentWebhookCreateSession` | `AgentWebhookPublicWithToken` | Plaintext token returned once only |
| POST | `/api/v1/agents/{agent_id}/webhooks/script` | `AgentWebhookCreateScript` | `AgentWebhookPublicWithToken` | Same; `command` required |
| GET | `/api/v1/agents/{agent_id}/webhooks` | — | `AgentWebhooksPublic` | Ordered by `created_at` DESC |
| GET | `/api/v1/agents/{agent_id}/webhooks/{webhook_pk}` | — | `AgentWebhookPublic` | `webhook_pk` is the row UUID |
| PATCH | `/api/v1/agents/{agent_id}/webhooks/{webhook_pk}` | `AgentWebhookUpdate` | `AgentWebhookPublic` | Type-mismatched fields → 400; does not return token |
| DELETE | `/api/v1/agents/{agent_id}/webhooks/{webhook_pk}` | — | `{"success": true}` | Cascades to logs |
| POST | `/api/v1/agents/{agent_id}/webhooks/{webhook_pk}/regenerate-token` | — | `AgentWebhookPublicWithToken` | Same URL slug, new token; old token invalidated immediately |
| GET | `/api/v1/agents/{agent_id}/webhooks/{webhook_pk}/logs` | `?limit=50` (1–200) | `AgentWebhookLogsPublic` | Ordered by `executed_at` DESC |

### Public Execution — `backend/app/api/routes/agent_hooks.py`

Mounted at the app root in `backend/app/main.py` (`app.include_router(agent_hooks_router, prefix="/agent-hooks")`). No JWT. No `/api/v1` prefix.

| Method | Path | Auth | Response |
|--------|------|------|----------|
| POST | `/agent-hooks/{webhook_id}` | `Authorization: Bearer <token>` header OR `?token=<token>` query | `{"success": true, "webhook_type": "session"\|"script", "log_id": "<uuid>"}` |

**Request handling sequence:**

```
1. Extract token from Authorization header or ?token= query param
   → 401 if absent

2. Check Content-Length header (fast path)
   → 413 if > 64 KB

3. Read body; hard-cap check on actual bytes
   → 413 if > 64 KB
   → Decode UTF-8 with errors="replace"

4. Snapshot allowlisted headers + caller IP

5. validate_webhook_token(webhook_id, token)
   → 404 if webhook not found or disabled
   → 401 if token mismatch

6. fire_webhook(webhook, payload, headers, remote_ip)
   → Always returns a log (even on infra errors)
   → HTTP 200 with log_id regardless of internal outcome
```

---

## Pydantic Schemas

Defined in `backend/app/models/agents/agent_webhook.py`:

| Schema | Use |
|--------|-----|
| `AgentWebhookType` | Constants: `SESSION = "session"`, `SCRIPT = "script"` |
| `AgentWebhook` | DB table model (`table=True`) |
| `AgentWebhookCreateSession` | POST body for session-type creation; `type` is `Literal["session"]` |
| `AgentWebhookCreateScript` | POST body for script-type creation; `command` required, max 2,000 chars; `command_timeout_seconds` defaults to 120 |
| `AgentWebhookUpdate` | PATCH body; all fields optional; `type` excluded (immutable) |
| `AgentWebhookPublic` | Response for list/get/update; includes computed `webhook_url`; never includes plaintext token |
| `AgentWebhookPublicWithToken` | Extends `AgentWebhookPublic` with `webhook_token: str`; returned only on create and regenerate |
| `AgentWebhooksPublic` | `data: list[AgentWebhookPublic]`, `count: int` |

Defined in `backend/app/models/agents/agent_webhook_log.py`:

| Schema | Use |
|--------|-----|
| `AgentWebhookLog` | DB table model (`table=True`) |
| `AgentWebhookLogPublic` | Response for log list |
| `AgentWebhookLogsPublic` | `data: list[AgentWebhookLogPublic]`, `count: int` |

All models re-exported from `backend/app/models/__init__.py`.

---

## Service Layer — `AgentWebhookService`

Class in `backend/app/services/agents/agent_webhook_service.py`. Static-method style, consistent with `AgentSchedulerService` and `TaskTriggerService`.

### Class Constants

| Constant | Value | Purpose |
|----------|-------|---------|
| `FORWARDED_HEADERS` | `("user-agent", "x-forwarded-for", "x-real-ip", "x-github-event", "x-gitlab-event", "x-hub-signature-256", "x-event-key")` | Header allowlist; anything outside this set is dropped before logging or forwarding |

### Access Control

| Method | Purpose |
|--------|---------|
| `verify_agent_access(db_session, agent_id, user_id)` | Checks agent exists and is owned by `user_id`. Raises `WebhookNotFoundError` or `WebhookPermissionError`. |
| `get_webhook_for_agent(db_session, webhook_pk, agent_id)` | Fetches webhook row and verifies it belongs to `agent_id`. Guards against cross-agent ID reuse. |

### Token Helpers

| Method | Purpose |
|--------|---------|
| `generate_webhook_credentials()` | Returns `(webhook_id, plaintext_token, encrypted_token, token_prefix)`. Uses `secrets.token_urlsafe(8)` for slug, `secrets.token_urlsafe(32)` for token. |
| `_generate_unique_webhook_id(db_session, max_attempts=5)` | Retries slug generation to handle (extremely unlikely) collisions; raises `RuntimeError` after `max_attempts`. |
| `build_webhook_url(webhook_id)` | Returns `f"{settings.webhook_base_url}/agent-hooks/{webhook_id}"`. `webhook_base_url` is an alias of `settings.backend_base_url`, which resolves `BACKEND_BASE_URL` (former name `WEBHOOK_BASE_URL` still honoured), falling back to `FRONTEND_HOST` — the single resolution point shared with task-trigger and server-channel webhook URLs, the Agent REST API base, A2A attachment links and the native-client OAuth discovery endpoints (see `docs/application/server_channels/server_channels_tech.md` → Configuration). |

### CRUD Methods

| Method | Returns |
|--------|---------|
| `create_session_webhook(db_session, agent_id, user_id, data)` | `(AgentWebhook, plaintext_token)` |
| `create_script_webhook(db_session, agent_id, user_id, data)` | `(AgentWebhook, plaintext_token)` |
| `list_webhooks(db_session, agent_id, user_id)` | `list[AgentWebhook]` ordered by `created_at` DESC |
| `get_webhook(db_session, agent_id, webhook_pk, user_id)` | `AgentWebhook` |
| `update_webhook(db_session, agent_id, webhook_pk, user_id, data)` | `AgentWebhook`; rejects type-mismatched fields |
| `delete_webhook(db_session, agent_id, webhook_pk, user_id)` | `None` |
| `regenerate_token(db_session, agent_id, webhook_pk, user_id)` | `(AgentWebhook, plaintext_token)` |

**Type-mismatch validation in `update_webhook`:** Fields `prompt`/`session_mode` are session-only; `command`/`command_timeout_seconds` are script-only. Sending a script-only field in a PATCH to a session webhook raises `WebhookValidationError` (HTTP 400).

### Execution Methods

#### `validate_webhook_token(db_session, webhook_id, provided_token)`

1. Looks up `AgentWebhook` by `webhook_id` slug.
2. Raises `WebhookNotFoundError` if not found **or if `enabled=False`** (no existence leakage on public endpoint).
3. Decrypts `webhook_token_encrypted` via `decrypt_field()`.
4. Compares with `hmac.compare_digest(stored_token, provided_token)` (timing-safe).
5. Raises `WebhookTokenInvalidError` on mismatch.
6. Returns the `AgentWebhook` row on success.

#### `fire_webhook(db_session, webhook, payload_text, payload_content_type, headers, remote_ip)`

Orchestrator. Always produces exactly one `AgentWebhookLog`.

```
start timer
filter headers to allowlist
load agent row

if agent not found:
    create log(status="error", error_message="Agent not found")
    touch last_execution
    return log

try:
    if webhook.type == "session":
        log = await _fire_session(...)
    elif webhook.type == "script":
        log = await _fire_script(...)
    else:
        log = create log(status="error", error_message="Unknown webhook type")
except Exception as exc:
    log = create log(status="error", error_message=str(exc))

touch last_execution
return log
```

#### `_fire_session(...)` (internal)

1. `_assemble_session_prompt(webhook, agent, payload_text, payload_content_type, headers_subset)` — builds prompt from `webhook.prompt` or `agent.entrypoint_prompt` or fallback, then appends separator block with webhook name, payload template, payload, and headers JSON. Caps assembled prompt at 20,000 chars with `[truncated]` marker.
2. `SessionService.create_session(db_session, user_id=webhook.owner_id, data=SessionCreate(agent_id, mode, title=f"Webhook: {webhook.name}"), integration_type="webhook")`.
3. If `create_session` returns `None` (no active environment): log `status="error"`.
4. `await MessageService.create_user_message_and_emit_event(db_session, session.id, prompt, answers_to_message_id=None)`.
5. On message enqueue failure: log `status="error"` with session reference preserved.
6. Success: log `status="session_started"`, `session_id` set.

#### `_fire_script(...)` (internal)

1. `get_active_environment(db_session, agent.id)` — from `environment_resolver.py`.
2. If no environment: log `status="error"`.
3. `await ensure_environment_running(environment, get_fresh_db_session=lambda: DBSession(engine))` — auto-activates if suspended/stopped; raises `RuntimeError` on error state or timeout.
4. Builds `exec_env` dict:
   ```python
   {
       "WEBHOOK_PAYLOAD": payload_text or "",
       "WEBHOOK_NAME": webhook.name,
       "WEBHOOK_ID": webhook.webhook_id,
       "WEBHOOK_HEADERS_JSON": json.dumps(headers_subset),
       "WEBHOOK_CONTENT_TYPE": payload_content_type or "",
   }
   ```
5. `await agent_env_connector.exec_command(base_url, auth_token, command=webhook.command, timeout=webhook.command_timeout_seconds or 120, env=exec_env, stdin=payload_text)`.
6. Truncates stdout/stderr to 10,000 chars.
7. `status = "success" if exit_code == 0 else "script_error"`.

### Log Helpers

| Method | Purpose |
|--------|---------|
| `_create_log(db_session, *, webhook, status, ...)` | Inserts immutable `AgentWebhookLog` row; truncates `payload_received` to 10,000 chars |
| `_touch_last_execution(db_session, webhook)` | Updates `webhook.last_execution` and `updated_at` to now; best-effort (exceptions logged, not re-raised) |
| `get_webhook_logs(db_session, agent_id, webhook_pk, user_id, limit=50)` | Returns logs ordered by `executed_at` DESC; `limit` clamped to minimum 1 |

---

## Token Encryption Flow

Same pattern as [Task Triggers](../../application/input_tasks/task_triggers_tech.md):

1. **Creation / regeneration**: `secrets.token_urlsafe(32)` → plaintext token. `encrypt_field(token)` → Fernet ciphertext stored in `webhook_token_encrypted`. `token[:8]` stored in `webhook_token_prefix`. Plaintext returned to the route handler once, then discarded.
2. **Validation**: `decrypt_field(webhook_token_encrypted)` → plaintext. `hmac.compare_digest(stored, provided)` — constant-time comparison regardless of token length.
3. **Rotation**: New `secrets.token_urlsafe(32)` → same encrypt-and-store flow. Old ciphertext is overwritten; old token immediately becomes invalid.

`encrypt_field` / `decrypt_field` live in `backend/app/core/security.py` and use the application's Fernet key derived from `SECRET_KEY`.

---

## Environment Resolver — Shared Helpers

`backend/app/services/agents/environment_resolver.py` contains two module-level functions that were extracted from `AgentSchedulerService` to avoid cross-service coupling:

| Function | Signature | Purpose |
|----------|-----------|---------|
| `get_active_environment` | `(session: DBSession, agent_id: UUID) -> AgentEnvironment \| None` | Returns the agent's active environment or `None` if not configured |
| `ensure_environment_running` | `async (environment: AgentEnvironment, get_fresh_db_session: Callable) -> AgentEnvironment` | Activates a suspended or stopped environment and polls until `status="running"` (max 120 s). Raises `RuntimeError` on error state or timeout. |

`AgentSchedulerService` now wraps these functions via thin shim methods; `AgentWebhookService` imports them directly. Both features share identical activation semantics.

---

## Agent-Env `/exec` Extension

`AgentEnvConnector.exec_command()` signature (in `backend/app/services/environments/agent_env_connector.py`):

```python
async def exec_command(
    self,
    base_url: str,
    auth_token: str,
    command: str,
    timeout: int = 120,
    env: dict[str, str] | None = None,    # extended for webhooks (also benefits schedulers)
    stdin: str | None = None,             # extended for webhooks
) -> dict:
    # Returns {"exit_code": int, "stdout": str, "stderr": str}
```

When `env` is set, it is included in the JSON POST body to `/exec`. The agent-env server merges the dict with `os.environ.copy()` (so PATH, HOME, etc. remain intact) before passing to `asyncio.create_subprocess_shell()`. When `stdin` is set, the body is piped to the subprocess stdin after `proc.communicate(input=stdin.encode("utf-8"))`. Both parameters default to `None` — existing callers (scheduler script triggers) are unaffected.

The `/exec` endpoint in `backend/app/env-templates/app_core_base/core/server/routes.py` enforces a 100 KB cap on individual env var values.

---

## Frontend Component Map

All components in `frontend/src/components/Agents/Webhooks/`.

### `AgentWebhooksCard.tsx`

- **Props:** `{ agentId: string }`
- **Query:** `useQuery(["agent-webhooks", agentId], () => AgentWebhooksService.listWebhooks({ agentId }), { enabled: !!agentId })`
- **Create flow state:** `createStep: "type_select" | "session_form" | "script_form" | null`
- **Mutations:** `toggleMutation` (update with `{enabled}`), `deleteMutation`, `regenerateMutation` — all invalidate `["agent-webhooks", agentId]`
- **After regenerate:** displays token reveal in a `Dialog` using `WebhookTokenDisplay`

### `WebhookTypeSelectorDialog.tsx`

Two clickable cards: Session Trigger (MessageSquare icon) and Script Trigger (Terminal icon, amber accent). Selecting a type calls `onSelect(type)` which transitions the parent's `createStep`.

### `CreateSessionWebhookForm.tsx` / `CreateScriptWebhookForm.tsx`

Each form holds its own local field state. On successful create mutation, the form transitions to the `WebhookTokenDisplay` panel within the same dialog. The token is shown only once; closing the dialog without copying requires a regenerate.

### `WebhookCard.tsx`

Per-webhook row component. Displays:
- Type icon: `MessageSquare` (session, primary color) or `Terminal` (script, amber)
- Name, enabled/disabled badge, type badge
- Masked URL: `{origin}/…/{webhook_id}` with copy-to-clipboard button (shows checkmark on success)
- Token prefix: `{webhook_token_prefix}…`
- `last_execution` relative timestamp: `Fired Xs ago` / `Xm ago` / etc.
- Action cluster: History (logs), Edit, Refresh (regenerate token + confirm dialog), Power (toggle), Trash (delete + confirm dialog)

Disabled webhooks: left side dims to 60% opacity; action cluster stays full contrast so the toggle is visually accessible.

### `WebhookLogsModal.tsx`

- **Query:** `useQuery(["agent-webhook-logs", webhook?.id], ..., { enabled: open && !!webhook })`
- Fetched only when the modal opens (lazy)
- Each row is a `LogRow` with expand/collapse toggle
- Status badges: `session_started` → green Play, `success` → green Check, `script_error` → amber Zap, `error` → red X
- Expanded row sections: caller IP, payload received, forwarded headers, prompt used, command, exit code, stdout, stderr, error message, session link (opens in new tab via `target="_blank"`)

### `WebhookTokenDisplay.tsx`

Re-exports `WebhookTokenDisplay` from `frontend/src/components/Tasks/Triggers/WebhookTokenDisplay.tsx`. The shared component implements the one-time reveal UI with:
- Warning banner ("Save this token now — it will not be shown again")
- Three copy blocks: full webhook URL, token, and a ready-to-run `curl` example
- Copy-to-clipboard buttons with success indicator

### React Query Keys

| Key | Scope |
|-----|-------|
| `["agent-webhooks", agentId]` | Webhook list for an agent |
| `["agent-webhook-logs", webhookPk]` | Logs for a specific webhook (fetched on-demand) |

---

## Database Migration

File: `backend/app/alembic/versions/ba8f1f14621f_add_agent_webhook_and_agent_webhook_log_.py`

Creates both `agent_webhook` and `agent_webhook_log` tables with all fields, foreign keys, and indexes as described in the schema section above. No changes to existing tables. The `/exec` extension is a code change only — no migration required.

---

## Error Hierarchy

Module: `backend/app/services/agents/agent_webhook_errors.py`

| Class | Status Code | Default Message |
|-------|-------------|----------------|
| `WebhookError` | 400 | base; carries `status_code` + `message` |
| `WebhookNotFoundError` | 404 | "Webhook not found" |
| `WebhookValidationError` | 400 | caller-supplied |
| `WebhookPermissionError` | 403 | "Not enough permissions" |
| `WebhookTokenInvalidError` | 401 | "Invalid or expired token" |

Route handlers call `_handle_webhook_error(exc)` which translates any `WebhookError` to a FastAPI `HTTPException` with the corresponding status code and message.

---

## Edge Cases

| Scenario | Behavior |
|----------|----------|
| Unknown `webhook_id` on public endpoint | 404 "Webhook not found" |
| `enabled=False` on public endpoint | 404 same shape — no existence leakage |
| Token missing | 401 "Token required" |
| Token mismatch | 401 "Invalid or expired token" |
| Payload > 64 KB | 413 before token validation |
| Binary body that fails UTF-8 decode | Stored as `errors="replace"` string; log proceeds normally |
| No active environment (script type) | HTTP 200; log `status="error"`, `error_message="No active environment found for agent"` |
| Environment in error state (script type) | HTTP 200; log `status="error"` from `RuntimeError` raised by `ensure_environment_running` |
| Env activation timeout (script type) | HTTP 200; log `status="error"`, message "Environment {id} activation timed out after 120 seconds" |
| Script non-zero exit | HTTP 200; log `status="script_error"`; exit code + stdout + stderr preserved |
| Script timeout | HTTP 200; log `status="script_error"`; exit code `-1` from `/exec` |
| Session creation returns `None` | HTTP 200; log `status="error"`, `error_message="Could not create session — no active environment"` |
| PATCH sends session-only field to script webhook | HTTP 400 `WebhookValidationError` |
| PATCH sends script-only field to session webhook | HTTP 400 `WebhookValidationError` |
| `headers_subset` when no allowlisted headers present | Stored as `{}` (empty dict), not `None` |
| `payload_template` + large payload exceeds 20,000-char prompt cap | Payload slice is trimmed; `[truncated]` marker appended |
| `name` duplicate within same agent | Allowed — no UNIQUE constraint. `created_at` disambiguates in UI. |
| Concurrent fires to same webhook | Both succeed independently; two logs, two sessions (session type). No row locking. |
| Token reveal closed before copy | Must regenerate to recover — by design, matching task triggers and MCP OAuth patterns |
| Agent deleted while fire in flight | Cascade deletes webhook row; next lookup returns 404 |
| Owner account deleted | Cascade on `user.id` → `owner_id` removes all webhooks |

---

## Security

- All authenticated CRUD endpoints verify `agent.owner_id == current_user.id` in the service layer before any read or write.
- `get_webhook_for_agent` additionally verifies `webhook.agent_id == agent_id` to prevent cross-agent ID reuse.
- The public endpoint has no JWT. Token validation is the sole auth mechanism.
- `authorization` and `cookie` headers are stripped from logs and never forwarded to prompts or env vars.
- Script commands execute inside the agent's Docker container — same sandbox as agent SDK tool calls. No host shell exposure.
- Payload bytes are passed as string values in the subprocess `env` dict, not interpolated into the command string. Shell injection from untrusted content is not possible via this path.
