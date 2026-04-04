# Agent Sharing - Technical Details

## File Locations

### Backend - Models

- `backend/app/models/sharing/agent_share.py` - AgentShare model (share records between users)
- `backend/app/models/agents/agent.py` - Clone fields on Agent model (`is_clone`, `parent_agent_id`, `clone_mode`, `update_mode`, `pending_update`, `pending_update_at`, `last_sync_at`)
- `backend/app/models/sharing/clone_update_request.py` - CloneUpdateRequest model (queued updates from parent to clone)
- `backend/app/models/credentials/credential.py` - Sharing fields (`allow_sharing`, `is_placeholder`, `placeholder_source_id`)
- `backend/app/models/credentials/credential_share.py` - CredentialShare model (read-only credential links)

### Backend - Routes

- `backend/app/api/routes/agent_shares.py` - All sharing endpoints (owner, recipient, clone, update operations)

### Backend - Services

- `backend/app/services/sharing/agent_share_service.py` - Share workflow logic (create, accept, decline, revoke)
- `backend/app/services/sharing/agent_clone_service.py` - Clone operations (create, detach, push updates, apply updates)
- `backend/app/services/agents/agent_service.py` - `list_agents()` method (includes clone visibility rules)

### Frontend - Share Management

- `frontend/src/components/Agents/ShareManagement/ShareManagementDialog.tsx` - Main dialog with tabs: Shares, Add Share, Push Updates
- `frontend/src/components/Agents/ShareManagement/AddShareForm.tsx` - Email input + share mode selection
- `frontend/src/components/Agents/ShareManagement/ShareList.tsx` - Table of shares with status badges and revoke action
- `frontend/src/components/Agents/ShareManagement/ClonesList.tsx` - Table of clones with update mode and sync status
- `frontend/src/components/Agents/ShareManagement/RevokeShareDialog.tsx` - Confirmation dialog with delete/detach/remove options

### Frontend - Accept Wizard

- `frontend/src/components/Agents/AcceptShareWizard/AcceptShareWizard.tsx` - Multi-step wizard container
- `frontend/src/components/Agents/AcceptShareWizard/WizardStepOverview.tsx` - Step 1: Agent overview
- `frontend/src/components/Agents/AcceptShareWizard/WizardStepCredentials.tsx` - Step 2: Placeholder credential setup
- `frontend/src/components/Agents/AcceptShareWizard/WizardStepAICredentials.tsx` - Step 3: AI credential selection
- `frontend/src/components/Agents/AcceptShareWizard/WizardStepConfirm.tsx` - Step 4: Final confirmation

### Frontend - Clone Management

- `frontend/src/components/Agents/CloneManagement/UpdateBanner.tsx` - Alert banner for pending updates
- `frontend/src/components/Agents/CloneManagement/ApplyUpdateDialog.tsx` - Confirmation dialog for applying updates
- `frontend/src/components/Agents/CloneManagement/DetachDialog.tsx` - Confirmation dialog for detaching from parent
- `frontend/src/components/Agents/CloneManagement/UpdateModeToggle.tsx` - Automatic/manual update mode switch
- `frontend/src/components/Agents/CloneManagement/PushUpdatesModal.tsx` - Modal for pushing updates with action options

### Frontend - Agent Integration

- `frontend/src/components/Agents/AgentSharingTab.tsx` - "Sharing" tab (owners) / "Clone Settings" tab (clones)
- `frontend/src/components/Agents/PendingAgentCard.tsx` - Pending share card with accept/decline
- `frontend/src/components/Agents/AgentCard.tsx` - Clone badge, update badge, mode badge, source info

### Frontend - Routes

- `frontend/src/routes/_layout/agents.tsx` - Pending shares section at top of Agents page
- `frontend/src/routes/_layout/agent/$agentId.tsx` - Update banner, Sharing/Clone Settings tab

### Frontend - Generated Client

