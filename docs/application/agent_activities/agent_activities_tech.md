# Agent Activities - Technical Details

## File Locations

### Backend

- **Model**: `backend/app/models/sessions/activity.py` - `Activity` (table), `ActivityCreate`, `ActivityUpdate`, `ActivityPublic`, `ActivityPublicExtended`, `ActivitiesPublic`, `ActivitiesPublicExtended`, `ActivityStats`
- **Service**: `backend/app/services/events/activity_service.py` - `ActivityService` (CRUD + event handlers), `ActivityNotFoundError`, `ActivityPermissionError`
- **Routes**: `backend/app/api/routes/activities.py` - REST API endpoints (thin controllers, all logic in service)
- **Event Registration**: `backend/app/main.py` - Registers activity event handlers on startup
- **Event Types**: `backend/app/models/events/event.py` - `ACTIVITY_CREATED`, `ACTIVITY_UPDATED`, `ACTIVITY_DELETED`, `STREAM_STARTED`, `STREAM_COMPLETED`, `STREAM_ERROR`, `STREAM_INTERRUPTED`, `SESSION_STATE_UPDATED`, `TASK_CREATED`, `TASK_STATUS_UPDATED`

### Frontend

- **Activities Page**: `frontend/src/routes/_layout/activities.tsx` - Main activity list with three sections, archive button, Show All Logs link
- **All Logs Page**: `frontend/src/routes/_layout/activities-all.tsx` - Compact paginated log view including archived activities
- **Sidebar Integration**: `frontend/src/components/Sidebar/AppSidebar.tsx` - `ActivitiesMenu` component with bell icon and stats polling
- **API Client**: `frontend/src/client/sdk.gen.ts` - Auto-generated `ActivitiesService`

### Migrations

- `backend/app/alembic/versions/580bc34f4161_add_activity_table.py` - Creates activity table
- `backend/app/alembic/versions/i6d5e7f8g9h0_add_input_task_id_to_activity.py` - Adds `input_task_id` FK
- `backend/app/alembic/versions/559320c34180_add_is_archived_to_activity.py` - Adds `is_archived` column (bool, default false)

## Database Schema

**Table**: `activity`

| Field | Type | Details |
|-------|------|---------|
| `id` | UUID | Primary key, auto-generated |
| `user_id` | UUID | FK → `user.id`, CASCADE delete |
| `session_id` | UUID (nullable) | FK → `session.id`, CASCADE delete |
| `agent_id` | UUID (nullable) | FK → `agent.id`, SET NULL on delete |
| `user_workspace_id` | UUID (nullable) | FK → `user_workspace.id`, CASCADE delete |
| `input_task_id` | UUID (nullable) | FK → `input_task.id`, CASCADE delete |
| `activity_type` | str | Event type identifier |
| `text` | str | Human-readable description |
| `action_required` | str | Empty string or action type (e.g., `"answers_required"`) |
| `is_read` | bool | Default `false` |
| `is_archived` | bool | Default `false` — hides activity from main feed without deleting |
| `created_at` | datetime | Auto-set to UTC now |

**Recommended indexes**: `(user_id, is_read, action_required)` for stats endpoint performance; `(user_id, is_archived)` for list endpoint filtering.

## API Endpoints

**Route file**: `backend/app/api/routes/activities.py` (prefix: `/activities`, tag: `activities`)

All business logic is in `ActivityService` — routes are thin controllers that call the service and map exceptions to HTTP responses via `_handle_activity_error()`.

| Method | Path | Purpose |
|--------|------|---------|
| `POST /activities/` | Create activity (validates agent/session ownership) |
| `GET /activities/` | List activities. Params: `agent_id`, `user_workspace_id`, `include_archived` (bool, default false), `skip`, `limit`, `order_desc`. Joins Agent, Session, InputTask for extended fields |
| `POST /activities/archive-logs` | Archive all log activities except live ones (type≠`session_running`); includes action-required activities. Returns `{"archived_count": N}` |
| `GET /activities/stats` | Returns `unread_count` and `action_required_count` (both exclude archived) |
| `PATCH /activities/{id}` | Update activity (mark as read). Emits `ACTIVITY_UPDATED` WebSocket event. Async |
| `POST /activities/mark-read` | Batch mark activities as read. Emits `ACTIVITY_UPDATED` for each. Async |
| `DELETE /activities/{id}` | Delete single activity. Emits `ACTIVITY_DELETED`. Async |
| `DELETE /activities/` | Delete all activities for current user. Emits `ACTIVITY_DELETED` for each. Async |

## Services & Key Methods

### `backend/app/services/events/activity_service.py` - `ActivityService`

**Custom Exceptions:**
- `ActivityNotFoundError` — activity does not exist
- `ActivityPermissionError` — user does not own the activity

**Ownership Helper:**
- `get_activity_with_ownership_check()` — fetches activity and raises `ActivityNotFoundError` / `ActivityPermissionError`

