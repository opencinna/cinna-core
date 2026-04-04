# User Dashboards — Implementation Plan

## Overview

User Dashboards provide a customizable grid-based control panel where users can monitor multiple agents at a glance — similar to monitoring system dashboards (Grafana, Datadog). Each dashboard is a named collection of **blocks**, where each block is linked to a specific agent and renders one of several **view types**: embedded webapp iframe, latest session summary, or latest tasks list.

**Core capabilities:**
- Create, rename, delete dashboards
- Add / remove / rearrange blocks on a grid layout
- Each block tied to one agent with configurable view type and display options
- Dashboard bypasses workspace filtering — any agent the user owns is eligible
- Sidebar "Dashboard" item gains a hover sub-menu (like workspace switcher): "Main" → current index page, "Manage Dashboards" → dashboard management page

**High-level flow:**
```
Sidebar "Dashboard" hover menu
  ├── "Main"              → / (existing index page)
  ├── [Dashboard A]       → /dashboards/{id}
  ├── [Dashboard B]       → /dashboards/{id}
  └── "Manage Dashboards" → /dashboards
```

---

## Architecture Overview

```
┌──────────────────────────────────────────────────────────────────┐
│ Frontend                                                          │
│  Sidebar (hover menu) ──→ /dashboards (manage) or /dashboards/id │
│  DashboardGrid ──→ DashboardBlock[]                               │
│    ├── WebAppView (iframe)                                        │
│    ├── LatestSessionView (session summary card)                   │
│    └── LatestTasksView (task list)                                │
└──────────┬───────────────────────────────────────────────────────┘
           │
           ▼
┌──────────────────────────────────────────────────────────────────┐
│ Backend API                                                       │
│  /api/v1/dashboards       — CRUD dashboards                      │
│  /api/v1/dashboards/{id}/blocks — CRUD blocks + reorder          │
│  Reads from existing agent/session/task APIs for block content    │
└──────────┬───────────────────────────────────────────────────────┘
           │
           ▼
┌──────────────────────────────────────────────────────────────────┐
│ PostgreSQL                                                        │
│  user_dashboard         — dashboard metadata per user             │
│  user_dashboard_block   — blocks within a dashboard               │
└──────────────────────────────────────────────────────────────────┘
```

---

## Data Models

### Table: `user_dashboard`

| Column | Type | Constraints | Default | Description |
|--------|------|-------------|---------|-------------|
| `id` | UUID | PK | uuid4 | Dashboard ID |
| `owner_id` | UUID | FK → user.id, ON DELETE CASCADE, NOT NULL | — | Owner user |
| `name` | VARCHAR(255) | NOT NULL | — | Dashboard display name |
| `description` | TEXT | nullable | NULL | Optional description |
| `sort_order` | INTEGER | NOT NULL | 0 | Order in sidebar menu |
| `created_at` | DATETIME | NOT NULL | utcnow | Creation timestamp |
| `updated_at` | DATETIME | NOT NULL | utcnow | Last update timestamp |

**Indexes:**
- `ix_user_dashboard_owner_id` on `owner_id` (query dashboards by user)
- `ix_user_dashboard_owner_sort` on `(owner_id, sort_order)` (ordered listing)

### Table: `user_dashboard_block`

| Column | Type | Constraints | Default | Description |
|--------|------|-------------|---------|-------------|
| `id` | UUID | PK | uuid4 | Block ID |
| `dashboard_id` | UUID | FK → user_dashboard.id, ON DELETE CASCADE, NOT NULL | — | Parent dashboard |
| `agent_id` | UUID | FK → agent.id, ON DELETE CASCADE, NOT NULL | — | Connected agent |
| `view_type` | VARCHAR(50) | NOT NULL | `"latest_session"` | View type: `webapp`, `latest_session`, `latest_tasks` |
| `title` | VARCHAR(255) | nullable | NULL | Custom title override (defaults to agent name if null) |
| `show_border` | BOOLEAN | NOT NULL | true | Show frame/border around block |
| `grid_x` | INTEGER | NOT NULL | 0 | Grid column position |
| `grid_y` | INTEGER | NOT NULL | 0 | Grid row position |
| `grid_w` | INTEGER | NOT NULL | 1 | Grid width (columns spanned) |
| `grid_h` | INTEGER | NOT NULL | 1 | Grid height (rows spanned) |
| `config` | JSON | nullable | NULL | Future-proof: additional view-specific config options |
| `created_at` | DATETIME | NOT NULL | utcnow | Creation timestamp |
| `updated_at` | DATETIME | NOT NULL | utcnow | Last update timestamp |

