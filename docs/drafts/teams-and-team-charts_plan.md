# AgenticTeams and TeamCharts — Implementation Plan

## Overview

AgenticTeams and TeamCharts introduce a visual org-chart builder where users define named agentic
teams, add nodes (roles such as developer, designer, project manager), and wire connections
between nodes using the platform's existing agent-handover prompt concept. Each team produces an
interactive chart page with view/edit mode toggle, manual node positioning, and an auto-arrange
button.

The feature is named "AgenticTeams" (rather than just "Teams") to distinguish it from future
user/permission team management. AgenticTeams focus on agent orchestration topology — defining
which agents play which roles and how they hand off work to each other.

Core capabilities:

- CRUD for AgenticTeams (name + icon), managed similarly to Workspaces — user-owned, workspace-independent
- CRUD for TeamNodes (a role label + optional agent link + 2-D position + optional team-lead flag)
- CRUD for TeamConnections (directed edge between two nodes + handover prompt + enabled flag)
- Team-lead node: exactly one node per team can be marked as `is_lead=true`, serving as the entry point when the team is invoked in processes
- Chart page: interactive org-chart rendered in the browser using a canvas/graph library
- View mode: read-only chart, hover over a connection reveals an edit/delete affordance
- Edit mode: create, move, resize, delete nodes and connections inline
- Auto-arrange: one-click top-down org-chart layout computed client-side (team-lead node placed at the top)
- Settings → Interface tab: "Agentic Teams" management card mirroring the existing WorkspaceSettings card
- Sidebar: "Agentic Teams" switcher above the Workspaces switcher

---

## Architecture Overview

```
User
  │
  ├─ Settings → Interface tab → AgenticTeamSettings card (CRUD teams list)
  │
  ├─ Sidebar → AgenticTeamsSwitcher (navigate to active team chart)
  │
  └─ /agentic-teams/{teamId}   ← AgenticTeamChartPage
         │
         ├─ View mode  ← read-only SVG/canvas graph
         │     └─ Hover connection → ConnectionHoverMenu (edit prompt / delete)
         │
         └─ Edit mode (toggle in page header)
               ├─ Drag nodes
               ├─ Draw connections (click source node → click target node)
               ├─ Edit / delete nodes and connections via node/edge menus
               └─ Auto-Arrange button → client-side Sugiyama/Coffman-Graham layout
```

Data flow:

```
Frontend ──→ /api/v1/agentic-teams/         (AgenticTeam CRUD)
         ──→ /api/v1/agentic-teams/{id}/nodes/       (TeamNode CRUD)
         ──→ /api/v1/agentic-teams/{id}/connections/ (TeamConnection CRUD)
             │
             └─ AgenticTeamService / TeamNodeService / TeamConnectionService
                    └─ PostgreSQL
```

Integration with existing systems:

- **WorkspaceSettings** pattern — AgenticTeamSettings mirrors the same Card + Dialog + AlertDialog structure
- **WorkspaceSwitcher** pattern — AgenticTeamsSwitcher mirrors the same SidebarMenuItem + DropdownMenu structure
- **AgentHandoverConfig** schema — `connection_prompt` field mirrors `handover_prompt`; same 2-3 sentence convention
- **UserDashboard** view/edit mode pattern — page header toggle, `isEditMode` local state
- Team-lead concept: one node per team marked `is_lead=true` — the entry point for future process invocation
- **Lucide-react icons** — team icons reuse `WORKSPACE_ICONS` config or a dedicated `TEAM_ICONS` config

---

## Data Models

### Table: `agentic_team`

Purpose: top-level named agentic team owned by a user (workspace-independent).

| Column | Type | Constraints | Default |
|--------|------|-------------|---------|
| `id` | UUID | PK | uuid4 |
| `owner_id` | UUID | FK → `user.id` CASCADE | required |
| `name` | VARCHAR(255) | NOT NULL, min 1 | required |
| `icon` | VARCHAR(50) | nullable | NULL |
| `created_at` | DATETIME | NOT NULL | utcnow |
| `updated_at` | DATETIME | NOT NULL | utcnow |

Indexes:
- `ix_agentic_team_owner_id` on `(owner_id)`

No `user_workspace_id` — agentic teams are explicitly workspace-independent by design (same as dashboards).

No cascade to nodes/connections needed at model level — handled via FK cascade on child tables.

---

### Table: `agentic_team_node`

Purpose: a node in the team org-chart. Currently nodes represent agents, but the schema is
designed for future expansion to other node types (e.g. human-in-the-loop via email).

| Column | Type | Constraints | Default |
|--------|------|-------------|---------|
| `id` | UUID | PK | uuid4 |
| `team_id` | UUID | FK → `agentic_team.id` CASCADE | required |
| `name` | VARCHAR(255) | NOT NULL, min 1 | required |
| `node_type` | VARCHAR(50) | NOT NULL | `"agent"` |
| `is_lead` | BOOLEAN | NOT NULL | `false` |
| `agent_id` | UUID | FK → `agent.id` CASCADE, NOT NULL | required |
| `pos_x` | FLOAT | NOT NULL | 0.0 |
| `pos_y` | FLOAT | NOT NULL | 0.0 |
| `created_at` | DATETIME | NOT NULL | utcnow |
| `updated_at` | DATETIME | NOT NULL | utcnow |

