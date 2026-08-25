# Mail Servers — Technical Details

## File Locations

### Backend
- `backend/app/models/email/mail_server_config.py` — `MailServerConfig`
  (table, now server-scoped — `user_id` dropped), `MailServerConfigPublic`,
  `MailServerConfigCreate`, `MailServerConfigUpdate`, `MailServerType`,
  `EncryptionType`, `MailServerChannelUsage`, `MailServerDeletionImpact`.
- `backend/app/services/email/mail_server_service.py` — `MailServerService`
  (CRUD + connection testing + the channel-reference deletion guard),
  `MailServerInUseError`.
- `backend/app/api/routes/mail_servers.py` — CRUD + test endpoints,
  **superuser-only** (was per-user before this phase).

### Frontend
- `frontend/src/components/Admin/MailServersCard.tsx` — full CRUD table,
  add/edit dialog, test connection, delete confirmation. Moved from
  `frontend/src/components/UserSettings/MailServerSettings.tsx` (deleted). <!-- nocheck -->
- `frontend/src/routes/_layout/admin/server-configuration.tsx` — "Mail
  Servers" tab, a peer of the "Channels" tab on the same admin page — not a
  Settings tab any more.
- `frontend/src/components/Admin/ServerChannels/MailServerSelect.tsx` — the
  server picker used by the email channel form
  (`ServerChannelForm.tsx`) to select `incoming_server_id` /
  `outgoing_server_id`.

### Migrations
- `029b03776737` — `mail_server_config` table creation (pre-existing).
- `ca0192122e0c` — Phase 4 of the channels & identity unification: drops
  `mail_server_config.user_id` (among other, unrelated drops — see
  [Email Integration tech](email_integration_tech.md#migrations-phase-4)).

## Database Schema

**`mail_server_config`** table:

| Field | Type | Description |
|-------|------|-------------|
| `id` | UUID, PK | |
| ~~`user_id`~~ | — | **Dropped** in `ca0192122e0c`. The table is server-scoped now; existing rows kept their credentials, only ownership changed. |
| `name` | string | Admin-friendly name |
| `server_type` | enum: `imap`, `smtp` | |
| `host` | string | Server hostname |
| `port` | integer | Server port |
| `encryption_type` | enum: `ssl`, `tls`, `starttls`, `none` | |
| `username` | string | Login username |
| `encrypted_password` | text | Encrypted via `encrypt_field()` |
| `created_at`, `updated_at` | datetime | |

No unique constraint ties a server to a single channel — any number of email
channels may reference the same `MailServerConfig` row.

## API Endpoints

**Router**: `backend/app/api/routes/mail_servers.py` — prefix:
`/api/v1/mail-servers`, tags: `mail-servers`. **Every route requires
`get_current_active_superuser`** (`SuperUser` dependency) — there is no
per-row ownership check any more; the dependency is the whole gate.

| Method | Path | Description |
|--------|------|--------------|
| `GET` | `/` | List every configured mail server (filterable by `server_type`) |
| `POST` | `/` | Create a new mail server config |
| `GET` | `/{server_id}` | Get server details (password redacted, `has_password` only) |
| `PUT` | `/{server_id}` | Update server config (re-encrypts password if changed) |
| `DELETE` | `/{server_id}` | Delete server — **409** if any channel still references it |
| `POST` | `/{server_id}/test-connection` | Test IMAP/SMTP connectivity with stored credentials |

The `DELETE` route's 409 body is `MailServerInUseError.impact.model_dump(mode="json")`
— a `MailServerDeletionImpact` carrying `channel_usages: list[MailServerChannelUsage]`.

## Services & Key Methods

`backend/app/services/email/mail_server_service.py` — `MailServerService`:

- `create_mail_server()` — creates the row with `encrypt_field()` for the
  password.
- `list_mail_servers()` — lists every server (server-scoped — no `user_id`
  filter any more), optional `server_type` filter.
- `get_mail_server_with_credentials()` — returns the decrypted password;
  internal use only (called by `EmailChannelAdapter.poll` and
  `EmailSendingService._send_single_email`).
- `update_mail_server()` — updates fields, re-encrypts the password only if
  a new one was supplied.
- `get_deletion_impact(session, server_id) -> MailServerDeletionImpact` —
  **new in this phase.** Scans every `ServerChannel` row and checks both
  `config["incoming_server_id"]` and `config["outgoing_server_id"]` against
  `server_id`, comparing by **parsed UUID** rather than string equality (the
  stored value is free-form JSON that no adapter validates, so it may be
  spelled in any way a UUID legally can be — uppercase hex, braces,
  `urn:uuid:`, hyphenless — all of which would compare unequal as strings
  and let a stale-reference deletion through). Written as an explicit Python
  scan rather than a JSON-operator SQL query, since the channel set is
  admin-sized and a readable loop survives a config-key rename better than a
  hand-written JSON path would.
- `delete_mail_server()` — calls `get_deletion_impact` first; raises
  `MailServerInUseError` (route → 409) if any usage is found, otherwise
  deletes.
- `test_connection()` / `_test_imap()` / `_test_smtp()` — unchanged from
  before this phase.
- `_to_public()` — unchanged; `has_password: bool` only.

## Frontend Components

`frontend/src/components/Admin/MailServersCard.tsx`:
- CRUD table: name, type, host, port, encryption.
- Add/Edit dialog: name, server type dropdown, host, port, encryption
  dropdown, username, password.
- Auto-port logic: updates port on server type or encryption change.
- Test connection button with loading state and success/error toast.
- Delete button with confirmation dialog; a 409 response (server in use)
  surfaces the referencing channels by name.
- React Query key: `["mail-servers"]`.

Mounted on `frontend/src/routes/_layout/admin/server-configuration.tsx`'s
"Mail Servers" tab — a peer of "Channels", not a tab under user Settings any
more.

## Security

- Passwords encrypted at rest via `encrypt_field()`
  (`backend/app/core/security.py`).
- `MailServerConfigPublic` never includes the password — only
  `has_password: bool`.
- Decryption only happens in `get_mail_server_with_credentials()`, when
  establishing an IMAP/SMTP connection.
- Every endpoint is gated on `get_current_active_superuser` — there is no
  broader role that may see or edit mail servers.

## Integration Points

- [Email Integration — Technical Details](email_integration_tech.md) —
  `EmailChannelAdapter.poll`/`send_message` resolve credentials through
  `MailServerService`.
- [Server Channels — Technical Reference](../server_channels/server_channels_tech.md) —
  `EmailChannelAdapter.validate_config_references` checks a channel's
  referenced server ids exist and are the right `server_type` at
  create/update time.
