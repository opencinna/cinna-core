# Input Task Management - Implementation Reference

## Purpose

Enable users to receive, refine, and execute incoming tasks through an AI-assisted preparation workflow. Tasks often arrive with incomplete information - this feature provides a structured way to transform vague requests into detailed, agent-ready instructions before execution.

## Feature Overview

**Flow (User-Initiated):**
1. User creates new task (manual entry or external source)
2. User opens task refinement screen with split view
3. Left panel: Task description editor + agent selector + execute button + sessions list
4. Right panel: AI refinement chat interface
5. User sends refinement comments → AI refines description and provides feedback
6. User reviews, continues refining, or clicks Execute
7. On Execute: system creates session linked to task via `source_task_id`
8. User can execute same task multiple times (creates additional sessions)
9. Task status syncs with session state (running, pending_input, completed, error)
10. User archives completed tasks to clear from active view

**Flow (Agent-Initiated via Handover):**
1. Source agent triggers handover tool with message for target agent
2. System creates InputTask with `agent_initiated=true`, `auto_execute=true`
3. If target agent has `refiner_prompt`, message is auto-refined
4. System auto-creates session and sends the (possibly refined) message
5. Task appears in Tasks list with `agent_initiated` flag
6. Task status syncs with session state as usual

## Architecture

```
Frontend UI → Backend API → AI Functions → Session Service
(React)       (FastAPI)     (LLM Refiner)  (Creates Sessions)

Task (1) ─────────────> (N) Session
       session_id            source_task_id
       (latest/primary)      (authoritative FK)
```

**Key Relationship:** A single task can spawn multiple sessions (retries, re-runs). The `Session.source_task_id` field links sessions back to their originating task.

## Data/State Lifecycle

### Task Status States

| Status | Description | User Action |
|--------|-------------|-------------|
| `new` | Task created, not yet refined | Refine or Execute |
| `refining` | User actively refining | Continue or Execute |
| `running` | Session active, agent working | Monitor |
| `pending_input` | Agent needs user input | Go to session |
| `completed` | Agent finished successfully | Archive |
| `error` | Agent encountered error | Review, retry, Archive |
| `archived` | User archived task | - |

### Session-to-Task Status Sync

When a task has connected sessions (via `source_task_id`), the task status automatically syncs based on session states:

**Computation Logic (priority order):**
- If ANY session has `status='error'` → task = `ERROR`
- If ANY session has unanswered `tool_questions_status='unanswered'` → task = `PENDING_INPUT`
- If ANY session is active with `interaction_status='running'` → task = `RUNNING`
- If ALL sessions are `completed` → task = `COMPLETED`
- Otherwise → task = `RUNNING` (active but not streaming)

**Status Protection:**
- Only tasks in execution phase (`running`, `pending_input`, `completed`, `error`) are synced
- Tasks in `new`, `refining`, or `archived` status are NOT overridden by session events

**Event Handlers:**
- `STREAM_STARTED` → syncs task to `RUNNING`
- `STREAM_COMPLETED` → computes and syncs status from all sessions
- `STREAM_ERROR` → syncs task to `ERROR`
- `TODO_LIST_UPDATED` → propagates session todo progress to linked task, emits `TASK_TODO_UPDATED`

### Refinement History Structure

Stored as JSON array in `InputTask.refinement_history`:
- Each item: `{ role: "user"|"ai", content: string, timestamp: ISO8601 }`
- Append-only for audit trail
- Last 5 items passed as context to AI refiner

### Todo Progress Tracking

When an agent uses the TodoWrite tool during execution, the progress is tracked for real-time display:

**Data Structure** (stored in `Session.todo_progress` and `InputTask.todo_progress`):
```json
[
  {"content": "Run the build", "activeForm": "Running the build", "status": "completed"},
  {"content": "Fix type errors", "activeForm": "Fixing type errors", "status": "in_progress"},
  {"content": "Update tests", "activeForm": "Updating tests", "status": "pending"}
]
```

**Event Flow:**
1. Agent calls TodoWrite tool during message processing
2. `MessageService.stream_message_with_events()` detects the tool call
3. Session's `todo_progress` is updated in database
4. `TODO_LIST_UPDATED` event is emitted with session_id and todos
5. `InputTaskService.handle_todo_list_updated()` handler:
   - Checks if session is linked to a task via `source_task_id`
   - Saves todos to task's `todo_progress` for persistence
   - Emits `TASK_TODO_UPDATED` event for frontend real-time updates

**Frontend Display:**
- `TaskTodoProgress` component shows horizontal progress indicator
- Each todo is a circle (pending), spinning loader (in_progress), or checkmark (completed)
- Tooltip shows full task content on hover
- Current in-progress step shown as text hint

## Database Schema

