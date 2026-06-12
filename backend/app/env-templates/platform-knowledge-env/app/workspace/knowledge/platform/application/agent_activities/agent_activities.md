# Agent Activities

## Purpose

Activities are a persistent notification/logging system that tracks important system events and agent actions. They keep users informed about what happened in their agent ecosystem, especially during background or unattended sessions.

## Core Concepts

- **Activity**: A single logged event with type, text, read status, archived status, and optional action-required indicator
- **Activity Type**: Category of event (e.g., `session_completed`, `questions_asked`, `error_occurred`, `email_task_incoming`)
- **Action Required**: Flag indicating the activity needs user intervention (e.g., answering agent questions, reviewing an email task)
- **Read/Unread**: Tracking whether the user has seen the activity, with automatic marking after 2 seconds of visibility
- **Archived**: Flag that hides log activities from the main feed without deleting them. Archived activities are visible in the All Logs page.
- **Activity Stats**: Aggregated counts (unread, action-required) used by the sidebar bell indicator — excludes archived activities

## User Stories / Flows

**1. Background Session Notification**
1. User starts an agent session and closes the browser
2. Agent completes work (or encounters error, or asks questions)
3. System creates appropriate activity with `is_read=false`
4. User returns, sees unread indicator on sidebar bell icon
5. User opens Activities page, sees new activity cards
6. After 2 seconds of visibility, activities auto-mark as read

**2. Action-Required Workflow**
1. Agent asks questions while user is disconnected
2. System creates `questions_asked` activity with `action_required="answers_required"`
3. Sidebar bell turns red (destructive color)
4. User clicks activity card, navigates to the session to answer questions

**3. Email Task Activity Lifecycle**
1. Email arrives, system creates input task and `email_task_incoming` activity
2. User reviews and executes the task, `email_task_incoming` is dismissed
3. Task completes, `email_task_reply_pending` activity created
4. User sends email reply, `email_task_reply_pending` dismissed

**4. Session State Declaration**
1. Agent uses `update_session_state` tool to declare outcome (completed/needs_input/error) with a summary
2. System creates activity with the agent's summary text
3. If `needs_input`, activity has `action_required="answers_required"`
4. Generic `session_completed` activity is skipped when agent already declared a result state

**5. Archive Logs**
1. User wants to clean up the Logs section without deleting history
2. User clicks "Archive Logs" — all log activities except live `session_running` ones (including action-required ones such as "Answers required") are marked `is_archived=true` and `is_read=true`
3. Main Activities page no longer shows archived entries
4. User can view full history (including archived) via the All Logs page (`/activities/all`)

## Business Rules

- **One activity per event**: No duplicate activities for the same event (exception: `session_running` is temporary and replaced with final state)
- **User ownership**: Activities are private, only visible to the owning user
- **Cascade deletion**: Activities deleted when related session/agent is deleted (database foreign keys)
- **Read state is one-way**: Once marked read, stays read (no "mark as unread")
- **Action required is immutable**: Set at creation, never changes
- **Session running lifecycle**: `session_running` is temporary — deleted if frontend is watching, replaced with final state if not
- **Error tracking**: Error activities always created, even if frontend was watching (important for audit trail)
- **Workspace inheritance**: Activity inherits `user_workspace_id` from its linked session, task, or agent
- **Result state priority**: When agent declares `result_state` via session state tool, the generic `session_completed` activity is skipped in favor of the more meaningful state-specific activity
- **Archive vs. delete**: `archive_logs` never deletes — it only hides. Archived activities are excluded from stats and the default list, but retrievable via `include_archived=true`
- **Archive scope**: All activities except live `session_running` ones are eligible for archiving — action-required activities (e.g. `answers_required`) are archived too; only `activity_type == "session_running"` is excluded
- **Stats exclude archived**: `unread_count` and `action_required_count` in the stats endpoint always exclude `is_archived=true` activities
- **Session activities carry task link**: When a session was started from an input task, all session lifecycle activities (`session_running`, `session_completed`, `error_occurred`, etc.) are linked to that task via `input_task_id`
- **Task lifecycle agent fallback**: Task lifecycle activities use `source_agent_id` from the event if present; otherwise fall back to `task.selected_agent_id` so `agent_id` is always populated when a task has an assigned agent

### Activity Types

