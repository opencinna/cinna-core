# Desktop App Authentication — Technical Details

## File Locations

### Backend — Models

- `backend/app/models/desktop_auth/__init__.py` — Re-exports all desktop auth models
- `backend/app/models/desktop_auth/desktop_oauth_client.py` — DesktopOAuthClient (table), DesktopOAuthClientCreate, DesktopOAuthClientPublic; also the `CLIENT_ORIGIN_BROWSER_CONSENT` / `CLIENT_ORIGIN_CLI_EXCHANGE` constants that populate the `origin` column
- `backend/app/models/desktop_auth/desktop_refresh_token.py` — DesktopRefreshToken (table)
- `backend/app/models/desktop_auth/desktop_auth_code.py` — DesktopAuthCode (table)
- `backend/app/models/desktop_auth/desktop_auth_request.py` — DesktopAuthRequest (table) — pending consent requests

### Backend — Routes

- `backend/app/api/routes/desktop_auth.py` — All OAuth endpoints under `/desktop-auth` prefix; also defines the shared request/response models (`ConsentRequest`, `ConsentResponse`, `TokenRequest`, `TokenResponse`, `UserInfoResponse`, `RevokeRequest`) and the `_parse_token_request` helper reused by the app surface
- `backend/app/api/routes/app_auth.py` — **Parallel mobile surface** under `/app-auth` prefix (tag `app-auth`). Mirrors every desktop endpoint but delegates to the same `DesktopAuthService` and reuses the desktop route's shared models/helpers; the only behavioural difference is that `authorize` redirects to `/app-auth/consent`. No new tables — writes to the same `desktop_*` store.
- `backend/app/main.py` — `/.well-known/cinna-desktop` and `/.well-known/cinna-app` discovery endpoints registered at app level (not under `/api/v1`)
- `backend/app/api/routes/cli.py` — `POST /cli/account/desktop-token` lives here, not on the desktop-auth surface: it is authenticated by the account CLI token (`AccountCLIContextDep`), so it belongs to the CLI router. It writes to the same `desktop_*` tables through `DesktopAuthService`

### Backend — Services

- `backend/app/services/desktop_auth/desktop_auth_service.py` — DesktopAuthService: consent flow, client management, authorization code, token exchange, CLI-account-token exchange, refresh rotation, revocation, cleanup
- `backend/app/services/cli/account_cli_service.py` — `AccountCLIService.exchange_for_desktop_token()`: the account-CLI half of the exchange (authorization decision, audit, email in the response). Carries the written security position on converting an account CLI token into a user session
- `backend/app/services/desktop_auth/desktop_auth_crypto.py` — Crypto helpers: ID/token generation, SHA-256 hashing, PKCE S256 verification
- `backend/app/services/desktop_auth/desktop_auth_scheduler.py` — Background cleanup scheduler (every 15 minutes)

### Backend — Configuration

- `backend/app/core/config.py` — `DESKTOP_AUTH_ENABLED`, `DESKTOP_ACCESS_TOKEN_EXPIRE_MINUTES`, `DESKTOP_REFRESH_TOKEN_EXPIRE_DAYS`, `DESKTOP_REFRESH_TOKEN_REUSE_GRACE_SECONDS` (default 60; controls the rotation reuse-grace window), `APP_AUTH_ENABLED` (mobile surface toggle; token lifetimes are shared with desktop)

### Backend — Migrations

