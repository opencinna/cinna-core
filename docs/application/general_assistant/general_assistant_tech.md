# General Assistant — Technical Reference

---

## File Locations

**Backend**

- `backend/app/models/agents/agent.py` — `Agent` table model (`is_general_assistant` field, partial unique index)
- `backend/app/models/users/user.py` — `User` table model (`general_assistant_enabled` field), `UserUpdateMe` schema
- `backend/app/services/users/general_assistant_service.py` — GA creation, lookup, background auto-create
- `backend/app/services/agents/agent_service.py` — workspace filter inclusion, no model-level guard needed
- `backend/app/services/sharing/agent_share_service.py` — sharing guard
- `backend/app/services/sessions/session_service.py` — building mode enforcement
- `backend/app/services/users/auth_service.py` — triggers auto-create after OAuth user creation
- `backend/app/api/routes/users.py` — `POST /users/me/general-assistant` endpoint
- `backend/app/api/routes/agents.py` — deletion guard in `DELETE /{id}`
- `backend/app/alembic/versions/4d769edd79d2_add_general_assistant.py` — schema migration

**Environment Template**

- `backend/app/env-templates/general-assistant-env/` — Docker template root
- `backend/app/env-templates/general-assistant-env/app/.env.template` — declares `BACKEND_URL` and `AGENT_AUTH_TOKEN` as pre-configured env vars (no extra configuration needed)
- `backend/app/env-templates/general-assistant-env/app/BUILDING_AGENT.md` — building-mode system prompt (the `GA_BUILDING_PROMPT` constant is read from here at runtime via standard prompt sync)
- `backend/app/env-templates/general-assistant-env/app/workspace/knowledge/platform/` — synced platform docs (mirrors `docs/application/` and `docs/agents/`)
- `backend/app/env-templates/general-assistant-env/app/workspace/knowledge/platform/README.md` — platform feature map (entry point for GA)
- `backend/app/env-templates/general-assistant-env/app/workspace/knowledge/platform/api_reference/` — auto-generated REST API reference grouped by domain
- `backend/app/env-templates/general-assistant-env/app/workspace/scripts/examples/` — working Python script patterns

**Sync Scripts**

- `.runnerkit/scripts/sync_ga_knowledge.py` — copies docs from `docs/application/` and `docs/agents/` into the environment template's knowledge directory; run manually when platform docs change
- `.runnerkit/scripts/check_docs_references.py` — validates internal doc links

**Frontend**

- `frontend/src/components/UserSettings/GeneralAssistantSettings.tsx` — Settings panel component
- `frontend/src/routes/_layout/settings.tsx` — mounts `GeneralAssistantSettings` as the "General Assistant" tab
- `frontend/src/routes/_layout/index.tsx` — dashboard agent selector (GA sorting, Sparkles icon, mode lock)
- `frontend/src/routes/_layout/agent/$agentId.tsx` — agent detail page (violet badge, hidden delete/share)

---

## Database Schema

### `agent` table additions

| Column | Type | Default | Notes |
|--------|------|---------|-------|
| `is_general_assistant` | `Boolean` | `false` | Identifies the GA; migration `server_default='false'` for existing rows |

**Partial unique index**

`ix_agent_general_assistant_per_user` — unique on `(owner_id)` where `is_general_assistant = true`. Enforced at the database level; ensures at most one GA per user regardless of application-layer checks.

### `user` table additions

| Column | Type | Default | Notes |
|--------|------|---------|-------|
| `general_assistant_enabled` | `Boolean` | `false` | All users start with GA disabled; opt-in via Settings |

---

## API Endpoints

### Create General Assistant

`POST /api/v1/users/me/general-assistant`

- Auth: `CurrentUser` (JWT required)
- Returns: `AgentPublic`
- HTTP 400 — `general_assistant_enabled` is `false` on the user record
- HTTP 409 — GA already exists for this user
- On success: creates the `Agent` record then calls `EnvironmentService.create_environment()` with the `general-assistant-env` template; falls back to `DEFAULT_AGENT_ENV_NAME` if the GA template is absent

### Enable / disable GA (via user update)

`PATCH /api/v1/users/me` — body: `{ "general_assistant_enabled": bool }`

Handled by the standard user update route; `UserUpdateMe.general_assistant_enabled` field is optional.

### Delete agent (guard)

`DELETE /api/v1/agents/{id}`

Returns HTTP 403 with `"The General Assistant cannot be deleted"` if `agent.is_general_assistant` is `true`. Guard is applied before calling `AgentService.delete_agent()`.

---

## Services & Key Methods

### `backend/app/services/users/general_assistant_service.py`

Constants defined at module level:

- `GA_AGENT_NAME = "General Assistant"`
- `GA_COLOR_PRESET = "violet"`
- `GA_ENV_TEMPLATE = "general-assistant-env"`
- `GA_BUILDING_PROMPT` — multi-line string describing the GA's operating procedure; stored as `workflow_prompt` on the agent record
- `GA_DESCRIPTION` — short description stored as `agent.description`

Methods on `GeneralAssistantService`:

- `get_general_assistant(session, user_id)` — SELECT query filtered by `owner_id` and `is_general_assistant=True`; returns `Agent | None`
- `create_general_assistant(session, user)` — async; creates the `Agent` record, then calls `EnvironmentService.create_environment()` with the GA template (tries `general-assistant-env/1.0.0` first, falls back to the platform default); returns the refreshed `Agent`
- `ensure_general_assistant(session, user)` — idempotent wrapper used during registration: calls `get_general_assistant` first, then `create_general_assistant` if no GA exists
- `trigger_auto_create_background(user_id)` — launches a daemon `threading.Thread`; the thread opens its own `SQLSession`, checks `general_assistant_enabled` and the absence of an existing GA, then calls `asyncio.run(create_general_assistant(...))` — safe to call from sync routes