| Type | Text | Action Required | Trigger |
|------|------|-----------------|---------|
| `session_running` | "Session is running" | (none) | Stream starts |
| `session_completed` | "Session completed" | (none) | Stream completes, user disconnected, no result_state |
| `session_feedback_required` | Agent summary | `answers_required` | Agent declares `needs_input` state |
| `questions_asked` | "Agent asked questions that need answers" | `answers_required` | Stream completes with unanswered questions |
| `error_occurred` | "Error: {message}" (truncated 100 chars) | (none) | Stream error |
| `task_completed` | "Task completed" | (none) | Task status → completed |
| `task_failed` | "Task failed" | (none) | Task status → error |
| `task_blocked` | "Task is blocked and requires attention" | `task_action_required` | Task status → blocked |
| `task_cancelled` | "Task was cancelled" | (none) | Task status → cancelled |
| `email_task_incoming` | "New email task received" | `task_review_required` | Email-originated task created |
| `email_task_reply_pending` | "Task completed. Email reply pending." | `reply_pending` | Email task status changes to completed |

### Sidebar Bell Indicator

Located in the sidebar footer. The bell icon has two visual indicators:

**Icon color** (notification state):
- Default: No unread activities
- Primary (blue): Unread informational activities
- Destructive (red): Unread activities requiring action

**Status dot** (connection state — small colored circle on the bell icon):
- Green: Connected (online)
- Yellow: Connecting
- Red: Disconnected (offline)

The tooltip shows both the label "Activities" and the current connection status (e.g., "Activities (Online)").

## Architecture Overview

```
Streaming Events (MessageService) ──→ EventBus ──→ Activity Event Handlers ──→ ActivityService ──→ DB
Email/Task Events ──→ EventBus ──→ Activity Event Handlers ──→ ActivityService ──→ DB
                                                                      │
                                                          WebSocket (ACTIVITY_CREATED/UPDATED/DELETED)
                                                                      │
Frontend Activities Page ←── React Query ←── REST API ←── Activities Routes
Frontend All Logs Page ←── React Query ←── REST API (include_archived=true)
Frontend Sidebar Bell ←── Polling (10s) + WebSocket connection status ←── Stats Endpoint
```

## Event-Driven Architecture

Activities are created/managed by event handlers registered at app startup:

| Event | Handler | Behavior |
|-------|---------|----------|
| `STREAM_STARTED` | `handle_stream_started` | Creates `session_running` activity with `input_task_id` from session |
| `STREAM_COMPLETED` | `handle_stream_completed` | Always creates completion activities; `is_read=True` if user was connected, `is_read=False` if disconnected |
| `STREAM_ERROR` | `handle_stream_error` | Deletes running, always creates `error_occurred` with `input_task_id` |
| `STREAM_INTERRUPTED` | `handle_stream_interrupted` | Deletes running, no completion (session resumable) |
| `SESSION_STATE_UPDATED` | `handle_session_state_updated` | Deletes running, creates state-specific activity with agent summary and `input_task_id` |
| `TASK_CREATED` | `handle_task_created` | Creates `email_task_incoming` (only for email-sourced tasks) |
| `TASK_STATUS_UPDATED` | `handle_task_status_changed` | Creates task lifecycle activities (completed/failed/blocked/cancelled) for ALL tasks; also manages email task activity lifecycle |

**Connected vs Disconnected Detection**: Uses `event_service.is_user_connected(user_id)` to determine `is_read` flag for completion activities. Both connected and disconnected users get completion activities — the difference is only in the read state.

**Task lifecycle agent resolution**: `handle_task_status_changed` reads `source_agent_id` or `changed_by_agent_id` from event meta (supporting both emitter formats). Falls back to `task.selected_agent_id` when neither is present, ensuring `agent_id` is populated whenever a task has an assigned agent.

## Integration Points

- [Event Bus](../realtime_events/event_bus_system.md) - All activity creation is event-driven via `EventService.register_handler()`
- [Agent Sessions](../agent_sessions/agent_sessions.md) - Activities track session lifecycle (running, completed, error, interrupted)
- [Input Tasks](../input_tasks/input_tasks.md) - Task lifecycle activities link to tasks via `input_task_id`; session activities also carry `input_task_id` when the session was started from a task
- [Email Sessions](../email_integration/email_sessions.md) - Email task incoming/reply pending activities
- [Streaming](../realtime_events/frontend_backend_agentenv_streaming.md) - `MessageService` emits streaming events that trigger activity creation
- [Agent Environments](../../agents/agent_environments/agent_environments.md) - Activities resolve agent_id through environment lookup
- [Agent Handover](../../agents/agent_handover/agent_handover.md) - Direct handovers create target-agent sessions that generate the full session activity lifecycle (running → completed/error), notifying target agent owners of delegated work
- [System Notifications](../system_notifications/system_notifications.md) - Creating an `error_occurred` activity is the trigger point for the `session_error` system notification. Both error paths (`create_error_activity()` for stream errors and `handle_session_state_updated()` for agent-declared errors) call `ActivityService._notify_session_error()` after the activity is persisted. The notification is a failure-isolated side-effect; the activity is created and the `ACTIVITY_CREATED` event is emitted regardless of whether the notification succeeds.

