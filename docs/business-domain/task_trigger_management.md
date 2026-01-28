# Task Trigger Management - Implementation Reference

## Purpose

Enable automatic and event-driven task execution through configurable triggers. Tasks can have multiple triggers of different types, each capable of providing a payload that is appended to the task prompt before execution. Three trigger types are supported: recurring schedules (CRON-based), one-time exact date execution, and webhook-based external event triggers.

## Feature Overview

**Core Capabilities:**
- **Schedule Triggers**: Recurring CRON-based execution (reuses the AI smart schedule function from `AgentSchedulerService`)
- **Exact Date Triggers**: One-time execution at a specific date and time
- **Webhook Triggers**: External HTTP-triggered execution with unique URLs and encrypted secret tokens
- **Payload Support**: Each trigger can provide a payload that gets appended to the task prompt
- **Multiple Triggers per Task**: A single task can have any combination of trigger types

**Architecture:**

```
Frontend UI (Modal)  →  Backend API  →  TaskTriggerService  →  Database
     │                                       │
     │                                       ├── Schedule: AgentSchedulerService (reused)
     │                                       ├── Exact Date: Simple datetime comparison
     │                                       └── Webhook: Public endpoint with token validation
     │
     └── Manage Triggers button in task detail header
         └── TriggerManagementModal component

Trigger Execution Flow:
  Schedule/Date Runner (APScheduler)  ─┐
                                       ├──→ TaskTriggerService.fire_trigger()
  Webhook HTTP Request ────────────────┘          │
                                            InputTaskService.execute_task()
                                            (with trigger payload appended to prompt)
```

**Key Design Decisions:**
- All trigger types stored in a single `task_trigger` table with a `type` discriminator
- Webhook tokens are random API tokens (not JWT), stored encrypted using the existing `encrypt_field()` pattern
- Schedule triggers reuse `AgentSchedulerService.calculate_next_execution()` and the `generate_agent_schedule` AI function
- A new APScheduler job polls for due schedule and exact-date triggers
- Webhook endpoints are public (no JWT auth) but validated via the encrypted secret token in the URL

## Data Models

### TaskTrigger Table

**File:** `backend/app/models/task_trigger.py`

**Purpose:** Stores all trigger configurations for tasks. Uses a `type` field as discriminator to differentiate behavior.

```
Table: task_trigger
─────────────────────────────────────────────────────────────────────────
Field                    Type                 Constraints / Notes
─────────────────────────────────────────────────────────────────────────
id                       UUID                 PK, default uuid4
task_id                  UUID                 FK → input_task.id, CASCADE
owner_id                 UUID                 FK → user.id, CASCADE
type                     str                  "schedule" | "exact_date" | "webhook"
name                     str                  User-facing label (e.g. "Daily morning check")
enabled                  bool                 default=True
payload_template         str | None           Static payload text appended to task prompt on fire

# Schedule-specific fields (type="schedule")
cron_string              str | None           CRON expression in UTC
timezone                 str | None           IANA timezone (e.g. "Europe/Berlin")
schedule_description     str | None           Human-readable from AI (e.g. "Every weekday at 7 AM CET")
last_execution           datetime | None      Last time this trigger fired
next_execution           datetime | None      Pre-calculated next fire time

# Exact date fields (type="exact_date")
execute_at               datetime | None      UTC datetime when trigger should fire (one-shot)
executed                 bool                 default=False, set to True after firing

# Webhook fields (type="webhook")
webhook_token_encrypted  str | None           Encrypted random API token (via encrypt_field)
webhook_token_prefix     str | None           First 8 chars of plaintext token (for display: "abc1****")
webhook_id               str | None           Short unique slug for URL (e.g. "a1b2c3d4")

# Timestamps
created_at               datetime             default=utcnow
updated_at               datetime             default=utcnow
─────────────────────────────────────────────────────────────────────────

Indexes:
- (task_id)                            — list triggers for a task
- (type, enabled, next_execution)      — scheduler polling query
- (type, enabled, execute_at, executed)— exact-date polling query
- (webhook_id) UNIQUE                  — webhook URL lookup
- (owner_id)                           — user's triggers

Relationships:
- task_trigger.task_id → input_task.id  (CASCADE on delete)
- task_trigger.owner_id → user.id       (CASCADE on delete)
```

**Design Notes:**
- All type-specific fields are nullable; only relevant fields are populated per type
- `webhook_id` is a short random slug (8-12 chars, URL-safe) used in the public webhook URL; separate from the secret token
- `webhook_token_encrypted` stores the full secret token encrypted via `encrypt_field()` from `backend/app/core/security.py`
- `webhook_token_prefix` stores the first 8 characters of the plaintext token for safe display in the UI (e.g., "a1b2c3d4...")
- `executed` flag on exact-date triggers prevents re-firing; once fired, the trigger is effectively spent
- `next_execution` for schedule triggers is pre-calculated using `AgentSchedulerService.calculate_next_execution()`

