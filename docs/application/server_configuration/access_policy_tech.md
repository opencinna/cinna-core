# Access Policy — Technical Details

## File Locations

### Backend
- `backend/app/models/server_config/server_config.py` — six access-policy columns on `ServerConfig`, the same fields on `ServerConfigUpdate`, the `AccessPolicyPublic` projection, and the `REGISTRATION_MODE_*` / `VALID_REGISTRATION_MODES` constants
- `backend/app/services/users/access_policy_service.py` — `AccessPolicyService`, the `AccessPolicy` / `RegistrationDecision` frozen dataclasses, the reason-code and origin constants, and the three domain exceptions
- `backend/app/services/server_config/server_config_service.py` — `get_or_create` (with the first-boot env seed) and `update` (calls `validate_update` before applying)
- `backend/app/api/routes/server_config.py` — the public `access-policy` route and its rate-limit dependency; the superuser get/put
- `backend/app/api/routes/_user_public.py` — `user_to_public`, the single builder for `UserPublic` (populates `can_change_email`)
- `backend/app/api/routes/users.py` — signup, `PATCH /users/me` email branch, `_require_password_auth` helper for the password endpoints, list-users projection
- `backend/app/api/routes/login.py` — password login gate, recovery, reset
- `backend/app/api/routes/oauth.py` — Google callback maps `RegistrationNotAllowedError` to 403
- `backend/app/services/users/user_service.py` — `register_user`, `reset_password`, `recover_password`, `update_password`, `set_password`
- `backend/app/services/users/auth_service.py` — `create_user_from_google`
- `backend/app/services/users/role_service.py` — `derive_default_role` reads the policy
- `backend/app/services/common/email_patterns.py` — `match_email_pattern` (shared, fail-closed)
- `backend/app/services/common/rate_limiter.py` — `RateLimiter`, `anonymous_caller_key`, `is_private_peer`
- `backend/app/api/deps.py` — `get_current_user` calls `AccessPolicyService.is_account_valid`
- `backend/app/core/config.py` — `ACCESS_POLICY_RATE_LIMIT_PER_MIN`; `AUTH_WHITELIST_USER_DOMAINS` and `DEFAULT_USER_ROLE` kept as seed-only
- `backend/app/core/db.py` — `init_db` materialises the `ServerConfig` singleton at prestart
- `backend/app/main.py` — startup calls `AccessPolicyService.warn_if_env_overrides_present()`
- `backend/app/alembic/versions/1d737d7ef0a0_add_access_policy_to_server_config.py` — migration (down_revision: `68aab27946e5`)

### Frontend
- `frontend/src/components/Admin/AccessPolicyCard.tsx` — the admin editor card
- `frontend/src/routes/_layout/admin/server-configuration.tsx` — mounts the card on the `access` hash tab
- `frontend/src/hooks/useAccessPolicy.ts` — the shared `["accessPolicy"]` query
- `frontend/src/routes/login/index.tsx` — policy-driven login page
- `frontend/src/routes/signup.tsx` — policy-driven signup page
- `frontend/src/components/UserSettings/UserInformation.tsx` — reads `UserPublic.can_change_email`
- `frontend/src/components/Auth/GoogleLoginButton.tsx` — returns `null` without `VITE_GOOGLE_CLIENT_ID`; the second half of the "is Google usable" condition

### Tests
- `backend/tests/api/server_config/access_policy_test.py` — public projection, rate limit, admin update validation
- `backend/tests/api/auth/access_policy_gating_test.py` — signup / login / Google / recovery / reset gating
- `backend/tests/api/users/users_can_change_email_test.py` — `can_change_email` projection and the `PATCH /users/me` refusal
- `backend/tests/unit/test_default_user_role_service.py` — `default_role`, `is_email_allowed` semantics
- `backend/tests/utils/server_config.py`, `backend/tests/utils/google_oauth.py` — test helpers

## Database Model

### `ServerConfig` — access-policy columns

