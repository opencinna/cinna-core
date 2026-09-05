# Authentication - Technical Details

## File Locations

### Backend - Models
- `backend/app/models/users/user.py` - User model (table), UserBase, UserCreate, UserRegister, UserUpdate, UserUpdateMe, UserPublic, Token, TokenPayload, OAuthConfig, SetPassword, NewPassword, UpdatePassword

### Backend - Routes
- `backend/app/api/routes/login.py` - Password login, test token, password recovery, password reset
- `backend/app/api/routes/oauth.py` - OAuth config, Google authorize/callback/link/unlink
- `backend/app/api/routes/users.py` - Signup, profile, password management, admin user CRUD
- `backend/app/api/routes/_user_public.py` - `user_to_public()`, the single builder for `UserPublic` (populates `can_change_email` and the derived `has_*` flags). Shared by `users.py` and `login.py` so the two cannot drift into disagreeing projections

### Backend - Services
- `backend/app/services/users/auth_service.py` - OAuth flows, state management, Google account creation
- `backend/app/services/users/user_service.py` - User CRUD, password hashing, registration, password recovery
- `backend/app/services/users/access_policy_service.py` - The single resolver for registration and sign-in policy. See [Access Policy — tech](../server_configuration/access_policy_tech.md)

### Backend - Core
- `backend/app/core/security.py` - JWT creation, bcrypt hashing, Google token verification, field encryption
- `backend/app/core/config.py` - Auth settings (secrets, token expiry, OAuth config). `AUTH_WHITELIST_USER_DOMAINS` remains only as the access-policy first-boot seed
- `backend/app/api/deps.py` - Auth dependency injection (CurrentUser, TokenDep, guest context)

### Frontend - Hooks
- `frontend/src/hooks/useAuth.ts` - Auth state management (login, logout, signup, current user query, post-login redirect, `ensureSessionValid` / `redirectToLoginPreservingTarget` helpers used by consent routes)

### Frontend - Entry Point
- `frontend/src/main.tsx` - React Query global `handleApiError`: on 401/403 from any `ApiError`, clears the token and redirects to `/login?redirect=<current-url>` (preserves return target, via `safeRedirectPath`). Skips redirect on `/guest/*` pages

### Frontend - Utils
- `frontend/src/utils.ts` - `safeRedirectPath()` validates a `?redirect=` value as a same-origin local path (rejects protocol-relative, backslash tricks, cross-origin)

### Frontend - Routes
- `frontend/src/routes/login/index.tsx` - Login page with email/password form and Google button; policy-driven (hides the form and the "Forgot your password?" link on a Google-only instance, behind an administrators-only disclosure)
- `frontend/src/routes/signup.tsx` - Registration page with Google button; policy-driven (explanatory panel instead of the form when self-serve signup is closed)
- `frontend/src/hooks/useAccessPolicy.ts` - Shared `["accessPolicy"]` query used by both pages
- `frontend/src/routes/recover-password.tsx` - Password recovery form
- `frontend/src/routes/reset-password.tsx` - Password reset form (token from URL)
- `frontend/src/routes/_layout.tsx` - Protected route guard: `isLoggedIn()` + `LoginService.testToken()`, clears token and redirects to `/login` on 401/403/404
- `frontend/src/routes/oauth/mcp-consent.tsx` - MCP OAuth consent page; `beforeLoad` calls `ensureSessionValid()`; approve mutation handles mid-page 401/403 via `redirectToLoginPreservingTarget()`
- `frontend/src/routes/desktop-auth/consent.tsx` - Cinna Desktop OAuth consent page; same `ensureSessionValid()` + mid-page recovery pattern

### Frontend - Components
- `frontend/src/components/Auth/GoogleLoginButton.tsx` - Google OAuth button component

## Database Schema

### User Table (`backend/app/models/users/user.py`)

Auth-relevant fields:

