# Email Integration — Technical Details

## File Locations

### Backend — Models
- `backend/app/models/email/mail_server_config.py` — `MailServerConfig`, `MailServerConfigPublic`, `MailServerConfigCreate`, `MailServerConfigUpdate`, `MailServerType`, `EncryptionType`
- `backend/app/models/email/agent_email_integration.py` — `AgentEmailIntegration`, `AgentEmailIntegrationPublic`, `EmailAccessMode`, `AgentSessionMode`, `EmailProcessAs`, `EmailCloneShareMode`, `ProcessEmailsResult`
- `backend/app/models/email/email_message.py` — `EmailMessage`, `EmailMessagePublic`
- `backend/app/models/email/outgoing_email_queue.py` — `OutgoingEmailQueue`, `OutgoingEmailQueuePublic`, `OutgoingEmailStatus`
- `backend/app/models/sessions/session.py` — Added: `email_thread_id`, `integration_type`, `sender_email` fields
- `backend/app/models/sharing/agent_share.py` — Added: `source` field (`manual` | `email_integration`)
- `backend/app/models/tasks/input_task.py` — Added: `source_email_message_id`, `source_agent_id`, `SendAnswerRequest`, `SendAnswerResponse`

### Backend — Services
- `backend/app/services/email/` — All email-related services (see below)
- `backend/app/services/email/__init__.py` — Re-exports main service classes
- `backend/app/services/email/mail_server_service.py` — `MailServerService` (CRUD + connection testing)
- `backend/app/services/email/integration_service.py` — `EmailIntegrationService` (CRUD + orchestration)
- `backend/app/services/email/routing_service.py` — `EmailRoutingService` (sender → target mapping)
- `backend/app/services/email/polling_service.py` — `EmailPollingService` (IMAP fetch + parse)
- `backend/app/services/email/processing_service.py` — `EmailProcessingService` (route + inject messages / create tasks)
- `backend/app/services/email/sending_service.py` — `EmailSendingService` (queue + SMTP send)
- `backend/app/services/email/imap_connector.py` — `IMAPConnector` (testable IMAP wrapper)
- `backend/app/services/email/smtp_connector.py` — `SMTPConnector` (testable SMTP wrapper)
- `backend/app/services/email/polling_scheduler.py` — APScheduler job (5 min interval)
- `backend/app/services/email/sending_scheduler.py` — APScheduler job (2 min interval)

### Backend — Updated Services
- `backend/app/services/sharing/agent_share_service.py` — `create_auto_share()` for email sender clones
- `backend/app/services/users/user_service.py` — `create_email_user()` for auto user provisioning
- `backend/app/services/sessions/session_service.py` — `get_session_by_email_thread()`, email params on `create_session()`
- `backend/app/services/sessions/message_service.py` — HMAC-signed `session_context` emission (includes `email_subject` from linked `EmailMessage`)
- `backend/app/services/sessions/session_context_signer.py` — HMAC-SHA256 signing/verification for session context
- `backend/app/services/tasks/input_task_service.py` — `send_email_answer()` for task-originated replies
- `backend/app/services/ai_functions/ai_functions_service.py` — `generate_email_reply()` wrapper

### Backend — AI Functions
- `backend/app/agents/email_reply_generator.py` — `generate_email_reply()` AI function
- `backend/app/agents/prompts/email_reply_generator_prompt.md` — Prompt template for reply generation

### Backend — Routes
- `backend/app/api/routes/mail_servers.py` — Mail server CRUD + test endpoints
- `backend/app/api/routes/email_integration.py` — Agent email integration config endpoints
- `backend/app/api/routes/input_tasks.py` — `POST /{id}/send-answer` endpoint

### Backend — Agent Environment
- `backend/app/env-templates/app_core_base/core/server/active_session_manager.py` — Per-session HMAC-verified context store
- `backend/app/env-templates/app_core_base/core/server/routes.py` — `GET /session/context?session_id=X`, `_store_session_context()` helper
- `backend/app/env-templates/app_core_base/core/server/prompt_generator.py` — `build_session_context_section()` for system prompt injection
- `backend/app/env-templates/app_core_base/core/scripts/get_session_context.py` — Stdlib-only CLI/import helper for agent scripts

