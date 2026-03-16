# Agent Handover

## Purpose

Allows conversational agents to delegate work to other agents (direct handover) or create tasks for user review (inbox task creation). Users configure handover targets through a UI with AI-assisted prompt generation; at runtime the agent decides when and how to invoke the handover.

## Core Concepts

### AgentHandoverConfig

Database record linking a source agent to a target agent with a handover prompt. Stores `enabled` flag so handovers can be paused without deletion.

### Handover Prompt

Compact 2-3 sentence natural language instruction (stored in `handover_prompt`) that defines:
- **Trigger condition** — when the handover should happen
- **Context to pass** — what data to include in the task message
- **Message format** — how the handover message should look

Kept compact because it is injected as a tool description into the agent's LLM context; shorter descriptions reduce token usage and improve comprehension.

### Direct Handover Mode

Agent specifies a `target_agent_id` → backend creates an `InputTask` with `auto_execute=true`, optionally auto-refines the message using the target agent's `refiner_prompt`, creates a new session for the target agent, and sends the (possibly refined) message automatically.

### Inbox Task Mode

Agent creates a task without specifying a target → backend creates an `InputTask` with `auto_execute=false`. The task lands in the user's inbox. The user manually selects an agent, optionally refines the message, and executes when ready.

### Enabled / Disabled

Handovers can be toggled on/off. Disabled handovers are excluded from the agent's tool configuration without losing the prompt configuration.

## User Stories / Flows

### 1. Configure a Handover

1. User opens the **Configuration** tab of the source agent
2. Clicks **Add Agent Handover** — dropdown shows available agents (filters out self and already-configured targets)
3. Selects a target agent and clicks **Add Handover** — an empty config record is created
4. Clicks **Generate** (sparkles icon) — AI analyzes both agents' prompts and produces a draft handover prompt
5. User reviews and edits the draft in the textarea
6. Clicks **Apply Prompt** to save
7. Toggle switch enables/disables the handover; trash icon removes it

### 2. Runtime — Direct Handover

1. Source agent is in conversation mode; user sends a message
2. System prompt includes all enabled handovers as tool descriptions
3. Agent detects the trigger condition and calls `mcp__task__create_agent_task` with the task message and `target_agent_id`
4. Tool calls `POST /api/v1/agents/tasks/create` on the backend with `source_session_id`
5. Backend creates `InputTask` (`agent_initiated=true`, `auto_execute=true`)
6. If target agent has a `refiner_prompt`, task message is auto-refined via AI
7. New session is created for the target agent linked to the task; refined message is sent
8. Backend logs a system message in the source session with `task_id` and `session_id`
9. User sees the system message with a clickable **View session** link

### 3. Runtime — Inbox Task

1. Agent calls `mcp__task__create_agent_task` without a `target_agent_id`
2. Tool calls `POST /api/v1/agents/tasks/create` on the backend
3. Backend creates `InputTask` (`agent_initiated=true`, `auto_execute=false`); no session is created
4. Backend logs a system message in the source session with `task_id` and `inbox_task=true`
5. User sees the system message with a clickable **View task** link and processes it manually

### 4. Cloned Agent Behavior

- When an agent is shared/cloned, handover configs are **not copied** to the clone
- The clone starts with an empty handover configuration
- When the parent owner pushes updates to clones, handover configs are **not synced** (workspace files only)
- Clone owners configure their own handover targets against agents they own

## Business Rules

- **No self-handover** — source and target must be different agents
- **Ownership required** — user must own both the source and target agents
- **No duplicates** — cannot create multiple handovers to the same target
- **Cascade delete** — deleting the source agent removes all its handover configs
- **Enabled-only injection** — only enabled handover configs are included in the agent's tool and system prompt at runtime
- **Clone isolation** — handover configuration is never propagated between clones or from parent to clone on push updates

## Architecture Overview

```
User → Agent Configuration UI → Backend API (handover CRUD)
                                     │
                                     ↓
                                AgentService.sync_agent_handover_config()
                                     │
                                     ↓
                            DockerAdapter → Agent-Env POST /config/agent-handovers
                                                (stored: docs/agent_handover_config.json)
                                                     │
                             ┌───────────────────────┘
                             ↓
Agent (conversation mode) uses create_agent_task tool
     │
     └──→ POST /api/v1/agents/tasks/create
               │
               ├── Direct Handover → InputTaskService.create_task_with_auto_refine()
               │                   → InputTaskService.execute_task()
               │                   → SessionService (creates session, sends message)
               │
               └── Inbox Task → InputTaskService.create_task()
                                (no session created, user processes manually)
```

## Integration Points

- **Agent Environment Core** — `create_agent_task` MCP tool registered in conversation mode; handles prompt injection of available handovers. See [create_agent_task tool](../agent_environment_core/create_agent_task_tool.md)
- **Input Tasks** — handover creates `InputTask` with `agent_initiated=true`; inbox tasks use `auto_execute=false`. See [Input Tasks](../../application/input_tasks/input_tasks.md)
- **Agent Sessions** — direct handover creates a new session for the target agent linked to the source task. See [Agent Environment Core](../agent_environment_core/agent_environment_core.md)
- **Agent Sharing** — clones start with empty handover configs; push updates from the parent never overwrite clone handover configuration. See [Agent Sharing](../agent_sharing/agent_sharing.md)
- **AI Functions** — prompt generation and task refinement use `AIFunctionsService`. See [AI Functions development guide](../../development/backend/ai_functions_development.md)
- **Agent Activities** — Sessions created by direct handovers generate activities (running/completed/error) that notify the target agent's owner via the sidebar bell indicator. See [Agent Activities](../../application/agent_activities/agent_activities.md)