| Field | Type | Purpose |
|-------|------|---------|
| `id` | UUID | Primary key, used as JWT `sub` claim |
| `email` | String (unique) | Login identifier |
| `hashed_password` | String (nullable) | Bcrypt hash; null for OAuth-only users |
| `google_id` | String (nullable, unique) | Google's unique user ID for OAuth linking |
| `is_active` | Boolean | Must be true to authenticate (checked in `deps.get_current_user` via `AccessPolicyService.is_account_valid`) |
| `is_superuser` | Boolean | Admin access flag |

## API Endpoints

### Login Routes (`backend/app/api/routes/login.py`)
- `POST /api/v1/login/access-token` - Password login (OAuth2PasswordRequestForm) -> Token. 403 `password_auth_disabled` for a gated non-superuser, checked **after** `authenticate()` and the active check
- `POST /api/v1/login/test-token` - Validate JWT -> UserPublic
- `POST /api/v1/password-recovery/{email}` - Initiate password reset -> Message. Gated silently: a refused send still returns the generic success message
- `POST /api/v1/reset-password/` - Complete password reset (NewPassword) -> Message. 403 `password_auth_disabled` for a gated non-superuser

### OAuth Routes (`backend/app/api/routes/oauth.py`)
- `GET /api/v1/auth/oauth/config` - OAuth availability -> `OAuthConfig` (`google_enabled` only; `allow_email_change` moved to `UserPublic.can_change_email`)
- `GET /api/v1/auth/google/authorize` - Start OAuth flow -> {authorization_url, state}
- `POST /api/v1/auth/google/callback` - Handle OAuth code (GoogleCallbackRequest) -> Token
- `POST /api/v1/auth/google/link` - Link Google to authenticated user (requires CurrentUser)
- `DELETE /api/v1/auth/google/unlink` - Unlink Google account (requires CurrentUser)

### User Routes (`backend/app/api/routes/users.py`) - Auth-relevant
- `POST /api/v1/users/signup` - Public registration (UserRegister) -> UserPublic. 403 + reason code on any policy refusal, raised before the duplicate check
- `GET /api/v1/users/me` - Current user profile -> UserPublic
- `PATCH /api/v1/users/me/password` - Change password (UpdatePassword). Gated by `_require_password_auth`
- `POST /api/v1/users/me/set-password` - Set password for OAuth users (SetPassword). Gated by `_require_password_auth`
- `PATCH /api/v1/users/me` - Profile update; an email change is refused with 403 while an allowed-email pattern list is configured

### Access-Policy Route
- `GET /api/v1/server-config/access-policy` - Public, rate-limited projection driving the login and signup pages -> `AccessPolicyPublic`. See [Access Policy — tech](../server_configuration/access_policy_tech.md)

## Services & Key Methods

### UserService (`backend/app/services/users/user_service.py`)
- `authenticate(session, email, password)` - Validate credentials, return User or None
- `register_user(session, email, password, full_name)` - Public signup; calls `AccessPolicyService.can_register(origin="signup")` before the duplicate check and raises `RegistrationNotAllowedError` on refusal
- `create_user(session, user_create)` - Admin user creation with password hashing
- `update_password(session, user, current_password, new_password)` - Password change with validation
- `set_password(session, user, new_password)` - Set password for OAuth-only users
- `reset_password(session, token, new_password)` - Token-based password reset; calls `AccessPolicyService.require_password_auth` once the token resolves its owner
- `recover_password(session, email)` - Generate reset token and send email; **silently skips** the send when `is_password_auth_allowed` is false, exactly like the cooldown skip
- `create_external_user(session, email, confirmed, provenance, passwordless=False)` - Get-or-create a user from an externally-arriving sender address (email integration, server channels). Uses the `external` origin, so the registration policy does not apply — the integration's own sender allowlist is the gate. Still picks up the policy's default role

### AuthService (`backend/app/services/users/auth_service.py`)
- `create_access_token(user_id)` - Generate JWT via `security.create_access_token()`
- `create_user_from_google(...)` - Calls `AccessPolicyService.can_register(origin="google")` first
- See [Google OAuth Tech](google_oauth_tech.md) for OAuth-specific methods
- `is_email_domain_allowed(email)` was **removed** — subsumed by `AccessPolicyService.can_register`

