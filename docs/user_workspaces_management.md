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
- icon (str, nullable) - Icon identifier for visual representation
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

**Visual Design**:
- Displays active workspace's selected icon (or default FolderKanban icon)
- Shows workspace name in expanded mode

**Menu Structure**:
1. "Default" (always present, uses FolderKanban icon)
2. List of user-created workspaces (each with their selected icon)
3. Separator
4. "New Workspace" option

**Menu Features**:
- Each workspace displays its custom icon
- Active workspace is highlighted with background color
- Check icon appears next to active workspace

**Interaction**:
- Hover/expanded mode shows current workspace name and icon
- Clicking opens dropdown menu
- Selecting workspace switches active context

### New Workspace Modal

**Trigger**: Click "New Workspace" in switcher menu

**Form Fields**:
- Workspace name (required, text input)
- Icon selector (optional, visual grid selector)

**Icon Selector**:
- 5-column grid layout with 20 themed icon options
- Themes include: analytics, financial, data, communication, people, e-commerce, business, startup, productivity, goals, scheduling
- Icons from lucide-react library
- Visual selection with hover states and active highlighting
- Default: "folder-kanban" icon

**Available Icons**:
- **Analytics**: Bar Chart, Trending Up, Pie Chart, Line Chart
- **Financial**: Dollar Sign, Credit Card
- **Data**: Database, Table, Spreadsheet
- **Communication**: Mail
- **People**: Users
- **E-commerce**: Shopping Cart
- **Business**: Briefcase, Building
- **Startup**: Rocket
- **General**: Layers, Folder Kanban
- **Productivity**: Zap
- **Goals**: Target
- **Scheduling**: Calendar

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

## Workspace Icons

User workspaces support visual customization through selectable icons to help users quickly identify different workspaces.

### Icon Features

**Selection**:
- Users choose from 20 predefined themed icons during workspace creation
- Icons organized by themes: analytics, financial, data, communication, business, etc.
- Default icon: "folder-kanban" (general purpose)

**Display**:
- Selected icon appears in sidebar button when workspace is active
- Icons shown next to workspace names in dropdown menu
- Default workspace always uses FolderKanban icon

**Visual Highlighting**:
- Active workspace highlighted with background color in dropdown
- Check icon appears next to active workspace name
- Icon selector in modal highlights selected icon with primary color border

**Technical Implementation**:
- Icons stored as string identifiers in database (`icon` column)
- Icon definitions in `frontend/src/config/workspaceIcons.ts`
- Uses lucide-react icon library
- Helper function `getWorkspaceIcon()` maps identifiers to components

### Icon Themes and Options

The system provides 20 curated icons across different themes:

| Theme | Icons | Use Cases |
|-------|-------|-----------|
| Analytics | Bar Chart, Trending Up, Pie Chart, Line Chart | Data analysis, reporting, metrics |
| Financial | Dollar Sign, Credit Card | Finance, accounting, payments |
| Data | Database, Table, Spreadsheet | Data management, ETL, databases |
| Communication | Mail | Email processing, messaging |
| People | Users | HR, customer management, teams |
| E-commerce | Shopping Cart | Online retail, orders |
| Business | Briefcase, Building | Corporate, enterprise projects |
| Startup | Rocket | New ventures, innovation |
| Productivity | Zap | Automation, efficiency |
| Goals | Target | OKRs, planning |
| Scheduling | Calendar | Time management, events |
| General | Layers, Folder Kanban | Multi-purpose workspaces |

## Future Extensibility

Design allows for:
- Cross-workspace entity views (without API rewrite)
- Workspace sharing/collaboration
- Workspace-level settings
- Bulk operations across workspaces
- Workspace color customization (icons already implemented)
- Custom icon uploads or extended icon libraries

## Implementation Details

### Backend Architecture

**Models** (`backend/app/models/user_workspace.py`):
- `UserWorkspace` - Main model with `id`, `user_id`, `name`, `icon`, timestamps
- `UserWorkspaceCreate/Update/Public` - Request/response schemas
- `icon` field - Optional string (max 50 chars) storing icon identifier
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
- `POST /api/v1/user-workspaces` - Create (requires name, optional icon)
- `GET /api/v1/user-workspaces/{id}` - Get single
- `PUT /api/v1/user-workspaces/{id}` - Update (name and/or icon)
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
- Initial migration: `backend/app/alembic/versions/3a154fd039f5_add_user_workspaces_support.py`
  - Adds `user_workspace` table
  - Adds nullable `user_workspace_id` column to 4 tables
  - Foreign keys with CASCADE delete
- Icon field migration: `backend/app/alembic/versions/88ff71b370a1_add_icon_field_to_user_workspace.py`
  - Adds nullable `icon` column to `user_workspace` table

### Frontend Architecture