Added to the existing singleton row (see [Disclaimer — tech](disclaimer_tech.md) for the disclaimer columns).

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `registration_mode` | `str` (varchar 16) | `"open"` | `"open"` or `"invite_only"`. Gates registration only. Any unrecognised value degrades to closed |
| `allowed_email_patterns` | `str` (Text) | `""` | Comma-separated `fnmatch` globs. Empty = no restriction |
| `password_auth_enabled` | `bool` | `True` | When false, non-superusers cannot use any password path |
| `google_auto_register` | `bool` | `True` | Whether a Google sign-in on an unknown email creates an account. Ignored in `invite_only` |
| `default_user_role` | `str` (varchar 32) | `"agent-user"` | `agent-user` or `agent-developer`. `admin` rejected at validation |
| `invite_include_desktop_default` | `bool` | `True` | Pre-ticks the invitation wizard's desktop checkbox. Presentation default only |

None of these participate in the `disclaimer_version` bump.

### `ServerConfigUpdate` — added fields

All optional, all `| None`: `registration_mode`, `allowed_email_patterns`, `password_auth_enabled`, `google_auto_register`, `default_user_role`, `invite_include_desktop_default`.

`None` means "not being changed", never "set this column to NULL" — `ServerConfigService.update` filters unset and `None` values out before copying, because every column behind the payload is NOT NULL.

### `AccessPolicyPublic` (Pydantic, no table)

| Field | Type | Source |
|-------|------|--------|
| `registration_open` | `bool` | `registration_mode == "open"` |
| `password_auth_enabled` | `bool` | column |
| `google_auth_enabled` | `bool` | `settings.google_oauth_enabled` (client id **and** secret configured) |
| `google_auto_register` | `bool` | column |
| `desktop_enabled` | `bool` | `settings.DESKTOP_AUTH_ENABLED` |
| `project_name` | `str` | `settings.PROJECT_NAME` |

Deliberately excludes `allowed_email_patterns` and `default_user_role`.

### `UserPublic.can_change_email`

`bool`, **required with no default**. A permissive default is how a producer that forgets the field ships an instance-wide policy fact as "yes". Built only by `user_to_public` in `backend/app/api/routes/_user_public.py`.

## Service Layer

`backend/app/services/users/access_policy_service.py` — static-method style, same shape as `ServerConfigService`.

### Dataclasses

```python
@dataclass(frozen=True)
class AccessPolicy:
    registration_mode: str
    allowed_email_patterns: str
    password_auth_enabled: bool
    google_auto_register: bool
    default_user_role: str
    invite_include_desktop_default: bool
    google_auth_enabled: bool          # derived from settings, not a column

    @property
    def registration_open(self) -> bool:  # only the literal "open"
```

```python
@dataclass(frozen=True)
class RegistrationDecision:
    allowed: bool
    reason: str | None = None          # None exactly when allowed
```

Frozen dataclasses rather than the ORM row: the policy is read on the login path and on every `UserPublic` projection, sometimes far from the session that produced it, where an attribute access on a detached row would trigger a lazy reload.

### Methods

| Method | Notes |
|--------|-------|
| `resolve(session) -> AccessPolicy` | Reads `ServerConfigService.get_or_create` and merges in `settings.google_oauth_enabled` |
| `to_public(policy) -> AccessPolicyPublic` | Adds `desktop_enabled` / `project_name` from settings |
| `normalize_email_patterns(patterns) -> str` | Canonical form: drop blank entries, strip the rest, rejoin with `", "`. Applied on write (`ServerConfigService.update`) **and** on read (`resolve`), so "is this list empty?" has exactly one answer |
| `is_email_allowed(policy, email) -> bool` | Empty/whitespace pattern string → `True`; otherwise delegates to `match_email_pattern`. The **only** place the shared matcher's fail-closed default is inverted |
| `can_register(session, *, email, origin) -> RegistrationDecision` | See the origin table below. Raises `ValueError` on an origin nobody has reasoned about, rather than guessing a gate |
| `is_password_auth_allowed(policy, user) -> bool` | `policy.password_auth_enabled or user.is_superuser` |
| `require_password_auth(session, user) -> None` | Raises `PasswordAuthDisabledError` unless allowed. The one gate, so the break-glass cannot drift between login, set-password, change-password and reset. Password **recovery** deliberately does not use it — a refusal there must be silent |
| `default_role(session) -> str` | Clamps an out-of-range stored value to `agent-user` |
| `can_change_email(session) -> bool` | `True` only when the pattern string is empty/whitespace |
| `is_account_valid(user) -> bool` | Today `user.is_active`. A seam for the browser-session path only; see the note below |
| `validate_update(session, current, update, acting_user) -> None` | Raises `AccessPolicyValidationError(reason, message)` |
| `warn_if_env_overrides_present() -> None` | Startup warning naming `/admin/server-configuration#access`. Never logs the values |