**Indexes:**
- `ix_user_dashboard_block_dashboard_id` on `dashboard_id` (list blocks per dashboard)
- `ix_user_dashboard_block_agent_id` on `agent_id` (find blocks for an agent, useful for cleanup)

**Notes:**
- `config` JSON column is reserved for future per-view-type settings (e.g., session count for latest_tasks, refresh interval). Not used in MVP but avoids a migration later.
- `view_type` is a string enum rather than DB enum to make adding new types easy.
- Deleting an agent cascades to all its blocks across all dashboards.
- Grid positions (`grid_x`, `grid_y`, `grid_w`, `grid_h`) follow the `react-grid-layout` coordinate system.

---

## Security Architecture

### Access Control

- **Ownership-based**: Users can only CRUD their own dashboards and blocks
- **Agent visibility**: When adding a block, only agents owned by the user (or shared with them) are selectable — reuse existing agent list query logic but **without** workspace filtering
- **No workspace scoping**: Dashboards are intentionally workspace-independent. The `user_dashboard` table has no `user_workspace_id` column

### Authorization Rules

| Action | Allowed |
|--------|---------|
| List dashboards | Owner only |
| Create dashboard | Authenticated user |
| View/edit/delete dashboard | Owner only |
| Add block (any agent) | Owner only; agent must be owned by or shared with user |
| View block content | Owner only; content fetched through existing authorized APIs |

### Input Validation

- Dashboard name: 1–255 characters, trimmed
- View type: must be one of allowed enum values
- Grid positions: non-negative integers; `grid_w` and `grid_h` ≥ 1
- Max blocks per dashboard: 20 (prevents abuse)
- Max dashboards per user: 10

---

## Backend Implementation

### Models

**File:** `backend/app/models/users/user_dashboard.py`

```
UserDashboardBase(SQLModel):
    name: str (1-255)
    description: str | None

UserDashboard(UserDashboardBase, table=True):
    id: UUID (PK)
    owner_id: UUID (FK user.id CASCADE)
    sort_order: int (default 0)
    created_at, updated_at
    # relationship: blocks

UserDashboardCreate(UserDashboardBase):
    (inherits name, description)

UserDashboardUpdate(SQLModel):
    name: str | None
    description: str | None
    sort_order: int | None

UserDashboardPublic(SQLModel):
    id, name, description, sort_order, created_at, updated_at
    blocks: list[UserDashboardBlockPublic]

UserDashboardBlockBase(SQLModel):
    agent_id: UUID
    view_type: str (default "latest_session")
    title: str | None
    show_border: bool (default True)
    grid_x, grid_y, grid_w, grid_h: int

UserDashboardBlock(UserDashboardBlockBase, table=True):
    id: UUID (PK)
    dashboard_id: UUID (FK user_dashboard.id CASCADE)
    config: dict | None
    created_at, updated_at

UserDashboardBlockCreate(UserDashboardBlockBase):
    (inherits all base fields)

UserDashboardBlockUpdate(SQLModel):
    view_type: str | None
    title: str | None
    show_border: bool | None
    grid_x, grid_y, grid_w, grid_h: int | None
    config: dict | None

UserDashboardBlockPublic(SQLModel):
    id, agent_id, view_type, title, show_border
    grid_x, grid_y, grid_w, grid_h, config
    created_at, updated_at
```

Register model import in `backend/app/models/__init__.py`.

### API Routes

**File:** `backend/app/api/routes/user_dashboards.py`

**Router prefix:** `/api/v1/dashboards`
**Tags:** `["Dashboards"]`
**Register in:** `backend/app/api/main.py`

| Method | Path | Description | Auth |
|--------|------|-------------|------|
| GET | `/` | List user's dashboards (with blocks) | CurrentUser |
| POST | `/` | Create dashboard | CurrentUser |
| GET | `/{dashboard_id}` | Get dashboard with blocks | CurrentUser (owner check) |
| PUT | `/{dashboard_id}` | Update dashboard metadata | CurrentUser (owner check) |
| DELETE | `/{dashboard_id}` | Delete dashboard | CurrentUser (owner check) |
| POST | `/{dashboard_id}/blocks` | Add block to dashboard | CurrentUser (owner check + agent access check) |
| PUT | `/{dashboard_id}/blocks/{block_id}` | Update block config | CurrentUser (owner check) |
| DELETE | `/{dashboard_id}/blocks/{block_id}` | Remove block | CurrentUser (owner check) |
| PUT | `/{dashboard_id}/blocks/layout` | Bulk update block grid positions | CurrentUser (owner check) |

