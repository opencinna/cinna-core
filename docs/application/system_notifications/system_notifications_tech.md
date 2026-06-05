# System Notifications — Technical Details

## File Locations

### Backend

- `backend/app/models/notifications/user_notification_setting.py` — `UserNotificationSetting` (table), `UserNotificationSettingUpdate`, `NotificationSettingItem`, `NotificationSettingsPublic`
- `backend/app/services/notifications/notification_catalog.py` — `NotificationType` enum, `NotificationTypeMeta` dataclass, `NOTIFICATION_CATALOG` dict
- `backend/app/services/notifications/notification_service.py` — `SystemNotificationService` (dispatch + throttle)
- `backend/app/services/notifications/notification_setting_service.py` — `NotificationSettingService` (preference CRUD)
- `backend/app/api/routes/notification_settings.py` — REST endpoints; registered in `backend/app/api/main.py`
- `backend/app/services/events/activity_service.py` — `_notify_session_error()` choke point wired into `create_error_activity()` and `handle_session_state_updated()`

### Email templates

- `backend/app/email-templates/src/session_error.mjml` — MJML source
- `backend/app/email-templates/build/session_error.html` — compiled HTML read at runtime
- `backend/app/email-templates/build/model_deprecated.html` — compiled HTML for model-deprecated notifications (no MJML source committed; edit the HTML directly or add MJML source and recompile)

The runtime reads only the built HTML. To modify a template from MJML source:

```bash
npx mjml backend/app/email-templates/src/session_error.mjml \
         -o backend/app/email-templates/build/session_error.html
```

Commit the resulting `build/*.html`.

### Frontend

- `frontend/src/components/UserSettings/NotificationSettings.tsx` — Settings card component
- `frontend/src/routes/_layout/settings.tsx` — My profile tab; renders `<NotificationSettings />`

### Migrations

- `6e43bbbec5cc` (`backend/app/alembic/versions/6e43bbbec5cc_add_user_notification_setting_table.py`) — creates `user_notification_setting` table (down_revision: `aa44agentapi04`)

---

## Database Schema

**Table**: `user_notification_setting`

| Field | Type | Constraints | Notes |
|-------|------|-------------|-------|
| `id` | UUID | PK, `default_factory=uuid4` | |
| `user_id` | UUID | FK → `user.id`, `ON DELETE CASCADE`, indexed | Session owner |
| `notification_type` | varchar(64) | NOT NULL | Catalog key, e.g. `"session_error"` |
| `email_enabled` | boolean | NOT NULL, default `True` | Whether email is on for this type |
| `created_at` | datetime | UTC now | |
| `updated_at` | datetime | UTC now, bumped on upsert | |

Constraints:
- `UniqueConstraint("user_id", "notification_type", name="uq_user_notification_setting_user_type")` — one row per (user, type) pair; upsert target
- `Index("ix_user_notification_setting_user_id", "user_id")` — list-by-user queries

**Design note:** `notification_type` is stored as a plain string (not a DB enum) so adding new catalog types requires no schema change and no migration. Validation against `NotificationType` happens at the service layer.

**Rows are created lazily** — only when a user changes a preference away from the catalog default. A missing row resolves to `NOTIFICATION_CATALOG[type].default_email_enabled`. No backfill migration needed for new types.

---

## Notification Catalog

`backend/app/services/notifications/notification_catalog.py`

### `NotificationType(str, Enum)`

```python
class NotificationType(str, Enum):
    SESSION_ERROR = "session_error"
    MODEL_DEPRECATED = "model_deprecated"
```

### `NotificationTypeMeta` (frozen dataclass)

| Field | Type | Purpose |
|-------|------|---------|
| `label` | str | User-facing name in Settings UI |
| `description` | str | Muted description in Settings UI |
| `default_email_enabled` | bool | Catalog default when no preference row exists |
| `email_template` | str | Filename under `email-templates/build/` |
| `subject` | `Callable[[dict], str]` | Builds the email subject from the render context |
| `dedup_scope` | `str \| None` | Context key used for per-event dedup (e.g. `"session_id"`) |

### `SESSION_ERROR` catalog entry

