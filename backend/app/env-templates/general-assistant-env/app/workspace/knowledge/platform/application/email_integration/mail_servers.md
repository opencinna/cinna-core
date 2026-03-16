# Mail Servers

## Purpose

Allow users to configure reusable IMAP and SMTP server connections for email integration. Mail servers are user-level resources (not agent-specific) that can be shared across multiple agent email integrations.

## Core Concepts

- **IMAP Server** — Incoming mail server used to poll and fetch emails for an agent
- **SMTP Server** — Outgoing mail server used to send agent replies
- **Server Type** — Either `imap` or `smtp`
- **Encryption Type** — Connection security: `ssl`, `tls`, `starttls`, or `none`
- **Connection Test** — On-demand validation that the server is reachable and credentials work

## User Stories / Flows

### 1. Add a Mail Server

1. User navigates to **Settings > Mail Servers**
2. Clicks "Add Server" to open the add dialog
3. Fills in: name, server type (IMAP/SMTP), host, port, encryption, username, password
4. Port auto-updates when server type or encryption changes (sensible defaults)
5. Clicks "Save" — server config is stored with encrypted password

### 2. Test Connection

1. User clicks "Test Connection" on an existing server
2. System attempts IMAP or SMTP connection with stored credentials
3. Success or error feedback is shown in the UI

### 3. Edit a Mail Server

1. User clicks "Edit" on an existing server
2. Updates fields (password field shown as "unchanged" unless modified)
3. Saves — password re-encrypted only if changed

### 4. Delete a Mail Server

1. User clicks "Delete" on an existing server
2. System validates the server is not currently used by any agent email integration
3. If in use, deletion is rejected with an error
4. If not in use, server config is removed after confirmation

### 5. Use in Agent Integration

1. When configuring an agent's email integration (Connection modal)
2. User selects from their existing IMAP servers for incoming and SMTP servers for outgoing
3. The selected servers are linked via `incoming_server_id` and `outgoing_server_id` on the integration

## Business Rules

- Mail servers are scoped to the current user — each user manages their own servers
- A server can be used by multiple agent integrations owned by the same user
- Deletion is blocked if the server is referenced by any agent email integration (`incoming_server_id` or `outgoing_server_id`)
- Passwords are encrypted at rest and **never** exposed in API responses — only `has_password: bool` is returned
- Connection testing is available for both IMAP and SMTP server types
- Port defaults update based on server type and encryption selection (e.g., IMAP+SSL → 993)

## Credential Separation

Mail server credentials are stored **separately** from the agent credential system:

| System | Purpose | Storage | Usage |
|--------|---------|---------|-------|
| **Agent Credential System** | Share credentials WITH agents | `credentials` table, synced to agent-env | Agents use in scripts (Odoo API, Gmail, etc.) |
| **Mail Server Credentials** | Backend polls/sends emails | `mail_server_config.encrypted_password` | Backend-only, NEVER shared with agents |

Encryption uses existing `encrypt_field()` / `decrypt_field()` from `backend/app/core/security.py`.

## Architecture Overview

```
User Settings UI → Mail Server API → MailServerService → PostgreSQL (encrypted passwords)
                                                    ↓
                                          Connection Test (IMAP4/SMTP)
                                                    ↓
Agent Email Integration → references mail_server_config → Polling/Sending Services → IMAP/SMTP
```

## Integration Points

- [Email Integration](email_integration.md) — Agent integrations reference mail servers for IMAP polling and SMTP sending
- [Email Sessions](email_sessions.md) — Polling and sending services use mail server credentials at runtime
