# Credentials Whitelist - Technical Details

## File Locations

### Whitelist Configuration
- `backend/app/services/credentials/credentials_service.py:42` - `AGENT_ENV_ALLOWED_FIELDS` dict mapping credential types to their allowed field names
- `backend/app/services/credentials/credentials_service.py:26` - `SENSITIVE_FIELDS` dict mapping credential types to fields that get redacted in README

### Filter Implementation
- `backend/app/services/credentials/credentials_service.py:258` - `filter_credential_data_for_agent_env()` - Core whitelist filter function
- `backend/app/services/credentials/credentials_service.py:736` - `prepare_credentials_for_environment()` - Orchestrates decryption + whitelisting + README generation

### Sync Path
- `backend/app/services/credentials/credentials_service.py:774` - Whitelist applied per-credential inside `prepare_credentials_for_environment()`
- `backend/app/services/credentials/credentials_service.py` - `sync_credentials_to_agent_environments()` - Sends filtered credentials to running containers

### Agent Environment (Inside Container)
- `backend/app/env-templates/app_core_base/core/server/agent_env_service.py` - `update_credentials()` writes filtered `credentials.json` and redacted `README.md` to workspace

## Key Methods

### `CredentialsService.filter_credential_data_for_agent_env()`

Located at `backend/app/services/credentials/credentials_service.py:258`

- Accepts `credential_type` (str) and `credential_data` (dict)
- Looks up allowed fields from `AGENT_ENV_ALLOWED_FIELDS` for the given type
- Returns new dict containing ONLY the whitelisted fields that exist in the input
- If credential type is not in the whitelist, logs a warning and returns empty dict (fail-safe)

### `CredentialsService.prepare_credentials_for_environment()`

Located at `backend/app/services/credentials/credentials_service.py:736`

- Retrieves all credentials linked to an agent with decrypted data
- For each credential, applies `filter_credential_data_for_agent_env()` to produce `credentials.json` content
- Generates redacted `README.md` using the same filtered field set (README mirrors credentials.json structure)
- Returns both the JSON data and README content for sync to agent container

### `CredentialsService.redact_credential_data()`

Located at `backend/app/services/credentials/credentials_service.py`

- Uses `SENSITIVE_FIELDS` to identify which values to replace with `***REDACTED***`
- Only redacts non-empty values; empty/null fields shown as-is (indicates missing config)
- Operates on already-whitelisted data, so excluded fields never appear

## Data Flow

```
Database (encrypted_data) → decrypt_field() → full credential dict
    → filter_credential_data_for_agent_env() → whitelisted dict
        → credentials.json (sent to container)
        → redact_credential_data() → README.md (sent to container, included in prompt)
```

## Configuration

- Whitelist is defined as a class-level constant on `CredentialsService` — changes require code deployment
- Sensitive fields for redaction also defined as class-level constant
- Both are version-controlled, providing audit trail for security reviews
- No runtime configuration or environment variables — whitelist is compile-time only
