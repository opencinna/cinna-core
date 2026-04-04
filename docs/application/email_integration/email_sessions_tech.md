# Email Sessions — Technical Details

## File Locations

### Backend — Services (Processing Pipeline)
- `backend/app/services/email/polling_service.py` — `EmailPollingService` (IMAP fetch + parse)
- `backend/app/services/email/processing_service.py` — `EmailProcessingService` (route + session/task creation)
- `backend/app/services/email/routing_service.py` — `EmailRoutingService` (sender → target mapping)
- `backend/app/services/email/sending_service.py` — `EmailSendingService` (queue + SMTP send)
- `backend/app/services/email/imap_connector.py` — `IMAPConnector` (testable IMAP wrapper)
- `backend/app/services/email/smtp_connector.py` — `SMTPConnector` (testable SMTP wrapper)
- `backend/app/services/email/polling_scheduler.py` — APScheduler (5 min interval)
- `backend/app/services/email/sending_scheduler.py` — APScheduler (2 min interval)

### Backend — Updated Services
- `backend/app/services/session_service.py` — `get_session_by_email_thread()`, `create_session()` with email params
- `backend/app/services/message_service.py` — HMAC-signed `session_context` emission with `email_subject` from linked `EmailMessage`
- `backend/app/services/session_context_signer.py` — HMAC-SHA256 signing/verification
- `backend/app/services/agent_share_service.py` — `create_auto_share()` for email senders
- `backend/app/services/user_service.py` — `create_email_user()` for auto user provisioning
- `backend/app/services/input_task_service.py` — `send_email_answer()` for task-originated replies
- `backend/app/services/ai_functions_service.py` — `generate_email_reply()` wrapper

### Backend — AI Functions
- `backend/app/agents/email_reply_generator.py` — `generate_email_reply()` AI function
- `backend/app/agents/prompts/email_reply_generator_prompt.md` — Prompt template

### Backend — Models
- `backend/app/models/email_message.py` — `EmailMessage`, `EmailMessagePublic`
- `backend/app/models/outgoing_email_queue.py` — `OutgoingEmailQueue`, `OutgoingEmailQueuePublic`, `OutgoingEmailStatus`
- `backend/app/models/session.py` — Added: `email_thread_id`, `integration_type`, `sender_email`
- `backend/app/models/input_task.py` — Added: `source_email_message_id`, `source_agent_id`, `SendAnswerRequest`, `SendAnswerResponse`

### Backend — Agent Environment (Session Context)
- `backend/app/env-templates/app_core_base/core/server/active_session_manager.py` — Per-session HMAC-verified context store with TTL cleanup
- `backend/app/env-templates/app_core_base/core/server/routes.py` — `GET /session/context?session_id=X`, `_store_session_context()` helper
- `backend/app/env-templates/app_core_base/core/server/prompt_generator.py` — `build_session_context_section()`
- `backend/app/env-templates/app_core_base/core/server/adapters/claude_code_sdk_adapter.py` — Passes `session_state` to prompt generation
- `backend/app/env-templates/app_core_base/core/scripts/get_session_context.py` — Stdlib-only CLI/import helper

### Frontend — Task Actions
- `frontend/src/routes/_layout/tasks/index.tsx` — "Send Answer" button for email-originated tasks
- `frontend/src/routes/_layout/task/$taskId.tsx` — "Send Answer" button in task detail footer

### Frontend — Session Badges
- `frontend/src/routes/_layout/session/$sessionId.tsx` — Integration type badge (Email/A2A)
- `frontend/src/components/Chat/MessageBubble.tsx` — `integrationTyp` prop
- `frontend/src/components/Chat/MessageList.tsx` — `integrationTyp` prop forwarding

### Tests
- `backend/tests/api/agents/agents_email_integration_test.py` — End-to-end email → session → response flow
- `backend/tests/api/agents/agents_email_task_integration_test.py` — Task mode: email → task → execute → send answer
- `backend/tests/stubs/email_stubs.py` — `StubIMAPConnector`, `StubSMTPConnector`

### Migrations
- `485e7e243dd5` — `email_message` table
- `8a95916ab539` — `outgoing_email_queue` table
- `7aeed6ea3abf` — `session.email_thread_id`, `session.integration_type`, `agent_share.source`
- `f3a1b2c4d5e6` — `session.sender_email`, `agent_email_integration.agent_session_mode`
- `h5c3d4e6f7g8` — `agent_email_integration.process_as`, `input_task` email source fields, `email_message.input_task_id`, `outgoing_email_queue.input_task_id`

## Services & Key Methods

### EmailPollingService (`polling_service.py`)
- `poll_agent_mailbox()` — Connects to IMAP, fetches unread emails, stores in `email_message` table, marks read on IMAP
- `poll_all_enabled_agents()` — Iterates all agents with enabled integration
- `_fetch_unread_emails()` — Queries IMAP for unread messages
- `_parse_email()` — Parses RFC822: headers (From, Subject, Message-ID, In-Reply-To, References), body, attachments metadata, HTML-to-text conversion
- `_is_addressed_to_agent()` — Validates email was addressed to `incoming_mailbox` (prevents processing emails for other recipients sharing IMAP inbox)

