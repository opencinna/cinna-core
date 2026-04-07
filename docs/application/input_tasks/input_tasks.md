# Input Tasks

## Purpose

Enable users to receive, refine, and execute incoming tasks through an AI-assisted preparation workflow. Tasks transform vague or incomplete requests into detailed, agent-ready instructions before execution, and can be created manually or by other agents.

The task system also serves as the primary **collaboration surface** for agent work: every agent — whether standalone or part of a team — reports findings, deliverables, and progress by posting comments on tasks. File attachments, status history, and subtask hierarchies make the full work trail visible to users without needing to read raw session logs.

## Core Concepts

- **Input Task**: A user-facing request container that goes through refinement before agent execution. Holds original message, current description, and refinement history.
- **Short Code**: Human-readable task identifier auto-generated on creation (e.g., `TASK-1`, `HR-42`). Globally unique per owner. Uses the team's `task_prefix` when the task belongs to a team; otherwise defaults to `"TASK"`.
- **Title**: Short label derived from the first line of `original_message` on creation (max 100 chars). User-editable. Falls back to truncated message if null.
- **Priority**: `low`, `normal` (default), `high`, `urgent`. Affects display ordering on the task board.
- **Task Status**: Lifecycle state from creation through execution to archival.
- **Refinement**: AI-assisted process to improve a task description using the target agent's workflow prompt as context.
- **Refinement History**: Append-only log of user comments and AI responses during refinement.
- **Task Execution**: Creating a session linked to the task and sending the refined description as the initial message.
- **Task Comment**: Structured message posted on a task by an agent, a user, or the system. The primary way agents report results and findings.
- **Task Attachment**: File attached to a task or comment — deliverables, reports, data exports, images. Files from agent workspaces are transferred to backend storage so they persist even if the environment is stopped.
- **Status History**: Immutable, append-only audit trail of every status transition, recording who made the change and why.
- **Subtask**: A child task created within a parent task's hierarchy. Inherits `team_id` and `owner_id` from the parent. Agents in teams create subtasks via the `mcp__agent_task__create_subtask` tool.
- **Team-Scoped Task**: Task with `team_id` set. Enables delegation tools and uses the team's short-code prefix.
- **Agent-Initiated Task**: Task created by an agent via the `mcp__agent_task__create_task` tool, either for direct handover or user inbox.
- **Auto-Execute**: Flag that triggers immediate task execution after creation without requiring the user to manually press Execute. Can be set by users from the Create Task dialog (Execute switch) or by agents via `mcp__agent_task__create_task`.
- **Source Session**: The agent session that created a task via handover; used for delegation tracking.
- **Todo Progress**: Real-time task completion progress from agent's TodoWrite tool calls.

## User Stories / Flows

### Flow 1: User-Initiated Task

1. User opens the Create Task dialog; enters a title (required) and optional description
2. If the user has teams, team badges with icons are displayed below the description — clicking selects a team; clicking again or "None" deselects
3. Agent badges with color presets are displayed below the teams row; when a team is selected, only that team's agents are shown, with the lead agent sorted first (Crown icon) and auto-selected; when no team is selected, all workspace agents are shown
4. Clicking an agent badge selects or deselects it; in team mode, selecting an agent sets both `selected_agent_id` and `assigned_node_id`
5. The Execute switch in the dialog footer is enabled by default; it is disabled when no agent is selected; when on, `auto_execute: true` is included in the task payload
6. User submits the form; if `auto_execute=true` and `selected_agent_id` is set, the task is immediately executed after creation — a session is created and the task description sent as the initial message; otherwise the task is created with status `new` for manual review
7. System auto-generates `short_code` (e.g., `TASK-1`) and derives a title; if a team is selected with no agent specified, the team's lead agent is auto-assigned by the service layer
8. User is navigated to the task detail page at `/task/$taskId` (accessible by UUID or short code)
9. Left body: editable description, attachments section, tabbed section (Comments / Sessions / Sub-tasks / Activity) — all content areas use full available width
10. Right sidebar: Parent Task (shown only when task has a parent, clickable badge with parent short code; tree icon opens full task tree popover), Status, Priority dropdown, Assignee (agent selector modal), Team (team selector modal), Triggers, Subtask progress (clickable chip opens subtask list popover), Dates, Execute button
11. User sends refinement comments or text-selection requests via the inline input bar — AI refines description and provides feedback
12. User continues refining or clicks Execute; execution stays on the task page and shows live session progress in the Sessions tab
13. System creates a session linked to the task via `source_task_id`
14. Session start automatically transitions task status to `in_progress`; real-time session events update the Sessions tab without leaving the page
15. Agent works and posts comments with findings; files are attached to comments
16. Session completion automatically transitions task to `completed` only if all subtasks are also completed; if incomplete subtasks remain, task stays `in_progress`; task can be re-executed (Execute button becomes "Run Again")
17. User sees the full comment thread and any deliverable files
18. User archives completed tasks

