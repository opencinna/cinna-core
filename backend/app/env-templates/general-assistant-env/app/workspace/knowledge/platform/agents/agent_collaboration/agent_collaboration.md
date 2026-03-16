# Agent Collaboration

## Purpose

Enables a coordinator agent to dispatch subtasks to multiple agents simultaneously and collect their results — a fan-out / fan-in pattern. Unlike single-target [handover](../agent_handover/agent_handover.md), collaboration runs multiple agents in parallel with a shared findings context, and the coordinator receives auto-feedback as each subtask completes.

## Core Concepts

### AgentCollaboration

Database record representing a coordinator-initiated multi-agent effort. Tracks overall status, shared context (findings), and links to the coordinator agent and source session. One collaboration has many subtasks.

### CollaborationSubtask

One unit of work within a collaboration, assigned to a single target agent. Each subtask becomes an InputTask + Session pair. Tracks individual status, result summary, and ordering.

### Shared Context (Findings)

A JSON structure (`shared_context.findings`) on the collaboration record where any participant agent can post intermediate results via the `post_finding` tool. All participants can read all findings via `get_collaboration_status`, enabling cross-agent information sharing during execution.

### Coordinator Agent

The agent that creates the collaboration. It decides what subtasks to dispatch, to which agents, and with what instructions. The coordinator receives auto-feedback as each subtask completes (via the existing InputTask feedback mechanism).

### Participant Agent

Any target agent assigned a subtask. Participant agents receive collaboration context injected into their system prompt — title, description, their specific task, and names of other participants. They can post findings and check overall status.

## User Stories / Flows

### 1. Coordinator Creates a Collaboration

1. Coordinator agent is in conversation mode; user requests something requiring multi-agent work
2. Agent decides to fan out and calls `create_collaboration` tool with a title, description, and list of subtasks (each specifying a target agent and task message)
3. Tool calls `POST /api/v1/agents/collaborations/create` on the backend
4. Backend validates coordinator ownership, creates `AgentCollaboration` record
5. For each subtask: validates target agent ownership, creates `CollaborationSubtask`, dispatches via `AgentService.create_agent_task()` (auto_execute=true)
6. Each subtask gets its own InputTask and Session; subtask status transitions to "running"
7. Coordinator receives confirmation with collaboration ID and subtask count

### 2. Participant Agent Executes Subtask

1. Target agent's session starts with collaboration context injected into system prompt (title, role, other participants)
2. Agent works on its assigned task using its normal tools and capabilities
3. Agent can call `post_finding` to share intermediate results with other participants
4. Agent can call `get_collaboration_status` to see other subtasks' progress and findings
5. When done, agent calls `update_session_state` with `state="completed"` and a summary

### 3. Subtask Completion Feedback Loop

1. When a subtask agent reports state via `update_session_state`, the auto-feedback hook in InputTaskService detects it
2. Hook calls `AgentCollaborationService.handle_subtask_state_update()` to update subtask status
3. If all subtasks reach terminal states (completed or error), the collaboration itself transitions to "completed" (or "error" if any subtask errored)
4. The existing auto-feedback mechanism delivers the subtask result back to the coordinator's source session

### 4. Coordinator Monitors Progress

1. Coordinator agent can call `get_collaboration_status` at any time to see:
   - Overall collaboration status
   - Per-subtask status, result summaries, and assigned agent names
   - All shared findings posted by participants
2. This allows the coordinator to synthesize results or take follow-up action

## Business Rules

- **Ownership required** — coordinator and all target agents must belong to the same user
- **No self-dispatch** — coordinator should not assign a subtask to itself (validated at agent level, not enforced in DB)
- **At least one subtask** — collaboration creation requires a non-empty subtask list
- **Subtask validation** — each subtask must have a valid `target_agent_id` and non-empty `task_message`; invalid subtasks are skipped (not rejected)
- **Status lifecycle (collaboration)**: `in_progress` → `completed` | `error`
- **Status lifecycle (subtask)**: `pending` → `running` → `completed` | `needs_input` | `error`
- **Auto-completion** — collaboration status transitions automatically when all subtasks reach terminal states
- **Error propagation** — if any subtask errors, the collaboration status becomes "error"
- **Finding attribution** — each finding is prefixed with the posting agent's name for traceability
- **Participant-only findings** — only the coordinator or subtask target agents can post findings
- **Cascade delete** — deleting a collaboration deletes all its subtasks

## Architecture Overview

```
Coordinator Agent (conversation mode)
     │
     ├──→ create_collaboration tool
     │         │
     │         └──→ POST /api/v1/agents/collaborations/create
     │                   │
     │                   ├── AgentCollaborationService.create_collaboration()
     │                   │         │
     │                   │         ├── Creates AgentCollaboration record
     │                   │         └── For each subtask:
     │                   │               ├── Creates CollaborationSubtask
     │                   │               └── AgentService.create_agent_task() (auto_execute=true)
     │                   │                         │
     │                   │                         └── Creates InputTask + Session for target agent
     │                   │
     │                   └── Returns collaboration_id, subtask_count
     │
     ├──→ get_collaboration_status tool  →  GET .../status  →  Full status + findings
     └──→ post_finding tool              →  POST .../findings  →  Append to shared_context

Target Agent (subtask session)
     │
     ├── System prompt includes collaboration context (title, role, other participants)
     ├── Uses post_finding to share results
     ├── Uses get_collaboration_status to see progress
     └── Calls update_session_state(state="completed", summary=...)
              │
              └── InputTaskService auto-feedback hook
                    │
                    ├── AgentCollaborationService.handle_subtask_state_update()
                    │         └── Updates subtask status; checks if all terminal → marks collaboration complete
                    └── Delivers feedback to coordinator's source session
```

## Integration Points

- **Agent Handover** — collaboration builds on the same `create_agent_task` infrastructure used by single-target handover. Collaboration is the multi-agent extension. See [Agent Handover](../agent_handover/agent_handover.md)
- **Input Tasks** — each subtask creates an `InputTask` with `auto_execute=true` and `agent_initiated=true`. The auto-feedback mechanism delivers results back to the coordinator. See [Input Tasks](../../application/input_tasks/input_tasks.md)
- **Agent Environment Core** — three new MCP tools (`create_collaboration`, `post_finding`, `get_collaboration_status`) are registered alongside existing task tools in conversation mode. See [Agent Environment Core](../agent_environment_core/agent_environment_core.md)
- **Agent Prompts** — collaboration context is injected into participant agents' system prompts via `PromptGenerator.build_collaboration_context_section()`. See [Agent Prompts](../agent_prompts/agent_prompts.md)
- **Agent Sessions** — collaboration context is enriched into the session context by `MessageService._build_session_context()`, which calls `AgentCollaborationService.get_collaboration_by_session()`. See [Agent Sessions](../../application/agent_sessions/agent_sessions.md)
