# Agent Environment Critical State — Technical Reference

## File Locations

### Backend — Models

- `backend/app/models/environments/environment.py` — `AgentEnvironment.critical_state` (bool, `server_default=false`), `critical_cause` (str|None, String(64)), `critical_since` (datetime|None); `AgentEnvironmentPublic` exposes all three; `AdminAgentEnvironmentPublic` inherits them via `AgentEnvironmentPublic`. Partial index `ix_agent_environment_critical_state` (WHERE `critical_state = true`).
- `backend/app/models/environments/agent_env_action_log.py` — `AgentEnvActionLog` (table model), `AgentEnvActionLogPublic`, `AgentEnvActionLogsPublic`
- `backend/app/models/__init__.py` — re-exports `AgentEnvActionLog`, `AgentEnvActionLogPublic`, `AgentEnvActionLogsPublic`

### Backend — Service Layer

- `backend/app/services/environments/agent_env_action_log_service.py` — `AgentEnvActionLogService` (record, list_for_environment, count_for_environment, latest_critical)
- `backend/app/services/environments/environment_lifecycle.py` — `_critical_warned_env_ids` module-level set; `_container_alive_else_raise`; `_emit_critical_state_event`; `_enter_critical_state`; `_clear_critical_state`; wiring in `_setup_new_container` and `_sync_dynamic_data`
- `backend/app/services/environments/adapters/base.py` — `EnvironmentAdapter.is_container_running()` (concrete default: `get_status() in {"running", "starting"}`)
- `backend/app/services/environments/environment_status_scheduler.py` — clears `critical_state` when health check flips env to `status="error"`
- `backend/app/services/agents/agent_schedule_scheduler.py` — `_poll_due_schedules` CRON gate; `_skip_schedule_for_critical_env` helper
- `backend/app/services/agents/agent_scheduler_service.py` — `execute_now` returns HTTP 400 when `environment.critical_state`

### Backend — Notifications

- `backend/app/services/notifications/notification_catalog.py` — `NotificationType.ENVIRONMENT_CRITICAL` (`"environment_critical"`); catalog entry with `label="Environment needs attention"`, `default_email_enabled=True`, `email_template="environment_critical.html"`, `dedup_scope="environment_id"`
- `backend/app/email-templates/build/environment_critical.html` — compiled HTML template

### Backend — Realtime Events

- `backend/app/models/events/event.py` — `EventType.ENVIRONMENT_CRITICAL_STATE_CHANGED = "environment_critical_state_changed"`

### Backend — API Routes

- `backend/app/api/routes/environments.py` — `GET /{environment_id}/action-logs`

### Backend — Migration

- `backend/app/alembic/versions/3974f541ab0b_agent_env_critical_state_action_log.py` — `down_revision = '6e6af979678c'`

### Frontend

- `frontend/src/components/Environments/EnvironmentCriticalBadge.tsx` — amber block component
- `frontend/src/components/Environments/EnvironmentActionLogsModal.tsx` — action-log modal
- `frontend/src/components/Environments/EnvironmentCard.tsx` — wires both; local state `actionLogsOpen`
- `frontend/src/components/Agents/AgentEnvironmentsTab.tsx` — subscribes to `ENVIRONMENT_CRITICAL_STATE_CHANGED`; invalidates `["env-action-logs", id]`
- `frontend/src/client/types.gen.ts` — `AgentEnvironmentPublic.critical_state`, `critical_cause`, `critical_since`; `AgentEnvActionLogPublic`; `AgentEnvActionLogsPublic`

---

## Data Models

### `AgentEnvironment` — modified columns

Three columns added to the existing table:

| Column | Type | Constraint | Description |
|--------|------|-----------|-------------|
| `critical_state` | `BOOLEAN` | `NOT NULL`, `server_default=false` | `true` when container is running but a provisioning step failed |
| `critical_cause` | `VARCHAR(64)` | nullable | Cause code: `package_install_failed` / `credential_sync_failed` / `file_sync_failed` / `provisioning_failed` |
| `critical_since` | `TIMESTAMPTZ` | nullable | When critical state was entered; stamped only on the `false → true` transition |

