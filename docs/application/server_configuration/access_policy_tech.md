# Access Policy — Technical Details

## File Locations

### Backend
- `backend/app/models/server_config/server_config.py` — six access-policy columns on `ServerConfig`, the same fields on `ServerConfigUpdate`, the `AccessPolicyPublic` projection, and the `REGISTRATION_MODE_*` / `VALID_REGISTRATION_MODES` constants
- `backend/app/services/users/access_policy_service.py` — `AccessPolicyService`, the `AccessPolicy` / `RegistrationDecision` frozen dataclasses, the reason-code and origin constants, and the three domain exceptions
- `backend/app/services/server_config/server_config_service.py` — `get_or_create` (with the first-boot env seed) and `update` (calls `validate_update` before applying)
- `backend/app/api/routes/server_config.py` — the public `access-policy` route and its rate-limit dependency; the superuser get/put. The sibling public `landing` route shares the **same** `_access_policy_limiter` object (see [Public Landing Page — tech](landing_page_tech.md))
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
- `backend/app/core/config.py` — `ACCESS_POLICY_RATE_LIMIT_PER_MIN` (raised 120 → 240 in phase 4 and re-documented as the **module-wide** anonymous budget); `AUTH_WHITELIST_USER_DOMAINS` and `DEFAULT_USER_ROLE` kept as seed-only
- `backend/app/core/db.py` — `init_db` materialises the `ServerConfig` singleton at prestart
- `backend/app/main.py` — startup calls `AccessPolicyService.warn_if_env_overrides_present()`
- `backend/app/alembic/versions/1d737d7ef0a0_add_access_policy_to_server_config.py` — migration (down_revision: `68aab27946e5`)

### Frontend
- `frontend/src/components/Admin/AccessPolicy/accessPolicy.ts` — shared `["serverConfig"]` query, the single-field `PATCH` mutation with its cache discipline, `pendingConfigField`, and the summary-sentence helpers (`describeRegistration`, `describeSignIn`, `describePatternCount`, `parsePatterns`) the cards below read
- `frontend/src/components/Admin/AccessPolicy/RegistrationCard.tsx` — registration mode + the allowed-email-patterns summary row (opens `AllowedEmailPatternsDialog`)
- `frontend/src/components/Admin/AccessPolicy/AllowedEmailPatternsDialog.tsx` — the patterns editor, the one explicit-save control on the tab
- `frontend/src/components/Admin/AccessPolicy/SignInMethodsCard.tsx` — password/Google sign-in switches
- `frontend/src/components/Admin/AccessPolicy/NewUserDefaultsCard.tsx` — default role + "Offer Cinna Desktop in invitations"
- `frontend/src/components/Admin/AccessPolicy/CompanyAiCredentialsCard.tsx` + `CompanyAiCredentialRow.tsx` — one row per managed credential with a three-segment role `ToggleGroup` editing `auto_provision_roles`, capped at 5 rows; see [Admin-Provisioned AI Credentials — tech](../ai_credentials/admin_ai_credential_provisioning_tech.md)
- `frontend/src/components/Common/QueryErrorAlert.tsx` — shared error-with-Retry panel, extracted from `RoutingStateBlocks.tsx`; used by all four cards above
- `frontend/src/routes/_layout/admin/server-configuration.tsx` — mounts the four cards plus `LandingPageCard` in a two-column grid on the `access` hash tab
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
| `invite_include_desktop_default` | `bool` | `True` | Pre-ticks the invitation wizard's desktop checkbox, and is the fallback the invite route applies when `InviteUserRequest.include_desktop` is `None`. The wizard reads it from the admin server-config endpoint; it is deliberately **absent** from the public `AccessPolicyPublic` projection. The value the admin actually submitted is then stored on the invitation row — the public accept page reads `include_desktop` off the invitation, never off the policy, because whether desktop was offered is a property of that invitation |

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
| `password_signup_available` | `bool` | `AccessPolicyService.signup_refusal_reason(policy) is None` |

Deliberately excludes `allowed_email_patterns` and `default_user_role` — and, since phase 4, `landing_markdown`: the welcome copy is content rather than front-door policy and gets its own endpoint, because this projection is fetched under one shared cache key by every `/login` and `/signup` load. See [Public Landing Page — tech](landing_page_tech.md).