### Flow 2: Agent-Initiated Direct Handover

1. Source agent calls `mcp__agent_task__create_task` with a target agent name
2. System creates task with `agent_initiated=true`, `auto_execute=true`, generates short code
3. If target agent has `refiner_prompt`, message is auto-refined
4. System auto-creates session; task transitions to `in_progress`
5. Target agent posts progress comments; attaches deliverables
6. Session completes; task transitions to `completed`
7. Task appears in Tasks list with full comment history

### Flow 3: Agent-Initiated Inbox Task

1. Source agent calls `mcp__agent_task__create_task` without specifying a target agent
2. System creates task with `agent_initiated=true`, `auto_execute=false`
3. Task appears in user's task inbox with status `new`
4. User reviews, optionally refines, selects agent, and executes
5. Subsequent flow matches Flow 1 from step 8

### Flow 4: Team Agent Delegation (Subtask Hierarchy)

1. User creates task assigned to a team's lead agent (team scoped, e.g., `HR-1`)
2. Lead agent starts session; task auto-transitions to `in_progress`
3. Lead agent calls `mcp__agent_task__create_subtask` to delegate work to connected team members
4. Each subtask is created with the team prefix (e.g., `HR-2`, `HR-3`), auto-executed for target agents
5. Each sub-agent posts comments with their results; attaches files; session completes
6. System posts a system comment on the parent task: "Subtask HR-2 completed by Recruiting Agent"
7. System delivers a feedback message to the lead agent's session (via `source_session_id`): "[Sub-task completed] HR-2 completed by Recruiting Agent. Read results with mcp__agent_task__get_details tool." If the session is idle, streaming auto-triggers so the lead agent processes the notification immediately
8. Lead agent reads subtask comments via `mcp__agent_task__get_details`
9. Lead agent aggregates results, posts a summary comment on `HR-1`, attaches final report
10. Lead agent's session completes; parent task transitions to `completed`
11. User sees the full task tree with all subtask work at a glance

### Flow 5: Email-Originated Task

1. Incoming email creates a task automatically (via email integration)
2. System assigns short code, derives title from email subject
3. Assigned agent's session starts; agent posts progress comments
4. User can trigger `send-answer` to email the result back to the sender

## Business Rules

### Task Status Lifecycle

| Status | Description | Automatic? |
|--------|-------------|-----------|
| `new` | Created, awaiting refinement or assignment | — |
| `refining` | User actively refining with AI | — |
| `open` | Refined and assigned, ready for execution | — |
| `in_progress` | Agent actively working | Yes — session start |
| `blocked` | Agent waiting for external input or dependency | Agent tool only |
| `completed` | Task finished successfully | Yes — session completion |
| `error` | Task failed | Yes — session error |
| `cancelled` | Cancelled by user or agent | — |
| `archived` | Archived by user | — |

**Archival**: Users can archive a task from any non-archived status. Archived tasks are excluded from subtask progress counts.

**Automatic status management**: The backend infers status from the session lifecycle. Agents should not call `mcp__agent_task__update_status` for normal completion — only for edge cases (`blocked`, explicit `cancelled`, or early `completed` before the session ends).

**Removed statuses (migrated)**:
- `running` — migrated to `in_progress`
- `pending_input` — migrated to `blocked`

### Session-to-Task Status Sync

