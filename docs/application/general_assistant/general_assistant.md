# General Assistant

The General Assistant (GA) is a system-created agent that helps users set up, configure, and manage their agentic workflows by reading platform documentation and executing Python scripts against the platform's own REST API. It is a special-purpose building-mode agent that each user owns at most one of.

---

## Core Concepts

- **System-created agent** — created automatically on signup or on demand via Settings; not created through the normal agent creation wizard
- **Building mode only** — GA sessions are always forced to building mode; the mode toggle is hidden in the UI
- **Workspace-agnostic** — the GA has `user_workspace_id=NULL` and appears in the agent selector across all workspaces
- **Platform-aware environment** — built from the `general-assistant-env` Docker template, which includes synced platform docs, auto-generated API reference, and example scripts pre-loaded in the container workspace
- **Singleton per user** — a partial unique index on the `agent` table enforces at most one GA per user

---

## User Stories / Flows

### User opt-in (all users)

1. All users start with `general_assistant_enabled=false` by default — the GA is not auto-created on signup
2. User opens Settings → General Assistant tab
3. User toggles the enable switch — this calls `PATCH /api/v1/users/me` with `{ general_assistant_enabled: true }`
4. Once enabled, a "Generate Assistant" button appears; clicking it calls `POST /api/v1/users/me/general-assistant`
5. The endpoint creates the agent and environment synchronously and returns the new `AgentPublic` record
6. The UI invalidates the agents query so the GA appears immediately in the agent selector

### Using the GA on the dashboard

1. The GA appears as the first pill in the dashboard agent selector, before all other agents, separated from regular agents by a vertical divider
2. The GA pill shows a Sparkles icon and uses the violet color preset
3. When the GA is selected, the mode toggle is hidden; the session is always started in building mode
4. The user types a request (e.g. "Create a CRON-triggered agent that monitors my inbox") and sends it
5. The GA reads `./knowledge/platform/README.md`, navigates to relevant feature docs, reads the API reference, then writes and executes Python scripts to fulfil the request step by step

### Viewing the GA agent page

- The GA agent detail page (`/agent/:agentId`) shows a "General Assistant" violet badge next to the agent name
- The three-dot action menu (delete, share) is hidden for GA agents
- The Sharing tab is removed from the agent configuration tabs

---

## Business Rules

- **One GA per user** — enforced by the partial unique index `ix_agent_general_assistant_per_user` (unique on `owner_id` where `is_general_assistant = true`). Attempting to create a second GA returns HTTP 409.
- **Feature flag gate** — creating a GA via the API requires `general_assistant_enabled=true` on the user record; returns HTTP 400 if not enabled.
- **All users** — `general_assistant_enabled` defaults to `false` (`Field(default=False)`); users must explicitly opt in via Settings → General Assistant tab.
- **Building mode enforcement** — `SessionService.create_session()` detects `agent.is_general_assistant` and overwrites the session mode to `"building"` regardless of what the caller requests.
- **Cannot be deleted** — the delete route (`DELETE /api/v1/agents/{id}`) raises HTTP 403 if `agent.is_general_assistant` is true.
- **Cannot be shared or cloned** — `AgentShareService.share_agent()` raises HTTP 403 if `agent.is_general_assistant` is true.
- **Workspace visibility** — `AgentService.list_agents()` always includes GA agents in workspace-filtered queries (via `OR is_general_assistant = true`) so the GA appears regardless of which workspace is active.
- **Environment template fallback** — if the `general-assistant-env` template is not found, creation falls back to the platform's `DEFAULT_AGENT_ENV_NAME` template with a warning log.
- **Background thread safety** — `trigger_auto_create_background()` re-checks `general_assistant_enabled` and the existence of an existing GA before creating one, making the operation safe to call multiple times.

---

## Architecture Overview

```
User (signup or settings opt-in)
        |
        v
Backend Route ──> GeneralAssistantService.create_general_assistant()
                        |
                        |── Creates Agent record (is_general_assistant=True,
                        |   user_workspace_id=None, ui_color_preset="violet")
                        |
                        └── EnvironmentService.create_environment()
                                using "general-assistant-env" Docker template
                                        |
                                        v
                                Container workspace pre-loaded with:
                                  ./knowledge/platform/   ← synced platform docs
                                  ./knowledge/platform/api_reference/  ← generated API ref
                                  ./scripts/examples/     ← working script patterns
                                  BACKEND_URL + AGENT_AUTH_TOKEN ← env vars for API calls

Dashboard
  sortedAgents = [GA agents first, then regular agents]
  GA pill → Sparkles icon, violet color, no mode toggle

Session creation
  SessionService.create_session() forces mode="building" for GA agents
```

---

## Integration Points

- [auth](../auth/auth.md) — GA auto-creation is triggered from both the password signup route and the OAuth callback via `AuthService.create_user_from_google()`
- [agent_management](../agent_management/agent_management.md) — GA reuses the `Agent` model and standard agent CRUD routes but is governed by additional guards in the delete and update routes
- [agent_sessions](../agent_sessions/agent_sessions.md) — building mode enforcement happens inside `SessionService.create_session()` by inspecting `is_general_assistant`
- [agent_environments](../../agents/agent_environments/agent_environments.md) — GA uses the `general-assistant-env` environment template, which extends `python-env-advanced` with `BACKEND_URL` pre-configured
- [user_workspaces](../user_workspaces/user_workspaces.md) — GA is workspace-agnostic; workspace filtering always includes it via the `OR is_general_assistant = true` clause
- [agent_sharing](../../agents/agent_sharing/agent_sharing.md) — sharing is explicitly blocked for GA agents in `AgentShareService.share_agent()`
- [getting_started](../getting_started/getting_started.md) — the GA is a companion to the onboarding flow, accessible once an API key is configured
