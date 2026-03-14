# User Dashboards — Technical Reference

## Database Models

**File:** `backend/app/models/user_dashboard.py`

### `user_dashboard_block_prompt_action` table

| Column | Type | Constraints | Default |
|--------|------|-------------|---------|
| `id` | UUID | PK | uuid4 |
| `block_id` | UUID | FK → user_dashboard_block.id CASCADE, NOT NULL | — |
| `prompt_text` | TEXT (max 2000) | NOT NULL | — |
| `label` | VARCHAR(100) | nullable | NULL |
| `sort_order` | INTEGER | NOT NULL | 0 |
| `created_at` | DATETIME | NOT NULL | utcnow |
| `updated_at` | DATETIME | NOT NULL | utcnow |

Indexes: `ix_user_dashboard_block_prompt_action_block_id` (block_id)

Cascade: `ondelete="CASCADE"` on block_id FK — deleting a block removes all its prompt actions.

### `user_dashboard` table

| Column | Type | Constraints | Default |
|--------|------|-------------|---------|
| `id` | UUID | PK | uuid4 |
| `owner_id` | UUID | FK → user.id CASCADE, NOT NULL | — |
| `name` | VARCHAR(255) | NOT NULL | — |
| `description` | TEXT | nullable | NULL |
| `sort_order` | INTEGER | NOT NULL | 0 |
| `created_at` | DATETIME | NOT NULL | utcnow |
| `updated_at` | DATETIME | NOT NULL | utcnow |

Indexes: `ix_user_dashboard_owner_id` (owner_id), `ix_user_dashboard_owner_sort` (owner_id, sort_order)

### `user_dashboard_block` table

| Column | Type | Constraints | Default |
|--------|------|-------------|---------|
| `id` | UUID | PK | uuid4 |
| `dashboard_id` | UUID | FK → user_dashboard.id CASCADE, NOT NULL | — |
| `agent_id` | UUID | FK → agent.id CASCADE, NOT NULL | — |
| `view_type` | VARCHAR(50) | NOT NULL | `"latest_session"` |
| `title` | VARCHAR(255) | nullable | NULL |
| `show_border` | BOOLEAN | NOT NULL | true |
| `show_header` | BOOLEAN | NOT NULL | false |
| `grid_x` | INTEGER | NOT NULL | 0 |
| `grid_y` | INTEGER | NOT NULL | 0 |
| `grid_w` | INTEGER | NOT NULL | 2 |
| `grid_h` | INTEGER | NOT NULL | 2 |
| `config` | JSON | nullable | NULL |
| `created_at` | DATETIME | NOT NULL | utcnow |
| `updated_at` | DATETIME | NOT NULL | utcnow |

Indexes: `ix_user_dashboard_block_dashboard_id` (dashboard_id), `ix_user_dashboard_block_agent_id` (agent_id)

Note: `config` is reserved for future per-view-type settings (e.g., refresh interval). Not used in current implementation.

Note: The `agent_id` foreign key (`foreign_key="agent.id"` with `ondelete="CASCADE"`) was added via a separate migration after initial table creation.

`UserDashboardBlock` has a `prompt_actions` Relationship to `UserDashboardBlockPromptAction` (cascade: all, delete-orphan, ordered by `sort_order`).

### Schema classes

The `view_type` field differs between the DB model and the Pydantic schemas:
- **DB model** (`UserDashboardBlock`): plain `str` field with `max_length=50`
- **Pydantic schemas** (`UserDashboardBlockBase`, `UserDashboardBlockUpdate`): `Literal["webapp", "latest_session", "latest_tasks"]` for validation

```
UserDashboardBase           — name, description
UserDashboard               — DB table; has blocks: List[UserDashboardBlock]
UserDashboardCreate         — inherits Base
UserDashboardUpdate         — name|None, description|None, sort_order|None
UserDashboardPublic         — id, name, description, sort_order, created_at, updated_at, blocks

UserDashboardBlockBase      — agent_id, view_type (Literal), title, show_border, show_header, grid_x/y/w/h
UserDashboardBlock          — DB table; view_type as plain str; belongs to UserDashboard; has prompt_actions
UserDashboardBlockCreate    — inherits Base
UserDashboardBlockUpdate    — all optional: view_type (Literal|None), title, show_border, show_header, grid_x/y/w/h, config
UserDashboardBlockPublic    — id, agent_id, view_type (str), title, show_border, show_header, grid_x/y/w/h, config, prompt_actions, created_at, updated_at

BlockLayoutUpdate           — block_id, grid_x, grid_y, grid_w, grid_h (for bulk layout endpoint)

UserDashboardBlockPromptActionBase   — prompt_text (1–2000 chars), label (max 100 chars, nullable), sort_order
UserDashboardBlockPromptAction       — DB table; belongs to UserDashboardBlock
UserDashboardBlockPromptActionCreate — inherits Base
UserDashboardBlockPromptActionUpdate — all optional: prompt_text, label, sort_order
UserDashboardBlockPromptActionPublic — id, block_id, prompt_text, label, sort_order, created_at, updated_at
```

