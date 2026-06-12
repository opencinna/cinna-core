# Google Service Account Credentials

## Purpose

Users upload a Google Service Account JSON key file as a credential. The JSON is validated, encrypted, and stored. When synced to agent environments, the private key is written as a **standalone file** (not embedded in `credentials.json`) — agents load it by path via `Credentials.from_service_account_file()`, matching standard Google SDK usage.

## Data Flow

```
User uploads/pastes SA JSON → Frontend validates → Backend validates → Encrypt & store
                                                                           │
                                                              Sync to agent environment
                                                                           │
                                              ┌────────────────────────────┴───────────────────────────┐
                                              │                                                        │
                                    credentials.json                                     {credential_id}.json
                              (entry with file_path ref)                            (actual SA JSON content)
```

- **credentials.json entry**: Contains only `file_path`, `project_id`, `client_email` — the private key is excluded
- **Standalone JSON file**: Full Google-issued SA JSON written to `credentials/{credential_id}.json`
- This separation keeps `credentials.json` safe for agent prompt context while providing full key access through the filesystem

## Environment File Structure

After syncing a service account credential, the agent environment contains:

```
workspace/
└── credentials/
    ├── credentials.json                                # References all credentials
    ├── README.md                                       # Redacted documentation
    └── {credential_id}.json                            # SA key file (named by credential UUID)
```

### credentials.json Entry

The entry contains a file path reference and identifying metadata — no private key:
- `id` - Credential UUID
- `name` - User-defined name
- `type` - `google_service_account`
- `credential_data.file_path` - Relative path to standalone SA JSON file (e.g., `credentials/{uuid}.json`)
- `credential_data.project_id` - GCP project identifier
- `credential_data.client_email` - Service account email address

### Standalone SA Key File

`credentials/{credential_id}.json` contains the full Google-issued service account JSON with all fields: `type`, `project_id`, `private_key_id`, `private_key`, `client_email`, `client_id`, `auth_uri`, `token_uri`, etc.

## Validation Rules

| Check | Where | HTTP Status |
|-------|-------|-------------|
| Valid JSON syntax | Backend route + Frontend form | 422 |
| `type` field equals `"service_account"` | Backend route + Frontend form | 422 |
| Required fields present: `project_id`, `private_key`, `client_email` | Backend route + Frontend form | 422 |

- Backend validates on both create and update operations
- Frontend validates on change/blur for immediate user feedback
- Valid JSON displays a green summary showing `project_id` and `client_email`

## Security

### Encryption at Rest
- Full SA JSON (including `private_key`) encrypted via Fernet symmetric encryption
- Decrypted only when syncing to agent environments or when the owner views the credential

### Agent Environment Exposure
- **credentials.json**: Only `file_path`, `project_id`, `client_email` — private key excluded via field whitelisting
- **Standalone JSON file**: Full SA JSON accessible only within the agent container filesystem
- **README.md**: Shows `private_key: "***REDACTED***"` and `private_key_id: "***REDACTED***"`

### Access Control
- Standard credential ownership model (only owner can view/edit/delete)
- Sharing via existing `CredentialShare` mechanism
- Agent linking via existing `AgentCredentialLink` mechanism

## Auto-Sync Behavior

SA credentials follow standard credential sync triggers (see [Agent Credentials](agent_credentials.md)):

| Trigger | SA-Specific Behavior |
|---------|---------------------|
| Credential created/updated | Standalone `.json` file written/overwritten |
| Credential deleted | Standalone `.json` file removed by cleanup logic |
| Credential unlinked from agent | Standalone `.json` file removed by cleanup logic |
| Multiple SA credentials on same agent | Each gets its own `{id}.json` file |
| Empty credential_data | Marked "incomplete"; no standalone file written |

### Orphan Cleanup

On every sync, `update_credentials()` reconciles SA files:
1. Lists all `*.json` files in `credentials/` (excluding `credentials.json`)
2. Deletes any that don't correspond to a current service account credential
3. Handles deletion and unlinking without requiring separate cleanup calls

## Agent Usage

Scripts discover the SA file via `credentials.json` and load it by path:
1. Read `credentials/credentials.json` and find the entry by credential ID or type
2. Get the `file_path` from `credential_data`
3. Use `service_account.Credentials.from_service_account_file(file_path)` to load credentials
4. Pass credentials to Google API clients (Sheets, BigQuery, Drive, etc.)

## Error Handling

| Scenario | Behavior |
|----------|----------|
| `credentials/` directory doesn't exist | Created by `update_credentials()` |
| File write permission error | `IOError` raised, logged, sync continues for other environments |
| Orphaned `.json` files from previous syncs | Cleaned up by reconciliation logic |

## Related Docs

- [Agent Credentials](agent_credentials.md) - Parent feature: credential lifecycle, sync rules, redaction
- [Agent Credentials Tech](agent_credentials_tech.md) - File locations, services, methods
- [Credentials Whitelist](credentials_whitelist.md) - Three-layer security model, per-type allowed fields
- [OAuth Credentials](oauth_credentials.md) - OAuth flow for Gmail, Drive, Calendar credentials
