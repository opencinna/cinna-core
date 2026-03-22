# Agent Collaboration — Technical Details

## File Locations

### Backend — Models
- `backend/app/models/agent_collaboration.py` — `AgentCollaboration`, `CollaborationSubtask` (table models), plus schema models (`AgentCollaborationCreate`, `AgentCollaborationPublic`, `CollaborationSubtaskCreate`, `CollaborationSubtaskPublic`, `PostFindingRequest`, `PostFindingResponse`, `CreateCollaborationRequest`, `CreateCollaborationResponse`)

### Backend — Routes
- `backend/app/api/routes/collaborations.py` — API router with prefix `/agents/collaborations`, tag `collaborations`
- `backend/app/api/main.py` — Router registration

### Backend — Services
- `backend/app/services/agent_collaboration_service.py` — `AgentCollaborationService` (all business logic)

### Backend — Migrations
- `backend/app/alembic/versions/f55c23690563_add_agent_collaboration_tables.py` — Creates `agent_collaboration` and `collaboration_subtask` tables

### Agent Environment — Tools
- `backend/app/env-templates/app_core_base/core/server/tools/create_collaboration.py` — `create_collaboration` MCP tool
- `backend/app/env-templates/app_core_base/core/server/tools/get_collaboration_status.py` — `get_collaboration_status` MCP tool
- `backend/app/env-templates/app_core_base/core/server/tools/post_finding.py` — `post_finding` MCP tool

### Agent Environment — Integration
- `backend/app/env-templates/app_core_base/core/server/adapters/claude_code_sdk_adapter.py` — Registers collaboration tools in the `task` MCP server alongside existing task tools
- `backend/app/env-templates/app_core_base/core/server/prompt_generator.py` — `build_collaboration_context_section()` injects collaboration context into participant system prompts

### Backend — Integration Points
- `backend/app/services/input_task_service.py` — Hook in `update_session_state` flow calls `AgentCollaborationService.handle_subtask_state_update()`
- `backend/app/services/message_service.py` — `_build_session_context()` enriches session context with collaboration fields via `AgentCollaborationService.get_collaboration_by_session()`

## Database Schema

### `agent_collaboration` table
- `id` (UUID, PK)
- `title` (str, 1-500 chars)
- `description` (str, nullable)
- `status` (str, max 50 — `in_progress`, `completed`, `error`)
- `coordinator_agent_id` (UUID, FK → `agent.id`, CASCADE)
- `source_session_id` (UUID, nullable, FK → `session.id`, SET NULL)
- `shared_context` (JSON — stores `{"findings": [...]}`)
- `owner_id` (UUID, FK → `user.id`, CASCADE)
- `created_at`, `updated_at` (datetime)

### `collaboration_subtask` table
- `id` (UUID, PK)
- `collaboration_id` (UUID, FK → `agent_collaboration.id`, CASCADE)
- `target_agent_id` (UUID, FK → `agent.id`, CASCADE)
- `task_message` (str)
- `status` (str, max 50 — `pending`, `running`, `completed`, `needs_input`, `error`)
- `result_summary` (str, nullable)
- `input_task_id` (UUID, nullable, FK → `input_task.id`, SET NULL)
- `session_id` (UUID, nullable, FK → `session.id`, SET NULL)
- `order` (int, default 0)
- `created_at`, `updated_at` (datetime)

### Relationships
- `AgentCollaboration.subtasks` → `CollaborationSubtask` (one-to-many, cascade delete, selectin loading)
- `CollaborationSubtask.collaboration` → `AgentCollaboration` (many-to-one, joined loading)

## API Endpoints

### `POST /api/v1/agents/collaborations/create`
- Auth: Bearer token (agent environment auth)
- Body: `CreateCollaborationRequest` — `title`, `description`, `subtasks` (list of dicts), `source_session_id`
- Response: `CreateCollaborationResponse` — `success`, `collaboration_id`, `subtask_count`, `message`, `error`
- Derives `coordinator_agent_id` from the source session's environment

### `POST /api/v1/agents/collaborations/{collaboration_id}/findings`
- Auth: Bearer token
- Body: `PostFindingRequest` — `finding`, `source_session_id` (optional, for agent attribution)
- Response: `PostFindingResponse` — `success`, `findings` list, `error`
- Resolves posting agent from `source_session_id` → environment → agent; falls back to coordinator

### `GET /api/v1/agents/collaborations/{collaboration_id}/status`
- Auth: Bearer token
- Response: `AgentCollaborationPublic` — full collaboration with subtask details and agent names

### `GET /api/v1/agents/collaborations/by-session/{session_id}`
- Auth: Bearer token
- Response: Dict with collaboration context keys (or empty dict if not a collaboration session)
- Used by prompt generator to inject collaboration context

## Services & Key Methods

### `AgentCollaborationService` (`backend/app/services/agent_collaboration_service.py`)

- `create_collaboration()` — Validates agents, creates records, dispatches subtasks via `AgentService.create_agent_task()`. Async.
- `post_finding()` — Appends attributed finding to `shared_context["findings"]`. Validates agent is participant.
- `get_collaboration_status()` — Returns `AgentCollaborationPublic` with subtask details and resolved agent names.
- `handle_subtask_state_update()` — Called from auto-feedback hook; updates subtask status, checks if all terminal → marks collaboration complete. Returns `(found, collaboration_complete)`.
- `get_collaboration_by_session()` — Looks up collaboration context for a session belonging to a subtask. Returns dict with collaboration_id, title, role, other participants.

## Agent Environment Tools

All three tools are registered in the `task` MCP server (alongside `create_agent_task`, `update_session_state`, `respond_to_task`) in `backend/app/env-templates/app_core_base/core/server/adapters/claude_code_sdk_adapter.py`.

Tool names as exposed to the agent SDK:
- `mcp__task__create_collaboration`
- `mcp__task__post_finding`
- `mcp__task__get_collaboration_status`

All three are included in `PRE_ALLOWED_TOOLS` in `backend/app/services/message_service.py` (auto-approved, no user confirmation needed).

## Prompt Injection

`PromptGenerator.build_collaboration_context_section()` generates a markdown section appended to the participant agent's system prompt containing:
- Collaboration title and description
- The agent's specific task (from `subtask.task_message`)
- Names of other participant agents
- Collaboration ID (for tool calls)
- Instructions to use `update_session_state`, `post_finding`, and `get_collaboration_status`

Session context enrichment happens in `MessageService._build_session_context()` which calls `AgentCollaborationService.get_collaboration_by_session()` and merges the returned fields into the session context dict.

## Configuration

No additional environment variables or settings required. Collaboration tools use the same `BACKEND_URL` and `AGENT_AUTH_TOKEN` environment variables as existing task tools.

## Security

- **Ownership validation** — coordinator and all target agents must belong to the authenticated user
- **Participant-only findings** — `post_finding` verifies the posting agent is either the coordinator or a subtask target
- **Owner-only status** — `get_collaboration_status` checks `collaboration.owner_id == user_id`
- **Auth token reuse** — uses the same per-agent `AGENT_AUTH_TOKEN` mechanism as other agent-env tools