Indexes:
- `ix_agentic_team_node_team_id` on `(team_id)`
- `ix_agentic_team_node_agent_id` on `(agent_id)` — supports lookups when an agent is deleted

`name` is the display name of the node. For agent nodes, this is auto-populated from the agent's
name on creation. For future node types (human, service), this would be the person's name or
service label.

`node_type` is stored as a plain `str` in the DB model and as a `Literal["agent"]` in Pydantic
schemas. Starting with `"agent"` — the type field exists now so future node types (e.g. `"human"`,
`"service"`) require only a migration to widen the Literal, not a schema redesign.

`agent_id` is required for the current MVP (all nodes are agent nodes). Uses `CASCADE` on delete —
deleting an agent removes its node from all teams. The same agent cannot appear twice in the same
team (unique constraint on `(team_id, agent_id)` enforced at service layer).

**Node color**: Agent nodes inherit `ui_color_preset` from their linked agent for card background
color. No separate color column on the node — the frontend resolves color from the agent data.
Future non-agent node types will define their own color rules separately.

`is_lead` marks exactly one node per team as the team lead — the entry point when the team is
addressed in processes. This constraint (at most one lead per team) is enforced at the service
layer: when a node is marked `is_lead=true`, any existing lead node in the same team is
automatically unmarked (`is_lead=false`). This makes it a simple toggle — no multi-step user
action required. A team with zero lead nodes is valid (no entry point defined yet). The lead
node is visually distinguished in the chart (crown/star badge) and placed at the top during
auto-arrange.

---

### Table: `agentic_team_connection`

Purpose: directed edge between two nodes. Stores a connection prompt following the same
handover-prompt convention (2-3 sentence trigger/context/format instruction).

| Column | Type | Constraints | Default |
|--------|------|-------------|---------|
| `id` | UUID | PK | uuid4 |
| `team_id` | UUID | FK → `agentic_team.id` CASCADE | required |
| `source_node_id` | UUID | FK → `agentic_team_node.id` CASCADE | required |
| `target_node_id` | UUID | FK → `agentic_team_node.id` CASCADE | required |
| `connection_prompt` | TEXT | NOT NULL | `""` (empty) |
| `enabled` | BOOLEAN | NOT NULL | `true` |
| `created_at` | DATETIME | NOT NULL | utcnow |
| `updated_at` | DATETIME | NOT NULL | utcnow |

Indexes:
- `ix_agentic_team_connection_team_id` on `(team_id)`
- `ix_agentic_team_connection_source_node_id` on `(source_node_id)`

Cascade behavior:
- `team_id` FK → `agentic_team.id` CASCADE: deleting team removes all connections
- `source_node_id` FK → `agentic_team_node.id` CASCADE: deleting source node removes its outgoing connections
- `target_node_id` FK → `agentic_team_node.id` CASCADE: deleting target node removes its incoming connections

Business constraint: no self-connections (`source_node_id != target_node_id`) enforced at service layer.
No unique constraint on `(source_node_id, target_node_id)` at DB level — enforced at service layer
to return a proper validation error.

---

### Model Schema Classes

File: `backend/app/models/agentic_teams/agentic_team.py`

```
AgenticTeamBase        — name, icon
AgenticTeam            — DB table, owner_id FK, timestamps
AgenticTeamCreate      — inherits AgenticTeamBase
AgenticTeamUpdate      — name|None, icon|None
AgenticTeamPublic      — id, owner_id, name, icon, created_at, updated_at
AgenticTeamsPublic     — data: list[AgenticTeamPublic], count: int

AgenticTeamNodeBase    — name, node_type, is_lead, agent_id, pos_x, pos_y
AgenticTeamNode        — DB table, team_id FK, agent_id FK (required), is_lead, timestamps
AgenticTeamNodeCreate  — agent_id (required), is_lead (default false), pos_x (default 0), pos_y (default 0)
                         name auto-populated from agent on creation
AgenticTeamNodeUpdate  — is_lead|None, pos_x|None, pos_y|None
                         (name and agent_id not updatable — delete and re-add to change agent)
AgenticTeamNodePublic  — id, team_id, agent_id, name, agent_ui_color_preset (resolved from agent),
                         node_type, is_lead, pos_x, pos_y, created_at, updated_at
AgenticTeamNodesPublic — data: list[AgenticTeamNodePublic], count: int

AgenticTeamConnectionBase   — source_node_id, target_node_id, connection_prompt, enabled
AgenticTeamConnection       — DB table, team_id FK, source/target node FKs, timestamps
AgenticTeamConnectionCreate — source_node_id, target_node_id, connection_prompt (default ""), enabled (default true)
AgenticTeamConnectionUpdate — connection_prompt|None, enabled|None
AgenticTeamConnectionPublic — id, team_id, source_node_id, target_node_id,
                              source_node_name (resolved), target_node_name (resolved),
                              connection_prompt, enabled, created_at, updated_at
AgenticTeamConnectionsPublic — data: list[AgenticTeamConnectionPublic], count: int
```

`source_node_name`/`target_node_name` are resolved in the service layer
before constructing the public response — same pattern used by `HandoverConfigPublic.target_agent_name`.

---

## Security Architecture

### Access Control

All team, node, and connection endpoints require `CurrentUser` (JWT authentication).