### Model Classes

**File:** `backend/app/models/task_trigger.py`

```
Constants:
  TriggerType.SCHEDULE = "schedule"
  TriggerType.EXACT_DATE = "exact_date"
  TriggerType.WEBHOOK = "webhook"

Database Model:
  TaskTrigger(table=True) — all fields as described above

Create Schemas:
  TaskTriggerCreateSchedule:
    - name: str
    - type: "schedule" (literal)
    - payload_template: str | None
    - natural_language: str         # User's NL schedule input (for AI conversion)
    - timezone: str                 # IANA timezone from browser

  TaskTriggerCreateExactDate:
    - name: str
    - type: "exact_date" (literal)
    - payload_template: str | None
    - execute_at: datetime          # When to fire (local time, converted to UTC by backend)
    - timezone: str                 # For display and conversion

  TaskTriggerCreateWebhook:
    - name: str
    - type: "webhook" (literal)
    - payload_template: str | None
    # No additional fields — token and webhook_id are generated server-side

Update Schema:
  TaskTriggerUpdate:
    - name: str | None
    - enabled: bool | None
    - payload_template: str | None
    # Schedule updates:
    - natural_language: str | None  # Re-run AI conversion if provided
    - timezone: str | None
    # Exact date updates:
    - execute_at: datetime | None
    # Webhook: token cannot be updated (only regenerated via dedicated endpoint)

Public Response Schemas:
  TaskTriggerPublic:
    - id, task_id, type, name, enabled, payload_template
    - cron_string, timezone, schedule_description, last_execution, next_execution  (schedule)
    - execute_at, executed  (exact_date)
    - webhook_id, webhook_token_prefix, webhook_url  (webhook — computed field)
    - created_at, updated_at

  TaskTriggerPublicWithToken:
    - Extends TaskTriggerPublic
    - webhook_token: str            # Full plaintext token, only returned once on creation

  TaskTriggersPublic:
    - data: list[TaskTriggerPublic]
    - count: int
```

### Model Exports

**File:** `backend/app/models/__init__.py`

Add exports:
- `TaskTrigger`, `TriggerType`
- `TaskTriggerCreateSchedule`, `TaskTriggerCreateExactDate`, `TaskTriggerCreateWebhook`
- `TaskTriggerUpdate`
- `TaskTriggerPublic`, `TaskTriggerPublicWithToken`, `TaskTriggersPublic`

## Security Architecture

### Webhook Token Security

- **Token Generation**: `secrets.token_urlsafe(32)` — 32-byte random token, URL-safe base64 encoded
- **Storage**: Token encrypted via `encrypt_field()` (Fernet symmetric encryption using PBKDF2-derived key from `ENCRYPTION_KEY` env var)
- **Display**: Only `webhook_token_prefix` (first 8 chars + "...") shown in UI after creation
- **Full token disclosure**: The full plaintext token is returned **only once** in the creation response (`TaskTriggerPublicWithToken`). After that, only the prefix is available. Users must copy it immediately.
- **URL structure**: `POST /api/v1/hooks/{webhook_id}` — the `webhook_id` is a public slug, the token is sent as a `Bearer` header or `?token=` query parameter
- **Validation**: On webhook call, decrypt stored token and compare with provided token using `hmac.compare_digest` (timing-safe)
- **Rate limiting**: Webhook endpoint should be rate-limited per `webhook_id` (future enhancement, note in code)
- **Token regeneration**: Dedicated endpoint to regenerate token (invalidates old one)

### Access Control

- **Trigger CRUD**: Only task owner can create/read/update/delete triggers for their tasks
- **Webhook execution**: Public endpoint, validated by secret token only (no JWT)
- **Ownership verification**: `TaskTriggerService` verifies `task.owner_id == current_user.id` on all operations
- **Cascade deletion**: Deleting a task deletes all its triggers (FK CASCADE)
- **Trigger execution authorization**: When a trigger fires, the task is executed on behalf of the task owner

### Input Validation

- Schedule triggers: CRON string validated by `croniter` library; minimum 30-minute interval (reuse `AgentSchedulerService` validation)
- Exact date triggers: `execute_at` must be in the future
- Webhook triggers: Payload size limited (e.g., 64KB max for webhook request body)
- Payload template: Max length 10,000 characters
- Name: 1-255 characters

## Backend Implementation

### API Routes

**File:** `backend/app/api/routes/task_triggers.py`

**Router prefix:** Nested under tasks: `/api/v1/tasks/{task_id}/triggers`