- `frontend/src/client/sdk.gen.ts` - `AgentSharesService` with all share/clone/update methods
- `frontend/src/client/types.gen.ts` - `AgentSharePublic`, `PendingSharePublic`, `AgentShareCreate`, `SetUpdateModeRequest`, `AcceptShareRequest`, `CloneUpdateRequestPublic`

### Migrations

- `backend/app/alembic/versions/6fefe45793e6_add_agent_sharing_models.py` - Creates `agent_share` table, adds clone fields to `agent`, adds placeholder fields to `credential`
- `backend/app/alembic/versions/7aeed6ea3abf_add_share_source_and_session_email_.py` - Adds share source tracking

### Tests

- `backend/tests/api/agents/` - Agent sharing test scenarios (within agents test directory)

## Database Schema

### AgentShare Table

- `id` (UUID, PK)
- `original_agent_id` (FK → Agent) - The agent being shared
- `shared_with_user_id` (FK → User) - Recipient
- `shared_by_user_id` (FK → User) - Owner who created the share
- `share_mode` (str) - "user" or "builder"
- `status` (str) - "pending", "accepted", "declined", "revoked", "deleted"
- `cloned_agent_id` (FK → Agent, nullable) - Created clone reference
- `source` (str) - "manual" or "email_integration"
- `provide_ai_credentials` (bool) - Whether owner provides AI credentials
- `conversation_ai_credential_id`, `building_ai_credential_id` (FK → AICredential, nullable)
- `shared_at`, `accepted_at`, `declined_at` (timestamps)
- **Indexes**: `ix_agent_share_original_agent`, `ix_agent_share_recipient`, `ix_agent_share_status`
- **Unique constraint**: `uq_agent_share_agent_user` (one share per agent+user pair)

### Agent Table (Clone Fields)

- `is_clone` (bool, default False)
- `parent_agent_id` (FK → Agent, nullable, self-referential)
- `clone_mode` (str, nullable) - "user" or "builder"
- `update_mode` (str, nullable) - "automatic" or "manual"
- `pending_update` (bool, default False)
- `pending_update_at` (datetime, nullable)
- `last_sync_at` (datetime, nullable)

### CloneUpdateRequest Table

- `id` (UUID, PK)
- `clone_agent_id` (FK → Agent)
- `parent_agent_id` (FK → Agent)
- `pushed_by_user_id` (FK → User)
- `copy_files_folder` (bool) - Whether to copy files/ and uploads/
- `rebuild_environment` (bool) - Whether to rebuild Docker environment
- `status` (str) - "pending", "applied", "dismissed"
- `created_at`, `applied_at`, `dismissed_at` (timestamps)

### Credential Table (Sharing Fields)

- `allow_sharing` (bool) - Whether credential can be shared with clones
- `is_placeholder` (bool) - Placeholder for non-shareable credentials
- `placeholder_source_id` (FK → Credential, nullable) - Original credential reference

### CredentialShare Table

- Links credentials to users for read-only shared access
- Created during clone setup for `allow_sharing=true` credentials

## API Endpoints

### Owner Operations

- `POST /api/v1/agents/{agent_id}/shares` - Share agent with user by email
- `GET /api/v1/agents/{agent_id}/shares` - List shares for an agent
- `GET /api/v1/agents/{agent_id}/clones` - List all clones of an agent
- `DELETE /api/v1/agents/{agent_id}/shares/{share_id}?action=delete|detach|remove` - Revoke share

### Recipient Operations

- `GET /api/v1/shares/pending` - List pending shares for current user
- `POST /api/v1/shares/{share_id}/accept` - Accept share and create clone
- `POST /api/v1/shares/{share_id}/decline` - Decline share

### Clone Operations

- `POST /api/v1/agents/{agent_id}/detach` - Detach clone from parent

### Update Operations

- `POST /api/v1/agents/{agent_id}/shares/push-updates` - Push updates to all clones
- `POST /api/v1/agents/{agent_id}/apply-update` - Apply pending update (clone owner)
- `GET /api/v1/agents/{agent_id}/update-status` - Get update status for clone
- `PATCH /api/v1/agents/{agent_id}/update-mode` - Set update mode for clone
- `GET /api/v1/agents/{agent_id}/update-requests` - Get pending update requests for clone
- `POST /api/v1/update-requests/{request_id}/dismiss` - Dismiss an update request

