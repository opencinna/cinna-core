# Email Sessions

## Purpose

Define how incoming emails are processed into agent sessions or tasks, how replies are generated and sent, and how email conversations maintain threading continuity. This covers session modes, processing modes, the polling-to-sending pipeline, and agent-side session context awareness.

## Core Concepts

- **Session Mode** — Determines where email sessions run: `clone` (isolated per sender) or `owner` (shared on parent agent)
- **Processing Mode** — Determines what happens with incoming emails: `new_session` (auto-respond) or `new_task` (human review first)
- **Email Threading** — Mapping email Message-ID / In-Reply-To / References headers to session continuity
- **Session Context** — HMAC-verified metadata (sender, subject, session_id) injected into agent's system prompt and exposed via API
- **Outgoing Email Queue** — Asynchronous queue for agent replies sent via SMTP
- **Pending Clone Creation** — Emails queued until the sender's clone environment is ready

## User Stories / Flows

### 1. Auto-Response Flow (New Session Mode)

1. Polling scheduler fetches unread emails from IMAP (every 5 min)
2. Routing service determines the target agent:
   - **Clone mode**: finds existing clone or creates one for the sender
   - **Owner mode**: routes directly to parent agent
3. If the target clone is still building → email queued with `pending_clone_creation=True`
4. Processing service matches email to an existing session via threading headers
5. If no existing session → new session created with `email_thread_id` and `integration_type="email"`
6. Email body injected as user message; agent streaming begins
7. On `STREAM_COMPLETED` event → last agent message queued in outgoing email queue
8. Sending scheduler delivers reply via SMTP (every 2 min) with proper threading headers

### 2. Review-Then-Respond Flow (New Task Mode)

1. Email polling and parsing happen identically
2. Processing service creates an **InputTask** instead of a session
3. Task pre-populated with email content, email agent pre-selected
4. Owner reviews task in Tasks UI, optionally refines or reassigns agent
5. Owner executes task → session created, agent processes it
6. Owner clicks **"Send Answer"** → AI generates reply from session results
7. Reply queued and sent via the original email agent's SMTP config
8. Alternative: owner provides `custom_message` to skip AI generation

### 3. Email Thread Continuation

1. User replies to an agent's email response
2. Reply's `In-Reply-To` header matches the original `Message-ID`
3. System finds the existing session via `email_thread_id` lookup
4. New message injected into the same session — conversation continues
5. Agent's response is threaded back with proper `References` chain

### 4. Pending Clone Retry

1. First email from a new sender triggers clone creation
2. Email marked `pending_clone_creation=True` (not processed yet)
3. Polling scheduler periodically retries pending messages
4. Once clone environment reaches `running` status → email is processed normally
5. Sender receives no immediate feedback (response arrives when agent processes)

## Business Rules

### Session Modes

**Clone Mode** (`agent_session_mode = 'clone'`, default):
- Each email sender gets a dedicated clone with isolated Docker environment
- Clone created via standard `AgentCloneService.create_clone()` flow
- Sessions belong to the sender's user account
- Clone count tracked against `max_clones` limit
- `clone_share_mode` controls access level: `user` or `builder`
- Ideal for: multi-user bots, customer support, public-facing agents

**Owner Mode** (`agent_session_mode = 'owner'`):
- Sessions created directly on the original agent in owner's user space
- No clone creation — all emails processed in the same environment
- `sender_email` stored on the session to route replies back to original sender
- Clone-specific settings (`max_clones`, `clone_share_mode`) ignored
- Ideal for: personal automation, single-user workflows

### Processing Modes

**New Session** (`process_as = 'new_session'`, default):
- Incoming emails immediately create/continue agent sessions
- Agent auto-responds — full routing/clone/session flow executes automatically
- Ideal for: automated workflows, customer support bots, always-on agents

**New Task** (`process_as = 'new_task'`):
- Incoming emails create InputTasks assigned to the agent owner
- No automatic session or response — owner reviews first
- Task pre-populated with email content; email agent pre-selected
- Owner can refine description, change assigned agent, then execute manually
- After execution, "Send Answer" generates AI-crafted reply from session results
- `source_agent_id` always points to original email agent (for SMTP config), even if task reassigned
- Ideal for: review-before-respond workflows, complex requests, human oversight

