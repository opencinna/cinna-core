# Agent Prompts - Technical Details

## File Locations

### Backend - Agent Environment Core (runs inside Docker)

- `backend/app/env-templates/app_core_base/core/server/prompt_generator.py` - PromptGenerator class: loads prompt files, assembles mode-specific system prompts
- `backend/app/env-templates/app_core_base/core/server/sdk_manager.py` - SDKManager class: coordinates adapter selection, delegates prompt generation to PromptGenerator
- `backend/app/env-templates/app_core_base/core/server/agent_env_service.py` - AgentEnvService class: reads/writes prompt files in workspace (WORKFLOW_PROMPT.md, ENTRYPOINT_PROMPT.md, REFINER_PROMPT.md)
- `backend/app/env-templates/app_core_base/core/server/routes.py` - HTTP endpoints `/chat`, `/chat/stream`, `/config/agent-prompts` (GET/POST)

### Backend - Prompt Templates (version-controlled)

- `backend/app/env-templates/app_core_base/core/prompts/BUILDING_AGENT.md` - Building agent instructions template
- `backend/app/env-templates/python-env-advanced/app/workspace/docs/WORKFLOW_PROMPT.md` - Workflow prompt template
- `backend/app/env-templates/python-env-advanced/app/workspace/docs/ENTRYPOINT_PROMPT.md` - Entrypoint prompt template
- `backend/app/env-templates/python-env-advanced/app/workspace/docs/REFINER_PROMPT.md` - Refiner prompt template
- `backend/app/env-templates/python-env-advanced/app/workspace/scripts/README.md` - Scripts catalog template

### Backend - Services

- `backend/app/services/environments/environment_service.py` - `EnvironmentService`: three-way reconcile + LWW prompt sync between backend DB and agent environments
- `backend/app/services/environments/prompt_sync.py` - **new** pure decision module: `PROMPT_FIELDS`, `normalise`, `content_hash`, `ReconcileAction`, `PULL_ACTIONS`, `PUSH_ACTIONS`, `decide()` — no I/O, fully unit-testable
- `backend/app/services/environments/synced_files.py` - **new** Synced Workspace File Registry: `SyncedFile`, `SYNCED_FILES`, `sync_class` (`"bidirectional"` / `"pull_only"`), `watched_rel_paths()`, `bidirectional_files()`, `pull_only_files()`
- `backend/app/services/environments/adapters/docker_adapter.py` - `DockerAdapter.get_agent_prompts()` now returns a dict with `workflow_prompt`, `entrypoint_prompt`, `refiner_prompt` content keys plus a `"mtimes"` sub-dict with the same field names as POSIX float mtimes (or `None` when env-core predates mtime reporting — backward-compatible)
- `backend/app/services/agents/agent_service.py` - `AgentService`: bumps per-prompt `*_updated_at` on UI edit; handles workflow prompt changes and A2A skills regeneration

### Backend - Routes

- `backend/app/api/routes/agents.py` - Agent CRUD and prompt sync endpoint
- `backend/app/api/routes/utils.py` - Prompt refinement AI utility endpoint

### Backend - Models

- `backend/app/models/agents/agent.py` - Agent model with prompt fields

### Frontend - Components

- `frontend/src/components/Agents/EditWorkflowPromptModal.tsx` - Modal for editing workflow_prompt
- `frontend/src/components/Agents/EditEntrypointPromptModal.tsx` - Modal for editing entrypoint_prompt
- `frontend/src/components/Agents/EditRefinerPromptModal.tsx` - Modal for editing refiner_prompt
- `frontend/src/components/Agents/EditRouterTriggerPromptModal.tsx` - Modal for editing and generating `router_trigger_prompt`. Contains a "Generate" button (`Wand2` icon) that calls `POST /agents/{id}/generate-router-trigger-prompt` and pre-fills the textarea. Save calls `PATCH /agents/{id}/router-trigger-prompt`. After save, invalidates `["agents"]`, `["agent", agentId]`, and `["app-mcp-routes", agentId]` so `McpConnectorsCardSimple` reflects the updated trigger prompt immediately. Accessible to all agent owners; the `readOnly` prop renders a read-only view (no Generate or Save buttons)
- `frontend/src/components/Agents/AgentConfigTab.tsx` - Configuration tab layout; renders the "Agent Prompts" card which now includes a **Trigger Prompt** button alongside Entrypoint Prompt, Workflow Prompt, and Refiner Prompt buttons; clicking opens `EditRouterTriggerPromptModal`

