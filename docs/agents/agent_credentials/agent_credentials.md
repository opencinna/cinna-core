# Agent Credentials

## Purpose

Agents access user-provided credentials (email, APIs, databases, OAuth services) to perform automated tasks. Credentials are encrypted at rest, securely synced to agent environments via field whitelisting, and made available in two formats: full data for scripts and redacted documentation for agent prompts.

## Core Concepts

- **Credential** - Encrypted record storing service access details (API keys, passwords, OAuth tokens)
- **Agent-Credential Link** - Many-to-many association between agents and credentials they can use
- **Field Whitelisting** - Security layer that only sends explicitly allowed fields to agent environments
- **Credential Redaction** - Sensitive values replaced with `***REDACTED***` for agent prompt context
- **Pre-Stream Refresh** - Automatic OAuth token refresh before each agent stream session

## Credential Types

1. **email_imap** - IMAP email access (host, port, login, password)
2. **email_smtp** - SMTP email sending (host, port, username, password, from_email, use_tls, use_ssl)
3. **odoo** - Odoo ERP API (url, database_name, login, api_token)
4. **gmail_oauth / gmail_oauth_readonly** - Gmail OAuth (access_token, token_type, expires_at, scope)
5. **gdrive_oauth / gdrive_oauth_readonly** - Google Drive OAuth
6. **gcalendar_oauth / gcalendar_oauth_readonly** - Google Calendar OAuth
7. **google_service_account** - Google Service Account (private key JSON)
8. **api_token** - Generic API Token (Bearer or Custom template)

## User Stories / Flows

### Credential Lifecycle in Agent Environment

1. User creates a credential in the Credentials UI (encrypted and stored in DB)
2. User links credential to an agent via Agent Credentials tab
3. Agent environment starts or rebuilds - credentials automatically synced to container
4. Agent scripts read `workspace/credentials/credentials.json` for full credential data
5. Agent prompt receives `workspace/credentials/README.md` with redacted values for context
6. User updates a credential - all running agent environments auto-sync

### API Token Processing

1. User creates API Token credential, choosing "Bearer" or "Custom" type
2. For Bearer: system generates `Authorization: Bearer {token}` header pair
3. For Custom: user provides template (e.g., `X-API-Key: {TOKEN}`), system parses to header name/value
4. Agent environment receives pre-processed `http_header_name` and `http_header_value` - no parsing needed

### Email SMTP Credential Usage in Agent Scripts

1. User creates an `email_smtp` credential with SMTP server settings
2. User links the credential to an agent
3. Agent environment receives all 7 SMTP fields in `credentials.json` (password is accessible by scripts)
4. In `README.md` (included in agent prompt), the `password` field is shown as `***REDACTED***`
5. Agent scripts use the credential to send email via Python's `smtplib` or similar libraries

Example script pattern:
```python
import json, smtplib
from email.mime.text import MIMEText

with open("workspace/credentials/credentials.json") as f:
    creds = {c["id"]: c["credential_data"] for c in json.load(f)}

smtp = creds["<credential-id>"]
msg = MIMEText("Hello from agent")
msg["Subject"] = "Test"
msg["From"] = smtp["from_email"]
msg["To"] = "recipient@example.com"

with smtplib.SMTP(smtp["host"], smtp["port"]) as server:
    if smtp["use_tls"]:
        server.starttls()
    server.login(smtp["username"], smtp["password"])
    server.send_message(msg)
```

### OAuth Token Refresh Before Stream

1. User initiates a stream (conversation) with an agent
2. System checks all OAuth credentials linked to the agent
3. Any tokens expiring within 10 minutes are refreshed via provider API
4. Refreshed credentials synced to agent environment
5. Stream begins with valid tokens guaranteed for expected duration

## Business Rules

### Field Whitelisting (Security)

