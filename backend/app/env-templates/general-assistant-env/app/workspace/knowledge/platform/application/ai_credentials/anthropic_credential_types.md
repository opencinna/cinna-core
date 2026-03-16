# Anthropic Credential Types

## Purpose

Support both Anthropic API Keys and Claude Code OAuth Tokens with automatic detection, appropriate environment variable handling, and expiry notification management.

## Core Concepts

- **API Key** - Traditional key from console.anthropic.com, prefix `sk-ant-api*`, no typical expiry
- **OAuth Token** - Generated via `claude setup-token` CLI, prefix `sk-ant-oat*`, 1-year expiry
- **Auto-Detection** - System detects credential type by prefix and sets the appropriate environment variable
- **Expiry Notification** - Optional date field with auto-set for OAuth tokens (11 months / 335 days)

## Credential Types

| Prefix | Type | Environment Variable | Typical Expiry |
|--------|------|---------------------|----------------|
| `sk-ant-api*` | API Key | `ANTHROPIC_API_KEY` | None |
| `sk-ant-oat*` | OAuth Token | `CLAUDE_CODE_OAUTH_TOKEN` | 1 year |
| Other | Unknown (defaults to API Key) | `ANTHROPIC_API_KEY` | None |

## User Stories / Flows

### Creating an OAuth Token Credential

1. User opens AI Credentials dialog, selects "Anthropic" type
2. Clicks "Instructions" button - modal opens with setup guides
3. Follows OAuth setup: runs `claude setup-token` locally, copies token
4. Pastes token (`sk-ant-oat01-...`) into API key field
5. Frontend auto-fills expiry date to 11 months from now
6. User saves credential
7. Backend detects OAuth token, confirms expiry auto-set

### Creating an API Key Credential

1. User opens AI Credentials dialog, selects "Anthropic"
2. Enters API key (`sk-ant-api03-...`)
3. Expiry field remains empty (optional, user can set manually)
4. User saves, backend detects API key type, no auto-expiry

### Environment Using OAuth Token

1. Environment with Anthropic SDK starts up
2. Environment lifecycle generates `.env` file
3. Detection runs on credential: `sk-ant-oat01-...` → `CLAUDE_CODE_OAUTH_TOKEN`
4. `.env` written with `CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-...` (and `ANTHROPIC_API_KEY=` empty)
5. Agent SDK reads `CLAUDE_CODE_OAUTH_TOKEN`

### Viewing Expiring Credentials

1. User opens Settings > AI Credentials
2. Credentials with expiry dates show color-coded badges:
   - Red: Expired (< 0 days)
   - Orange: Expiring very soon (≤ 30 days)
   - Amber: Expiring soon (31-60 days)
   - Gray: Not expiring soon (> 60 days)
3. Hover shows tooltip with exact date and days remaining

### Updating API Key to OAuth Token

1. User edits an existing Anthropic credential
2. Replaces API key with OAuth token (`sk-ant-oat01-...`)
3. Frontend auto-fills new expiry date
4. Backend detects type change, confirms auto-set expiry

## Business Rules

- **Auto-detection is server-side** - Backend detects credential type on create, update, and environment generation
- **Expiry is informational** - Not enforced; serves as a reminder for token renewal
- **Auto-expiry for OAuth only** - Only OAuth tokens (`sk-ant-oat*`) get auto-set expiry (335 days)
- **User can override** - Auto-set expiry date can be modified or cleared by the user
- **Unknown prefixes default to API Key** - If prefix is not recognized, treated as standard API key
- **Both env vars passed to container** - Docker template includes both `ANTHROPIC_API_KEY` and `CLAUDE_CODE_OAUTH_TOKEN`; only the appropriate one is populated

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
- **Frontend Dialog** - Auto-fill expiry on OAuth token input, instructions modal
- **Credentials List** - Expiry badge display with color coding

---

*Last updated: 2026-03-02*