### Instance Files (per environment at runtime)

- `{instance_dir}/app/core/prompts/BUILDING_AGENT.md` - Building agent instructions (copied from template)
- `{instance_dir}/app/workspace/docs/WORKFLOW_PROMPT.md` - Workflow prompt (maintained by building agent)
- `{instance_dir}/app/workspace/docs/ENTRYPOINT_PROMPT.md` - Entrypoint prompt (maintained by building agent)
- `{instance_dir}/app/workspace/docs/REFINER_PROMPT.md` - Refiner prompt (maintained by building agent)
- `{instance_dir}/app/workspace/scripts/README.md` - Scripts catalog (maintained by building agent)
- `{instance_dir}/app/workspace/credentials/README.md` - Redacted credentials docs (synced from backend)
- `{instance_dir}/app/workspace/knowledge/` - Integration knowledge base (topic subdirectories)

## Database Schema

### Agent Model

- Table: `agent` (defined in `backend/app/models/agents/agent.py`)
- `workflow_prompt: str | None` - System prompt for conversation mode (WORKFLOW_PROMPT.md content)
- `entrypoint_prompt: str | None` - Trigger message for workflow execution (ENTRYPOINT_PROMPT.md content)
- `refiner_prompt: str | None` - Task refinement instructions (REFINER_PROMPT.md content, Text column)
- `router_trigger_prompt: str | None` - Short natural-language description for the App MCP router (Text column, nullable). NOT injected into building or conversation system prompts. Snapshotted into `AgentBundleRevision.router_trigger_prompt` at publish time
- `workflow_prompt_updated_at: datetime | None` — per-prompt logical clock; bumped whenever `workflow_prompt` changes in the DB (UI edit, env→DB pull, bundle apply-update). `None` treated as "−∞" (oldest) in the LWW tiebreak
- `entrypoint_prompt_updated_at: datetime | None` — same for `entrypoint_prompt`
- `refiner_prompt_updated_at: datetime | None` — same for `refiner_prompt`

### AgentEnvironment Model

The `AgentEnvironment` table (defined in `backend/app/models/environments/environment.py`) gains three new columns for the per-prompt common-ancestor hash (reconcile baseline), scoped per environment so blue-green pairs have independent histories:

- `workflow_prompt_synced_hash: str | None` — SHA-256 of last-reconciled `workflow_prompt` content (VARCHAR(64), nullable)
- `entrypoint_prompt_synced_hash: str | None` — last-reconciled `entrypoint_prompt`
- `refiner_prompt_synced_hash: str | None` — last-reconciled `refiner_prompt`

`None` baseline means "never reconciled" — the next reconcile seeds from DB (or pulls if DB is empty and env has content).

### Migration

`backend/app/alembic/versions/9675dc695735_add_prompt_sync_reconciliation_fields.py` (revision `9675dc695735`, revises `a3c9e1f7b204`) — adds the three `Agent.*_updated_at` columns (`TIMESTAMP WITH TIME ZONE`, nullable) and the three `AgentEnvironment.*_synced_hash` columns (`VARCHAR(64)`, nullable). All columns default to `NULL` so existing rows backfill cleanly. Downgrade drops all six columns.

## API Endpoints

### Backend API (FastAPI)

- `backend/app/api/routes/agents.py`
  - `PUT /api/v1/agents/{id}` - Update agent including prompt fields (`require_developer` gate). Calls `AgentService.update_agent()` which triggers `handle_workflow_prompt_change()` on workflow_prompt changes
  - `POST /api/v1/agents/{id}/sync-prompts` - **Intentional force-push**: DB always wins for all three prompts; baselines are reset to the DB hashes after writing. Does not run the three-way reconcile. Use as an explicit escape hatch when you want DB to override whatever is in the env. Calls `EnvironmentService.sync_agent_prompts_to_environment()`
  - `PATCH /api/v1/agents/{id}/router-trigger-prompt` - Owner-only update of `Agent.router_trigger_prompt`. Accessible to `agent-user` accounts (no `require_developer` gate). Body: `RouterTriggerPromptUpdate {router_trigger_prompt: str | None}`. Also propagates the new value to the auto-managed `AppAgentRoute` via `AppAgentRouteService.sync_router_trigger_prompt_from_agent`. Returns `AgentPublic`
  - `POST /api/v1/agents/{id}/generate-router-trigger-prompt` - AI generator for `router_trigger_prompt`. Sources from `agent.name` + `agent.description`. Returns `GenerateRouterTriggerPromptResponse {success: bool, trigger_prompt: str | None, error: str | None}`. Returns an error (not 4xx) when the agent has no description. Accessible to any agent owner (no developer gate)