- Agent environments receive ONLY whitelisted fields per credential type (allowlist approach)
- OAuth `refresh_token` - NEVER exposed to agent (backend handles refresh)
- OAuth `client_secret` - NEVER exposed to agent (stays on backend server)
- Unknown credential types return empty dict (fail-safe default)
- Whitelisted fields are the same fields shown in both `credentials.json` and `README.md`
- See [Credentials Whitelist](credentials_whitelist.md) for the three-layer security model, per-type field lists, and adding new credential types

### Redaction Rules

- Sensitive field values replaced with `***REDACTED***` in README
- Fields with empty/null values shown as-is (safe, indicates missing configuration)
- README structure mirrors `credentials.json` exactly (no confusion for agents)
- Fields excluded by whitelisting do NOT appear in README at all

### Auto-Sync Triggers

Credentials automatically sync to running agent environments when:
- Environment starts (initial sync)
- Environment rebuilds while running (re-sync after rebuild)
- Credential updated by user (sync to all affected agents)
- Credential deleted (remove from all affected agents)
- Credential linked to agent (sync to that agent's environments)
- Credential unlinked from agent (remove from that agent)

### Sync Behavior

- Only syncs to running environments (stopped environments sync on next start)
- Sync errors are logged but don't block other environments from syncing
- Uses FastAPI background tasks (from routes) or direct async calls (from services)

### OAuth Refresh Rules

- Refresh threshold: tokens expiring within 600 seconds (10 minutes)
- Refresh is synchronous before streaming starts (blocking)
- Refresh failures logged but don't block streaming (graceful degradation)
- Supported OAuth types: gmail_oauth, gmail_oauth_readonly, gdrive_oauth, gdrive_oauth_readonly, gcalendar_oauth, gcalendar_oauth_readonly

## Architecture Overview

```
User manages credentials (UI) → Encrypted storage (DB)
         │
         ├→ Link credential to agent
         │
         ├→ Environment starts/rebuilds → CredentialsService prepares data
         │                                        │
         │                                        ├→ Decrypt + whitelist fields → credentials.json
         │                                        └→ Redact sensitive values → README.md
         │                                                    │
         │                                                    └→ Sync to agent container
         │
         └→ Stream initiated → Refresh expiring OAuth tokens → Sync → Start stream
```

### File Structure in Agent Environment

```
workspace/
└── credentials/
    ├── credentials.json      # Full credential data (whitelisted fields only)
    └── README.md             # Redacted docs (included in agent prompt)
```

## Integration Points

- [Agent Environments](../agent_environments/agent_environments.md) - Credentials synced during environment lifecycle events
- [Agent Environment Data Management](../agent_environment_data_management/agent_environment_data_management.md) - Credential sync as part of data management operations
- [Agent Environment Core](../agent_environment_core/agent_environment_core.md) - Agent-env server receives and stores credential files
- [OAuth Credentials](oauth_credentials.md) - OAuth flow, Google scopes, token refresh lifecycle, CSRF protection
- [Credentials Whitelist](credentials_whitelist.md) - Three-layer security model, per-type allowed fields, whitelist vs blacklist rationale
- [Google Service Account](google_service_account.md) - SA JSON key files, standalone file sync, file-path references in credentials.json
- [Credential Sharing](credential_sharing.md) - User-to-user credential sharing with read-only access for recipients
- [Agent Prompts](../agent_prompts/agent_prompts.md) - Credentials README included in building mode prompt <!-- TODO: create agent_prompts docs -->

## Best Practices

### For Users
- Update credentials through UI to trigger auto-sync to running environments
- Link credentials to agents before starting environments (otherwise they'll be empty until next sync)
- Use credential IDs in agent scripts (more stable than names which can change)

### For Agents (via README.md)
- Use credential IDs for lookup - IDs never change, unlike names
- Load credentials at script start and reuse connections
- Handle errors gracefully - credentials might be invalid or expired
- OAuth tokens are auto-refreshed before each stream - no manual refresh needed
- Never hardcode credentials - always read from the credentials file
