# AI Credentials Management - Implementation Reference

## Purpose

Enable users to manage multiple named AI credentials (API keys) for different SDK providers, with default credential selection that auto-syncs to user profile for backward compatibility.

## Feature Overview

- User creates named AI credentials (e.g., "Production Anthropic", "Testing OpenAI")
- User marks one credential of each type as "default"
- Default credential values auto-sync to user's profile fields (`ai_credentials_encrypted`)
- Existing environment creation continues to work via synced profile fields
- Explicit credential linking to agent environments
- Owner-provided credentials during agent sharing
- AI credentials step in accept share wizard
- Clone credential setup with sharing support

## Architecture

```
User creates AI Credential → Encrypted storage in ai_credential table
                          ↓
User sets as default → Auto-sync to user.ai_credentials_encrypted
                          ↓
Environment creation → Reads from user profile (backward compatible)
```

**Key Concepts:**
- **Named AI Credentials**: Reusable credentials with names, stored encrypted
- **Default Credentials**: One credential per type marked as default
- **Auto-Sync**: Default credentials automatically update user profile fields

## Data/State Lifecycle

### AI Credential Types

| Type | SDK ID | Required Fields |
|------|--------|-----------------|
| `anthropic` | `claude-code/anthropic` | `api_key` |
| `minimax` | `claude-code/minimax` | `api_key` |
| `openai_compatible` | `google-adk-wr/openai-compatible` | `api_key`, `base_url`, `model` |

**Reference:** SDK to credential type mapping in `backend/app/services/environment_service.py:24` (`SDK_API_KEY_MAP`)

### Credential States

| Field | Values | Description |
|-------|--------|-------------|
| `is_default` | `true` / `false` | Whether this is the default for its type |
| `encrypted_data` | JSON string | Encrypted `{api_key, base_url?, model?}` |

## Database Schema

**Migration:** `backend/app/alembic/versions/h8c9d0e1f2g3_add_ai_credentials_table.py`

**Table:** `ai_credential`
- `id` (UUID, PK)
- `owner_id` (UUID, FK → user.id, CASCADE)
- `name` (VARCHAR 255)
- `type` (VARCHAR 50) - "anthropic" | "minimax" | "openai_compatible"
- `encrypted_data` (TEXT) - Fernet-encrypted JSON
- `is_default` (BOOLEAN)
- `created_at`, `updated_at` (DATETIME)

**Indexes:**
- `ix_ai_credential_owner_type` - (owner_id, type)
- `ix_ai_credential_owner_default` - (owner_id, is_default)

**Models:** `backend/app/models/ai_credential.py`
- `AICredential` (table model)
- `AICredentialCreate`, `AICredentialUpdate` (input schemas)
- `AICredentialPublic`, `AICredentialsPublic` (response schemas)
- `AICredentialType` (enum)
- `AICredentialData` (internal decrypted data schema)

## Backend Implementation

### API Routes