### Frontend — Components
- `frontend/src/components/UserSettings/MailServerSettings.tsx` — Mail server CRUD settings panel
- `frontend/src/components/Agents/EmailIntegrationCard.tsx` — Main integration card (toggle, actions)
- `frontend/src/components/Agents/EmailAccessModal.tsx` — Access mode, patterns, domain allowlist
- `frontend/src/components/Agents/EmailConnectionModal.tsx` — IMAP/SMTP server selection
- `frontend/src/components/Agents/EmailSessionsModal.tsx` — Session mode, max clones, share mode, processing mode
- `frontend/src/components/Agents/ShareManagement/ShareList.tsx` — Email share badges
- `frontend/src/components/Agents/ShareManagement/ClonesList.tsx` — Email clone badges

### Frontend — Routes
- `frontend/src/routes/_layout/settings.tsx` — "Mail Servers" tab
- `frontend/src/routes/_layout/tasks/index.tsx` — "Send Answer" button for email-originated tasks
- `frontend/src/routes/_layout/task/$taskId.tsx` — "Send Answer" button in task detail
- `frontend/src/routes/_layout/session/$sessionId.tsx` — Email/A2A integration type badges

### Tests
- `backend/tests/api/agents/agents_email_integration_test.py` — End-to-end: email → session → response → outgoing email
- `backend/tests/api/agents/agents_email_task_integration_test.py` — Task mode: email → task → execute → send answer
- `backend/tests/stubs/email_stubs.py` — `StubIMAPConnector`, `StubSMTPConnector`

### Migrations
- `029b03776737` — `mail_server_config` table
- `cc1e27a71798` — `agent_email_integration` table
- `485e7e243dd5` — `email_message` table
- `8a95916ab539` — `outgoing_email_queue` table
- `7aeed6ea3abf` — `agent_share.source` + `session.email_thread_id` + `session.integration_type`
- `f3a1b2c4d5e6` — `agent_email_integration.agent_session_mode` + `session.sender_email`
- `h5c3d4e6f7g8` — `agent_email_integration.process_as` + `input_task` email source fields + `email_message.input_task_id` + `outgoing_email_queue.input_task_id`

## Database Schema

### New Tables

**`mail_server_config`** — User's IMAP/SMTP server configurations

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

**`agent_email_integration`** — Per-agent email integration settings (one-to-one with agent)

| Field | Type | Description |
|-------|------|-------------|
| `id` | UUID, PK | |
| `agent_id` | UUID, FK → `agent.id` CASCADE, unique | Parent agent only |
| `enabled` | boolean, default False | Integration active |
| `access_mode` | enum: `open`, `restricted` | Who can send emails |
| `auto_approve_email_pattern` | string, nullable | Glob patterns for restricted mode |
| `allowed_domains` | string, nullable | Comma-separated domain allowlist |
| `max_clones` | integer, default 50, range 1-1000 | Max email-initiated clones |
| `clone_share_mode` | enum: `user`, `builder`, default `user` | Share mode for auto-created clones |
| `agent_session_mode` | enum: `clone`, `owner`, default `clone` | Where sessions are created |
| `process_as` | enum: `new_session`, `new_task`, default `new_session` | How incoming emails are processed |
| `incoming_server_id` | UUID, FK → `mail_server_config.id` SET NULL | IMAP server |
| `incoming_mailbox` | string | Email address to monitor |
| `outgoing_server_id` | UUID, FK → `mail_server_config.id` SET NULL | SMTP server |
| `outgoing_from_address` | string | Sender address for replies |
| `created_at`, `updated_at` | datetime | |

**`email_message`** — Parsed incoming emails

| Field | Type | Description |
|-------|------|-------------|
| `id` | UUID, PK | |
| `agent_id` | UUID, FK → `agent.id` CASCADE | Parent agent |
| `clone_agent_id` | UUID, FK → `agent.id` SET NULL | Routed target clone |
| `session_id` | UUID, FK → `session.id` SET NULL | Created session |
| `input_task_id` | UUID, FK → `input_task.id` SET NULL | Created task (task mode) |
| `email_message_id` | string | RFC Message-ID header |
| `sender` | string | Sender email address |
| `subject` | string | |
| `body` | text | Email body content |
| `references` | text, nullable | References header (threading) |
| `in_reply_to` | string, nullable | In-Reply-To header |
| `received_at` | datetime | |
| `processed` | boolean, default False | |
| `processing_error` | text, nullable | Error message if failed |
| `pending_clone_creation` | boolean, default False | Waiting for clone readiness |
| `attachments_metadata` | JSON, nullable | `[{filename, content_type, size}]` |
| `created_at`, `updated_at` | datetime | |

