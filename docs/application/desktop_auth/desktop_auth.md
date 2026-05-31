# Desktop App Authentication

## Overview

Server-side OAuth 2.0 + PKCE infrastructure that allows the Cinna Desktop application to authenticate against any Cinna instance — self-hosted or cloud (opencinna.io). The desktop app acts as a public OAuth client: it opens the user's browser for login, which navigates to a consent page in the SPA. The user approves or denies there, and the desktop app receives an authorization code via a local callback, which it exchanges for an access token and refresh token.

The flow uses a **consent-page pattern** (mirroring the MCP OAuth flow) so that the `/authorize` endpoint works correctly behind nginx that only proxies `/api`, `/mcp`, and `/.well-known/*`. Because the JWT lives in localStorage, it cannot be sent in a browser navigation. Instead, the public `/authorize` endpoint stores the request by nonce and redirects to the SPA consent page, which uses its localStorage JWT to call the authenticated `/consent` endpoint.

## Core Capabilities

- **Instance discovery** — User provides a domain (e.g. `my-company.cinna.io`) or selects "Cloud" (`opencinna.io`); desktop app validates the instance via `/.well-known/cinna-desktop`
- **Browser-based consent flow** — Standard OAuth 2.0 authorization code flow with PKCE (RFC 7636 + RFC 8252), routed through a frontend consent page
- **Lazy client registration** — Desktop apps do not need to pre-register; a new `DesktopOAuthClient` is created automatically on first consent approval
- **Token pair** — Short-lived access token (15 min) + long-lived refresh token (30 days)
- **Silent refresh** — Desktop app renews access tokens without user interaction
- **Multi-instance** — User can be logged into multiple instances simultaneously
- **Per-device session management** — Each desktop installation registers as a separate client; revoke individual devices from Settings
- **User profile lookup** — Desktop apps can call a dedicated `/userinfo` endpoint to display the signed-in user's email and full name after a successful token exchange

## User Flows

### Connecting Cinna Desktop (first time — lazy registration)

1. User opens Cinna Desktop, enters instance URL or selects "Cloud"
2. Desktop fetches `/.well-known/cinna-desktop` to validate the instance and get metadata (`authorization_endpoint`, `token_endpoint`, `userinfo_endpoint` — RFC 8414)
3. Desktop generates PKCE verifier and challenge, opens the browser to `/api/v1/desktop-auth/authorize?device_name=...&code_challenge=...&state=...&redirect_uri=...`
4. The backend (public endpoint, no auth required) stores a pending consent request keyed by a random nonce, then redirects the browser to `{FRONTEND_HOST}/desktop-auth/consent?request={nonce}`
5. The SPA consent page loads, fetches display metadata (`GET /requests/{nonce}`), and shows the user a card: "Allow **{device_name}** to sign in as **{email}**?"
6. The consent route validates the local JWT in `beforeLoad` (not just its presence) — unauthenticated or expired-token visitors are redirected to `/login?redirect=/desktop-auth/consent?request={nonce}` and bounced back here after re-authenticating. A token that expires while the user sits on the consent screen is handled the same way via the mutation's `onError`. See [Expired-Session Recovery on Consent Pages](../auth/auth.md#expired-session-recovery-on-consent-pages)
7. User clicks **Approve**; the SPA calls `POST /consent` with its localStorage JWT
8. The backend lazily creates a new `DesktopOAuthClient` for this device, issues an authorization code, and returns `{redirect_to: "http://localhost:{port}/callback?code=...&state=...&client_id=..."}` — `client_id` is included so lazy-registered clients learn their server-assigned id before calling `/token`
9. The SPA navigates the browser to `redirect_to`; the desktop app's ephemeral local HTTP server captures the code and the `client_id`
10. Desktop exchanges the authorization code + PKCE verifier for tokens via `POST /token` (using the `client_id` from the callback)
11. The token response also includes `client_id` — the desktop app stores it for future refresh calls
12. Desktop calls `GET /api/v1/desktop-auth/userinfo` with the new access token to fetch the user's email and name
13. Desktop displays "Connected to {instance_name}" with user info

### Reconnecting with an Existing Client

If the desktop app already has a `client_id` from a previous registration:

1. Desktop opens browser to `/api/v1/desktop-auth/authorize?client_id={client_id}&code_challenge=...&state=...&redirect_uri=...`
2. Backend verifies the client exists and is not revoked, stores a pending consent request, redirects to consent page
3. Consent page shows "Allow {device_name} (from stored client metadata) to sign in..."
4. User approves → existing client is reused (no new `DesktopOAuthClient` created)
5. Flow continues from step 9 above

### Token Refresh (silent)

Desktop app silently refreshes the access token before it expires:
1. POST to `/api/v1/desktop-auth/token` with `grant_type=refresh_token`, `client_id`, and the stored refresh token
2. Backend validates the token, revokes it, and returns a new access + refresh token pair (rotation)
3. Token response includes `client_id` (consistent with code exchange response)
4. Desktop stores the new tokens; user is uninterrupted

### Disconnecting from Settings

1. User navigates to **Settings > Security > Desktop Sessions**
2. Sees a list of all connected desktop apps (device name, platform, last used time)
3. Clicks "Disconnect" on a device and confirms in the dialog
4. Backend revokes the client and all its refresh tokens
5. The next API call from that desktop app — even with a still-valid (unexpired) access token — is rejected with `401 Desktop session has been revoked`, because `get_current_user` checks `DesktopOAuthClient.is_revoked` on every request whose JWT carries `client_kind="desktop"`
6. The refresh endpoint also rejects the refresh token with `invalid_grant`
7. Desktop app shows "Session expired, please log in again"

