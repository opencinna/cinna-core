# Agent Credentials - Technical Details

## File Locations

### Backend - Models
- `backend/app/models/credential.py` - Credential model with encrypted_data, credential types enum, agent link model
- `backend/app/models/link_models.py` - AgentCredentialLink many-to-many junction table

### Backend - Services
- `backend/app/services/credentials_service.py` - Core credential preparation, syncing, redaction, whitelisting
- `backend/app/services/oauth_credentials_service.py` - OAuth token refresh and flow management
- `backend/app/services/environment_lifecycle.py` - Credential sync during environment lifecycle events

### Backend - Routes
- `backend/app/api/routes/credentials.py` - Credential CRUD with auto-sync triggers
- `backend/app/api/routes/oauth_credentials.py` - OAuth flow endpoints (authorize, callback, metadata, refresh)
- `backend/app/api/routes/agents.py` - Agent credential link/unlink with auto-sync

### Agent Environment (Inside Container)
- `backend/app/env-templates/app_core_base/core/server/agent_env_service.py` - Writes credential files to workspace
- `backend/app/env-templates/app_core_base/core/server/routes.py` - POST /config/credentials endpoint
- `backend/app/env-templates/app_core_base/core/server/prompt_generator.py` - Loads credentials README for prompt

### Frontend
- `frontend/src/components/Agents/AgentCredentialsTab.tsx` - Link/unlink credentials to agents
- `frontend/src/components/Credentials/` - Full credential management UI (create, edit, delete, share)
- `frontend/src/components/Credentials/CredentialForms/ApiTokenCredentialForm.tsx` - API token template form
- `frontend/src/components/Credentials/CredentialForms/OAuthCredentialForm.tsx` - OAuth flow handler
- `frontend/src/components/Credentials/CredentialFields/OAuthCredentialFields.tsx` - Unified OAuth UI for all 6 OAuth types (grant button, metadata display)
- `frontend/src/routes/credentials/oauth/callback.tsx` - OAuth callback handler route

### Tests
- `backend/tests/api/credentials/test_credentials.py` - Credential CRUD tests
- `backend/tests/utils/credential.py` - Test utilities for credential creation

### Migrations
- `backend/app/alembic/versions/dff3725fe567_*.py` - Initial credentials table with encryption
- `backend/app/alembic/versions/8deb1f26c518_*.py` - Agent-credential many-to-many link
- `backend/app/alembic/versions/774f47bf7fdd_*.py` - API token credential type
- `backend/app/alembic/versions/a52c4af4a9e5_*.py` - Additional OAuth credential types
- `backend/app/alembic/versions/e1f2g3h4i5j6_*.py` - Email SMTP credential type

## Database Schema

### Credential Table
- `id` (UUID, PK) - Unique credential identifier
- `name` (str) - User-defined credential name
- `credential_type` (enum) - Type of credential (email_imap, email_smtp, odoo, gmail_oauth, api_token, etc.)
- `encrypted_data` (str) - Fernet-encrypted JSON blob with credential fields
- `owner_id` (UUID, FK → User) - Credential owner
- `user_workspace_id` (UUID, FK → UserWorkspace, nullable) - Optional workspace assignment
- `allow_sharing` (bool) - Whether credential can be shared with other users
- `is_placeholder` (bool) - Placeholder for cloned agent credential requirements

### AgentCredentialLink Table
- `agent_id` (UUID, FK → Agent) - Linked agent
- `credential_id` (UUID, FK → Credential) - Linked credential
- Composite primary key on (agent_id, credential_id)

## API Endpoints

### Credential CRUD (triggers environment sync)
- `backend/app/api/routes/credentials.py`
  - `POST /api/v1/credentials/` - Create credential (encrypts data)
  - `PATCH /api/v1/credentials/{id}` - Update credential (triggers sync to all linked running environments)
  - `DELETE /api/v1/credentials/{id}` - Delete credential (triggers sync to remove from environments)

### Agent-Credential Linking (triggers environment sync)
- `backend/app/api/routes/agents.py`
  - `POST /api/v1/agents/{agent_id}/credentials/{credential_id}` - Link credential to agent
  - `DELETE /api/v1/agents/{agent_id}/credentials/{credential_id}` - Unlink credential from agent

