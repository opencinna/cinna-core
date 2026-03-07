# Input Tasks — Technical Reference

## File Locations

### Backend

**Models:**
- `backend/app/models/input_task.py` — InputTask table, all schema classes
- `backend/app/models/session.py` — source_task_id, todo_progress, result_state, result_summary additions
- `backend/app/models/agent_handover.py` — auto_feedback field, UpdateSessionStateRequest, RespondToTaskRequest schemas
- `backend/app/models/__init__.py` — exports

**Routes:**
- `backend/app/api/routes/input_tasks.py` — CRUD and action endpoints
- `backend/app/api/routes/agents.py` — session state and feedback agent endpoints
- `backend/app/api/main.py` — router registration

**Services:**
- `backend/app/services/input_task_service.py` — main service
- `backend/app/services/session_service.py` — create_session, list_task_sessions, delete_session additions
- `backend/app/services/activity_service.py` — handle_session_state_updated, handle_task_created, handle_task_status_changed additions
- `backend/app/services/ai_functions_service.py` — refine_task method

**AI Functions:**
- `backend/app/agents/task_refiner.py` — LLM task refinement agent
- `backend/app/agents/prompts/task_refiner_prompt.md` — refinement system prompt
- `backend/app/agents/__init__.py` — exports refine_task

**Agent-Env Tools:**
- `backend/app/env-templates/app_core_base/core/server/tools/update_session_state.py`
- `backend/app/env-templates/app_core_base/core/server/tools/respond_to_task.py`
- `backend/app/env-templates/app_core_base/core/server/adapters/claude_code.py` — tool registration
- `backend/app/env-templates/app_core_base/core/server/prompt_generator.py` — session state prompt injection

**Migrations:**
- `backend/app/alembic/versions/l2g3h4i5j6k7_add_input_task_table.py` — creates input_task table, adds source_task_id to session
- `backend/app/alembic/versions/o5j6k7l8m9n0_add_agent_initiated_fields_to_input_task.py` — agent_initiated, auto_execute, source_session_id
- `backend/app/alembic/versions/p6k7l8m9n0o1_add_todo_progress_to_session.py` — todo_progress on session and input_task
- `backend/app/alembic/versions/u1p2q3r4s5t6_add_session_state_and_task_feedback.py` — result_state, result_summary, auto_feedback, feedback_delivered
- `backend/app/alembic/versions/i6d5e7f8g9h0_add_input_task_id_to_activity.py` — input_task_id FK on activity table

**App Startup:**
- `backend/app/main.py` — event handler registration

### Frontend

**Routes:**
- `frontend/src/routes/_layout/tasks.tsx` — tasks list page
- `frontend/src/routes/_layout/task/$taskId.tsx` — task detail / refinement page

**Components:**
- `frontend/src/components/Tasks/TaskStatusBadge.tsx`
- `frontend/src/components/Tasks/CreateTaskDialog.tsx`
- `frontend/src/components/Tasks/RefinementChat.tsx`
- `frontend/src/components/Tasks/TaskTodoProgress.tsx`
- `frontend/src/components/Chat/SubTasksPanel.tsx`
- `frontend/src/components/Chat/ChatHeader.tsx`
- `frontend/src/components/Chat/MessageBubble.tsx`
- `frontend/src/components/Agents/AgentHandovers.tsx`
- `frontend/src/components/Sidebar/AppSidebar.tsx`

**Generated Client:**
- `frontend/src/client/sdk.gen.ts` — TasksService
- `frontend/src/client/types.gen.ts` — InputTaskPublic, InputTaskPublicExtended, InputTaskCreate, InputTaskUpdate, RefineTaskRequest, RefineTaskResponse, ExecuteTaskRequest, ExecuteTaskResponse

## Database Schema

**Table:** `input_task`

Key fields:
- `owner_id` (UUID FK → user.id)
- `original_message` (str, immutable after creation)
- `current_description` (str, updated during refinement)
- `status` (InputTaskStatus enum)
- `selected_agent_id` (UUID FK → agent.id, nullable)
- `session_id` (UUID FK → session.id, nullable — latest/primary session for quick access)
- `user_workspace_id` (UUID FK → user_workspace.id, nullable)
- `agent_initiated` (bool) — created by agent handover tool
- `auto_execute` (bool) — skip user review and execute immediately
- `source_session_id` (UUID FK → session.id, nullable) — originating agent session
- `auto_feedback` (bool, default=True) — forward state updates to source agent
- `feedback_delivered` (bool, default=False) — prevents duplicate feedback
- `refinement_history` (JSON array) — `{role, content, timestamp}` items, append-only
- `todo_progress` (JSON array) — TodoWrite tool progress from execution
- `created_at`, `updated_at`, `executed_at`, `completed_at`, `archived_at`

**Session model additions** (`backend/app/models/session.py`):
- `source_task_id` (UUID FK → input_task.id, nullable)
- `todo_progress` (JSON array, nullable)
- `result_state` (str, nullable: "completed"|"needs_input"|"error")
- `result_summary` (str, nullable)

## API Endpoints

**File:** `backend/app/api/routes/input_tasks.py`

- `POST /api/v1/tasks` — create new task
- `GET /api/v1/tasks` — list tasks with status/workspace filter
- `GET /api/v1/tasks/{id}` — get task with agent name and sessions info
- `PATCH /api/v1/tasks/{id}` — update task fields
- `DELETE /api/v1/tasks/{id}` — delete task (async; emits ACTIVITY_DELETED for linked activities before CASCADE)
- `POST /api/v1/tasks/{id}/refine` — AI-assisted refinement
- `POST /api/v1/tasks/{id}/execute` — execute task (creates session)
- `POST /api/v1/tasks/{id}/archive` — archive task
- `GET /api/v1/tasks/{id}/sessions` — list all sessions for a task
- `GET /api/v1/tasks/by-source-session/{session_id}` — list tasks created by a source session (SubTasksPanel)

