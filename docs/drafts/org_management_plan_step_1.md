t# Step 1: Organizational Foundation

## Goal
Establish basic organizational entities, context switching UI, and org-level ownership WITHOUT implementing RBAC or sharing. All existing functionality remains unchanged; org context acts as "private mode with labels."

---

## Database Models

### New Model Files (backend/app/models/)

Create separate model files following project pattern (one file per entity):

**backend/app/models/organization.py**
- `Organization` (table=True): id, name (unique), created_at, is_active
- `OrganizationPublic`: API response model
- `OrganizationCreate`: name only
- `OrganizationUpdate`: name only (optional)

**backend/app/models/role.py**
- `Role` (table=True): id, organization_id (FK), name, is_locked, created_at
- Unique constraint: (organization_id, name)
- `RolePublic`: API response model
- `RoleCreate`: name only
- `RoleUpdate`: name only (optional)

**backend/app/models/organization_member.py**
- `OrganizationMember` (table=True): id, organization_id (FK), user_id (FK), role_id (FK), joined_at
- Unique constraint: (organization_id, user_id)
- `OrganizationMemberPublic`: API response with user details + role name
- `OrganizationMemberCreate`: user_id, role_id
- `OrganizationMemberUpdate`: role_id only (optional)

**backend/app/models/team.py**
- `Team` (table=True): id, organization_id (FK), parent_team_id (FK, nullable), name, created_at
- Unique constraint: (organization_id, name)
- `TeamPublic`: API response with parent team name if exists
- `TeamCreate`: name, parent_team_id (optional)
- `TeamUpdate`: name (optional), parent_team_id (optional)

**backend/app/models/team_member.py**
- `TeamMember` (table=True): id, team_id (FK), user_id (FK), added_at
- Unique constraint: (team_id, user_id)
- `TeamMemberPublic`: API response with user details
- `TeamMemberCreate`: user_id only

### Modified Model Files

**backend/app/models/users/user.py** (no changes for Step 1, already has id/email/etc)

**backend/app/models/agents/agent.py** (add org ownership)
- Add `organization_id: UUID | None` (FK → Organization, nullable)
- Nullable allows private agents (null = private mode)
- Update `AgentPublic` to include organization_id
- Update `AgentCreate` to accept organization_id (set from active context)

**backend/app/models/workspace.py** (add org ownership)
- Add `organization_id: UUID | None` (FK → Organization, nullable)
- Add `is_org_workspace: bool` (default: False)
- Nullable org_id allows private workspaces
- Update `WorkspacePublic` and `WorkspaceCreate` accordingly

---

## Service Layer (backend/app/services/)

Create service files for business logic (routes call services, not direct DB operations):

**backend/app/services/organization_service.py**
- `OrganizationService.create_organization(session, user_id, data)` - Creates org + 5 default roles + member with owner role
- `OrganizationService.get_user_organizations(session, user_id)` - List user's orgs
- `OrganizationService.update_organization(session, org_id, data)` - Update org name
- `OrganizationService.delete_organization(session, org_id)` - Hard delete org + cascade members/teams/roles

**backend/app/services/role_service.py**
- `RoleService.create_default_roles(session, org_id)` - Creates 5 built-in roles (Owner locked)
- `RoleService.create_custom_role(session, org_id, data)` - Creates unlocked role
- `RoleService.get_org_roles(session, org_id)` - List roles
- `RoleService.update_role(session, role_id, data)` - Fails if locked
- `RoleService.delete_role(session, role_id)` - Fails if locked or in use

**backend/app/services/organization_member_service.py**
- `OrganizationMemberService.add_member(session, org_id, user_id, role_id)` - Add member
- `OrganizationMemberService.update_member_role(session, member_id, role_id)` - Change role
- `OrganizationMemberService.remove_member(session, member_id)` - Fails if last owner
- `OrganizationMemberService.get_org_members(session, org_id)` - List with user + role info

