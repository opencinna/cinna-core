# App MCP Server -- Technical Details

## File Locations

### Backend -- Models

- `backend/app/models/app_mcp/app_agent_route.py` -- `AppAgentRoute`, `AppAgentRouteAssignment`, `UserAppAgentRoute` (DB tables) + all Pydantic schemas (`AppAgentRouteCreate`, `AppAgentRouteUpdate`, `AppAgentRoutePublic`, `AppAgentRouteAssignmentPublic`, `UserAppAgentRouteCreate`, `UserAppAgentRouteUpdate`, `UserAppAgentRoutePublic`, `SharedRoutePublic`, `UserAppAgentRoutesResponse`)
- `backend/app/models/app_mcp/app_mcp_token.py` -- `AppMCPToken` (opaque OAuth tokens for app-level MCP)
- `backend/app/models/app_mcp/app_mcp_oauth_client.py` -- `AppMCPOAuthClient` (DCR clients for app-level MCP)
- `backend/app/models/app_mcp/app_mcp_auth_code.py` -- `AppMCPAuthCode`, `AppMCPAuthRequest` (OAuth authorization flow)
- `backend/app/models/app_mcp/__init__.py` -- re-exports all models
- `backend/app/models/__init__.py` -- includes app_mcp models in the global re-export

### Backend -- Routes

- `backend/app/api/routes/agent_app_mcp_routes.py` -- Agent-scoped CRUD endpoints at `/api/v1/agents/{agent_id}/app-mcp-routes/` (any authenticated agent owner)
- `backend/app/api/routes/app_agent_routes.py` -- Admin CRUD endpoints at `/api/v1/admin/app-agent-routes/` (superuser only)
- `backend/app/api/routes/user_app_agent_routes.py` -- User endpoints at `/api/v1/users/me/app-agent-routes/`
- `backend/app/api/main.py` -- route registration for all three routers

### Backend -- Services

- `backend/app/services/app_mcp/app_agent_route_service.py` -- `AppAgentRouteService` (agent-scoped and admin CRUD, assignments, effective routes), `UserAppAgentRouteService` (personal/legacy CRUD, toggle, shared route listing), `get_effective_routes_for_user()`
- `backend/app/services/app_mcp/app_mcp_routing_service.py` -- `AppMCPRoutingService` with `route_message()`, `_try_pattern_match()`, `_ai_classify()`; `RoutingResult` includes `transformed_message` field; `_route_identity()` passes Stage 1 transformation to Stage 2 and applies cascade logic
- `backend/app/services/app_mcp/app_mcp_request_handler.py` -- `AppMCPRequestHandler` with `handle_send_message()`, `_resolve_session()`, session lock management; uses `routing_result.transformed_message` as `effective_message` for message creation and title generation; stores `app_mcp_original_message` in session metadata when transformation occurs
- `backend/app/services/app_mcp/app_mcp_oauth_service.py` -- `AppMCPOAuthService` for app-level OAuth token lifecycle

### Backend -- MCP Server

- `backend/app/mcp/app_server.py` -- `create_app_mcp_server()` singleton factory
- `backend/app/mcp/app_tools.py` -- `send_message` tool registration on the App MCP FastMCP instance
- `backend/app/mcp/app_prompts.py` -- dynamic per-user MCP prompt listing
- `backend/app/mcp/app_token_verifier.py` -- `AppMCPTokenVerifier` for validating app-level OAuth tokens
- `backend/app/mcp/server.py` -- `MCPServerRegistry` extended with `"app"` path handling and `get_or_create_app_server()`

### Backend -- AI Router