**`outgoing_email_queue`** — Queued agent reply emails

| Field | Type | Description |
|-------|------|-------------|
| `id` | UUID, PK | |
| `agent_id` | UUID, FK → `agent.id` CASCADE | Parent agent (owns SMTP) |
| `clone_agent_id` | UUID, FK → `agent.id` CASCADE, nullable | Clone that generated response |
| `session_id` | UUID, FK → `session.id` CASCADE, nullable | Session (null for task-originated replies) |
| `message_id` | UUID, FK → `message.id` CASCADE, nullable | Agent's reply message (null for task-originated) |
| `input_task_id` | UUID, FK → `input_task.id` SET NULL, nullable | Source task (for task-originated replies) |
| `recipient` | string | Recipient email address |
| `subject` | string | |
| `body` | text | Formatted email body |
| `references` | text, nullable | References header |
| `in_reply_to` | string, nullable | In-Reply-To header |
| `status` | enum: `pending`, `sent`, `failed` | |
| `retry_count` | integer, default 0 | Max 3 retries |
| `last_error` | text, nullable | |
| `created_at`, `updated_at`, `sent_at` | datetime | |

### Updated Tables

**`agent_share`** — Added: `source` (string, default `"manual"`): `"manual"` | `"email_integration"`

**`session`** — Added: `email_thread_id` (string, nullable), `integration_type` (string, nullable: `"email"` | `"a2a"`), `sender_email` (string, nullable)

**`input_task`** — Added: `source_email_message_id` (UUID, FK → `email_message.id` SET NULL), `source_agent_id` (UUID, FK → `agent.id` SET NULL)

## API Endpoints

### Mail Server Configuration

**Router**: `backend/app/api/routes/mail_servers.py` — prefix: `/api/v1/mail-servers`, tags: `mail-servers`

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | List user's mail servers (filterable by `server_type`) |
| `POST` | `/` | Create new mail server config |
| `GET` | `/{server_id}` | Get server details (password redacted) |
| `PUT` | `/{server_id}` | Update server config |
| `DELETE` | `/{server_id}` | Delete server (validates not in use) |
| `POST` | `/{server_id}/test-connection` | Test IMAP/SMTP connectivity |

### Agent Email Integration

**Router**: `backend/app/api/routes/email_integration.py` — prefix: `/api/v1/agents`, tags: `email-integration`

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/{agent_id}/email-integration` | Get integration config (null if none) |
| `POST` | `/{agent_id}/email-integration` | Create or update integration (upsert) |
| `PUT` | `/{agent_id}/email-integration/enable` | Enable integration (validates required fields) |
| `PUT` | `/{agent_id}/email-integration/disable` | Disable integration |
| `DELETE` | `/{agent_id}/email-integration` | Remove integration |
| `POST` | `/{agent_id}/email-integration/process-emails` | Manual trigger: poll IMAP + process + retry pending |

### Input Tasks (Email-Originated)

**Router**: `backend/app/api/routes/input_tasks.py` — prefix: `/api/v1/tasks`, tags: `tasks`

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/{id}/send-answer` | Generate AI reply and queue email for an email-originated task |

## Service Architecture

All email-related services live in `backend/app/services/email/`:

- `mail_server_service.py:MailServerService` — CRUD with password encryption, connection testing (IMAP/SMTP)
- `integration_service.py:EmailIntegrationService` — CRUD for agent integrations, enable/disable validation, `process_emails()` manual trigger, clone count tracking
- `routing_service.py:EmailRoutingService` — `route_email()` → (target_agent_id, is_ready, session_mode), access control, auto-user creation, auto-share + auto-clone
- `polling_service.py:EmailPollingService` — IMAP connection, fetch unread, parse (subject, body, headers, attachments metadata), deduplication by Message-ID, recipient validation
- `processing_service.py:EmailProcessingService` — Branches on `integration.process_as`: new_session (route + session + message injection) or new_task (create InputTask), handles pending clone retries
- `sending_service.py:EmailSendingService` — `queue_outgoing_email()`, `send_pending_emails()`, `handle_stream_completed()` (event handler on STREAM_COMPLETED), MIME building with threading headers, retry logic (max 3)
- `imap_connector.py:IMAPConnector` — Testable IMAP wrapper with injectable instance
- `smtp_connector.py:SMTPConnector` — Testable SMTP wrapper with injectable instance
- `polling_scheduler.py` — APScheduler job: every 5 min polls all enabled agents
- `sending_scheduler.py` — APScheduler job: every 2 min flushes SMTP queue