`password_signup_available` is derived from two fields already projected here, so it discloses nothing new. It is deliberately **not** `can_register`, which also consults the pattern list and therefore needs an address: this is "the door exists", which is what a projection with no viewer may say.

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
| `to_public(policy) -> AccessPolicyPublic` | Adds `desktop_enabled` / `project_name` from settings, and `password_signup_available` from the policy |
| `signup_refusal_reason(policy) -> str \| None` | The **sole** encoding of the door-level signup gates — `registration_open`, then `password_auth_enabled`. Two readers consume it and neither re-encodes it: `can_register`'s `SIGNUP` branch (which then adds the per-address pattern check) and `AccessPolicy.password_signup_available` (which is this returning `None`). A gate added here reaches both at once |
| `normalize_email_patterns(patterns) -> str` | Canonical form: drop blank entries, strip the rest, rejoin with `", "`. Applied on write (`ServerConfigService.update`) **and** on read (`resolve`), so "is this list empty?" has exactly one answer |
| `is_email_allowed(policy, email) -> bool` | Empty/whitespace pattern string → `True`; otherwise delegates to `match_email_pattern`. The **only** place the shared matcher's fail-closed default is inverted |
| `can_register(session, *, email, origin) -> RegistrationDecision` | See the origin table below. Raises `ValueError` on an origin nobody has reasoned about, rather than guessing a gate. The `SIGNUP` branch delegates its door-level gates to `signup_refusal_reason`; the non-signup branch keeps its own `registration_open` + `google_auto_register` checks |
| `is_password_auth_allowed(policy, user) -> bool` | `policy.password_auth_enabled or user.is_superuser` |
| `require_password_auth(session, user) -> None` | Raises `PasswordAuthDisabledError` unless allowed. The one gate, so the break-glass cannot drift between login, set-password, change-password and reset. Password **recovery** deliberately does not use it — a refusal there must be silent |
| `default_role(session) -> str` | Clamps an out-of-range stored value to `agent-user` |
| `can_change_email(session) -> bool` | `True` only when the pattern string is empty/whitespace |
| `is_account_valid(user) -> bool` | Today `user.is_active`. A seam for the browser-session path only; see the note below |
| `validate_update(session, current, update, acting_user) -> None` | Raises `AccessPolicyValidationError(reason, message)` |
| `warn_if_env_overrides_present() -> None` | Startup warning naming `/admin/server-configuration#access`. Never logs the values |

**`is_account_valid` scope.** Only `deps.get_current_user` (the browser-session path) calls it. The guest, agent-env, account-CLI and owner-resolution token families in `deps`, the login routes' own inactive check, and `AuthService.authenticate_with_google` still test `is_active` inline — they have different trust shapes and are switched over when there is a second rule to switch them for.

### Origins