**backend/app/services/team_service.py**
- `TeamService.create_team(session, org_id, data)` - Validate parent team if nesting
- `TeamService.update_team(session, team_id, data)` - Update name/parent
- `TeamService.delete_team(session, team_id)` - Hard delete + cascade members
- `TeamService.add_team_member(session, team_id, user_id)` - Add member
- `TeamService.remove_team_member(session, team_id, user_id)` - Remove member

---

## Backend API Routes

### New Endpoints (backend/app/api/routes/)

Create **organizations.py**:
- `POST /api/v1/organizations` - Create org (user becomes first owner)
- `GET /api/v1/organizations` - List user's orgs
- `GET /api/v1/organizations/{org_id}` - Get org details
- `PATCH /api/v1/organizations/{org_id}` - Update org (name only for Step 1)
- `DELETE /api/v1/organizations/{org_id}` - Delete org (owner only, hard delete)

Create **org_members.py**:
- `GET /api/v1/organizations/{org_id}/members` - List org members
- `POST /api/v1/organizations/{org_id}/members` - Add member (owner only)
- `PATCH /api/v1/organizations/{org_id}/members/{user_id}` - Update role
- `DELETE /api/v1/organizations/{org_id}/members/{user_id}` - Remove member

Create **teams.py**:
- `GET /api/v1/organizations/{org_id}/teams` - List org teams
- `POST /api/v1/organizations/{org_id}/teams` - Create team
- `PATCH /api/v1/organizations/{org_id}/teams/{team_id}` - Update team
- `DELETE /api/v1/organizations/{org_id}/teams/{team_id}` - Delete team
- `POST /api/v1/organizations/{org_id}/teams/{team_id}/members` - Add team member
- `DELETE /api/v1/organizations/{org_id}/teams/{team_id}/members/{user_id}` - Remove member

Create **roles.py**:
- `GET /api/v1/organizations/{org_id}/roles` - List org roles
- `POST /api/v1/organizations/{org_id}/roles` - Create custom role (owner only)
- `PATCH /api/v1/organizations/{org_id}/roles/{role_id}` - Update role (fails if locked)
- `DELETE /api/v1/organizations/{org_id}/roles/{role_id}` - Delete role (fails if locked or in use)

### Modified Endpoints

**backend/app/api/deps.py** - Add dependency injection:
- `ActiveOrgDep` - Extract active org from request header/context (`X-Active-Org`)
- Validates user is member of org (query OrganizationMember)
- Returns org_id or None (private mode)

**Existing agent/workspace routes** - Add org_id filtering:
- Inject `ActiveOrgDep` into route handlers
- Filter list queries by `organization_id = null` (private) or `organization_id = active_org`
- When creating agent/workspace, set `organization_id` based on active context
- Routes call respective service methods (e.g., `AgentService`, `WorkspaceService`)

---

## Frontend State Management

### Context Management (frontend/src/)

**Create hooks/useOrgContext.ts**:
- Store active org in localStorage: `active_org_id`
- Provide: `{ activeOrg, setActiveOrg, isPrivateMode }`
- isPrivateMode = activeOrg === null
- Expose switch function: `switchContext(org_id | null)`

**Update hooks/useAuth.ts**:
- Include user's org memberships in auth state
- Fetch orgs on login: `GET /api/v1/organizations`

### Query Keys (TanStack Query)
- Scope all queries by org context: `["agents", activeOrgId]`, `["workspaces", activeOrgId]`
- Invalidate on context switch

---

## UI Components

### Context Switcher (frontend/src/components/)

**Create OrgContext/ContextSwitcher.tsx**:
- Dropdown in header/sidebar showing current context
- Options: "Private Mode" + list of user's orgs
- On change: call `switchContext()` → invalidate queries → UI refreshes
- Visual indicator: badge showing "Private" or org name
- Show that in the User's menu as a block wrapped with separators (in this user menu frontend/src/components/Sidebar/User.tsx) 

### Organization Management

