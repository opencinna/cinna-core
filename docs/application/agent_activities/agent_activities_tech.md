# Agent Activities - Technical Details

## File Locations

### Backend

- **Model**: `backend/app/models/sessions/activity.py` - `Activity` (table), `ActivityCreate`, `ActivityUpdate`, `ActivityPublic`, `ActivityPublicExtended`, `ActivitiesPublic`, `ActivitiesPublicExtended`, `ActivityStats`
- **Service**: `backend/app/services/events/activity_service.py` - `ActivityService` (CRUD + event handlers)
- **Routes**: `backend/app/api/routes/activities.py` - REST API endpoints
- **Event Registration**: `backend/app/main.py` - Registers activity event handlers on startup
- **Event Types**: `backend/app/models/events/event.py` - `ACTIVITY_CREATED`, `ACTIVITY_UPDATED`, `ACTIVITY_DELETED`, `STREAM_STARTED`, `STREAM_COMPLETED`, `STREAM_ERROR`, `STREAM_INTERRUPTED`, `SESSION_STATE_UPDATED`, `TASK_CREATED`, `TASK_STATUS_UPDATED`

### Frontend

- **Activities Page**: `frontend/src/routes/_layout/activities.tsx` - Main activity list with filtering, auto-read, navigation
- **Sidebar Integration**: `frontend/src/components/Sidebar/AppSidebar.tsx` - `ActivitiesMenu` component with bell icon and stats polling
- **API Client**: `frontend/src/client/sdk.gen.ts` - Auto-generated `ActivitiesService`

### Migrations

- `backend/app/alembic/versions/580bc34f4161_add_activity_table.py` - Creates activity table
- `backend/app/alembic/versions/i6d5e7f8g9h0_add_input_task_id_to_activity.py` - Adds `input_task_id` FK

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
| `created_at` | datetime | Auto-set to UTC now |

**Recommended indexes**: `(user_id, is_read, action_required)` for stats endpoint performance.

## API Endpoints

**Route file**: `backend/app/api/routes/activities.py` (prefix: `/activities`, tag: `activities`)

| Method | Path | Purpose |
|--------|------|---------|
| `POST /activities/` | Create activity (validates agent/session ownership) |
| `GET /activities/` | List activities with optional `agent_id`, `user_workspace_id` filter, pagination, ordering. Joins Agent (name, color) and Session (title) |
| `GET /activities/stats` | Returns `unread_count` and `action_required_count` |
| `PATCH /activities/{id}` | Update activity (mark as read). Emits `ACTIVITY_UPDATED` WebSocket event |
| `POST /activities/mark-read` | Batch mark activities as read. Emits `ACTIVITY_UPDATED` for each |
| `DELETE /activities/{id}` | Delete single activity. Emits `ACTIVITY_DELETED` WebSocket event |
| `DELETE /activities/` | Delete all activities for current user. Emits `ACTIVITY_DELETED` for each |

## Services & Key Methods

### `backend/app/services/events/activity_service.py` - `ActivityService`

**CRUD Methods:**
- `create_activity()` - Creates activity, auto-inherits `user_workspace_id` from linked session/task/agent
- `get_activity()` - Get by ID
- `update_activity()` - Generic update (partial)
- `mark_as_read()` - Mark single activity as read
- `mark_multiple_as_read()` - Batch mark as read
- `list_user_activities()` - List with agent filter, pagination, ordering
- `get_activity_stats()` - Count unread and action-required activities
- `delete_activity()` - Delete by ID

**Session/Task Lookup Methods:**
- `find_activity_by_session_and_type()` - Find by session_id + activity_type (latest)
- `delete_activity_by_session_and_type()` - Delete by session_id + activity_type
- `find_activity_by_task_and_type()` - Find by input_task_id + activity_type
- `delete_activity_by_task_and_type()` - Delete by input_task_id + activity_type (returns deleted activity for event emission)

**Streaming Lifecycle Methods:**
- `create_session_running_activity()` - Creates running activity, emits `ACTIVITY_CREATED`
- `delete_session_running_activity()` - Deletes running activity, emits `ACTIVITY_DELETED`
- `create_error_activity()` - Creates error activity with truncated message
- `create_completion_activities()` - Creates `session_completed` + optional `questions_asked`. Skips if session has `result_state` set
- `transition_running_to_completion()` - Deletes running, creates completion activities
- `transition_running_to_error()` - Deletes running, creates error activity

**Event Handlers (registered in `backend/app/main.py`):**
- `handle_stream_started()` - `STREAM_STARTED` → creates `session_running`
- `handle_stream_completed()` - `STREAM_COMPLETED` → connected: delete running; disconnected: transition to completion
- `handle_stream_error()` - `STREAM_ERROR` → delete running + create `error_occurred`
- `handle_stream_interrupted()` - `STREAM_INTERRUPTED` → delete running
- `handle_session_state_updated()` - `SESSION_STATE_UPDATED` → delete running + create state-specific activity with agent summary
- `handle_task_created()` - `TASK_CREATED` → create `email_task_incoming` (email tasks only)
- `handle_task_status_changed()` - `TASK_STATUS_UPDATED` → manage email task activity lifecycle

## Frontend Components

### `frontend/src/routes/_layout/activities.tsx` - `ActivitiesList`

- Fetches activities via `ActivitiesService.listActivities()` with React Query
- Agent filter sidebar using `AgentsService.readAgents()`
- `IntersectionObserver` tracks fully-visible activities (threshold: 1.0)
- After 2s visibility, calls `markAsReadMutation` → `ActivitiesService.markActivitiesAsRead()`
- Subscribes to `ACTIVITY_CREATED`, `ACTIVITY_UPDATED`, `ACTIVITY_DELETED` WebSocket events for real-time updates
- Click handler: navigates to `/task/$taskId` for task activities or `/session/$sessionId` for session activities
- Visual: `session_running` gets emerald background + spinning `Loader2` icon; email types get `Mail` icon
- "Clear All" dropdown menu calls `ActivitiesService.deleteAllActivities()`
- Workspace-filtered via `useWorkspace()` hook

### `frontend/src/components/Sidebar/AppSidebar.tsx` - `ActivitiesMenu`

- Polls `ActivitiesService.getActivityStats()` every 10 seconds via `refetchInterval`
- Bell icon color: default (no unread), green/primary (unread), red/destructive (action required)
- Positioned between Dashboard and Agents menu items
- Subscribes to activity WebSocket events to invalidate stats cache

## Configuration

- No dedicated env vars
- Stats polling interval: 10 seconds (hardcoded in `ActivitiesMenu`)
- Default pagination limit: 100 activities
- Auto-read delay: 2 seconds (hardcoded in `ActivitiesList`)

## Security

- All routes require `CurrentUser` authentication
- Ownership verification on create (validates agent/session belongs to user)
- Ownership verification on update/delete (activity must belong to user or user is superuser)
- Activities filtered by `user_id` - users can only see their own
- Cascade deletes clean up activities when parent entities are removed

## Related Context

- Session model with `interaction_status` and `result_state`: `backend/app/models/sessions/session.py`
- Message model with `tool_questions_status`: `backend/app/models/sessions/session.py` (SessionMessage)
- Message service emitting streaming events: `backend/app/services/sessions/message_service.py`
- Event bus system: `backend/app/services/events/event_service.py`
- User connection tracking: `backend/app/services/events/event_service.py:is_user_connected()`
- Active streaming manager: `backend/app/services/sessions/active_streaming_manager.py`