Ownership verification (owner-only, no superuser bypass):
- AgenticTeams: `team.owner_id == current_user.id`
- Nodes: resolved via `node.team_id → agentic_team.owner_id == current_user.id`
- Connections: resolved via `connection.team_id → agentic_team.owner_id == current_user.id`

Superusers do NOT bypass ownership checks for agentic teams. Only the owner can manage their
teams, nodes, and connections. This is intentional — agentic teams represent personal agent
orchestration configurations, and future user-based team/permission features will have their
own separate access model.

No encryption required — teams contain no sensitive data. `connection_prompt` is user-authored
text, not a credential.

### Input Validation

- `name`: min length 1, max 255 — validated in Pydantic `AgenticTeamBase`
- `icon`: max 50 chars — validated in `AgenticTeamBase`
- `name` (node): min length 1, max 255 — validated in `AgenticTeamNodeBase`
- `connection_prompt`: max 2000 chars (same limit as dashboard prompt actions) — validated in `AgenticTeamConnectionBase`
- Self-connection guard (`source_node_id != target_node_id`) at service layer
- Duplicate connection guard at service layer

### Rate Limiting

No special rate limiting beyond standard platform patterns. No external calls, no AI generation
(optional future enhancement).

---

## Backend Implementation

### API Routes

File: `backend/app/api/routes/agentic_teams.py`

Register in `backend/app/api/main.py` under prefix `/api/v1/agentic-teams` with tag `agentic-teams`.

#### AgenticTeam Endpoints

| Method | Path | Description | Auth |
|--------|------|-------------|------|
| `GET` | `/api/v1/agentic-teams/` | List user's agentic teams (`skip`, `limit`) | CurrentUser |
| `POST` | `/api/v1/agentic-teams/` | Create agentic team | CurrentUser |
| `GET` | `/api/v1/agentic-teams/{team_id}` | Get single agentic team | CurrentUser + ownership |
| `PUT` | `/api/v1/agentic-teams/{team_id}` | Update team name/icon | CurrentUser + ownership |
| `DELETE` | `/api/v1/agentic-teams/{team_id}` | Delete team (cascades nodes+connections) | CurrentUser + ownership |

