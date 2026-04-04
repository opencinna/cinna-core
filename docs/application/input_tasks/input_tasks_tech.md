# Input Tasks — Technical Reference

## File Locations

### Backend

**Models:**
- `backend/app/models/input_task.py` — InputTask table, all schema classes, `InputTaskStatus` constants
- `backend/app/models/task_comment.py` — TaskComment table, `TaskCommentCreate`, `AgentTaskCommentCreate`, `TaskCommentPublic`
- `backend/app/models/task_attachment.py` — TaskAttachment table, `TaskAttachmentPublic`
- `backend/app/models/task_status_history.py` — TaskStatusHistory table, `TaskStatusHistoryPublic`
- `backend/app/models/session.py` — `source_task_id`, `todo_progress`, `result_state`, `result_summary` additions
- `backend/app/models/__init__.py` — exports

**Routes:**
- `backend/app/api/routes/input_tasks.py` — CRUD, refinement, execution, collaboration endpoints (comments, attachments, subtasks, short-code access); note: `archive_task` is `async def` (requires event loop for real-time event emission)
- `backend/app/api/routes/task_agent_api.py` — internal agent API endpoints (called by MCP tools)
- `backend/app/api/main.py` — router registration

**Services:**
- `backend/app/services/input_task_service.py` — main service (extended with collaboration methods)
- `backend/app/services/task_comment_service.py` — comment creation, listing, deletion
- `backend/app/services/task_attachment_service.py` — file upload, workspace attach, download
- `backend/app/services/session_service.py` — `create_session`, `list_task_sessions`, `delete_session`
- `backend/app/services/activity_service.py` — task activity lifecycle handlers
- `backend/app/services/ai_functions_service.py` — `refine_task` method

**AI Functions:**
- `backend/app/agents/task_refiner.py` — LLM task refinement agent
- `backend/app/agents/prompts/task_refiner_prompt.md` — refinement system prompt
- `backend/app/agents/__init__.py` — exports `refine_task`

**Agent-Env Tools:**
- `backend/app/env-templates/app_core_base/core/server/tools/agent_task_add_comment.py` — SDK tool for Claude Code adapter
- `backend/app/env-templates/app_core_base/core/server/tools/agent_task_update_status.py` — SDK tool for Claude Code adapter
- `backend/app/env-templates/app_core_base/core/server/tools/agent_task_create_task.py` — SDK tool for Claude Code adapter
- `backend/app/env-templates/app_core_base/core/server/tools/agent_task_create_subtask.py` — SDK tool for Claude Code adapter
- `backend/app/env-templates/app_core_base/core/server/tools/agent_task_get_details.py` — SDK tool for Claude Code adapter
- `backend/app/env-templates/app_core_base/core/server/tools/agent_task_list_tasks.py` — SDK tool for Claude Code adapter
- `backend/app/env-templates/app_core_base/core/server/tools/mcp_bridge/task_server.py` — MCP bridge server (OpenCode adapter)

**Migrations:**
- `backend/app/alembic/versions/l2g3h4i5j6k7_add_input_task_table.py` — initial `input_task` table
- `backend/app/alembic/versions/o5j6k7l8m9n0_add_agent_initiated_fields_to_input_task.py` — agent_initiated, auto_execute, source_session_id
- `backend/app/alembic/versions/p6k7l8m9n0o1_add_todo_progress_to_session.py` — todo_progress
- `backend/app/alembic/versions/u1p2q3r4s5t6_add_session_state_and_task_feedback.py` — result_state, result_summary, auto_feedback, feedback_delivered
- `backend/app/alembic/versions/i6d5e7f8g9h0_add_input_task_id_to_activity.py` — input_task_id FK on activity

**App Startup:**
- `backend/app/main.py` — event handler registration

### Frontend

**Routes:**
- `frontend/src/routes/_layout/tasks/index.tsx` — Tasks page with view mode toggle (Board / List); header has "New Task" button and view switcher; Board view renders `TaskBoard` component; List view renders a compact table with left sidebar status filters (Active, Completed, All, Archived), rows grouped by date (Today, Yesterday, Last week, Older), sorted by `updated_at` desc; each row shows status dot, short code, title, team/agent badge with color preset, and relative time
- `frontend/src/routes/_layout/task/$taskId.tsx` — unified task detail page; `taskId` param accepts either a UUID or a short code; detects format with `isUUID()` regex and calls `TasksService.getTaskDetail()` (UUID) or `TasksService.getTaskDetailByCode()` (short code) accordingly; full-width layout (no max-width constraints); sessions displayed as a tab alongside Comments/Sub-tasks/Activity (not as a standalone block)
- `frontend/src/routes/_layout/tasks/$shortCode.tsx` — redirect-only route; performs `beforeLoad` redirect from `/tasks/$shortCode` to `/task/$taskId` preserving the short code value

