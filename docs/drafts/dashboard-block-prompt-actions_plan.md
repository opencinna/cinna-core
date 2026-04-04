# dashboard-block-prompt-actions — Implementation Plan

## Overview

`dashboard_block_prompt_actions` extends the existing user dashboard blocks with configurable
one-click action buttons. Each block can have multiple prompt actions (each has a prompt text
and optional display label). In view mode, hovering a block reveals small action buttons.
Clicking one creates a new agent session (conversation mode) and sends the prompt as the
first user message. The button then becomes a clickable spinner icon that navigates to the
newly created session.

**Core capabilities:**
- Dashboard blocks support N prompt actions (no backend-enforced cap needed; keep it
  practical — the UI naturally limits what is usable)
- CRUD for prompt actions via dedicated REST endpoints nested under blocks
- Frontend hover overlay with action buttons (view mode only — hidden in edit mode)
- Post-click state: spinner replaces button; click spinner to open session page
- Session created in `conversation` mode via the existing Sessions API

**High-level flow:**
```
User hovers block (view mode)
  → PromptAction buttons appear (one per action)
  → User clicks a button
      → POST /api/v1/sessions  (agent_id, mode="conversation")
      → POST /api/v1/sessions/{id}/messages/stream (prompt_text as content)
      → Button becomes a Loader2 spinner icon (in-progress state stored in component state)
  → User clicks spinner icon
      → navigate("/session/{session_id}")
  → (Or user waits; block auto-refreshes every 30s showing latest session)
```

---

## Architecture Overview

```
UserDashboardBlockPromptAction (new DB table)
  └── FK → user_dashboard_block.id (CASCADE)

API route: /api/v1/dashboards/{dashboard_id}/blocks/{block_id}/prompt-actions
  GET    → list prompt actions for block
  POST   → create prompt action
  PUT    /{action_id} → update prompt action
  DELETE /{action_id} → delete prompt action

Frontend:
  UserDashboardBlockPublic extended with prompt_actions: UserDashboardBlockPromptActionPublic[]
  DashboardBlock.tsx → adds PromptActionsOverlay on hover (view mode only)
  EditBlockDialog.tsx → adds PromptActionsEditor section
  PromptActionsOverlay.tsx → new component (hover state, spinner state, navigation)
```

---

## Data Models

### New table: `user_dashboard_block_prompt_action`

| Column | Type | Constraints | Default |
|--------|------|-------------|---------|
| `id` | UUID | PK | uuid4 |
| `block_id` | UUID | FK → user_dashboard_block.id CASCADE, NOT NULL | — |
| `prompt_text` | TEXT | NOT NULL | — |
| `label` | VARCHAR(100) | nullable | NULL |
| `sort_order` | INTEGER | NOT NULL | 0 |
| `created_at` | DATETIME | NOT NULL | utcnow |
| `updated_at` | DATETIME | NOT NULL | utcnow |

**Indexes:**
- `ix_user_dashboard_block_prompt_action_block_id` on `block_id` (main query path)

**Cascade:** `ondelete="CASCADE"` on block_id FK — deleting a block removes all its prompt actions automatically.

**No FK to agent**: prompt_actions are tied to blocks, not agents directly. The block already carries the `agent_id`.

**No label default**: if label is NULL, the frontend displays a truncated version of the prompt_text.

### SQLModel Schema Classes

```
UserDashboardBlockPromptActionBase (SQLModel)
  prompt_text: str (non-empty, max_length=2000)
  label: str | None (max_length=100, nullable)
  sort_order: int (default=0)

UserDashboardBlockPromptAction (table=True)
  id: UUID PK
  block_id: UUID FK
  prompt_text: str
  label: str | None
  sort_order: int
  created_at: datetime
  updated_at: datetime

UserDashboardBlockPromptActionCreate (inherits Base)
  — no extra fields

UserDashboardBlockPromptActionUpdate (SQLModel)
  prompt_text: str | None
  label: str | None
  sort_order: int | None

UserDashboardBlockPromptActionPublic (SQLModel)
  id: UUID
  block_id: UUID
  prompt_text: str
  label: str | None
  sort_order: int
  created_at: datetime
  updated_at: datetime
```

