# Anthropic Credential Types

## Purpose

Accept Anthropic credentials with automatic detection, appropriate environment variable handling, and expiry notification management. **API keys are the recommended credential for agents.** Claude Code OAuth tokens are still accepted so existing credentials keep working, but the UI steers users away from them: they are tied to a Claude subscription login that can be signed out unexpectedly, which silently breaks every cloud agent using the credential.

## Core Concepts

- **API Key** (recommended) - Key from console.anthropic.com, prefix `sk-ant-api*`, no typical expiry
- **OAuth Token** (legacy, not recommended) - Generated via `claude setup-token` CLI, prefix `sk-ant-oat*`, 1-year expiry. Can be signed out before it expires
- **Auto-Detection** - System detects credential type by prefix and sets the appropriate environment variable
- **Expiry Notification** - Optional date field with auto-set for OAuth tokens (11 months / 335 days)

## Credential Types

| Prefix | Type | Environment Variable | Typical Expiry |
|--------|------|---------------------|----------------|
| `sk-ant-api*` | API Key | `ANTHROPIC_API_KEY` | None |
| `sk-ant-oat*` | OAuth Token | `CLAUDE_CODE_OAUTH_TOKEN` | 1 year |
| Other | Unknown (defaults to API Key) | `ANTHROPIC_API_KEY` | None |

## User Stories / Flows

### Creating an API Key Credential

1. User opens the Add wizard on the AI Credentials card and selects "Anthropic"
2. The details step recommends an API key from console.anthropic.com and notes that OAuth tokens are also accepted (the full "Anthropic Setup Guide" is under the card's ⋯ menu)
3. Enters API key (`sk-ant-api03-...`)
4. Expiry field remains empty (optional, user can set manually)
5. User saves, backend detects API key type, no auto-expiry

### Pasting an OAuth Token (discouraged)

1. User pastes a token (`sk-ant-oat01-...`) into the API key field
2. An informational warning appears under the field: OAuth tokens can be signed out unexpectedly; an API key is recommended. Saving is not blocked
3. The token is saved as before: frontend auto-fills expiry date to 11 months from now, backend detects the OAuth token and confirms expiry auto-set

### Moving an Existing OAuth Token Credential to an API Key

1. User edits an existing Anthropic credential whose stored key is an OAuth token (`is_oauth_token`)
2. The edit dialog shows the same warning under the API key field while it is blank
3. User pastes an API key; the warning disappears
4. Agents keep referencing the same credential, so nothing else changes

### Environment Using OAuth Token

1. Environment with Anthropic SDK starts up
2. Environment lifecycle generates `.env` file
3. Detection runs on credential: `sk-ant-oat01-...` → `CLAUDE_CODE_OAUTH_TOKEN`
4. `.env` written with `CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-...` (and `ANTHROPIC_API_KEY=` empty)
5. Agent SDK reads `CLAUDE_CODE_OAUTH_TOKEN`

### Viewing Expiring Credentials

1. User opens Settings > AI Credentials
2. Credentials with expiry dates show their state on the row:
   - Expired (< 0 days): red "Expired" badge
   - Expiring soon (≤ 60 days): "Expires in N days" badge
   - Later (> 60 days): no badge, "Expires <date>" in the row's secondary text
3. Hover shows tooltip with exact date and days remaining; expired or ≤ 30-day credentials sort right after the default one on the card

### Updating API Key to OAuth Token (discouraged)

1. User edits an existing Anthropic credential
2. Replaces API key with OAuth token (`sk-ant-oat01-...`); the warning appears
3. Frontend auto-fills new expiry date
4. Backend detects type change, confirms auto-set expiry

## Business Rules

- **API keys are the recommended path, OAuth tokens remain supported** - Users can still save an OAuth token (create and edit); the UI only stops steering them to it. The setup guide and Add wizard give step-by-step instructions only for API keys; the guide's "Claude Code OAuth Tokens" article explains why `claude setup-token` tokens are not recommended and how to switch. The OAuth warning is informational and never blocks saving
- **Auto-detection is server-side** - Backend detects credential type on create, update, and environment generation
- **Expiry is informational** - Not enforced; serves as a reminder for token renewal
- **Auto-expiry for OAuth only** - Only OAuth tokens (`sk-ant-oat*`) get auto-set expiry (335 days)
- **User can override** - Auto-set expiry date can be modified or cleared by the user
- **Unknown prefixes default to API Key** - If prefix is not recognized, treated as standard API key
- **Both env vars passed to container** - Docker template includes both `ANTHROPIC_API_KEY` and `CLAUDE_CODE_OAUTH_TOKEN`; only the appropriate one is populated
- **OAuth tokens cannot be used for AI Functions** - OAuth tokens (`sk-ant-oat*`) are incompatible with the Anthropic Messages API used for AI utility calls (titles, schedules, SQL generation, etc.). The system rejects them at both the settings save endpoint and the service layer. See [AI Functions SDK Routing](ai_functions_sdk_routing.md)
- **OAuth tokens skipped for model discovery** - The daily model-discovery cron detects `sk-ant-oat*` prefixes and skips the Anthropic `/v1/models` call, recording `"oauth_token_unsupported"` as `models_discovery_error`. Health classification for environments backed by OAuth credentials falls back to the static `RETIRED_MODELS` catalog. No false-positive health warnings are raised for these credentials.

## Architecture Overview

```
Credential created/updated → Backend auto-detects type by prefix
                           ↓
OAuth token detected → Auto-set expiry_notification_date (335 days)
                           ↓
Environment starts → detect_anthropic_credential_type() called
                           ↓
.env file generated → Correct env var populated (ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN)
                           ↓
Container started → Agent SDK reads the populated env var
```

## Integration Points

- **AI Credentials Service** - Detection on create/update for expiry auto-set. See [AI Credentials](ai_credentials.md)
- **Environment Lifecycle** - Detection during `.env` file generation for correct env var
- **Frontend Dialog** - API-key hint in the Add wizard, OAuth-token warning (`AnthropicOAuthTokenWarning`) in the Add wizard and edit dialog, auto-fill expiry on OAuth token input, setup guide modal
- **Credentials List** - Expiry badge display with color coding; `is_oauth_token` field drives UI disabling in AI Functions credential picker
- **AI Functions SDK Routing** - OAuth tokens rejected for use with AI utility functions. See [AI Functions SDK Routing](ai_functions_sdk_routing.md)
- **Model Discovery** - OAuth tokens are skipped by the discovery cron (`sk-ant-oat*` prefix detected, `"oauth_token_unsupported"` recorded). Model health for these environments uses static fallback only. See [Model Freshness Tech](../../agents/agent_environments/model_freshness_tech.md)

---

*Last updated: 2026-09-16*
