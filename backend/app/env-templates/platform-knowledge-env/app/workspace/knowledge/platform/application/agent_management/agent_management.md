# Agent Management

## Purpose

An **Agent** is the logical definition layer of the platform — a persistent configuration artifact that describes what an AI assistant does, how it runs, and how external systems can reach it. Every environment, session, integration, and automation flows from the agent definition. Creating and configuring an agent is the entry point for all platform capabilities.

## Core Concepts

- **Agent** — a named, owned entity with prompts, SDK selection, credentials, and integration settings; workspace-scoped
- **Active Environment** — the environment the agent currently routes sessions to (`active_environment_id`); can be swapped for blue-green deployment
- **Agent Config** — the union of the core agent record plus its linked sub-entities (schedules, handover configs, plugins, email settings, MCP connectors)
- **Install** — a user's running copy of a published bundle; seeded from the latest bundle revision and linked to a persistent per-user App Data volume

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
- `agent_sdk_building` / `agent_sdk_conversation` — selected AI provider per mode (format: `engine/provider`, e.g., `claude-code/anthropic`, `opencode/openai`); immutable after creation
- Three SDK engines: **Claude Code** (Anthropic; MiniMax temporarily disabled in the UI — not currently supported), **OpenCode** (75+ providers: Anthropic, OpenAI, Google, Bedrock, Azure, Ollama), **Google ADK** (OpenAI-compatible, Vertex)
- `model_override_building` / `model_override_conversation` — optional per-mode model override (e.g., `gpt-4o`, `claude-opus-4`); overrides the adapter's default when set
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

