# Google Service Account Credentials - Technical Details

## File Locations

### Backend - Models
- `backend/app/models/credential.py` - `GOOGLE_SERVICE_ACCOUNT` enum value in `CredentialType`
- `backend/app/models/credential.py` - `GoogleServiceAccountData` Pydantic validation model

### Backend - Services
- `backend/app/services/credentials_service.py` - Validation, processing, environment preparation for SA credentials
- `backend/app/services/credentials_service.py` - `SENSITIVE_FIELDS["google_service_account"]`: `["private_key", "private_key_id"]`
- `backend/app/services/credentials_service.py` - `AGENT_ENV_ALLOWED_FIELDS["google_service_account"]`: `["file_path", "project_id", "client_email"]`
- `backend/app/services/credentials_service.py` - `REQUIRED_FIELDS["google_service_account"]`: `["type", "project_id", "private_key", "client_email"]`

### Backend - Routes
- `backend/app/api/routes/credentials.py` - Server-side SA JSON validation on `POST /credentials/` and `PUT /credentials/{id}`

### Agent Environment (Inside Container)
- `backend/app/env-templates/app_core_base/core/server/agent_env_service.py` - Writes standalone SA JSON files, handles orphan cleanup
- `backend/app/env-templates/app_core_base/core/server/models.py` - `CredentialsUpdate.service_account_files: list[dict] | None`
- `backend/app/env-templates/app_core_base/core/server/routes.py` - `POST /config/credentials` passes `service_account_files` to service

### Frontend
- `frontend/src/components/Credentials/CredentialFields/ServiceAccountFields.tsx` - SA JSON textarea + file upload + validation UI
- `frontend/src/components/Credentials/CredentialForms/ServiceAccountCredentialForm.tsx` - Wrapper passing form control to `ServiceAccountFields`

### Frontend - Type Map Updates (6 files)
- `frontend/src/components/Credentials/AddCredential.tsx` - z.enum + dropdown `<SelectItem>`
- `frontend/src/components/Credentials/EditCredential.tsx` - Rendering branch
- `frontend/src/components/Credentials/CredentialCard.tsx` - `FileJson` icon + "Google Service Account" label
- `frontend/src/components/Credentials/columns.tsx` - Label map entry
- `frontend/src/components/Agents/AgentCredentialsTab.tsx` - Label function case
- `frontend/src/routes/_layout/credential/$credentialId.tsx` - Label function + rendering branch

## Services & Key Methods

### CredentialsService (`backend/app/services/credentials_service.py`)

- `validate_service_account_json()` - Validates JSON structure: `type` must be `"service_account"`, checks required fields (`project_id`, `private_key_id`, `private_key`, `client_email`)
- `_process_service_account_credential()` - Converts full SA JSON into `{file_path, project_id, client_email}` reference for `credentials.json`; private key never appears in the reference
- `prepare_credentials_for_environment()` - Collects SA files into `service_account_files` list, replaces `credential_data` with processed file-path reference before field filtering
- `generate_credentials_readme()` - Includes SA usage example with `from_service_account_file()` pattern

### AgentEnvService (`backend/app/env-templates/app_core_base/core/server/agent_env_service.py`)

- `update_credentials()` - Accepts optional `service_account_files: list[dict]` parameter; writes each SA file as `credentials/{credential_id}.json`; reconciles orphaned `.json` files by comparing against current SA list

## API Endpoints

### Credential CRUD (SA-specific validation)
- `backend/app/api/routes/credentials.py`
  - `POST /api/v1/credentials/` - Validates SA JSON via `validate_service_account_json()` before creation; returns 422 on invalid
  - `PUT /api/v1/credentials/{id}` - Validates SA JSON via `validate_service_account_json()` before update; returns 422 on invalid

### Agent Environment Internal API
- `backend/app/env-templates/app_core_base/core/server/routes.py`
  - `POST /config/credentials` - Passes `service_account_files` through to `AgentEnvService.update_credentials()`

## Data Structures

### prepare_credentials_for_environment() Return

Returns a dict with three keys:
- `credentials_json` - List of all credentials; SA entries have `credential_data` replaced with `{file_path, project_id, client_email}` reference
- `credentials_readme` - Redacted README content; SA private key shown as `***REDACTED***`
- `service_account_files` - List of dicts with `credential_id` (str) and `json_content` (dict containing full SA JSON); used by agent-env to write standalone files

### Reused Infrastructure (No SA-specific changes)

- Encryption/decryption: `backend/app/core/security.py` - `encrypt_field()` / `decrypt_field()`
- Credential CRUD operations (standard SQLModel patterns)
- Auto-sync event handlers: `event_credential_updated()`, `event_credential_deleted()`, etc.
- Docker adapter's `set_credentials()` HTTP proxy - passes `service_account_files` key automatically
- Credential sharing and agent linking infrastructure
- Credential completeness check pattern

## Frontend Component Details

### ServiceAccountFields (`frontend/src/components/Credentials/CredentialFields/ServiceAccountFields.tsx`)

- Two-column layout: Name + Notes on left, JSON textarea + file upload on right
- Monospace textarea (280px height) for pasting or viewing JSON
- "Upload JSON" button triggers file picker (`.json` files only)
- Client-side validation on change/blur: checks JSON syntax, `type === "service_account"`, required fields
- Shows green validation summary (`project_id`, `client_email`) when valid
- Shows amber security warning about private key storage