**State Management** (`frontend/src/hooks/useWorkspace.tsx`):
- `useWorkspace()` hook - Central workspace state management using React Context
- `WorkspaceProvider` - Context provider wrapping app in `__root.tsx`
- localStorage key: `last_user_workspace_id`
- Exports: `activeWorkspace`, `activeWorkspaceId`, `switchWorkspace()`, `createWorkspaceMutation`, etc.
- **Shared state pattern**: All components access same workspace state via Context (prevents state desync)
- Switching workspace automatically remounts list page components via `key` prop pattern

**Workspace Change Detection**:

The system needs to detect when users switch workspaces to redirect them from detail pages (which show entities from the old workspace). However, it must NOT redirect on normal page navigation when the workspace hasn't changed.

**Implementation Details**:

1. **localStorage Value Normalization**:
   - `getActiveWorkspaceId()` normalizes inconsistent values to ensure proper comparison
   - Empty string `""` → converted to `null` (default workspace)
   - String `"null"` → converted to actual `null`
   - This prevents false positives where `"" !== null` would trigger unwanted redirects

2. **Change Detection via Previous Value Tracking**:
   - `WorkspaceProvider` maintains `previousWorkspaceId` ref initialized to current workspace ID
   - Ref stored in Context, shared across all `useWorkspace()` hook instances
   - On workspace change effect: compares `previousWorkspaceId.current` with `activeWorkspaceId`
   - Only redirects if values differ (actual workspace change occurred)
   - Updates ref to new value after processing change

3. **Why Not `isInitialMount` Pattern**:
   - ❌ **Problem with per-hook refs**: Each component calling `useWorkspace()` would get separate `isInitialMount` ref
   - ❌ **False positives**: When navigating to agent detail page, new components mount and their effects run
   - ❌ **State lifecycle mismatch**: Hook instances mount/unmount independently from workspace state
   - ✅ **Solution**: Track actual value change, not mount state

4. **Redirect Behavior**:
   - When workspace changes (previous ≠ current):
     - On list pages (`/`, `/agents`, `/credentials`, `/sessions`, `/activities`): No redirect needed
     - On detail pages (`/agent/[id]`, `/credential/[id]`, etc.): Redirect to index
   - When workspace unchanged (previous === current):
     - No redirect, regardless of page type
     - Allows normal navigation to detail pages without interference

5. **Context Structure**:
   ```typescript
   WorkspaceContext = {
     activeWorkspaceId: string | null,           // Current workspace
     setActiveWorkspaceIdState: (id) => void,    // State setter
     previousWorkspaceId: MutableRefObject<string | null>  // Previous value for comparison
   }
   ```

