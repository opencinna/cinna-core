# Identity MCP Server -- Technical Details

## File Locations

### Backend -- Models

- `backend/app/models/identity/identity_models.py` -- `IdentityAgentBinding`, `IdentityBindingAssignment` (DB tables) + Pydantic schemas: `IdentityAgentBindingCreate`, `IdentityAgentBindingUpdate`, `IdentityAgentBindingPublic`, `IdentityBindingAssignmentPublic`, `IdentityContactPublic`
- `backend/app/models/identity/__init__.py` -- re-exports identity models
- `backend/app/models/sessions/session.py` -- `Session` extended with `identity_caller_id`, `identity_binding_id`, `identity_binding_assignment_id`

### Backend -- Routes

- `backend/app/api/routes/identity.py` -- Identity owner CRUD at `/api/v1/identity/` (bindings, assignments, summary)
- `backend/app/api/routes/identity_contacts.py` -- Target user routes at `/api/v1/users/me/identity-contacts/`

### Backend -- Services

- `backend/app/services/identity/identity_service.py` -- `IdentityService` (binding CRUD, assignment management, contact listing, per-person toggle)
- `backend/app/services/identity/identity_routing_service.py` -- `IdentityRoutingService` (Stage 2 routing: pattern match + AI classification within an identity)

### Backend -- Routing Integration

- `backend/app/services/app_mcp/app_agent_route_service.py` -- `EffectiveRoute` dataclass extended with identity fields; `get_effective_routes_for_user()` includes identity contacts as routes with `source = "identity"`
- `backend/app/services/app_mcp/app_mcp_routing_service.py` -- `RoutingResult` extended with identity fields; `AppMCPRoutingService.route_message()` invokes `_route_identity()` when Stage 1 selects an identity route; `AppMCPRoutingService._route_identity()` delegates to Stage 2
- `backend/app/services/app_mcp/app_mcp_request_handler.py` -- `AppMCPRequestHandler._resolve_session()` handles identity session creation and resumption; `_create_identity_session()` sets owner as session user; `_check_identity_session_validity()` validates binding and assignment on resumption

### Frontend

- `frontend/src/components/UserSettings/IdentityServerCard.tsx` -- Settings > Channels tab card for identity owner management (list bindings, add/edit/delete, manage user assignments)
- `frontend/src/components/Agents/McpConnectorsCard.tsx` -- Extended with third integration type option: "Identity MCP Server Integration"
- `frontend/src/components/UserSettings/AppAgentRoutesCard.tsx` -- Extended with "Identity Contacts" section showing received identity contacts with per-person enable/disable toggle

## Database Schema

### `identity_agent_binding` -- Agents exposed behind an identity

| Column | Type | Constraints | Description |
|--------|------|------------|-------------|
| `id` | UUID | PK | Primary key |
| `owner_id` | UUID | FK > user.id, CASCADE, indexed | Identity owner; also acts as the identity's primary key for callers |
| `agent_id` | UUID | FK > agent.id, CASCADE, indexed | Agent exposed through this binding |
| `trigger_prompt` | Text | NOT NULL | Describes when Stage 2 should select this agent |
| `message_patterns` | Text | nullable | Newline-separated fnmatch patterns for Stage 2 pattern matching |
| `session_mode` | str(20) | default: "conversation" | Session mode for routing to this agent |
| `is_active` | bool | default: true | Owner toggle — disable agent for all callers at once |
| `created_at` | datetime | default: now | |
| `updated_at` | datetime | default: now | |

- Unique constraint: `(owner_id, agent_id)` — one binding per agent per identity
- Indexes: `owner_id`, `agent_id`

### `identity_binding_assignment` -- Per-caller agent access

| Column | Type | Constraints | Description |
|--------|------|------------|-------------|
| `id` | UUID | PK | Primary key |
| `binding_id` | UUID | FK > identity_agent_binding.id, CASCADE, indexed | Which binding this assignment grants access to |
| `target_user_id` | UUID | FK > user.id, CASCADE, indexed | Caller who can reach this agent |
| `is_active` | bool | default: true | Owner toggle — disable this agent for this specific caller |
| `is_enabled` | bool | default: false | Caller toggle — caller opts in or out of this identity owner |
| `auto_enable` | bool | default: false | If true, `is_enabled` starts as true; superuser-only |
| `created_at` | datetime | default: now | |

- Unique constraint: `(binding_id, target_user_id)` — one assignment per binding per caller
- Indexes: `binding_id`, `target_user_id`
- Application-level constraint: `binding.owner_id != target_user_id` (self-exclusion)