Happens in the user's page profile frontend/src/routes/_layout/settings.tsx
There should exist Tab, 'Organisations', where user can navigate to the proper org management UI.

**Create OrgSettings/OrgList.tsx**:
- List user's organizations
- Actions: create new org, select org for settings

**Create OrgSettings/OrgDetails.tsx**:
- View/edit org name
- Delete org (owner only, confirmation modal)

**Create OrgSettings/MembersList.tsx**:
- Table: user email, role, joined date
- Actions: add member (email input), change role dropdown, remove
- Add member: check if user exists via email, create invitation if not found

**Create OrgSettings/TeamsList.tsx**:
- Tree view for teams (max 2-level nesting)
- Actions: create team, create subteam, edit, delete
- Click team → TeamDetails component

**Create OrgSettings/TeamDetails.tsx**:
- Team name, parent team (if any)
- Member list with add/remove actions

**Create OrgSettings/RolesList.tsx**:
- Table: role name, is_locked indicator, member count
- Actions: create custom role, edit role (disabled if locked), delete role (disabled if locked or in use)
- Owner role displays with lock icon, tooltip explaining it cannot be modified
- For Step 1: roles displayed but no permission editing UI (foundation only)

### Modified Components

**Sidebar navigation**:
- Filter agents/workspaces by active org context

**Agent/Workspace creation forms**:
- Automatically tag created entities with `organization_id = activeOrg`
- No UI change needed, happens in backend via context header

---

## Context Switching Mechanism

### Request Headers
- Frontend sends: `X-Active-Org: {org_id}` or omit header for private mode
- Backend dependency `ActiveOrgDep` extracts and validates:
  - User is member of org (check OrganizationMember table)
  - Returns org_id or null

### localStorage Persistence
- `active_org_id` key stores user's current context
- Restored on page reload
- Cleared on logout

### Browser Window Independence
- Each tab has own React state for activeOrg
- Changing context in one tab doesn't affect others (localStorage read on mount only)

---

## Ownership Model (Step 1 - No RBAC)

### Default Behavior
- **Private mode** (org_id = null):
  - User sees only their private agents/workspaces
  - Existing functionality unchanged

- **Org mode** (org_id = active_org):
  - User sees agents/workspaces with `organization_id = active_org`
  - Created entities belong to org (cannot be taken when user leaves)
  - NO permission checks yet - all org members see all org resources

### Simple Access Control (placeholder for RBAC)
- Check: is user member of org? (OrganizationMember exists)
- Check: is user owner? (role_id = owner_role_id) for delete/modify org
- All other operations allowed for all org members
- Full RBAC implemented in Step 2

---

## Role Management (Foundation Only)

### Built-in Roles Auto-Creation

When organization is created, automatically create 5 default roles:

1. **Owner** (is_locked: true)
   - Cannot be deleted or modified
   - At least one org member must have this role
   - Used for org deletion, ownership transfer

2. **Admin** (is_locked: false)
   - Can be modified/deleted by owner
   - Foundation for org-wide admin permissions

3. **Builder** (is_locked: false)
   - Foundation for agent creation permissions

4. **Experienced** (is_locked: false)
   - Foundation for intermediate user permissions

5. **Regular** (is_locked: false)
   - Foundation for basic user permissions

### Role Assignment Logic

- Organization creator automatically assigned "Owner" role
- When adding member: owner selects role from dropdown
- Changing member role: update OrganizationMember.role_id
- Deleting role: fails if any members assigned to it
- Locked roles: UI prevents edit/delete, backend returns 403

### Role Enforcement (Step 1 Scope)

**What IS enforced:**
- Owner role required for org deletion
- Owner role required for org name modification
- Cannot delete/modify locked "Owner" role
- At least one owner must exist (prevents removing last owner)

**What is NOT enforced (deferred to Step 2):**
- Permission-based access to agents/workspaces
- Role-based CRUD restrictions
- Team-level permission inheritance
- Custom role permissions (stored but not evaluated)

