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
9. **ssh_key** - SSH key pair (generated or imported) materialized into `~/.ssh/` inside the agent container for `git clone git@…`, `ssh …`, etc. See [SSH Key Credentials](ssh_key_credentials.md).

## User Stories / Flows

### Credential Lifecycle in Agent Environment

1. User creates a credential in the Credentials UI (encrypted and stored in DB)
2. User links credential to an agent via Agent Credentials tab
3. Agent environment starts or rebuilds - credentials automatically synced to container
4. Agent scripts read `workspace/credentials/credentials.json` for full credential data
5. Agent prompt receives `workspace/credentials/README.md` with redacted values for context
6. User updates a credential - all running agent environments auto-sync

### Install-Time Placeholder Credentials and the Setup Flow

When a bundle is installed, placeholder credentials are created for any user-provided (PBU) or template-provided (PBT) specs that were not filled in at install time:

1. After install, the user is redirected to the agent detail page's **Credentials tab** (`/agent/$agentId#credentials`)
2. The Credentials tab fetches `GET /agents/{id}/credentials`, which decrypts each linked credential and returns `is_placeholder` + `status` (`"complete"` / `"incomplete"`) per row
3. A top-of-card amber `Alert` summarises how many credentials still need setup (e.g. "2 credentials still need to be filled in")
4. Each row where `is_placeholder=true` OR `status === "incomplete"` shows a "Setup needed" amber badge next to the credential name
5. Clicking the credential name link (which navigates to `/credential/$credentialId`) opens the full credential detail page where the user fills in the missing values
6. Once all required fields pass the per-type completeness check, `is_placeholder` is cleared and the runtime gate re-evaluates
7. The `SetupNeededBanner` above the tabs clears automatically when `INSTALL_SETUP_COMPLETED` fires

### API Token Processing

1. User creates API Token credential, choosing "Bearer" or "Custom" type
2. For Bearer: system generates `Authorization: Bearer {token}` header pair
3. For Custom: user provides template (e.g., `X-API-Key: {TOKEN}`), system parses to header name/value
4. Agent environment receives pre-processed `http_header_name` and `http_header_value` - no parsing needed
5. If the credential has a `service_uri` set (a non-secret audience/slot id stored as a `Credential` column), it is also synced into the credential data so agent scripts can read it alongside the header pair

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

- [Cinna CLI Integration](../../application/cinna_cli_integration/cinna_cli_integration.md) - Credentials are **not** sent to the CLI user's machine. The remote agent environment holds all credentials; `cinna exec` runs commands inside that environment where credentials are already available. There is no `/credentials` CLI endpoint.
- [Agent Environments](../agent_environments/agent_environments.md) - Credentials synced during environment lifecycle events
- [Agent Environment Data Management](../agent_environment_data_management/agent_environment_data_management.md) - Credential sync as part of data management operations
- [Agent Environment Core](../agent_environment_core/agent_environment_core.md) - Agent-env server receives and stores credential files
- [OAuth Credentials](oauth_credentials.md) - OAuth flow, Google scopes, token refresh lifecycle, CSRF protection
- [Credentials Whitelist](credentials_whitelist.md) - Three-layer security model, per-type allowed fields, whitelist vs blacklist rationale
- [Google Service Account](google_service_account.md) - SA JSON key files, standalone file sync, file-path references in credentials.json
- [Credential Sharing](credential_sharing.md) - User-to-user credential sharing with read-only access for recipients
- [SSH Key Credentials](ssh_key_credentials.md) - SSH key pair credentials: generate/import, ~/.ssh/ materialization, known_hosts seeding, security model
- [Agent Prompts](../agent_prompts/agent_prompts.md) - Credentials README included in building mode prompt <!-- TODO: create agent_prompts docs -->
- [Agent Bundles & Installs](../agent_bundles/agent_bundles.md) - Install-time placeholder credentials and the runtime gate; the Credentials tab is the primary surface for resolving gate blocks after install

## Current User Context

Every agent environment's `credentials.json` contains a **synthetic, reserved entry** with `id="current_user"`. It is not a real `Credential` row and has no `CredentialType` — it is built on-the-fly from the agent owner's `User` row (`agent.owner_id`) each time credentials are prepared for the environment. It carries no secrets and appears unredacted in `credentials/README.md` so the agent's system prompt always knows who it is operating on behalf of.

### Shape

