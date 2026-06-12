# Agent Task Tools (Agent-Env Side)

## Purpose

MCP tools running inside agent Docker containers that enable agents to interact with the task system: creating tasks, reporting results via comments, attaching files, updating status, and reading task details. Available only in **conversation mode** — building mode agents cannot use these tools.

## Tools

### mcp__agent_task__create_task

Creates a new standalone task. Optionally assigns it to an agent by name for auto-execution. For team agents, the task inherits the team context from the calling session's task.

**Parameters:** `title` (required), `description` (optional), `assigned_to` (optional — agent or team member name), `priority` (optional — low/normal/high/urgent)

1. Agent invokes `mcp__agent_task__create_task` with `title`, optional `description`, `assigned_to`, `priority`
2. Tool retrieves the backend session ID via `get_backend_session_id()` (global state in `sdk_manager.py`)
3. Makes authenticated `POST /api/v1/agent/tasks/create` with `source_session_id`
4. Backend resolves `assigned_to` by name (team node name first, then agent name fallback)
5. If assigned: creates `InputTask` with `auto_execute=true`, optionally auto-refines, creates session, sends message
6. If unassigned: creates `InputTask` as inbox task for user review
7. Backend posts system message to source session with task link
8. Tool returns the task's short code (e.g., `TASK-5` or `HR-42`)

### mcp__agent_task__create_subtask

Creates a subtask under the agent's current task and delegates it to a connected team member. Only available in team context — the parent task must have a `team_id`.

**Parameters:** `title` (required), `description` (optional), `assigned_to` (optional — team member name), `priority` (optional — low/normal/high/urgent)

1. Agent invokes `mcp__agent_task__create_subtask` with `title`, optional `description`, `assigned_to`, `priority`
2. Tool retrieves the backend session ID via `get_backend_session_id()`
3. Makes authenticated `POST /api/v1/agent/tasks/current/subtask` with `source_session_id`
4. Backend resolves the parent task from `source_session_id` (`InputTask WHERE session_id = source_session_id`)
5. Backend validates team membership and connection topology (target must be reachable via directed connection)
6. Creates child `InputTask` linked to parent with `source_session_id` set to the creating session, auto-executes if assigned
7. When the subtask's session completes, the system auto-delivers a feedback message to the `source_session_id` session (the creating agent), waking it up if idle
8. Tool returns the subtask short code and parent task short code

## mcp__agent_task__add_comment

Posts a comment on a task — the primary way agents share findings, results, and deliverables with users and other agents.

**Parameters:** `content` (required — markdown supported), `files` (optional — list of workspace file paths to attach), `task` (optional — short code; defaults to current task)

**File validation (agent-env side):** Before the comment is sent to the backend, the tool validates that every path in `files` exists locally in the agent's Docker workspace (`/app/workspace`). Relative paths are resolved against `/app/workspace`. If any file is missing, the tool returns an error listing the missing paths and does **not** post the comment. This prevents referencing files that were never created.

**File attachment flow (backend side):** When `files` is provided and all files pass local validation, the backend receives the absolute paths and calls `TaskAttachmentService.attach_from_workspace()` to fetch each file from the agent environment, store it in backend storage, and create `TaskAttachment` records linked to the comment.

Both the Claude Code adapter (`agent_task_add_comment.py`) and the MCP bridge (`task_server.py`) implement the same local validation logic.

## mcp__agent_task__get_details

Retrieves full details of a task and automatically uploads all task-associated files to the calling agent's workspace.

**Parameters:** `task` (optional — short code; defaults to current task)

**File auto-upload behavior:** When an agent calls `get_details`, the backend:

1. Collects all files linked to the task from two sources:
   - `InputTaskFile` user uploads (files the user attached to the task before or during execution)
   - `TaskAttachment` records (files agents or users attached to the task or its comments)
2. For each file, reads it from backend storage and `POST /files/upload` to the calling agent's environment with a `subfolder` of `task_{SHORT_CODE}` (e.g., `task_TASK-5` or `task_HR-42`)
3. Files land in the agent's workspace at `/app/workspace/uploads/task_{SHORT_CODE}/`
4. The response includes an `uploaded_files` list with each file's local workspace path and size

The tool output renders a **Task Files** section listing the uploaded paths so the agent knows exactly where to find them without needing to search the workspace.

Files are deduplicated by filename within a task — if a filename appears in both `InputTaskFile` and `TaskAttachment` sources, only the first occurrence is uploaded.

Files exceeding the platform file size limit are skipped with a warning log. If the agent environment is not running, file upload is skipped silently and the rest of the task details are still returned.

**Explicit task_id variant:** `GET /agent/tasks/{task_id}/details?source_session_id=...` also performs file upload when `source_session_id` is provided. Without `source_session_id`, the `files` key is stripped from the response (no environment to upload to).

## Registration

