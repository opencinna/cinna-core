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
| **Account-Creation Chokepoint** | `UserService.create_account` — the one and only place a `User` row is built, whatever the arrival path. Owns address normalisation, the `is_superuser ⇒ admin` invariant, the policy-derived default role, and auto-provisioning of company AI credentials |
| **Account Origin** | `AccountOrigin` — which arrival path an account came from: `signup`, `google`, `invite`, `admin`, `external`, `seed`. Required by the chokepoint; decides whether the registration gate applies, and is stamped on the provisioning audit event |
| **Invitation** | An administrator's offer of a pre-created, passwordless account. Exactly one `user_invitation` row per account, re-armed in place rather than duplicated. Its status is **derived, never stored** — `accepted` → `revoked` → `expired` → `pending`, in that precedence order |
| **Invitation Token** | The HS256 JWT the invite link carries: `purpose="invite"`, `sub` = the invited address, `jti` = the row's `token_jti`, `exp` = the row's `expires_at`. Verification resolves the invitation **by `jti`**, so resending rotates the live link and every link issued before it stops working |
| **`password_accepted`** | The single server-side answer to "may this invitee set a password" — `AccessPolicyService.is_password_auth_allowed(policy, user)`, the same predicate login uses. Surfaced as one derived boolean on the invitation lookup; the accept page renders its password form on that boolean and on nothing else |
| **Auth Hint** | `any` / `password` / `google` on an invitation. **Presentational only**: it decides which method the email and the accept page *lead with*. It never enables a method the policy forbids and never hides one it allows |
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
6. Backend creates the user through the [account-creation chokepoint](#the-account-creation-chokepoint) with hashed password and the policy's default role, and grants any company AI credentials configured for that role
7. Confirmation email sent (if SMTP configured)
8. User redirected to login page

### Inviting a User (Admin)

1. A superuser opens **Admin → Users** and clicks **Invite user**. (Creating an account with an admin-chosen password is still there, one step back, as **Create with password**.)
2. Step 1 collects the address, an optional full name, the role, the sign-in method to lead with, and whether to send the email at all
3. Step 2 shows the AI credentials the new account will start with — pre-ticked from exactly the same `auto_provision_roles` predicate a self-registering account of that role would have been granted by — plus an "Also invite to Cinna Desktop" checkbox defaulting to the instance's `invite_include_desktop_default`
4. On submit the backend creates the account through the [chokepoint](#the-account-creation-chokepoint) with `origin=invite`, **no password**, and `is_superuser` derived from the role; grants the chosen credentials through `AccountProvisioningService`; writes the `user_invitation` row; mints the token from the committed row; and attempts the email
5. The success screen shows the accept link (always — SMTP is not required), whether the email went out, and what was provisioned. Neither a provisioning failure nor a mail failure can fail the invite: the account exists either way
6. The row appears in the users table with a **Status** of *Invited*. Its action menu carries **Resend invitation**, **Copy invite link** and **Revoke invitation**

### Accepting an Invitation

**By password.**

1. The invitee opens `/accept-invite?token=…`
2. The page posts the token to `POST /invitations/lookup` (body, not URL) and gets back the masked address, the name to pre-fill, which method to lead with, and `password_accepted`
3. The password form is rendered **iff `password_accepted`**; the Google button is rendered iff the instance and this frontend build both have Google configured
4. Setting a password calls `POST /invitations/accept`, which sets the hash, stamps `accepted_at`, **confirms the email address** (clicking a link only the invitee received proves control of it), and returns the same `LoginResponse` shape `POST /login/access-token` does
5. The browser stores the token and lands on `/accept-invite/done`, which offers the dashboard and — when the invitation included it — a link to Cinna Desktop

**By Google.** The accept page's Google button routes through the ordinary `/login?redirect=/accept-invite/done` flow. The pre-existing auto-link-by-address inside `AuthService.authenticate_with_google` claims the account; the callback route then settles the invitation. The account keeps `hashed_password = None` and is nonetheless *claimed*.

**By password reset.** An invitee who used "forgot password" instead of their invite link has still proved control of the address, so completing `POST /reset-password/` settles the invitation too.

In both of those last two cases the bookkeeping is a **side record**: if it fails, the person is still signed in and the invitation simply stays `pending` for the admin to see and resend. It can never cost someone an authentication they already completed.

### Password Recovery

1. User enters email on recovery page
2. The response is always the same 200 and the same body — "If an account exists for that email, a password recovery email has been sent" — whether or not the address has an account. **Every** reason not to send is a silent no-op: unknown address, deactivated account, password sign-in off for this user (saying otherwise would identify which addresses belong to superusers), an unclaimed invited account whose invitation is no longer pending, the 300 s cooldown, outbound mail unconfigured, and a delivery or template failure
3. Backend generates time-limited reset token
4. Backend sends email with reset link containing token
5. User clicks link, enters new password
6. Backend validates the token, refuses a never-claimed invited account whose invitation is not `pending` (as "Invalid token"), re-checks the same policy gate for the token's owner (403 `password_auth_disabled` for a gated non-superuser), then hashes and saves the new password
7. User redirected to login page

### Set Password (OAuth Users)

1. OAuth-only user (no password set) navigates to profile settings
2. User enters desired password
3. Backend sets password, enabling dual authentication

### Logout

1. User clicks logout
2. Frontend removes `access_token` from localStorage
3. Frontend redirects to login page

### The Account-Creation Chokepoint

Every arrival path ends in the same function, `UserService.create_account`:

```
signup | google | invite | admin | external | seed
                    │
                    ▼
UserService.create_account(session, *, email, origin, password, full_name,
                           username, google_id, role, is_superuser, is_active,
                           email_confirmed, skip_auto_provision) -> User
                    │  normalise address → policy gate → duplicate check →
                    │  resolve role → INSERT → commit
                    ▼
AccountProvisioningService.on_account_created(session, user, origin)
```

Before this there were **four** places that built a `User` row — the signup path, the Google path, the passwordless branch of `create_external_user`, and the local-dev `/private/users/` helper — and they already disagreed: only one normalised the address, only two derived the default role through `RoleService`, none agreed about `email_confirmed`. An architecture test now pins that `User(` is constructed in exactly one function, that no code path builds the row by validating another model into it, and that every call into the chokepoint names an origin.

What the chokepoint owns:

- **Address normalisation is part of identity.** The address is stripped, lowercased and validated, and the duplicate check runs on the *normalised* form — `Alice@x.com` and `alice@x.com` are one human, and Postgres' unique index is case-sensitive. A caller that checked the raw string would otherwise produce an `IntegrityError` 500 instead of a 400. `POST /users/` and `/private/users/` both translate the resulting `ValueError` into a `400`.
- **The policy gate is re-asserted, not moved.** `register_user` and `create_user_from_google` still call `can_register` *before* their own duplicate check — that ordering is what keeps a closed instance answering identically for known and unknown addresses. The second call inside the chokepoint is the structural guarantee that a future path which forgets the gate is refused anyway.
- **`is_superuser` forces `role = "admin"`** even against an explicitly passed role, and auto-confirms the address. Note the asymmetry: `update_user` does **not** re-apply this invariant — creation is stricter than update, and superusers editing the general user form are trusted to keep the two fields consistent.
- **Role validation.** An unknown `role` is a `ValueError`, checked here because this is now the only door.
- **Auto-provisioning**, unless `skip_auto_provision=True` (reserved for a path that applies its own explicit credential list). Provisioning can never cost the person their account — see [Admin-Provisioned AI Credentials](../ai_credentials/admin_ai_credential_provisioning.md#auto-provisioning-at-account-creation).

Origins and their gates are owned by the [Access Policy](../server_configuration/access_policy.md): `signup` and `google` are gated; `admin`, `invite`, `external` and `seed` carry their own admission decision and always pass. An origin that is neither raises — a new arrival path cannot silently inherit "no policy applies".

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

### Invitations

**One row, one live link.**

- Exactly one `user_invitation` row per account, enforced by a unique index on `user_id`. Resend re-arms it in place: new `token_jti`, new expiry, `revoked_at` cleared. It never inserts a second row
- The invitation is resolved **by `jti`**, never by address. That is what makes rotation mean something: the link the previous email carried verifies its signature and then finds nothing. The address on the token is compared against the account's own afterwards, as an independent second check — so an admin who changes the address after inviting invalidates the outstanding link
- Acceptance is **single-use**: the password write and `accepted_at` land in one transaction, so a second attempt with the same link is refused exactly like a forgery
- Invitations expire after `INVITATION_EXPIRE_DAYS` (7 by default). The token's `exp` **is** the row's `expires_at`, so a link can never outlive the invitation or vice versa

**Status is derived in one place.** `accepted` outranks `revoked` outranks `expired` outranks `pending`. Nothing stores a status string; the admin badge, the public lookup and the accept check all ask the same function.

**Validate the transition, not the state.** Every lifecycle guard refuses only what the request actually changes:

- **Resend** is the repair action for *every* non-accepted state. Expiry is what it fixes, and a revoked invitation is reactivated by design (the menu labels it "Send new invitation" there). Only acceptance refuses, with 409
- **Revoke** is idempotent, and revoking an already-*expired* invitation succeeds — expiry is not a state that request transitions. Only acceptance refuses. The row and its `token_jti` are kept rather than deleted, so a revoked token cannot be replayed if the row is later re-armed
- **Copy invite link** reads the outstanding link out *without* rotating it — deliberately not resend, or an admin copying a link for a chat message would silently kill the email the person is about to click. It is refused for anything but a `pending` invitation, because the link for a revoked, expired or accepted one is a dud, and it is audited because handing it over signs the recipient in as that account
- **Inviting** never refuses over `auth_hint`. The hint is presentational and is re-resolved at every send, so it is not this request's business what the policy will be later

**A pre-created account cannot be used before it is claimed.**

- The row is real and active from the moment of the invite, but it has no password: `POST /login/access-token` answers the ordinary "Incorrect email or password"
- Revocation means the account cannot be claimed, not merely that one link stopped working — and that is only true if **every** door asks. Three doors can hand a never-claimed account a way in, and all three refuse one whose invitation is not `pending`: password recovery, password reset, and **signing in with Google** (the auto-link by address). Otherwise the person the admin just un-invited types their address into "forgot password", or simply presses "Sign in with Google", and takes the account while the admin's list still reads *revoked*
- **Expired is the worse half.** Nothing deactivates an account when an offer merely lapses — there is no sweeper — so an expired invitation leaves the account claimable indefinitely unless the doors check. An expired `admin` invitation left a *superuser* account standing open. The same rule covers it, because the doors ask about the invitation's status, not about the account's activation
- "Never claimed" means **neither a password hash nor a linked Google identity**. A Google-accepted invitee is *claimed* — their invitation reads `accepted` forever, and they keep emailed password recovery like anyone else
- Inviting an account with `is_active=false` is supported at the API level (`InviteUserRequest.is_active`, for pre-create-now/activate-later; the wizard does not expose it). The field is `bool | null` and `null` — which is what the wizard sends — means *the submission did not state one*: a brand-new account still defaults to active, while an invite that **adopts** an account that already exists keeps whatever activation state it already had. That is the point of the nullability: re-inviting an account an administrator had deliberately deactivated must not silently reactivate it, and provisioning then reports one `user_inactive` skip per credential the admin ticked so the wizard can say so rather than showing an empty report. No mail is sent to a deactivated account — a link that can only answer "no longer valid" is worse than none, and the recipient cannot tell that from a forgery — but the invite still succeeds, and the returned link works as soon as the account is activated. A suppressed send deliberately does not stamp `last_sent_at`, so it does not arm the resend cooldown for a mail that never left

**Non-enumeration.** Both public invitation endpoints are anonymous and take an attacker-chosen token, so any difference between the answer to a forged token and a real one would answer "does this account exist and is it invited":

- `POST /invitations/lookup` answers `200 {"valid": false}` — and *nothing else*, every other field absent rather than null — for a malformed token, a bad signature, a cross-purpose token, a rotated-away `jti`, an expired / revoked / already-accepted invitation, a deleted or deactivated account, an account someone has since claimed, and an address the admin has changed
- `POST /invitations/accept` answers `400` with one fixed detail for every one of those **and** for "password auth is not available to you". There is no 403 branch on that route at all. Nobody is surprised by it, because the page they came from asked `lookup` first, got `password_accepted=false` from the same predicate, and did not render a form
- Both are rate-limited per caller IP (`INVITATION_RATE_LIMIT_PER_MIN`, 30/min), and **neither ever sends email** — an anonymous endpoint that mails an attacker-chosen address is both a timing oracle and an outbound-mail amplifier. Resend is superuser-only
- The superuser routes can afford to be specific (404 for no invitation, 409 for already accepted, 429 with the cooldown deadline) because `get_current_active_superuser` refuses before any lookup runs — a non-admin gets the same 403 for a user id that exists and one that does not

**Known limitations, recorded rather than hidden:**

- **A residual timing signal remains on password recovery and confirmation-resend.** Status and body carry nothing, but the send is synchronous, so the first probe of a registered address is measurably slower than one of an unregistered address (later probes are fast either way — the cooldown short-circuits them). Closing it means moving the send off the request path; an artificial delay was deliberately *not* added, because it would trade a real signal for a fabricated one
- **Login timing was closed, volume was not.** A failed login now burns the same bcrypt work whether the address is unknown, passwordless or wrong-password — which matters because every unaccepted invitation *is* a passwordless account. `POST /login/access-token` still has no rate limiter; that is recorded, not fixed here
- **The Users table Status column shows "Invited" with no day count.** `UserPublic` carries `invitation_status` but not the expiry, and fetching the expiry per row would be an N+1. The countdown ("Invitation expires in 3 days") lives in the row's action menu, which fetches that one invitation only while the menu is open
- **The resend cooldown is only discoverable by attempting it.** The 429 carries `resend_available_at` and the toast turns it into a local time; the menu item cannot be pre-disabled, because the list projection does not carry `last_sent_at`
- **Inviting an address outside `allowed_email_patterns` is not pre-validated in the browser.** Deliberately: matching that policy client-side would be a second implementation of a policy question. The invite origin is ungated by design, so the server accepts it — see [Access Policy](../server_configuration/access_policy.md)

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
                                          └─► can_register(AccountOrigin.SIGNUP) ──┐
                                                                                   │
Google Button ──→ OAuth Flow ──→ AuthService.authenticate_with_google()            │
                                    └─► can_register(AccountOrigin.GOOGLE) ────────┤
                                                                                   ▼
                                        UserService.create_account(origin=…)  ← the only User(...) 
                                              └─► AccountProvisioningService.on_account_created()
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
- **[Admin-Provisioned AI Credentials](../ai_credentials/admin_ai_credential_provisioning.md)** - `AccountProvisioningService.on_account_created` runs at the chokepoint and grants the company AI credentials configured for the new account's role
- **[User Roles](../user_roles/user_roles.md)** - the chokepoint resolves the new account's role from the access policy (or an explicit value for admin-intent origins) and pins `admin` for superusers
- **[Email Confirmation](email_confirmation.md)** - The invite email is one of the outbound-email gate's exceptions (admin-initiated), and accepting an invitation confirms the address
- **[Desktop One-Click Onboarding](../desktop_onboarding/desktop_onboarding.md)** - An invitation optionally carries the Cinna Desktop block, in the email and again on `/accept-invite/done`; the choice is stored on the invitation row, not re-read from the policy
- **[User Roles](../user_roles/user_roles.md)** - The invite wizard picks the new account's role; `role="admin"` implies `is_superuser` (and auto-confirmation), derived server-side rather than accepted as a separate field
- **Route Protection** - All `/_layout/*` frontend routes require valid authentication
