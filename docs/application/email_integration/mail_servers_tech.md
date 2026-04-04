# Mail Servers — Technical Details

## File Locations

### Backend
- `backend/app/models/email/mail_server_config.py` — `MailServerConfig` (table), `MailServerConfigPublic`, `MailServerConfigCreate`, `MailServerConfigUpdate`, `MailServerType`, `EncryptionType`
- `backend/app/services/email/mail_server_service.py` — `MailServerService` (CRUD + connection testing)
- `backend/app/api/routes/mail_servers.py` — Mail server CRUD + test endpoints

### Frontend
- `frontend/src/components/UserSettings/MailServerSettings.tsx` — Full CRUD table, add/edit dialog, test connection, delete confirmation
- `frontend/src/routes/_layout/settings.tsx` — "Mail Servers" tab registration

### Migrations
- `029b03776737` — `mail_server_config` table creation

## Database Schema

**`mail_server_config`** table:

| Field | Type | Description |
|-------|------|-------------|
| `id` | UUID, PK | |
| `user_id` | UUID, FK → `user.id` CASCADE | Owner |
| `name` | string | User-friendly name |
| `server_type` | enum: `imap`, `smtp` | |
| `host` | string | Server hostname |
| `port` | integer | Server port |
| `encryption_type` | enum: `ssl`, `tls`, `starttls`, `none` | |
| `username` | string | Login username |
| `encrypted_password` | text | Encrypted via `encrypt_field()` |
| `created_at`, `updated_at` | datetime | |

## API Endpoints

**Router**: `backend/app/api/routes/mail_servers.py` — prefix: `/api/v1/mail-servers`, tags: `mail-servers`

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | List user's mail servers (filterable by `server_type` query param) |
| `POST` | `/` | Create new mail server config |
| `GET` | `/{server_id}` | Get server details (password redacted, returns `has_password`) |
| `PUT` | `/{server_id}` | Update server config (re-encrypts password if changed) |
| `DELETE` | `/{server_id}` | Delete server (validates not in use by any integration) |
| `POST` | `/{server_id}/test-connection` | Test IMAP/SMTP connectivity with stored credentials |

## Services & Key Methods

`backend/app/services/email/mail_server_service.py` — `MailServerService`:

- `create_mail_server()` — Creates config with `encrypt_field()` for password
- `get_user_mail_servers()` — Lists servers by user, optional `server_type` filter
- `get_mail_server_with_credentials()` — Returns decrypted password (internal use only)
- `update_mail_server()` — Updates config, re-encrypts password if changed
- `delete_mail_server()` — Validates server is not referenced by any `agent_email_integration`, then deletes
- `test_connection()` — Dispatches to `_test_imap()` or `_test_smtp()` based on server type
- `_test_imap()` — Validates IMAP4 or IMAP4_SSL connection and login
- `_test_smtp()` — Validates SMTP or SMTP_SSL connection with optional STARTTLS

## Frontend Components

`frontend/src/components/UserSettings/MailServerSettings.tsx`:
- CRUD table with columns: name, type, host, port, encryption
- Add/Edit dialog: name, server type dropdown, host, port, encryption dropdown, username, password
- Auto-port logic: updates port on server type or encryption change
- Test connection button with loading spinner and success/error toast
- Delete button with confirmation dialog (blocked if server in use)
- React Query key: `["mail-servers"]`

## Security

- Passwords encrypted at rest via `encrypt_field()` using `backend/app/core/security.py`
- `MailServerConfigPublic` schema never includes password — exposes `has_password: bool` instead
- Decryption only happens in `get_mail_server_with_credentials()` when establishing IMAP/SMTP connections
- All endpoints scoped to authenticated user (`CurrentUser` dependency)