| Field | Value |
|-------|-------|
| `label` | `"Session errors"` |
| `description` | `"Email me when one of my agent sessions ends with an error."` |
| `default_email_enabled` | `True` |
| `email_template` | `"session_error.html"` |
| `subject` | `lambda ctx: f"{settings.PROJECT_NAME} — Session error on agent {ctx.get('agent_name', 'your agent')}"` |
| `dedup_scope` | `"session_id"` |

### `MODEL_DEPRECATED` catalog entry

| Field | Value |
|-------|-------|
| `label` | `"Deprecated AI models"` |
| `description` | `"Email me when one of my agent environments is configured to use an AI model that is deprecated or no longer available."` |
| `default_email_enabled` | `True` |
| `email_template` | `"model_deprecated.html"` |
| `subject` | `lambda ctx: f"{settings.PROJECT_NAME} — Update the AI model for {ctx.get('instance_name', 'your environment')}"` |
| `dedup_scope` | `"environment_id"` |

Dispatched from `model_discovery_service.dispatch_model_deprecation_notifications()` (called by
the discovery cron after each `refresh_all_credentials` run) when an environment **newly**
transitions into a warning state. Context keys: `project_name`, `agent_name`, `instance_name`,
`environment_id`, `detail` (per-mode model + CTA), `link`.

Email template: `backend/app/email-templates/build/model_deprecated.html`.

### How to add a new notification type

1. Add an enum value to `NotificationType` (e.g. `TASK_FAILED = "task_failed"`).
2. Add a matching `NOTIFICATION_CATALOG` entry with the appropriate metadata.
3. Build and commit an email template under `email-templates/build/`.
4. No service, route, or migration change is needed — the Settings API and dispatch service read from the catalog.

---

## API Endpoints

**Router**: `backend/app/api/routes/notification_settings.py`
**Prefix**: `/notification-settings`
**Tag**: `notification-settings`
**Auth**: `CurrentUser` required on all endpoints; user can only read/write their own preferences

| Method | Path | Body | Response | Purpose |
|--------|------|------|----------|---------|
| `GET` | `/notification-settings/` | — | `NotificationSettingsPublic` | Catalog merged with user's effective state (one item per catalog type) |
| `PUT` | `/notification-settings/{notification_type}` | `UserNotificationSettingUpdate` (`{ email_enabled: bool }`) | `NotificationSettingItem` | Upsert one preference; returns HTTP 404 for unknown `notification_type` |

---

## Service Layer

### `SystemNotificationService` (`notification_service.py`)

All methods are static.

**`notify(db_session, *, user_id, notification_type, context) -> None`**

The single entry point for all system notifications. Guards are evaluated in order; any failing guard causes a silent return:

1. Catalog lookup — unknown type logs a warning and returns.
2. `settings.emails_enabled` — if `False`, calls `_log_disabled_once()` and returns.
3. User lookup — returns if user is missing, inactive, or has no email address.
4. Preference check — calls `NotificationSettingService.is_email_enabled()`; returns if disabled.
5. Throttle check — calls `_should_send()`; returns if suppressed.
6. Context is sanitized (`_sanitize_context()` truncates `error_text` to 500 chars).
7. Subject and HTML rendered from catalog meta.
8. `create_task_with_error_logging(_async_send(recipient, email_data))` schedules the blocking SMTP send off the event loop.
9. `_mark_sent()` records the dispatch in the throttle state.

The entire body is wrapped in `try/except`; any unhandled exception is logged and swallowed.

**`_async_send(recipient, email_data) -> None`**

Runs `send_email()` in a worker thread via `anyio.to_thread.run_sync()`. SMTP failures are caught and logged.

**`_sanitize_context(context) -> dict`**

Returns a copy of context with `error_text` truncated to 500 characters (`_MAX_ERROR_TEXT_CHARS = 500`).

**`_should_send(notification_type, context, user_id) -> bool`**

Throttle check only (does not mutate state):
- Dedup: if `meta.dedup_scope` is set, looks up `(notification_type.value, str(context[dedup_scope]))` in `_dedup_seen`. Returns `False` if last-sent timestamp is within `DEDUP_TTL_SECONDS`.
- Rate cap: looks up `user_id` in `_user_window`. Returns `False` if the deque length is `>= MAX_PER_WINDOW` after pruning.