```
CRUD Operations:
─────────────────────────────────────────────────────────────────
POST   /tasks/{task_id}/triggers/schedule
  - Request: TaskTriggerCreateSchedule
  - Response: TaskTriggerPublic
  - Calls AI schedule generator, creates trigger with CRON
  - Dependencies: SessionDep, CurrentUser
  - Auth: task ownership check

POST   /tasks/{task_id}/triggers/exact-date
  - Request: TaskTriggerCreateExactDate
  - Response: TaskTriggerPublic
  - Creates one-shot trigger at specified datetime
  - Dependencies: SessionDep, CurrentUser
  - Auth: task ownership check

POST   /tasks/{task_id}/triggers/webhook
  - Request: TaskTriggerCreateWebhook
  - Response: TaskTriggerPublicWithToken  (includes full token ONCE)
  - Generates webhook_id + secret token, encrypts token
  - Dependencies: SessionDep, CurrentUser
  - Auth: task ownership check

GET    /tasks/{task_id}/triggers
  - Response: TaskTriggersPublic
  - Lists all triggers for task
  - Dependencies: SessionDep, CurrentUser
  - Auth: task ownership check

GET    /tasks/{task_id}/triggers/{trigger_id}
  - Response: TaskTriggerPublic
  - Get single trigger details
  - Dependencies: SessionDep, CurrentUser
  - Auth: task ownership check

PATCH  /tasks/{task_id}/triggers/{trigger_id}
  - Request: TaskTriggerUpdate
  - Response: TaskTriggerPublic
  - Update trigger (recalculates next_execution if schedule fields change)
  - Dependencies: SessionDep, CurrentUser
  - Auth: task ownership check

DELETE /tasks/{task_id}/triggers/{trigger_id}
  - Response: {"success": true}
  - Dependencies: SessionDep, CurrentUser
  - Auth: task ownership check

POST   /tasks/{task_id}/triggers/{trigger_id}/regenerate-token
  - Response: TaskTriggerPublicWithToken  (new full token ONCE)
  - Only for webhook triggers — generates new token, invalidates old
  - Dependencies: SessionDep, CurrentUser
  - Auth: task ownership check
```

**Webhook Execution Endpoint (Public):**

**File:** `backend/app/api/routes/webhooks.py`

```
POST   /hooks/{webhook_id}
  - Request: WebhookPayload (body: any JSON, optional)
  - Auth: Bearer token in Authorization header OR ?token= query parameter
  - Response: {"success": true, "message": "Task execution triggered"}
  - No SessionDep/CurrentUser — public endpoint
  - Validates token → looks up trigger → fires task execution
  - Rate limited per webhook_id (future)
```

**Router Registration:**

**File:** `backend/app/api/main.py`

```python
from app.api.routes import task_triggers, webhooks

api_router.include_router(
    task_triggers.router,
    prefix="/tasks",
    tags=["task-triggers"]
)
api_router.include_router(
    webhooks.router,
    prefix="/hooks",
    tags=["webhooks"]
)
```

### Service Layer

**File:** `backend/app/services/task_trigger_service.py`

**Class:** `TaskTriggerService`

**Exception Classes:**
- `TriggerError` — base exception with `message` and `status_code`
- `TriggerNotFoundError(TriggerError)` — 404
- `TriggerValidationError(TriggerError)` — 400
- `TriggerPermissionError(TriggerError)` — 403
- `WebhookTokenInvalidError(TriggerError)` — 401

**Helper Methods:**

```
verify_task_ownership(db_session, task_id, user_id) -> InputTask
  - Gets task, verifies owner_id matches user_id
  - Raises TaskNotFoundError or TriggerPermissionError

get_trigger_with_check(db_session, trigger_id, task_id, user_id) -> TaskTrigger
  - Gets trigger, verifies it belongs to the task and user owns the task
  - Raises TriggerNotFoundError or TriggerPermissionError

generate_webhook_credentials() -> tuple[str, str, str, str]
  - Generates: (webhook_id, plaintext_token, encrypted_token, token_prefix)
  - webhook_id: secrets.token_urlsafe(8) → URL-safe 11-char slug
  - token: secrets.token_urlsafe(32) → 43-char random token
  - encrypted: encrypt_field(token)
  - prefix: token[:8]
```

**CRUD Methods:**