**`is_account_valid` scope.** Only `deps.get_current_user` (the browser-session path) calls it. The guest, agent-env, account-CLI and owner-resolution token families in `deps`, the login routes' own inactive check, and `AuthService.authenticate_with_google` still test `is_active` inline — they have different trust shapes and are switched over when there is a second rule to switch them for.

### Origins

Module constants: `ORIGIN_SIGNUP`, `ORIGIN_GOOGLE`, `ORIGIN_ADMIN`, `ORIGIN_INVITE`, `ORIGIN_EXTERNAL`, `ORIGIN_SEED`. Strings for now; phase 2 of zero-touch-onboarding replaces them with an `AccountOrigin` enum carried through one creation chokepoint.

| Origin | Gate |
|--------|------|
| `signup` | open mode **and** `password_auth_enabled` **and** pattern match |
| `google` | open mode **and** `google_auto_register` **and** pattern match |
| `admin`, `invite`, `external`, `seed` | Always allowed (`_UNGATED_ORIGINS`) — each carries its own admission decision |

Evaluation order inside `can_register`: registration mode first, then the per-origin method switch, then the pattern match.

### Reason codes

Returned as the HTTP `detail`. Readers compare `detail.split(":", 1)[0]` against the code; the remainder (only ever present on `invalid_email_pattern`) is opaque display text.

| Code | Raised by | HTTP |
|------|-----------|------|
| `registration_closed` | `can_register` (invite-only) | 403 on `/users/signup` and `/auth/google/callback` |
| `email_not_allowed` | `can_register` (pattern miss) | 403, same routes |
| `password_auth_disabled` | `can_register` (signup) and `require_password_auth` | 403 on signup, login, reset, set/change password |
| `google_auto_register_disabled` | `can_register` (google) | 403 on `/auth/google/callback` |
| `google_oauth_not_configured` | `validate_update` | 400 on `PUT /admin/server-config` |
| `no_admin_google_account` | `validate_update` | 400, same route |
| `no_admin_password` | `validate_update` | 400, same route |
| `invalid_registration_mode` | `validate_update` | 400, same route |
| `invalid_default_user_role` | `validate_update` | 400, same route |
| `invalid_email_pattern:<entry>` | `validate_update` | 400, same route — the only composite code |

### Exceptions

| Exception | Base | Carries | Mapped to |
|-----------|------|---------|-----------|
| `AccessPolicyValidationError` | `ValueError` | `.reason` (code), `.message` (human, logged not returned) | 400 in `update_server_config` |
| `RegistrationNotAllowedError` | `ValueError` | `.reason`; `str(exc)` is the code | 403 in signup and the Google callback |
| `PasswordAuthDisabledError` | `ValueError` | `.reason` = `password_auth_disabled` | 403 in the password routes |

### `validate_update` rules

Two scoping rules, both so an unrelated edit never fails on a control the admin was not touching:

- **Shape** is checked only for fields actually submitted (`model_dump(exclude_unset=True)` with `None` filtered out). A stored value that has drifted out of range must not block a disclaimer edit; readers already degrade unknown values conservatively
- **Lockout** is checked only on the *transition* into Google-only mode — `password_auth_enabled` submitted `False` while the stored value is `True`. An instance already in that state stays editable even if Google later becomes unconfigured, because superusers keep the break-glass

Order: shape first (a malformed value is a typo, and naming it is more useful than whatever lockout it also implies), then lockout.

1. `registration_mode` ∈ `VALID_REGISTRATION_MODES` → else `invalid_registration_mode`
2. `default_user_role` ∈ `{agent-user, agent-developer}` → else `invalid_default_user_role`
3. `allowed_email_patterns` → `_first_invalid_pattern` → else `invalid_email_pattern:<entry>`
4. Turning password auth off requires `settings.google_oauth_enabled` → else `google_oauth_not_configured`
5. Turning password auth off requires a Google-linked administrator (`_has_google_linked_admin`) → else `no_admin_google_account`
6. Turning password auth off requires a **password-capable** administrator (`_has_password_capable_admin`: an active superuser with a non-null `hashed_password`) → else `no_admin_password`

