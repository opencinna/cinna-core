# Google Service Account Credential

## Overview

The `google_service_account` credential type allows users to upload or paste a Google Service Account JSON key file. The JSON is validated, encrypted, and stored. When synced to agent environments, the full JSON is written as a **standalone file** (`credentials/{credential_id}.json`), and `credentials.json` contains only a `file_path` reference with metadata — the private key never appears in `credentials.json`.

This mirrors how Google service accounts are typically used: scripts load the key file by path via `Credentials.from_service_account_file()`.

## Architecture

### Data Flow

```
User uploads/pastes JSON → Frontend validates → Backend validates → Encrypt & store
                                                                        ↓
                                                         Sync to agent environment
                                                                        ↓
                                              ┌─────────────────────────┴──────────────────────────┐
                                              │                                                     │
                                    credentials.json                                  {credential_id}.json
                              (entry with file_path ref)                         (actual SA JSON content)
```

### Integration Points

- **Credential Model** (`backend/app/models/credential.py`) — `GOOGLE_SERVICE_ACCOUNT` enum value + `GoogleServiceAccountData` validation model
- **CredentialsService** (`backend/app/services/credentials_service.py`) — Validation, processing, environment preparation
- **Credential Routes** (`backend/app/api/routes/credentials.py`) — Server-side validation on create/update
- **AgentEnvService** (`backend/app/env-templates/.../agent_env_service.py`) — Writes standalone SA JSON files, cleans up orphans
- **Frontend** — `ServiceAccountFields` component with JSON textarea, file upload, and client-side validation

### What's Reused (No Changes Needed)

- Encryption/decryption (`encrypt_field()`/`decrypt_field()` in `backend/app/core/security.py`)
- Credential CRUD operations (`backend/app/crud.py`)
- Auto-sync event handlers (`event_credential_updated()`, `event_credential_deleted()`, etc.)
- Docker adapter's `set_credentials()` HTTP proxy — passes new `service_account_files` key automatically
- Credential sharing and agent linking infrastructure
- Credential completeness check pattern

## Components

### Backend

**`CredentialsService`** (`backend/app/services/credentials_service.py`)
- `validate_service_account_json()` — Validates JSON structure: `type` must be `"service_account"`, required fields: `project_id`, `private_key_id`, `private_key`, `client_email`
- `_process_service_account_credential()` — Converts full SA JSON into `{file_path, project_id, client_email}` reference for `credentials.json`
- `prepare_credentials_for_environment()` — Collects SA files into `service_account_files` list, replaces `credential_data` with processed reference before filtering
- `generate_credentials_readme()` — Includes SA usage example with `from_service_account_file()` pattern

**Service Configuration**:
```python
SENSITIVE_FIELDS = {
    "google_service_account": ["private_key", "private_key_id"],
}

AGENT_ENV_ALLOWED_FIELDS = {
    "google_service_account": ["file_path", "project_id", "client_email"],
}

REQUIRED_FIELDS = {
    "google_service_account": ["type", "project_id", "private_key", "client_email"],
}
```

**Credential Routes** (`backend/app/api/routes/credentials.py`)
- `POST /credentials/` — Validates SA JSON before creation; returns 422 on invalid
- `PUT /credentials/{id}` — Validates SA JSON before update; returns 422 on invalid

**AgentEnvService** (`backend/app/env-templates/.../agent_env_service.py`)
- `update_credentials()` accepts optional `service_account_files: list[dict]`
- Writes each SA file as `credentials/{credential_id}.json`
- Cleans up orphaned `.json` files (credential deleted/unlinked) by reconciling against current SA list

**Agent-Env Models** (`backend/app/env-templates/.../models.py`)
- `CredentialsUpdate` includes `service_account_files: list[dict] | None = None`

**Agent-Env Routes** (`backend/app/env-templates/.../routes.py`)
- `POST /config/credentials` passes `service_account_files` through to service

### Frontend

**`ServiceAccountFields`** (`frontend/src/components/Credentials/CredentialFields/ServiceAccountFields.tsx`)
- Two-column layout: Name + Notes on left, JSON textarea + file upload on right
- Monospace textarea (280px height) for pasting or viewing JSON
- "Upload JSON" button triggers file picker (`.json` files only)
- Client-side validation on change/blur: checks JSON syntax, `type === "service_account"`, required fields
- Shows green validation summary (project_id, client_email) when valid
- Shows amber security warning about private key storage

**`ServiceAccountCredentialForm`** (`frontend/src/components/Credentials/CredentialForms/ServiceAccountCredentialForm.tsx`)
- Wrapper that passes `form.control` and `form.watch` to `ServiceAccountFields`

**Type Maps Updated** (6 files):
- `AddCredential.tsx` — z.enum + dropdown `<SelectItem>`
- `EditCredential.tsx` — Rendering branch
- `CredentialCard.tsx` — `FileJson` icon + "Google Service Account" label
- `columns.tsx` — Label map entry
- `AgentCredentialsTab.tsx` — Label function case
- `credential/$credentialId.tsx` — Label function + rendering branch

## Agent Environment File Structure