```
create_schedule_trigger(db_session, task_id, user_id, data: TaskTriggerCreateSchedule) -> TaskTrigger
  - Verifies task ownership
  - Calls AIFunctionsService.generate_schedule() with data.natural_language and data.timezone
  - If AI returns error, raises TriggerValidationError
  - Converts CRON to UTC via AgentSchedulerService.convert_local_cron_to_utc()
  - Calculates next_execution via AgentSchedulerService.calculate_next_execution()
  - Creates TaskTrigger record with type="schedule"

create_exact_date_trigger(db_session, task_id, user_id, data: TaskTriggerCreateExactDate) -> TaskTrigger
  - Verifies task ownership
  - Validates execute_at is in the future
  - Converts local execute_at to UTC using data.timezone
  - Creates TaskTrigger record with type="exact_date", executed=False

create_webhook_trigger(db_session, task_id, user_id, data: TaskTriggerCreateWebhook) -> tuple[TaskTrigger, str]
  - Verifies task ownership
  - Calls generate_webhook_credentials()
  - Creates TaskTrigger record with type="webhook"
  - Returns (trigger, plaintext_token) — token returned only at creation time

list_triggers(db_session, task_id, user_id) -> list[TaskTrigger]
  - Verifies task ownership
  - Returns all triggers for task, ordered by created_at

get_trigger(db_session, trigger_id, task_id, user_id) -> TaskTrigger
  - Gets trigger with ownership check

update_trigger(db_session, trigger_id, task_id, user_id, data: TaskTriggerUpdate) -> TaskTrigger
  - Gets trigger with ownership check
  - If schedule fields changed: re-runs AI schedule generation, recalculates next_execution
  - If exact_date execute_at changed: validates future, resets executed=False
  - Updates fields, sets updated_at

delete_trigger(db_session, trigger_id, task_id, user_id) -> bool
  - Gets trigger with ownership check
  - Deletes trigger

regenerate_webhook_token(db_session, trigger_id, task_id, user_id) -> tuple[TaskTrigger, str]
  - Gets trigger with ownership check
  - Validates trigger type is "webhook"
  - Generates new credentials (new token, keeps same webhook_id)
  - Returns (trigger, new_plaintext_token)
```

**Webhook Execution Methods:**

```
validate_webhook_token(db_session, webhook_id: str, provided_token: str) -> TaskTrigger
  - Looks up trigger by webhook_id
  - Checks trigger is enabled
  - Decrypts stored token via decrypt_field()
  - Compares with hmac.compare_digest (timing-safe)
  - Returns trigger if valid, raises WebhookTokenInvalidError if not

async fire_trigger(db_session, trigger: TaskTrigger, payload: str | None = None) -> None
  - Gets the linked InputTask
  - Validates task has a selected_agent_id (required for execution)
  - Validates agent has active environment
  - Builds final prompt: task.current_description + trigger.payload_template + payload
  - Calls InputTaskService.execute_task() with the combined prompt
  - Updates trigger: last_execution=now
  - For schedule triggers: recalculates next_execution
  - For exact_date triggers: sets executed=True
  - Logs the execution
```

**Scheduler Polling Methods (static, for APScheduler):**

```
async poll_due_triggers() -> None
  - Opens fresh DB session
  - Queries schedule triggers where: enabled=True, next_execution <= now
  - Queries exact_date triggers where: enabled=True, executed=False, execute_at <= now
  - For each due trigger: calls fire_trigger() in background task
  - Uses create_task_with_error_logging() for non-blocking execution
  - Catches and logs all errors per-trigger (one failure doesn't block others)
```

### Background Scheduler

**File:** `backend/app/services/task_trigger_scheduler.py`

**Pattern:** Follows `file_cleanup_scheduler.py` and `environment_suspension_scheduler.py`

```
from apscheduler.schedulers.background import BackgroundScheduler

scheduler = BackgroundScheduler()

def run_trigger_poll():
    """Synchronous wrapper for async poll_due_triggers"""
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(TaskTriggerService.poll_due_triggers())
    finally:
        loop.close()

def start_scheduler():                    # Named start_scheduler to match convention
    scheduler.add_job(
        run_trigger_poll,
        "interval",
        minutes=1,                        # Check every minute for due triggers
        id="task_trigger_poll",
        max_instances=1,                  # Prevent overlapping executions
    )
    scheduler.start()
    logger.info("Task trigger scheduler started (polls every 1 minute)")

def shutdown_scheduler():                 # Named shutdown_scheduler to match convention
    scheduler.shutdown()
```

**Convention note:** Functions are named `start_scheduler` / `shutdown_scheduler` matching the existing schedulers at `backend/app/services/file_cleanup_scheduler.py` and `backend/app/services/environment_suspension_scheduler.py`. They are aliased on import in `main.py` to `start_task_trigger_scheduler` / `shutdown_task_trigger_scheduler`.

**Registration in `backend/app/main.py`:**

Follows the exact pattern at `backend/app/main.py:57-71` (imports at module level, calls in startup/shutdown):

```python
# Add at module level (backend/app/main.py:57-64, alongside existing scheduler imports):
from app.services.task_trigger_scheduler import (
    start_scheduler as start_task_trigger_scheduler,
    shutdown_scheduler as shutdown_task_trigger_scheduler
)

# Add in on_startup() (backend/app/main.py:70-71, after existing scheduler starts):
start_task_trigger_scheduler()

# Add in on_shutdown() (backend/app/main.py:166-167, after existing scheduler shutdowns):
shutdown_task_trigger_scheduler()
```

**Note:** Export names should follow the convention used by existing schedulers — `start_scheduler` and `shutdown_scheduler` as function names, aliased on import to avoid collisions. See `file_cleanup_scheduler` and `environment_suspension_scheduler` imports at `main.py:57-64` for the exact pattern.