**Components:**
- `frontend/src/components/Tasks/TaskBoard.tsx` — kanban board with 4 columns: Open (includes `new`, `refining`, `open` statuses), In Progress, Blocked, Completed; subscribes to `TASK_STATUS_CHANGED`, `TASK_SUBTASK_CREATED`, `SUBTASK_COMPLETED` for real-time updates; no inline filters or create button (handled by parent page header)
- `frontend/src/components/Tasks/TaskShortCodeBadge.tsx` — clickable or static short code badge with status-aware color; navigates to `/task/$taskId` on click
- `frontend/src/components/Tasks/TaskStatusBadge.tsx` — color-coded status badge with icon
- `frontend/src/components/Tasks/TaskStatusPill.tsx` — compact status indicator used in task detail header and subtask rows
- `frontend/src/components/Tasks/TaskPriorityBadge.tsx` — colored priority label (Low, Normal, High, Urgent)
- `frontend/src/components/Tasks/SubtaskProgressChip.tsx` — `{completed}/{total}` subtask counter with percentage color coding; hidden when `total <= 0`
- `frontend/src/components/Tasks/CreateTaskDialog.tsx` — modal with message textarea, title, priority, optional agent selector, optional team selector; generates task and navigates to detail
- `frontend/src/components/Tasks/RefinementChat.tsx` — refinement history display + comment input; calls `TasksService.refineTask()`
- `frontend/src/components/Tasks/TaskTodoProgress.tsx` — horizontal progress indicator for TodoWrite tool usage; subscribes to `TASK_TODO_UPDATED`
- `frontend/src/components/Tasks/TaskSessionsModal.tsx` — modal listing all sessions linked to a task; opened from the sessions block "View all" link
- `frontend/src/components/Chat/SubTasksPanel.tsx` — slide-out showing sub-tasks for current session; real-time via `SESSION_STATE_UPDATED`
- `frontend/src/components/Chat/ChatHeader.tsx` — badge showing subtask count; toggles SubTasksPanel

Note: `TaskDetail.tsx` has been deleted. Its functionality is now handled entirely within `frontend/src/routes/_layout/task/$taskId.tsx` as an inline page component.

**Generated Client:**
- `frontend/src/client/sdk.gen.ts` — `TasksService`
- `frontend/src/client/types.gen.ts` — `InputTaskPublic`, `InputTaskPublicExtended`, `InputTaskDetailPublic`, `InputTaskCreate`, `InputTaskUpdate`, `RefineTaskRequest`, `RefineTaskResponse`, `ExecuteTaskRequest`, `ExecuteTaskResponse`, `TaskCommentPublic`, `TaskAttachmentPublic`, `TaskStatusHistoryPublic`

## Database Schema

### Table: `input_task`

Core fields (existing):
- `id` (UUID PK)
- `owner_id` (UUID FK → user.id CASCADE)
- `original_message` (str, immutable after creation)
- `current_description` (str, updated during refinement)
- `status` (`InputTaskStatus` values — see below)
- `selected_agent_id` (UUID FK → agent.id, nullable)
- `session_id` (UUID FK → session.id, nullable — latest/primary session)
- `user_workspace_id` (UUID FK → user_workspace.id, nullable)
- `agent_initiated` (bool)
- `auto_execute` (bool)
- `source_session_id` (UUID FK → session.id, nullable)
- `source_email_message_id` (UUID FK → email_message.id, nullable)
- `source_agent_id` (UUID FK → agent.id, nullable)
- `auto_feedback` (bool, default=True)
- `feedback_delivered` (bool, default=False)
- `refinement_history` (JSON array — `{role, content, timestamp}` items, append-only)
- `todo_progress` (JSONB array, nullable)
- `error_message` (str, nullable)
- `created_at`, `updated_at`, `executed_at`, `completed_at`, `archived_at`