```json
{
  "id": "current_user",
  "name": "Current User",
  "type": "current_user",
  "notes": "Auto-generated identity & details of the agent owner. Not a real credential.",
  "credential_data": {
    "username": "evgeny",
    "full_name": "Evgeny L.",
    "email": "owner@example.com",
    "email_confirmed": true,
    "timezone": "Europe/Berlin",
    "language": "en",
    "locale": "en-GB",
    "conversation_style": "ai_default",
    "custom_details": {
      "REAL_NAME": "Evgeny L.",
      "FAVORITE_FOOD": "hotdogs"
    }
  }
}
```

`timezone`, `language`, and `locale` are `null` when the user has not set them. `conversation_style` is always present (non-null, defaults to `"ai_default"`). The three nullable fields are filled on first browser login via `PATCH /users/me/locale-defaults` (NULL-only fill, never overwrites an explicit choice) and can be set manually in the **Communication & Locale** card (Settings → My profile).

The `id` is a fixed sentinel (not a UUID). Real credential IDs are UUIDs, so there is no collision.

### Consuming it in scripts

```python
import json

with open("workspace/credentials/credentials.json") as f:
    creds = {c["id"]: c["credential_data"] for c in json.load(f)}

me = creds["current_user"]
send_to = me["email"]
real_name = me["custom_details"].get("REAL_NAME")
```

The standard dict-comprehension consumer pattern absorbs the synthetic entry naturally: `creds["current_user"]` is the identity + `custom_details` map. No script changes needed for existing consumers.

### custom_details — User's Details card

`custom_details` is a user-authored `KEY = value` map. Users edit it from the **User's Details** card (Settings → My profile). The card body shows the normalized map as read-only `KEY="value"` lines; an **Edit** button in the card header opens a modal with a free-text editor (env-file style: `REAL_NAME = Master of the universe`). On save, the backend normalizes each key to `UPPER_SNAKE` form and stores both the raw text (for re-opening the editor) and the normalized map. Invalid input (bad key, duplicates, >100 keys, >10 KB) returns a 422 with a line-referencing error shown inline in the modal (editor stays open).

The My profile tab orders its cards: **User Information**, **User's Details**, **Communication & Locale**, **Notifications**.

### Locale and communication preferences

The `current_user` block also carries four locale and communication preference fields set in the **Communication & Locale** card (Settings → My profile). Each control auto-saves on change (a per-field `PATCH /users/me` fires immediately — there is no Save button); the three locale fields use searchable dropdowns, and conversation style is a plain select:

- `timezone` — IANA timezone string (e.g. `Europe/Berlin`). `null` when unset.
- `language` — preferred communication language (e.g. `en`, `English`). `null` when unset.
- `locale` — BCP-47 formatting locale for dates/times/numbers (e.g. `en-US`, `de-DE`). Distinct from `language`. `null` when unset.
- `conversation_style` — tone hint for the agent in conversation mode. One of `ai_default` (no adjustment), `concise_direct`, or `friendly_chatty`. Always present (non-null).

Agents read these directly from `credentials.json` `current_user.credential_data`. The `credentials/README.md` `## Current User` section explains each field and instructs the agent how to honor them. In addition, for `concise_direct` and `friendly_chatty`, a single tone sentence is appended to the conversation-mode system prompt by the prompt generator (see [Agent Prompts](../agent_prompts/agent_prompts.md)). Note: the tone sentence requires an environment image rebuild to take effect; the `credentials.json` / README fields propagate immediately via normal credential sync.

### Re-sync fan-out

When a user saves their locale/communication preferences (via `PATCH /users/me`) or their User's Details, the platform re-syncs **all running environments of all agents the user owns** via `event_user_details_updated`. The block injected into any given environment always reflects that environment's install owner — so foreign installs (agents installed by someone else) show the installer's details, not the original publisher's.

### Prompt visibility

Because the block carries no secrets (only the owner's public identity and their own self-authored notes), it is intentionally exempt from the whitelist/redaction machinery and appears fully unredacted in `credentials/README.md`. The `## Current User` section in the README tells the agent who it is operating on behalf of and includes a one-line access snippet.

### Related synthetic entry: `owner_identity_token`

`current_user` is not the only synthetic, host-computed entry. When an environment has at least one linked `agent_api` connection, a second reserved entry (`id="owner_identity"`, `type="owner_identity_token"`) is appended the same way — built host-side from the install owner, never stored, never redacted, never user-editable. It carries a short-lived signed token the agent sends on Agent REST API calls so the producer can identify the calling user (and apply per-user scopes). It appears unredacted in `credentials/README.md` (the token is meant to be sent on the wire, not hidden). Full detail lives in the agent_api feature docs — see [Agent REST API → Caller Identity & Producer Scopes](../agent_api/agent_api.md#caller-identity--producer-scopes).

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
- Use `creds["current_user"]["email"]` to know who to notify without any per-agent config
