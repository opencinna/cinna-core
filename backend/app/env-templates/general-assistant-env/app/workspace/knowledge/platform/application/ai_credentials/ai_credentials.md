# AI Credentials Management

## Purpose

Enable users to manage multiple named AI credentials (API keys) for different LLM providers, with default credential selection, environment linking, and sharing between users.

## Core Concepts

- **Named AI Credential** - Reusable, encrypted API key with a user-defined name (e.g., "Production Anthropic", "Testing OpenAI")
- **Default Credential** - One credential per type marked as default; auto-synced to user profile for backward compatibility
- **Credential Type** - Provider category: `anthropic`, `minimax`, `openai_compatible`
- **Auto-Sync** - When a credential is set as default, its values are copied to the user's profile fields (`ai_credentials_encrypted`) so existing code continues working
- **Environment Linking** - Environments can use default credentials or be explicitly linked to specific credentials
- **Credential Provision** - Agent owners can attach their AI credentials when sharing, so recipients don't need their own

## Credential Types

| Type | SDK ID | Required Fields |
|------|--------|-----------------|
| `anthropic` | `claude-code/anthropic` | `api_key` |
| `minimax` | `claude-code/minimax` | `api_key` |
| `openai_compatible` | `google-adk-wr/openai-compatible` | `api_key`, `base_url`, `model` |

## User Stories / Flows

### Creating and Managing Credentials

1. User opens Settings > AI Credentials
2. Clicks "Add" to create a new credential
3. Enters name, selects type, provides API key (and base_url/model for openai_compatible)
4. Optionally marks as default
5. Credential is encrypted and stored
6. If marked as default, previous default for that type is unset, and values sync to user profile

### Using Default Credentials with Environments

1. User creates an environment with `use_default_ai_credentials = true` (default)
2. Backend resolves the user's default credentials for each SDK type
3. Credentials injected into container `.env` file
4. If no default exists for a required type, an error is returned

### Using Specific Credentials with Environments

1. User creates environment, toggles "Use Default AI Credentials" OFF
2. Selects specific credentials from dropdowns (filtered by SDK type)
3. Backend validates credentials match SDK requirements
4. Environment created with explicit credential links

### Sharing Agent WITH AI Credentials

1. Owner creates a share, toggles "Provide AI Credentials" ON
2. Owner selects credentials for conversation (and building if applicable)
3. Share record created with credential IDs
4. Recipient accepts share in wizard, sees "Credentials provided by owner" - no action needed
5. Clone created, owner's credentials linked via credential share records
6. Clone environment uses shared credentials

### Sharing Agent WITHOUT AI Credentials

1. Owner shares agent, leaves "Provide AI Credentials" OFF
2. Recipient accepts share in wizard
3. Wizard shows AI Credentials step:
   - If recipient has matching defaults: auto-selected with green badge
   - If recipient has other credentials: dropdown to select
   - If no credentials exist: error with link to Settings
4. Clone created with recipient's own credentials

### Updating Credentials and Rebuilding Environments

1. User updates an AI credential's API key
2. After save, the affected environments dialog appears automatically
3. Shows all environments using this credential (with usage type: conversation, building, or both)
4. All environments pre-selected by default for batch rebuild
5. User can selectively rebuild or skip

## Business Rules

- **One default per type** - Setting a new default automatically unsets the previous one for the same type
- **Auto-sync on default** - Default credential values are always copied to user profile fields
- **Profile cleared on delete** - If the deleted credential was the default, corresponding user profile fields are cleared
- **Ownership validation** - All operations verify the credential belongs to the requesting user
- **Cascade delete** - Deleting a user cascades to all their credentials
- **Type immutable on edit** - Credential type cannot be changed after creation
- **Name required** - Max 255 characters
- **API key required** - For all types
- **Base URL + Model required** - Only for `openai_compatible` type
- **Keys never exposed** - API responses show `has_api_key: true` instead of the actual key
- **Share access control** - Shared credentials can only be used, not modified, by recipients

## Architecture Overview

```
User creates AI Credential → Encrypted storage in ai_credential table
                          ↓
User sets as default → Auto-sync to user.ai_credentials_encrypted
                          ↓
Environment creation → Resolves credentials (default or explicit link)
                          ↓
Container startup → Credentials injected into .env file
```

```
Owner shares agent → Optionally attaches own AI credentials
                          ↓
Recipient accepts → Wizard handles credential selection/display
                          ↓
Clone created → Uses owner's shared or recipient's own credentials
```

## Integration Points

- **Agent Environments** - Credentials resolved during environment creation; injected into Docker `.env` files. See [Agent Environments](../../agents/agent_environments/agent_environments.md)
- **Agent Sharing** - Owner can provide credentials with shares; recipients select credentials in accept wizard. See [Agent Sharing](../../agents/agent_sharing/agent_sharing.md)
- **Agent Cloning** - Clone setup links credentials based on share configuration or recipient selection
- **User Profile** - Default credentials auto-sync to `ai_credentials_encrypted` for backward compatibility
- **Anthropic Dual Types** - Special handling for API Keys vs OAuth Tokens. See [Anthropic Credential Types](anthropic_credential_types.md)
- **Affected Environments** - After credential updates, automatic detection and batch rebuild of affected environments. See [Affected Environments Widget](affected_environments_widget.md)
- **Environment Lifecycle** - Credential type detection determines which environment variables are set. See `backend/app/services/environment_lifecycle.py`

---

*Last updated: 2026-03-02*
