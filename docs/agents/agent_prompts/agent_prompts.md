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
3. Backend runs the three-way reconcile for each of the three bidirectional prompt files
4. For each file the reconcile compares the DB content, the env file content, and the per-environment baseline hash to decide the action (NOOP, PULL, PUSH, CONFLICT with LWW tiebreak)
5. Any DB-side change (`PULL`, `CONFLICT_PULL`, `SEED_PULL`) updates the Agent model and bumps the per-prompt `*_updated_at` clock; if `workflow_prompt` changed, A2A skills are regenerated and `AGENT_UPDATED` is emitted to the owner so the UI refreshes

## Business Rules

### Prompt Design Principles

- **Systematic Building Process** - Building agent follows ordered steps: analyze requirements, check credentials, plan script architecture, generate scripts, update scripts README, update workflow prompt, define entrypoint, define refiner prompt
- **Single-Purpose Scripts** - Each script handles exactly one operation. Enables composability, progress tracking, debugging, and reuse
- **Human-Like Entrypoints** - ENTRYPOINT_PROMPT.md must be conversational (e.g., "What is my time-off balance?"), not technical (e.g., "Query Odoo API and return JSON")
- **Mandatory Documentation Updates** - scripts/README.md must be updated immediately after every script creation/modification. Failure means future sessions lose script awareness
- **Conversation Agent as Bridge** - The conversation agent executes scripts, parses outputs, rephrases results in natural language, and communicates with users. It is not just a script runner

### Personal Memory Injection

The agent can maintain a private per-install memory area at `./app-data/memory/`. Its `*.md` files are injected into system prompts as a `## Personalization / User Memory` block in both building and conversation modes.

- **Location**: `./app-data/memory/` inside the App Data volume; canonical file is `MEMORY.md`. Additional `*.md` files are supported
- **Who can write**: Any sender can induce a write — there is no owner gating. The agent writes with its native file tools when the user asks it to remember a preference
- **Scope**: Personalization and small personal facts only (how to address the user, a default option, preferred tone). Workflow logic, scripts, and process steps belong in `WORKFLOW_PROMPT.md` / `scripts/`
- **Injection**: `app-data/memory/*.md` files are read fresh on each request, sorted by filename (case-insensitive), labeled with `### <filename>` sub-headers, and accumulated under a 20,000-character cap (≈5,000 tokens). A single file that exceeds the cap is included as a truncated slice rather than dropped. An empty or missing memory area is a true no-op — zero prompt tokens added. Never raises; any I/O error logs a warning and yields no injection
- **Privacy**: The memory area is excluded from bundle snapshots and git automatically (because `app-data/` is already in `BUNDLE_EXCLUDED_TOPLEVEL`). It is not a synced file and never round-trips to the DB — the inverse of `STATUS.md`, which is pull-cached
- **Persistence**: Lives in the App Data volume, so it survives `apply_update`, environment rebuild, uninstall, and reinstall
- **Rollout caveat**: `prompt_generator.py` is baked into the agent Docker image. The memory reader takes effect only after an environment **image rebuild and recreate** (same as the `conversation_style` / Communication Style block). Once the code is present, memory content propagates live — read fresh per request

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
  - `./app-data/memory/` — personal per-install memory files (`*.md`); auto-injected into system prompts; private to this install, never versioned
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

The three bidirectional prompt documents (`WORKFLOW_PROMPT.md`, `ENTRYPOINT_PROMPT.md`, `REFINER_PROMPT.md`) use a **three-way reconcile with LWW tiebreak** — not a blind one-directional push. This means edits made in either place (UI or the container via cinna-cli) are preserved and never silently clobbered.

**Reconcile model:**
- A per-environment baseline hash (`AgentEnvironment.*_synced_hash`) records the last-reconciled common ancestor for each prompt
- On each reconcile pass, the DB content hash and env file content hash are compared against the baseline
- If only one side changed since the baseline, that side wins (PULL or PUSH) — no conflict
- If both sides changed (genuine divergence), the LWW tiebreaker uses the per-prompt `Agent.*_updated_at` clock (DB side) versus the file mtime clamped for clock skew (env side). A tie favours the env (the direction that was being lost before this feature)
- If content is identical on both sides, it is a NOOP — only the baseline is healed to the current hash
- A `None` baseline means "never reconciled" — DB wins on first sync (`SEED_PUSH`) unless DB is empty and env has content (`SEED_PULL`)
- Conflicts are logged as WARNINGs with both hashes and the winner for observability

**Reconcile triggers** (all three prompts, including `refiner_prompt` which was previously omitted on the push path):
- **Every environment start, activation, and rebuild** — `_sync_dynamic_data` calls `reconcile_agent_prompts` (replaces old blind `set_agent_prompts`)
- **New container first setup** — `reconcile_agent_prompts(prefer="db")` seeds DB content and initialises baselines; subsequent reconciles are full three-way
- **Building session completes** — `STREAM_COMPLETED` event triggers `handle_stream_completed_event` → `reconcile_agent_prompts`
- **Workspace file change** — env-core watcher fires `WORKSPACE_FILES_CHANGED` (e.g. after a Mutagen sync from cinna-cli) → `handle_workspace_files_changed_event` → `reconcile_agent_prompts`
- **UI edit followed by force-push** — `POST /agents/{id}/sync-prompts` is an **intentional force-push** (DB always wins, baselines are reset); use this as an explicit override when you know the DB version should replace the env file