**File:** `backend/app/api/routes/ai_credentials.py`

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/v1/ai-credentials/` | List user's AI credentials |
| `POST` | `/api/v1/ai-credentials/` | Create new AI credential |
| `GET` | `/api/v1/ai-credentials/{credential_id}` | Get credential details |
| `PATCH` | `/api/v1/ai-credentials/{credential_id}` | Update credential |
| `DELETE` | `/api/v1/ai-credentials/{credential_id}` | Delete credential |
| `POST` | `/api/v1/ai-credentials/{credential_id}/set-default` | Set as default, sync to user profile |

**Router Registration:** `backend/app/api/main.py`

### Service

**File:** `backend/app/services/ai_credentials_service.py`

**Class:** `AICredentialsService` (singleton: `ai_credentials_service`)

**Methods:**
- `list_credentials(session, user_id)` - List all credentials for user
- `get_credential(session, credential_id, user_id)` - Get with ownership check
- `create_credential(session, user_id, data)` - Create and encrypt
- `update_credential(session, credential_id, user_id, data)` - Update encrypted data
- `delete_credential(session, credential_id, user_id)` - Delete, clear profile if default
- `set_default(session, credential_id, user_id)` - Set default, sync to user profile
- `get_default_for_type(session, user_id, cred_type)` - Get default credential
- `_decrypt_credential(credential)` - Decrypt to `AICredentialData`
- `_sync_default_to_user_profile(session, user, credential)` - Auto-sync to user fields
- `_clear_user_profile_for_type(session, user, cred_type)` - Clear on default deletion

### Encryption

**Pattern:** Uses existing Fernet encryption from `backend/app/core/security.py`
- `encrypt_field(value)` - Encrypt string
- `decrypt_field(encrypted_value)` - Decrypt string

Credential data stored as encrypted JSON: `{"api_key": "...", "base_url": "...", "model": "..."}`

### Auto-Sync Logic

When `set_default()` is called:
1. Unset previous default for same type
2. Set new credential as default
3. Decrypt credential data
4. Update user's `ai_credentials_encrypted` via `crud.update_user_ai_credentials()`

**Mapping:**
- `anthropic` → `anthropic_api_key`
- `minimax` → `minimax_api_key`
- `openai_compatible` → `openai_compatible_api_key`, `openai_compatible_base_url`, `openai_compatible_model`

## Frontend Implementation

### Components

**AICredentials Settings:** `frontend/src/components/UserSettings/AICredentials.tsx`
- Credentials list card with compact line items
- Each row: name, default star icon, type label, edit/delete buttons
- Add button opens dialog
- Set default via star icon button
- SDK Preferences card (unchanged from before)

**Add/Edit Dialog:** `frontend/src/components/UserSettings/AICredentialDialog.tsx`
- Name input
- Type selector (disabled when editing)
- API Key input (password field)
- OpenAI Compatible: Base URL and Model inputs
- "Set as default" checkbox

### API Client

**Service:** `frontend/src/client/sdk.gen.ts` - `AiCredentialsService`
- `listAiCredentials()` - GET list
- `createAiCredential(data)` - POST create
- `getAiCredential(data)` - GET single
- `updateAiCredential(data)` - PATCH update
- `deleteAiCredential(data)` - DELETE
- `setAiCredentialDefault(data)` - POST set default

**Types:** `frontend/src/client/types.gen.ts`
- `AICredentialPublic`, `AICredentialCreate`, `AICredentialUpdate`
- `AICredentialsPublic`
- `AICredentialType`

### State Management

**Query Keys:**
- `["aiCredentialsList"]` - List of named credentials
- `["aiCredentialsStatus"]` - User's credential status (has_* flags)

**Mutations:**
- Create/Update/Delete invalidate both query keys
- Set default also invalidates `["aiCredentialsStatus"]`

## Security Features

**Encryption:**
- All API keys encrypted at rest using Fernet (PBKDF2-HMAC-SHA256)
- Decryption only when needed (set default sync, credential usage)
- Never expose raw keys in API responses (`has_api_key: true` instead)

**Access Control:**
- All routes require authentication (`CurrentUser` dependency)
- Ownership validation on all operations
- CASCADE delete when user deleted

**Validation:**
- API key required for all types
- Base URL and Model required for `openai_compatible` type
- Name required, max 255 characters

## Backward Compatibility

**User Profile Auto-Sync:**
- When credential set as default → values copied to `user.ai_credentials_encrypted`
- Existing code reading from user profile continues to work
- Environment creation unchanged

**Existing UI:**
- Old flat credential inputs removed from UI
- All credential management now via named credentials
- SDK Preferences card unchanged

## File Locations Reference

### Backend

**Models:**
- `backend/app/models/ai_credential.py` - AICredential model and schemas
- `backend/app/models/__init__.py` - Exports added

**Routes:**
- `backend/app/api/routes/ai_credentials.py` - CRUD endpoints
- `backend/app/api/main.py` - Router registration

**Services:**
- `backend/app/services/ai_credentials_service.py` - Core logic

**Migration:**
- `backend/app/alembic/versions/h8c9d0e1f2g3_add_ai_credentials_table.py`

**Encryption:**
- `backend/app/core/security.py` - `encrypt_field()`, `decrypt_field()`

**Related:**
- `backend/app/crud.py` - `update_user_ai_credentials()` for sync
- `backend/app/models/user.py` - `ai_credentials_encrypted` field

### Frontend

**Components:**
- `frontend/src/components/UserSettings/AICredentials.tsx` - Main settings UI
- `frontend/src/components/UserSettings/AICredentialDialog.tsx` - Add/Edit dialog

**Client (auto-generated):**
- `frontend/src/client/sdk.gen.ts` - `AiCredentialsService`
- `frontend/src/client/types.gen.ts` - TypeScript types

## Phase 2 Implementation Details

### Environment Credential Linking

**Purpose:** Allow environments to use specific credentials instead of defaults

**Schema Changes:** `backend/app/models/environment.py`
- `use_default_ai_credentials` (BOOLEAN, default true) - Use user's default credentials
- `conversation_ai_credential_id` (UUID, nullable) - Explicit link for conversation SDK
- `building_ai_credential_id` (UUID, nullable) - Explicit link for building SDK

**Migration:** `backend/app/alembic/versions/k1f2g3h4i5j6_add_env_ai_credentials.py`

**Service Updates:** `backend/app/services/environment_service.py`
- `SDK_TO_CREDENTIAL_TYPE` mapping at line 24 - Maps SDK IDs to AI credential types
- `create_environment()` - Resolves defaults from AICredential table or validates linked credentials

**Frontend Updates:** `frontend/src/components/Environments/AddEnvironment.tsx`
- "Use Default AI Credentials" switch (default: ON)
- When OFF: Credential dropdowns for conversation/building modes filtered by SDK type
- Validation: Check defaults exist or explicit credentials selected

### Agent Share Credential Provision

**Purpose:** Owner can attach AI credentials to share so recipient doesn't need their own

**Schema Changes:** `backend/app/models/agent_share.py`
- `provide_ai_credentials` (BOOLEAN, default false) - Owner provides credentials
- `conversation_ai_credential_id` (UUID, nullable) - Owner's credential for conversation SDK
- `building_ai_credential_id` (UUID, nullable) - Owner's credential for building SDK
- `AICredentialRequirement` schema - SDK type and purpose for wizard

**New Model:** `backend/app/models/ai_credential_share.py`
- `AICredentialShare` table - Junction for sharing credentials with recipients
- `AICredentialSharePublic`, `AICredentialShareCreate` schemas

**Migration:** `backend/app/alembic/versions/j0e1f2g3h4i5_add_share_ai_credentials.py`

**Service Updates:** `backend/app/services/agent_share_service.py`
- `share_agent()` - Accepts `provide_ai_credentials`, `conversation_ai_credential_id`, `building_ai_credential_id`
- Validates credentials exist and belong to owner

**Frontend Updates:** `frontend/src/components/Agents/AgentSharingTab.tsx`
- "Provide AI Credentials" switch in share dialog
- When ON: Credential dropdowns based on agent's active environment SDKs
- Info text explaining credential sharing implications

### Accept Share Wizard AI Credentials Step

**Purpose:** Handle AI credential selection/display when accepting a share

**New Component:** `frontend/src/components/Agents/AcceptShareWizard/WizardStepAICredentials.tsx`

**If owner provided credentials:**
- Green section: "AI Credentials Provided"
- Shows credential names with "Provided by owner" badge
- No action needed from recipient

**If owner did NOT provide credentials:**
- Shows required SDKs based on agent's environment
- Checks if recipient has matching default credentials
- If defaults exist: Shows "Using default: [Name]" with green badge
- If no defaults: Dropdown to select from existing credentials
- If no credentials: Error message with link to Settings

**Validation:**
- Continue button disabled until all required SDK types have credentials
- Both explicit selection and default fallback are valid

**Wizard Flow Update:** `frontend/src/components/Agents/AcceptShareWizard/AcceptShareWizard.tsx`
- Step type: `"overview" | "ai_credentials" | "credentials" | "confirm"`
- State: `aiCredentialSelections` with `conversationCredentialId`, `buildingCredentialId`
- AI Credentials step shown when `!share.ai_credentials_provided && required_ai_credential_types.length > 0`
- Accept mutation includes `ai_credential_selections` in request body

### Clone Credential Setup

**Service Updates:** `backend/app/services/agent_clone_service.py`

**`create_clone()` changes:**
1. Gets original agent's active environment SDK settings
2. Creates clone environment with same SDK settings
3. If share has `provide_ai_credentials=true`:
   - Creates `AICredentialShare` links for recipient via `AICredentialsService.share_credential()`
   - Links clone environment to shared credentials
4. If share has `provide_ai_credentials=false`:
   - Uses recipient's credentials selected in wizard (from `ai_credential_selections`)
   - Falls back to recipient's default credentials if not explicitly selected

### AI Credential Sharing Table

**New Table:** `ai_credential_shares`

**Migration:** `backend/app/alembic/versions/i9d0e1f2g3h4_add_ai_credential_shares.py`

| Column | Type | Description |
|--------|------|-------------|
| `id` | UUID | Primary key |
| `ai_credential_id` | UUID | FK to ai_credential.id (CASCADE) |
| `shared_with_user_id` | UUID | FK to user.id (CASCADE) |
| `shared_by_user_id` | UUID | FK to user.id |
| `shared_at` | DATETIME | When share was created |

**Indexes:**
- `ix_ai_credential_shares_credential` - (ai_credential_id)
- `ix_ai_credential_shares_recipient` - (shared_with_user_id)

**Service Methods:** `backend/app/services/ai_credentials_service.py`
- `share_credential(session, credential_id, owner_id, recipient_id)` - Creates share link
- `can_access_credential(session, credential_id, user_id)` - Checks ownership or share access
- `get_credential_for_use(session, credential_id, user_id)` - Returns decrypted data if accessible
- `revoke_share(session, credential_id, recipient_id)` - Removes share link
- `list_shared_with_me(session, user_id)` - Lists credentials shared with user

## Implementation Scenarios

### Scenario 1: User creates environment with default credentials

```
1. User selects SDKs for conversation/building
2. use_default_ai_credentials = true (default)
3. Backend resolves user's default credentials for each SDK type
4. Environment created, credentials injected into container
5. If no default exists → error returned
```

### Scenario 2: User creates environment with specific credentials

```
1. User selects SDKs for conversation/building
2. User toggles use_default_ai_credentials = false
3. User selects specific credentials from dropdowns
4. Backend validates credentials match SDK requirements
5. Environment created with explicit credential links
6. Credentials injected into container
```

### Scenario 3: Owner shares agent WITH AI credentials

```
1. Owner creates share, toggles "Provide AI Credentials" ON
2. Owner selects credentials for conversation (and building if builder mode)
3. Share record created with credential IDs
4. Recipient accepts share in wizard
5. Wizard shows "Credentials provided by owner" - no action needed
6. Clone created, owner's credentials linked via AICredentialShare
7. Clone environment uses shared credentials
```

### Scenario 4: Owner shares agent WITHOUT AI credentials

```
1. Owner creates share, leaves "Provide AI Credentials" OFF
2. Share record created without credential IDs
3. Recipient accepts share in wizard
4. Wizard shows AI Credentials step:
   a. If recipient has matching defaults → auto-selected
   b. If recipient lacks credentials → must add or blocked
