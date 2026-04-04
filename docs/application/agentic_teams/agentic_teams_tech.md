# AgenticTeams — Technical Reference

## Backend

### Models

File: `backend/app/models/agentic_teams/agentic_team.py`

#### DB Tables

| Class | Table | Purpose |
|-------|-------|---------|
| `AgenticTeam` | `agentic_team` | Top-level team (owner_id FK → user.id CASCADE) |
| `AgenticTeamNode` | `agentic_team_node` | Team node (team_id FK CASCADE, agent_id FK CASCADE) |
| `AgenticTeamConnection` | `agentic_team_connection` | Directed edge (team_id, source_node_id, target_node_id FKs CASCADE) |

#### Schema Classes

```
AgenticTeamBase        — name, icon
AgenticTeam            — DB table, owner_id FK, timestamps
AgenticTeamCreate      — inherits AgenticTeamBase
AgenticTeamUpdate      — name|None, icon|None
AgenticTeamPublic      — id, owner_id, name, icon, created_at, updated_at
AgenticTeamsPublic     — data: list[AgenticTeamPublic], count: int

AgenticTeamNodeBase    — name, node_type, is_lead, agent_id, pos_x, pos_y
AgenticTeamNode        — DB table, team_id FK, agent_id FK (required), timestamps
AgenticTeamNodeCreate  — agent_id (required), is_lead (default false), pos_x, pos_y
                         name is NOT in Create — auto-populated from agent.name
AgenticTeamNodeUpdate  — is_lead|None, pos_x|None, pos_y|None only
AgenticTeamNodePublic  — id, team_id, agent_id, name, agent_ui_color_preset (resolved),
                         node_type, is_lead, pos_x, pos_y, created_at, updated_at
AgenticTeamNodesPublic — data: list[AgenticTeamNodePublic], count: int
AgenticTeamNodePositionUpdate — id: UUID, pos_x: float, pos_y: float

AgenticTeamConnectionBase   — source_node_id, target_node_id, connection_prompt, enabled
AgenticTeamConnection       — DB table, team_id FK, source/target node FKs, timestamps
AgenticTeamConnectionCreate — inherits AgenticTeamConnectionBase
AgenticTeamConnectionUpdate — connection_prompt|None, enabled|None
AgenticTeamConnectionPublic — id, team_id, source_node_id, target_node_id,
                              source_node_name (resolved), target_node_name (resolved),
                              connection_prompt, enabled, created_at, updated_at
AgenticTeamConnectionsPublic — data: list[AgenticTeamConnectionPublic], count: int

AgenticTeamChartPublic — team: AgenticTeamPublic, nodes: list[AgenticTeamNodePublic],
                         connections: list[AgenticTeamConnectionPublic]
```

### Services

#### `AgenticTeamService` (`backend/app/services/agentic_teams/agentic_team_service.py`)

- `get_team(session, team_id, user_id) -> AgenticTeam` — Raises 404 for both not-found and wrong-owner
- `list_teams(session, user_id, skip, limit) -> tuple[list, int]`
- `create_team(session, user_id, data) -> AgenticTeam`
- `update_team(session, team_id, user_id, data) -> AgenticTeam`
- `delete_team(session, team_id, user_id) -> None`

#### `AgenticTeamNodeService` (`backend/app/services/agentic_teams/agentic_team_node_service.py`)

- `get_node(session, team_id, node_id, user_id) -> AgenticTeamNode`
- `list_nodes(session, team_id, user_id) -> list[AgenticTeamNode]`
- `create_node(session, team_id, user_id, data) -> AgenticTeamNode`
  - Verifies agent ownership (`agent.owner_id == user_id`)
  - Prevents duplicate `(team_id, agent_id)` — raises 409
  - Auto-populates name from `agent.name`
  - If `is_lead=True`: auto-unmarks existing lead in team
- `update_node(session, team_id, node_id, user_id, data) -> AgenticTeamNode`
  - Only `is_lead`, `pos_x`, `pos_y` updatable
  - If `is_lead=True`: auto-unmarks previous lead (not current node)
- `delete_node(session, team_id, node_id, user_id) -> None`
- `bulk_update_positions(session, team_id, user_id, positions) -> list[AgenticTeamNode]`
  - Validates all IDs belong to team before writing any — raises 400 on foreign IDs