- `backend/app/api/routes/utils.py`
  - `POST /api/v1/utils/refine-prompt/` - AI-powered prompt refinement using Gemini. Input: `RefinePromptRequest`, Output: `RefinePromptResponse`

### Agent Environment API (inside Docker)

- `backend/app/env-templates/app_core_base/core/server/routes.py`
  - `GET /config/agent-prompts` - Read WORKFLOW_PROMPT.md, ENTRYPOINT_PROMPT.md, REFINER_PROMPT.md from workspace. Response (`AgentPromptsResponse` in `models.py`) now includes optional per-file POSIX mtimes: `workflow_prompt_mtime`, `entrypoint_prompt_mtime`, `refiner_prompt_mtime` (all `float | None`). Additive and backward-compatible — older backends ignore them; `reconcile_agent_prompts` uses them as the env-side timestamp for the LWW tiebreak after clamping for clock skew
  - `POST /config/agent-prompts` - Write prompt files to workspace docs directory
  - `POST /chat/stream` - Main chat endpoint. Accepts `mode` and `agent_sdk` parameters; SDK Manager delegates prompt assembly to PromptGenerator based on mode

## Services & Key Methods

### PromptGenerator (`prompt_generator.py`)

- `__init__(workspace_dir)` - Initializes with workspace path, loads BUILDING_AGENT.md (cached)
- `_load_building_agent_prompt()` - Reads `/app/core/prompts/BUILDING_AGENT.md`, caches in `self.building_agent_prompt`
- `_load_scripts_readme()` - Reads workspace `scripts/README.md` (fresh on each call)
- `_load_workflow_prompt()` - Reads workspace `WORKFLOW_PROMPT.md` from docs/ (fresh on each call)
- `_load_entrypoint_prompt()` - Reads workspace `ENTRYPOINT_PROMPT.md` from docs/ (fresh on each call)
- `_load_refiner_prompt()` - Reads workspace `REFINER_PROMPT.md` from docs/ (fresh on each call)
- `_load_credentials_readme()` - Reads workspace `credentials/README.md`
- `_get_knowledge_topics()` - Scans workspace `knowledge/` for subdirectory names, returns comma-separated list
- `_load_handover_prompt()` - Reads `handover_prompt` field from workspace `{workspace}/docs/agent_handover_config.json`; returns the consolidated handover instructions string or None if file absent or empty
- `_load_conversation_style()` - Reads `credentials/credentials.json`, locates the `type == "current_user"` entry, and returns `credential_data.conversation_style` as a string, or `None` if the file is absent or the entry is missing. Never raises. Used exclusively by `generate_conversation_mode_prompt`.
- `_get_environment_context()` - Builds environment context metadata section
- `build_session_context_section(session_context)` - Builds session metadata section from session context dict
- `generate_building_mode_prompt(session_context)` - Assembles full building mode prompt. Returns `SystemPromptPreset` dict: `{"type": "preset", "preset": "claude_code", "append": combined_docs}`
- `generate_conversation_mode_prompt(session_context)` - Assembles conversation mode prompt. Returns plain string. Assembly order: WORKFLOW_PROMPT.md → scripts/README.md → credentials/README.md → knowledge topics → environment context → session context section → task context section (if session has a linked task) → handover prompt (from `{workspace}/docs/agent_handover_config.json`, if present) → **Communication Style section** (optional — appended last; present only for `concise_direct` and `friendly_chatty`; `ai_default` appends nothing). The Communication Style section is a single-sentence Markdown block (`## Communication Style`) read from `credentials.json` `current_user.conversation_style`. Note: this file lives in the env-template and is baked into the agent Docker image — the tone sentence takes effect **only after the environment image is rebuilt and the environment is recreated**. The four locale/style fields in `credentials.json` and `credentials/README.md` propagate immediately via normal credential sync without a rebuild.
- `generate_prompt(mode, session_state)` - Factory method routing to building or conversation prompt generator

