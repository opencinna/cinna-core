---
feature: input_tasks
domain: tasks
one_liner: "Lets a user or agent submit, AI-refine, execute, and track a task through comments, attachments, status history, and team subtask delegation."
docs:
  tech: input_tasks_tech.md
affects: [agent_environment_core]
---
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
- **External Ref**: Caller-supplied idempotency key on task creation (`external_ref`, max 64 chars), unique per owner. An external client sends its own local task id so a retried create returns the first task instead of making a second one.
- **Sync Cursor**: The `updated_since` and `updated_since_id` query params on the task list — an incremental-pull cursor for clients that poll for what changed, instead of re-reading the whole list.
- **Externally-Executed Task**: A task a user-authenticated client runs *outside* cinna (Cinna Desktop running it on a local agent). There is no cinna session; the client mirrors status back through the user status route so the web view reflects reality.
- **Delegation Metadata**: Optional `delegation_metadata` set on create (`requester_key`, `origin_kind`, optional origin agent/chat/task references, `depth`, `root`, optional `group`) that marks a task as a durable, cross-agent delegation rather than an ordinary in-platform handover. Requires `external_ref`; excludes `parent_task_id`.
- **Delegation Result**: The structured report an executing agent files with the `handover_report` tool (`status`, `summary`, `question`, `audience`, `artifacts`, `body`, `reply_state`), stored on the task and read by the requester instead of the chat transcript. See [Cross-Agent Delegation Contract](#cross-agent-delegation-contract).

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

### Flow 5: Task Executed Outside cinna (External Client Mirror)

A user-authenticated client — today, the Cinna Desktop app — can run a task on a
local agent of its own and keep the cinna copy in step. cinna never executes such
a task and never opens a session for it; it holds the mirror (title, description,
priority, comments, attachments, short code) and the status the client reports.

1. The client creates the task with `POST /api/v1/tasks/`, sending its own local task id as `external_ref` and `external_executor: "desktop"`. If the response is lost and the client retries, the same call returns the same task — no duplicate, and no second execution
2. The client runs the work locally and reports progress with `POST /api/v1/tasks/{id}/status` (`open`, `in_progress`, `blocked`, `completed`, `error`, `cancelled`)
3. Each report is validated against the transition table, written to the status history attributed to the *user*, and posted to the comment feed as a `status_change` comment — identical in shape to a status change made anywhere else, so the web task page shows the real state instead of a permanent `new`
4. The client posts its handoff notes and deliverables through the ordinary comment and attachment endpoints
5. The client polls `GET /api/v1/tasks/?updated_since=<cursor>` to pick up changes made on the web (a priority change, a re-assignment, an archive), advancing with the last row's `updated_at` and `id` as `updated_since` and `updated_since_id`. Authenticated owner-room Socket.IO events can trigger these refreshes; polling remains a fallback
6. After a reinstall or a re-link, the client re-binds its local tasks to their cinna counterparts by `external_ref`, which is returned on every task, instead of matching on title

Nothing in the web UI writes through this path — the web executes tasks with
sessions, and the session lifecycle drives status for those. See
[User-Reported Status](#user-reported-status-work-executed-outside-cinna).

### Flow 6: Cross-Agent Delegation

A caller on the other end of a task is not always a live cinna session in the
same conversation — it may be a separate agent instance, another automation, or
a client relaying on an agent's behalf. The delegation contract exchanges a
structured result through the task record itself, so the requester does not
have to watch the executor's chat to know what happened.

1. The caller confirms the server supports the contract with `GET /api/v1/tasks/delegation-capabilities`, then creates the task with `POST /api/v1/tasks/`, supplying `external_ref` and `delegation_metadata` (`requester_key`, `origin_kind`, optional origin references, `depth`, `root`, optional `group`)
2. cinna executes the task on the assigned agent as usual; the agent's first message carries an appended note (absent from an ordinary task) explaining that a structured report — not a chat reply — is what the requester reads
3. The agent calls `handover_report` with `status` (`in_progress` / `blocked` / `done` / `failed`), a `summary`, and — for `blocked` — a `question` and an `audience` (`requester` or `user`)
4. The report is stored as the task's delegation result and drives its status; a `blocked` or terminal report is authoritative and survives the ordinary session-completion status recompute
5. If `blocked`, the task owner answers with `POST /api/v1/tasks/{id}/delegation-reply`, which resumes the agent's session with the reply as a new message
6. The requester reads the outcome from `GET /api/v1/tasks/{id}` (or `/detail`) — the delegation result's `status`, plus `reply_state` when a question was involved

See [Cross-Agent Delegation Contract](#cross-agent-delegation-contract) for the full rule set.

**A concrete external client.** The account CLI implements the caller side of
this flow as `cinna delegation create|status|report|reply` — confirmed by that
tool's own docs and tests, not inferred: **cinna-cli** repo, `/Users/evgenyl/dev/ml-llm/cinna-cli`, `docs/features/delegation/delegation.md`, `docs/features/delegation/delegation_tech.md`, `tests/test_delegation.py`. <!-- nocheck -->
`create`'s retry identity is the target agent plus a caller-chosen key, hashed
into `external_ref`; `status` is a single read, never a poll loop, and that
repo's own docs say a requester agent should end its turn and let **Cinna
Desktop** deliver the result rather than poll `status` in a loop. Beyond that
one claim, neither repo states how Desktop routes a `requester`-audience
question versus a `user`-audience one, or where either surfaces in its UI —
those are left unverified here.

## Business Rules

### Task Status Lifecycle

| Status | Description | Automatic? |
|--------|-------------|-----------|
| `new` | Created, awaiting refinement or assignment | — |
| `refining` | User actively refining with AI | — |
| `open` | Refined and assigned, ready for execution | — |
| `in_progress` | Agent actively working | Yes — session start |
| `blocked` | Agent waiting for external input or dependency | Agent tool, or an external client via the user status route |
| `completed` | Task finished successfully | Yes — session completion |
| `error` | Task failed | Yes — session error |
| `cancelled` | Cancelled by user or agent | — |
| `archived` | Archived by user | — |

**Archival**: Users can archive a task from any non-archived status. Archived tasks are excluded from subtask progress counts.

**Automatic status management**: The backend infers status from the session lifecycle. Agents should not call `mcp__agent_task__update_status` for normal completion — only for edge cases (`blocked`, explicit `cancelled`, or early `completed` before the session ends).

**Explicit status writes** come from two places, and only two: an agent inside its
environment (`mcp__agent_task__update_status` → `/api/v1/agent/tasks/*`), and a
user-authenticated client reporting on work cinna is not running
(`POST /api/v1/tasks/{id}/status`). Both go through the same auditing path, so
every change — automatic or explicit — leaves a status-history row and a
`status_change` comment. See [User-Reported Status](#user-reported-status-work-executed-outside-cinna).

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

### User-Reported Status (work executed outside cinna)

`POST /api/v1/tasks/{id}/status` is the user-authenticated mirror of the agent
status tool. It exists because every other *explicit* status write lives behind
the scoped environment token on `/api/v1/agent/tasks/*`, which by design refuses a
user token — so a client running a task on its own machine had no way to say so, and
the task read as `new` on the web for ever while its comments described work that
had finished.

- **Allowed targets**: `open`, `in_progress`, `blocked`, `completed`, `error`, `cancelled`. Anything else is refused with the same shape an agent's out-of-set request gets — a 400 naming the allowed set (`User can only set status to: ...`). `new` is the create state, `refining` belongs to the refine flow, and `archived` has its own route (`POST /{id}/archive`, which owns `archived_at`)
- **Transitions are validated** against the shared public transition table (session recomputation remains separate). `completed → in_progress` is allowed for Run Again. Client copies of the transition table should include this widening; narrowing the published table requires coordinated client updates. A client that keeps a local copy of the table and sends the *path* rather than the destination (`new → in_progress → completed`, not `new → completed`) never trips this
- **Attribution is the user**, never "the system". The status history is an audit trail, and recording a human's client as the platform would be a lie in it
- **It records; it does not act.** No session is started, resumed or interrupted. The route's only effects are the status field, the history row, the `status_change` comment and the real-time event
- **The server wins where the server is doing the work.** For a task cinna is executing, the session-driven recompute runs on the next session event and overrides whatever a client wrote. This is not a conflict in practice — an externally-executed task has no session, so nothing contends — but it is the rule if a client writes to a task it does not own the execution of
- **Ownership failures answer `400`, not `404`.** Every route in the task API reports "not yours" as a permission error, which carries a 400; only a genuinely absent task is a 404. Clients should treat the two identically on this route — unbind, do not retry

### External Execution Ownership (`external_executor`)

A client sets `external_executor: "desktop"` on create or PATCH to mark work it
runs outside cinna. The nullable string is limited to 100 characters after
trimming; blank values normalize to null. It is separate from the immutable
create-retry key `external_ref` and appears on public, extended and detail responses.

- A marked task disables automatic execution and automatic refinement. Manual Execute, Run Again and Refine also refuse it on the server, so a web client cannot accidentally duplicate local work.
- A marker can be claimed only when no cinna session is linked, including older sessions linked through `source_task_id`. Session creation and marker changes lock the same task row until their writes commit.
- An existing marker cannot be changed or cleared while status is `in_progress`, `blocked` or `refining`. Report completion, error or cancellation first, then PATCH `{"external_executor": null}` to release it. Repeating the same marker is allowed. Release does not turn automatic execution back on; explicitly execute when ready. A completed, released task supports Run Again.
- User-reported progress retains user attribution. Marked work leaves `executed_at` unset because cinna performed no execution; claiming a legacy sessionless mirror clears its old execution timestamp. `completed_at` still records completion.
- Task detail shows a notice, and board/list/chat subtask rows show an execution flag. Desktop tasks say “Running on Desktop” or “Managed by Desktop” according to status; other clients receive generic external-execution wording. Execute/refine controls explain why they are disabled. Comments and attachments remain available.
- Marker changes emit owner-scoped `task_updated` events, refreshing these web surfaces. The web UI has no marker-editing control; release belongs to the external client's task workflow.

### Idempotent Creation (`external_ref`)

`POST /api/v1/tasks/` accepts an optional `external_ref` (≤ 64 chars): the
caller's own identifier for the task. It is unique per owner, so two users may
independently use the same string.

- A create with an `external_ref` this owner has already used **returns the existing task**, not a duplicate and not a conflict. A retry after a lost response is meant to be indistinguishable from the first call
- **A matched create does not re-fire `auto_execute`.** Deduplicating the row while repeating its side effect would be worse than the duplicate row the key exists to prevent: same task, second agent session, duplicate work and duplicate result comments
- Blank and whitespace-only refs normalise to "no ref" and are stored as null, so they never collide with each other
- `external_ref` is **ignored on `POST /{id}/subtasks/`**. A ref there could match an unrelated root task, and the route would return that task while silently dropping the parent the caller asked for. The field is cleared rather than rejected, so a client that fills it uniformly still gets a real subtask
- The ref is returned on every task, so a client that lost its local database can re-bind by ref instead of guessing from titles

### Cross-Agent Delegation Contract

- **Requires `external_ref`, excludes `parent_task_id`.** A create with `delegation_metadata` but no `external_ref`, or with `parent_task_id` also set, is refused (400). Repeating the same `(owner, external_ref)` with identical `delegation_metadata` returns the same task (the ordinary idempotent-create behavior); repeating it with different metadata is refused (409) — a plain task cannot silently become a delegation, or the reverse, on retry.
- **Capability discovery.** `GET /api/v1/tasks/delegation-capabilities` — versioned (`version`, `metadata`, `structured_result`, `reply`) — lets a caller confirm the server understands `delegation_metadata` before sending it; an older server has no other way to say so. It is registered ahead of `GET /{id}` so it can never be captured by the id-matching route.
- **The report is the channel, not the chat.** Executing a delegated task appends reporting instructions to the agent's first message; an agent whose brief was written by another agent otherwise has no way to know a structured report is expected. The agent reports with the `handover_report` tool — shipped in the environment template — using `in_progress` / `blocked` (requires a `question`, defaults `audience` to `user`) / `done` / `failed`, a `summary`, `body`, and optional portable (HTTP(S)-only) `artifacts`. Unknown fields anywhere in the contract are rejected (422).
- **Two report routes, one gate.** An agent's own `handover_report` call reports through the environment-authenticated route, resolved from its session. A delegation handed to an executor outside any cinna session instead reports through the owner-authenticated `PUT /api/v1/tasks/{id}/delegation-result`, and only while that task is `in_progress` or `blocked` (409 otherwise). Both answer 404 — not the file's usual 400 — to a non-owner or non-delegated task, deliberately, so these newer routes do not reveal task existence the way the rest of the task API does.
- **Reports are idempotent, and resumptions are recorded.** A closed task (`completed`/`error`/`cancelled`/`archived`) refuses new reports (409). An identical retry (same fields, same session) is not reapplied — it returns the already-stored result, so a lost acknowledgement is harmless. A `done`/`failed` report following an earlier `blocked` one is recorded as a `blocked → in_progress → <status>` step, matching "someone answered in chat and the agent resumed" rather than being rejected as an invalid jump.
- **A blocked question is answered once.** `POST /api/v1/tasks/{id}/delegation-reply` addresses one exact result by ID and resumes the linked session. A reply to a stale, non-blocked, or already differently-answered result is refused (409); a failed send is rolled back so the owner can retry, and a reply whose acknowledgement is lost mid-flight is reported uncertain rather than resent automatically. Once delivered, the stored report itself keeps `status: "blocked"` exactly as the agent sent it — only `reply_state` becomes `delivered` — while the task moves to `in_progress`. A reader must check both fields: `status="blocked"` with `reply_state="delivered"` is an answered question, not an open one.
- **A structured result outlasts the session.** The ordinary session-completion status sync leaves a delegated task alone whenever its result is `done`, `failed`, or an undelivered `blocked` question — a finished session cannot silently erase a pending ask or an already-reported outcome. Starting a fresh execution on a delegated task clears the previous result.

**Known limitation.** The session-completion status sync only defers for a `done`/`failed` result or an undelivered `blocked` question — **not** for `in_progress`, and not for no report at all. An agent session that finishes normally without ever calling `handover_report` (a stale environment, or ignored instructions) can therefore still let the ordinary session-based status computation move the task straight to `completed` or `error`, with no structured result behind it. This is expected until every agent environment is rebuilt with `handover_report` available; a planned follow-up will stop the session-completion sync from resolving a delegated task at all once its environment advertises the tool.

`handover_report` ships in the environment template
(`backend/app/env-templates/app_core_base/core/server/tools/`), which is baked
into the container image on environment build, not live-mounted. An
environment created before this change does not gain the tool until it is
rebuilt — see [Agent Environment Core](../../agents/agent_environment_core/agent_environment_core.md).

### Incremental Sync (`updated_since`)

`GET /api/v1/tasks/?updated_since=<timestamp>` returns only tasks whose
`updated_at` is strictly newer, ordered `(updated_at asc, id asc)`. It is a
polling cursor for external clients; without the param the list keeps its normal
`created_at desc` order, so the web task list is untouched.

**Task activity counts as a change to the task.** Comments and attachments live
in their own tables, so writing one leaves the task row untouched unless
something bumps it deliberately — and it does. Adding a comment, uploading or
attaching a file, and removing either all bump `input_task.updated_at` in the
same transaction as the write itself. Without that, the sharpest case would be
silent: an agent posting a `blocked` question as a comment, on a task whose
status does not change, would produce no delta row at all, and a client driving
its inbox from the cursor would never surface it.

One limit remains, and it is deliberate:

- **Deletions are not reported.** A deleted task simply stops appearing; there is no tombstone table. A client that needs to notice removals reconciles with a full list on start and periodically

**Page by advancing the cursor, not by `skip`.** The sort key is mutable: a row
already returned that is updated again moves to the tail and pushes an unread row
back into the window `skip` has already consumed. Read a page, take the last row's
`updated_at` and `id` as `updated_since` and `updated_since_id`, then repeat until a page comes back short. This pair preserves unread rows sharing a timestamp across page boundaries. Timestamp-only requests keep their strict-after behavior and can skip such ties; supplying an ID without a timestamp is a 400.

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

### Workspace Assignment Rules

- If the API caller provides `user_workspace_id`, that value is used as-is
- If `user_workspace_id` is omitted (or `null`) and the task ends up with a `selected_agent_id` (either supplied explicitly or auto-resolved from a team lead), the task inherits the agent's `user_workspace_id`
- If neither is provided, the task falls back to `NULL` (default workspace)
- Agent-initiated task creation (`create_task_from_agent`) is unaffected — it continues to inherit workspace from the source session

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

- **Agent Handover**: Source agent uses `mcp__agent_task__create_task` to create tasks for direct handover or inbox — see [Agent Handover](../../agents/agent_handover/agent_handover.md). This is a live, in-platform handoff between two sessions; the [Cross-Agent Delegation Contract](#cross-agent-delegation-contract) is a separate, session-independent mechanism for handoffs that are not a single in-platform tool call
- **Agentic Teams**: Team-scoped tasks use the team's `task_prefix`; subtask delegation follows team topology — see [Agentic Teams](../../agents/agentic_teams/agentic_teams.md)
- **Sessions**: Task execution creates sessions with `source_task_id` backlink; session lifecycle events drive automatic status updates — see [Agent Sessions](../agent_sessions/agent_sessions.md)
- **Agent Environment Core**: Seven MCP tools (`mcp__agent_task__*`) let agents interact with tasks from inside environments — six for the live in-platform task flow (`create_task`, `create_subtask`, `add_comment`, `update_status`, `get_details`, `list_tasks`) plus `handover_report` for the cross-agent delegation contract. `get_details` automatically uploads task files to the agent workspace. `add_comment` validates attached file paths locally before sending — see [Agent Environment Core](../../agents/agent_environment_core/agent_environment_core.md), [Agent Task Tools](../../agents/agent_environment_core/create_agent_task_tool.md) and [Session State Tools](../../agents/agent_environment_core/session_state_tools.md)
- **Task Triggers**: Automated rules (CRON, webhook, date) that fire task execution; gains short-codes automatically — see [Task Triggers](task_triggers.md)
- **Activities**: Session state events generate activities for user notification — see [Agent Activities](../agent_activities/agent_activities.md)
- ~~**Email Integration**~~: Incoming email can no longer create tasks — the email-originated task flow (and the "Send Answer" AI reply) was removed when email became a [Server Channel](../server_channels/server_channels.md) (Phase 4 of the channels & identity unification); see [Email Integration — Capabilities removed](../email_integration/email_integration.md#capabilities-removed-in-this-refactor)
- **File Management**: Task attachments use the same storage infrastructure as agent file management — see [Agent File Management](../../agents/agent_file_management/agent_file_management.md)
- **Real-time Events**: `TASK_COMMENT_ADDED`, `TASK_STATUS_CHANGED`, `TASK_ATTACHMENT_ADDED`, `SUBTASK_COMPLETED` events notify the frontend — see [Real-time Events](../realtime_events/event_bus_system.md)
- **Native Clients (Cinna Desktop)**: A signed-in desktop client mirrors tasks it executes locally through the ordinary user-scoped task API — `external_ref` and `external_executor` on create, `POST /{id}/status` to report progress, the paired sync cursor to poll, and authenticated owner-room events to trigger refreshes. It authenticates with an ordinary user token from the desktop OAuth flow (see [Desktop Auth](../desktop_auth/desktop_auth.md)); no task-specific token type exists, and these routes are *not* part of the `/api/v1/external/` surface used for agent discovery and chat — see [External Agent Access](../external_agent_access/external_agent_access.md)

## Changelog

- 2026-09-11: External execution ownership prevents duplicate cinna runs; marker-aware web controls and owner events expose the execution source. Incremental paging now accepts a task ID alongside the timestamp, and completed tasks may restart through the validated status API.
- 2026-09-19: Added the cross-agent delegation contract — `delegation_metadata` on create, the `handover_report` tool and its owner/environment report routes, the delegation reply route, and a status-sync guard that lets a structured `done`/`failed`/`blocked` result outlive session completion.