### Prompt Assembly on Trigger Fire

When a trigger fires, the prompt sent to the agent is assembled as follows:

```
Base:     task.current_description        (always present)
+
Template: trigger.payload_template         (if set on trigger — static context)
+
Dynamic:  webhook request body             (webhook only — dynamic payload from caller)
```

**Format:**

```
{task.current_description}

---
Trigger: {trigger.name}
{trigger.payload_template if set}

{webhook_payload if webhook trigger and payload provided}
```

This way:
- Schedule triggers append only the static `payload_template` (e.g., "Focus on error rates and latency metrics")
- Exact date triggers append only the static `payload_template` (e.g., "Contract expires in 30 days, check alternatives")
- Webhook triggers append both `payload_template` AND the dynamic webhook request body (e.g., GitHub PR payload JSON)

## Frontend Implementation

### Trigger Management Modal

**File:** `frontend/src/components/Tasks/Triggers/TriggerManagementModal.tsx`

**Purpose:** Full CRUD interface for task triggers, opened from the task detail page header.

**Modal Structure:**
```
┌────────────────────────────────────────────────────────────────┐
│ Task Triggers                                            [X]  │
├────────────────────────────────────────────────────────────────┤
│                                                                │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │ ● Daily metrics check          Schedule    [On]  [···]  │  │
│  │   Every weekday at 7:00 AM CET                          │  │
│  │   Next: Mon, Jan 27, 2026 at 7:00 AM                    │  │
│  └──────────────────────────────────────────────────────────┘  │
│                                                                │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │ ● Contract expiry check        Exact Date  [On]  [···]  │  │
│  │   Mar 1, 2026 at 9:00 AM CET                            │  │
│  └──────────────────────────────────────────────────────────┘  │
│                                                                │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │ ● PR review webhook            Webhook     [On]  [···]  │  │
│  │   Token: a1b2c3d4...                                    │  │
│  │   URL: https://app.example.com/api/v1/hooks/xYz9...     │  │
│  │   [Copy URL]                                            │  │
│  └──────────────────────────────────────────────────────────┘  │
│                                                                │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │  [+ Schedule]  [+ Exact Date]  [+ Webhook]              │  │
│  └──────────────────────────────────────────────────────────┘  │
│                                                                │
└────────────────────────────────────────────────────────────────┘
```

**Components:**

```
TriggerManagementModal.tsx       — Main modal with trigger list and add buttons
├── TriggerCard.tsx              — Individual trigger display card with enable toggle and menu
├── AddScheduleTriggerForm.tsx   — Form: name, NL schedule input, timezone, payload template
├── AddExactDateTriggerForm.tsx  — Form: name, date/time picker, timezone, payload template
├── AddWebhookTriggerForm.tsx    — Form: name, payload template (token auto-generated)
└── WebhookTokenDisplay.tsx      — One-time token display with copy button + warning
```

**Directory:** `frontend/src/components/Tasks/Triggers/`

### Task Detail Page Header Integration

**File:** `frontend/src/routes/_layout/task/$taskId.tsx`

Add a "Triggers" button to the header (next to "Edit Task" button, before the agent selector):

```
Button: icon=Zap (from lucide-react), label="Triggers"
  - Shows trigger count badge if triggers exist (e.g., "Triggers (3)")
  - onClick: opens TriggerManagementModal
  - State: triggerModalOpen (boolean)
```

**Placement in header (between Edit Task and agent selector):**
```
[← Back]  Task Refinement  ...  [Edit Task] [⚡ Triggers (2)] [🤖 Agent] [⋮]
```

### Add Schedule Trigger Form

**File:** `frontend/src/components/Tasks/Triggers/AddScheduleTriggerForm.tsx`

**Fields:**
- `name`: text input (required, placeholder: "e.g., Daily morning check")
- `schedule_input`: text input for NL schedule (required, placeholder: "e.g., every workday at 7 AM")
- `payload_template`: textarea (optional, placeholder: "Additional context for the agent...")
- `timezone`: auto-detected from browser (`Intl.DateTimeFormat().resolvedOptions().timeZone`), displayed read-only

