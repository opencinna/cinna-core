# Agent Prompts

## Purpose

System prompt construction for agent environments. Each agent environment operates in one of two modes - building or conversation - and receives a tailored system prompt assembled from static templates and dynamic workspace files. The prompt system also manages bidirectional sync of three user-editable prompt documents (workflow, entrypoint, refiner) between the backend database and the agent environment filesystem.

## Core Concepts

- **Building Mode Prompt** - Development-focused system prompt that combines the Claude Code preset with building agent instructions and workspace context. Used when users are creating scripts, configuring integrations, and developing workflows
- **Conversation Mode Prompt** - Execution-focused system prompt built from the workflow prompt and available scripts/credentials. Used when running pre-built workflows and interacting with end users
- **WORKFLOW_PROMPT.md** - System prompt for conversation mode. Describes the agent's role, script execution steps, data presentation guidelines, and decision logic
- **ENTRYPOINT_PROMPT.md** - Short, human-like trigger message (1-2 sentences) that initiates workflow execution. Used for scheduled/automated runs as the first user message
- **REFINER_PROMPT.md** - Instructions for the task refiner to transform vague user requests into detailed task descriptions. Defines default values, mandatory fields, and enhancement guidelines
- **BUILDING_AGENT.md** - Static template defining the building agent's role, workspace structure, and development guidelines. Copied from version-controlled template during environment initialization
- **scripts/README.md** - Dynamic catalog of existing scripts maintained by the building agent. Auto-loaded into prompts so agents know what scripts already exist
- **credentials/README.md** - Redacted documentation of credentials shared with the agent. Shows structure but hides sensitive values
- **knowledge/** - Integration-specific documentation organized by topic. Only folder names are included in prompts; agents read files on-demand
- **Trigger Prompt** (`Agent.router_trigger_prompt`) - A short, capability-verb-focused sentence that the App MCP router uses to decide when to pick this agent for an incoming message (e.g., "Plans meetings and books events in my calendar"). Not part of the agent's conversation or building system prompt — used only by the router classifier. Editable via the "Trigger Prompt" button in the Agent Prompts card; a "Generate" button calls `POST /agents/{id}/generate-router-trigger-prompt` (AI-generated from the agent's name and description using `gemini-2.5-flash-lite`). Snapshotted into `AgentBundleRevision.router_trigger_prompt` at publish time

## User Stories / Flows

### Building a New Workflow

1. User creates an agent and opens a building mode session
2. Environment initializes with BUILDING_AGENT.md copied from template
3. System prompt assembles: Claude Code preset + BUILDING_AGENT.md + any existing workspace docs
4. User describes desired workflow to the building agent
5. Building agent creates scripts in `./scripts/`, updates `scripts/README.md` immediately
6. Building agent writes WORKFLOW_PROMPT.md with execution and presentation instructions
7. Building agent writes ENTRYPOINT_PROMPT.md with a natural trigger message
8. Building agent writes REFINER_PROMPT.md with task refinement guidelines
9. On session completion, prompts auto-sync from environment to backend Agent model

### Executing a Workflow (Conversation Mode)

1. User (or scheduler) sends a message to a conversation mode session
2. System prompt assembles: WORKFLOW_PROMPT.md + scripts/README.md + credentials/README.md + knowledge topics + environment context + session context + task context (if session is linked to a task) + handover instructions (if any handovers are configured)
3. Conversation agent executes scripts, parses outputs (JSON, CSV), rephrases results in natural language
4. Agent communicates results to user conversationally

### Editing Prompts via UI

1. User edits workflow_prompt, entrypoint_prompt, or refiner_prompt in the frontend
2. Backend updates Agent model in database
3. User clicks "Sync to Environment" to push changes to the active environment
4. Environment files (docs/*.md) are overwritten with new content

### Prompt Sync After Building Session

1. Building session stream completes
2. `stream_completed` event fires
3. Backend reads prompt files from environment via adapter
4. Agent model fields updated: `workflow_prompt`, `entrypoint_prompt`, `refiner_prompt`
5. If workflow_prompt changed, A2A skills are regenerated

## Business Rules

### Prompt Design Principles

- **Systematic Building Process** - Building agent follows ordered steps: analyze requirements, check credentials, plan script architecture, generate scripts, update scripts README, update workflow prompt, define entrypoint, define refiner prompt
- **Single-Purpose Scripts** - Each script handles exactly one operation. Enables composability, progress tracking, debugging, and reuse
- **Human-Like Entrypoints** - ENTRYPOINT_PROMPT.md must be conversational (e.g., "What is my time-off balance?"), not technical (e.g., "Query Odoo API and return JSON")
- **Mandatory Documentation Updates** - scripts/README.md must be updated immediately after every script creation/modification. Failure means future sessions lose script awareness
- **Conversation Agent as Bridge** - The conversation agent executes scripts, parses outputs, rephrases results in natural language, and communicates with users. It is not just a script runner

### Three-Part Prompt Structure

1. **User's Building Request** (to building agent) - "I want an agent that checks my time-off balance"
2. **ENTRYPOINT_PROMPT.md** (user trigger) - "What is my time-off balance?"
3. **WORKFLOW_PROMPT.md** (agent execution) - Run script, parse JSON, rephrase for user

### Data Passing Between Scripts

- **Small data** (IDs, counts) - Use command-line arguments
- **Large data** (lists, records) - Use CSV/JSON files in `workspace/files/` folder
- Producer scripts output files; consumer scripts read them
- File formats must be documented in scripts/README.md and WORKFLOW_PROMPT.md

### Workspace File Organization

- `./scripts/` - All Python scripts — **bundle-owned**, snapshotted at publish, replaced on update
- `./docs/` - Human documentation (Markdown reports, summaries, workflow prompts) — **bundle-owned**
- `./knowledge/` - Integration docs and API guides — **bundle-owned**
- `./files/` - Static assets shipped with the bundle — **bundle-owned**; for runtime data use `./app-data/storage/`
- `./app-data/` - **Per-user persistent App Data** — never overwritten by bundle updates; agents SHOULD write all runtime state here:
  - `./app-data/storage/` — structured data (databases, JSON, CSVs)
  - `./app-data/uploads/` — user-provided files at runtime
  - `./app-data/cache/` — cached downloads and processed output
- All packages installed via `uv`

**Persistence rules for bundle agents**:
- Conversation-mode runs SHOULD only write to `/tmp` or `./app-data/`. Writing to bundle-owned folders during conversation mode will be lost on the next update.
- Building-mode runs MAY write anywhere. The publisher's working install is what gets snapshotted on publish.

### Credential Security

- Never read `credentials.json` directly during building mode conversations
- Only access credentials programmatically within scripts
- Review `credentials/README.md` to see available credentials
- Ask users to share missing credentials before proceeding

### Model Selection

- **Building Mode** - Uses Sonnet (default) for superior code generation
- **Conversation Mode** - Uses Haiku for speed and cost efficiency
- Model selection is automatic based on session mode

### Prompt Sync Rules

- **Environment to Backend** - Automatic after building session completion. Updates Agent model fields
- **Backend to Environment** - Manual, triggered by user via sync endpoint. Requires active running environment
- Workflow prompt changes trigger A2A skills regeneration and optional description update
- **Publish snapshot** - At publish time (`POST /agents/{id}/publish`), the four prompt fields (`workflow_prompt`, `entrypoint_prompt`, `refiner_prompt`, `router_trigger_prompt`) are copied from the `Agent` row into `AgentBundleRevision` and into the revision's `manifest.json` (`prompts.router_trigger`). On `apply_update`, all four fields are synced back onto the install's `Agent` row and the auto-managed `AppAgentRoute.trigger_prompt` is refreshed

### Trigger Prompt Scope

The `router_trigger_prompt` field is agent-level metadata — it is not injected into building or conversation mode system prompts. Its only consumer is the App MCP router (`AIFunctionsService.route_to_agent`). The field is owned by the agent owner and is editable by any authenticated owner (publisher install or foreign install) via the focused `PATCH /agents/{id}/router-trigger-prompt` endpoint, which bypasses the `require_developer` gate so `agent-user` accounts can refine their install's trigger prompt without needing a role upgrade. Saving also propagates the new value to the install's auto-managed `AppAgentRoute` immediately via `AppAgentRouteService.sync_router_trigger_prompt_from_agent`

## Architecture Overview

```
Building Mode:
  Claude Code Preset
    + BUILDING_AGENT.md (static template)
    + scripts/README.md (dynamic)
    + docs/WORKFLOW_PROMPT.md (dynamic)
    + docs/ENTRYPOINT_PROMPT.md (dynamic)
    + docs/REFINER_PROMPT.md (dynamic)
    + credentials/README.md (dynamic)
    + knowledge/ topic names (dynamic)
    → SystemPromptPreset dict (preset: "claude_code", append: combined docs)

Conversation Mode:
  docs/WORKFLOW_PROMPT.md (main prompt)
    + scripts/README.md (available tools)
    + credentials/README.md (available credentials)
    + knowledge/ topic names
    + environment context
    + session context (integration type, session ID, sender, etc.)
    + task context (short_code, title, priority, team, delegation — if session has a linked task)
    + handover instructions (from agent_handover_config.json — if any handovers configured)
    → Plain string prompt

Sync Flow:
  Building Session Completes → stream_completed event
    → EnvironmentService reads prompts from env
    → Updates Agent model (workflow_prompt, entrypoint_prompt, refiner_prompt)
    → Triggers A2A skills regeneration if workflow_prompt changed

  User Edits in UI → PUT /agents/{id}
    → Agent model updated in DB
    → POST /agents/{id}/sync-prompts
    → EnvironmentService writes prompts to env files
```

## Integration Points

- **[Agent Sessions](../../application/agent_sessions/agent_sessions.md)** - Session mode determines which prompt assembly path is used
- **[Agent Environments](../agent_environments/agent_environments.md)** - Prompts live as files inside Docker containers; environment lifecycle initializes prompt templates
- **[Agent Environment Core](../agent_environment_core/agent_environment_core.md)** - PromptGenerator and SDK Manager run inside the container, assembling and applying prompts at runtime
- **[Agent Credentials](../agent_credentials/agent_credentials.md)** - credentials/README.md is loaded into prompts for credential awareness
- **[Input Tasks](../../application/input_tasks/input_tasks.md)** - REFINER_PROMPT.md guides task refinement before execution; ENTRYPOINT_PROMPT.md serves as trigger for scheduled tasks
- **[Agent Schedulers](../agent_schedulers/agent_schedulers.md)** - ENTRYPOINT_PROMPT.md is the default trigger message for scheduler executions when no custom schedule prompt is configured
- **[A2A Protocol](../../application/a2a_integration/a2a_protocol/a2a_protocol.md)** - Workflow prompt changes trigger A2A skills regeneration
- **[Multi SDK](../agent_environment_core/multi_sdk.md)** - SDK adapter selection determines model and prompt format per mode
- **[Knowledge Management](../../application/knowledge_sources/knowledge_sources.md)** - Knowledge topic folders listed in prompts; agents read files on-demand
- **[Agent Handover](../agent_handover/agent_handover.md)** - The consolidated `handover_prompt` from `{workspace}/docs/agent_handover_config.json` is appended at the end of conversation mode prompts so agents know when and how to delegate work
- **[App MCP Server](../../application/app_mcp_server/app_mcp_server.md)** - `router_trigger_prompt` is the only prompt field consumed by the App MCP router; it is snapshotted into the bundle revision and propagated to the auto-managed `AppAgentRoute` at install time and on apply-update
- **[Agent Bundles](../agent_bundles/agent_bundles.md)** - `router_trigger_prompt` is snapshotted into `AgentBundleRevision` at publish and drives auto-route creation at install time