### `session` table -- Identity MCP extensions

Three nullable columns added to the existing `session` table:

| Column | Type | Constraints | Description |
|--------|------|------------|-------------|
| `identity_caller_id` | UUID | FK > user.id, SET NULL, nullable, indexed | The caller's user ID (for session resumption auth) |
| `identity_binding_id` | UUID | FK > identity_agent_binding.id, SET NULL, nullable | The Stage 2 binding that was selected |
| `identity_binding_assignment_id` | UUID | FK > identity_binding_assignment.id, SET NULL, nullable | The assignment linking binding to caller |

Identity sessions additionally store non-queryable display data in `session_metadata`:
- `identity_caller_name` — caller's full name (for session header label)
- `identity_owner_name` — identity owner's full name (returned as `agent_name` in MCP response)
- `identity_match_method` — Stage 2 match method: `"only_one"`, `"pattern"`, or `"ai"`
- `app_mcp_route_type` — fixed value `"identity"`
- `app_mcp_match_method` — Stage 1 match method

`integration_type` is set to `"identity_mcp"` for identity sessions (distinct from `"app_mcp"`).

## Pydantic Schemas

### `IdentityAgentBindingCreate`
```
agent_id: UUID
trigger_prompt: str
message_patterns: str | None = None
session_mode: str = "conversation"
assigned_user_ids: list[UUID] = []   # users assigned on creation
auto_enable: bool = False            # superuser-only
```

### `IdentityAgentBindingUpdate`
```
trigger_prompt: str | None = None
message_patterns: str | None = None
session_mode: str | None = None
is_active: bool | None = None
```

### `IdentityAgentBindingPublic`
```
id: UUID
agent_id: UUID
agent_name: str          # resolved from Agent.name
trigger_prompt: str
message_patterns: str | None
session_mode: str
is_active: bool
created_at: datetime
updated_at: datetime
assignments: list[IdentityBindingAssignmentPublic] = []
```

### `IdentityBindingAssignmentPublic`
```
id: UUID
binding_id: UUID
target_user_id: UUID
target_user_name: str    # resolved from User.full_name
target_user_email: str   # resolved from User.email
is_active: bool
is_enabled: bool
created_at: datetime
```

### `IdentityContactPublic`
```
owner_id: UUID
owner_name: str          # identity owner's full_name
owner_email: str         # identity owner's email
is_enabled: bool         # True if ANY of this owner's assignments to the caller are enabled
agent_count: int         # number of active bindings shared with this caller
assignment_ids: list[UUID]  # all assignment IDs for bulk per-person toggle
```

## API Routes

### Identity Bindings — `/api/v1/identity/`

Owner manages which of their agents are exposed and which users can reach each agent.

| Method | Path | Response | Description |
|--------|------|----------|-------------|
| `GET` | `/bindings/` | `list[IdentityAgentBindingPublic]` | List all bindings for current user with assignments |
| `POST` | `/bindings/` | `IdentityAgentBindingPublic` | Create binding; validates agent ownership; creates assignments for `assigned_user_ids` |
| `PUT` | `/bindings/{binding_id}` | `IdentityAgentBindingPublic` | Update trigger prompt, patterns, session_mode, or is_active |
| `DELETE` | `/bindings/{binding_id}` | `Message` | Delete binding; cascades assignments |
| `POST` | `/bindings/{binding_id}/assignments` | `list[IdentityBindingAssignmentPublic]` | Bulk assign users; skips duplicates and self |
| `DELETE` | `/bindings/{binding_id}/assignments/{user_id}` | `Message` | Remove a single user assignment |
| `GET` | `/summary/` | `list[IdentityAgentBindingPublic]` | Same as `/bindings/` — full identity summary for Settings card |

All routes: `SessionDep`, `CurrentUser`. Authorization: `binding.owner_id == current_user.id`.

Error codes: 403 (permission/ownership), 404 (not found), 409 (duplicate constraint).

### Identity Contacts — `/api/v1/users/me/identity-contacts/`

Target users manage which identity owners they have enabled.

| Method | Path | Response | Description |
|--------|------|----------|-------------|
| `GET` | `/` | `list[IdentityContactPublic]` | List identity contacts (people who shared agents with current user) |
| `PATCH` | `/{owner_id}` | `Message` | Toggle all assignments from a given owner on/off (per-person toggle) |

The `PATCH` endpoint accepts `{ "is_enabled": bool }` and updates all binding assignments from that owner to the current user simultaneously.

## Service Layer

### `IdentityService` (`backend/app/services/identity/identity_service.py`)