**Behavior:**
- On submit: calls `POST /tasks/{taskId}/triggers/schedule`
- Backend runs AI schedule generation and returns the created trigger with `schedule_description` and `next_execution`
- On success: shows trigger in list with description and next execution time
- On error (AI can't parse): shows error message from backend

**Reuse:** This follows the same pattern as `SmartScheduler.tsx` in the agent config, but creates a task trigger instead of an agent schedule.

### Add Exact Date Trigger Form

**File:** `frontend/src/components/Tasks/Triggers/AddExactDateTriggerForm.tsx`

**Fields:**
- `name`: text input (required)
- `execute_at`: date and time picker (required, must be in future)
- `payload_template`: textarea (optional)
- `timezone`: auto-detected, displayed read-only

**Behavior:**
- On submit: calls `POST /tasks/{taskId}/triggers/exact-date`
- Validation: date must be in the future (client-side + server-side)
- On success: shows trigger in list with scheduled date

### Add Webhook Trigger Form

**File:** `frontend/src/components/Tasks/Triggers/AddWebhookTriggerForm.tsx`

**Fields:**
- `name`: text input (required, placeholder: "e.g., GitHub PR webhook")
- `payload_template`: textarea (optional, placeholder: "Static context prepended to webhook payload...")

**Behavior:**
- On submit: calls `POST /tasks/{taskId}/triggers/webhook`
- Response includes full `webhook_token` (one-time) and `webhook_url`
- Shows `WebhookTokenDisplay` component with:
  - Warning: "Copy this token now. It won't be shown again."
  - Full token with copy-to-clipboard button
  - Full webhook URL with copy-to-clipboard button
  - Example curl command with copy button

### Trigger Card Component

**File:** `frontend/src/components/Tasks/Triggers/TriggerCard.tsx`

**Display per type:**

Schedule:
- Icon: Clock (lucide-react)
- Badge: "Schedule" (blue)
- Info: schedule_description, "Next: {formatted next_execution}"
- Toggle: enabled/disabled

Exact Date:
- Icon: CalendarClock (lucide-react)
- Badge: "Exact Date" (amber)
- Info: formatted execute_at, "Executed" badge if executed=true
- Toggle: enabled/disabled

Webhook:
- Icon: Webhook (lucide-react)
- Badge: "Webhook" (green)
- Info: token prefix display, URL with copy button
- Toggle: enabled/disabled

**Actions menu (···):**
- Edit trigger → opens edit form
- For webhook: "Regenerate Token" → confirmation dialog → calls regenerate endpoint → shows new token once
- Delete trigger → confirmation dialog

### State Management (React Query)

**Query Keys:**
- `["task-triggers", taskId]` — list all triggers for a task

**Mutations:**
- `createScheduleTrigger` → `POST /tasks/{taskId}/triggers/schedule`
- `createExactDateTrigger` → `POST /tasks/{taskId}/triggers/exact-date`
- `createWebhookTrigger` → `POST /tasks/{taskId}/triggers/webhook`
- `updateTrigger` → `PATCH /tasks/{taskId}/triggers/{triggerId}`
- `deleteTrigger` → `DELETE /tasks/{taskId}/triggers/{triggerId}`
- `regenerateToken` → `POST /tasks/{taskId}/triggers/{triggerId}/regenerate-token`

**Cache Invalidation:**
- All mutations invalidate `["task-triggers", taskId]`

### API Client

A manual API client with TypeScript types is provided at `frontend/src/components/Tasks/Triggers/triggerApi.ts`. This uses the OpenAPI `request` function directly and works without running `make gen-client`.

After deploying the backend, optionally regenerate the full auto-generated client:
```bash
source ./backend/.venv/bin/activate && make gen-client
```

This will generate:
- `TaskTriggersService` (or methods on `TasksService`) in `sdk.gen.ts`
- TypeScript types in `types.gen.ts`

At that point, `triggerApi.ts` can be replaced with imports from `@/client`.

## Database Migrations

### Migration File

**File:** `backend/app/alembic/versions/v2q3r4s5t6u7_add_task_trigger_table.py`

**Creates:**
- `task_trigger` table with all columns as specified in the data model
- Indexes:
  - `ix_task_trigger_task_id` on `(task_id)`
  - `ix_task_trigger_schedule_poll` on `(type, enabled, next_execution)` — for efficient scheduler polling
  - `ix_task_trigger_exact_date_poll` on `(type, enabled, execute_at, executed)` — for date trigger polling
  - `ix_task_trigger_webhook_id` UNIQUE on `(webhook_id)` — for webhook URL lookup
  - `ix_task_trigger_owner_id` on `(owner_id)`

**Foreign Keys:**
- `task_id` → `input_task.id` ON DELETE CASCADE
- `owner_id` → `user.id` ON DELETE CASCADE

**Downgrade:**
- Drop `task_trigger` table and all indexes

## Error Handling & Edge Cases

### Trigger Creation Errors
- **AI schedule parsing failure**: Return AI error message (e.g., "Cannot extract schedule: too vague")
- **Schedule too frequent**: Return "Minimum interval is 30 minutes" (enforced by AI function + backend validation)
- **Exact date in past**: Return "Execution date must be in the future"
- **Task has no agent**: Trigger creation succeeds, but execution will fail with appropriate error logged
- **Webhook ID collision**: Retry with new random slug (extremely unlikely with 11-char URL-safe tokens)

### Trigger Execution Errors
- **Task has no selected agent**: Log error, skip execution, do not disable trigger
- **Agent has no active environment**: Log error, skip execution; environment auto-start will be attempted by the session creation flow
- **Task execution fails**: Log error with trigger_id and task_id; do not disable trigger (transient failures should be retried on next schedule)
- **Webhook with invalid token**: Return 401 with generic "Invalid or expired token" message (no information leakage)
- **Webhook with disabled trigger**: Return 404 (trigger not found) to avoid confirming existence
- **Webhook payload too large**: Return 413 with "Payload exceeds maximum size of 64KB"

### Concurrent Execution
- The scheduler uses `max_instances=1` on APScheduler to prevent overlapping poll cycles
- Each trigger is fired independently; one trigger's failure doesn't block others
- Multiple triggers firing simultaneously for the same task create multiple sessions (this is correct behavior — same as manual "Run Again")

### Trigger Lifecycle
- Deleting a task cascades to delete all triggers (FK CASCADE)
- Disabling a trigger (enabled=false) prevents it from firing but preserves configuration
- Exact-date triggers with `executed=True` remain in the list for audit but won't fire again
- Re-enabling an expired exact-date trigger requires updating `execute_at` to a future date

## UI/UX Considerations

### Trigger Count Badge
- The "Triggers" button in the task header shows count of active (enabled) triggers
- If no triggers exist, button shows just "Triggers" without count
- If triggers exist but all disabled, shows count in muted style

### Webhook Token Display
- Full token shown only once on creation (and on regeneration)
- Warning banner: "Save this token now — it can't be retrieved later"
- Copy-to-clipboard for both token and full URL
- Example `curl` command generated for easy testing:
  ```
  curl -X POST https://your-domain/api/v1/hooks/{webhook_id} \
    -H "Authorization: Bearer {token}" \
    -H "Content-Type: application/json" \
    -d '{"key": "value"}'
  ```

### Empty State
- When no triggers exist: "No triggers configured. Add a trigger to automate this task."
- Three buttons to add each type with brief descriptions

### Status Indicators
- Schedule triggers: Show next execution in relative time ("in 3 hours") + absolute time
- Exact date triggers: Show date with "Pending" or "Executed" status
- Webhook triggers: Show "Active" or "Disabled" with last invocation time (if available from `last_execution`)

### Payload Template Guidance
- Help text: "Optional context appended to the task description when this trigger fires"
- For webhooks: "The webhook request body will be appended after this template"
- Placeholder examples per type:
  - Schedule: "Focus on error rates and latency metrics from the past 24 hours"
  - Exact date: "Contract #1234 expires in 30 days. Research alternative providers."
  - Webhook: "Review the following GitHub event:"

## Integration Points

### With InputTaskService
- `TaskTriggerService.fire_trigger()` calls `InputTaskService.execute_task()` to create sessions
- The combined prompt (task description + payload) is passed as the `message_to_send` parameter
- Task status sync continues to work via existing event handlers

### With AgentSchedulerService
- Reuses `AgentSchedulerService.calculate_next_execution()` for schedule triggers
- Reuses `AgentSchedulerService.convert_local_cron_to_utc()` for timezone conversion

### With AIFunctionsService
- Reuses `AIFunctionsService.generate_schedule()` for natural language → CRON conversion
- Same validation rules (30-minute minimum interval)

### With Security Module
- Uses `encrypt_field()` / `decrypt_field()` from `backend/app/core/security.py` for webhook tokens

### With APScheduler Infrastructure
- Follows the same pattern as `file_cleanup_scheduler.py` and `environment_suspension_scheduler.py`
- Registered in `main.py` startup/shutdown events

### API Client Regeneration
After backend changes:
```bash
source ./backend/.venv/bin/activate && make gen-client
```

## Future Enhancements (Out of Scope)

- **Trigger execution history/audit log**: `TaskTriggerExecution` table tracking every fire event with status, timing, and session_id
- **Webhook payload transformation**: JSONPath or template-based extraction from webhook body before appending to prompt
- **Trigger conditions/filters**: Only fire if webhook payload matches certain conditions (e.g., GitHub event type = "pull_request")
- **Trigger chaining**: One trigger's output triggers another task
- **Webhook signature verification**: Validate GitHub/Slack webhook signatures (HMAC-SHA256)
- **Rate limiting on webhook endpoint**: Per webhook_id rate limits
- **Retry policies**: Configurable retry on trigger execution failure
- **Notification on trigger failure**: Alert user when triggers fail to execute
- **Bulk trigger management**: Enable/disable all triggers for a task
- **Trigger templates**: Pre-built trigger configurations for common use cases (GitHub PR review, daily standup, etc.)

## Summary Checklist

### Backend Tasks
- [x] Create `backend/app/models/task_trigger.py` with `TaskTrigger` model and all schema classes
- [x] Export models from `backend/app/models/__init__.py`
- [x] Create Alembic migration for `task_trigger` table with indexes
- [x] Create `backend/app/services/task_trigger_service.py` with all CRUD and execution methods
- [x] Create `backend/app/services/task_trigger_scheduler.py` with APScheduler polling job
- [x] Create `backend/app/api/routes/task_triggers.py` with trigger CRUD endpoints
- [x] Create `backend/app/api/routes/webhooks.py` with public webhook execution endpoint
- [x] Register routers in `backend/app/api/main.py`
- [x] Register scheduler startup/shutdown in `backend/app/main.py`
- [x] Add webhook payload size validation middleware or in-route check

### Frontend Tasks
- [x] Create `frontend/src/components/Tasks/Triggers/` directory
- [x] Create `TriggerManagementModal.tsx` — main modal with trigger list
- [x] Create `TriggerCard.tsx` — individual trigger display with enable toggle and actions menu
- [x] Create `AddScheduleTriggerForm.tsx` — schedule creation form with NL input
- [x] Create `AddExactDateTriggerForm.tsx` — date/time picker form
- [x] Create `AddWebhookTriggerForm.tsx` — webhook creation form
- [x] Create `WebhookTokenDisplay.tsx` — one-time token display with copy buttons
- [x] Add "Triggers" button to task detail page header (`$taskId.tsx`)
- [x] Add trigger count badge to the header button
- [ ] Regenerate frontend API client (`make gen-client`) — run after backend is deployed

### Post-Implementation Steps
- [ ] Apply migration: `make migrate` (or `docker compose exec backend alembic upgrade head`)
- [ ] Regenerate frontend client: `source ./backend/.venv/bin/activate && make gen-client`
- [ ] Optionally replace `triggerApi.ts` manual client with auto-generated `TaskTriggersService`

### Testing & Validation
- [ ] Verify schedule trigger creation with AI NL conversion
- [ ] Verify exact-date trigger creation with future date validation
- [ ] Verify webhook trigger creation returns token only once
- [ ] Verify webhook URL responds correctly with valid token
- [ ] Verify webhook URL returns 401 with invalid token
- [ ] Verify webhook URL returns 404 for disabled triggers
- [ ] Verify scheduler polls and fires due schedule triggers
- [ ] Verify scheduler polls and fires due exact-date triggers
- [ ] Verify exact-date triggers only fire once (executed=True)
- [ ] Verify payload assembly: task description + template + dynamic payload
- [ ] Verify task execution creates sessions correctly when trigger fires
- [ ] Verify cascade deletion: deleting task removes all triggers
- [ ] Verify token regeneration invalidates old token
- [ ] Verify trigger enable/disable toggle works
- [ ] Verify CRUD operations respect ownership

## File Locations Reference

**Backend - Models:**
- `backend/app/models/task_trigger.py` (new)
- `backend/app/models/__init__.py` (update exports)

**Backend - Routes:**
- `backend/app/api/routes/task_triggers.py` (new)
- `backend/app/api/routes/webhooks.py` (new)
- `backend/app/api/main.py` (register routers)

**Backend - Services:**
- `backend/app/services/task_trigger_service.py` (new)
- `backend/app/services/task_trigger_scheduler.py` (new)

**Backend - Reused Services:**
- `backend/app/services/agent_scheduler_service.py` (CRON calculation, timezone conversion)
- `backend/app/services/ai_functions_service.py` (NL schedule generation)
- `backend/app/services/input_task_service.py` (task execution)
- `backend/app/core/security.py` (encrypt_field, decrypt_field)

**Backend - Migration:**
- `backend/app/alembic/versions/v2q3r4s5t6u7_add_task_trigger_table.py`

**Backend - Startup:**
- `backend/app/main.py` (register scheduler)

**Frontend - Components:**
- `frontend/src/components/Tasks/Triggers/triggerApi.ts` — manual API service + types (use until gen-client is run)
- `frontend/src/components/Tasks/Triggers/TriggerManagementModal.tsx`
- `frontend/src/components/Tasks/Triggers/TriggerCard.tsx`
- `frontend/src/components/Tasks/Triggers/AddScheduleTriggerForm.tsx`
- `frontend/src/components/Tasks/Triggers/AddExactDateTriggerForm.tsx`
- `frontend/src/components/Tasks/Triggers/AddWebhookTriggerForm.tsx`
- `frontend/src/components/Tasks/Triggers/WebhookTokenDisplay.tsx`

**Frontend - Pages:**
- `frontend/src/routes/_layout/task/$taskId.tsx` (updated header with Triggers button + modal)

**Frontend - Client (auto-generated, after running `make gen-client`):**
- `frontend/src/client/sdk.gen.ts`
- `frontend/src/client/types.gen.ts`

---

**Document Version:** 1.1
**Last Updated:** 2026-01-27
**Status:** Implemented — pending migration (`make migrate`) and client regeneration (`make gen-client`)
