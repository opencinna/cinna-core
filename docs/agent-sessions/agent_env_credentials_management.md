# Agent Environment Credentials Management

## Overview

Agents can access user-provided credentials (email, APIs, databases) to perform automated tasks. Credentials are encrypted in the database, securely synced to agent environments, and made available to scripts in two formats:
- **Full data** (`credentials.json`) - for scripts to use programmatically
- **Redacted documentation** (`credentials/README.md`) - for building agent's prompt context

## Architecture

### Data Flow

1. **User manages credentials** → Stored encrypted in database (`Credential` model)
2. **User shares credential with agent** → Link created (`AgentCredentialLink`)
3. **Environment starts/rebuilds** → Credentials synced to container
4. **User updates credential** → Auto-syncs to all running environments

### File Structure in Agent Environment

```
workspace/
└── credentials/
    ├── credentials.json      # Full credentials data (for scripts)
    └── README.md            # Redacted docs (for agent prompt)
```

## Components

### Backend Services

**`CredentialsService`** (`backend/app/services/credentials_service.py`)
- `prepare_credentials_for_environment()` - Prepares both JSON and README data
- `generate_credentials_readme()` - Creates redacted documentation
- `redact_credential_data()` - Redacts sensitive fields only if they have values
- `sync_credentials_to_agent_environments()` - Syncs to all running environments
- Event handlers: `event_credential_updated()`, `event_credential_deleted()`, `event_credential_shared()`, `event_credential_unshared()`

**`EnvironmentLifecycleManager`** (`backend/app/services/environment_lifecycle.py`)
- `_sync_agent_data()` - Syncs prompts and credentials after start/rebuild
- Called automatically after: start, restart, rebuild (if was running)

### Agent Environment Service

**`AgentEnvService`** (`backend/app/env-templates/python-env-advanced/app/core/server/agent_env_service.py`)
- `update_credentials()` - Writes both `credentials.json` and `README.md`
- `get_credentials_readme()` - Loads README content for prompt

**API Route** (`backend/app/env-templates/python-env-advanced/app/core/server/routes.py`)
- `POST /config/credentials` - Accepts credentials data from backend

### API Routes

**Credentials Routes** (`backend/app/api/routes/credentials.py`)
- CREATE, UPDATE, DELETE operations trigger automatic environment sync
- All operations use `CredentialsService` for business logic

**Agent Routes** (`backend/app/api/routes/agents.py`)
- Link/unlink credentials trigger automatic environment sync
- Uses `CredentialsService.link_credential_to_agent()` / `unlink_credential_from_agent()`

## Credential Types

### Supported Types

1. **`email_imap`** - IMAP email access
   - Fields: `host`, `port`, `login`, `password`, `is_ssl`
   - Sensitive: `password`

2. **`odoo`** - Odoo ERP API
   - Fields: `url`, `database_name`, `login`, `api_token`
   - Sensitive: `api_token`

3. **`gmail_oauth`** - Gmail OAuth
   - Fields: `access_token`, `refresh_token`, `token_type`, `expires_at`, `scope`
   - Sensitive: `access_token`, `refresh_token`

## Security Model

### Database Layer
- Credentials encrypted using `encrypt_field()` / `decrypt_field()` (`backend/app/core/security.py`)
- Stored in `Credential.encrypted_data` field

### Environment Layer
- **`credentials.json`**: Full data (scripts read this)
- **`README.md`**: Redacted data (included in agent prompt)

### Redaction Rules
- Only redacts fields that have actual values (not empty/null)
- Empty fields shown as-is (safe, indicates missing configuration)
- README shows exact same structure as JSON (agent not confused)

## Automatic Synchronization

### Triggers

Credentials automatically sync to running environments when:

1. **Environment starts** → Initial sync
2. **Environment rebuilds** (if was running) → Re-sync after rebuild
3. **Credential updated** → Sync to all affected agents' running environments
4. **Credential deleted** → Sync to remove from all affected agents
5. **Credential shared with agent** → Sync to that agent's running environments
6. **Credential unshared from agent** → Sync to remove from that agent

### Implementation

- Uses FastAPI background tasks (routes) or direct async calls (services)
- Only syncs to **running** environments (stopped environments sync on next start)
- Errors logged but don't block other environments from syncing

## Prompt Integration

The building agent's prompt includes `credentials/README.md` content via:

**`PromptGenerator`** (`backend/app/env-templates/python-env-advanced/app/core/server/prompt_generator.py`)
- `_load_credentials_readme()` - Loads README from workspace
- `generate_building_mode_prompt()` - Includes credentials in system prompt with security warnings

### Agent Instructions

Building agent receives:
- Full credential structure (with redacted sensitive values)
- Security rules (never read credentials.json directly)
- Usage examples (only for credentials with data)
- Clear indication when credentials are empty/need configuration

## Best Practices

### For Users
- Update credentials through UI (triggers auto-sync)
- Share credentials before starting environment (or they'll be empty)
- Credentials persist in workspace (survive rebuilds)

### For Development
- Always use `CredentialsService` methods (never direct crud calls)
- Event handlers ensure consistency across running environments
- Redaction logic keeps sensitive data out of prompts while showing structure
