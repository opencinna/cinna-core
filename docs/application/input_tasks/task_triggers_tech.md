# Task Triggers — Technical Reference

## File Locations

### Backend

**Models:**
- `backend/app/models/tasks/task_trigger.py` — TaskTrigger table, TriggerType constants, all schema classes
- `backend/app/models/__init__.py` — exports (TaskTrigger, TriggerType, all create/update/public schemas)

**Routes:**
- `backend/app/api/routes/task_triggers.py` — trigger CRUD endpoints (nested under /tasks/{task_id}/triggers)
- `backend/app/api/routes/webhooks.py` — public webhook execution endpoint (no JWT auth)
- `backend/app/api/main.py` — router registration

**Services:**
- `backend/app/services/tasks/task_trigger_service.py` — main service
- `backend/app/services/tasks/task_trigger_scheduler.py` — APScheduler polling job

**Reused Services:**
- `backend/app/services/agents/agent_scheduler_service.py` — calculate_next_execution, convert_local_cron_to_utc
- `backend/app/services/ai_functions/ai_functions_service.py` — generate_schedule NL→CRON AI function
- `backend/app/services/tasks/input_task_service.py` — execute_task called on trigger fire
- `backend/app/core/security.py` — encrypt_field, decrypt_field for webhook tokens

**Migrations:**
- `backend/app/alembic/versions/v2q3r4s5t6u7_add_task_trigger_table.py`

**App Startup:**
- `backend/app/main.py` — start_task_trigger_scheduler / shutdown_task_trigger_scheduler registration

### Frontend

**Components:**
- `frontend/src/components/Tasks/Triggers/TriggerManagementModal.tsx` — main modal with trigger list
- `frontend/src/components/Tasks/Triggers/TriggerCard.tsx` — individual trigger card with toggle and actions menu
- `frontend/src/components/Tasks/Triggers/AddScheduleTriggerForm.tsx` — NL schedule input form
- `frontend/src/components/Tasks/Triggers/AddExactDateTriggerForm.tsx` — date/time picker form
- `frontend/src/components/Tasks/Triggers/AddWebhookTriggerForm.tsx` — webhook creation form
- `frontend/src/components/Tasks/Triggers/WebhookTokenDisplay.tsx` — one-time token display with copy buttons and warning
- `frontend/src/components/Tasks/Triggers/triggerApi.ts` — manual fetch-based API service + TypeScript types (pre-gen-client)

**Pages:**
- `frontend/src/routes/_layout/task/$taskId.tsx` — Triggers button with count badge in header, opens TriggerManagementModal

**Generated Client (after `make gen-client`):**
- `frontend/src/client/sdk.gen.ts` — TaskTriggersService
- `frontend/src/client/types.gen.ts` — TaskTriggerPublic, TaskTriggerPublicWithToken, TaskTriggersPublic, TriggerType

## Database Schema

**Table:** `task_trigger`

Key fields:
- `id` (UUID PK, default uuid4)
- `task_id` (UUID FK → input_task.id, CASCADE on delete)
- `owner_id` (UUID FK → user.id, CASCADE on delete)
- `type` (str discriminator: "schedule"|"exact_date"|"webhook")
- `name` (str, 1–255 chars)
- `enabled` (bool, default=True)
- `payload_template` (str|None, max 10,000 chars)

Schedule-specific fields:
- `cron_string` (str|None) — UTC CRON expression
- `timezone` (str|None) — IANA timezone for display
- `schedule_description` (str|None) — AI-generated human-readable label
- `last_execution` (datetime|None)
- `next_execution` (datetime|None) — pre-calculated; used by scheduler polling query

Exact date-specific fields:
- `execute_at` (datetime|None) — UTC fire time
- `executed` (bool, default=False) — set True after firing; prevents re-fire

Webhook-specific fields:
- `webhook_token_encrypted` (str|None) — Fernet-encrypted secret token
- `webhook_token_prefix` (str|None) — first 8 chars of plaintext token, safe for UI display
- `webhook_id` (str|None, UNIQUE) — short URL-safe slug for public webhook URL

