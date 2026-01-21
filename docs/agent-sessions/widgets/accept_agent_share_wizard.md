# Accept Agent Share Wizard - Implementation Reference

## Purpose

Multi-step dialog wizard that guides users through accepting a shared agent, handling credential setup, and creating a clone agent with proper configuration.

## Feature Overview

**Flow:**
1. User receives pending share notification on Agents page
2. User clicks "Accept" on pending share card
3. Wizard opens with step indicator
4. Step 1 (Overview): Review agent details, access level, what they'll receive
5. Step 2 (AI Credentials): Select AI credentials or see owner-provided ones (conditional)
6. Step 3 (Integration Credentials): Configure non-shareable credentials (conditional)
7. Step 4 (Confirm): Review summary and accept
8. Clone agent created, wizard closes, agents list refreshes

**Key Concepts:**
- **Dynamic Step Count**: 2-4 steps based on AI credentials and integration credentials requirements
- **AI Credential Categories**: Owner-provided (ready to use) vs User-selected (choose from own credentials)
- **Integration Credential Categories**: Grouped by type with dropdown selection (only types shown, not owner's credential names for privacy)
- **Credential Selection Options**: Use shared from owner, select from existing credentials, or create new
- **Inline Credential Creation**: Users can create new credentials directly within the wizard (modal opens immediately on selection)
- **Skip Option**: Users can skip non-shareable credential setup and configure later
- **Clone Creation**: Calls `AgentSharesService.acceptShare()` with credential selections (IDs) and AI credential selections

## Architecture

```
PendingAgentCard → AcceptShareWizard → WizardSteps → AgentSharesService.acceptShare()
                           │
                    ┌──────┴──────────────────────┐
                    │             │               │
             WizardStepOverview   │    WizardStepCredentials
                    │             │               │
                    │   WizardStepAICredentials   │
                    │             │               │
                    └─────────────┴───────────────┘
                                  │
                         WizardStepConfirm
                                  │
                         AgentCloneService.create_clone()
```

## Data/State Lifecycle

### Wizard State

| State | Type | Description |
|-------|------|-------------|
| `currentStep` | `"overview" \| "ai_credentials" \| "credentials" \| "confirm"` | Active wizard step |
| `credentialSelections` | `Record<string, CredentialSelection>` | User's credential selections by credential name |
| `aiCredentialSelections` | `{conversationCredentialId, buildingCredentialId}` | User's AI credential selections |

**CredentialSelection Structure:**
```typescript
{
  sourceCredentialName: string    // Original credential name (internal use only, not displayed to user)
  sourceCredentialType: string    // Credential type (e.g., "api_token", "gmail_oauth")
  allowSharing: boolean           // Whether the original credential allows sharing
  selectedCredentialId: string | null  // User's selected credential ID (null = use shared)
}
```

**Privacy Note:** Original credential names from the owner's agent are never displayed to the recipient. Only credential types are shown (e.g., "Gmail OAuth", "API Token") to protect the owner's naming conventions and potential sensitive information in credential names.

### Step Navigation Logic

| Condition | Step Progression |
|-----------|------------------|
| AI creds provided + no integration creds required | Overview → Confirm (2 steps) |
| AI creds provided + has integration creds required | Overview → Credentials → Confirm (3 steps) |
| AI creds needed + no integration creds required | Overview → AI Credentials → Confirm (3 steps) |
| AI creds needed + has integration creds required | Overview → AI Credentials → Credentials → Confirm (4 steps) |

**AI Credentials Step Condition:**
- Shown when `!share.ai_credentials_provided && share.required_ai_credential_types.length > 0`

### AI Credential Categories

| Category | Condition | UI Treatment |
|----------|-----------|--------------|
| Provided by Owner | `share.ai_credentials_provided` | Green section, "Provided by owner" badge |
| User Default Available | User has default for SDK type | Green badge, "Using default: [Name]" |
| Selection Required | No default, user has credentials | Dropdown to select credential |
| Setup Required | No credentials for SDK type | Error message, link to Settings |

### Integration Credential Requirement Categories

| Category | `allow_sharing` | UI Treatment |
|----------|-----------------|--------------|
| Shared Available | `true` | Green card, dropdown with "Use shared from owner" default + user's own credentials |
| Your Credential Required | `false` | Yellow card, dropdown with user's own credentials only |

**Dropdown Options for Each Credential:**
1. "Use shared from owner" (only for shareable credentials, default selection)
2. User's existing credentials of the same type
3. "Create new credential..." option (immediately opens creation dialog when selected)

## Database Schema

### Related Models

**PendingSharePublic:** `backend/app/models/agent_share.py`
- `id`, `original_agent_id`, `original_agent_name`
- `share_mode` - "user" or "builder"
- `shared_by_email`, `shared_by_name`
- `credentials_required` - List of `CredentialRequirement`
- `ai_credentials_provided` - Whether owner is providing AI credentials
- `conversation_ai_credential_name` - Name of provided conversation credential (if any)
- `building_ai_credential_name` - Name of provided building credential (if any)
- `required_ai_credential_types` - List of `AICredentialRequirement`

**CredentialRequirement:** `backend/app/models/agent_share.py`
- `name` - Credential name
- `type` - Credential type (e.g., "api_token", "gmail_oauth")
- `allow_sharing` - Whether owner has enabled sharing

**AICredentialRequirement:** `backend/app/models/agent_share.py`
- `sdk_type` - SDK type (e.g., "anthropic", "minimax")
- `purpose` - "conversation" or "building"

### Clone Creation

When wizard completes, `AgentCloneService.create_clone()` creates:
- Agent record with `is_clone=true`, `parent_agent_id` set
- Environment for clone (same template and SDK settings as parent)
- Workspace files copied from parent
- Integration credentials: shared (via `CredentialShare`) or placeholders
- AI credentials: shared (via `AICredentialShare`) or linked to recipient's credentials

## Backend Implementation

### Routes

**File:** `backend/app/api/routes/agent_shares.py`

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/shares/pending` | List pending shares for current user |
| `POST` | `/shares/{share_id}/accept` | Accept share with credentials data |
| `POST` | `/shares/{share_id}/decline` | Decline share |

### Services

**AgentShareService:** `backend/app/services/agent_share_service.py`
- `get_pending_shares(session, user_id)` - Query pending shares for user
- `accept_share(session, share_id, recipient_id, credentials_data)` - Validate and create clone

**AgentCloneService:** `backend/app/services/agent_clone_service.py`
- `create_clone(session, original_agent, recipient_id, clone_mode, credentials_data)` - Full clone creation
- `copy_workspace(original_env_id, clone_env_id)` - Copy scripts, docs, knowledge
- `setup_clone_credentials(session, original_agent, clone, user_provided_data)` - Handle credential sharing/placeholders

### Request/Response Models

**AcceptShareRequest:** `backend/app/api/routes/agent_shares.py`
- `credentials` - Optional dict `{credential_name: credential_id}` for credential selections
  - If a credential ID is provided, that credential is linked to the clone
  - If null/undefined for a shareable credential, the shared credential is used
  - If null/undefined for a non-shareable credential, a placeholder is created
- `ai_credential_selections` - Optional dict `{conversation_credential_id, building_credential_id}`

**AICredentialSelections:** `backend/app/api/routes/agent_shares.py`
- `conversation_credential_id` - UUID of user's AI credential for conversation SDK
- `building_credential_id` - UUID of user's AI credential for building SDK

## Frontend Implementation

### Component Structure

**AcceptShareWizard:** `frontend/src/components/Agents/AcceptShareWizard/AcceptShareWizard.tsx`
- Main dialog container with step indicator
- Manages wizard state (`currentStep`, `credentialsData`, `aiCredentialSelections`)
- Dynamic step navigation based on AI credentials and integration credentials requirements
- Handles accept mutation via `AgentSharesService.acceptShare()` with AI credential selections

**WizardStepOverview:** `frontend/src/components/Agents/AcceptShareWizard/WizardStepOverview.tsx`
- Displays agent info, sharer details
- Explains access level (User vs Builder permissions)
- Lists what recipient will receive (copy, prompts, scripts, updates)

**WizardStepAICredentials:** `frontend/src/components/Agents/AcceptShareWizard/WizardStepAICredentials.tsx`
- Shows AI credential status for required SDK types
- If owner provided: Green section showing "Provided by owner" with credential names
- If user must provide: Queries user's AI credentials via `AiCredentialsService.listAiCredentials()`
- Shows default credentials with green badge or dropdown for selection
- Validates all required SDK types have credentials before allowing Continue

**WizardStepCredentials:** `frontend/src/components/Agents/AcceptShareWizard/WizardStepCredentials.tsx`
- Groups credentials by type with visual type badges (credential names from owner are NOT shown for privacy)
- Displays informational note: newly created credentials need to be configured later
- Fetches user's existing credentials via `CredentialsService.readCredentials()`
- For each credential type, displays dropdown with:
  - "Use shared from owner" option (for shareable credentials, default)
  - User's existing credentials of the same type
  - "Create new credential..." option (immediately opens dialog when selected)
- Inline credential creation dialog:
  - Opens immediately when "Create new credential..." is selected
  - Auto-focus on name input field
  - Enter key submits the form
  - Auto-selects newly created credentials after creation
- Allows "Skip for now" for non-shareable credentials without selections

**WizardStepConfirm:** `frontend/src/components/Agents/AcceptShareWizard/WizardStepConfirm.tsx`
- Summary card with agent name, access level, sharer
- Credentials status summary:
  - "Using owner's shared credentials" count (green)
  - "Using your own credentials" count (blue)
  - "Skipped (setup later)" count (yellow)
- Warning alert if non-shareable credentials were skipped
- Final accept button triggers clone creation

**Index Export:** `frontend/src/components/Agents/AcceptShareWizard/index.ts`
- Exports `AcceptShareWizard` component

### Parent Components

**PendingAgentCard:** `frontend/src/components/Agents/PendingAgentCard.tsx`
- Card displaying pending share info
- Accept/Decline buttons
- Clicking Accept opens wizard (in parent route)

**Agents Page:** `frontend/src/routes/_layout/agents.tsx`
- Fetches pending shares via `AgentSharesService.getPendingShares()`
- Renders PendingAgentCard for each pending share
- Manages wizard open state and selected share
- Handles decline mutation inline

### Hooks and State

**React Query Usage:**
- `useMutation()` for `AgentSharesService.acceptShare()`
- `useQuery()` for `AiCredentialsService.listAiCredentials()` in AI credentials step
- `useQuery()` for `CredentialsService.readCredentials()` in integration credentials step
- `useMutation()` for `CredentialsService.createCredential()` for inline credential creation
- Query invalidation on success: `["agents"]`, `["pendingShares"]`, `["credentials"]`

**Component Props:**
- `open` / `onOpenChange` - Dialog visibility control
- `share` - `PendingSharePublic` object with all share details
- `onComplete` - Callback after successful acceptance

**AI Credentials Step Props:**
- `share` - Contains `ai_credentials_provided`, `required_ai_credential_types`
- `aiCredentialSelections` - Current selections `{conversationCredentialId, buildingCredentialId}`
- `onChange` - Callback to update selections
- `onNext` / `onBack` - Navigation callbacks

**Integration Credentials Step Props:**
- `share` - Contains `credentials_required` list with type and allow_sharing info
- `credentialSelections` - Current selections `Record<string, CredentialSelection>`
- `onChange` - Callback to update selections when user picks from dropdown or creates new
- `onNext` / `onBack` - Navigation callbacks

## Security Features

### Validation Rules

**Backend:**
- Share must exist and be in "pending" status
- Recipient ID must match `shared_with_user_id`
- Original agent must still exist

**Frontend:**
- No sensitive credential data passed through wizard (only credential IDs)
- Inline credential creation creates credentials on the backend, returns only ID
- No credential values stored in URL or visible state

### Access Control

- Only the designated recipient can accept/decline
- Credentials with `allow_sharing=false` require user to select or create their own credentials
- User can only select credentials they own or have been shared with them
- Backend validates credential ownership/access before linking
- Clone inherits share mode (user/builder) restrictions

## Key Integration Points

### Wizard Entry Point

`frontend/src/routes/_layout/agents.tsx`
- Renders `AcceptShareWizard` when user clicks Accept on `PendingAgentCard`
- Passes selected `PendingSharePublic` object to wizard

### Clone Creation Chain

1. `AcceptShareWizard` calls `AgentSharesService.acceptShare()`
2. Route `POST /shares/{share_id}/accept` → `agent_shares.py:accept_share()`
3. `AgentShareService.accept_share()` validates and delegates to clone service
4. `AgentCloneService.create_clone()` creates agent, environment, workspace, credentials

### Credential Resolution

`backend/app/api/routes/agent_shares.py:_share_to_pending_public()`
- Queries `AgentCredentialLink` for original agent's credentials
- Builds `CredentialRequirement` list with `allow_sharing` status
- Frontend uses this to determine which step/fields to show

## File Locations Reference

### Frontend - Wizard Components

- `frontend/src/components/Agents/AcceptShareWizard/AcceptShareWizard.tsx` - Main wizard container
- `frontend/src/components/Agents/AcceptShareWizard/WizardStepOverview.tsx` - Step 1: Agent overview
- `frontend/src/components/Agents/AcceptShareWizard/WizardStepAICredentials.tsx` - Step 2: AI credentials (conditional)
- `frontend/src/components/Agents/AcceptShareWizard/WizardStepCredentials.tsx` - Step 3: Integration credential setup (conditional)
- `frontend/src/components/Agents/AcceptShareWizard/WizardStepConfirm.tsx` - Final step: Confirmation
- `frontend/src/components/Agents/AcceptShareWizard/index.ts` - Module export

### Frontend - Related Components

- `frontend/src/components/Agents/PendingAgentCard.tsx` - Share card with Accept/Decline
- `frontend/src/routes/_layout/agents.tsx` - Agents page with pending shares section

### Backend - Routes

- `backend/app/api/routes/agent_shares.py` - Share acceptance endpoints

### Backend - Services

- `backend/app/services/agent_share_service.py` - `accept_share()`, `get_pending_shares()`
- `backend/app/services/agent_clone_service.py` - `create_clone()`, `setup_clone_credentials()`

### Backend - Models

- `backend/app/models/agent_share.py` - `PendingSharePublic`, `CredentialRequirement`
- `backend/app/models/credential.py` - `Credential.allow_sharing` field

### Frontend - Generated Client

- `frontend/src/client/sdk.gen.ts` - `AgentSharesService.acceptShare()`, `getPendingShares()`
- `frontend/src/client/types.gen.ts` - `PendingSharePublic`, `AcceptShareRequest`

### Related Documentation

- `docs/business-domain/shared_agents_management.md` - Full sharing feature overview
- `docs/business-domain/ai_credentials_management.md` - AI credentials management (Phase 2 implemented)

---

**Document Version:** 3.1
**Last Updated:** 2026-01-21
**Status:** Complete (Improved Credentials Step: type-only display for privacy, immediate modal on create, Enter to submit)