### AccessPolicyService (`backend/app/services/users/access_policy_service.py`)
The single resolver for the front-door policy. Auth-relevant entry points:
- `can_register(session, *, email, origin)` - Registration gate for `signup` / `google`; `admin`, `invite`, `external` and `seed` always pass
- `require_password_auth(session, user)` - Raises `PasswordAuthDisabledError` unless `password_auth_enabled or user.is_superuser`. Used by login, reset, set-password and change-password. Password **recovery** uses the predicate directly so it can skip silently
- `can_change_email(session)` - Drives `UserPublic.can_change_email` and the `PATCH /users/me` email branch
- `is_account_valid(user)` - Today `user.is_active`; called by `deps.get_current_user` only
Full contract in [Access Policy — tech](../server_configuration/access_policy_tech.md)

### Security (`backend/app/core/security.py`)
- `create_access_token(subject, expires_delta)` - JWT creation (PyJWT, HS256, SECRET_KEY)
- `verify_password(plain, hashed)` - Bcrypt comparison via passlib
- `get_password_hash(password)` - Bcrypt hashing via passlib

### Dependencies (`backend/app/api/deps.py`)
- `TokenDep` - OAuth2PasswordBearer extracting JWT from Authorization header
- `get_current_user(session, token)` - Decode JWT, fetch User, validate account validity via `AccessPolicyService.is_account_valid` (today: `is_active`) — the single seam a future per-request rule lands in. Other token families in `deps` still test `is_active` inline
- `CurrentUser` - Annotated dependency for authenticated user
- `get_current_active_superuser(current_user)` - Admin-only guard (403 if not superuser)
- `get_current_user_or_guest(session, token)` - Resolves both regular user and guest JWT types

## Frontend Components

### useAuth Hook (`frontend/src/hooks/useAuth.ts`)
- `isLoggedIn()` - Checks `localStorage.getItem("access_token") !== null` (localStorage-only — does not verify the token with the backend)
- `ensureSessionValid(returnTo)` - For use inside a route's `beforeLoad` on public consent/authorize pages. Verifies the token via `LoginService.testToken()`; on 401/403/404 clears the token and throws a TanStack `redirect({ to: "/login", search: { redirect: returnTo } })`. Re-throws other errors. Used by `oauth/mcp-consent.tsx` and `desktop-auth/consent.tsx`
- `redirectToLoginPreservingTarget()` - Imperative variant for in-page error handlers (e.g. mutation `onError`). Clears the token and navigates the browser to `/login?redirect=<current-url>` after passing the current location through `safeRedirectPath`
- `user` query - `useQuery(["currentUser"], UsersService.readUserMe())` with auto-logout on 401/404
- `loginMutation` - Password login via `LoginService.loginAccessToken()`, stores token, then calls `navigateToPostAuthTarget(readRedirectFromUrl())`
- `signUpMutation` - Registration via `UsersService.registerUser()`; on success navigates to `/login`, preserving the current `?redirect=` param
- `logout()` - Removes token from localStorage, navigates to /login
- `readRedirectFromUrl()` - Private helper that reads `?redirect=` from `window.location.search` and runs it through `safeRedirectPath()`; returns null when absent or unsafe
- `navigateToPostAuthTarget(target)` - Private helper that uses `window.location.assign()` so arbitrary same-origin paths (with their own query string) work regardless of TanStack Router's typed route registry

### Route Guard (`frontend/src/routes/_layout.tsx`)
- `beforeLoad` checks `isLoggedIn()`, throws `redirect({ to: "/login" })` if false; then calls `LoginService.testToken()` and on 401/403/404 clears the token and redirects to `/login`
- All routes under `/_layout/` are protected