**Why This Approach Works**:
- Previous value stored in ref (doesn't trigger re-renders when updated)
- Ref shared via Context (all hook instances see same value)
- Comparison happens in effect that depends on `activeWorkspaceId`
- Effect only performs redirect when actual state change detected
- Normal navigation never changes `activeWorkspaceId`, so never redirects

**Icon Configuration** (`frontend/src/config/workspaceIcons.ts`):
- `WORKSPACE_ICONS` - Array of 20 predefined icon options with themes
- `getWorkspaceIcon()` - Helper function to retrieve icon component by name
- Icons sourced from lucide-react library
- Each icon has: name (identifier), icon (component), label (display name), theme (category)

**UI Components**:
- `SidebarWorkspaceSwitcher` (`frontend/src/components/Common/WorkspaceSwitcher.tsx`)
  - Dropdown in sidebar footer
  - Displays active workspace icon in sidebar button
  - Shows workspace icons in dropdown menu
  - Highlights active workspace with background color and check icon
- `CreateWorkspaceModal` (`frontend/src/components/Common/CreateWorkspaceModal.tsx`)
  - Form with name field and icon selector
  - Icon selector displays 5-column grid of clickable icons
  - Selected icon highlighted with primary color
  - Submits both name and icon to API
- Both integrated in `AppSidebar.tsx` above Appearance menu

**Entity Creation**:
- `AddAgent.tsx` - Reads `activeWorkspaceId` from hook, includes in `AgentCreate` payload
- `AddCredential.tsx` - Same pattern as agents
- Workspace ID automatically set to `null` for default workspace

**List Page Filtering**:
- Query keys include `activeWorkspaceId`: `["agents", activeWorkspaceId]`
- Query functions read workspace ID from `queryKey` parameter (not closure)
- API calls pass `userWorkspaceId: workspaceId ?? ""`
- Implemented in: `agents.tsx`, `credentials.tsx`, `sessions.tsx`, `activities.tsx`, `index.tsx`

**Workspace Switching Mechanism**:
- List page components use `key={activeWorkspaceId ?? 'default'}` prop for forced remount
- When workspace changes in Context, all components re-render with new `activeWorkspaceId`
- Changed `key` prop forces React to unmount and remount component tree
- Fresh queries automatically fetch with new workspace ID from query key
- Detail pages redirect to index when workspace switches (entity likely doesn't exist in new workspace)

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

**React Context for Shared State**:
- `WorkspaceContext` provides single source of truth for `activeWorkspaceId`
- Prevents state desynchronization across components (each calling `useWorkspace()` sees same state)
- Before Context: each component had separate state instance → switching didn't propagate
- After Context: state change in one component triggers re-render in all consumers
- **Important**: Context also stores `previousWorkspaceId` ref for change detection (see Workspace Change Detection above)

**Component Remount via Key Prop**:
- React `key` prop pattern forces component remount when workspace changes
- Avoids complex React Query cache manipulation (removeQueries/invalidateQueries issues)
- Simpler mental model: workspace change = fresh component mount = fresh data fetch
- Creating workspace auto-switches to it (better UX)

**Null vs Undefined vs Empty String Semantics**:
- `null` workspace ID → "Default" workspace (explicit filter for entities without workspace)
- `undefined` workspace ID → no filter (returns all entities across workspaces)
- `""` (empty string) → **normalized to `null`** by `getActiveWorkspaceId()` (prevents comparison bugs)
- `"null"` (string) → **normalized to `null`** by `getActiveWorkspaceId()` (handles edge cases)
- Frontend sends `undefined` when not filtering, `null` for default
- **Critical**: Always use actual `null` for default workspace, never empty string

### Troubleshooting

**Issue: Detail pages redirect to index unexpectedly**

Symptoms:
- Navigating to `/agent/[id]` or other detail pages immediately redirects to `/`
- Console shows "Workspace changed to: [value]" repeatedly
- Happens even though workspace hasn't actually changed

Common Causes & Solutions:

1. **Empty string vs null comparison issue**:
   - Problem: localStorage contains `""` instead of being absent (null)
   - Check: `localStorage.getItem('last_user_workspace_id')` returns `""`
   - Fix: `getActiveWorkspaceId()` normalizes empty strings to `null`
   - Verify: After normalization, comparison works correctly

2. **isInitialMount pattern with per-hook refs**:
   - Problem: Each component calling `useWorkspace()` gets separate ref
   - Symptom: `isInitialMount.current` shows `false` even on first page load
   - Fix: Don't use per-hook refs; use shared ref in Context or value-based change detection
   - Current solution: Track `previousWorkspaceId` in shared Context ref

3. **Workspace change detection on every render**:
   - Problem: Effect runs on every component mount, not just workspace changes
   - Check: Effect should compare previous and current workspace IDs
   - Fix: Only redirect if `previousWorkspaceId.current !== activeWorkspaceId`
   - Verify: Navigation to detail pages works without redirect when workspace unchanged

4. **localStorage value type inconsistencies**:
   - Problem: Sometimes stores `"null"` (string) instead of actual null
   - Check: `typeof localStorage.getItem('last_user_workspace_id')`
   - Fix: Normalize both `""` and `"null"` string to actual `null`

**Debugging Steps**:

1. Check localStorage value:
   ```javascript
   console.log('Raw value:', localStorage.getItem('last_user_workspace_id'))
   console.log('Type:', typeof localStorage.getItem('last_user_workspace_id'))
   ```

2. Verify normalization:
   ```javascript
   // In getActiveWorkspaceId()
   console.log('Before normalization:', value)
   console.log('After normalization:', normalizedValue)
   ```

3. Track workspace changes:
   ```javascript
   // In workspace change effect
   console.log('Previous:', previousWorkspaceId.current, 'Current:', activeWorkspaceId)
   console.log('Values equal?', previousWorkspaceId.current === activeWorkspaceId)
   ```

4. Monitor redirect logic:
   ```javascript
   console.log('Current path:', window.location.pathname)
   console.log('Is list page?', listPages.includes(window.location.pathname))
   ```

**Best Practices**:
- Always use actual `null` for default workspace, never `""` or `"null"` strings
- Share refs/state that need to be consistent across hook instances via Context
- Use value comparison for change detection, not mount state tracking
- Test navigation to detail pages after implementing workspace features

### Extension Points

**Future Features Ready**:
- **Workspace Settings**: Add `settings` JSON column to `user_workspace` table
- **Workspace Sharing**: Add `user_workspace_members` link table with role field
- **Cross-Workspace Views**: UI already has "all workspaces" capability (pass `undefined` filter)
- **Bulk Operations**: Service methods accept lists, easy to extend
- **Workspace Templates**: Add `template_id` field to copy structure
- **Workspace Colors**: Add `ui_color` field to model (icons already implemented)
- **Custom Icons**: Allow users to upload custom SVG icons or choose from extended icon libraries

**Migration Path for Moving Entities**:
- Add `PATCH /api/v1/agents/{id}/workspace` endpoint
- Update `agent.user_workspace_id` directly
- Sessions/activities auto-inherit new workspace (cascade logic)
- Frontend: Add "Move to Workspace" option in entity menus

**Search Across Workspaces**:
- Add `search_all_workspaces: bool` query param to list endpoints
- When true, ignore workspace filter
- Useful for global search feature
