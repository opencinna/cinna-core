# Disclaimer — Technical Details

## File Locations

### Backend
- `backend/app/models/server_config/server_config.py` — `ServerConfig`, `ServerConfigUpdate`, `DisclaimerPublic`
- `backend/app/services/server_config/server_config_service.py` — `ServerConfigService`
- `backend/app/api/routes/server_config.py` — Route handlers (registered in `backend/app/api/main.py`)
- `backend/app/alembic/versions/3a52a997a322_*.py` — Migration adding `server_config` table (down_revision: `c1a4b2d3e5f6`)

### Frontend
- `frontend/src/components/Admin/DisclaimerCard.tsx` — Admin editor card
- `frontend/src/components/Onboarding/DisclaimerModal.tsx` — User-facing blocking modal
- `frontend/src/routes/_layout/admin/server-configuration.tsx` — Admin route with `HashTabs`
- `frontend/src/components/Sidebar/AdminMenu.tsx` — "Server Configuration" menu entry
- `frontend/src/routes/_layout/index.tsx` — Dashboard: disclaimer query, gate logic, modal mounting, ordering with Getting Started

## Database Model

### `ServerConfig` (`table=True`, `__tablename__ = "server_config"`)

Singleton row — only one ever exists; created lazily on first access.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `id` | `uuid.UUID` | `uuid4()` | Primary key |
| `disclaimer_enabled` | `bool` | `False` | Whether the disclaimer is active |
| `disclaimer_markdown` | `str` (Text) | `""` | Markdown content shown in the modal |
| `disclaimer_display_mode` | `str` | `"new_users"` | `"new_users"` or `"every_login"` |
| `disclaimer_version` | `int` | `1` | Incremented on content or mode change |
| `updated_at` | `datetime` | `now(UTC)` | Timestamp of last update |
| `updated_by_id` | `uuid.UUID \| None` | `None` | FK → `user.id` (SET NULL on delete) |

### `ServerConfigUpdate` (Pydantic, no table)

All fields optional; sent as the `PUT /admin/server-config` request body.

| Field | Type |
|-------|------|
| `disclaimer_enabled` | `bool \| None` |
| `disclaimer_markdown` | `str \| None` |
| `disclaimer_display_mode` | `str \| None` |

### `DisclaimerPublic` (Pydantic, no table)

Minimal projection returned to any authenticated user.

| Field | Type | Description |
|-------|------|-------------|
| `enabled` | `bool` | Whether the disclaimer is active |
| `markdown` | `str` | Raw Markdown text |
| `display_mode` | `str` | `"new_users"` or `"every_login"` |
| `version` | `int` | Current version — keys browser storage flags |

## API Endpoints

### `GET /api/v1/server-config/disclaimer`
- **Auth**: Any authenticated user (`CurrentUser`)
- **Response**: `DisclaimerPublic`
- **Behaviour**: Returns the disclaimer projection; lazily creates the singleton row if missing
- **Query key (frontend)**: `["disclaimer"]`

### `GET /api/v1/admin/server-config`
- **Auth**: Superuser only (`get_current_active_superuser`)
- **Response**: Full `ServerConfig` row
- **Query key (frontend)**: `["serverConfig"]`

### `PUT /api/v1/admin/server-config`
- **Auth**: Superuser only
- **Body**: `ServerConfigUpdate` (all optional fields)
- **Response**: Updated `ServerConfig` row
- **Side effect**: Increments `disclaimer_version` if `disclaimer_markdown` or `disclaimer_display_mode` changed; stamps `updated_at` and `updated_by_id` on every call

## Service Layer

`backend/app/services/server_config/server_config_service.py` — static-method style, mirrors `mail_server_service.py`.

### `ServerConfigService.get_or_create(session) -> ServerConfig`
Returns the single `ServerConfig` row. On the very first call (empty table), inserts a new default row. Race-hardened: catches `IntegrityError` from a concurrent insert, rolls back, and re-reads the existing row.

### `ServerConfigService.update(session, data, user_id) -> ServerConfig`
Applies a partial `ServerConfigUpdate`. Computes `content_changed` by comparing incoming values for `disclaimer_markdown` and `disclaimer_display_mode` against the current row. Increments `disclaimer_version` only when `content_changed` is true.