**File:** `backend/app/api/routes/agents.py`

- `POST /api/v1/agents/sessions/update-state` — agent declares session outcome (state + summary)
- `POST /api/v1/agents/tasks/respond` — source agent responds to sub-task clarification

## Services & Key Methods

**InputTaskService** (`backend/app/services/input_task_service.py`):

Exception classes: `InputTaskError`, `TaskNotFoundError`, `AgentNotFoundError`, `PermissionDeniedError`, `ValidationError`

Helper methods:
- `verify_agent_access()` — verify agent exists and user owns it, optionally require active environment
- `get_task_with_ownership_check()` — get task and verify owner
- `parse_status_filter()`, `parse_workspace_filter()` — filter parsing utilities
- `get_task_extended()`, `list_tasks_extended()` — with agent name and session result state joined

CRUD operations:
- `create_task()`, `get_task()`, `list_tasks()`, `update_task()`, `update_status()`, `delete_task()`
- `append_to_refinement_history()` — append-only history update
- `link_session()` — set session_id and status to running
- `reset_task_if_no_sessions()` — reset to NEW if all linked sessions deleted; recomputes if sessions remain

Business logic:
- `refine_task()` — status transition, history append, AI call, description update
- `execute_task_sync()` — agent validation, session creation, linking (sync version for route handler)
- `execute_task()` — async version returning (success, session, error) tuple; used by handover flow
- `create_task_with_auto_refine()` — create + auto-refine if agent has refiner_prompt; returns (task, message_to_send)

Session state and feedback:
- `list_tasks_by_source_session()` — query by source_session_id, joins result_state/result_summary
- `deliver_feedback_to_source()` — create user message in source session, trigger agent if idle

Event handlers (static async):
- `handle_stream_started()` → task = RUNNING
- `handle_stream_completed()` → compute and sync from all sessions
- `handle_stream_error()` → task = ERROR
- `handle_todo_list_updated()` → propagate session todos to task, emit TASK_TODO_UPDATED
- `handle_session_state_updated()` → check auto_feedback, call deliver_feedback_to_source

Email task methods:
- `send_email_answer()` — also deletes email_task_reply_pending activity and emits ACTIVITY_DELETED
- `update_status()` — emits TASK_STATUS_UPDATED for email-originated tasks (triggers activity lifecycle)

**SessionService** (`backend/app/services/session_service.py`):
- `create_session()` — accepts optional source_task_id parameter
- `list_task_sessions()` — list by source_task_id
- `delete_session()` — returns source_task_id of deleted session for post-deletion task reset

**ActivityService** (`backend/app/services/activity_service.py`):
- `handle_session_state_updated()` — maps result_state to activity type, creates activity for offline notification
- `create_completion_activities()` — skips generic activity when result_state already set
- `handle_task_created()` — creates email_task_incoming activity on TASK_CREATED event
- `handle_task_status_changed()` — manages email task activity lifecycle on TASK_STATUS_UPDATED
- `find_activity_by_task_and_type()`, `delete_activity_by_task_and_type()` — task-specific activity helpers

**AIFunctionsService** (`backend/app/services/ai_functions_service.py`):
- `refine_task()` — fetches agent workflow_prompt and refiner_prompt, calls task_refiner LLM agent

## Frontend Components

- `frontend/src/routes/_layout/tasks.tsx` — status filter sidebar, task cards list, CreateTaskDialog, navigate to detail on click
- `frontend/src/routes/_layout/task/$taskId.tsx` — split view: left (description editor, agent selector, execute button, sessions list), right (refinement chat)
- `frontend/src/components/Tasks/TaskStatusBadge.tsx` — color-coded status badges with icons per status
- `frontend/src/components/Tasks/CreateTaskDialog.tsx` — modal with message textarea, optional agent selector, creates task and navigates to refinement page
- `frontend/src/components/Tasks/RefinementChat.tsx` — displays refinement_history (user and AI), comment input, calls TasksService.refineTask() on submit
- `frontend/src/components/Tasks/TaskTodoProgress.tsx` — horizontal progress indicator for TodoWrite tool usage; circles per todo item (pending/in_progress/completed); subscribes to TASK_TODO_UPDATED
- `frontend/src/components/Chat/SubTasksPanel.tsx` — slide-out overlay showing sub-tasks for current session; real-time via SESSION_STATE_UPDATED + 10s polling fallback
- `frontend/src/components/Chat/ChatHeader.tsx` — ListTodo badge showing sub-task count; toggles SubTasksPanel on click
- `frontend/src/components/Chat/MessageBubble.tsx` — detects task_feedback metadata; renders colored border + state icon (CheckCircle2/HelpCircle/AlertTriangle)
- `frontend/src/components/Agents/AgentHandovers.tsx` — auto-feedback toggle switch per handover configuration

## Event Handler Registration

**File:** `backend/app/main.py`

- `TASK_CREATED` → `ActivityService.handle_task_created`
- `TASK_STATUS_UPDATED` → `ActivityService.handle_task_status_changed`
- `SESSION_STATE_UPDATED` → `ActivityService.handle_session_state_updated`
- `SESSION_STATE_UPDATED` → `InputTaskService.handle_session_state_updated`

## Security

- All task operations restricted to owner (or superuser for their own tasks)
- Agent selection validated for ownership at create and update time
- Sessions inherit ownership from task owner
- `original_message` immutable after creation (audit trail)
- `refinement_history` append-only (conversation audit)
- Tasks only linkable to user's own agents
- Agent must have active environment to execute
- Task must have selected_agent_id to execute
- Status transitions validated (archived tasks cannot be re-executed without first becoming active)
