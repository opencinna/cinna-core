# Access Policy

## Purpose

Give a superuser one place to decide **who may get an account on this instance and how they sign in** — registration mode, an allowed-email pattern list, the password/Google sign-in switches, and the role new accounts receive. The policy lives in the database (`ServerConfig`), replacing the `AUTH_WHITELIST_USER_DOMAINS` and `DEFAULT_USER_ROLE` env settings, and a small public projection of it drives what the login and signup pages render before anyone is authenticated.

## Core Concepts

- **Access policy** — six admin-editable switches on the singleton `ServerConfig` row (`registration_mode`, `allowed_email_patterns`, `password_auth_enabled`, `google_auto_register`, `default_user_role`, `invite_include_desktop_default`), plus two facts derived from deployment settings (is Google OAuth configured, is the desktop client enabled). Resolved together by one service; never re-derived anywhere else
- **Registration mode** — `open` (anyone whose address matches the patterns may create an account) or `invite_only` (nobody self-registers). Only the literal `"open"` opens registration; any unrecognised value degrades to closed
- **Allowed email patterns** — a comma-separated list of `fnmatch` globs (`*@acme.com, *@*.acme.com, jane@acme.com`). **Empty means no restriction.** This is the inverse of the fail-closed default the same matcher uses for channel sender allowlists, and it is the backward-compatible behaviour the platform shipped with
- **Password auth switch** — when off, non-superusers cannot use password login, signup, recovery, reset or set-password. The instance becomes Google-only for everyone except administrators
- **Superuser break-glass** — superusers keep password login, recovery and reset even when password auth is off, so a broken or unreachable Google configuration can never lock every administrator out of their own instance
- **Google auto-register** — whether a Google sign-in from an unknown address creates an account. Ignored in invite-only mode, where Google never registers anyone
- **Public access-policy projection** — an unauthenticated, rate-limited read that says what the **instance offers** (may anyone register, which sign-in methods exist, is desktop enabled, what the instance is called). It deliberately carries no patterns and no default role: those describe who gets in and what they become, which is not for anonymous readers
- **Reason code** — every policy refusal answers with a stable machine-readable string (`registration_closed`, `email_not_allowed`, …) as the HTTP `detail`, so the UI renders its own wording instead of pattern-matching English

## Admin User Stories / Flows

### Closing the instance to self-registration

1. Superuser opens **Admin → Server Configuration → Access** (`/admin/server-configuration#access`)
2. In **Who can join**, changes **Registration** from "Anyone with an allowed email" to "Invite only". The change saves immediately
3. New visitors to `/signup` now see an explanatory panel instead of the form; `/login` no longer shows the "Sign up" link
4. Existing users are unaffected — they keep signing in exactly as before

### Restricting registration to company domains

1. In **Who can join**, the superuser types a comma-separated pattern list into **Allowed email addresses**, e.g. `*@acme.com, *@*.acme.com`
2. A live counter beside the label reads "2 patterns" (it reads "No restriction" while the box is empty)
3. Unlike every other control on the card, the patterns box has an explicit **Save patterns** / **Cancel** pair — a partly-typed pattern must not be saved on every keystroke
4. If an entry cannot ever match a real address, the save is rejected and the offending entry is named inline under the box
5. Once a pattern list exists, users can no longer edit their own email address in **Settings → My profile** (the address is the identity the policy is written against)

### Making the instance Google-only

1. In **How they sign in**, the superuser turns **Password sign-in** off
2. The server validates first: Google OAuth must be configured, and some administrator must be able to sign in through Google. Either check failing returns an error and nothing is applied
3. A third check requires that some administrator actually **has a password** — a superuser provisioned only through Google has none, so the break-glass would open onto nothing. The remedy is one call away (set a password on an admin account), which is why it is a separate, separately-actionable error
4. On success, `/login` renders the Google button as the only way in, with a small **"Sign in with password (administrators)"** disclosure that reveals the password form for the break-glass path
5. Non-superusers who try password login get a 403; superusers still log in

### Choosing what new accounts become

