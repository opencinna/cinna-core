# User Roles — Technical Reference

## File Locations

### Model
- `backend/app/models/users/user.py` — `UserRole` enum, `DEVELOPER_OR_ADMIN_ROLES`, `VALID_USER_ROLES`, `UserBase.role` field, `UserRolePublic`, `UserRoleUpdate`

### Service
- `backend/app/services/users/role_service.py` — `RoleService`

### API Routes
- `backend/app/api/routes/users.py` — `GET /users/me/role`, `PATCH /users/{user_id}/role`, `GET /users/?role=<filter>`
- `backend/app/api/deps.py` — `require_developer` FastAPI dependency

### Frontend
- `frontend/src/routes/_layout/admin/users.tsx` — Admin → Users page (single plain table; no Roles tab)
- `frontend/src/components/Admin/EditUser.tsx` — Edit User dialog with the role dropdown alongside `is_superuser` / `is_active`
- `frontend/src/components/Admin/UserActionsMenu.tsx` — row actions menu; rendered for every user including the current admin (so superusers can self-edit their role)
- `frontend/src/components/Common/AgentUserWelcomeBanner.tsx` — first-login banner for agent-user accounts

## Database Schema

### `user` (modified for role)

New column added in Phase 3 migration:

| Column | Type | Notes |
|--------|------|-------|
| `role` | varchar(32) NOT NULL DEFAULT 'agent-user' | `agent-user`, `agent-developer`, or `admin` |

Migration backfill: `UPDATE user SET role = 'admin' WHERE is_superuser = TRUE`. Non-superuser rows stay at `agent-user` (the column default).

## Role Enum

```python
class UserRole(str, Enum):
    USER = "agent-user"
    DEVELOPER = "agent-developer"
    ADMIN = "admin"

VALID_USER_ROLES = [r.value for r in UserRole]
DEVELOPER_OR_ADMIN_ROLES = {UserRole.DEVELOPER.value, UserRole.ADMIN.value}
```

## API Endpoints

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| `GET` | `/api/v1/users/me/role` | CurrentUser | Returns `{role: str}` |
| `PATCH` | `/api/v1/users/{user_id}/role` | superuser | Body: `{role: str}`; strict admin-invariant validation; cannot self-modify |
| `PATCH` | `/api/v1/users/{user_id}` | superuser | General user update; `UserUpdate` body includes optional `role`. When `role` actually changes, emits `USER_ROLE_CHANGED`. No admin-invariant enforcement; no self-edit block (used by Admin → Users → Edit User dialog) |
| `GET` | `/api/v1/users/?role=agent-developer` | superuser | Filter users by role |

The `role` field is also included in `UserPublic` (returned by `GET /users/me`) so the frontend can read it from the standard me-fetch without an extra call.

### `UserRolePublic`
```python
class UserRolePublic(SQLModel):
    role: str
```

### `UserRoleUpdate`
```python
class UserRoleUpdate(SQLModel):
    role: str  # must be in VALID_USER_ROLES
```

## `require_developer` Dependency

Defined in `backend/app/api/deps.py`:

```python
def require_developer(current_user: CurrentUser) -> User:
    from app.services.users.role_service import RoleService
    try:
        RoleService.require_developer(current_user)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    return current_user
```

Usage on routes:
```python
@router.post("/agents/", dependencies=[Depends(require_developer)])
```

Superusers always pass (defense-in-depth check on `user.is_superuser` before checking the `role` field).

## RoleService Key Methods

| Method | Notes |
|--------|-------|
| `is_developer(user) -> bool` | True if `is_superuser` or `role in DEVELOPER_OR_ADMIN_ROLES` |
| `require_developer(user) -> None` | Raises `PermissionError` if not developer/admin |
| `require_user(user) -> None` | No-op sanity check; any active user passes |
| `set_role(session, target_user, new_role, changed_by) -> User` | Async; validates transition rules; emits `USER_ROLE_CHANGED` |
| `derive_default_role(is_superuser: bool) -> str` | `'admin'` if superuser, else `'agent-user'` |

### Transition Validation in `set_role`

1. `new_role` must be in `VALID_USER_ROLES`
2. `target_user.id != changed_by.id` — cannot change own role
3. If `target_user.is_superuser`: `new_role` must be `'admin'` — cannot demote a superuser via role endpoint
4. If `not target_user.is_superuser`: `new_role` must not be `'admin'` — cannot promote to admin via role endpoint (use `is_superuser` flag instead)
5. No-op if `target_user.role == new_role` (returns unchanged user)

## WebSocket Event

`USER_ROLE_CHANGED` emitted after a successful `set_role` call (via `PATCH /users/{id}/role`) or when `PATCH /users/{id}` changes the `role` column:

```python
{
    "user_id": "<uuid>",
    "new_role": "agent-developer",
    "previous_role": "agent-user",
    "changed_by_user_id": "<uuid>"
}
```

Scoped to the target user's room so only they receive it. Frontend handler refetches `["currentUser"]` and re-routes if demoted away from a developer-only page. Emit failures are logged and not raised — the role change is already persisted and the user will see the correct role on next `readUserMe`.

## Frontend Components

### `EditUser` dialog

