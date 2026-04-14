# Identity MCP Server — Implementation Plan

## Overview

The Identity MCP Server introduces a **person-level abstraction** for the App MCP Server routing system. Instead of sharing individual agents directly, users expose themselves as a routable "identity" — a virtual contact point that other users can address by name. When a message is routed to an identity, a **two-stage routing** process occurs: the first router (caller's side) resolves the message to a person, and the second router (identity owner's side) selects the appropriate agent from the identity owner's portfolio.

**Core capabilities:**
- New MCP Connector integration type: "Identity MCP Server Integration"
- Two-stage routing: person resolution → agent selection within that person's identity
- Identity owners control which agents are discoverable behind their identity, **per-user** — different users may see different agents behind the same identity
- Callers address people naturally ("ask User B to prepare an annual report") without knowing individual agent names
- Sessions execute in the identity owner's space, responses flow back to the caller

**High-level flow:**

```
User A (caller):  "Ask User B to prepare an annual report"
       │
       ▼
┌──────────────────────────────┐
│  Stage 1: Caller's Router    │
│  Effective routes include:   │
│  - Agent routes (existing)   │
│  - Identity routes (new)     │
│  AI classifies → "User B"   │
└──────────┬───────────────────┘
           │
           ▼
┌──────────────────────────────┐
│  Stage 2: Identity Router    │
│  User B's agents accessible  │
│  to User A:                  │
│  - Annual Report Agent       │
│  - Data Analysis Agent       │
│  (HR Policy Agent is NOT     │
│   shared with User A)        │
│  AI classifies → "Annual     │
│  Report Agent"               │
└──────────┬───────────────────┘
           │
           ▼
┌──────────────────────────────┐
│  Session created in User B's │
│  space. Response streamed    │
│  back to User A.             │
└──────────────────────────────┘
```

## Architecture Overview

### System Components

```
Identity Owner (User B) UI:
  Settings > Channels tab > "Identity Server" card
    → Manages identity agent bindings (which agents are behind identity)
    → Manages which users receive this identity (share with users)
    → Shows summary of agents shared via identity

  Agent > Integrations tab > MCP Connectors > New > "Identity MCP Server Integration"
    → Quick-add shortcut: adds this agent to identity with a trigger prompt
    → Links to the Settings > Channels > Identity Server card for full management

User Settings (User A) UI:
  Settings > Channels tab > "MCP Server" card > "Identity Contacts" section
    → Shows people who shared their identity with User A (after "MCP Shared Agents")
    → Enable/disable toggle per identity contact

Backend:
  IdentityAgentBinding (DB)       — binds specific agents to an identity (with trigger prompts)
  IdentityBindingAssignment (DB)  — per-agent user access (which users can reach which agents)
  IdentityRoutingService          — Stage 2 routing (select agent within identity, filtered by caller)
  AppMCPRoutingService            — Extended Stage 1 routing (includes identity contacts)
  AppMCPRequestHandler            — Extended to handle identity routing results
```

### Data Flow

```
MCP Client → App MCP Server → Stage 1 Router
                                    │
                        ┌───────────┴───────────┐
                        │                       │
                   Agent Route              Identity Route
                   (existing)               (new)
                        │                       │
                        ▼                       ▼
                   Direct to             Stage 2 Router
                   agent session         (identity owner's agents)
                        │                       │
                        ▼                       ▼
                   Session in            Session in identity
                   caller's space        owner's space
                        │                       │
                        └───────┬───────────────┘
                                ▼
                        Response streamed back to caller
```

### Key Architectural Decisions

1. **Identity is per-user with per-agent-per-user sharing**: A user has ONE identity with multiple agents behind it, but **different users may see different agents**. User B can share Agent X with User A but not User C, and share Agent Y with User C but not User A. Stage 2 routing only considers agents accessible to the specific caller. Identity is **workspace-independent** (like agentic teams) — it represents a person across the whole platform, not within a specific workspace. Agents from any workspace can be bound to the identity.

2. **Two-stage routing, not nested MCP**: The second routing happens server-side within the same request. No additional MCP connections or OAuth flows are needed.

3. **Sessions execute in identity owner's space**: The agent runs with the identity owner's credentials, environment, and workspace. The identity owner sees these sessions in their UI (like any other agent session). The caller communicates via their MCP client and receives responses there.

4. **Cross-user session model**: A new `integration_type = "identity_mcp"` marks these sessions. The session is owned by the identity owner (User B) and visible in their session list. The caller (User A) communicates through their MCP client and receives responses via the App MCP response.

5. **Self-exclusion**: Users cannot create identity routes for themselves (they already have direct agent routes).

