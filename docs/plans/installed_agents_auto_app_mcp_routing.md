# Installed Agents — Auto App MCP Routing — Implementation Plan

**Feature name**: `installed-agents-auto-app-mcp-routing`
**Document path**: `docs/plans/installed_agents_auto_app_mcp_routing.md`
**Status**: Draft
**Date**: 2026-05-11

---

## Context

A user with the `agent-user` role installs an agent from the catalog and expects it to be immediately reachable through the App MCP Server from external clients (Claude Desktop, etc.) without further configuration. Today install and App MCP routing are decoupled: `InstallService` creates an `Agent` row + environment but no `AppAgentRoute`, and `AppMCPRoutingService.route_message` only resolves agents through `AppAgentRoute` + `AppAgentRouteAssignment`. Result: a freshly installed agent is invisible to the router and the user gets *"No agents are configured for your account."*

This feature wires the two flows together by:

1. Introducing a publisher-owned **Trigger Prompt** on the agent (snapshotted to bundle revisions) that the AI router uses for classification.
2. Adding an AI generator for that prompt sourced from the agent description.
3. Auto-creating an `AppAgentRoute` and a self-assignment for the installer at install time.
4. Extending the `agent-user` role's UI shell so they can view and toggle the auto-created route (Integrations → MCP Connectors card only).
5. Enriching the router's classification context with `agent_name`.

After this change, the user flow becomes:
1. Install agent from catalog.
2. (Once) Connect Claude Desktop to App MCP endpoint via Settings → Channels.
3. From Claude Desktop: *"ask cinna to plan a meeting on monday in a free slot for 1 hour"* → router resolves the installed Calendar Planner → session is created and streamed.

Out of scope: identity MCP server auto-binding, direct MCP connector changes, A2A token auto-issuance, changes to building-mode routing.

---

## Architectural Considerations

### Where the field lives

`router_trigger_prompt` is an agent-level prompt that flows publisher → bundle revision → install:

- On `Agent`: editable by the agent owner (publisher works on their working install).
- On `AgentBundleRevision`: snapshotted at publish, immutable per revision (same pattern as `workflow_prompt`, `entrypoint_prompt`, `refiner_prompt`).
- Apply-update copies it back onto `Agent` and propagates into the auto-created `AppAgentRoute`.

### Source of truth for the route

The auto-created `AppAgentRoute` is **bundle-managed**. We add an `is_auto_managed: bool` column so apply-update can refresh `trigger_prompt`/`name`/`session_mode` only when the user hasn't overridden them. Manual edits via the UI flip `is_auto_managed=False`, after which the route is treated as user-owned and never touched by apply-update.

Uninstall already cascades via `AppAgentRoute.agent_id` FK → no extra cleanup needed.

### AI generator: description vs workflow prompt

Use the **description** field. Reasons:

- Description is short, user-facing, in natural language about *what the agent does* — exactly the shape the router consumes.
- Workflow prompts are long, internal, written *for* the agent (tools, edge cases, formatting). They produce noisier, less stable summaries and would churn the trigger prompt on every workflow edit.
- Description is stable across updates; trigger prompt should be stable too.

Model: `gemini-2.5-flash-lite` (same model as `AIFunctionsService.route_to_agent`, so generator and consumer share the same representation). Target output ~120–150 chars, single sentence, capability-verb-focused.

### Agent name in router context

`AIFunctionsService.route_to_agent` currently passes `trigger_prompt` per candidate. Extend the candidate payload to `{agent_name, trigger_prompt}` (also `prompt_examples` if present). Small change; helps disambiguate near-duplicates.

### `agent-user` Integrations tab