### Changes to existing models

**`UserDashboardBlock` (DB table):** Add `prompt_actions` Relationship to `UserDashboardBlockPromptAction` (back_populates="block"), with `cascade="all, delete-orphan"`.

**`UserDashboardBlockPublic` (API response):** Add `prompt_actions: list[UserDashboardBlockPromptActionPublic] = []`.

The `prompt_actions` list must be eagerly loaded whenever blocks are loaded. The service layer
already uses `selectinload(UserDashboard.blocks)` — extend it to also load
`selectinload(UserDashboard.blocks).selectinload(UserDashboardBlock.prompt_actions)`.

---

## Security Architecture

- **Ownership via dashboard:** All prompt action endpoints require the caller to own the
  dashboard containing the block. The service resolves: `block → dashboard → owner_id check`.
- **No agent ownership check:** The block creation already validates agent ownership; prompt
  actions only need block ownership.
- **No encryption needed:** Prompt text is user-controlled content, not a secret.
- **Input validation:** `prompt_text` must be non-empty (min_length=1). `label` is optional
  and short (max 100 chars). `sort_order` is an integer with default 0.
- **Rate limiting:** Not required at this stage.
- **Access control rule:** Unauthenticated requests → 401. Authenticated but wrong owner → 403
  (via the dashboard ownership check propagating from `get_dashboard`).

---

## Backend Implementation

### New model file additions

**File:** `backend/app/models/users/user_dashboard.py`

Add `UserDashboardBlockPromptAction`, `UserDashboardBlockPromptActionCreate`,
`UserDashboardBlockPromptActionUpdate`, `UserDashboardBlockPromptActionPublic` to the
existing file (keep all dashboard models in one file as is the current pattern).

Add `prompt_actions` relationship to `UserDashboardBlock`:
```python
prompt_actions: list["UserDashboardBlockPromptAction"] = Relationship(
    back_populates="block",
    sa_relationship_kwargs={"cascade": "all, delete-orphan", "order_by": "UserDashboardBlockPromptAction.sort_order"},
)
```

Update `UserDashboardBlockPublic` to include:
```python
prompt_actions: list[UserDashboardBlockPromptActionPublic] = []
```

### API Routes

**File:** `backend/app/api/routes/user_dashboards.py`

Add four new endpoints under the block namespace. These must be registered **before** the
existing `/{dashboard_id}/blocks/{block_id}` routes to avoid path conflicts with the new
`/prompt-actions` sub-path.

| Method | Path | Description | Response |
|--------|------|-------------|----------|
| GET | `/{dashboard_id}/blocks/{block_id}/prompt-actions` | List prompt actions for a block | `list[UserDashboardBlockPromptActionPublic]` |
| POST | `/{dashboard_id}/blocks/{block_id}/prompt-actions` | Create a prompt action | `UserDashboardBlockPromptActionPublic` |
| PUT | `/{dashboard_id}/blocks/{block_id}/prompt-actions/{action_id}` | Update a prompt action | `UserDashboardBlockPromptActionPublic` |
| DELETE | `/{dashboard_id}/blocks/{block_id}/prompt-actions/{action_id}` | Delete a prompt action | `Message` |

All endpoints use `CurrentUser` + `SessionDep`.

Update `_block_to_public()` helper to include `prompt_actions`:
```python
prompt_actions=[_action_to_public(a) for a in block.prompt_actions]
```

Add `_action_to_public()` helper.

Also update `_dashboard_to_public()` — no change needed since it calls `_block_to_public()`.

Update all imports in the models `__init__.py` to export the new schema classes.

### Service Layer

**File:** `backend/app/services/users/user_dashboard_service.py`

Add methods to `UserDashboardService`:

```python
list_prompt_actions(session, dashboard_id, block_id, owner_id) -> list[UserDashboardBlockPromptAction]
    # Calls get_dashboard() for ownership check
    # Verifies block belongs to dashboard
    # Returns ordered list (by sort_order)

create_prompt_action(session, dashboard_id, block_id, owner_id, data) -> UserDashboardBlockPromptAction
    # Calls get_dashboard() + verify block in dashboard
    # Creates and commits record

update_prompt_action(session, dashboard_id, block_id, action_id, owner_id, data) -> UserDashboardBlockPromptAction
    # Calls get_dashboard() + verify block in dashboard
    # Fetches action by id and block_id
    # Updates and commits

delete_prompt_action(session, dashboard_id, block_id, action_id, owner_id) -> bool
    # Calls get_dashboard() + verify block in dashboard
    # Fetches action, deletes and commits
```

