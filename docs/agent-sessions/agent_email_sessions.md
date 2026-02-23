# Agent Email Sessions - Integration Architecture

## Overview

Agents can receive emails at configured IMAP mailboxes, process them as session messages, and send replies back via SMTP. The feature uses the **shared agents (clone) mechanism** for complete session and workspace isolation between different email senders. Each email sender gets their own cloned agent with independent sessions, environment, and data. An alternative **owner mode** allows sessions to run directly on the original agent for personal automation use cases.

A **"Process as New Task"** mode allows incoming emails to create **InputTasks** instead of auto-responding sessions. The agent owner can review, refine, choose the right agent, and execute on their own terms. After execution, a **"Send Answer"** button generates an AI-crafted email reply from the session results and sends it back to the original sender via the existing SMTP infrastructure.

## Core Concepts

### Security Model: Clone-Based Isolation

**Problem**: If multiple email senders share a single agent environment, their files, state, and session data can leak between sessions.

**Solution**: Leverage the existing **shared agents** mechanism. Each email sender gets their own clone of the agent, providing:
- **Workspace isolation**: Each sender's files exist in a separate Docker environment
- **Session isolation**: Sessions belong to the sender's user account, not the agent owner
- **State isolation**: Agent scripts running in one clone cannot access another clone's data
- **Identity separation**: Each sender maps to a real user account in the system

### Architecture Pattern