- `backend/app/alembic/versions/d3e4f5a6b7c8_add_desktop_auth_tables.py` — Creates desktop_oauth_client, desktop_refresh_token, desktop_auth_code tables
- `backend/app/alembic/versions/d7e34bcff709_add_desktop_auth_request_table.py` — Adds desktop_auth_request table (consent-page nonce store)
- `backend/app/alembic/versions/e8f1a2b3c4d5_add_revoked_at_to_desktop_refresh_token.py` — Adds nullable `revoked_at` column to desktop_refresh_token (down_revision: ab55mcpprovider01); legacy NULL rows fall through to genuine-replay behaviour
- `backend/app/alembic/versions/21729ab9b43d_add_origin_to_desktop_oauth_client.py` — Adds `origin` to desktop_oauth_client with `server_default='browser_consent'` (down_revision: 7de57f5d4b8f). Every pre-existing row predates the exchange endpoint, so the backfill is true by construction
- `backend/app/alembic/versions/b4c1d7e93f28_add_account_token_provenance_to_desktop_client.py` — Adds nullable `minted_by_account_token_id` + index + `SET NULL` FK to cli_token (down_revision: 21729ab9b43d). No backfill: every existing row was browser-granted, for which NULL is the true value, not a missing one
- `backend/app/alembic/versions/c9a2f5b1d604_make_desktop_client_origin_explicit.py` — Backfills `origin` and **drops its server default** (down_revision: b4c1d7e93f28). The default was doing two jobs and only one was wanted: giving pre-existing rows a true value (kept, as a one-time UPDATE) and silently supplying a value for future inserts (removed). Note that dropping the *Python-side* default alone achieves nothing — SQLModel does not validate `table=True` models, so the attribute is `None` and the server default fills it in; the DB constraint is what makes omission loud

### Backend — Tests

- `backend/tests/api/desktop_auth/test_desktop_auth.py` — Scenario-based integration tests covering the full consent flow, redirect-URI validation (incl. native mobile schemes + env gating), and `client_kind` metadata; also `test_token_response_email_reveals_a_substituted_account`, the committed reproduction of a stranger approving a lazy-registration consent, and `test_consent_lazy_registration_request_has_no_owner_to_bind_to`, which pins that residual as a decision
- `backend/tests/api/app_auth/test_app_auth.py` — Parallel-surface tests: `/.well-known/cinna-app` discovery, full mobile PKCE flow, `client_kind="mobile"` metadata, redirect validation, refresh rotation, and cross-surface token interoperability (app token works on `/desktop-auth/userinfo`)
- `backend/tests/utils/desktop_auth.py` — Test helpers: PKCE pair generation, consent flow steps, token exchange
- `backend/tests/architecture/credential_minting_gate_test.py` — Structural test that **derives** the set of routes that must carry the credential-minting gate (from what the account-token cascade revokes and what auth-free routes redeem into it) and asserts each carries it in either shape — route dependency, or an in-handler call under `body.action != "deny"`. Goes red on a new ungated minting route; see its module docstring for the classes it cannot see
- `backend/tests/api/cli/test_account_desktop_token.py` — Scenario tests for the CLI account-token exchange: lifecycle, client binding + origin laundering, auth matrix, audit events — and, unlike every other item in this list, one scenario asserting an **absence**: `test_desktop_token_exchange_is_not_developer_gated` demotes the user to `agent-user` (checking the demotion took) and asserts the exchange still succeeds. The endpoint is not role-gated, deliberately — see the ruling in `AccountCLIService.exchange_for_desktop_token`'s security position. Also the gate's action granularity (`test_cli_exchanged_session_may_deny_the_consent_it_may_not_approve`: approve 403, deny 200 on the same pending request, both surfaces) and the blast radius of the supersede-revoke (`test_supersession_does_not_sign_out_the_users_other_desktops`: two clients, the revoke fires on one and stops there)

### Frontend

