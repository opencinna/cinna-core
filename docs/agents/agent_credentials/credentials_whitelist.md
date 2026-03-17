# Credentials Whitelist

## Purpose

The credentials system uses a **whitelist (allowlist) approach** to control what data is exposed to agent environments. Only explicitly listed fields are transferred — all others are denied by default. This prevents accidental exposure of sensitive data like OAuth refresh tokens and client secrets when new fields are added to the database.

## Three-Layer Security Model

Credentials pass through three security layers before reaching an agent:

```
Layer 1: Database Encryption
  All credentials encrypted at rest in Credential.encrypted_data field
                    │
                    ▼
Layer 2: Field Whitelisting (credentials.json)
  WHITELIST approach: only explicitly allowed fields pass through
  Default behavior: DENY (unknown fields excluded)
  Applied before syncing to agent container
                    │
                    ▼
Layer 3: Value Redaction (README.md)
  Shows FILTERED structure (same fields as credentials.json)
  Sensitive values replaced with ***REDACTED***
  Fields removed by whitelist don't appear at all
  Included in agent prompt for context
```

## Design Principles

1. **Explicit Allow** - Only fields listed in the whitelist are transferred to agents
2. **Fail-Safe Default** - Unknown credential types return empty dict (better to break than leak)
3. **Minimal Exposure** - Agent gets only what it needs to function
4. **No Refresh Tokens** - Backend handles OAuth token refresh transparently
5. **No Secrets** - Client secrets never leave the backend server

## What Gets Whitelisted

### OAuth Credentials (Gmail, Drive, Calendar)

Allowed fields: `access_token`, `token_type`, `expires_at`, `scope`, `granted_user_email`, `granted_user_name`

Excluded (backend-only): `refresh_token`, `client_secret`, `granted_at`

### Non-OAuth Credentials

- **email_imap** - Allowed: `host`, `port`, `login`, `password`, `is_ssl`
- **email_smtp** - Allowed: `host`, `port`, `username`, `password`, `from_email`, `use_tls`, `use_ssl`
- **odoo** - Allowed: `url`, `database_name`, `login`, `api_token`
- **api_token** - Allowed: `http_header_name`, `http_header_value` (pre-processed from raw token + template)
- **google_service_account** - Allowed: `file_path`, `project_id`, `client_email`

For API tokens, raw fields like `api_token_type`, `api_token_template`, and `api_token` are NOT included — they are pre-processed into ready-to-use HTTP header pairs before whitelisting.

## Why Whitelist Over Blacklist

- **Blacklist risk**: A new field added to the database (e.g., `internal_user_id`) would be automatically exposed to agents unless someone remembers to add it to the exclusion list
- **Whitelist safety**: New fields are automatically excluded — a developer must explicitly add them to the allowed list, which gets caught in code review

## OAuth-Specific Security

### Why No Refresh Tokens in Agent Environment?

1. **Backend handles refresh** - Token refresh happens automatically before each stream; agent always gets a fresh access token
2. **Reduced attack surface** - If an agent container is compromised, the attacker only gets a short-lived access token (~1 hour); the long-lived refresh token remains secure in the backend database
3. **Simplified agent logic** - Agent doesn't need OAuth refresh logic; just uses the provided access token like any other credential

## Adding New Credential Types

When adding a new credential type, the whitelist must be explicitly updated:

1. Define the database model and add to `CredentialType` enum
2. Add whitelist entry to `AGENT_ENV_ALLOWED_FIELDS` — only include fields the agent needs
3. Add redaction rules to `SENSITIVE_FIELDS` for README generation
4. Never include refresh tokens, client secrets, or backend-only API keys
5. Test that sensitive fields are excluded from agent environment output

## Security Audit Checklist

- All credential types have an `AGENT_ENV_ALLOWED_FIELDS` entry
- OAuth types do NOT include `refresh_token` or `client_secret`
- Non-OAuth types only include fields needed for agent function
- New credential types reviewed for security before merging
- Agent environment tests verify filtering works correctly

## Related Docs

- [Agent Credentials](agent_credentials.md) - Parent feature: credential lifecycle, sync rules, redaction
- [Agent Credentials Tech](agent_credentials_tech.md) - File locations, services, methods
- [OAuth Credentials](oauth_credentials.md) - OAuth flow, token refresh lifecycle, CSRF protection
