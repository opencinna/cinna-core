# User Workspaces

## Purpose

Workspaces let users organize agents, credentials, sessions, and tasks into separate contexts (e.g., "Financial Data", "Email Management") to reduce UI clutter. Users create multiple workspaces and switch between them, with each workspace showing only its associated entities.

## Core Concepts

- **Workspace** - Named isolation boundary for a user's entities, with an optional icon for visual identification
- **Default Workspace** - Virtual workspace for entities with no explicit workspace assignment (`user_workspace_id = null`)
- **Active Workspace** - The currently selected workspace in the browser session, stored in localStorage (not server-side)
- **Workspace Inheritance** - Sessions and activities automatically inherit their workspace from their parent agent, rather than being assigned directly

## User Stories / Flows

### Creating a Workspace

1. User clicks workspace switcher in sidebar footer
2. User clicks "New Workspace"
3. Modal appears with name field and icon selector (5x4 grid of 20 themed icons)
4. User enters name, optionally selects icon (defaults to folder-kanban)
5. Workspace created and automatically becomes the active workspace

### Switching Workspaces

1. User clicks workspace switcher in sidebar
2. Dropdown shows "Default" + all user-created workspaces with their icons
3. Active workspace highlighted with background color and check icon
4. User selects a workspace
5. All list pages refresh to show only entities belonging to that workspace
6. If user was on a detail page, they are redirected to the index page

### Entity Assignment

1. When creating an agent or credential, the active workspace ID is automatically included in the create payload
2. If "Default" workspace is active, `user_workspace_id` is set to `null`
3. Sessions inherit workspace from their parent agent at creation time
4. Activities inherit workspace from their parent session/agent

### Direct URL Navigation

1. User accesses an entity detail page via URL
2. Entity loads from API regardless of active workspace
3. UI auto-activates the entity's workspace
4. Browser's stored workspace ID updates accordingly

## Business Rules

- **One active workspace per browser tab** - different tabs can have different active workspaces simultaneously
- **Stateless API** - server never tracks "active workspace" per user; frontend passes filter on each request
- **Null means Default** - entities with `user_workspace_id = null` belong to the Default workspace
- **Cascade delete** - deleting a workspace cascades to its `user_workspace_id` foreign keys
- **Workspace-aware entities**: agents, credentials, sessions, activities, input tasks, knowledge sources
- **Explicitly assigned entities**: agents, credentials (user picks workspace)
- **Inherited entities**: sessions (from agent), activities (from session/agent)
- **Deletion behavior** - if the deleted workspace was active, UI switches to Default

## Architecture Overview

```
User → Sidebar Switcher → localStorage (active workspace ID)
                              ↓
         List Pages (pass workspace ID as query param)
                              ↓
Frontend → Backend API → Service Layer → PostgreSQL
              (stateless filter)         (user_workspace_id column)
```

## Integration Points

- **Agents** - `user_workspace_id` column on agents table; list endpoint accepts workspace filter
- **Credentials** - same pattern as agents; workspace assigned at creation
- **Sessions** - inherit workspace from parent agent via `SessionService.create_session()`
- **Activities** - inherit workspace from session/agent via `ActivityService.create_activity()`
- **Input Tasks** - `user_workspace_id` column; filtered in list endpoint
- **Knowledge Sources** - `user_workspace_id` column with unique constraint per git repo + workspace

## Workspace Icons

Users select from 20 predefined themed icons during workspace creation:

| Theme | Icons |
|-------|-------|
| Analytics | Bar Chart, Trending Up, Pie Chart, Line Chart |
| Financial | Dollar Sign, Credit Card |
| Data | Database, Table, Spreadsheet |
| Communication | Mail |
| People | Users |
| E-commerce | Shopping Cart |
| Business | Briefcase, Building |
| Startup | Rocket |
| General | Layers, Folder Kanban |
| Productivity | Zap |
| Goals | Target |
| Scheduling | Calendar |

Icons are stored as string identifiers in the database and rendered via lucide-react components on the frontend.
