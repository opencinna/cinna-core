# Agent Management

## Purpose

An **Agent** is the logical definition layer of the platform — a persistent configuration artifact that describes what an AI assistant does, how it runs, and how external systems can reach it. Every environment, session, integration, and automation flows from the agent definition. Creating and configuring an agent is the entry point for all platform capabilities.

## Core Concepts

- **Agent** — a named, owned entity with prompts, SDK selection, credentials, and integration settings; workspace-scoped
- **Active Environment** — the environment the agent currently routes sessions to (`active_environment_id`); can be swapped for blue-green deployment
- **Agent Config** — the union of the core agent record plus its linked sub-entities (schedules, handover configs, plugins, email settings, MCP connectors)
- **Clone** — a copy of an agent shared with another user; clones sync from the parent agent based on `update_mode` (automatic or manual)

## Agent Configuration Areas

The agent entity and its directly attached sub-entities represent the full configuration surface. Each area is managed separately and documented in detail:

### Identity & Lifecycle
- `name`, `description` — display identity and discovery
- `is_active` — soft-deactivation toggle
- `ui_color_preset`, `show_on_dashboard`, `conversation_mode_ui` — UI presentation preferences
- `inactivity_period_limit` — auto-suspension policy for the agent's environments (None / 2 days / 1 week / 1 month / always_on); see [Agent Environments](../../agents/agent_environments/agent_environments.md)

### Prompts
Three agent-level prompt fields drive all session behavior:
- `workflow_prompt` — the agent's primary execution instructions
- `entrypoint_prompt` — the trigger message sent at session start
- `refiner_prompt` — guidelines for AI-assisted task refinement

See [Agent Prompts](../../agents/agent_prompts/agent_prompts.md)

### SDK & AI Provider
- `agent_sdk_building` / `agent_sdk_conversation` — selected AI provider and model per mode; immutable after creation
- `agent_sdk_config` — stores discovered tools (`sdk_tools`) and user-approved tools (`allowed_tools`) for automatic permission granting

See [Multi-SDK](../../agents/agent_environment_core/multi_sdk.md) · [Tools Approval](../../agents/agent_environment_core/tools_approval_management.md)

### Credentials
Agents are linked to service credentials (email accounts, APIs, databases, OAuth) via a many-to-many association. Linked credentials are synced into the agent's environments at session start with field-level whitelisting and automatic OAuth token refresh.

See [Agent Credentials](../../agents/agent_credentials/agent_credentials.md)

### AI Credentials
Each environment mode (building / conversation) links to a named AI credential (LLM API key). A default credential is used unless an explicit override is set per mode.

See [AI Credentials](../../application/ai_credentials/ai_credentials.md)

### Plugins
Agents install marketplace plugins via `AgentPluginLink` records, with independent enable/disable flags per mode (conversation / building) and version pinning.

See [Agent Plugins](../../agents/agent_plugins/agent_plugins.md)

### Schedulers
Multiple `AgentSchedule` records can be attached to an agent, each with a CRON expression, optional custom prompt, and independent enable/disable state. Schedules trigger new sessions automatically.

See [Agent Schedulers](../../agents/agent_schedulers/agent_schedulers.md)

### Handover Configuration
`AgentHandoverConfig` records define delegation targets — other agents this agent can route tasks to, with a natural-language trigger condition (`handover_prompt`). Excluded from clones by default.

See [Agent Handover](../../agents/agent_handover/agent_handover.md) · [Input Tasks](../../application/input_tasks/input_tasks.md)

### Email Integration
Per-agent email settings (`AgentEmailIntegration`) configure IMAP/SMTP mailbox binding, sender access rules, processing mode (new session vs. new task), and isolation mode (shared vs. per-sender clone).

See [Email Integration](../../application/email_integration/email_integration.md)

### A2A Protocol
`a2a_config` stores auto-extracted skills (derived from `workflow_prompt`) and an enabled flag. Skills are regenerated whenever the workflow prompt changes and are exposed publicly via the A2A agent card.

See [A2A Protocol](../../application/a2a_integration/a2a_protocol/a2a_protocol.md) · [A2A Access Tokens](../../application/a2a_integration/a2a_access_tokens/a2a_access_tokens.md)

### MCP Connectors
Agents can be exposed as remote MCP tool servers via named connectors, each with mode, access control list, and max-client limit. `example_prompts` (stored on the agent) are surfaced as MCP slash commands.

See [MCP Integration](../../application/mcp_integration/agent_mcp_architecture.md)

### Sharing & Cloning
An agent can be shared with other users as a read-only clone ("user" mode) or an editable clone ("builder" mode). Guest tokens provide time-limited unauthenticated access. Update propagation from parent to clone follows the `update_mode` setting.

See [Agent Sharing](../../agents/agent_sharing/agent_sharing.md)

## Architecture Overview

```
Agent (config entity)
  ├── Prompts ──────────────────────────→ Agent Environment (runtime)
  │                                              │
  ├── SDK selection ───────────────────→         └──→ Session (conversation)
  ├── Credentials (linked) ──────────→ Synced into container at session start
  ├── AI Credentials (per mode) ─────→ Bound per environment mode
  ├── Plugins (per mode) ────────────→ Loaded into container
  │
  ├── Schedulers ─────────────────────→ Trigger sessions automatically (CRON)
  ├── Handover Configs ───────────────→ Delegate tasks to other agents
  │
  ├── Email Integration ──────────────→ Receive emails → create sessions/tasks
  ├── A2A Config (skills) ────────────→ Expose agent to external A2A clients
  ├── MCP Connectors ─────────────────→ Expose agent as MCP tool server
  │
  └── Sharing (clones / guests) ──────→ Other users / unauthenticated access
```

## Agent Creation Wizard

The entry point for all agent management is the **New Agent Creation Wizard** — a multi-step SSE-streaming flow that creates the agent, spins up its first environment, optionally links credentials, and opens the first session in one go.

See [New Agent Creation Wizard](./new_agent_wizard.md)

## Integration Points

| Feature | How it connects to agent config |
|---------|--------------------------------|
| [Agent Environments](../../agents/agent_environments/agent_environments.md) | Agent owns environments; `active_environment_id` selects the active one |
| [Agent Sessions](../../application/agent_sessions/agent_sessions.md) | Sessions are created against the agent's active environment |
| [Agent Activities](../../application/agent_activities/agent_activities.md) | Activity feed is scoped to the agent's sessions and tasks |
| [User Workspaces](../../application/user_workspaces/user_workspaces.md) | Agents are isolated per workspace via `user_workspace_id` |
| [Knowledge Sources](../../application/knowledge_sources/knowledge_sources.md) | Knowledge retrieval is available to agents via a tool injected in the environment |
| [Input Tasks](../../application/input_tasks/input_tasks.md) | Agents create and receive tasks; `refiner_prompt` drives task refinement |