Rule 6 exists because `is_password_auth_allowed` returning `True` for a superuser is vacuous when that superuser has no password hash — exactly the state `create_user_from_google` leaves an admin who has only ever signed in with Google. Without it, the sole Google-only admin could throw the switch, and the day the Google client secret rotates both doors are shut: Google rejects them, password login has nothing to verify against, and recovery only works if SMTP happens to be configured. It is a separate code from rule 5 because the remedy is different and one call away (`POST /users/me/set-password`).

**`_first_invalid_pattern(patterns) -> str | None`.** `match_email_pattern` rejects nothing — `fnmatch` compiles any string — so "malformed" is defined here. An entry is rejected when it could never match a real address:

- it contains whitespace (a missing comma, the common paste mistake), or
- it has no `@` and is not a pure wildcard (`set(entry) - {"*", "?"}` is non-empty)

A bare `acme.com` is therefore refused: it looks like it should work and never matches anything. `*` is accepted and means everyone. Blank entries between commas are skipped as harmless — validation deliberately does not refuse an admin's save over a trailing comma. What keeps that tolerance from becoming a hazard is `normalize_email_patterns`, which strips the blanks before the value is stored; see [Why the pattern list is canonicalised](access_policy.md#why-the-pattern-list-is-canonicalised).

**`_has_google_linked_admin(session, acting_user)`.** True when the acting user is a superuser with a `google_id`, or when any active superuser row has a non-null `google_id`.

**`_has_password_capable_admin(session)`.** True when any active superuser row has a non-null `hashed_password`. Does not consider the acting user specially — the question is whether the instance retains a usable break-glass at all.

## API Endpoints