### `ServerConfigService.to_disclaimer_public(config) -> DisclaimerPublic`
Converts a `ServerConfig` row to the `DisclaimerPublic` projection.

## Frontend Components

### `DisclaimerCard` (`frontend/src/components/Admin/DisclaimerCard.tsx`)

Admin editor rendered inside the "Interface" tab of the Server Configuration page.

- Data source: `useQuery({ queryKey: ["serverConfig"], queryFn: ServerConfigService.getServerConfig })`
- Mutation: `ServerConfigService.updateServerConfig` — on success, invalidates `["serverConfig"]` and `["disclaimer"]`
- **Enable/Disable Switch**: calls `updateMutation.mutate({ disclaimer_enabled: checked })`
- **"Edit Disclaimer Message" Dialog**: split-pane — `Textarea` (left) + `MarkdownRenderer` live preview (right); draft state initialised from stored value when the dialog opens; Save calls `updateMutation.mutate({ disclaimer_markdown: draftMarkdown })`
- **"Show disclaimer" Select**: values `"new_users"` (label: "New User Only") and `"every_login"` (label: "Every Login"); calls `updateMutation.mutate({ disclaimer_display_mode: value })`

### `DisclaimerModal` (`frontend/src/components/Onboarding/DisclaimerModal.tsx`)

Non-dismissible user-facing dialog.

- Props: `open: boolean`, `markdown: string`, `onAcknowledge: () => void`
- Dismissal suppressed via `onPointerDownOutside`, `onInteractOutside`, `onEscapeKeyDown` — all call `e.preventDefault()`
- `showCloseButton={false}` removes the default Dialog close button
- Renders `<MarkdownRenderer content={markdown} />` in a scrollable `prose` container (max-height 55vh)
- Single action: `<Button onClick={onAcknowledge}>I Understand</Button>`
- Icon: `ShieldCheck` (Lucide, violet accent)

### Admin Route (`frontend/src/routes/_layout/admin/server-configuration.tsx`)

- Route path: `/admin/server-configuration`
- `beforeLoad` guard: redirects to `/login` if not authenticated; redirects to `/` if not `is_superuser`
- Renders `HashTabs` with a single tab `interface` → `<DisclaimerCard />`
- Page header: "Server Configuration" / "Configure server-wide settings"

### `AdminMenu` (`frontend/src/components/Sidebar/AdminMenu.tsx`)

Added entry: `RouterLink to="/admin/server-configuration"` → label "Server Configuration". Follows the same `DropdownMenuItem` pattern as other admin links; the enclosing admin menu is already superuser-gated.

## Dashboard Gate Logic (`frontend/src/routes/_layout/index.tsx`)

The disclaimer gate and ordering logic live in the Dashboard route component.

```
Query ["disclaimer"] → ServerConfigService.getDisclaimer() → DisclaimerPublic
```

**Storage key computation:**
```
display_mode === "every_login"  → sessionStorage key: disclaimer_session_v{version}
display_mode === "new_users"    → localStorage key:   disclaimer_ack_v{version}
```

**`shouldShowDisclaimer` (boolean):**
```
disclaimer?.enabled && disclaimer.markdown.trim() && !disclaimerSeen
```
Where `disclaimerSeen` reads from the appropriate storage object using the versioned key.

**`acknowledgeDisclaimer` callback (called by `DisclaimerModal.onAcknowledge`):**
1. Writes `"true"` to the versioned storage key
2. Increments `disclaimerAckTick` (state counter) to force re-evaluation of `disclaimerSeen`
3. If `pendingGettingStarted` is set, clears it and calls `setShowGettingStarted(true)`

**Ordering with Getting Started Modal:**
- `DisclaimerModal` is mounted unconditionally when `disclaimer` data is available; `open={shouldShowDisclaimer}`
- `GettingStartedModal` receives `open={showGettingStarted && !shouldShowDisclaimer}` — even if `showGettingStarted` is true, the Getting Started Modal will not render while a disclaimer is pending
- In `ApiKeyOnboarding.onComplete`: if `shouldShowDisclaimer` is true at that moment, sets `pendingGettingStarted=true` instead of immediately calling `setShowGettingStarted(true)`

## Migration

Revision: `3a52a997a322`
Down revision: `c1a4b2d3e5f6`
Operation: Creates the `server_config` table with all columns described above. No changes to existing tables.

---

*Last updated: 2026-06-14*