**Binding management (owner perspective):**

- `create_binding(db_session, owner_id, data: IdentityAgentBindingCreate, is_superuser: bool) -> IdentityAgentBindingPublic`
  - Validates agent ownership (`agent.owner_id == owner_id`)
  - Validates `auto_enable` requires superuser
  - Raises `IdentityNotFoundError` if agent not found, `IdentityPermissionError` for access violations
  - Raises `IntegrityError` (caught at route level → 409) for duplicate `(owner_id, agent_id)`
  - Creates assignments for `assigned_user_ids` with self-exclusion and duplicate skip

- `list_bindings(db_session, owner_id) -> list[IdentityAgentBindingPublic]`
- `update_binding(db_session, binding_id, owner_id, data: IdentityAgentBindingUpdate) -> IdentityAgentBindingPublic | None`
- `delete_binding(db_session, binding_id, owner_id) -> bool` — CASCADE handles assignments

- `get_active_bindings_for_user(db_session, owner_id, target_user_id) -> list[IdentityAgentBinding]`
  - Joins `IdentityAgentBinding` + `IdentityBindingAssignment`
  - Filters: `binding.is_active=True`, `assignment.is_active=True`, `assignment.is_enabled=True`, `assignment.target_user_id=target_user_id`
  - Used by Stage 2 routing to filter accessible agents

**Assignment management:**

- `assign_users(db_session, binding_id, owner_id, user_ids: list[UUID], auto_enable: bool) -> list[IdentityBindingAssignmentPublic]`
  - Skips existing assignments (no-op) and self-assignments
  - Returns all current assignments for the binding

- `remove_assignment(db_session, binding_id, owner_id, target_user_id: UUID) -> bool`

**User-facing (target user perspective):**

- `get_identity_contacts(db_session, user_id) -> list[IdentityContactPublic]`
  - Joins `IdentityBindingAssignment` + `IdentityAgentBinding`
  - Filters: `assignment.is_active=True`, `binding.is_active=True`, `assignment.target_user_id=user_id`
  - Groups by `binding.owner_id` — one `IdentityContactPublic` per distinct identity owner
  - `is_enabled` is `True` if ANY assignment from that owner is enabled

- `toggle_identity_contact(db_session, owner_id, user_id, is_enabled: bool) -> bool`
  - Updates `is_enabled` on ALL assignments from `owner_id` to `user_id`
  - Per-person toggle — affects all agents from that owner at once

### `IdentityRoutingService` (`backend/app/services/identity/identity_routing_service.py`)

Stage 2 routing — selects an agent from the owner's bindings accessible to the caller.

- `route_within_identity(db_session, owner_id, caller_user_id, message) -> IdentityRoutingResult | None`
  1. Calls `IdentityService.get_active_bindings_for_user()` to get accessible bindings
  2. If none → returns `None`
  3. If one → uses directly (`match_method = "only_one"`)
  4. Tries `_try_pattern_match()` — fnmatch against each binding's `message_patterns`
  5. Falls back to `_ai_classify()` — uses `route_to_agent()` from `app.agents.app_agent_router` with binding trigger prompts
  6. Returns `IdentityRoutingResult(agent_id, agent_name, session_mode, binding_id, binding_assignment_id, match_method)`

`IdentityRoutingResult` dataclass:
```python
agent_id: uuid.UUID
agent_name: str
session_mode: str
binding_id: uuid.UUID
binding_assignment_id: uuid.UUID
match_method: str  # "only_one" | "pattern" | "ai"
```

## Integration with App MCP Routing

### `EffectiveRoute` (extended)

`EffectiveRoute` in `app_agent_route_service.py` has three identity-specific optional fields:

```python
source: str  # "admin" | "user" | "identity"
identity_owner_id: uuid.UUID | None = None
identity_owner_name: str | None = None
identity_owner_email: str | None = None
```

Identity contacts are added to the effective routes list by `get_effective_routes_for_user()`. Each distinct identity owner becomes one `EffectiveRoute` with:
- `source = "identity"`
- `agent_id` = placeholder UUID (resolved in Stage 2)
- `agent_name` = owner's full name
- `trigger_prompt` = auto-generated: "Contact {full_name} ({email}). Routes to their available agents."
- `message_patterns = None` (identity routes never use Stage 1 pattern matching)

### `RoutingResult` (extended)

```python
is_identity: bool = False
identity_owner_id: uuid.UUID | None = None
identity_owner_name: str | None = None
identity_stage2_match_method: str | None = None
identity_binding_id: uuid.UUID | None = None
identity_binding_assignment_id: uuid.UUID | None = None
```

