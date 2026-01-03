# User Workspaces Management

## Overview

User workspaces provide a way to organize and separate different work contexts (e.g., financial data processing, email management) to reduce UI clutter. Users can create multiple workspaces and switch between them, with each workspace showing only its associated entities.

## Core Concept

- **Entity Name**: `user_workspace` (table: `user_workspace`)
- **Default Workspace**: When `user_workspace_id` is `null` on entities, they belong to the "Default" workspace
- **Single Active Workspace**: User has one active workspace at a time in their browser session
- **Browser Session State**: Active workspace is stored in browser (localStorage), not as user setting
- **Multi-Window Support**: Different browser windows can have different active workspaces simultaneously

## Data Model

### New Table: `user_workspace`

```
user_workspace
- id (UUID, PK)
- user_id (UUID, FK to users)
- name (str)
- created_at (datetime)
- updated_at (datetime)
```

### Modified Tables

Add `user_workspace_id` (UUID, nullable, FK to user_workspace) to:
- `agents`
- `agent_sessions`
- `credentials`
- `activities` (related to sessions)

**Null value** = entity belongs to "Default" workspace

## UI Components

### Sidebar Workspace Switcher

**Location**: Above "Appearance" menu item in `frontend/src/components/Sidebar/AppSidebar.tsx`

**Behavior**: Similar to appearance switcher

**Menu Structure**:
1. "Default" (always present)
2. List of user-created workspaces
3. Separator
4. "New Workspace" option

**Interaction**:
- Hover/expanded mode shows current workspace name
- Clicking opens dropdown menu
- Selecting workspace switches active context

### New Workspace Modal

**Trigger**: Click "New Workspace" in switcher menu

**Form Fields**:
- Workspace name (required, text input)

**Similar to**: Modal pattern shown in `docs/development/frontend_development_llm.md:88`

## Frontend Behavior

### Active Workspace State

- Stored in browser: `last_user_workspace_id`
- Loaded on app initialization
- Updated when user switches workspaces

### Entity Creation

When user creates new agent/session/credential:
- If workspace is active: set `user_workspace_id` to active workspace ID
- If "Default" active: set `user_workspace_id` to `null`

### Entity Filtering

For queries listing:
- Agents
- Sessions
- Credentials
- Activities

**Filter logic**:
- If "Default" workspace active: show entities where `user_workspace_id IS NULL`
- If custom workspace active: show entities where `user_workspace_id = <active_workspace_id>`

### Direct URL Navigation

When accessing entity via direct URL:
1. Load entity from API
2. Check entity's `user_workspace_id`
3. Auto-activate corresponding workspace in UI
4. Update browser's `last_user_workspace_id`

## API Considerations

### API Changes

**Minimal changes to existing APIs**:
- Accept optional `user_workspace_id` parameter in list/create endpoints
- Return `user_workspace_id` field in responses

**New endpoints needed**:
- `GET /api/v1/user-workspaces` - List user's workspaces
- `POST /api/v1/user-workspaces` - Create new workspace
- `GET /api/v1/user-workspaces/{id}` - Get workspace details
- `PUT /api/v1/user-workspaces/{id}` - Update workspace
- `DELETE /api/v1/user-workspaces/{id}` - Delete workspace

### API Behavior