### SDKManager (`sdk_manager.py`)

- `__init__()` - Initializes adapter registry, reads `SDK_ADAPTER_BUILDING` and `SDK_ADAPTER_CONVERSATION` env vars
- `_get_adapter(mode)` - Gets or creates SDK adapter for specified mode (e.g., "claude-code/anthropic", "opencode/openai")
- Delegates prompt generation and model selection to the adapter, which uses PromptGenerator internally

### `prompt_sync.py` (new — pure decision module)

`backend/app/services/environments/prompt_sync.py`

- `PROMPT_FIELDS` — ordered tuple of `(field_name, filename)` for the three bidirectional prompts
- `normalise(content)` — `.strip()`; empty/whitespace → `None`
- `content_hash(content)` — SHA-256 hex digest of normalised content, or `None` if empty
- `ReconcileAction` — `str` enum: `NOOP`, `PULL`, `PUSH`, `CONFLICT_PULL`, `CONFLICT_PUSH`, `SEED_PUSH`, `SEED_PULL`
- `PULL_ACTIONS` / `PUSH_ACTIONS` — frozensets of the actions that produce a DB-side / env-side write respectively
- `decide(db_content, env_content, base_hash, db_ts, env_ts)` — pure three-way decision; `>=` on env_ts vs db_ts favours env on ties; `None` timestamps treated as `−∞` via `_MIN_TS`

### `synced_files.py` (new — Synced Workspace File Registry)

`backend/app/services/environments/synced_files.py`

- `SyncedFile(key, rel_path, sync_class)` — frozen dataclass; `sync_class` is `"bidirectional"` or `"pull_only"`
- `SYNCED_FILES` — 5 entries: three bidirectional prompt docs + `"cli_commands"` (`docs/CLI_COMMANDS.yaml`) + `"status"` (`app-data/storage/STATUS.md`)
- `watched_rel_paths()` — all `rel_path`s; mirrors env-core `_WATCHED_FILES` (drift is caught by a unit test)
- `bidirectional_files()` / `pull_only_files()` — filtered views

### AgentEnvService (`agent_env_service.py`)

- `get_agent_prompts()` — reads WORKFLOW_PROMPT.md, ENTRYPOINT_PROMPT.md, REFINER_PROMPT.md; returns tuple of three strings
- `get_agent_prompt_mtimes()` — returns `(workflow_mtime, entrypoint_mtime, refiner_mtime)` as `tuple[float | None, ...]`; each value is `Path.stat().st_mtime` or `None` if the file does not exist
- `update_agent_prompts(workflow_prompt, entrypoint_prompt, refiner_prompt)` — writes content to docs/ files
- `_read_prompt_file(filename)` / `_write_prompt_file(filename, content)` — single-file helpers

### EnvironmentService (`environment_service.py`)

- **`reconcile_agent_prompts(session, environment, agent, *, prefer=None)`** — orchestrator for the three-way reconcile. `prefer="db"` forces `SEED_PUSH` for every field (used by first-container setup and `apply_update`). Returns `ReconcileResult(pulled, pushed, conflicts, noops, success)`. Best-effort — never raises into lifecycle paths; on failure logs and returns `success=False`. Emits `AGENT_UPDATED` to the owner for every DB-side change via `_emit_agent_updated`
- `sync_agent_prompts_from_environment(session, environment, agent)` — legacy wrapper; still calls `reconcile_agent_prompts` for backward-compatibility with callers
- `sync_agent_prompts_to_environment(environment, workflow_prompt, entrypoint_prompt, refiner_prompt)` — **force-push** (DB wins, baselines reset); called by `POST /agents/{id}/sync-prompts`
- `handle_stream_completed_event(event_data)` — event handler for building session completion; delegates to `reconcile_agent_prompts`
- `handle_workspace_files_changed_event(event_data)` — event handler for `WORKSPACE_FILES_CHANGED`; delegates to `reconcile_agent_prompts`
- `_emit_agent_updated(environment, agent, changed_fields)` — emits `AGENT_UPDATED` with `{agent_id, environment_id, changed_fields}` meta to the agent owner; best-effort (failures are swallowed)
- `_clamp_env_mtime(raw_mtime)` — clamps a container-reported POSIX mtime to `server_now + skew_bound` to prevent a clock-ahead container from always winning the LWW tiebreak