### `AppMCPRoutingService.route_message()` (extended)

After Stage 1 selects a route, if `selected.source == "identity"`:
- Calls `_route_identity()` which delegates to `IdentityRoutingService.route_within_identity()`
- Returns a `RoutingResult` with `is_identity=True` and all identity fields populated
- `agent_name` in the result is the identity owner's name, not the internal agent name

### `AppMCPRequestHandler._resolve_session()` (extended)

**Session resumption (identity):**
```python
identity_stmt = (
    select(Session, Agent)
    .join(Agent, ...)
    .where(
        Session.id == existing_session_id,
        Session.identity_caller_id == user_id,    # auth by caller, not owner
        Session.integration_type == "identity_mcp",
    )
)
```
If found, calls `_check_identity_session_validity()` before allowing resumption.

**Session creation (identity):**
Calls `_create_identity_session()` which:
1. Creates session with `user_id = identity_owner_id` (NOT the caller)
2. Sets `integration_type = "identity_mcp"`
3. Sets `identity_caller_id`, `identity_binding_id`, `identity_binding_assignment_id`
4. Stores `identity_caller_name`, `identity_owner_name`, `identity_match_method` in `session_metadata`

**Binding validity check** (`_check_identity_session_validity()`):
- Looks up `session.identity_binding_id` → checks `binding.is_active`
- Looks up `session.identity_binding_assignment_id` → checks `assignment.is_active` and `assignment.is_enabled`
- Returns error string `"This identity connection is no longer active."` on any failure, `None` if valid

**Response payload:**
For identity sessions, `agent_name` in the JSON response is the identity owner's full name (from `session_metadata["identity_owner_name"]`):
```json
{
  "response": "Here is the annual report...",
  "context_id": "session-uuid-in-owner-space",
  "agent_name": "User B"
}
```

## Frontend Components

### `IdentityServerCard.tsx` (Settings > Channels tab)

Owner-only card. Loads from `["identity-bindings"]` query key via `GET /api/v1/identity/bindings/`.

**State:**
- `expandedBindings: Set<string>` — which binding rows show user assignments
- Inline add form state (agent selector, trigger prompt, message patterns, session mode, assigned user IDs, user search query)
- Edit dialog state (mirrors add form fields for the selected binding)

**Queries:**
- `["identity-bindings"]` — binding list with assignments
- `["agents-for-identity"]` — owner's agents (lazy, only when add form is open); filters out already-bound agents
- `["users-list"]` — all platform users (lazy, only when add form or edit dialog open); filters out current user

**Mutations:** `createBindingMutation`, `updateBindingMutation`, `deleteBindingMutation`, `toggleBindingMutation`, `assignUsersMutation`, `removeAssignmentMutation` — all invalidate `["identity-bindings"]`.

**UI:**
- Each binding row: session mode icon (Wrench for building, MessageCircle for conversation), agent name, trigger prompt (truncated), active/inactive badge
- Row controls: expand chevron, active toggle switch, edit button, delete (AlertDialog)
- Expanded section: user assignment pills with remove buttons; inline user search for add/edit

### `AppAgentRoutesCard.tsx` (Settings > Channels, "Identity Contacts" section)

Extended with a new section after "MCP Shared Agents". Loads contacts from `["identity-contacts"]` query key via `GET /api/v1/users/me/identity-contacts/`.

Each row shows: owner name, owner email, per-person enable/disable toggle. Toggle calls `PATCH /api/v1/users/me/identity-contacts/{owner_id}` with `{ is_enabled: bool }`.

### `McpConnectorsCard.tsx` (Agent > Integrations tab)

Extended with a third option in the type selector step of the creation dialog: "Identity MCP Server Integration". Selecting this shows a form that creates an identity binding for the current agent, with trigger prompt, session mode, and user picker — equivalent to the add form in `IdentityServerCard.tsx`.

### Session Header Label

For sessions with `integration_type = "identity_mcp"`, the session header shows: "Via Identity — initiated by {identity_caller_name}" (sourced from `session_metadata.identity_caller_name`).

## Query Key Summary

| Query Key | Endpoint | Owner |
|-----------|----------|-------|
| `["identity-bindings"]` | `GET /api/v1/identity/bindings/` | Identity owner |
| `["identity-contacts"]` | `GET /api/v1/users/me/identity-contacts/` | Target user (caller) |
| `["agents-for-identity"]` | `GET /api/v1/agents/?limit=200` | Identity owner (lazy) |
| `["users-list"]` | `UsersService.readUsers()` | Identity owner (lazy) |
