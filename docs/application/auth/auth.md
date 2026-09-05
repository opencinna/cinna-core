# Authentication

## Purpose

Provides user identity and session management for the platform. Users authenticate via password or Google OAuth, receive a JWT token, and use it for all subsequent API requests. Supports dual authentication (both methods on one account), password recovery, and a database-backed access policy that decides who may register and which sign-in methods the instance offers.

## Core Concepts

| Concept | Definition |
|---------|-----------|
| **JWT Token** | HS256-signed token containing user ID and expiration. Used as bearer token for all API requests |
| **Password Auth** | Email + password login using bcrypt-hashed passwords |
| **Google OAuth** | Authorization code flow via Google popup. See [Google OAuth](google_oauth.md) |
| **Access Policy** | The admin-configured front door: registration mode, allowed email patterns, and the password / Google sign-in switches. Stored on the `ServerConfig` singleton and resolved by `AccessPolicyService`. Replaces the retired `AUTH_WHITELIST_USER_DOMAINS` env setting. See [Access Policy](../server_configuration/access_policy.md) |
| **Allowed Email Patterns** | Comma-separated globs (`*@acme.com`) restricting who may register. Empty means no restriction. Gates registration only — never login |
| **Superuser Break-Glass** | Superusers keep password login, recovery and reset even when password sign-in is switched off for everyone else |
| **Reason Code** | The machine-readable `detail` string a policy refusal returns (`registration_closed`, `email_not_allowed`, `password_auth_disabled`, `google_auto_register_disabled`) |
| **Access Token** | The JWT stored in frontend `localStorage` and auto-included in API requests |
| **Guest Token** | Special JWT with `role=chat-guest` for unauthenticated agent chat access via guest share links |

## User Stories / Flows

### Password Login

