# Two-Factor Authentication (2FA)

Two-factor authentication adds an optional second verification step to password and Google OAuth login. After a user enrolls at least one factor — a WebAuthn passkey or a TOTP authenticator app — every subsequent login requires that second step before an access token is issued. 2FA is off by default; users opt in through **Settings > Security**.

See [tech reference](user_2fa_tech.md) for models, route signatures, and service internals.

---

## Core Capabilities

- Optional, per-user. Existing sessions and users are unaffected until the user explicitly enrolls.
- Two supported factor types: **WebAuthn passkeys** (FIDO2, platform and roaming authenticators) and **TOTP** authenticator apps (Google Authenticator, Authy, 1Password, etc.).
- Single-use **recovery codes** (8 codes) issued at first enrollment; regenerable from Settings.
- Step-up re-authentication required for any action that disables or weakens 2FA (disable, delete a factor, regenerate recovery codes).
- Audit trail of every meaningful 2FA event via the existing `SecurityEvent` infrastructure.

---

## High-Level Login Flow

```
Login
 +-------------------------------------------------------------------------+
 |  1. Submit credentials (password or Google OAuth)                        |
 |     -> UserService.authenticate / Google OAuth callback                  |
 |                                                                         |
 |  2.   if user.two_factor_enabled is False:                              |
 |           return LoginToken { kind="token", access_token }   <- no change|
 |       else:                                                             |
 |           return MfaChallenge { kind="mfa_challenge",                   |
 |                                  challenge_token, expires_at,           |
 |                                  allowed_methods }                      |
 |                                                                         |
 |  3.  Frontend navigates to /login/mfa and shows:                        |
 |       - "Use passkey" button  -> WebAuthn assertion                     |
 |       - "Enter 6-digit code"  -> TOTP form                              |
 |       - "Use a recovery code" -> recovery-code form                     |
 |                                                                         |
 |  4.  POST /login/mfa/verify { challenge_token, method, payload }        |
 |     -> MfaService.verify_challenge                                      |
 |     -> Returns LoginToken { kind="token", access_token }                |
 +-------------------------------------------------------------------------+
```

The `challenge_token` is held in memory by `MfaChallengeContext` in the frontend. It is never written to `localStorage` or `sessionStorage`; a page reload during the challenge returns the user to step 1.

---

## User Flows

### Enroll a Passkey

1. Settings > Security > Passkeys card > "Add passkey".
2. Enter a nickname for the authenticator (e.g. "iPhone Touch ID").
3. The browser shows the native authenticator prompt (Touch ID, Windows Hello, YubiKey tap).
4. On success the server persists the credential. If this is the **first factor ever enrolled**, 2FA is enabled and 8 recovery codes are generated.
5. The UI shows the one-shot recovery-codes modal. The user must click "I've saved these codes" before the modal closes.

### Enroll a TOTP App

1. Settings > Security > Authenticator App card > "Set up authenticator app".
2. The server generates a fresh base32 secret and returns a QR code, an `otpauth://` URI, and a signed `secret_token` handle — nothing is persisted yet.
3. User scans the QR or enters the base32 secret into their authenticator app.
4. User types the 6-digit code from the app. On success the server stores the encrypted secret.
5. Same first-factor logic: if TOTP is the first factor enrolled, recovery codes are generated and shown once.

### Login with 2FA Enabled

1. User enters email and password (or uses Google OAuth).
2. Backend returns an `MfaChallenge` instead of an access token.
3. Frontend navigates to `/login/mfa`.
4. Preferred path: passkey button — browser shows native dialog; assertion POSTed to `/login/mfa/verify`.
5. Fallback: TOTP form (6-digit code) or "Use a recovery code" link (raw code, case-insensitive, dashes ignored).
6. On success the final `LoginToken` is returned; the frontend stores `access_token` in `localStorage` and navigates to the post-login target.

### Login Challenge Form UX

The TOTP form on the login challenge page uses a structured inline error with two possible kinds:

- **`invalid_code`** — triggers the "wave" auto-clear animation: a 3-second sweep through the OTP input slots, shaking each one, clearing each digit, and refocusing slot 0. The red error highlight clears in sync with the wave completing.
- **`attempt_limit_exceeded`** — shows the inline error message without the wave (the challenge is dead; retrying is pointless). The frontend should redirect to `/login` after this.

The wave animation keyframes (`shake-x`, `shake-slot`) live in `frontend/src/index.css` and are suppressed by `prefers-reduced-motion`.

### Rename or Delete a Passkey