**Bulk layout update** (`PUT /blocks/layout`):
- Accepts array of `{ block_id, grid_x, grid_y, grid_w, grid_h }`
- Single transaction update for drag-and-drop rearrangement
- Avoids N individual PUT calls when user rearranges the grid

**List response** includes blocks eagerly loaded (no separate call needed for initial render).

### Service Layer

**File:** `backend/app/services/users/user_dashboard_service.py`

```
class UserDashboardService:
    @staticmethod
    def list_dashboards(session, owner_id) -> list[UserDashboard]
        # Ordered by sort_order, eager-load blocks

    @staticmethod
    def create_dashboard(session, owner_id, data: UserDashboardCreate) -> UserDashboard
        # Validate max dashboards limit (10)
        # Set sort_order = max existing + 1

    @staticmethod
    def update_dashboard(session, dashboard_id, owner_id, data: UserDashboardUpdate) -> UserDashboard

    @staticmethod
    def delete_dashboard(session, dashboard_id, owner_id) -> bool

    @staticmethod
    def add_block(session, dashboard_id, owner_id, data: UserDashboardBlockCreate) -> UserDashboardBlock
        # Validate max blocks limit (20)
        # Validate agent_id belongs to owner (skip workspace filter)
        # Validate view_type is allowed enum value
        # If view_type == "webapp": validate agent has webapp_enabled

    @staticmethod
    def update_block(session, dashboard_id, block_id, owner_id, data: UserDashboardBlockUpdate) -> UserDashboardBlock

    @staticmethod
    def delete_block(session, dashboard_id, block_id, owner_id) -> bool

    @staticmethod
    def update_block_layout(session, dashboard_id, owner_id, layouts: list[BlockLayoutUpdate]) -> list[UserDashboardBlock]
        # Bulk update grid positions in single transaction
```

### Error Handling

- 404: Dashboard or block not found
- 403: Dashboard not owned by current user
- 400: Agent not found or not accessible by user
- 400: View type "webapp" but agent has `webapp_enabled = false`
- 422: Validation errors (name too long, invalid grid values)
- 409: Max dashboards or max blocks limit reached

---

## Frontend Implementation

### NPM Dependency

Add `react-grid-layout` for the drag-and-drop grid:
```bash
cd frontend && npm install react-grid-layout @types/react-grid-layout
```

### Routes

| Route | File | Description |
|-------|------|-------------|
| `/dashboards` | `src/routes/_layout/dashboards.tsx` | Dashboard management page (list, create, delete) |
| `/dashboards/$dashboardId` | `src/routes/_layout/dashboards.$dashboardId.tsx` | Single dashboard view with grid |

Both are protected routes (inside `_layout/`).

### Sidebar Changes

**File:** `src/components/Sidebar/AppSidebar.tsx`

Replace the simple "Dashboard" menu item with a new component: `SidebarDashboardMenu`.

**File:** `src/components/Sidebar/SidebarDashboardMenu.tsx`

Pattern: Follow `WorkspaceSwitcher.tsx` hover menu pattern.

- Uses `DropdownMenu` with `modal={false}`
- Trigger: `SidebarMenuButton` with Home icon + "Dashboard" text
- Clicking trigger directly navigates to `/` (existing behavior preserved)
- Hovering opens dropdown with:
  - **"Main"** → links to `/` (current index page)
  - **Separator**
  - **List of user dashboards** (fetched via React Query) → each links to `/dashboards/{id}`
  - **Separator**
  - **"Manage Dashboards"** → links to `/dashboards`
- Active state indicator (Check icon + accent bg) on current dashboard

**Query key:** `["userDashboards"]` — lightweight list (name + id only, no blocks).

### Component Structure