## Sidebar Layout

The Activities bell is in the sidebar footer (below the main navigation), alongside dashboard switcher, agentic teams, appearance, and user menu. The main navigation menu (Dashboard, Tasks, Agents, Sessions, Credentials) is in the content area above.

## Activities Page — System Heartbeat Dashboard

The Activities page is a **cross-workspace** dashboard showing the state of the user's entire system at a glance. It presents data in three prioritized sections:

### Section 1: Requires Action (hidden when empty)

Shows only activities where `action_required !== ""`. These are the critical items needing human attention. The section is hidden entirely when there are no pending actions, keeping the page clean.

Items include `session_feedback_required`, `questions_asked`, `email_task_incoming`, and `email_task_reply_pending`. Each row shows the agent badge, session/task title, activity text, and an action-type chip in destructive styling. Clicking navigates to the relevant session or task.

### Section 2: Happening Now

Shows what is actively running right now, across all workspaces. Three types of rows are combined into a single list:

- **Task + Session (combined row)**: When an in-progress task has a session actively streaming (`interaction_status = "running"` or `"pending_stream"`), both are shown as one row — task short code, task title, agent badge, and session title reference. Shows a spinning `Loader2` icon in emerald.
- **Task only**: In-progress task with no currently active session — shows status dot, short code, title, agent badge.
- **Session only**: Active session with no associated task (`source_task_id = null`) — shows mode icon (Wrench for building, MessageCircle for conversation), title, agent badge. Shows "Starting..." label if pending.

This section polls every 15 seconds and also updates via WebSocket events.

### Section 3: Logs (history)

All non-archived activities grouped by time period: **Today → Yesterday → Last 7 days → Older**. Each entry shows: icon (status-colored), entity icon (MessageCircle for sessions, ClipboardList for tasks), agent badge, event text, relative timestamp (right-aligned), and an unread dot indicator. The `session_running` type gets emerald background and a spinning icon.

**Auto-mark-as-read**: An `IntersectionObserver` (threshold 1.0) tracks fully visible log entries. After 2 seconds of full visibility, entries are marked as read via the batch mark-read endpoint.

**Archive Logs button**: Calls `POST /activities/archive-logs`. Archived activities disappear from this section but remain accessible via the All Logs page.

**Show All Logs dropdown**: Links to `/activities/all` — the full paginated log view including archived activities.

### Cross-Workspace Behavior

All three API calls on the Activities page omit the `userWorkspaceId` parameter entirely, causing the backend to return data from all workspaces owned by the user. This page does not respect the workspace switcher — it always shows everything.

## All Logs Page (`/activities/all`)

A compact secondary page showing the complete activity log with pagination (100 per page), including archived activities. Useful for reviewing historical events that have been archived from the main feed. Does not have Requires Action or Happening Now sections — logs only.

## Statistics & Polling

- Stats endpoint returns `unread_count` and `action_required_count`
- Both counts exclude `is_archived=true` activities
- Sidebar polls stats every 10 seconds, plus real-time WebSocket event invalidation
- Activities page subscribes to WebSocket events for real-time updates: `ACTIVITY_CREATED`, `ACTIVITY_UPDATED`, `ACTIVITY_DELETED`, `SESSION_INTERACTION_STATUS_CHANGED`, `SESSION_STATE_UPDATED`, `TASK_STATUS_CHANGED`
- Activities: fetched with limit 200 (cross-workspace), archived excluded by default
- Sessions and tasks: polled every 15 seconds with `refetchInterval`

## Future Enhancements

- Activity grouping (e.g., "Agent created 5 files")
- User preferences for activity types
- Browser push notifications for action-required activities
- Activity search and filtering by date/type/read status
- Mark all as read bulk action in Logs section header
- Auto-delete old activities after retention period
- Task progress bar in combined task+session rows (using `todo_progress`)