### Scheduler Registration

Both schedulers registered in `backend/app/main.py` during app lifespan:
- `start_email_polling_scheduler()` — every 5 min: poll IMAP
- `start_email_sending_scheduler()` — every 2 min: flush SMTP queue
- `STREAM_COMPLETED` event → `EmailSendingService.handle_stream_completed()`

### Integration with Existing Services

| Existing Service | New Integration |
|------------------|-----------------|
| `AgentShareService` | `create_auto_share()` — creates pre-accepted share + clone in one step |
| `UserService` | `create_email_user()` — creates user from email, bypasses domain whitelist |
| `SessionService` | `create_session()` accepts `email_thread_id`, `integration_type`, `sender_email` |
| `SessionService` | `get_session_by_email_thread()` — finds session by thread ID |
| `MessageService` | Emits HMAC-signed `session_context` (including `email_subject` from linked `EmailMessage`) |
| `InputTaskService` | `send_email_answer()` — generates AI reply and queues outgoing email |
| `AIFunctionsService` | `generate_email_reply()` — AI-powered email reply from session results |
| Event Bus | `STREAM_COMPLETED` event triggers `EmailSendingService.handle_stream_completed()` |

## Frontend Components

### Settings: Mail Server Management
- `frontend/src/components/UserSettings/MailServerSettings.tsx` — Full CRUD table, add/edit dialog, test connection button, delete confirmation
- Tab: "Mail Servers" in User Settings (between AI Credentials and SSH Keys)
- React Query key: `["mail-servers"]`

### Agent: Email Integration Card
- `frontend/src/components/Agents/EmailIntegrationCard.tsx` — Enable/disable toggle, clone count, config completeness indicators, action buttons for sub-modals
- `frontend/src/components/Agents/EmailAccessModal.tsx` — Access mode, auto-approve patterns, domain allowlist
- `frontend/src/components/Agents/EmailConnectionModal.tsx` — IMAP/SMTP server dropdown selection, mailbox/from addresses
- `frontend/src/components/Agents/EmailSessionsModal.tsx` — Session mode, clone share mode, processing mode, max clones
- Only shown for non-clone agents on the Integrations tab

### Task Actions
- `frontend/src/routes/_layout/tasks/index.tsx` — "Send Answer" button (Mail icon) for email-originated tasks when status is `completed` or `error`
- `frontend/src/routes/_layout/task/$taskId.tsx` — "Send Answer" button in task detail footer

### Share & Session Badges
- `frontend/src/components/Agents/ShareManagement/ShareList.tsx` — Indigo "Email" badge for email-originated shares
- `frontend/src/components/Agents/ShareManagement/ClonesList.tsx` — Indigo "Email" badge for email-initiated clones
- `frontend/src/routes/_layout/session/$sessionId.tsx` — Integration type badge (Email/A2A) in session header

## Configuration

- `AGENT_AUTH_TOKEN` — Used for HMAC-signing session context sent to agent environments
- Mail server encryption key: reuses existing `encrypt_field()` / `decrypt_field()` from `backend/app/core/security.py`
- Polling interval: 5 minutes (configured in `polling_scheduler.py`)
- Sending interval: 2 minutes (configured in `sending_scheduler.py`)
- Max retries for outgoing emails: 3
- Default max clones per agent: 50 (range 1-1000)

## Security

- Mail server passwords encrypted at rest via `encrypt_field()`, decrypted only at connection time
- `MailServerConfigPublic` schema exposes `has_password: bool` instead of actual password
- Agent ownership validated on all integration API operations
- Clone agents cannot have email integrations (parent-only)
- Session context uses three-layer security: system prompt injection, HMAC-verified per-session store, and stdlib-only helper script. See [Email Sessions](email_sessions.md) for details
