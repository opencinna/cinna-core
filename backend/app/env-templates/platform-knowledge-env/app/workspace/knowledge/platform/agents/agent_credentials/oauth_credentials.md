# OAuth Credentials

## Purpose

Users grant Google service credentials (Gmail, Google Drive, Google Calendar, Google Meet) to agents through an OAuth flow. Instead of manually copying access tokens, users click "Grant from Google", complete authorization, and tokens are automatically stored, encrypted, refreshed, and synced to agent environments.

## Supported OAuth Types & Scopes

| Type | Service | Access | Scopes |
|------|---------|--------|--------|
| `gmail_oauth` | Gmail | Full | `gmail.modify`, `gmail.send` |
| `gmail_oauth_readonly` | Gmail | Read-only | `gmail.readonly` |
| `gdrive_oauth` | Google Drive | Full | `drive` |
| `gdrive_oauth_readonly` | Google Drive | Read-only | `drive.readonly` |
| `gcalendar_oauth` | Google Calendar | Full | `calendar` |
| `gcalendar_oauth_readonly` | Google Calendar | Read-only | `calendar.readonly` |
| `gmeet_oauth` | Google Meet | Full | `meetings.space.created`, `meetings.space.settings`, `meetings.space.readonly` |
| `gmeet_oauth_readonly` | Google Meet | Read-only | `meetings.space.readonly`, `drive.meet.readonly` |

All scopes are under the `https://www.googleapis.com/auth/` prefix. Every type also requests `userinfo.email`, `userinfo.profile` and `openid`. Read-only variants provide safer access for monitoring/analysis agents.

Google Meet: `gmeet_oauth` creates and manages meeting spaces (Meet REST API v2) and, through `meetings.space.readonly`, reads every space, conference record and participant the user can access; `gmeet_oauth_readonly` reads conference records, participants, recordings, transcripts and smart notes, and needs `drive.meet.readonly` (a Google *restricted* scope) to reach the files Meet stores in Drive: recordings download via Drive, while transcripts are Google Docs, so they are exported (e.g. as plain text) or read through the Meet transcript entries API. `gmeet_oauth` therefore covers the read-only type's Meet metadata, including meetings created elsewhere, but it does not carry `drive.meet.readonly`, so it cannot download recordings or transcripts: use `gmeet_oauth_readonly` for those artifacts. Space management (`spaces.create`, `spaces.patch`, ending conferences) is unchanged and stays limited to spaces created by this app. The environment README's Meet section for the full type now also shows listing conference records and participants. Meet types have no Calendar coupling: to invite attendees, create the event with a `gcalendar_oauth` credential.

### Google-side prerequisites for Meet

- Enable the **Google Meet REST API** in the Google Cloud project that owns the OAuth client.
- Add `meetings.space.created`, `meetings.space.settings`, `meetings.space.readonly` and `drive.meet.readonly` to the OAuth consent screen's scopes.
- Scope sensitivity: `drive.meet.readonly` is **restricted** (production apps need Google restricted-scope verification, possibly a security assessment); `meetings.space.created` and `meetings.space.readonly` are **sensitive**; `meetings.space.settings` is non-sensitive.
- Recordings, transcripts and smart notes are Workspace features, so they are likely unavailable for personal Gmail accounts (unconfirmed).
- Transcript entries are deleted 30 days after the meeting ends.
- Meetings with invitees go through `gcalendar_oauth`; the Meet types carry no Calendar coupling.

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
- **Expiring soon** - Token expires within 30 minutes (`OAUTH_REFRESH_THRESHOLD_SECONDS`, default 1800); the platform refreshes it automatically
- **Expired** - Token has expired (refreshed on next sync/stream/sweep, or manually)
- **Needs re-authorization** - Google rejected the refresh token (or none was stored); automatic refresh stops until the owner re-authorizes (metadata `needs_reauthorization: true`, last failure in `refresh_error`)

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
- `refresh_attempted_at`, `refresh_error`, `refresh_error_kind` (`reauth_required` | `provider_error`), `refresh_error_at` - refresh bookkeeping; written on failure, cleared on success and on re-authorization

### Fields Exposed to Agent Environment (via whitelisting)

Only these fields reach the agent container in `credentials.json`:
- `access_token`, `token_type`, `expires_at`, `scope`, `granted_user_email`, `granted_user_name`

Excluded fields (backend-only):
- `refresh_token` - Backend handles token refresh transparently
- `client_secret` - Never leaves the backend server
- `granted_at` - Not needed by agent scripts
- `refresh_*` bookkeeping fields - backend-only (the whitelist keeps them out of the container); the owner sees only `refresh_error` / `needs_reauthorization` via the metadata endpoint

## Token Refresh Lifecycle

Google access tokens live ~1 hour. Every refresh goes through one backend primitive (`OAuthRefreshService.refresh_if_needed`), so triggers never race each other. A refresh is attempted only when the token has less than the threshold left (default **30 minutes**, setting `OAUTH_REFRESH_THRESHOLD_SECONDS`, valid 60-2400).

### Triggers

