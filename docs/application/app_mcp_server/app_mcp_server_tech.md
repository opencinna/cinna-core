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

- `backend/app/agents/app_agent_router.py` -- `RouteToAgentResult` dataclass and `route_to_agent()` function; returns both agent ID and optional transformed message (routing prefix stripped); validates transformation: discards empty, identical-to-original, or exceeding-2x-length results
- `backend/app/agents/prompts/app_agent_router_prompt.md` -- prompt template instructing the LLM to return JSON `{"agent_id": "...", "message": "..."}` with routing prefix stripping rules
- `backend/app/services/ai_functions/ai_functions_service.py` -- `route_to_agent()` method returns `RouteToAgentResult | None`

### Backend -- OAuth Extensions

- `backend/app/mcp/oauth_routes.py` -- extended for app-level resource URLs (register, authorize, token, revoke)
- `backend/app/api/routes/mcp_consent.py` -- extended GET/POST to handle `AppMCPAuthRequest` nonces

### Backend -- Migrations

- `backend/app/alembic/versions/bccf5d92996f_add_app_mcp_server_models.py` -- creates `app_agent_route`, `app_agent_route_assignment`, `user_app_agent_route`, `app_mcp_token`, `app_mcp_oauth_client` tables
- `backend/app/alembic/versions/a07a133c69d2_add_app_mcp_auth_code_tables.py` -- creates `app_mcp_auth_code`, `app_mcp_auth_request` tables
- `backend/app/alembic/versions/72406c16543f_add_agent_id_to_session_table.py` -- adds `agent_id` to `session` table with backfill
- Migration adding `auto_enable_for_users` column to `app_agent_route` with backfill for existing admin-created routes

### Frontend

- `frontend/src/components/Agents/McpConnectorsCard.tsx` -- Unified card for both direct MCP connectors and App MCP Server routes; two-step creation dialog (type selector → form); App MCP form includes user assignment and admin-only "Make Active for Users" toggle
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

- `id` (UUID, PK), `name` (str), `agent_id` (FK > agent, CASCADE), `session_mode` (str), `trigger_prompt` (Text), `message_patterns` (Text, nullable), `prompt_examples` (Text, nullable), `channel_app_mcp` (bool), `is_active` (bool), `auto_enable_for_users` (bool, default False), `created_by` (FK > user, CASCADE), `created_at`, `updated_at`
- Indexes: `agent_id`, `created_by`
- `auto_enable_for_users`: when `True`, assignments to new users are created with `is_enabled=True`; only superusers may set this field

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
- `POST /` -- Create a route for this agent; agent_id in body is overridden by path parameter
- `PUT /{route_id}` -- Update route (creator or superuser); `agent_id` cannot be changed via update
- `DELETE /{route_id}` -- Delete route (creator or superuser)
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
agent_owner_name: str        # resolved from agent.owner
agent_owner_email: str       # resolved from agent.owner
created_by: uuid.UUID
created_at: datetime
updated_at: datetime
assignments: list[AppAgentRouteAssignmentPublic]
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

- `create_route(db_session, data, current_user)` -- creates route with optional bulk user assignments; validates agent exists; enforces ownership check for non-superusers; enforces `auto_enable_for_users` superuser-only rule; when `activate_for_myself=True`, auto-adds creator as assigned user with `is_enabled=True`; other assignments' `is_enabled` follows `auto_enable_for_users`
- `list_routes(db_session)` -- lists all routes across all agents (superuser-only path)
- `list_routes_for_agent(db_session, agent_id, current_user)` -- lists routes for a specific agent; non-superusers see only routes they created
- `get_route(db_session, route_id)` -- get single route by ID (no ownership check, superuser path)
- `get_route_for_agent(db_session, agent_id, route_id, current_user)` -- get route with agent + ownership validation; returns None for missing or unauthorized (treats as not-found for security)
- `update_route(db_session, route_id, data)` -- update route (superuser-only path, no ownership check)
- `update_route_for_agent(db_session, agent_id, route_id, data, current_user)` -- update with ownership check; raises ValueError on permission violation; blocks `auto_enable_for_users=True` for non-superusers; `agent_id` field is immutable
- `delete_route(db_session, route_id)` -- delete route (superuser-only path)
- `delete_route_for_agent(db_session, agent_id, route_id, current_user)` -- delete with ownership check; raises ValueError on permission violation
- `assign_users(db_session, route_id, user_ids, auto_enable=False)` -- bulk assign users, skip duplicates; `auto_enable` controls `is_enabled` for new assignments
- `remove_assignment(db_session, route_id, user_id)` -- remove single assignment
- `get_effective_routes_for_user(db_session, user_id, channel)` -- returns unified `EffectiveRoute` list combining assigned routes (active + enabled) and personal routes (active), filtered by channel
- `toggle_admin_assignment(db_session, assignment_id, user_id, is_enabled)` -- allow a user to toggle their own route assignment on/off

### `UserAppAgentRouteService`

- `create_route(db_session, user_id, data)` -- creates personal route (soft-deprecated); validates agent ownership and unique constraint
- `list_routes(db_session, user_id)` -- lists existing personal routes
- `update_route(db_session, route_id, user_id, data)` -- update personal route (ownership check)
- `delete_route(db_session, route_id, user_id)` -- delete personal route (ownership check)
- `get_shared_routes(db_session, user_id)` -- returns `list[SharedRoutePublic]` with JOINed agent owner info and route creator ("shared by") info for all routes assigned to the user

### `AppMCPRoutingService`

- `route_message(db, user_id, message, channel)` -- main entry: gets effective routes, tries pattern match, falls back to AI, returns `RoutingResult` (with optional `transformed_message`) or None
- `_try_pattern_match(message, routes)` -- fnmatch-based glob matching against `message_patterns`
- `_ai_classify(message, routes)` -- calls `AIFunctionsService.route_to_agent()`, returns `(EffectiveRoute, transformed_message)` tuple or None
- `_route_identity(db, selected_route, caller_user_id, message, stage1_method, transformed_message)` -- Stage 2 delegation; passes Stage 1's transformed message to identity router; applies cascade logic (Stage 2 wins > Stage 1 fallback > None)

### `AppMCPRequestHandler`

- `handle_send_message(user_id, message, context_id, mcp_ctx)` -- main tool handler: resolves session, creates message with effective (transformed) content, streams response, returns JSON
- `_resolve_session(db, user_id, message, context_id)` -- resumes existing session by `context_id` (checking `session.caller_id == user_id` for app_mcp, `session.identity_caller_id == user_id` for identity_mcp) or routes to new agent and creates session; for app_mcp sessions, sets `session.user_id = agent.owner_id` and `session.caller_id = user_id`; returns 4-tuple `(session, agent, is_new_session, routing_result)`; on session resumption `routing_result` is None (no transformation on resume)
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
- User search/select fetches from `GET /api/v1/users` (enabled only when App MCP form step is open); current user is excluded from the dropdown (use "Activate for Myself" instead)
- "Make Active for Users" (`auto_enable_for_users`): rendered for all users but `disabled={!isAdmin}`; non-admins see a disabled toggle with a tooltip explanation
- Assigned users displayed as removable pills

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
