# Email Confirmation — Technical Details

## File Locations

### Backend - Models

- `backend/app/models/users/user.py` — `User` table (new fields), `ConfirmEmailRequest`, `ResendConfirmationResponse`
- `backend/app/models/bundles/catalog.py` — `CatalogEntryPublic` (`publisher_email_confirmed` field)
- `backend/app/models/email/outgoing_email_queue.py` — `OutgoingEmailStatus.BLOCKED_UNCONFIRMED` status value

### Backend - Services

- `backend/app/services/users/email_confirmation_service.py` — `EmailConfirmationService` (the central gate and all confirmation lifecycle methods)
- `backend/app/services/users/user_service.py` — `recover_password` (cooldown), `create_user`/`register_user`/`create_email_user` (trigger first confirmation email, superuser auto-confirm)
- `backend/app/services/users/auth_service.py` — Google OAuth auto-confirm at account creation and at login
- `backend/app/services/notifications/notification_service.py` — `SystemNotificationService.notify()` (choke point for all system notifications)
- `backend/app/services/email/sending_service.py` — `EmailSendingService.queue_outgoing_email` (enqueue-time gate) and `_send_single_email` (send-time defense)
- `backend/app/services/tasks/input_task_service.py` — `InputTaskService.send_email_answer` (manual reply gate)
- `backend/app/services/agents/agent_service.py` — `AgentService._enforce_agent_creation_limit`
- `backend/app/services/bundles/catalog_service.py` — populates `publisher_email_confirmed` in catalog projection

### Backend - Routes

- `backend/app/api/routes/login.py` — `POST /confirm-email/`, `POST /resend-confirmation/{email}`
- `backend/app/api/routes/users.py` — `POST /users/me/resend-confirmation`
- `backend/app/api/routes/agents.py` — translates `ValueError` from limit enforcement to HTTP 403
- `backend/app/api/routes/cli.py` — same ValueError → 403 translation for account-CLI agent creation

### Backend - Utilities and Config

- `backend/app/utils.py` — `generate_email_confirmation_token`, `verify_email_confirmation_token`, `generate_confirmation_email`
- `backend/app/core/config.py` — `EMAIL_CONFIRM_TOKEN_EXPIRE_HOURS`, `CONFIRMATION_EMAIL_COOLDOWN_SECONDS`, `PASSWORD_RECOVERY_EMAIL_COOLDOWN_SECONDS`, `AGENT_LIMIT_UNCONFIRMED`, `AGENT_LIMIT_CONFIRMED`
- `backend/app/email-templates/src/confirm_email.mjml` — MJML source for the confirmation email
- `backend/app/email-templates/build/confirm_email.html` — built HTML template

### Backend - Migration

- `backend/app/alembic/versions/61220fd330c3_add_email_confirmation_fields_to_user.py`
  - `down_revision = '3a52a997a322'`

### Frontend

- `frontend/src/routes/confirm-email.tsx` — public confirm-email landing page (token from URL)
- `frontend/src/components/UserSettings/UserInformation.tsx` — confirmation status indicator + resend button with cooldown countdown
- `frontend/src/components/Common/PublisherEmailConfirmedIcon.tsx` — reusable green-check / amber-warning icon component
- `frontend/src/components/Catalog/CatalogCard.tsx` — publisher email indicator via `PublisherEmailConfirmedIcon`
- `frontend/src/components/Install/InstallAgentHeaderCard.tsx` — publisher email indicator via `PublisherEmailConfirmedIcon`

---

## Database Schema

### `User` Table — New Fields

| Field | Type | Default | Purpose |
|-------|------|---------|---------|
| `email_confirmed` | `bool` | `False` (application-level; existing rows backfilled `True` by migration) | The confirmation marker; gates all non-recovery outbound email |
| `email_confirmed_at` | `datetime \| None` | `None` | When the address was confirmed (audit / UI display) |
| `last_confirmation_email_sent_at` | `datetime \| None` | `None` | Cooldown anchor for resend-confirmation; lives on the user row for multi-worker consistency |
| `last_password_recovery_email_sent_at` | `datetime \| None` | `None` | Cooldown anchor for password-recovery; same reasoning — public by-email endpoint cannot use in-memory state |

