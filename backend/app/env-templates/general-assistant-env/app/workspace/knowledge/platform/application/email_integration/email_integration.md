# Email Integration

## Purpose

Enable agents to receive emails at configured IMAP mailboxes, process them as agent sessions or reviewable tasks, and send replies back via SMTP. Provides a complete email-to-agent automation pipeline with security isolation, flexible access control, and two distinct processing modes.

## Core Concepts

- **Mail Server** — User-configured IMAP or SMTP server with encrypted credentials. See [Mail Servers](mail_servers.md)
- **Agent Email Integration** — Per-agent configuration linking mail servers, access rules, and processing behavior
- **Email Session** — Agent session initiated by an incoming email, with threading support. See [Email Sessions](email_sessions.md)
- **Email Message** — Parsed incoming email stored for processing and audit
- **Outgoing Email Queue** — Queued agent reply emails awaiting SMTP delivery
- **Clone-Based Isolation** — Each email sender gets their own cloned agent with independent environment (clone mode)
- **Owner Mode** — All emails processed on the original agent in the owner's environment
- **Process as New Task** — Incoming emails create reviewable InputTasks instead of auto-responding sessions

## User Stories / Flows

### 1. Setup Flow

1. User adds IMAP and SMTP server configurations in **Settings > Mail Servers**
2. User navigates to agent's **Integrations** tab and opens Email Integration
3. User configures **Connection** (selects IMAP/SMTP servers, sets mailbox and from-address)
4. User configures **Access** rules (open/restricted mode, domain allowlist, auto-approve patterns)
5. User configures **Sessions** (clone/owner mode, new session/new task processing, max clones)
6. User enables the integration — validation ensures required fields are set

### 2. Incoming Email (New Session Mode)

1. Polling scheduler (every 5 min) fetches unread emails from IMAP
2. System identifies the sender and routes the email to a target agent:
   - **Clone mode**: finds or creates a sender-specific clone
   - **Owner mode**: routes to the parent agent directly
3. System matches email to an existing session via threading headers, or creates a new session
4. Email body is injected as a user message; agent streaming begins
5. Agent response is queued in the outgoing email queue
6. Sending scheduler (every 2 min) delivers the reply via SMTP with threading headers

### 3. Incoming Email (New Task Mode)

1. Polling and parsing happen identically to new session mode
2. Instead of creating a session, an **InputTask** is created for the agent owner
3. Owner reviews the task in the Tasks UI, optionally refines description or reassigns to a different agent
4. Owner executes the task — agent processes it and produces results
5. Owner clicks **"Send Answer"** — system generates an AI-crafted email reply from session results
6. Reply is queued and sent via the original email agent's SMTP configuration

### 4. First-Time Sender (Clone Mode)

1. Email arrives from an unknown sender (no user account)
2. System auto-creates a user account with a random password
3. System creates an auto-share + clone for the sender
4. Clone environment build starts (Docker image build + workspace copy)
5. Email is queued with `pending_clone_creation=True`
6. Retry scheduler picks up the email once the clone environment is active
7. Sender can later claim their account via password reset or OAuth login

## Business Rules

### Access Control

- **Open mode**: Any email sender gets access (subject to `max_clones` and `allowed_domains`)
- **Restricted mode**: Only senders matching one of these criteria:
  1. Agent pre-shared with them via Share Management UI
  2. Sender email matches `auto_approve_email_pattern` (glob-style, e.g., `*@example.com,tech-*@another.com`)
- `allowed_domains` applies in both modes as an additional filter (comma-separated, case-insensitive)
- Emails from non-matching senders are silently ignored
- `process_as` only affects future emails; already-processed emails retain their state

### Auto-User Creation

- Created with random password (not sent to the user)
- Uses per-integration `allowed_domains`, independent of global `AUTH_WHITELIST_DOMAINS`
- User can claim account via password reset or OAuth login
- Once logged in, users see their email conversations in the UI

### Auto-Share & Auto-Clone (Clone Mode)

- If sender has a **pending** share for this agent → auto-accept it
- If sender has no share → create share (`status: accepted`, `source: email_integration`) and create clone
- Share appears in Share Management UI with an "Email" badge
- Clone count tracked against `max_clones` limit (configurable, default 50, max 1000)

### Email Threading

- `Message-ID` header maps to `email_thread_id` on the session
- `In-Reply-To` and `References` headers match follow-up emails to existing sessions
- Agent replies include proper `In-Reply-To` and `References` for client threading

### Task Mode Rules

- `source_agent_id` on InputTask always points to the original email agent (for SMTP config), even if the user reassigns the task to a different agent for execution
- `source_email_message_id` being non-null identifies email-originated tasks (no separate `source` field needed)
- Each email creates a separate task — no threading/grouping at the task level
- AI reply generation uses session results; optional `custom_message` skips AI generation

## Architecture Overview

```
External Sender → Email → IMAP Server → Backend Polling (parent agent)
                                              |
                                      Sender Identification
                                              |
                                     process_as setting?
                                       /            \
                                   new_task       new_session
                                      |               |
                                Create InputTask   EmailRoutingService
                                for agent owner     (clone/owner mode)
                                      |               |
                              Owner reviews,     Route to target agent
                              refines, executes       |
                                      |          Message → Session
                              "Send Answer"           |
                                      |          Agent Response
                              AI generates reply      |
                                      |          Email Queue → SMTP → Sender
                              Email Queue → SMTP → Sender
```

## Security Model

### 1. Clone-Based Isolation (Primary)
- Each email sender gets their own Docker environment (clone mode)
- Workspace files completely isolated per clone
- Sessions belong to the sender's user account
- No cross-sender data leakage at the environment level

### 2. Credential Separation
- Mail server credentials stored encrypted, **backend-only** — never shared with agents
- Decryption only happens when connecting to IMAP/SMTP
- API responses expose `has_password: bool` instead of actual password
- See [Mail Servers](mail_servers.md) for details

### 3. Rate Limiting & Resource Protection
- `max_clones` limit per agent (configurable, default 50, max 1000)
- Per-integration `allowed_domains` filter
- Polling frequency: every 5 minutes per enabled agent
- Sending queue: max 3 retry attempts per email

### 4. Email Sender Identity
- Email "From" addresses can be spoofed — accepted as known limitation
- For higher security, use restricted mode with specific email patterns
- Future: SPF/DKIM/DMARC verification

### 5. Recipient Validation
- Polling service validates that emails are actually addressed to the agent's `incoming_mailbox`
- Prevents processing emails addressed to others sharing the same IMAP inbox

## Integration Points

- Agent Sessions — Session creation, streaming, message injection <!-- TODO: link when agents/agent_sessions docs are created -->
- [Agent Sharing](../../agents/agent_sharing/agent_sharing.md) — Auto-share + clone creation for email senders
- [Agent Environments](../../agents/agent_environments/agent_environments.md) — Docker environment build for clones
- [Agent Environment Core](../../agents/agent_environment_core/agent_environment_core.md) — Session context injection for email awareness
- [Mail Servers](mail_servers.md) — IMAP/SMTP server configuration and credential management
- [Email Sessions](email_sessions.md) — Session modes, processing, threading, and sending
- [Input Tasks](../input_tasks/input_tasks.md) — Task creation from emails, "Send Answer" flow
- [AI Functions](../../development/backend/ai_functions_development.md) — Email reply generation via LLM cascade
