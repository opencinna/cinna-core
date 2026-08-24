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

- `backend/app/services/identity/identity_service.py` -- `IdentityService` (binding CRUD, assignment management, contact listing, per-person toggle, and `verify_identity_access()` -- the shared six-condition re-verification behind both the access grant and the session validity check)
- `backend/app/services/identity/identity_routing_service.py` -- `IdentityRoutingService` (Stage 2 routing: AI classification within an identity); takes ids and text, opens its own read session; `IdentityRoutingResult` includes `transformed_message` field; `_ai_classify()` returns `(binding, transformed_message)` tuple

### Backend -- Routing Integration

- `backend/app/services/routing/identity_candidate_provider.py` -- `IdentityCandidateProvider.build(db, caller_user_id)` -- Stage 1's identity candidates, one per owner, with the `identity:{owner_id}` `ref_id` namespace (`identity_ref_id()` / `parse_identity_ref()`)
- `backend/app/services/app_mcp/app_mcp_routing_service.py` -- `RoutingResult` extended with identity fields; `AppMCPRoutingService.route_message()` composes the route and identity providers and invokes `_route_identity()` when an identity candidate wins; `IdentityPick` is Stage 1's answer shape for a person
- `backend/app/services/sessions/channel_ingestion_service.py` -- `create_identity_session()` builds the session in the owner's space from an `IdentityGrant`; `assert_access()`'s `channel_caller` arm honours a re-verified grant as the one alternative to the three-way owner invariant
- `backend/app/models/sessions/session_sender.py` -- `IdentityGrant` (owner/binding/assignment ids) and `ChannelAccessPolicy.identity_grant`
- `backend/app/services/app_mcp/app_mcp_request_handler.py` -- `AppMCPRequestHandler._resolve_session()` handles identity session creation and resumption; `_create_identity_session()` delegates to `ChannelIngestionService.create_identity_session()`; `_check_identity_session_validity()` re-verifies on resumption

**Where identity used to live.** `AppAgentRouteService.get_effective_routes_for_user()` had a third arm appending one `EffectiveRoute` per identity owner with `source = "identity"` and a placeholder `agent_id`. That made "the people this caller can address" a fact only the App MCP route service could answer and only the App MCP surface could consume — and, because every identity route carried the same placeholder id, two owners on one ballot collided. The arm is gone; `EffectiveRoute`'s identity fields remain as vestigial optionals until the `AppAgentRoute` family is deleted.

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
| `message_patterns` | Text | nullable | **Dead column.** Newline-separated fnmatch patterns; Stage 2 stopped reading it in Phase 1 of the channels & identity unification (glob pre-matching deleted). Still stored and editable in the UI; dropped by a later phase |
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
- `identity_match_method` — Stage 2 match method: `"only_one"` or `"ai"`. `"pattern"` is no longer producible — glob pre-matching was deleted in Phase 1 of the channels & identity unification
- `app_mcp_route_type` — fixed value `"identity"`
- `app_mcp_match_method` — Stage 1 match method

`integration_type` is `"identity_mcp"` for identity sessions **created on the App MCP path** (distinct from `"app_mcp"`). It is **not** the marker of "an identity session" in general: since Phase 3 of the channels & identity unification an identity-routed *channel* session keeps `integration_type = "channel_<type>"`, because `ChannelOutboundService._resolve_channel_session` gates reply delivery on that prefix — stamping such a session `identity_mcp` would route correctly, run correctly, and never deliver a reply. The reliable cross-surface markers are the three identity columns (`identity_caller_id` / `identity_binding_id` / `identity_binding_assignment_id`). The channel path also stamps `identity_caller_name` into `session_metadata` (same key, so one UI branch serves both) but deliberately not `identity_owner_name`, since there the owner is the session's own user.

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

**Access re-verification:**

- `_live_binding(db, binding_id)` / `_live_assignment(db, assignment_id)` -- conditions 1 and 2, one implementation each, shared by both checks below so a liveness rule can never be tightened on one path and forgotten on the other
- `verify_identity_access(db, *, owner_id, binding_id, assignment_id, caller_user_id, agent_id) -> str | None`
  - Re-verifies an identity **authorization claim** — the ids behind `ChannelAccessPolicy.identity_grant`, handed in by the routing layer
  - Six conditions, all of them, every time:
    1. the `IdentityAgentBinding` exists and is `is_active`
    2. the `IdentityBindingAssignment` exists, `is_active`, `is_enabled`
    3. `assignment.binding_id == binding.id`
    4. `assignment.target_user_id == caller_user_id`
    5. `binding.agent_id == agent_id`
    6. `binding.owner_id == agent.owner_id == owner_id`
  - Conditions 3–6 are the ones a "still active?" check alone misses: they stop three individually-live rows belonging to three different authorizations from being assembled into a fourth that never existed
  - Returns `None` on success, else `IdentityService.IDENTITY_REVOKED_MESSAGE` — one message for every failure, deliberately: the caller is somebody else's guest, and naming *which* fact failed would describe the owner's configuration to them. The specific reason is logged

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