- `frontend/src/components/Auth/NativeAuthConsentPage.tsx` — Shared consent screen component for both native surfaces; parameterized by the service endpoints (`getRequest`/`submitConsent`) and renders "Cinna Mobile" vs "Cinna Desktop" copy from `client_kind`
- `frontend/src/routes/desktop-auth/consent.tsx` — Public SPA consent page at `/desktop-auth/consent?request={nonce}` (thin wrapper around `NativeAuthConsentPage` wired to `DesktopAuthService`)
- `frontend/src/routes/app-auth/consent.tsx` — Public SPA consent page at `/app-auth/consent?request={nonce}` (wrapper wired to `AppAuthService`)
- `frontend/src/components/UserSettings/DesktopSessionsCard.tsx` — "App Sessions" card: connected devices list (desktop + mobile, since both client kinds share the `desktop_oauth_client` table) + disconnect dialog. Platform icon maps macOS/Windows/Linux/iOS/Android; disconnect is a ghost icon button (`Unplug`)
- `frontend/src/routes/_layout/settings.tsx` — DesktopSessionsCard added to Security tab (the Local Development card was also moved here from the Channels tab)
- `frontend/src/client/sdk.gen.ts` — `DesktopAuthService` + `AppAuthService` (auto-generated)

## Database Schema

### desktop_oauth_client

| Field | Type | Constraints |
|-------|------|-------------|
| id | UUID | PK |
| client_id | VARCHAR(64) | unique, indexed (ix_desktop_oauth_client_client_id) |
| user_id | UUID | FK -> user.id CASCADE, indexed (ix_desktop_oauth_client_user_id) |
| device_name | VARCHAR(200) | not null |
| platform | VARCHAR(50) | nullable |
| app_version | VARCHAR(50) | nullable |
| is_revoked | BOOLEAN | default false |
| origin | VARCHAR(32) | not null, **no default** (see c9a2f5b1d604 — an insert that omits it raises, so a new creation path cannot be handed the reassuring value for free); `'cli_exchange'` when the client's most recent grant came from `POST /cli/account/desktop-token`. Provenance, not display text — never accepted from a request body |
| minted_by_account_token_id | UUID | nullable, indexed (ix_desktop_oauth_client_minted_by_account_token_id), FK -> cli_token.id **SET NULL**; the account CLI token behind a `cli_exchange` grant, NULL for a browser consent. Written and cleared together with `origin` by `_stamp_grant`. Not projected into `DesktopOAuthClientPublic` — it identifies a *different* credential |
| last_used_at | TIMESTAMP WITH TZ | nullable |
| created_at | TIMESTAMP WITH TZ | default now |

**`GrantProvenance`** (`models/desktop_auth/desktop_oauth_client.py`) — a frozen
dataclass pairing `origin` with `minted_by_account_token_id`. Both columns
describe one grant, so they are written and compared as one value: `__eq__` is
derived from the fields, so a provenance field added to the class necessarily
enters the comparison that decides whether a grant supersedes the previous one.
It does **not** prevent someone adding a provenance column and writing it outside
`_stamp_grant` — that exposure is the same either way.

### desktop_refresh_token

| Field | Type | Constraints |
|-------|------|-------------|
| id | UUID | PK |
| client_id | UUID | FK -> desktop_oauth_client.id CASCADE, indexed (ix_desktop_refresh_token_client_id) |
| user_id | UUID | FK -> user.id CASCADE |
| token_hash | VARCHAR | unique, indexed (ix_desktop_refresh_token_hash) |
| token_family | UUID | not null, indexed (ix_desktop_refresh_token_family) |
| is_revoked | BOOLEAN | default false |
| revoked_at | TIMESTAMP WITH TZ | nullable; stamped when a token is rotated out (normal rotation path). NULL for hard-revoked tokens (client disconnect / family revocation), so those rows can never qualify for grace re-rotation |
| expires_at | TIMESTAMP WITH TZ | not null |
| created_at | TIMESTAMP WITH TZ | default now |

### desktop_auth_code

| Field | Type | Constraints |
|-------|------|-------------|
| id | UUID | PK |
| code_hash | VARCHAR | unique, indexed (ix_desktop_auth_code_hash) |
| user_id | UUID | FK -> user.id CASCADE |
| client_id | VARCHAR(64) | not null |
| code_challenge | VARCHAR(128) | not null |
| redirect_uri | VARCHAR(255) | not null |
| is_used | BOOLEAN | default false |
| expires_at | TIMESTAMP WITH TZ | not null (5-minute TTL) |
| created_at | TIMESTAMP WITH TZ | default now |