**Migrations:**
- `backend/app/alembic/versions/l2g3h4i5j6k7_add_input_task_table.py` - Creates `input_task` table, adds `source_task_id` to `session`
- `backend/app/alembic/versions/o5j6k7l8m9n0_add_agent_initiated_fields_to_input_task.py` - Adds agent handover fields
- `backend/app/alembic/versions/p6k7l8m9n0o1_add_todo_progress_to_session.py` - Adds `todo_progress` JSON field to session and input_task tables

**Models:** `backend/app/models/input_task.py`
- `InputTask` - Database table with:
  - Core fields: owner_id, original_message, current_description, status
  - Agent fields: selected_agent_id, session_id, user_workspace_id
  - **Agent-initiated fields**: `agent_initiated` (bool), `auto_execute` (bool), `source_session_id` (UUID, FK to session)
  - History: refinement_history (JSON array)
  - **Todo progress**: `todo_progress` (JSON array) - Tracks TodoWrite tool progress from agent execution
  - Timestamps: created_at, updated_at, executed_at, completed_at, archived_at
- `InputTaskCreate`, `InputTaskUpdate` - API input schemas (Create includes agent_initiated, auto_execute, source_session_id)
- `InputTaskPublic`, `InputTaskPublicExtended` - API response schemas (extended includes agent_name)
- `RefineTaskRequest`, `RefineTaskResponse` - Refinement action schemas
- `ExecuteTaskRequest`, `ExecuteTaskResponse` - Execution action schemas
- `InputTaskStatus` - Status enum constants

**Session Model Update:** `backend/app/models/session.py`
- Added `source_task_id: uuid.UUID | None` with FK to `input_task.id`
- Added `todo_progress: list | None` (JSON) - Stores TodoWrite tool progress during agent execution
- Both fields included in `SessionPublic` schema

## Backend Implementation

### API Routes

**File:** `backend/app/api/routes/input_tasks.py`

**Architecture:** Routes are thin controllers that delegate to `InputTaskService` methods. Service exceptions (`InputTaskError` subclasses) are converted to HTTP exceptions via `_handle_service_error()`.

**CRUD Operations:**
- `POST /api/v1/tasks` - Create new task (uses `verify_agent_access`, `create_task`)
- `GET /api/v1/tasks` - List tasks with status filter (uses `list_tasks_extended`)
- `GET /api/v1/tasks/{id}` - Get single task with agent name (uses `get_task_extended`)
- `PATCH /api/v1/tasks/{id}` - Update task (uses `get_task_with_ownership_check`, `verify_agent_access`, `update_task`)
- `DELETE /api/v1/tasks/{id}` - Delete task (uses `get_task_with_ownership_check`, `delete_task`)

**Actions:**
- `POST /api/v1/tasks/{id}/refine` - Refine with AI (uses `get_task_with_ownership_check`, `refine_task`)
- `POST /api/v1/tasks/{id}/execute` - Execute task (uses `get_task_with_ownership_check`, `execute_task_sync`)
- `POST /api/v1/tasks/{id}/archive` - Archive task (uses `get_task_with_ownership_check`, `update_status`)
- `GET /api/v1/tasks/{id}/sessions` - List sessions (uses `get_task_with_ownership_check`, `SessionService.list_task_sessions`)

**Router Registration:** `backend/app/api/main.py` - includes `input_tasks.router`

### Services

**InputTaskService:** `backend/app/services/input_task_service.py`

*Exception Classes:*
- `InputTaskError` - Base exception with message and status_code
- `TaskNotFoundError` - Task not found (404)
- `AgentNotFoundError` - Agent not found (404)
- `PermissionDeniedError` - User lacks permissions (400)
- `ValidationError` - Validation failed (400)

*Helper Methods:*
- `verify_agent_access()` - Verify agent exists and user has access, optionally require active environment
- `get_task_with_ownership_check()` - Get task and verify ownership
- `parse_status_filter()` - Parse status filter string to list of statuses
- `parse_workspace_filter()` - Parse workspace filter string to UUID and apply flag
- `get_task_extended()` - Get task with agent name and sessions info
- `list_tasks_extended()` - List tasks with extended info, handles filter parsing

*CRUD Operations:*
- `create_task()` - Create with original_message = current_description
- `get_task()`, `get_task_with_agent()` - Retrieve with optional agent name join
- `list_tasks()` - Filter by status, workspace, with pagination
- `update_task()`, `update_description()` - Modify task fields
- `update_status()` - Change status with appropriate timestamp updates
- `append_to_refinement_history()` - Add user/ai message to history
- `link_session()` - Set session_id and status to running
- `delete_task()` - Remove task