```
src/components/Dashboard/
├── DashboardHeader.tsx           (existing - keep as is)
├── UserDashboards/
│   ├── DashboardGrid.tsx         — react-grid-layout wrapper, handles drag/resize
│   ├── DashboardBlock.tsx        — Single block container: header bar + view content
│   ├── AddBlockDialog.tsx        — Modal to add a new block (agent picker + view type)
│   ├── EditBlockDialog.tsx       — Modal to edit block settings (view type, title, border)
│   ├── views/
│   │   ├── WebAppView.tsx        — iframe embedding agent webapp
│   │   ├── LatestSessionView.tsx — Recent session card with status + link to full view
│   │   └── LatestTasksView.tsx   — Recent tasks list for the agent
│   └── ManageDashboardsPage.tsx  — List/create/delete dashboards
```

### DashboardGrid Component

- Wraps `react-grid-layout` `<ResponsiveGridLayout>`
- Default: 12-column grid
- Breakpoints: `lg: 12 cols, md: 8 cols, sm: 4 cols, xs: 2 cols`
- Min block size: 2×2 (cols × rows)
- Drag handles on block header bar
- On layout change → debounced PUT to `/blocks/layout` (300ms)
- "Add Block" button (+ icon) shown at top-right of grid area
- Edit mode toggle: when off, grid is static (no dragging); when on, blocks can be rearranged

### DashboardBlock Component

- **Header bar**: Agent name (or custom title), color indicator from agent's `ui_color_preset`, kebab menu (Edit, Remove)
- **Border**: Conditional `border` class based on `show_border` config
- **Content area**: Renders the appropriate view component based on `view_type`
- **Loading state**: Skeleton placeholder while view content loads
- **Error state**: "Agent unavailable" if agent was deleted (block persists briefly until cascade removes it)

### View Components

#### WebAppView

- Renders an `<iframe>` pointing to the agent's webapp preview URL: `/api/v1/agents/{agent_id}/webapp/`
- Uses the authenticated user's token (owner preview route, not share URL)
- Shows "Webapp not enabled" placeholder if agent's `webapp_enabled` is false
- Shows "Environment not running" with activate button if env is suspended
- iframe gets `width: 100%`, `height: 100%` of block content area
- `sandbox` attribute for security: `allow-scripts allow-same-origin allow-forms`

#### LatestSessionView

- Fetches latest session for the agent via existing sessions API (filter by agent, limit 1, ordered by `last_message_at`)
- Displays: session title, mode indicator (building/conversation), result state badge, last message preview (truncated), time since last activity
- "View Session" button → navigates to `/sessions/{sessionId}`
- "All Sessions" link → navigates to `/sessions/agent/{agentId}`
- Empty state: "No sessions yet" with muted text

#### LatestTasksView

- Fetches recent tasks for the agent via existing tasks API (filter by agent, limit 5, ordered by `updated_at`)
- Displays compact task list: status icon + color, task title (truncated), time
- Status color coding: matches existing task status colors (violet=new, blue=running, amber=pending, red=error, green=completed)
- "View All" link → navigates to `/tasks` (filtered)
- Empty state: "No tasks yet"

### State Management

**Query keys:**
- `["userDashboards"]` — dashboard list (sidebar menu + manage page)
- `["userDashboard", dashboardId]` — single dashboard with blocks
- `["dashboardBlockSessions", agentId]` — latest session for a block
- `["dashboardBlockTasks", agentId]` — latest tasks for a block

**Mutations:**
- `createDashboard` → invalidates `["userDashboards"]`
- `deleteDashboard` → invalidates `["userDashboards"]`
- `updateDashboard` → invalidates `["userDashboards"]`, `["userDashboard", id]`
- `addBlock` → invalidates `["userDashboard", dashboardId]`
- `updateBlock` → invalidates `["userDashboard", dashboardId]`
- `deleteBlock` → invalidates `["userDashboard", dashboardId]`
- `updateBlockLayout` → optimistic update (move blocks instantly, sync in background)

**Refresh strategy for block content:**
- Block views auto-refresh every 30 seconds (configurable later via `config` JSON)
- Uses `refetchInterval: 30000` on block content queries
- Manual refresh button per block (optional, in header kebab menu)

### User Flows

#### Creating a Dashboard

1. User hovers "Dashboard" in sidebar → clicks "Manage Dashboards"
2. Manage page shows list of existing dashboards as cards
3. User clicks "New Dashboard" button
4. Dialog: name field (required), description (optional)
5. Dashboard created, user redirected to `/dashboards/{id}` (empty grid)

#### Adding a Block

