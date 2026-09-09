---
feature: desktop_auth
domain: application
one_liner: "OAuth 2.0 with PKCE authentication for Cinna Desktop and Cinna Mobile, covering token rotation, per-device revocation, and a consent-free grant linked from an existing CLI account token."
docs:
  tech: desktop_auth_tech.md
---
# Desktop App Authentication

## Overview

Server-side OAuth 2.0 + PKCE infrastructure that allows the Cinna Desktop application to authenticate against any Cinna instance — self-hosted or cloud (opencinna.io). The desktop app acts as a public OAuth client: it opens the user's browser for login, which navigates to a consent page in the SPA. The user approves or denies there, and the desktop app receives an authorization code via a local callback, which it exchanges for an access token and refresh token.

The flow uses a **consent-page pattern** (mirroring the MCP OAuth flow) so that the `/authorize` endpoint works correctly behind nginx that only proxies `/api`, `/mcp`, and `/.well-known/*`. Because the JWT lives in localStorage, it cannot be sent in a browser navigation. Instead, the public `/authorize` endpoint stores the request by nonce and redirects to the SPA consent page, which uses its localStorage JWT to call the authenticated `/consent` endpoint.

### Mobile clients — the parallel `/app-auth` surface

Cinna Mobile uses the **same flow** through a parallel route namespace mounted at `/api/v1/app-auth/*` (discovered via `/.well-known/cinna-app`), with its own consent page at `/app-auth/consent`. This surface is a thin namespace over the **same backing service, storage tables, and token logic** as `/desktop-auth` — it is not an independent stack. The only differences are the URL prefix and the consent-page redirect target; a consent request, client, code, or token is interchangeable across the two surfaces. Desktop clients keep using `/desktop-auth` unchanged. The mobile app's redirect URIs are native private-use schemes rather than loopback (see [Redirect URI Validation](#redirect-uri-validation)), and the consent screen reflects the client kind ("Cinna Mobile" vs "Cinna Desktop") via the backend-derived `client_kind`. Gated by `APP_AUTH_ENABLED`.

## Core Capabilities

- **Instance discovery** — User provides a domain (e.g. `my-company.cinna.io`) or selects "Cloud" (`opencinna.io`); the client validates the instance via `/.well-known/cinna-desktop` (desktop) or `/.well-known/cinna-app` (mobile). The desktop document optionally also carries a `local_dev` block — see [Desktop One-Click Onboarding](../desktop_onboarding/desktop_onboarding.md)
- **Browser-based consent flow** — Standard OAuth 2.0 authorization code flow with PKCE (RFC 7636 + RFC 8252), routed through a frontend consent page
- **Parallel mobile surface** — Cinna Mobile authenticates through `/app-auth/*` (mirror of `/desktop-auth/*`) backed by the same service and storage; only the URL namespace and native redirect schemes differ
- **Client-kind-aware consent** — The consent screen renders "Cinna Mobile" vs "Cinna Desktop" copy/icon based on the `client_kind` the backend derives from the redirect URI scheme
- **Lazy client registration** — Native apps do not need to pre-register; a new `DesktopOAuthClient` is created automatically on first consent approval
- **CLI account-token exchange (silent link)** — A desktop that finds a CLI `account.json` in a workshop tree can trade that account token for a desktop session at `POST /api/v1/cli/account/desktop-token`, with no browser consent. The resulting client is stamped `origin="cli_exchange"` so it stays distinguishable in the App Sessions list
- **Token pair** — Short-lived access token (15 min) + long-lived refresh token (30 days)
- **Silent refresh** — Desktop app renews access tokens without user interaction
- **Multi-instance** — User can be logged into multiple instances simultaneously
- **Per-device session management** — Each desktop or mobile installation registers as a separate client; revoke individual devices from Settings. Because both surfaces share the `DesktopOAuthClient` table, the Settings card lists desktop and mobile clients together
- **User profile lookup** — Desktop apps can call a dedicated `/userinfo` endpoint to display the signed-in user's email and full name after a successful token exchange

## User Flows

### Connecting Cinna Desktop (first time — lazy registration)

