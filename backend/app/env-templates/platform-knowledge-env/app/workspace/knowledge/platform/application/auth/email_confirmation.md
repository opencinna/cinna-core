# Email Confirmation

## Purpose

Hardens a publicly-reachable server against abuse of its outbound-email capability. Users must confirm their email address before the platform delivers any outbound email on their behalf (except password recovery). Confirmation status also keys tiered agent-creation limits.

## Core Concepts

| Concept | Definition |
|---------|-----------|
| **Email-confirmed marker** | Boolean on `User` (`email_confirmed`) set to `True` once the user clicks a confirmation link or logs in via Google OAuth |
| **Outbound-email gate** | Central check (`EmailConfirmationService.is_outbound_email_allowed`) applied before every non-recovery outbound email send |
| **Resend cooldown** | 300-second (5-minute) minimum interval between confirmation email sends, stored on the user row so it works across multiple backend workers |
| **Password-recovery cooldown** | Parallel 300-second cooldown on password-recovery emails — rate-limiting only, no gate: password recovery always works regardless of confirmation status |
| **Agent-creation limit** | Tiered cap: superuser = unlimited, confirmed = 50, unconfirmed = 5 (standalone agents only; bundle installs excluded) |
| **Google auto-confirm** | Users who create an account via Google OAuth, or who log in via Google OAuth on a pre-existing unconfirmed account, are auto-confirmed without clicking a link |

## User Flows

### New User — Email/Password Signup

1. User registers via the signup form
2. Backend creates the account with `email_confirmed=False`
3. Backend immediately sends a confirmation email (bypassing the cooldown for the first send)
4. User clicks the link in the email, landing on `/confirm-email?token=…`
5. Frontend submits the token; backend confirms the address and returns success
6. User is now confirmed — outbound email and the 50-agent cap are both unlocked

### Confirming Email (Token Link)

1. User opens the confirmation link from email
2. Landing page (`/confirm-email`) extracts the token from the URL query string
3. Frontend calls `POST /confirm-email/` with the token
4. On success: page shows a confirmation message and links to the dashboard
5. On failure (bad/expired token): page shows an error with a prompt to request a new link
6. Confirming an already-confirmed account is idempotent — returns success without re-sending

### Resending the Confirmation Email (In-App)

1. Unconfirmed user opens Settings > My profile
2. Email row shows an amber indicator and a "Resend confirmation" button
3. User clicks the button — `POST /users/me/resend-confirmation` is called
4. If not in cooldown, a new confirmation email is sent; the button disables with a countdown timer
5. On cooldown, the response still returns success with a `resend_available_at` timestamp; the button stays disabled until the timer expires

### Resending Without Login (Public Endpoint)

An unauthenticated user who lost the link can visit the login/recovery flow. The public `POST /resend-confirmation/{email}` always returns generic success — it never reveals whether the address exists, is already confirmed, or is in cooldown (non-enumerating).

### New User — Google OAuth

1. User authenticates via Google
2. Backend creates the account and sets `email_confirmed=True` immediately (Google has already verified the address)
3. User sees the green confirmed indicator from the first login; no extra step required

### Existing User — Google Login Auto-Confirm

1. An existing user (created before this feature, or via email/password) authenticates via Google OAuth
2. If their account was unconfirmed, the backend auto-confirms them on successful Google login
3. On next page load the profile indicator flips to green

### Unconfirmed User at Agent Limit

1. Unconfirmed user tries to create their 6th agent (via the UI, account-CLI, or any creation route)
2. Backend returns HTTP 403 with the message: "Agent limit reached (5). Confirm your email to raise the limit to 50."
3. Creating agents via bundle install does not count toward the limit; only user-created standalone agents count

### Blocked Outbound Email

1. An unconfirmed user is reachable on an **email Server Channel** and one of their agents answers a message that arrived on it (per-agent Email Integration was deleted in Phase 4 of the channels & identity unification — migration `ca0192122e0c` dropped the table; email is a channel transport now)
2. The agent's reply is written to the durable `OutgoingEmailQueue` by `EmailChannelAdapter`, and the outbound-email gate fires at send time in `EmailSendingService._send_single_email` — the entry is marked `BLOCKED_UNCONFIRMED` (a terminal status; it is never retried) and nothing leaves the server
3. Confirming the address does not resurrect an already-blocked entry; only mail queued afterwards is sent

### Publisher Email Indicator on Catalog and Install Cards

Catalog cards and the install detail header show the publisher's email address. When the publisher's email is not confirmed, an amber warning icon appears beside the email. When confirmed, a green check icon is shown. The indicator helps users assess the trust level of a bundle publisher.

## Business Rules

### Outbound-Email Gate

