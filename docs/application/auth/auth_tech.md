# Authentication - Technical Details

## File Locations

### Backend - Models
- `backend/app/models/users/user.py` - User model (table), UserBase, UserCreate, UserRegister, UserUpdate, UserUpdateMe, UserPublic, Token, TokenPayload, OAuthConfig, SetPassword, NewPassword, UpdatePassword; `UserRole` / `VALID_USER_ROLES` and the `AccountOrigin` enum (it lives next to `UserRole` rather than in the access-policy service because both that service and the creation chokepoint need it, and a constant owned by one of two peers is how an import cycle starts)
- `backend/app/models/users/user_invitation.py` - `UserInvitation` (table) plus the whole projection family: `UserInvitationPublic`, `InviteUserRequest`, `InviteUserResponse`, `ResendInvitationResponse`, `InvitationLinkPublic`, `InvitationLookupRequest`, `InvitationLookupPublic`, `AcceptInvitationRequest`, `InviteProvisioningSummary`, `InviteProvisioningSkip`; the `INVITATION_STATUS_*` / `VALID_INVITATION_STATUSES` vocabulary and the `INVITATION_AUTH_HINT_*` / `VALID_INVITATION_AUTH_HINTS` set. Declares **no** SQLModel `Relationship` back to `User` — see [Why the invitation model has no relationship](#why-the-invitation-model-has-no-relationship)

### Backend - Routes
- `backend/app/api/routes/login.py` - Password login, test token, password recovery, password reset
- `backend/app/api/routes/invitations.py` - The **public** half of the invitation lifecycle: `POST /invitations/lookup` and `POST /invitations/accept`. Its own module and its own tag (`InvitationsService` in the generated client) so the anonymous surface stays separate from the superuser one on `/users/*`, and so the non-enumeration rules live in one file. Registered in `api/main.py` right after `users.router`
- `backend/app/api/routes/oauth.py` - OAuth config, Google authorize/callback/link/unlink
- `backend/app/api/routes/users.py` - Signup, profile, password management, admin user CRUD. `POST /users/` wraps `create_user` in a `try/except ValueError → 400`: its own duplicate check compares the address as typed, while the chokepoint normalises before storing, so `Foo@Bar.com` against an existing `foo@bar.com` gets past the first check and is caught by the second
- `backend/app/api/routes/private.py` - Local-development `POST /private/users/` helper; goes through `UserService.create_account(origin=AccountOrigin.ADMIN)` like every other path, so a dev-seeded account is indistinguishable from a real one
- `backend/app/api/routes/_user_public.py` - `user_to_public()`, the single builder for `UserPublic` (populates `can_change_email` and the derived `has_*` flags). Shared by `users.py` and `login.py` so the two cannot drift into disagreeing projections. Also `_load_missing_columns()` — see [The `model_dump()` expiry trap](#the-model_dump-expiry-trap). Gained a keyword-only `invitation_status: str | None = None`, which — unlike `can_change_email` — is **not** resolved inside the builder when omitted: a lookup there would be one query per user on the admin list and one per request on `/users/me`, `/login/test-token` and the eight other single-row producers that have no use for it. `read_users` batches the whole page's statuses in one `IN` query (`InvitationService.status_map`) and passes the answer in

### Backend - Services
- `backend/app/services/users/auth_service.py` - OAuth flows, state management, Google account creation
- `backend/app/services/users/user_service.py` - `create_account` (the chokepoint), user CRUD, password hashing, registration, password recovery
- `backend/app/services/users/invitation_service.py` - `InvitationService` — the whole invitation lifecycle, plus the two predicates other modules borrow (`claim_refused`, `is_unclaimed`) and the five audit event-type constants
- `backend/app/services/users/account_provisioning_service.py` - `AccountProvisioningService.on_account_created` / `provision_explicit` / `on_account_deactivated` / `on_account_reactivated` / `on_account_deleted`. Two *creation* entry points, one shared body (`_provision`) and one shared outer net (`_guarded`); the three lifecycle hooks handle a minted per-user key's revoke and re-mint (shared credentials are untouched by all three). See [Admin-Provisioned AI Credentials — tech](../ai_credentials/admin_ai_credential_provisioning_tech.md#accountprovisioningservice-servicesusersaccount_provisioning_servicepy)
- `backend/app/core/db.py` - `init_db` seeds the first superuser through `create_user(..., origin=AccountOrigin.SEED)`; `seed` is ungated because the first superuser has to be creatable on an instance whose access policy does not exist yet
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
- `frontend/src/routes/accept-invite.tsx` - Layout route for the accept flow. Renders only an `<Outlet>`, the same shape as `login.tsx`: a file-based parent has to render one for its children to be addressable. Deliberately **unguarded**, like `desktop.tsx` — the visitor has no session yet
- `frontend/src/routes/accept-invite/index.tsx` - The accept page. `validateSearch: z.object({ token: z.string().catch("") })`, then a `POST` lookup with the token in the body. Renders the password form iff `lookup.password_accepted === true`, and never consults `useAccessPolicy()` for that decision
- `frontend/src/routes/accept-invite/done.tsx` - Post-acceptance landing page. `beforeLoad` calls `ensureSessionValid`, like `desktop-auth/consent.tsx`, since it lives outside the `_layout` guard but the visitor is signed in by then. `?desktop=<bool>` is carried over from the accept page rather than re-read from the policy — whether desktop was offered is a property of *this invitation*, and the invitation is unreachable once its token is spent
- `frontend/src/routes/_layout.tsx` - Protected route guard: `isLoggedIn()` + `LoginService.testToken()`, clears token and redirects to `/login` on 401/403/404
- `frontend/src/routes/oauth/mcp-consent.tsx` - MCP OAuth consent page; `beforeLoad` calls `ensureSessionValid()`; approve mutation handles mid-page 401/403 via `redirectToLoginPreservingTarget()`
- `frontend/src/routes/desktop-auth/consent.tsx` - Cinna Desktop OAuth consent page; same `ensureSessionValid()` + mid-page recovery pattern

### Frontend - Components
- `frontend/src/components/Auth/GoogleLoginButton.tsx` - Google OAuth button component
- `frontend/src/components/Admin/InviteUserDialog.tsx` - The two-step invite wizard (**who** → **provisioning**), the primary action in the Admin → Users page header, plus its `InviteSuccess` screen (copyable accept link, `email_sent` state, provisioning summary). One `<form>` across both steps, so Enter means "Next" on step 1 and "Send" only on step 2
- `frontend/src/components/Admin/InvitationActions.tsx` - The row-menu entries (resend / copy link / revoke) and the expiry countdown. Returns `null` unless the row has an outstanding invitation. The `expires_at` query is `enabled` only then, and only runs while one row's menu is open, because `DropdownMenuContent` unmounts on close
- `frontend/src/components/Admin/columns.tsx` - The existing **Status** column cell, extended: `is_active` is asked first (`Inactive`), then the invitation status (`Invited` / `Expired` / `Revoked`), then `Active`. A status this build does not recognise renders **nothing** rather than falling through to `Active`
- `frontend/src/utils/invitationStatus.ts` - The status vocabulary on the frontend side, plus `knownInvitationStatus` (returns `null` for anything unrecognised), `isOutstandingInvitation`, `isUnknownInvitationStatus`, `daysUntil` / `formatDaysUntil`
- `frontend/src/components/Admin/AddUser.tsx` - Gained an optional `trigger?: React.ReactElement` prop so the users page can present it as the quieter **Create with password** button beside **Invite user**

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

`UserPublic` also carries `invitation_status: str | None = None` — one of `VALID_INVITATION_STATUSES`, or `None` for an account that was never invited (which is most of them, and every account predating invitations). **Optional with a default**, unlike `can_change_email`: ten of the eleven `UserPublic` producers legitimately have no invitation to report, and a required field there would 500 all of them, which `tests/architecture/user_public_builder_test.py` records having happened once already.

### `user_invitation` Table (`backend/app/models/users/user_invitation.py`)

Migration **`89878dff89a6`** (`down_revision = "b71863b32aa1"`).

| Field | Type | Purpose |
|-------|------|---------|
| `id` | UUID | Primary key |
| `user_id` | UUID, FK `user.id` **ON DELETE CASCADE**, unique | The invited account. One invitation per account |
| `token_jti` | UUID, unique | The live token's `jti`. **Rotated in place on every resend** — this is the whole revocation mechanism |
| `invited_by_id` | UUID, nullable, FK `user.id` **ON DELETE SET NULL** | The issuing admin |
| `auth_hint` | String(16) | `any` / `password` / `google`. Presentational only |
| `include_desktop` | Boolean | Whether the email and the done page offer Cinna Desktop |
| `expires_at` | timestamptz | Also the token's `exp`, by construction |
| `accepted_at` | timestamptz, nullable | Set once, terminal |
| `revoked_at` | timestamptz, nullable | Cleared by resend |
| `last_sent_at` | timestamptz, nullable | Arms the per-row resend cooldown. A suppressed send does **not** stamp it |
| `send_count` | Integer | Incremented only on a mail that actually left |
| `created_at` / `updated_at` | timestamptz | |

Indexes: `uq_user_invitation_user_id` and `uq_user_invitation_token_jti`, both unique, and **nothing else**. There is deliberately no index on `expires_at` — nothing queries by it (expiry is evaluated in Python on a row already fetched by one of those two keys), and the sweeper that would scan by it does not exist yet. The migration carries a comment telling whoever adds that sweeper to add the index in the same migration; the model declares no `index=True`, so `--autogenerate` will not re-propose it meanwhile.

There is no `status` column. Status is derived by `InvitationService.status_of` from the three timestamps and the clock.

#### Why the invitation model has no relationship

The two `ondelete=` clauses are the entire deletion mechanism, and they only work because the model declares **no** SQLModel `Relationship` back to `User`. `users.delete_user` is a bare `session.delete(user); session.commit()` with nothing else behind it. An unconfigured relationship would make SQLAlchemy try to NULL a `NOT NULL user_id` before Postgres ever got to cascade, and deleting an invited account would raise `IntegrityError`.

The asymmetry between the two keys is behavioural, not cosmetic: deleting an invited account removes its invitation (CASCADE), while removing the administrator who *issued* invitations must not remove them (SET NULL) — getting that backwards would make offboarding an admin silently invalidate every outstanding invite, with nothing in the application reporting it.

## API Endpoints

### Login Routes (`backend/app/api/routes/login.py`)
- `POST /api/v1/login/access-token` - Password login (OAuth2PasswordRequestForm) -> Token. 403 `password_auth_disabled` for a gated non-superuser, checked **after** `authenticate()` and the active check
- `POST /api/v1/login/test-token` - Validate JWT -> UserPublic
- `POST /api/v1/login/session/refresh` - Cookie `cinna_refresh` + header `X-CSRF-Token` (+ `Origin` check when present) -> `WebSessionToken {access_token, token_type, expires_in: 900}`. Rotates the cookie (`Set-Cookie`), except inside the reuse-grace window. 401 `session_expired` (cookie cleared) for unknown/expired/revoked/replayed; 403 `csrf_failed`
- `POST /api/v1/login/session/logout` - 204, idempotent; with a cookie it needs `X-CSRF-Token` (403 `csrf_failed`) and clears the cookie
- `POST /api/v1/login/session/revoke-all` - CurrentUser, 204. `end_sessions(reason="sign_out_everywhere", include_api_tokens=True)`, clears the cookie
- `POST /api/v1/login/access-token` additionally takes `X-Cinna-Web-Session: 1` (opt in to a cookie session; `LoginToken.csrf_token` is then non-null, else null) and answers 429 `{"detail":{"code":"rate_limited","message":"rate_limited"}}` + integer `Retry-After` when throttled (`Retry-After` is in `CORSMiddleware.expose_headers`). `/login/mfa/verify`, the Google callback and invitation accept issue tokens through the same `issue_login_token`
- Cookie: `cinna_refresh`, `HttpOnly`, `SameSite=Strict`, host-only, `Path=/api/v1/login/session`, `Max-Age` = `WEB_REFRESH_TOKEN_EXPIRE_DAYS`, `Secure` per `WEB_SESSION_COOKIE_SECURE` (unset: on unless `ENVIRONMENT=local`)
- `POST /api/v1/password-recovery/{email}` - Initiate password reset -> Message. **Non-enumerating by contract**: always `200`, always the same body — `{"message": "If an account exists for that email, a password recovery email has been sent"}` — for a known and an unknown address alike, with no `detail` key. It no longer 404s for an unknown address. **Every** reason not to send is a silent no-op inside `UserService.recover_password`: outbound mail unconfigured, unknown address, deactivated account, password auth off for this user (superusers keep the break-glass, so a refusal would identify them), an unclaimed invited account whose invitation is not `pending`, the 300 s cooldown, and any failure in the token/template/SMTP/commit block. The service's `bool` return value is for tests and internal callers and is deliberately **not** projected into the response. Status and body carry no signal; response *time* still does — see [Login and recovery timing](#login-and-recovery-timing)
- `POST /api/v1/reset-password/` - Complete password reset (NewPassword) -> Message. `async def` since phase 3, purely because settling an invitation emits a security event and `SecurityEventService.create_event` is a coroutine — status codes and body are unchanged. 403 `password_auth_disabled` for a gated non-superuser. A never-claimed account whose invitation is not `pending` is refused as `"Invalid token"` (400) rather than with a branch of its own: the reason is the account's state, not the token's, and a distinct status would report it to whoever holds the link. On success it calls `InvitationService.mark_accepted_if_pending(..., method="reset_password")` **after** the `except` block — see [The two hook sites](#the-two-hook-sites)

### OAuth Routes (`backend/app/api/routes/oauth.py`)
- `GET /api/v1/auth/oauth/config` - OAuth availability -> `OAuthConfig` (`google_enabled` only; `allow_email_change` moved to `UserPublic.can_change_email`)
- `GET /api/v1/auth/google/authorize` - Start OAuth flow -> {authorization_url, state}
- `POST /api/v1/auth/google/callback` - Handle OAuth code (GoogleCallbackRequest) -> Token
- `POST /api/v1/auth/google/link` - Link Google to authenticated user (requires CurrentUser)
- `DELETE /api/v1/auth/google/unlink` - Unlink Google account (requires CurrentUser)

### User Routes (`backend/app/api/routes/users.py`) - Auth-relevant
- `POST /api/v1/users/signup` - Public registration (UserRegister) -> UserPublic. 403 + reason code on any policy refusal, raised before the duplicate check
- `GET /api/v1/users/me` - Current user profile -> UserPublic
- `PATCH /api/v1/users/me/password` - Change password (UpdatePassword). Gated by `_require_password_auth`. Ends every other session, keeping the caller's (`sid` / desktop `external_client_id` from `CurrentTokenClaims`); API tokens stay
- `POST /api/v1/users/me/set-password` - Set password for OAuth users (SetPassword). Gated by `_require_password_auth`. Same session effect as a change
- `PATCH /api/v1/users/me` - Profile update; an email change is refused with 403 while an allowed-email pattern list is configured

### Invitation Routes — public (`backend/app/api/routes/invitations.py`)

Both anonymous, both rate-limited by a module-level `RateLimiter()` keyed on `anonymous_caller_key(request)` through a `_invitation_rate_limit(request)` dependency declared **before** `SessionDep`, so a throttled request costs no pool connection and no query (the `server_config.py` shape). Budget: `INVITATION_RATE_LIMIT_PER_MIN` (30). Neither ever writes a row or sends mail.

- `POST /api/v1/invitations/lookup` - Body `InvitationLookupRequest` (`{"token": …}`) -> `InvitationLookupPublic`, serialised `response_model_exclude_none=True`. The token travels in the **body**, not the path, matching `NewPassword.token`: a JWT in a URL is written to every proxy access log, kept in browser history and leaked through `Referer`. Valid: `valid=true` plus `email_masked`, `full_name`, `auth_hint`, `password_accepted`, `google_auth_enabled`, `include_desktop`, `project_name`, `expires_at`. Invalid: `200 {"valid": false}` and nothing else — `exclude_none` is what makes the other keys *absent* rather than null. `password_accepted` in particular must be absent rather than `false`, because a field always present with a boolean value is itself a channel
- `POST /api/v1/invitations/accept` - Body `AcceptInvitationRequest` (`token`, `password` 8–128, optional `full_name`) -> `response_model=LoginResponse` — the same discriminated union `POST /login/access-token` declares, so the frontend narrows it with the code it already has. Only the `LoginToken` arm is reachable (the account is being given its first password in this request, so it cannot have enrolled a second factor); the token is minted with `AuthService.create_access_token(user.id)`. Every token failure is `400` with one fixed detail, `"This invitation is no longer valid"`. A too-short password is a `422` from the request schema, which is fine — it does not depend on whether the token is real. **There is no 403 branch**: `InvitationInvalidError` is the only exception type the handler translates, and a second branch here would reopen the oracle

### Invitation Routes — superuser (`backend/app/api/routes/users.py`)

All five guarded by `Depends(get_current_active_superuser)`, which refuses *before* any lookup runs — so a non-admin gets the same 403 for a user id that exists and one that does not.

- `POST /api/v1/users/invite` - Body `InviteUserRequest` -> `InviteUserResponse` (`user`, `invitation`, `accept_url`, `email_sent`, `provisioning`, `adopted_existing_account`). `async def`. The route pre-checks for a duplicate address purely to drive the wizard's "resend instead?" hint — `create_account` stays the authority, since it normalises before its own duplicate check — and one `except ValueError → 400 str(e)` covers all three of its refusals (malformed address, duplicate, invalid role). `user` is built through `user_to_public`, never `UserPublic.model_validate`: by that point four separate commits have expired the instance
- `GET /api/v1/users/{user_id}/invitation` -> `UserInvitationPublic`; 404 when the account was never invited
- `POST /api/v1/users/{user_id}/invitation/resend` -> `ResendInvitationResponse`. 404 no invitation, 409 already accepted, **429** on the per-row cooldown with `Retry-After` and a structured detail `{"code": "invitation_resend_cooldown", "message": …, "resend_available_at": <iso>}`
- `POST /api/v1/users/{user_id}/invitation/revoke` -> `UserInvitationPublic`. Idempotent; 404 no invitation, 409 already accepted
- `GET /api/v1/users/{user_id}/invitation/link` -> `InvitationLinkPublic` (`accept_url`, `expires_at`). Mints from the **stored** `jti` without rotating it, so reading the link out does not kill the email the recipient may be about to click. 409 (`InvitationNotPendingError`, naming the status) for anything but a pending invitation, because the link for a revoked/expired/accepted one is a dud. Audited as `user.invitation.link_issued`

### Access-Policy Route
- `GET /api/v1/server-config/access-policy` - Public, rate-limited projection driving the login and signup pages -> `AccessPolicyPublic`. See [Access Policy — tech](../server_configuration/access_policy_tech.md)

## Services & Key Methods

### The account-creation chokepoint

```python
UserService.create_account(
    session,
    *,
    email: str,
    origin: AccountOrigin,
    password: str | None = None,
    full_name: str | None = None,
    username: str | None = None,
    google_id: str | None = None,
    role: str | None = None,
    is_superuser: bool = False,
    is_active: bool = True,
    email_confirmed: bool = False,
    skip_auto_provision: bool = False,
) -> User
```

The only place in the backend a `User` row is constructed. Sequence: normalise + validate the address (`_EMAIL_ADAPTER` on `email.strip().lower()`) → `AccessPolicyService.can_register(origin=…)` → duplicate check on the normalised address → validate `role` against `VALID_USER_ROLES` → resolve the role → `INSERT` → `commit` + `refresh` → `AccountProvisioningService.on_account_created` unless `skip_auto_provision`.

Role resolution, in order: `is_superuser` pins `admin` (and sets `email_confirmed=True` + `email_confirmed_at`) even against an explicitly passed role; otherwise an explicit `role`; otherwise `RoleService.derive_default_role(session=…, is_superuser=False)`. `update_user` applies no such invariant — **create is stricter than update**.

`is_active` is in the signature although the phase spec did not list it: `UserCreate` carries it, so `create_user` needs somewhere to put it, and `create_external_user` passes it explicitly.

Raises `ValueError` (invalid address, duplicate, invalid role) and `RegistrationNotAllowedError` (policy refusal). It does **not** raise for a provisioning failure: `on_account_created` already guarantees that, and the `try` around it here is deliberate belt-and-braces — it makes "an account is never lost to a provisioning failure" a property of the account code rather than a promise borrowed from another module. Its handler rolls back unconditionally and logs from pre-snapshotted locals, for the reason documented on `AccountProvisioningService._restore_session`.

Callers:

| Caller | Origin | Notes |
|---|---|---|
| `UserService.create_user(session, user_create, origin=AccountOrigin.ADMIN)` | `admin` (default) | Thin adapter over the chokepoint for the `UserCreate`-shaped callers. Reads `exclude_unset` to tell "the caller did not say" from "the caller said `agent-user`" — `UserBase.role` carries a column default, so only the first should pick up the policy default |
| `core.db.init_db` | `seed` | Overrides the `create_user` default |
| `UserService.register_user` | `signup` | After its own `can_register` |
| `AuthService.create_user_from_google` | `google` | `google_id`, `email_confirmed=True`, after its own `can_register` |
| `UserService.create_external_user` | `external` | Both branches; the non-passwordless one passes `secrets.token_urlsafe(32)` |
| `POST /private/users/` | `admin` | Local-dev helper |

### Email lookups are case-insensitive

Request schemas `UserCreate`, `UserRegister`, `UserUpdate` and `UserUpdateMe` lowercase and strip `email` on input (`_NormalizedEmailInput` mixin in `models/users/user.py`); `UserBase`/response models are deliberately untouched so legacy mixed-case rows are not rewritten in responses. Lookups compare `func.lower(User.email)`, so `Alice@x.com` finds `alice@x.com`. **Legacy-duplicate rule:** if two rows differ only in case, `get_user_by_email` returns the exact-match row, else `None` (ambiguous, logged); duplicate checks use `email_in_use`, never "lookup returned None". Consequence: Google sign-in against an ambiguous pair is refused by the create path's duplicate check instead of linking an arbitrary account. `find_active_users_by_email_ci` (active-only, returns a list) remains the recipient lookup for issued agent-API keys.

Legacy rows already stored mixed-case are **not rewritten**; only new input is lowercased. A legacy pair (e.g. `Foo@x.com` + `FOO@x.com`) cannot self-recover: password recovery and confirmation resend find a user only when the typed address matches a stored one exactly, otherwise `get_user_by_email` returns `None` and logs a warning. To find such pairs, run once:

```sql
SELECT lower(email), array_agg(email), count(*) FROM "user" GROUP BY 1 HAVING count(*) > 1;
```

An admin resolves each pair by merging the accounts or renaming one row.

### UserService (`backend/app/services/users/user_service.py`)
- `authenticate(session, email, password)` - Validate credentials, return User or None. Both early-return branches — unknown address, and an account with no `hashed_password` — now spend one bcrypt verification against a cached dummy hash (`_burn_password_verification`) before returning, so a miss costs the same as a wrong password. See [Login and recovery timing](#login-and-recovery-timing)
- `find_users_by_email_ci(session, email) -> list[User]` - Every user (active or not) whose `func.lower(User.email)` equals the stripped, lowercased input, ordered by id. Normally 0–1 rows; more than one means **legacy rows that differ only in case**, written before every write path normalised.
- `get_user_by_email(session, email) -> User | None` - Case-insensitive resolve. With a legacy case-only duplicate, the row whose stored address equals the input exactly wins (reset/confirmation tokens carry the stored address verbatim); with no exact match it logs a warning and returns `None` rather than guess an account. `AuthService.get_user_by_email` delegates to it. Also used by `EmailConfirmationService` (resend and confirm).
- `email_in_use(session, email, exclude_user_id=None) -> bool` - The duplicate check for **every write path** (`create_account`, `register_user`, `POST /users/`, `PATCH /users/me`, `PATCH /users/{id}`, the invite wizard's pre-check via `find_users_by_email_ci`). It counts every case-insensitive match, so a legacy ambiguity can never read as "free" the way `get_user_by_email` returning `None` would.
- `register_user(session, email, password, full_name)` - Public signup; calls `AccessPolicyService.can_register(origin=AccountOrigin.SIGNUP)` before the duplicate check and raises `RegistrationNotAllowedError` on refusal, then delegates to `create_account`
- `create_user(session, user_create, origin=AccountOrigin.ADMIN)` - `UserCreate`-shaped adapter over `create_account`
- `update_password(session, user, current_password, new_password, keep_web_session_id=None, keep_desktop_client_pk=None)` - Password change with validation; calls `UserSessionService.end_sessions(reason="password_changed")` in the same commit
- `set_password(session, user, new_password, keep_*)` - Set password for OAuth-only users; `end_sessions(reason="password_set")`
- `update_user` with a `password` key (admin set) calls `end_sessions(reason="admin_password_set")`, keeping nothing
- `disable_all_factors(..., commit=True)` - `commit=False` lets the Google wipe land in its caller's transaction
- `reset_password(session, token, new_password) -> User` - Token-based password reset (`end_sessions(reason="password_reset", include_api_tokens=True)`); refuses a never-claimed invited account whose invitation is not pending (as `"Invalid token"`), then calls `AccessPolicyService.require_password_auth` once the token resolves its owner. **Returns the `User`** since phase 3 (it used to return `None`), so the route has an identity to settle the invitation against instead of decoding the same token a second time
- `recover_password(session, email) -> bool` - Generate reset token and send email. **Every** refusal is a silent no-op — see the route contract above. The whole token/template/send/commit block is inside one handler, because it runs *only* for an address that has an account: a raise anywhere in it would be a 500 for a known address beside a 200 for an unknown one, which is precisely the leak the method exists to close. The handler calls `restore_session` before returning, since its commit sits inside the `try`
- `_claim_refused(session, user) -> bool` - Deferred-import shim over `InvitationService.claim_refused` (`InvitationService` imports this module for `create_account`, so the edge only goes one way at import time). Nothing else: the fail-closed net lives on the predicate, which serves three doors, so a second `try` here would be a second implementation of that policy
- `create_external_user(session, email, confirmed, provenance, passwordless=False)` - Get-or-create a user from an externally-arriving sender address (email integration, server channels). Uses the `external` origin, so the registration policy does not apply — the integration's own sender allowlist is the gate. Still picks up the policy's default role. Normalises the address *before* the get-or-create lookup so the idempotency check asks about the same address the chokepoint will store; the two branches now differ only in whether a password hash exists at all

### AuthService (`backend/app/services/users/auth_service.py`)
- `create_user_access_token(user, *, web_session_id=None, expires_delta=None)` - The one minting path for user JWTs (web logins, invitation accept, the account-API proxy's inner token): stamps `sv`, `iat` and optional `sid`. Replaces `create_access_token(user_id)`. `sv` is read from `user` as it is now, so commit any `end_sessions` first
- `link_google_account(..., wipe_password=False)` - With `wipe_password` (set when the auto-linked account was **unconfirmed**) clears the password, all MFA enrollment and ends every session and API token in the link's commit; see [Google OAuth](google_oauth.md)
- `create_user_from_google(...)` - Calls `AccessPolicyService.can_register(origin=AccountOrigin.GOOGLE)` first — kept here as well as inside the chokepoint because this is the refusal the OAuth callback route turns into a 403, and it must happen before anything is written. Then delegates to `create_account(origin=google, email_confirmed=True)`
- `authenticate_with_google(...)` - The Google-claim email is now lowercased + stripped **before** the auto-link lookup. Google may return a mixed-case address for a Workspace account whose platform row was stored lowercase; `get_user_by_email` is now case-insensitive, but the create path and the lookup must still agree on one canonical form
- See [Google OAuth Tech](google_oauth_tech.md) for OAuth-specific methods
- `is_email_domain_allowed(email)` was **removed** — subsumed by `AccessPolicyService.can_register`

### AccessPolicyService (`backend/app/services/users/access_policy_service.py`)
The single resolver for the front-door policy. Auth-relevant entry points:
- `can_register(session, *, email, origin: AccountOrigin)` - Registration gate for `signup` / `google`; `admin`, `invite`, `external` and `seed` always pass. Takes the `AccountOrigin` enum (the phase-1 `ORIGIN_*` string constants are gone). An origin that is neither ungated nor explicitly gated raises, so a new arrival path cannot silently inherit "no policy applies"
- `require_password_auth(session, user)` - Raises `PasswordAuthDisabledError` unless `password_auth_enabled or user.is_superuser`. Used by login, reset, set-password and change-password. Password **recovery** uses the predicate directly so it can skip silently
- `can_change_email(session)` - Drives `UserPublic.can_change_email` and the `PATCH /users/me` email branch
- `is_account_valid(user)` - Today `user.is_active`; called by `deps.get_current_user` only
Full contract in [Access Policy — tech](../server_configuration/access_policy_tech.md)

### InvitationService (`backend/app/services/users/invitation_service.py`)

Pure derivations:

- `status_of(invitation) -> str` — **the one place** a status is derived. `accepted` → `revoked` → `expired` → `pending`, and the ordering is fixed so a revoked-then-expired row does not read differently depending on which check ran first. Every lifecycle gate asks it too rather than reading `accepted_at` directly
- `password_accepted(policy, user) -> bool` — delegates to `AccessPolicyService.is_password_auth_allowed`, deliberately rather than restating `policy.password_auth_enabled or user.is_superuser`
- `effective_auth_hint(policy, user, invitation) -> str` — which method the *email* leads with, resolved at send time and never stored. Narrows to the one available method when only one is, otherwise honours the admin's stored preference. Returns `any` when neither is available, which is a reachable state (`google_auth_enabled` is derived from the environment, while the lockout validation guards only the *transition* into Google-only)
- `mask_email(email)` — `alice@acme.com` → `a***@acme.com`; a one-character local part masks to `***@…`
- `build_accept_url(token)` — `{FRONTEND_HOST}/accept-invite?token=…`

Reads:

- `get_for_user(session, user_id)`, `to_public(session, invitation)` (resolves `invited_by_email` with one extra lookup, since there is no relationship to traverse)
- `status_map(session, user_ids) -> dict[UUID, str]` — one `IN` query for a whole page. `read_users` calls this once and passes `invitation_status=` per row into `user_to_public`; a caller that loops it per user has reintroduced the N+1 it exists to prevent
- `is_unclaimed(user)` — `hashed_password is None and google_id is None`. A fact about the **row**, deliberately not about the invitation, because `mark_accepted_if_pending` is non-raising and can legitimately leave a claimed account reading `pending`
- `claim_refused(session, user)` — unclaimed **and** has an invitation **and** its status is not `pending`. **The one answer to "may this account still be claimed", asked by all three claim doors**: password recovery, password reset, and the Google auto-link in `AuthService.link_google_account`. (The invite link is the fourth way in and asks the same question one step earlier, in `_resolve`, from the token.) Checks "claimed" first, which is what makes the rule expire on its own: once someone accepts, their invitation reads `accepted` forever and must never take recovery — or a Google link — away from an account in daily use. **Never raises**; an unexpected error is answered as "refused", because every caller's refusal is already indistinguishable from its ordinary output while allowing on an error would reopen the bypass at all three doors
- `is_interrupted_invite(session, user)` — unclaimed **and** has no invitation row: the wreckage of an `invite` that committed the account and then failed before writing the invitation. Too much for a re-invite (the duplicate check refuses it) and too little for resend (there is no row to rotate), so the admin's only exit was deleting the user. Re-submitting the wizard adopts the row instead. One other thing fits this shape — a passwordless `create_external_user` row (an inbound channel sender) — and adopting it is a reasonable thing for an admin to do, so it is *disclosed* rather than refused: `adopted_existing_account` is logged, stamped into the audit event, **and returned on `InviteUserResponse`** so the wizard does not announce a brand-new account about a row that already existed. `_resume_interrupted` writes a field from the new submission **if and only if the submission stated it**, and "stated it" is exactly `is not None` — one uniform test, no per-field special cases. That is only possible because `InviteUserRequest` makes omission representable: `is_active` is `bool | None` (a `bool = True` default silently reactivated an account an administrator had deliberately deactivated, from a wizard that shows no active toggle), and a blank or whitespace-only `full_name` is normalised to `None` by a field validator at the edge (it is an omission spelled differently, and an admin who typed only an address erased the name an adopted row carried). A brand-new account still defaults to active — that default belongs to `create_account`

Lifecycle (all `async def`, because each emits a security event):

- `create_invitation(session, *, user, invited_by, include_desktop, auth_hint, expires_in_days=None) -> (row, token)` — one function returns both, and the token is minted from the **committed** row's `jti`. Splitting them is how you get a token that passes signature and purpose verification and then fails the row lookup, which is by design indistinguishable from a forgery: every recipient sees "no longer valid" and no log says why
- `send(session, *, invitation, user, token) -> bool` — **never raises**; the bool is the whole error channel. Re-resolves the access policy on every send. Suppressed (returning `False`, and *not* stamping `last_sent_at`) when mail is unconfigured, when the account is deactivated, and when the policy offers the person no sign-in method at all — the email template has password/google/either branches and no "neither"
- `invite(session, *, admin, data) -> InviteResult` — account (chokepoint, `origin=invite`, `skip_auto_provision=True`, `is_superuser` **derived** from the role) → `AccountProvisioningService.provision_explicit` → invitation row + token → mail → two audit events (the invited account's feed and the acting admin's)
- `resend(...)`, `revoke(...)`, `issue_link(...)`, `record_link_issued(...)` — see the route contracts above. `resend` checks the cooldown *before* mutating anything, so a refused resend leaves the live token alone; rotation, once done, is **not** rolled back when the send fails, because the new link is the request's actual product and is handed back to the admin
- `_resolve(session, token) -> (invitation, user) | None` — every check a token must pass, in one place, returning a single `None` for eight distinct reasons: bad signature/expiry/purpose, unparseable `jti`, no row for that `jti`, status not `pending`, missing or deactivated user, an account someone has already claimed, and an address mismatch. Ordered by cost — purpose and signature before any database access, then one indexed SELECT
- `lookup(...)`, `accept_with_password(...)`, `mark_accepted_if_pending(session, user, *, method) -> bool`

Audit event types, all free-form strings on `SecurityEventCreate`. Into the **invited account's** feed: `user.invitation.created`, `.resent`, `.revoked`, `.accepted`, `.link_issued`. Into the **acting admin's** own feed, and only from `invite`: `admin.user_invitation.create` — a distinct type, named in the `admin.` namespace after the object acted on, matching `admin.managed_ai_credential.create`. The pair carries the same `details` (that is what correlates them) but never the same `event_type`: emitting `user.invitation.created` into both feeds records one fact twice, and an admin onboarding fifty people accumulates fifty rows in their own feed that read as if fifty accounts had been created *for them*. `_emit` is best-effort and never raises — every caller has already committed the thing the event describes, so an unwritable audit row must not take it away.

#### The unclaimed-account check inside `_resolve`

`_resolve` refuses a token whose account is no longer `is_unclaimed`, and that line is load-bearing rather than defensive. A `pending` invitation is not proof the offer is outstanding: `mark_accepted_if_pending` is non-raising by contract, so one transient failure on the Google or reset path leaves a claimed account with a `pending` row. Without this check a live invite link would be a password-overwrite primitive on an account already in use — and the accept route mints a session token with no second-factor branch, on the premise that a redeeming account cannot have enrolled one, a premise only this line makes true.

#### The two hook sites

Both settle an invitation for someone who has **already** proved who they are, so both are built to the same rule: *invitation bookkeeping must never cost someone an authentication they already completed.*

- `POST /reset-password/` calls the hook **after** its `except` block. That net maps an unrecognised `ValueError` to a **404**, so a bookkeeping failure raised inside it would answer a successful password reset with "the user does not exist"
- `POST /auth/google/callback` calls it **outside** the route's blanket `except Exception → 400 "OAuth error"`, and only on the `LoginToken` arm — an `MfaChallenge` means the person has not finished authenticating, and settling there would record an acceptance that never happened. The route was restructured to assign a `response` variable inside the `try` and return it afterwards, rather than returning from three points inside it

`mark_accepted_if_pending` is non-raising *by construction*, not merely wrapped in a `try` at the call sites — the same shape as `AccountProvisioningService.on_account_created`: identifiers are snapshotted while the session is known good (on an aborted transaction even `user.id` is a query, so a handler that reads one to build its log line throws from inside itself), `restore_session` runs **first** in the handler before any logging or audit write, and the session is left usable because both callers commit afterwards. It settles only a `pending` invitation; an expired or revoked one is left as it stands, so the admin's revocation is not undone by the person arriving through another door.

### Invitation tokens (`backend/app/utils.py`)

```python
generate_invitation_token(*, email: str, jti: str, expires_at: datetime) -> str
verify_invitation_token(token: str) -> tuple[str, str] | None   # (email, jti)
generate_invitation_email(*, email_to, full_name, accept_link, web_link,
                          desktop_link, auth_hint, invited_by_name,
                          expires_in_days) -> EmailData
```

Claims: `{exp, nbf, sub, jti, purpose="invite"}` — the **confirmation-token** pattern, not the reset-token one. `verify_invitation_token` checks `purpose` first (a purposeless reset token would otherwise validate here — same key, same algorithm), then requires both `sub` and `jti`, and returns `None` for every failure without distinguishing them. `exp` is the invitation row's own `expires_at`, so link and row expire at the same instant by construction rather than by two settings agreeing.

Both halves of the return value are used, for different things: the **`jti` resolves the invitation**, and the address is then compared against the resolved account's own as an independent second check.

The case-folding in that second check is deliberate, and so is the fact that nothing on this path looks an account up by address. The column is a plain `VARCHAR(255)` (no citext), so case-insensitivity comes from the query (`func.lower(User.email)`), not the schema — see [Email lookups are case-insensitive](#email-lookups-are-case-insensitive). The invite path is built not to depend on that: `invite` lets `create_account` be the normalising authority instead of keying on the caller's string, the token's `sub` is minted from the **stored** `user.email` after normalisation, and `_resolve` resolves by `jti` — so a legacy mixed-case row cannot lock itself out of its own invitation, while an admin genuinely *changing* the address still invalidates the outstanding link.  Do not drop the folding on the invite path just because the general lookup is now case-insensitive.

One interaction worth knowing while that work is outstanding: since `POST /password-recovery/{email}` answers a generic `200` for an unknown address instead of the old `404`, a mixed-case recovery attempt is now **silent** where it used to be a visible error. The recovery change is correct non-enumeration behaviour and not a regression — it just makes the case-sensitivity defect quieter to hit.

`generate_invitation_email` is given the **effective** hint (resolved against the policy at send time), and `desktop_link` is `None` when the invitation does not include the desktop block, so the template branches on presence rather than on a second boolean that could disagree with it. The template pair is `email-templates/src/invite.mjml` + `build/invite.html`; the build artefact is **hand-derived** from `new_account.html` rather than compiled, because `invite` is a legacy hand-edited template that matches no mjml version (see [mjml version pin](../system_notifications/system_notifications_tech.md#mjml-version-pin)).

#### `verify_password_reset_token` now rejects any `purpose` claim

A password-reset token carries no `purpose` claim and — for backward compatibility with links already sitting in people's inboxes — never will, so this verifier cannot *require* one the way `verify_email_confirmation_token` does. What it can do, and now does, is **refuse every token that carries one**: a `purpose` claim means the token was minted for a different flow, and that flow's own verifier is the only one entitled to accept it. It also now requires a non-empty `sub`.

This is a rejection rather than a requirement, which is why legacy reset links still verify.

Without it the function accepted any HS256 token signed with `SECRET_KEY` that had a `sub` — **including the invitation token**. An invitee could have replayed their invite link at `POST /reset-password/` and set a password there, so revoking the invitation would have stopped nothing, resend's `jti` rotation would have invalidated nothing, and the single-use property that makes an accepted invitation terminal would have been lost. A leaked invite link would have been a week-long account-takeover primitive.

### Login and recovery timing

Two changes and one recorded residual.

**Login (closed).** `UserService.authenticate` used to return before any hashing for an unknown address *and* for an account with no `hashed_password`, so a fast answer meant "unknown, or passwordless" while a real password account paid the full bcrypt cost. Phase 3 is what makes that matter: **every unaccepted invitation is a `hashed_password=None` account**, so this phase would otherwise have turned a narrow leak into one covering every person an admin has invited. Both branches now call `_burn_password_verification(password)`, which verifies against a lazily-generated, cached dummy hash. Two supporting details:

- `_safe_verify_password` wraps every comparison, because passlib raises `PasswordSizeError` past `MAX_PASSWORD_SIZE` (4096 bytes; bcrypt's 72-byte limit is only a silent truncation). Uncaught, that would be a 500 on exactly one of the two branches while the other answered 400 — an oracle whichever branch it landed on
- if the dummy-hash generation itself fails, the cache is left unset, so the next miss retries. The failure mode is a *slower* miss, never a faster one

The cost is only exactly equal while every stored hash uses the current bcrypt cost factor. `pwd_context` is single-scheme today; raising the cost later would leave legacy hashes verifying more cheaply than the dummy.

**Recovery (status and body closed, timing recorded).** `POST /password-recovery/{email}` no longer 404s, and every refusal is a silent no-op. What that does **not** close is wall-clock time: the known path performs a synchronous SMTP round-trip plus a row write, the unknown path returns after one SELECT, and the 300 s cooldown then makes the *second* request fast for a known address too — slow-then-fast means the address exists. `POST /resend-confirmation/{email}` has the same shape. Closing it means moving the send off the request path; an artificial delay was deliberately not added, because it would trade a real signal for a fabricated one.

**Volume (closed by the login throttle).** `POST /login/access-token` is rate limited per source and by failed attempts per account; see the Configuration table. The limiter is in-process, so the limits are per worker. The throttle runs before the password is checked, so a locked account answers 429 even for the right password.

### Security (`backend/app/core/security.py`)
- `create_access_token(subject, expires_delta)` - JWT creation (PyJWT, HS256, SECRET_KEY)
- `verify_password(plain, hashed)` - Bcrypt comparison via passlib
- `get_password_hash(password)` - Bcrypt hashing via passlib

### Dependencies (`backend/app/api/deps.py`)
- `TokenDep` - OAuth2PasswordBearer extracting JWT from Authorization header
- `get_current_user(session, token)` - Decode JWT (expired → 401, forged → 403), then `_authenticate_user_payload`: agent-env reject, `TokenPayload` shape, user lookup, account validity via `AccessPolicyService.is_account_valid` (today: `is_active`; the single seam a future per-request rule lands in), desktop client revocation, and `UserSessionService.is_token_current` (`sv`, 401). `authenticate_user_jwt`, `get_current_user_or_guest` and the websocket resolver share it via `UserTokenRejected`. Other token families in `deps` still test `is_active` inline
- `CurrentTokenClaims` - the caller's decoded claims, used to find the session to keep on a password change

### Web sessions and revocation
- `WebSessionService` (`services/users/web_session_service.py`): `create`, `find_live_session`, `rotate` (locks the `user_web_session` row, then re-reads the token row; CSRF is checked before anything is written; grace-window replay returns without rotating), `verify_csrf` (constant time), `revoke`, `revoke_all_for_user`, `cleanup_expired` (deletes sessions ended over a day ago; run every 15 minutes by `backend/app/services/users/web_session_scheduler.py`, started and stopped from `main.py`)
- `UserSessionService` (`services/users/user_session_service.py`): `is_token_current`, `kept_sessions_from_claims`, `end_sessions`, `revoke_api_tokens_for_user`. Reasons: `password_changed`, `password_set`, `password_reset`, `admin_password_set`, `google_link_wipe`, `sign_out_everywhere`; each writes a `SESSIONS_REVOKED` security event
- `LoginThrottle` (`services/users/login_throttle_service.py`): `check_password_attempt`, `record_failure`, `record_success`, `check_native_token`, `reset`; HTTP mapping in `api/routes/_login_throttle_http.py`
- Models: `User.session_version` (starts at 1), `UserWebSession`, `UserWebRefreshToken` (`models/users/user_web_session.py`, hashes only)
- `SecurityHeadersMiddleware` (`core/security_headers_middleware.py`): sets `nosniff`, `Referrer-Policy` and (https only) HSTS where absent; never `X-Frame-Options` or CSP; skips `nosniff` on agent-served content paths
- `CurrentUser` - Annotated dependency for authenticated user
- `get_current_active_superuser(current_user)` - Admin-only guard (403 if not superuser)
- `get_current_user_or_guest(session, token)` - Resolves both regular user and guest JWT types

## The `model_dump()` Expiry Trap

`user_to_public()` calls `_load_missing_columns(user)` before its `model_dump()`, and the reason is a real 500 that phase 2 armed.

`model_dump()` is the one way of reading a SQLModel row that does **not** go through SQLAlchemy's instrumented descriptors — it reads `__dict__`. Ordinary attribute access on an expired instance silently emits a `SELECT`; `model_dump()` just returns whatever keys happen to still be in `__dict__`, which right after a `commit()` is none of them. `id` and `email` are then simply absent from the payload and `UserPublic` fails validation.

`create_account` refreshes the new row and then runs auto-provisioning, which commits a child credential and an audit row; the account-creating routes hand the now-expired instance straight to the builder. It stayed invisible because with outbound email configured the routes call `send_confirmation_email(user=user)` in between, which touches an attribute and un-expires the instance. Turn SMTP off — the default for a fresh install — and creating an account 500s *after* the row is committed: the person exists and is told they do not.

Implementation notes:

- The condition is "a mapped column key is missing from `__dict__`", **not** "the instance is expired": a `load_only()` / deferred load produces the same absence without ever setting the expired flag.
- Restricted to `mapper.column_attrs`. `state.unloaded` is the tempting shortcut and the wrong one — it always contains relationship keys, which `model_dump()` never reads, so it would force a full-row `SELECT` for every row of the users list. As written, list rows come straight off a `select` with every column loaded, the missing set is empty, and no SQL is emitted.
- Reloads through the instance's own `state.session` (a refresh through a foreign session raises), and only when `state.persistent` — a *pending* row has no database row to refresh and its `__dict__` is already authoritative, a *transient* row has neither, and a *detached* expired row cannot be repaired at all (the builder's contract is a live row belonging to `session`).
- The repair lives at the single builder rather than at the one call site, because the trap belongs to `model_dump()`, not to account creation: any commit anywhere upstream arms the same failure.

## Architecture Test

`backend/tests/architecture/account_creation_chokepoint_test.py` walks the backend AST (excluding `env-templates/` and `alembic/`) and pins:

1. `User(...)` is constructed in exactly one place — `services/users/user_service.py::create_account`.
2. No code path builds the row the other way, by validating another model into it.
3. Every call into the chokepoint names an `origin`, and `core.db.init_db` is the only caller carrying `AccountOrigin.SEED`.
4. `add_members` keeps `actor` keyword-only with no default, and no route module calls it with `actor=None` — `actor=None` means *the system*, and a route is never the system.
5. Neither file phase 2 owns imports a provider client (a cheap static echo of the "no third-party provider on the login path" invariant).

Rule 4 is the one phase 3 had to keep green: routing the invite wizard's explicit credential list through `AccountProvisioningService.provision_explicit` — rather than looping `add_members` in the route — is what keeps the sanctioned `actor=None` call site at exactly one, inside that service, while the invite path threads the acting admin through as `actor=admin`.

An anchor test asserts that every hand-written name the walk depends on still exists, so a rename cannot quietly empty the checks.

## Frontend Components

### useAuth Hook (`frontend/src/hooks/useAuth.ts`)
- `isLoggedIn()` - Whether an in-memory access token exists (`getAccessToken() !== null`; does not verify with the backend). `bootstrapSession` restores the token after a reload when the stored CSRF token says a cookie session may exist. Session state lives in `frontend/src/lib/authSession.ts` (in-memory token, single-flight refresh within a tab and across tabs via `navigator.locks` + `BroadcastChannel("cinna-auth")`, `getFreshAccessToken`, `bootstrapSession` with a 5 s timeout, `logoutSession` waiting up to 2 s) and `authSessionClient.ts` (transport and 401-refresh-retry). No user JWT is written to Web Storage; a legacy `localStorage["access_token"]` is discarded
- `ensureSessionValid(returnTo)` - For use inside a route's `beforeLoad` on public consent/authorize pages. Verifies the token via `LoginService.testToken()`; on 401/403/404 clears the token and throws a TanStack `redirect({ to: "/login", search: { redirect: returnTo } })`. Re-throws other errors. Used by `oauth/mcp-consent.tsx` and `desktop-auth/consent.tsx`
- `redirectToLoginPreservingTarget()` - Imperative variant for in-page error handlers (e.g. mutation `onError`). Clears the token and navigates the browser to `/login?redirect=<current-url>` after passing the current location through `safeRedirectPath`
- `user` query - `useQuery(["currentUser"], UsersService.readUserMe())` with auto-logout on 401/404
- `loginMutation` - Password login via `LoginService.loginAccessToken()`, stores token, then calls `navigateToPostAuthTarget(readRedirectFromUrl())`
- `signUpMutation` - Registration via `UsersService.registerUser()`; on success navigates to `/login`, preserving the current `?redirect=` param
- `logout()` - Drops the in-memory session, calls `POST /login/session/logout` (waits up to 2 s), navigates to /login
- `frontend/src/components/UserSettings/SignOutEverywhere.tsx` - confirm dialog in `PasswordCard`; calls `LoginService.sessionRevokeAll()` then `window.location.assign("/login")`
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
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `config.py` | Access token lifetime (default: 15) |
| `WEB_REFRESH_TOKEN_EXPIRE_DAYS` | `config.py` | Sliding browser-session lifetime (default 14) |
| `WEB_REFRESH_TOKEN_REUSE_GRACE_SECONDS` | `config.py` | Window in which a rotated refresh token still yields an access token (default 60) |
| `WEB_SESSION_COOKIE_SECURE` | `config.py` | `Secure` flag of `cinna_refresh`; unset = on unless `ENVIRONMENT=local` |
| `SECURITY_HSTS_MAX_AGE_SECONDS` | `config.py` | HSTS max-age the backend sends on https responses (default 31536000; 0 disables) |
| `LOGIN_RATE_LIMIT_ENABLED` | `config.py` | Master switch of the login throttle (default True; off in the test conftest) |
| `LOGIN_RATE_LIMIT_PER_SOURCE_PER_MIN` | `config.py` | Password-login attempts per source per minute (default 30) |
| `LOGIN_FAILURES_PER_ACCOUNT_SOURCE` / `LOGIN_FAILURES_PER_ACCOUNT` / `LOGIN_FAILURE_WINDOW_SECONDS` | `config.py` | Failures that trigger a lockout: 5 per account+source, 20 per account, inside 900 s |
| `LOGIN_LOCKOUT_BASE_SECONDS` / `LOGIN_LOCKOUT_MAX_SECONDS` | `config.py` | Lockout length `BASE * 2^(failures - threshold)`, capped (30 s, 900 s) |
| `NATIVE_TOKEN_RATE_LIMIT_PER_SOURCE_PER_MIN` | `config.py` | Calls per source per minute to `/desktop-auth/token` and `/app-auth/token` (default 60) |

On the local stack every request shares one source, so `.env.example` carries commented-out loosened values for the two per-source limits (300 and 600); never use them in production.
| `AUTH_WHITELIST_USER_DOMAINS` | `.env` / `config.py` | **Seed only** — converted to access-policy glob patterns on first boot and by migration `1d737d7ef0a0`. No live decision reads it; a startup warning names the admin page when it is still set |
| `ACCESS_POLICY_RATE_LIMIT_PER_MIN` | `config.py` (default 120) | Per-IP budget for the public access-policy projection |
| `INVITATION_EXPIRE_DAYS` | `config.py` (default 7) | Lifetime of an invitation **and** of its token — one number, since the token's `exp` is the row's `expires_at` |
| `INVITATION_RESEND_COOLDOWN_SECONDS` | `config.py` (default 300) | Minimum seconds between resends of the same invitation. Per row, off `last_sent_at` — not a global throttle; what is being protected is one person's inbox |
| `INVITATION_RATE_LIMIT_PER_MIN` | `config.py` (default 30) | Per-IP budget for the two anonymous invitation endpoints. Lower than the access-policy projection's because these take an attacker-chosen token and each costs an indexed SELECT |

`settings.auth_whitelist_domains` and `settings.allow_user_email_change` were removed; the live answers are `AccessPolicyService.is_email_allowed` and `AccessPolicyService.can_change_email`.
| `EMAILS_ENABLED` | `config.py` | Whether password recovery emails are sent |

## Security

- **JWT**: HS256 with 32-byte random SECRET_KEY, 15-minute expiry, `sv` revocation, in-memory in the SPA; refresh by `HttpOnly` `SameSite=Strict` cookie plus CSRF token and `Origin` allowlist
- **Passwords**: Bcrypt hashing via passlib CryptContext
- **Token validation**: `get_current_user()` verifies signature, expiration, and user active status
- **Access policy**: Enforced at registration for both password and Google paths, and on every password path for sign-in method. Reason codes are stable machine-readable strings, never prose
- **Non-enumeration**: signup refuses on policy before the duplicate check; password recovery answers one generic 200 for every outcome (status and body identical for a known and an unknown address, no `detail` key); the Google callback answers with the same 403 shape as signup; both public invitation endpoints answer identically for a forged token and a real-but-unusable one, and neither sends mail
- **Cross-purpose token replay**: invitation tokens are stamped `purpose="invite"` and checked before anything else; `verify_password_reset_token` rejects any token that carries a `purpose` claim at all, which is what stops an invite link being redeemable at `POST /reset-password/`
- **Revocation means unclaimable**: password recovery and reset both refuse a never-claimed account (no password hash *and* no Google identity) whose invitation is not `pending`
- **Lockout prevention**: Cannot unlink Google without password set; cannot delete superuser self; superusers keep the password break-glass when password sign-in is off; `PUT /admin/server-config` refuses to turn password sign-in off unless an administrator can use Google *and* an administrator has a password to fall back on
