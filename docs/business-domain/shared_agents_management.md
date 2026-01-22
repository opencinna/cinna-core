# Shared Agents Management - Implementation Reference

## Purpose

Enable users to share agents with other users via a clone-based model where recipients get isolated copies with independent sessions, while owners can push updates to all clones.

## Feature Overview

**Flow:**
1. Owner shares agent → pending share record created
2. Recipient sees pending share in Agents page → accepts via wizard
3. System creates clone (agent + environment + workspace files + credentials)
4. Clone owner uses agent independently with own sessions and data
5. Owner can push updates → clones receive synced scripts/prompts/knowledge
6. Clone owner can detach to become fully independent

**Key Concepts:**
- **Clone-based sharing**: Recipients get functional copies, not shared access
- **Two sharing modes**: "user" (conversation only) / "builder" (full access)
- **Credential handling**: Shareable credentials linked, private ones create placeholders
- **Update mechanism**: Push from parent, automatic or manual apply on clones

## Architecture

```
Owner's Agent
     │
     ├──► Clone A (user mode) - Recipient A
     ├──► Clone B (user mode) - Recipient B
     └──► Clone C (builder mode) - Recipient C

Frontend → Backend API → AgentShareService → AgentCloneService → Environment/Workspace
                              │                      │
                        AgentShare table        Agent (clone)
```

**Sharing Flow:**
```
Share → Pending → Accept → Clone Created → Active Use
                    │
              Credentials Setup (immediate)
                    │
              Environment Build (background)
                    │
              Workspace Copy (after build)
                    │
              Auto-Start Environment
```

## Data/State Lifecycle

### AgentShare Status

| Status | Description |
|--------|-------------|
| `pending` | Share created, awaiting recipient action |
| `accepted` | Recipient accepted, clone created |
| `declined` | Recipient declined |
| `revoked` | Owner revoked access |
| `deleted` | Recipient deleted their clone |

### Clone Update States

| Field | Values | Description |
|-------|--------|-------------|
| `update_mode` | `automatic` / `manual` | How updates are applied |
| `pending_update` | `true` / `false` | Update queued from parent |
| `last_sync_at` | datetime | Last successful sync |

### Clone Update Request

When an owner pushes updates, a `CloneUpdateRequest` record is created for each clone:

| Field | Description |
|-------|-------------|
| `clone_agent_id` | The clone receiving the update |
| `parent_agent_id` | The parent pushing the update |
| `pushed_by_user_id` | The owner who pushed |
| `copy_files_folder` | Whether to copy files/ and uploads/ folders |
| `rebuild_environment` | Whether to rebuild the Docker environment |
| `status` | `pending` / `applied` / `dismissed` |
| `created_at` | When the request was created |
| `applied_at` | When the request was applied |

**Update Actions:**
- **Standard update** (always): Syncs scripts, docs, knowledge, workspace_requirements.txt
- **Copy files folder** (optional): Also copies files/ and uploads/ directories
- **Rebuild environment** (optional): Triggers full Docker environment rebuild

### Integration Credential Handling

| Credential Type | During Clone | Result |
|-----------------|--------------|--------|
| `allow_sharing=true` | CredentialShare link created | Shared read-only access |
| `allow_sharing=false` | Placeholder created | User must configure values |

### AI Credential Handling

When sharing an agent, the owner can optionally provide AI credentials for the clone:

| Scenario | Environment Behavior |
|----------|---------------------|
| Owner provides AI credentials | Credentials shared via `AICredentialShare`, stored on environment as `conversation_ai_credential_id` / `building_ai_credential_id` |
| Recipient selects own credentials | Recipient's credential IDs stored on environment |
| Neither provided | Environment uses recipient's default AI credentials from profile |

**Important**: When AI credentials are specifically assigned to an environment (either shared or selected), the system uses **only** those credentials with **no fallback** to the user's profile credentials. This ensures:
- Cloned agents always use the shared credentials from the original owner
- No accidental substitution of the recipient's own credentials
- Clear separation between shared and personal AI credentials

**Resolution Priority** (in `environment_lifecycle.py:_update_environment_config`):
1. Explicitly provided API keys (during environment creation)
2. Named credentials stored on environment (`conversation_ai_credential_id`, `building_ai_credential_id`) - checked via `get_credential_for_use()` which handles both owned and shared credentials
3. User's profile credentials - **only if no credentials are specifically assigned**