Following the **A2A integration pattern** + **Shared Agents pattern**:
- Owner configures email integration (IMAP/SMTP) on their agent
- Email polling happens on the parent agent (owner's config)
- Incoming emails are routed to sender-specific clones (or parent agent in owner mode)
- Sessions execute in the target's isolated environment
- Responses queue back through the parent's SMTP configuration

### Email Integration Flow

```
External User -> Email -> IMAP Server -> Backend Polling (parent agent)
                                              |
                                      Sender Identification
                                              |
                                     process_as setting?
                                       /            \
                                   new_task       new_session
                                      |               |
                                Create InputTask     EmailRoutingService
                                for agent owner       (clone/owner mode)
                                      |               |
                              Owner reviews,     Route to target agent
                              refines task            |
                                      |          Message -> Session
                              Execute task            |
                                      |          Agent Response
                              Session created         |
                                      |          Email Queue
                              Agent responds          |
                                      |          SMTP send
                                "Send Answer"         |
                                  button         External User
                                      |
                              AI generates reply
                                      |
                                  SMTP send
                                      |
                                External User
```

### Agent Session Modes

**Clone Mode** (`agent_session_mode = 'clone'`, default):
- Each email sender gets a dedicated clone with an isolated Docker environment
- Clone created via standard `AgentCloneService.create_clone()` flow
- Sessions belong to the sender's user account
- Ideal for multi-user bots, customer support, public-facing agents

**Owner Mode** (`agent_session_mode = 'owner'`):
- Sessions are created directly on the original agent in the owner's user space
- No clone creation — all emails processed in the same environment
- `sender_email` is stored on the session to route replies back to the original sender
- Ideal for personal automation where the agent owner wants their own agent to auto-reply

Access control (open/restricted) and domain allowlists apply equally in both modes. Clone-specific settings (`max_clones`, `clone_share_mode`) are ignored in owner mode.

### Email Processing Mode (`process_as`)

**New Session** (`process_as = 'new_session'`, default):
- Incoming emails are processed immediately into agent sessions (original behavior)
- Agent auto-responds — the full routing/clone/session flow executes automatically
- Ideal for automated workflows, customer support bots, always-on agents

**New Task** (`process_as = 'new_task'`):
- Incoming emails create **InputTasks** assigned to the agent owner
- No automatic session or response — the owner reviews the task first
- The task is pre-populated with the email content and the email agent is pre-selected
- Owner can refine the task description, change the assigned agent, then execute manually
- After execution, the **"Send Answer"** button generates an AI-crafted reply from session results
- The reply is queued and sent via the original agent's SMTP config (preserved in `source_agent_id`)
- Ideal for review-before-respond workflows, complex requests, or when human oversight is needed

Key design decisions:
- `source_agent_id` on `InputTask` always points to the original email agent (for SMTP config), even if the user reassigns the task to a different agent for execution
- `source_email_message_id` being non-null identifies email-originated tasks (no separate `source` field needed)
- Each email creates a separate task — no threading/grouping at the task level
- `process_as` only affects future emails; already-processed emails retain their state

### Access Control Modes

**Open Mode** (`access_mode = 'open'`):
- Any email sender automatically gets access (clone or session)
- Protected by `max_clones` limit and optional per-integration `allowed_domains`
- Use case: Public-facing support agents, general-purpose assistants

**Restricted Mode** (`access_mode = 'restricted'`):
- Only senders matching one of these criteria get access:
  1. Owner has pre-shared the agent with them via the Share Management UI
  2. Sender's email matches `auto_approve_email_pattern` (glob-style, e.g., `*@example.com,tech-*@another.com`)
- Emails from non-matching senders are silently ignored
- Use case: Internal team agents, controlled-access scenarios

### Auto-User Creation

When an email arrives from an unknown sender (no existing user account) and access is allowed:
1. System creates a full user account with the sender's email
2. A random password is generated (user doesn't receive it)
3. User can later claim the account via password reset or OAuth login
4. Once logged in, they see their email conversations in the UI
5. Auto-creation uses the **per-integration allowlist**, independent of `AUTH_WHITELIST_DOMAINS`

**Implementation**: `UserService.create_email_user()` in `backend/app/services/user_service.py`

### Auto-Share & Auto-Accept (Clone Mode)

When an email arrives from a known/new user who doesn't have a clone yet:
1. If user already has a **pending** share for this agent -> auto-accept it
2. If user has no share -> create a share (`status: accepted`, `source: email_integration`) and create clone
3. The share appears in the Share Management UI with an `email` badge
4. Clone creation follows the standard `AgentCloneService.create_clone()` flow

**Implementation**: `AgentShareService.create_auto_share()` in `backend/app/services/agent_share_service.py`

### First-Email Handling

Creating a clone involves Docker image build + workspace copy, which takes time:
- The incoming email message is **silently queued** (`email_message` table, `pending_clone_creation=True`)
- The polling/processing scheduler periodically retries pending messages
- Once the clone's environment is active (`running` status), queued messages are processed
- The sender receives no immediate feedback (response arrives when agent processes the message)

### Email Threading

Email conversations map to agent sessions via email headers:
- `Message-ID` header serves as `email_thread_id` on the session
- `In-Reply-To` and `References` headers are used to match follow-up emails to existing sessions
- Agent replies include proper `In-Reply-To` and `References` headers for client threading

### Session Context for Agent Scripts

Agent scripts running inside environments can query session metadata via HTTP:
- `GET /session/context` endpoint (localhost-only, no auth) returns:
  - `integration_type` (e.g., "email")
  - `agent_id`, `is_clone`, `parent_agent_id`
  - `session_id`, `backend_session_id`, `mode`
- Enables conditional logic in agent scripts (e.g., format responses differently for email)

**Implementation**:
- `ActiveSessionManager.set_current_context()` stores context when a stream starts
- `routes.py::get_session_context()` exposes it via HTTP
- Context is cleared automatically when the stream ends

## File Structure

### New Files

```
backend/app/
├── models/
│   ├── mail_server_config.py           # IMAP/SMTP server credentials (encrypted)
│   ├── agent_email_integration.py      # Per-agent email integration settings + EmailProcessAs enum
│   ├── email_message.py                # Parsed incoming emails (+ input_task_id)
│   └── outgoing_email_queue.py         # Queued agent reply emails (+ input_task_id, nullable session/message)
│
├── agents/
│   ├── email_reply_generator.py        # AI function: generate email reply from session results
│   └── prompts/
│       └── email_reply_generator_prompt.md  # Prompt template for reply generation
│
├── services/email/                     # All email-related services
│   ├── __init__.py                     # Re-exports main service classes
│   ├── mail_server_service.py          # CRUD + connection testing for mail servers
│   ├── integration_service.py          # CRUD + orchestration for agent integrations
│   ├── routing_service.py              # Sender -> target agent mapping + auto-share
│   ├── polling_service.py              # IMAP connection, fetching, parsing, storage
│   ├── polling_scheduler.py            # APScheduler job (every 5 min)
│   ├── processing_service.py           # Route emails to sessions, inject messages
│   ├── sending_service.py              # Queue + send agent replies via SMTP
│   └── sending_scheduler.py            # APScheduler job (every 2 min)
│
├── api/routes/
│   ├── mail_servers.py                 # Mail server CRUD + test endpoints
│   └── email_integration.py           # Agent email integration config endpoints
│
└── alembic/versions/
    ├── 029b03776737_add_mail_server_config_table.py
    ├── cc1e27a71798_add_agent_email_integration_table.py
    ├── 7aeed6ea3abf_add_share_source_and_session_email_.py
    ├── 485e7e243dd5_add_email_message_table.py
    ├── 8a95916ab539_add_outgoing_email_queue_table.py
    ├── f3a1b2c4d5e6_add_agent_session_mode_and_sender_email.py
    └── h5c3d4e6f7g8_add_email_task_processing.py

frontend/src/
├── components/
│   ├── UserSettings/
│   │   └── MailServerSettings.tsx       # Mail server CRUD settings panel
│   └── Agents/
│       ├── EmailIntegrationCard.tsx     # Top-level integration card (toggle, actions)
│       ├── EmailAccessModal.tsx         # Access mode, patterns, domain allowlist
│       ├── EmailConnectionModal.tsx     # IMAP/SMTP server assignment
│       └── EmailSessionsModal.tsx       # Session mode, max clones, share mode
```

### Modified Files

```
backend/app/
├── models/
│   ├── agent_share.py                  # Added: source field (manual | email_integration)
│   ├── session.py                      # Added: email_thread_id, integration_type, sender_email
│   └── __init__.py                     # Registered new models
├── services/
│   ├── agent_share_service.py          # Added: create_auto_share() method
│   ├── user_service.py                 # Added: create_email_user() method
│   ├── session_service.py              # Added: email thread support, get_session_by_email_thread()
│   ├── message_service.py             # Added: session_context emission, STREAM_COMPLETED hook
│   ├── input_task_service.py           # Added: send_email_answer() method
│   └── ai_functions_service.py         # Added: generate_email_reply() wrapper
├── api/main.py                         # Registered mail_servers + email_integration routers
├── main.py                             # Registered polling/sending schedulers + event handler
└── env-templates/python-env-advanced/app/core/server/
    ├── active_session_manager.py       # Added: set/get/clear_current_context()
    └── routes.py                       # Added: GET /session/context endpoint + context storage

frontend/src/
├── components/
│   ├── Agents/
│   │   ├── AgentIntegrationsTab.tsx    # Added: EmailIntegrationCard render
│   │   ├── EmailSessionsModal.tsx      # Added: process_as radio group (New Session / New Task)
│   │   └── ShareManagement/
│   │       ├── ShareList.tsx           # Added: email source badge
│   │       └── ClonesList.tsx          # Added: email clone badge
│   └── Chat/
│       ├── MessageBubble.tsx           # Added: integrationTyp prop
│       └── MessageList.tsx             # Added: integrationTyp prop forwarding
├── routes/_layout/
│   ├── settings.tsx                    # Added: "Mail Servers" tab
│   ├── tasks.tsx                       # Added: "Send Answer" button for email-originated tasks
│   ├── task/$taskId.tsx                # Added: "Send Answer" button in task detail footer
│   └── session/$sessionId.tsx          # Added: Email/A2A badges in session header
└── client/                             # Auto-regenerated (schemas, sdk, types)
```

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
| `email_message_id` | string | Message-ID email header |
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
| `message_id` | UUID, FK → `message.id` CASCADE, nullable | Agent's reply message (null for task-originated replies) |
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

**`agent_share`** — Added field:
- `source` (string, default `"manual"`): `"manual"` | `"email_integration"`

**`session`** — Added fields:
- `email_thread_id` (string, nullable): Email Message-ID for thread matching
- `integration_type` (string, nullable): `"email"` | `"a2a"` | null
- `sender_email` (string, nullable): Original sender (owner mode only)

**`input_task`** — Added fields (email source tracking):
- `source_email_message_id` (UUID, FK → `email_message.id` SET NULL, nullable): Original email that created this task
- `source_agent_id` (UUID, FK → `agent.id` SET NULL, nullable): Original email agent (preserved for SMTP config lookup even if task is reassigned to a different agent)

### Credential Storage

Mail server credentials are stored **separately** from the existing credential system:

| System | Purpose | Storage | Usage |
|--------|---------|---------|-------|
| **Existing Credential System** | Share credentials WITH agents | `credentials` table, synced to agent-env | Agents use in scripts (Odoo API, Gmail, etc.) |
| **Mail Server Credentials** | Backend polls/sends emails | `mail_server_config.encrypted_password` | Backend-only, NEVER shared with agents |

Encryption uses existing `encrypt_field()` / `decrypt_field()` from `backend/app/core/security.py`.

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

The endpoint validates task ownership, checks that `source_email_message_id` is set, generates an AI reply from session results via `AIFunctionsService.generate_email_reply()`, and queues it in `outgoing_email_queue` using the `source_agent_id`'s SMTP config. Accepts an optional `custom_message` to skip AI generation.

### Agent Environment (Internal)

**Router**: `backend/app/env-templates/python-env-advanced/app/core/server/routes.py`

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/session/context` | Current session metadata (localhost-only, no auth) |

## Service Architecture

### Email Service Organization

All email-related services live in `backend/app/services/email/`:

```
services/email/
├── __init__.py              # Re-exports: MailServerService, EmailIntegrationService,
│                            #   EmailRoutingService, EmailAccessDenied,
│                            #   EmailPollingService, EmailProcessingService,
│                            #   EmailSendingService
│
├── mail_server_service.py   # MailServerService
│                            #   CRUD with password encryption
│                            #   Connection testing (IMAP/SMTP)
│
├── integration_service.py   # EmailIntegrationService
│                            #   CRUD for agent email integrations
│                            #   Enable/disable with validation
│                            #   process_emails() — manual trigger
│                            #   Clone count tracking
│
├── routing_service.py       # EmailRoutingService
│                            #   route_email() -> (target_agent_id, is_ready, session_mode)
│                            #   Access control (open/restricted, domains, patterns)
│                            #   Auto-user creation + auto-share + auto-clone
│                            #   EmailAccessDenied exception
│
├── polling_service.py       # EmailPollingService
│                            #   IMAP connection + fetch unread emails
│                            #   Email parsing (subject, body, headers, attachments metadata)
│                            #   Deduplication by Message-ID
│                            #   Recipient validation (incoming_mailbox match)
│                            #   poll_all_enabled_agents() for scheduler
│
├── polling_scheduler.py     # APScheduler job — every 5 minutes
│                            #   poll_all_enabled_agents() -> process each -> retry pending
│
├── processing_service.py    # EmailProcessingService
│                            #   Branches on integration.process_as:
│                            #     new_session: Routes to target agents via EmailRoutingService,
│                            #       finds/creates sessions, injects messages
│                            #     new_task: Creates InputTask for agent owner to review
│                            #   Handles pending clone creation retries
│
├── sending_service.py       # EmailSendingService
│                            #   queue_outgoing_email() — creates queue entries
│                            #   send_pending_emails() — processes SMTP queue
│                            #   handle_stream_completed() — event handler on STREAM_COMPLETED
│                            #   MIME message building with threading headers
│                            #   Retry logic (max 3 attempts)
│
└── sending_scheduler.py     # APScheduler job — every 2 minutes
                             #   send_pending_emails() — flush SMTP queue
```

### End-to-End Flow

**1. Setup** — User adds IMAP/SMTP servers in Settings > Mail Servers, then configures an agent's email integration (connection, access rules, session mode) via the EmailIntegrationCard modals.

**2. Polling (every 5 min)** — `EmailPollingService.poll_all_enabled_agents()` connects to each enabled agent's IMAP server, fetches UNSEEN emails addressed to `incoming_mailbox`, deduplicates by Message-ID, stores them in `email_message` table, marks them read on IMAP.

**3. Routing** — `EmailRoutingService.route_email()` determines the target:
- **OWNER mode**: Check access -> check env status -> return `(agent_id, is_ready, OWNER)`
- **CLONE mode**: Find existing clone -> if found: check readiness -> if not found: check access -> check `max_clones` -> ensure user exists -> auto-accept pending share or create new auto-share+clone

**4. User Provisioning** — If sender has no account, `UserService.create_email_user()` creates one with a random password (bypasses domain whitelist).

**5. Processing** — `EmailProcessingService` checks `integration.process_as`:

- **New Session path** (`_process_email_to_session()`):
  - Determines `thread_id` from `in_reply_to` or `email_message_id`
  - Looks up existing session by `email_thread_id` via `SessionService.get_session_by_email_thread()`
  - Creates new session if needed (with `email_thread_id`, `integration_type="email"`, optional `sender_email`)
  - Sends message via `SessionService.send_session_message()` with `initiate_streaming=True`

- **New Task path** (`_process_email_to_task()`):
  - Creates `InputTask` with email content as `original_message`/`current_description`
  - Sets `source_email_message_id` and `source_agent_id` for email tracking
  - Pre-selects the email agent as `selected_agent_id`
  - Marks email as `processed=True` with `input_task_id` link
  - No session created — owner reviews and executes manually

**6. Agent Streaming** — Session and agent-env run normally. The agent-env receives `session_context.integration_type="email"` and exposes it via `GET /session/context`.

**6b. Task Execution (new_task path only)** — Owner reviews the task in the Tasks UI, optionally refines the description or reassigns to a different agent, then clicks Execute. `InputTaskService.execute_task()` creates a session via `SessionService.create_session()` (with `source_task_id` link), sends the task description as the initial message via `SessionService.send_session_message()`, and the agent processes it normally. The task status transitions: `new` → `running`, and `session_id` is linked.

**7. Sending Reply** — Two paths depending on how the email was processed:

- **Auto-reply (new_session path, every 2 min)**: `EmailSendingService.handle_stream_completed()` fires on `STREAM_COMPLETED` events. If `session.integration_type == "email"`, the last agent message is queued in `outgoing_email_queue`. The sending scheduler sends it via SMTP with proper `In-Reply-To`/`References` threading headers.

- **Manual reply (new_task path)**: Owner clicks "Send Answer" on the task after agent execution completes. `InputTaskService.send_email_answer()` retrieves the last agent message (`role == "agent"`) from the linked session (or `session.result_summary` if available), generates an AI-crafted reply via `AIFunctionsService.generate_email_reply()`, then queues it in `outgoing_email_queue` with `input_task_id` reference. The sending scheduler sends it via SMTP using the `source_agent_id`'s SMTP config. Alternatively, a `custom_message` can be provided to skip AI generation.

### Scheduler Registration

Both schedulers are registered in `backend/app/main.py` during the app lifespan:

```python
# Startup
start_email_polling_scheduler()    # Every 5 min: poll IMAP
start_email_sending_scheduler()    # Every 2 min: flush SMTP queue

# Event handler
event_service.register_handler(
    event_type=EventType.STREAM_COMPLETED,
    handler=EmailSendingService.handle_stream_completed
)

# Shutdown
shutdown_email_polling_scheduler()
shutdown_email_sending_scheduler()
```

### Integration Points with Existing Services

| Existing Service | New Integration |
|------------------|-----------------|
| `AgentShareService` | `create_auto_share()` — creates pre-accepted share + clone in one step |
| `UserService` | `create_email_user()` — creates user from email, bypasses domain whitelist |
| `SessionService` | `create_session()` accepts `email_thread_id`, `integration_type`, `sender_email` |
| `SessionService` | `get_session_by_email_thread()` — finds session by thread ID for continuity |
| `MessageService` | Emits `session_context` (including `integration_type`) to agent-env on every stream |
| `InputTaskService` | `send_email_answer()` — generates AI reply and queues outgoing email for task-originated emails |
| `AIFunctionsService` | `generate_email_reply()` — AI-powered email reply generation from session results |
| Event Bus | `STREAM_COMPLETED` event triggers `EmailSendingService.handle_stream_completed()` |

## Frontend Architecture

### Settings: Mail Server Management

**Component**: `frontend/src/components/UserSettings/MailServerSettings.tsx`
**Tab**: "Mail Servers" in User Settings (between AI Credentials and SSH Keys)

- Full CRUD table for IMAP/SMTP server configs
- Add/Edit dialog: name, server type, host, port, encryption, username, password
- Auto-updates port when server type or encryption changes (sensible defaults)
- Test connection button with loading state and success/error feedback
- Delete confirmation dialog (prevents deletion if server is in use)
- React Query keys: `["mail-servers"]`

### Agent: Email Integration Card

**Component**: `frontend/src/components/Agents/EmailIntegrationCard.tsx`
**Location**: Agent Integrations tab (only shown for non-clone agents)

Top-level card with:
- Enable/disable toggle
- Clone count display (`email_clone_count/max_clones`)
- Config completeness indicators (connection valid + access valid)
- Action buttons opening three sub-modals:
  - **Access** (`EmailAccessModal.tsx`) — access mode, auto-approve patterns, domain allowlist
  - **Sessions** (`EmailSessionsModal.tsx`) — session mode (clone/owner), max clones, share mode, email processing mode (new session/new task)
  - **Connection** (`EmailConnectionModal.tsx`) — IMAP/SMTP server selection, mailbox/from addresses
- Manual "Process Emails" button (triggers immediate poll + process)

### Tasks: Email-Originated Task Actions

**Tasks List** (`frontend/src/routes/_layout/tasks.tsx`):
- Email-originated tasks (with `source_email_message_id`) show a **"Send Answer"** button with Mail icon
- Button visible when task status is `completed` or `error` (has session results)
- Shows loading state during AI reply generation
- Toast notification on success/error

**Task Detail** (`frontend/src/routes/_layout/task/$taskId.tsx`):
- **"Send Answer"** button in the footer action buttons area, same visibility/enable logic as tasks list
- Positioned next to the Execute button for easy access

### Share Management: Email Badges

**ShareList.tsx**: Shares with `source === "email_integration"` show an indigo "Email" badge with Mail icon.

**ClonesList.tsx**: Clones created via email integration show an indigo "Email" badge next to their name.

### Session UI: Integration Badges

**Session header** (`$sessionId.tsx`): Shows integration type badge next to the mode indicator:
- Email sessions: indigo `Email` badge with Mail icon
- A2A sessions: purple `A2A` badge with Plug icon

**MessageList/MessageBubble**: `integrationTyp` prop is passed through for potential per-message indicators.

## Security Considerations

### 1. Clone-Based Isolation (Primary)
- Each email sender gets their own Docker environment
- Workspace files are completely isolated per clone
- Sessions belong to the sender's user account
- No cross-sender data leakage at the environment level

### 2. Credential Separation
- Mail server credentials stored encrypted, **backend-only** — never shared with agents or exposed in API responses
- Decryption only happens in backend services when connecting to IMAP/SMTP
- `MailServerConfigPublic` schema exposes `has_password: bool` instead of actual password

### 3. Rate Limiting & Resource Protection
- `max_clones` limit per agent (configurable, default 50, max 1000)
- Per-integration `allowed_domains` filter
- `auto_approve_email_pattern` for restricted mode
- Polling frequency: every 5 minutes per enabled agent
- Sending queue: max 3 retry attempts per email

### 4. Email Sender Identity
- Email "From" addresses can be spoofed — accepted as known limitation
- For higher security, use restricted mode with specific email patterns
- Future: SPF/DKIM/DMARC verification

### 5. Auto-Created User Accounts
- Created with random password (not sent to user)
- User can claim via password reset or OAuth
- Per-integration `allowed_domains` controls which email domains can trigger user creation
- Independent of global `AUTH_WHITELIST_DOMAINS`

### 6. Recipient Validation
- `EmailPollingService._is_addressed_to_agent()` validates that the email was actually sent to the agent's configured `incoming_mailbox`
- Prevents processing emails addressed to others sharing the same IMAP inbox

## Implementation References

**Models**:
- `backend/app/models/mail_server_config.py` — `MailServerConfig`, `MailServerConfigPublic`, `MailServerConfigCreate`, `MailServerConfigUpdate`
- `backend/app/models/agent_email_integration.py` — `AgentEmailIntegration`, `AgentEmailIntegrationPublic`, `EmailAccessMode`, `AgentSessionMode`, `EmailProcessAs`, `ProcessEmailsResult`
- `backend/app/models/input_task.py` — `InputTask` (added `source_email_message_id`, `source_agent_id`), `SendAnswerRequest`, `SendAnswerResponse`
- `backend/app/models/email_message.py` — `EmailMessage`, `EmailMessagePublic`
- `backend/app/models/outgoing_email_queue.py` — `OutgoingEmailQueue`, `OutgoingEmailQueuePublic`, `OutgoingEmailStatus`

**Services**:
- `backend/app/services/email/mail_server_service.py` — `MailServerService` (CRUD + connection testing)
- `backend/app/services/email/integration_service.py` — `EmailIntegrationService` (CRUD + orchestration)
- `backend/app/services/email/routing_service.py` — `EmailRoutingService` (sender -> target mapping)
- `backend/app/services/email/polling_service.py` — `EmailPollingService` (IMAP fetch + parse)
- `backend/app/services/email/processing_service.py` — `EmailProcessingService` (route + inject messages)
- `backend/app/services/email/sending_service.py` — `EmailSendingService` (queue + SMTP send)
- `backend/app/services/email/polling_scheduler.py` — APScheduler (5 min interval)
- `backend/app/services/email/sending_scheduler.py` — APScheduler (2 min interval)

**AI Functions**:
- `backend/app/agents/email_reply_generator.py` — `generate_email_reply()` AI function
- `backend/app/agents/prompts/email_reply_generator_prompt.md` — Prompt template for email reply generation
- `backend/app/services/ai_functions_service.py` — `generate_email_reply()` wrapper

**Updated Services**:
- `backend/app/services/agent_share_service.py` — `create_auto_share()` method
- `backend/app/services/user_service.py` — `create_email_user()` method
- `backend/app/services/session_service.py` — `get_session_by_email_thread()`, email params on `create_session()`
- `backend/app/services/message_service.py` — `session_context` emission to agent-env
- `backend/app/services/input_task_service.py` — `send_email_answer()` method for email-originated task replies

**API Routes**:
- `backend/app/api/routes/mail_servers.py` — Mail server CRUD + test
- `backend/app/api/routes/email_integration.py` — Agent email integration config

**Agent-Env**:
- `backend/app/env-templates/python-env-advanced/app/core/server/active_session_manager.py` — Context storage
- `backend/app/env-templates/python-env-advanced/app/core/server/routes.py` — `GET /session/context`

**Frontend**:
- `frontend/src/components/UserSettings/MailServerSettings.tsx` — Settings panel
- `frontend/src/components/Agents/EmailIntegrationCard.tsx` — Main integration card
- `frontend/src/components/Agents/EmailAccessModal.tsx` — Access control modal
- `frontend/src/components/Agents/EmailConnectionModal.tsx` — Server assignment modal
- `frontend/src/components/Agents/EmailSessionsModal.tsx` — Session mode + email processing mode modal
- `frontend/src/components/Agents/ShareManagement/ShareList.tsx` — Email share badges
- `frontend/src/components/Agents/ShareManagement/ClonesList.tsx` — Email clone badges
- `frontend/src/routes/_layout/tasks.tsx` — "Send Answer" button for email-originated tasks
- `frontend/src/routes/_layout/task/$taskId.tsx` — "Send Answer" button in task detail
- `frontend/src/routes/_layout/session/$sessionId.tsx` — Integration type badges

**Migrations**:
- `029b03776737` — `mail_server_config` table
- `cc1e27a71798` — `agent_email_integration` table
- `7aeed6ea3abf` — `agent_share.source` + `session.email_thread_id` + `session.integration_type`
- `485e7e243dd5` — `email_message` table
- `8a95916ab539` — `outgoing_email_queue` table
- `f3a1b2c4d5e6` — `agent_email_integration.agent_session_mode` + `session.sender_email`
- `h5c3d4e6f7g8` — `agent_email_integration.process_as` + `input_task` email source fields + `email_message.input_task_id` + `outgoing_email_queue.input_task_id` + nullable session_id/message_id

## Benefits

1. **Complete Isolation**: Each email sender gets an isolated Docker environment (clone mode) — no cross-sender data leakage
2. **Reuses Existing Infrastructure**: Leverages shared agents mechanism, clone service, suspension scheduler — no new isolation infrastructure
3. **Flexible Session Modes**: Clone mode for multi-user scenarios, owner mode for personal automation
4. **Granular Access Control**: Open/restricted modes, domain allowlists, glob-pattern auto-approval
5. **Seamless User Provisioning**: Auto-creates user accounts from email addresses, claimable via password reset or OAuth
6. **Email Threading**: Maintains conversation continuity across multiple email exchanges via standard headers
7. **Asynchronous Processing**: Queue-based sending prevents blocking agent responses, with retry logic for failures
8. **Agent Awareness**: Agent scripts can detect email sessions via `GET /session/context` and adapt behavior
9. **Full UI Visibility**: Email-originated shares, clones, and sessions are clearly badged across the interface
10. **Manual Override**: "Process Emails" button allows immediate poll + process without waiting for scheduler
11. **Standard Protocol Support**: Works with any IMAP/SMTP server, supports SSL/TLS/STARTTLS encryption
12. **Review-Before-Respond**: "New Task" processing mode gives human oversight over email requests before auto-responding — review, refine, choose the right agent, then send an AI-crafted reply