5. Clone created with recipient's own credentials
6. Clone environment uses recipient's credentials
```

### Scenario 5: Owner pushes update with SDK change

```
1. Owner changes active environment to one with different SDKs
2. Owner pushes update to clones
3. Clone receives SDK setting update
4. Clone's credential links NOT changed (clone owner's responsibility)
5. If SDK change makes clone's credentials incompatible:
   - Clone environment shows warning
   - Clone owner must update credential links manually
```

## Related Documentation

- `docs/business-domain/shared_agents_management.md` - Agent sharing feature
- `docs/agent-sessions/agent_env_docker.md` - Environment architecture
- `docs/security_credentials_whitelist.md` - Credential encryption pattern

## Phase 2 File Locations Reference

### Backend - Models

- `backend/app/models/ai_credential_share.py` - AICredentialShare model and schemas
- `backend/app/models/agent_share.py` - Updated with AI credential provision fields
- `backend/app/models/environment.py` - Updated with AI credential linking fields

### Backend - Migrations

- `backend/app/alembic/versions/i9d0e1f2g3h4_add_ai_credential_shares.py` - AI credential shares table
- `backend/app/alembic/versions/j0e1f2g3h4i5_add_share_ai_credentials.py` - Agent share AI credential fields
- `backend/app/alembic/versions/k1f2g3h4i5j6_add_env_ai_credentials.py` - Environment AI credential fields

### Backend - Services

- `backend/app/services/ai_credentials_service.py` - Sharing methods added
- `backend/app/services/environment_service.py` - SDK to credential type mapping
- `backend/app/services/agent_share_service.py` - AI credential provision handling
- `backend/app/services/agent_clone_service.py` - Clone AI credential setup

### Backend - Routes

- `backend/app/api/routes/agent_shares.py` - Updated endpoints for AI credentials

### Frontend - Components

- `frontend/src/components/Environments/AddEnvironment.tsx` - Credential selection UI
- `frontend/src/components/Agents/AgentSharingTab.tsx` - Share dialog AI credentials
- `frontend/src/components/Agents/AcceptShareWizard/WizardStepAICredentials.tsx` - New wizard step
- `frontend/src/components/Agents/AcceptShareWizard/AcceptShareWizard.tsx` - Updated wizard flow

---

**Document Version:** 3.0
**Last Updated:** 2026-01-17
**Status:** Implemented