New collaboration fields:
- `short_code` (VARCHAR(20), nullable, unique per owner)
- `sequence_number` (INTEGER, nullable — from `user.task_sequence_counter`)
- `title` (VARCHAR(500), nullable — editable; derived from first line of `original_message` on creation)
- `priority` (VARCHAR(20), default=`"normal"`)
- `parent_task_id` (UUID FK → input_task.id SET NULL, nullable — subtask hierarchy)
- `team_id` (UUID FK → agentic_team.id SET NULL, nullable — team-scoped tasks)
- `assigned_node_id` (UUID FK → agentic_team_node.id SET NULL, nullable)
- `created_by_node_id` (UUID FK → agentic_team_node.id SET NULL, nullable)

Indexes: `ix_input_task_owner_status`, `ix_input_task_parent_task_id`, `ix_input_task_team_id`, `ix_input_task_assigned_node_id`

**`InputTaskStatus` values:**

| Constant | Value | Notes |
|----------|-------|-------|
| `NEW` | `"new"` | |
| `REFINING` | `"refining"` | |
| `OPEN` | `"open"` | New — ready for agent execution |
| `IN_PROGRESS` | `"in_progress"` | Replaces old `running` |
| `BLOCKED` | `"blocked"` | Replaces old `pending_input` |
| `COMPLETED` | `"completed"` | |
| `ERROR` | `"error"` | |
| `CANCELLED` | `"cancelled"` | New |
| `ARCHIVED` | `"archived"` | |

Legacy aliases on `InputTaskStatus`: `RUNNING = "in_progress"`, `PENDING_INPUT = "blocked"` (backward compatibility).

### Table: `task_comment`

- `id` (UUID PK)
- `task_id` (UUID FK → input_task.id CASCADE, NOT NULL)
- `content` (TEXT, min 1, max 10000)
- `comment_type` (VARCHAR(30), default=`"message"`)
- `author_node_id` (UUID FK → agentic_team_node.id SET NULL, nullable)
- `author_agent_id` (UUID FK → agent.id SET NULL, nullable)
- `author_user_id` (UUID FK → user.id SET NULL, nullable)
- `comment_meta` (JSON, nullable — stored as column `metadata`; used by status_change type: `{from_status, to_status}`)
- `created_at` (DATETIME)

Index: `ix_task_comment_task_id`

**Author pattern**: agent comments set `author_agent_id`; team-context agents also set `author_node_id` (role display). User comments set `author_user_id`. System comments leave all author fields NULL.

### Table: `task_attachment`

- `id` (UUID PK)
- `task_id` (UUID FK → input_task.id CASCADE, NOT NULL)
- `comment_id` (UUID FK → task_comment.id SET NULL, nullable — inline or standalone)
- `file_name` (VARCHAR(500))
- `file_path` (VARCHAR(1000) — relative path in backend storage)
- `file_size` (BIGINT, nullable)
- `content_type` (VARCHAR(200), nullable)
- `uploaded_by_agent_id` (UUID FK → agent.id SET NULL, nullable)
- `uploaded_by_user_id` (UUID FK → user.id SET NULL, nullable)
- `source_agent_id` (UUID FK → agent.id SET NULL, nullable — where file originated)
- `source_workspace_path` (VARCHAR(1000), nullable — original path in agent workspace)
- `created_at` (DATETIME)

Indexes: `ix_task_attachment_task_id`, `ix_task_attachment_comment_id`

Storage path: `uploads/{owner_id}/task_attachments/{attachment_id}/{filename}` (relative to `UPLOAD_BASE_PATH`)

### Table: `task_status_history`

- `id` (UUID PK)
- `task_id` (UUID FK → input_task.id CASCADE, NOT NULL)
- `from_status` (VARCHAR(30))
- `to_status` (VARCHAR(30))
- `changed_by_agent_id` (UUID FK → agent.id SET NULL, nullable)
- `changed_by_user_id` (UUID FK → user.id SET NULL, nullable)
- `reason` (TEXT, nullable)
- `created_at` (DATETIME)

Index: `ix_task_status_history_task_id`

### Related Table Modifications

**`agentic_team`** — new column `task_prefix` (VARCHAR(10), nullable). `AgenticTeamCreate` now includes `task_prefix` so it can be set at creation time, not only via update. When non-null, tasks created under this team use this string as the short-code prefix instead of `"TASK"`.

