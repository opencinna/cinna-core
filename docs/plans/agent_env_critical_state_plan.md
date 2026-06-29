# Agent-Environment Critical State + Action/Event Log + CRON Gating — Implementation Plan

## Overview

When a user rebuilds an agent environment, the Docker container can start and become **healthy**, but a *later* provisioning step fails (custom Python package install, credential/file sync, etc.). Today this hard-fails to `status="error"`, so the env card looks like an **offline agent** — which is misleading: the container is running fine, but the agent-env is in a *degraded/critical* state the owner must fix.

This feature introduces a **first-class persisted critical state** that coexists with `status="running"`, a **persisted action/event log** capturing full error detail, an **amber warning surface** on the env card (mirroring the model_freshness badge), an **owner email notification** (catalog type), and **CRON gating** so an unstable env keeps everything working *except* scheduled executions.

**Core capabilities**

- Distinguish "container not running" (true offline, existing behavior) from "running but critical" (NEW).
- Persist a `critical_state` flag + cause on `AgentEnvironment`, raised when a post-start/post-rebuild step fails while the container is alive.
- Persist an **AgentEnvActionLog** of env operations (rebuild, setup-after-rebuild, package install, file/credential sync, cron-skip) with full, untruncated error detail.
- Amber "Action required" block on the env card with a **"Show details"** modal rendering the captured log entries.
- New `environment_critical` system notification (fire-once-per-transition) to the agent owner.
- CRON gating: a critical env receives no scheduled runs; each due run is recorded as a **`skipped`** `AgentScheduleLog` entry and the owner is notified (dedup-throttled).

**High-level flow**

```
rebuild/start (container becomes healthy)
        │
        ▼
post-start step fails (uv install / credential sync)  ──► container still running
        │
        ▼
EnvironmentLifecycleManager._enter_critical_state(env, cause, action_log_id)
   ├─ env.critical_state = True ; env.critical_cause = "package_install_failed"
   ├─ status STAYS "running" (container is up)
   ├─ AgentEnvActionLog row (status="error", full detail)        [persisted]
   ├─ ENVIRONMENT_CRITICAL_STATE_CHANGED websocket event
   └─ SystemNotificationService.notify(environment_critical)     [transition-gated]
        │
        ▼
Frontend env card: amber "Action required" block + [Show details] ──► action-log modal
        │
        ▼
Scheduler poll: env.critical_state == True
   ├─ AgentScheduleLog(status="skipped", reason="env critical")  [persisted]
   └─ SystemNotificationService.notify(environment_critical, scope=cron-skip dedup)
```

---

## Architecture Overview

### System components

| Component | Role | New / Modified |
|-----------|------|----------------|
| `AgentEnvironment` model | Add `critical_state`, `critical_cause`, `critical_since` columns | Modified (+migration) |
| `AgentEnvActionLog` model + table | Persisted env-operation log with full error detail | **New** (+migration) |
| `EnvironmentLifecycleManager` | Detect post-start step failures; enter/clear critical state; write action-log rows | Modified |
| `DockerEnvironmentAdapter` | Distinguish "container alive but step failed" from "container gone" | Modified |
| `environment_status_scheduler` | Clear critical state when container truly dies (status→error supersedes critical) | Modified (light) |
| `AgentEnvActionLogService` | Create/query action-log rows | **New** |
| `notification_catalog` | `ENVIRONMENT_CRITICAL` type + email template | Modified (no migration) |
| `agent_schedule_scheduler` | Gate CRON on `critical_state`; log `skipped`; notify | Modified |
| `AgentEnvironmentPublic` projection | Expose `critical_state`/`critical_cause`/`critical_since` | Modified |
| New action-log REST route | `GET /environments/{id}/action-logs` | **New** |
| `EnvironmentCard` + new `EnvironmentCriticalBadge` / details modal | Amber surface + "Show details" | **New / Modified** |

### Data flow / integration points

- **Lifecycle → DB**: the lifecycle is the source of truth for entering/clearing critical state. It already mutates `AgentEnvironment` in place + `commit()`; we add a single helper (`_enter_critical_state` / `_clear_critical_state`) to centralize this (the codebase currently has **no** central set-status helper — this feature introduces a focused one for the critical dimension only).
- **Lifecycle → Action log**: every entry/clear writes an `AgentEnvActionLog` row carrying the full error string.
- **Lifecycle → Notifications**: transition into critical fires `environment_critical` to `Agent.owner_id`, mirroring `model_deprecated`'s `_warned_env_ids` transition gating.
- **Scheduler → Action log + ScheduleLog + Notifications**: a due schedule against a critical env writes a `skipped` `AgentScheduleLog` and notifies (separate dedup scope).
- **Projection → Frontend**: critical fields ride `AgentEnvironmentPublic` (persisted columns, unlike the transient `model_health`); the env card renders the amber block; the action-log route powers the "Show details" modal.

---

## Critical-State vs. Status Decision (resolved)

**Decision: add persisted boolean/cause columns (`critical_state`, `critical_cause`, `critical_since`) that COEXIST with `status="running"`. Do NOT add a new `status` enum value.**

**Justification (grounded):**