**CRUD Methods:**
- `create_activity()` — Creates activity, auto-inherits `user_workspace_id` from linked session/task/agent
- `get_activity()` — Get by ID (no ownership check)
- `update_activity()` — Async; ownership-checked update with `ACTIVITY_UPDATED` WebSocket emission
- `mark_as_read()` — Mark single activity as read
- `mark_multiple_as_read()` — Async; batch mark as read with `ACTIVITY_UPDATED` event per activity
- `get_activity_stats()` — Count unread and action-required; excludes `is_archived=true`
- `delete_activity_for_user()` — Async; ownership-checked delete with `ACTIVITY_DELETED` emission
- `delete_all_for_user()` — Async; bulk delete with `ACTIVITY_DELETED` per activity; returns count
- `delete_activity()` — Internal-only delete (no ownership check, no event)

**Extended Listing:**
- `_parse_workspace_filter()` — Parses `user_workspace_id` string param: `None`→no filter, `""`→NULL workspace, UUID string→specific workspace. Raises `ValueError` for invalid UUIDs
- `list_user_activities_extended()` — Joins Activity + Agent + Session + InputTask. Supports `include_archived` flag (default False). Returns `ActivitiesPublicExtended` with `task_short_code` and `task_title` fields

**Archive:**
- `archive_logs()` — Marks `is_archived=True` and `is_read=True` for all activities where `activity_type != "session_running"` (action-required activities included; only live `session_running` excluded). Returns count archived

**Session/Task Lookup Methods:**
- `find_activity_by_session_and_type()` — Find by session_id + activity_type (latest)
- `delete_activity_by_session_and_type()` — Delete by session_id + activity_type
- `find_activity_by_task_and_type()` — Find by input_task_id + activity_type
- `delete_activity_by_task_and_type()` — Delete by input_task_id + activity_type (returns deleted activity for event emission)

**Streaming Lifecycle Methods:**
- `create_session_running_activity()` — Creates running activity with `input_task_id=session.source_task_id`, emits `ACTIVITY_CREATED`
- `delete_session_running_activity()` — Deletes running activity, emits `ACTIVITY_DELETED`
- `create_error_activity()` — Creates error activity with truncated message; includes `input_task_id=session.source_task_id`; also calls `_notify_session_error()` to dispatch the generic `session_error` system notification to the session owner (see [System Notifications tech](../system_notifications/system_notifications_tech.md))
- `create_completion_activities()` — Creates `session_completed` + optional `questions_asked` with `input_task_id=session.source_task_id`. Skips if session has `result_state` set or status is `"error"`
- `transition_running_to_completion()` — Deletes running, creates completion activities
- `transition_running_to_error()` — Deletes running, creates error activity

**Event Handlers (registered in `backend/app/main.py`):**
- `handle_stream_started()` — `STREAM_STARTED` → creates `session_running` (with `input_task_id`)
- `handle_stream_completed()` — `STREAM_COMPLETED` → always creates completion; `is_read=True` when user connected, `is_read=False` when disconnected
- `handle_stream_error()` — `STREAM_ERROR` → delete running + create `error_occurred` (with `input_task_id`)
- `handle_stream_interrupted()` — `STREAM_INTERRUPTED` → delete running
- `handle_session_state_updated()` — `SESSION_STATE_UPDATED` → delete running + create state-specific activity with agent summary and `input_task_id`; when `state="error"`, also calls `_notify_session_error()` to dispatch the `session_error` system notification (see [System Notifications tech](../system_notifications/system_notifications_tech.md))
- `handle_task_created()` — `TASK_CREATED` → create `email_task_incoming` (email tasks only)
- `handle_task_status_changed()` — `TASK_STATUS_UPDATED` → task lifecycle activities for ALL tasks + email task activity lifecycle. Reads both `new_status`/`to_status` and `source_agent_id`/`changed_by_agent_id` from event meta (dual-format support). Falls back to `task.selected_agent_id` when no agent in event meta

**Task lifecycle map** (`_TASK_LIFECYCLE_MAP`):

| Status | Activity Type | Text | Action Required |
|--------|--------------|------|-----------------|
| `completed` | `task_completed` | "Task completed" | "" |
| `error` | `task_failed` | "Task failed" | "" |
| `blocked` | `task_blocked` | "Task is blocked and requires attention" | `task_action_required` |
| `cancelled` | `task_cancelled` | "Task was cancelled" | "" |

## Frontend Components

### `frontend/src/routes/_layout/activities.tsx` - `ActivitiesList`

The Activities page is a three-section **cross-workspace** system heartbeat dashboard. No workspace filter is applied — all API calls omit `userWorkspaceId` to return data from all workspaces.

**React Query keys and data sources:**

| Query key | Service call | Purpose |
|-----------|-------------|---------|
| `["activities-all"]` | `ActivitiesService.listActivities({ limit: 200 })` | All non-archived activities for Requires Action + Logs sections |
| `["sessions-active"]` | `SessionsService.listSessions({ limit: 50 })` | All sessions, filtered client-side for active ones |
| `["tasks-in-progress"]` | `TasksService.listTasks({ status: "in_progress" })` | All in-progress tasks |

