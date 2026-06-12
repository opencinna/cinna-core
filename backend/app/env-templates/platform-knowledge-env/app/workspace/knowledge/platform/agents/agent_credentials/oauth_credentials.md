# OAuth Credentials

## Purpose

Users grant Google service credentials (Gmail, Google Drive, Google Calendar) to agents through an OAuth flow. Instead of manually copying access tokens, users click "Grant from Google", complete authorization, and tokens are automatically stored, encrypted, refreshed, and synced to agent environments.

## Supported OAuth Types & Scopes

| Type | Service | Access | Scopes |
|------|---------|--------|--------|
| `gmail_oauth` | Gmail | Full | `gmail.modify`, `gmail.send` |
| `gmail_oauth_readonly` | Gmail | Read-only | `gmail.readonly` |
| `gdrive_oauth` | Google Drive | Full | `drive` |
| `gdrive_oauth_readonly` | Google Drive | Read-only | `drive.readonly` |
| `gcalendar_oauth` | Google Calendar | Full | `calendar` |
| `gcalendar_oauth_readonly` | Google Calendar | Read-only | `calendar.readonly` |

All scopes are under the `https://www.googleapis.com/auth/` prefix. Read-only variants provide safer access for monitoring/analysis agents.

## User Flows

### Granting OAuth Credentials

1. User creates a new credential, selects an OAuth type (e.g., `gmail_oauth`)
2. Form shows name, notes fields, and a "Grant from Google" button (no manual token fields)
3. User clicks "Grant from Google"
4. Backend generates authorization URL with appropriate scopes + CSRF state token
5. User is redirected to Google authorization screen
6. User grants permissions on Google's consent page
7. Google redirects back with authorization code
8. Backend exchanges code for access + refresh tokens via Google API
9. Tokens + metadata (email, name, scopes, timestamps) stored encrypted in credential
10. User redirected to credential detail page showing OAuth metadata

### Re-Authorization

1. User views an existing OAuth credential
2. UI shows metadata: granted email, scopes, token expiration
3. User clicks "Re-authorize with Google" to refresh permissions
4. Same OAuth flow as initial grant, updating existing credential record
5. Updated tokens auto-synced to all linked running agent environments

### Token Status Indicators

- **Active** - Token is valid and not expiring soon
- **Expiring soon** - Token expires within 10 minutes (auto-refreshed before streams)
- **Expired** - Token has expired (will be refreshed on next stream or manually)

## OAuth Credential Data Structure

Fields stored in `Credential.encrypted_data`:

- `access_token` - Short-lived token for API calls (~1 hour lifetime)
- `refresh_token` - Long-lived token to obtain new access tokens (persists until revoked)
- `token_type` - Always "Bearer" for Google OAuth
- `expires_at` - Unix timestamp when access token expires
- `scope` - Space-separated list of granted OAuth scopes
- `granted_user_email` - Google account email (for user reference)
- `granted_user_name` - Google account display name (for user reference)
- `granted_at` - Unix timestamp when credential was initially granted

### Fields Exposed to Agent Environment (via whitelisting)

Only these fields reach the agent container in `credentials.json`:
- `access_token`, `token_type`, `expires_at`, `scope`, `granted_user_email`, `granted_user_name`

Excluded fields (backend-only):
- `refresh_token` - Backend handles token refresh transparently
- `client_secret` - Never leaves the backend server
- `granted_at` - Not needed by agent scripts

## Token Refresh Lifecycle

### Pre-Stream Refresh (Synchronous)

Before each agent stream starts:
1. System checks all OAuth credentials linked to the agent
2. Tokens expiring within 10 minutes (600 seconds) are refreshed
3. Refresh uses stored `refresh_token` to get a new `access_token` from Google
4. Updated credential synced to agent environment
5. Stream starts with guaranteed-valid tokens

This ensures agents always have valid tokens for the expected stream duration.

### Event-Driven Refresh

When a credential is updated (including after token refresh):
- Existing `event_credential_updated()` handler fires
- Updated credential auto-synced to all linked running environments
- Agents receive fresh tokens without interruption

### Refresh Error Handling

- Refresh failures are logged but don't block streaming (graceful degradation)
- Revoked refresh tokens result in logged errors - user must re-authorize
- Failed refresh doesn't affect other credentials or environments

## Security

### CSRF Protection
- State tokens generated for each OAuth flow initiation
- Stored in-memory with 10-minute expiration
- Include credential type and user ID context
- Callback validates state token ownership before saving tokens

### Authorization Rules
- Only credential owner can initiate OAuth flow
- Only credential owner can view OAuth metadata (email, scopes)
- OAuth callback validates state token ownership before saving

### Token Exposure Control
- Refresh tokens never exposed in API responses or to frontend
- Access tokens only sent to agent environments (never to frontend)
- All tokens encrypted at rest using Fernet encryption
- Sensitive values redacted in agent prompt README

## Integration

- Builds on existing credential storage, encryption, and agent environment sync infrastructure
- Uses same Google OAuth configuration as user authentication (`GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`)
- No new environment variables needed - reuses existing Google Cloud Console OAuth app
- Google Cloud Console requires additional redirect URI for credential OAuth callback
- All 6 OAuth types use unified UI component and identical backend flow