Partial index: `ix_agent_environment_critical_state` on `(critical_state)` WHERE `critical_state = true`. Used by the scheduler's hot-path check.

All three fields are exposed on `AgentEnvironmentPublic` (and inherited by `AdminAgentEnvironmentPublic`). They are persisted columns, so they populate automatically via `model_validate(environment)` — no transient attachment required.

### `AgentEnvActionLog` — new table

**Table name:** `agent_env_action_log`

Append-only. Mirrors `AgentScheduleLog`'s shape (free-string `status`, indexed by parent + `executed_at`). Never updated; deleted only via CASCADE.

| Column | Type | Constraint | Description |
|--------|------|-----------|-------------|
| `id` | `UUID` | PK | Row identifier |
| `environment_id` | `UUID` | FK → `agent_environment.id` (CASCADE), indexed | Parent environment |
| `agent_id` | `UUID` | FK → `agent.id` (CASCADE), indexed | Denormalized agent ref for owner lookups |
| `action` | `VARCHAR(48)` | NOT NULL | One of: `rebuild`, `setup_after_rebuild`, `package_install`, `system_package_install`, `credential_sync`, `file_sync`, `cron_skipped`, `provisioning` |
| `status` | `VARCHAR(24)` | NOT NULL | `success` / `error` / `skipped` (free string, same convention as `AgentScheduleLog`) |
| `cause` | `VARCHAR(64)` | nullable | The `critical_cause` code when `status="error"` — links the action-log row to the env flag |
| `summary` | `VARCHAR(512)` | nullable | Short human-readable cause line; safe to surface in lists; seeds the email brief |
| `detail` | `TEXT` | nullable | Full, untruncated error/output text (uv resolver output, exception strings). Source for "Show details". Must never contain secrets. |
| `executed_at` | `TIMESTAMPTZ` | NOT NULL, indexed | When the action happened (UTC) |

**Indexes:** `ix_agent_env_action_log_environment_id`, `ix_agent_env_action_log_agent_id`, `ix_agent_env_action_log_executed_at`

**Response schemas:**
- `AgentEnvActionLogPublic` — all fields above
- `AgentEnvActionLogsPublic` — `{ data: list[AgentEnvActionLogPublic], count: int }`

---

## Migration

**Revision:** `3974f541ab0b`
**Down revision:** `6e6af979678c`

**Upgrade** creates the `agent_env_action_log` table (columns + three indexes), adds three columns to `agent_environment`, and creates the partial index on `critical_state`.

**Downgrade** drops all of the above in reverse order.

---

## Service Layer

### `AgentEnvActionLogService`

Location: `backend/app/services/environments/agent_env_action_log_service.py`

| Method | Signature | Description |
|--------|-----------|-------------|
| `record` | `record(db_session, *, environment_id, agent_id, action, status, cause=None, summary=None, detail=None) -> AgentEnvActionLog` | Add + commit + refresh an immutable row. Called at all sites with a defensive try/except — a logging failure must never abort the lifecycle or scheduler poll. |
| `list_for_environment` | `list_for_environment(db_session, environment_id, limit=50) -> list[AgentEnvActionLog]` | Recent rows, `executed_at DESC`. |
| `count_for_environment` | `count_for_environment(db_session, environment_id) -> int` | Total row count (for list response). |
| `latest_critical` | `latest_critical(db_session, environment_id) -> AgentEnvActionLog | None` | Most recent `status="error"` row. |

### `EnvironmentLifecycleManager` — new helpers

Location: `backend/app/services/environments/environment_lifecycle.py`

**Module-level state:**
```python
_critical_warned_env_ids: set[str] = set()
```
Process-local set of environment IDs for which a critical-state email has been dispatched. Mirrors `model_discovery_service._warned_env_ids`. Cleared on `_clear_critical_state`; reset on process restart (at most one extra email after a deploy, consistent with `model_deprecated`).

