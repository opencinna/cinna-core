# Task Triggers

## Purpose

Enable automatic and event-driven task execution through configurable triggers. Each trigger can carry a payload appended to the task description at fire time, allowing dynamic context injection. A single task can have multiple triggers of different types running concurrently.

## Core Concepts

- **Task Trigger**: A rule attached to an InputTask that fires execution automatically under certain conditions.
- **Schedule Trigger**: Recurring CRON-based execution defined via natural language (e.g., "every weekday at 7 AM"). Reuses the AI smart schedule system from agent schedulers.
- **Exact Date Trigger**: One-time execution at a specific datetime. Fires once, then marked `executed`.
- **Webhook Trigger**: HTTP-triggered execution via a unique public URL and encrypted secret token.
- **Payload Template**: Static text appended to the task description when the trigger fires.
- **Webhook Payload**: Dynamic request body from the external caller, appended after the payload template (webhook triggers only).
- **Webhook ID**: Short public URL slug used to identify the webhook endpoint; separate from the secret token.
- **Token Prefix**: First 8 characters of the webhook token, safe to display in UI (e.g., `a1b2c3d4...`).

## User Stories / Flows

### Creating a Schedule Trigger

1. User opens the Triggers modal from the task detail page header
2. Clicks "+ Schedule"
3. Enters a name and natural language schedule (e.g., "every workday at 7 AM")
4. Browser timezone detected automatically
5. Optionally adds a payload template for static context
6. Submits → backend runs AI schedule conversion → creates trigger with CRON string and next execution time
7. Trigger card shows human-readable description (e.g., "Every weekday at 7:00 AM CET") and next execution time

### Creating an Exact Date Trigger

1. User clicks "+ Exact Date"
2. Enters name and selects date/time using a date picker
3. Optionally adds payload template
4. Submits → backend validates date is in future → creates trigger
5. Trigger fires once at the specified time, then shows "Executed" status in UI

### Creating a Webhook Trigger

1. User clicks "+ Webhook"
2. Enters name and optional payload template
3. Submits → backend generates webhook URL and secret token server-side
4. **Token is shown only once** — user must copy immediately
5. Full webhook URL and example curl command shown for easy integration setup
6. Token can be regenerated later (invalidates old token)

### Trigger Execution

1. APScheduler polls every minute for due schedule/exact-date triggers
2. For webhooks: external system calls `POST /api/v1/hooks/{webhook_id}` with Bearer token
3. System validates token (timing-safe) and fires the trigger
4. Prompt assembled: task description + payload template + dynamic webhook body (if webhook)
5. `InputTaskService.execute_task()` creates a new session with assembled prompt
6. Task status syncs via existing session event handlers

### Managing Triggers

- Enable/disable toggle per trigger (disabled triggers don't fire; webhook returns 404)
- Edit trigger (updates schedule or exact date, recalculates next execution)
- Delete trigger (removed immediately)
- Regenerate webhook token (old token immediately invalidated, new token shown once)

## Business Rules

### Trigger Type Rules

- **Schedule**: CRON expression stored in UTC; minimum 30-minute interval enforced by AI function and backend validation; `next_execution` pre-calculated after each fire
- **Exact Date**: `execute_at` must be in the future at creation; set `executed=True` after firing; re-enabling requires updating `execute_at` to a future date
- **Webhook**: Token generated server-side with cryptographically random bytes; stored encrypted; only prefix shown in UI after creation

### Token Security Rules

- Full token returned only once: on creation or on regeneration
- Token stored encrypted using Fernet symmetric encryption (same `encrypt_field()` pattern as credentials)
- Webhook endpoint validates token with `hmac.compare_digest` (timing-safe, prevents timing attacks)
- Disabled triggers return 404 on webhook call (avoids confirming existence to attackers)
- Invalid token returns 401 with generic message (no information leakage)

### Payload Assembly

```
{task.current_description}

---
Trigger: {trigger.name}
{trigger.payload_template if set}

{webhook_request_body if webhook trigger and body provided}
```

- Schedule and exact-date triggers: task description + payload template only
- Webhook triggers: task description + payload template + dynamic request body

### Execution Rules

- Trigger fires regardless of current task status (creates additional sessions, same as manual "Run Again")
- Task must have `selected_agent_id`; if not, execution logged and skipped (trigger not disabled)
- Agent environment auto-start attempted by session creation flow
- Multiple triggers firing simultaneously for the same task create multiple sessions (correct behavior)
- Each trigger failure is logged independently; one failure does not block others

### Trigger Lifecycle Rules

- Deleting a task cascades to delete all its triggers (FK CASCADE)
- Disabling a trigger preserves configuration but prevents firing
- Exact-date triggers with `executed=True` remain in list for audit but won't fire again
- Schedule triggers update `last_execution` and recalculate `next_execution` after each fire
- Webhook triggers update `last_execution` on each successful call

## Architecture Overview

```
Frontend (Modal) → Backend API → TaskTriggerService → Database

Scheduled execution:
  APScheduler (1-min poll) → TaskTriggerService.poll_due_triggers()
                               → fire_trigger()
                               → InputTaskService.execute_task()

Webhook execution:
  External HTTP POST /hooks/{id} → token validation → fire_trigger()
                                                     → InputTaskService.execute_task()
```

## Integration Points

- **Input Tasks**: All triggers fire via `InputTaskService.execute_task()` — see [Input Tasks](input_tasks.md)
- **Agent Schedulers**: Reuses `AgentSchedulerService.calculate_next_execution()` and `convert_local_cron_to_utc()` for CRON handling — see [Agent Schedulers](../../agents/agent_schedulers/agent_schedulers.md)
- **AI Functions**: Reuses `AIFunctionsService.generate_schedule()` for natural language → CRON conversion
- **Security Module**: Uses `encrypt_field()` / `decrypt_field()` from `backend/app/core/security.py` for webhook token encryption
- **APScheduler Infrastructure**: Follows same pattern as `file_cleanup_scheduler.py` and `environment_suspension_scheduler.py`