### EmailProcessingService (`processing_service.py`)
- `process_incoming_email()` — Routes email to clone/parent, checks readiness, dispatches to session or task path
- `process_pending_emails()` — Retries emails with `pending_clone_creation=True` once clone is ready
- `_process_email_to_session()` — Determines `thread_id`, finds/creates session, injects message, initiates streaming
- `_process_email_to_task()` — Creates `InputTask` with email content, sets `source_email_message_id` and `source_agent_id`
- `_format_email_as_message()` — Formats email into user message with subject, sender, body, attachments info
- `_handle_processing_error()` — Records `processing_error` on `EmailMessage`

### EmailRoutingService (`routing_service.py`)
- `route_email()` → `(target_agent_id, is_ready, session_mode)` — Core routing logic
  - **Owner mode**: Check access → check env status → return `(agent_id, is_ready, OWNER)`
  - **Clone mode**: Find existing clone → if found: check readiness → if not found: check access → check `max_clones` → ensure user → auto-share + clone
- `_find_existing_clone()` — Looks for accepted share + clone for sender+agent combo
- `_check_access_allowed()` — Validates: `allowed_domains`, `access_mode`, `auto_approve_email_pattern`
- `_ensure_user_exists()` — Calls `UserService.create_email_user()` if no account
- `_auto_create_share_and_clone()` — Creates auto-share + clone via `AgentShareService.create_auto_share()`
- `_auto_accept_pending_share()` — Auto-accepts pending shares for incoming email senders
- `_is_clone_ready()` — Checks clone's environment is active and running
- `_match_email_pattern()` — Glob pattern matching for comma-separated patterns

### EmailSendingService (`sending_service.py`)
- `queue_outgoing_email()` — Creates `OutgoingEmailQueue` entry with threading headers
  - Determines recipient: sender_email (owner mode) or clone owner's email (clone mode)
  - Looks up SMTP config via parent agent's integration
- `send_pending_emails()` — Processes queue: SMTP connect → send → mark SENT / retry on failure (max 3)
- `handle_stream_completed()` — Event handler on `STREAM_COMPLETED`: if email session, queues last agent message
- Builds MIME messages with proper `In-Reply-To` and `References` headers

### Schedulers
- `polling_scheduler.py` — APScheduler: `run_email_polling()` every 5 min → `poll_all_enabled_agents()` → process each → retry pending
- `sending_scheduler.py` — APScheduler: `run_email_sending()` every 2 min → `send_pending_emails()`

### AI Reply Generation
- `backend/app/agents/email_reply_generator.py:generate_email_reply()` — Takes original email details + session results, produces professional reply
- `backend/app/services/ai_functions_service.py:generate_email_reply()` — Wrapper using multi-provider cascade
- `backend/app/services/input_task_service.py:send_email_answer()` — Retrieves last agent message or result_summary, generates reply, queues outgoing email

## Session Context Implementation

### Backend Side
- `backend/app/services/message_service.py` — On every stream, emits HMAC-signed `session_context` to agent-env
  - Context includes: `integration_type`, `sender_email`, `email_subject` (fetched from linked `EmailMessage` via `email_thread_id`), `email_thread_id`, `backend_session_id`
- `backend/app/services/session_context_signer.py` — HMAC-SHA256 signing with `AGENT_AUTH_TOKEN`

### Agent-Env Side
- `active_session_manager.py:ActiveSessionManager`
  - `set_session_context()` — Stores HMAC-verified context per `backend_session_id`
  - `get_session_context()` — Retrieves context for a specific session
  - `cleanup_session_context()` — Explicit cleanup on stream end
  - TTL-based cleanup (24h) as fallback safety net
- `routes.py:get_session_context()` — `GET /session/context?session_id=X` (localhost-only, no auth)
  - With `session_id`: per-session lookup (404 if not found)
  - Without: legacy fallback to last-set context
- `routes.py:_store_session_context()` — Helper that stores context via both legacy and per-session APIs
- `prompt_generator.py:PromptGenerator.build_session_context_section()` — Generates "Session Context (Server-Verified, Read-Only)" system prompt section
- `scripts/get_session_context.py` — Stdlib-only CLI/import for agent scripts to query context

### Legacy Compatibility
- Legacy single-context API (`set_current_context`/`get_current_context`/`clear_context`) retained for backward compatibility
- New per-session API is primary; legacy API updates alongside it

## Frontend Components

### Task "Send Answer" Actions
- `frontend/src/routes/_layout/tasks/index.tsx` — "Send Answer" button with Mail icon on email-originated tasks (has `source_email_message_id`)
  - Visible when task status is `completed` or `error`
  - Shows loading state during AI reply generation
  - Toast notification on success/error
- `frontend/src/routes/_layout/task/$taskId.tsx` — Same "Send Answer" button in task detail footer, next to Execute button

### Session Integration Badges
- `frontend/src/routes/_layout/session/$sessionId.tsx` — Badge next to mode indicator:
  - Email sessions: indigo "Email" badge with Mail icon
  - A2A sessions: purple "A2A" badge with Plug icon
- `frontend/src/components/Chat/MessageBubble.tsx` / `MessageList.tsx` — `integrationTyp` prop forwarded for potential per-message indicators

## Event Bus Integration

Scheduler and event registration in `backend/app/main.py`:
- `start_email_polling_scheduler()` — Startup: every 5 min IMAP poll
- `start_email_sending_scheduler()` — Startup: every 2 min SMTP queue flush
- `event_service.register_handler(EventType.STREAM_COMPLETED, EmailSendingService.handle_stream_completed)` — Auto-queue reply on stream completion
- `shutdown_email_polling_scheduler()` / `shutdown_email_sending_scheduler()` — Cleanup on shutdown