### Consent Page Route Guards
- `frontend/src/routes/oauth/mcp-consent.tsx` `beforeLoad` calls `ensureSessionValid(buildConsentReturnTo(nonce, app_mcp))` so that visitors with an expired token (not just a missing one) get bounced through `/login?redirect=/oauth/mcp-consent?...` instead of landing on the page and failing on Authorize
- `frontend/src/routes/desktop-auth/consent.tsx` `beforeLoad` calls `ensureSessionValid("/desktop-auth/consent?request=<nonce>")` with the same intent
- Both pages also wire the approve/consent mutation's `onError` to `redirectToLoginPreservingTarget()` so that a token expiring while the user sits on the page does not strand them on a "Could not validate credentials" message
- The mcp-consent page uses raw `fetch()` (not the generated client), so its helpers throw a local `ConsentRequestError` carrying `status` — the mutation `onError` reads `error.status` instead of relying on the global `ApiError` handler

### Login Page (`frontend/src/routes/login/index.tsx`)
- Zod validation (email, password >= 8 chars)
- Google button above email form with "Or continue with email" divider (divider only when the form is also shown)
- Links to /signup (only when `registration_open`) and /recover-password (inside the password form, so it hides with it)
- `googleAvailable = Boolean(import.meta.env.VITE_GOOGLE_CLIENT_ID) && (policy?.google_auth_enabled ?? true)` — both halves are required, and `!googleAvailable` is the floor that keeps the password form visible when no other way in exists
- `validateSearch` accepts `redirect?: string`; `beforeLoad` honors it for already-logged-in visitors via `throw redirect({ href: target })` (validated through `safeRedirectPath`)
- Forwards the `redirect` param into the `/signup` link so users can switch auth mode without losing the target

### Signup Page (`frontend/src/routes/signup.tsx`)
- `selfServeSignupAllowed = registrationOpen && passwordAuthEnabled`; when false the form is replaced by an explanatory `Alert` and the Google button is kept
- Same `validateSearch` / `beforeLoad` redirect handling as login
- Forwards the `redirect` param into the `/login` link

### Google Login Button (`frontend/src/components/Auth/GoogleLoginButton.tsx`)
- Before launching Google's popup, captures the current `?redirect=` param, validates it via `safeRedirectPath`, and stashes the result in `sessionStorage` under `google_oauth_redirect` (alongside the existing `google_oauth_state`)
- After the Google callback succeeds, reads the stashed redirect and navigates via `window.location.assign()`; cleared in both `onSuccess` and `onError`
- Why `sessionStorage`: Google's redirect strips query params, so the redirect target wouldn't survive the round-trip via URL alone

## Configuration

| Setting | Location | Purpose |
|---------|----------|---------|
| `SECRET_KEY` | `.env` / `config.py` | JWT signing secret (32-byte random) |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `config.py` | Token lifetime (default: 8 days) |
| `AUTH_WHITELIST_USER_DOMAINS` | `.env` / `config.py` | **Seed only** — converted to access-policy glob patterns on first boot and by migration `1d737d7ef0a0`. No live decision reads it; a startup warning names the admin page when it is still set |
| `ACCESS_POLICY_RATE_LIMIT_PER_MIN` | `config.py` (default 120) | Per-IP budget for the public access-policy projection |

`settings.auth_whitelist_domains` and `settings.allow_user_email_change` were removed; the live answers are `AccessPolicyService.is_email_allowed` and `AccessPolicyService.can_change_email`.
| `EMAILS_ENABLED` | `config.py` | Whether password recovery emails are sent |

## Security

- **JWT**: HS256 with 32-byte random SECRET_KEY, 8-day expiry
- **Passwords**: Bcrypt hashing via passlib CryptContext
- **Token validation**: `get_current_user()` verifies signature, expiration, and user active status
- **Access policy**: Enforced at registration for both password and Google paths, and on every password path for sign-in method. Reason codes are stable machine-readable strings, never prose
- **Non-enumeration**: signup refuses on policy before the duplicate check; password recovery skips silently; the Google callback answers with the same 403 shape as signup
- **Lockout prevention**: Cannot unlink Google without password set; cannot delete superuser self; superusers keep the password break-glass when password sign-in is off; `PUT /admin/server-config` refuses to turn password sign-in off unless an administrator can use Google *and* an administrator has a password to fall back on