**Live UI refresh:** when the reconcile writes to the DB (`PULL`, `CONFLICT_PULL`, `SEED_PULL`), the backend emits `AGENT_UPDATED` to the owner. The agent detail page subscribes and invalidates `["agent", agentId]` / `["agents"]` so the Prompts cards in `AgentConfigTab` re-render with the pulled content.

**Workflow prompt changes** (regardless of the reconcile direction) trigger A2A skills regeneration and optional description update — unchanged from before.

**Publish snapshot:** at publish time (`POST /agents/{id}/publish`), the four prompt fields (`workflow_prompt`, `entrypoint_prompt`, `refiner_prompt`, `router_trigger_prompt`) are copied from the `Agent` row into `AgentBundleRevision` and into the revision's `manifest.json` (`prompts.router_trigger`). On `apply_update`, all four fields are written onto the install's `Agent` row, per-prompt `*_updated_at` clocks are bumped (DB authoritative), and any synced-hash baselines are cleared so the next reconcile treats the revision content as the current anchor (see "apply_update baseline reset" in [Agent Bundles](../agent_bundles/agent_bundles.md)). The auto-managed `AppAgentRoute.trigger_prompt` is also refreshed.

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
    + environment context (documents ./app-data/memory/ location)
    + session context (if present)
    + ## Personalization / User Memory block (app-data/memory/*.md — if non-empty)
    → SystemPromptPreset dict (preset: "claude_code", append: combined docs)

Conversation Mode:
  docs/WORKFLOW_PROMPT.md (main prompt)
    + scripts/README.md (available tools)
    + credentials/README.md (available credentials)
    + knowledge/ topic names
    + environment context (documents ./app-data/memory/ location)
    + session context (integration type, session ID, sender, etc.)
    + task context (short_code, title, priority, team, delegation — if session has a linked task)
    + handover instructions (from agent_handover_config.json — if any handovers configured)
    + Communication Style (from credentials.json current_user — if set)
    + ## Personalization / User Memory block (app-data/memory/*.md — if non-empty)
    → Plain string prompt

Sync Flow (three-way reconcile + LWW):
  Any Trigger → reconcile_agent_prompts(session, environment, agent, prefer?)
    → adapter.get_agent_prompts() → {content, mtimes}
    → for each of {workflow, entrypoint, refiner}:
        base_hash = environment.<field>_synced_hash   (common ancestor)
        db_hash   = content_hash(agent.<field>)
        env_hash  = content_hash(env_file)
        decide() → NOOP | PULL | PUSH | SEED_* | CONFLICT_* (LWW)
    → apply DB writes (PULL/CONFLICT_PULL/SEED_PULL)
        → bump agent.<field>_updated_at
        → if workflow_prompt: regen A2A skills + emit AGENT_UPDATED
    → apply env writes (PUSH/CONFLICT_PUSH/SEED_PUSH)
    → update environment.<field>_synced_hash (new baseline)
    → commit

  Triggers:
    Building Session Completes → STREAM_COMPLETED → handle_stream_completed_event
    Workspace file changed (Mutagen/cli) → WORKSPACE_FILES_CHANGED → handle_workspace_files_changed_event
    Env start/activate/rebuild → _sync_dynamic_data → reconcile_agent_prompts
    New container first setup → reconcile_agent_prompts(prefer="db")  [DB wins, baselines init]
    Bundle apply-update → bump *_updated_at, clear baselines, reconcile(prefer="db")

  Force-push (escape hatch):
    POST /agents/{id}/sync-prompts → DB always wins, baselines reset
    PUT /agents/{id} (UI edit) → agent updated, then reconcile (or force-push, DB wins)
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
- **[Agent Bundles](../agent_bundles/agent_bundles.md)** - `router_trigger_prompt` is snapshotted into `AgentBundleRevision` at publish; `apply_update` overwrites all four prompt fields from the revision, bumps per-prompt `*_updated_at` clocks, and clears `*_synced_hash` baselines so the reconcile treats the revision content as the new anchor rather than pulling stale env files back
- **[cinna-cli Integration](../../application/cinna_cli_integration/cinna_cli_integration.md)** - bidirectional prompt sync is the path that cinna-cli edits flow through: Mutagen→env-core watcher→`WORKSPACE_FILES_CHANGED`→`reconcile_agent_prompts`; no CLI-side change is required by this feature
- **[App Sync](../../application/app_sync/app_sync.md)** - the reconcile mirrors App Sync's `content_fingerprint` no-op short-circuit and LWW pattern; same conceptual model applied to prompt docs
- **[Realtime Events](../../application/realtime_events/event_bus_system.md)** - the `WORKSPACE_FILES_CHANGED` event is the shared trigger for all five synced workspace files; `AGENT_UPDATED` is emitted by `reconcile_agent_prompts` on every env→DB prompt pull