- `route_within_identity(owner_id, caller_user_id, message) -> IdentityRoutingResult | None`
  1. Opens its **own** short-lived read session (see below), then calls `IdentityService.get_active_bindings_for_user()` to get accessible bindings
  2. If none → returns `None`
  3. If one → uses directly (`match_method = "only_one"`). The single-binding shortcut stays a Stage-2 property; it is not flattened into Stage 1, which chose a *person* and cannot know how many agents that person exposes to this caller without re-deriving access per candidate
  4. Otherwise `_ai_classify()` — builds a `Candidate` per binding (via the shared `_binding_candidates()` builder) and calls `AgentClassifier.classify()` (`backend/app/services/routing/agent_classifier.py`) directly, not `route_to_agent()` — routing_tuning's Phase 5 collapsed this and two other near-copies onto one classifier
  5. Returns `IdentityRoutingResult(agent_id, agent_name, session_mode, binding_id, binding_assignment_id, match_method)`

**No caller session crosses the boundary.** The signature takes ids and text, and the service opens and closes its own read-only session. That is deliberate: Stage 2 is called from routing contexts that must not hand their transaction to a decision (`ChannelRoutingService.decide` is held to exactly these properties by `tests/architecture/channel_routing_purity_test.py`). The module performs no `add` / `commit` / `delete`, and returns plain data — never an ORM instance, whose session would be closed by the time the caller read an attribute off it.

**Glob pre-matching is gone.** `_try_pattern_match()` and Stage 2's reads of `IdentityAgentBinding.message_patterns` were removed: a second routing mechanism with silently higher priority than the classifier, which no trace explains well, is worse than one classifier call. `match_method` can therefore no longer be `"pattern"` at Stage 2. The **column and its API surface remain** (create/update/read still persist it) until the `message_patterns` removal migration.

`IdentityRoutingResult` dataclass:
```python
agent_id: uuid.UUID
agent_name: str        # value, copied out inside the service's own session
session_mode: str      # value, ditto
binding_id: uuid.UUID
binding_assignment_id: uuid.UUID
match_method: str  # "only_one" | "ai"
```

## Consumers

Identity is a routing-layer concept; `IdentityCandidateProvider` (Stage 1 candidates) and `IdentityRoutingService` (Stage 2) are shared, and each surface composes them into its own ballot:

| Consumer | Composes Stage 1 | Calls Stage 2 from | Extra gate | Session |
|---|---|---|---|---|
| App MCP Server | `AppMCPRoutingService.route_message` (routes + identities) | `_route_identity` in the same service | none beyond the per-person contact toggle | `ChannelIngestionService.create_identity_session`, `integration_type="identity_mcp"` |
| Server Channels (Phase 3) | `ChannelRoutingService._route_installed` (owned agents + identities) | `ChannelRoutingService._route_identity`, inside `decide` | the sender's `channel_user_setting.allow_identity_routing` | `ChannelInboundService._ingest` → `ingest_inbound_message`, `integration_type="channel_<type>"` |

The channel path differs in two further respects worth pinning here:

- **Authorization crosses a thread hop.** Stage 2 returns an `IdentityGrant` (owner/binding/assignment) on `RoutingDecisionResult.identity_grant`; `ChannelIngestionService.assert_access`'s `channel_caller` arm re-reads all six conditions via `IdentityService.verify_identity_access` before any session exists, and does so again on every subsequent message (the grant is rebuilt from the session row by `_resume_identity_grant`, never cached). The App MCP `mcp_caller` arm carries the grant but does not consult it — its per-message check is the liveness-only resume check.
- **Session vs. thread ownership diverge.** `session.user_id` is the identity owner; `ChannelThreadBinding.user_id` stays the sender.

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
Calls `_create_identity_session()`, which keeps only what is App-MCP-specific — the sender shape (`SessionSender.from_app_mcp(caller, identity_caller_user_id=caller)`), `integration_type = "identity_mcp"`, and the display metadata the MCP response reads back — and delegates the session itself to `ChannelIngestionService.create_identity_session()`, which:
1. Calls `assert_access` with `ChannelAccessPolicy(identity_grant=IdentityGrant(owner_id, binding_id, assignment_id), ...)`
2. Creates the session with `user_id = identity_owner_id` (NOT the caller)
3. Stamps `identity_caller_id` (from `sender.platform_user_id`), `identity_binding_id`, `identity_binding_assignment_id` — all three already in `_STAMPABLE_COLUMNS`
4. Merges the caller's `session_metadata_extra` (`identity_caller_name`, `identity_owner_name`, `identity_match_method`, …)

It is shared rather than inline so that a surface other than App MCP can create the same kind of session; `sender` and `integration_type` are parameters because those are the only things that genuinely differ per surface.

### The access grant

