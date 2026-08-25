# Mail Servers

## Purpose

Since Phase 4 of the channels & identity unification refactor, mail servers
are **admin-owned, server-scoped infrastructure**, not a per-user resource.
A superuser configures reusable IMAP and SMTP server connections under
**Admin → Server Configuration → Mail Servers**; an email
[Server Channel](../server_channels/server_channels.md) references one of
each by id, the same way a Google Chat channel references its own service
account. There is no more per-user ownership and no more per-agent
"Connection" modal — a mail server is configured once and any number of
email channels may point at it.

## Core Concepts

- **IMAP Server** — incoming mail server an email channel polls.
- **SMTP Server** — outgoing mail server an email channel sends replies
  through.
- **Server Type** — either `imap` or `smtp`.
- **Encryption Type** — connection security: `ssl`, `tls`, `starttls`, or
  `none`.
- **Connection Test** — on-demand validation that the server is reachable
  and the stored credentials work.
- **Channel reference** — an email channel's `config` names an
  `incoming_server_id` (must be `imap`) and an `outgoing_server_id` (must be
  `smtp`); a wrong id or type is rejected at channel create/update time.

## User Stories / Flows

### 1. Add a mail server

1. Superuser opens **Admin → Server Configuration → Mail Servers**.
2. Adds a server: name, server type (IMAP/SMTP), host, port, encryption,
   username, password. Port auto-updates on server type/encryption change
   (sensible defaults).
3. Saves — the server config is stored with the password encrypted at rest.

### 2. Test connection

1. Superuser clicks "Test Connection" on an existing server.
2. The platform attempts an IMAP or SMTP connection with the stored
   credentials and reports success or a specific failure.

### 3. Edit a mail server

1. Superuser edits an existing server; the password field shows
   "unchanged" unless a new one is typed.
2. Saves — the password is re-encrypted only if it was changed.

### 4. Delete a mail server

1. Superuser attempts to delete a server.
2. If any email channel's `config` still references it (as either the
   incoming or the outgoing server), the deletion is **rejected** (HTTP 409)
   and the response names every referencing channel and which role it plays
   — see [The deletion guard](#the-deletion-guard-new-in-this-phase).
3. Once no channel references it, the deletion succeeds.

### 5. Reference from an email channel

1. When an admin creates or edits an email channel (Admin → Server
   Configuration → Channels), the channel form's server pickers list the
   configured IMAP and SMTP servers.
2. The selected servers are stored as `incoming_server_id` /
   `outgoing_server_id` in the channel's non-secret `config`.

## Business Rules

- **Mail servers are server-scoped, not user-scoped.** There is a single
  fleet of IMAP/SMTP servers for the whole platform, administered by
  superusers only. Existing rows from before this phase kept their stored
  credentials — only ownership changed (their `user_id` column was dropped).
- A server may be referenced by any number of email channels.
- **The deletion guard changed in this phase.** Deletion used to be blocked
  by a reference from `agent_email_integration`; it is now blocked by a
  reference from any `ServerChannel.config` (`incoming_server_id` or
  `outgoing_server_id`). See
  [The deletion guard](#the-deletion-guard-new-in-this-phase).
- Passwords are encrypted at rest and are **never** exposed in API
  responses — only `has_password: bool` is returned.
- Connection testing is available for both IMAP and SMTP server types.
- Port defaults update based on server type and encryption selection (e.g.
  IMAP + SSL → 993).

## The deletion guard (new in this phase)

A `ServerChannel` references a mail server by a plain UUID inside its JSON
`config` column — there is no foreign key behind it. An unguarded delete
would therefore succeed and silently leave a channel pointing at nothing;
inbound mail would simply stop arriving with no error anywhere. Deletion is
blocked (HTTP 409) whenever any channel still references the server, and the
response body lists every referencing channel (its id, name, and whether the
role is `incoming` or `outgoing`) so the admin can detach or edit those
channels first. There is no `force` override, unlike the credential-deletion
guard elsewhere on the platform: nothing legitimate is served by breaking a
live channel, and detaching the server from the channel first is a one-click
admin action.

## Credential Separation

Mail server credentials remain stored **separately** from the agent
credential system, exactly as before this phase:

| System | Purpose | Storage | Usage |
|--------|---------|---------|-------|
| **Agent Credential System** | Share credentials WITH agents | `credentials` table, synced to agent-env | Agents use in scripts (Odoo API, Gmail, etc.) |
| **Mail Server Credentials** | Backend polls/sends emails | `mail_server_config.encrypted_password` | Backend-only, NEVER shared with agents |

Encryption uses the existing `encrypt_field()` / `decrypt_field()` from
`backend/app/core/security.py`.

## Architecture Overview

```
Admin Settings UI → Mail Server API (superuser-only) → MailServerService → PostgreSQL (encrypted passwords)
                                                                ↓
                                                      Connection Test (IMAP4/SMTP)
                                                                ↓
Email Server Channel (config.incoming_server_id / outgoing_server_id)
    → EmailChannelAdapter.poll() / send_message() → Polling/Sending Services → IMAP/SMTP
```

## Integration Points

- [Email Integration](email_integration.md) — the email channel transport
  that references mail servers for IMAP polling and SMTP sending.
- [Server Channels](../server_channels/server_channels.md) — mail servers
  are administered as a peer of the Channels tab, the same relationship a
  Google Chat channel has with its own service account.
- [Email Sessions](email_sessions.md) — the polling and outgoing-queue
  services that use mail server credentials at runtime.
