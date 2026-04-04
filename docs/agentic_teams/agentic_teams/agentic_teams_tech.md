# AgenticTeams — Technical Reference

## File Locations

### Backend

| File | Purpose |
|------|---------|
| `backend/app/models/agentic_teams/agentic_team.py` | All SQLModel models and Pydantic schemas for teams, nodes, connections |
| `backend/app/api/routes/agentic_teams.py` | 17 API endpoints, router prefix `/agentic-teams` |
| `backend/app/services/agentic_teams/agentic_team_service.py` | Team CRUD service |
| `backend/app/services/agentic_teams/agentic_team_node_service.py` | Node CRUD service, lead enforcement, bulk positions, color resolution |
| `backend/app/services/agentic_teams/agentic_team_connection_service.py` | Connection CRUD service, node name resolution |
| `backend/app/alembic/versions/e8c9e80a2914_add_agentic_teams_tables.py` | Migration: creates all three tables |

### Frontend

| File | Purpose |
|------|---------|
| `frontend/src/routes/_layout/agentic-teams/index.tsx` | Index page — redirects to first team or shows settings card |
| `frontend/src/routes/_layout/agentic-teams/$teamId.tsx` | Chart page — fetches chart data, owns all mutations, renders header |
| `frontend/src/components/AgenticTeams/AgenticTeamChart.tsx` | Main chart component — React Flow canvas, auto-arrange, position debounce |
| `frontend/src/components/AgenticTeams/AgenticTeamChartNode.tsx` | Custom React Flow node — color mapping, lead badge, kebab menu |
| `frontend/src/components/AgenticTeams/AgenticTeamChartEdge.tsx` | Custom React Flow edge — bezier path, hover popover with edit/delete |
| `frontend/src/components/AgenticTeams/AgenticTeamSettings.tsx` | Settings card + `AgenticTeamFormDialog` (shared by settings page and chart header) |
| `frontend/src/components/AgenticTeams/AgenticTeamsSwitcher.tsx` | Sidebar switcher component |
| `frontend/src/components/AgenticTeams/AddNodeDialog.tsx` | Dialog for adding an agent node to a team |
| `frontend/src/components/AgenticTeams/ConnectionEditDialog.tsx` | Dialog for editing connection prompt and enabled state |

---

## Database Schema

### Table: `agentic_team`

| Column | Type | Constraints | Notes |
|--------|------|-------------|-------|
| `id` | UUID | PK | `uuid4` default |
| `owner_id` | UUID | FK `user.id` CASCADE, NOT NULL | Owning user |
| `name` | VARCHAR(255) | NOT NULL, min 1 | Team display name |
| `icon` | VARCHAR(50) | nullable | Icon key from `WORKSPACE_ICONS` config |
| `created_at` | DATETIME | NOT NULL | UTC, set at creation |
| `updated_at` | DATETIME | NOT NULL | UTC, updated on every write |

Indexes: `ix_agentic_team_owner_id` on `(owner_id)`

No `user_workspace_id` — teams are workspace-independent by design.

### Table: `agentic_team_node`

| Column | Type | Constraints | Notes |
|--------|------|-------------|-------|
| `id` | UUID | PK | `uuid4` default |
| `team_id` | UUID | FK `agentic_team.id` CASCADE, NOT NULL | Parent team |
| `agent_id` | UUID | FK `agent.id` CASCADE, NOT NULL | Linked agent |
| `name` | VARCHAR(255) | NOT NULL, min 1 | Auto-populated from `agent.name` at creation; not updatable |
| `node_type` | VARCHAR(50) | NOT NULL | Always `"agent"` in MVP; field exists for future extensibility |
| `is_lead` | BOOLEAN | NOT NULL | Default `false`; at most one `true` per team (enforced in service) |
| `pos_x` | FLOAT | NOT NULL | X canvas position; default `0.0` |
| `pos_y` | FLOAT | NOT NULL | Y canvas position; default `0.0` |
| `created_at` | DATETIME | NOT NULL | UTC |
| `updated_at` | DATETIME | NOT NULL | UTC |

Indexes:
- `ix_agentic_team_node_team_id` on `(team_id)`
- `ix_agentic_team_node_agent_id` on `(agent_id)`

No unique DB constraint on `(team_id, agent_id)` — enforced at service layer to return a proper 409.

`agent_ui_color_preset` is not stored on the node; it is resolved at read time from `agent.ui_color_preset` inside `node_to_public()`.

