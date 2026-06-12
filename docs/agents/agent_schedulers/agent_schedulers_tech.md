# Agent Schedulers — Technical Details

## File Locations

### Backend

**Models:**
- `backend/app/models/agents/agent_schedule.py` — AgentSchedule database model + request/response schemas (includes `schedule_type`, `command` fields)
- `backend/app/models/agents/agent_schedule_log.py` — AgentScheduleLog database model + AgentScheduleLogPublic/AgentScheduleLogsPublic response schemas

**Routes:**
- `backend/app/api/routes/agents.py` — Schedule CRUD, AI generation, and logs endpoints (nested under agent routes)

**Services:**
- `backend/app/services/agents/agent_scheduler_service.py` — Schedule CRUD, CRON conversion, next execution calculation, log creation/retrieval, environment resolution shims (delegate to `environment_resolver.py`)
- `backend/app/services/agents/agent_schedule_scheduler.py` — Background scheduler (APScheduler) that polls and executes due schedules with branching logic for schedule types
- `backend/app/services/agents/environment_resolver.py` — Shared helpers `get_active_environment` and `ensure_environment_running`, extracted here so that [Agent Webhooks](../agent_webhooks/agent_webhooks.md) can reuse them without cross-service coupling. `AgentSchedulerService` delegates to these functions via thin wrapper methods.
- `backend/app/services/bundles/schedule_sync.py` — Bundle schedule propagation helpers: `snapshot_schedules`, `sig`, `materialise`, and `merge` (see Bundle Propagation section below)

**Agent-Env Endpoint:**
- `backend/app/env-templates/app_core_base/core/server/routes.py` — `POST /exec` endpoint for executing shell commands inside the agent container

**Environment Connector:**
- `backend/app/services/environments/agent_env_connector.py` — `exec_command()` method for calling the `/exec` endpoint

**AI Function:**
- `backend/app/agents/schedule_generator.py` — Natural language to CRON conversion via LLM
- `backend/app/agents/prompts/schedule_generator_prompt.md` — Prompt template for CRON generation

**Bundle propagation model:**
- `backend/app/models/bundles/agent_bundle_revision.py` — `AgentBundleRevision.schedules` field (JSON list) stores the snapshot; also exposed on `AgentBundleRevisionPublic`

**Migrations:**
- `backend/app/alembic/versions/7ef8eae8f523_add_agent_schedule_table.py` — Creates `agent_schedule` table
- `backend/app/alembic/versions/a4c8d9e0f1b2_add_name_prompt_to_agent_schedule.py` — Adds `name` and `prompt` fields
- `backend/app/alembic/versions/b1c2d3e4f5a6_add_schedule_types_and_logs.py` — Adds `schedule_type` + `command` columns, creates `agent_schedule_log` table with indexes

**Tests:**
- `backend/tests/api/agents/agent_schedules_test.py` — Integration tests (lifecycle, CRON conversion, permissions, schedule types, execution logging)
- `backend/tests/utils/schedule.py` — Test utilities (generate, create, list, update, delete)

### Frontend

**Components:**
- `frontend/src/components/Agents/AgentSchedulesCard.tsx` — Main schedule management card (type selector, create/edit dialogs, list, toggle, delete, execution logs modal)

**Integration:**
- `frontend/src/components/Agents/AgentConfigTab.tsx` — Renders `AgentSchedulesCard` alongside `AgentHandovers` in a 2-column grid

## Database Schema

### Table: `agent_schedule`