- Session start → task = `in_progress` (system comment: "Agent {name} started working")
- Session completion → task = `completed` only if ALL subtasks are also completed; if incomplete subtasks remain, task stays `in_progress` (system comment; triggers parent notification if subtask)
- Session error → task = `error` (system comment with error details)
- If task has `parent_task_id` and transitions to `completed` → system comment posted on parent; parent agent notified
- Idle active sessions (no streaming, no pending input) do not block task completion — only actively running or pending sessions count
- Incomplete subtasks (non-archived, non-completed) block task completion — even if all sessions are done, the task remains `in_progress` until every subtask reaches `completed` or `archived`
- On task completion or error, if `source_session_id` is set and `auto_feedback` is enabled, a feedback message is delivered to the source session (e.g., `[Sub-task completed] HR-2 completed by Agent Name`). If the source session is idle, streaming is auto-triggered so the parent agent processes the notification immediately

### Short Code Generation

- Per-owner monotonic counter (`task_sequence_counter` on `user` table), incremented atomically
- Prefix determined at creation: team's `task_prefix` if task has `team_id` and team has non-null prefix; otherwise `"TASK"`
- Format: `{prefix}-{counter}` (e.g., `TASK-1`, `HR-42`)
- Counter is global per owner — the 42nd task of any prefix is the user's 42nd overall task
- Short codes are globally unique per owner

### Comment Types

| Type | Description | Author |
|------|-------------|--------|
| `message` | Regular user or agent comment | User or agent |
| `result` | Agent final result / deliverable (semantically tagged) | Agent |
| `status_change` | Auto-generated on every status transition | System |
| `assignment` | Auto-generated on task assignment change | System |
| `system` | Platform notifications (subtask completion, etc.) | System |

### Task Attachments

- Files from agent workspaces are fetched by the backend from the agent-env HTTP API (`GET /workspace/download/{rel_path}`) and stored in `backend/data/uploads/{owner_id}/task_attachments/{attachment_id}/{filename}`
- Path normalization is applied before fetching: `./reports/file.json`, `/app/workspace/reports/file.json`, and `reports/file.json` are all treated as equivalent
- Files persist in backend storage even when the agent environment is stopped or rebuilt
- `source_agent_id` and `source_workspace_path` record provenance ("generated by Recruiting Agent at `output/report.csv`")
- Attachments can be linked to a specific comment (`comment_id`) or standalone on the task
- Download endpoint: `GET /api/v1/tasks/{task_id}/attachments/{attachment_id}/download`

### Task File Auto-Upload on get_details

When an agent calls `mcp__agent_task__get_details`, the backend automatically uploads all files associated with the task directly into the agent's Docker workspace. This eliminates the need for agents to manually locate or request task-related files.

Files are collected from two sources:
- **User-uploaded files** (`InputTaskFile`) — files the user attached to the task before or during execution
- **Task attachments** (`TaskAttachment`) — files attached by agents or users via comments or the task attachment panel

All collected files are uploaded to `/app/workspace/uploads/task_{SHORT_CODE}/` in the calling agent's environment (e.g., `/app/workspace/uploads/task_HR-5/`). The tool response lists each file's local workspace path so the agent can reference them immediately.

Files are deduplicated by filename within a single task. Files exceeding the platform size limit are skipped. If the agent's environment is not running, file upload is silently skipped and the task details are still returned.

### File Validation in add_comment

Before the `mcp__agent_task__add_comment` tool sends a comment to the backend, it validates that every file path in the `files` parameter exists locally in the agent's workspace at `/app/workspace`. Relative paths are resolved against `/app/workspace`. If any file is missing, the tool returns an error listing the missing paths and does **not** post the comment. This prevents posting comments with broken file references.

As defense-in-depth, the backend also tracks attachment outcomes. The API returns `AgentCommentResponse` with `attachments_count` and `failed_attachments` fields, so even if validation is bypassed (e.g., stale environment without updated tool code), the MCP tool can detect and report partial or total attachment failure to the agent.

### Team Assignment Rules

- A task's team can be changed after creation via `PATCH /api/v1/tasks/{id}` (the `InputTaskUpdate` model now includes `team_id`)
- When the team is changed via the UI, the frontend immediately fetches team nodes, finds the lead node (`is_lead=True`), and updates `selected_agent_id` and `assigned_node_id` to match the lead agent
- At task creation, if `team_id` is provided but neither `selected_agent_id` nor `assigned_node_id` is set, the service layer auto-assigns the team's lead node agent

### Subtask Rules

