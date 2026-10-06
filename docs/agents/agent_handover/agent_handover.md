---
feature: agent_handover
domain: agents
one_liner: "Lets a conversational agent delegate work to another agent directly, or queue a task for itself that a person runs later, using AI-assisted prompt generation."
docs:
  tech: agent_handover_tech.md
---
# Agent Handover

## Purpose

Allows conversational agents to delegate work to other agents (direct handover) or queue tasks for itself that a person runs later (own queue). Users configure handover targets through a UI with AI-assisted prompt generation; at runtime the agent decides when and how to invoke the handover.

## Core Concepts

### AgentHandoverConfig

Database record linking a source agent to a target agent with a handover prompt. Stores `enabled` flag so handovers can be paused without deletion.

### Handover Prompt

Compact 2-3 sentence natural language **trigger** (stored in `handover_prompt`) that defines:
- **Trigger condition** — when the handover should happen, in this conversation
- **Content to pass** — what the handed-over task description must contain, with an example

It names no tool: how to delegate is the platform's business (the `cinna-tasks` skill and the `cinna` server's `tasks_targets` / `tasks_create`), and differs between environments. At runtime the agent's prompt gets a **Configured handovers** section built from the synced `handovers` list — one line per enabled pair with the target name and its `tasks_targets` target string (`agent:<uuid>`), the pair's trigger verbatim beneath, and one sentence saying to hand over with `tasks_create` and the target as `assignee`. `PromptGenerator._load_handover_prompt()` renders it from `{workspace}/docs/agent_handover_config.json` (so a config written by an older backend still yields current text) and appends it after the task context block.

**Environments built before the task protocol** (`task_protocol_version` 0) have only the retired `agent_task` tools, so the backend syncs them the pre-protocol prompt instead (`AgentService.build_legacy_handover_prompt`, verbatim the old text naming `create_agent_task`), and their conversations still receive the `[Sub-task …]` feedback lines.

### Direct Handover Mode

Agent calls `tasks_create` with an `assignee` (a `tasks_targets` string, or a unique name) → backend creates an `InputTask` with `agent_initiated=true`, `auto_execute=true`, optionally auto-refines the message using the target agent's `refiner_prompt`, creates a new session for the target agent, and sends the (possibly refined) message automatically.

### Own Queue Mode

Agent calls `tasks_create` without an `assignee` → backend creates an `InputTask` assigned to the calling agent (on a team subtask, its own node) with `auto_execute=false`, status `new` or `open`; no session starts. A person runs it from the UI, and the report rule applies to that run.

### Enabled / Disabled

Handovers can be toggled on/off. Disabled handovers are excluded from the agent's tool configuration without losing the prompt configuration.

## User Stories / Flows

### 1. Configure a Handover

1. User opens the **Configuration** tab of the source agent. The **Handover to Agents** card shows up to 5 compact rows (target agent, an Enabled/Off badge, a one-line prompt preview), with a **Show all (N)** link opening a side Sheet when there are more
2. Clicks **Add handover** — an agent picker dialog shows available agents (filters out self and already-configured targets)
3. Selects a target agent — the picker closes and an empty config record is created; the prompt-edit dialog opens immediately on the new handover so it is never left half-configured
4. In the prompt dialog, clicks **Generate with AI** (sparkles icon) — AI analyzes both agents' prompts and writes a draft into the textarea without saving it
5. User reviews and edits the draft
6. Clicks **Save** to persist the prompt
7. Back on the card (or the Show-all Sheet), each row's **⋯** menu offers **Edit prompt** (reopens the same dialog), **Enable**/**Disable** (auto-saves immediately, no confirmation), and **Delete handover** (behind an `AlertDialog` confirm naming the target agent)

### 2. Runtime — Direct Handover

1. Source agent is in conversation mode; user sends a message
2. System prompt includes the **Configured handovers** section (target string and trigger per enabled pair)
3. Agent detects the trigger condition and calls `tasks_create` with `title`, `description`, `assignee` (the target string) and an `id` of its choosing; a repeated call with the same `id` returns the same task and starts nothing
4. Tool calls `POST /api/v1/agent/tasks/protocol/create` on the backend with `source_session_id`
5. Backend creates `InputTask` (`agent_initiated=true`, `auto_execute=true`, `source_session_id` set)
6. If target agent has a `refiner_prompt`, task message is auto-refined via AI
7. New session is created for the target agent linked to the task; refined message is sent. The target learns that a report is required from its context block (`report required: yes`) and the `cinna-tasks` skill
8. Backend logs a system message in the source session with `task_id` and `session_id`
9. User sees the system message with a clickable **View session** link
10. The source agent ends its turn. When the target's session settles, the result arrives in the source conversation as a message beginning `Task result from <agent> on <ref>:` (status, summary, body, artifacts, question) — or, for a source environment at protocol 0, as the legacy `[Sub-task completed] …` line. A `blocked` question addressed to the requester is answered with `tasks_reply`