- The container *is* running. `status` describes the **container/lifecycle** dimension; the health-check scheduler (`environment_status_scheduler._check_environment_statuses`, lines 35–108) only sets `status="error"` when the env-core health is unhealthy **AND** `adapter.get_status() != "running"` (a deliberate double-gate). A running-but-degraded env must keep `status="running"` so sessions, chat, terminals, and logs keep working — exactly the requirement.
- The status string has **no DB enum** (`environment.py` line 24 is a plain `str` with values documented in a comment), so adding a value is cheap — but it would conflate two orthogonal axes and break the existing `EnvironmentStatusBadge` mapping (`error → destructive`, etc.), which is what makes the env look "offline." A separate flag keeps the green "Running" badge while layering an amber warning on top — exactly the model_freshness pattern (badge overlay, not a status replacement).
- Mirrors the established precedent: `model_health` is an *additional* signal that coexists with `status`. The only difference is **persistence**: `model_health` is transient (recomputed per request via `to_public_with_health`), but critical state is an **event** raised by the lifecycle at a specific moment and must survive across requests and process restarts, so it must be a persisted column — not recomputable on demand.

**Why persisted (not transient like model_health):** there is no cheap, pure function that can recompute "did the last rebuild's package install fail?" from current env state. It is a historical event. Hence columns + an action log, not a computed projection.

**Cause vocabulary** (`critical_cause`, plain `str`, matching the no-enum convention used for `status` and `AgentScheduleLog.status`):

| `critical_cause` | Raised when |
|------------------|-------------|
| `package_install_failed` | `install_custom_packages` / `install_system_packages` raised while container was alive |
| `file_sync_failed` | workspace file/credential sync into a live container failed |
| `credential_sync_failed` | `set_credentials` raised against a live container |
| `provisioning_failed` | generic catch-all for any other post-start step failure |

---

## Schema Decision: New Table vs. Reuse (resolved)

**Decision: NEW lightweight table `agent_env_action_log`. Do NOT reuse `AgentScheduleLog`; do NOT store detail in `system_notification`.**

**Justification (grounded):**

- **`AgentScheduleLog` is the wrong shape.** Its columns are schedule-centric (`schedule_id` FK CASCADE, `prompt_used`, `command_executed`, `command_exit_code`, `session_id`) and it is keyed to a parent schedule. Env operations (rebuild, setup, package install, sync) have no schedule. Forcing them into that table would require nullable-everything and a fake parent. The schedule-log *pattern* (immutable, append-only, free-string `status`, indexed by parent + `executed_at`, last-N in UI) is the right shape to **copy**, not the table to reuse.
- **`system_notification` cannot hold the detail.** Email bodies are deliberately truncated to ≤500 chars (`_MAX_ERROR_TEXT_CHARS` in `notification_service.py`) and must never include stack traces. The full, untruncated detail the "Show details" affordance renders must live elsewhere — the action log.
- **`AgentEnvironment.config["last_error"]`** already stores the last rebuild error string, but it is a single overwritten slot with no history, no per-step granularity, and is not projected to the frontend. The action log gives an append-only history the UI can page through.

The action log doubles as the data source for the `skipped` CRON entries' env-side record and any future env-operation audit needs (extensibility).

---

## Data Models

### Modified: `AgentEnvironment` (`backend/app/models/environments/environment.py`)

Add three columns to the table model (near the existing `status` / `status_message` fields, ~line 24–26):

| Field | Type | Constraints / default | Purpose |
|-------|------|-----------------------|---------|
| `critical_state` | `bool` | `default=False`, `nullable=False`, `server_default=false` | True ⇒ container running but env degraded |
| `critical_cause` | `str \| None` | `String(64)`, nullable | One of the cause codes above |
| `critical_since` | `datetime \| None` | `DateTime(timezone=True)`, nullable | When critical state was entered (for transition + display) |

Add a partial index for the scheduler's hot-path check and any admin fleet query:

- `ix_agent_environment_critical_state` — partial btree on `critical_state` where `critical_state = true`.

### Modified: `AgentEnvironmentPublic` (same file, ~line 150–177)

Add to the public projection (these are **persisted**, so they auto-populate from `model_validate(environment)` — no `to_public_with_health`-style attachment needed):

```python
critical_state: bool = False
critical_cause: str | None = None
critical_since: datetime | None = None
```

(Also add to `AdminAgentEnvironmentPublic`, ~line 202–228, so the admin fleet console can surface a critical column beside `is_stale` / `model_health_warning` — see Future Enhancements; minimal here.)

### New: `AgentEnvActionLog` (`backend/app/models/environments/agent_env_action_log.py`)

Mirrors `AgentScheduleLog`'s shape (immutable, append-only, free-string status).

**Table `agent_env_action_log`:**