**`_mark_sent(notification_type, context, user_id) -> None`**

Records a dispatched notification: updates `_dedup_seen` and appends to `_user_window[user_id]`.

**`_prune_locked(now) -> None`**

Removes expired dedup entries and empties per-user windows. Called under `_throttle_lock` before every check and mark.

**`_log_disabled_once() -> None`**

Logs the emails-disabled WARNING at most once per process lifetime using a module-level `_disabled_warned` flag.

### Throttle constants and state

| Constant | Value | Meaning |
|----------|-------|---------|
| `DEDUP_TTL_SECONDS` | 1800 (30 min) | Suppresses repeat sends for the same (type, dedup-value) |
| `RATE_WINDOW_SECONDS` | 900 (15 min) | Rolling window for the per-user cap |
| `MAX_PER_WINDOW` | 5 | Maximum notifications per user per window |

State is process-local and reset on restart. Thread safety is provided by `_throttle_lock` (a `threading.Lock`).

### `NotificationSettingService` (`notification_setting_service.py`)

All methods are static.

**`is_email_enabled(db_session, user_id, notification_type) -> bool`**

Returns the stored row's `email_enabled` value, or `NOTIFICATION_CATALOG[notification_type].default_email_enabled` when no row exists.

**`list_for_user(db_session, user_id) -> list[NotificationSettingItem]`**

Fetches all rows for the user, then iterates the catalog and merges with stored overrides. Returns one `NotificationSettingItem` per catalog type.

**`set_email_enabled(db_session, user_id, notification_type, enabled) -> NotificationSettingItem`**

Upserts the preference row (`updated_at` is bumped on update) and returns the merged item. The notification_type is validated against `NotificationType` at the route layer before reaching this method.

---

## Session-Error Wiring

### Choke point: `ActivityService._notify_session_error()`

`backend/app/services/events/activity_service.py`

```python
async def _notify_session_error(
    db_session: DBSession,
    chat_session: Session,
    agent_id: UUID | None,
    user_id: UUID,
    error_text: str | None,
) -> None
```

Builds the render context and calls `SystemNotificationService.notify()`. The entire body is wrapped in `try/except` so a notification failure never affects the calling code.

**Context built here:**

| Key | Source |
|-----|--------|
| `project_name` | `settings.PROJECT_NAME` |
| `agent_name` | `Agent.name` (falls back to `"your agent"` if agent missing) |
| `session_title` | `chat_session.title` (falls back to `"Untitled session"`) |
| `session_id` | `str(chat_session.id)` |
| `error_text` | `error_text` parameter (falls back to `"Session encountered an error"`) |
| `link` | `f"{settings.FRONTEND_HOST}/sessions/{chat_session.id}"` |

### Call site 1 — stream error path

`ActivityService.create_error_activity()` (~line 615):
- Called when `STREAM_ERROR` fires while the user was disconnected.
- Creates `error_occurred` activity, emits `ACTIVITY_CREATED` WebSocket event, then calls `_notify_session_error(error_text=error_message)`.

### Call site 2 — agent-declared error path

`ActivityService.handle_session_state_updated()` (~line 1118):
- Called when `SESSION_STATE_UPDATED` fires with `state="error"`.
- Creates `error_occurred` activity, emits `ACTIVITY_CREATED`, then calls `_notify_session_error(error_text=summary)` where `summary` is the agent-provided error description.

---

## Email Template

**Source (MJML):** `backend/app/email-templates/src/session_error.mjml`
**Runtime file (HTML):** `backend/app/email-templates/build/session_error.html`

`render_email_template()` in `backend/app/utils.py` reads from `email-templates/build/` and renders the HTML with Jinja2. The template receives the context dict built by `_notify_session_error()`.

**Jinja2 placeholders used:**

| Placeholder | Content |
|-------------|---------|
| `{{ project_name }}` | Platform name |
| `{{ agent_name }}` | Agent name |
| `{{ session_title }}` | Session title |
| `{{ error_text }}` | Truncated error message (≤500 chars) |
| `{{ link }}` | Deep link to the session page |

---

## Platform Email Sender