**`user`** — new column `task_sequence_counter` (INTEGER, default=0). Per-user monotonic counter incremented atomically on each task creation.

### Model Schema Classes

**`backend/app/models/input_task.py`:**
- `InputTaskBase` — `original_message`, `current_description`
- `InputTask` — DB table (all columns above)
- `InputTaskCreate` — includes new: `title?`, `priority?`, `team_id?`, `assigned_node_id?`, `parent_task_id?`
- `InputTaskUpdate` — includes new: `title?`, `priority?`, `team_id?`, `assigned_node_id?` (team can be changed after creation)
- `InputTaskPublic` — includes new: `short_code`, `title`, `priority`, `parent_task_id`, `team_id`, `assigned_node_id`, `created_by_node_id`, `subtask_count`, `subtask_completed_count`
- `InputTaskPublicExtended` — extends Public with: `agent_name`, `refinement_history`, `todo_progress`, `sessions_count`, `latest_session_id`, `attached_files`, `assigned_node_name`, `team_name`
- `InputTaskDetailPublic` — extends Extended with: `comments: list[TaskCommentPublic]`, `attachments: list[TaskAttachmentPublic]`, `subtasks: list[InputTaskPublic]`, `status_history: list[TaskStatusHistoryPublic]`
- `AgentTaskStatusUpdate` — agent edge-case status update (`status`, `reason?`, `task?` short code)
- `AgentSubtaskCreate` — agent subtask creation (`title`, `description?`, `assigned_to?`, `priority?`, `task?` short code, `source_session_id?`)
- `AgentTaskCreate` — agent standalone task creation (`title`, `description?`, `assigned_to?`, `priority?`, `source_session_id?`)
- `AgentTaskOperationResponse` — generic agent op response (`success`, `task` short code, `parent_task?` short code, `assigned_to?` resolved name, `message?`, `error?`)

## API Endpoints

### File: `backend/app/api/routes/input_tasks.py`

**Task CRUD:**
- `POST /api/v1/tasks/` — create task; auto-generates `short_code` and `title`
- `GET /api/v1/tasks/` — list tasks; new query params: `root_only` (exclude subtasks), `team_id`, `priority`
- `GET /api/v1/tasks/{id}` — get task (`InputTaskPublicExtended`)
- `PATCH /api/v1/tasks/{id}` — update task
- `DELETE /api/v1/tasks/{id}` — delete task (emits ACTIVITY_DELETED for linked activities)

**Task Actions:**
- `POST /api/v1/tasks/{id}/refine` — AI-assisted refinement
- `POST /api/v1/tasks/{id}/execute` — execute task (creates session)
- `POST /api/v1/tasks/{id}/send-answer` — email reply for email-originated tasks
- `POST /api/v1/tasks/{id}/archive` — archive task
- `GET /api/v1/tasks/{id}/sessions` — list all sessions for a task
- `GET /api/v1/tasks/by-source-session/{session_id}` — list tasks created by a source session

**Short-Code Access (new):**
- `GET /api/v1/tasks/by-code/{short_code}` — get task by short code (`InputTaskPublicExtended`)
- `GET /api/v1/tasks/by-code/{short_code}/detail` — full detail: comments, attachments, subtasks, history
- `GET /api/v1/tasks/by-code/{short_code}/tree` — recursive subtask tree

**Detail by UUID (new):**
- `GET /api/v1/tasks/{id}/detail` — full detail by UUID (`InputTaskDetailPublic`)

**Comments (new):**
- `GET /api/v1/tasks/{id}/comments/` — list comments (chronological, paginated)
- `POST /api/v1/tasks/{id}/comments/` — add comment (user-initiated)
- `DELETE /api/v1/tasks/{id}/comments/{comment_id}` — delete comment (ownership check via task)

**Attachments (new):**
- `GET /api/v1/tasks/{id}/attachments/` — list attachments
- `POST /api/v1/tasks/{id}/attachments/` — upload attachment (multipart)
- `GET /api/v1/tasks/{id}/attachments/{attachment_id}/download` — download file (streaming)
- `DELETE /api/v1/tasks/{id}/attachments/{attachment_id}` — delete attachment and file on disk