**`_container_alive_else_raise(adapter, original_exc) -> bool`**

Probes the container after a setup step exception. Returns `True` if alive (caller enters critical state). Re-raises `original_exc` if the container is gone or the probe itself raises — failing safe toward the offline `status="error"` path.

```python
alive = await adapter.is_container_running()  # get_status() in {"running", "starting"}
if not alive:
    raise original_exc
return True
```

**`_enter_critical_state(db_session, environment, agent, *, cause, summary, detail=None, action="provisioning") -> None`**

1. Calls `AgentEnvActionLogService.record(status="error", ...)` (best-effort)
2. Sets `environment.critical_state = True`, `critical_cause = cause`; stamps `critical_since` only on the `false → true` transition
3. Sets `status_message = f"Running, but setup incomplete: {summary}"` — status stays `"running"`
4. Commits
5. Emits `ENVIRONMENT_CRITICAL_STATE_CHANGED(critical_state=True, cause, summary)`
6. Dispatches `environment_critical` notification (gated by `_critical_warned_env_ids`)

**`_clear_critical_state(db_session, environment, agent, *, action="provisioning", summary=None, detail=None) -> None`**

No-op if `not environment.critical_state`. Otherwise:
1. Calls `AgentEnvActionLogService.record(status="success", ...)` (best-effort)
2. Sets `critical_state = False`, `critical_cause = None`, `critical_since = None`
3. Sets `status_message = "Environment is running"`
4. Commits
5. Discards env from `_critical_warned_env_ids`
6. Emits `ENVIRONMENT_CRITICAL_STATE_CHANGED(critical_state=False, cause=None)`

**Wiring points in the lifecycle:**

- `_setup_new_container`: wraps `install_custom_packages()` and `install_system_packages()` — each uses `_container_alive_else_raise` in the `except` branch; on `True`, calls `_enter_critical_state` with `cause="package_install_failed"` and the appropriate `action` tag
- `_sync_dynamic_data`: wraps `set_credentials()` — on alive container, calls `_enter_critical_state(cause="credential_sync_failed", action="credential_sync")`; the detail is a sanitized `f"{type(e).__name__}: {e}"` string
- `start_environment`, `activate_suspended_environment`, `rebuild_environment`: before calling setup/sync, snapshot `was_critical_before`; after successful completion, call `_clear_critical_state` only if something actually entered critical during this run (tracked via a temporary `_tracked_enter` monkey-patch on the instance). This avoids clearing a pre-existing critical flag that was not touched by the current operation.

### `EnvironmentAdapter.is_container_running()`

Location: `backend/app/services/environments/adapters/base.py`

Concrete default implementation (adapters may override for a cheaper probe):
```python
async def is_container_running(self) -> bool:
    return await self.get_status() in {"running", "starting"}
```

### `environment_status_scheduler` — critical-state clear

Location: `backend/app/services/environments/environment_status_scheduler.py`

When the health check flips an env to `status="error"` (container confirmed down), the scheduler also clears critical state in the same commit:
```python
if env.critical_state:
    env.critical_state = False
    env.critical_cause = None
    env.critical_since = None
    _critical_warned_env_ids.discard(str(env.id))
    critical_cleared = True
```
If cleared, it emits a second `ENVIRONMENT_CRITICAL_STATE_CHANGED(critical_state=False)` event alongside the `ENVIRONMENT_STATUS_CHANGED` event.

### CRON Gating — `agent_schedule_scheduler`

Location: `backend/app/services/agents/agent_schedule_scheduler.py`

In `_poll_due_schedules`, after resolving `agent`, the active environment is fetched and checked:
```python
environment = AgentSchedulerService.get_active_environment(db_session, agent.id)
if environment is not None and environment.critical_state:
    await _skip_schedule_for_critical_env(...)
    continue
```