### Email Threading Rules

- `Message-ID` header → `email_thread_id` on session
- `In-Reply-To` / `References` headers used to match follow-up emails to existing sessions
- Agent replies include proper `In-Reply-To` and `References` for email client threading
- New emails (no `In-Reply-To`) create new sessions; replies join existing sessions

### Outgoing Email Rules

- **Auto-reply path** (new_session): `STREAM_COMPLETED` event triggers queue entry from last agent message
- **Manual reply path** (new_task): "Send Answer" generates AI reply or uses custom_message, queues it
- SMTP delivery uses parent agent's outgoing server config in both cases
- Max 3 retry attempts for failed sends
- Threading headers (`In-Reply-To`, `References`) preserved for proper email client threading

## Session Context for Agent Scripts

Agent scripts running inside environments can detect email sessions and access metadata:

**Three-Layer Security:**

1. **System Prompt Injection** — Server-verified metadata (sender, subject, session_id) injected into LLM system prompt under "Session Context (Server-Verified, Read-Only)". LLM instructed to trust these values over message content (mitigates prompt injection via email body)

2. **HMAC-Verified Per-Session Context Store** — Backend HMAC-signs `session_context` with `AGENT_AUTH_TOKEN`. Agent-env verifies signature before storing. Context stored in dict keyed by `backend_session_id`, supporting parallel sessions without cross-session leakage. Context cleaned up on stream end with TTL-based cleanup (24h) as fallback

3. **Helper Script** — `/app/core/scripts/get_session_context.py` provides stdlib-only CLI/import access. LLM passes `backend_session_id` from system prompt to scripts as CLI argument. Scripts query core server directly, bypassing LLM for authoritative context

**Context Fields:**
- `integration_type` — e.g., `"email"`
- `sender_email` — original sender
- `email_subject` — subject of the initiating email (fetched from linked `EmailMessage`)
- `email_thread_id` — email Message-ID for threading
- `agent_id`, `is_clone`, `parent_agent_id`
- `backend_session_id`

**HTTP Endpoint:** `GET /session/context?session_id=<backend_session_id>` (localhost-only, no auth)

## Architecture Overview

```
Polling Scheduler (5 min)
    → EmailPollingService.poll_all_enabled_agents()
        → IMAP connect → fetch UNSEEN → parse → store EmailMessage
            → EmailProcessingService
                → process_as = new_session?
                    → EmailRoutingService.route_email()
                        → Clone mode: find/create clone + auto-share
                        → Owner mode: route to parent
                    → Find/create session (thread matching)
                    → Inject message → Agent streaming
                → process_as = new_task?
                    → Create InputTask for owner review

STREAM_COMPLETED event (auto-reply)
    → EmailSendingService.handle_stream_completed()
        → Queue last agent message in outgoing_email_queue

"Send Answer" button (manual reply)
    → InputTaskService.send_email_answer()
        → AIFunctionsService.generate_email_reply() or custom_message
        → Queue reply in outgoing_email_queue

Sending Scheduler (2 min)
    → EmailSendingService.send_pending_emails()
        → SMTP connect → send → mark sent / retry on failure
```

## Integration Points

- [Email Integration](email_integration.md) — Parent feature: access control, security model, overall architecture
- [Mail Servers](mail_servers.md) — IMAP/SMTP server credentials used by polling and sending services
- Agent Sessions — Session lifecycle, streaming, message injection <!-- TODO: link when agents/agent_sessions docs are created -->
- [Agent Sharing](../../agents/agent_sharing/agent_sharing.md) — Auto-share and clone creation for email senders
- [Agent Environment Core](../../agents/agent_environment_core/agent_environment_core.md) — Session context injection, prompt generation, helper scripts
- [Input Tasks](../input_tasks/input_tasks.md) — Task creation from emails, "Send Answer" flow
- [Agent Activities](../agent_activities/agent_activities.md) — Email-originated tasks create `email_task_incoming` and `email_task_reply_pending` activities that notify the agent owner via the sidebar bell indicator