| Field | Type | Description |
|-------|------|-------------|
| `id` | UUID (PK) | Schedule identifier |
| `agent_id` | UUID (FK → agent.id, CASCADE) | Parent agent |
| `name` | str | User-friendly label (e.g., "Daily data collection") |
| `cron_string` | str | CRON expression in UTC |
| `description` | str | Human-readable description from AI |
| `enabled` | bool (default: true) | Whether schedule is active |
| `prompt` | Text, nullable | Schedule-specific prompt (null = use agent's entrypoint_prompt) |
| `schedule_type` | str (default: "static_prompt") | Discriminator: "static_prompt" or "script_trigger" |
| `command` | Text, nullable | Shell command to execute (only for script_trigger) |
| `last_execution` | datetime, nullable | When schedule last ran |
| `next_execution` | datetime | Pre-calculated next run time (UTC) |
| `created_at` | datetime | Record creation timestamp |
| `updated_at` | datetime | Last modification timestamp |

**Relationship:** Many-to-one with Agent (`agent.schedules`, cascade delete)

### Table: `agent_schedule_log`

| Field | Type | Description |
|-------|------|-------------|
| `id` | UUID (PK) | Log entry identifier |
| `schedule_id` | UUID (FK → agent_schedule.id, CASCADE) | Parent schedule |
| `agent_id` | UUID (FK → agent.id, CASCADE) | Agent reference (denormalized) |
| `schedule_type` | str | Snapshot of type at execution time |
| `status` | str | "success", "session_triggered", or "error" |
| `prompt_used` | Text, nullable | Prompt sent to agent (static_prompt) |
| `command_executed` | Text, nullable | Command that was run (script_trigger) |
| `command_output` | Text, nullable | stdout from execution (truncated to 10,000 chars) |
| `command_exit_code` | int, nullable | Exit code from command |
| `session_id` | UUID (FK → session.id, SET NULL), nullable | Session created (if any) |
| `error_message` | Text, nullable | Error details if execution failed |
| `executed_at` | datetime | When execution happened (UTC) |

**Indexes:** `ix_agent_schedule_log_schedule_id`, `ix_agent_schedule_log_agent_id`, `ix_agent_schedule_log_executed_at`

**Relationships:** Many-to-one with AgentSchedule (cascade delete), Many-to-one with Agent (cascade delete), Many-to-one with Session (SET NULL)

## API Endpoints

All endpoints in `backend/app/api/routes/agents.py`, nested under `/api/v1/agents/{id}/schedules`. All verify agent ownership.

| Method | Path | Purpose | Request | Response |
|--------|------|---------|---------|----------|
| POST | `/{id}/schedules/generate` | AI CRON generation (stateless preview) | `ScheduleRequest` | `ScheduleResponse` |
| POST | `/{id}/schedules` | Create schedule | `CreateScheduleRequest` | `AgentSchedulePublic` |
| GET | `/{id}/schedules` | List all schedules | — | `AgentSchedulesPublic` |
| PUT | `/{id}/schedules/{schedule_id}` | Update schedule (partial) | `UpdateScheduleRequest` | `AgentSchedulePublic` |
| DELETE | `/{id}/schedules/{schedule_id}` | Delete schedule | — | `Message` |
| POST | `/{id}/schedules/{schedule_id}/run` | Manually trigger a schedule immediately | — | `Message` |
| GET | `/{id}/schedules/{schedule_id}/logs` | List execution logs (last 50) | — | `AgentScheduleLogsPublic` |

### Request/Response Models

Defined in `backend/app/models/agents/agent_schedule.py`:

- **ScheduleRequest** — `natural_language: str`, `timezone: str` (for AI generation)
- **ScheduleResponse** — `success: bool`, `description`, `cron_string`, `next_execution` (ISO 8601), `error`
- **CreateScheduleRequest** — `name`, `cron_string`, `timezone`, `description`, `prompt` (optional), `enabled`, `schedule_type` (default "static_prompt"), `command` (optional)
- **UpdateScheduleRequest** — All fields optional except `schedule_type` (immutable); `timezone` required when `cron_string` changes
- **AgentSchedulePublic** — Full schedule fields including `schedule_type` and `command`
- **AgentSchedulesPublic** — `data: list[AgentSchedulePublic]`, `count: int`

Defined in `backend/app/models/agents/agent_schedule_log.py`:

- **AgentScheduleLogPublic** — All log fields for API response
- **AgentScheduleLogsPublic** — `data: list[AgentScheduleLogPublic]`, `count: int`

**Run Now response messages** (returned as `Message.message` and surfaced in the frontend success toast):
- `"Schedule triggered successfully"` — env was running; execution completed synchronously
- `"Environment is starting; the schedule will run automatically once it's ready."` — env was waking up; execution deferred to background
- 400 error — no active environment, or env is in `error` state

### Route Validation

Create endpoint validates schedule type + command combination:
- `schedule_type == "script_trigger"` requires non-empty `command`
- Unknown `schedule_type` values are rejected with 400
- `schedule_type` is excluded from `UpdateScheduleRequest` — immutable after creation

## Services & Key Methods

### Scheduler Service — `backend/app/services/agents/agent_scheduler_service.py`

| Method | Purpose |
|--------|---------|
| `convert_local_cron_to_utc(cron_string, timezone)` | Converts CRON from user's local timezone to UTC |
| `calculate_next_execution(cron_string)` | Calculates next run time from UTC CRON using croniter |
| `generate_schedule_preview(natural_language, timezone)` | Orchestrates AI call + CRON conversion + next execution calculation |
| `create_schedule(session, agent_id, name, cron_string, timezone, description, prompt, enabled, schedule_type, command)` | Creates AgentSchedule record with CRON conversion |
| `get_agent_schedules(session, agent_id)` | Lists all schedules for an agent, ordered by created_at |
| `get_schedule_by_id(session, schedule_id)` | Gets single schedule |
| `update_schedule(session, schedule_id, **fields)` | Partial update; recalculates next_execution if cron_string changes |
| `delete_schedule(session, schedule_id)` | Deletes schedule by ID |
| `get_all_enabled_schedules(session)` | Returns all enabled schedules (for background polling) |
| `update_execution_time(session, schedule_id, last_execution)` | Updates timestamps after execution |
| `verify_agent_access(session, agent_id, user)` | Validates user owns the agent |
| `get_schedule_for_agent(session, schedule_id, agent_id)` | Validates schedule belongs to agent |
| `create_log(session, schedule_id, agent_id, ...)` | Creates immutable AgentScheduleLog entry |
| `get_schedule_logs(session, schedule_id, limit=50)` | Returns recent logs ordered by executed_at DESC |
| `execute_now(session, agent_id, schedule_id)` | Manually triggers a schedule. Returns `ManualRunResult(action)` — the route layer maps `action` → toast message (user-facing copy lives in the route, not the service). `action="executed"` if the environment was already running (synchronous execution). `action="env_starting"` if the environment was suspended/stopped/activating/starting — in that case a background task (`create_task_with_error_logging`) runs the module-level `_activate_env_and_run_schedule(agent_id, schedule_id)` helper which opens a fresh `DBSession(engine)`, calls `ensure_environment_running`, then dispatches through the shared `_dispatch_schedule(db, schedule, agent)` helper (used by both the sync fast path and the deferred path). Background-task failures (activation timeout / env entered error / disappeared / dispatch raised) are surfaced via an `AgentScheduleLog` row + `EventType.CRON_ERROR` event so the UI logs panel and activity feed see the same error shape a cron-poll failure would. Raises `ScheduleError(400)` if the agent has no active environment or the env is in error state; `ScheduleNotFoundError(404)` if the schedule is missing or belongs to another agent. |
| `get_active_environment(session, agent_id)` | Thin shim; delegates to `environment_resolver.get_active_environment()` |
| `ensure_environment_running(environment, get_fresh_db_session)` | Thin shim; delegates to `environment_resolver.ensure_environment_running()`. Auto-activates suspended/stopped environments; raises on error/timeout. Used by manual schedule runs (deferred path), cron-polled script triggers, and agent webhooks. |

### Background Scheduler — `backend/app/services/agents/agent_schedule_scheduler.py`

- Uses **APScheduler BackgroundScheduler**
- Polls every 1 minute for due schedules (`next_execution <= now`, `enabled = true`)
- Session creation routes through `ChannelIngestionService.ingest_inbound_message` with `SessionSender.from_system_trigger(...)` (`kind="system_trigger"`) — see [channel ingestion](../../application/agent_sessions/channel_ingestion.md) / [tech](../../application/agent_sessions/channel_ingestion_tech.md). The `allow_system_trigger_fastpath=True` policy is paired with an asserted structural invariant (`expected_owner_id == agent.owner_id == sender.platform_user_id`) — a fire that mis-stamps the owner raises, not silently widens trust.
- Branches on `schedule.schedule_type`:
  - `_execute_static_prompt()` — original behavior: resolves prompt, creates session, creates log entry
  - `_execute_script_trigger()` — resolves environment, auto-activates if needed, calls `AgentEnvConnector.exec_command()`, checks OK vs non-OK output, creates session with context if needed, creates log entry
- `_build_script_context_message()` — formats command output into a context message for the agent session
- Error handling: logs errors without advancing schedule on failure; creates error log entries
- Started/stopped via app lifecycle hooks in `backend/app/main.py`

### Agent-Env Connector — `backend/app/services/environments/agent_env_connector.py`

| Method | Purpose |
|--------|---------|
| `exec_command(base_url, auth_token, command, timeout=120)` | POSTs to `/exec` endpoint in agent container, returns `{"exit_code", "stdout", "stderr"}` |

### Agent-Env `/exec` Endpoint — `backend/app/env-templates/app_core_base/core/server/routes.py`

- `POST /exec` — executes shell command via `asyncio.create_subprocess_shell()`
- Working directory: `/app/workspace/`
- Timeout enforcement (default 120s, max 300s)
- Output truncation at 10,000 chars each (stdout/stderr)
- Same bearer token auth as all other agent-env endpoints

### AI Schedule Generator — `backend/app/agents/schedule_generator.py`

- Loads prompt from `backend/app/agents/prompts/schedule_generator_prompt.md`
- Passes user input + current time + timezone to LLM via provider manager (cascade selection)
- Returns `{ success, description, cron_string }` or `{ success: false, error }`
- CRON output is in **local time** — backend converts to UTC

## Frontend Components

### AgentSchedulesCard — `frontend/src/components/Agents/AgentSchedulesCard.tsx`

- **Props:** `{ agentId: string, readOnly?: boolean }` — when `readOnly=true` (consumer bundle installs): New/Edit/Delete actions are hidden; Power toggle, Run now, and Logs remain. The card header shows an informational note "Managed by the bundle publisher — you can enable/disable, run, and view logs." The empty state message also adapts to distinguish "no publisher schedules" from "no schedules yet"
- **Query:** `useQuery` with key `["agent-schedules", agentId]`, calls `AgentsService.listSchedules()`
- **Logs query:** `useQuery` with key `["schedule-logs", scheduleId]`, calls `AgentsService.listScheduleLogs()`, fetched on-demand when logs modal opens
- **Mutations:** create, update, toggle (`{enabled: !current}`), delete — all invalidate query key; `runNowMutation` calls `POST /{id}/schedules/{schedule_id}/run` and surfaces `response.message` in the success toast (so the "starting" notification reaches the user directly)

**Type Selector (create dialog step 1):**
- Two cards: Static Prompt (FileText icon) and Script Trigger (Terminal icon, amber)
- Clicking a card transitions to the type-specific form

**Create Dialog (step 2):**
- Static Prompt form: name, timing/generate, prompt textarea
- Script Trigger form: name, timing/generate, command input (single-line, monospace, max 2000 chars)
- Back button to return to type selector

**Edit Dialog:**
- Conditionally shows prompt or command fields based on `schedule.schedule_type`
- Schedule type is not changeable

**Schedule Row:**
- Name (bold), description (muted), next execution time
- Badges: enabled/disabled, "Custom prompt" (static_prompt with prompt), "Script trigger" (amber badge with Terminal icon)
- For script_trigger: truncated command displayed below description
- Action buttons: logs (History icon), edit (Pencil), toggle (Power), delete (Trash2)

**Execution Logs Modal:**
- `LogDetailRow` component with expandable accordion details
- Color-coded status: green check (success), amber lightning (session_triggered), red X (error)
- Details: command/prompt used, command output (monospace pre block), exit code, session link, error message
- Session links navigate to `/session/{session_id}`

**State management:**
- `createStep: "type_select" | "form"` — tracks create dialog step
- `createType: "static_prompt" | "script_trigger"` — selected type
- `logsModalOpen: boolean` — logs modal visibility
- `logsSchedule: AgentSchedulePublic | null` — which schedule's logs to show
- All state reset on dialog close

### Integration in AgentConfigTab — `frontend/src/components/Agents/AgentConfigTab.tsx`

Renders `AgentSchedulesCard` and `AgentHandovers` in a 2-column responsive grid (`grid-cols-1 lg:grid-cols-2`).

The card is rendered for foreign (bundle consumer) installs too: `AgentConfigTab` checks `showOperationalSettings || readOnly`, passing `readOnly={true}` when the agent is a non-publisher bundle install. `AgentHandovers` stays gated on `showOperationalSettings` only.

## Bundle Propagation — Technical Details

### Revision `schedules` field — `AgentBundleRevision`

`AgentBundleRevision.schedules` is a JSON column (`list`, default `[]`). Each entry is a dict with exactly these fields snapshotted from the publisher's `AgentSchedule` rows:

```json
{
  "name": "Daily data collection",
  "cron_string": "0 6 * * 1-5",
  "description": "Every weekday at 7 AM CET",
  "prompt": "Collect today's market data",
  "schedule_type": "static_prompt",
  "command": null,
  "enabled": true
}
```

`next_execution` and `last_execution` are **never** snapshotted — they are per-install runtime state. `cron_string` is stored in UTC (same as the publisher's row). The field is exposed on `AgentBundleRevisionPublic`. Existing revisions before this feature default to `[]` (fully backward compatible).

Because `schedules` is part of the manifest body that feeds `content_hash`, a schedule-only change (no file/prompt change) still produces a new `content_hash` and triggers `INSTALL_UPDATE_AVAILABLE` on foreign installs.

### Schedule Sync helper — `backend/app/services/bundles/schedule_sync.py`

| Function | Purpose |
|----------|---------|
| `snapshot_schedules(schedules)` | Projects `AgentSchedule` rows into the `{name, cron_string, description, prompt, schedule_type, command, enabled}` dict shape for the revision |
| `sig(source)` | Returns the behavioral signature `(schedule_type, cron_string, command, prompt)` from either an `AgentSchedule` row or a snapshot dict |
| `materialise(session, install, revision)` | Creates `AgentSchedule` rows on `install` from `revision.schedules`; called at install time; returns the count of rows created |
| `merge(session, install, revision)` | Reconciles existing rows against the new revision using `sig()` — keeps behaviorally-unchanged rows (refreshing cosmetic fields), deletes removed/changed rows, creates new/changed definitions; commits the session |

### Publish snapshot — `PublishService._collect_schedule_specs`

Called from `_publish_locked` after credential spec collection. Calls `AgentSchedulerService.get_agent_schedules` on the publisher install, then delegates to `snapshot_schedules`. The result is stored in both `manifest["schedules"]` (on-disk) and `revision.schedules` (DB column).

### Install materialisation — `InstallService._materialise_schedules`

Called as step 7 of `_install_from_revision` (after the App MCP route step). Thin wrapper over `schedule_sync.materialise` that owns the commit. Best-effort: a failure logs a warning and marks the install `last_update_status="degraded"` but does not abort the install. The created `AgentSchedule` rows are ordinary rows — the background scheduler picks them up and executes them in the consumer's own environment and sessions with no special handling.

### Apply-update merge — `InstallService.apply_update`

After prompt sync and App MCP route refresh, `schedule_sync.merge(session, install, revision)` is called. Best-effort: a failure logs a warning but does not fail the update.

Merge algorithm:
1. Group new revision definitions by `sig()` into `new_by_sig`
2. For each existing `AgentSchedule` row: if `sig(row)` is in `new_by_sig`, keep the row (preserve `enabled`, `next_execution`, `last_execution`, logs) and refresh cosmetic `name`/`description`; otherwise delete it
3. For each remaining unconsumed definition in `new_by_sig`: create a new `AgentSchedule` with the publisher's `enabled` state and a freshly computed `next_execution`

### Route guards — `backend/app/api/routes/agents.py`

Two helpers enforce the read-only contract on foreign (bundle consumer) installs:

```python
def _is_foreign_install(agent: Agent) -> bool:
    return agent.bundle_uuid is not None and not agent.is_publisher_install

def _guard_foreign_schedule_write(agent: Agent) -> None:
    if _is_foreign_install(agent):
        raise HTTPException(status_code=403, ...)
```

| Endpoint | Foreign install | Publisher install / standalone |
|----------|----------------|-------------------------------|
| `POST /{id}/schedules` | 403 | Allowed |
| `DELETE /{id}/schedules/{id}` | 403 | Allowed |
| `PUT /{id}/schedules/{id}` | Allowed **only when** `exclude_unset` fields ⊆ `{"enabled"}`; any other set field → 403 | Full partial update allowed |
| `POST /{id}/schedules/{id}/run` | Allowed | Allowed |
| `GET /{id}/schedules/{id}/logs` | Allowed | Allowed |

## Configuration

- No feature flags — scheduling is always available
- Minimum CRON interval is per schedule type and enforced **deterministically in the backend** (`AgentSchedulerService.validate_frequency`, called on generate-preview / create / update), not by the AI prompt: `static_prompt` = 10 minutes, `script_trigger` = no minimum. The limit is the smallest gap between consecutive fire times (so `*/40` = 20 min real gap), defined in `AgentSchedulerService.MINIMUM_INTERVAL_MINUTES`. The AI prompt no longer rejects on frequency — it only translates natural language to CRON.
- Background scheduler poll interval: 1 minute (hardcoded in `agent_schedule_scheduler.py`)
- Command timeout default: 120 seconds, max: 300 seconds
- Output truncation: 10,000 characters per stream (stdout/stderr)
- Log display limit: 50 most recent entries per schedule

## Security

- All schedule endpoints verify agent ownership before any operation
- PUT/DELETE endpoints additionally verify `schedule.agent_id == agent_id` (prevents cross-agent access)
- Schedule generation endpoint is stateless — does not persist anything, safe for preview
- Background scheduler runs server-side with direct DB access (no user auth context)
- Commands execute inside the agent's Docker container — same sandbox as agent SDK tool calls; does not expand the attack surface
- Command output is truncated before being sent as session context
- The backend does NOT execute commands on the host — only relays to the container via HTTP
- `command` field validated: non-empty string, max 2000 characters
- `schedule_type` field: enum validation (rejects unknown values)