`_skip_schedule_for_critical_env` performs all side effects in best-effort try/except blocks:
1. `AgentSchedulerService.create_log(status="skipped", error_message=reason)` — surfaces in the schedule execution-logs modal
2. `AgentEnvActionLogService.record(action="cron_skipped", status="skipped", ...)` — surfaces in the env action-log modal
3. `SystemNotificationService.notify(NotificationType.ENVIRONMENT_CRITICAL, ...)` — throttled by `environment_id` dedup
4. `AgentSchedulerService.update_execution_time(...)` — advances `next_execution` unconditionally (to break the per-minute skip loop); `last_execution` is NOT updated (that would imply success)

### Manual "Run now" block — `AgentSchedulerService.execute_now`

Location: `backend/app/services/agents/agent_scheduler_service.py`

```python
if environment.critical_state:
    raise ScheduleError(
        "Agent environment is in a critical state (the container is "
        "running but a setup step failed). Resolve the environment "
        "issue, then run the schedule again.",
        status_code=400,
    )
```

---

## Notifications

### `NotificationType.ENVIRONMENT_CRITICAL`

Location: `backend/app/services/notifications/notification_catalog.py`

| Field | Value |
|-------|-------|
| Enum value | `"environment_critical"` |
| Label | `"Environment needs attention"` |
| Description | `"Email me when one of my agent environments starts but a setup step fails, or a scheduled run is skipped because the environment is unstable."` |
| `default_email_enabled` | `True` |
| `email_template` | `"environment_critical.html"` |
| Subject lambda | `f"{PROJECT_NAME} — Action needed for {ctx.get('instance_name', 'your environment')}"` |
| `dedup_scope` | `"environment_id"` |

The shared `environment_id` dedup key means one notification type, one Settings toggle, and a unified 30-minute throttle across both the setup-failure path and the cron-skip path per environment.

**Email context keys passed at dispatch:**
- `project_name`, `agent_name`, `instance_name`, `environment_id`, `reason` (brief summary), `detail` (same as `reason` — full detail is NOT in the email), `link` (`{FRONTEND_HOST}/agents/{agent_id}`)

**Recipient:** `Agent.owner_id`

---

## Realtime Event

**Event type:** `ENVIRONMENT_CRITICAL_STATE_CHANGED` (`"environment_critical_state_changed"`)

**Payload:**
```json
{
  "environment_id": "<uuid>",
  "agent_id": "<uuid>",
  "instance_name": "<string>",
  "critical_state": true | false,
  "cause": "<string | null>",
  "summary": "<string | null>"
}
```

Emitted from both `_enter_critical_state` (via `_emit_critical_state_event`) and `_clear_critical_state`. Also emitted by `environment_status_scheduler` when offline supersedes critical.

---

## API Route

### `GET /api/v1/environments/{environment_id}/action-logs`

**Auth:** `CurrentUser`, owner-gated (resolves env → agent → asserts `agent.owner_id == current_user.id`; superuser allowed)

**Query parameters:**
- `limit` (int, default `50`, min `1`, max `200`)

**Response:** `AgentEnvActionLogsPublic`
```json
{
  "data": [AgentEnvActionLogPublic, ...],
  "count": <int>
}
```

**Error responses:**
- `404` — environment not found
- `403` — caller is not the owner (and not superuser)

Rows are newest-first (`executed_at DESC`). Write-only from the lifecycle and scheduler — no create/update endpoints.

---

## Frontend Components

### `EnvironmentCriticalBadge.tsx`

Location: `frontend/src/components/Environments/EnvironmentCriticalBadge.tsx`

Props:
- `environment: AgentEnvironmentPublic`
- `onShowDetails?: () => void` — optional. When omitted (e.g. read-only agent-user view), the "Show details" button is hidden (the underlying route would return 403).

Returns `null` when `!environment.critical_state`.

Renders an amber block using `bg-orange-100 text-orange-800 dark:bg-orange-900 dark:text-orange-200 border-orange-200` with `AlertTriangle` icon. Displays:
- "Action required" heading
- "The container is running, but setup did not finish. Your agent may not behave as expected until this is resolved."
- Cause-specific line derived from `critical_cause` via a `CAUSE_LABELS` lookup map
- "Show details" button (`onClick={onShowDetails}`) when prop is provided