6. **Identity route appears as a "person" in Stage 1**: In the AI router, identity routes appear with the person's name and email. No custom trigger prompts — users are employees addressed by name in a company context. The trigger prompt is auto-generated: "Use this agent if user asks to contact {full_name} ({email})."

7. **Binding validity enforced on session continuity**: If an identity agent binding is disabled or deleted mid-conversation, subsequent messages on that `context_id` fail with an error indicating the connection is no longer active. This is unlike regular App MCP sessions which survive route deletion.

8. **Dedicated Settings card**: Identity management lives in Settings > Channels > "Identity Server" card (similar pattern to the "MCP Server" card), not embedded in per-agent McpConnectorsCard. The per-agent Integrations tab offers a shortcut to add an agent to identity.

9. **Separate sessions per topic**: Different requests to the same identity create separate sessions (potentially with different agents). There is no single "conversation with a person" — each routing is independent, producing its own `context_id`.

10. **No special owner actions on identity sessions**: Identity owner sees identity sessions as regular agent sessions — no redirect, no special controls. Same simplicity as app_mcp sessions. Additional owner actions are a future enhancement.

## Data Models

### `identity_agent_binding` — Agents exposed behind an identity

Links specific agents to a user's identity. Each binding has its own trigger prompt for Stage 2 routing.

| Field | Type | Constraints | Description |
|-------|------|------------|-------------|
| `id` | UUID | PK | Primary key |
| `owner_id` | UUID | FK → user.id, CASCADE, indexed | Identity owner (redundant for query convenience) |
| `agent_id` | UUID | FK → agent.id, CASCADE, indexed | Agent exposed through identity |
| `trigger_prompt` | Text | NOT NULL | Describes when this agent should be selected (Stage 2) |
| `message_patterns` | Text | nullable | Newline-separated glob patterns for Stage 2 pattern matching (same as App MCP routes) |
| `session_mode` | str(20) | default: "conversation" | Session mode for this agent |
| `is_active` | bool | default: true | Toggle individual agent within identity |
| `created_at` | datetime | default: now | Creation timestamp |
| `updated_at` | datetime | default: now | Last update timestamp |

- **Unique constraint**: `(owner_id, agent_id)` — one binding per agent per identity
- **Indexes**: `owner_id`, `agent_id`
- **Validation**: Agent must be owned by `owner_id`

### `identity_binding_assignment` — Per-agent user access

Links a specific identity agent binding to a target user. Controls which users can reach which agents behind an identity. This is the key table that enables **different agents for different users**.