*Business Logic Operations:*
- `refine_task()` - Full refinement workflow: status transition, history, AI call, update
- `execute_task_sync()` - Synchronous task execution: agent validation, session creation, linking
- `create_task_with_auto_refine()` - Creates task and auto-refines if agent has `refiner_prompt`
  - Returns `(task, message_to_send)` tuple
  - Used by agent handover flow
- `execute_task()` - Async version that creates session, links to task, sends message
  - Returns `(success, session, error)` tuple
  - Used by agent handover flow

*Event Handlers (static async methods):*
- `handle_stream_started()` - Syncs task status to RUNNING
- `handle_stream_completed()` - Computes and syncs status from all linked sessions
- `handle_stream_error()` - Syncs task status to ERROR
- `handle_todo_list_updated()` - Propagates session todo progress to linked task, emits `TASK_TODO_UPDATED` event

**SessionService Updates:** `backend/app/services/session_service.py`
- `create_session()` - Now accepts optional `source_task_id` parameter
- `list_task_sessions()` - List sessions by source_task_id

**AIFunctionsService:** `backend/app/services/ai_functions_service.py`
- `refine_task()` - Wrapper that fetches agent workflow_prompt and refiner_prompt, calls task refiner

### AI Function - Task Refiner

**Agent:** `backend/app/agents/task_refiner.py`
- `refine_task()` - Takes current_description, agent_workflow_prompt, user_comment, refinement_history
- Uses `get_provider_manager()` for LLM calls
- Returns `{ success, refined_description, feedback_message }` or `{ success: false, error }`

**System Prompt:** `backend/app/agents/prompts/task_refiner_prompt.md`
- Defines task refinement assistant role
- Instructions for analyzing and improving task descriptions
- JSON output format specification

**Export:** `backend/app/agents/__init__.py` - exports `refine_task`

## Frontend Implementation

### Routes

**Tasks List:** `frontend/src/routes/_layout/tasks.tsx`
- Status filter sidebar (active, completed, archived, all)
- Task cards with status badge, agent name, description preview
- Create task button → `CreateTaskDialog`
- Navigate to task detail on click
- "Go to Session" button for running tasks

**Task Detail/Refinement:** `frontend/src/routes/_layout/task/$taskId.tsx`
- Split view: left panel (description, agent, execute, sessions list), right panel (refinement chat)
- Agent selector dropdown
- Description with edit/preview toggle
- Original message display (read-only, shown when different from current)
- Execute button (changes to "Run Again" when sessions exist)
- Sessions list showing all sessions spawned by this task with status indicators
- Delete confirmation dialog

### Components

**Tasks Directory:** `frontend/src/components/Tasks/`

**TaskStatusBadge:** `frontend/src/components/Tasks/TaskStatusBadge.tsx`
- Color-coded status badges with icons for each status

**CreateTaskDialog:** `frontend/src/components/Tasks/CreateTaskDialog.tsx`
- Modal with textarea for original_message
- Optional agent selector
- Creates task and navigates to refinement page

**RefinementChat:** `frontend/src/components/Tasks/RefinementChat.tsx`
- Displays refinement_history messages (user and AI)
- Input for new refinement comments
- Calls `TasksService.refineTask()` on submit
- Shows loading state during AI processing

**TaskTodoProgress:** `frontend/src/components/Tasks/TaskTodoProgress.tsx`
- Horizontal progress indicator for agent's TodoWrite tool usage
- Shows circles for each todo item (pending/in_progress/completed)
- Tooltip on hover shows full task content
- Displays current in-progress step as text hint
- Subscribes to `TASK_TODO_UPDATED` events for real-time updates

### Sidebar Integration

**File:** `frontend/src/components/Sidebar/AppSidebar.tsx`
- Added `ClipboardList` icon import
- Added Tasks menu item to `itemsAfterActivities` array: `{ icon: ClipboardList, title: "Tasks", path: "/tasks" }`

### API Client

**Generated Service:** `TasksService` in `frontend/src/client/sdk.gen.ts`
- `createTask()`, `listTasks()`, `getTask()`, `updateTask()`, `deleteTask()`
- `refineTask()`, `executeTask()`, `archiveTask()`
- `listTaskSessions()` - List sessions for a task

**Generated Types:** `frontend/src/client/types.gen.ts`
- `InputTaskPublic`, `InputTaskPublicExtended`, `InputTasksPublicExtended`
- `InputTaskCreate`, `InputTaskUpdate`
- `RefineTaskRequest`, `RefineTaskResponse`
- `ExecuteTaskRequest`, `ExecuteTaskResponse`

## Security Features

### Authorization Rules

- All task operations restricted to owner (or superuser for their own tasks)
- Agent selection validated for ownership
- Sessions inherit ownership from task owner

### Data Protection

