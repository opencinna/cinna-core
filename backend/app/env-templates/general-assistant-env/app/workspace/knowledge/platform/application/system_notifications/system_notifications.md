# System Notifications

## Purpose

System notifications are platform-initiated messages sent to users when significant events occur in their agent ecosystem. The feature introduces a **generic notification layer** designed to carry any number of future notification types without requiring re-architecture: a typed catalog drives all dispatch logic, per-user preferences live in a dedicated table, and a built-in throttle prevents inbox storms.

The first notification type is `session_error`: an email to the agent owner when one of their sessions ends with an error.

## Core Concepts

- **Notification Catalog** — The code-only registry of every notification type the platform supports. Each entry defines user-facing copy, the default opt-in state, the email template, the subject builder, and the dedup key. No database table; adding a new type requires no schema migration.
- **System Notification** — A platform-generated alert about a backend event, distinct from agent-authored messages or activity feed entries. Currently email-only.
- **Notification Preference** — A per-user, per-type record that overrides the catalog default. A missing preference row means "use the catalog default."
- **Throttle** — An in-memory gate preventing duplicate or burst notifications: per-(type, session) dedup within a 30-minute window, plus a per-user cap of 5 emails per 15-minute window.
- **Platform Email Sender** — The shared SMTP configuration (`SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `EMAILS_FROM_EMAIL`, `EMAILS_FROM_NAME`). The same sender handles password-reset emails, new-user invitations, and all system notifications. When `SMTP_HOST` or `EMAILS_FROM_EMAIL` is not set, `emails_enabled` resolves to `False` and all notification dispatch is skipped.

## Notification Types

### `session_error` — Session error email

**Label:** "Session errors"
**Description:** "Email me when one of my agent sessions ends with an error."
**Default:** enabled for all users (no preference row needed to receive it)
**Recipient:** always the session owner (`Session.user_id`) — never the email-integration address or any other party

**When it fires**

A `session_error` notification is dispatched from two error paths, both routed through a single choke point (`ActivityService._notify_session_error()`):

1. **Stream error path** (`STREAM_ERROR` event) — when a background stream fails while the user is disconnected. `ActivityService.create_error_activity()` creates the `error_occurred` activity and then calls `_notify_session_error()`.
2. **Agent-declared error path** (`SESSION_STATE_UPDATED` event with `state="error"`) — when the agent uses the `update_session_state` tool to declare an error outcome with a summary. `ActivityService.handle_session_state_updated()` creates the `error_occurred` activity and then calls `_notify_session_error()`.

In both cases the `error_occurred` activity is always created first; the notification is a side-effect that is failure-isolated from the activity pipeline.

**Email content**

- Subject: `{PROJECT_NAME} — Session error on agent {agent_name}`
- Body: agent name, session title, truncated error text (at most 500 characters), a "View session" button linking to `{FRONTEND_HOST}/sessions/{session_id}`, and a footer note: "Turn these off in Settings → Notifications."
- No stack traces, credentials, or full prompt text are ever included.

## User Flows

### 1. Receive a session-error email

1. User's agent session ends with an error (stream failure or agent-declared).
2. Platform creates an `error_occurred` activity (visible in the sidebar bell and Activities page).
3. If platform email is configured and the user has not disabled session-error notifications, the platform sends an email to the session owner.
4. The throttle suppresses additional emails for the same session within 30 minutes and caps the total to 5 emails per 15 minutes per user across all sessions.
5. User clicks "View session" in the email and is taken directly to the session page.

### 2. Opt out of session-error emails

1. User opens **Settings > My profile** tab.
2. The "Notifications" card (beside the User Information card) shows one toggle per catalog type.
3. User turns off "Session errors".
4. Platform saves the preference; future session-error emails for this user are suppressed.

### 3. Re-enable notifications

1. Same path as above — user turns the toggle back on.
2. Preference row is updated; the catalog default no longer applies.

## Business Rules

- **Default on:** the `session_error` type has `default_email_enabled=True`. Users who have never changed this setting receive session-error emails without taking any action.
- **Preference resolution:** the effective setting is the stored preference row when it exists, otherwise the catalog default. A user who has never touched the setting is treated identically to one who has explicitly enabled it.
- **Catalog-driven list:** the Settings UI renders one toggle per catalog entry. New notification types appear in the UI automatically after a client regeneration — no frontend code change required.
- **Owner-only recipient:** the notification always goes to `Session.user_id`. It is never sent to the email-integration sender address, a workspace admin, or any third party.
- **Failure isolation:** a notification dispatch failure (SMTP error, template render error, or any other exception) is caught, logged, and swallowed. It never propagates into the activity creation or event pipeline.
- **Dedup prevents double-send:** both error paths share the same `_notify_session_error()` helper. If both somehow fire for the same session (edge case), the per-(type, session_id) dedup TTL ensures only one email is sent.
- **Throttle is best-effort:** the throttle state is in-process memory. On a process restart the state resets, which may result in at most one extra email shortly after a deploy. This is intentional; a distributed throttle (Redis) is not justified for the MVP.
- **emails_enabled guard:** when the platform is configured without SMTP (`SMTP_HOST` or `EMAILS_FROM_EMAIL` not set), all dispatch is silently skipped. A one-time WARNING is logged to avoid log spam.
- **Inactive or missing user:** if the session owner's account is inactive or has been deleted, dispatch is skipped silently.
- **Unknown notification type on PUT:** a PUT to a type not in the catalog returns HTTP 404.

## Integration Points

- [Agent Activities](../agent_activities/agent_activities.md) — creating an `error_occurred` activity is the trigger point. The notification dispatch is a side-effect of that creation; the activity itself is the source of truth and is unaffected by notification failures.
- [Agent Sessions](../agent_sessions/agent_sessions.md) — the session row provides the owner (`user_id`), session title, and the `session_id` used for dedup and the deep link in the email.
- [Auth / Users](../auth/auth.md) — preferences live in a new table FK-referenced to `user`; no new column on the `user` table. The recipient address is `User.email`.
- [Realtime Events](../realtime_events/event_bus_system.md) — the `STREAM_ERROR` and `SESSION_STATE_UPDATED` events on the event bus are what trigger the two error paths in `ActivityService`.
- [Email Integration / Mail Servers](../email_integration/mail_servers.md) — **distinct** from per-user IMAP/SMTP mail servers used for agent email automation. System notifications use the platform-level SMTP sender configured via `SMTP_*` environment variables, not the `mail_server_config` table.

## Settings UI Location

Settings → My profile tab → "Notifications" card (in a two-column grid beside the User Information card). The card renders one Switch per catalog type using data from `GET /notification-settings/`. Future notification types appear automatically without frontend changes.

## Security

- Preferences are read and written only for `CurrentUser` — no admin surface and no cross-user access.
- Email bodies contain only the agent name, session title, and a truncated (≤500 characters) error message. No stack traces, credentials, prompt content, or internal identifiers are included.
- The recipient is always the session owner; the platform never leaks one user's session data to another.
- SMTP credentials are env-var only and never logged.