### `UserPublic` — Added Fields

```python
email_confirmed: bool = False
email_confirmed_at: datetime | None = None
```

Raw cooldown timestamps are intentionally NOT exposed on `UserPublic`. Only `resend_available_at` is surfaced, and only by the authenticated resend endpoint, to avoid leaking timing data broadly.

### New API Schemas (same file)

```python
class ConfirmEmailRequest(SQLModel):
    """POST body for /confirm-email/ — the token from the email link."""
    token: str

class ResendConfirmationResponse(SQLModel):
    """Response for POST /users/me/resend-confirmation."""
    message: str
    resend_available_at: datetime | None = None
```

### `CatalogEntryPublic` — Added Field

```python
publisher_email_confirmed: bool = False
```

Populated in `catalog_service.py` by looking up the publisher `User` row from the bundle's owner. Exposed on both `CatalogEntryPublic` (models) and in the bundles route projection.

### `OutgoingEmailStatus` — New Value

```python
BLOCKED_UNCONFIRMED = "blocked_unconfirmed"
```

Terminal status — a queue entry marked `BLOCKED_UNCONFIRMED` is never retried.

---

## API Endpoints

### Login Routes (`backend/app/api/routes/login.py`)

| Method | Path | Auth | Request | Response | Notes |
|--------|------|------|---------|----------|-------|
| `POST` | `/api/v1/confirm-email/` | None (public) | `ConfirmEmailRequest { token }` | `Message` | 400 on bad/expired token; 403 on inactive user; 404 on unknown email; idempotent |
| `POST` | `/api/v1/resend-confirmation/{email}` | None (public) | Path param | `Message` | Always returns generic success; non-enumerating |

### User Routes (`backend/app/api/routes/users.py`)

| Method | Path | Auth | Request | Response | Notes |
|--------|------|------|---------|----------|-------|
| `POST` | `/api/v1/users/me/resend-confirmation` | `CurrentUser` | None | `ResendConfirmationResponse` | Returns `resend_available_at` for countdown; always success |

---

## Services and Key Methods

### `EmailConfirmationService` (`backend/app/services/users/email_confirmation_service.py`)

```python
# Central gate — the single source of truth for the outbound-email decision
@staticmethod
def is_outbound_email_allowed(user: User | None) -> bool:
    """Returns True only for active, email-confirmed users. Fail-safe: None/inactive → False."""

# Sends a confirmation email respecting the cooldown.
# force=True bypasses the cooldown (used for first send at account creation).
# Returns True if sent, False if suppressed (cooldown, emails disabled, already confirmed).
# Never raises on SMTP failure — logs and returns False.
@staticmethod
def send_confirmation_email(*, session: Session, user: User, force: bool = False) -> bool: ...

# Public resend by email address — non-enumerating, always silent
@staticmethod
def resend_confirmation(*, session: Session, email: str) -> None: ...

# Verify token, set email_confirmed=True + email_confirmed_at=now; idempotent
# Raises ValueError on bad/expired token, missing user, or inactive user
@staticmethod
def confirm_email(*, session: Session, token: str) -> User: ...

# Direct confirm (no token) — used for Google OAuth auto-confirm; idempotent
@staticmethod
def mark_confirmed(*, session: Session, user: User) -> None: ...

# Earliest time next resend is permitted, for UI countdown
@staticmethod
def resend_available_at(user: User) -> datetime | None: ...
```

Module-level helper (not on the class):
```python
def _cooldown_elapsed(last_sent: datetime | None, interval: timedelta) -> bool:
    """True if interval has elapsed since last_sent (or never sent).
    Handles naive DB timestamps by assuming UTC."""
```

### `UserService` — Modified Methods (`backend/app/services/users/user_service.py`)