`AccountOrigin`, a `str` enum on `backend/app/models/users/user.py` next to `UserRole`: `SIGNUP`, `GOOGLE`, `INVITE`, `ADMIN`, `EXTERNAL`, `SEED`. (It lives on the model rather than here because both this service and `UserService.create_account` need it, and a constant owned by one of two peers is how an import cycle starts.) The phase-1 `ORIGIN_*` string constants are gone. `can_register` takes the enum and is reached from the one creation chokepoint, `UserService.create_account` — see [Auth — tech](../auth/auth_tech.md#the-account-creation-chokepoint).

`_UNGATED_ORIGINS = {ADMIN, INVITE, EXTERNAL, SEED}`. An origin that is in neither set raises rather than guessing which gate it wanted.

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
- **Rate limit**: per-IP via `_access_policy_rate_limit`, a `Depends` declared **before** `SessionDep` resolves so a throttled request costs no pool connection and no query. Budget: `settings.ACCESS_POLICY_RATE_LIMIT_PER_MIN` (default **240**/min since phase 4). Over budget → 429 with a `Retry-After` header. **One `RateLimiter` object serves every anonymous endpoint in the module** — today this route and `GET /server-config/landing` — so the budget is spent in requests, not page views: a `/login` view costs one, a `/start` view two. A per-route limiter would hand one caller a fresh budget for each new public read added there
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
| `POST /api/v1/password-recovery/{email}` | `is_password_auth_allowed` in `UserService.recover_password` — **silent skip**, no error | 200 generic "If an account exists for that email, a password recovery email has been sent" — the same status and body an unknown address gets (the former 404 is gone), and every other reason not to send is a silent no-op too |
| `POST /api/v1/reset-password/` | `require_password_auth` in `UserService.reset_password`, after the token resolves its owner *and* after the never-claimed-invited-account refusal | 403 `password_auth_disabled` (the invitation refusal is a 400 `"Invalid token"` instead — the reason is the account's state, not the token's) |
| `POST /api/v1/invitations/accept` | `is_password_auth_allowed` via `InvitationService.password_accepted` — the same predicate the lookup surfaces as `password_accepted` | The generic 400, identical to every other failure. **No 403 branch**: a distinct status for a real-but-unusable token would answer "is this token genuine" |
| `PATCH /api/v1/users/me/password` | `_require_password_auth(session, current_user)` in the route | 403 `password_auth_disabled` |
| `POST /api/v1/users/me/set-password` | same | 403 `password_auth_disabled` |
| `PATCH /api/v1/users/me` (email change) | `can_change_email(session)` | 403 `"Email changes are not allowed"` |

Ordering matters on login: the policy check runs **after** authentication, never before. Checking first would answer for an address that has no account at all, and the constant-time credential check is what keeps the endpoint from being an oracle.

Ordering matters on signup too: the policy refusal is raised **before** the duplicate check, so a closed instance answers identically for a known and an unknown address.

## Consumers of the resolved policy

| Consumer | Call |
|----------|------|
| `RoleService.derive_default_role(*, session, is_superuser)` | `AccessPolicyService.default_role(session)` for non-superusers; superusers always `admin` |
| `user_to_public(session, user, *, can_change_email=None, invitation_status=None)` | `can_change_email` resolved once and passed in when projecting a list (`GET /users/` resolves it once for the whole page). `invitation_status` is per row and is deliberately *not* resolved inside the builder — `read_users` batches it with `InvitationService.status_map` |
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

`retry: false` because the endpoint is IP rate-limited. One shared key means login, signup and the admin card cost one request per minute between them. **Every caller must degrade gracefully when `data` is `undefined`.** `/start` reads the same key, and additionally reads `["landingPage"]` — two requests from one shared anonymous bucket.

The module also exports **`googleSignInAvailable(source)`**, the one place the two independently configured Google facts are combined (the backend's client id/secret, reported by a projection, and this build's `VITE_GOOGLE_CLIENT_ID`, without which `GoogleLoginButton` renders nothing). It is structurally typed — `{ google_auth_enabled?: boolean | null }` — because the same fact reaches `/accept-invite` on `InvitationLookupPublic` rather than on `AccessPolicyPublic`; narrowing it to one response model is what left that page with a fourth hand-rolled copy. It returns `undefined` when the source has not answered, deliberately, because the callers do not share a degradation: `/login` and `/signup` apply `?? true`, `/start` and `/accept-invite` require `=== true`.

### The Access tab cards (`frontend/src/components/Admin/AccessPolicy/`)

The tab is a two-column grid of five cards (`grid grid-cols-1 lg:grid-cols-2 gap-6 items-start`) rather than one wide card: `RegistrationCard` · `SignInMethodsCard` · `NewUserDefaultsCard` · `CompanyAiCredentialsCard` · `LandingPageCard` (existing apart from its header icon — see [Public Landing Page — tech](landing_page_tech.md)), all half-width — nothing spans the grid (the credentials card was briefly `lg:col-span-2` as a checkbox table; it is now a preview list of rows, each with a segmented role toggle, per the UI guidelines' card-width rule). Every card title carries a lucide icon (`ShieldCheck` · `LogIn` · `UserPlus` · `KeyRound` · `Globe`). The single `AccessPolicyCard` and `AutoProvisionedCredentialsMatrix` this replaced are deleted; every helper they held now lives in `accessPolicy.ts` or on the individual card.

All four `ServerConfig`-backed cards share `accessPolicy.ts`: `useServerConfig()` (`["serverConfig"]`), `useServerConfigUpdate()` (the single-field `PATCH`, publishing the returned row into the cache **before** invalidating `["serverConfig"]` and `["accessPolicy"]` so a control does not visibly snap back under its own success toast), and `pendingConfigField(mutation)` (which key of the in-flight payload to disable — every write on this tab carries exactly one field). `CompanyAiCredentialsCard` is the exception: it does not touch `ServerConfig` at all, it patches `ManagedAICredential.auto_provision_roles` (see below).

| Card | Control | Field |
|------|---------|-------|
| `RegistrationCard` | Select "Registration" — "Anyone with an allowed email" / "Invite only" | `registration_mode` |
| `RegistrationCard` | Summary row "Allowed email addresses" (count + first two patterns), pencil opens `AllowedEmailPatternsDialog` | `allowed_email_patterns` |
| `SignInMethodsCard` | Switch "Password sign-in" | `password_auth_enabled` |
| `SignInMethodsCard` | Switch "Create accounts on Google sign-in" | `google_auto_register` |
| `NewUserDefaultsCard` | Select "Default role" — Agent User / Agent Developer | `default_user_role` |
| `NewUserDefaultsCard` | Switch "Offer Cinna Desktop in invitations" | `invite_include_desktop_default` |
| `CompanyAiCredentialsCard` | `PreviewList` of credential rows (name + provider type, a **User · Dev · Admin** `ToggleGroup type="multiple"` as the row's one control), capped at 5 rows (granted-first then alphabetical), link to `/admin/ai-credentials` | **Not a `ServerConfig` column.** Each toggle is a `PATCH /admin/llm-providers/{id}` carrying only `auto_provision_roles`; the card shares the AI Credentials page's query key (`managedCredentialsQueryKey()` / `MANAGED_CREDENTIALS_QUERY_PREFIX`, which is **not** renamed — a cache key is not a route path) and renders the `409 auto_provision_conflict` inline |

**`AllowedEmailPatternsDialog`** (`frontend/src/components/Admin/AccessPolicy/AllowedEmailPatternsDialog.tsx`) is the one explicit-save control on the tab: opened from `RegistrationCard`'s pencil button, it holds its own draft state (seeded from the persisted value, `null` meaning "unedited"), a **Save** / **Cancel** footer, and sends `{ allowed_email_patterns: value.trim() }` — never `null`, since the backend reads a null field as "not being changed", so clearing the list has to travel as an empty string. A rejected save renders inline under the textarea (`role="alert"`, `aria-invalid`, `aria-describedby`) rather than a toast, via an `onError` override passed to `useServerConfigUpdate`; every other card's mutation uses the hook's default toast.

Every other control mutates immediately on change (`onValueChange` / `onCheckedChange` straight into `useServerConfigUpdate().mutate`).

**Error rendering.** The reason-code map lives in a shared module, `frontend/src/utils/accessPolicyReasons.ts`, because the same codes are raised on four unrelated surfaces (this tab, signup, password login, the Google button). `parseAccessPolicyReason(error)` reads `error.body.detail` and splits on the **first** colon only (an offending pattern entry may itself contain one); `accessPolicyReasonCopy(error)` maps every code in its `REASON_COPY` record — which includes `no_admin_password`, `no_admin_google_account`, `google_oauth_not_configured`, `invalid_registration_mode`, `invalid_default_user_role`, and the four registration/auth refusal codes (`registration_closed`, `email_not_allowed`, `password_auth_disabled`, `google_auto_register_disabled`) — to human copy. `accessPolicy.ts`'s `reasonMessage(error)` calls `accessPolicyReasonCopy` and falls back to `Could not save the access policy (${code}).` only for a code no release of this frontend has heard of.

A wrapper, `handlePolicyAwareError` in `frontend/src/utils.ts`, binds `accessPolicyReasonCopy` in front of the existing `handleError` for mutations that can be refused by the policy: both mutations in `hooks/useAuth.ts`, `routes/reset-password.tsx`, and `components/UserSettings/ChangePassword.tsx`. `components/Auth/GoogleLoginButton.tsx` and `components/UserSettings/SetPassword.tsx` call `accessPolicyReasonCopy` directly instead, since their error handling isn't a plain toast.

**Summary lines** (each card's `CardDescription`), from `accessPolicy.ts`: `describeRegistration({registrationOpen, patternCount})` → "Anyone can register." / "Anyone with a matching address can register (N patterns)." / "Only people you invite get an account."; `describeSignIn({passwordAuthEnabled, googleAuthEnabled})` → "Employees sign in with Google or a password." / "…with a password." / "…with Google only." Both are derived from the **persisted** row only, never a draft, and read `Reading the current policy…` / `"How employees get in."`-style placeholders while their query has no data yet.

**Two pattern counts on purpose.** The dialog's own draft count drives its label counter — "No restriction" at 0, else "N pattern(s)". `RegistrationCard`'s `describeRegistration` reads the **persisted** count, so an open, unsaved dialog is never narrated as already in force.

**Disabled states** (`SignInMethodsCard`).

```ts
const passwordSwitchLocked =
  passwordAuthEnabled && publicPolicy !== undefined && !publicPolicy.google_auth_enabled
```

Locked only in the direction that would fail (turning it off) and only after the projection has answered. The switch also gets `pointer-events-none` so hover reaches the wrapping `TooltipTrigger`; tooltip: "Configure Google OAuth first — otherwise nobody could sign in." The auto-register switch is `disabled={pendingField === "google_auto_register" || inviteOnly}` and its label greys out in invite-only.

Fallbacks when the config row has not loaded: `open`, `true`, `true`, `agent-user`, `true`, and `google_auth_enabled` → **`false`** (the conservative direction for an admin control, unlike login/signup).

**Error state, per card.** Each card gates its `QueryErrorAlert` on there being **no** data to show (`isError && config === undefined`, or for `SignInMethodsCard`, `(isError || policyError) && (!config || !publicPolicy)`) rather than on `isError` alone — a background refetch that fails while the last good value is still cached must not blank a live card.

### Admin route (`frontend/src/routes/_layout/admin/server-configuration.tsx`)

`HashTabs` order: `interface` ("Interface"), **`access` ("Access")**, `channels` ("Channels"), `mail-servers` ("Mail Servers"). `access` is deliberately second — `HashTabs` lands on `tabs[0]` when there is no hash, and Interface has been that landing tab; the tab is addressed directly as `/admin/server-configuration#access` from the startup warning.

### Login page (`frontend/src/routes/login/index.tsx`)

```ts
const passwordSignupAvailable = policy?.password_signup_available ?? true
const googleAvailable = googleSignInAvailable(policy) ?? true

const showPasswordForm =
  passwordAuthEnabled || passwordFormRevealed || !googleAvailable
```

**Two independently configured facts.** Google sign-in needs the *backend*'s client id and secret (reported as `google_auth_enabled`) **and** the *frontend build*'s `VITE_GOOGLE_CLIENT_ID` — `GoogleLoginButton` returns `null` without it. Reading only one of them is how this page ends up hiding the password form in favour of a button that was never rendered. Both are resolved by the shared `googleSignInAvailable` helper.

**The sign-up link reads the projection's answer, not this page's recombination.** It used to check `registration_open` alone and offer a Sign up link that `/signup` — which required both facts — then refused to render a form for.

`!googleAvailable` is the **floor**: a policy that turns off password sign-in on a build that cannot show Google would otherwise leave the page with no way in. The backend's lockout rule only sees its own settings, so it cannot catch that combination — the UI has to.

- Failed/absent policy read degrades permissively: `password_auth_enabled ?? true`, `password_signup_available ?? true`
- While the query is pending the credential block renders two `<Skeleton className="h-10 w-full" />` rather than the form, so a Google-only instance never flashes a password form. The Google button is not gated on pending
- Break-glass disclosure when the form is hidden: a centered `<button type="button" aria-expanded={passwordFormRevealed}>` labelled **"Sign in with password (administrators)"**. Revealing it unmounts the focused button, so a `useEffect` calls `form.setFocus("username")`
- The "Forgot your password?" link lives inside the password form, so it is hidden with it
- The "Or continue with email" divider renders only when `googleAvailable && showPasswordForm`
- The sign-up link block renders only when `!policyPending && passwordSignupAvailable`, preserving the `redirect` search param

### Signup page (`frontend/src/routes/signup.tsx`)

```ts
const selfServeSignupAllowed = policy?.password_signup_available ?? true
// `registration_open` is still read, but for the *copy* only
const registrationOpen = policy?.registration_open ?? true
```

Same permissive fallbacks and the same shared `googleSignInAvailable` helper. `registration_open` survives on this page **only to pick one of four explanatory sentences** — that is presentation. The decision itself is the projection's.

While pending, three skeletons. When not allowed, the form is replaced by an `<Alert>`:

- Title: `registrationOpen ? (googleAvailable ? "Accounts are created with Google here" : "Ask your administrator for an account") : "This server is invite-only"`. The title has to consult `googleAvailable` for the same reason the description does: reaching this branch only pins `password_signup_available === false`, and with registration open that pins password auth off but says nothing about Google — open + no password + no Google is a legal state, since the lockout rule only protects the superuser break-glass path. The old wording asserted a button that may not be rendered below
- Description, four variants: open+google → "Password sign-up is turned off on this server. Continue with Google below, or ask your administrator for access."; open+no-google → same without the Google clause; invite-only+google → "Ask your administrator for an invitation. If you already have an account, sign in with Google below."; invite-only+no-google → "…sign in from the login page."

The Google button is **kept** in the closed branch whenever Google is configured — in invite-only mode it is how an invited, pre-created account gets in. The "Already have an account? Log in" link renders in both branches.

### Profile email field (`frontend/src/components/UserSettings/UserInformation.tsx`)

`const canChangeEmail = currentUser?.can_change_email === true` — strict `=== true`, so absent/loading means "not yet". When false the Edit Profile dialog **replaces** the input (rather than disabling it) with a read-only block showing the current address plus: "This server restricts which email addresses may sign in, so the address cannot be changed here. Ask an administrator." The submit handler only considers the email field when `canChangeEmail`.

### Generated client

- `ServerConfigService.getAccessPolicy()` — `GET /api/v1/server-config/access-policy`, no arguments; `ServerConfigService.getLandingPage()` — `GET /api/v1/server-config/landing`
- Types `AccessPolicyPublic` (now with `password_signup_available`) and `LandingPagePublic`; `ServerConfigUpdate` carries the six access-policy fields plus `landing_markdown`; `UserPublic.can_change_email` is required

## Configuration

| Setting | Location | Purpose |
|---------|----------|---------|
| `ACCESS_POLICY_RATE_LIMIT_PER_MIN` | `config.py` (default **`240`**) | Per-IP budget shared by **every** anonymous endpoint in `api/routes/server_config.py` — this projection and the landing read. Raised from 120 when the landing read joined the same limiter, to keep the effective visitor capacity |
| `AUTH_WHITELIST_USER_DOMAINS` | `.env` / `config.py` | **Seed only.** Comma-separated domains, converted to `*@domain` globs on first boot and by the migration. No live decision reads it |
| `DEFAULT_USER_ROLE` | `.env` / `config.py` | **Seed only.** `Literal["agent-user", "agent-developer"]`; a present-but-invalid value still fails loudly at startup |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | `.env` / `config.py` | Both required for `google_oauth_enabled` → `AccessPolicyPublic.google_auth_enabled` |
| `VITE_GOOGLE_CLIENT_ID` | `frontend/.env` (build time) | Required for the Google button to render at all; independent of the backend setting |
| `DESKTOP_AUTH_ENABLED` | `config.py` | Surfaced as `AccessPolicyPublic.desktop_enabled`. Gates the **advertisement** of the desktop client only: `/start` hides its download card, while `/desktop` renders unconditionally and `GET /desktop/download` stays ungated, so links in already-sent new-account emails keep working |
| `PROJECT_NAME` | `config.py` | Surfaced as `AccessPolicyPublic.project_name` |

Removed by this change: `settings.auth_whitelist_domains` and `settings.allow_user_email_change` (the computed properties), `AuthService.is_email_domain_allowed`, and `OAuthConfig.allow_email_change`.

---

*Last updated: 2026-09-07*