- `node_to_public(session, node) -> AgenticTeamNodePublic` — resolves `agent_ui_color_preset`

#### `AgenticTeamConnectionService` (`backend/app/services/agentic_teams/agentic_team_connection_service.py`)

- `get_connection(session, team_id, conn_id, user_id) -> AgenticTeamConnection`
- `list_connections(session, team_id, user_id) -> list[AgenticTeamConnection]`
- `create_connection(session, team_id, user_id, data) -> AgenticTeamConnection`
  - Self-connection guard → 400
  - Verifies source/target nodes belong to team → 404
  - Duplicate `(source, target)` guard → 409
- `update_connection(session, team_id, conn_id, user_id, data) -> AgenticTeamConnection`
- `delete_connection(session, team_id, conn_id, user_id) -> None`
- `connection_to_public(session, conn) -> AgenticTeamConnectionPublic` — resolves node names

### Routes

File: `backend/app/api/routes/agentic_teams.py`

Registered in `backend/app/api/main.py`:
```python
api_router.include_router(agentic_teams.router)
```

Router prefix: `/api/v1/agentic-teams`, tag: `agentic-teams`

| Method | Path | Handler | Response |
|--------|------|---------|---------|
| GET | `/` | `list_agentic_teams` | `AgenticTeamsPublic` |
| POST | `/` | `create_agentic_team` | `AgenticTeamPublic` |
| GET | `/{team_id}` | `get_agentic_team` | `AgenticTeamPublic` |
| PUT | `/{team_id}` | `update_agentic_team` | `AgenticTeamPublic` |
| DELETE | `/{team_id}` | `delete_agentic_team` | `Message` |
| GET | `/{team_id}/chart` | `get_agentic_team_chart` | `AgenticTeamChartPublic` |
| PUT | `/{team_id}/nodes/positions` | `bulk_update_node_positions` | `list[AgenticTeamNodePublic]` |
| GET | `/{team_id}/nodes/` | `list_team_nodes` | `AgenticTeamNodesPublic` |
| POST | `/{team_id}/nodes/` | `create_team_node` | `AgenticTeamNodePublic` |
| GET | `/{team_id}/nodes/{node_id}` | `get_team_node` | `AgenticTeamNodePublic` |
| PUT | `/{team_id}/nodes/{node_id}` | `update_team_node` | `AgenticTeamNodePublic` |
| DELETE | `/{team_id}/nodes/{node_id}` | `delete_team_node` | `Message` |
| GET | `/{team_id}/connections/` | `list_team_connections` | `AgenticTeamConnectionsPublic` |
| POST | `/{team_id}/connections/` | `create_team_connection` | `AgenticTeamConnectionPublic` |
| GET | `/{team_id}/connections/{conn_id}` | `get_team_connection` | `AgenticTeamConnectionPublic` |
| PUT | `/{team_id}/connections/{conn_id}` | `update_team_connection` | `AgenticTeamConnectionPublic` |
| DELETE | `/{team_id}/connections/{conn_id}` | `delete_team_connection` | `Message` |

**Routing note**: `PUT /{team_id}/nodes/positions` is registered **before** `PUT /{team_id}/nodes/{node_id}` to prevent FastAPI treating `"positions"` as a `node_id` path parameter.

### Migration

File: `backend/app/alembic/versions/e8c9e80a2914_add_agentic_teams_tables.py`

Creates three tables in dependency order:
1. `agentic_team` (with `ix_agentic_team_owner_id` index)
2. `agentic_team_node` (with FKs to `agentic_team` and `agent`)
3. `agentic_team_connection` (with FKs to `agentic_team` and `agentic_team_node` for both ends)

All FKs use `ondelete="CASCADE"`.

---

## Frontend

### npm Packages Added

- `@xyflow/react` — React Flow for chart canvas (MIT)
- `@dagrejs/dagre` — Dagre layout algorithm for auto-arrange (MIT)
- `@radix-ui/react-popover` (via shadcn `popover` component)

### Files Created