Also update `get_dashboard()` and `list_dashboards()` to eager-load
`prompt_actions` for each block using chained `selectinload`:

```python
.options(
    selectinload(UserDashboard.blocks).selectinload(UserDashboardBlock.prompt_actions)
)
```

Add a private helper `_get_block(session, dashboard_id, block_id)` that fetches a block
ensuring it belongs to the dashboard (raises 404 if not found) — used by all prompt action
service methods.

### Exports

**File:** `backend/app/models/__init__.py`

Add exports for:
- `UserDashboardBlockPromptAction`
- `UserDashboardBlockPromptActionCreate`
- `UserDashboardBlockPromptActionUpdate`
- `UserDashboardBlockPromptActionPublic`

---

## Frontend Implementation

### API Client Regeneration

After backend changes are applied, regenerate the frontend client:
```bash
bash scripts/generate-client.sh
```

This updates `frontend/src/client/types.gen.ts`, `sdk.gen.ts`, and `schemas.gen.ts` to include
`UserDashboardBlockPromptActionPublic` and the new `DashboardsService` methods for
prompt actions.

### State Management

**React Query keys:**
- `["userDashboard", dashboardId]` — already used; will now include `prompt_actions` in
  block data since the API response is updated. No new query key needed for listing.
- Mutations for create/update/delete prompt actions invalidate `["userDashboard", dashboardId]`.

**In-component state for the "active session" (post-click spinner):**
- Store `Map<actionId, sessionId>` in local state within `DashboardBlock` (or
  `PromptActionsOverlay`). No persistence needed. State resets on page reload (which is fine —
  the user can find the session in the latest sessions view).
- Type: `Record<string, string>` where key=actionId, value=sessionId.

### UI Components

#### New: `PromptActionsOverlay.tsx`

**File:** `frontend/src/components/Dashboard/UserDashboards/PromptActionsOverlay.tsx`

Props:
```typescript
interface PromptActionsOverlayProps {
  actions: UserDashboardBlockPromptActionPublic[]
  agentId: string
}
```

Behavior:
- Renders a floating overlay over the block content area using absolute positioning
- Visible on parent hover (CSS group/hover approach or JS hover state on the block container)
- Shows one small pill button per action
- Each button: shows `label` if set, otherwise truncated `prompt_text` (max ~30 chars with ellipsis)
- On click:
  1. Disable the button immediately
  2. Call `SessionsService.createSession({ requestBody: { agent_id: agentId, mode: "conversation" } })`
  3. On session created: call `SessionsService.sendStreamMessage({ sessionId, requestBody: { content: action.prompt_text } })`
  4. Store `{ [action.id]: session.id }` in the active sessions state map
  5. The button transforms into a `Loader2` spinner icon (animated `animate-spin`)
- Spinner click: navigate to `/session/{sessionId}` using TanStack Router's `useNavigate()`
- Layout: buttons stacked vertically or in a flex-wrap row at the bottom of the block,
  semi-transparent background (`bg-background/80 backdrop-blur-sm`), fade-in animation

#### Modified: `DashboardBlock.tsx`

Changes:
- Import `PromptActionsOverlay`
- Add `isHovered` state (using `onMouseEnter`/`onMouseLeave` on the outer container)
- In the content area, wrap with a `relative` container so overlay can position absolutely
- Render `<PromptActionsOverlay>` only when:
  - `!isEditMode` (never show in edit mode)
  - `block.prompt_actions.length > 0`
  - `isHovered` (or always render but conditionally show via CSS opacity/pointer-events)
- Pass `actions={block.prompt_actions}` and `agentId={agent.id}`