### `backend/app/services/agents/agent_service.py`

- `AgentService.list_agents(session, user_id, ..., workspace_filter, apply_workspace_filter)` — when `apply_workspace_filter=True`, the WHERE clause includes `OR Agent.is_general_assistant == True` alongside the workspace UUID condition; this ensures the GA is always returned regardless of the active workspace

### `backend/app/services/sharing/agent_share_service.py`

- `AgentShareService.share_agent()` — raises `HTTPException(403)` before processing if `agent.is_general_assistant` is `true`

### `backend/app/services/sessions/session_service.py`

- `SessionService.create_session()` — after loading the `Agent`, checks `agent.is_general_assistant`; if `true`, calls `data.model_copy(update={"mode": "building"})` to override whatever mode the caller passed in

### `backend/app/services/users/auth_service.py`

- `AuthService.create_user_from_google()` — creates the new `User` record; GA is not auto-created (user must opt in via Settings)

### `backend/app/api/routes/users.py`

- `register_user()` (`POST /users/signup`) — registers the user; GA is not auto-created (user must opt in via Settings)

---

## Frontend Components

### `GeneralAssistantSettings` (`frontend/src/components/UserSettings/GeneralAssistantSettings.tsx`)

- Reads `currentUser.general_assistant_enabled` from `useAuth()` hook to control the enable/disable toggle
- Toggle fires `UsersService.updateUserMe({ requestBody: { general_assistant_enabled: enabled } })` via `useMutation`; on success invalidates the `["currentUser"]` query
- Queries `["agents", "general-assistant"]` to detect whether a GA already exists (`agentsData.data.find(a => a.is_general_assistant)`)
- When enabled but no GA exists: shows "Generate Assistant" button that calls `UsersService.generateGeneralAssistant()` (`POST /users/me/general-assistant`)
- When GA exists: shows a violet "General Assistant is active" badge and a link to the agent page (`/agent/:agentId`)
- Mounted as the "General Assistant" tab (hash `#general-assistant`) in `frontend/src/routes/_layout/settings.tsx`

### Dashboard Agent Selector (`frontend/src/routes/_layout/index.tsx`)

- `sortedAgents` is computed via `useMemo`: GA agents are moved to the front of the array (`agentsWithActiveEnv.filter(a => a.is_general_assistant)` then regular agents)
- Each GA pill renders `<Sparkles className="h-3.5 w-3.5" />` before the agent name
- A vertical divider (`<div className="h-6 w-px bg-border mx-1" />`) is inserted after the last GA pill when regular agents also exist
- Mode lock: a `useEffect` watches `selectedAgentId`; when the selected agent has `is_general_assistant=true` and `mode !== "building"`, it forces `setMode("building")` — the mode toggle is hidden for GA agents (the JSX returns `null` when `isGASelected` is `true`)

### Agent Detail Page (`frontend/src/routes/_layout/agent/$agentId.tsx`)

- When `agent.is_general_assistant` is `true`:
  - A violet "General Assistant" badge (Sparkles icon + text) is rendered in the page header next to the agent name
  - The three-dot action menu (containing delete and share actions) is hidden entirely (`!agent.is_general_assistant` gate)
  - The "Sharing" tab is filtered out from the agent configuration tabs

---

## Environment Template Structure

The `general-assistant-env` template extends `python-env-advanced` with the following additions:

- `app/.env.template` — `BACKEND_URL` and `AGENT_AUTH_TOKEN` are declared as pre-configured; they are injected by the platform at container start, not set by the user
- `app/workspace/knowledge/platform/` — full copy of `docs/application/` and `docs/agents/` synced via `.runnerkit/scripts/sync_ga_knowledge.py`
- `app/workspace/knowledge/platform/api_reference/` — per-domain Markdown files auto-generated from `frontend/openapi.json`; `README.md` contains an index linking to each domain file
- `app/workspace/scripts/examples/` — ready-to-run Python scripts covering common GA tasks (create agent, create workspace, create session, create scheduler, set up email integration, link credentials, etc.)

Knowledge sync command (run from project root after updating platform docs):

```
python3 .runnerkit/scripts/sync_ga_knowledge.py
```

---

## Configuration

No additional environment variables are required beyond the standard platform configuration. The environment template's `app/.env.template` notes that `BACKEND_URL` and `AGENT_AUTH_TOKEN` are provided automatically by the platform at container startup.

The platform uses these settings from `backend/app/core/config.py` during GA environment creation:

- `settings.DEFAULT_AGENT_ENV_NAME` / `settings.DEFAULT_AGENT_ENV_VERSION` — used as the fallback environment template when `general-assistant-env` is unavailable

---

## Security

- All GA-related API endpoints require a valid JWT (`CurrentUser` dependency); there are no unauthenticated paths
- A user can only create, view, or interact with their own GA; ownership is checked via `agent.owner_id == current_user.id` in all agent routes
- The GA cannot be shared with other users (`AgentShareService` guard) and cannot be cloned
- The GA cannot be deleted through any API path (HTTP 403 guard in the delete route)
- `BACKEND_URL` and `AGENT_AUTH_TOKEN` injected into the container are scoped to the owning user's permissions; the GA can only call API endpoints accessible to that user
- Credential values in API responses are never exposed in GA messages (GA building prompt includes an explicit rule: "NEVER expose credential values in your messages")
