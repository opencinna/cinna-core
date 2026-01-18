# Input Task Management - Implementation Reference

## Purpose

Enable users to receive, refine, and execute incoming tasks through an AI-assisted preparation workflow. Tasks often arrive with incomplete information - this feature provides a structured way to transform vague requests into detailed, agent-ready instructions before execution.

## Feature Overview

**Flow:**
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
| `ready` | Refinement complete | Execute |
| `running` | Session active, agent working | Monitor |
| `pending_input` | Agent needs user input | Go to session |
| `completed` | Agent finished successfully | Archive |
| `error` | Agent encountered error | Review, retry, Archive |
| `archived` | User archived task | - |

### Refinement History Structure

Stored as JSON array in `InputTask.refinement_history`:
- Each item: `{ role: "user"|"ai", content: string, timestamp: ISO8601 }`
- Append-only for audit trail
- Last 5 items passed as context to AI refiner

## Database Schema

**Migration:** `backend/app/alembic/versions/l2g3h4i5j6k7_add_input_task_table.py`
- Creates `input_task` table
- Adds `source_task_id` column to `session` table with FK to `input_task.id`
- Index on `owner_id, status` for efficient listing

**Models:** `backend/app/models/input_task.py`
- `InputTask` - Database table with owner_id, original_message, current_description, status, selected_agent_id, session_id, user_workspace_id, refinement_history, timestamps
- `InputTaskCreate`, `InputTaskUpdate` - API input schemas
- `InputTaskPublic`, `InputTaskPublicExtended` - API response schemas (extended includes agent_name)
- `RefineTaskRequest`, `RefineTaskResponse` - Refinement action schemas
- `ExecuteTaskRequest`, `ExecuteTaskResponse` - Execution action schemas
- `InputTaskStatus` - Status enum constants

**Session Model Update:** `backend/app/models/session.py`
- Added `source_task_id: uuid.UUID | None` with FK to `input_task.id`
- Included in `SessionPublic` schema

## Backend Implementation

### API Routes

**File:** `backend/app/api/routes/input_tasks.py`

**CRUD Operations:**
- `POST /api/v1/tasks` - Create new task
- `GET /api/v1/tasks` - List tasks with status filter (active, completed, archived, all)
- `GET /api/v1/tasks/{id}` - Get single task with agent name
- `PATCH /api/v1/tasks/{id}` - Update task (description, agent)
- `DELETE /api/v1/tasks/{id}` - Delete task

**Actions:**
- `POST /api/v1/tasks/{id}/refine` - Refine with AI assistance
- `POST /api/v1/tasks/{id}/execute` - Execute (create session linked via source_task_id)
- `POST /api/v1/tasks/{id}/archive` - Archive completed/error task
- `GET /api/v1/tasks/{id}/sessions` - List all sessions spawned by this task

**Router Registration:** `backend/app/api/main.py` - includes `input_tasks.router`

### Services

**InputTaskService:** `backend/app/services/input_task_service.py`
- `create_task()` - Create with original_message = current_description
- `get_task()`, `get_task_with_agent()` - Retrieve with optional agent name join
- `list_tasks()` - Filter by status, workspace, with pagination
- `update_task()`, `update_description()` - Modify task fields
- `update_status()` - Change status with appropriate timestamp updates
- `append_to_refinement_history()` - Add user/ai message to history
- `link_session()` - Set session_id and status to running
- `delete_task()` - Remove task

**SessionService Updates:** `backend/app/services/session_service.py`
- `create_session()` - Now accepts optional `source_task_id` parameter
- `list_task_sessions()` - List sessions by source_task_id

**AIFunctionsService:** `backend/app/services/ai_functions_service.py`
- `refine_task()` - Wrapper that fetches agent workflow_prompt and calls task refiner

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
- `frontend/src/components/Sidebar/AppSidebar.tsx`

**Frontend - Client (auto-generated):**
- `frontend/src/client/sdk.gen.ts`
- `frontend/src/client/types.gen.ts`

---

**Document Version:** 2.0
**Last Updated:** 2026-01-18
**Status:** Implementation Complete