1. User clicks "+ Add Block" button on dashboard grid
2. Dialog opens with:
   - Agent picker dropdown (all user's agents, no workspace filter, shows agent color + name)
   - View type radio: "Web App", "Latest Session", "Latest Tasks"
   - "Web App" option disabled with tooltip if selected agent has `webapp_enabled = false`
3. User selects agent + view type, clicks "Add"
4. Block appears at first available grid position (bottom of grid)

#### Rearranging Blocks

1. User enables "Edit Layout" toggle (top of grid)
2. Blocks show drag handles and resize handles
3. User drags blocks to rearrange or resizes them
4. Layout auto-saves (debounced) via bulk layout update API
5. User disables "Edit Layout" to lock layout

#### Editing a Block

1. User clicks kebab menu on block header → "Edit"
2. Dialog: view type, custom title, show border toggle
3. Changes saved, block re-renders

#### Deleting a Block

1. User clicks kebab menu → "Remove"
2. Confirmation (inline, not modal): "Remove this block?"
3. Block removed, grid reflows

### Manage Dashboards Page

- Card grid showing each dashboard: name, description, block count, last updated
- "New Dashboard" card/button
- Each card: click to open dashboard, kebab menu (Rename, Delete)
- Delete requires confirmation dialog
- Empty state: "Create your first dashboard to monitor your agents at a glance"

---

## Database Migrations

### Migration: `add_user_dashboard_tables.py`

**Upgrade:**
1. Create `user_dashboard` table with all columns
2. Create `user_dashboard_block` table with all columns
3. Add indexes: `ix_user_dashboard_owner_id`, `ix_user_dashboard_owner_sort`, `ix_user_dashboard_block_dashboard_id`, `ix_user_dashboard_block_agent_id`
4. Add foreign keys with CASCADE delete

**Downgrade:**
1. Drop `user_dashboard_block` table
2. Drop `user_dashboard` table

---

## Error Handling & Edge Cases

### Agent Deleted While on Dashboard

- Block's `agent_id` FK has CASCADE delete → block is automatically removed from DB
- Frontend: if a block's agent query returns 404, show "Agent removed" placeholder, then refetch dashboard to get updated block list

### Environment Not Running (WebApp View)

- WebApp iframe shows loading/error state from the existing webapp serving logic
- Block header shows "Environment suspended" indicator
- User can click "Activate" from block header (reuses existing environment activation)

### Webapp Not Enabled

- If `view_type = "webapp"` but agent has `webapp_enabled = false`:
  - Block shows "Web App not enabled for this agent" with link to agent settings
  - Backend validation prevents creating a block with `webapp = true` for non-webapp agents, but agent settings can change later

### Dashboard Limit Reached

- Backend returns 409 when user tries to create dashboard #11
- Frontend disables "New Dashboard" button and shows tooltip: "Maximum 10 dashboards"

### Block Limit Reached

- Backend returns 409 when user tries to add block #21 to a dashboard
- Frontend disables "Add Block" button with tooltip: "Maximum 20 blocks per dashboard"

### Concurrent Layout Edits

- Last-write-wins for grid layout (acceptable for single-user dashboards)
- Optimistic updates on frontend; if server rejects, revert to server state

---

## UI/UX Considerations

### Block Header

- Compact: agent color dot + title + kebab menu
- Title: custom title if set, otherwise agent name
- Color dot uses agent's `ui_color_preset` via existing `getColorPreset()` utility

### View Type Indicators

- Small icon in block header corner indicating view type:
  - WebApp: Globe icon
  - Latest Session: MessageSquare icon
  - Latest Tasks: ClipboardList icon

### Empty Dashboard

- Centered illustration + "Add your first block" CTA
- Suggestion text: "Add agent blocks to create your monitoring dashboard"

### Responsive Behavior

- Grid reflows based on `react-grid-layout` responsive breakpoints
- Mobile: 2-column layout, blocks stack vertically
- Block content adapts to available space

### Sidebar Menu

- Dashboard list in hover menu: max 5 shown, then "View all..." link to manage page
- Active dashboard highlighted with accent background + check icon (same pattern as workspace switcher)

---

## Integration Points

- **Existing Agent List API**: Reused for agent picker in "Add Block" dialog — called without workspace filter param
- **Existing Sessions API**: `LatestSessionView` calls sessions list endpoint filtered by agent_id, limit=1
- **Existing Tasks API**: `LatestTasksView` calls tasks list endpoint filtered by agent_id, limit=5
- **Existing Webapp Serving**: `WebAppView` uses owner preview route `/api/v1/agents/{agent_id}/webapp/`
- **Agent Color Presets**: `getColorPreset()` utility from `src/utils/colorPresets.ts`
- **Sidebar Components**: shadcn/ui Sidebar primitives (SidebarMenu, SidebarMenuItem, SidebarMenuButton)
- **API Client Regeneration**: Run `bash scripts/generate-client.sh` after adding backend routes

---

## Future Enhancements (Out of Scope)

- **Real-time block updates via WebSocket**: Subscribe to session/task events for live block refresh (currently using polling)
- **Additional view types**: "Agent Status" (environment health), "Activity Feed", "Chat Quick-Send", "Custom Metrics"
- **Dashboard sharing**: Share dashboard view with other users or via public link
- **Per-block refresh interval**: Configurable via `config` JSON column (infrastructure ready)
- **Dashboard templates**: Pre-built dashboard layouts for common monitoring scenarios
- **Block-level auto-refresh configuration**: Per-view-type refresh intervals stored in `config`
- **Drag blocks between dashboards**: Move blocks from one dashboard to another
- **Dashboard duplication**: Clone an existing dashboard as starting point
- **Fullscreen block view**: Expand a single block to fill the page

---

## Summary Checklist

### Backend Tasks

- [ ] Create model file `backend/app/models/users/user_dashboard.py` with `UserDashboard`, `UserDashboardBlock`, and all schema variants (Base, Create, Update, Public)
- [ ] Register models in `backend/app/models/__init__.py`
- [ ] Create service `backend/app/services/users/user_dashboard_service.py` with CRUD operations, layout bulk update, and validation logic
- [ ] Create routes `backend/app/api/routes/user_dashboards.py` with all endpoints (list, create, get, update, delete dashboards; add, update, delete, layout-update blocks)
- [ ] Register router in `backend/app/api/main.py` with prefix `/api/v1/dashboards` and tag `"Dashboards"`
- [ ] Generate Alembic migration: `alembic revision --autogenerate -m "add user dashboard tables"`
- [ ] Review migration, add indexes, verify CASCADE behavior
- [ ] Apply migration: `alembic upgrade head`

### Frontend Tasks

- [ ] Install `react-grid-layout` and `@types/react-grid-layout`
- [ ] Regenerate API client: `bash scripts/generate-client.sh`
- [ ] Create `SidebarDashboardMenu` component following `WorkspaceSwitcher` hover menu pattern
- [ ] Replace simple "Dashboard" item in `AppSidebar.tsx` with `SidebarDashboardMenu`
- [ ] Create route `src/routes/_layout/dashboards.tsx` (manage dashboards page)
- [ ] Create route `src/routes/_layout/dashboards.$dashboardId.tsx` (single dashboard view)
- [ ] Create `src/components/Dashboard/UserDashboards/DashboardGrid.tsx` (react-grid-layout wrapper)
- [ ] Create `src/components/Dashboard/UserDashboards/DashboardBlock.tsx` (block container)
- [ ] Create `src/components/Dashboard/UserDashboards/AddBlockDialog.tsx` (agent picker + view type)
- [ ] Create `src/components/Dashboard/UserDashboards/EditBlockDialog.tsx` (block settings)
- [ ] Create `src/components/Dashboard/UserDashboards/views/WebAppView.tsx` (iframe embed)
- [ ] Create `src/components/Dashboard/UserDashboards/views/LatestSessionView.tsx` (session summary)
- [ ] Create `src/components/Dashboard/UserDashboards/views/LatestTasksView.tsx` (task list)
- [ ] Create `src/components/Dashboard/UserDashboards/ManageDashboardsPage.tsx` (list/create/delete)

### Testing & Validation

- [ ] Verify dashboard CRUD operations (create, list, update, delete)
- [ ] Verify block CRUD operations including bulk layout update
- [ ] Verify ownership authorization (user cannot access another user's dashboard)
- [ ] Verify agent access check when adding blocks (only owned/shared agents)
- [ ] Verify CASCADE delete: deleting agent removes its blocks, deleting dashboard removes all blocks
- [ ] Verify max limits (10 dashboards, 20 blocks per dashboard)
- [ ] Verify webapp view type validation (agent must have webapp_enabled)
- [ ] Verify sidebar hover menu shows dashboards and navigates correctly
- [ ] Verify grid drag-and-drop persists layout
- [ ] Verify block content loads correctly for each view type
- [ ] Verify responsive grid behavior on different screen sizes