### desktop_auth_request

| Field | Type | Constraints |
|-------|------|-------------|
| id | UUID | PK |
| nonce_hash | VARCHAR | unique, indexed (ix_desktop_auth_request_nonce_hash) |
| device_name | VARCHAR(200) | nullable |
| platform | VARCHAR(50) | nullable |
| app_version | VARCHAR(50) | nullable |
| client_id | VARCHAR(64) | nullable (null = lazy registration) |
| code_challenge | VARCHAR(128) | not null |
| redirect_uri | VARCHAR(255) | not null |
| state | VARCHAR(255) | not null |
| is_used | BOOLEAN | default false |
| expires_at | TIMESTAMP WITH TZ | not null (5-minute TTL), indexed (ix_desktop_auth_request_expires_at) |
| created_at | TIMESTAMP WITH TZ | default now |

## API Endpoints

### Discovery (root level, no auth)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/.well-known/cinna-desktop` | Instance metadata: `instance_name`, `authorization_endpoint`, `token_endpoint`, `userinfo_endpoint`, `version`, `desktop_auth_enabled` — field names follow RFC 8414 (OAuth 2.0 Authorization Server Metadata) |
| GET | `/.well-known/cinna-app` | Same shape as `cinna-desktop` but `authorization_endpoint`/`token_endpoint`/`userinfo_endpoint` point at `/api/v1/app-auth/*`, plus `app_auth_enabled`. Used by Cinna Mobile for instance discovery |