### Table: `agentic_team_connection`

| Column | Type | Constraints | Notes |
|--------|------|-------------|-------|
| `id` | UUID | PK | `uuid4` default |
| `team_id` | UUID | FK `agentic_team.id` CASCADE, NOT NULL | Parent team |
| `source_node_id` | UUID | FK `agentic_team_node.id` CASCADE, NOT NULL | Origin node |
| `target_node_id` | UUID | FK `agentic_team_node.id` CASCADE, NOT NULL | Destination node |
| `connection_prompt` | VARCHAR(2000) | NOT NULL | Handover prompt text; default `""` |
| `enabled` | BOOLEAN | NOT NULL | Default `true`; disabled connections render dashed |
| `created_at` | DATETIME | NOT NULL | UTC |
| `updated_at` | DATETIME | NOT NULL | UTC |

Indexes:
- `ix_agentic_team_connection_team_id` on `(team_id)`
- `ix_agentic_team_connection_source_node_id` on `(source_node_id)`

No unique DB constraint on `(source_node_id, target_node_id)` — enforced at service layer (409 on duplicate). Self-connections (`source == target`) rejected with 400.

Both source and target FKs use `ondelete="CASCADE"` — deleting either endpoint node removes all affected connections.

---

## API Endpoints

All endpoints are under prefix `/api/v1/agentic-teams`. All require authentication (`CurrentUser` dependency). There is no superuser bypass — access is strictly owner-only.

### Team Endpoints

| Method | Path | Request Body | Response | Description |
|--------|------|-------------|----------|-------------|
| GET | `/` | — | `AgenticTeamsPublic` | List all teams owned by the current user |
| POST | `/` | `AgenticTeamCreate` | `AgenticTeamPublic` | Create a new team |
| GET | `/{team_id}` | — | `AgenticTeamPublic` | Get a single team |
| PUT | `/{team_id}` | `AgenticTeamUpdate` | `AgenticTeamPublic` | Update name or icon |
| DELETE | `/{team_id}` | — | `Message` | Delete team (cascades to nodes and connections) |

### Chart Bulk Endpoint

| Method | Path | Response | Description |
|--------|------|----------|-------------|
| GET | `/{team_id}/chart` | `AgenticTeamChartPublic` | Returns team + all nodes + all connections in one request. Primary fetch for the chart page. |

The chart endpoint resolves `agent_ui_color_preset` for all nodes and `source_node_name`/`target_node_name`/`source_node_color_preset`/`target_node_color_preset` for all connections inside the response construction.

### Node Endpoints

| Method | Path | Request Body | Response | Description |
|--------|------|-------------|----------|-------------|
| GET | `/{team_id}/nodes/` | — | `AgenticTeamNodesPublic` | List nodes |
| POST | `/{team_id}/nodes/` | `AgenticTeamNodeCreate` | `AgenticTeamNodePublic` | Add agent node |
| GET | `/{team_id}/nodes/{node_id}` | — | `AgenticTeamNodePublic` | Get single node |
| PUT | `/{team_id}/nodes/{node_id}` | `AgenticTeamNodeUpdate` | `AgenticTeamNodePublic` | Update `is_lead`, `pos_x`, `pos_y` |
| DELETE | `/{team_id}/nodes/{node_id}` | — | `Message` | Delete node (cascades to connections) |
| PUT | `/{team_id}/nodes/positions` | `list[AgenticTeamNodePositionUpdate]` | `list[AgenticTeamNodePublic]` | Bulk update positions |

The `/positions` endpoint is registered before `/{node_id}` in the router file to prevent FastAPI from treating the literal string `"positions"` as a node UUID path parameter.

### Connection Endpoints

| Method | Path | Request Body | Response | Description |
|--------|------|-------------|----------|-------------|
| GET | `/{team_id}/connections/` | — | `AgenticTeamConnectionsPublic` | List connections |
| POST | `/{team_id}/connections/` | `AgenticTeamConnectionCreate` | `AgenticTeamConnectionPublic` | Create connection |
| GET | `/{team_id}/connections/{conn_id}` | — | `AgenticTeamConnectionPublic` | Get single connection |
| PUT | `/{team_id}/connections/{conn_id}` | `AgenticTeamConnectionUpdate` | `AgenticTeamConnectionPublic` | Update prompt or enabled |
| DELETE | `/{team_id}/connections/{conn_id}` | — | `Message` | Delete connection |
| POST | `/{team_id}/connections/{conn_id}/generate-prompt` | — | `GenerateConnectionPromptResponse` | Generate a handover prompt via AI based on both agents' configurations |