**Legacy file attachment (pre-collaboration):**
- `POST /api/v1/tasks/{id}/files/{file_id}` — attach pre-uploaded FileUpload to task
- `DELETE /api/v1/tasks/{id}/files/{file_id}` — detach FileUpload from task

### File: `backend/app/api/routes/task_agent_api.py`

Called by MCP tools inside agent environments. Authentication via JWT (same CurrentUser dep). Agent identity resolved from `task.selected_agent_id`.

**Helper (module-level):**
- `_resolve_task_from_session(db_session, session_id) -> InputTask` — queries `InputTask WHERE session_id = session_id`; raises `ValidationError("No task linked to this session")` if not found; used by all `current/*` endpoints

**Endpoints:**
- `POST /agent/tasks/create` — agent creates standalone task; resolves `assigned_to` by name, inherits team context from session, auto-executes if assigned
- `GET /agent/tasks/by-code/{short_code}` — resolves short code (e.g. `HR-17`) to `{task_id, short_code}`; used by tools that accept a `task` param to obtain the UUID before making subsequent calls
- `POST /agent/tasks/current/comment` — agent posts comment on its current task; requires `source_session_id` in body; calls `_resolve_task_from_session` to find task
- `POST /agent/tasks/current/status` — agent updates status of its current task; requires `source_session_id` in body; calls `_resolve_task_from_session`
- `GET /agent/tasks/current/details` — agent gets details of its current task; `source_session_id` passed as query param; calls `_resolve_task_from_session`; automatically uploads task files to agent environment; `async def`
- `POST /agent/tasks/current/subtask` — agent creates subtask under its current task (resolved from `source_session_id`); delegates to `create_subtask` with team topology validation
- `POST /agent/tasks/{task_id}/comment` — agent posts comment with optional workspace file paths (explicit task_id variant)
- `POST /agent/tasks/{task_id}/status` — agent explicitly updates status (edge cases: blocked, cancelled, completed; explicit task_id variant)
- `POST /agent/tasks/{task_id}/subtask` — agent creates subtask with explicit parent task ID (validates team membership and connection topology)
- `GET /agent/tasks/my-tasks` — agent lists tasks (`scope`: `assigned` / `created` / `team`)
- `GET /agent/tasks/{task_id}/details` — agent gets simplified task detail (recent comments, subtask progress; explicit task_id variant); accepts optional `source_session_id` query param — when provided, uploads task files to agent environment; `async def`

## Services & Key Methods

### `InputTaskService` (`backend/app/services/input_task_service.py`)

Exception classes: `InputTaskError`, `TaskNotFoundError`, `AgentNotFoundError`, `PermissionDeniedError`, `ValidationError`

**Helper methods:**
- `verify_agent_access()` — verify agent exists and user owns it, optionally require active environment
- `get_task_with_ownership_check()` — get task and verify owner
- `parse_status_filter()`, `parse_workspace_filter()` — filter parsing utilities
- `get_task_extended()`, `list_tasks_extended()` — with agent name, team/node names, and session data joined

**CRUD:**
- `create_task()` — generates `short_code` via `_generate_short_code()`, sets `title` from first line of `original_message`; **new**: if `team_id` is set but neither `selected_agent_id` nor `assigned_node_id` is provided, queries `AgenticTeamNode` for the lead node (`is_lead=True`) and auto-assigns both `selected_agent_id` and `assigned_node_id`
- `_generate_short_code(session, owner_id, team_id=None) -> tuple[str, int]` — atomic counter increment; prefix from team or default "TASK"
- `get_task_by_short_code(session, short_code, user_id)` — lookup by `(short_code, owner_id)`
- `get_task_detail(session, task_id, user_id) -> InputTaskDetailPublic` — full detail with comments (inline attachments), standalone attachments, subtasks, status history
- `get_task_tree(session, task_id, user_id)` — recursive subtask tree
- `list_tasks_extended()` — supports new filters: `root_only`, `team_id`, `priority`
- `update_task()`, `delete_task()`, `update_status()`, `append_to_refinement_history()`
- `link_session()` — set session_id, status to in_progress
- `reset_task_if_no_sessions()` — reset to NEW if all linked sessions deleted