## Database Schema

### Migrations

- **Agent clone fields**: `backend/app/alembic/versions/6fefe45793e6_add_agent_sharing_models.py`
  - Adds `is_clone`, `parent_agent_id`, `clone_mode`, `update_mode`, `pending_update`, `pending_update_at`, `last_sync_at` to agents table
  - Creates `agent_share` table
  - Creates `credential_share` table (for standalone credential sharing)

### Models

**AgentShare:** `backend/app/models/agent_share.py`
- `original_agent_id`, `shared_with_user_id`, `shared_by_user_id` - Relationship UUIDs
- `share_mode` - "user" or "builder"
- `status` - Share lifecycle state
- `cloned_agent_id` - Reference to created clone (after acceptance)
- `shared_at`, `accepted_at`, `declined_at` - Timestamps

**Agent (clone fields):** `backend/app/models/agent.py`
- `is_clone` - Boolean flag
- `parent_agent_id` - FK to parent Agent (self-referential)
- `clone_mode` - "user" or "builder"
- `update_mode` - "automatic" or "manual"
- `pending_update`, `pending_update_at` - Update queue state
- `last_sync_at` - Last successful sync timestamp

**CredentialShare:** `backend/app/models/credential_share.py`
- Links credentials to users for read-only shared access
- Independent from agent sharing (standalone credential sharing)

**CloneUpdateRequest:** `backend/app/models/clone_update_request.py`
- `clone_agent_id`, `parent_agent_id`, `pushed_by_user_id` - Relationship UUIDs
- `copy_files_folder`, `rebuild_environment` - Optional actions
- `status` - "pending", "applied", or "dismissed"
- `created_at`, `applied_at`, `dismissed_at` - Timestamps

**Credential (sharing fields):** `backend/app/models/credential.py`
- `allow_sharing` - Whether credential can be shared
- `is_placeholder` - Placeholder for non-shareable credentials in clones
- `placeholder_source_id` - Original credential this placeholder was created from

## Backend Implementation

### Routes

**File:** `backend/app/api/routes/agent_shares.py`

**Owner Operations:**
- `POST /agents/{agent_id}/shares` - Share agent with user by email
- `GET /agents/{agent_id}/shares` - List shares for an agent
- `GET /agents/{agent_id}/clones` - List all clones of an agent
- `DELETE /agents/{agent_id}/shares/{share_id}?action=delete|detach` - Revoke share

**Recipient Operations:**
- `GET /shares/pending` - List pending shares for current user
- `POST /shares/{share_id}/accept` - Accept share, create clone
- `POST /shares/{share_id}/decline` - Decline share

**Clone Operations:**
- `POST /agents/{agent_id}/detach` - Detach clone from parent

**Update Operations:**
- `POST /agents/{agent_id}/shares/push-updates` - Push updates to all clones
- `POST /agents/{agent_id}/apply-update` - Apply pending update (clone owner)
- `GET /agents/{agent_id}/update-status` - Get update status for clone
- `PATCH /agents/{agent_id}/update-mode` - Set update mode for clone

### Services

**AgentService:** `backend/app/services/agent_service.py`
- `list_agents()` - List agents for user (all users including superusers only see own agents)

**AgentShareService:** `backend/app/services/agent_share_service.py`
- `share_agent()` - Create pending share, validate ownership and target user
- `accept_share()` - Accept share, create clone via AgentCloneService
- `decline_share()` - Decline pending share
- `revoke_share()` - Revoke share with delete/detach action
- `get_pending_shares()` - List pending shares for user
- `get_agent_shares()` - List shares for agent (owner view)
- `get_agent_clones()` - List all clones of agent

**AgentCloneService:** `backend/app/services/agent_clone_service.py`
- `create_clone()` - Create clone agent, environment, copy workspace, setup credentials
- `copy_workspace()` - Copy workspace directories (with optional files folder)
- `setup_clone_credentials()` - Link shareable credentials, create placeholders for private
- `detach_clone()` - Detach clone from parent
- `push_updates()` - Queue update requests to all clones with selected actions
- `apply_update()` - Apply pending update requests from parent to clone
- `get_update_status()` - Get update status for a clone
- `set_update_mode()` - Change automatic/manual update mode
- `get_pending_update_requests()` - Get pending update requests for a clone
- `dismiss_update_request()` - Dismiss an update request
- `check_and_apply_automatic_updates()` - Check and apply automatic updates (called before suspension)