The generate-prompt endpoint is read-only (no state mutation); it returns the AI-generated text for the client to preview and optionally save via the standard PUT endpoint.

---

## Model Schemas

### `AgenticTeamCreate`

```python
name: str  # min_length=1, max_length=255
icon: str | None  # max_length=50, default None
```

### `AgenticTeamUpdate`

```python
name: str | None  # min_length=1, max_length=255
icon: str | None  # max_length=50
```

### `AgenticTeamPublic`

```python
id: uuid.UUID
owner_id: uuid.UUID
name: str
icon: str | None
task_prefix: str | None  # default None; used for short-code generation (e.g., "HR" → HR-1)
created_at: datetime
updated_at: datetime
```

### `AgenticTeamsPublic`

```python
data: list[AgenticTeamPublic]
count: int
```

### `AgenticTeamNodeCreate`

```python
agent_id: uuid.UUID
is_lead: bool  # default False
pos_x: float   # default 0.0
pos_y: float   # default 0.0
```

`name` is intentionally absent — it is auto-populated from `agent.name` at the service layer.

### `AgenticTeamNodeUpdate`

```python
is_lead: bool | None
pos_x: float | None
pos_y: float | None
```

`name` and `agent_id` are not updatable via this schema.

### `AgenticTeamNodePublic`

```python
id: uuid.UUID
team_id: uuid.UUID
agent_id: uuid.UUID
name: str
agent_ui_color_preset: str | None  # resolved from agent at read time
node_type: str
is_lead: bool
pos_x: float
pos_y: float
created_at: datetime
updated_at: datetime
```

### `AgenticTeamNodePositionUpdate`

```python
id: uuid.UUID
pos_x: float
pos_y: float
```

Used exclusively by the bulk positions endpoint.

### `AgenticTeamConnectionCreate`

```python
source_node_id: uuid.UUID
target_node_id: uuid.UUID
connection_prompt: str  # default "", max_length=2000
enabled: bool           # default True
```

### `AgenticTeamConnectionUpdate`

```python
connection_prompt: str | None
enabled: bool | None
```

### `AgenticTeamConnectionPublic`

```python
id: uuid.UUID
team_id: uuid.UUID
source_node_id: uuid.UUID
target_node_id: uuid.UUID
source_node_name: str              # resolved from source node at read time
target_node_name: str              # resolved from target node at read time
source_node_color_preset: str | None  # resolved from source agent.ui_color_preset at read time
target_node_color_preset: str | None  # resolved from target agent.ui_color_preset at read time
connection_prompt: str
enabled: bool
created_at: datetime
updated_at: datetime
```

### `GenerateConnectionPromptResponse`

```python
success: bool
connection_prompt: str | None  # present when success is True
error: str | None              # present when success is False
```

Response model for the AI prompt generation endpoint. When `success` is `True`, `connection_prompt` contains the AI-generated text. When `False`, `error` contains the reason.

### `AgenticTeamChartPublic`

```python
team: AgenticTeamPublic
nodes: list[AgenticTeamNodePublic]
connections: list[AgenticTeamConnectionPublic]
```

---

## Service Layer

### `AgenticTeamService`

All methods are `@staticmethod`.

| Method | Signature | Notes |
|--------|-----------|-------|
| `get_team` | `(session, team_id, user_id) -> AgenticTeam` | Raises 404 if team not found or user is not owner |
| `list_teams` | `(session, user_id, skip, limit) -> tuple[list, int]` | Returns (teams, total_count) for paginated listing |
| `create_team` | `(session, user_id, data) -> AgenticTeam` | Sets `owner_id`, commits |
| `update_team` | `(session, team_id, user_id, data) -> AgenticTeam` | Merges only set fields via `model_dump(exclude_unset=True)`, updates `updated_at` |
| `delete_team` | `(session, team_id, user_id) -> None` | Cascades via FK; no explicit node/connection cleanup needed |

### `AgenticTeamNodeService`