- Rename: PATCH from the passkey row in Settings > Security (no re-auth needed).
- Delete: DELETE from the passkey row. Requires a fresh-factor step-up proof. If this is the user's last remaining 2FA factor, 2FA is automatically turned off (same wipe-and-flag flow as "Turn off 2FA"). The Settings UI detects the last-factor condition from `mfaStatus` and shows a warning dialog before the request fires.

### Disable TOTP

Requires a fresh-factor proof (password, TOTP code, or passkey assertion). Same last-factor semantics as passkey deletion: if TOTP is the user's only remaining factor, removing it automatically turns 2FA off. Endpoint is also idempotent — removing TOTP when none is enrolled returns success.

### Regenerate Recovery Codes

1. Settings > Security > Recovery Codes card > "Regenerate codes".
2. Requires a fresh-factor proof.
3. The prior batch is wiped; 8 new codes are returned once in plaintext.
4. The one-shot modal is shown again.

### Disable 2FA Entirely

1. Settings > Security > "Turn off" button in the status card.
2. `DisableTwoFactorDialog` prompts for a fresh factor — password, TOTP code, or passkey (with a step-up WebAuthn ceremony).
3. On confirm, ALL factors are wiped: passkeys, TOTP, recovery codes, pending challenges. `two_factor_enabled` is set to `False`.

---

## Business Rules

| Rule | Detail |
|------|--------|
| **Off by default** | All existing users have `two_factor_enabled=False`; no migration impact. |
| **At least one factor required** | `two_factor_enabled` flips to `True` only when the first passkey or TOTP secret is successfully enrolled. It cannot be turned on without completing enrollment. |
| **Recovery codes issued once** | Generated automatically at first enrollment. Shown in plaintext exactly once. Each code is single-use; consuming it marks `used_at`. |
| **Recovery codes regeneration invalidates prior batch** | Prior rows are deleted before fresh codes are inserted (using `batch_id` grouping). |
| **Last-factor auto-disable** | Deleting the last passkey (`DELETE /passkeys/{id}`) or removing TOTP (`DELETE /totp`) while 2FA is on automatically turns 2FA off via the same wipe-and-flag flow as `POST /mfa/disable` (clears passkeys, TOTP secret, recovery codes, pending challenges; sets `two_factor_enabled=False`; writes `MFA_DISABLED` security event with `reason="last_factor_removed"`). No separate disable call is required. |
| **Step-up required to weaken 2FA** | Disable, delete-a-factor, and regenerate-codes all require a fresh proof: password, TOTP code, or passkey assertion. The access token alone is not enough. |
| **Challenge TTL** | Login challenges expire 5 minutes (`MFA_CHALLENGE_TTL_SECONDS=300`) after creation. Expired challenges return `410 Gone`. |
| **Attempt cap per challenge** | 5 failed verifications (`MFA_MAX_ATTEMPTS_PER_CHALLENGE=5`) lock the challenge; further attempts return `429`. User must restart login. |
| **Per-user rate limit** | At most 10 `POST /login/mfa/verify` attempts per 5-minute window per user (in-memory token bucket). Excess attempts return `429` and log `MFA_RATE_LIMITED`. |
| **Anonymous rate limit** | At most 20 `POST /login/mfa/verify` attempts per 5-minute window per source IP, applied before challenge resolution. Catches token-spray probes that never resolve to a real user. Bad-token and orphaned-challenge probes are logged at `WARNING` level in server logs. |
| **TOTP replay protection** | `last_used_step` on `UserTotpSecret` rejects the same RFC-6238 time-step submitted twice within the valid ±1 window. |
| **TOTP clock skew** | `pyotp.TOTP.verify` accepts ±1 step (30 s). Wider windows are not accepted. |
| **Challenge single-use** | A successfully verified challenge is marked `consumed_at` and cannot be reused. |
| **`DELETE /totp` is idempotent** | Returns success even when no TOTP is enrolled. |
| **Google-OAuth users without a password** | May still use passkey or TOTP for step-up proof; password proof is unavailable to them for step-up mutations. |
| **Password reset does not bypass 2FA** | Password recovery lets the user change their password, but 2FA remains in force on the next login. The UI communicates this explicitly. |

---

## Settings UI — Last-Factor Awareness

The Settings > Security tab passes `mfaStatus` down through `SecurityTab → PasskeySection → PasskeyList` and independently to `DisableTotpDialog`. Both components switch their copy when the action would remove the user's last remaining factor:

- **Passkey delete dialog** — title and description switch to warn the user that deleting this passkey will turn 2FA off.
- **Disable TOTP dialog** — title becomes "Remove TOTP (and turn off 2FA)", description reads "This is your last 2FA factor. If you remove it, two-factor authentication will be turned off for your account." Button label changes to "Remove and turn off 2FA".
- **Success toast** — reflects the auto-disable (e.g. "Two-factor authentication turned off").

---