### OAuth Flow Endpoints
- `backend/app/api/routes/oauth_credentials.py`
  - `POST /api/v1/credentials/{credential_id}/oauth/authorize` - Initiate OAuth flow, returns authorization URL
  - `POST /api/v1/credentials/oauth/callback` - Handle OAuth callback, exchange code for tokens
  - `GET /api/v1/credentials/{credential_id}/oauth/metadata` - Get OAuth metadata (email, scopes, expiration)
  - `POST /api/v1/credentials/{credential_id}/oauth/refresh` - Manually trigger token refresh

### Agent Environment Internal API
- `backend/app/env-templates/app_core_base/core/server/routes.py`
  - `POST /config/credentials` - Receives credential data from backend, writes to workspace

## Services & Key Methods

### CredentialsService (`backend/app/services/credentials_service.py`)
- `prepare_credentials_for_environment()` - Decrypts credentials, applies field whitelisting, returns JSON and README data
- `generate_credentials_readme()` - Creates redacted README with ID-based lookup examples
- `redact_credential_data()` - Replaces sensitive field values with `***REDACTED***` (only for non-empty values)
- `_process_api_token_credential()` - Converts API token input (type + template + token) to ready-to-use HTTP headers
- `sync_credentials_to_agent_environments()` - Syncs credential files to all running environments of an agent
- `refresh_expiring_credentials_for_agent()` - Checks OAuth tokens linked to agent, refreshes those expiring within threshold
- `event_credential_updated()` - Event handler: syncs updated credential to all linked agents' running environments
- `event_credential_deleted()` - Event handler: syncs removal to all linked agents' running environments
- `event_credential_shared()` / `event_credential_unshared()` - Event handlers for credential link changes
- `AGENT_ENV_ALLOWED_FIELDS` - Dict mapping credential types to their whitelisted field names

### OAuthCredentialsService (`backend/app/services/oauth_credentials_service.py`)
- `initiate_oauth_flow()` - Generates state token, builds Google authorization URL with type-specific scopes
- `handle_oauth_callback()` - Validates state token, exchanges code for tokens, stores encrypted in credential
- `refresh_oauth_token()` - Refreshes OAuth access token using stored refresh token
- `get_oauth_scopes_for_type()` - Maps credential type to required Google OAuth scopes
- `get_oauth_metadata()` - Extracts non-sensitive metadata (email, scopes, expiration) for display

### EnvironmentLifecycleManager (`backend/app/services/environment_lifecycle.py`)
- `_sync_agent_data()` - Called after start/restart/rebuild, syncs prompts and credentials to container

### AgentEnvService (`backend/app/env-templates/app_core_base/core/server/agent_env_service.py`)
- `update_credentials()` - Writes `credentials.json` and `README.md` to workspace/credentials/
- `get_credentials_readme()` - Loads README content for inclusion in agent prompt

### PromptGenerator (`backend/app/env-templates/app_core_base/core/server/prompt_generator.py`)
- `_load_credentials_readme()` - Reads credentials README from workspace
- `generate_building_mode_prompt()` - Includes credentials documentation in system prompt with security warnings

## Configuration

- `CREDENTIAL_REFRESH_THRESHOLD_SECONDS = 600` - OAuth refresh threshold (10 minutes before expiry)
- Encryption uses Fernet symmetric encryption with PBKDF2-HMAC-SHA256 key derivation
- Encryption key derived from `SECRET_KEY` environment variable (`backend/app/core/security.py`)
- `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` - Shared with user OAuth login, used for credential OAuth flows
- OAuth state tokens stored in-memory with 10-minute expiration
- Google Cloud Console requires redirect URI for credential OAuth callback (separate from user auth callback)

## Security

### Encryption at Rest
- All credential data encrypted via `encrypt_field()` / `decrypt_field()` in `backend/app/core/security.py`
- Stored as single encrypted blob in `Credential.encrypted_data` field
- Decryption only happens when preparing data for environment sync

### Field Whitelisting
- `CredentialsService.AGENT_ENV_ALLOWED_FIELDS` defines per-type allowed fields
- Only whitelisted fields pass from backend to agent container
- OAuth `refresh_token` and `client_secret` excluded from all types
- Unknown credential types produce empty dict (fail-safe)

### Prompt Redaction
- Sensitive fields redacted with `***REDACTED***` in README (prompt-visible data)
- Redaction applies only to non-empty values; empty fields shown as-is
- README structure identical to credentials.json structure (minus sensitive values)

### Access Control
- Only credential owner (or users with share access) can manage credentials
- Agent-credential linking restricted to agents owned by the current user
- Environment sync only targets running environments owned by the agent's owner