| Method | Signature | Notes |
|--------|-----------|-------|
| `get_node` | `(session, team_id, node_id, user_id) -> AgenticTeamNode` | Verifies team ownership first, then node membership |
| `list_nodes` | `(session, team_id, user_id) -> list[AgenticTeamNode]` | All nodes for a team, unordered |
| `create_node` | `(session, team_id, user_id, data) -> AgenticTeamNode` | Validates agent ownership; checks duplicate; unmarks existing lead if `is_lead=True`; auto-sets `name` from `agent.name` |
| `update_node` | `(session, team_id, node_id, user_id, data) -> AgenticTeamNode` | Unmarks existing lead if `is_lead=True`; applies only set fields |
| `delete_node` | `(session, team_id, node_id, user_id) -> None` | Connection cascade via FK |
| `bulk_update_positions` | `(session, team_id, user_id, positions) -> list[AgenticTeamNode]` | Validates all IDs belong to team before updating; single commit |
| `node_to_public` | `(session, node) -> AgenticTeamNodePublic` | Resolves `agent_ui_color_preset` from `agent.ui_color_preset`; gracefully handles missing agent |

### `AgenticTeamConnectionService`

| Method | Signature | Notes |
|--------|-----------|-------|
| `get_connection` | `(session, team_id, conn_id, user_id) -> AgenticTeamConnection` | Verifies team ownership first |
| `list_connections` | `(session, team_id, user_id) -> list[AgenticTeamConnection]` | All connections for a team |
| `create_connection` | `(session, team_id, user_id, data) -> AgenticTeamConnection` | Rejects self-connections (400); verifies source and target nodes belong to team; rejects duplicates (409) |
| `update_connection` | `(session, team_id, conn_id, user_id, data) -> AgenticTeamConnection` | Only `connection_prompt` and `enabled` are updatable |
| `delete_connection` | `(session, team_id, conn_id, user_id) -> None` | Simple delete after ownership verification |
| `connection_to_public` | `(session, conn) -> AgenticTeamConnectionPublic` | Resolves `source_node_name`, `target_node_name`, `source_node_color_preset`, and `target_node_color_preset` from nodes and their linked agents; gracefully handles missing nodes/agents (empty string / None fallback) |
| `generate_connection_prompt` | `(session, team_id, conn_id, user_id) -> str` | Loads both agents from the connection's nodes; calls `AIFunctionsService.generate_handover_prompt` with both agents' names, entrypoint prompts, and workflow prompts; raises `TeamConnectionError(500)` if AI generation fails |

---

## Frontend Components

### Route: `agentic-teams.index.tsx`

- Fetches `["agenticTeams"]` on load.
- If teams exist, immediately redirects to `/agentic-teams/$teamId` (first team).
- If no teams exist, renders `AgenticTeamSettings` card inside a full-page scroll container.
- Sets page header to static "Agentic Teams" with `Network` icon.

### Route: `agentic-teams.$teamId.tsx`

The primary chart page. Owns all mutation logic and passes callbacks down to `AgenticTeamChart`.

**Data fetching:**
- Query key: `["agenticTeamChart", teamId]`
- Calls `AgenticTeamsService.getAgenticTeamChart({ teamId })`
- Single request returns team + nodes + connections

**Local state:**
- `isEditMode: boolean` — toggled via lock/unlock button in page header
- `menuOpen: boolean` — controls the team ⋮ dropdown
- `showEditDialog / showDeleteDialog: boolean` — dialog visibility

**Mutations (all via `AgenticTeamsService`):**

| Mutation | Invalidates | Notes |
|---------|-------------|-------|
| `deleteTeamMutation` | `["agenticTeams"]` | Navigates to `/agentic-teams` on success |
| `updateTeamMutation` | `["agenticTeams"]`, `["agenticTeamChart", teamId]` | |
| `createNodeMutation` | `["agenticTeamChart", teamId]` | |
| `updateNodeMutation` | `["agenticTeamChart", teamId]` | |
| `deleteNodeMutation` | `["agenticTeamChart", teamId]` | |
| `bulkUpdatePositionsMutation` | none | Positions are reflected locally; no cache invalidation needed |
| `createConnectionMutation` | `["agenticTeamChart", teamId]` | Created with empty prompt and `enabled = true` |
| `updateConnectionMutation` | `["agenticTeamChart", teamId]` | |
| `deleteConnectionMutation` | `["agenticTeamChart", teamId]` | |
| `generateConnectionPromptMutation` | none | Calls `POST /{team_id}/connections/{conn_id}/generate-prompt`; on success stores `data.connection_prompt` in local `generatedPrompt` state; no cache invalidation (does not modify the connection) |