Cause labels:
| `critical_cause` | Label |
|-----------------|-------|
| `package_install_failed` | "Custom package install failed." |
| `system_package_install_failed` | "System package install failed." |
| `file_sync_failed` | "Workspace file sync failed." |
| `credential_sync_failed` | "Credential sync failed." |
| `provisioning_failed` | "A provisioning step failed." |
| unknown / null | "A setup step did not finish." |

### `EnvironmentActionLogsModal.tsx`

Location: `frontend/src/components/Environments/EnvironmentActionLogsModal.tsx`

Props:
- `environmentId: string`
- `open: boolean`
- `onOpenChange: (open: boolean) => void`

React Query key: `["env-action-logs", environmentId]`

Fetches via `EnvironmentsService.getEnvironmentActionLogs({ environmentId })`, enabled only when `open && !!environmentId` (lazy loading).

Dialog is `max-w-2xl` with a `max-h-[60vh] overflow-y-auto` scrollable body.

Each `ActionLogRow` shows:
- `action` badge (monospace outline badge)
- Relative `executed_at` timestamp
- `ActionLogStatusBadge` (error=red, skipped=gray, success=green)
- Expand/collapse chevron when `summary` or `detail` is present

Expanded detail for error rows renders in `bg-red-50 border-red-200 text-red-700` with a monospace `<pre>` capped at `max-h-40`. Non-error detail uses neutral `bg-muted`.

### `EnvironmentCard.tsx` wiring

Location: `frontend/src/components/Environments/EnvironmentCard.tsx`

- Local state: `const [actionLogsOpen, setActionLogsOpen] = useState(false)`
- Renders `<EnvironmentCriticalBadge environment={environment} onShowDetails={() => setActionLogsOpen(true)} />` in the meta-badges area (below `ModelHealthBadge`)
- Renders `<EnvironmentActionLogsModal environmentId={environment.id} open={actionLogsOpen} onOpenChange={setActionLogsOpen} />` at the card root

The `EnvironmentStatusBadge` is unchanged — green stays green.

### `AgentEnvironmentsTab.tsx` event handling

Location: `frontend/src/components/Agents/AgentEnvironmentsTab.tsx`

```typescript
if (event.type === EventTypes.ENVIRONMENT_CRITICAL_STATE_CHANGED) {
  // Invalidate env list (critical_state field) and the action-log query.
  queryClient.invalidateQueries(...)
}
```

The tab also registers `ENVIRONMENT_CRITICAL_STATE_CHANGED` in its event subscription list alongside the existing `ENVIRONMENT_*` events.

---

## Schedule Execution Log — `skipped` Status

The `AgentScheduleLog` model uses a free-string `status` field. The `"skipped"` value is now a documented member of the set (alongside `"success"`, `"session_triggered"`, `"error"`). No migration required.

The schedule execution-logs modal in `AgentSchedulesCard.tsx` renders `"skipped"` with neutral/gray styling (no icon that implies success or failure).

---

## Error Handling and Edge Cases

- **Probe error defaults to offline.** If `is_container_running()` itself raises, `_container_alive_else_raise` re-raises the original exception — failing safe toward `status="error"` rather than masking a dead container as merely critical.
- **Action-log write failure is best-effort.** A `try/except` around every `AgentEnvActionLogService.record` call prevents log failures from aborting the lifecycle or scheduler. A `WARNING` is logged; the DB session is rolled back if a commit-level failure occurred.
- **Notification dispatch failure is best-effort.** The notify call is wrapped; exceptions are caught and logged at `DEBUG` level.
- **Multi-worker process-restart.** `_critical_warned_env_ids` is in-process memory. On restart it resets, which may produce at most one extra email per previously-critical environment. Consistent with the `model_deprecated` precedent; acceptable for the MVP.
- **Foreign (bundle consumer) installs.** Their materialised CRON schedules are ordinary rows; gating applies identically. No special handling.

*Last updated: 2026-06-20*
