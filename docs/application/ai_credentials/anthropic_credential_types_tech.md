# Anthropic Credential Types - Technical Details

## File Locations

### Backend

**Detection Utility:**
- `backend/app/utils.py:163` - `detect_anthropic_credential_type()` - Returns `(env_var_name, key_type_description)` tuple

**Service Logic:**
- `backend/app/services/credentials/ai_credentials_service.py:76-83` - Auto-set expiry on credential creation (OAuth tokens)
- `backend/app/services/credentials/ai_credentials_service.py:130-136` - Auto-set expiry on credential update (key changed to OAuth)

**Environment Generation:**
- `backend/app/services/environments/environment_lifecycle.py:1293-1344` - Credential type detection and `.env` variable selection

**Models:**
- `backend/app/models/credentials/ai_credential.py` - `expiry_notification_date` field on `AICredentialBase`, `AICredentialCreate`, `AICredentialUpdate`, `AICredentialPublic`

**Templates:**
- `backend/app/env-templates/python-env-advanced/docker-compose.template.yml:22-23` - Both env vars passed with optional syntax (`${VAR:-}`)

**Migration:**
- `backend/app/alembic/versions/67bd39e7e42c_add_expiry_notification_date_to_ai_.py` - Expiry date column

### Frontend

**Components:**
- `frontend/src/components/UserSettings/AnthropicCredentialsModal.tsx` - Instructions modal (API Key setup article + OAuth setup article)
- `frontend/src/components/UserSettings/AICredentialDialog.tsx:92-101` - `useEffect` for auto-fill expiry on OAuth token input
- `frontend/src/components/UserSettings/AICredentialDialog.tsx:226-241` - Anthropic info banner with "Instructions" button
- `frontend/src/components/UserSettings/AICredentialDialog.tsx:277-303` - Expiry date input field
- `frontend/src/components/UserSettings/AICredentials.tsx:49-89` - `getExpiryBadgeProps()` function (color coding logic)
- `frontend/src/components/UserSettings/AICredentials.tsx:249-267` - Expiry badge rendering in credential rows
- `frontend/src/components/Common/RelativeTime.tsx` - Extended with `asBadge`, `icon`, `showTooltip`, `colorCode` parameters

## Detection Logic

`detect_anthropic_credential_type(api_key)` at `backend/app/utils.py:163`:
- `sk-ant-oat*` → `("CLAUDE_CODE_OAUTH_TOKEN", "OAuth Token")`
- `sk-ant-api*` → `("ANTHROPIC_API_KEY", "API Key")`
- Other → `("ANTHROPIC_API_KEY", "API Key (Unknown Format)")`

## Environment Variable Generation

`environment_lifecycle.py:1293-1344` - `_generate_env_file()`:

1. Checks if Anthropic SDK is used (`uses_anthropic`)
2. If credential exists, calls `detect_anthropic_credential_type()`
3. Sets the appropriate variable, leaves the other empty with a comment
4. Both variables always present in `.env` for template compatibility

Output patterns:
- OAuth: `ANTHROPIC_API_KEY=` + `CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-...`
- API Key: `ANTHROPIC_API_KEY=sk-ant-api03-...` + `CLAUDE_CODE_OAUTH_TOKEN=`

## Expiry Auto-Set Logic

**On create** (`ai_credentials_service.py:76-83`):
- If type is `anthropic` and no expiry date provided
- Detects OAuth token by prefix
- Sets `expiry_notification_date = now + 335 days`

**On update** (`ai_credentials_service.py:130-136`):
- If API key is being changed, type is `anthropic`, and no explicit expiry provided
- Detects if new key is OAuth token
- Sets expiry to `now + 335 days`

## Frontend Auto-Fill

`AICredentialDialog.tsx:92-101`:
- `useEffect` watches `apiKey` and `type` state
- When user types OAuth token prefix (`sk-ant-oat`): calculates `today + 335 days`, fills expiry field
- User can adjust or clear

## Expiry Badge Color Coding

`AICredentials.tsx:49-89` - `getExpiryBadgeProps(expiryDate)`:

| Status | Days Until Expiry | Color |
|--------|------------------|-------|
| Expired | < 0 | Red (`bg-red-100 dark:bg-red-950`) |
| Expiring Very Soon | ≤ 30 | Orange (`bg-orange-100 dark:bg-orange-950`) |
| Expiring Soon | 31-60 | Amber (`bg-amber-100 dark:bg-amber-950`) |
| Not Expiring Soon | > 60 | Gray (`bg-muted text-muted-foreground`) |

Display format: `[Credential Name] [star] [calendar icon + date badge] [Type] [Actions]`

## Instructions Modal

`AnthropicCredentialsModal.tsx`:
- Encyclopedia-style layout (pattern from `GettingStartedModal.tsx`)
- Violet accent colors, dark mode support, 800px max width
- Article 1: "Setup via API Keys" - link to console.anthropic.com
- Article 2: "Setup via Claude Code OAuth" - `claude setup-token` instructions, 1-year expiration notice

Triggered from `AICredentialDialog.tsx:226-241` when type is `anthropic`.

---

*Last updated: 2026-03-02*