Timestamps: `created_at`, `updated_at`

Indexes:
- `ix_task_trigger_task_id` on `(task_id)` — list triggers for a task
- `ix_task_trigger_schedule_poll` on `(type, enabled, next_execution)` — scheduler polling
- `ix_task_trigger_exact_date_poll` on `(type, enabled, execute_at, executed)` — date trigger polling
- `ix_task_trigger_webhook_id` UNIQUE on `(webhook_id)` — webhook URL lookup
- `ix_task_trigger_owner_id` on `(owner_id)`

**Schema classes** (`backend/app/models/tasks/task_trigger.py`):
- `TriggerType` — constants: SCHEDULE, EXACT_DATE, WEBHOOK
- `TaskTrigger(table=True)` — database model
- `TaskTriggerCreateSchedule` — name, type literal, payload_template, natural_language, timezone
- `TaskTriggerCreateExactDate` — name, type literal, payload_template, execute_at, timezone
- `TaskTriggerCreateWebhook` — name, type literal, payload_template (token/webhook_id server-generated)
- `TaskTriggerUpdate` — all fields optional; natural_language triggers AI re-conversion; execute_at resets executed=False
- `TaskTriggerPublic` — all fields except encrypted token; includes computed webhook_url
- `TaskTriggerPublicWithToken` — extends TaskTriggerPublic with full plaintext webhook_token (one-time)
- `TaskTriggersPublic` — data list + count

## API Endpoints

**File:** `backend/app/api/routes/task_triggers.py` (router prefix: `/api/v1/tasks/{task_id}/triggers`)

- `POST /tasks/{task_id}/triggers/schedule` — create schedule trigger (runs AI NL→CRON conversion); response: TaskTriggerPublic
- `POST /tasks/{task_id}/triggers/exact-date` — create exact date trigger; response: TaskTriggerPublic
- `POST /tasks/{task_id}/triggers/webhook` — create webhook trigger; response: TaskTriggerPublicWithToken (full token once)
- `GET /tasks/{task_id}/triggers` — list all triggers for task; response: TaskTriggersPublic
- `GET /tasks/{task_id}/triggers/{trigger_id}` — get single trigger; response: TaskTriggerPublic
- `PATCH /tasks/{task_id}/triggers/{trigger_id}` — update trigger (recalculates next_execution if schedule fields changed); response: TaskTriggerPublic
- `DELETE /tasks/{task_id}/triggers/{trigger_id}` — delete trigger; response: `{"success": true}`
- `POST /tasks/{task_id}/triggers/{trigger_id}/regenerate-token` — regenerate webhook secret (keeps same webhook_id); response: TaskTriggerPublicWithToken

**File:** `backend/app/api/routes/webhooks.py` (public, no JWT auth)

- `POST /hooks/{webhook_id}` — execute webhook trigger; auth via Authorization Bearer header or `?token=` query param; response: `{"success": true, "message": "Task execution triggered"}`

## Services & Key Methods

**TaskTriggerService** (`backend/app/services/tasks/task_trigger_service.py`):

Exception classes: `TriggerError`, `TriggerNotFoundError`, `TriggerValidationError`, `TriggerPermissionError`, `WebhookTokenInvalidError`

Helper methods:
- `verify_task_ownership()` — get task, verify owner_id matches user_id; raises TaskNotFoundError or TriggerPermissionError
- `get_trigger_with_check()` — get trigger, verify task/user ownership
- `generate_webhook_credentials()` — returns (webhook_id, plaintext_token, encrypted_token, token_prefix); uses secrets.token_urlsafe(8) for ID, secrets.token_urlsafe(32) for token