### `GET /api/v1/server-config/access-policy`
- **Auth**: none — the login and signup pages must render the right front door before anyone has a token
- **Response**: `AccessPolicyPublic`
- **Rate limit**: per-IP via `_access_policy_rate_limit`, a `Depends` declared **before** `SessionDep` resolves so a throttled request costs no pool connection and no query. Budget: `settings.ACCESS_POLICY_RATE_LIMIT_PER_MIN` (default 120/min). Over budget → 429 with a `Retry-After` header
- **Caller key**: `anonymous_caller_key(request)` from `backend/app/services/common/rate_limiter.py` — the socket peer, or the **last** `X-Forwarded-For` hop when the peer is private/loopback (see [Local Agent Kit — tech](../local_agent_kit/local_agent_kit_tech.md#rate-limiting-anonymous_caller_key))
- **Query key (frontend)**: `["accessPolicy"]`

### `PUT /api/v1/admin/server-config`
- **Auth**: superuser
- **Body**: `ServerConfigUpdate` — now also carries the six access-policy fields
- **Response**: the full `ServerConfig` row
- **Normalisation**: a submitted `allowed_email_patterns` is canonicalised by `AccessPolicyService.normalize_email_patterns` before it is copied onto the row
- **Validation**: `AccessPolicyService.validate_update` runs **before any field is copied** — a half-applied lockout update is exactly the state an admin cannot recover from. On rejection: 400 with the bare reason code as `detail`; the human message is logged, not returned, so wording stays in the UI where it can be translated
- **Logging**: field *names* only, never values

### Gated auth endpoints

| Endpoint | Gate | Failure |
|----------|------|---------|
| `POST /api/v1/users/signup` | `can_register(origin="signup")` in `UserService.register_user`, **before** the duplicate-email check | 403 + reason code |
| `POST /api/v1/login/access-token` | `require_password_auth` **after** `authenticate` and the `is_active` check | 403 `password_auth_disabled` |
| `POST /api/v1/auth/google/callback` | `can_register(origin="google")` in `AuthService.create_user_from_google` | 403 + reason code |
| `POST /api/v1/password-recovery/{email}` | `is_password_auth_allowed` in `UserService.recover_password` — **silent skip**, no error | 200 generic "Password recovery email sent" |
| `POST /api/v1/reset-password/` | `require_password_auth` in `UserService.reset_password`, after the token resolves its owner | 403 `password_auth_disabled` |
| `PATCH /api/v1/users/me/password` | `_require_password_auth(session, current_user)` in the route | 403 `password_auth_disabled` |
| `POST /api/v1/users/me/set-password` | same | 403 `password_auth_disabled` |
| `PATCH /api/v1/users/me` (email change) | `can_change_email(session)` | 403 `"Email changes are not allowed"` |

Ordering matters on login: the policy check runs **after** authentication, never before. Checking first would answer for an address that has no account at all, and the constant-time credential check is what keeps the endpoint from being an oracle.

Ordering matters on signup too: the policy refusal is raised **before** the duplicate check, so a closed instance answers identically for a known and an unknown address.

## Consumers of the resolved policy

| Consumer | Call |
|----------|------|
| `RoleService.derive_default_role(*, session, is_superuser)` | `AccessPolicyService.default_role(session)` for non-superusers; superusers always `admin` |
| `user_to_public(session, user, *, can_change_email=None)` | `can_change_email` resolved once and passed in when projecting a list (`GET /users/` resolves it once for the whole page) |
| `GET /users/me/ai-credentials/status` | builds `UserPublicWithAICredentials` with `can_change_email=AccessPolicyService.can_change_email(session)` |
| `deps.get_current_user` | `is_account_valid(user)` |

## Seeding and startup

### First-boot seed (`ServerConfigService._first_boot_seed`)

Runs only when the `server_config` row does not exist. Converts `settings.AUTH_WHITELIST_USER_DOMAINS` (`acme.com,corp.io`) into the glob syntax (`*@acme.com, *@corp.io`) and copies `settings.DEFAULT_USER_ROLE`. These two settings are read **here and in the migration only**.

### Prestart materialisation (`init_db`)

`backend/app/core/db.py` calls `ServerConfigService.get_or_create(session)` at prestart. `get_or_create` COMMITs when it creates the row, and the policy is now read from login, signup, the Google callback and every `UserPublic` projection — none of which should be the thing that commits an unrelated row. Creating it once at prestart makes every later call a pure read.

### Startup warning (`warn_if_env_overrides_present`)

Called from `backend/app/main.py` startup. Warns when `AUTH_WHITELIST_USER_DOMAINS` is set or `DEFAULT_USER_ROLE` differs from `agent-user`, naming `/admin/server-configuration#access`. Logs the setting names, never the values.

## Migration

Revision: `1d737d7ef0a0` — "add access policy to server config"
Down revision: `68aab27946e5`

- Six `op.add_column` calls with `server_default`s matching the model defaults. The server defaults are **kept**, not dropped: a row inserted by a tool that does not know these columns still lands in a legal state
- Data step `_seed_from_env()`: converts `AUTH_WHITELIST_USER_DOMAINS` → patterns and copies `DEFAULT_USER_ROLE`, then issues a single `UPDATE server_config SET ...` with no `WHERE` — on a singleton table that touches the one row if it exists and zero rows if it does not. Short-circuits when there is nothing to carry over
- The settings import is wrapped in a broad `except`, because a failed migration stops the deploy. **Note the direction of that failure**: a skipped seed leaves `allowed_email_patterns=''`, which the policy reads as *no restriction* — an install that was gating signups by domain would come up open. The skip is therefore logged at `error`, and `warn_if_env_overrides_present` warns again at every subsequent startup
- Downgrade drops the six columns

## Frontend

### `useAccessPolicy` (`frontend/src/hooks/useAccessPolicy.ts`)

```ts
useQuery<AccessPolicyPublic>({
  queryKey: ["accessPolicy"],
  queryFn: () => ServerConfigService.getAccessPolicy(),
  staleTime: 60_000,
  retry: false,
})
```

`retry: false` because the endpoint is IP rate-limited. One shared key means login, signup and the admin card cost one request per minute between them. **Every caller must degrade gracefully when `data` is `undefined`.**

### `AccessPolicyCard` (`frontend/src/components/Admin/AccessPolicyCard.tsx`)

Named export. Title "Access & New Users", `ShieldCheck` icon. Reads `["serverConfig"]` (`ServerConfigService.getServerConfig`) for the row and `useAccessPolicy()` only for `google_auth_enabled` — whether Google sign-in is *configured* is a deployment fact, not a column.

Three sections separated by `<Separator />`:

| Section | Control | Field |
|---------|---------|-------|
| Who can join | Select "Registration" — "Anyone with an allowed email" / "Invite only" | `registration_mode` |
| Who can join | Textarea "Allowed email addresses" | `allowed_email_patterns` |
| How they sign in | Switch "Password sign-in" | `password_auth_enabled` |
| How they sign in | Switch "Create accounts on Google sign-in" | `google_auto_register` |
| New users | Select "Default role" — Agent User / Agent Developer | `default_user_role` |
| New users | Switch "Offer Cinna Desktop in invitations" | `invite_include_desktop_default` |

**Saving.** Every control except the textarea mutates immediately on change. The patterns textarea is the one explicit-save control: a **Save patterns** / **Cancel** pair appears while the draft is dirty, and sends `{ allowed_email_patterns: patternsValue.trim() }` — never `null`, since the backend reads a null field as "not being changed", so clearing the list has to travel as an empty string.

`onSuccess` runs `queryClient.setQueryData(["serverConfig"], data)` (so controls do not snap back), then invalidates **both** `["serverConfig"]` and `["accessPolicy"]`, then toasts "Access policy updated". Draft/error reset is scoped with `if ("allowed_email_patterns" in variables)`.

**Error rendering.** The reason-code map moved out of this component into a shared module, `frontend/src/utils/accessPolicyReasons.ts`, because the same codes are raised on four unrelated surfaces (this card, signup, password login, the Google button). `parseAccessPolicyReason(error)` reads `error.body.detail` and splits on the **first** colon only (an offending pattern entry may itself contain one); `accessPolicyReasonCopy(error)` maps every code in its `REASON_COPY` record — which includes `no_admin_password`, `no_admin_google_account`, `google_oauth_not_configured`, `invalid_registration_mode`, `invalid_default_user_role`, and the four registration/auth refusal codes (`registration_closed`, `email_not_allowed`, `password_auth_disabled`, `google_auto_register_disabled`) — to human copy. Locally, `reasonMessage(error)` calls `accessPolicyReasonCopy` and falls back to `Could not save the access policy (${code}).` only for a code no release of this frontend has heard of. A pattern failure renders inline under the textarea (`role="alert"`, `aria-invalid` on the textarea, `aria-describedby` pointing at the error id); every other code goes to an error toast.

A wrapper, `handlePolicyAwareError` in `frontend/src/utils.ts`, binds `accessPolicyReasonCopy` in front of the existing `handleError` for mutations that can be refused by the policy: both mutations in `hooks/useAuth.ts`, `routes/reset-password.tsx`, and `components/UserSettings/ChangePassword.tsx`. `components/Auth/GoogleLoginButton.tsx` and `components/UserSettings/SetPassword.tsx` call `accessPolicyReasonCopy` directly instead, since their error handling isn't a plain toast.

**Summary line** (the `CardDescription`) — `"${signIn}; ${registration}; new users become ${role}s."`, e.g. "Employees sign in with Google only; registration is invite-only; new users become Agent Users." Reads `Reading the current policy…` while `policyKnown` is false, where `policyKnown = !isLoading && !publicPolicyPending` — `isPending`, not `data !== undefined`, because the projection is `retry: false` and a failed read must still release the summary.

**Two pattern counts on purpose.** `draftPatternCount` (from the textarea draft) drives the label counter — "No restriction" at 0, else "N pattern(s)". `persistedPatternCount` (from the saved value) feeds the summary sentence, so unsaved edits are never narrated as in force.

**Disabled states.**

```ts
const passwordSwitchLocked =
  passwordAuthEnabled && publicPolicy !== undefined && !publicPolicy.google_auth_enabled
```

Locked only in the direction that would fail (turning it off) and only after the projection has answered. The switch also gets `pointer-events-none` so hover reaches the wrapping `TooltipTrigger`; tooltip: "Configure Google OAuth first — otherwise nobody could sign in." The auto-register switch is `disabled={busy || inviteOnly}` and its label greys out in invite-only.

Fallbacks when the config row has not loaded: `open`, `true`, `true`, `agent-user`, `true`, and `google_auth_enabled` → **`false`** (the conservative direction for an admin control, unlike login/signup).

### Admin route (`frontend/src/routes/_layout/admin/server-configuration.tsx`)

`HashTabs` order: `interface` ("Interface"), **`access` ("Access")**, `channels` ("Channels"), `mail-servers` ("Mail Servers"). `access` is deliberately second — `HashTabs` lands on `tabs[0]` when there is no hash, and Interface has been that landing tab; the card is addressed directly as `/admin/server-configuration#access` from the startup warning. Card wrapped in `max-w-3xl`.

### Login page (`frontend/src/routes/login/index.tsx`)

```ts
const googleAvailable =
  Boolean(import.meta.env.VITE_GOOGLE_CLIENT_ID) &&
  (policy?.google_auth_enabled ?? true)

const showPasswordForm =
  passwordAuthEnabled || passwordFormRevealed || !googleAvailable
```

**Two independently configured facts.** Google sign-in needs the *backend*'s client id and secret (reported as `google_auth_enabled`) **and** the *frontend build*'s `VITE_GOOGLE_CLIENT_ID` — `GoogleLoginButton` returns `null` without it. Reading only one of them is how this page ends up hiding the password form in favour of a button that was never rendered.

`!googleAvailable` is the **floor**: a policy that turns off password sign-in on a build that cannot show Google would otherwise leave the page with no way in. The backend's lockout rule only sees its own settings, so it cannot catch that combination — the UI has to.

- Failed/absent policy read degrades permissively: `password_auth_enabled ?? true`, `registration_open ?? true`
- While the query is pending the credential block renders two `<Skeleton className="h-10 w-full" />` rather than the form, so a Google-only instance never flashes a password form. The Google button is not gated on pending
- Break-glass disclosure when the form is hidden: a centered `<button type="button" aria-expanded={passwordFormRevealed}>` labelled **"Sign in with password (administrators)"**. Revealing it unmounts the focused button, so a `useEffect` calls `form.setFocus("username")`
- The "Forgot your password?" link lives inside the password form, so it is hidden with it
- The "Or continue with email" divider renders only when `googleAvailable && showPasswordForm`
- The sign-up link block renders only when `!policyPending && registrationOpen`, preserving the `redirect` search param

### Signup page (`frontend/src/routes/signup.tsx`)

```ts
const selfServeSignupAllowed = registrationOpen && passwordAuthEnabled
```

Same permissive fallbacks and the same `googleAvailable` expression. While pending, three skeletons. When not allowed, the form is replaced by an `<Alert>`:

- Title: `registrationOpen ? "Accounts are created with Google here" : "This server is invite-only"`
- Description, four variants: open+google → "Password sign-up is turned off on this server. Continue with Google below, or ask your administrator for access."; open+no-google → same without the Google clause; invite-only+google → "Ask your administrator for an invitation. If you already have an account, sign in with Google below."; invite-only+no-google → "…sign in from the login page."

The Google button is **kept** in the closed branch whenever Google is configured — in invite-only mode it is how an invited, pre-created account gets in. The "Already have an account? Log in" link renders in both branches.

### Profile email field (`frontend/src/components/UserSettings/UserInformation.tsx`)

`const canChangeEmail = currentUser?.can_change_email === true` — strict `=== true`, so absent/loading means "not yet". When false the Edit Profile dialog **replaces** the input (rather than disabling it) with a read-only block showing the current address plus: "This server restricts which email addresses may sign in, so the address cannot be changed here. Ask an administrator." The submit handler only considers the email field when `canChangeEmail`.

### Generated client

- `ServerConfigService.getAccessPolicy()` — `GET /api/v1/server-config/access-policy`, no arguments
- Type `AccessPolicyPublic`; `ServerConfigUpdate` carries the six new optional fields; `UserPublic.can_change_email` is required

## Configuration

| Setting | Location | Purpose |
|---------|----------|---------|
| `ACCESS_POLICY_RATE_LIMIT_PER_MIN` | `config.py` (default `120`) | Per-IP budget for the public projection |
| `AUTH_WHITELIST_USER_DOMAINS` | `.env` / `config.py` | **Seed only.** Comma-separated domains, converted to `*@domain` globs on first boot and by the migration. No live decision reads it |
| `DEFAULT_USER_ROLE` | `.env` / `config.py` | **Seed only.** `Literal["agent-user", "agent-developer"]`; a present-but-invalid value still fails loudly at startup |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | `.env` / `config.py` | Both required for `google_oauth_enabled` → `AccessPolicyPublic.google_auth_enabled` |
| `VITE_GOOGLE_CLIENT_ID` | `frontend/.env` (build time) | Required for the Google button to render at all; independent of the backend setting |
| `DESKTOP_AUTH_ENABLED` | `config.py` | Surfaced as `AccessPolicyPublic.desktop_enabled` |
| `PROJECT_NAME` | `config.py` | Surfaced as `AccessPolicyPublic.project_name` |

Removed by this change: `settings.auth_whitelist_domains` and `settings.allow_user_email_change` (the computed properties), `AuthService.is_email_domain_allowed`, and `OAuthConfig.allow_email_change`.

---

*Last updated: 2026-09-05*