**Local state additions:**
- `generatedPrompt: string | null` — holds the latest AI-generated prompt text. Reset to `null` just before the mutation fires. Passed as a prop through `AgenticTeamChart` → `ConnectionEditDialog`.

**Page header:** dynamically set via `usePageHeader()`. Contains team name with `Network` icon, edit-mode toggle button (lock/unlock icon pair), and ⋮ dropdown for edit/delete team actions.

### `AgenticTeamChart`

Pure presentational component (no direct API calls). Receives data and callbacks as props.

New props added:
- `onGenerateConnectionPrompt: (connId: string) => void` — called when the Generate button is clicked inside `ConnectionEditDialog`
- `isGeneratingPrompt: boolean` — forwarded to `ConnectionEditDialog` as `isGenerating`
- `generatedPrompt: string | null` — forwarded to `ConnectionEditDialog`; the chart passes this down so the dialog can auto-apply the text when it arrives

**React Flow configuration:**
- Custom node type: `agenticTeamNode` → `AgenticTeamChartNode`
- Custom edge type: `agenticTeamEdge` → `AgenticTeamChartEdge`
- `nodesDraggable`: only in edit mode
- `nodesConnectable`: only in edit mode
- `elementsSelectable`: only in edit mode
- Background grid (`<Background />`): only rendered in edit mode
- `fitView` enabled with 20% padding

**Position debounce:** After a drag-drop, position changes are debounced 300ms before calling `onBulkUpdatePositions`. This prevents a flood of API calls during a single drag gesture.

**Local position preservation:** When external data changes (e.g., after a node is added), the chart syncs by merging server positions with current local positions — existing nodes keep their locally-moved positions even before the server save completes.

**Auto-Arrange (`autoArrangeLayout`):**
```
dagre.graphlib.Graph
  rankdir: "TB"
  nodesep: 80
  ranksep: 120
  node dimensions: 250w x 60h
```
Positions are adjusted by `NODE_WIDTH / 2` and `NODE_HEIGHT / 2` to center nodes on the Dagre-computed center point.

### `AgenticTeamChartNode`

Custom React Flow node. Rendered as a styled `div` with:
- Background color from `COLOR_MAP` keyed on `agent_ui_color_preset`
- Lead badge: yellow crown icon in the top-right corner when `is_lead = true`
- Lead ring: yellow `ring-2` border when `is_lead = true`
- Edit mode: dashed border, `cursor-move`
- View mode: solid border
- Handles (connection points): visible in edit mode (`!bg-muted-foreground/50`), invisible in view mode (`!bg-transparent !border-0`)
- Kebab menu: only rendered in edit mode, contains "Set as Lead"/"Unset Lead" and "Remove Node"

Color presets mapped:

| Preset value | Tailwind class (light / dark) |
|---|---|
| `slate` (default) | `bg-slate-100` / `bg-slate-800` |
| `blue` | `bg-blue-100` / `bg-blue-900/40` |
| `green` | `bg-green-100` / `bg-green-900/40` |
| `red` | `bg-red-100` / `bg-red-900/40` |
| `purple` | `bg-purple-100` / `bg-purple-900/40` |
| `orange` | `bg-orange-100` / `bg-orange-900/40` |
| `yellow` | `bg-yellow-100` / `bg-yellow-900/40` |
| `pink` | `bg-pink-100` / `bg-pink-900/40` |
| `indigo` | `bg-indigo-100` / `bg-indigo-900/40` |
| `cyan` | `bg-cyan-100` / `bg-cyan-900/40` |

Unknown or null preset values fall back to `slate`.

### `AgenticTeamChartEdge`

Custom React Flow edge. Uses `getBezierPath` for the curve.

**In view mode:**
- Only the visible edge path is rendered (no hover zone, no label).
- Disabled connections render with `strokeDasharray="6 3"`.

**In edit mode:**
- An invisible thick (16px) transparent stroke path is layered on top of the visible edge as a wider hover target.
- At the edge midpoint, an `EdgeLabelRenderer`-positioned div renders a `Popover` trigger (pencil icon button).
- The button's opacity transitions between `0` and `100` based on `hovering || popoverOpen` state.
- The `Popover` contains "Edit" and "Delete" buttons. Clicking either fires the corresponding callback.

### `AgenticTeamSettings`