Example structure:
```tsx
<div className="flex-1 overflow-hidden min-h-0 relative"
     onMouseEnter={() => setIsHovered(true)}
     onMouseLeave={() => setIsHovered(false)}>
  {renderView()}
  {!isEditMode && block.prompt_actions && block.prompt_actions.length > 0 && (
    <PromptActionsOverlay
      actions={block.prompt_actions}
      agentId={agent.id}
      isVisible={isHovered}
    />
  )}
</div>
```

#### Modified: `EditBlockDialog.tsx`

Add a "Prompt Actions" section at the bottom of the edit dialog (below the show_header toggle).

The section:
- Header: "Prompt Actions" with a "+ Add" button
- List of existing prompt actions (from `block.prompt_actions`)
- Each item shows: label field (optional, placeholder "Button label"), prompt_text textarea,
  delete button (trash icon), drag handle for reordering (optional for MVP — can be sort_order
  based on array position)
- On save, the dialog calls create/update/delete mutations for each changed action

Implementation approach for simplicity:
- Maintain a local `pendingActions` state (copy of block.prompt_actions + any new ones)
- On "+ Add" click: add an empty entry to `pendingActions` with `id = null` (new)
- On save submit: diff `pendingActions` vs. `block.prompt_actions`:
  - New items (no id): call `createPromptAction` for each
  - Updated items: call `updatePromptAction` for each that changed
  - Deleted items: call `deletePromptAction` for each removed
- All mutations fire in parallel with `Promise.all()`
- After all resolve: invalidate `["userDashboard", dashboardId]` and close dialog

Alternative (simpler): Handle prompt actions as a separate mini-UI within the dialog, using
individual "save" actions per item (add button → immediately POSTs; delete button →
immediately DELETEs; edit field → updates on blur or with explicit save per row). This avoids
the diff logic at the cost of more API calls during editing.

**Recommended approach:** Simpler per-item immediate mutations (add=POST, delete=DELETE on
each item independently). The dialog form for view_type/title/show_border/show_header saves
on "Save Changes". Prompt actions are managed independently in the same dialog. This aligns
with how credentials and similar list-based settings work elsewhere.

---

## Database Migrations

**Migration file:** `backend/app/alembic/versions/{hash}_add_dashboard_block_prompt_actions.py`

Operations:
```python
def upgrade() -> None:
    op.create_table(
        "user_dashboard_block_prompt_action",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("block_id", sa.Uuid(), nullable=False),
        sa.Column("prompt_text", sa.Text(), nullable=False),
        sa.Column("label", sa.String(100), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["block_id"], ["user_dashboard_block.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_user_dashboard_block_prompt_action_block_id",
        "user_dashboard_block_prompt_action",
        ["block_id"],
    )

def downgrade() -> None:
    op.drop_index("ix_user_dashboard_block_prompt_action_block_id",
                  table_name="user_dashboard_block_prompt_action")
    op.drop_table("user_dashboard_block_prompt_action")
```

Migration must be generated from inside Docker or with the venv activated:
```bash
make migration  # or: docker compose exec backend alembic revision --autogenerate -m "add_dashboard_block_prompt_actions"
make migrate    # or: docker compose exec backend alembic upgrade head
```

---

## Error Handling and Edge Cases

| Condition | Handling |
|-----------|----------|
| `prompt_text` is empty string | Backend: 422 validation error (min_length=1) |
| Block does not belong to dashboard | Service raises 404 "Block not found" |
| Action does not belong to block | Service raises 404 "Prompt action not found" |
| Dashboard not owned by user | Service raises 403 "Not enough permissions" |
| Agent has no active environment (session creation fails) | Frontend shows toast error |
| Session message stream fails | Frontend shows toast error; spinner reverts to button |
| `agent` is undefined in DashboardBlock | PromptActionsOverlay not rendered (guarded by agent check) |
| Block deleted while session in flight | No issue — session was already created |
| Fullscreen view | PromptActionsOverlay works identically (same DashboardBlock component is reused) |

---

## UI/UX Considerations

- **Prompt action buttons** should be subtle — small pill buttons with limited opacity
  backdrop, appearing only on hover. Use `transition-opacity` for smooth appearance.
