# Session State Tools (Agent-Env Side)

## Purpose

Two MCP tools running inside agent Docker containers that implement bi-directional communication between a target agent (executing a task) and the source agent (that created the task via handover). Available only in **conversation mode**.

- **`update_session_state`** — called by the target agent to declare its session outcome
- **`respond_to_task`** — called by the source agent to reply to a sub-task clarification request

## How They Work

### update_session_state

The target agent calls this tool when it finishes work, needs clarification, or encounters an unrecoverable error.

1. Agent invokes `mcp__task__update_session_state` with a `state` and `summary`
2. Tool makes authenticated `POST /api/v1/agents/sessions/update-state` with the current backend session ID
3. Backend stores `result_state` and `result_summary` on the Session model
4. Backend emits `SESSION_STATE_UPDATED` event — two handlers fire:
   - `ActivityService.handle_session_state_updated()` → creates an offline activity notification for the user
   - `InputTaskService.handle_session_state_updated()` → if `auto_feedback=true`, delivers a feedback message to the source session
5. If the source session is idle when feedback arrives, the source agent is automatically triggered to process it

**States:**

| State | Meaning | Source Agent Sees |
|-------|---------|------------------|
| `completed` | Task finished successfully | `[Sub-task completed] {summary}` |
| `needs_input` | Agent needs clarification from source | `[Sub-task needs input] {summary}` |
| `error` | Unrecoverable failure | `[Sub-task error] {summary}` |

### respond_to_task

The source agent calls this tool after receiving a `[Sub-task needs input]` feedback message.

1. Source agent invokes `mcp__task__respond_to_task` with `task_id` and `message`
2. Tool makes authenticated `POST /api/v1/agents/tasks/respond`
3. Backend verifies the source session owns the task, resets `result_state` to null (session back in progress)
4. Backend sends `message` to the target session via `SessionService.send_session_message()`
5. Target agent continues processing with the provided answer

## Registration

- Both tools registered in the `task` MCP server alongside `create_agent_task`
- Registered in the Claude Code adapter only when `mode == "conversation"`
- Full tool names: `mcp__task__update_session_state`, `mcp__task__respond_to_task`
- Both listed in backend's pre-allowed tools — agents can invoke without per-call user approval

## System Prompt Integration

### Target Agent Prompt (all conversation-mode sessions)

`prompt_generator.py` appends a "Session State Reporting" section to every conversation-mode system prompt:
- Instructs agent to call `update_session_state` when finished, needing clarification, or on unrecoverable error
- Explains the three valid states and when to use each

### Source Agent Prompt (agents with handover configs)

`agent_service.py:sync_agent_handover_config()` appends a "Handling Sub-Task Feedback" section:
- Describes the three message prefixes the source agent will receive
- Instructs use of `respond_to_task` when a sub-task reports `needs_input`
- Explains auto-triggering behavior (source agent may be awakened automatically)

## Authentication

Same bearer token mechanism as other agent-env → backend calls:
- `Authorization: Bearer <AGENT_AUTH_TOKEN>` — token generated at environment creation
- Validated against the environment record in the backend database

## File References

- **Tool implementations**:
  - `backend/app/env-templates/app_core_base/core/server/tools/update_session_state.py`
  - `backend/app/env-templates/app_core_base/core/server/tools/respond_to_task.py`
- **Adapter registration**: `backend/app/env-templates/app_core_base/core/server/adapters/claude_code.py`
- **Target agent prompt injection**: `backend/app/env-templates/app_core_base/core/server/prompt_generator.py`
- **Source agent prompt injection**: `backend/app/services/agent_service.py` — `sync_agent_handover_config()`
- **Backend endpoints**:
  - `backend/app/api/routes/agents.py` — `POST /api/v1/agents/sessions/update-state`
  - `backend/app/api/routes/agents.py` — `POST /api/v1/agents/tasks/respond`
- **Event handlers**:
  - `backend/app/services/input_task_service.py` — `handle_session_state_updated()`, `deliver_feedback_to_source()`
  - `backend/app/services/activity_service.py` — `handle_session_state_updated()`
- **Pre-allowed list**: `backend/app/services/message_service.py`

## Related Docs

- [Input Tasks](../../application/input_tasks/input_tasks.md) — feature documentation: task lifecycle, bi-directional feedback flows, auto-feedback rules
- [Input Tasks Tech](../../application/input_tasks/input_tasks_tech.md) — backend services, session model additions, event handler registration
- [Create Agent Task Tool](create_agent_task_tool.md) — sibling tool: creates the task that these tools later report on
- [Agent Handover](../agent_handover/agent_handover.md) — feature that configures auto_feedback per handover target
- [Agent Environment Core](agent_environment_core.md) — parent feature: server running inside Docker containers

---

*Aspect of: Agent Environment Core. Related feature: [Input Tasks](../../application/input_tasks/input_tasks.md)*