## Dashboard Nudge Banner

Users who have never enrolled 2FA see a dismissable banner on the dashboard encouraging them to set it up. Dismissal is stored in `localStorage` keyed by `"mfa.enable_banner.dismissed.{user_id}"` so the prompt reappears on a fresh browser/device. The banner disappears permanently once `user.two_factor_enabled` is `True`.

---

## Empty / Loading / Error States

| State | Behavior |
|-------|----------|
| Security tab loading | Spinner while `GET /users/me/mfa/status` resolves. |
| 2FA off, no factors | Status card shows "OFF" badge and explainer copy; Passkey and TOTP cards show empty-state CTAs. |
| WebAuthn unsupported browser | "Add passkey" button is greyed out with a tooltip: "Your browser doesn't support passkeys." |
| User cancels WebAuthn dialog (`NotAllowedError` / `AbortError`) | Non-destructive toast "Cancelled — no changes made"; no state change. |
| Challenge expired (410) | Frontend redirects to `/login` with a toast. |
| Too many attempts (429, `attempt_limit_exceeded`) | Inline error shown on the challenge form without the wave animation; challenge is dead. Frontend should redirect to `/login`. |
| Rate limit hit (429, `rate_limited`) | Toast shown on `/login/mfa` before redirecting to `/login`. |
| Wrong TOTP / recovery code (400, `invalid_code`) | Wave auto-clear animation on TOTP form; attempts counter incremented server-side. |
| Last-factor delete | Dialog warns "This is your last 2FA factor. Removing it will turn off two-factor authentication for your account." Button label flips to "Remove and turn off 2FA"; on success the toast also reflects the auto-disable. |
| Step-up required (401) | The proof form surfaces a "Verification failed" error. |
| Recovery codes modal | Cannot be dismissed without clicking "I've saved these codes". |
| TOTP input | `inputMode="numeric"`, `autocomplete="one-time-code"` for mobile keyboard and password manager offer. |

---

## Integration Points

### Auth (login + Google OAuth)

Both `POST /login/access-token` and `POST /auth/google/callback` return a `LoginResponse` discriminated union. When `user.two_factor_enabled=True` they return `MfaChallenge`; otherwise `LoginToken`. See [auth](../auth/auth.md) and [Google OAuth](../auth/google_oauth.md).

The `allowed_methods` list in the challenge is computed by `MfaService.allowed_methods_for_user` and reflects only the factors the specific user has enrolled with unused codes.

### Desktop Auth

The Cinna Desktop OAuth flow (`/desktop-auth/authorize` → consent page → `/desktop-auth/token`) reuses the browser session, so any 2FA challenge that was satisfied during the browser login is inherited. No additional 2FA step is added by the desktop-auth flow. See [Desktop Auth](../desktop_auth/desktop_auth.md#2fa-and-desktop-auth).

### Encryption

TOTP secrets are encrypted at rest with the existing Fernet `encrypt_field` / `decrypt_field` helpers (`backend/app/core/security.py`). No new key or salt. The `secret_token` enrollment handle is also a Fernet-encrypted JSON envelope; it expires after 10 minutes (`_TOTP_ENROLLMENT_TTL_SECONDS=600`).

### Security Events

Every meaningful 2FA action writes a `SecurityEvent` row (see [tech ref](user_2fa_tech.md#security-events)).

### A2A Access Tokens

Pre-issued A2A machine tokens are explicitly **out of scope** for user 2FA. They are scoped programmatic credentials that authenticate independently of the user's web session. Enabling 2FA on a user account has no effect on existing A2A tokens for that user's agents. See [A2A Access Tokens](../a2a_integration/a2a_access_tokens/a2a_access_tokens.md#2fa-does-not-apply-to-a2a-tokens).

---

## Threat Model Summary

- **Phishing resistance**: WebAuthn passkeys are origin-bound — they cannot be used on a fraudulent site.
- **Authenticator-app protection**: TOTP codes rotate every 30 s and are verified server-side.
- **Recovery codes**: bcrypt-hashed, single-use, batch-invalidated on regeneration.
- **Challenge tokens**: `secrets.token_urlsafe(32)`, single-use, server-side; constant-time comparison via `secrets.compare_digest`.
- **Sign-count regression**: A passkey whose sign count decreases (possible clone) still authenticates but logs `MFA_SIGN_COUNT_REGRESSION` at severity `"high"`.
- **Superusers have no admin override**: Superusers cannot disable 2FA on behalf of another user in MVP. The recovery path for a locked-out user with no recovery codes is a direct database operation.
- **Guest sessions**: Guest-share JWTs (`role=chat-guest`) and bundle install flows never traverse the MFA branch.

---

*Last updated: 2026-05-21*