**Status and collaboration:**
- `update_task_status(session, task_id, new_status, changed_by_agent_id=None, changed_by_user_id=None, changed_by_system=False, reason=None)` — validates transition, creates `TaskStatusHistory`, creates system comment, emits `TASK_STATUS_CHANGED`
- `update_task_status_from_agent(session, task_id, agent_id, data: AgentTaskStatusUpdate)` — verifies agent is assigned; delegates to `update_task_status()`
- `create_task_from_agent(session, user_id, data: AgentTaskCreate) -> (InputTask, resolved_name)` — resolves session context, agent name (team node or agent fallback), team inheritance; creates and optionally auto-executes task; posts system message to source session
- `create_subtask(session, parent_task_id, creating_agent_id, data: AgentSubtaskCreate)` — validates team membership, connection topology, creates child task, auto-executes if assigned, posts system comment on parent
- `list_agent_tasks(session, user_id, status=None, scope="assigned")` — scope: assigned / created / team
- `get_agent_task_details(session, task_id, user_id)` — simplified view for agent consumption
- `get_subtask_progress(session, task_id)` — returns `{total, completed, in_progress, blocked}`
- `_notify_parent_task(session, parent_task, completed_subtask)` — post system comment on parent; trigger parent agent if session is idle; emit `SUBTASK_COMPLETED`
- `_collect_task_files_info(session, task_id) -> list[dict]` — queries `InputTaskFile` (user uploads linked to task) and `TaskAttachment` (agent/user attachments on task and its comments); deduplicates by filename; returns list of `{file_name, file_size, content_type, source, storage_path}` dicts
- `upload_task_files_to_agent_env(session, task_details, source_session_id) -> dict` — async; resolves agent environment from `source_session_id`; reads each file from backend storage; POSTs to agent-env `POST /files/upload` with `subfolder=task_{SHORT_CODE}`; files land at `/app/workspace/uploads/task_{SHORT_CODE}/` in the container; injects `uploaded_files` list into returned `task_details`; strips internal `files` key before returning; skips files exceeding size limit; silently skips if environment is not running

**Session event handlers (static async, registered in `backend/app/main.py`):**
- `handle_session_started()` — task → `in_progress` (system comment)
- `handle_session_completed()` — task → `completed` only if all subtasks are also completed (via `compute_status_from_sessions` which checks `get_subtask_progress`); stays `in_progress` if incomplete subtasks remain (system comment; triggers `_notify_parent_task`)
- `handle_session_error()` — task → `error` (system comment with error message)
- `handle_todo_list_updated()` — propagate session todos to task, emit `TASK_TODO_UPDATED`

**Removed methods** (replaced by comment/status model):
- `handle_session_state_updated()` — replaced by `update_task_status()` + task comments
- `deliver_feedback_to_source()` — replaced by `_notify_parent_task()`
- `respond_to_task()` — replaced by `add_comment` on parent task

**Email task methods:**
- `send_email_answer()` — generates AI email reply, queues for SMTP delivery; deletes `email_task_reply_pending` activity
- `update_status()` — emits `TASK_STATUS_UPDATED` for email-originated tasks

### `MessageService` — task context enrichment (`backend/app/services/message_service.py`)

The module-level function `_build_session_context(db, session_db, env, agent)` now queries `InputTask` by `session_id` and — when a matching task is found — populates the following keys into the session context dict. These are consumed by `PromptGenerator.build_task_context_section()` inside the agent environment:

| Key | Source |
|-----|--------|
| `task_short_code` | `InputTask.short_code` |
| `task_title` | `InputTask.title` |
| `task_description` | `InputTask.current_description` |
| `task_priority` | `InputTask.priority` |
| `task_status` | `InputTask.status` |
| `task_created_by_name` | Creator agent name (if `agent_initiated`) or user full name / email |
| `task_created_by_type` | `"agent"` or `"user"` |
| `parent_task_short_code` | `InputTask.short_code` of parent (subtasks only) |
| `parent_task_title` | Parent task title |
| `parent_task_description` | Parent task `current_description` |
| `parent_assigned_agent_name` | Name of agent assigned to parent task |
| `parent_node_name` | Name of team node assigned to parent task |
| `team_name` | `AgenticTeam.name` (team-scoped tasks) |
| `node_name` | `AgenticTeamNode.name` of assigned node |
| `downstream_team_members` | List of `{node_name, agent_name, agent_description, connection_prompt}` dicts for enabled outbound connections from the assigned node |
| `delegation_connection_prompt` | `AgenticTeamConnection.connection_prompt` on the connection from parent node → current node (subtasks with team context) |