System notifications reuse the shared SMTP sender. No separate mail server table is involved.

**`backend/app/core/config.py` — relevant settings:**

| Setting | Default | Purpose |
|---------|---------|---------|
| `SMTP_HOST` | `None` | SMTP server hostname |
| `SMTP_PORT` | `587` | SMTP server port |
| `SMTP_TLS` | `True` | Enable STARTTLS |
| `SMTP_SSL` | `False` | Enable SSL/TLS wrapping |
| `SMTP_USER` | `None` | SMTP authentication username |
| `SMTP_PASSWORD` | `None` | SMTP authentication password |
| `EMAILS_FROM_EMAIL` | `None` | Sender email address |
| `EMAILS_FROM_NAME` | `None` (falls back to `PROJECT_NAME`) | Sender display name |

**`emails_enabled` computed property** (`config.py` line 121):

```python
@property
def emails_enabled(self) -> bool:
    return bool(self.SMTP_HOST and self.EMAILS_FROM_EMAIL)
```

Email sending (password reset, system notifications, new-user emails) is disabled globally when either `SMTP_HOST` or `EMAILS_FROM_EMAIL` is unset.

**`send_email()` / `render_email_template()` — `backend/app/utils.py`:**

- `render_email_template(template_name, context)` — reads `email-templates/build/{template_name}` and renders with Jinja2.
- `send_email(email_to, subject, html_content)` — sends via the `emails` library using the settings above; asserts `emails_enabled` before sending.

---

## Frontend Component

### `NotificationSettings.tsx`

`frontend/src/components/UserSettings/NotificationSettings.tsx`

A shadcn `Card` titled "Notifications". Renders one `Switch` per catalog item returned by `GET /notification-settings/`. The list is data-driven: future notification types appear automatically after client regeneration.

**React Query key:** `["notification-settings"]`
**Query:** `NotificationSettingsService.readNotificationSettings()`
**Mutation:** `NotificationSettingsService.updateNotificationSetting({ notificationType, requestBody: { email_enabled } })`

On mutation success: invalidates `["notification-settings"]` and shows a success toast. On error: invalidates to revert the optimistic change and shows an error toast. The mutating row's `Switch` is disabled while its mutation is in flight (per-row `pending` state using `updateMutation.variables?.notificationType`).

Accessibility: each `Switch` has `id={switchId}` matching its `Label htmlFor`, and `aria-describedby` pointing to the description paragraph.

**Tab registration:** `frontend/src/routes/_layout/settings.tsx` — My profile tab, in a two-column grid alongside `UserInformation` (`<NotificationSettings />` renders a bare `Card`; `UserInformation` also renders a bare `Card` so the two sit at equal half-width).

**Client regeneration note:** after backend changes run `source ./backend/.venv/bin/activate && make gen-client` to land `NotificationSettingsService` and the new types in `frontend/src/client/`.

---

## Models

### `UserNotificationSetting` (table model)

```python
class UserNotificationSetting(SQLModel, table=True):
    __tablename__ = "user_notification_setting"
    id: uuid.UUID
    user_id: uuid.UUID          # FK → user.id, CASCADE
    notification_type: str      # max 64 chars
    email_enabled: bool
    created_at: datetime
    updated_at: datetime
```

### `UserNotificationSettingUpdate` (request body)

```python
class UserNotificationSettingUpdate(SQLModel):
    email_enabled: bool
```

### `NotificationSettingItem` (per-type state)

```python
class NotificationSettingItem(SQLModel):
    notification_type: str
    label: str
    description: str
    email_enabled: bool
```

### `NotificationSettingsPublic` (GET response)

```python
class NotificationSettingsPublic(SQLModel):
    data: list[NotificationSettingItem]
```

---

## Migration

**ID:** `6e43bbbec5cc`
**File:** `backend/app/alembic/versions/6e43bbbec5cc_add_user_notification_setting_table.py`
**down_revision:** `aa44agentapi04`

Upgrade creates `user_notification_setting` with all columns, the unique constraint `uq_user_notification_setting_user_type`, and the index `ix_user_notification_setting_user_id`. Downgrade drops the index then the table. No data backfill — absent rows resolve to catalog defaults.