Roles exist structurally but don't control resource access yet - foundation for RBAC system.

---

## Migration Strategy

### Alembic Migration
1. Create tables: Organization, Role, OrganizationMember, Team, TeamMember
2. Add columns: `organization_id` to agents/workspaces tables (nullable)
3. No data migration needed (all existing data remains private)

### Organization Creation Flow
1. User calls `POST /api/v1/organizations` with org name
2. Route calls `OrganizationService.create_organization()`
3. Service creates Organization record
4. Service calls `RoleService.create_default_roles()` → creates 5 built-in roles (Owner, Admin, Builder, Experienced, Regular)
5. Service creates OrganizationMember linking user_id + owner_role_id
6. Service commits transaction and returns organization
7. Route returns org details to frontend

### Rollout
- Deploy backend + frontend together (feature flag optional)
- Users continue in private mode by default
- Users opt-in to create organizations

---

## Files to Reference

### Backend
- `backend/app/models/` - New model files: organization.py, role.py, organization_member.py, team.py, team_member.py
- `backend/app/models/agents/agent.py` - Modify to add organization_id
- `backend/app/models/workspace.py` - Modify to add organization_id, is_org_workspace
- `backend/app/services/` - New service files: organization_service.py, role_service.py, organization_member_service.py, team_service.py
- `backend/app/api/deps.py` - Add `ActiveOrgDep` dependency injection
- `backend/app/api/routes/organizations.py` - New file (calls OrganizationService)
- `backend/app/api/routes/org_members.py` - New file (calls OrganizationMemberService)
- `backend/app/api/routes/teams.py` - New file (calls TeamService)
- `backend/app/api/routes/roles.py` - New file (calls RoleService)
- `backend/app/api/main.py` - Register new routers
- `backend/app/alembic/versions/` - New migration file

### Frontend
- `frontend/src/hooks/useOrgContext.ts` - New file
- `frontend/src/hooks/useAuth.ts` - Modify to include orgs
- `frontend/src/components/OrgContext/` - New folder for context switcher
- `frontend/src/components/OrgSettings/` - New folder for org management UI (includes RolesList.tsx)
- `frontend/src/routes/_layout/organizations.tsx` - New route
- `frontend/src/routes/_layout/org-settings.tsx` - New route (tabs: Details, Members, Teams, Roles)
- Regenerate OpenAPI client: `bash scripts/generate-client.sh`

---

## Key Design Principles

1. **Backward Compatible**: Existing private mode functionality untouched
2. **Org as Container**: Organizations are labels/containers, not enforcers (yet)
3. **Context-Driven**: Active org determines visibility, stored in localStorage
4. **Service Layer Pattern**: Routes call services (backend/app/services/), services handle business logic and DB operations
5. **Owner-Only Mutations**: Only owners can modify org itself, all members see all resources
6. **Hard Ownership**: Org-created entities stay with org, no transfer on user exit
7. **Skeleton First**: Build structure for RBAC (Role entity + built-in roles) but don't enforce permissions
8. **Locked Owner Role**: Owner role created automatically, cannot be deleted/modified, ensures org always has owner
9. **No Sharing Yet**: Agents/resources visible to all org members by default

---

## What's NOT in Step 1

- ❌ RBAC permission enforcement (roles stored but only Owner role enforced for org operations)
- ❌ Permission definitions (no Permission table, no role-permission mapping)
- ❌ Sharing/unsharing agents with specific teams
- ❌ Replicant agents
- ❌ Credential types (private/org_private/org_shared)
- ❌ Org workspaces vs private workspaces distinction (workspaces created but no enforcement)
- ❌ Team-based permission inheritance
- ❌ Custom role permission configuration UI
- ❌ Invitation system (direct member addition only, no email invites)
- ❌ Audit logging

These features added in subsequent steps building on this foundation.

**Step 1 delivers:** Organizational structure with roles as data model foundation, enabling Step 2 to add permission system on top.