### AgentService (`agent_service.py`)

- `handle_workflow_prompt_change(agent, new_workflow_prompt, trigger_description_update)` — regenerates A2A skills; optionally triggers background description generation
- `update_agent()` — when a prompt field changes, bumps the matching `agent.<field>_updated_at = now()` before commit; calls `handle_workflow_prompt_change()` when `workflow_prompt` is modified

## Frontend Components

### EditWorkflowPromptModal (`EditWorkflowPromptModal.tsx`)

- Modal dialog for editing the `workflow_prompt` field
- Uses React Hook Form with Zod validation
- Submits via `AgentsService.updateAgent()` mutation
- Invalidates `["agents"]` query on success

### EditEntrypointPromptModal (`EditEntrypointPromptModal.tsx`)

- Modal dialog for editing the `entrypoint_prompt` field
- Same pattern as EditWorkflowPromptModal

### EditRefinerPromptModal (`EditRefinerPromptModal.tsx`)

- Modal dialog for editing the `refiner_prompt` field
- Same pattern as EditWorkflowPromptModal

### Agent Detail Route (`frontend/src/routes/_layout/agent/$agentId.tsx`)

- Subscribes to `EventTypes.AGENT_UPDATED` from `eventService.ts`
- On receipt, invalidates `["agent", agentId]` and `["agents"]` so the Workflow / Entrypoint / Refiner prompt cards in `AgentConfigTab.tsx` re-render with pulled content
- The subscription is scoped to the agent detail page; only fires when `event.meta.agent_id` matches the page's `agentId`
- DB→env pushes and NOOPs do not emit `AGENT_UPDATED`, so the listener does not cause spurious refetches

## Configuration

### Environment Variables (inside Docker)

- `SDK_ADAPTER_BUILDING` - SDK adapter identifier for building mode (e.g., "claude-code/anthropic")
- `SDK_ADAPTER_CONVERSATION` - SDK adapter identifier for conversation mode (e.g., "claude-code/anthropic")

### Prompt Assembly Behavior

- Building mode: BUILDING_AGENT.md is cached at PromptGenerator initialization; all other files loaded fresh per request
- Conversation mode: All files loaded fresh per request (no caching)
- Empty or missing prompt files are silently skipped during assembly
- Knowledge topics scan ignores hidden directories (starting with `.`)
- Conversation mode appends the handover prompt (from `{workspace}/docs/agent_handover_config.json`) last before the Communication Style section; silently skipped if the file is absent or the `handover_prompt` field is empty
- Conversation mode appends the Communication Style section after the handover prompt when `conversation_style` is `concise_direct` or `friendly_chatty`; `ai_default` appends nothing (zero prompt change). This is a single sentence inside a `## Communication Style` Markdown block. Because `prompt_generator.py` is baked into the agent Docker image, this section takes effect only after an environment image rebuild.

## Tests

- `backend/tests/unit/test_prompt_sync.py` — unit tests for `decide()` across the full decision table (NOOP, PULL, PUSH, SEED_PUSH, SEED_PULL, CONFLICT_PULL, CONFLICT_PUSH, empty-file/blank cases)
- `backend/tests/unit/test_synced_files_registry.py` — **drift-guard test**: asserts that env-core `_WATCHED_FILES` equals the `SYNCED_FILES` registry `rel_path`s. Parses `app_core_base/core/main.py` via the AST so the check works without importing the env-core module; fails the suite if the two lists diverge

## Security

- `credentials/README.md` shows redacted values only - sensitive data replaced with `[REDACTED]`
- Building agent instructions explicitly forbid reading `credentials.json` directly in conversation
- Scripts access actual credential values programmatically at runtime
- Prompt sync endpoints require authenticated user with agent ownership
- Agent prompt fields stored as plain text in database (not encrypted - they contain instructions, not secrets)
- Env-side mtime used in the LWW tiebreak is clamped to `server_now + skew_bound` by `_clamp_env_mtime` to prevent a container with a clock far in the future from always winning the conflict resolution