1. Login page reads the public access-policy projection and renders only what the policy allows — the password form is hidden on a Google-only instance, behind a "Sign in with password (administrators)" disclosure
2. User enters email and password on the login page
3. Frontend submits credentials as OAuth2PasswordRequestForm
4. Backend validates credentials (bcrypt comparison), then the active check, then the access-policy check — in that order, so an address with no account never gets a different answer
5. Non-superuser on an instance with password auth off → 403 `password_auth_disabled`; superusers pass (break-glass)
6. Backend returns JWT access token
7. Frontend stores token in `localStorage` and navigates to the post-login target — the validated `?redirect=` URL search param if present, otherwise the dashboard (see [Post-Login Redirect](#post-login-redirect))

### User Registration (Signup)

1. Signup page reads the public access-policy projection. If registration is invite-only or password sign-in is off, the form is replaced by an explanatory panel (the Google button is kept when Google is configured, since it is how an existing account signs in)
2. User fills signup form (full name, email, password, confirm password)
3. Frontend validates locally (email format, password >= 8 chars, passwords match)
4. Backend checks the access policy: open registration, password auth on, and a match against the allowed email patterns. Any refusal is a 403 carrying the reason code — raised **before** the uniqueness check, so a closed instance answers identically for known and unknown addresses
5. Backend checks email uniqueness
6. Backend creates user with hashed password and the policy's default role
7. Confirmation email sent (if SMTP configured)
8. User redirected to login page

### Password Recovery

1. User enters email on recovery page
2. If password sign-in is off and the user is not a superuser, the send is skipped **silently** and the generic success message is still returned — saying otherwise would identify which addresses belong to superusers
3. Backend generates time-limited reset token
4. Backend sends email with reset link containing token
5. User clicks link, enters new password
6. Backend validates the token, re-checks the same policy gate for the token's owner (403 `password_auth_disabled` for a gated non-superuser), then hashes and saves the new password
7. User redirected to login page

### Set Password (OAuth Users)

1. OAuth-only user (no password set) navigates to profile settings
2. User enters desired password
3. Backend sets password, enabling dual authentication

### Logout

1. User clicks logout
2. Frontend removes `access_token` from localStorage
3. Frontend redirects to login page

## Business Rules

### Token Lifecycle
- JWT tokens expire after 8 days (configurable via `ACCESS_TOKEN_EXPIRE_MINUTES`)
- Tokens contain only user ID (`sub`) and expiration (`exp`)
- Invalid/expired tokens return 403; missing users return 404
- Frontend clears token and redirects to login on 401/404 errors

### Registration Restrictions
- Registration is governed by the [Access Policy](../server_configuration/access_policy.md) on the `ServerConfig` singleton, edited at **Admin → Server Configuration → Access**. `AUTH_WHITELIST_USER_DOMAINS` is retired: it seeds the policy on first boot and by migration, and nothing reads it for a live decision
- In `invite_only` mode nobody self-registers — neither by password signup nor by Google
- When `allowed_email_patterns` is non-empty, only matching addresses can register. An empty list (and the single pattern `*`) means no restriction
- Patterns apply to both password signup and Google registration
- Patterns gate **registration only**. Existing users are never locked out by a pattern edit, and switching to invite-only never evicts anyone
- Admin-created users (`POST /api/v1/users/`), invitation acceptances, externally-arriving channel senders, and the first-superuser bootstrap all bypass the registration policy — each carries its own admission decision
- While a pattern list is configured, users cannot change their own email address (`UserPublic.can_change_email` is false; `PATCH /users/me` refuses with 403)

### Sign-in Method Restrictions
- `password_auth_enabled=false` makes the instance Google-only: non-superusers are refused on login, signup, password recovery, reset, set-password and change-password with `password_auth_disabled`
- Superusers keep every password path as a break-glass, so a broken Google configuration cannot lock all administrators out
- The switch cannot be turned off unless Google OAuth is configured, some administrator can sign in with Google, **and** some administrator actually has a password (an admin provisioned only through Google has none, so the break-glass would open onto nothing) — all validated server-side on `PUT /admin/server-config`
- The login page keeps the password form visible whenever Google is not actually usable in the browser (the frontend build also needs `VITE_GOOGLE_CLIENT_ID`, which the backend's lockout rule cannot see)

### Account Protection
- Cannot unlink Google OAuth if no password is set (prevents lockout)
- Cannot delete superuser account via self-service
- Inactive users are blocked from authentication
- Password changes require current password verification
- Cannot set password if one already exists (use change password instead)

### Dual Authentication
- Users can have both password and Google OAuth linked
- `has_password` and `has_google_account` booleans exposed in user profile
- Either method produces the same JWT token

### Post-Login Redirect
- `/login` and `/signup` accept an optional `?redirect=` URL search param naming the page to land on after successful authentication
- After password login, signup-then-login chain, or Google OAuth, the frontend navigates to the validated redirect target instead of the dashboard
- Already-authenticated users hitting `/login` or `/signup` with `?redirect=` are sent straight to the target (no re-auth required)
- The `?redirect=` value is preserved across the login ↔ signup switch links and through the Google OAuth round-trip (stashed in `sessionStorage` while Google's popup is open)
- Validation: redirect targets must be same-origin local paths starting with `/`; protocol-relative (`//host`), backslash tricks (`/\\host`), and cross-origin URLs are rejected and silently fall back to the dashboard (open-redirect protection)
- Primary consumer: the MCP OAuth consent page (`/oauth/mcp-consent`) — when an MCP client opens the consent URL in an embedded browser with no platform session, the user can log in inline and resume the OAuth flow without re-triggering it from the MCP client. See [MCP Integration](../mcp_integration/agent_mcp_architecture.md)
- Secondary consumer: the Cinna Desktop consent page (`/desktop-auth/consent`) — same pattern for the desktop OAuth flow. See [Desktop Auth](../desktop_auth/desktop_auth.md)

### Expired-Session Recovery on Consent Pages
- Public consent/authorize pages (`/oauth/mcp-consent`, `/desktop-auth/consent`) sit outside the `_layout` guard and previously trusted any non-empty `access_token` in `localStorage` — an expired token was enough to render the page, and clicking Authorize then failed with "Could not validate credentials" leaving the user stranded
- Both routes now validate the token in `beforeLoad` by calling `LoginService.testToken()`. On 401/403/404 the token is cleared and the user is redirected to `/login?redirect=<this-page>` so they re-authenticate and land back on the consent screen
- If the token expires while the user sits on the consent screen (between page load and the Authorize click), the mutation's `onError` handler detects 401/403, clears the token, and bounces through `/login?redirect=` preserving the consent URL — the user never sees the credentials error
- The global API error handler (React Query's shared `onError`) also preserves the current URL as `?redirect=` when bouncing on 401/403, so any future protected page using the generated client gets the same return-to-page behavior automatically

## Architecture Overview

```
GET /server-config/access-policy (public) ──→ /login, /signup render themselves
                                                                              │
Login Page ──→ POST /login/access-token ──→ UserService.authenticate()
                                              └─► AccessPolicyService.require_password_auth ──→ JWT Token
                                                                              │
Signup Page ──→ POST /users/signup ──→ UserService.register_user()
                                          └─► AccessPolicyService.can_register(origin="signup") ──→ User Created
                                                                              │
Google Button ──→ OAuth Flow ──→ AuthService.authenticate_with_google()
                                    └─► can_register(origin="google") on first login ──→ JWT Token
                                                                              │
                                                                              ▼
Frontend (localStorage) ──→ Authorization Header ──→ deps.get_current_user() ──→ CurrentUser
```

## Integration Points

- **[Access Policy](../server_configuration/access_policy.md)** - The admin-configured front door: who may register, which sign-in methods exist, what role new accounts get, and whether users may edit their own email address
- **[Google OAuth](google_oauth.md)** - Alternative authentication method via Google popup flow
- **[Guest Sharing](../../agents/guest_sharing/guest_sharing.md)** - Special guest JWT tokens (`role=chat-guest`) for unauthenticated access to agent chat via guest share links
- **[User Workspaces](../user_workspaces/user_workspaces.md)** - Workspace context applied after authentication
- **[AI Credentials](../ai_credentials/ai_credentials.md)** - User model stores encrypted AI credentials
- **Route Protection** - All `/_layout/*` frontend routes require valid authentication
