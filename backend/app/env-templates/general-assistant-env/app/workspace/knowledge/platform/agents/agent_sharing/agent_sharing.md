# Agent Sharing

## Purpose

Enable users to share agents with other users via a clone-based model where recipients get isolated copies with independent sessions, while owners can push updates to all clones.

## Core Concepts

- **Clone-based sharing** - Recipients receive functional copies of the agent, not shared access to the original. Each clone has its own environment, sessions, and data
- **Share modes** - Two levels of access: "user" (conversation only, read-only config) and "builder" (full configuration and building access)
- **Push updates** - Owners can push workspace changes (scripts, prompts, knowledge) to all clones. Clones choose automatic or manual update mode
- **Credential handling** - Shareable credentials are linked read-only; private credentials create placeholders that recipients must configure
- **Detach** - Clone owners can detach from the parent to become fully independent agents, severing the update relationship

## User Stories / Flows

### Sharing an Agent (Owner)

1. Owner opens agent detail page, navigates to "Sharing" tab
2. Opens Share Management dialog, enters recipient's email and selects share mode (User/Builder)
3. Optionally provides AI credentials for the clone to use
4. System creates a pending share record
5. Recipient sees pending share on their Agents page

### Accepting a Share (Recipient)

1. Recipient opens Agents page, sees pending share card with agent info and share mode
2. Clicks "Accept" to launch the Accept Share Wizard:
   - **Step 1 - Overview**: Agent name, description, sharer info, share mode
   - **Step 2 - Credentials**: Configure values for any placeholder credentials (non-shareable ones)
   - **Step 3 - AI Credentials**: Select AI credentials for conversation/building (if owner provided theirs, can use those)
   - **Step 4 - Confirm**: Review and confirm
3. System creates clone agent with its own environment (built in background)
4. Clone appears in recipient's agent list with clone badge

### Pushing Updates (Owner)

1. Owner opens Share Management dialog for the agent
2. Navigates to Clones tab, clicks "Push Updates"
3. Selects optional actions: copy files/uploads folder, rebuild environment
4. System creates update requests for each clone:
   - **Automatic mode clones**: Updates queued, applied when environment becomes inactive (before suspension)
   - **Manual mode clones**: Update notification shown, clone owner decides when to apply

### Applying Updates (Clone Owner)

1. Clone owner sees update banner on agent detail page
2. Reviews pending update requests (what will be synced)
3. Clicks "Apply Update" or dismisses individual requests
4. System syncs workspace from parent (scripts, docs, knowledge) and performs any optional actions

### Detaching a Clone

1. Clone owner opens agent detail, navigates to "Clone Settings" tab
2. Clicks "Detach from Parent"
3. Confirms in dialog (explains consequences: no more updates, full ownership)
4. Clone becomes independent agent, can now be shared by the new owner

### Revoking a Share (Owner)

1. Owner opens Share Management dialog, sees list of shares
2. Clicks revoke on a share, chooses action:
   - **Delete**: Removes the clone and all its data
   - **Detach**: Clone becomes independent (recipient keeps it)
   - **Remove**: Removes the share record (for declined/revoked shares)

## Business Rules

### Share Lifecycle

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

### Clone Update Requests

When an owner pushes updates, a `CloneUpdateRequest` record is created per clone:
- **Standard update** (always): Syncs scripts, docs, knowledge, workspace_requirements.txt
- **Copy files folder** (optional): Also copies `files/` and `uploads/` directories
- **Rebuild environment** (optional): Triggers full Docker environment rebuild
- Request status: `pending` → `applied` or `dismissed`

### Credential Handling During Clone Creation

| Credential Type | During Clone | Result |
|-----------------|--------------|--------|
| `allow_sharing=true` | CredentialShare link created | Shared read-only access |
| `allow_sharing=false` | Placeholder created | User must configure values |

### AI Credential Handling

| Scenario | Behavior |
|----------|---------|
| Owner provides AI credentials | Shared via `AICredentialShare`, stored on clone environment |
| Recipient selects own credentials | Recipient's credential IDs stored on environment |
| Neither provided | Environment uses recipient's default AI credentials from profile |

When AI credentials are explicitly assigned to an environment (shared or selected), the system uses **only** those credentials with **no fallback** to profile credentials.

**Resolution Priority:**
1. Explicitly provided API keys (during environment creation)
2. Named credentials stored on environment - checked via `get_credential_for_use()` which handles both owned and shared credentials
3. User's profile credentials - **only if no credentials are specifically assigned**

### Access Control

**Original Agent (Non-Clone):**

| Action | Owner | Others |
|--------|-------|--------|
| View/Edit/Delete | Yes | No |
| Share | Yes | No |
| Push Updates | Yes | No |

**Clone - User Mode:**

| Action | Clone Owner |
|--------|-------------|
| View Configuration | Read-only |
| Edit Prompts/Scripts | No |
| Edit Scheduler/Handover/Interface | Yes |
| Conversation Mode | Yes |
| Building Mode | No |
| Apply Update / Detach | Yes |

**Clone - Builder Mode:**

| Action | Clone Owner |
|--------|-------------|
| Full Configuration | Yes |
| Edit Scheduler/Handover/Interface | Yes |
| Building Mode | Yes |
| Share (while clone) | No |
| Apply Update / Detach | Yes |

### Clone-Specific Configurations

These are **always editable** by clone owners regardless of mode, and **never synced** during push updates:

| Configuration | Rationale |
|--------------|-----------|
| Scheduler | Different automation needs, timezones, execution patterns |
| Handover Config | Different agent portfolios, handover targets |
| Interface Settings | Different UI preferences |

Only workspace files (scripts, docs, knowledge) are synced during push updates.

### Sharing Constraints

- Clones cannot be shared while `parent_agent_id` is set - must detach first
- Self-sharing is prevented (cannot share with yourself)
- Unique constraint: one share per agent+user pair
- Superusers see only their own agents (no special admin visibility)

### Share Sources

Shares can be created from two sources:
- **manual** - Owner explicitly shares via UI
- **email_integration** - System creates auto-accepted shares for email-based agent collaboration

## Architecture Overview

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

**Clone Creation Flow:**
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

## Integration Points

- **[Agent Environments](../agent_environments/agent_environments.md)** - Clone creation spawns a new environment with `source_environment_id` for workspace copying
- **[Agent Credentials](../agent_credentials/agent_credentials.md)** - Credential sharing/placeholder creation during clone setup. See [credential sharing](../agent_credentials/credential_sharing.md)
- **[AI Credentials](../../application/ai_credentials/ai_credentials.md)** - AI credential sharing via `AICredentialShare` model
- **[Agent Environment Data Management](../agent_environment_data_management/agent_environment_data_management.md)** - Workspace file copying during clone creation and updates
- **[Email Sessions](../../application/email_integration/email_sessions.md)** - Auto-share creation for email integration agents
- **[Guest Sharing](guest_sharing.md)** - Token-based anonymous access to agents (separate sharing mechanism)