## Security Model

### PKCE (Proof Key for Code Exchange)

Desktop apps are public clients — they cannot store a client secret. PKCE prevents authorization code interception:
- Desktop generates a random `code_verifier` (43–128 chars, URL-safe)
- `code_challenge = BASE64URL(SHA256(code_verifier))` sent in the authorize request
- `code_verifier` sent in the token exchange; backend verifies the match
- Prevents a malicious app on the same machine from stealing the authorization code

### Token Storage

| Token | Lifetime | Desktop storage | Backend storage |
|-------|----------|-----------------|-----------------|
| Access token | 15 minutes | In-memory only | Not stored (stateless JWT) |
| Refresh token | 30 days | OS keychain (encrypted) | SHA-256 hash in DB |
| Authorization code | 5 minutes | Ephemeral (captured once) | SHA-256 hash in DB |
| Consent nonce | 5 minutes | In browser URL only | SHA-256 hash in DB |

### Refresh Token Rotation

- Every refresh token use issues a new refresh token and invalidates the old one
- Tokens share a `token_family` UUID for rotation chain tracking
- **Replay detection**: if a revoked token in a family is reused (stolen token scenario), the entire family is immediately revoked, forcing re-authentication
- Follows OAuth 2.0 Security Best Current Practice (RFC 9700)

### Redirect URI Validation

Three native-client redirect forms are accepted, all per RFC 8252 (native-app OAuth BCP):

- **Desktop loopback** (RFC 8252 §7.3) — `http://localhost:{port}{path}` or `http://127.0.0.1:{port}{path}`, port in the range 1024–65535. Path is unrestricted: the security boundary is the loopback host + the desktop app's per-port binding, so only the legitimate app receives the code (the local callback route may be `/callback`, `/oauth/callback`, etc.).
- **Mobile app scheme** (RFC 8252 §7.1, private-use URI scheme) — `cinna-mobile://...`. Fixed and tied to the installed app, so it is accepted in **all environments** (this is the production mobile redirect; phones cannot run a loopback listener).
- **Expo Go dev redirect** — `exp://{dev-host}:{port}/--/oauth/callback`. The host/port are a developer's Metro server and vary per machine, so this form is accepted in **non-production only** (`settings.ENVIRONMENT != "production"`). PKCE (S256) already protects the code; gating to non-production is defense-in-depth so the broad `exp://` host pattern never reaches production.

Everything else (e.g. arbitrary `https://` URIs) is rejected with HTTP 400 `invalid_redirect_uri`, preventing open-redirect attacks. The redirect URI presented at token exchange must exactly match the one stored with the auth code.

### Consent Page Security

- The consent nonce is stored as a SHA-256 hash; the raw nonce appears only in the browser URL
- Nonces are single-use (marked used immediately on approve or deny)
- Nonces expire after 5 minutes
- The `GET /requests/{nonce}` endpoint returns only non-secret display metadata (device_name, platform, app_version) — not the code_challenge or redirect_uri
- The `POST /consent` endpoint requires a valid JWT (`CurrentUser`); an unauthenticated browser cannot approve a request

### Integration with Existing Auth

- Desktop access tokens are standard JWTs with the same structure as web session tokens, plus two extra claims: `client_kind="desktop"` and `external_client_id=<DesktopOAuthClient.id>`
- All existing API endpoints work transparently with desktop tokens (same `CurrentUser` dependency), including the dedicated `GET /desktop-auth/userinfo` profile endpoint
- On every request whose JWT carries `client_kind="desktop"`, `get_current_user` performs an indexed lookup on `DesktopOAuthClient` by `external_client_id`. If the row is missing or `is_revoked=True`, the request is rejected with `401 Desktop session has been revoked` — so a "Disconnect" from Settings takes effect immediately rather than waiting up to 15 minutes for the access token to expire
- The same lookup also stamps `last_used_at` on success (throttled to once per minute) so the Settings UI reflects recent activity
- Google OAuth users (no password) can authenticate via the browser-based authorize flow

### 2FA and Desktop Auth

The desktop OAuth flow reuses the browser session: the user logs in via their browser (step 4 above), and any 2FA challenge required by the platform is satisfied during that browser login before the consent page is reached. The desktop-auth flow itself adds no extra MFA step and issues no separate challenge. As a result, a user who has 2FA enabled on their account will complete the second factor in the browser as part of normal login; the desktop app then receives a short-lived access token scoped to that already-MFA-verified session.

See [Two-Factor Authentication](../user_2fa/user_2fa.md) for full 2FA details.

## Infrastructure Requirements

The `/.well-known/cinna-desktop` discovery endpoint must reach the backend through the reverse proxy. Without it, the desktop app cannot validate the instance before login. See [Nginx Setup](../../infrastructure/nginx_setup.md) for the required location block and how it fits alongside the other origin-root well-known URIs.

## Settings UI

**Settings > Security > Desktop Sessions card** shows:
- List of connected desktop apps with device name, platform icon, app version badge, and last-used time
- "Disconnect" button (destructive, requires confirmation) to revoke a specific device
- Empty state with download prompt when no devices are connected

Note: There is no separate "Register" button in the UI — clients are created automatically during the first consent flow from Cinna Desktop.