### OAuth Flow (under `/api/v1/desktop-auth`)

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/clients` | CurrentUser | List user's active clients |
| DELETE | `/clients/{client_id}` | CurrentUser | Revoke client + all its tokens |
| GET | `/authorize` | None (public) | Store consent request, redirect to SPA consent page |
| GET | `/requests/{nonce}` | None (public) | Return display metadata for a pending consent request |
| POST | `/consent` | CurrentUser; **approve is refused for a CLI-exchanged session** | Approve or deny a pending consent request; returns redirect_to URL. The credential-minting gate (`ensure_not_cli_exchanged_session`) runs **inside the handler, on the approving branch only**, keyed `!= "deny"` so an action added later is gated by default: approving mints a *new* native client with clean `browser_consent` provenance and no cascade link, which is the same self-replication shape as minting a CLI token. `action="deny"` mints nothing and stays reachable — it was briefly a route-level dependency, which refused deny as well, and that is why it is not one. The SPA calls this with the browser's localStorage JWT, so no legitimate flow presents a native token here (verified against the desktop client, which never calls it) |
| POST | `/token` | None (public) | Exchange code or refresh token for token pair (includes client_id). Accepts both `application/x-www-form-urlencoded` (OAuth 2.0 RFC 6749 §3.2) and `application/json` request bodies |
| GET | `/userinfo` | CurrentUser | Return `{sub, email, full_name, username}` for the bearer token holder |
| POST | `/revoke` | CurrentUser | Revoke client or specific refresh token |

Note: `POST /clients` (explicit client registration) has been removed. Clients are created lazily on first consent approval.

### CLI account-token exchange (under `/api/v1/cli`)

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| POST | `/cli/account/desktop-token` | Account CLI token (`AccountCLIContextDep`) | Exchange a CLI account token for a desktop access + refresh pair bound to the supplied `client_id` (lazily registering one from `device_name` / `platform` / `app_version` when absent), plus the account owner's `email`. 403 when `client_id` names a client that is revoked, unknown, or another user's — the three cases are deliberately indistinguishable |

The response body is the `/desktop-auth/token` shape plus `email`, so the desktop refreshes it through the ordinary token endpoint afterwards. See [CLI account-token exchange](desktop_auth.md#cli-account-token-exchange) for the security position and [Account CLI Workspace](../cinna_cli_integration/account_cli_workspace.md) for the token being spent.

### OAuth Flow (under `/api/v1/app-auth`)

The same eight endpoints exist under `/api/v1/app-auth` (tag `app-auth`) with identical contracts — they delegate to the same `DesktopAuthService` and share the `desktop_*` tables. The only difference: `GET /app-auth/authorize` redirects the browser to `/app-auth/consent` instead of `/desktop-auth/consent`. Because storage is shared, a token minted on either surface is accepted by either `/userinfo`, and a client registered through one is listed by the other.

## Services & Key Methods

### DesktopAuthService (`backend/app/services/desktop_auth/desktop_auth_service.py`)

All methods are `@staticmethod`:

**Client management:**
- `list_clients(session, user_id) -> list[DesktopOAuthClientPublic]` — Non-revoked clients for user
- `revoke_client(session, user_id, client_id_str) -> None` — Soft-revoke + cascade revoke all tokens
- `verify_active_or_raise(session, external_client_id) -> DesktopOAuthClient` — Used by `get_current_user` to enforce immediate revocation of desktop-issued access tokens; raises `DesktopAuthError("client_missing" | "client_invalid" | "revoked", ...)` and stamps `last_used_at` (throttled). See [Live Access Token Revocation Check](#live-access-token-revocation-check)

**Consent flow:**
- `create_auth_request(session, device_name?, platform?, app_version?, client_id?, code_challenge, redirect_uri, state) -> str` — Store pending request row; returns raw nonce
- `get_auth_request(session, nonce) -> dict | None` — Returns display metadata or None if not found/used/expired
- `process_consent(session, user_id, nonce, action) -> dict` — Returns `{"redirect_to": "..."}`. On approve: resolves or lazily creates client, issues auth code, marks nonce used. On deny: marks nonce used, returns redirect with error=access_denied.

**CLI account-token exchange:**
- `issue_tokens_for_cli_exchange(session, user_id, client_id_str?, device_name?, platform?, app_version?, account_token_id) -> dict[str, Any]` — Resolve-or-lazily-register the client, stamp the grant, mint through `_create_token_pair`. Returns the same dict shape as `exchange_code`. Makes **no authorization decision** — the caller has already authenticated; `AccountCLIService.exchange_for_desktop_token` owns the decision and the audit
- `revoke_clients_for_account_token(session, account_token_id) -> int` — The desktop half of `AccountCLIService.revoke_account_token`'s cascade: revokes every client whose *current* grant came from that account token, plus its live refresh tokens. Returns the client count. Does not commit, so the CLI and desktop halves land in one transaction
- `is_cli_exchanged_session(session, external_client_id) -> bool` — Predicate behind the credential-minting gate (`ensure_not_cli_exchanged_session` in `api/deps.py`; `forbid_cli_exchanged_desktop_session` is its `Depends` adapter, aliased `NoCliExchangedSession`). Reads the provenance `_stamp_grant` maintains, so a session since re-authorized in a browser answers False. Returns False for anything that is not a live desktop session — safe rather than fail-open, because a revoked client cannot authenticate at all
- `classify_client_rejection(session, user_id, client_id_str) -> str` — Audit-only label for a refused `client_id`: `"revoked_own"` (the caller's own disconnected client, the routine retry) vs `"unknown_or_foreign"`. Runs only on the failure path, only for the audit record; the caller-facing 403 is raised earlier and is identical either way, so this cannot widen what a caller learns
- `_stamp_grant(session, client, provenance: GrantProvenance) -> None` — Private; the **single writer of every _grant_** of the two columns, so they can never disagree about one session. Not the single writer of the columns themselves: `origin` is also seeded at row creation by `_resolve_or_register_client`, which mints no tokens and does not call this — the property to audit for is that no *grant* stamps provenance elsewhere (only `exchange_code` and `issue_tokens_for_cli_exchange` call it, and rotation carries the pair forward untouched). When the provenance actually **changes**, it also revokes the client's still-live refresh tokens (without stamping `revoked_at`, so they are ineligible for grace re-rotation) — which is what lets per-client scalars describe a per-family fact. Conditional rather than unconditional on purpose: a same-provenance re-grant shares a badge and a cascade link, so retiring it buys nothing, while an unconditional revoke would reach two shipped surfaces this feature does not otherwise touch (the browser consent flow and the mobile `/app-auth` flow) and hard-fail an in-flight refresh — on mobile, the exact scenario the reuse-grace window exists for. Does not commit
- `_resolve_or_register_client(session, user_id, client_id_str?, device_name?, platform?, app_version?, created_origin) -> DesktopOAuthClient` — Private; the lazy-registration path shared by `process_consent` and the CLI exchange, so both apply identical ownership checks (403 on missing/revoked/foreign) and identical registration semantics. Does not commit

**Token flow:**
- `create_authorization_code(session, user_id, client_id_str, code_challenge, redirect_uri) -> str` — Issue auth code for existing client (used by process_consent internally)
- `exchange_code(session, code, client_id_str, redirect_uri, code_verifier) -> dict` — Validate code + PKCE, stamp `origin="browser_consent"`, issue token pair; dict includes `client_id`
- `refresh_tokens(session, refresh_token_value, client_id_str) -> dict` — Validate + rotate, replay detection with reuse-grace window, issue new pair; dict includes `client_id`

**Revocation:**
- `revoke_token_family(session, family_id) -> None` — Revoke all tokens in a rotation chain (does NOT stamp `revoked_at`; theft-response / hard revocations use this path)
- `revoke_by_refresh_token(session, user_id, refresh_token_value) -> None` — Revoke specific token + family
- `_revoke_live_family_tokens(session, family_id, now) -> None` — Private; stamps `revoked_at = now` on every still-live token in the family. Used by the grace re-rotation path to collapse the family back to a single live token before issuing the fresh pair

**Cleanup:**
- `cleanup_expired(session) -> int` — Delete expired auth codes, expired consent requests, and old revoked/expired refresh tokens

### DesktopAuthCrypto (`backend/app/services/desktop_auth/desktop_auth_crypto.py`)

- `generate_client_id() -> str` — 32-char URL-safe random string
- `generate_auth_code() -> str` — 48-char URL-safe random string (also used as nonce)
- `generate_refresh_token() -> str` — 64-char URL-safe random string
- `hash_token(value) -> str` — SHA-256 hex digest
- `verify_pkce(verifier, challenge) -> bool` — S256 PKCE verification with constant-time comparison

## Token Response

The `TokenResponse` model (both code exchange and refresh) includes:

```json
{
  "access_token": "...",
  "refresh_token": "...",
  "token_type": "bearer",
  "expires_in": 900,
  "client_id": "abc123...",
  "email": "user@example.com"
}
```

The `client_id` field allows desktop apps using lazy registration to discover their assigned client_id after the first token exchange. Subsequent refresh calls must include this `client_id`.

`email` names the account the returned tokens belong to, and it is there for a security reason: a consent request naming no client can be approved by any authenticated holder of its nonce, while the app that redeems the code is the one that started the flow and holds the PKCE verifier — so an app can be handed a working session for an account its user did not mean to sign in to, with nothing else in the response revealing it. `DesktopAuthService._token_payload` is the single writer of this shape (code exchange, CLI exchange and refresh all go through it), so the field cannot reach some issuance paths and not others. **It makes the substitution visible; it does not prevent it** — prevention is the client comparing this address against the account it expected. Pinned by `test_token_response_email_reveals_a_substituted_account`, which reproduces the substitution rather than only the happy path.

## Frontend Consent Page

Route: `/desktop-auth/consent?request={nonce}` (file: `frontend/src/routes/desktop-auth/consent.tsx`)

- Public route (not under `_layout/`), but `beforeLoad` redirects unauthenticated users to `/login`
- On mount: `GET /requests/{nonce}` to fetch display metadata
- Renders: a "Signed in as" block with the current user's name/email (lightweight `GET /users/me` query sharing the `["currentUser"]` cache key) so the user can confirm which account they're authorizing; plus device name, platform, app version from the request metadata
- Approve button: `POST /consent` with `action="approve"` → receives `redirect_to` → `window.location.href = redirect_to`
- Deny button: `POST /consent` with `action="deny"` → receives `redirect_to` with `error=access_denied` → navigates there
- "Use another account" link (footer): calls `redirectToLoginPreservingTarget()` — clears the stored `access_token` (logout) and redirects to `/login?redirect=<this consent URL>`, so a user who authenticated as the wrong account can sign in as a different one and land back on the same consent request
- After redirect, attempts to close the browser tab (works for script-opened tabs)

## Security Notes

- Redirect URI validation (`_validate_redirect_uri`) accepts four native-client forms per RFC 8252: (1) **loopback HTTP** `http://localhost:{1024-65535}{path}` / `http://127.0.0.1:{1024-65535}{path}` — desktop, path unrestricted (§7.3); (2) **mobile app scheme** `cinna-mobile://...` plus hyphenated dev/preview variants `cinna-mobile-dev://...` (`_APP_SCHEME_RE`, regex `^cinna-mobile(-[a-z0-9]+)*://`) — private-use URI scheme (§7.1), accepted in all environments; (3) **iOS bundle scheme** `io.opencinna.ios://...` and dotted dev/staging variants `io.opencinna.ios.dev://...` (`_IOS_SCHEME_RE`, regex `^io\.opencinna\.ios(\.[a-z0-9]+)*://`) — Apple's bundle-id-as-URL-scheme convention (§7.1), accepted in all environments; (4) **Expo Go dev** `exp://{host}:{port}/...` (`_EXPO_DEV_RE`) — accepted only when `settings.ENVIRONMENT != "production"`. Anything else → HTTP 400 `invalid_redirect_uri`. The same validation runs at both `authorize` and `create_authorization_code` call sites; token exchange compares the presented `redirect_uri` against the stored one by exact string match (`auth_code.redirect_uri != redirect_uri`).
- All token values stored as SHA-256 hashes; raw values are never persisted
- Consent nonces stored as SHA-256 hashes; raw nonce appears only in the browser URL during the consent flow
- Access tokens are standard JWTs (same `create_access_token()` as web login) — `CurrentUser` dependency works unchanged, but now performs an extra `DesktopOAuthClient.is_revoked` lookup when the JWT carries `client_kind="desktop"` so disconnects propagate immediately (see [Live Access Token Revocation Check](#live-access-token-revocation-check) below)
- `GET /authorize` is now public — authentication happens at `POST /consent` via the SPA's localStorage JWT
- Replay detection with rotation reuse-grace window (RFC 9700 §4.14.2): when a revoked token is re-presented, `refresh_tokens()` checks whether `revoked_at` is set AND `now - revoked_at <= DESKTOP_REFRESH_TOKEN_REUSE_GRACE_SECONDS` (default 60 s). If within the window, the token is treated as a benign lost-rotation-response retry: the token is re-validated (must not be expired, client must be active), any still-live successor in the family is revoked via `_revoke_live_family_tokens()` (family collapses to one live token), and a fresh pair is issued from the same `token_family`. Outside the window, or when `revoked_at` is NULL (legacy rows, or tokens hard-revoked by `revoke_token_family()` / `revoke_client()`), the full-family revocation path runs and returns 400 `invalid_grant`. Hard-revocation paths deliberately do not stamp `revoked_at`, ensuring that tokens revoked for security reasons (theft detection, explicit disconnect) can never enter the grace path.
- `code_challenge_method` must be `S256`; other methods rejected with 400
- Cross-user protection: if a `client_id` is provided in the authorize request, `POST /consent` validates that the client belongs to the consenting user (HTTP 403 if not). The CLI exchange runs the identical check through the shared `_resolve_or_register_client`
- `origin` is stamped **per grant**, not at registration only: both `exchange_code` and `issue_tokens_for_cli_exchange` write it through `_stamp_grant`. Freezing it at registration would let a CLI exchange that names an already-browser-registered `client_id` produce a session indistinguishable from a browser one. Refresh rotation leaves it alone — rotating is not a new grant
- **A grant that changes the provenance supersedes the client's previous one.** `_stamp_grant` revokes the client's live refresh tokens as it writes the columns — but **only when the provenance it writes differs from the one already on the row**; a same-provenance re-grant leaves live families alone, for the reasons in the service inventory above. The revoke is also scoped to that one client row (`DesktopRefreshToken.client_id == client.id`), never to the user, so it never disturbs the user's other desktop or mobile clients. Without the supersede-revoke, a client could hold several concurrent live families (each `_create_token_pair` call without a parent mints a fresh one) while a single-valued `origin` described only the newest — so a later browser consent would clear the `cli_exchange` badge while the CLI-minted family kept working. Users read the badge in the contrapositive (*no badge ⇒ no CLI-minted session*), so that direction is the one that matters. The residue is bounded: an access token from the superseded grant lives until it expires, because access tokens are checked against the client's `is_revoked`, which a re-grant deliberately does not set
- The refusal path returns **one merged 403** for missing / revoked / foreign so a client id cannot be probed; `classify_client_rejection` distinguishes them for the audit log only
- **The ownership check in `process_consent` runs above the approve/deny branch.** Both branches consume the pending request, and before the hoist only approve happened to resolve a client, so a deny on someone else's *named* client was refused by the shape of the code rather than by a check. A request naming no client has no owner to check — see the residual in the Token Response section above and `test_consent_lazy_registration_request_has_no_owner_to_bind_to`

## Live Access Token Revocation Check

`backend/app/api/deps.py::get_current_user` inspects the decoded JWT payload for the `client_kind` claim. When the value equals `"desktop"`, it delegates to `DesktopAuthService.verify_active_or_raise(session, external_client_id)` which:

1. Parses `external_client_id` from the JWT as a UUID — raises `DesktopAuthError("client_missing" | "client_invalid", ...)` if absent or malformed.
2. Loads the `DesktopOAuthClient` row by PK. Missing row OR `is_revoked=True` → `DesktopAuthError("revoked", "Desktop session has been revoked")`.
3. Throttled stamping: if `last_used_at` is `NULL` or older than `DESKTOP_LAST_USED_THROTTLE_SECONDS` (60s), sets `last_used_at = now()` and commits. The throttle keeps write amplification low for chatty clients while still giving the Settings UI a near-live "last active" timestamp.

The dep catches `DesktopAuthError` and re-raises as `HTTPException(401, detail=e.message)` — the service stays HTTP-agnostic so it can be reused from WS deps or other callers later (same pattern as `CLIAuthError` / `_resolve_cli_context`).

The check fires for every authenticated request from a desktop client (not just `/api/v1/external/...`), so `/api/v1/users/me`, `/api/v1/desktop-auth/userinfo`, etc. all reject revoked tokens. Web-session JWTs lack `client_kind`, so they short-circuit without the extra DB hit.

Test coverage lives in `backend/tests/api/desktop_auth/test_desktop_auth.py`:
- `test_revoked_desktop_client_blocks_access_token` — revocation invalidates `/users/me`
- `test_revoked_desktop_client_blocks_external_a2a_endpoints` — same, for `/api/v1/external/agents`
- `test_desktop_token_updates_last_used_at` — successful calls update the stamp
- `test_revoked_desktop_client_rejects_userinfo` — `/desktop-auth/userinfo` is covered too
