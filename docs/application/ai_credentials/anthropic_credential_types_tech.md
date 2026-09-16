# Anthropic Credential Types - Technical Details

## File Locations

### Backend

**Detection Utility:**
- `backend/app/services/ai_providers/anthropic.py` - `AnthropicAdapter.classify_key()` - the single declaration of the `sk-ant-oat` / `sk-ant-api` prefix rule. Returns a `KeyClassification(is_oauth_token, env_var_name, label)`
- `backend/app/utils.py` - `detect_anthropic_credential_type()` - thin facade over `classify_key`, kept so its long-standing callers do not churn. Returns the same `(env_var_name, key_type_description)` tuple it always did

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
- `frontend/src/components/UserSettings/AnthropicCredentialsModal.tsx` - Setup guide modal (API Key setup article + "Claude Code OAuth Tokens" article explaining why they are not recommended)
- `frontend/src/components/UserSettings/AICredentials/AnthropicOAuthTokenWarning.tsx` - Inline warning shown under the API key field when the key is an OAuth token
- `frontend/src/components/UserSettings/AICredentials/AddAICredentialWizard.tsx` / `frontend/src/components/UserSettings/AICredentials/EditAICredentialDialog.tsx` - expiry date handling (`toDateInput`, `expiry_notification_date`) on create and edit
- `frontend/src/components/UserSettings/AICredentials/AddAICredentialWizard.tsx` - Anthropic guidance shown while adding an Anthropic credential (recommends an API key; notes OAuth tokens are also accepted)
- `frontend/src/components/UserSettings/AICredentials/EditAICredentialDialog.tsx` - Expiry date input field
- `frontend/src/components/UserSettings/AICredentials/AICredentialRow.tsx` - `describeExpiry()` drives the expiry badge (variant + fact text)
- `frontend/src/components/UserSettings/AICredentials/AICredentialRow.tsx` - Expiry badge rendering in credential rows
- `frontend/src/components/Common/RelativeTime.tsx` - Extended with `asBadge`, `icon`, `showTooltip`, `colorCode` parameters

## Detection Logic

`AnthropicAdapter.classify_key(api_key)` in `backend/app/services/ai_providers/anthropic.py`:
- `sk-ant-oat*` → OAuth token, `CLAUDE_CODE_OAUTH_TOKEN`, `"OAuth Token"`
- `sk-ant-api*` → API key, `ANTHROPIC_API_KEY`, `"API Key"`
- Empty → API key, `ANTHROPIC_API_KEY`, `"API Key (Empty)"`
- Other → API key, `ANTHROPIC_API_KEY`, `"API Key (Unknown Format)"`

`detect_anthropic_credential_type(api_key)` in `backend/app/utils.py` returns `(env_var_name, label)` from that classification, unchanged.

**Why it lives on the adapter.** The `sk-ant-oat` prefix test was previously written out independently in six places — `utils.py`, the AI-functions credential guard in `routes/users.py`, `ai_functions_service`, the model-discovery probe, the managed-credential projection and the AI-credential projection — each deciding for itself what an OAuth token is. Every non-Anthropic adapter answers `is_oauth_token=False`, which is what allows callers to ask the adapter without first checking `cred.type == ANTHROPIC`; that property is pinned by a test.

## Environment Variable Generation

`environment_lifecycle.py:1293-1344` - `_generate_env_file()`:

1. Checks if Anthropic SDK is used (`uses_anthropic`)
2. If credential exists, calls `detect_anthropic_credential_type()` (which asks the Anthropic adapter)
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

`AddAICredentialWizard.tsx` / `EditAICredentialDialog.tsx`:
- A derived flag (`isOAuthToken` / `isPastedOAuthToken`) is true when the Anthropic key field starts with `sk-ant-oat`
- When it flips on: an effect calculates `today + 335 days` and fills the expiry field
- User can adjust or clear

## OAuth Token Warning

OAuth tokens (`claude setup-token`) are accepted but discouraged — the subscription login behind them can be signed out unexpectedly, breaking every cloud agent on the credential. `AnthropicOAuthTokenWarning` renders under the API key field:
- Add wizard: while the typed key starts with `sk-ant-oat`
- Edit dialog: while the typed key starts with `sk-ant-oat`, or while the field is blank and the stored credential has `is_oauth_token` (nudges replacement in place)

The warning is informational — it never disables Save. The backend does not reject OAuth tokens; detection, env-var routing and expiry auto-set are unchanged.

## Expiry Display

`credentialTypes.ts` - `describeExpiry(expiryDate)` returns `{ badge, fact, tooltip }`, rendered by `AICredentialRow.tsx`:

| Status | Days Until Expiry | Rendering |
|--------|------------------|-----------|
| Expired | < 0 | `destructive` badge "Expired" |
| Expiring soon | 0-60 | `secondary` badge "Expires today" / "Expires in N days" |
| Not expiring soon | > 60 | No badge; row fact "Expires <date>" |

The tooltip always carries the exact date (and days remaining). `isExpiryUrgent()` (expired or ≤ 30 days) ranks urgent credentials right after the default one on the card.

## Setup Guide Modal

`AnthropicCredentialsModal.tsx`:
- Encyclopedia-style layout (pattern from `GettingStartedModal.tsx`)
- Violet accent colors, dark mode support, 800px max width
- Article 1: "Setup via API Keys" - link to console.anthropic.com, the recommended path
- Article 2: "Claude Code OAuth Tokens" - why `claude setup-token` tokens are not recommended, and how to switch an existing credential to an API key

Opened from the ⋯ menu of `AICredentialsCard.tsx` (never from inside the Add wizard).

---

*Last updated: 2026-09-16*