### Configuration

**Settings:** `backend/app/core/config.py`
- `ENV_INSTANCES_DIR` - Base path for environment workspaces

## Frontend Implementation

### Components - Agents Page

**PendingAgentCard:** `frontend/src/components/Agents/PendingAgentCard.tsx`
- Displays pending shares with agent info, share mode, and sharer details
- Accept/Decline buttons trigger wizard or decline mutation

**AgentCard:** `frontend/src/components/Agents/AgentCard.tsx`
- Clone badge indicator for cloned agents
- Update available badge for clones with `pending_update=true`
- Clone mode badge (User Access / Builder Access)
- Source info showing who shared the agent

### Components - Accept Share Wizard

**AcceptShareWizard:** `frontend/src/components/Agents/AcceptShareWizard/AcceptShareWizard.tsx`
- Multi-step wizard container with step tracking
- Manages state across wizard steps

**WizardStepOverview:** `frontend/src/components/Agents/AcceptShareWizard/WizardStepOverview.tsx`
- Step 1: Agent overview, share mode display, sharer info

**WizardStepCredentials:** `frontend/src/components/Agents/AcceptShareWizard/WizardStepCredentials.tsx`
- Step 2: Credential input forms for placeholders requiring user configuration

**WizardStepConfirm:** `frontend/src/components/Agents/AcceptShareWizard/WizardStepConfirm.tsx`
- Step 3: Final confirmation before creating clone

### Components - Share Management (Owner)

**ShareManagementDialog:** `frontend/src/components/Agents/ShareManagement/ShareManagementDialog.tsx`
- Main dialog with tabs: Shares list, Add Share, Push Updates
- Fetches shares and clones for the agent

**AddShareForm:** `frontend/src/components/Agents/ShareManagement/AddShareForm.tsx`
- Email input for target user
- Share mode selection (User/Builder) with descriptions

**ShareList:** `frontend/src/components/Agents/ShareManagement/ShareList.tsx`
- Table of current shares with status badges
- Revoke action button per share

**RevokeShareDialog:** `frontend/src/components/Agents/ShareManagement/RevokeShareDialog.tsx`
- Confirmation dialog for revoking access
- Delete vs Detach choice for accepted shares with clones

**ClonesList:** `frontend/src/components/Agents/ShareManagement/ClonesList.tsx`
- Table of clones with update mode and sync status
- Push Updates button to sync all clones

### Components - Clone Management

**UpdateBanner:** `frontend/src/components/Agents/CloneManagement/UpdateBanner.tsx`
- Alert banner shown when `pending_update=true`
- Apply Update button triggers ApplyUpdateDialog

**ApplyUpdateDialog:** `frontend/src/components/Agents/CloneManagement/ApplyUpdateDialog.tsx`
- Confirmation dialog for applying updates from parent
- Lists what will be updated (prompts, scripts, knowledge)

**DetachDialog:** `frontend/src/components/Agents/CloneManagement/DetachDialog.tsx`
- Confirmation dialog for detaching from parent
- Explains consequences (no more updates, full ownership)

**UpdateModeToggle:** `frontend/src/components/Agents/CloneManagement/UpdateModeToggle.tsx`
- Switch between automatic and manual update modes
- Uses `AgentSharesService.setUpdateMode()`

### Components - Agent Detail Integration

**AgentSharingTab:** `frontend/src/components/Agents/AgentSharingTab.tsx`
- Tab content for "Sharing" (owners) or "Clone Settings" (clones)
- For owners: Share management card with link to ShareManagementDialog
- For clones: Update mode toggle, detach option, parent info

### Routes

**Agents Page:** `frontend/src/routes/_layout/agents.tsx`
- Pending shares section at top showing PendingAgentCard components
- Fetches pending shares via `AgentSharesService.getPendingShares()`

**Agent Detail:** `frontend/src/routes/_layout/agent/$agentId.tsx`
- UpdateBanner shown for clones with pending updates
- "Sharing" tab (or "Clone Settings" for clones) added to HashTabs
- Clone badge in header with sharer info

### Generated Client

**File:** `frontend/src/client/sdk.gen.ts`

Service methods in `AgentSharesService`:
- `shareAgent()`, `getAgentShares()`, `getAgentClones()`, `revokeShare()`
- `getPendingShares()`, `acceptShare()`, `declineShare()`
- `detachClone()`
- `pushUpdatesToClones()`, `applyUpdate()`, `getUpdateStatus()`, `setUpdateMode()`