| File | Purpose |
|------|---------|
| `frontend/src/components/AgenticTeams/AgenticTeamSettings.tsx` | Settings card — mirrors `WorkspaceSettings.tsx` |
| `frontend/src/components/AgenticTeams/AgenticTeamsSwitcher.tsx` | Sidebar switcher — mirrors `SidebarWorkspaceSwitcher` |
| `frontend/src/components/AgenticTeams/AgenticTeamChart.tsx` | React Flow chart with Dagre auto-arrange |
| `frontend/src/components/AgenticTeams/AgenticTeamChartNode.tsx` | Custom React Flow node |
| `frontend/src/components/AgenticTeams/AgenticTeamChartEdge.tsx` | Custom React Flow edge with hover menu |
| `frontend/src/components/AgenticTeams/ConnectionEditDialog.tsx` | Dialog to edit connection prompt + enabled |
| `frontend/src/components/AgenticTeams/AddNodeDialog.tsx` | Dialog to add agent node to team |
| `frontend/src/routes/_layout/agentic-teams/index.tsx` | Index route — redirect or empty state |
| `frontend/src/routes/_layout/agentic-teams/$teamId.tsx` | Chart page — mutations + header |

### Files Modified

| File | Change |
|------|--------|
| `frontend/src/routes/_layout/settings.tsx` | Added `AgenticTeamSettings` to Interface tab |
| `frontend/src/components/Sidebar/AppSidebar.tsx` | Added `AgenticTeamsSwitcher` above `SidebarWorkspaceSwitcher` |
| `frontend/src/routeTree.gen.ts` | Registered new routes (manually, until next `tsr generate` run) |
| `frontend/src/client/` | Regenerated from OpenAPI spec (contains `AgenticTeamsService`) |

### Query Keys

| Key | Data |
|-----|------|
| `["agenticTeams"]` | Team list (used by settings card and sidebar switcher) |
| `["agenticTeamChart", teamId]` | Chart data (team + nodes + connections) |

### Generated API Client

`AgenticTeamsService` in `frontend/src/client/sdk.gen.ts` provides all 17 endpoint methods:
- `listAgenticTeams`, `createAgenticTeam`, `getAgenticTeam`, `updateAgenticTeam`, `deleteAgenticTeam`
- `getAgenticTeamChart`
- `bulkUpdateNodePositions`
- `listTeamNodes`, `createTeamNode`, `getTeamNode`, `updateTeamNode`, `deleteTeamNode`
- `listTeamConnections`, `createTeamConnection`, `getTeamConnection`, `updateTeamConnection`, `deleteTeamConnection`

### Chart Implementation

**Node color**: `AgenticTeamNodePublic.agent_ui_color_preset` maps to Tailwind bg classes via `COLOR_MAP` in `AgenticTeamChartNode.tsx`.

**Auto-arrange algorithm**: Dagre `rankdir: "TB"` (top-down). Lead node is naturally placed at top when it has no incoming connections. After layout, bulk positions are saved via `PUT /nodes/positions`.

**Debounced position save**: 300ms after drag-stop (`onNodesChange` with `type === "position" && !dragging`).

**Connection hover**: Invisible 16px stroke overlay path + `EdgeLabelRenderer` popover at edge midpoint. Only visible in edit mode.

---

## Tests

File: `backend/tests/api/agentic_teams/test_agentic_teams.py`

8 scenario-based tests covering:
1. `test_agentic_team_full_lifecycle` — CRUD + auth/ownership guards
2. `test_agentic_team_superuser_no_bypass` — Superuser cannot access other users' teams
3. `test_agentic_team_nodes_full_lifecycle` — Node operations with validation (no real agents)
4. `test_agentic_team_nodes_with_real_agents` — Node CRUD, is_lead toggle, duplicate prevention
5. `test_agentic_team_connections_full_lifecycle` — Connection CRUD, self-connection, duplicate guards
6. `test_bulk_position_update` — Bulk positions + invalid ID rejection
7. `test_chart_bulk_endpoint` — Chart returns all data with resolved names
8. `test_cascade_delete` — Node deletion cascades connections; team deletion cascades all

Conftest at `tests/api/agentic_teams/conftest.py` uses the same environment stubs as agent tests (environment adapter, session patch, background tasks, external service mocks).