| Field | Type | Constraints | Description |
|-------|------|-------------|-------------|
| `id` | UUID | PK, `default_factory=uuid4` | Log identifier |
| `environment_id` | UUID | FK → `agent_environment.id`, `ondelete="CASCADE"`, indexed | Parent environment |
| `agent_id` | UUID | FK → `agent.id`, `ondelete="CASCADE"`, indexed | Denormalized agent ref (owner lookups, fleet queries) |
| `action` | `str` | `String(48)`, not null | `"rebuild"`, `"setup_after_rebuild"`, `"package_install"`, `"system_package_install"`, `"credential_sync"`, `"file_sync"`, `"cron_skipped"` |
| `status` | `str` | `String(24)`, not null | `"success"` \| `"error"` \| `"skipped"` (free string; documented in docstring, matching `AgentScheduleLog`) |
| `cause` | `str \| None` | `String(64)`, nullable | The `critical_cause` code when `status="error"` (links the env flag to the row) |
| `summary` | `str \| None` | `String(512)`, nullable | Short human-readable cause line (safe to surface in lists, may seed the email's brief cause) |
| `detail` | `Text \| None` | nullable | **Full, untruncated** error/output text (uv resolver output, stack-free exception string). Source for "Show details". |
| `executed_at` | datetime | `DateTime(timezone=True)`, default now, indexed | When the action happened (UTC) |

**Indexes:** `ix_agent_env_action_log_environment_id`, `ix_agent_env_action_log_agent_id`, `ix_agent_env_action_log_executed_at`.

**Relationships:** many-to-one `AgentEnvironment` (CASCADE), many-to-one `Agent` (CASCADE). Append-only; never updated; deleted only via cascade.

**Response schemas (same file):**

- `AgentEnvActionLogPublic` — all fields above.
- `AgentEnvActionLogsPublic` — `{ data: list[AgentEnvActionLogPublic], count: int }`.

Re-export both new models in `backend/app/models/__init__.py`.

---

## Security Architecture

- **No encryption fields.** Action-log `detail` is operational text (uv output, exception messages). It must **not** contain secrets: credential sync errors must log only the failure reason/HTTP status, never the credential payload. The `summary`/`detail` builders must sanitize the same way the existing adapter logs do (the adapter already logs `install_result.stdout`, not secrets).
- **Access control:** the `GET /environments/{id}/action-logs` route is **owner-gated** (same ownership check as other per-environment routes — resolve env → agent → assert `agent.owner_id == CurrentUser.id`, with superuser allowed). No cross-user access; no admin-only surface in this MVP beyond the owner.
- **Notification recipient** is always `Agent.owner_id` (mirrors `model_deprecated`), never a third party. The email passes through the existing `is_outbound_email_allowed` confirmation gate and per-user preference.
- **Email body** carries only agent name, env/instance name, a **brief** cause line (the `summary`, ≤500 chars via the existing sanitizer), and a deep link. **No `detail`/stack/output** in the email — full detail stays behind the authenticated action-log route.
- **Input validation:** the action-log route is read-only (no body); pagination capped (default/limit 50, max 200).

---

## Backend Implementation

### Service Layer

#### New: `AgentEnvActionLogService` (`backend/app/services/environments/agent_env_action_log_service.py`)

| Method | Signature | Description |
|--------|-----------|-------------|
| `record` | `record(db_session, *, environment_id, agent_id, action, status, cause=None, summary=None, detail=None) -> AgentEnvActionLog` | Construct + add + commit + refresh an immutable row. Failure-isolated at call sites (best-effort; a logging failure must never abort lifecycle). |
| `list_for_environment` | `list_for_environment(db_session, environment_id, limit=50) -> list[AgentEnvActionLog]` | Recent rows, `executed_at DESC`. |
| `latest_critical` | `latest_critical(db_session, environment_id) -> AgentEnvActionLog \| None` | Most recent `status="error"` row — convenience for the card's primary cause line. |

#### Modified: `EnvironmentLifecycleManager` (`backend/app/services/environments/environment_lifecycle.py`)

Introduce two focused helpers (the critical dimension only — not a full status refactor):

- `_enter_critical_state(self, db_session, environment, agent, *, cause, summary, detail) -> None`
  1. Record an `AgentEnvActionLog` (`status="error"`, `cause`, `summary`, full `detail`) via `AgentEnvActionLogService.record`.
  2. Set `environment.critical_state = True`, `environment.critical_cause = cause`, `environment.critical_since = now` (only stamp `critical_since` on the **transition** False→True, to preserve fire-once semantics and the original onset time).
  3. **Do NOT change `status`** — leave it `running`. Also keep `status_message` informative (e.g. `"Running, but setup incomplete: {summary}"`) so the existing (currently-unrendered) field is meaningful.
  4. `db_session.add(environment); db_session.commit()`.
  5. Emit a new `ENVIRONMENT_CRITICAL_STATE_CHANGED` event (`critical_state=True`, `cause`, `summary`) over the event bus (so the env card updates live, mirroring `ENVIRONMENT_STATUS_CHANGED`).
  6. Best-effort `SystemNotificationService.notify(environment_critical)` — see Notifications. Wrapped so a dispatch failure never propagates.

- `_clear_critical_state(self, db_session, environment, *, action, detail=None) -> None`
  Called when a subsequent successful rebuild/start completes the post-start steps. Records a `success` action-log row, sets `critical_state=False`, `critical_cause=None`, `critical_since=None`, restores a normal `status_message`, commits, emits `ENVIRONMENT_CRITICAL_STATE_CHANGED(critical_state=False)`, and discards the env from the in-memory transition set.

**Wiring the failure detection** — the key behavioral change is distinguishing *step failed while container alive* from *container not running*:

- In `_setup_new_container` (lines 553–613), wrap `install_custom_packages` (583) and `install_system_packages` (590) in `try/except`. On exception, **probe the container** via the adapter's new `is_container_running()` (below):
  - **Container alive** ⇒ call `_enter_critical_state(cause="package_install_failed", summary="Failed to install custom packages", detail=str(e))`. The rebuild/start then **completes normally** (status stays `running`, env is usable) instead of bubbling to the outer `except` that sets `status="error"`.
  - **Container gone** ⇒ re-raise, preserving today's true-offline `status="error"` behavior.
- In `_sync_dynamic_data` (lines 255–360), wrap the credential-sync call (`set_credentials`, line 326) similarly: alive ⇒ `_enter_critical_state(cause="credential_sync_failed")`; gone ⇒ re-raise.
- In `rebuild_environment`'s success branch (the `was_running` path, lines 1184–1210, which logs `"Setting up new container after rebuild"` at 1187 and calls `_setup_new_container` then `_sync_dynamic_data`): because the setup helpers now handle alive-but-failed internally, a critical state is entered *and the rebuild still reports success* (`status="running"`). The outer `except` at 1231 (`"Failed to rebuild environment"`, sets `status="error"`, re-raises) now fires **only** for genuine container-down / docker-level failures — exactly the intended split.
- On a **successful** full setup (no exception), call `_clear_critical_state` if the env was previously critical, so a fixed env recovers automatically on the next rebuild/restart.

**Note on scattered status writes:** we deliberately scope this to the critical dimension; we do not refactor the ~8 inline `status=` writes. The two helpers centralize only critical-state mutation + event + action-log + notification, which is the new seam.

#### Modified: `DockerEnvironmentAdapter` (`backend/app/services/environments/adapters/docker_adapter.py`)

Add a cheap liveness probe so the lifecycle can branch (the adapter currently has `health_check` and `get_status` but the install paths never call them):

- `is_container_running(self) -> bool` — wraps the existing `get_status()` (lines 340–367) and returns `get_status() in {"running", "starting"}`. Used only by the lifecycle's `except` branches; does not change `install_custom_packages` / `install_system_packages` themselves (they still raise on non-zero exit as today). This keeps the adapter's "did the step fail" reporting unchanged while giving the lifecycle the missing "is the container still there" signal.

#### Modified: `environment_status_scheduler` (`backend/app/services/environments/environment_status_scheduler.py`)

When the health scheduler genuinely flips an env to `status="error"` (container confirmed down, lines 58–71), it should **clear** `critical_state` (the container is now offline — the offline status supersedes the running-but-degraded warning; we don't want both a red "Error" badge and an amber "Action required" badge). Set `critical_state=False`, `critical_cause=None`, `critical_since=None` in the same commit. (Record an action-log row `action="rebuild"`/generic, `status="error"`, summarizing the transition is optional but recommended for the history.)

### Notifications

#### Modified: `notification_catalog.py` — add `ENVIRONMENT_CRITICAL` type (NO migration)

Add to `NotificationType` enum:

```python
ENVIRONMENT_CRITICAL = "environment_critical"
```

Add a catalog entry (`NotificationTypeMeta`):

| Field | Value |
|-------|-------|
| `label` | `"Environment needs attention"` |
| `description` | `"Email me when one of my agent environments starts but a setup step fails, or a scheduled run is skipped because the environment is unstable."` |
| `default_email_enabled` | `True` |
| `email_template` | `"environment_critical.html"` |
| `subject` | `lambda ctx: f"{settings.PROJECT_NAME} — Action needed for {ctx.get('instance_name', 'your environment')}"` |
| `dedup_scope` | `"environment_id"` |

**Email template** — author `backend/app/email-templates/src/environment_critical.mjml`, build to `backend/app/email-templates/build/environment_critical.html`. Context keys:

| Key | Source |
|-----|--------|
| `project_name` | `settings.PROJECT_NAME` |
| `agent_name` | `Agent.name` |
| `instance_name` | `AgentEnvironment.instance_name` |
| `environment_id` | `str(env.id)` |
| `reason` | one of: setup-failure cause summary, or "a scheduled run was skipped" |
| `detail` | the **brief** `summary` (≤500 chars; full detail intentionally omitted, lives in the action log) |
| `link` | `{FRONTEND_HOST}/agents/{agent.id}` |

#### Notification-Type Decision: ONE type, TWO dedup scopes (resolved)

**Decision: a single `environment_critical` type, reused for both the setup-failure email and the CRON-skip email, distinguished by the dedup *value* passed in `context`.**

**Justification (grounded):**

- The catalog dedup is keyed on `(notification_type.value, str(context[dedup_scope]))` in `_should_send`. With `dedup_scope="environment_id"`, both the setup-failure path and the CRON-skip path would *collide on the same dedup key* if they passed the same `environment_id` — which is actually **desirable**: once the owner has been told "this env needs attention," the every-minute CRON poll must not storm them. A single 30-minute TTL across both causes is the correct UX.
- However, the two are semantically the same user-facing concern ("your env needs attention"), so a single catalog entry + single Settings toggle is cleaner than two. One toggle, one type, one template that varies its `reason` line.
- The **transition-once gating** for the *setup-failure* path uses the in-memory `_warned_env_ids`-style set (so a persistently-critical env doesn't re-email on every rebuild attempt). The **CRON-skip** path relies on the catalog `dedup_scope="environment_id"` 30-minute throttle (the scheduler polls every minute; the throttle is the right tool there, exactly as the docs describe it being "a second line of defense"). Both paths therefore avoid storms, by the mechanism best suited to each.

**Rejected alternative — two types** (`environment_critical` + `cron_skipped`): would add a second Settings toggle and a second template for what users perceive as one problem, and the two emails would not throttle each other (different dedup keys), producing *more* noise, not less. One type with a shared `environment_id` dedup value is strictly better here.

#### Transition gating for the setup-failure path

Add a process-local `_critical_warned_env_ids: set[str]` in the lifecycle (or a small shared module), mirroring `model_discovery_service._warned_env_ids`:

- On `_enter_critical_state` transition False→True: if `env_key not in _critical_warned_env_ids`, add it and dispatch the email; otherwise skip dispatch (state already known).
- On `_clear_critical_state`: `discard(env_key)` so a future re-failure re-emails.
- Reset on process restart is acceptable (at most one extra email after deploy), consistent with `model_deprecated`.

### CRON Gating (Scheduler)

#### Modified: `agent_schedule_scheduler._poll_due_schedules` (`backend/app/services/agents/agent_schedule_scheduler.py`, lines 82–138)

In the per-schedule loop (after resolving `agent = db_session.get(Agent, schedule.agent_id)`, ~line 104), resolve the agent's active environment and short-circuit on critical state **before** dispatching:

1. `environment = AgentSchedulerService.get_active_environment(db_session, agent.id)` (delegates to `environment_resolver.get_active_environment`, which reads `agent.active_environment_id`).
2. If `environment is not None and environment.critical_state`:
   - `AgentSchedulerService.create_log(status="skipped", error_message="Skipped: environment is in a critical state and is not eligible for scheduled execution. Resolve the environment issue and the schedule will resume.", ...)`.
   - `AgentEnvActionLogService.record(action="cron_skipped", status="skipped", summary="Scheduled run skipped — env critical", detail=<schedule name + critical_cause>)`.
   - `SystemNotificationService.notify(environment_critical, context={..., "environment_id": str(environment.id), "reason": "a scheduled run was skipped"})` — throttled by the shared `environment_id` dedup scope.
   - **Advance `next_execution`** (so the schedule is re-evaluated next cycle, not stuck re-firing the same due time) via `AgentSchedulerService.update_execution_time`, but **do not** advance `last_execution` semantics that imply success — record the skip explicitly. (Decision: advancing `next_execution` is required to prevent a tight skip-loop every minute; the dedup throttle additionally caps notifications.)
   - `continue` (skip dispatch entirely).
3. Otherwise proceed to the existing `script_trigger` / `static_prompt` branches unchanged.

This blocks **only** CRON. Sessions, chat, terminals, logs, and the env console are untouched because they don't consult `critical_state`. Manual "Run now" (`AgentSchedulerService.execute_now`) is a separate decision (see Open Questions) — default: also block with a clear 400, so the contract is consistent.

#### Add `"skipped"` to the `AgentScheduleLog` status set

`AgentScheduleLog.status` is a free string (model line 34, documented values only). Add `"skipped"` to the documented set in the model docstring and to the frontend `LogStatusBadge` mapping (gray/neutral). No migration (free string). This is the cleanest fit per the requirement.

### API Routes

#### New: action-log route (`backend/app/api/routes/environments.py`)

| Method | Path | Auth | Request | Response |
|--------|------|------|---------|----------|
| GET | `/api/v1/environments/{environment_id}/action-logs` | `CurrentUser`, owner-gated (env→agent→owner; superuser allowed) | query `limit` (default 50, max 200) | `AgentEnvActionLogsPublic` |

Resolve `environment_id` → `AgentEnvironment` (404 if missing) → `Agent` → assert ownership (403 otherwise) → `AgentEnvActionLogService.list_for_environment`. Use the existing per-environment ownership helper pattern already in `environments.py` (tags drive the generated `EnvironmentsService` client name).

No new write endpoints — action-log rows are written only by the lifecycle/scheduler.

### Realtime Event

Add `ENVIRONMENT_CRITICAL_STATE_CHANGED` to the event-type registry (alongside `ENVIRONMENT_STATUS_CHANGED`). Payload: `{ environment_id, agent_id, critical_state: bool, cause: str|None, summary: str|None }`. The frontend `AgentEnvironmentsTab` already subscribes to `ENVIRONMENT_*` events for live invalidation; add this event to its invalidation set.

---

## Frontend Implementation

### New: `EnvironmentCriticalBadge.tsx` (`frontend/src/components/Environments/EnvironmentCriticalBadge.tsx`)

Mirrors `ModelHealthBadge.tsx` (the canonical reference):

- Props: `{ environment: AgentEnvironmentPublic, onShowDetails: () => void }`.
- Early return `null` when `!environment.critical_state`.
- Renders an **amber** block (reuse the model_freshness palette: `bg-orange-100 text-orange-800 dark:bg-orange-900 dark:text-orange-200`, `AlertTriangle` icon). Unlike the small model badge, this is a slightly more prominent **block** with copy "Action required — the container is running but setup did not finish," a cause-specific line derived from `critical_cause` (e.g. "Custom package install failed"), and a **"Show details"** button (`role="button"`, `onClick={onShowDetails}`).
- Tooltip optional; the primary affordance is the "Show details" button opening the modal.

### New: `EnvironmentActionLogsModal.tsx` (`frontend/src/components/Environments/EnvironmentActionLogsModal.tsx`)

Mirrors `AgentSchedulesCard.tsx`'s `LogDetailRow` + execution-logs `Dialog` (lines 177 / 1173–1215):

- `Dialog` (`max-w-2xl`, body `max-h-[60vh] overflow-y-auto`), opened on demand.
- Fetches via React Query key `["env-action-logs", environmentId]`, `EnvironmentsService.listActionLogs({ environmentId })`, `enabled` only while the modal is open (lazy, same as schedule logs).
- Each row: action badge, relative `executed_at`, a status badge (error=red, skipped=gray, success=green), and an expandable section (`ChevronDown/Up`) revealing `summary` and the **full `detail`** in a scrollable `<pre>` (`max-h-40`) inside a red `bg-red-50 border-red-200 text-red-700` block for `error` rows — directly reusing the existing error-detail visual convention.

### Modified: `EnvironmentCard.tsx` (`frontend/src/components/Environments/EnvironmentCard.tsx`)

- Keep `EnvironmentStatusBadge` exactly as-is (it shows the green "Running" badge — the container IS running). **Do not** route critical state through the status badge; that is what makes the env look offline today.
- In the meta-badges flex-wrap row (where `ModelHealthBadge` mounts, ~line 395), or as a full-width block directly below it, render `<EnvironmentCriticalBadge environment={environment} onShowDetails={...} />`.
- Add local state `actionLogsOpen` + render `<EnvironmentActionLogsModal environmentId={environment.id} open={actionLogsOpen} onOpenChange={...} />`; `onShowDetails` sets it open.
- Visual separation per requirement: a `stopped`/`error` env still shows the destructive/secondary status badge (container not running); a `running` + `critical_state` env shows the green status badge **plus** the amber action-required block — visually distinct treatments.

### State Management

- New query key `["env-action-logs", environmentId]`; invalidated on `ENVIRONMENT_CRITICAL_STATE_CHANGED`.
- Existing `["agent-environments", agentId]` list query (in `AgentEnvironmentsTab`) already polls every 10s and invalidates on `ENVIRONMENT_*`; the persisted `critical_state` field rides the env objects automatically once the projection is updated and the client regenerated.

### Client Regeneration

After backend changes (new columns on `AgentEnvironmentPublic`, new `AgentEnvActionLog*` schemas, new route): regenerate the OpenAPI client:

```bash
source ./backend/.venv/bin/activate && make gen-client
# (or: bash scripts/generate-client.sh)
```

This adds `critical_state`/`critical_cause`/`critical_since` to `AgentEnvironmentPublic`, the `AgentEnvActionLogPublic`/`AgentEnvActionLogsPublic` types, and `EnvironmentsService.listActionLogs`. The `environment_critical` notification toggle appears automatically in Settings → Notifications (catalog-driven, no frontend change).

---

## Database Migrations

Run via Docker (`make migration` to autogenerate, then review/edit, `make migrate` to apply).

### Migration 1 — `agent_env_action_log` table + `AgentEnvironment` critical columns

Single migration (or two; one is fine since both are this feature):

**Upgrade:**
- `create_table("agent_env_action_log", ...)` with columns from the model; FKs `environment_id → agent_environment.id (ondelete="CASCADE")`, `agent_id → agent.id (ondelete="CASCADE")`.
- Create indexes: `ix_agent_env_action_log_environment_id`, `ix_agent_env_action_log_agent_id`, `ix_agent_env_action_log_executed_at`.
- `add_column("agent_environment", critical_state BOOLEAN NOT NULL server_default=sa.false())`, `critical_cause VARCHAR(64) NULL`, `critical_since TIMESTAMPTZ NULL`.
- Create partial index `ix_agent_environment_critical_state` on `critical_state` WHERE `critical_state = true`.
- (After backfill, the `server_default` on `critical_state` may be dropped if the team prefers app-level defaults — optional; keeping it is safe.)

**Downgrade:**
- Drop the partial index; drop the three `agent_environment` columns; drop `agent_env_action_log` indexes; drop the table.

**Migration notes (grounded in repo gotchas):**
- The repo has historically had **multiple Alembic heads**; check `alembic heads` and set `down_revision` to the single current head (or add a merge migration if multiple exist) before applying. This is a recurring trap in this codebase.
- Autogenerate may need hand-trimming (drift from prior hand-edited migrations); review the generated file.

### No migration for the notification type

`ENVIRONMENT_CRITICAL` is catalog-only (enum value + `NotificationTypeMeta` + built `environment_critical.html`). `UserNotificationSetting.notification_type` is a plain string; a missing preference row means "use catalog default," so no schema change and no backfill — exactly the `model_deprecated` precedent.

---

## Error Handling & Edge Cases

- **Container alive vs gone ambiguity:** the adapter's `install_*` raises an identical generic `Exception` whether the container died or the install genuinely failed (`execute_command` collapses `NotFound` → `exit_code=1`). The new `is_container_running()` probe in the `except` branch is the disambiguator. If the probe itself errors, **default to re-raise** (treat as offline) — failing safe toward the existing behavior rather than masking a dead container as merely "critical."
- **Notification dispatch failure** never propagates (wrapped; the existing `notify` is already failure-isolated, but the call site is also guarded).
- **Action-log write failure** is best-effort: log a warning, never abort the lifecycle or the scheduler poll.
- **Critical → recovered:** a later successful rebuild/restart clears the flag and records a `success` row; the amber block disappears; the env is removed from the transition set so a future failure re-emails.
- **Critical + container later dies:** the health scheduler sets `status="error"` and clears `critical_state` (offline supersedes critical) — the card shows the red "Error" badge, not the amber block.
- **Skip-loop prevention:** advancing `next_execution` on each CRON skip prevents the schedule from re-firing every minute; the notification dedup throttle caps emails to one per 30 minutes per env.
- **Foreign (bundle consumer) installs:** their materialised schedules are ordinary rows; CRON gating applies identically (the scheduler doesn't care who authored the schedule). No special handling.
- **`emails_enabled` off / unconfirmed user:** dispatch silently skipped (existing guards). The amber card + action log still work — the email is the only suppressed channel.

---

## UI/UX Considerations

- **Color semantics:** green "Running" status badge (container up) + **amber** action-required block (degraded) — never red for a running env. Red is reserved for true offline/error.
- **Copy:** lead with reassurance + action — "The container is running, but a setup step didn't finish. Your agent may not behave as expected until this is resolved." Then the cause line + "Show details."
- **Show details modal:** full untruncated `detail` in a monospace scrollable block; the real uv error ("No solution found when resolving dependencies… google-api-python-clients>=2.0.0") is exactly what the user needs to fix their `workspace_requirements.txt`.
- **CRON-skip visibility:** the `skipped` schedule-log entry shows in the existing schedule execution-logs modal with neutral styling and the skip reason, so a user investigating "why didn't my schedule run?" finds the answer in the place they already look.
- **Accessibility:** amber block carries `role="status"`/`aria-live` is unnecessary (not live-critical), but the "Show details" control is a real `<button>` with discernible text.

---

## Integration Points

- **Model Freshness** — canonical reference for the amber-badge-on-env-card convention (`ModelHealthBadge`), the transition-once notification discipline (`_warned_env_ids`), and the catalog-no-migration pattern. This feature parallels all three; the only divergence is persistence (columns, not a transient projection).
- **System Notifications** — new `environment_critical` catalog type; reuses `SystemNotificationService.notify`, dedup throttle, per-user preference, and the Settings → Notifications toggle (auto-rendered).
- **Agent Schedulers** — CRON gating lives in `_poll_due_schedules`; the `skipped` status joins the existing free-string set; the env-side record rides the new action log.
- **Agent Environments lifecycle** — the `_enter/_clear_critical_state` helpers and the alive-vs-gone branching in `_setup_new_container` / `_sync_dynamic_data` are the core behavioral change.
- **Realtime Events** — `ENVIRONMENT_CRITICAL_STATE_CHANGED` joins the env event family for live card updates.
- **Client regen** — required after the projection + route changes (`make gen-client`).
- **Docs** — update `docs/agents/agent_environments/` (new `agent_env_critical_state.md` + `_tech.md`) and add a Feature Registry row; cross-link from `model_freshness.md`, `agent_schedulers.md`, and `system_notifications.md`.

---

## Future Enhancements (Out of Scope)

- **Admin fleet column:** a `critical_state` column on `/admin/agent-envs` beside `is_stale` / `model_health_warning` (the `AdminAgentEnvironmentPublic` field is added in this plan but the table UI is deferred).
- **In-app "Retry setup" action:** a button on the amber block that re-runs only the failed provisioning step instead of a full rebuild.
- **Per-cause remediation CTAs:** richer, cause-specific guidance (e.g. "Edit workspace_requirements.txt") linking to the file viewer.
- **Distributed transition tracking:** replace the in-memory `_critical_warned_env_ids` with a persisted `critical_since`-based check if multi-worker email duplication becomes a concern (the `critical_since` column already supports this — gate on "did `critical_since` change since last poll").

---

## Test Scenarios (API-only, scenario-based — see `backend/tests/README.md`)

> Tests run inside Docker (`make test-backend`), are API-only (no direct DB access), and scenario-based. Check `backend/tests/api/agents/README.md` and any environments test README for local conventions before writing.

**Critical-state lifecycle:**
- Rebuild where the container starts healthy but a simulated `install_custom_packages` failure occurs (stub adapter `is_container_running → True`) ⇒ env ends with `status="running"`, `critical_state=True`, `critical_cause="package_install_failed"`; an `AgentEnvActionLog` row exists with full `detail`.
- Rebuild where the container is genuinely gone (stub `is_container_running → False`) ⇒ env ends `status="error"`, `critical_state=False` (existing offline behavior preserved).
- Credential-sync failure against a live container ⇒ `critical_state=True`, `critical_cause="credential_sync_failed"`.
- Successful rebuild after a prior critical state ⇒ `critical_state` cleared, a `success` action-log row recorded.
- Health scheduler confirms container down on a critical env ⇒ `status="error"` AND `critical_state` cleared.

**Action-log API:**
- `GET /environments/{id}/action-logs` as owner ⇒ 200, rows in `executed_at DESC`, full `detail` present.
- As non-owner ⇒ 403; unknown env ⇒ 404; `limit` respected and capped at 200.

**Notifications:**
- Entering critical state fires `environment_critical` to `Agent.owner_id` exactly once per transition (second rebuild while still critical ⇒ no second email).
- Recovery then re-failure ⇒ email fires again.
- User with the toggle off (or unconfirmed) ⇒ no email; action log + flag still set.
- Email body contains the brief `summary` and deep link, **not** the full `detail`.

**CRON gating:**
- Due schedule against a critical env ⇒ no session/exec; one `AgentScheduleLog` with `status="skipped"` and the skip reason; `next_execution` advanced; one `environment_critical` notification (CRON-skip reason), throttled so repeated polls within 30 min don't re-email.
- Due schedule against a healthy running env ⇒ executes normally (regression).
- Critical env cleared ⇒ next due schedule executes (resumes).
- `execute_now` (manual run) against a critical env ⇒ 400 with clear message (per the chosen contract).

**Regression:**
- Full agents + environments + schedulers suites pass; existing `status="error"` offline paths and `model_deprecated` behavior unaffected.

---

## Summary Checklist

### Backend
- [ ] Add `critical_state` / `critical_cause` / `critical_since` columns to `AgentEnvironment` (`models/environments/environment.py`) + expose on `AgentEnvironmentPublic` and `AdminAgentEnvironmentPublic`.
- [ ] Create `AgentEnvActionLog` model + `AgentEnvActionLogPublic` / `AgentEnvActionLogsPublic` (`models/environments/agent_env_action_log.py`); re-export in `models/__init__.py`.
- [ ] Create `AgentEnvActionLogService` (`services/environments/agent_env_action_log_service.py`): `record`, `list_for_environment`, `latest_critical`.
- [ ] Add `DockerEnvironmentAdapter.is_container_running()` (`adapters/docker_adapter.py`).
- [ ] Add `_enter_critical_state` / `_clear_critical_state` helpers to `EnvironmentLifecycleManager`; wrap `install_custom_packages` / `install_system_packages` in `_setup_new_container` and `set_credentials` in `_sync_dynamic_data` with alive-vs-gone branching; clear on successful setup.
- [ ] Add process-local `_critical_warned_env_ids` transition gating for the setup-failure email.
- [ ] Clear `critical_state` in `environment_status_scheduler._check_environment_statuses` when status flips to `error`.
- [ ] Add `ENVIRONMENT_CRITICAL` to `NotificationType` + catalog entry; author `environment_critical.mjml` → build `environment_critical.html`.
- [ ] Add `ENVIRONMENT_CRITICAL_STATE_CHANGED` event type; emit from the helpers.
- [ ] Gate CRON in `agent_schedule_scheduler._poll_due_schedules`: skip on `critical_state`, write `AgentScheduleLog(status="skipped")` + action-log `cron_skipped` + notify, advance `next_execution`.
- [ ] Add `"skipped"` to documented `AgentScheduleLog.status` set; block `execute_now` on critical env (400).
- [ ] Add `GET /environments/{id}/action-logs` (owner-gated) in `routes/environments.py`.

### Migration
- [ ] Generate migration (Docker) creating `agent_env_action_log` (+3 indexes) and adding 3 columns + partial index to `agent_environment`; verify single Alembic head / add merge if needed; downgrade drops all.

### Frontend (after `make gen-client`)
- [ ] Create `EnvironmentCriticalBadge.tsx` (amber block + "Show details", mirrors `ModelHealthBadge`).
- [ ] Create `EnvironmentActionLogsModal.tsx` (mirrors schedule-logs `LogDetailRow` + Dialog; full `detail` in error block).
- [ ] Wire both into `EnvironmentCard.tsx`; keep `EnvironmentStatusBadge` unchanged (green stays green).
- [ ] Add `"skipped"` styling to schedule `LogStatusBadge` (neutral).
- [ ] Add `["env-action-logs", id]` query; invalidate on `ENVIRONMENT_CRITICAL_STATE_CHANGED` in `AgentEnvironmentsTab`.

### Docs
- [ ] New `docs/agents/agent_environments/agent_env_critical_state.md` + `_tech.md`; Feature Registry row; cross-links from `model_freshness`, `agent_schedulers`, `system_notifications`.

### Testing & validation
- [ ] API tests for critical-state lifecycle, action-log route, notification transition gating, CRON skip + resume, manual-run block, and regressions (offline path, healthy schedule execution, model_deprecated).
- [ ] Run `make test-backend` (full agents/environments/schedulers suites) inside Docker.
