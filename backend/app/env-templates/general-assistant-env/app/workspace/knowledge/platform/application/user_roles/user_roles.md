# User Roles

## Purpose

Introduce a three-value role system (`agent-user`, `agent-developer`, `admin`) that gates building-mode features from regular end-users while preserving the existing `is_superuser` / `admin` tier. The goal is to let operators deploy Cinna for a team where most members interact with pre-built agents (install, chat, manage credentials) while a smaller group of developers creates, publishes, and maintains bundles.

## Core Concepts

- **`agent-user`** — the default role for every new signup. Can browse the catalog, install bundles, chat with agents in conversation mode, manage their own credentials, app-data, and settings. Cannot create agents, access building-mode sessions, or publish bundles
- **`agent-developer`** — promoted by an admin. Unlocks the full developer UI: agent creation, building-mode sessions, publishing bundles, schedule configuration, webhook management, and all integration setup
- **`admin`** — the existing superuser tier (`is_superuser = true`). Has all developer privileges plus admin-console access (agent environments fleet, user management, marketplace, roles table). The `role` column is kept in sync with `is_superuser` — superusers always hold `role = 'admin'`
- **`require_developer` dependency** — a FastAPI dependency used on routes that require at least `agent-developer`. Superusers always pass. Raises 403 for `agent-user` accounts
- **`USER_ROLE_CHANGED` event** — WebSocket event emitted after a role change (from either `PATCH /users/{id}/role` or `PATCH /users/{id}` when `role` differs); the affected user's frontend refetches `GET /users/me` and re-routes if demoted

## User Stories / Flows

### New User Signs Up

1. User completes registration (email/password or Google OAuth)
2. `User.role` defaults to `agent-user`
3. User sees the unified sidebar: Dashboard, Tasks, Agents, Sessions, Credentials in the main nav and Activities + Catalog + User menu in the footer. The User Settings link lives inside the user-icon dropdown at the bottom of the sidebar (shared by every role)
4. Agent creation, building-mode sessions, bundle management, and the workspace switcher are not visible (workspace switcher is gated by the `workspacesEnabled` toggle, which defaults to off for new users — see [user_workspaces.md](../user_workspaces/user_workspaces.md))
5. On first login after the role system is introduced, an `AgentUserWelcomeBanner` appears explaining the role split (dismissible, shown once)

### Admin Promotes a User to Developer

1. Admin opens **Admin → Users**, finds the target user in the list
2. Admin opens the row's actions menu and chooses **Edit User**
3. The Edit User dialog shows a **Role** dropdown alongside the existing fields; admin selects `Agent Developer` and saves
4. UI calls `PATCH /api/v1/users/{user_id}` with the full user payload including `role: "agent-developer"`
5. The route persists the change and emits `USER_ROLE_CHANGED` when the role actually differs
6. Target user's frontend receives the WS event, refetches `currentUser`, and unlocks the full developer navigation

### Admin Demotes a Developer back to User

1. Same flow with `{role: "agent-user"}`
2. After `USER_ROLE_CHANGED` arrives on the frontend, the user is re-routed away from any developer-only page
3. Existing agents remain owned by the user; building-mode sessions are blocked at the API layer; published bundles continue to be installable by others

### Developer Attempts to Publish Without Role

1. `agent-user` calls `POST /agents/{id}/publish`
2. `require_developer` dependency raises 403 with message "This action requires the agent-developer role. Ask an admin to promote your account."
3. API returns 403; frontend surfaces the error

## Business Rules

- **Default on signup** — every new `User` row starts with `role = 'agent-user'`; the only exception is a user created with `is_superuser = true`, who gets `role = 'admin'`
- **Admin invariant** — `is_superuser = true` ↔ `role = 'admin'`. The dedicated `PATCH /users/{user_id}/role` endpoint enforces this strictly (cannot demote a superuser there, cannot promote to admin there). The general `PATCH /users/{user_id}` admin form does **not** enforce the invariant — superusers operating that form are trusted to keep `is_superuser` and `role` consistent (the form exposes both fields side-by-side)
- **Cannot change own role via `/role` endpoint** — `set_role` raises ValueError if `target_user.id == changed_by.id`. The general user-edit form does not block self-edit, so a superuser can adjust their own `role` (e.g., to keep it in sync after toggling `is_superuser`)
- **Role changes are admin-only** — both `PATCH /users/{user_id}/role` and `PATCH /users/{user_id}` are gated on `get_current_active_superuser`
- **Downgrade does not delete agents** — demoting a developer to user leaves their existing agents intact; they simply cannot create new ones or start building-mode sessions
- **Superusers always pass `require_developer`** — even if a superuser's stored `role` value is stale, `is_superuser` is checked as a defense-in-depth fallback in `RoleService.is_developer`