Located at `frontend/src/components/Admin/EditUser.tsx`. Opened from `UserActionsMenu` on every row (including the current admin's own row).

- Form fields: email, full name, password (+ confirm), role dropdown, `is_superuser` checkbox, `is_active` checkbox
- Role dropdown options: `Agent User` (`agent-user`), `Agent Developer` (`agent-developer`), `Admin` (`admin`)
- Submit calls `PATCH /users/{id}` with the full payload — backend persists role and emits `USER_ROLE_CHANGED` only when the role actually differs
- Invalidates `["users"]` on success

### `AgentUserWelcomeBanner`

Located at `frontend/src/components/Common/AgentUserWelcomeBanner.tsx`. Rendered in the main layout (`_layout.tsx`).

- Shown once to users with `role = 'agent-user'` on first login after the role system is introduced
- Dismissible; dismissed state persisted in localStorage
- Explains the agent-user vs agent-developer split and how to request promotion

## Role-Based Navigation

The main sidebar nav (`frontend/src/components/Sidebar/AppSidebar.tsx` + `Main.tsx`) is now unified across all roles — every user sees Dashboard, Tasks, Agents, Sessions, Credentials in the main nav and Activities + Catalog in the footer. Role-specific gating happens at:

- **`AppSidebar.tsx`** — `isDeveloper` still gates the dashboard switcher, the agentic-teams switcher, and (combined with `is_superuser`) the admin menu; the workspace switcher is gated by `workspacesEnabled` from `useWorkspace()` instead of role
- **Dashboard** (`frontend/src/routes/_layout/index.tsx`) — `isAgentUser` hides the "+ New Agent" badge, hides the Conversation/Building mode toggle, and skips the auto-fallback to the New Agent flow when the user has no agents
- **Agent detail** (`frontend/src/routes/_layout/agent/$agentId.tsx`) — `isDeveloper` hides the page-header `EllipsisVertical` menu (Edit / Delete) for agent-users; the bundle-id chip + copy button were removed from the header for everyone (still available in the Bundle tab body). `isAgentUser` tab filter includes `"configuration"`, `"credentials"`, `"environments"`, and `"integrations"` — Integrations is now reachable from the install detail page. Other tabs (prompts, schedulers, bundle, plugins, interface) are excluded
- **Agent Integrations tab** (`frontend/src/components/Agents/AgentIntegrationsTab.tsx`) — when `isAgentUser`, renders only `McpConnectorsCardSimple` (the degraded MCP Connectors view); the full tab — A2A access tokens, Agent REST API, Guest Share, the full `McpConnectorsCard` (direct + agent-to-agent connectors), Webapp Share, Local Dev, GIT Versioning, and Webhooks — is visible only to developers. There is no identity card, no routes card, and no email card on this tab
- **Agent Configuration tab** (`frontend/src/components/Agents/AgentConfigTab.tsx`) — for `agent-user`, `showOperationalSettings=false` hides the Schedules and Handovers row; the Agent Prompts card (including the Trigger Prompt button) is always shown

The Bundle tab on the agent detail page remains developer-only.

## Configuration

### `DEFAULT_USER_ROLE`

Controls the `UserRole` assigned to newly created **non-superuser** accounts at creation time. Set it in the project root `.env` file.

| Property | Value |
|----------|-------|
| Type | `Literal["agent-user", "agent-developer"]` |
| Default | `"agent-user"` |
| File | `backend/app/core/config.py`, field `DEFAULT_USER_ROLE` |

**Allowed values:**
- `agent-user` — default; new signups can install, chat, and manage settings but cannot create agents or publish bundles
- `agent-developer` — new signups start with full developer access; useful for internal/team deployments where every account is a developer

`admin` is intentionally excluded from the `Literal` type. It cannot be configured here — that would break the `role ⇔ is_superuser` invariant. A present-but-invalid value (e.g. `admin` or a typo) causes `Settings()` to raise a `ValidationError` at startup and the backend will not start. This fail-loud behaviour is consistent with the existing `ENVIRONMENT: Literal[...]` pattern in `config.py`: a mis-configured provisioning value is a security-relevant error that should never be silently masked.

**Unset/empty** falls back to `agent-user` via `env_ignore_empty=True` — behaviour is identical to before the setting was introduced.

### Single source of truth: `RoleService.derive_default_role`

`RoleService.derive_default_role(*, is_superuser: bool) -> str` in `backend/app/services/users/role_service.py` is the only place that translates the setting into an actual role value:

- `is_superuser=True` → always returns `'admin'`, ignoring `DEFAULT_USER_ROLE`
- `is_superuser=False` → returns `settings.DEFAULT_USER_ROLE`; defends against any future config widening by falling back to `'agent-user'` if the value is outside `{'agent-user', 'agent-developer'}`

Both creation paths call through this helper:

- **Password signup** — `UserService.create_user` in `backend/app/services/users/user_service.py`: calls `derive_default_role` when the caller does not supply an explicit `role`. An explicit caller-provided role (e.g. an admin creating a developer through the admin form) is honoured as-is and is not overridden.
- **Google OAuth first login** — `AuthService.create_user_from_google` in `backend/app/services/users/auth_service.py`: always calls `derive_default_role(is_superuser=False)` directly, since Google first-login users are always non-superusers.

The setting affects **creation time only**. It never touches existing users and it never overrides the superuser → `admin` mapping.

### Persistence-layer fallback

The `role` column in the `user` table has `DEFAULT 'agent-user'` at the database level (set in the Phase 3 migration). This is a persistence-layer safety net; `derive_default_role` is the authoritative runtime gate.