- All platform-generated outbound email passes through `EmailConfirmationService.is_outbound_email_allowed()` for the responsible user
- The gate is fail-safe: a missing or inactive user returns `False`
- **Exceptions (gate always bypassed):**
  - Password recovery emails — an unconfirmed user must always be able to recover their account
  - Admin test-email (superuser diagnostic to an arbitrary address)
  - Welcome/new-account email sent at admin-created user creation (carries a temporary password and is admin-initiated)
- **Gated surfaces (exhaustive list):**
  1. System notifications — single choke point in `SystemNotificationService.notify()` covers all current and future notification types
  2. Agent email replies on an email Server Channel — blocked at send time in `EmailSendingService._send_single_email()`, which marks the queue entry `BLOCKED_UNCONFIRMED`

### Cooldowns

- Confirmation email resend: 300 seconds between any two sends to the same address (first send at signup bypasses the cooldown via `force=True`)
- Password-recovery email: 300 seconds between sends; skipped silently when in cooldown so the public endpoint always returns a generic success message
- Both cooldowns are stored as timestamps on the `User` row rather than in memory, so they are consistent across multiple backend workers (gunicorn)
- The authenticated resend endpoint (`POST /users/me/resend-confirmation`) returns `resend_available_at` so the frontend can show a countdown

### Agent-Creation Limits

- Superusers: unlimited
- Email-confirmed users: 50 standalone agents
- Unconfirmed users: 5 standalone agents
- The count includes only user-created standalone agents (`bundle_uuid IS NULL AND is_publisher_install == False`)
- Bundle installs (consumer copies of others' work) never count toward the cap
- All three creation routes enforce the same limit through the same helper: `POST /agents/`, `POST /agents/create-flow`, and the account-CLI `POST /account/agents`
- Error response is HTTP 403

### Auto-Confirmation Rules

- Google OAuth account creation: confirmed immediately
- Google OAuth login on an existing unconfirmed account: confirmed on successful login
- Superuser creation (by admin or initial-data seeding): confirmed immediately
- Email-integration-created users: start unconfirmed; a confirmation email is sent if SMTP is configured
- Admin-created non-superusers: start unconfirmed; both the welcome email (which carries the temporary password) and a confirmation email are sent

### Token Security

- Confirmation tokens are HS256 JWTs with a `"purpose": "email_confirm"` claim
- The purpose claim prevents a password-reset token from being used to confirm an email address, and vice versa (password-reset tokens have no purpose claim; the confirmation verifier rejects any token lacking the exact purpose value)
- Token expiry: 48 hours (configurable via `EMAIL_CONFIRM_TOKEN_EXPIRE_HOURS`)
- No secrets are logged; the token is only transmitted in the confirmation link

### Backfill and Existing Users

Existing users at migration time are backfilled to `email_confirmed=True`. This prevents the feature from immediately breaking outbound email, capping agent counts at 5, and suppressing notifications for legitimate existing accounts — the anti-abuse target is new signups on a public server, not pre-existing users. New users created after the migration default to unconfirmed.

### Non-Enumeration

The public resend-confirmation endpoint (`POST /resend-confirmation/{email}`) never reveals whether an address is registered, already confirmed, or in cooldown. It always returns a generic success message. The password-recovery endpoint has always raised 404 for unknown email addresses — that behavior is unchanged by this feature.

## Architecture Overview

```
Signup / admin-create
      │  (email_confirmed=False; confirmation email sent immediately)
      ▼
User clicks link ──→ POST /confirm-email/ ──→ email_confirmed=True
      ▲                                                │
      │  resend (cooldown-gated)                       ▼
 Profile UI "Resend confirmation"      outbound email and full agent limit unlocked

Google OAuth create/login ─────────────────────────────────────► email_confirmed=True (auto)

Outbound email path ──→ EmailConfirmationService.is_outbound_email_allowed(user)?
      ├─ password recovery  → ALWAYS allowed (bypass gate)
      └─ everything else    → allowed only if user.email_confirmed
```

## Integration Points

- **[Authentication](auth.md)** — reuses the JWT token pattern from password recovery; confirmation and resend routes live alongside the existing recovery routes in `login.py`
- **[Google OAuth](google_oauth.md)** — Google-authenticated users are auto-confirmed at create time and at login time
- **[Email Integration](../email_integration/email_integration.md)** — agent email replies (auto-session and manual) are gated by the outbound-email gate applied to the agent owner
- **[System Notifications](../system_notifications/system_notifications.md)** — the central `notify()` choke point checks confirmation status before dispatching any notification type
- **[Agent Bundles](../../agents/agent_bundles/agent_bundles.md)** — `CatalogEntryPublic` exposes `publisher_email_confirmed`; catalog and install cards render the indicator
- **[User Roles](../user_roles/user_roles.md)** — superusers are always unlimited regardless of confirmation status
- **[Account CLI Workspace](../cinna_cli_integration/account_cli_workspace.md)** — the account-CLI agent-creation route delegates to the same `AgentService.create_agent` that enforces the limit