## Services & Key Methods

### AgentShareService (`backend/app/services/sharing/agent_share_service.py`)

- `share_agent()` - Create pending share, validate ownership and target user, enforce unique constraint
- `create_auto_share()` - Create pre-accepted share for email integration (skips pending state)
- `accept_share()` - Validate share, create clone via AgentCloneService, update share status
- `decline_share()` - Mark share as declined
- `revoke_share()` - Revoke share with delete/detach action
- `delete_share_record()` - Remove share record (for terminal states only)
- `get_pending_shares()` - List pending shares for user as recipient
- `get_agent_shares()` - List all shares for an agent (owner view)
- `get_agent_clones()` - List all clones of an agent

### AgentCloneService (`backend/app/services/sharing/agent_clone_service.py`)

- `create_clone()` - Create clone agent record, environment with `source_environment_id`, copy workspace, setup credentials, AI credential sharing
- `copy_workspace()` - Copy workspace directories (scripts, docs, knowledge, files, uploads) using `ENV_INSTANCES_DIR` paths
- `setup_clone_credentials()` - Link shareable credentials via CredentialShare, create placeholders for private ones
- `detach_clone()` - Clear `parent_agent_id` and `is_clone`, make clone independent
- `push_updates()` - Create CloneUpdateRequest per clone, set `pending_update=True`
- `apply_update()` - Merge actions from all pending requests, sync workspace, optionally copy files and rebuild
- `check_and_apply_automatic_updates()` - Called by environment suspension scheduler for auto-mode clones
- `get_update_status()` - Return pending_update, last_sync_at, update_mode, parent info
- `set_update_mode()` - Switch between automatic/manual
- `get_pending_update_requests()` - Get pending CloneUpdateRequest records
- `dismiss_update_request()` - Mark request as dismissed

## Frontend Components

### Share Management Dialog

`ShareManagementDialog.tsx` - Main dialog with tabbed interface:
- Tab 1: Share list (ShareList) showing current shares with status
- Tab 2: Add share form (AddShareForm) with email + mode selection
- Tab 3: Clones list (ClonesList) with push updates functionality

### Accept Share Wizard

`AcceptShareWizard.tsx` - 4-step wizard:
1. Overview → 2. Credentials → 3. AI Credentials → 4. Confirm
- Manages state across steps, handles accept mutation on final step
- Credentials step only shown when agent has non-shareable credentials

### Clone Management Components

- `UpdateBanner.tsx` - Persistent alert shown at top of agent detail when `pending_update=true`
- `ApplyUpdateDialog.tsx` - Lists pending update requests with action details
- `PushUpdatesModal.tsx` - Owner selects optional actions (copy files, rebuild) before pushing
- `UpdateModeToggle.tsx` - Uses `AgentSharesService.setUpdateMode()` API
- `DetachDialog.tsx` - Explains consequences of detaching

### Agent Integration

- `AgentSharingTab.tsx` - Conditionally renders owner sharing controls or clone settings based on agent role
- `PendingAgentCard.tsx` - Displayed in agents list, launches AcceptShareWizard on accept
- `AgentCard.tsx` - Shows clone badge, update available indicator, clone mode badge

## Configuration

- `ENV_INSTANCES_DIR` (`backend/app/core/config.py`) - Base path for environment workspaces, used for workspace copy operations

## Security

- **Credential isolation** - Non-shareable credentials never leave owner's agent; only structure (name, type) shared as placeholder
- **Session isolation** - Each clone has separate sessions and workspace data
- **Ownership validation** - All operations verify user owns the agent or is the intended recipient
- **Clone re-sharing prevention** - Clones cannot be shared while `parent_agent_id` is set
- **AI credential isolation** - Shared AI credentials use `AICredentialShare` model with explicit grant, no ambient access