The enrichment runs inside a `try/except` block — failures are logged as warnings and do not break message delivery.

### `TaskCommentService` (`backend/app/services/task_comment_service.py`)

- `add_comment(session, task_id, data, author_agent_id=None, author_node_id=None, author_user_id=None)` — creates comment record, emits `TASK_COMMENT_ADDED`
- `add_comment_from_agent(session, task_id, agent_id, data: AgentTaskCommentCreate)` — resolves agent's node in team context; if `file_paths` provided delegates to `TaskAttachmentService.attach_from_workspace()`; creates comment with agent/node author
- `add_system_comment(session, task_id, content, comment_type="system", comment_meta=None)` — no author fields; used for status changes, subtask notifications
- `list_comments(session, task_id, skip, limit) -> tuple[list[TaskCommentPublic], int]` — chronological ASC; eager-loads inline attachments
- `delete_comment(session, comment_id, user_id)` — ownership check via task
- `_to_public(session, comment, include_attachments=True)` — resolves `author_name`, `author_role`, `inline_attachments`

### `TaskAttachmentService` (`backend/app/services/task_attachment_service.py`)

- `upload_attachment(session, task_id, file: UploadFile, uploaded_by_user_id=None, comment_id=None)` — stores file, creates `TaskAttachment` record, emits `TASK_ATTACHMENT_ADDED`
- `attach_from_workspace(session, task_id, agent_id, file_paths, comment_id=None)` — resolves agent's active environment; for each path: normalizes to a relative path (handles `./reports/file.json`, `/app/workspace/reports/file.json`, and `reports/file.json` formats), then calls `GET /workspace/download/{rel_path}` on agent-env HTTP API; stores file; creates `TaskAttachment` with origin tracking
- `get_download_stream(session, task_id, attachment_id, user_id) -> tuple[Path, str, str]` — ownership check; returns (abs_path, filename, content_type)
- `list_attachments(session, task_id)` — all attachments for a task (chronological)
- `delete_attachment(session, task_id, attachment_id, user_id)` — deletes DB record and file on disk

**Storage path pattern:** `uploads/{owner_id}/task_attachments/{attachment_id}/{filename}`
Base path resolved from `settings.UPLOAD_BASE_PATH`. Path traversal protection applied before serving.

## Event Handler Registration

**File:** `backend/app/main.py`

- `TASK_CREATED` → `ActivityService.handle_task_created`
- `TASK_STATUS_UPDATED` → `ActivityService.handle_task_status_changed`
- `SESSION_STARTED` → `InputTaskService.handle_session_started`
- `STREAM_COMPLETED` → `InputTaskService.handle_session_completed`
- `STREAM_ERROR` → `InputTaskService.handle_session_error`
- `TODO_LIST_UPDATED` → `InputTaskService.handle_todo_list_updated`

## Real-Time Events

New events emitted from the task collaboration system (file: `backend/app/models/event.py`):

| Event | Trigger | Payload |
|-------|---------|---------|
| `TASK_COMMENT_ADDED` | `TaskCommentService.add_comment()` | `task_id`, `short_code`, `comment_id`, `author_name`, `has_attachments` |
| `TASK_STATUS_CHANGED` | `InputTaskService.update_task_status()` | `task_id`, `short_code`, `from_status`, `to_status` |
| `TASK_ATTACHMENT_ADDED` | `TaskAttachmentService._emit_attachment_event()` | `task_id`, `short_code`, `attachment_id`, `file_name` |
| `SUBTASK_COMPLETED` | `InputTaskService._notify_parent_task()` | `parent_task_id`, `subtask_id`, `subtask_short_code` |

All events are scoped to the task owner (`user_id`).

The task detail page also subscribes to session-domain events to update the Sessions tab in real time:

| Event | Source | Used For |
|-------|--------|---------|
| `SESSION_UPDATED` | Session service | Refresh sessions list |
| `SESSION_INTERACTION_STATUS_CHANGED` | Session service | Update streaming indicator and tab icon pulse |
| `SESSION_STATE_UPDATED` | Session service | Update session status |
| `STREAM_COMPLETED` | Stream handler | Mark session as complete |

These events are matched by `meta.source_task_id` or by `meta.session_id` / `event.model_id` against a ref-tracked set of known session IDs (`sessionIdsRef`). The ref pattern avoids stale closures — `useMultiEventSubscription` does not re-subscribe when the handler changes, so session IDs are tracked via a `useRef` updated by `useEffect`.