**Types:** `frontend/src/client/types.gen.ts`
- `AgentSharePublic`, `AgentSharesPublic`, `AgentShareCreate`
- `PendingSharePublic`, `PendingSharesPublic`
- `SetUpdateModeRequest`, `AcceptShareRequest`

## Security Features

**Credential Protection:**
- Non-shareable credentials never leave owner's agent
- Only structure (name, type) shared as placeholder
- Clone cannot access original credential values

**Session Isolation:**
- Each clone has separate sessions
- Workspace data isolated per clone

**Access Validation:**
- User authentication required
- Ownership validation for all operations
- Clone mode restrictions (user vs builder)

**Clone Re-sharing Prevention:**
- Clones cannot be shared while `parent_agent_id` is set
- Must detach first to enable sharing

**Superuser Consistency:**
- Superusers see only their own agents (same as regular users)
- No special admin visibility into other users' agents

## Key Integration Points

**Clone Creation Flow:** `AgentShareService.accept_share()`
1. Validate share exists and is pending
2. Validate recipient matches share target
3. Call `AgentCloneService.create_clone()`:
   - Creates clone agent record
   - Creates environment with `auto_start=True` and `source_environment_id` (spawns background task)
   - Sets up credentials (immediate, committed to DB)
4. Update share record with `cloned_agent_id`
5. Background task completes:
   - Builds Docker image
   - **Copies workspace files** from source environment (after directory exists)
   - Auto-starts the environment
   - UI shows "Activating" state during this process

**Workspace Copy:** `AgentCloneService.copy_workspace()`
- Called inside `_create_environment_background()` **after** environment directory is created
- Uses `ENV_INSTANCES_DIR` + environment ID paths
- Copies: `app/workspace/scripts/`, `app/workspace/docs/`, `app/workspace/knowledge/`, `app/workspace/files/`, `app/workspace/uploads/`
- Does NOT copy: logs, databases, credentials (handled separately via dynamic sync)

**Update Push Flow:** `AgentCloneService.push_updates()`
1. Verify ownership and non-clone status
2. Find all clones via `parent_agent_id`
3. Create `CloneUpdateRequest` record for each clone with selected actions
4. Set `pending_update=True` on all clones
5. Return counts of queued clones (automatic mode vs manual mode)

**Note:** Updates are queued, not immediately applied. For automatic mode clones, updates are applied when the environment becomes inactive (before suspension).

**Automatic Update Application:** `AgentCloneService.check_and_apply_automatic_updates()`
- Called by environment suspension scheduler before suspending an environment
- Checks if agent is a clone with `update_mode="automatic"` and `pending_update=True`
- Applies all pending update requests (syncs workspace, copies files if requested, rebuilds if requested)
- Marks update requests as applied

**Manual Update Application:** `AgentCloneService.apply_update()`
1. Get all pending `CloneUpdateRequest` records for the clone
2. Merge actions from all requests (if any request has an option enabled, that action is performed)
3. Copy workspace (standard update always happens)
4. Copy files folder if any request has `copy_files_folder=True`
5. Rebuild environment if any request has `rebuild_environment=True`
6. Mark all pending requests as applied

**Credential Setup:** `AgentCloneService.setup_clone_credentials()`
- Query `AgentCredentialLink` for original agent's credentials
- For `allow_sharing=True`: Create `CredentialShare` link
- For `allow_sharing=False`: Create placeholder `Credential`

## Access Control Rules

### Original Agent (Non-Clone)

| Action | Owner | Others |
|--------|-------|--------|
| View/Edit/Delete | Yes | No |
| Share | Yes | No |
| Push Updates | Yes | No |

### Clone - User Mode

| Action | Clone Owner |
|--------|-------------|
| View Configuration | Read-only |
| Edit Prompts | No |
| Edit Scheduler | Yes |
| Edit Handover Config | Yes |
| Conversation Mode | Yes |
| Building Mode | No |
| Apply Update / Detach | Yes |

### Clone - Builder Mode

| Action | Clone Owner |
|--------|-------------|
| Full Configuration | Yes |
| Edit Scheduler | Yes |
| Edit Handover Config | Yes |
| Building Mode | Yes |
| Share | No (while clone) |
| Apply Update / Detach | Yes |