- `register_user`: after creating the user, calls `EmailConfirmationService.send_confirmation_email(force=True)` for the first send
- `create_user` (admin): superusers get `email_confirmed=True` + `email_confirmed_at=now()` at object construction time; non-superusers start unconfirmed and receive a confirmation email via `send_confirmation_email(force=True)`
- `create_email_user` (email integration): user starts unconfirmed; confirmation email sent via `send_confirmation_email(force=True)`
- `recover_password`: password recovery is **never** gated by `email_confirmed`. A 300-second per-user cooldown (`last_password_recovery_email_sent_at`) rate-limits repeated sends; when in cooldown, the send is skipped silently so the public response stays generic. The existing 404 for unknown email is unchanged.

### `AuthService` — Modified Methods (`backend/app/services/users/auth_service.py`)

- `create_user_from_google`: sets `email_confirmed=True` and `email_confirmed_at=datetime.now(timezone.utc)` on the `User` constructor
- `authenticate_with_google`: after resolving an existing user, calls `EmailConfirmationService.mark_confirmed(session, user)` if `not user.email_confirmed` — auto-confirms on successful Google login

### `AgentService._enforce_agent_creation_limit` (`backend/app/services/agents/agent_service.py`)

```python
@staticmethod
def _enforce_agent_creation_limit(*, session: Session, user: User) -> None:
    """Raise ValueError if user is at/over their agent cap.
    - superuser → unlimited
    - email_confirmed → AGENT_LIMIT_CONFIRMED (default 50)
    - unconfirmed → AGENT_LIMIT_UNCONFIRMED (default 5)
    Counts owner_id==user.id AND bundle_uuid IS NULL AND is_publisher_install==False.
    """
```

Called from:
- `AgentService.create_agent` (covers `POST /agents/` and `POST /account/agents`)
- `AgentService.create_agent_flow` (covers `POST /agents/create-flow`)

Route layer translates `ValueError` to HTTP 403. The error message for unconfirmed users is:
```
"Agent limit reached (5). Confirm your email to raise the limit to 50."
```

---

## Outbound-Email Gate — Complete Choke Points

Every outbound email path checks `EmailConfirmationService.is_outbound_email_allowed(user)` except password recovery and admin test email.

| Surface | Location | Decision | Notes |
|---------|----------|----------|-------|
| All system notifications | `notification_service.py :: SystemNotificationService.notify()` | **GATE** on recipient user | Single choke point — covers all current and future notification types |
| Agent email reply — enqueue | `sending_service.py :: EmailSendingService.queue_outgoing_email()` | **GATE** on agent/install owner | Primary UX check — returns `None` on failure (no queue entry created) |
| Agent email reply — send | `sending_service.py :: EmailSendingService._send_single_email()` | **GATE** on agent/install owner | Defense-in-depth; marks entry `BLOCKED_UNCONFIRMED` (terminal) |
| Manual "Send Answer" | `input_task_service.py :: InputTaskService.send_email_answer()` | **GATE** on task owner | Returns `{"success": False, "error": "Your email is not confirmed..."}` |
| Password recovery | `user_service.py :: UserService.recover_password()` | **BYPASS** (always allowed) | Cooldown only; recovery must always work |
| Welcome/new-account email | `routes/users.py :: create_user()` | **BYPASS** (admin-initiated) | Carries temporary password; sent regardless of gate (D3) |
| Admin test-email | `routes/utils.py :: test_email()` | **BYPASS** (superuser diagnostic) | Arbitrary address; superuser-only |

The "responsible user" for agent email replies is the platform account that owns the mailbox/agent, not the external email recipient:
- Owner mode (`bundle_uuid IS NULL` or `is_publisher_install`): `parent_agent.owner_id`
- Install mode (foreign install): publisher install's `owner_id` (SMTP config lives there)

---

## Token Utilities (`backend/app/utils.py`)