Ownership is inverted for an identity session — `session.user_id` is the identity **owner** while `identity_caller_id` records who is talking — and `ChannelIngestionService.assert_access`'s `channel_caller` arm asserts a three-way invariant (`agent.owner_id == policy.expected_owner_id == sender.platform_user_id`) that this inversion cannot satisfy. `ChannelAccessPolicy.identity_grant` is the one deliberate alternative:

- **Ids only.** `IdentityGrant` is a claim from the routing layer, not a conclusion
- **`assert_access` re-reads all of them** through `IdentityService.verify_identity_access` before honouring it. The routing decision and the session creation are separated by a worker-thread hop and possibly an auto-install wait; the owner may have revoked in between
- `assert_access` therefore takes a `db: DBSession` — the honest signature for a check that reads, and the same snapshot the session will be created in
- With no grant present the invariant is exactly as strict as before
- The `mcp_caller` arm carries the grant but does not consult it. App MCP's own re-verification is the per-message resume check, which is **liveness only** (conditions 1 and 2) — not the grant arm's four linkage conditions. That asymmetry is inherited, not introduced: App MCP never derived the linkage facts at create time either, because the routing decision that produced the ids ran in the same transaction moments before. So on today's only caller of `create_identity_session` the grant is carried and stamped but not re-read; it becomes a live check the moment a `channel_caller` sender uses that method

**Binding validity check** (`_check_identity_session_validity()`):
- Delegates to `IdentityService.check_session_validity()`, which runs the shared `_live_binding` / `_live_assignment` predicates — **conditions 1 and 2 only**, unchanged behaviour
- Deliberately not the full six. Conditions 3–6 ask "do these ids actually belong together", which is a question about a claim someone handed in; a session's ids were written together in one statement by `create_identity_session` *after* the grant was verified, and none of the fields they link is editable afterwards. Re-deriving them here would turn a revocation check into an integrity check, and would only ever reject rows written past the API
- What *can* change after creation is exactly what is re-read: the owner deactivating the binding or the assignment, and the caller opting out
- A session carrying neither identity column is not identity-routed and passes untouched
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
- Add form state (agent selector, trigger prompt, message patterns, session mode) plus assigned users as a `UserAllowlistSelectedItem[]`
- Edit dialog state (mirrors add form fields for the selected binding)

**Queries:**
- `["identity-bindings"]` — binding list with assignments
- `["agents-for-identity"]` — owner's agents (lazy, only when add form is open); filters out already-bound agents
- User selection uses the shared `UserAllowlistPicker` → `["user-search", q]` via `GET /users/search` (works for non-admin owners); no full user-list fetch. See [User Selector Pattern](../../development/frontend/user_selector_pattern.md)

**Mutations:** `createBindingMutation`, `updateBindingMutation`, `deleteBindingMutation`, `toggleBindingMutation`, `assignUsersMutation`, `removeAssignmentMutation` — all invalidate `["identity-bindings"]`.

**UI:**
- Each binding row: session mode icon (Wrench for building, MessageCircle for conversation), agent name, trigger prompt (truncated), active/inactive badge
- Row controls: expand chevron, active toggle switch, edit button, delete (AlertDialog)
- Expanded section: user assignment pills with remove buttons (rendered by the shared `UserAllowlistPicker`); pill labels come from the assignment's `target_user_name`/`target_user_email`

### `AppAgentRoutesCard.tsx` (Settings > Channels, "Identity Contacts" section)

Extended with a new section after "MCP Shared Agents". Loads contacts from `["identity-contacts"]` query key via `GET /api/v1/users/me/identity-contacts/`.

Each row shows: owner name, owner email, per-person enable/disable toggle. Toggle calls `PATCH /api/v1/users/me/identity-contacts/{owner_id}` with `{ is_enabled: bool }`.

### `McpConnectorsCard.tsx` (Agent > Integrations tab)

Extended with a third option in the type selector step of the creation dialog: "Identity MCP Server Integration". Selecting this shows a form that creates an identity binding for the current agent, with trigger prompt, session mode, and the shared `UserAllowlistPicker` — equivalent to the add form in `IdentityServerCard.tsx`.

### Session Header Label

For sessions with `integration_type = "identity_mcp"`, the session header shows a "Via Identity" badge, suffixed with `{identity_caller_name}` from `session_metadata` when present. A **second** branch covers the channel path: `integration_type.startsWith("channel_")` **and** `session_metadata.identity_caller_name` renders "Via Identity — {caller}". Two branches rather than one because the channel session must keep its `channel_` prefix for the reply to deliver, so the badge cannot key off `integration_type` alone.

## Query Key Summary

| Query Key | Endpoint | Owner |
|-----------|----------|-------|
| `["identity-bindings"]` | `GET /api/v1/identity/bindings/` | Identity owner |
| `["identity-contacts"]` | `GET /api/v1/users/me/identity-contacts/` | Target user (caller) |
| `["agents-for-identity"]` | `GET /api/v1/agents/?limit=200` | Identity owner (lazy) |
| `["user-search", q]` | `GET /api/v1/users/search` (via shared `UserAllowlistPicker`) | Any authenticated user |