### Clone-Specific Configurations

Certain configurations are **always editable by clone owners** regardless of clone mode, and are **never affected by push updates** from the parent agent:

| Configuration | Editable by Clone Owner | Synced During Push Updates |
|--------------|------------------------|---------------------------|
| **Scheduler** | Yes (both modes) | No |
| **Handover Config** | Yes (both modes) | No |
| **Interface Settings** | Yes (both modes) | No |
| **Prompts** | Builder mode only | Yes |
| **Scripts** | Builder mode only | Yes |
| **Knowledge** | Builder mode only | Yes |

**Rationale for Clone-Specific Configs**:

1. **Scheduler**: Clone owners may have different automation needs, timezones, and execution patterns than the original owner.

2. **Handover Config**: Handover requires ownership of both source and target agents. Clone owners have different agent portfolios than the original owner, so their handover targets must be different.

3. **Interface Settings**: Clone owners may want different UI configurations (conversation view, terminology, etc.).

**Important**: When a parent pushes updates to clones:
- Only workspace files (scripts, docs, knowledge) are synced
- Clone-specific configurations (scheduler, handover, interface) remain unchanged
- This ensures clones can be customized for the recipient's workflow while still receiving core functionality updates

## File Locations Reference

**Backend - Routes:**
- `backend/app/api/routes/agent_shares.py`
- `backend/app/api/routes/agents.py` (list agents via service)

**Backend - Services:**
- `backend/app/services/agent_service.py` (list_agents method)
- `backend/app/services/agent_share_service.py`
- `backend/app/services/agent_clone_service.py`

**Backend - Models:**
- `backend/app/models/agent.py` (clone fields)
- `backend/app/models/agent_share.py`
- `backend/app/models/credential.py` (sharing fields)
- `backend/app/models/credential_share.py`

**Backend - Migration:**
- `backend/app/alembic/versions/6fefe45793e6_add_agent_sharing_models.py`

**Frontend - Share Management:**
- `frontend/src/components/Agents/ShareManagement/ShareManagementDialog.tsx`
- `frontend/src/components/Agents/ShareManagement/AddShareForm.tsx`
- `frontend/src/components/Agents/ShareManagement/ShareList.tsx`
- `frontend/src/components/Agents/ShareManagement/ClonesList.tsx`
- `frontend/src/components/Agents/ShareManagement/RevokeShareDialog.tsx`

**Frontend - Clone Management:**
- `frontend/src/components/Agents/CloneManagement/UpdateBanner.tsx`
- `frontend/src/components/Agents/CloneManagement/ApplyUpdateDialog.tsx`
- `frontend/src/components/Agents/CloneManagement/DetachDialog.tsx`
- `frontend/src/components/Agents/CloneManagement/UpdateModeToggle.tsx`

**Frontend - Accept Wizard:**
- `frontend/src/components/Agents/AcceptShareWizard/AcceptShareWizard.tsx`
- `frontend/src/components/Agents/AcceptShareWizard/WizardStepOverview.tsx`
- `frontend/src/components/Agents/AcceptShareWizard/WizardStepCredentials.tsx`
- `frontend/src/components/Agents/AcceptShareWizard/WizardStepConfirm.tsx`

**Frontend - Agent Components:**
- `frontend/src/components/Agents/PendingAgentCard.tsx`
- `frontend/src/components/Agents/AgentCard.tsx`
- `frontend/src/components/Agents/AgentSharingTab.tsx`

**Frontend - Routes:**
- `frontend/src/routes/_layout/agents.tsx`
- `frontend/src/routes/_layout/agent/$agentId.tsx`

**Frontend - Client (auto-generated):**
- `frontend/src/client/sdk.gen.ts` (AgentSharesService)
- `frontend/src/client/types.gen.ts`

**Documentation:**
- `docs/business-domain/shared_agents_plan/` - Phased implementation plans

## Implementation Status

| Phase | Name | Status |
|-------|------|--------|
| 1 | Credential Sharing | Complete |
| 2 | Agent Sharing Models | Complete |
| 3 | Agent Sharing Backend | Complete |
| 4 | Update Mechanism | Complete |
| 5 | Frontend: Pending Shares | Complete |
| 6 | Frontend: Management | Complete |

---

**Document Version:** 3.1
**Last Updated:** 2026-01-19
**Status:** Complete (All Phases)
