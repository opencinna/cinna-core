# Email Integration — Technical Details

Email is a **Server Channels transport** (`channel_type="email"`). Most of the
pipeline (routing, thread bindings, admin CRUD, availability policy) lives in
[Server Channels — Technical Reference](../server_channels/server_channels_tech.md);
this file covers only what is specific to the email transport. Per-agent
Email Integration (models, services, routes, frontend, tests) is **deleted
outright** — see [Deletions](#deletions-phase-4) below.

## File Locations

### Backend — Models (retained)
- `backend/app/models/email/email_message.py` — `EmailMessage`, `EmailMessagePublic`.
  `agent_id` is now **nullable** (migration `907124e812c5`) — see
  [Database Schema](#database-schema).
- `backend/app/models/email/outgoing_email_queue.py` — `OutgoingEmailQueue`,
  `OutgoingEmailQueuePublic`, `OutgoingEmailStatus`. Unchanged.
- `backend/app/models/email/mail_server_config.py` — `MailServerConfig`,
  now server-scoped (`user_id` dropped). See [Mail Servers — Technical Details](mail_servers_tech.md).

### Backend — Services (retained, adapted)
- `backend/app/services/server_channels/adapters/email.py` —
  `EmailChannelAdapter(PolledChannelTransport)`: the transport itself. Config
  validation, `poll()`, `send_message()`, thread-key composite
  build/parse, `record_routing_outcome()`. See
  [The `EmailChannelAdapter`](#the-emailchanneladapter-adaptersemailpy) below.
- `backend/app/services/server_channels/channel_poll_service.py` —
  `ChannelPollService.poll_enabled_channels(db)`: drives every enabled polled
  channel through one fetch-and-process pass. Transport-agnostic — it
  enumerates polled channel types from the adapter registry, not a hardcoded
  email-specific list.
- `backend/app/services/server_channels/channel_poll_scheduler.py` — the
  APScheduler wrapper (60s interval), `TESTING`-gated. Submits the poll
  coroutine to the main event loop, the same pattern every other scheduler in
  this codebase follows.
- `backend/app/services/email/polling_service.py` — `EmailPollingService`:
  now a bag of **IMAP/MIME mechanics only**, with no driver of its own.
  `_fetch_unread_emails`, `_parse_email`, `_extract_body`,
  `_extract_attachment_metadata`, `_decode_header_value`, `_mark_email_read`,
  `_is_addressed_to_channel` (renamed from `_is_addressed_to_agent`), and
  `format_email_as_message` are called by `EmailChannelAdapter`, which is
  their only caller.
- `backend/app/services/email/sending_service.py` — `EmailSendingService`:
  now **the send half only**. Enqueueing is `ChannelOutboundService`'s job via
  `EmailChannelAdapter.send_message`; `send_pending_emails` drains
  `outgoing_email_queue` unchanged, resolving SMTP configuration per entry
  through `entry.session_id` → `ChannelThreadBinding` → `ServerChannel.config`
  rather than through a per-agent integration.
- `backend/app/services/email/imap_connector.py` / `smtp_connector.py` —
  unchanged, testable connector wrappers.
- `backend/app/services/email/mail_server_service.py` — `MailServerService`.
  See [Mail Servers — Technical Details](mail_servers_tech.md) for its
  channel-deletion-guard changes.

### Backend — Routes (retained)
- `backend/app/api/routes/mail_servers.py` — superuser-only CRUD. See
  [Mail Servers — Technical Details](mail_servers_tech.md).
- Channel CRUD, webhook (n/a for email), and admin routes are all
  `backend/app/api/routes/server_channels.py` — see
  [Server Channels — Technical Reference](../server_channels/server_channels_tech.md#api-endpoints).
  There is no email-specific route file any more.

### Migrations (Phase 4)
- `ca0192122e0c` — `email becomes a server channel`: drops
  `agent_email_integration`; drops `mail_server_config.user_id`; drops
  `input_task.source_email_message_id` and `input_task.source_agent_id`;
  makes `server_channel.webhook_token` nullable.
- `907124e812c5` — `email messages are stored before routing`: makes
  `email_message.agent_id` nullable.

## Deletions (Phase 4)

Deleted outright, no compatibility shim:

**Models / tables**
- `app/models/email/agent_email_integration.py` and the
  `agent_email_integration` table — with it, `EmailAccessMode`,
  `AgentSessionMode`, `EmailProcessAs`, `EmailCloneShareMode`,
  `ProcessEmailsResult`, and every field on the deleted table (`access_mode`,
  `process_as`, `auto_approve_email_pattern`, `allowed_domains`,
  `max_clones`, `clone_share_mode`, `agent_session_mode`,
  `incoming_server_id`/`outgoing_server_id`/`incoming_mailbox`/`outgoing_from_address`).
- `input_task.source_email_message_id`, `input_task.source_agent_id`, and the
  `SendAnswerRequest`/`SendAnswerResponse` models around them.

**Services**
- `app/services/email/integration_service.py` (`EmailIntegrationService`)
- `app/services/email/routing_service.py` (`EmailRoutingService` — clone/owner
  routing, `_find_existing_clone`, `_check_access_allowed`,
  `_ensure_user_exists`, `_auto_create_share_and_clone`,
  `_auto_accept_pending_share`, `_is_clone_ready`, `_match_email_pattern`) —
  every one of these has a Server Channels equivalent (`ChannelPolicyService`,
  `ChannelRoutingService`, `UserService.create_external_user`,
  `app.services.common.email_patterns.match_email_pattern`)
- `app/services/email/processing_service.py` (`EmailProcessingService`) — the
  routing/session/task orchestration it did is now split between
  `EmailChannelAdapter` (inbound mapping) and the shared
  `ChannelInboundService`/`ChannelRoutingService` (routing, ingestion)
- `app/services/tasks/input_task_service.py:send_email_answer()` — the "Send
  Answer" AI reply generation
- `app/agents/email_reply_generator.py:generate_email_reply()` and
  `app/agents/prompts/email_reply_generator_prompt.md`
- `app/services/ai_functions/ai_functions_service.py:generate_email_reply()`
  wrapper

**Routes**
- `app/api/routes/email_integration.py` and its registration in `api/main.py`
- `POST /api/v1/tasks/{id}/send-answer`

**Frontend**
- `components/Agents/EmailIntegrationCard.tsx`
- `components/Agents/EmailAccessModal.tsx`
- `components/Agents/EmailConnectionModal.tsx`
- `components/Agents/EmailSessionsModal.tsx`
- the Email Integration card's slot in `components/Agents/AgentIntegrationsTab.tsx`
- the "Send Answer" buttons in `routes/_layout/tasks/index.tsx` and
  `routes/_layout/task/$taskId.tsx`

**Tests**
- `backend/tests/api/agents/integrations/agents_email_integration_test.py` (deleted) <!-- nocheck -->
- `backend/tests/api/agents/integrations/agents_email_task_integration_test.py` (deleted) <!-- nocheck -->

**Kept**: `EmailMessage`, `OutgoingEmailQueue`, `MailServerConfig`
(re-scoped), `imap_connector`, `smtp_connector`, `sending_service`,
`sending_scheduler`, and the pure IMAP/MIME mechanics on `EmailPollingService`.

## Database Schema

### `email_message` (retained, one column relaxed)

Unchanged fields except:

| Field | Type | Description |
|-------|------|--------------|
| `agent_id` | UUID, FK → `agent.id` **ON DELETE CASCADE, nullable** | **`NULL` until routing stamps it, and `NULL` forever for mail that was never routed.** Every arrival is stored on arrival, before whitelist/policy/routing has run — see [Every arrival is recorded, before anything decides](email_integration.md#every-arrival-is-recorded-before-anything-decides) in the business-logic doc. `EmailChannelAdapter.record_routing_outcome` stamps this (and `session_id`) after the fact, thread-wide, once routing succeeds. |
| `input_task_id` | UUID, FK → `input_task.id` ON DELETE SET NULL | Column retained for legacy rows written before this phase; nothing writes it any more. |

### `outgoing_email_queue` (unchanged)

Same shape as before this phase. `agent_id` on a new row is always the agent
the channel bound the thread to (`binding.agent_id` — the identity owner's
agent on an identity-routed thread); `input_task_id` is `NULL` on every row
this phase's code writes and is consulted only as a legacy fallback in
`EmailSendingService._resolve_responsible_user`.

### `mail_server_config` — see [Mail Servers — Technical Details](mail_servers_tech.md#database-schema)

### `agent_email_integration` — **dropped**

## The `EmailChannelAdapter` (`adapters/email.py`)

`EmailChannelAdapter(PolledChannelTransport)` — `channel_type = "email"`.

### Capabilities

```python
ChannelCapabilities(
    supports_progress_updates=False,   # would mail 3 separate notices otherwise
    supports_message_edit=False,
    supports_markdown=False,           # replies go out as text/plain
    max_message_chars=None,            # SMTP caps size, not chars
    supports_sync_reply=False,         # THE important one — see below
    inbound_mode="polled",
    needs_webhook_token=False,
    needs_outbound_credentials=False,  # credential is the referenced SMTP server
)
```

`supports_sync_reply=False` is why every denial in the inbound pipeline
reaches the sender as nothing at all (decided behaviour, not a gap — mailing
a decline is a probing oracle and a spam amplifier).

### Config

Non-secret, on `ServerChannel.config`:

```json
{
  "incoming_server_id": "<mail_server_config uuid, type=imap>",
  "outgoing_server_id": "<mail_server_config uuid, type=smtp>",
  "incoming_mailbox": "support@corp.com",
  "from_address": "support@corp.com"
}
```

`validate_config` is a pure shape check (both server ids parse as UUIDs, both
addresses contain `@`). `validate_config_references(db, config)` additionally
checks both referenced `MailServerConfig` rows exist and are the right
`server_type` (`IMAP` / `SMTP`) — this needs a database session, which is why
it is a separate method from `validate_config` (see `adapters/base.py`'s
docstring on the split).

`has_outbound_credentials(channel)` is **overridden**: it reports whether
`outgoing_server_id` is set in `config`, not whether
`ServerChannel.encrypted_secrets` is populated (which is always empty for
this transport — the credential lives in `mail_server_config`,
backend-only). The adapter registry refuses to import if a transport declares
`needs_outbound_credentials=False` without overriding this method.

### Inbound: `poll(channel)`

1. Resolves the incoming `MailServerConfig` and its decrypted password.
2. Runs the blocking IMAP fetch (`_fetch_mailbox`) on a worker thread via
   `anyio.to_thread.run_sync` — `imaplib` is blocking and this coroutine runs
   on the main event loop.
3. `_fetch_mailbox` reuses `EmailPollingService._fetch_unread_emails` /
   `_parse_email` / `_is_addressed_to_channel` / `_mark_email_read`
   unmodified — it is a driver over the retained mechanics, not a second IMAP
   implementation.
4. `_store_arrivals` persists every accepted arrival as an `EmailMessage` row
   **before** classification — see
   [Database Schema](#database-schema) above — and drops only a redelivery of
   mail whose stored row already has `agent_id` set (an unrouted redelivery is
   still returned, so the pipeline's own recovery paths can retry it).
5. `_to_inbound_message` maps each parsed mail onto `ChannelInboundMessage`:
   `sender_email`/`external_user_id` from `From:` (spoofable — documented in
   the adapter's own docstring), `thread_key` = the **root** Message-ID
   (`References[0]` → `In-Reply-To` → own `Message-ID`), `external_message_id`
   = the message's own `Message-ID`, `text` = `EmailPollingService.format_email_as_message`.

A transient fetch failure (mail server down, credentials rejected) is logged
and answered with an empty list — never returned as an inbound event, so the
next poll tick retries cleanly.

### `record_routing_outcome(db, channel, *, thread_key, agent_id, session_id)`

Stamps `agent_id`/`session_id` onto every still-unstamped `EmailMessage` row
whose stored thread root matches `thread_key` (via `_stored_root_expr`, a SQL
expression mirroring the Python root-derivation rule). Thread-wide rather than
message-wide: this heals an arrival that was stored before its thread had an
agent at all (always true of the first message of a thread; may be true of
several parked messages). This is also what makes
`message_service._build_session_context`'s by-`session_id` `EmailMessage`
lookup findable — see
[A real, non-obvious gap: `email_subject` context](#a-real-non-obvious-gap-email_subject-context)
below for why that lookup still never fires on a channel session.

### Outbound: `send_message(channel, thread_key, text)`

Does **not** send. `thread_key` arrives as the composite
`"<root>|<last>"` built by `build_reply_thread_key` (see
[Server Channels tech — `_binding_thread_key`](../server_channels/server_channels_tech.md)
for where that composite is constructed). `parse_reply_thread_key` splits it
back into `(root_id, last_id)`; the split is on the literal substring
`">|<"`, not on the bare `|`, because `|` is legal RFC 5322 `atext` and can
legitimately appear inside a Message-ID — both halves are always
angle-bracket-normalized first, so `">|<"` is unambiguous.

The method looks up the `ChannelThreadBinding` by `(channel.id, root_id)`,
resolves the recipient as **the bound platform account's email**
(`binding.user_id` → `User.email`) — deliberately *not* the `From:` address
the original mail arrived with, since that header is spoofable and replying
to it would let a forged sender redirect an agent's answer — and enqueues an
`OutgoingEmailQueue` row with `in_reply_to = last_id or root_id` and
`references` built by `_references_chain(root_id, last_id)`. Raises
`ChannelSendError` for every case where the reply cannot be addressed
(missing config, no binding, no session, no resolvable recipient) — the
caller (`ChannelOutboundService._deliver`) records that on the binding and in
the debug feed.

Returns `None` always: unlike a chat message id, an email's `Message-ID` is
assigned by the SMTP server at delivery time, minutes after this call
returns, and nothing on the channel path consumes the return value anyway.

### Thread-key helpers (module-level functions, not adapter methods)

- `build_reply_thread_key(root_message_id, last_message_id) -> str` — joins
  root and last into the composite, or returns the bare root if there is no
  last id yet or it equals the root.
- `parse_reply_thread_key(thread_key) -> tuple[str, str | None]` — the
  inverse. A key the parser cannot recognize is returned as `(thread_key,
  None)` rather than raising — the worst outcome is a reply sent without
  threading headers (renders as a new conversation in one mail client),
  which is strictly better than refusing to send at all.

### `get_setup_instructions`

Returns `webhook_url=None`-aware admin copy: "Polled IMAP (no inbound URL)",
and a `"Sender verification"` detail line stating the `From:`-header trust
tier explicitly, plus setup steps that state the same thing and note that
mail for a different recipient in the same inbox is ignored (so one IMAP
account may serve several channels).

## `ChannelPollService` / `channel_poll_scheduler`

`ChannelPollService.poll_enabled_channels(db)` (`channel_poll_service.py`):
- Enumerates channel types via `channel_types_with_inbound_mode("polled")`
  from the adapter registry — not a hardcoded email-specific list, so a
  second polled transport needs no change here.
- Queries enabled `ServerChannel` rows of those types only — a disabled
  channel is never polled, mirroring the webhook route's enabled-only lookup.
- Per channel: calls `adapter.poll(channel)`, then feeds every returned
  message into `ChannelInboundService.process_inbound` (the shared,
  post-verification pipeline entry point — the same one a webhook reaches
  after `verify_inbound` succeeds).
- Failure isolation is per-channel and then per-message: one channel's IMAP
  outage, or one message the pipeline chokes on, does not stop the batch.

`channel_poll_scheduler.py` is the timer wrapper: `POLL_INTERVAL_SECONDS = 60`,
`TESTING`-gated (load-bearing — a poller running under test opens real IMAP
connections from a worker thread and would make unrelated suites flaky),
APScheduler on a worker thread submitting the async poll to the main event
loop via `asyncio.run_coroutine_threadsafe` (the same pattern every other
scheduler in this codebase uses, since the pipeline's fire-and-forget tasks
must outlive the scheduler job that started them). **No leader election** —
documented limitation, same as `channel_pending_scheduler`; do **not** copy
the advisory-lock leader pattern from `model_discovery_scheduler` (known
connection leak on pooled connections).

## `EmailSendingService` — send half only (`sending_service.py`)

- `_resolve_channel(db, entry)` — `entry.session_id` → `ChannelThreadBinding`
  → `ServerChannel`. `None` (permanent failure) for an entry with no session,
  no binding, or a deleted channel.
- `_resolve_responsible_user(db, entry)` — `entry.agent_id` → `Agent.owner_id`
  → `User`; the account whose `email_confirmed` state gates the send
  (outbound-email confirmation gate, unrelated to and pre-dating this
  refactor). `entry.input_task_id` is consulted first only for legacy rows —
  nothing enqueues one any more.
- `_send_single_email` resolves SMTP config as
  `channel.config["outgoing_server_id"]` / `["from_address"]` rather than
  through a per-agent integration; every resolution failure (no channel bound,
  missing outgoing config, malformed server id, missing SMTP server) is
  recorded as a **permanent** failure on the row via `_mark_failed`, never
  retried — a misconfigured channel does not improve with a retry.
- `send_pending_emails`, MIME building, and the retry loop (`MAX_RETRIES = 3`)
  are otherwise unchanged from before this phase.

## A real, non-obvious gap: `email_subject` context

`message_service._build_session_context` enriches the agent-facing
`session_context` (system-prompt injection + `GET /session/context`) with
`email_subject`, fetched from the linked `EmailMessage`, but only when
`session_db.integration_type == "email"` **literally**. A channel-originated
email session is always stamped `integration_type = "channel_email"` (the
`channel_<type>` convention every Server Channels session uses), so **this
branch never fires for a single channel-routed email session** — the subject
never reaches the system-prompt injection or the `GET /session/context`
endpoint for email arriving through the channel. The subject still reaches
the agent through the message text itself, since
`EmailPollingService.format_email_as_message` includes a `Subject:` line —
it is just not available through the server-verified session-context
channel any more. This is a genuine behavioural gap introduced by this
phase (the equality check was never updated for the new `integration_type`
value) rather than a documented design choice; it is called out here rather
than silently carried forward. See also the corrected docstring on
`EmailPollingService.format_email_as_message`, which used to claim the
opposite.