**Inline sub-components:**

- `ActionRequiredRow` — for activities where `action_required !== ""`; destructive/alert styling with action-type chip
- `TaskWithSessionRow` / `TaskOnlyRow` / `SessionOnlyRow` — for the Happening Now section
- `LogEntryRow` (using `forwardRef`) — for time-grouped log entries; the only rows attached to the `IntersectionObserver` for auto-mark-as-read. Shows status-colored icon + entity icon (MessageCircle for sessions, ClipboardList for tasks)

**`HappeningItem` union type** (discriminated by `kind` field):
```typescript
type HappeningItem =
  | { kind: "task_with_session"; task: InputTaskPublicExtended; session: SessionPublicExtended }
  | { kind: "task_only"; task: InputTaskPublicExtended }
  | { kind: "session_only"; session: SessionPublicExtended }
```

**Combination logic** (in `useMemo`): Sessions are filtered client-side for `interaction_status === "running" || "pending_stream"`. Each in-progress task is matched to an active session via `task.latest_session_id`. Matched pairs become `task_with_session` rows; unmatched tasks become `task_only` rows. Active sessions with `source_task_id === null` that weren't claimed by any task become `session_only` rows.

**WebSocket subscriptions:** `ACTIVITY_CREATED`, `ACTIVITY_UPDATED`, `ACTIVITY_DELETED`, `SESSION_INTERACTION_STATUS_CHANGED`, `SESSION_STATE_UPDATED`, `TASK_STATUS_CHANGED` — all invalidate all three query keys and `["activity-stats"]`

**Polling:** Sessions and tasks queries use `refetchInterval: 15000`

**Auto-mark-as-read:** `IntersectionObserver` (threshold: 1.0) applied only to `LogEntryRow` refs. After 2s visibility, calls `ActivitiesService.markActivitiesAsRead()`.

**"Archive Logs"** button calls `ActivitiesService.archiveLogs()` → `POST /activities/archive-logs`.

**"Show All Logs"** dropdown item links to `/activities/all`.

**No `useWorkspace` hook** — the page intentionally ignores the active workspace.

### `frontend/src/routes/_layout/activities-all.tsx` - All Logs Page

A compact secondary view for the full activity history including archived entries.

- Calls `ActivitiesService.listActivities({ includeArchived: true, limit: 100, skip: offset })` for paginated access
- Pagination by 100 records, previous/next controls
- No Requires Action or Happening Now sections — logs only
- Same cross-workspace behavior (no workspace filter)

### `frontend/src/components/Sidebar/AppSidebar.tsx` - `ActivitiesMenu`

- Polls `ActivitiesService.getActivityStats()` every 10 seconds via `refetchInterval`
- Bell icon color: default (no unread), primary/blue (unread informational), destructive/red (action required)
- Positioned in sidebar footer
- Subscribes to activity WebSocket events to invalidate stats cache

## Model Schemas

### `ActivityPublicExtended`

Extended response model that includes joined data from related entities:

| Field | Source |
|-------|--------|
| `agent_name` | `Agent.name` |
| `agent_ui_color_preset` | `Agent.ui_color_preset` |
| `session_title` | `Session.title` |
| `task_short_code` | `InputTask.short_code` |
| `task_title` | `InputTask.title` |

These fields are `null` when the activity has no corresponding linked entity.

## Configuration

- No dedicated env vars
- Stats polling interval: 10 seconds (hardcoded in `ActivitiesMenu`)
- Sessions/tasks polling interval: 15 seconds (hardcoded in `ActivitiesList`)
- Activities fetch limit: 200 (cross-workspace, non-archived)
- All Logs page size: 100 per page
- Auto-read delay: 2 seconds (hardcoded in `ActivitiesList`)

## Security

- All routes require `CurrentUser` authentication
- Ownership verification on create (validates agent/session belongs to user — superuser check removed, strict owner-only)
- Ownership verification on update/delete via `get_activity_with_ownership_check()` — raises `ActivityPermissionError` (→ HTTP 400) rather than 404 to avoid leaking existence
- Activities filtered by `user_id` — users can only see their own
- Cascade deletes clean up activities when parent entities are removed

## Related Context

- Session model with `interaction_status` and `result_state`: `backend/app/models/sessions/session.py`
- Message model with `tool_questions_status`: `backend/app/models/sessions/session.py` (SessionMessage)
- Message service emitting streaming events: `backend/app/services/sessions/message_service.py`
- Event bus system: `backend/app/services/events/event_service.py`
- User connection tracking: `backend/app/services/events/event_service.py` — `is_user_connected()`
- Active streaming manager: `backend/app/services/sessions/active_streaming_manager.py`
- Input task model with `short_code` and `title`: `backend/app/models/tasks/input_task.py`
- Background task error handling: `backend/app/utils.py` — `create_task_with_error_logging()` (handles sync worker threads via `anyio.from_thread`)