- All six tools registered in the `agent_task` MCP server: `add_comment`, `update_status`, `create_task`, `create_subtask`, `get_details`, `list_tasks`
- Registered in the Claude Code adapter only when `mode == "conversation"` — not available in building mode
- Full tool names: `mcp__agent_task__create_task`, `mcp__agent_task__create_subtask`, `mcp__agent_task__add_comment`, `mcp__agent_task__get_details`, `mcp__agent_task__update_status`, `mcp__agent_task__list_tasks`
- Listed in backend's pre-allowed tools — agents can invoke without per-call user approval

## Session ID Access

Python's `contextvars.ContextVar` does not propagate into tool execution contexts created by the Claude SDK. The tools access session IDs through global state:

- `get_backend_session_id()` — returns the active backend session UUID from a global map
- An async lock (`_sdk_session_lock`) in `send_message_stream()` serializes SDK sessions to prevent race conditions when multiple requests arrive concurrently

## System Prompt Integration

Task creation availability is injected into the conversation mode system prompt in two ways:

1. **Task context section** (`prompt_generator.py:build_task_context_section()`) — for sessions with a linked task, includes delegation instructions referencing `mcp__agent_task__create_subtask` and downstream team members
2. **Handover prompt** (`prompt_generator.py:_load_handover_prompt()`) — reads `{workspace}/docs/agent_handover_config.json` from the agent's workspace directory at runtime; provides named handover targets and trigger instructions for `mcp__agent_task__create_task`

## Authentication

The tools use the same bearer token as other agent-env → backend calls:

- `Authorization: Bearer <AGENT_AUTH_TOKEN>` — token generated at environment creation
- Validated against the environment record in the backend database

## File References

- **Tool implementations**:
  - `backend/app/env-templates/app_core_base/core/server/tools/agent_task_create_task.py`
  - `backend/app/env-templates/app_core_base/core/server/tools/agent_task_create_subtask.py`
  - `backend/app/env-templates/app_core_base/core/server/tools/agent_task_add_comment.py` — includes local file existence validation
  - `backend/app/env-templates/app_core_base/core/server/tools/agent_task_get_details.py` — renders "Task Files" section from `uploaded_files`
- **MCP bridge (OpenCode)**: `backend/app/env-templates/app_core_base/core/server/tools/mcp_bridge/task_server.py` — all six tools; `add_comment` and `get_details` implement the same validation/rendering as their Claude Code counterparts
- **Adapter registration**: `backend/app/env-templates/app_core_base/core/server/adapters/claude_code_sdk_adapter.py`
- **Global session state + helpers**: `backend/app/env-templates/app_core_base/core/server/sdk_manager.py` (`get_backend_session_id()`)
- **Prompt injection**: `backend/app/env-templates/app_core_base/core/server/prompt_generator.py` — `build_task_context_section()`, `_load_handover_prompt()`
- **Config storage**: `backend/app/env-templates/app_core_base/core/server/agent_env_service.py` — `get_agent_handover_config()`
- **Runtime config file**: `{workspace}/docs/agent_handover_config.json`
- **Agent-env upload endpoint**: `backend/app/env-templates/app_core_base/core/server/routes.py` — `POST /files/upload` (accepts optional `subfolder` form field; used by task file auto-upload to place files under `uploads/task_{SHORT_CODE}/`)
- **Backend endpoints**:
  - `backend/app/api/routes/task_agent_api.py` — `POST /api/v1/agent/tasks/create`
  - `backend/app/api/routes/task_agent_api.py` — `GET /api/v1/agent/tasks/by-code/{short_code}` (used by `get_details`, `update_status`, `add_comment` tools to resolve explicit `task` params)
  - `backend/app/api/routes/task_agent_api.py` — `POST /api/v1/agent/tasks/current/subtask`
  - `backend/app/api/routes/task_agent_api.py` — `GET /api/v1/agent/tasks/current/details` and `GET /api/v1/agent/tasks/{task_id}/details` — both `async def`; delegate file upload to service
- **Service methods**: `backend/app/services/tasks/input_task_service.py`
  - `create_task_from_agent()`, `create_subtask()`
  - `_collect_task_files_info()` — gathers `InputTaskFile` + `TaskAttachment` records for a task
  - `upload_task_files_to_agent_env()` — async; reads files from backend storage and POSTs to agent-env `/files/upload` with `subfolder`
- **Pre-allowed list**: `backend/app/services/sessions/message_service.py`
- **System message rendering**: `frontend/src/components/Chat/MessageBubble.tsx` — renders task creation notifications with session/task links

## Related Docs

- [Agent Handover](../agent_handover/agent_handover.md) — feature documentation: configuration management, UI, business rules, clone behavior
- [Agent Handover Tech](../agent_handover/agent_handover_tech.md) — backend services, database schema, API endpoints, sync flow
- [Session State Tools](session_state_tools.md) — sibling tools: `update_status` and `add_comment` for reporting on tasks
- [Agent Environment Core](agent_environment_core.md) — parent feature: server running inside Docker containers
- [Input Tasks](../../application/input_tasks/input_tasks.md) — task lifecycle, subtask delegation, comments