1. User opens Cinna Desktop, enters instance URL or selects "Cloud"
2. Desktop fetches `/.well-known/cinna-desktop` to validate the instance and get metadata (`authorization_endpoint`, `token_endpoint`, `userinfo_endpoint` — RFC 8414), plus the optional `local_dev` block when this instance advertises the cinna-cli account-workspace bootstrap (see [Desktop One-Click Onboarding](../desktop_onboarding/desktop_onboarding.md))
3. Desktop generates PKCE verifier and challenge, opens the browser to `/api/v1/desktop-auth/authorize?device_name=...&code_challenge=...&state=...&redirect_uri=...`
4. The backend (public endpoint, no auth required) stores a pending consent request keyed by a random nonce, then redirects the browser to `{FRONTEND_HOST}/desktop-auth/consent?request={nonce}`
5. The SPA consent page loads, fetches display metadata (`GET /requests/{nonce}`), and shows the user a card: "Allow **{device_name}** to sign in as **{email}**?"
6. The consent route validates the local JWT in `beforeLoad` (not just its presence) — unauthenticated or expired-token visitors are redirected to `/login?redirect=/desktop-auth/consent?request={nonce}` and bounced back here after re-authenticating. A token that expires while the user sits on the consent screen is handled the same way via the mutation's `onError`. See [Expired-Session Recovery on Consent Pages](../auth/auth.md#expired-session-recovery-on-consent-pages)
7. User clicks **Approve**; the SPA calls `POST /consent` with its localStorage JWT
8. The backend lazily creates a new `DesktopOAuthClient` for this device, issues an authorization code, and returns `{redirect_to: "http://localhost:{port}/callback?code=...&state=...&client_id=..."}` — `client_id` is included so lazy-registered clients learn their server-assigned id before calling `/token`
9. The SPA navigates the browser to `redirect_to`; the desktop app's ephemeral local HTTP server captures the code and the `client_id`
10. Desktop exchanges the authorization code + PKCE verifier for tokens via `POST /token` (using the `client_id` from the callback)
11. The token response also includes `client_id` — the desktop app stores it for future refresh calls — and `email`, the account the tokens belong to, which the app must compare with the account the user expected (see [Consent Page Security](#consent-page-security))
12. Desktop calls `GET /api/v1/desktop-auth/userinfo` with the new access token to fetch the user's email and name
13. Desktop displays "Connected to {instance_name}" with user info

### Reconnecting with an Existing Client

If the desktop app already has a `client_id` from a previous registration:

1. Desktop opens browser to `/api/v1/desktop-auth/authorize?client_id={client_id}&code_challenge=...&state=...&redirect_uri=...`
2. Backend verifies the client exists and is not revoked, stores a pending consent request, redirects to consent page
3. Consent page shows "Allow {device_name} (from stored client metadata) to sign in..."
4. User approves → existing client is reused (no new `DesktopOAuthClient` created)
5. Flow continues from step 9 above

### Linking from a CLI account token (silent link)

When Cinna Desktop opens a workshop folder that already contains a CLI
`Cloud/<host>/.cinna/account.json` — i.e. the user has already run `cinna login`
against that instance — it can link the profile without asking for credentials
again:

1. Desktop reads the account CLI token out of `account.json` (same OS user, same machine)
2. Desktop calls `POST /api/v1/cli/account/desktop-token` with that token as the bearer credential, plus its own `client_id` if it has one (otherwise `device_name` / `platform` / `app_version` for lazy registration, exactly as the consent flow accepts them)
3. Backend issues a normal desktop access + refresh pair bound to that client, and returns the account owner's `email` so the desktop can name the profile without a second round-trip
4. Desktop stores the pair and refreshes it through the ordinary `POST /desktop-auth/token` endpoint from then on — nothing about the session is special after issuance
5. The CLI token is **used, not spent**: the exchange neither consumes nor revokes it, and it can be exchanged again (e.g. by a desktop that lost its refresh token)

The session appears in **Settings > Security > App Sessions** with a **CLI link**
badge, because no human approved it in a browser. It is revoked either by
disconnecting it there or by revoking the account CLI token that bought it —
that revocation cascades. See
[CLI account-token exchange](#cli-account-token-exchange) for the security
position, and the [Account CLI Workspace](../cinna_cli_integration/account_cli_workspace.md)
docs for the account token itself.

### Token Refresh (silent)

Desktop app silently refreshes the access token before it expires:
1. POST to `/api/v1/desktop-auth/token` with `grant_type=refresh_token`, `client_id`, and the stored refresh token
2. Backend validates the token, revokes it, and returns a new access + refresh token pair (rotation)
3. Token response includes `client_id` (consistent with code exchange response)
4. Desktop stores the new tokens; user is uninterrupted

### Disconnecting from Settings

1. User navigates to **Settings > Security > App Sessions**
2. Sees the 5 most recently used connected desktop and mobile apps (device name, platform, last used time), or opens "Show all (N)" for the full list
3. Hovers the device row to reveal the disconnect icon button, clicks it, and confirms in the dialog naming the device
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
- **Reuse-grace window**: a brief grace period (60 seconds by default, configurable) covers the case where a rotation response was lost before the client could persist the new tokens — for example when iOS suspends an app mid-refresh or a network timeout fires after the server already rotated. Within the window, re-presenting the revoked token is treated as a benign lost-response retry: the server re-validates the token family, revokes any orphaned successor, and issues a fresh pair. This prevents spurious logouts in unreliable network conditions.
- **Replay detection**: outside the grace window, reusing a revoked token is treated as a genuine replay (stolen-token scenario) — the entire family is immediately revoked, forcing re-authentication. Hard revocations (Disconnect from Settings, or `revoke_token_family` triggered by theft detection) do not stamp a rotation timestamp on the revoked tokens, so those tokens can never qualify for grace re-rotation. Theft detection stays strict.
- Follows OAuth 2.0 Security Best Current Practice (RFC 9700 §4.14.2)

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
- **Any *authenticated* holder of the nonce can approve a request that names no `client_id`** (the lazy-registration flow), because such a request has no owner to bind to — a decision, not a gap: the nonce travels only in the requesting browser's URL, and there is no owner column nor will one be added. The consequence is real: the app that started the flow holds the PKCE verifier and redeems the code, so a stranger's approval hands that app a working session **for the stranger's account**. Every desktop token response therefore carries `email`, the account the tokens actually belong to. That field makes the substitution *visible*; only the client's comparison of it against the expected account prevents anything. Denying someone else's pending request burns it — the milder half of the same residual

### Integration with Existing Auth

- Desktop access tokens are standard JWTs with the same structure as web session tokens, plus two extra claims: `client_kind="desktop"` and `external_client_id=<DesktopOAuthClient.id>`
- All existing API endpoints work transparently with desktop tokens (same `CurrentUser` dependency), including the dedicated `GET /desktop-auth/userinfo` profile endpoint
- On every request whose JWT carries `client_kind="desktop"`, `get_current_user` performs an indexed lookup on `DesktopOAuthClient` by `external_client_id`. If the row is missing or `is_revoked=True`, the request is rejected with `401 Desktop session has been revoked` — so a "Disconnect" from Settings takes effect immediately rather than waiting up to 15 minutes for the access token to expire
- The same lookup also stamps `last_used_at` on success (throttled to once per minute) so the Settings UI reflects recent activity
- Google OAuth users (no password) can authenticate via the browser-based authorize flow

### CLI account-token exchange

`POST /api/v1/cli/account/desktop-token` is the one path that issues a desktop
session without a browser consent, so it is also the one that needs its scope
stated rather than assumed.

**It converts a scope-restricted credential into a broader one.** An account CLI
token reaches `/cli/account/*` and nothing else, and the account CLI's generic
API-proxy hatch additionally denies credentials, users, admin, MFA and the other
clients' auth surfaces. The desktop access token it buys is an ordinary user
JWT — every route `CurrentUser` guards, including `GET /credentials/{id}/with-data`,
which returns decrypted secret values no account-CLI route will return.

What makes that acceptable:

- **For a developer, the capabilities are mostly ones the file already carried.** An account CLI token only comes into existence under a live signed-in browser session (a developer-gated Settings card, or a `cinna login` device approval), and whoever can read the resulting file can already mint per-agent CLI tokens for every agent that user can build, and run arbitrary commands inside those environments. **The argument does not hold for a non-developer holder** — `cinna login` approval has no role gate, and minting would refuse them every agent, so for them this is a straight escalation to a full user session. That escalation was weighed and accepted, not relabelled away: it is a real increase in what a stolen `account.json` yields for that group. It is allowed on the narrower ground that the session is their *own* account, no more than signing in to the web app with their password gives them. What the exchange never does is cross to another user.
- **RULING: the exchange is ungated by role, deliberately — do not "harden" it by adding one.** This is a decision, not a gap nobody got round to closing, and it is recorded so the next editor finds the decision rather than the gap. The reasoning: **role restrictions belong in the backend's reply to a specific capability, never in gating authentication.** An agent-user develops locally all day; when they ask to publish, the backend refuses *the publish*. That is the platform answering a capability request on its merits — not the CLI, and not the desktop, deciding by role who may hold a session at all. A gate here would move the refusal to the wrong layer and still deny nothing, since the session it withheld is one the same user gets by typing their password into the web app.
- **On hijacking, the objection this ruling usually meets: a compromised session is compromised whatever its type** — a role-gated one no less than this one. The remedy is revoking it in Settings, not a narrower issuance path. Which is exactly why revoke must *actually* revoke, and what the cascade in the next bullet and the badge's honesty about its own gaps below exist for. Those are the controls this ruling leans on, so weakening either is what would make it wrong.
- **The result is visible, killable, and dies with the token that bought it.** The client is stamped `origin="cli_exchange"` and linked to the account token. Every *grant* of tokens writes that pair through one function (`_stamp_grant`) — the single-writer property is over grants, not over the columns, since `origin` is also seeded when a client row is first created — and that function also retires the client's previous grant, so a later browser consent on the same client id cannot clear the badge while leaving the CLI-minted session alive. **Retiring the previous grant is scoped to that one client and fires only when the provenance actually changes**: linking a laptop does not sign out a phone, a second desktop, or the browser, and re-consenting on the same client with the same provenance signs out nothing at all. Disconnecting from Settings revokes the refresh family and kills the access token on its next request. Revoking the account CLI token cascades into the desktop session too, and the session is refused on the surfaces that would mint a replacement CLI credential or a fresh native session (see below). It is **not** a complete remediation — the leak-response note names a credential it *can* mint that outlives every revocation control.
- **Both outcomes are audited.** `CLI_ACCOUNT_DESKTOP_TOKEN_ISSUED` on success and `CLI_ACCOUNT_DESKTOP_TOKEN_DENIED` on an authorization failure, each carrying the client id and source IP, never a token value. A refusal is classified for the log — a desktop retrying a client the user disconnected is recorded as routine, so it cannot bury the rare suspicious case.

**What is deliberately not claimed: that the exchange stays on one machine.** A
CLI account token is a bearer credential with no device binding — the machine
name recorded with it is self-reported — and the endpoint checks no IP, origin or
device. A copied `account.json` works from anywhere, and `cinna login` exists
precisely so the approving browser and the requesting machine can differ. The
single-machine story is the intended usage, never a control.

**Responding to a leaked `account.json` — what actually ends it.** The exchanged
session is a full user JWT for as long as it lives, so treat it as an account
compromise rather than a token compromise:

- Revoking the account CLI token disconnects the desktop session it bought (cascade) and any per-agent child tokens.
- The session cannot mint replacement credentials of the classes the cascade reaches: a desktop session whose client carries `cli_exchange` provenance is refused by the account-setup-token, per-agent-setup-token and device-login-approval routes, and on the **approve** action of the desktop and mobile consent endpoints — approving a consent would otherwise mint a whole new native session with clean browser provenance and no cascade link. (Deny mints nothing and stays reachable; a gate against minting must never block the safety action.) Without the gate it could mint a *fresh* account CLI token carrying no provenance link, and revoking the original would have ended nothing.
- **One credential class outlives every revocation control the product has, and it is recorded here rather than fixed.** The session is *not* refused on the platform's MCP OAuth consent: through `POST /mcp/consent/{nonce}/approve` and `POST /mcp/oauth/token` it can mint an App MCP access + refresh pair bound to the user. Reproduced by execution: after the account token is revoked, the desktop session is dead on its next request while that MCP refresh token keeps answering, for up to thirty days from issue. It appears in no list — not App Sessions, not a connector's token card, which lists direct tokens only — and the one route that revokes it, `POST /mcp/oauth/revoke`, takes the token **value** as a form field with no authentication: the thief holds exactly what it requires and the owner holds nothing it accepts. Its only teardown today is deleting the user. A control that exists but is unreachable by the person who needs it is worse than an absent one, because anyone who finds the route stops looking. Wiring these tokens into the cascade, and giving them a listing and an owner-reachable revoke, is the MCP feature's territory and is raised separately.
- **So revocation is still not complete, and the docs will not pretend otherwise.** A full user JWT can read decrypted credential values and use them off-platform after every token here is dead — nothing in this system can recall a secret that has already left it. And the gate covers the minting surfaces known to satisfy its property today; it cannot cover one added tomorrow that nobody re-derives it against. So: rotate the credentials that session could read, read App Sessions for entries nobody recognises, and — until the MCP gap is closed — assume an MCP credential may still be live.

**One gap in the badge itself.** After a browser or mobile re-consent on a
CLI-exchanged client, an access token issued by the superseded grant stays valid
until it expires (15 minutes by default) while the row already reads
`browser_consent`. For that window the badge understates what is live. Revoking
the client, or the account token behind it, closes it on the next request.

The exchange reuses `DesktopAuthService`'s issuance primitive rather than minting
its own token shape, so rotation, replay detection, family revocation and the
reuse-grace window all apply to the issued pair unchanged. It is rate-limited per
account token, since every exchange without a `client_id` registers a new client.

### 2FA and Desktop Auth

The desktop OAuth flow reuses the browser session: the user logs in via their browser (step 4 above), and any 2FA challenge required by the platform is satisfied during that browser login before the consent page is reached. The desktop-auth flow itself adds no extra MFA step and issues no separate challenge. As a result, a user who has 2FA enabled on their account will complete the second factor in the browser as part of normal login; the desktop app then receives a short-lived access token scoped to that already-MFA-verified session.

The CLI account-token exchange above is the exception worth naming: it issues a
desktop session with no browser step at all, so no 2FA challenge is presented at
exchange time. The second factor was satisfied earlier — when the user signed in
to the browser session that minted the account CLI token — and the exchange
inherits that, in the same way the CLI's own account routes do. A desktop session
linked this way is therefore no stronger than the account token on disk.

See [Two-Factor Authentication](../user_2fa/user_2fa.md) for full 2FA details.

## Infrastructure Requirements

The `/.well-known/cinna-desktop` (desktop) and `/.well-known/cinna-app` (mobile) discovery endpoints must reach the backend through the reverse proxy. Without them, the native apps cannot validate the instance before login. See [Nginx Setup](../../infrastructure/nginx_setup.md) for the required location blocks and how they fit alongside the other origin-root well-known URIs.

## Settings UI

**Settings > Security > App Sessions card** shows the **5 most recently used** connected apps (most recent `last_used_at` first, apps that have never run last), each row built from a device-icon tile, the device name, and one metadata line:
- List of connected **desktop and mobile** apps — both surfaces share the `DesktopOAuthClient` table, so the card lists every native client kind together — with device name, a platform icon tile (macOS/Windows/Linux/iOS/Android, falling back to a generic device icon), and a metadata line of up to two facts joined by "·": the app version (`v{app_version}`, omitted when unknown) and either "Last used {relative time}" or, for a session that has never run, "Connected {relative time}"
- A **CLI link** badge, with a tooltip explaining it, on any session created by the CLI account-token exchange (`origin="cli_exchange"`). Browser-consent sessions are unbadged — the badge marks the exception, not the rule, so it is not buried. The app version is a fact, not a state, so it lives in the metadata line rather than as a badge
- A **"Show all (N)"** link beneath the five rows when more than 5 apps are connected, opening a full-height sheet with every session, using the identical row
- A hover-revealed disconnect icon button on each row (ghost, turns destructive-red on hover) — the row's only action is its purpose, so it is shown inline rather than behind a menu — that opens an `AlertDialog` naming the device before revoking
- Empty state: "No apps are connected to this account yet." with a link to the Cinna Desktop download page. A failed load renders as a distinct error state (with retry), never as the empty state

Note: There is no separate "Register" button in the UI — clients are created automatically during the first consent flow from Cinna Desktop or Cinna Mobile.
