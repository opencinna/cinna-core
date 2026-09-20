# Session State Tools (Agent-Env Side)

## Purpose

MCP tools running inside agent Docker containers that implement bi-directional communication between a target agent (executing a task) and whoever is waiting on the result — the source agent for an in-platform handover, or the requester of a durable cross-agent delegation. Available only in **conversation mode**.

- **`update_status`** — called by the target agent to declare task outcome (edge cases only; standard completion is automatic)
- **`add_comment`** — called by agents to post findings, results, and replies on tasks
- **`handover_report`** — called by an agent executing a task created with `delegation_metadata` (the [Cross-Agent Delegation Contract](../../application/input_tasks/input_tasks.md#cross-agent-delegation-contract)) to file the structured result the requester reads, instead of `update_status`/`add_comment`

## How They Work

### update_status

The target agent calls this tool for edge-case status updates: `blocked` (waiting for external input), explicit `completed` (before session ends), or `cancelled`. Standard `in_progress` and `completed` transitions are handled automatically by the backend from session lifecycle events.

**Parameters:** `status` (required), `reason` (optional), `task` (optional short code — defaults to current task)

1. Agent invokes `mcp__agent_task__update_status` with `status`, optional `reason`, optional `task` short code
2. If `task` short code provided: tool first calls `GET /api/v1/agent/tasks/by-code/{short_code}` to resolve the UUID, then posts to `POST /api/v1/agent/tasks/{task_id}/status`
3. If no `task` provided (default): tool posts to `POST /api/v1/agent/tasks/current/status` with `source_session_id`; backend resolves the current task from the session
4. Backend validates the status transition and updates the task
5. Backend creates a `TaskStatusHistory` record and system comment
6. Backend emits `TASK_STATUS_CHANGED` event

**Edge-case statuses:**

| Status | When to Use |
|--------|-------------|
| `blocked` | Agent is waiting for external input or resources |
| `completed` | Agent wants to mark done before session naturally ends |
| `cancelled` | Task is no longer viable |

### add_comment

Agents use this tool to post findings, results, and progress as comments on their task. This is the primary reporting mechanism for task work.

**Parameters:** `content` (required), `files` (optional — workspace file paths to attach), `task` (optional short code — defaults to current task)

1. Agent invokes `mcp__agent_task__add_comment` with `content`, optional `files` (workspace paths), optional `task` short code
2. If `task` short code provided: tool first calls `GET /api/v1/agent/tasks/by-code/{short_code}` to resolve the UUID, then posts to `POST /api/v1/agent/tasks/{task_id}/comment`
3. If no `task` provided (default): tool posts to `POST /api/v1/agent/tasks/current/comment` with `source_session_id`; backend resolves the current task from the session
4. Backend creates a `TaskComment` with `comment_type="agent"`, optionally attaching workspace files
5. Comment appears in the task's comment thread visible to the user

### handover_report

Reports the structured result of a task delegated through the [Cross-Agent Delegation Contract](../../application/input_tasks/input_tasks.md#cross-agent-delegation-contract) — a durable alternative to `update_status`/`add_comment` for a requester that is not watching this session's chat. Executing such a task appends an instruction to the agent's first message explaining that this tool, not a chat reply, is what the requester reads.

**Parameters:** `status` (required — `in_progress` / `blocked` / `done` / `failed`), `summary` (required, ≤ 1000 chars), `question` (required when `status` is `blocked`), `audience` (`requester` or `user`, default `user`), `artifacts` (optional — portable HTTP(S) `ref` only; upload workspace files with `add_comment` first), `body` (optional — the full answer or report)

1. Agent invokes `handover_report` (Claude Code: `mcp__agent_task__handover_report`; OpenCode MCP bridge: `agent_task_handover_report`) with the fields above
2. The tool resolves the active backend session ID (`get_backend_session_id()`) and posts to `POST /api/v1/agent/tasks/current/delegation-result` with that `source_session_id`
3. The backend resolves the task from the session, validates the report, and stores it as the task's delegation result — driving the task's status (`done`/`failed` complete or fail it; `blocked` holds it with the question)
4. The agent ends its turn; a reply to a `blocked` question later resumes this same session as a new message

Unlike `update_status`/`add_comment`, `handover_report` has no explicit-`task`-short-code form — it always reports for the session's own current task.

## Registration

- All three tools registered in the `agent_task` MCP server alongside `create_task`, `create_subtask`, `get_details`, `list_tasks`
- Registered in the Claude Code adapter only when `mode == "conversation"`
- Full tool names: `mcp__agent_task__update_status`, `mcp__agent_task__add_comment`, `mcp__agent_task__handover_report`
- All listed in backend's pre-allowed tools — agents can invoke without per-call user approval

## System Prompt Integration

### Task Context Section (all conversation-mode sessions with linked task)

`prompt_generator.py:build_task_context_section()` appends task reporting instructions to every conversation-mode session that has a linked task:
- Instructs agent to use `mcp__agent_task__add_comment` to post findings and results
- Explains that `mcp__agent_task__update_status` is for edge cases only (blocked, explicit completed, cancelled)
- Standard completion is automatic when the session ends

### Source Agent Prompt (agents with handover configs)

`agent_service.py:sync_agent_handover_config()` appends a "Handling Sub-Task Feedback" section:
- Describes the notification messages the source agent receives when subtasks complete, need input, or error
- Explains auto-triggering behavior (source agent may be awakened automatically)

## Authentication

Same scoped env-token mechanism as other agent-env → backend calls:
- `Authorization: Bearer <AGENT_AUTH_TOKEN>` — scoped env JWT minted at environment creation
- `X-Agent-Env-Id: <ENV_ID>` — environment UUID header
- Authenticated by the scoped `AgentEnvContextDep` (not `get_current_user`); resolves and is confined to the calling env's own `(environment, agent, owner)` scope. See [Agent Environment Core](agent_environment_core_tech.md#jwt-authentication-scoped-env-token).

## File References

- **Tool implementations**:
  - `backend/app/env-templates/app_core_base/core/server/tools/agent_task_update_status.py`
  - `backend/app/env-templates/app_core_base/core/server/tools/agent_task_add_comment.py`
  - `backend/app/env-templates/app_core_base/core/server/tools/agent_task_handover_report.py` — `handover_report`
- **MCP bridge (OpenCode)**: `backend/app/env-templates/app_core_base/core/server/tools/mcp_bridge/task_server.py` — includes `handover_report`, mirroring the Claude Code tool
- **Adapter registration**: `backend/app/env-templates/app_core_base/core/server/adapters/claude_code_sdk_adapter.py`, `backend/app/env-templates/app_core_base/core/server/adapters/opencode_sdk_adapter.py`
- **Task context prompt injection**: `backend/app/env-templates/app_core_base/core/server/prompt_generator.py` — `build_task_context_section()`
- **Source agent prompt injection**: `backend/app/services/agents/agent_service.py` — `sync_agent_handover_config()`
- **Backend endpoints**:
  - `backend/app/api/routes/task_agent_api.py` — `GET /api/v1/agent/tasks/by-code/{short_code}` (short code → UUID resolution)
  - `backend/app/api/routes/task_agent_api.py` — `POST /api/v1/agent/tasks/current/status` (session-resolved variant)
  - `backend/app/api/routes/task_agent_api.py` — `POST /api/v1/agent/tasks/{task_id}/status` (explicit task_id variant)
  - `backend/app/api/routes/task_agent_api.py` — `POST /api/v1/agent/tasks/current/comment` (session-resolved variant)
  - `backend/app/api/routes/task_agent_api.py` — `POST /api/v1/agent/tasks/{task_id}/comment` (explicit task_id variant)
  - `backend/app/api/routes/task_agent_api.py` — `POST /api/v1/agent/tasks/current/delegation-result` (`handover_report`'s endpoint)
- **Session event handlers**:
  - `backend/app/services/tasks/input_task_service.py` — `handle_session_completed()`, `handle_session_error()`
  - `backend/app/services/events/activity_service.py` — `handle_session_state_updated()`
- **Delegation report/reply logic**: `backend/app/services/tasks/delegation_service.py` — `DelegationService`
- **Pre-allowed list**: `backend/app/services/sessions/message_service.py`

## Related Docs

- [Input Tasks](../../application/input_tasks/input_tasks.md) — feature documentation: task lifecycle, subtask delegation, comments, and the [Cross-Agent Delegation Contract](../../application/input_tasks/input_tasks.md#cross-agent-delegation-contract)
- [Input Tasks Tech](../../application/input_tasks/input_tasks_tech.md) — backend services, task agent API, session event handlers, `DelegationService`
- [Create Agent Task Tool](create_agent_task_tool.md) — sibling tool: creates the task that these tools later report on
- [Agent Handover](../agent_handover/agent_handover.md) — feature that configures handover targets and prompts for the *in-platform* handoff; the delegation contract is a separate, session-independent mechanism
- [Agent Environment Core](agent_environment_core.md) — parent feature: server running inside Docker containers

---

*Aspect of: Agent Environment Core. Related feature: [Input Tasks](../../application/input_tasks/input_tasks.md)*