After syncing a service account credential:

```
workspace/
└── credentials/
    ├── credentials.json                                    # References all credentials
    ├── README.md                                          # Redacted documentation
    └── a1b2c3d4-e5f6-7890-abcd-ef1234567890.json        # SA key file (named by credential ID)
```

### credentials.json Entry

The entry contains only a file path reference and identifying metadata — no private key:

```json
{
  "id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "name": "My GCP Service Account",
  "type": "google_service_account",
  "notes": "For BigQuery access",
  "credential_data": {
    "file_path": "credentials/a1b2c3d4-e5f6-7890-abcd-ef1234567890.json",
    "project_id": "my-gcp-project",
    "client_email": "my-sa@my-gcp-project.iam.gserviceaccount.com"
  }
}
```

### Standalone SA Key File

`credentials/a1b2c3d4-e5f6-7890-abcd-ef1234567890.json` contains the full Google-issued JSON:

```json
{
  "type": "service_account",
  "project_id": "my-gcp-project",
  "private_key_id": "key123",
  "private_key": "-----BEGIN RSA PRIVATE KEY-----\n...\n-----END RSA PRIVATE KEY-----\n",
  "client_email": "my-sa@my-gcp-project.iam.gserviceaccount.com",
  "client_id": "123456789",
  "auth_uri": "https://accounts.google.com/o/oauth2/auth",
  "token_uri": "https://oauth2.googleapis.com/token"
}
```

### prepare_credentials_for_environment() Return Structure

```python
{
    "credentials_json": [...],           # All credentials (SA entries have file_path reference)
    "credentials_readme": "...",         # Redacted README
    "service_account_files": [           # SA JSON files to write as standalone files
        {
            "credential_id": "uuid-string",
            "json_content": { ... }      # Full SA JSON
        }
    ]
}
```

## Security

### Encryption at Rest
- Full SA JSON (including `private_key`) encrypted via `encrypt_field()` (Fernet symmetric encryption)
- Decrypted only when syncing to agent environments or when owner views the credential

### Agent Environment Exposure
- **credentials.json**: Only `file_path`, `project_id`, `client_email` — private key excluded
- **Standalone JSON file**: Full SA JSON accessible only within the agent container filesystem
- **README.md**: Shows `private_key: "***REDACTED***"` and `private_key_id: "***REDACTED***"`

### Input Validation
- Backend validates on create/update: `type === "service_account"`, required fields present (422 on failure)
- Frontend validates on change/blur for immediate feedback

### Access Control
- Standard credential ownership model (only owner can view/edit/delete)
- Sharing via existing `CredentialShare` mechanism
- Agent linking via existing `AgentCredentialLink` mechanism

## Automatic Synchronization

Sync behavior follows the standard credential sync triggers (see [agent_env_credentials_management.md](../agent-sessions/agent_env_credentials_management.md)):

| Trigger | SA-Specific Behavior |
|---------|---------------------|
| Credential created/updated | Standalone `.json` file written/overwritten |
| Credential deleted | Standalone `.json` file removed by cleanup logic |
| Credential unlinked from agent | Standalone `.json` file removed by cleanup logic |
| Multiple SA credentials on same agent | Each gets its own `{id}.json` file |
| Empty credential_data | Marked "incomplete"; no standalone file written |

### Orphan Cleanup

`update_credentials()` reconciles on every sync:
1. Lists all `*.json` files in `credentials/` (excluding `credentials.json`)
2. Deletes any that don't correspond to a current service account credential
3. This handles deletion and unlinking without requiring separate cleanup calls

## Agent Usage

Scripts discover the SA file via `credentials.json` and load it directly:

```python
import json
from google.oauth2 import service_account
from googleapiclient.discovery import build

# Load credentials reference
with open('credentials/credentials.json', 'r') as f:
    all_credentials = json.load(f)

# Find by credential ID (recommended)
credential_id = 'a1b2c3d4-e5f6-7890-abcd-ef1234567890'
for cred in all_credentials:
    if cred['id'] == credential_id:
        sa_file_path = cred['credential_data']['file_path']

        # Load service account credentials
        creds = service_account.Credentials.from_service_account_file(sa_file_path)

        # Use with Google Sheets
        sheets = build('sheets', 'v4', credentials=creds)

        # Use with BigQuery
        from google.cloud import bigquery
        bq = bigquery.Client(credentials=creds, project=cred['credential_data']['project_id'])

        break
```

## Error Handling

### Validation Errors

| Scenario | HTTP Status | Where |
|----------|-------------|-------|
| Invalid JSON syntax | 422 | Backend route, Frontend form |
| `type` not `"service_account"` | 422 | Backend route, Frontend form |
| Missing `project_id`, `private_key`, or `client_email` | 422 | Backend route, Frontend form |

### File System Edge Cases

| Scenario | Behavior |
|----------|----------|
| `credentials/` dir doesn't exist | Created by `update_credentials()` |
| File write permission error | `IOError` raised, logged, sync continues for other environments |
| Orphaned `.json` files from previous syncs | Cleaned up by reconciliation logic |
