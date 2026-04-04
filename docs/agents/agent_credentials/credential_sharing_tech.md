# Credential Sharing - Technical Details

## File Locations

### Backend - Models
- `backend/app/models/credentials/credential_share.py` - CredentialShare table model, CredentialSharePublic, CredentialShareCreate, SharedCredentialPublic response models
- `backend/app/models/credentials/credential.py` - Updated: added `allow_sharing` field to CredentialBase/CredentialUpdate, added `share_count`, `is_shared`, `owner_email` to CredentialPublic

### Backend - Services
- `backend/app/services/credentials/credential_share_service.py` - Core sharing logic: share, revoke, toggle, access checks
- `backend/app/services/credentials/credentials_service.py` - Updated: `link_credential_to_agent()` allows linking shared credentials (not just owned)

### Backend - Routes
- `backend/app/api/routes/credential_shares.py` - Sharing CRUD endpoints (share, list, revoke, toggle, shared-with-me)
- `backend/app/api/routes/credentials.py` - Updated: credential detail allows viewing if user owns OR has share

### Frontend - Components
- `frontend/src/components/Credentials/CredentialSharing.tsx` - Sharing toggle, share dialog, share list with revoke buttons
- `frontend/src/components/Credentials/SharedWithMeCredentials.tsx` - "Shared with Me" credential list with owner email and share date
- `frontend/src/components/Credentials/CredentialCard.tsx` - Updated: "Shareable" badge, user count badge for active shares

### Frontend - Routes
- `frontend/src/routes/_layout/credentials.tsx` - Updated: split into "My Credentials" and "Shared with Me" sections
- `frontend/src/routes/_layout/credential/$credentialId.tsx` - Updated: SharedCredentialView (read-only) vs OwnedCredentialView (full edit)
- `frontend/src/components/Agents/AgentCredentialsTab.tsx` - Updated: fetches both owned and shared credentials, shows "Shared" indicator in dropdown

### Migrations
- `backend/app/alembic/versions/g7b8c9d0e1f2_add_credential_sharing.py` - `allow_sharing` column on credential table, new `credential_shares` table

### Router Registration
- `backend/app/api/main.py` - Added `credential_shares.router` import and registration

## Database Schema

### Modified Table: `credential`
- Added: `allow_sharing` (boolean, default false)
- Added index: `ix_credential_allow_sharing` (partial index for shareable credentials)

### New Table: `credential_shares`
- `id` (UUID, PK)
- `credential_id` (UUID, FK → credential, CASCADE delete)
- `shared_with_user_id` (UUID, FK → user, CASCADE delete)
- `shared_by_user_id` (UUID, FK → user)
- `shared_at` (datetime)
- `access_level` (varchar, default 'read')
- Unique constraint: `(credential_id, shared_with_user_id)`

## API Endpoints

### Credential Sharing (`backend/app/api/routes/credential_shares.py`)
- `POST /api/v1/credentials/{credential_id}/shares` - Share credential with user by email
- `GET /api/v1/credentials/{credential_id}/shares` - List all shares for a credential
- `DELETE /api/v1/credentials/{credential_id}/shares/{share_id}` - Revoke specific share
- `GET /api/v1/credentials/shared-with-me` - Get credentials shared with current user
- `PATCH /api/v1/credentials/{credential_id}/sharing` - Enable/disable sharing

### Updated Credential Endpoints (`backend/app/api/routes/credentials.py`)
- `GET /api/v1/credentials/{id}` - Now allows viewing if user owns OR has share (returns `is_shared=true` for shared)
- All endpoints now return `share_count`, `is_shared`, `owner_email` in CredentialPublic response via `_credential_to_public()` helper

## Services & Key Methods

### CredentialShareService (`backend/app/services/credentials/credential_share_service.py`)
- `share_credential()` - Create share with validations (ownership, allow_sharing, target exists, not self, not duplicate)
- `revoke_credential_share()` - Delete share record with ownership check
- `get_shares_by_credential()` - List shares with resolved user emails
- `get_credentials_shared_with_me()` - Query shares where user is recipient
- `get_share_count_for_credential()` - Count shares for a credential
- `update_credential_sharing()` - Toggle allow_sharing; auto-revokes all shares when disabled
- `can_user_access_credential()` - Check if user owns OR has share
- `delete_all_shares_for_credential()` - Bulk delete for credential deletion

### Updated CredentialsService (`backend/app/services/credentials/credentials_service.py`)
- `link_credential_to_agent()` - Updated to allow linking shared credentials (not just owned)

## Frontend Components

### CredentialSharing (`frontend/src/components/Credentials/CredentialSharing.tsx`)
- Toggle switch for enable/disable sharing
- "Share" button opens dialog with email input form
- List of current shares with revoke buttons
- Confirmation dialog when disabling sharing with active shares

### SharedWithMeCredentials (`frontend/src/components/Credentials/SharedWithMeCredentials.tsx`)
- Displays credentials shared with current user
- Shows owner email and share date
- Blue "Shared" badge to distinguish from owned credentials

### Credential Detail Route (`frontend/src/routes/_layout/credential/$credentialId.tsx`)
- Detects `is_shared` flag to switch between SharedCredentialView (read-only) and OwnedCredentialView (full edit)
- Header shows "Shared" badge and owner email for shared credentials
- Delete button hidden for shared credentials

### AgentCredentialsTab (`frontend/src/components/Agents/AgentCredentialsTab.tsx`)
- Fetches both owned credentials and credentials shared with user
- Shows "Shared" indicator in credential selection dropdown
- Displays "Shared" badge in table for linked shared credentials

## State Management

### Query Keys
- `["credentials"]` - User's owned credentials list
- `["credential", credentialId]` - Single credential detail
- `["credential-shares", credentialId]` - Shares for a credential
- `["credentials-shared-with-me"]` - Credentials shared with current user

### Mutations
- `shareCredential` - Create new share
- `revokeCredentialShare` - Delete share
- `updateCredentialSharing` - Toggle allow_sharing

## Security

### Validation Rules
- Owner-only operations: share, revoke, toggle allow_sharing
- Share requires `allow_sharing=true` on credential
- Cannot share with non-existent users, yourself, or create duplicates

### Access Control
- `CredentialShareService.can_user_access_credential()` - Returns true if owner OR has share
- `CredentialsService.link_credential_to_agent()` - Allows linking owned OR shared credentials to agents
- Share recipients get read-only access (can use, cannot see values)
- Credential values (encrypted_data) never exposed to share recipients
- Revoking share immediately removes access
- Disabling sharing is destructive (revokes all shares with warning)