1. In **New users**, the superuser picks **Default role** — Agent User or Agent Developer — and, at the foot of the section, ticks which **Company AI credentials** each role's new accounts receive (a credentials × roles matrix; see [Admin-Provisioned AI Credentials](../ai_credentials/admin_ai_credential_provisioning.md#auto-provisioning-at-account-creation)). The matrix is a view over `ManagedAICredential.auto_provision_roles` — it stores nothing of its own, and one toggle is one `PATCH /admin/llm-providers/{id}`
2. Every subsequently created non-superuser account (password signup, Google first login, externally-arriving channel sender) picks up that role. Existing users are untouched
3. **Offer Cinna Desktop in invitations** pre-ticks a checkbox in the invitation wizard. It is a presentation default only and has no security meaning

### Migrating from the env settings

1. The instance upgrades. The migration copies `AUTH_WHITELIST_USER_DOMAINS` into the pattern list (`acme.com` becomes `*@acme.com`) and `DEFAULT_USER_ROLE` into the default role, once
2. If the operator leaves either setting in `.env`, a warning at every startup names `/admin/server-configuration#access` and explains that the database is now the sole authority
3. Editing `.env` and restarting has no effect on a live instance

## End-User Stories / Flows

### Signing up on an open instance with a pattern list

1. Visitor opens `/signup`, which reads the public projection and renders the password form plus (if configured) the Google button
2. Visitor submits an address outside the pattern list → 403 with reason `email_not_allowed`
3. Visitor submits an allowed address → account created with the configured default role, confirmation email sent

### Arriving at an invite-only instance

1. Visitor opens `/signup` and sees a panel titled "This server is invite-only" telling them to ask their administrator for an invitation
2. The Google button is still shown when Google is configured — in invite-only mode it is how an already-existing account signs in, not a way to create one
3. A Google sign-in from an unknown address is refused with `registration_closed`

### Signing in on a Google-only instance

1. Visitor opens `/login`; the password form is not rendered and there is no "Forgot your password?" link
2. An administrator clicks **"Sign in with password (administrators)"**, the form appears, and they sign in with their password
3. A non-superuser who reveals the form and submits correct credentials still gets a 403 (`password_auth_disabled`) — the disclosure is a UI affordance, not a bypass

### Requesting password recovery when password auth is off

1. User submits their address on `/recover-password`
2. The server answers with the same generic "Password recovery email sent" message regardless of outcome, and silently skips the send for non-superusers. Saying "recovery is not available for you" would identify which addresses belong to superusers

## Business Rules

- **Patterns gate registration, never login.** The pattern list is checked when an account is *created* and never again. Editing it does not lock existing users out. The same applies to `registration_mode`: flipping to invite-only stops new accounts, it does not evict anyone
- **Empty pattern list means everyone.** So does the single pattern `*`. "Empty" is decided in exactly one place: the list is canonicalised on write (blank entries dropped, the rest stripped and rejoined with `, `), so a value of nothing but separators — `" , "` — is stored as the empty string and read as "no restriction" by every reader
- **Invite-only beats auto-register.** In `invite_only` mode a Google sign-in never creates an account, whatever `google_auto_register` says. The admin card greys the auto-register switch out and explains why
- **Superusers keep the password path.** Password login, recovery and reset stay available to `is_superuser` accounts when password auth is off. This break-glass is exactly why the server can afford to be strict about requiring a Google-linked administrator before the switch is thrown
- **Lockout prevention is server-side.** Turning password sign-in off is rejected unless all three hold: Google OAuth is configured; an administrator can sign in with Google (the acting admin has a `google_id`, or some active superuser does); and some active superuser has a password hash, so the break-glass is not vacuous. The UI's disabled switch is a courtesy on top of this, not the control
- **Validation is scoped to what was submitted.** Only fields present in the update body are shape-checked, so a value that has drifted out of range on the row cannot block an unrelated edit (e.g. a disclaimer change). The lockout check runs only on the *transition* into Google-only mode, so an instance already in that state stays editable
- **Some origins are never gated.** Admin-created accounts, invitation acceptances, externally-arriving channel senders, and the first-superuser bootstrap all bypass the registration policy. Admin intent outranks self-service policy; an invitation *is* the admission decision; an inbound integration's own sender allowlist is its registration gate; and the bootstrap must work on an instance with no policy row yet
- **Refusals never enumerate.** Signup answers 403 with the reason code *before* the duplicate-email check, so a closed instance answers identically for a known and an unknown address. The Google callback uses the same status and body
- **Email changes are disabled while a pattern list exists.** `UserPublic.can_change_email` is false whenever `allowed_email_patterns` is non-empty; the profile form renders a read-only address, and `PATCH /users/me` refuses an email change with 403
- **Access-policy edits never bump `disclaimer_version`.** Changing who may register must not force every user to re-acknowledge an unchanged disclaimer
- **Pattern values are never logged.** Config-update logs name the changed *fields* only — a pattern list names customer domains and these lines reach shared log aggregators
- **Superuser only.** Reading or writing the full `ServerConfig` requires `is_superuser`. The access-policy projection is the only part any anonymous caller can see, and it is rate-limited per IP

