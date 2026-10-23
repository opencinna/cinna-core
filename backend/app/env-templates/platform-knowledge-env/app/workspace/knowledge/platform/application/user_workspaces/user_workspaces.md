---
feature: user_workspaces
domain: [application, sharing]
one_liner: "Lets a user split their agents, credentials, sessions, and tasks into separate named contexts to reduce clutter, with workspace filtering optional per user."
docs:
  tech: user_workspaces_tech.md
---
# User Workspaces

## Purpose

Workspaces let users organize agents, credentials, sessions, and tasks into separate contexts (e.g., "Financial Data", "Email Management") to reduce UI clutter. Users create multiple workspaces and switch between them, with each workspace showing only its associated entities.

## Core Concepts

- **Workspace** - Named isolation boundary for a user's entities, with an optional icon for visual identification
- **Default Workspace** - Virtual workspace for entities with no explicit workspace assignment (`user_workspace_id = null`)
- **Active Workspace** - The workspace a browser tab is currently showing. Held per tab (sessionStorage) with a global last-manual-pick preference (localStorage) that new tabs start from; never server-side
- **Workspace Inheritance** - Sessions and activities automatically inherit their workspace from their parent agent, rather than being assigned directly
- **Workspaces Enabled Toggle** - Per-user UI preference (Settings → Interface → Workspaces card) that turns workspace filtering on or off. When off, list queries omit the workspace filter and show every entity owned by the user; the sidebar workspace switcher is hidden. Persisted on the `User` row (`User.workspaces_enabled` boolean column) so the setting follows the user across browsers and devices. The toggle is role-agnostic and works the same for `agent-user`, `agent-developer`, and `admin`

## User Stories / Flows

### Creating a Workspace

1. User clicks workspace switcher at the top of the sidebar (above navigation menu)
2. User clicks "Manage Workspaces" — navigates to Settings → Interface tab
3. In the Workspaces card, user clicks "New Workspace"
4. Modal appears with name field and icon selector (5x4 grid of 20 themed icons)
5. User enters name, optionally selects icon (defaults to folder-kanban)
6. Workspace created

### Switching Workspaces

1. User clicks workspace switcher at the top of the sidebar
2. Dropdown shows "Default" + all user-created workspaces with their icons
3. Active workspace highlighted with background color and check icon
4. User selects a workspace
5. All list pages refresh to show only entities belonging to that workspace
6. If the user was on a page that is not a list page (`/`, `/agents`, `/credentials`, `/sessions`, `/activities`, `/artifacts`, `/tasks`) and the workspace actually changed, they are redirected to `/`; list pages stay in place and remount
7. The pick is written to both the tab and the global preference (other open tabs are not updated live)

### Entity Assignment

1. When creating an agent or credential, the active workspace ID is automatically included in the create payload
2. If "Default" workspace is active, `user_workspace_id` is set to `null`
3. Sessions inherit workspace from their parent agent at creation time
4. Activities inherit workspace from their parent session/agent

### Opening an Entity from Another Workspace

1. User opens the detail page of an owned entity (agent, agent sessions list, session, artifact, credential, task) by URL or in-app link
2. The entity loads regardless of the active workspace
3. If workspaces are enabled, the user owns the entity, and the entity's workspace (Default or one of the user's workspaces) differs from the tab's, the tab switches to it and shows the toast `Switched to "<name>" workspace for this tab`
4. Only this tab changes: the global preference is untouched, so new tabs still start from the last manual pick. The page is never navigated away
5. The decision is made once per entity id per page mount; a later manual switch on the same page is not overridden
6. Never auto-switches for: shared or guest entities (owner is not the current user), foreign-bundle installs and sessions of such agents (workspace-agnostic), workspaces disabled. Environment file/database pages do not auto-activate (they do not load the agent)

## Business Rules

- **Active workspace is per browser tab** - two layers: the tab workspace (sessionStorage `tab_user_workspace_id`, value `"default"` = Default pinned) and the global preference (localStorage `last_user_workspace_id`). Boot uses the tab value, else the global one. Tabs opened from links start with the global value; reload and "Duplicate tab" keep the tab's workspace. Manual dropdown picks write both layers (creating a workspace in Settings does not switch to it); auto-activation from a detail page writes the tab only
- **Stale workspace repair** - if the stored active workspace no longer exists in the loaded workspace list (e.g. deleted in another tab or in Settings), the tab value is cleared and the global preference is used; if that is stale too, it is cleared and Default is used
- **Redirect only on manual dropdown pick** - creating or deleting a workspace in Settings does not redirect
- **Stateless API** - server never tracks "active workspace" per user; frontend passes filter on each request
- **Null means Default** - entities with `user_workspace_id = null` belong to the Default workspace
- **Cascade delete** - deleting a workspace cascades to its `user_workspace_id` foreign keys
- **Workspace-aware entities**: agents, credentials, sessions, activities, input tasks, artifacts (the `/artifacts` "Mine" tab is workspace-filtered). Knowledge sources are server-wide and not workspace-scoped — see [Knowledge Sources](../knowledge_sources/knowledge_sources.md)
- **Explicitly assigned entities**: agents, credentials (user picks workspace)
- **Inherited entities**: sessions (from agent), activities (from session/agent)
- **Deletion behavior** - if the deleted workspace was active, UI switches to Default
- **Toggle default for new users** - the `User.workspaces_enabled` column defaults to `false`, so new accounts see all of their entities without a workspace filter and the sidebar switcher is hidden until they opt in
- **Toggle gates the sidebar switcher** - the workspace switcher in the sidebar appears only when Workspaces Enabled is on. Role does not affect this — every role sees (or doesn't see) the switcher based purely on this toggle
- **Toggle gates list filtering** - when the toggle is on, list queries pass `userWorkspaceId = activeWorkspaceId ?? ""` and the backend applies the workspace filter. When off, queries omit the parameter (`undefined`) so the backend returns every owned entity
- **Workspace assignment on create when toggle is off** - new agents, credentials, and tasks created while the toggle is off are persisted with `user_workspace_id = null` (Default workspace); enabling the toggle later does not retroactively assign them
- **Cross-workspace visibility for foreign-bundle installs** - when a list query is filtered to a non-default workspace, foreign-bundle installs (agents installed from another publisher's bundle, identified by `bundle_uuid IS NOT NULL` AND `is_publisher_install = false` AND `user_workspace_id IS NULL`) always appear, regardless of the active workspace. Plain default-workspace agents (`user_workspace_id IS NULL` with no bundle linkage) are NOT shown in non-default views — they belong only to the Default workspace

## Architecture Overview

```
User → Sidebar Switcher → sessionStorage (tab) + localStorage (global preference)
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
- **Artifacts** - `user_workspace_id` column; the `/artifacts` "Mine" tab is workspace-filtered; detail page auto-activates the workspace
- **Knowledge Sources** - not workspace-scoped; sources are server-wide and admin-managed, with a separate per-source access level (private/public/shared) governing who may query them. See [Knowledge Sources](../knowledge_sources/knowledge_sources.md)

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

## Changelog

- Active workspace became per tab (sessionStorage) with a global localStorage preference; detail pages of owned entities auto-activate their workspace for the tab; redirect to `/` now only on a manual dropdown pick away from non-list pages.