```python
_EMAIL_CONFIRM_PURPOSE = "email_confirm"

def generate_email_confirmation_token(email: str) -> str:
    """HS256 JWT, expires EMAIL_CONFIRM_TOKEN_EXPIRE_HOURS (default 48h).
    Payload: {exp, nbf, sub: email, purpose: "email_confirm"}."""

def verify_email_confirmation_token(token: str) -> str | None:
    """Decode and verify. Returns the email (sub) on success, None on failure.
    Rejects tokens where purpose != "email_confirm" — prevents cross-use
    with password-reset tokens (which carry no purpose claim)."""

def generate_confirmation_email(email_to: str, email: str, token: str) -> EmailData:
    """Renders confirm_email.html with {project_name, username, email,
    valid_hours, link} where link = f"{FRONTEND_HOST}/confirm-email?token={token}"."""
```

---

## Configuration (`backend/app/core/config.py`)

| Setting | Default | Purpose |
|---------|---------|---------|
| `EMAIL_CONFIRM_TOKEN_EXPIRE_HOURS` | `48` | Confirmation link validity window |
| `CONFIRMATION_EMAIL_COOLDOWN_SECONDS` | `300` | Min interval between confirmation resends |
| `PASSWORD_RECOVERY_EMAIL_COOLDOWN_SECONDS` | `300` | Min interval between password-recovery sends |
| `AGENT_LIMIT_UNCONFIRMED` | `5` | Max standalone agents for unconfirmed users |
| `AGENT_LIMIT_CONFIRMED` | `50` | Max standalone agents for confirmed users |

---

## Database Migration

**Revision:** `61220fd330c3` (`down_revision = '3a52a997a322'`)

**Strategy (D9 — backfill existing users as confirmed):**

1. `ADD COLUMN email_confirmed BOOLEAN NOT NULL server_default true` — backfills every existing row to `True`
2. `ALTER COLUMN email_confirmed DROP DEFAULT` — drops the server default so SQLModel's application-level `default=False` governs new inserts
3. `ADD COLUMN email_confirmed_at TIMESTAMP NULL`
4. `ADD COLUMN last_confirmation_email_sent_at TIMESTAMP NULL`
5. `ADD COLUMN last_password_recovery_email_sent_at TIMESTAMP NULL`
6. `UPDATE user SET email_confirmed_at = NOW() WHERE email_confirmed = true AND email_confirmed_at IS NULL` — backfills confirmed-at for UI consistency

**Downgrade:** drops all four columns.

---

## Frontend Components

### `confirm-email.tsx` (`frontend/src/routes/confirm-email.tsx`)

Public route (outside `_layout`). Reads `?token=` from the URL via `validateSearch`, calls `POST /confirm-email/` on mount, and renders one of three states: loading, success (with a link back to the dashboard), or failure (with a prompt to request a new link). Visual pattern matches `reset-password.tsx`.

### `UserInformation.tsx` (`frontend/src/components/UserSettings/UserInformation.tsx`)

Email confirmation UI block added to the email row in Settings > My profile:
- **Confirmed state:** green `CheckCircle` icon + "Confirmed" label
- **Unconfirmed state:** amber `AlertTriangle` icon + "Not confirmed" + "Resend confirmation" button
- Resend button uses `useMutation` calling `UsersService.resendConfirmationMe()`
- On success: `resend_available_at` stored in component state; button disables with a `Math.max(0, disabledUntil - now)` countdown (ticked with a 1-second interval effect)
- Initial disabled state derived from `resend_available_at` in the mutation response

### `PublisherEmailConfirmedIcon.tsx` (`frontend/src/components/Common/PublisherEmailConfirmedIcon.tsx`)

Reusable icon component. Accepts `confirmed: boolean`. Renders:
- `confirmed=true`: green check icon
- `confirmed=false`: amber warning icon with tooltip "Publisher email not confirmed"

Used in:
- `frontend/src/components/Catalog/CatalogCard.tsx` — beside `entry.publisher_email` (appears in both the email-above-name and email-below-name positions depending on card variant)
- `frontend/src/components/Install/InstallAgentHeaderCard.tsx` — beside the publisher email in the install detail header

### State Management

- Confirmation status (`email_confirmed`, `email_confirmed_at`) flows in via the existing `["currentUser"]` React Query cache — no new context or store needed
- Catalog/install cards consume `CatalogEntryPublic` via existing queries; `publisher_email_confirmed` flows in automatically after client regeneration (`make gen-client`)