CRUD methods:
- `create_schedule_trigger()` — calls AIFunctionsService.generate_schedule(); converts CRON to UTC via AgentSchedulerService; calculates next_execution
- `create_exact_date_trigger()` — validates future date, converts local to UTC using timezone
- `create_webhook_trigger()` — calls generate_webhook_credentials(); returns (trigger, plaintext_token)
- `list_triggers()`, `get_trigger()`, `update_trigger()`, `delete_trigger()`
- `regenerate_webhook_token()` — generates new credentials, keeps same webhook_id; returns (trigger, new_plaintext_token)

Webhook execution methods:
- `validate_webhook_token()` — lookup by webhook_id, check enabled, decrypt via decrypt_field(), compare with hmac.compare_digest
- `fire_trigger()` — assembles prompt (description + payload_template + webhook body), calls InputTaskService.execute_task(), updates last_execution/next_execution (schedule) or executed=True (exact_date)

Scheduler method:
- `poll_due_triggers()` — queries due schedule triggers (next_execution <= now) and due exact-date triggers (execute_at <= now, executed=False); fires each as background task with error logging

**TaskTriggerScheduler** (`backend/app/services/tasks/task_trigger_scheduler.py`):
- APScheduler BackgroundScheduler; interval=1 minute; max_instances=1 (prevents overlapping poll cycles)
- Functions named `start_scheduler` / `shutdown_scheduler`; aliased on import in main.py to `start_task_trigger_scheduler` / `shutdown_task_trigger_scheduler`
- Follows the pattern of `file_cleanup_scheduler.py` and `environment_suspension_scheduler.py`

## Frontend Components

- `TriggerManagementModal.tsx` — lists all triggers for task; three add buttons (Schedule/Exact Date/Webhook); opened from task detail header Triggers button
- `TriggerCard.tsx` — display varies by type: Clock icon + blue badge (schedule), CalendarClock + amber badge (exact date), Webhook icon + green badge (webhook); enable/disable toggle; actions menu (edit, regenerate token for webhooks, delete)
- `AddScheduleTriggerForm.tsx` — name + NL schedule input + optional payload + auto-detected timezone (read-only display); calls `POST .../triggers/schedule`
- `AddExactDateTriggerForm.tsx` — name + date/time picker + optional payload + timezone; client-side future-date validation; calls `POST .../triggers/exact-date`
- `AddWebhookTriggerForm.tsx` — name + optional payload; calls `POST .../triggers/webhook`; shows WebhookTokenDisplay on success
- `WebhookTokenDisplay.tsx` — warning banner ("Save this token now"); copy-to-clipboard for token, full URL, and example curl command
- `triggerApi.ts` — manual fetch-based API client with TypeScript types; used until `make gen-client` is run, then replaced with auto-generated client imports

**React Query:**
- Query key: `["task-triggers", taskId]`
- Mutations for create/update/delete/regenerate — all invalidate `["task-triggers", taskId]`

## Configuration

- `ENCRYPTION_KEY` env var — required for Fernet webhook token encryption (same key as credential encryption)
- APScheduler poll interval: 1 minute (hardcoded in `task_trigger_scheduler.py`)
- Webhook payload size limit: 64KB (enforced at webhook route)
- Payload template max length: 10,000 characters
- Minimum schedule interval: 30 minutes (AI function validation + backend validation via croniter)

## Security

- Trigger CRUD restricted to task owner; verified via `verify_task_ownership()` on all service methods
- Webhook endpoint is public (no JWT); authenticated solely via encrypted secret token
- Token generated with `secrets.token_urlsafe(32)` — cryptographically random, URL-safe base64
- Token stored encrypted via `encrypt_field()` (Fernet symmetric encryption, PBKDF2-derived key from ENCRYPTION_KEY)
- Token comparison uses `hmac.compare_digest` to prevent timing attacks
- Disabled triggers return 404 on webhook calls to avoid existence confirmation
- Invalid tokens return 401 with generic message (no information leakage)
- Token prefix (first 8 chars) safe to display in UI; full token returned only once
- Cascade deletion: deleting a task removes all its triggers via FK CASCADE
- `fire_trigger()` executes on behalf of task owner (user_id from trigger.owner_id)