### Why the pattern list is canonicalised

Three readers ask "is this list empty?", and left to themselves they gave three different answers for a value of nothing but separators (`" , "`): validation skipped the blank entries and accepted it; the matcher saw a non-blank string, built a list with no usable entries, and refused **everyone**; and the admin card counted non-blank entries and reported "anyone can register". An admin who cleared the textarea imperfectly would have got a silently closed instance that the UI showed as open.

"Empty" is therefore decided once, by `normalize_email_patterns`: blank entries are dropped, the rest stripped, and the result rejoined with `", "`. `" , "` becomes `""` and `"*@acme.com,"` becomes `"*@acme.com"`. It is applied **on write**, so the stored value is canonical, and **again on read**, so a row written before the normaliser existed is interpreted the same way. Trailing commas therefore stay harmless without ever becoming load-bearing.

**Upgrade note.** Applying the normaliser on read is a real behavioural change for any install that already had a separators-only value (`" , "`) stored: before `resolve()` normalised on read, `is_email_allowed`'s own `.strip()` saw a non-blank string and handed it to the matcher, which refused **everyone**; after this release the same row resolves to `""` and is read as **no restriction**. An instance that happened to be closed by an unnoticed stray separator opens on upgrade.

## Architecture Overview

```
Admin → Server Configuration → Access
        │  PUT /admin/server-config
        ▼
 ServerConfigService.update ──► AccessPolicyService.validate_update   (shape + lockout, BEFORE anything is applied)
        │
        ▼
   server_config row (6 access-policy columns)
        │
        ├──► AccessPolicyService.resolve() ──► AccessPolicy (frozen dataclass)
        │        │
        │        ├──► to_public()  ──► GET /server-config/access-policy  (public, rate-limited)
        │        │                          └──► /login, /signup render themselves
        │        ├──► can_register(email, origin) ──► signup, Google callback
        │        ├──► is_password_auth_allowed(user) ──► login, recovery, reset, set/change password
        │        ├──► default_role() ──► RoleService.derive_default_role
        │        └──► can_change_email() ──► UserPublic.can_change_email, PATCH /users/me
        │
        └──► first-boot seed only: AUTH_WHITELIST_USER_DOMAINS, DEFAULT_USER_ROLE
```

## Integration Points

- **[Authentication](../auth/auth.md)** — the policy is the gate on signup, password login, password recovery/reset, and set/change password. The superuser break-glass and the non-enumerating refusal shape are described there in flow terms
- **[Google OAuth](../auth/google_oauth.md)** — `google_auto_register` decides whether a Google first login creates an account; `registration_mode = invite_only` overrides it. `google_auth_enabled` in the public projection reports whether the *backend* has a client id and secret configured
- **[User Roles](../user_roles/user_roles.md)** — `default_user_role` is the source of truth for the role a new non-superuser account receives, replacing the `DEFAULT_USER_ROLE` env setting
- **[Disclaimer](disclaimer.md)** — shares the same `ServerConfig` singleton row, the same `PUT /admin/server-config` endpoint and the same `["serverConfig"]` query key, on a sibling tab. The two are otherwise independent: access-policy edits never touch `disclaimer_version`, and disclaimer edits never touch the policy
- **[Local Agent Kit](../local_agent_kit/local_agent_kit.md)** — the third occupant of the `ServerConfig` row (`local_agent_kit_enabled`), and the source of the shared anonymous rate-limiter helper the public projection reuses
- **[Server Channels](../server_channels/server_channels.md)** and **[Email Integration](../email_integration/email_integration.md)** — accounts auto-created for externally-arriving senders use the `external` origin and bypass the registration policy; the channel's own sender allowlist is the gate. They still pick up the policy's default role
- **[Desktop Auth](../desktop_auth/desktop_auth.md)** — `desktop_enabled` in the public projection mirrors the `DESKTOP_AUTH_ENABLED` deployment setting

---

*Last updated: 2026-09-05*
