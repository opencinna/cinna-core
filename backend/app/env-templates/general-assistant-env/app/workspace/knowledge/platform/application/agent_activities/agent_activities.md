# Agent Activities

## Purpose

Activities are a persistent notification/logging system that tracks important system events and agent actions. They keep users informed about what happened in their agent ecosystem, especially during background or unattended sessions.

## Core Concepts

- **Activity**: A single logged event with type, text, read status, and optional action-required indicator
- **Activity Type**: Category of event (e.g., `session_completed`, `questions_asked`, `error_occurred`, `email_task_incoming`)
- **Action Required**: Flag indicating the activity needs user intervention (e.g., answering agent questions, reviewing an email task)
- **Read/Unread**: Tracking whether the user has seen the activity, with automatic marking after 2 seconds of visibility
- **Activity Stats**: Aggregated counts (unread, action-required) used by the sidebar bell indicator

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

## Business Rules

- **One activity per event**: No duplicate activities for the same event (exception: `session_running` is temporary and replaced with final state)
- **User ownership**: Activities are private, only visible to the owning user
- **Cascade deletion**: Activities deleted when related session/agent is deleted (database foreign keys)
- **Read state is one-way**: Once marked read, stays read (no "mark as unread")
- **Action required is immutable**: Set at creation, never changes
- **Session running lifecycle**: `session_running` is temporary - deleted if frontend is watching, replaced with final state if not
- **Error tracking**: Error activities always created, even if frontend was watching (important for audit trail)
- **Workspace inheritance**: Activity inherits `user_workspace_id` from its linked session, task, or agent
- **Result state priority**: When agent declares `result_state` via session state tool, the generic `session_completed` activity is skipped in favor of the more meaningful state-specific activity

### Activity Types

| Type | Text | Action Required | Trigger |
|------|------|-----------------|---------|
| `session_running` | "Session is running" | (none) | Stream starts |
| `session_completed` | "Session completed" | (none) | Stream completes, user disconnected, no result_state |
| `session_feedback_required` | Agent summary | `answers_required` | Agent declares `needs_input` state |
| `questions_asked` | "Agent asked questions that need answers" | `answers_required` | Stream completes with unanswered questions |
| `error_occurred` | "Error: {message}" (truncated 100 chars) | (none) | Stream error |
| `email_task_incoming` | "New email task received" | `task_review_required` | Email-originated task created |
| `email_task_reply_pending` | "Task completed. Email reply pending." | `reply_pending` | Email task status changes to completed |

### Sidebar Bell Indicator

- Default color: No unread activities
- Green (primary): Unread informational activities
- Red (destructive): Unread activities requiring action

## Architecture Overview

```
Streaming Events (MessageService) ──→ EventBus ──→ Activity Event Handlers ──→ ActivityService ──→ DB
Email/Task Events ──→ EventBus ──→ Activity Event Handlers ──→ ActivityService ──→ DB
                                                                      │
                                                          WebSocket (ACTIVITY_CREATED/UPDATED/DELETED)
                                                                      │
Frontend Activities Page ←── React Query ←── REST API ←── Activities Routes
Frontend Sidebar Bell ←── Polling (10s) ←── Stats Endpoint
```

## Event-Driven Architecture

Activities are created/managed by event handlers registered at app startup:

| Event | Handler | Behavior |
|-------|---------|----------|
| `STREAM_STARTED` | `handle_stream_started` | Creates `session_running` activity |
| `STREAM_COMPLETED` | `handle_stream_completed` | If user connected: deletes running. If disconnected: creates completion + questions activities |
| `STREAM_ERROR` | `handle_stream_error` | Deletes running, always creates `error_occurred` |
| `STREAM_INTERRUPTED` | `handle_stream_interrupted` | Deletes running, no completion (session resumable) |
| `SESSION_STATE_UPDATED` | `handle_session_state_updated` | Deletes running, creates state-specific activity with agent summary |
| `TASK_CREATED` | `handle_task_created` | Creates `email_task_incoming` (only for email-sourced tasks) |
| `TASK_STATUS_UPDATED` | `handle_task_status_changed` | Manages email task activity lifecycle (dismiss incoming, create/dismiss reply_pending) |

**Connected vs Disconnected Detection**: Uses `event_service.is_user_connected(user_id)` to determine whether to create notification activities or just clean up the running indicator.

## Integration Points

- [Event Bus](../realtime_events/event_bus_system.md) - All activity creation is event-driven via `EventService.register_handler()`
- [Agent Sessions](../agent_sessions/agent_sessions.md) - Activities track session lifecycle (running, completed, error, interrupted)
- [Input Tasks](../input_tasks/input_tasks.md) - Email task activities link to tasks via `input_task_id`
- [Email Sessions](../email_integration/email_sessions.md) - Email task incoming/reply pending activities
- [Streaming](../realtime_events/frontend_backend_agentenv_streaming.md) - `MessageService` emits streaming events that trigger activity creation
- [Agent Environments](../../agents/agent_environments/agent_environments.md) - Activities resolve agent_id through environment lookup
- [Agent Handover](../../agents/agent_handover/agent_handover.md) - Direct handovers create target-agent sessions that generate the full session activity lifecycle (running → completed/error), notifying target agent owners of delegated work

## Statistics & Polling

- Stats endpoint returns `unread_count` and `action_required_count`
- Sidebar polls stats every 10 seconds
- Activities list page subscribes to WebSocket events for real-time updates
- Pagination: default limit 100, loaded at once

## Future Enhancements

- Activity grouping (e.g., "Agent created 5 files")
- User preferences for activity types
- Browser push notifications for action-required activities
- Activity search and filtering by date/type/read status
- Mark all as read bulk action
- Auto-delete old activities after retention period