- `original_message` immutable after creation (audit trail)
- `refinement_history` append-only (conversation audit)
- Tasks only linkable to user's own agents

### Validation

- Agent must have active environment to execute
- Task must have selected_agent_id to execute
- Status transitions validated (archived tasks cannot be re-executed without first becoming active)

## Key Integration Points

### With Agent Handovers

When an agent triggers a handover to another agent:
- `AgentService.execute_handover()` calls `InputTaskService.create_task_with_auto_refine()`
- Task is created with `agent_initiated=true`, `auto_execute=true`, `source_session_id` set
- If target agent has `refiner_prompt`, message is automatically refined
- `InputTaskService.execute_task()` creates session and sends message
- Task appears in Tasks list, allowing users to monitor agent-to-agent workflows

**Related Documentation:** `docs/agent-sessions/agent_handover_management.md`

### With Sessions

- Task creates session on execute via `SessionService.create_session(source_task_id=task.id)`
- Session stores `source_task_id` for backlink
- Task refinement page lists all sessions via `TasksService.listTaskSessions()`
- Task `session_id` field stores latest/primary session for quick access

### With Agents

- Refinement uses agent's `workflow_prompt` as AI context
- Execution requires active agent environment (`agent.active_environment_id`)
- Agent selector shows user's agents with names

### With AI Functions

- `AIFunctionsService.refine_task()` orchestrates LLM call
- Uses provider manager for cascade provider selection
- Task refiner prompt provides structured output format

## File Locations Reference

**Backend - Models:**
- `backend/app/models/input_task.py`
- `backend/app/models/session.py` (source_task_id addition)
- `backend/app/models/__init__.py` (exports)

**Backend - Routes:**
- `backend/app/api/routes/input_tasks.py`
- `backend/app/api/main.py` (router registration)

**Backend - Services:**
- `backend/app/services/input_task_service.py`
- `backend/app/services/session_service.py` (create_session, list_task_sessions)
- `backend/app/services/ai_functions_service.py` (refine_task method)

**Backend - AI Function:**
- `backend/app/agents/task_refiner.py`
- `backend/app/agents/prompts/task_refiner_prompt.md`
- `backend/app/agents/__init__.py`

**Backend - Migration:**
- `backend/app/alembic/versions/l2g3h4i5j6k7_add_input_task_table.py`

**Frontend - Routes:**
- `frontend/src/routes/_layout/tasks.tsx`
- `frontend/src/routes/_layout/task/$taskId.tsx`

**Frontend - Components:**
- `frontend/src/components/Tasks/TaskStatusBadge.tsx`
- `frontend/src/components/Tasks/CreateTaskDialog.tsx`
- `frontend/src/components/Tasks/RefinementChat.tsx`
- `frontend/src/components/Tasks/TaskTodoProgress.tsx`
- `frontend/src/components/Sidebar/AppSidebar.tsx`

**Frontend - Client (auto-generated):**
- `frontend/src/client/sdk.gen.ts`
- `frontend/src/client/types.gen.ts`

---

**Document Version:** 2.4
**Last Updated:** 2026-01-18
**Status:** Implementation Complete

**Changes in v2.4:**
- Added `todo_progress` JSON field to Session and InputTask models for TodoWrite tool tracking
- Added `TODO_LIST_UPDATED` and `TASK_TODO_UPDATED` event types
- Added `InputTaskService.handle_todo_list_updated()` event handler for propagating session todos to tasks
- Added `TaskTodoProgress` frontend component for real-time progress display
- Tasks list now subscribes to `TASK_TODO_UPDATED` events for live updates
- Migration: `p6k7l8m9n0o1_add_todo_progress_to_session.py`

**Changes in v2.3:**
- Refactored routes to be thin controllers delegating to service layer
- Added exception classes: `InputTaskError`, `TaskNotFoundError`, `AgentNotFoundError`, `PermissionDeniedError`, `ValidationError`
- Added helper methods: `verify_agent_access()`, `get_task_with_ownership_check()`, `parse_status_filter()`, `parse_workspace_filter()`
- Added high-level methods: `get_task_extended()`, `list_tasks_extended()`, `refine_task()`, `execute_task_sync()`
- Removed code duplication for ownership/agent verification across routes

**Changes in v2.2:**
- Added agent-initiated task fields: `agent_initiated`, `auto_execute`, `source_session_id`
- Added `create_task_with_auto_refine()` method for task creation with auto-refinement
- Added `execute_task()` method for task execution (session creation + message sending)
- Agent handovers now create tasks instead of sessions directly
- Tasks created by handovers are auto-refined if target agent has `refiner_prompt`

**Changes in v2.1:**
- Removed `ready` status from lifecycle (redundant with `refining`)
- Added session-to-task status sync via event handlers
- Task status now automatically updates based on connected session states
