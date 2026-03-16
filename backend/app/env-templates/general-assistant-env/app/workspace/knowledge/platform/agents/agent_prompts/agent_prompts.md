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
2. System prompt assembles: WORKFLOW_PROMPT.md + scripts/README.md + credentials/README.md + knowledge topics
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

- `./scripts/` - All Python scripts
- `./files/` - Machine-format files (JSON, CSV, binaries, intermediate data)
- `./docs/` - Human documentation (Markdown reports, summaries, workflow prompts)
- All packages installed via `uv`

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