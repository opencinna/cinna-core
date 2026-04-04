# AI Credentials Management

## Purpose

Enable users to manage multiple named AI credentials (API keys) for different LLM providers, with default credential selection, environment linking, and sharing between users.

## Core Concepts

- **Named AI Credential** - Reusable, encrypted API key with a user-defined name (e.g., "Production Anthropic", "Testing OpenAI")
- **Default Credential** - One credential per type marked as default; auto-synced to user profile for backward compatibility
- **Prioritized Default Resolution** - When "Use Default" is selected for an SDK engine, the system finds the best matching default credential using priority: Anthropic > Google (Gemini) > OpenAI > any other compatible type (oldest first)
- **Credential Type** - Provider category: `anthropic`, `minimax`, `openai`, `openai_compatible`, `google`
- **Auto-Sync** - When a credential is set as default, its values are copied to the user's profile fields (`ai_credentials_encrypted`) so existing code continues working
- **Environment Linking** - Environments can use default credentials or be explicitly linked to specific credentials
- **Credential Provision** - Agent owners can attach their AI credentials when sharing, so recipients don't need their own

## Credential Types

| Type | Required Fields | Optional Fields | Compatible SDK Engines |
|------|-----------------|-----------------|------------------------|
| `anthropic` | `api_key` | — | `claude-code`, `opencode` |
| `minimax` | `api_key` | — | `claude-code` |
| `openai` | `api_key` | — | `opencode` |
| `openai_compatible` | `api_key`, `base_url`, `model` | — | `opencode` |
| `google` | `api_key` | `base_url` | `opencode` |

Note: `anthropic` credentials also support OAuth tokens (prefix `sk-ant-oat*`) — see [Anthropic Credential Types](anthropic_credential_types.md).

## User Stories / Flows

### Creating and Managing Credentials

1. User opens Settings > AI Credentials
2. Clicks "Add" to create a new credential
3. Enters name, selects type, provides API key (and base_url/model for openai_compatible)
4. Optionally marks as default
5. Credential is encrypted and stored
6. If marked as default, previous default for that type is unset, and values sync to user profile

### Setting Up API Keys and Defaults

1. User opens User Settings > AI Credentials
2. The **Default SDK Preferences** panel shows compact summary rows for Conversation and Building modes, each displaying: SDK engine name, resolved credential, and model override (if set)
3. User clicks the **Edit** (pencil) button on a mode row to open a modal dialog with the cascading three-step selection:
   - **Step 1 — SDK Engine**: Claude Code or OpenCode
   - **Step 2 — Credential**: Dropdown filtered to credentials compatible with the chosen engine; first option is "Use Default" with a resolved default indicator showing which credential will actually be used (via prioritized resolution)
   - **Step 3 — Model Override** (optional): Free-text field with suggestions; leave empty to use the SDK adapter's built-in default
4. User clicks **Save** in the modal — values for that mode are saved
5. Saved defaults pre-populate the Add Environment dialog on future environment creation

### Using Default Credentials with Environments

1. User opens the Add Environment dialog
2. Conversation and Building modes are shown as compact summary rows pre-populated from the user's saved default preferences
3. User clicks the **Edit** (pencil) button on a mode row to open a modal with SDK engine, credential, and model override fields
4. If left on "Default", the resolved default indicator shows which credential will be used (prioritized resolution: Anthropic > Google > OpenAI > other compatible defaults)
5. Credentials injected into the container `.env` or SDK config files

### Using Specific Credentials with Environments

1. User opens Add Environment dialog and clicks Edit on a mode row
2. Selects a specific credential from the dropdown (filtered by the chosen SDK engine)
3. Backend validates the selected credential type matches the SDK engine compatibility rules
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
- **Prioritized default resolution** - When resolving which default credential to use for an SDK engine, priority order is: Anthropic > Google (Gemini) > OpenAI > any other compatible type ordered by creation date (oldest first). Only credentials marked as default AND compatible with the SDK engine are considered
- **Auto-sync on default** - Default credential values are always copied to user profile fields
- **Profile cleared on delete** - If the deleted credential was the default, corresponding user profile fields are cleared
- **Ownership validation** - All operations verify the credential belongs to the requesting user
- **Cascade delete** - Deleting a user cascades to all their credentials
- **Type immutable on edit** - Credential type cannot be changed after creation
- **Name required** - Max 255 characters
- **API key required** - For all credential types
- **Base URL + Model required** - Only for `openai_compatible` type; base URL is optional for `google`
- **Keys never exposed** - API responses show `has_api_key: true` instead of the actual key
- **Share access control** - Shared credentials can only be used, not modified, by recipients

## Architecture Overview

```
User creates AI Credential → Encrypted storage in ai_credential table
                          ↓
User sets as default → Auto-sync to user.ai_credentials_encrypted
                          ↓
User saves Default SDK Preferences → Stored as:
  user.default_sdk_conversation / default_sdk_building (engine+type)
  user.default_ai_credential_conversation_id / _building_id (FK → ai_credential)
  user.default_model_override_conversation / _building (optional string)
                          ↓
Environment creation → Resolves credentials:
  explicit credential ID from request
  → or user.default_ai_credential_*_id (named credential default)
  → or user.ai_credentials_encrypted (legacy flat default, for backward compat)
                          ↓
Container startup → Credentials injected into .env file or SDK config files
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
- **Environment Lifecycle** - Credential type detection determines which environment variables are set. See `backend/app/services/environments/environment_lifecycle.py`

---

*Last updated: 2026-03-21*
