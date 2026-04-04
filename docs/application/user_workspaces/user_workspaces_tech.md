# User Workspaces - Technical Details

## File Locations

### Backend

- **Model**: `backend/app/models/users/user_workspace.py` - `UserWorkspace`, `UserWorkspaceBase`, `UserWorkspaceCreate`, `UserWorkspaceUpdate`, `UserWorkspacePublic`, `UserWorkspacesPublic`
- **Service**: `backend/app/services/users/user_workspace_service.py` - `UserWorkspaceService`
- **Routes**: `backend/app/api/routes/user_workspaces.py` - CRUD endpoints
- **Migrations**:
  - `backend/app/alembic/versions/3a154fd039f5_add_user_workspaces_support.py` - initial table + FK columns on 4 tables
  - `backend/app/alembic/versions/88ff71b370a1_add_icon_field_to_user_workspace.py` - icon column
  - `backend/app/alembic/versions/8b77ba42b38d_change_credential_workspace_fk_to_set_.py` - credential FK behavior

### Frontend

- **Hook**: `frontend/src/hooks/useWorkspace.tsx` - `useWorkspace()` hook, `WorkspaceProvider` context, `getActiveWorkspaceId()`, `setActiveWorkspaceId()`
- **Switcher**: `frontend/src/components/Common/WorkspaceSwitcher.tsx` - `SidebarWorkspaceSwitcher`
- **Create Modal**: `frontend/src/components/Common/CreateWorkspaceModal.tsx` - `CreateWorkspaceModal`
- **Icon Config**: `frontend/src/config/workspaceIcons.ts` - `WORKSPACE_ICONS` array, `getWorkspaceIcon()` helper

### Modified Entity Models (workspace FK column)

- `backend/app/models/agents/agent.py` - `user_workspace_id` on `AgentBase`, `Agent`, `AgentPublic`, `AgentCreate`
- `backend/app/models/credentials/credential.py` - `user_workspace_id` on `CredentialCreate`, `Credential`, `CredentialPublic`
- `backend/app/models/sessions/session.py` - `user_workspace_id` on `SessionBase`, `Session`, `SessionPublic`
- `backend/app/models/sessions/activity.py` - `user_workspace_id` on `ActivityBase`, `ActivityPublic`
- `backend/app/models/tasks/input_task.py` - `user_workspace_id` on `InputTaskBase`, `InputTask`, `InputTaskPublic`
- `backend/app/models/knowledge/knowledge.py` - `user_workspace_id` on `KnowledgeSource` (non-nullable, with unique index)

### Routes with Workspace Filtering

- `backend/app/api/routes/agents.py` - `user_workspace_id` query param on list endpoint
- `backend/app/api/routes/credentials.py` - same pattern
- `backend/app/api/routes/sessions.py` - same pattern
- `backend/app/api/routes/activities.py` - same pattern
- `backend/app/api/routes/input_tasks.py` - same pattern
- `backend/app/api/routes/knowledge.py` - same pattern

### Frontend Routes Using Workspace Filter

- `frontend/src/routes/_layout/agents.tsx` - query key includes `activeWorkspaceId`
- `frontend/src/routes/_layout/credentials.tsx`
- `frontend/src/routes/_layout/sessions/index.tsx`
- `frontend/src/routes/_layout/activities.tsx`
- `frontend/src/routes/_layout/tasks/index.tsx`
- `frontend/src/routes/_layout/index.tsx` (dashboard)
- `frontend/src/routes/_layout/agent/creating.tsx`
- `frontend/src/routes/_layout/agent/$agentId/conversations.tsx`
- `frontend/src/routes/_layout/task/$taskId.tsx`

### Tests

- `backend/tests/api/workspaces/test_workspaces.py` - workspace CRUD tests
- `backend/tests/api/workspaces/test_credentials_workspaces.py` - credential workspace filtering
- `backend/tests/utils/workspace.py` - test utilities

## Database Schema

### Table: `user_workspace`

| Field | Type | Notes |
|-------|------|-------|
| id | UUID | PK |
| user_id | UUID | FK to `user.id`, CASCADE delete |
| name | str | min 1, max 255 chars |
| icon | str (nullable) | max 50 chars, icon identifier string |
| created_at | datetime | UTC |
| updated_at | datetime | UTC |

### FK Column on Related Tables

`user_workspace_id` (UUID, nullable, FK to `user_workspace.id`) added to: `agent`, `credential`, `session`, `activity`, `input_task`, `knowledge_source`

Null value = entity belongs to Default workspace.

## API Endpoints

`backend/app/api/routes/user_workspaces.py`:

- `GET /api/v1/user-workspaces/` - List user's workspaces (paginated via `skip`/`limit`)
- `POST /api/v1/user-workspaces/` - Create workspace (body: `UserWorkspaceCreate` with `name`, optional `icon`)
- `GET /api/v1/user-workspaces/{workspace_id}` - Get single workspace
- `PUT /api/v1/user-workspaces/{workspace_id}` - Update workspace (body: `UserWorkspaceUpdate`)
- `DELETE /api/v1/user-workspaces/{workspace_id}` - Delete workspace (returns `Message`)

All endpoints require authentication (`CurrentUser`). GET/PUT/DELETE verify `workspace.user_id == current_user.id`.

### Workspace Filtering on Entity List Endpoints

All entity list endpoints accept optional `user_workspace_id` query parameter:
- Not provided → returns ALL entities (no filter)
- `null` value → filters for `user_workspace_id IS NULL` (default workspace)
- UUID value → filters for exact workspace match

## Services & Key Methods

`backend/app/services/users/user_workspace_service.py` - `UserWorkspaceService`:
- `create_workspace()` - validates and creates workspace for user
- `get_workspace()` - fetch by ID
- `get_user_workspaces()` - paginated list for user
- `count_user_workspaces()` - count for user
- `update_workspace()` - partial update with `model_dump(exclude_unset=True)`
- `delete_workspace()` - delete by ID

### Workspace Inheritance in Other Services

- `SessionService.create_session()` - reads `agent.user_workspace_id` and assigns to new session
- `ActivityService.create_activity()` - checks session first, then agent, to determine workspace

## Frontend Components

### WorkspaceProvider (`frontend/src/hooks/useWorkspace.tsx`)

React Context provider wrapping the app in `__root.tsx`. Provides shared workspace state:
- `activeWorkspaceId` - current workspace (state)
- `setActiveWorkspaceIdState` - state setter
- `previousWorkspaceId` - mutable ref for change detection

### useWorkspace Hook (`frontend/src/hooks/useWorkspace.tsx`)

Central workspace state management. Returns:
- `workspaces` - list of user workspaces (from `useQuery`)
- `activeWorkspace` - current workspace object or `"default"`
- `activeWorkspaceId` - current workspace ID or `null`
- `switchWorkspace()` - updates localStorage and state
- `createWorkspaceMutation` - creates workspace and auto-switches to it
- `deleteWorkspaceMutation` - deletes workspace, switches to default if was active
- `updateWorkspaceMutation` - updates workspace name/icon

### SidebarWorkspaceSwitcher (`frontend/src/components/Common/WorkspaceSwitcher.tsx`)

Dropdown in sidebar footer. Shows active workspace icon in button, lists all workspaces with icons in dropdown, highlights active with background + check icon.

### CreateWorkspaceModal (`frontend/src/components/Common/CreateWorkspaceModal.tsx`)

Dialog with name input and 5-column icon selector grid. Resets form on close. Submits via `createWorkspaceMutation`.

### Icon Config (`frontend/src/config/workspaceIcons.ts`)

- `WORKSPACE_ICONS` - array of 20 `WorkspaceIconOption` objects (`name`, `icon`, `label`, `theme`)
- `getWorkspaceIcon()` - maps icon name string to lucide-react component, defaults to `FolderKanban`

## Key Implementation Patterns

### localStorage Value Normalization

`getActiveWorkspaceId()` normalizes inconsistent values:
- Empty string `""` → `null` (default workspace)
- String `"null"` → actual `null`
- Prevents false comparison bugs between `""`, `"null"`, and `null`

### Workspace Change Detection

Uses shared `previousWorkspaceId` ref in Context (not per-hook refs) to detect actual workspace changes:
- Compares `previousWorkspaceId.current` with `activeWorkspaceId`
- Only redirects if values differ (actual workspace switch)
- Detail pages redirect to index; list pages stay in place
- Avoids false redirects during normal navigation

### Component Remount via Key Prop

List page components use `key={activeWorkspaceId ?? 'default'}`:
- Workspace change triggers new `key` → React unmounts/remounts component tree
- Fresh mount creates fresh queries with new workspace ID
- Avoids complex query cache manipulation

### Null vs Undefined Semantics

- `null` workspace ID → "Default" workspace (filter for `IS NULL`)
- `undefined` → no filter (returns all entities)
- `""` (empty string) → normalized to `null` by `getActiveWorkspaceId()`
- Frontend sends `null` for default, UUID for specific workspace

## Security

- All workspace endpoints require JWT authentication
- Ownership check: `workspace.user_id == current_user.id` on GET/PUT/DELETE
- Foreign key CASCADE ensures workspace deletion cleans up references
- Workspace filter is a query parameter (stateless API), no server-side session tracking