## Developer-Only Features

The following routes and UI surfaces require `agent-developer` or `admin`:

| Capability | Gate |
|-----------|------|
| Create agent (`POST /agents/`) | `require_developer` |
| Update agent (`PATCH /agents/{id}`) | `require_developer` |
| Delete agent (`DELETE /agents/{id}`) | `require_developer` |
| Sync prompts | `require_developer` |
| Start a building-mode session | `require_developer` |
| Publish a bundle | `require_developer` |
| Edit bundle ID | `require_developer` |
| Update bundle metadata | `require_developer` |
| Delete bundle | `require_developer` |
| Manage grants | `require_developer` |
| List own bundles (`GET /bundles/`) | `require_developer` |

The following are available to all authenticated users (any role):

| Capability | Notes |
|-----------|-------|
| Browse the catalog | `GET /catalog/` |
| Install a bundle | `POST /catalog/{id}/install` |
| Start a conversation-mode session | Any owner |
| Apply update to own install | `POST /agents/{id}/apply-update` |
| Check updates | `POST /agents/{id}/check-updates` |
| Toggle update mode | `PATCH /agents/{id}/update-mode` |
| Uninstall | `POST /agents/{id}/uninstall` |
| View and manage app-data | `GET/DELETE /users/me/app-data` |
| Manage own credentials | |
| Manage own settings | |

## UI Surface per Role

### `agent-user`

- **Sidebar (main)**: Dashboard, Tasks, Agents, Sessions, Credentials — same items as developers; the menu is no longer split by role
- **Sidebar (footer)**: Activities, Catalog (workspace-agnostic, lives below Activities), User-icon dropdown with User Settings + theme switcher + Log Out
- **Workspace switcher visibility**: gated by the per-user `workspacesEnabled` toggle (Settings → Interface → Workspaces card), not by role
- **Dashboard differences**: no "+ New Agent" badge, no Conversation/Building mode toggle; if the user has zero agents the page does not auto-fall-back into the New Agent flow
- **Agent detail page**: conversation mode only; Update Available banner; credential linking; Uninstall button; no prompts editor, no schedulers UI, no integrations tab, no Bundle tab; the bundle-id chip + copy button are not shown in the page header (the bundle ID is still editable inside the Bundle tab itself)
- **Settings**: Profile, SSH Keys, App Data, Interface (Workspaces toggle, Agentic Teams, Dashboards) — no developer-specific tabs
- **Admin**: not accessible

### `agent-developer`

- Full sidebar (today's layout): all agents, tasks, teams, settings
- **Agent detail page**: full tabbed UI including Bundle tab with publish/grants management
- **Catalog**: visible (can also install bundles in addition to creating agents from scratch)
- **Settings**: all tabs

### `admin`

- All developer capabilities plus:
- **Admin console**: Agent Environments fleet, Users (with role-aware Edit User dialog), Plugin Marketplaces
- `PATCH /users/{id}/role` endpoint access
- `GET /users/?role=<filter>` query parameter support

## Architecture Overview

```
User.role (string column, "agent-user" | "agent-developer" | "admin")
    │
    ├── RoleService.require_developer(user) → raises PermissionError
    │        │
    │        └── route: dependencies=[Depends(require_developer)]
    │
    ├── RoleService.set_role(session, target_user, new_role, changed_by)
    │        │
    │        └── emits USER_ROLE_CHANGED → frontend refetches currentUser
    │
    └── Frontend: currentUser.role → role-aware navigation + UI gating
```

## Integration Points

| Feature | Relationship |
|---------|-------------|
| [Agent Bundles & Installs](../../agents/agent_bundles/agent_bundles.md) | Publish, bundle-id edit, and bundle CRUD require `agent-developer` |
| [Agent Management](../agent_management/agent_management.md) | Agent create/update/delete require `agent-developer`; the Bundle tab is only rendered for developers |
| [Auth](../auth/auth.md) | `User.role` is stored on the `User` model; exposed in `UserPublic` response and `GET /users/me/role`; default set at signup |