| Field | Type | Constraints | Description |
|-------|------|------------|-------------|
| `id` | UUID | PK | Primary key |
| `binding_id` | UUID | FK → identity_agent_binding.id, CASCADE, indexed | Which agent binding |
| `target_user_id` | UUID | FK → user.id, CASCADE, indexed | User who can access this agent via identity |
| `is_active` | bool | default: true | Owner-level toggle (can owner disable this specific agent for this user?) |
| `is_enabled` | bool | default: false | Target user-level toggle (can they opt out of this person's identity?) |
| `auto_enable` | bool | default: false | If true, `is_enabled` starts as true (superuser-only) |
| `created_at` | datetime | default: now | Creation timestamp |

- **Unique constraint**: `(binding_id, target_user_id)` — one assignment per binding per user
- **Indexes**: `binding_id`, `target_user_id`
- **Constraint**: binding.owner_id != target_user_id (application-level, self-exclusion)

**How per-user identity works:**

```
User B (identity owner) has 3 agents in identity:
  - Annual Report Agent  → shared with [User A, User C]
  - Data Analysis Agent  → shared with [User A]
  - HR Policy Agent      → shared with [User C]

When User A addresses User B:
  Stage 2 sees: Annual Report Agent, Data Analysis Agent (2 agents)

When User C addresses User B:
  Stage 2 sees: Annual Report Agent, HR Policy Agent (2 agents)
```

**Derived identity contacts (no separate identity_route table):**

A user "appears" as an identity contact for a target user if they have **at least one** active + enabled binding assignment for that target user. This is computed via query — no separate `identity_route` table needed.

```sql
-- Identity contacts for User A (people they can address)
SELECT DISTINCT owner.id, owner.full_name, owner.email
FROM identity_agent_binding b
JOIN identity_binding_assignment a ON a.binding_id = b.id
JOIN user owner ON owner.id = b.owner_id
WHERE a.target_user_id = :user_a_id
  AND a.is_active = True
  AND a.is_enabled = True
  AND b.is_active = True
```

### Session model extension

Existing `Session` model gets a new `integration_type` value:
- `integration_type = "identity_mcp"` — identifies sessions initiated via identity routing

**New columns on `session` table** (nullable, only populated for identity sessions):

| Field | Type | Constraints | Description |
|-------|------|------------|-------------|
| `identity_caller_id` | UUID | FK → user.id, SET NULL, nullable, indexed | The user who initiated the request (User A) |
| `identity_binding_id` | UUID | FK → identity_agent_binding.id, SET NULL, nullable | The binding selected in Stage 2 |
| `identity_binding_assignment_id` | UUID | FK → identity_binding_assignment.id, SET NULL, nullable | The assignment linking binding to caller |

These are **first-class columns** (not JSON metadata) because the session has three parties (agent owner, session owner, caller) and SQL queries need to filter/validate by `identity_caller_id` efficiently (e.g., session resumption, session listing).

Additional metadata stored in `session.session_metadata` JSON (non-queryable display data):
- `identity_caller_name` — name of the caller (for session label display)
- `identity_owner_name` — name of the identity owner
- `identity_match_method` — "ai" | "only_one" | "pattern" (Stage 2 match method)

**Important**: Unlike regular App MCP sessions (owned by caller), identity sessions are **owned by the identity owner** (User B) since the agent runs in their space. The identity owner sees these sessions in their normal session UI (agent sidebar, session list). The `identity_caller_id` column tracks who initiated the request, displayed as a label in the session header (e.g., "Initiated by User A via Identity").

## Security Architecture

### Access Control

| Action | Who |
|--------|-----|
| Create identity agent binding | Agent owner only |
| Assign users to a binding (share agent via identity) | Binding owner only |
| Enable/disable received identity contact | Target user (self-toggle on assignment) |
| Toggle `auto_enable` on assignment | Superuser only |
| View own identity bindings + assignments | Identity owner only |
| View received identity contacts | Target user only |

### Cross-User Execution

- Stage 2 routing runs with identity owner's agent portfolio — caller cannot see or enumerate these agents
- Session created under identity owner's user_id — uses their credentials, environment, workspace
- **Identity owner sees sessions**: identity sessions appear in the owner's session list like normal sessions, with an "identity" label indicating the caller
- **Caller communicates via MCP**: caller sends/receives messages through their MCP client only (no platform UI access to the session)
- Response text is the only data returned to caller (no agent IDs, session IDs, or internal metadata exposed)
- Caller receives a `context_id` for session continuity, but this ID maps to a session in the identity owner's space; the system validates `session.identity_caller_id` (not `session.user_id`) for resumption
- **Binding validity enforced**: before processing each message on an identity session, the system verifies the identity agent binding is still active (`is_active=True`) AND the binding assignment for the caller is still active/enabled; if either check fails, returns error: "This identity connection is no longer active"
- Rate limiting consideration: identity owners may want to limit how frequently their agents can be invoked by others (future enhancement)

### Input Validation

- Agent must be owned by identity owner on binding creation
- `binding.owner_id != target_user_id` on assignment creation (self-exclusion)
- Target user must exist
- Trigger prompts required (non-empty) on bindings
- Unique constraint on `(binding_id, target_user_id)` for assignments

## Backend Implementation

### API Routes

#### Identity Agent Bindings — `/api/v1/identity/bindings/`

Agent owner manages which of their agents are exposed behind their identity.

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | List all identity agent bindings for current user (includes assignments per binding) |
| `POST` | `/` | Create a new binding (agent_id, trigger_prompt, session_mode, optional assigned_user_ids) |
| `PUT` | `/{binding_id}` | Update binding (trigger_prompt, session_mode, is_active) |
| `DELETE` | `/{binding_id}` | Remove agent from identity (cascades assignments) |
| `POST` | `/{binding_id}/assignments` | Assign users to this binding (share this agent via identity) |
| `DELETE` | `/{binding_id}/assignments/{user_id}` | Remove user assignment from this binding |

Dependencies: `SessionDep`, `CurrentUser`

Authorization: Only the identity owner (binding.owner_id == current_user.id)

#### Identity Summary — `/api/v1/identity/summary/`

Read-only summary for the Identity Server card in Settings.

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Returns all bindings with their assignments for current user (full identity view) |

Dependencies: `SessionDep`, `CurrentUser`

#### User-facing Identity Contacts — `/api/v1/users/me/identity-contacts/`

Target users manage their received identity contacts.

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | List identity contacts (people who shared agents with me via identity) |
| `PATCH` | `/{assignment_id}` | Toggle `is_enabled` on my assignment (opt in/out of a specific person's identity) |

Dependencies: `SessionDep`, `CurrentUser`

Authorization: Only the target user (assignment.target_user_id == current_user.id)

Note: The `is_enabled` toggle is per-person (all assignments from the same owner are toggled together) — see UI/UX section. Alternatively, it could be per-assignment for finer control. Start with per-person for simplicity.

### Pydantic Schemas

```
IdentityAgentBindingCreate:
  agent_id: UUID
  trigger_prompt: str
  message_patterns: str | None = None
  session_mode: str = "conversation"
  assigned_user_ids: list[UUID] = []       # users who get access to this agent via identity
  auto_enable: bool = False                # superuser-only

IdentityAgentBindingUpdate:
  trigger_prompt: str | None = None
  message_patterns: str | None = None
  session_mode: str | None = None
  is_active: bool | None = None

IdentityBindingAssignmentPublic:
  id: UUID
  binding_id: UUID
  target_user_id: UUID
  target_user_name: str      # resolved
  target_user_email: str     # resolved
  is_active: bool
  is_enabled: bool
  created_at: datetime

IdentityAgentBindingPublic:
  id: UUID
  agent_id: UUID
  agent_name: str            # resolved
  trigger_prompt: str
  message_patterns: str | None
  session_mode: str
  is_active: bool
  created_at: datetime
  updated_at: datetime
  assignments: list[IdentityBindingAssignmentPublic] = []

IdentityContactPublic:
  owner_id: UUID
  owner_name: str            # identity owner's full name
  owner_email: str           # identity owner's email
  is_enabled: bool           # target user's toggle (per-person)
  agent_count: int           # number of active bindings shared with this user
  assignment_ids: list[UUID] # assignment IDs for bulk toggle
```

### Service Layer

#### `IdentityService` (`backend/app/services/identity/identity_service.py`)

**Binding management:**
- `create_binding(db_session, owner_id, data: IdentityAgentBindingCreate, is_superuser: bool) -> IdentityAgentBindingPublic` — validates agent ownership, unique constraint; creates assignments for `assigned_user_ids` with self-exclusion check; `auto_enable` superuser-only
- `list_bindings(db_session, owner_id) -> list[IdentityAgentBindingPublic]` — all bindings for this owner with their assignments
- `update_binding(db_session, binding_id, owner_id, data: IdentityAgentBindingUpdate) -> IdentityAgentBindingPublic | None`
- `delete_binding(db_session, binding_id, owner_id) -> bool` — cascades assignments
- `get_active_bindings_for_user(db_session, owner_id, target_user_id) -> list[IdentityAgentBinding]` — active bindings accessible to a specific target user (used by Stage 2 routing)

**Assignment management:**
- `assign_users(db_session, binding_id, owner_id, user_ids: list[UUID], auto_enable: bool) -> list[IdentityBindingAssignmentPublic]` — bulk assign users; skips duplicates; validates self-exclusion
- `remove_assignment(db_session, binding_id, owner_id, user_id: UUID) -> bool`

**User-facing (target user):**
- `get_identity_contacts(db_session, user_id) -> list[IdentityContactPublic]` — distinct people who shared agents with this user via identity; grouped by owner
- `toggle_identity_contact(db_session, owner_id, user_id, is_enabled: bool) -> bool` — toggles all assignments from a given owner for this target user (per-person toggle)

#### `IdentityRoutingService` (`backend/app/services/identity/identity_routing_service.py`)

**Stage 2 routing** — selects an agent from the identity owner's bindings **that are accessible to the caller**.

- `route_within_identity(db_session, owner_id, caller_user_id, message) -> IdentityRoutingResult | None`
  1. Get active bindings for identity owner that are assigned to `caller_user_id` (via `get_active_bindings_for_user`)
  2. If no bindings → return None (no agents available for this caller)
  3. If single binding → use directly (no AI needed)
  4. Try pattern matching: check each binding's `message_patterns` against the message using fnmatch (same logic as `AppMCPRoutingService._try_pattern_match`)
  5. If no pattern match → call AI router with binding trigger prompts
  6. Return `IdentityRoutingResult(agent_id, agent_name, session_mode, binding_id, binding_assignment_id, match_method)`

Reuses `route_to_agent()` from `app.agents.app_agent_router` for AI classification (same LLM router, different agent list).

### Integration with Existing Routing

#### Extending `EffectiveRoute`

Add a new field to `EffectiveRoute`:

```python
@dataclass
class EffectiveRoute:
    route_id: uuid.UUID
    agent_id: uuid.UUID          # for identity routes: dummy/None
    agent_name: str              # for identity routes: person's name
    session_mode: str
    trigger_prompt: str
    message_patterns: str | None
    source: str                  # "admin" | "user" | "identity"
    # New fields for identity:
    identity_owner_id: uuid.UUID | None = None
    identity_owner_name: str | None = None
    identity_owner_email: str | None = None
```

#### Extending `get_effective_routes_for_user()`

After collecting assigned routes and personal routes, also collect identity contacts (derived from binding assignments):

```
# Identity contacts — distinct owners who have active+enabled assignments for this user
SELECT DISTINCT owner.id, owner.full_name, owner.email
FROM identity_agent_binding b
JOIN identity_binding_assignment a ON a.binding_id = b.id
JOIN user owner ON owner.id = b.owner_id
WHERE a.target_user_id = :user_id
  AND a.is_active = True        -- owner hasn't disabled this assignment
  AND a.is_enabled = True       -- target user hasn't opted out
  AND b.is_active = True        -- binding itself is active

For each distinct identity owner:
  → EffectiveRoute(
      route_id = UUID(0),           # placeholder — identity uses owner_id for routing
      agent_id = UUID(0),           # placeholder — resolved in Stage 2
      agent_name = owner.full_name,
      trigger_prompt = "Contact {owner.full_name} ({owner.email}). Routes to their available agents.",
      source = "identity",
      identity_owner_id = owner.id,
      ...
    )
```

Note: One EffectiveRoute per **person**, not per binding. Stage 2 handles agent selection.

#### Extending `AppMCPRoutingService.route_message()`

After Stage 1 routing selects a route:
- If `route.source == "identity"` → invoke Stage 2 routing via `IdentityRoutingService.route_within_identity(owner_id, caller_user_id, message)`
- Stage 2 filters bindings to only those accessible to the caller, then selects the best agent
- Stage 2 produces the actual `agent_id`, `session_mode`, `binding_id`, and `binding_assignment_id`

#### Extending `RoutingResult`

```python
@dataclass
class RoutingResult:
    agent_id: uuid.UUID
    agent_name: str
    session_mode: str
    route_id: uuid.UUID
    route_source: str
    match_method: str
    # New fields for identity:
    is_identity: bool = False
    identity_owner_id: uuid.UUID | None = None
    identity_owner_name: str | None = None
    identity_stage2_match_method: str | None = None
    identity_binding_id: uuid.UUID | None = None
    identity_binding_assignment_id: uuid.UUID | None = None
```

#### Extending `AppMCPRequestHandler._resolve_session()`

When `routing_result.is_identity == True`:

1. Look up the agent using `routing_result.agent_id` (resolved by Stage 2)
2. Create session with `user_id = routing_result.identity_owner_id` (NOT the caller)
3. Set `integration_type = "identity_mcp"`
4. Set identity columns: `identity_caller_id`, `identity_binding_id`, `identity_binding_assignment_id`
5. Store display-only data in `session_metadata`: `identity_caller_name`, `identity_owner_name`, `identity_match_method`
6. Return session + agent + is_new_session

When resuming a session by `context_id`:
- For `integration_type = "identity_mcp"`, validate `session.identity_caller_id` matches the current user (not `session.user_id`) — this is an indexed column for fast lookup
- **Binding validity check**: before resuming, verify that `session.identity_binding_id` still exists and `is_active=True`, AND `session.identity_binding_assignment_id` still exists with `is_active=True` and `is_enabled=True`. If either check fails, return error: "This identity connection is no longer active."

#### Response flow

The response from `stream_and_collect_response()` returns text as usual. The `handle_send_message()` method returns the response with the `context_id` to the caller. The caller sees:
```json
{
  "response": "Here is the annual report...",
  "context_id": "session-uuid-in-user-b-space",
  "agent_name": "User B"   // Shows person's name, not the internal agent name
}
```

## Frontend Implementation

### UI Components

#### IdentityServerCard — Settings > Channels tab (new component)

A new card component `IdentityServerCard.tsx` in Settings > Channels tab, placed alongside the existing "MCP Server" card (`AppAgentRoutesCard`). This card is **owner-only** — it represents the current user's personal identity sharing configuration.

**Card structure:**

1. **Card header**: "Identity Server" title
2. **Identity agents list**: Each agent is a list row showing:
   - Agent name, trigger prompt (truncated), session mode icon, active/inactive toggle, edit and delete actions
   - Below the agent name: **user badges/pills** showing which users this agent is shared with (e.g., `[User A] [User C]`), with `×` to remove
   - "Add Agent" button: opens inline form with agent selector dropdown, trigger prompt textarea, message patterns textarea, session mode select, and "Share with Users" multi-select
   - Edit action: opens same inline form pre-filled

This gives the owner a clear picture: **what agent is shared with what users**. Compact list view — each row is one agent with user badges underneath.

#### AppAgentRoutesCard — "Identity Contacts" section (received identities)

Extend the existing `AppAgentRoutesCard.tsx` (Settings > Channels > "MCP Server" card) with a new section **after** "MCP Shared Agents":

**"Identity Contacts" section:**
- Lists identities shared with the current user (people they can address via MCP)
- Each row: person's name, person's email, enable/disable toggle
- "Disabled by owner" label when `is_active == false` — toggle is greyed out
- Similar visual pattern to "MCP Shared Agents" section (same card, new section)
- This is where User A sees which colleagues they can contact via identity routing

#### McpConnectorsCard — Third integration type (shortcut)

Extend the two-step creation dialog in `McpConnectorsCard.tsx` to add a third option:

**Step 1 (type_select):** Three card buttons:
1. "Direct MCP Connector" (existing)
2. "App MCP Server Integration" (existing)
3. "Identity MCP Server Integration" (new)

**Step 2c (form + identity):**
- Form pre-selects the current agent; shows trigger prompt textarea, session mode select, and **"Share with Users" multi-select** (same user picker component)
- If superuser: "Make Active for Users" toggle (auto_enable)
- "Add to Identity" button: creates the identity agent binding + user assignments
- If the agent is already in the identity, show its current binding with edit/remove options and existing user assignments
- Info text: "View your full Identity Server setup in Settings > Channels"

**Card body — Identity indicator:**
- If the current agent is part of the user's identity, show a small label/badge: "Part of Identity Server" with a link to Settings > Channels

#### Session UI — Identity session label

For sessions with `integration_type = "identity_mcp"`:
- Session header shows a label: "Via Identity — initiated by {caller_name}" (resolved from `identity_caller_id` in session metadata)
- Identity owner sees the session in their agent's session list — no special actions, same as app_mcp sessions

#### Queries and Mutations

**Identity bindings (owner view):**
- `["identity-bindings"]` — GET `/api/v1/identity/bindings/` (includes assignments per binding)
- `createIdentityBindingMutation` — POST; invalidates `["identity-bindings"]`
- `updateIdentityBindingMutation` — PUT; invalidates `["identity-bindings"]`
- `deleteIdentityBindingMutation` — DELETE; invalidates `["identity-bindings"]`
- `assignUsersMutation` — POST `/{binding_id}/assignments`; invalidates `["identity-bindings"]`
- `removeAssignmentMutation` — DELETE `/{binding_id}/assignments/{user_id}`; invalidates `["identity-bindings"]`

**Identity summary (owner view — Settings card):**
- `["identity-summary"]` — GET `/api/v1/identity/summary/`

**Identity contacts (target user view):**
- `["identity-contacts"]` — GET `/api/v1/users/me/identity-contacts/`
- `toggleIdentityContactMutation` — PATCH; invalidates `["identity-contacts"]`

### User Flows

#### User B: Adding agent to identity from Integrations tab (primary flow)

1. Opens an agent's Integrations tab > MCP Connectors card > "New"
2. Selects "Identity MCP Server Integration"
3. Writes a trigger prompt for this agent, selects session mode
4. In "Share with Users" section, searches and selects User A and User C
5. Clicks "Add to Identity" — binding + assignments created
6. User A and User C now see User B in their "Identity Contacts" section in the MCP Server card

#### User B: Reviewing identity setup (Settings card)

1. Opens Settings > Channels tab
2. Sees "Identity Server" card showing the full picture: each agent and which users it's shared with
3. Can expand each agent row to see/manage user assignments
4. Can edit trigger prompts, toggle bindings active/inactive, add/remove user assignments
5. This is the aggregate view — the single place to see everything

#### User A: Receiving and Using Identity

1. Opens Settings > Channels tab
2. In the "MCP Server" card, sees "Identity Contacts" section (after "MCP Shared Agents") showing "User B" with enable toggle
3. Enables User B's identity
4. In MCP client, types "Ask User B to prepare the annual report"
5. Stage 1 routes to User B's identity → Stage 2 selects "Annual Report Agent"
6. Response streams back: "Here is the annual report..."
7. Subsequent messages with same `context_id` continue the conversation with the same agent

#### User B: Viewing identity sessions

1. User B sees a new session appear in their agent's session list
2. Session header shows: "Via Identity — initiated by User A"
3. Session behaves like any other agent session — no special owner controls
4. The agent processes future messages from User A automatically

#### Edge Cases

- **No accessible bindings**: If identity owner has no active agent bindings assigned to this caller, Stage 2 returns error: "{owner_name} has no agents available for you right now. Please contact them."
- **Single accessible binding**: Skips AI classification, routes directly
- **All assignments disabled by owner**: Person excluded from caller's effective routes
- **All assignments disabled by target user**: Person excluded from effective routes
- **Agent deleted**: Binding cascade-deleted → assignments cascade-deleted; if no bindings remain accessible, person disappears from effective routes
- **Binding disabled mid-conversation**: Subsequent messages on the `context_id` fail with error: "This identity connection is no longer active." The session remains visible to the identity owner but cannot receive new messages from the caller.
- **Assignment disabled mid-conversation**: Same behavior — connection is no longer active, session is frozen

## Database Migrations

### Migration: `add_identity_mcp_tables.py`

**Tables to create:**

1. `identity_agent_binding`:
   - All fields as defined in Data Models
   - Unique constraint: `(owner_id, agent_id)`
   - Indexes: `owner_id`, `agent_id`

2. `identity_binding_assignment`:
   - All fields as defined in Data Models
   - Unique constraint: `(binding_id, target_user_id)`
   - Indexes: `binding_id`, `target_user_id`

3. `session` table alterations:
   - Add `identity_caller_id` (UUID, FK → user.id ON DELETE SET NULL, nullable, indexed)
   - Add `identity_binding_id` (UUID, FK → identity_agent_binding.id ON DELETE SET NULL, nullable)
   - Add `identity_binding_assignment_id` (UUID, FK → identity_binding_assignment.id ON DELETE SET NULL, nullable)

**Downgrade**: Drop session columns, then drop both tables (assignments first due to FK)

## Error Handling & Edge Cases

| Scenario | Behavior |
|----------|----------|
| No identity bindings active for owner | Error: "{owner_name} has no agents available right now. Please contact them." |
| All assignments for a user disabled by owner | Person excluded from effective routes for that user silently |
| All assignments for a person disabled by target user | Person excluded from effective routes silently |
| Stage 2 AI can't determine agent | Error: "Could not determine which of {owner_name}'s agents to use. Please be more specific." |
| Agent environment not active | Same as existing: auto-activate, pending until ready |
| Agent deleted (binding cascade) | If last binding deleted, identity appears as having no agents |
| Self-sharing attempt | 400: "Cannot share identity with yourself" |
| Non-superuser sets auto_enable | 400: "Only administrators can auto-enable identities for users" |
| Duplicate binding assignment | 409: "This agent is already shared with this user via identity" |
| Duplicate agent binding | 409: "Agent already added to identity" |
| Agent not owned by user | 403: "You can only add your own agents to your identity" |
| Binding disabled mid-conversation | Error on next message: "This identity connection is no longer active" |
| Assignment disabled/removed mid-conversation | Error on next message: "This identity connection is no longer active" |
| Context_id for identity session from different caller | Falls through to new routing (security) |
| Concurrent messages on identity session | Same lock mechanism as existing App MCP |

## UI/UX Considerations

### Identity Server Card (owner view)

- Located in Settings > Channels tab, alongside the existing "MCP Server" card
- Use a "User" or "Contact" icon (e.g., `UserCircle` from Lucide) in the card header
- "Identity Agents" section lists agents with their trigger prompts — make clear these are the agents behind the user's identity
- "Shared With" section shows who can reach this identity — similar UX to sharing users in App MCP routes

### Identity Contacts in MCP Server Card (target user view)

- New section inside the existing `AppAgentRoutesCard`, placed after "MCP Shared Agents"
- Person-centric display: name + email (no agent details — the caller doesn't see internal agents)
- Clear visual distinction between "disabled by me" (toggle off) vs "disabled by owner" (greyed out toggle with label)
- This placement makes sense because identity contacts are part of the user's MCP routing configuration

### McpConnectorsCard (agent-level shortcut)

- Third option in type selector uses a person/identity icon to distinguish from the MCP/server icons
- Quick-add form is minimal — just trigger prompt and session mode
- "Part of Identity Server" badge on agents that are already in the identity
- Link to Settings > Channels for full identity management

### Session Label for Identity Sessions

- Subtle label in session header: "Via Identity — initiated by {caller_name}"
- Helps identity owners distinguish sessions that came through identity vs direct sessions

### Trigger Prompt Guidance

- When adding an agent to identity, provide a hint: "Describe what this agent does so the system can route requests to it. Example: 'Generates annual financial reports and summaries'"
- Consider adding a "Generate" button (like agent handover) that auto-generates trigger prompts from the agent's system prompt

## Integration Points

- **[App MCP Server](../application/app_mcp_server/app_mcp_server.md)** — Identity routes plug into the existing effective routes system and routing pipeline
- **[Agent Sessions](../application/agent_sessions/agent_sessions.md)** — New `integration_type = "identity_mcp"` with cross-user session ownership
- **[AI Functions](../../development/backend/ai_functions_development.md)** — Stage 2 routing reuses `route_to_agent()` for AI classification
- **[Agent Management](../application/agent_management/agent_management.md)** — Identity bindings reference agents by ID; deletion cascades
- **[MCP Connectors Card](../application/mcp_integration/mcp_connector_setup.md)** — Extended with third integration type option

## Future Enhancements (Out of Scope)

1. **Identity profiles / bios**: Users write a description of their identity (role, expertise) that helps Stage 1 routing. Currently the trigger prompt is auto-generated from the person's name and email.

2. **Rate limiting**: Identity owners set per-user or global rate limits on how often their agents can be invoked via identity.

3. **Audit log**: Identity owners see a log of who called their identity, when, which agent was selected, and the message summary.

4. **Identity groups / teams**: Instead of sharing with individual users, share with groups or teams. Integrates with agentic_teams.

5. **Bi-directional identity**: If both User A and User B have identities shared with each other, their agents can address each other by name — enabling multi-person agent workflows.

6. **Identity MCP prompts**: Expose identity contacts as MCP prompts (similar to App MCP prompts for agent routes) so external AI clients can discover available people.

7. **Custom identity trigger prompt**: Instead of the auto-generated "Contact {name}" prompt, identity owners write a custom description of when to route to them.

8. **Notification to identity owner**: When someone initiates a session via identity, the owner gets a real-time notification (activity feed integration).

9. **Approval flow**: Identity owner can require approval before a session is created (especially for sensitive agents).

10. **Message pattern support for identity routes**: Allow identity routes to have fnmatch patterns like regular routes (e.g., `ask john *`, `@john *`).

## Summary Checklist

### Backend Tasks

- [ ] Create `backend/app/models/identity/identity_models.py` with `IdentityAgentBinding` and `IdentityBindingAssignment` tables + Pydantic schemas
- [ ] Create `backend/app/models/identity/__init__.py` and add to `backend/app/models/__init__.py`
- [ ] Create Alembic migration: `add_identity_mcp_tables` with both tables, unique constraints, indexes, and session table columns (`identity_caller_id`, `identity_binding_id`, `identity_binding_assignment_id`)
- [ ] Create `backend/app/services/identity/identity_service.py` — binding CRUD, assignment CRUD, identity contacts query
- [ ] Create `backend/app/services/identity/identity_routing_service.py` — Stage 2 routing logic (filtered by caller's accessible bindings)
- [ ] Create `backend/app/services/identity/__init__.py`
- [ ] Create `backend/app/api/routes/identity.py` — bindings CRUD + assignment endpoints
- [ ] Create `backend/app/api/routes/identity_contacts.py` — user-facing identity contacts endpoints
- [ ] Register new routers in `backend/app/api/main.py`
- [ ] Extend `EffectiveRoute` dataclass with identity fields
- [ ] Extend `get_effective_routes_for_user()` to include identity contacts (derived from binding assignments)
- [ ] Extend `RoutingResult` dataclass with identity fields
- [ ] Extend `AppMCPRoutingService.route_message()` to trigger Stage 2 for identity routes
- [ ] Extend `AppMCPRequestHandler._resolve_session()` for identity sessions (cross-user ownership, metadata)
- [ ] Extend `AppMCPRequestHandler._resolve_session()` context_id resumption for identity sessions (validate `identity_caller_id`)
- [ ] Add binding/assignment validity check on identity session resumption — fail if binding inactive or assignment disabled
- [ ] Mask internal agent name in response — return identity owner's name instead

### Frontend Tasks

- [ ] Create `IdentityServerCard.tsx` in Settings > Channels tab — owner's aggregate view (agent list with user badges per agent)
- [ ] Extend `McpConnectorsCard.tsx`: add "Identity MCP Server Integration" as third option (primary flow — trigger prompt + user assignment per agent)
- [ ] Extend `AppAgentRoutesCard.tsx`: add "Identity Contacts" section after "MCP Shared Agents" with per-person enable/disable toggle
- [ ] Add identity session label in session header for `integration_type = "identity_mcp"` ("Via Identity — initiated by {caller_name}")
- [ ] Add React Query hooks for identity bindings, assignments, summary, and contacts
- [ ] Regenerate API client: `bash scripts/generate-client.sh`

### Testing & Validation

- [ ] Test identity binding CRUD (create, list, update, delete, ownership enforcement)
- [ ] Test binding assignment CRUD (assign users, remove, self-exclusion, unique constraint)
- [ ] Test identity contacts listing (derived from assignments, grouped by owner) and per-person toggle
- [ ] Test Stage 1 routing includes identity contacts in effective routes (one per person, not per binding)
- [ ] Test Stage 2 routing filters by caller: User A sees only bindings assigned to them, not all of owner's bindings
- [ ] Test Stage 2 routing: single accessible binding (no AI), multiple accessible bindings (AI classification)
- [ ] Test cross-user session creation (session owned by identity owner, response to caller)
- [ ] Test context_id resumption for identity sessions (validates caller, not session owner)
- [ ] Test error cases: no bindings, disabled binding, deleted agent, self-sharing
- [ ] Test auto_enable superuser-only restriction
- [ ] Test mid-conversation binding disable: session fails with "connection no longer active"
- [ ] Test mid-conversation assignment disable: same failure behavior
- [ ] Test cascade deletion: agent deleted → binding deleted → assignments deleted