---

## API Routes

**File:** `backend/app/api/routes/user_dashboards.py`
**Prefix:** `/api/v1/dashboards`
**Tag:** `"Dashboards"`
**Auth:** All endpoints require `CurrentUser`

| Method | Path | Description | Response |
|--------|------|-------------|----------|
| GET | `/` | List user's dashboards with blocks | `list[UserDashboardPublic]` |
| POST | `/` | Create dashboard | `UserDashboardPublic` |
| GET | `/{dashboard_id}` | Get dashboard with blocks | `UserDashboardPublic` |
| PUT | `/{dashboard_id}` | Update dashboard metadata | `UserDashboardPublic` |
| DELETE | `/{dashboard_id}` | Delete dashboard | `Message` |
| POST | `/{dashboard_id}/blocks` | Add block | `UserDashboardBlockPublic` |
| PUT | `/{dashboard_id}/blocks/layout` | Bulk update grid positions | `list[UserDashboardBlockPublic]` |
| PUT | `/{dashboard_id}/blocks/{block_id}` | Update block config | `UserDashboardBlockPublic` |
| DELETE | `/{dashboard_id}/blocks/{block_id}` | Delete block | `Message` |
| GET | `/{dashboard_id}/blocks/{block_id}/prompt-actions` | List prompt actions for a block | `list[UserDashboardBlockPromptActionPublic]` |
| POST | `/{dashboard_id}/blocks/{block_id}/prompt-actions` | Create prompt action | `UserDashboardBlockPromptActionPublic` |
| PUT | `/{dashboard_id}/blocks/{block_id}/prompt-actions/{action_id}` | Update prompt action | `UserDashboardBlockPromptActionPublic` |
| DELETE | `/{dashboard_id}/blocks/{block_id}/prompt-actions/{action_id}` | Delete prompt action | `Message` |
| GET | `/{dashboard_id}/blocks/{block_id}/latest-session` | Get most recent session for block (within 12 h) | `SessionPublic` |

The `/latest-session` route path uses the literal segment `/latest-session` to avoid conflicting with the `/{action_id}` prompt-action routes. It verifies dashboard ownership, then calls `SessionService.get_recent_block_session()` which queries sessions by `dashboard_block_id`, `user_id`, and `last_message_at >= now - 12h`. Returns 404 if no qualifying session is found.

The `/blocks/layout` route is registered **before** `/{block_id}` to avoid FastAPI path conflict. Prompt action routes are nested under `/{block_id}/prompt-actions` — the literal path segment prevents any conflict.

### Webapp Owner Preview Routes (iframe auth)

**File:** `backend/app/api/routes/webapp.py`
**Prefix:** `/api/v1/agents/{agent_id}/webapp`

The `serve_webapp_file` and `webapp_data_api` endpoints support three auth methods for iframe embedding:

1. **Authorization header** — standard Bearer token (used by the main app's OpenAPI client)
2. **`?token=` query parameter** — JWT passed via URL (used by iframe initial load)
3. **`webapp_owner_token` cookie** — httponly cookie set on first `?token=` request, scoped to `/api/v1/agents/{agent_id}/webapp`

Auth resolution in `backend/app/api/routes/webapp.py:_resolve_user_from_token()` — validates JWT and returns User, same logic as `get_current_user` in deps but without the OAuth2PasswordBearer dependency.

Cookie is set via `Response.set_cookie()` with `httponly=True`, `samesite="strict"`, path-scoped to the agent's webapp prefix, `max_age=8*24*3600` (matches JWT expiry).

### Error codes

| Code | Condition |
|------|-----------|
| 404 | Dashboard or block not found |
| 403 | Dashboard not owned by current user |
| 400 | Agent not found or not accessible by user |
| 400 | `view_type = "webapp"` but agent has `webapp_enabled = False` |
| 409 | Max dashboards (10) or max blocks (20) limit reached |
| 422 | Validation error: invalid view_type, negative grid values, name too long |

---

## Service Layer

**File:** `backend/app/services/user_dashboard_service.py`

```python
class UserDashboardService:
    list_dashboards(session, owner_id) -> list[UserDashboard]
        # Ordered by sort_order; eager loads blocks + prompt_actions via chained selectinload

    create_dashboard(session, owner_id, data) -> UserDashboard
        # Enforces MAX_DASHBOARDS_PER_USER = 10
        # sort_order = max existing + 1

    get_dashboard(session, dashboard_id, owner_id) -> UserDashboard
        # Raises 404 if not found; 403 if owner mismatch
        # Eager loads blocks + prompt_actions (chained selectinload)

    _get_block(session, dashboard_id, block_id) -> UserDashboardBlock
        # Fetches block ensuring it belongs to dashboard; raises 404 if not found

    update_dashboard(session, dashboard_id, owner_id, data) -> UserDashboard

    delete_dashboard(session, dashboard_id, owner_id) -> bool

    add_block(session, dashboard_id, owner_id, data) -> UserDashboardBlock
        # Enforces MAX_BLOCKS_PER_DASHBOARD = 20
        # Validates view_type in {"webapp", "latest_session", "latest_tasks"}
        # Validates agent.owner_id == owner_id (no workspace filter)
        # If view_type == "webapp": validates agent.webapp_enabled == True

    update_block(session, dashboard_id, block_id, owner_id, data) -> UserDashboardBlock

    delete_block(session, dashboard_id, block_id, owner_id) -> bool

    update_block_layout(session, dashboard_id, owner_id, layouts) -> list[UserDashboardBlock]
        # Single transaction for all grid position updates

    list_prompt_actions(session, dashboard_id, block_id, owner_id) -> list[UserDashboardBlockPromptAction]
        # Ownership via get_dashboard(); block verified via _get_block()
        # Returns ordered by sort_order

    create_prompt_action(session, dashboard_id, block_id, owner_id, data) -> UserDashboardBlockPromptAction

    update_prompt_action(session, dashboard_id, block_id, action_id, owner_id, data) -> UserDashboardBlockPromptAction
        # Raises 404 if action not found under this block

    delete_prompt_action(session, dashboard_id, block_id, action_id, owner_id) -> bool
```

---

## Frontend Routes

| Route | File | Description |
|-------|------|-------------|
| `/dashboards` | `src/routes/_layout/dashboards.index.tsx` | Manage dashboards page (list, create, rename, delete) |
| `/dashboards/$dashboardId` | `src/routes/_layout/dashboards.$dashboardId.tsx` | Dashboard view with grid, header actions, and dropdown menu |
| `/dashboard-fullscreen/$dashboardId` | `src/routes/dashboard-fullscreen/$dashboardId.tsx` | Fullscreen view — no sidebar/header, read-only grid |

The manage-dashboards route uses `dashboards.index.tsx` (not `dashboards.tsx`) to avoid a TanStack Router conflict with the co-located `dashboards.$dashboardId.tsx` route segment.

The fullscreen route is outside `_layout/` (no sidebar/header chrome) but has its own `beforeLoad` auth guard via `isLoggedIn()` check with redirect to `/login`.

---

## Frontend Components

```
src/routes/_layout/dashboards.$dashboardId.tsx
    — Route component; owns isEditMode/showAddBlock/menuOpen state
    — Sets page header via usePageHeader(): dashboard name + action buttons + dropdown menu
    — Dropdown menu: Open Fullscreen, Edit Dashboard, Delete Dashboard
    — Edit Dashboard dialog (name/description form)
    — Delete Dashboard alert dialog (confirmation + redirect to /dashboards)
    — Passes isEditMode/showAddBlock down to DashboardGrid

src/routes/dashboard-fullscreen/$dashboardId.tsx
    — Standalone fullscreen page (no _layout wrapper)
    — Own auth guard (beforeLoad + isLoggedIn)
    — Renders DashboardGrid with isEditMode=false (read-only)
    — Full viewport: h-screen w-screen overflow-hidden

src/components/Sidebar/SidebarDashboardMenu.tsx
    — Exported as SidebarDashboardSwitcher
    — Dropdown menu in sidebar footer (above SidebarWorkspaceSwitcher)
    — Queries ["userDashboards"]; shows active dashboard name or "Dashboards" as button label
    — Lists all dashboards (no count limit); plus "Manage Dashboards" link
    — Active dashboard highlighted with accent bg + Check icon
    — Does NOT use a hover trigger; user clicks the button to open the dropdown

src/components/Dashboard/UserDashboards/
    ManageDashboardsPage.tsx
        — Uses usePageHeader() from src/routes/_layout to inject page title + "New Dashboard" button
        — Card grid layout: p-6 md:p-8 overflow-y-auto + mx-auto max-w-7xl wrapper
        — Create/rename/delete flows
    DashboardGrid.tsx
        — Accepts isEditMode, showAddBlock, onCloseAddBlock, onRequestAddBlock as props
        — react-grid-layout v2 responsive grid; debounced layout save
        — Passes isEditMode to each DashboardBlock
        — No toolbar/subheader — all actions are in the page header
    DashboardBlock.tsx
        — Accepts isEditMode prop
        — Edit mode: colored header (agent color preset) with dot, view icon, title, kebab menu (Edit/Remove) + always shows border
        — Regular mode: header shown when block.show_header is true (same colored bar, no kebab menu); border only when block.show_border is true
        — Inline delete confirmation replaces block content
        — onError toast handler on delete mutation
        — isHovered state tracks hover; renders PromptActionsOverlay in view mode when block has prompt_actions
        — webappIframeRef: useRef<HTMLIFrameElement | null> — created unconditionally; passed to WebAppView for attachment and to PromptActionsOverlay for context collection
        — Passes isWebApp={block.view_type === "webapp"} and iframeRef={webappIframeRef} (only when webapp) to PromptActionsOverlay
    AddBlockDialog.tsx
        — Agent picker (all owned agents); view type radio group
    EditBlockDialog.tsx
        — Edit view_type, custom title, show_border toggle, show_header toggle
        — "Prompt Actions" section: displays saved actions (delete per-item); "+ Add" form for new actions (save/discard per-item); immediate mutations (no batch save)
    PromptActionsOverlay.tsx
        — Renders at bottom of block content area as absolute overlay; visible on hover OR while streaming
        — Shows one pill button per action (label or truncated prompt_text, max 28 chars)
        — Props: actions, agentId, blockId, dashboardId, isVisible, isWebApp, iframeRef (optional)
        — On mount: calls DashboardsService.getBlockLatestSession() to restore session indicator
        — Left-side session indicator:
            webapp blocks  → MessageCircle icon (muted=no session, primary=active session, clickable to navigate)
            non-webapp     → ExternalLink icon when session exists (clickable to navigate)
        — On button click:
            1. resolveSession() — tries getBlockLatestSession; on 404 creates new session via SessionsService.createSession with dashboard_block_id
            2. For webapp blocks: concurrently calls buildPageContext(iframeRef)
            3. Subscribes to WebSocket stream room before sending: eventService.subscribeToRoom(`session_{id}_stream`)
            4. Sends message via MessagesService.sendMessageStream with optional page_context
        — Streaming state: isStreaming flag; streamingEvents: StreamEvent[]
            — While streaming: shows StreamingMessage (conversationModeUi="compact") above action bar
            — If no events yet: shows Loader2 + "Processing..." text
            — All action buttons disabled while streaming
        — Three useEffect subscriptions (each self-cleaning):
            1. session_interaction_status_changed → sets/clears isStreaming based on newStatus
            2. stream_event → accumulates StreamEvent[] by event_seq (deduped); forwards webapp_action events to iframe via postMessage; stream_completed clears state
            3. Unmount cleanup → unsubscribes all event listeners and stream rooms
        — resolveSession() (module-level helper): tries GET latest-session; creates new on 404
    views/
        WebAppView.tsx         — <iframe> embedding /api/v1/agents/{id}/webapp/?token={jwt}
        LatestSessionView.tsx  — Fetches latest 10 sessions; scrollable list with mode icon, title, timestamp per row (matches sessions index page style)
        LatestTasksView.tsx    — Fetches recent tasks; status dot color coding
```

---

## React Query State

| Key | Description |
|-----|-------------|
| `["userDashboards"]` | Dashboard list (sidebar + manage page); invalidated on create/update/delete |
| `["userDashboard", dashboardId]` | Single dashboard with blocks; invalidated on block add/update/delete/layout + dashboard edit |
| `["dashboardBlockSessions", agentId]` | Latest session for block; refetchInterval=30000 |
| `["dashboardBlockTasks", agentId]` | Latest tasks for block; refetchInterval=30000 |
| `["allAgents"]` | All owned agents without workspace filter (AddBlockDialog + DashboardViewPage) |

---

## react-grid-layout Integration

`DashboardGrid` uses `react-grid-layout` v2. The v2 API differs significantly from v1:

```typescript
import { ResponsiveGridLayout, useContainerWidth, type Layout } from "react-grid-layout"

// No WidthProvider wrapper — v2 provides useContainerWidth() hook instead
const { containerRef, width } = useContainerWidth()

// Pass width prop directly; only render when width > 0
{width > 0 && (
  <ResponsiveGridLayout
    width={width}
    layouts={{ lg: gridLayout, md: gridLayout, sm: gridLayout, xs: gridLayout }}
    breakpoints={BREAKPOINTS}
    cols={COLS}
    rowHeight={120}
    dragConfig={{ enabled: isEditMode, handle: ".drag-handle", bounded: false, threshold: 3 }}
    resizeConfig={{ enabled: isEditMode, handles: ["se"] }}
    onLayoutChange={handleLayoutChange}
    margin={[8, 8]}
    containerPadding={[16, 16]}
  >
    ...
  </ResponsiveGridLayout>
)}
```

Key differences from v1:
- Named import `ResponsiveGridLayout` (not `Responsive` + `WidthProvider` wrapper)
- `useContainerWidth()` hook provides `containerRef` and `width` (attach `containerRef` to the wrapper div)
- `dragConfig` object replaces `isDraggable` / `draggableHandle` props
- `resizeConfig` object replaces `isResizable` prop
- `@types/react-grid-layout` package is NOT used — v2 ships its own TypeScript types

Grid configuration:
- **Breakpoints**: `lg: 1200, md: 996, sm: 768, xs: 480`
- **Columns**: `lg: 12, md: 8, sm: 4, xs: 2`
- **Row height**: 120px
- **Min block size**: 2×2 (enforced via `minW: 2, minH: 2` in layout items)
- **Drag handle**: `.drag-handle` CSS class on a transparent overlay div (visible on hover in edit mode only)
- **Layout persistence**: On layout change → debounced 300ms → `PUT /blocks/layout`
- **Optimistic**: Layout changes are visible immediately; if save fails, on next refetch the server state is restored

---

## Alembic Migrations

**Initial migration:** `backend/app/alembic/versions/0045add3641e_add_user_dashboard_tables.py`

Creates `user_dashboard` and `user_dashboard_block` tables with all columns, foreign keys (CASCADE), and indexes. Downgrade drops both tables in reverse dependency order.

**Follow-up migration** (separate revision): Adds proper `foreign_key="agent.id"` with `ondelete="CASCADE"` to the `agent_id` column on `user_dashboard_block`. This was applied as a corrective migration after a code review fix.

**`b8c9d0e1f2g3`**: Adds `show_header` boolean column (NOT NULL, server default `false`) to `user_dashboard_block`.

**`361c33705d15`**: Creates `user_dashboard_block_prompt_action` table with `id`, `block_id` (FK CASCADE), `prompt_text`, `label`, `sort_order`, `created_at`, `updated_at`. Creates index on `block_id`.

**`b3029a8e84ac`**: Adds `dashboard_block_id` (UUID, nullable) to the `session` table with FK → `user_dashboard_block.id` (`ondelete="SET NULL"`) and index `ix_session_dashboard_block_id`. This column tags sessions created from prompt actions so the latest-session endpoint can retrieve them efficiently.

---

## Tests

**Location:** `backend/tests/api/dashboards/`

- `conftest.py` — imports environment adapter stubs and AI credential fixtures from shared `tests/utils/fixtures.py`
- `test_dashboards.py` — 11 scenario-based tests covering:
  - Dashboard CRUD lifecycle + ownership guards + 404 for ghost IDs
  - Block CRUD lifecycle + block-level ownership guards
  - Bulk layout update + unknown block_id → 404
  - Max 10 dashboards limit (409)
  - Max 20 blocks per dashboard limit (409)
  - Agent access validation (other user's agent → 400)
  - Webapp view_type validation (webapp_enabled=False → 400, True → 200)
  - Invalid view_type string → 422
  - Prompt action full CRUD lifecycle (create, list, update, delete, cascade with block delete)
  - Prompt action ownership guards (cross-user access, unauthenticated, ghost IDs)
  - Prompt action input validation (empty text, oversized text/label → 422)
  - Block latest-session endpoint: no session → 404, session with no messages → 404 (last_message_at NULL), session with message → 200 with correct session, ownership guard, ghost dashboard_id → 404

Helper utilities: `backend/tests/utils/dashboard.py` — includes `create_prompt_action`, `list_prompt_actions`, `update_prompt_action`, `delete_prompt_action` helpers. `backend/tests/utils/session.py` — `create_dashboard_block_session()` creates a session tagged with `dashboard_block_id`.
