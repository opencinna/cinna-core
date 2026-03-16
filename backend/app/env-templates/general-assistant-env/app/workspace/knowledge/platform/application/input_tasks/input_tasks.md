# Input Tasks

## Purpose

Enable users to receive, refine, and execute incoming tasks through an AI-assisted preparation workflow. Tasks transform vague or incomplete requests into detailed, agent-ready instructions before execution, and can be created manually or by other agents.

## Core Concepts

- **Input Task**: A user-facing request container that goes through refinement before agent execution. Holds original message, current description, and refinement history.
- **Task Status**: Lifecycle state from creation through execution to archival.
- **Refinement**: AI-assisted process to improve a task description using the target agent's workflow prompt as context.
- **Refinement History**: Append-only log of user comments and AI responses during refinement.
- **Task Execution**: Creating a session linked to the task and sending the refined description as the initial message.
- **Agent-Initiated Task**: Task created by an agent via `create_agent_task` tool, either for direct handover or user inbox.
- **Auto-Execute**: Flag on agent-created tasks that triggers immediate execution without user review.
- **Source Session**: The agent session that created a task via handover; used for feedback delivery.
- **Auto-Feedback**: Flag controlling whether target agent state updates are automatically forwarded to the source agent.
- **Session Result State**: Agent-declared outcome stored on a session (`completed`, `needs_input`, `error`).
- **Todo Progress**: Real-time task completion progress from agent's TodoWrite tool calls.

## User Stories / Flows

### Flow 1: User-Initiated Task

1. User creates a task with an initial message description
2. User opens the task refinement page (split view)
3. Left panel: description editor, agent selector, execute button, sessions list
4. Right panel: AI chat interface for refinement
5. User sends refinement comments → AI refines description and provides feedback
6. User continues refining or clicks Execute
7. System creates a session linked to the task via `source_task_id`
8. User can execute same task multiple times (creates additional sessions)
9. Task status syncs automatically with session state
10. User archives completed tasks

### Flow 2: Agent-Initiated Direct Handover

1. Source agent calls `create_agent_task` with a target agent specified
2. System creates task with `agent_initiated=true`, `auto_execute=true`
3. If target agent has `refiner_prompt`, message is auto-refined
4. System auto-creates session and sends the (possibly refined) message
5. Task appears in Tasks list with `agent_initiated` flag
6. Task status syncs with session state

### Flow 3: Agent-Initiated Inbox Task

1. Source agent calls `create_agent_task` without a target agent
2. System creates task with `agent_initiated=true`, `auto_execute=false`
3. Task appears in user's inbox with status `NEW`
4. User reviews task, optionally refines description
5. User selects appropriate agent
6. User executes task when ready
7. Task status syncs with session state

### Flow 4: Session State Reporting and Bi-Directional Feedback

1. Target agent finishes → calls `update_session_state(state, summary)`
2. Session's `result_state` and `result_summary` are updated
3. Real-time event notifies frontend (`SESSION_STATE_UPDATED`)
4. Activity created for offline notification
5. If task has `auto_feedback=true` → feedback message sent to source session
6. Source agent receives feedback → can auto-respond or escalate to user
7. Source agent replies via `respond_to_task(task_id, message)` → resets session state, resumes target agent

## Business Rules

### Task Status Lifecycle

| Status | Description | Allowed Actions |
|--------|-------------|-----------------|
| `new` | Created, not yet refined | Refine, Execute, Archive, Delete |
| `refining` | User actively refining | Continue, Execute |
| `running` | Session active | Monitor |
| `pending_input` | Agent waiting for user input | Navigate to session |
| `completed` | Agent finished successfully | Archive, Run Again |
| `error` | Agent encountered error | Retry, Archive |
| `archived` | Archived by user | — |

### Session-to-Task Status Sync

When sessions are linked to a task via `source_task_id`, task status updates automatically:

- If ANY session has status `error` → task = `ERROR`
- If ANY session has unanswered tool questions → task = `PENDING_INPUT`
- If ANY session is actively streaming → task = `RUNNING`
- If ALL sessions are completed → task = `COMPLETED`
- Otherwise → task = `RUNNING`

Status sync only applies to tasks in execution phase (`running`, `pending_input`, `completed`, `error`). Tasks in `new`, `refining`, or `archived` status are protected from override.

### Session Deletion Reset

When a session linked to a task is deleted:

- If ALL sessions are removed and task is in execution phase → task resets to `NEW`, clears session links and timestamps
- If some sessions remain → task status recomputed from remaining sessions
- Tasks in `new`, `refining`, or `archived` status are not affected

### Auto-Feedback Rules

- `auto_feedback` flag copied from `AgentHandoverConfig` to task at creation
- When `auto_feedback=true` and target agent calls `update_session_state`:
  - Message created in source session with prefix `[Sub-task completed]`, `[Sub-task needs input]`, or `[Sub-task error]`
  - If source session is idle → agent processing triggered automatically
  - If source session is streaming → message stays pending until stream ends
- `feedback_delivered` flag prevents duplicate delivery

### Refinement Rules

- `original_message` is immutable after creation (audit trail)
- `refinement_history` is append-only
- Refinement uses the target agent's `workflow_prompt` as AI context
- Last 5 history items are passed to the AI refiner for context

### Execution Rules

- Task must have a selected agent to execute
- Agent must have an active environment to execute
- Archived tasks cannot be re-executed without first becoming active

### Todo Progress Tracking

- When agent calls TodoWrite tool during execution, progress is captured
- Stored on both Session and InputTask for persistence and display
- Real-time updates via `TASK_TODO_UPDATED` event

## Architecture Overview

```
User → Frontend → Backend API → InputTaskService → SessionService
                      │
                      ├── Refine: AIFunctionsService → TaskRefiner (LLM)
                      └── Execute: SessionService.create_session(source_task_id)

Task (1) ──────────────────────────────────────> (N) Session
        source_task_id (authoritative FK)

Bi-Directional Feedback:
Source Session ──create_agent_task──> Task ──execute──> Target Session
      ↑                                                       │
      └──── deliver_feedback_to_source ◄── update_session_state
```

## Integration Points

- **Agent Handover**: Source agent calls `create_agent_task` tool to create tasks for direct handover or inbox — see [Agent Handover](../../agents/agent_handover/agent_handover.md)
- **Sessions**: Task execution creates sessions with `source_task_id` backlink — see [Agent Sessions](../agent_sessions/agent_sessions.md)
- **Task Triggers**: Automated rules (CRON, webhook, date) that fire task execution — see [Task Triggers](task_triggers.md)
- **Activities**: Session state events generate activities for user notification — see [Agent Activities](../agent_activities/agent_activities.md)
- **Email Integration**: Incoming emails can create tasks automatically — see [Email Integration](../email_integration/email_integration.md)
- **Agent Environment Core**: `update_session_state` and `respond_to_task` agent-env tools — see [Agent Environment Core](../../agents/agent_environment_core/agent_environment_core.md)