- Only agents in a **team context** can create subtasks (requires `team_id` on parent task)
- Delegation is topology-constrained: the creating agent's node must have a directed connection to the target node in the team graph
- Orphaned subtasks become root tasks on parent delete (SET NULL, not CASCADE)
- Subtask inherits `team_id` and `owner_id` from parent; gets its own short code with the team prefix
- Subtask records `source_session_id` from the creating agent's session, enabling automatic feedback delivery on completion

### Refinement Rules

- `original_message` is immutable after creation (audit trail)
- `refinement_history` is append-only
- Refinement uses the target agent's `workflow_prompt` as AI context
- Last 5 history items are passed to the AI refiner for context

### Execution Rules

- Task must have a selected agent to execute
- Agent must have an active environment to execute
- Only `running` (legacy alias for `in_progress`) and `archived` statuses block execution; tasks in `completed`, `error`, `cancelled`, and other non-archived statuses can be re-executed
- Execute action stays on the task detail page — it does not navigate away; live session progress is shown in the sessions block via real-time events
- The Execute button label changes to "Run Again" when one or more sessions already exist for the task

### Todo Progress Tracking

- When agent calls TodoWrite tool during execution, progress is captured
- Stored on both Session and InputTask for persistence and display
- Real-time updates via `TASK_TODO_UPDATED` event

## Architecture Overview

```
User → Frontend → Backend API → InputTaskService → SessionService
                      │
                      ├── Refine: AIFunctionsService → TaskRefiner (LLM)
                      ├── Execute: SessionService.create_session(source_task_id)
                      ├── Comments: TaskCommentService
                      ├── Attachments: TaskAttachmentService ←→ Agent Environment HTTP API
                      │               (GET /workspace/download/{rel_path} to fetch from agent)
                      └── get_details: InputTaskService.upload_task_files_to_agent_env()
                                       (POST /files/upload?subfolder=task_{CODE} to push to agent)

Task (1) ──────────────────────────────────────> (N) Session
        source_task_id (authoritative FK)

Task (1) ──────────────────────────────────────> (N) TaskComment
Task (1) ──────────────────────────────────────> (N) TaskAttachment
Task (1) ──────────────────────────────────────> (N) TaskStatusHistory
Task (1, parent) ──────────────────────────────> (N) Task (subtasks, parent_task_id)

Agent Collaboration (team context):
Parent Task ──create_subtask──> Subtask ──auto_execute──> Target Agent Session
      ↑                                                           │
      ├──── system comment on parent ◄── session completed ───────┘
      └──── feedback message to parent's session (auto_feedback) ──┘
```

## Integration Points

- **Agent Handover**: Source agent uses `mcp__agent_task__create_task` to create tasks for direct handover or inbox — see [Agent Handover](../../agents/agent_handover/agent_handover.md)
- **Agentic Teams**: Team-scoped tasks use the team's `task_prefix`; subtask delegation follows team topology — see [Agentic Teams](../../agents/agentic_teams/agentic_teams.md)
- **Sessions**: Task execution creates sessions with `source_task_id` backlink; session lifecycle events drive automatic status updates — see [Agent Sessions](../agent_sessions/agent_sessions.md)
- **Agent Environment Core**: Six MCP tools (`mcp__agent_task__*`) let agents interact with tasks from inside environments. `get_details` automatically uploads task files to the agent workspace. `add_comment` validates attached file paths locally before sending — see [Agent Environment Core](../../agents/agent_environment_core/agent_environment_core.md) and [Agent Task Tools](../../agents/agent_environment_core/create_agent_task_tool.md)
- **Task Triggers**: Automated rules (CRON, webhook, date) that fire task execution; gains short-codes automatically — see [Task Triggers](task_triggers.md)
- **Activities**: Session state events generate activities for user notification — see [Agent Activities](../agent_activities/agent_activities.md)
- **Email Integration**: Incoming emails can create tasks automatically — see [Email Integration](../email_integration/email_integration.md)
- **File Management**: Task attachments use the same storage infrastructure as agent file management — see [Agent File Management](../../agents/agent_file_management/agent_file_management.md)
- **Real-time Events**: `TASK_COMMENT_ADDED`, `TASK_STATUS_CHANGED`, `TASK_ATTACHMENT_ADDED`, `SUBTASK_COMPLETED` events notify the frontend — see [Real-time Events](../realtime_events/event_bus_system.md)