### 3. Runtime — Own Queue

1. Agent calls `tasks_create` without an `assignee`
2. Tool calls `POST /api/v1/agent/tasks/protocol/create` on the backend
3. Backend creates `InputTask` assigned to the calling agent (`agent_initiated=true`, `auto_execute=false`); no session is created
4. Backend logs a system message "Task <code> added to <agent>'s queue" in the source session with `task_id` and `inbox_task=true`
5. User sees the system message with a clickable **View task** link and runs the task when ready

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
- **Enabled-only injection** — only enabled handover configs are included in the agent's Configured handovers prompt section at runtime
- **Configured pairs are not a whitelist** — `tasks_create` accepts any target `tasks_targets` lists (the owner's other active agents, or the current task's team nodes); a configured pair only adds a trigger to the prompt
- **Report rule** — on an environment at task protocol 1 or later, a handed-over task whose session ends without `tasks_report` is left blocked with "Ended without reporting" instead of auto-completing. See [Input Tasks — Report rule](../../application/input_tasks/input_tasks.md#report-rule-task-protocol-v1)
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
Agent (conversation mode) calls tasks_create on the `cinna` server
     │
     └──→ POST /api/v1/agent/tasks/protocol/create
               │
               ├── Direct Handover → InputTaskService.create_task_with_auto_refine()
               │                   → InputTaskService.execute_task()
               │                   → SessionService (creates session, sends message)
               │
               └── Own queue → task assigned to the caller, not started
                                (no session created, user processes manually)
```

## Integration Points

- **Agent Environment Core** — the `cinna` MCP server's `tasks_targets` / `tasks_create` / `tasks_reply` in conversation mode, and the Configured handovers prompt section. See [Cinna Task Tools](../agent_environment_core/cinna_tasks_tools.md)
- **Input Tasks** — handover creates `InputTask` with `agent_initiated=true`; own-queue tasks use `auto_execute=false`. Both kinds count toward the `tasks_create` chain limits (depth 2, 20 per conversation). See [Input Tasks](../../application/input_tasks/input_tasks.md)
- **Cross-Agent Delegation Contract** — a separate, session-independent mechanism on Input Tasks for handoffs where the requester is not a live cinna session in this conversation (a different agent instance, another automation, or a client relaying on an agent's behalf): the task carries `delegation_metadata` from creation, and the executing agent reports back with `tasks_report` instead of a chat reply. Unlike this feature's configured pairs, it does not go through `AgentHandoverConfig`. See [Input Tasks — Cross-Agent Delegation Contract](../../application/input_tasks/input_tasks.md#cross-agent-delegation-contract) and [Cinna Task Tools](../agent_environment_core/cinna_tasks_tools.md#tasks_report)
- **Agent Sessions** — direct handover creates a new session for the target agent linked to the source task. See [Agent Environment Core](../agent_environment_core/agent_environment_core.md)
- **Agent Bundles & Installs** — installed agents start with empty handover configs (not included in bundle revision snapshots); apply-update never overwrites handover configuration. See [Agent Bundles & Installs](../agent_bundles/agent_bundles.md)
- **AI Functions** — prompt generation and task refinement use `AIFunctionsService`. See [AI Functions development guide](../../development/backend/ai_functions_development.md)
- **Agent Activities** — Sessions created by direct handovers generate activities (running/completed/error) that notify the target agent's owner via the sidebar bell indicator. See [Agent Activities](../../application/agent_activities/agent_activities.md)
- **Agentic Teams** — A handover config and a team connection between the same two agents are kept in sync in both directions (create/update/delete on either side propagates to the other; adding an agent to a team backfills connections from any pre-existing handover configs), so configuring a handover once is visible from both the agent's own Configuration tab and the team canvas. See [Agentic Teams](../agentic_teams/agentic_teams.md)

