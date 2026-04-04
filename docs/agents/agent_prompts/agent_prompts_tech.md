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

- `backend/app/services/environments/environment_service.py` - `EnvironmentService`: bidirectional prompt sync between backend DB and agent environments
- `backend/app/services/agents/agent_service.py` - `AgentService`: handles workflow prompt changes, triggers A2A skills regeneration

### Backend - Routes

- `backend/app/api/routes/agents.py` - Agent CRUD and prompt sync endpoint
- `backend/app/api/routes/utils.py` - Prompt refinement AI utility endpoint

### Backend - Models

- `backend/app/models/agents/agent.py` - Agent model with prompt fields

### Frontend - Components

- `frontend/src/components/Agents/EditWorkflowPromptModal.tsx` - Modal for editing workflow_prompt
- `frontend/src/components/Agents/EditEntrypointPromptModal.tsx` - Modal for editing entrypoint_prompt
- `frontend/src/components/Agents/EditRefinerPromptModal.tsx` - Modal for editing refiner_prompt

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

## API Endpoints

### Backend API (FastAPI)

- `backend/app/api/routes/agents.py`
  - `PUT /api/v1/agents/{id}` - Update agent including prompt fields. Calls `AgentService.update_agent()` which triggers `handle_workflow_prompt_change()` on workflow_prompt changes
  - `POST /api/v1/agents/{id}/sync-prompts` - Push prompt fields from backend DB to active environment files. Calls `EnvironmentService.sync_agent_prompts_to_environment()`

- `backend/app/api/routes/utils.py`
  - `POST /api/v1/utils/refine-prompt/` - AI-powered prompt refinement using Gemini. Input: `RefinePromptRequest`, Output: `RefinePromptResponse`

### Agent Environment API (inside Docker)

- `backend/app/env-templates/app_core_base/core/server/routes.py`
  - `GET /config/agent-prompts` - Read WORKFLOW_PROMPT.md, ENTRYPOINT_PROMPT.md, REFINER_PROMPT.md from workspace
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
- `_get_environment_context()` - Builds environment context metadata section
- `build_session_context_section(session_context)` - Builds session metadata section from session context dict
- `generate_building_mode_prompt(session_context)` - Assembles full building mode prompt. Returns `SystemPromptPreset` dict: `{"type": "preset", "preset": "claude_code", "append": combined_docs}`
- `generate_conversation_mode_prompt(session_context)` - Assembles conversation mode prompt. Returns plain string. Assembly order: WORKFLOW_PROMPT.md → scripts/README.md → credentials/README.md → knowledge topics → environment context → session context section → task context section (if session has a linked task) → handover prompt (from `{workspace}/docs/agent_handover_config.json`, if present)
- `generate_prompt(mode, session_state)` - Factory method routing to building or conversation prompt generator

### SDKManager (`sdk_manager.py`)

- `__init__()` - Initializes adapter registry, reads `SDK_ADAPTER_BUILDING` and `SDK_ADAPTER_CONVERSATION` env vars
- `_get_adapter(mode)` - Gets or creates SDK adapter for specified mode (e.g., "claude-code/anthropic", "opencode/openai")
- Delegates prompt generation and model selection to the adapter, which uses PromptGenerator internally

### AgentEnvService (`agent_env_service.py`)

- `__init__(workspace_dir)` - Initializes with workspace directory path
- `get_agent_prompts()` - Reads WORKFLOW_PROMPT.md, ENTRYPOINT_PROMPT.md, REFINER_PROMPT.md. Returns tuple of three strings
- `update_agent_prompts(workflow_prompt, entrypoint_prompt, refiner_prompt)` - Writes prompt content to docs/ directory files
- `_read_prompt_file(filename)` - Reads a single prompt file from docs/ directory
- `_write_prompt_file(filename, content)` - Writes content to a single prompt file in docs/ directory

### EnvironmentService (`environment_service.py`)

- `sync_agent_prompts_from_environment(session, environment, agent)` - Reads prompts from environment via adapter's `get_agent_prompts()`. Updates Agent model fields. Calls `AgentService.handle_workflow_prompt_change()` if workflow_prompt changed
- `sync_agent_prompts_to_environment(environment, workflow_prompt, entrypoint_prompt, refiner_prompt)` - Pushes prompts from backend to environment via adapter's `set_agent_prompts()`. Requires active running environment
- `handle_stream_completed_event(event_data)` - Event handler for building session completion. Auto-triggers `sync_agent_prompts_from_environment()` for building mode sessions only

### AgentService (`agent_service.py`)

- `handle_workflow_prompt_change(agent, new_workflow_prompt, trigger_description_update)` - Unified handler for workflow_prompt changes. Regenerates A2A skills. Optionally triggers background description generation
- `update_agent()` - General agent update. Calls `handle_workflow_prompt_change()` when workflow_prompt field is modified

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

## Configuration

### Environment Variables (inside Docker)

- `SDK_ADAPTER_BUILDING` - SDK adapter identifier for building mode (e.g., "claude-code/anthropic")
- `SDK_ADAPTER_CONVERSATION` - SDK adapter identifier for conversation mode (e.g., "claude-code/anthropic")

### Prompt Assembly Behavior

- Building mode: BUILDING_AGENT.md is cached at PromptGenerator initialization; all other files loaded fresh per request
- Conversation mode: All files loaded fresh per request (no caching)
- Empty or missing prompt files are silently skipped during assembly
- Knowledge topics scan ignores hidden directories (starting with `.`)
- Conversation mode appends the handover prompt (from `{workspace}/docs/agent_handover_config.json`) last, after the task context section; silently skipped if the file is absent or the `handover_prompt` field is empty

## Security

- `credentials/README.md` shows redacted values only - sensitive data replaced with `[REDACTED]`
- Building agent instructions explicitly forbid reading `credentials.json` directly in conversation
- Scripts access actual credential values programmatically at runtime
- Prompt sync endpoints require authenticated user with agent ownership
- Agent prompt fields stored as plain text in database (not encrypted - they contain instructions, not secrets)