Exported as two components from the same file:
- `AgenticTeamFormDialog` — shared dialog for create and edit operations (used in settings, chart header, and switcher).
- `AgenticTeamSettings` — the settings card rendered in the Interface tab and on the index page when no teams exist.

Query key used: `["agenticTeams"]`.

The icon picker reuses `WORKSPACE_ICONS` from `@/config/workspaceIcons` — the same icon set used by workspace and dashboard icons. Default icon is `"users"`.

### `AgenticTeamsSwitcher`

Rendered as a `SidebarMenuItem` in the sidebar. Uses the same `SidebarMenuButton` + `DropdownMenu` pattern as the workspace switcher.

Active team is detected by matching the current pathname against `/^\/agentic-teams\/([^/]+)/`. The button label shows the active team name, or "Agentic Teams" when no team is active.

Creating a team from the switcher auto-navigates to the new team chart via `navigate({ to: "/agentic-teams/$teamId", params: { teamId: newTeam.id } })`.

### `AddNodeDialog`

- Queries `["allAgents"]` (all agents owned by user, limit 200) when dialog is open.
- Filters out agents already in the team using the `existingAgentIds` prop.
- Shows a preview of the node name that will be used (the selected agent's name).
- On submit, passes `agentId` and `isLead` boolean up to the parent handler.

### `ConnectionEditDialog`

Props:
- `open: boolean`
- `onClose: () => void`
- `connection: AgenticTeamConnectionPublic | null`
- `onSave: (connectionId: string, prompt: string, enabled: boolean) => void`
- `onGenerate: (connectionId: string) => void`
- `isPending: boolean` — true while the save mutation is in flight
- `isGenerating: boolean` — true while the AI generate mutation is in flight
- `generatedPrompt: string | null` — when non-null, the dialog auto-applies this text to the textarea

Behavior:
- Shows source and target agent names as **colored badges**: each badge uses `getColorPreset(color_preset)` from `@/utils/colorPresets` and applies `badgeBg` + `badgeText` Tailwind classes, plus a `Bot` icon. The badges are separated by an `ArrowRight` icon.
- Shows a "Generate" button (Sparkles icon) next to the "Handover Prompt" label. The button calls `onGenerate(connection.id)` and is disabled while `isGenerating` is true (label changes to "Generating...").
- When `generatedPrompt` changes and the dialog is open, the textarea is updated automatically via `useEffect`. A local `appliedGeneratedPrompt` ref prevents the same generated text from being re-applied if props cycle.
- Textarea for `connection_prompt` with a live character counter (`{n}/2000`), `maxLength=2000`, `rows=5`.
- Switch for `enabled` state.
- Pre-fills fields from `connection` when the dialog opens. Resets `appliedGeneratedPrompt` on open so a fresh generate applies correctly.

---

## Error Handling

| Scenario | HTTP Status | Detail |
|---------|-------------|--------|
| Team not found or wrong owner | 404 | "Agentic team not found" |
| Node not found or wrong team | 404 | "Node not found" |
| Connection not found or wrong team | 404 | "Connection not found" |
| Agent not found or wrong owner | 404 | "Agent not found" |
| Source node not found in team | 404 | "Source node not found" |
| Target node not found in team | 404 | "Target node not found" |
| Duplicate agent in team | 409 | "This agent is already in the team" |
| Duplicate connection | 409 | "A connection between these nodes already exists" |
| Self-connection | 400 | "Source and target nodes must be different" |
| Node ID not in team (bulk positions) | 400 | "One or more node IDs do not belong to this team" |

---

## Database Migration

Migration file: `backend/app/alembic/versions/e8c9e80a2914_add_agentic_teams_tables.py`

Revision ID: `e8c9e80a2914`
Down revision: `a1b2c3d4e5f7`

Creates tables in dependency order:
1. `agentic_team` (with index on `owner_id`)
2. `agentic_team_node` (with indexes on `team_id` and `agent_id`)
3. `agentic_team_connection` (with indexes on `team_id` and `source_node_id`)

All three tables are dropped in reverse order on downgrade.

---

## React Query Keys

| Key | Used by | Scope |
|-----|---------|-------|
| `["agenticTeams"]` | Switcher, Settings card, Index page | All teams list |
| `["agenticTeamChart", teamId]` | Chart page | Single team's full chart data |
| `["allAgents"]` | AddNodeDialog | All agents for agent picker |