**GET /api/v1/agentic-teams/** response: `AgenticTeamsPublic`
**POST /api/v1/agentic-teams/** body: `AgenticTeamCreate`, response: `AgenticTeamPublic`
**PUT /api/v1/agentic-teams/{team_id}** body: `AgenticTeamUpdate`, response: `AgenticTeamPublic`
**DELETE** response: `{"message": "Agentic team deleted"}`

#### Node Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/v1/agentic-teams/{team_id}/nodes/` | List nodes for team |
| `POST` | `/api/v1/agentic-teams/{team_id}/nodes/` | Create node |
| `GET` | `/api/v1/agentic-teams/{team_id}/nodes/{node_id}` | Get single node |
| `PUT` | `/api/v1/agentic-teams/{team_id}/nodes/{node_id}` | Update node (label, pos, agent_id, is_lead) |
| `DELETE` | `/api/v1/agentic-teams/{team_id}/nodes/{node_id}` | Delete node (cascades connections) |

**POST body**: `AgenticTeamNodeCreate`, response: `AgenticTeamNodePublic`
**PUT body**: `AgenticTeamNodeUpdate`, response: `AgenticTeamNodePublic`

When `is_lead=true` is set on create or update, the service layer automatically unmarks the
previous lead node (if any) in the same team before marking the new one.

#### Connection Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/v1/agentic-teams/{team_id}/connections/` | List connections for team |
| `POST` | `/api/v1/agentic-teams/{team_id}/connections/` | Create connection |
| `GET` | `/api/v1/agentic-teams/{team_id}/connections/{conn_id}` | Get single connection |
| `PUT` | `/api/v1/agentic-teams/{team_id}/connections/{conn_id}` | Update prompt or enabled |
| `DELETE` | `/api/v1/agentic-teams/{team_id}/connections/{conn_id}` | Delete connection |

**POST body**: `AgenticTeamConnectionCreate`, response: `AgenticTeamConnectionPublic`
**PUT body**: `AgenticTeamConnectionUpdate`, response: `AgenticTeamConnectionPublic`

#### Bulk Endpoint (for performance)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/v1/agentic-teams/{team_id}/chart` | Return nodes + connections together |

Response schema `AgenticTeamChartPublic`:
```
AgenticTeamChartPublic:
  team: AgenticTeamPublic
  nodes: list[AgenticTeamNodePublic]
  connections: list[AgenticTeamConnectionPublic]
```

This endpoint is the primary fetch for the chart page — one round-trip instead of three.

#### Bulk Node Position Update

| Method | Path | Description |
|--------|------|-------------|
| `PUT` | `/api/v1/agentic-teams/{team_id}/nodes/positions` | Bulk update node positions |

Body: `list[AgenticTeamNodePositionUpdate]` where each entry is `{id: UUID, pos_x: float, pos_y: float}`.
Used after a drag-reposition or an auto-arrange to persist the entire layout in one call (same
pattern as `PUT /api/v1/dashboards/{id}/blocks/layout` for grid layout).

---

### Service Layer

File: `backend/app/services/agentic_teams/agentic_team_service.py` — `AgenticTeamService`

Key methods:

- `get_team(session, team_id, user_id) -> AgenticTeam` — fetch + ownership check; raises 404/403
- `list_teams(session, user_id, skip, limit) -> tuple[list[AgenticTeam], int]`
- `create_team(session, user_id, data: AgenticTeamCreate) -> AgenticTeam`
- `update_team(session, team_id, user_id, data: AgenticTeamUpdate) -> AgenticTeam`
- `delete_team(session, team_id, user_id) -> None`

File: `backend/app/services/agentic_teams/agentic_team_node_service.py` — `AgenticTeamNodeService`

Key methods:

- `get_node(session, team_id, node_id, user_id) -> AgenticTeamNode` — verifies node belongs to team, team owned by user
- `list_nodes(session, team_id, user_id) -> list[AgenticTeamNode]`
- `create_node(session, team_id, user_id, data: AgenticTeamNodeCreate) -> AgenticTeamNode`
  - Verify agent ownership (`agent.owner_id == user_id`)
  - Prevent duplicate agent in same team (unique `(team_id, agent_id)` enforced at service layer)
  - Auto-populate `name` from agent's name
  - If `is_lead=true`: unmark existing lead node in team (set `is_lead=false`)
- `update_node(session, team_id, node_id, user_id, data: AgenticTeamNodeUpdate) -> AgenticTeamNode`
  - Only `is_lead`, `pos_x`, `pos_y` are updatable (agent cannot be changed — delete and re-add)
  - If `is_lead` changed to `true`: unmark existing lead node in team
- `delete_node(session, team_id, node_id, user_id) -> None` — DB cascade handles connections
  - Note: if the deleted node was `is_lead=true`, the team has no lead until one is re-assigned
- `bulk_update_positions(session, team_id, user_id, positions: list[AgenticTeamNodePositionUpdate]) -> list[AgenticTeamNode]`
  - Validates all node IDs belong to the team before batch updating

File: `backend/app/services/agentic_teams/agentic_team_connection_service.py` — `AgenticTeamConnectionService`

Key methods:

- `get_connection(session, team_id, conn_id, user_id) -> AgenticTeamConnection`
- `list_connections(session, team_id, user_id) -> list[AgenticTeamConnection]`
- `create_connection(session, team_id, user_id, data: AgenticTeamConnectionCreate) -> AgenticTeamConnection`
  - Validates source and target nodes both belong to team
  - Prevents self-connection
  - Prevents duplicate `(source_node_id, target_node_id)` pair
- `update_connection(session, team_id, conn_id, user_id, data: AgenticTeamConnectionUpdate) -> AgenticTeamConnection`
- `delete_connection(session, team_id, conn_id, user_id) -> None`

---

## Frontend Implementation

### Routes

| Route file | Path | Description |
|------------|------|-------------|
| `frontend/src/routes/_layout/agentic-teams.tsx` | `/agentic-teams` | AgenticTeams index (list + manage) — redirect to first team or empty state |
| `frontend/src/routes/_layout/agentic-teams/$teamId.tsx` | `/agentic-teams/$teamId` | AgenticTeamChartPage — main chart view/edit |

Both are protected routes (`_layout/` prefix).

---

### Settings — Interface Tab

File: `frontend/src/components/UserSettings/AgenticTeamSettings.tsx`

This component mirrors `WorkspaceSettings.tsx` exactly:

- `Card` with title "Agentic Teams" and description
- `Button` "New Agentic Team" opens `AgenticTeamFormDialog`
- `Table` with rows: icon | name | edit button | delete button
- `AgenticTeamFormDialog` (create + edit mode) with `Input` for name and `IconSelector` for icon (can reuse `WORKSPACE_ICONS` or a new `AGENTIC_TEAM_ICONS` config)
- `AlertDialog` for delete confirmation

Modifications to `frontend/src/routes/_layout/settings.tsx`:

- Import `AgenticTeamSettings` and add the `<AgenticTeamSettings />` card within the existing "Interface" tab content, alongside the existing Workspaces card (not as a separate tab).

Query keys: `["agenticTeams"]`

---

### Sidebar — AgenticTeams Switcher

File: `frontend/src/components/AgenticTeams/AgenticTeamsSwitcher.tsx`

Mirrors `SidebarWorkspaceSwitcher.tsx`:

- `SidebarMenuItem` wrapping a `DropdownMenu`
- Button label: active team name, or "Agentic Teams" when not on a `/agentic-teams/*` route
- Dropdown items: list of all user agentic teams (no limit, same as dashboards)
- Each item navigates to `/agentic-teams/{team.id}`
- Active team highlighted with `bg-accent` + `Check` icon
- Separator + "Manage Agentic Teams" → `/agentic-teams` (index page)
- Uses `Users` or `Network` lucide icon for the teams menu button

No "active team" state stored in localStorage. Active team is determined from the current route
param (`$teamId`) — if the current route is `/agentic-teams/:id`, that team is active in the switcher.

Modifications to sidebar layout: Add `<AgenticTeamsSwitcher />` above `<SidebarWorkspaceSwitcher />` in
the sidebar footer (same file where `SidebarWorkspaceSwitcher` is currently rendered — likely
`frontend/src/routes/__root.tsx` or the sidebar layout component). Locate this in the actual
sidebar assembly file during implementation.

---

### AgenticTeamChartPage

File: `frontend/src/routes/_layout/agentic-teams/$teamId.tsx`

**Page header** (via `usePageHeader` hook, same as dashboard page):

```
[Network icon] [Team Name]       [Auto-Arrange]  [Edit Layout / Lock Layout]  [⋮]
                                                                               ├─ Edit Team
                                                                               └─ Delete Team
```

"Edit Layout" button toggles `isEditMode` local state.
"Auto-Arrange" button is always visible (triggers layout recalculation).
"⋮" dropdown contains Edit Team (opens `AgenticTeamFormDialog`) and Delete Team (with confirmation).

**Main body**: renders `<AgenticTeamChart />` component.

---

### AgenticTeamChart Component

File: `frontend/src/components/AgenticTeams/AgenticTeamChart.tsx`

This is the core chart rendering component. It receives `nodes`, `connections`, `isEditMode`,
and mutation callbacks as props.

**Technology choice**: Use a lightweight React-based graph library. Recommended: **React Flow**
(`@xyflow/react`, MIT license). It provides:
- Node drag-and-drop with position persistence
- Custom node and edge components
- Edge click detection for the hover menu
- Programmatic layout (Dagre/ELK integration for auto-arrange)
- No canvas — pure SVG/HTML, easy to style with Tailwind

If React Flow is not acceptable, an alternative is to render an SVG manually with `foreignObject`
for node labels, tracking positions in component state.

**Node rendering** (`AgenticTeamChartNode` custom node component):
- Rounded card with robot icon (`Bot`), agent name, card background color from agent's `ui_color_preset`
- **Team-lead badge**: if `is_lead=true`, node shows a `Crown` icon badge in the top-right corner
- In edit mode: shows drag cursor; right-click or kebab menu → Edit / Delete / Set as Lead (toggle)
- "Add connection" affordance in edit mode: clicking a source node starts connection draw mode,
  then clicking a target node creates the connection

**Connection rendering** (`AgenticTeamChartEdge` custom edge component):
- Arrow SVG path (directed)
- `enabled=false` connections rendered as dashed line
- On hover in edit mode: show a small circle button at the midpoint — clicking it opens `ConnectionHoverMenu`
- In view mode: no interactive affordances on connections (read-only)

**ConnectionHoverMenu** (edit mode only):
- Small `Popover` or positioned `div` anchored to the edge midpoint
- Contains: "Edit Connection" button (opens `ConnectionEditDialog`) and "Delete" button (with inline confirmation)

**ConnectionEditDialog**:
- `Dialog` with `Textarea` for `connection_prompt` (follows `AgentHandovers.tsx` pattern)
- Enable/disable toggle (`Switch`)
- "Save" and "Cancel" buttons

---

### Auto-Arrange

Client-side layout computation, no backend call needed.

Algorithm: **Sugiyama-style top-down layered layout** using the `dagre` npm package (or
`@dagrejs/dagre`, which React Flow uses internally). Steps:

1. Build a directed graph from current nodes and connections
2. Run Dagre layout with `rankdir: "TB"` (top to bottom)
3. Extract computed `x`, `y` positions for each node
4. Update local node positions in React state
5. Call `PUT /api/v1/teams/{team_id}/nodes/positions` to persist

The "Auto-Arrange" button is available in both view mode and edit mode — it is a non-destructive
layout operation that only changes positions.

---

### State Management

**Query keys:**

- `["agenticTeams"]` — team list for sidebar and settings
- `["agenticTeamChart", teamId]` — bulk chart fetch (nodes + connections) for chart page
- Query data from `GET /api/v1/agentic-teams/{id}/chart` populates both nodes and connections

**Mutations (in `AgenticTeamChartPage` or passed to `AgenticTeamChart`):**

- `createNodeMutation` — optimistic add, invalidate `["agenticTeamChart", teamId]`
- `updateNodeMutation` — optimistic update
- `deleteNodeMutation`
- `bulkUpdatePositionsMutation` — debounced 300ms after drag stop (same pattern as dashboard layout save)
- `createConnectionMutation`
- `updateConnectionMutation`
- `deleteConnectionMutation`

AgenticTeam CRUD mutations in `AgenticTeamSettings`:
- `createTeamMutation` — invalidates `["agenticTeams"]`
- `updateTeamMutation`
- `deleteTeamMutation` — on success, if current route is `/agentic-teams/{deleted_id}`, redirect to `/agentic-teams`

---

### User Flows

**Creating a team:**
1. Settings → Interface tab → Agentic Teams card → "New Agentic Team"
2. Enter name, pick icon, submit
3. Team appears in the list and in the sidebar Agentic Teams switcher

**Opening a team chart:**
1. Click team name in sidebar Agentic Teams switcher → navigates to `/agentic-teams/{id}`
2. Chart page loads, calls `GET /api/v1/agentic-teams/{id}/chart`
3. Nodes and connections rendered in view mode

**Adding a node (edit mode):**
1. Click "Edit Layout" in page header
2. Click "+ Add Node" button (appears in header in edit mode, similar to "+ Add Block")
3. `AddNodeDialog` opens: agent selector (required dropdown — shows only agents not already in team), "Set as team lead" checkbox; name auto-populated from agent
4. Node appears at a default position (center of viewport); user drags to desired position
5. Positions auto-save after 300ms debounce

**Setting a team lead:**
1. In edit mode, right-click or kebab menu on a node → "Set as Lead"
2. Node is marked `is_lead=true`; previous lead (if any) is automatically unmarked
3. Lead node shows a crown/star badge in the top-right corner
4. Auto-arrange places the lead node at the top of the hierarchy

**Adding a connection (edit mode):**
1. In edit mode, hover a source node — an "add connection" handle appears
2. Click handle, then click target node — connection created with empty prompt
3. `ConnectionEditDialog` opens immediately to fill in `connection_prompt`
4. Alternatively: click "Add Connection" in a node's context menu (dropdown)

**Editing a connection prompt (edit mode):**
1. In edit mode, hover over a connection line — a circle button appears at midpoint
2. Click circle → `ConnectionHoverMenu` opens (Popover)
3. Click "Edit Connection" → `ConnectionEditDialog` opens
4. Edit prompt text + enabled toggle, save

**Auto-arranging:**
1. Click "Auto-Arrange" button (top-right of chart body, always visible)
2. Dagre layout runs client-side, node positions update immediately
3. Lead node (if any) is placed at the top of the hierarchy as the root
4. Bulk position update sent to backend (debounced 300ms)

---

## Database Migrations

Migration file naming: `add_agentic_teams_tables.py`

Migration: `backend/app/alembic/versions/<hash>_add_agentic_teams_tables.py`

Operations in upgrade():

1. Create `agentic_team` table with columns as specified; create `ix_agentic_team_owner_id` index
2. Create `agentic_team_node` table (including `is_lead` BOOLEAN NOT NULL DEFAULT false); create `ix_agentic_team_node_team_id` and `ix_agentic_team_node_agent_id` indexes
3. Create `agentic_team_connection` table; create `ix_agentic_team_connection_team_id` and `ix_agentic_team_connection_source_node_id` indexes

Foreign key details:
- `agentic_team.owner_id` → `user.id` ON DELETE CASCADE
- `agentic_team_node.team_id` → `agentic_team.id` ON DELETE CASCADE
- `agentic_team_node.agent_id` → `agent.id` ON DELETE CASCADE (required)
- `agentic_team_connection.team_id` → `agentic_team.id` ON DELETE CASCADE
- `agentic_team_connection.source_node_id` → `agentic_team_node.id` ON DELETE CASCADE
- `agentic_team_connection.target_node_id` → `agentic_team_node.id` ON DELETE CASCADE

Downgrade(): drop `agentic_team_connection`, `agentic_team_node`, `agentic_team` tables in reverse order.

No data migration needed — all new tables.

---

## Error Handling and Edge Cases

| Scenario | Handling |
|----------|----------|
| Team not found or not owned | 404 HTTPException from `AgenticTeamService.get_team()` |
| Node does not belong to team | 404 from `AgenticTeamNodeService.get_node()` |
| Self-connection attempt | 400 `"Source and target nodes must be different"` from service |
| Duplicate connection | 409 `"A connection between these nodes already exists"` |
| Agent linked to node is deleted | FK CASCADE — node is removed from all teams; connections involving that node are also removed |
| Same agent added twice to team | 409 `"This agent is already in the team"` from service |
| Team deleted while chart is open | 404 on next chart fetch — frontend shows error state and redirects to `/agentic-teams` |
| Setting `is_lead` on a node | Service unmarks previous lead atomically; no error if no prior lead exists |
| Deleting lead node | Team has no lead until user re-assigns; valid state (no entry point defined yet) |
| Node deleted while connection is being drawn | Connection creation fails with 404 — frontend shows toast error and resets drawing state |
| `connection_prompt` too long | Pydantic validation returns 422 with field error |
| Max teams per user | No hard cap defined in MVP. Consider adding a 20-team soft limit (same as 20 blocks/dashboard) in a follow-up. |
| Bulk position update with invalid node IDs | 400 — service validates all IDs belong to team before writing any |
| Auto-arrange with disconnected graph | Dagre handles disconnected components; each subgraph arranged independently |

---

## UI/UX Considerations

**View mode chart:**
- Background: subtle dot-grid pattern (standard in flowchart UIs, can be provided by React Flow)
- Nodes: rounded cards with robot icon (`Bot`), agent name in bold, card background color from agent's `ui_color_preset`
- Lead node: distinguished by a `Crown` (lucide) icon badge in the top-right corner + subtle gold/accent border
- Connection arrows: `text-muted-foreground` color; dashed for `enabled=false`
- Connection hover zone (edit mode only): invisible thick overlay path (e.g. 12px stroke) over the visible thin arrow (2px) to make hover easy to trigger
- Connection hover circle button (edit mode only): small `Button variant="secondary"` pinned at edge midpoint, only visible on hover of the thick overlay

**Edit mode:**
- Page header switches "Edit Layout" button label to "Lock Layout" (same as dashboard pattern)
- Node cards get a subtle dashed border in edit mode to indicate editability
- Cursor changes to `move` on node hover

**Empty state (no nodes):**
- Center of chart area shows: `"No nodes yet. Switch to edit mode to add team members."` with an icon

**Auto-Arrange button:**
- Positioned as a small icon button in the top-right corner of the chart body area (not in the page header), overlaid on the chart canvas
- Tooltip: `"Auto-arrange nodes"`
- Uses `Network` or `LayoutGrid` lucide icon

**Teams settings card:**
- Mirror: same layout as `WorkspaceSettings.tsx` — `Card` max-w-lg, table of items with edit/delete buttons

**Sidebar AgenticTeamsSwitcher:**
- Icon: `Users` or `Network` from lucide-react
- Tooltip: `"Agentic Teams"`
- Positioned in the same `SidebarGroup` as `SidebarWorkspaceSwitcher`, above it

---

## Integration Points

### API Client Regeneration

After backend changes, run:
```
bash scripts/generate-client.sh
```

This regenerates `frontend/src/client/sdk.gen.ts`, `frontend/src/client/types.gen.ts`, and
`frontend/src/client/schemas.gen.ts`. The new `TeamsService` will be auto-generated from the
`teams` tag on the OpenAPI routes.

### Router Registration

Add to `backend/app/api/main.py`:
```python
from app.api.routes import agentic_teams
api_router.include_router(agentic_teams.router, prefix="/agentic-teams", tags=["agentic-teams"])
```

### Sidebar Layout File

Locate the file that assembles `<SidebarWorkspaceSwitcher />` in the sidebar footer and add
`<AgenticTeamsSwitcher />` directly above it. Based on the codebase structure, this is likely in
`frontend/src/routes/__root.tsx` or a dedicated sidebar assembly component. Verify during
implementation.

### Settings Tab

Modify `frontend/src/routes/_layout/settings.tsx` to import `AgenticTeamSettings` and add the tab.

### Agent Deletion Side Effect

When an agent is deleted, `agentic_team_node` rows with that `agent_id` are cascade-deleted via FK
`CASCADE`. Connections involving those nodes are also cascade-deleted. No special handling needed
in `agent_service.py` — the DB constraints handle it automatically.

### No Workspace Scoping

AgenticTeams have no `user_workspace_id` column. The AgenticTeams switcher in the sidebar never passes a
workspace filter. This is consistent with dashboards (`user_dashboard` also has no workspace
column) and is by design — agentic teams represent agent orchestration topology independent of workflow
workspaces.

---

## npm Package Dependencies

The following packages need to be added to `frontend/package.json`:

- `@xyflow/react` — React Flow for the chart canvas (MIT license)
- `@dagrejs/dagre` — Dagre layout algorithm for auto-arrange (MIT license)

Both are production dependencies. Verify license compatibility before adding.

---

## Future Enhancements (Out of Scope for MVP)

See `docs/drafts/agentic-teams-product-vision.md` for the full product vision and long-term
roadmap. Key items for reference:

### Near-term (post-MVP)
- **Team execution engine** — invoke a team on a task; lead node receives, delegates via connections, results flow back
- **Human-in-the-loop nodes** — `node_type: "human"` with email/Slack/WhatsApp channels, async handling, timeouts
- **Conditional routing** — connections with conditions ("if output contains X, go to node A")
- **Transform prompts** — pre-process messages before handover (summarize, translate, extract)
- **Team-lead process invocation** — `is_lead` node as entry point for automated processes
- **AI-generated connection prompts** — similar to `AgentHandovers` "Generate" button

### Mid-term
- **Execution trace & observability** — real-time visualization of active nodes, message flow, time per node
- **Team templates & marketplace** — pre-built structures (IT Support, Content Pipeline, Customer Onboarding)
- **Shared team context** — project brief / scratchpad accessible to all nodes during execution
- **Approval gates** — human nodes that approve/reject rather than produce work
- **Cost tracking** — per-team, per-node token/API cost analytics
- **Feedback connections** — backward edges with loop limits for review cycles

### Long-term
- **Multi-tenancy & collaboration** — shared agentic teams across org users with role-based access
- **Team-of-teams composition** — a node in one team can be another entire team
- **Self-optimizing teams** — suggest prompt improvements based on execution history
- **Proactive teams** — monitor triggers (email, ticket, schedule) and self-activate
- **Internationalization** — multilingual handovers, timezone-aware human nodes, locale-adapted templates
- **Guardrails & governance** — PII redaction rules per connection, budget limits, audit trails

### Implementation considerations for MVP
- Keep `connection_prompt` as TEXT but design with awareness that it may evolve into structured
  JSON `connection_config` in a future migration
- The bulk chart endpoint (`GET /chart`) pattern becomes the execution engine's "load team
  definition" call — keep the response shape clean
- Node position data is UI-only metadata — the execution engine will use only graph topology
- `node_type` field is the extensibility seam — all future node types flow through it

---

## Summary Checklist

### Backend Tasks

- [ ] Create `backend/app/models/agentic_teams/agentic_team.py` with `AgenticTeam`, `AgenticTeamNode`, `AgenticTeamConnection` DB models and all schema variants (`Base`, `Create`, `Update`, `Public`, `Public` lists, `AgenticTeamChartPublic`, `AgenticTeamNodePositionUpdate`)
- [ ] Create `backend/app/services/agentic_teams/agentic_team_service.py` with `AgenticTeamService` (list, create, get, update, delete) and owner-only enforcement (no superuser bypass)
- [ ] Create `backend/app/services/agentic_teams/agentic_team_node_service.py` with `AgenticTeamNodeService` including `bulk_update_positions`, agent ownership validation, and `is_lead` toggle logic (auto-unmark previous lead)
- [ ] Create `backend/app/services/agentic_teams/agentic_team_connection_service.py` with `AgenticTeamConnectionService` including self-connection and duplicate-connection guards
- [ ] Create `backend/app/api/routes/agentic_teams.py` with all AgenticTeam, AgenticTeamNode, AgenticTeamConnection, and bulk endpoints (list + chart + positions) using `SessionDep` and `CurrentUser`
- [ ] Register `agentic-teams` router in `backend/app/api/main.py`
- [ ] Generate Alembic migration `add_agentic_teams_tables.py` creating `agentic_team`, `agentic_team_node`, `agentic_team_connection` tables with all FK constraints and indexes
- [ ] Review and apply migration; verify downgrade works

### Frontend Tasks

- [ ] Install `@xyflow/react` and `@dagrejs/dagre` npm packages
- [ ] Create `frontend/src/components/AgenticTeams/AgenticTeamSettings.tsx` — mirrors `WorkspaceSettings.tsx` with "Agentic Teams" card, `AgenticTeamFormDialog`, delete `AlertDialog`; uses `["agenticTeams"]` query key
- [ ] Add `<AgenticTeamSettings />` card to the Interface tab in `frontend/src/routes/_layout/settings.tsx` alongside the Workspaces card
- [ ] Regenerate API client (`bash scripts/generate-client.sh`) after backend routes are implemented
- [ ] Create `frontend/src/components/AgenticTeams/AgenticTeamsSwitcher.tsx` — mirrors `SidebarWorkspaceSwitcher.tsx`; navigates to `/agentic-teams/{id}`; active state from current route param
- [ ] Add `<AgenticTeamsSwitcher />` above `<SidebarWorkspaceSwitcher />` in the sidebar assembly file
- [ ] Create `frontend/src/routes/_layout/agentic-teams.tsx` — agentic teams index page; shows empty state or redirects to first team
- [ ] Create `frontend/src/routes/_layout/agentic-teams/$teamId.tsx` — `AgenticTeamChartPage`; calls `GET /api/v1/agentic-teams/{id}/chart`; manages `isEditMode` state; injects header content via `usePageHeader`
- [ ] Create `frontend/src/components/AgenticTeams/AgenticTeamChart.tsx` — React Flow chart with custom nodes and edges; accepts `nodes`, `connections`, `isEditMode`, mutation callbacks as props
- [ ] Create `frontend/src/components/AgenticTeams/AgenticTeamChartNode.tsx` — custom React Flow node: agent name, robot icon, card color from agent's `ui_color_preset`, lead badge (crown icon), edit mode kebab menu with "Set as Lead" option
- [ ] Create `frontend/src/components/AgenticTeams/AgenticTeamChartEdge.tsx` — custom React Flow edge: directed arrow, dashed for disabled, hover circle button in view mode
- [ ] Create `frontend/src/components/AgenticTeams/ConnectionHoverMenu.tsx` — `Popover` anchored to edge midpoint with Edit/Delete actions
- [ ] Create `frontend/src/components/AgenticTeams/ConnectionEditDialog.tsx` — `Dialog` with `Textarea` for `connection_prompt` and `Switch` for `enabled`
- [ ] Create `frontend/src/components/AgenticTeams/AddNodeDialog.tsx` — `Dialog` with required agent selector (dropdown of user's agents, excluding already-added ones), "Set as team lead" checkbox; name auto-populated from selected agent
- [ ] Implement auto-arrange using Dagre in `AgenticTeamChart.tsx`; wire to "Auto-Arrange" button; lead node at top; call bulk positions endpoint after layout
- [ ] Implement debounced bulk position save (300ms) after drag-stop in `AgenticTeamChart.tsx`

### Testing and Validation Tasks

- [ ] Verify AgenticTeam CRUD: create, read, update, delete via API; confirm owner-only enforcement (other user AND superuser cannot read/modify)
- [ ] Verify AgenticTeamNode CRUD: nodes belong to team; deleting node cascades connections; deleting agent cascades node removal; duplicate agent in same team returns 409; `is_lead` toggle unmarks previous lead
- [ ] Verify AgenticTeamConnection CRUD: self-connection returns 400; duplicate connection returns 409; deleting source or target node removes connection
- [ ] Verify `GET /api/v1/agentic-teams/{id}/chart` returns combined `team`, `nodes`, `connections` with resolved node names
- [ ] Verify `PUT /api/v1/agentic-teams/{id}/nodes/positions` bulk update persists positions; returns 400 for foreign node IDs
- [ ] Verify frontend chart page loads, renders nodes and connections correctly
- [ ] Verify lead node displays crown badge and is placed at top during auto-arrange
- [ ] Verify drag reposition in edit mode saves positions after debounce
- [ ] Verify connection hover menu appears on hover and opens edit dialog
- [ ] Verify auto-arrange produces valid top-down layout with lead at root and saves positions
- [ ] Verify AgenticTeamSettings card creates, edits, and deletes teams; list refreshes after each mutation
- [ ] Verify AgenticTeamsSwitcher highlights active team and navigates correctly
- [ ] Verify sidebar shows AgenticTeamsSwitcher above WorkspaceSwitcher
- [ ] Verify settings Interface tab shows Agentic Teams card alongside Workspaces card
- [ ] Verify deleting a team while on its chart page redirects to `/agentic-teams`