## Frontend Components

- `frontend/src/routes/_layout/tasks/index.tsx` — Tasks page: Board/List view toggle in header; Board view delegates to `TaskBoard` component; List view shows compact table with status sidebar filters, date-grouped rows (Today/Yesterday/Last week/Older), status dots, agent/team badges with color presets, relative timestamps
- `frontend/src/routes/_layout/task/$taskId.tsx` — unified task detail page (Linear-style layout): accepts UUID or short code in `$taskId` param; full-width layout with left body and right sidebar panel; four tabs: Comments, Sessions, Sub-tasks, Activity; session and subtask tab icons pulse blue when active sessions or in-progress subtasks exist; tab counters rendered as round pill badges; session rows are clickable (navigate to session page) with agent color-preset badges and relative timestamps; WebSocket session event handler uses a `sessionIdsRef` to avoid stale closure issues — matches events by `meta.session_id` or `event.model_id` against known task session IDs; subscribes to `TASK_COMMENT_ADDED`, `TASK_STATUS_CHANGED`, `TASK_ATTACHMENT_ADDED`, `SUBTASK_COMPLETED`, `TASK_SUBTASK_CREATED` (task events) and `SESSION_UPDATED`, `SESSION_INTERACTION_STATUS_CHANGED`, `SESSION_STATE_UPDATED`, `STREAM_COMPLETED` (session events)
- `frontend/src/routes/_layout/tasks/$shortCode.tsx` — redirect-only: `beforeLoad` redirects `/tasks/$shortCode` to `/task/$taskId`
- `frontend/src/components/Tasks/TaskBoard.tsx` — kanban board: 4 columns (Open merges `new`/`refining`/`open`, In Progress, Blocked, Completed); `TaskShortCodeBadge`, `TaskPriorityBadge`, `SubtaskProgressChip` per card; workspace-aware via `useWorkspace`; no inline filters or create dialog (managed by parent page)
- `frontend/src/components/Tasks/TaskShortCodeBadge.tsx` — status-color-coded short code badge; `clickable` prop controls navigation to `/task/$taskId`
- `frontend/src/components/Tasks/TaskPriorityBadge.tsx` — colored label for `low`, `normal`, `high`, `urgent`; hides for `"normal"` (default)
- `frontend/src/components/Tasks/SubtaskProgressChip.tsx` — `{completed}/{total}` chip; hidden when `total <= 0`
- `frontend/src/components/Tasks/TaskStatusBadge.tsx` — icon + color per status
- `frontend/src/components/Tasks/TaskStatusPill.tsx` — compact inline status indicator used in header and subtask rows
- `frontend/src/components/Tasks/CreateTaskDialog.tsx` — form with message, title, priority, agent, team; calls `TasksService.createTask()`
- `frontend/src/components/Tasks/RefinementChat.tsx` — shows `refinement_history`; submits via `TasksService.refineTask()`
- `frontend/src/components/Tasks/TaskTodoProgress.tsx` — TodoWrite progress indicator; subscribes to `TASK_TODO_UPDATED`
- `frontend/src/components/Tasks/TaskSessionsModal.tsx` — lists all sessions for a task; opened from the Sessions tab "View all" link
- `frontend/src/components/Chat/SubTasksPanel.tsx` — slide-out subtask list for current chat session; subscribes to `SESSION_STATE_UPDATED`

## Security

- All task endpoints restricted to owner via `CurrentUser` (JWT required)
- Agent API endpoints (`/agent/tasks/*`) authenticate via same JWT; agent identity resolved from `task.selected_agent_id`, not from the token
- Team delegation validated: creating agent's node must have directed connection to target node in team topology
- `original_message` immutable after creation (audit trail)
- `refinement_history` append-only (conversation audit)
- `status_history` append-only, write-only from the API (never deleted)
- Attachment download: ownership verified against `task.owner_id`; path traversal protection applied before serving files
- Agent workspace attach: file content fetched from agent-env HTTP API using per-environment Bearer token; stored in backend — agent env access not required for subsequent user downloads
- Status transitions validated against `InputTaskStatus.VALID_TRANSITIONS` map
- `task_prefix` validated: 1–10 uppercase alphanumeric characters (team settings)