### Webhooks
Per-agent HTTP webhook endpoints let external systems trigger the agent on demand. Each webhook is owner-only (not visible to shared or guest users) and carries its own bearer token. Two trigger types: session (starts a new agent session seeded with the incoming payload) and script (runs a shell command in the agent's Docker environment). Managed via the **Integrations tab > Webhooks card**.

See [Agent Webhooks](../../agents/agent_webhooks/agent_webhooks.md)

### Agent Status (Integrations tab card)
The **Integrations tab > Agent status card** (owner/developer only; hidden from `agent-user` role) shows the current self-reported status snapshot (severity, summary, reported/fetched timestamps) and lets the owner configure a **status refresh command** — a shell command or `/run:<name>` CLI reference that runs inside the container immediately before every manual or forced status refresh. Default value is `/run:status`. Failures are non-blocking and surface as a transient warning banner.

See [Agent Status Tracking](../../agents/agent_status_tracking/agent_status_tracking.md)

### Bundles & Installs
An agent developer can **publish** their agent as a versioned bundle. Other users **install** the bundle, each getting their own running copy plus a persistent per-user App Data area. The publisher can push updates to all installs; users choose manual or automatic update mode. Guest tokens continue to provide time-limited unauthenticated access to a user's install.

**Uninstalling:** The header kebab on any foreign install's detail page offers an **Uninstall** option (available regardless of user role). A confirmation dialog explains that the install and environment are removed but per-bundle App Data is preserved and reattaches on reinstall. The action calls `POST /agents/{id}/uninstall` via `InstallsService.uninstallInstall`. Publisher installs cannot be uninstalled this way (backend returns 400).

See [Agent Bundles & Installs](../../agents/agent_bundles/agent_bundles.md) · [Agent App Data](../../agents/agent_app_data/agent_app_data.md) · [Guest Sharing](../../agents/guest_sharing/guest_sharing.md)

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
  ├── Webhooks ───────────────────────→ Trigger sessions/scripts from external HTTP calls
  ├── Handover Configs ───────────────→ Delegate tasks to other agents
  │
  ├── Email Integration ──────────────→ Receive emails → create sessions/tasks
  ├── A2A Config (skills) ────────────→ Expose agent to external A2A clients
  ├── MCP Connectors ─────────────────→ Expose agent as MCP tool server
  │
  └── Bundles & Installs ──────────────→ Published to catalog; other users install
```

## Agent Detail Page — Tab Layout by Role

The agent detail page (`/agent/$agentId`) shows different tabs and read-only states depending on the user's role and whether the agent is a foreign install:

### Agent Developer (own agent or publisher install)
All tabs visible: **Configuration** (editable), **Integrations**, **Credentials**, **Plugins**, **Environments**, **Interface**, **Bundle**. Default landing tab: **Configuration**.

### Agent Developer (foreign install)
Tabs: **Configuration** (read-only), **Integrations**, **Credentials**, **Plugins**, **Environments**, **Interface**. No Bundle tab. Default landing tab: **Configuration**. The Configuration tab renders read-only because `configReadOnly = !!agent.bundle_uuid && !agent.is_publisher_install` — edit modals disable inputs and hide Save.

### Agent User (any install)
Tabs: **Configuration** (read-only, Information + Agent Prompts only), **Credentials**, **Environments**. Default landing tab: **Configuration**. The Configuration tab hides the Schedules + Handovers row via `showOperationalSettings={false}` (`AgentConfigTab` prop).

### Read-only Configuration tab
Foreign installs render the Configuration tab read-only. The tab uses `AgentConfigTab` with `readOnly={true}`, which passes `readOnly` into each edit modal — Description, Entrypoint prompt, Workflow prompt, Refiner prompt, and Example Prompts modals disable their inputs and hide the Save button.

## Agents List Page — Card Presentation

The `/agents` route renders one `AgentCard` per agent. Each card communicates the agent's purpose through a content area below the agent name:

- **Capability badges** — when the agent has at least one active integration, a row of labelled icon badges replaces the entrypoint-prompt preview. Badges appear for every enabled integration:

  | Badge label | Source flag | Lucide icon |
  |-------------|-------------|-------------|
  | Bundle | `bundle_uuid` set **and** `is_publisher_install` (bundle publisher install) | Package |
  | API | `agent_api_enabled` | Network |
  | Web App | `webapp_enabled` | Globe |
  | Email | `has_email_integration` | Mail |
  | MCP | `has_mcp_connectors` | Unplug |
  | Webhooks | `has_webhooks` | Webhook |
  | GIT | `git_versioning_enabled` | GitBranch |
  | A2A | `a2a_config.enabled` | Waypoints |

- **Entrypoint-prompt preview** — when no integration is active, a monospace block shows the first four lines of `entrypoint_prompt` (if set). This is the fallback; the badge row takes precedence whenever any badge would appear.

- **Colored card border** — a purely visual identification aid layered on top of the same flags used for the Bundle and API badges: the card gets a **green** border if the agent is the bundle publisher install (`bundle_uuid` set and `is_publisher_install`), otherwise a **blue** border if `agent_api_enabled` is set, otherwise no special border. Green takes priority over blue when both conditions apply. No data model or API change — computed client-side in `AgentCard`.

- **Card ordering** — the list is ordered deterministically by **creation date ascending (newest agents last)**, with the agent `id` as a stable tiebreaker. This is enforced server-side in `AgentService.list_agents` via `order_by(Agent.created_at, Agent.id)`; without an explicit `ORDER BY` Postgres returns rows in an unstable order, which made the cards appear to shuffle between refetches.

The four computed capability flags (`has_email_integration`, `has_mcp_connectors`, `has_webhooks`, `git_versioning_enabled`) are derived server-side and carried on `AgentPublic` (batched in `compute_capability_flags`). They reflect only *actively enabled* integrations — `AgentEmailIntegration.enabled`, `MCPConnector.is_active`, `AgentWebhook.enabled`, and presence of an `AgentGitSource` row respectively. The other three flags (`agent_api_enabled`, `webapp_enabled`, `a2a_config.enabled`) are pre-existing fields on the agent record.

See [Agent Status Tracking — Tech](../../agents/agent_status_tracking/agent_status_tracking_tech.md) for the `AgentPublic` model changes and `compute_capability_flags` service method that backs this feature.

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
| [Agent Bundles & Installs](../../agents/agent_bundles/agent_bundles.md) | Agent developers publish agents as versioned bundles from the Bundle tab; the agent record IS the install record |
| [Guest Sharing](../../agents/guest_sharing/guest_sharing.md) | Guest Share Links card on Integrations tab — owner creates disposable URLs for unauthenticated chat access |
| [User Roles](../../application/user_roles/user_roles.md) | Agent create/update/delete and building-mode sessions require `agent-developer` role |