1. **On sync** - environment start and every credential push to running environments (`sync_credentials_to_agent_environments`) refresh expiring tokens first, then push.
2. **Per turn, every stream path** - refreshed in `SessionStreamProcessor` right after the stream starts, so UI chat, channel follow-up turns, A2A and MCP turns all get it. If anything was refreshed, the credentials are pushed to the running environment.
3. **Background sweep** - every 10 minutes (`OAUTH_REFRESH_SWEEP_INTERVAL_MINUTES`) the leader worker refreshes expiring tokens of credentials linked to agents with a running environment and pushes them, so idle environments do not hold an expired token. Also covers `mcp_provider` `oauth_dcr` credentials.
4. **On demand from the container** - a script calls `credentials.access_token(id)` (see below) and the platform refreshes if needed.
5. **Owner's manual refresh** - `POST /credentials/{id}/oauth/refresh` forces a refresh and bypasses the failure state below.

With the 30-minute threshold and 10-minute sweep, a token in a running environment stays valid for at least ~20 minutes.

### Concurrency

A refresh takes a per-credential in-process lock plus a Postgres advisory lock (non-blocking try-lock, re-read after acquiring), so concurrent triggers on one or many workers refresh once. If another worker holds the lock for ~5 s the caller proceeds with the current token. If the owner re-authorizes while a refresh is in flight, the stale refresh result is dropped.

### Failure States

- **`reauth_required`** - no refresh token, or Google answered `invalid_grant` / `unauthorized_client`. Automatic attempts (sync, stream, sweep, container endpoint) stop until the owner re-authorizes or manually refreshes. Re-authorization (the OAuth callback rebuilds the stored data) clears the error.
- **`provider_error`** - Google 5xx, timeout or network failure. Retried after a 60 s cooldown (`OAUTH_REFRESH_COOLDOWN_SECONDS`).
- Failures never block streaming or syncing: the stale token is pushed as-is and the error is recorded on the credential.
- Known gap: `mcp_provider` `oauth_dcr` refresh has no failure cooldown; a failing one is retried on every sync, start and stream.

### Agent-Facing Access Token

Scripts that run longer than a token lifetime should call the SDK instead of reading `credentials.json` once:

```python
from core.cinna_api import credentials
token = credentials.access_token(credential_id, min_ttl=300)
```

It returns the synced token when it has more than `min_ttl` seconds left (no network), otherwise asks the platform (`POST /api/v1/agent/credentials/{id}/access-token`, scoped environment token), which refreshes if needed. Only credentials linked to the calling agent and of a Google OAuth type are served. Failures raise `CredentialRefreshError` with a `code` (`credential_not_linked`, `not_refreshable`, `reauthorization_required`, `provider_error`, `refresh_in_progress`, `oauth_not_configured`, `unauthorized`, `unavailable`); relay `str(exc)` to the user. If the platform is unreachable but the synced token is still valid, that token is returned; `unauthorized` (HTTP 401/403) means the environment should be restarted or rebuilt, and has no local fallback. The generated credentials README tells agents that tokens are checked before every turn, on env start and about every 10 minutes, and that an `AttributeError` on `credentials.access_token` means the environment needs a rebuild (use the HTTP fallback).

**Rebuild caveat:** the SDK lives in the per-environment copy of the core, so it exists only in environments created or rebuilt after this feature. Older environments can call the same endpoint with `curl` (documented in the generated credentials README); the backend-side refresh triggers work without a rebuild.

Technical details: [OAuth Credentials Tech](oauth_credentials_tech.md).

## Security

### CSRF Protection
- State tokens generated for each OAuth flow initiation
- Stored in the `credential_oauth_state` table with a 10-minute expiration, only as a SHA-256 hash
- Row carries credential id, user id and origin (`web` | `desktop`)
- Single-use: the callback deletes the row (committed) before talking to Google, so a failed
  exchange cannot be replayed; expired rows are purged when a new flow starts
- Callback validates state token ownership before saving tokens

### Authorization Rules
- Only credential owner can initiate OAuth flow
- Only credential owner can view OAuth metadata (email, scopes)
- OAuth callback validates state token ownership before saving

### Desktop-started authorization

Cinna Desktop starts a flow through its own endpoints (see
[Desktop Credential Delivery](../../application/desktop_credentials/desktop_credentials.md)); the
state carries a `desktop.` prefix and `origin="desktop"`, and the user finishes consent in a browser
that normally has no Cinna session, so the public callback cannot rely on a login.

- **Session binding.** If the callback request carries a valid Cinna session, it must be the state's
  user, else 403 with a generic message and the state is burned (a phishing link dies on first use).
  A sessionless browser is accepted.
- **Masked owner on the page.** The callback response includes `owner_email_masked`
  (`ev***@example.com`) and the page shows it. This mitigates consent phishing: an attacker starts a
  flow for their own credential and sends the link to a victim, hoping the victim's Google grant is
  stored in the attacker's credential. The victim sees an account that is not theirs and can stop.
- **503 vs 501.** Desktop endpoints answer 503 `oauth_not_configured` when Google OAuth is not
  configured; the web `authorize` endpoint answers 501.

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
- All 8 OAuth types use unified UI component and identical backend flow

## Changelog

- OAuth refresh hardening: single locked refresh primitive, 30-minute threshold, refresh on sync / every stream path / background sweep, recorded failure state with re-authorization, container `credentials.access_token()` and endpoint.
- Authorization state moved from an in-process dict to the hashed, single-use `credential_oauth_state` table (works across workers and restarts); callback session binding and masked owner email; Desktop-started flows.