- APIs remain stateless (don't track active workspace server-side)
- Frontend includes workspace filter in query parameters
- Multiple browser windows with different workspaces won't conflict

## Future Extensibility

Design allows for:
- Cross-workspace entity views (without API rewrite)
- Workspace sharing/collaboration
- Workspace-level settings
- Bulk operations across workspaces

## Implementation Details

### Backend Architecture

**Models** (`backend/app/models/user_workspace.py`):
- `UserWorkspace` - Main model with `id`, `user_id`, `name`, timestamps
- `UserWorkspaceCreate/Update/Public` - Request/response schemas
- Foreign key: `user_id` → `user.id` with CASCADE delete

**Modified Models**:
- `Agent`, `Credential` - Direct workspace assignment (user sets explicitly)
- `Session` - Inherits workspace from agent automatically
- `Activity` - Inherits workspace from session or agent automatically

**Service Layer** (`backend/app/services/user_workspace_service.py`):
- `UserWorkspaceService` - Encapsulates all workspace business logic
- Methods: `create_workspace`, `get_workspace`, `get_user_workspaces`, `update_workspace`, `delete_workspace`

**API Routes** (`backend/app/api/routes/user_workspaces.py`):
- `GET /api/v1/user-workspaces` - List with pagination
- `POST /api/v1/user-workspaces` - Create (requires name)
- `GET /api/v1/user-workspaces/{id}` - Get single
- `PUT /api/v1/user-workspaces/{id}` - Update
- `DELETE /api/v1/user-workspaces/{id}` - Delete (switches to default if active)

**Workspace Inheritance Logic**:
- `SessionService.create_session()` - Reads `agent.user_workspace_id` and assigns to new session
- `ActivityService.create_activity()` - Checks session first, then agent, to determine workspace
- No manual workspace assignment needed for inherited entities

**List Endpoint Filtering**:
- All list endpoints accept optional `user_workspace_id` query parameter
- `None` (param not provided) → returns ALL entities (no filter)
- `null` value → filters for `user_workspace_id IS NULL` (default workspace)
- UUID value → filters for exact workspace match
- Implemented in: `agents.py`, `credentials.py`, `sessions.py`, `activities.py`

**Database Migration**:
- Migration file: `backend/app/alembic/versions/3a154fd039f5_add_user_workspaces_support.py`
- Adds `user_workspace` table
- Adds nullable `user_workspace_id` column to 4 tables
- Foreign keys with CASCADE delete

### Frontend Architecture

**State Management** (`frontend/src/hooks/useWorkspace.ts`):
- `useWorkspace()` hook - Central workspace state management
- localStorage key: `last_user_workspace_id`
- Exports: `activeWorkspace`, `activeWorkspaceId`, `switchWorkspace()`, `createWorkspaceMutation`, etc.
- Switching workspace triggers query invalidation for all entity lists

**UI Components**:
- `SidebarWorkspaceSwitcher` (`frontend/src/components/Common/WorkspaceSwitcher.tsx`) - Dropdown in sidebar footer
- `CreateWorkspaceModal` (`frontend/src/components/Common/CreateWorkspaceModal.tsx`) - Simple form with name field
- Both integrated in `AppSidebar.tsx` above Appearance menu

**Entity Creation**:
- `AddAgent.tsx` - Reads `activeWorkspaceId` from hook, includes in `AgentCreate` payload
- `AddCredential.tsx` - Same pattern as agents
- Workspace ID automatically set to `null` for default workspace

**List Page Filtering**:
- Query keys include `activeWorkspaceId`: `["agents", activeWorkspaceId]`
- API calls pass `userWorkspaceId: activeWorkspaceId || undefined`
- Switching workspace changes query key → automatic refetch
- Implemented in: `agents.tsx`, `credentials.tsx`, `sessions.tsx`, `activities.tsx`

### Key Design Decisions

**Workspace Inheritance Hierarchy**:
1. User explicitly sets workspace for **Agents** and **Credentials** only
2. Sessions inherit from parent agent (reduces UI complexity)
3. Activities inherit from session/agent (maintains context)
4. No UI selectors needed for sessions/activities

**Stateless API Design**:
- Server never tracks "active workspace" per user
- Frontend passes filter explicitly on each request
- Enables multi-window/tab support with different active workspaces
- REST API principle: workspace filter is just another query parameter

**Query Invalidation Strategy**:
- `switchWorkspace()` invalidates: `["agents"]`, `["credentials"]`, `["sessions"]`, `["activities"]`
- Query keys include workspace ID for proper cache separation
- Creating workspace auto-switches to it (better UX)

**Null vs Undefined Semantics**:
- `null` workspace ID → "Default" workspace (explicit filter)
- `undefined` workspace ID → no filter (returns all entities)
- Frontend sends `undefined` when not filtering, `null` for default

### Extension Points

**Future Features Ready**:
- **Workspace Settings**: Add `settings` JSON column to `user_workspace` table
- **Workspace Sharing**: Add `user_workspace_members` link table with role field
- **Cross-Workspace Views**: UI already has "all workspaces" capability (pass `undefined` filter)
- **Bulk Operations**: Service methods accept lists, easy to extend
- **Workspace Templates**: Add `template_id` field to copy structure
- **Workspace Colors/Icons**: Add `ui_color`, `icon` fields to model

**Migration Path for Moving Entities**:
- Add `PATCH /api/v1/agents/{id}/workspace` endpoint
- Update `agent.user_workspace_id` directly
- Sessions/activities auto-inherit new workspace (cascade logic)
- Frontend: Add "Move to Workspace" option in entity menus

**Search Across Workspaces**:
- Add `search_all_workspaces: bool` query param to list endpoints
- When true, ignore workspace filter
- Useful for global search feature