- **Labels** should be concise. If label is blank, show truncated prompt_text with max 25 chars.
- **Spinner** replaces the button using the same size/position to avoid layout shift.
- **Multiple actions:** Show all buttons stacked. In practice, 1–3 actions fit naturally;
  more than 5 would be crowded. No hard cap enforced.
- **Edit mode:** No prompt action buttons visible. The "edit block" dialog is the configuration surface.
- **Fullscreen mode:** Prompt action buttons work the same way.
- **Success feedback:** No toast needed after clicking — the spinner IS the feedback.
  A toast only on error.
- **Accessibility:** Buttons need `title` or `aria-label` set to the full prompt_text (even
  if label truncates it).

---

## Integration Points

- **Sessions API (`POST /api/v1/sessions`):** Used to create the new session. The `agent_id`
  comes from the block. `mode="conversation"`.
- **Messages API (`POST /api/v1/sessions/{id}/messages/stream`):** Used to send the prompt
  as the first message immediately after session creation.
- **Session page:** User navigates to `/session/{session_id}` by clicking the spinner.
- **Fullscreen view:** Reuses `DashboardBlock.tsx` → no extra work needed.
- **Client regeneration:** `bash scripts/generate-client.sh` after backend API changes.
- **React Query invalidation:** Prompt action CRUD mutations invalidate
  `["userDashboard", dashboardId]` which refreshes the block data including prompt_actions.

---

## Future Enhancements (Out of Scope)

- **Sort order drag-and-drop** for reordering prompt actions within the edit dialog
- **Per-action color/icon** customization
- **"Start in building mode" toggle** per action (currently always conversation mode)
- **Action execution history** — tracking which sessions were created by which action
- **Max prompt actions cap** per block (e.g., 10) with backend 409 enforcement
- **Action templates** — pre-built prompt suggestions for common agent tasks

---

## Summary Checklist

### Backend Tasks

- [ ] Add `UserDashboardBlockPromptAction` DB model to `backend/app/models/users/user_dashboard.py`
- [ ] Add `UserDashboardBlockPromptActionCreate`, `Update`, `Public` schema classes to same file
- [ ] Add `prompt_actions` Relationship to `UserDashboardBlock` model
- [ ] Update `UserDashboardBlockPublic` to include `prompt_actions: list[UserDashboardBlockPromptActionPublic] = []`
- [ ] Export new classes from `backend/app/models/__init__.py`
- [ ] Update `selectinload` in service to chain-load prompt_actions in `list_dashboards`, `get_dashboard`
- [ ] Add `_get_block()` helper to `UserDashboardService`
- [ ] Add `list_prompt_actions()`, `create_prompt_action()`, `update_prompt_action()`, `delete_prompt_action()` to `UserDashboardService`
- [ ] Add `_action_to_public()` helper and update `_block_to_public()` in routes file
- [ ] Add 4 new API endpoints for prompt actions in `backend/app/api/routes/user_dashboards.py`
- [ ] Generate Alembic migration: `make migration`
- [ ] Review and apply migration: `make migrate`

### Frontend Tasks

- [ ] Regenerate frontend client: `bash scripts/generate-client.sh`
- [ ] Create `PromptActionsOverlay.tsx` component
- [ ] Update `DashboardBlock.tsx` to add hover state and render `PromptActionsOverlay`
- [ ] Update `EditBlockDialog.tsx` to add prompt actions management section
- [ ] Verify TypeScript types compile correctly for new client types

### Testing Tasks

- [ ] Write backend tests in `backend/tests/api/dashboards/test_dashboards.py`
  - Prompt action CRUD lifecycle on a block
  - Prompt actions returned in dashboard/block API responses
  - Ownership guard: other user cannot manage prompt actions
  - 404 for non-existent action or block
  - Validation: empty prompt_text → 422
- [ ] Add `create_prompt_action()`, `list_prompt_actions()`, etc. helpers to `backend/tests/utils/dashboard.py`
- [ ] Run full test suite to confirm no regressions

### Documentation Tasks

- [ ] Update `docs/application/user_dashboards/user_dashboards.md` with new UX flows and block config section
- [ ] Update `docs/application/user_dashboards/user_dashboards_tech.md` with new model, routes, service methods
- [ ] Update `docs/README.md` if needed (user_dashboards entry already exists)