Currently `agent-user` is steered to a simplified shell (Catalog + Installed + Settings). The Integrations tab needs to be reachable for installs but the *only* card visible is **MCP Connectors**, and that card itself is degraded for `agent-user`: no "Direct MCP Connector" sub-type, no `auto_enable_for_users` superuser toggle, no user-share multi-select — just the auto-created route with name/trigger prompt (read-only mirror of the agent's `router_trigger_prompt`) and a per-user enable/disable toggle.

### Self-call session ownership

For `agent-user` installs the installer is the agent owner, so `session.user_id == session.caller_id`. The UI "MCP" badge + caller email already exists; verify it doesn't render a confusing "called by yourself" string. Trivial display tweak if so.

### Backfill

A one-time data migration creates `AppAgentRoute` + self-assignment for every existing install (`Agent.is_publisher_install=False`) that has a non-empty `description`, generating `router_trigger_prompt` via the new AI function. Idempotent — skip if a route already exists for the agent owned by the installer.

---

## Affected Files

### Backend models
- `backend/app/models/agents/agent.py` — add `router_trigger_prompt: str | None`.
- `backend/app/models/agents/agent.py` — `AgentBase`, `AgentPublic`, `AgentCreate`, `AgentUpdate` expose the field.
- `backend/app/models/bundles/agent_bundle_revision.py` — add `router_trigger_prompt: str | None`.
- `backend/app/models/app_mcp/app_agent_route.py` — add `is_auto_managed: bool` (default False).

### Alembic migration
- New migration in `backend/app/alembic/versions/` adding the three columns above + backfill data migration step (or a separate migration script invoked from `initial_data` / a Make target — pick the project's existing convention).

### Backend services
- `backend/app/services/bundles/install_service.py` — in `_install_from_revision`, after the readiness gate passes, call a new helper `_auto_create_app_mcp_route(session, install, revision, user)` that:
  - Skips if `revision.router_trigger_prompt` is empty (logs a degraded note on the install).
  - Calls `AppAgentRouteService.create_route` with `activate_for_myself=True`, `session_mode="conversation"`, `channel_app_mcp=True`, `is_auto_managed=True`, `created_by=user.id`, `name=install.name`, `trigger_prompt=revision.router_trigger_prompt`.
  - Catches exceptions and logs (do not abort install).
- `backend/app/services/bundles/install_service.py` — on apply-update, if existing `AppAgentRoute.is_auto_managed=True`, refresh `trigger_prompt`/`name` from the new revision; otherwise leave alone.
- `backend/app/services/agents/agent_publish_service.py` (or wherever revisions are snapshotted from `Agent`) — copy `router_trigger_prompt` into the new `AgentBundleRevision`.
- `backend/app/services/app_mcp/app_agent_route_service.py` — accept `is_auto_managed` in create payload; flip to `False` when a non-system user edits via the public PUT endpoint.
- `backend/app/services/ai_functions/` — new AI function `generate_router_trigger_prompt(agent_name: str, description: str) -> str` using `gemini-2.5-flash-lite`. Follow the existing ai-function pattern (see `route_to_agent` for shape).
- `backend/app/services/app_mcp/app_mcp_routing_service.py` / wherever it calls `AIFunctionsService.route_to_agent` — pass `agent_name` alongside `trigger_prompt`.

### Backend routes
- `backend/app/api/routes/agents.py` (or `agent_prompts.py` if separate) — accept `router_trigger_prompt` in update payloads.
- `backend/app/api/routes/agent_app_mcp_routes.py` — allow `agent-user` access for installs they own (existing ownership check already passes; verify role gates don't block).
- New endpoint or extension under `/ai-functions/` to expose the trigger-prompt generator to the frontend (mirror of existing generator endpoints like the agentic-team handover-prompt generator).

### Frontend
- `frontend/src/components/Agents/AgentPromptsTab.tsx` (or the Agent Prompts card component) — add a "Trigger Prompt" field with a "Generate" button. Button calls the new AI function endpoint; pre-fills field; user can edit.
- `frontend/src/components/Agents/McpConnectorsCard.tsx` — render a simplified view when the current user is `agent-user` (single auto-managed route row, on/off toggle, no create dialog).
- `frontend/src/routes/_layout/agent/$agentId.tsx` (or wherever Integrations tab is gated) — make Integrations tab visible to `agent-user` but mount only the MCP Connectors card.
- `frontend/src/routes/_layout/...` — wherever role gating decides shell layout, ensure `agent-user` can navigate to `/agent/{id}/integrations`.
- Regenerated client after backend changes (`bash scripts/generate-client.sh`).

### Tests
- `backend/tests/api/bundles/...` — install creates AppAgentRoute + self-assignment with correct fields; degraded install when `router_trigger_prompt` is empty; apply-update refresh; user override preservation.
- `backend/tests/api/app_mcp/...` — auto-managed route flag flipping on PUT.
- `backend/tests/api/ai_functions/...` — generator returns a non-empty short string.
- `backend/tests/api/agents/...` — `Agent` update accepts `router_trigger_prompt`.

### Docs
- `docs/agents/agent_bundles/agent_bundles.md` + `..._tech.md` — auto-route creation on install, apply-update propagation, `is_auto_managed` semantics.
- `docs/application/app_mcp_server/app_mcp_server.md` + `..._tech.md` — bundle-driven auto-route, `is_auto_managed` column, agent-name-in-classification change.
- `docs/agents/agent_prompts/agent_prompts.md` + `..._tech.md` — new Trigger Prompt field + generator.
- `docs/application/user_roles/user_roles.md` + `..._tech.md` — `agent-user` Integrations tab access (MCP Connectors only).
- `docs/application/ai_credentials/ai_functions_sdk_routing.md` — add the new generator entry.
- `docs/README.md` — update only if a feature is added or a description meaningfully changes.

---

## Edge Cases & Behaviors

1. **Publisher leaves `router_trigger_prompt` empty at publish time.** Publish UI shows a soft nudge with a "Generate" button (does not block publish). If still empty: install proceeds, no route is auto-created, install detail page surfaces an "Available in your Cinna router: not configured" hint with a "Set trigger prompt" link to the agent's Prompts tab (visible to the owner only — for `agent-user` installs this routes them to the editable Trigger Prompt field on their install).

2. **Push-update propagation.** Apply-update copies `revision.router_trigger_prompt` onto `Agent.router_trigger_prompt` and onto `AppAgentRoute.trigger_prompt` *only if* `AppAgentRoute.is_auto_managed=True`. If the user has edited the route via the UI, `is_auto_managed` is already `False` and the update is skipped.

3. **Reinstall after uninstall.** Uninstall cascade-deletes the route via FK. Reinstall produces a new `Agent.id` and a fresh auto-managed route. `AppDataVolume` reattaches by `(user_id, bundle_id)` as today.

4. **Conflict detection at install.** After auto-route creation, compute a simple similarity check (lowercased token overlap or LLM-based, pick the cheaper) between the new route's `trigger_prompt` and the installer's existing effective routes. If a near-match exists, surface a toast on the install completion page: *"You already have an agent for similar tasks: '<name>'. The router may need clarification."* Non-blocking.

5. **`agent-user` toggling the route off.** The Installed-agent MCP card writes `AppAgentRouteAssignment.is_enabled=False` (per-user toggle), not `AppAgentRoute.is_active`. Keeps the route alive for future re-enable.

6. **Self-call sessions.** Verify the session-header "MCP" badge + caller chip degrades gracefully when `session.user_id == session.caller_id`. Show the badge but suppress the redundant caller email row.

7. **Backfill idempotency.** Backfill skips installs that already have an `AppAgentRoute` with `is_auto_managed=True`, and skips installs where `description` is empty (no generator input).

---

## Implementation Phases

### Phase 1 — Schema & generator
- Add `router_trigger_prompt` to `Agent` and `AgentBundleRevision`.
- Add `is_auto_managed` to `AppAgentRoute`.
- Alembic migration.
- New AI function `generate_router_trigger_prompt`.
- AI function endpoint mirroring existing generator endpoints.
- Backend tests for the generator.

### Phase 2 — UI: Trigger Prompt field & publisher nudge
- Trigger Prompt field + Generate button in Agent Prompts card.
- Frontend client regeneration.
- Publish dialog nudge.
- Frontend tests / smoke check.

### Phase 3 — Install-time auto-route
- `InstallService` helper to auto-create `AppAgentRoute` + assignment with `is_auto_managed=True`.
- Skip-with-log when trigger prompt empty.
- Backend tests: install creates route, degraded install, role check.

### Phase 4 — Apply-update propagation
- Push-update copies `router_trigger_prompt` to install + auto-managed routes.
- `is_auto_managed=False` flip on manual edit via PUT.
- Backend tests for propagation and override preservation.

### Phase 5 — Router classification with agent name
- Extend `AIFunctionsService.route_to_agent` candidate payload with `agent_name`.
- Backend tests for routing disambiguation.

### Phase 6 — `agent-user` Integrations access
- Frontend role gating: Integrations tab visible to `agent-user`, only MCP Connectors card mounted.
- McpConnectorsCard simplified mode for `agent-user`.
- Verify ownership check at `agent_app_mcp_routes.py:49-53` covers `agent-user` installs (it should — they're the owner).

### Phase 7 — Conflict detection + self-call UI polish
- Install-page toast when fuzzy match found.
- Session badge tweak when `user_id == caller_id`.

### Phase 8 — Backfill migration
- One-time migration that generates trigger prompts and auto-routes for existing installs with non-empty descriptions. Idempotent.

### Phase 9 — Documentation
- Update agent_bundles, app_mcp_server, agent_prompts, user_roles, ai_functions_sdk_routing docs.

---

## Verification

End-to-end:
1. Publisher creates an agent with description "Plans meetings using my calendar", clicks "Generate" on Trigger Prompt → publishes a bundle.
2. As `agent-user`, install the bundle → install completes, dashboard shows the agent.
3. Inspect DB: `AppAgentRoute` row exists with `is_auto_managed=True`, matching `trigger_prompt`, `session_mode="conversation"`, `channel_app_mcp=True`. `AppAgentRouteAssignment` row exists with installer's `user_id` and `is_enabled=True`.
4. From Claude Desktop connected to `{MCP_SERVER_BASE_URL}/app/mcp`: send *"ask cinna to plan a meeting on monday in a free slot for 1 hour"*. Expect a session to be created against the installed agent and a streamed response.
5. Publisher edits description + regenerates trigger prompt + publishes a new revision. As `agent-user`, accept the update. Verify `AppAgentRoute.trigger_prompt` updated to new value.
6. Edit `AppAgentRoute.trigger_prompt` manually via UI. Publish another revision and accept the update. Verify the manual value is preserved (`is_auto_managed=False`).
7. Uninstall agent. Verify `AppAgentRoute` and assignment are cascade-deleted.
8. Run backfill migration on a DB with pre-existing installs. Verify routes are created only for installs with non-empty descriptions; rerunning is a no-op.

Tests:
- `make test-backend` clean.
- Type-check the touched frontend files with `npx tsc --noEmit 2>&1 | grep -E "(AgentPromptsTab|McpConnectorsCard)" | head -20`.

---

*Last updated: 2026-05-11*