- `backend/app/agents/app_agent_router.py` -- since [Auto Routing Tuning](../routing_tuning/routing_tuning.md)'s Phase 5, a thin `list[dict]`-in adapter over `backend/app/services/routing/agent_classifier.py`'s `AgentClassifier.classify` — see that feature's tech doc for the actual prompt rendering, parsing, and trace emission. `RouteToAgentResult` is an alias of `ClassificationResult`, not a second dataclass; `route_to_agent(message, available_agents, provider_kwargs=None)` still takes `available_agents` as `{id, name, trigger_prompt, prompt_examples}` dicts (kept because this function and `AIFunctionsService.route_to_agent` publish that shape to callers outside routing) and returns both agent ID and optional transformed message (routing prefix stripped); validates transformation: discards empty, identical-to-original, or exceeding-2x-length results
- `backend/app/agents/router_trigger_prompt_generator.py` -- `generate_router_trigger_prompt(agent_name, description, provider_kwargs)` function; model `gemini-2.5-flash-lite`; target output ~120–150 chars, single capability-verb sentence; on any generation failure returns a fallback string `"Handles tasks related to: <description snippet>"`
- `backend/app/agents/prompts/app_agent_router_prompt.md` -- prompt template instructing the LLM to return JSON `{"agent_id": "...", "message": "...", "confidence": 0.0, "reason": "...", "runner_up": "<uuid>|NONE"}`, with routing-prefix-stripping rules and an "Example messages" sub-list per candidate (rendered from `prompt_examples`) that the model is told to weigh at least as heavily as the trigger-prompt description. `confidence` / `reason` / `runner_up` are advisory only — see [Auto Routing Tuning](../routing_tuning/routing_tuning.md#phase-5--classifier-unification-prompt_examples-and-confidence)
- `backend/app/services/ai_functions/ai_functions_service.py` -- `route_to_agent()` method returns `RouteToAgentResult | None` by delegating to the adapter above; `generate_router_trigger_prompt(agent_name, description, user, db)` method wraps the generator, honours the user's `default_ai_functions_sdk` preference

### Backend -- OAuth Extensions

- `backend/app/mcp/oauth_routes.py` -- extended for app-level resource URLs (register, authorize, token, revoke)
- `backend/app/api/routes/mcp_consent.py` -- extended GET/POST to handle `AppMCPAuthRequest` nonces

### Backend -- Migrations

- `backend/app/alembic/versions/bccf5d92996f_add_app_mcp_server_models.py` -- creates `app_agent_route`, `app_agent_route_assignment`, `user_app_agent_route`, `app_mcp_token`, `app_mcp_oauth_client` tables
- `backend/app/alembic/versions/a07a133c69d2_add_app_mcp_auth_code_tables.py` -- creates `app_mcp_auth_code`, `app_mcp_auth_request` tables
- `backend/app/alembic/versions/72406c16543f_add_agent_id_to_session_table.py` -- adds `agent_id` to `session` table with backfill
- Migration adding `auto_enable_for_users` column to `app_agent_route` with backfill for existing admin-created routes
- `backend/app/alembic/versions/dd4ef5a6b7c8_add_router_trigger_prompt_and_auto_managed.py` -- adds `is_auto_managed` boolean (server default `false`) to `app_agent_route`; also adds `router_trigger_prompt` text NULLABLE to `agent` and `agent_bundle_revision` tables

### Frontend

- `frontend/src/components/Agents/McpConnectorsCard.tsx` -- Unified card for both direct MCP connectors and App MCP Server routes; two-step creation dialog (type selector → form); App MCP form includes user assignment and admin-only "Make Active for Users" toggle
- `frontend/src/components/Agents/McpConnectorsCardSimple.tsx` -- Simplified single-route view rendered for `agent-user` role on the Integrations tab; shows the auto-managed route with a per-user enable/disable toggle only
- `frontend/src/components/UserSettings/AppAgentRoutesCard.tsx` -- Settings card with App MCP URL (copyable), "MCP Shared Agents" section (shared routes with toggle), and read-only legacy personal routes display
- `frontend/src/components/Sidebar/AdminMenu.tsx` -- Admin dropdown menu (no longer includes "Application Agents" item)
- `frontend/src/routes/oauth/mcp-consent.tsx` -- adapted for `app_mcp=true` query param

### Tests

- `backend/tests/api/app_mcp/__init__.py` -- test package
- `backend/tests/api/app_mcp/conftest.py` -- domain fixtures (patched create_session, environment adapter, background tasks)
- `backend/tests/api/app_mcp/app_agent_routes_test.py` -- admin CRUD lifecycle, assignments, user personal routes, toggle, unique constraint
- `backend/tests/api/app_mcp/app_mcp_session_test.py` -- session creation, context_id reuse, invalid context_id, no routes, two independent sessions; session ownership (user_id=owner, caller_id=caller); cross-caller context_id isolation; caller_email in GET /sessions/{id} response
- `backend/tests/api/app_mcp/prompt_examples_test.py` -- prompt_examples lifecycle and validation for both AppAgentRoute and IdentityAgentBinding
- `backend/tests/utils/app_agent_route.py` -- test utility helpers for admin and user route API calls
- `backend/tests/unit/test_routing_message_transformation.py` -- unit tests for `RouteToAgentResult`, JSON parsing, sanity guards, `_ai_classify()` tuple propagation, cascade logic, and `AIFunctionsService` passthrough

## Database Schema

### `app_agent_route` -- Route definitions (any agent owner or superuser)

- `id` (UUID, PK), `name` (str), `agent_id` (FK > agent, CASCADE), `session_mode` (str), `trigger_prompt` (Text), `message_patterns` (Text, nullable), `prompt_examples` (Text, nullable), `channel_app_mcp` (bool), `is_active` (bool), `auto_enable_for_users` (bool, default False), `is_auto_managed` (bool, default False), `created_by` (FK > user, CASCADE), `created_at`, `updated_at`
- Indexes: `agent_id`, `created_by`
- `auto_enable_for_users`: when `True`, assignments to new users are created with `is_enabled=True`; only superusers may set this field
- `is_auto_managed`: set to `True` by `InstallService._auto_create_app_mcp_route` and by the Phase 8 backfill. When `True`, `apply_update` refreshes `trigger_prompt` and `name` from the new revision's `router_trigger_prompt`. Any user edit via the public `PUT /{route_id}` endpoint flips this to `False` permanently — the field is not settable from the `AppAgentRouteCreate` public body; the service exposes it only via the internal `auto_managed=` kwarg (trust boundary: prevents users from creating routes that the apply-update path would overwrite)

### `app_agent_route_assignment` -- Route > user link

- `id` (UUID, PK), `route_id` (FK > app_agent_route, CASCADE), `user_id` (FK > user, CASCADE), `is_enabled` (bool), `created_at`
- Unique constraint: `(route_id, user_id)`
- `is_enabled` initial value depends on `auto_enable_for_users`: `True` if superuser with auto-enable, `False` otherwise

### `user_app_agent_route` -- User personal routes (soft-deprecated)

- `id` (UUID, PK), `user_id` (FK > user, CASCADE), `agent_id` (FK > agent, CASCADE), `session_mode` (str), `trigger_prompt` (Text), `message_patterns` (Text, nullable), `channel_app_mcp` (bool), `is_active` (bool), `created_at`, `updated_at`
- Unique constraint: `(user_id, agent_id)` -- one personal route per agent per user
- No new records are created via the current UI; existing records continue to function

### `app_mcp_token` -- OAuth tokens for app-level MCP

- `id` (UUID, PK), `user_id` (FK > user), `client_id` (str, indexed), `token_hash` (str, unique indexed), `token_type` (str), `scope` (str, nullable), `expires_at` (datetime), `is_revoked` (bool), `created_at`

### `app_mcp_oauth_client` -- DCR clients for app-level MCP

- `id` (UUID, PK), `client_id` (str, unique indexed), `client_secret_hash` (str), `client_name` (str, nullable), `redirect_uris` (JSON), `created_at`

### `app_mcp_auth_code` / `app_mcp_auth_request` -- OAuth authorization flow

- Auth request stores nonce, client_id, redirect_uri, scope, state, user_id, expires_at
- Auth code stores code hash, client_id, redirect_uri, scope, user_id, expires_at, is_used

### Session model extension

- `session.agent_id` (UUID, FK > agent, nullable, indexed) -- added with backfill from `agent_environment.agent_id`
- `session.integration_type = "app_mcp"` -- identifies App MCP sessions
- `session.user_id` -- set to `agent.owner_id` (the agent owner sees and manages this session in the platform UI)
- `session.caller_id` (UUID, FK > user, nullable, indexed, ON DELETE SET NULL) -- tracks the MCP client user who initiated the session; used for `context_id` validation and for displaying caller email in the session page header
- `SessionPublic.caller_id` -- exposes caller UUID in session list and get responses
- `SessionPublicExtended.caller_name` / `caller_email` -- resolved at query time from the User table via LEFT JOIN on `caller_id`; available in `GET /sessions/` and `GET /sessions/{id}`
- Routing metadata stored in `session.session_metadata` JSON: `app_mcp_route_type`, `app_mcp_route_id`, `app_mcp_agent_name`, `app_mcp_session_mode`, `app_mcp_match_method`, `app_mcp_original_message` (only when message transformation occurred)

### Migration

- `2c222ba66e57_add_caller_id_to_session.py` -- adds `caller_id` column + FK + index; backfills existing `app_mcp` sessions by moving `user_id` → `caller_id` and setting `user_id` to `agent.owner_id`

## API Endpoints

### Agent-Scoped Routes -- `/api/v1/agents/{agent_id}/app-mcp-routes/`

Any authenticated user who owns the agent (or a superuser) can use these endpoints.

- `GET /` -- List App MCP routes for this agent; non-superusers see only routes they created; superusers see all
- `POST /` -- Create a route for this agent; agent_id in body is overridden by path parameter; `is_auto_managed` is NOT settable from this body (internal kwarg only)
- `PUT /{route_id}` -- Update route (creator or superuser); `agent_id` cannot be changed via update; if the route has `is_auto_managed=True`, any successful update flips it to `False`
- `DELETE /{route_id}` -- Delete route (creator or superuser)
- `GET /conflicts` -- Find effective routes similar to this agent's auto-managed route using Jaccard token-overlap. Returns `RouteConflictResponse`. Empty matches when no auto-managed route exists or no similarity threshold is crossed. Used by the install page for the conflict toast
- `POST /{route_id}/assignments` -- Assign users to route; `is_enabled` defaults follow `auto_enable_for_users`
- `DELETE /{route_id}/assignments/{user_id}` -- Remove user assignment

### Admin Routes -- `/api/v1/admin/app-agent-routes/`

Superuser-only. Provides cross-agent visibility for administrative management.

- `GET /` -- List all routes across all agents
- `POST /` -- Create route with optional `assigned_user_ids`
- `GET /{route_id}` -- Get route details
- `PUT /{route_id}` -- Update route (no ownership check)
- `DELETE /{route_id}` -- Delete route (cascades assignments)
- `POST /{route_id}/assignments` -- Assign users to route
- `DELETE /{route_id}/assignments/{user_id}` -- Remove user assignment

### User Routes -- `/api/v1/users/me/app-agent-routes/`

- `GET /` -- List user's routes: returns `{ personal_routes, shared_routes }`
- `POST /` -- Create personal route (soft-deprecated)
- `PUT /{route_id}` -- Update personal route (ownership check)
- `DELETE /{route_id}` -- Delete personal route (ownership check)
- `PATCH /admin-assignments/{assignment_id}` -- Toggle shared route enable/disable

### Utility

- `GET /api/v1/utils/mcp-info/` -- Returns `{ mcp_server_url }` for frontend to display the copyable App MCP URL

## Pydantic Schemas

### `AppAgentRouteCreate`

```python
name: str
agent_id: uuid.UUID           # overridden by path param in agent-scoped endpoint
session_mode: str = "conversation"
trigger_prompt: str
message_patterns: str | None = None
channel_app_mcp: bool = True
is_active: bool = True
auto_enable_for_users: bool = False   # superuser-only; rejected with 400 for non-superusers
activate_for_myself: bool = False     # when True, auto-adds creator as assigned user with is_enabled=True; UI defaults to True
assigned_user_ids: list[uuid.UUID] = []
```

### `AppAgentRoutePublic`

```python
id: uuid.UUID
name: str
agent_id: uuid.UUID
agent_name: str              # resolved at response time
session_mode: str
trigger_prompt: str
message_patterns: str | None
channel_app_mcp: bool
is_active: bool
auto_enable_for_users: bool
is_auto_managed: bool        # True when created by InstallService; flipped to False on any user PUT
agent_owner_name: str        # resolved from agent.owner
agent_owner_email: str       # resolved from agent.owner
created_by: uuid.UUID
created_at: datetime
updated_at: datetime
assignments: list[AppAgentRouteAssignmentPublic]
```

`AppAgentRouteAssignmentPublic` carries `user_id` plus resolved display info `user_email` / `user_full_name` (nullable) so the shared `UserAllowlistPicker` can render pills without a separate user lookup (parity with `IdentityBindingAssignmentPublic`). Populated by `_assignment_to_public()` in `app_agent_route_service.py`; when serialising a route's full assignment list, `_assignments_to_public()` batch-resolves all assigned users in a single query to avoid an N+1 lookup.

### `RouteConflictMatch` / `RouteConflictResponse`

Returned by `GET /api/v1/agents/{agent_id}/app-mcp-routes/conflicts`. Used by the install page to surface a non-blocking toast when the auto-created route's trigger prompt is similar to an existing effective route.

```python
class RouteConflictMatch(SQLModel):
    route_id: uuid.UUID
    agent_id: uuid.UUID
    agent_name: str
    trigger_prompt: str
    similarity: float         # 0.0–1.0 Jaccard token-overlap score

class RouteConflictResponse(SQLModel):
    matches: list[RouteConflictMatch] = []  # sorted descending by similarity
```

### `SharedRoutePublic`

Returned in `UserAppAgentRoutesResponse.shared_routes` for the user's settings view.

```python
route_id: uuid.UUID
name: str
agent_name: str
agent_owner_name: str        # agent's owner full name
agent_owner_email: str       # agent's owner email
shared_by_name: str          # route creator's name (may differ from agent owner)
session_mode: str
trigger_prompt: str
is_active: bool              # route-level toggle (set by route creator)
assignment_id: uuid.UUID
is_enabled: bool             # user-level toggle
```

### `UserAppAgentRoutesResponse`

```python
personal_routes: list[UserAppAgentRoutePublic]   # legacy UserAppAgentRoute records
shared_routes: list[SharedRoutePublic]           # AppAgentRoute records assigned to user
```

## Services & Key Methods

### `AppAgentRouteService`

- `create_route(db_session, data, current_user, *, auto_managed=False)` -- creates route with optional bulk user assignments; validates agent exists; enforces ownership check for non-superusers; enforces `auto_enable_for_users` superuser-only rule; when `activate_for_myself=True`, auto-adds creator as assigned user with `is_enabled=True`; other assignments' `is_enabled` follows `auto_enable_for_users`. The `auto_managed` kwarg (internal only, not from the request body) sets `AppAgentRoute.is_auto_managed=True` — used by `InstallService` and the Phase 8 backfill
- `list_routes(db_session)` -- lists all routes across all agents (superuser-only path)
- `list_routes_for_agent(db_session, agent_id, current_user)` -- lists routes for a specific agent; non-superusers see only routes they created
- `get_route(db_session, route_id)` -- get single route by ID (no ownership check, superuser path)
- `get_route_for_agent(db_session, agent_id, route_id, current_user)` -- get route with agent + ownership validation; returns None for missing or unauthorized (treats as not-found for security)
- `update_route(db_session, route_id, data)` -- update route (superuser-only path, no ownership check)
- `update_route_for_agent(db_session, agent_id, route_id, data, current_user)` -- update with ownership check; raises ValueError on permission violation; blocks `auto_enable_for_users=True` for non-superusers; `agent_id` field is immutable; flips `is_auto_managed=False` on any route that was previously auto-managed
- `delete_route(db_session, route_id)` -- delete route (superuser-only path)
- `delete_route_for_agent(db_session, agent_id, route_id, current_user)` -- delete with ownership check; raises ValueError on permission violation
- `assign_users(db_session, route_id, user_ids, auto_enable=False)` -- bulk assign users, skip duplicates; `auto_enable` controls `is_enabled` for new assignments
- `remove_assignment(db_session, route_id, user_id)` -- remove single assignment
- `get_effective_routes_for_user(db_session, user_id, channel)` -- returns unified `EffectiveRoute` list combining assigned routes (active + enabled) and personal routes (active), filtered by channel
- `toggle_admin_assignment(db_session, assignment_id, user_id, is_enabled)` -- allow a user to toggle their own route assignment on/off
- `find_route_conflicts_for_agent(db_session, agent_id, user_id, threshold=None)` -- compares the agent's auto-managed route trigger prompt against all other effective routes for `user_id` using Jaccard token-overlap; excludes identity routes and routes targeting the same agent; returns `RouteConflictResponse` sorted by descending similarity; returns empty when no auto-managed route exists for the agent
- `sync_router_trigger_prompt_from_agent(db_session, agent)` -- called after `PATCH /agents/{id}/router-trigger-prompt` and the generic `PUT /agents/{id}` save; propagates the new value onto the agent's auto-managed route (`is_auto_managed=True`) so the router sees it immediately without waiting for an apply-update. When no auto-managed route exists, delegates to `_create_auto_route_for_agent` instead of no-oping
- `_create_auto_route_for_agent(*, db_session, agent, trigger_prompt)` -- backfill-on-demand counterpart to `InstallService._auto_create_app_mcp_route`, for an install whose revision carried no trigger prompt (route skipped, install degraded). Builds the identical `AppAgentRouteCreate` shape (`session_mode="conversation"`, `channel_app_mcp=True`, `is_active=True`, `activate_for_myself=True`) and calls `create_route(..., auto_managed=True)`. Returns `None` without creating when the prompt is empty, when `agent.bundle_uuid IS NULL` (standalone agents — owner manages exposure explicitly), or when the owner already has a manual `is_auto_managed=False` route on the agent. Attribution is the **agent owner**, not the caller, so a superuser edit still lands the route + enabled self-assignment on the owner

### `UserAppAgentRouteService`

- `create_route(db_session, user_id, data)` -- creates personal route (soft-deprecated); validates agent ownership and unique constraint
- `list_routes(db_session, user_id)` -- lists existing personal routes
- `update_route(db_session, route_id, user_id, data)` -- update personal route (ownership check)
- `delete_route(db_session, route_id, user_id)` -- delete personal route (ownership check)
- `get_shared_routes(db_session, user_id)` -- returns `list[SharedRoutePublic]` with JOINed agent owner info and route creator ("shared by") info for all routes assigned to the user

### `AppMCPRoutingService`

- `route_message(db, user_id, message, channel)` -- main entry: gets effective routes, tries pattern match, falls back to AI, returns `RoutingResult` (with optional `transformed_message`) or None
- `_try_pattern_match(message, routes)` -- fnmatch-based glob matching against `message_patterns`
- `_ai_classify(message, routes)` -- builds a `Candidate` (`ref_id`, `name`, `trigger_prompt`, `prompt_examples`) per effective route and calls `AgentClassifier.classify(candidates, message)` directly (`backend/app/services/routing/agent_classifier.py`, not `AIFunctionsService.route_to_agent()` — routing_tuning's Phase 5 collapsed this and two other near-copies onto one classifier); returns `(EffectiveRoute, transformed_message)` tuple or None
- `_route_identity(db, selected_route, caller_user_id, message, stage1_method, transformed_message)` -- Stage 2 delegation; passes Stage 1's transformed message to identity router; applies cascade logic (Stage 2 wins > Stage 1 fallback > None)

### `AppMCPRequestHandler`

Session resolution flows through `ChannelIngestionService.assert_access` + `resolve_or_create_session` — see [channel ingestion](../agent_sessions/channel_ingestion.md) / [tech](../agent_sessions/channel_ingestion_tech.md). Message injection stays on the legacy `MessageService.create_message` + `stream_and_collect_response` pipeline (documented inline in the handler) due to a session-lock conflict with `initiate_stream`.

- `handle_send_message(user_id, message, context_id, mcp_ctx)` -- main tool handler: resolves session, creates message with effective (transformed) content, streams response, returns JSON
- `_try_resume_session(...)` -- single helper that resumes an existing session by `context_id` with strict `(integration_type, caller-column)` match (channel-edge resume verification; not delegated to `ChannelIngestionService._verify_resume_sender`)
- New-session creation supplies `caller_id` (app_mcp) or `identity_caller_id` + identity-binding columns (identity_mcp) via `extra_session_kwargs`; the service stamps them post-create via its whitelisted `_STAMPABLE_COLUMNS`
- Effective message: `routing_result.transformed_message or original_message`; used for `MessageService.create_message()` and title generation
- Session lock management: per-session `asyncio.Lock` with 500-entry cap and best-effort eviction

## Frontend Components

### McpConnectorsCard (`McpConnectorsCard.tsx`)

Handles both direct MCP connector management and App MCP Server route management for a specific agent, rendered in the agent's Integrations tab.

**Two-step creation dialog:**
- Step 1 (type_select): Two card buttons — "Direct MCP Connector" and "App MCP Server Integration"
- Step 2a (form + direct): Existing direct connector form (name, mode, allowed emails)
- Step 2b (form + app_mcp): App MCP form with name, session mode, trigger prompt, message patterns, user assignment multi-select, and "Make Active for Users" toggle

**App MCP form specifics:**
- Route name defaults to the agent's name when the form opens
- "Activate for Myself" switch (default ON): auto-adds the creator as an assigned user with `is_enabled=True`
- "Make Active for Users" (`auto_enable_for_users`): rendered for all users but `disabled={!isAdmin}`; non-admins see a disabled toggle with a tooltip explanation
- Both the create-step and **edit** dialog "Shared with Users" sections use the shared `UserAllowlistPicker` (`frontend/src/components/Common/UserAllowlistPicker.tsx`) — see [User Selector Pattern](../../development/frontend/user_selector_pattern.md). It searches server-side via `GET /users/search` (key `["user-search", q]`, works for non-admin agent-developers), not the admin-only `["users-list"]`/`GET /users/`; the current user is excluded server-side (use "Activate for Myself" instead). Edit-dialog pills resolve labels from the assignment's `user_email`/`user_full_name` (populated by `_assignment_to_public()`); create-step state is a `UserAllowlistSelectedItem[]` mapped to `assigned_user_ids` on submit

**Card body unified list:**
- Direct connectors section (existing)
- Separator (if both types have items)
- App MCP Routes section: name, session mode icon (MessageCircle for conversation, Wrench for building), active toggle, user count, edit and delete actions

**Queries and mutations:**
- `["app-mcp-routes", agentId]` -- fetches from `GET /api/v1/agents/{agent_id}/app-mcp-routes`
- `createAppMcpRouteMutation` -- POST to agent-scoped endpoint; invalidates `["app-mcp-routes", agentId]`
- `updateAppMcpRouteMutation` -- PUT; invalidates same query
- `deleteAppMcpRouteMutation` -- DELETE; invalidates same query
- `toggleAppMcpRouteMutation` -- PUT with `is_active` toggle; invalidates same query

### McpConnectorsCardSimple (`McpConnectorsCardSimple.tsx`)

Degraded view of the MCP Connectors card rendered for the `agent-user` role via `AgentIntegrationsTab`. Hides all developer-tier affordances:

- Finds the agent's auto-managed route by filtering `routes.find((r) => r.is_auto_managed)` from `GET /api/v1/agents/{agent_id}/app-mcp-routes`
- Toggle writes `AppAgentRouteAssignment.is_enabled` via `UserAppAgentRoutesService.toggleAdminAssignment` — the route itself stays `is_active=True`
- When no auto-managed route exists, shows a dashed-border hint leading with the consequence ("Not available in MCP clients yet") and directing the user to set a Trigger Prompt on the Configuration tab
- Shares React Query key `["app-mcp-routes", agentId]` with `EditRouterTriggerPromptModal` so the trigger-prompt mirror refreshes after a save without a manual reload

**Copy carries the feature explanation.** This card is an `agent-user`'s only exposure to App MCP routing — they never see the developer card's creation dialog, so nothing else tells them what the switch governs. The route name is deliberately **not** rendered (it is always the agent's own name, shown directly above). Instead the body is two labelled sections:

1. **"Available in external MCP clients"** — the `Switch`'s explicit label, with state-dependent helper text ("Your MCP client can send messages to this agent." / "…will not see or use this agent."). When the route carries no assignment for the current user, the disabled switch is explained rather than left silently greyed
2. **"When this agent gets picked"** — frames `AppAgentRoute.trigger_prompt` as a routing rule (quoted `blockquote`) rather than prose, names it as the agent's **Trigger Prompt**, and points at the Configuration tab to edit it

A footer states the negative space (turning it off hides the agent from MCP clients only — chat, schedules, and other integrations keep working) and links to `/settings#channels` for the MCP Server URL and setup steps.

### AppAgentRoutesCard (`AppAgentRoutesCard.tsx`)

Settings card in Settings > Channels tab. Read-focused view.

**Sections:**
1. Card header: App MCP Server URL (copyable) + help button (opens Getting Started modal at "app-mcp-setup" article)
2. "MCP Shared Agents" section: lists routes assigned to the user with agent name, owner name, "shared by" info, and enable/disable toggle; "Disabled by admin" label shown only when the route creator has disabled the route
3. "Personal Routes" section (legacy): shown only when existing personal routes exist; read-only with "Legacy" badge and note pointing to agent Integrations tab; no create/edit functionality

**State:**
- `["user", "appAgentRoutes"]` -- fetches `UserAppAgentRoutesResponse`; reads `shared_routes` field
- `["mcp-info"]` -- App MCP Server URL (staleTime: Infinity)
- `toggleSharedMutation` -- PATCH to `/users/me/app-agent-routes/admin-assignments/{assignment_id}`

**Removed from this component:**
- "Add Agent" button (route creation moved to agent Integrations tab)
- Personal route CRUD (soft-deprecated; only display remains)

### AdminMenu (`AdminMenu.tsx`)

The "Application Agents" dropdown item has been removed. The Admin menu now only contains: Users, Knowledge Sources, Plugin Marketplaces.

## Security

- **Agent-scoped endpoints**: `CurrentUser` guard + agent ownership verification (`agent.owner_id == current_user.id OR current_user.is_superuser`); returns 403 for unauthorized
- **Admin endpoints**: `get_current_active_superuser` guard
- **User endpoints**: `CurrentUser` guard + ownership verification on personal routes
- **`auto_enable_for_users`**: blocked for non-superusers at the service layer (ValueError → 400)
- **OAuth tokens**: SHA256-hashed, stored in `app_mcp_token` table; separate from per-connector tokens
- **Session isolation**: for `app_mcp` sessions, `context_id` validated against `caller_id` (not `user_id`, which is now the agent owner); for `identity_mcp` sessions, validated against `identity_caller_id`
- **Route access security**: `get_route_for_agent()` returns None (not 403) for unauthorized access to avoid information leakage
- **Concurrent message protection**: per-session asyncio.Lock prevents parallel processing

## Configuration

- `MCP_SERVER_BASE_URL` -- backend setting for the MCP server base URL; exposed to frontend via `/api/v1/utils/mcp-info/`
- App MCP Server URL: `{MCP_SERVER_BASE_URL}/app/mcp`
