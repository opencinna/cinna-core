# Email Sessions

## Purpose

Since Phase 4 of the channels & identity unification refactor, incoming
email no longer has its own session/task **processing modes** — it is routed
by [Server Channels](../server_channels/server_channels.md) the same way a
Google Chat message is. What survives, and what this document now covers, is
what is genuinely email-specific about a channel-routed email conversation:
**threading continuity** (mapping `Message-ID` / `In-Reply-To` / `References`
onto a persistent conversation) and the **durable outgoing email queue** that
delivers replies. Session *modes* (`install` / `owner`) and processing
*modes* (`new_session` / `new_task`) are gone — see
[What is gone](#what-is-gone) below.

## Core Concepts

- **Email Threading** — mapping the email's `Message-ID` / `In-Reply-To` /
  `References` headers onto a `ChannelThreadBinding`'s `thread_key`, which is
  always the **root** Message-ID of the conversation, never the latest.
- **Outgoing Email Queue** — the durable, retried SMTP delivery queue every
  agent reply to an email channel goes through (`OutgoingEmailQueue`).
- **Session Context** — HMAC-verified metadata (sender, subject, session id)
  the platform can inject into an agent's system prompt. Still exists as
  infrastructure, but its `email_subject` enrichment does not currently reach
  a channel-routed email session — see
  [A note on session context](#a-note-on-session-context) below.
- **Channel Thread Binding** — the shared Server Channels concept that
  replaces "session mode": one binding pins one email thread to one platform
  `(user, agent, session)` triple, built the same way a Google Chat thread
  binding is.

## User Stories / Flows

### 1. First message on a new thread

1. The channel poll scheduler fetches the mail (see
   [Email Integration](email_integration.md)).
2. The sender is whitelist-checked and resolved to (or auto-registered as) a
   platform user.
3. Routing runs: the sender's own agents first, then the server's
   auto-install catalog. If an auto-install bundle matches, it is installed
   for that sender and the message is parked until the environment is ready.
4. A `ChannelThreadBinding` is created, keyed on the thread's root
   `Message-ID`. Once the target agent's session exists, the email body is
   injected as the first user message and the agent begins streaming.
5. On stream completion, the agent's last message is enqueued into
   `OutgoingEmailQueue` with `In-Reply-To` / `References` headers pointing at
   the message that was just answered.
6. The existing sending scheduler drains the queue over SMTP, with up to
   three retries per entry.

### 2. Continuing a thread

1. The sender replies to the agent's email.
2. The reply's `In-Reply-To` (or the first entry of its `References` chain)
   resolves to the same thread root, so the existing
   `ChannelThreadBinding` is found and the message is fed straight into the
   already-existing session — no re-routing.
3. The new agent reply is queued the same way, referencing the **latest**
   inbound message id (not the thread root) in its own `In-Reply-To`, while
   the `References` chain carries both.

### 3. A message that cannot be delivered

1. The agent's reply cannot be sent — the channel has no `from_address`
   configured, the referenced SMTP server no longer exists, or SMTP itself
   rejects the send.
2. The queue entry accumulates `retry_count`; after the third failed attempt
   it is marked permanently failed and the failure is recorded on the
   binding.
3. There is no notice sent to the sender about the failure — a polled
   transport has no side channel for that — so a lost reply can currently
   only be discovered by an admin looking at the debug feed, or by the
   sender asking again.

## What is gone

Deleted with the per-agent Email Integration, no replacement:

- **Session Mode** (`install` / `owner`) — where an email session ran used
  to be a per-agent choice. Every channel-routed email session now runs
  exactly where Server Channels routing decides: on the sender's own agent,
  or — with identity routing opted in — on an identity owner's agent (see
  [Server Channels — Identity routing](../server_channels/server_channels.md#identity-routing-whose-thread-whose-workspace)).
  There is no "shared owner environment" mode any more.
- **Processing Mode** (`new_session` / `new_task`) — every channel-routed
  email now auto-responds; there is no "create a reviewable task instead"
  path. See [Input Tasks](../input_tasks/input_tasks.md) — the
  email-originated task flow described there previously is gone.
- **"Send Answer"** — the manually-triggered, AI-generated email reply from
  a completed task's results. Gone along with task mode itself.
- **Pending clone creation** — replaced by the shared auto-install parking
  mechanism (`ChannelThreadBinding.status = pending_install`) every channel
  uses while an environment is still building.

## Business Rules

### Threading rules

- **The binding's `thread_key` is always the thread's root Message-ID** —
  `References[0]` if present, else `In-Reply-To`, else the message's own
  `Message-ID`. Never the latest message in the thread. Binding on the
  latest instead would open a fresh binding on every reply — a symptom that
  reads as "the agent forgot the conversation," not as a threading bug.
- The **stored** binding's `thread_key` is exactly that root id — nothing
  else. The reply headers a sender's mail client needs (`In-Reply-To`,
  `References`) are derived separately, at send time, from the binding's
  `last_external_message_id` (already tracked by the inbound pipeline) — see
  the tech doc for the composite key mechanics.
- A first message with no `References`/`In-Reply-To` is the root of its own
  thread by construction (its own `Message-ID` becomes the `thread_key`).

### Outgoing queue rules

- Every agent reply on an email channel is enqueued into
  `OutgoingEmailQueue`, never sent synchronously — this is the one channel
  transport with a durable, retried outbound path (Google Chat's outbound
  stays best-effort).
- SMTP configuration is resolved **per queue entry**, through
  `entry.session_id` → `ChannelThreadBinding` → the bound channel's `config`
  — never through a per-agent setting, since none exists any more.
- Up to three retry attempts; a permanently failed entry keeps its
  `last_error` and is never retried again, but it is also never silently
  dropped.
- The reply's recipient is always the platform account's own email address
  (resolved from the binding), never the raw `From:` header the original
  mail arrived with — the header is spoofable, and replying to it would let
  a forged sender redirect an agent's answer to somewhere else.

### A note on session context

The platform still has session-context infrastructure (HMAC-signed,
system-prompt-injected metadata, plus a `GET /session/context` endpoint for
agent scripts) and it still populates `sender_email` and `email_thread_id`
for a channel-routed email session. **`email_subject` currently does not
reach that context on a channel-routed email session** — the enrichment code
only fires when a session's `integration_type` is literally `"email"`, and
every channel-routed email session is stamped `"channel_email"` instead. This
is a real gap left by this refactor, not a deliberate design choice; the
subject still reaches the agent through the message text itself (the
formatted email includes a `Subject:` line), just not through the
server-verified session-context channel. See
[Email Integration — Technical Details](email_integration_tech.md#a-real-non-obvious-gap-email_subject-context)
for the exact code location.

## Architecture Overview

```
channel_poll_scheduler (60s, TESTING-gated)
    → ChannelPollService.poll_enabled_channels(db)
        → EmailChannelAdapter.poll(channel)
            → IMAP fetch → parse → store EmailMessage (agent_id NULL) → filter redeliveries
        → ChannelInboundService.process_inbound(...) per message
            → whitelist / channel policy / user resolution
            → existing binding?  → feed straight into the bound session
            → new thread?        → ChannelRoutingService.decide(...) (Pass 1 / Pass 2)
                                  → bind (root Message-ID) → install if needed → ingest

STREAM_COMPLETED (agent reply)
    → ChannelOutboundService._deliver
        → EmailChannelAdapter.send_message(channel, composite_thread_key, text)
            → resolve binding → resolve recipient (platform account email)
            → enqueue OutgoingEmailQueue (In-Reply-To / References set)

sending_scheduler (2 min, pre-existing, unchanged)
    → EmailSendingService.send_pending_emails(db)
        → resolve channel via entry.session_id → ChannelThreadBinding → ServerChannel
        → SMTP connect → send → mark sent / retry (max 3)
```

## Integration Points

- [Email Integration](email_integration.md) — the parent feature: the
  transport, the trust model, the removed capabilities.
- [Server Channels](../server_channels/server_channels.md) — routing,
  bindings, admin policy — the shared machinery this document assumes.
- [Mail Servers](mail_servers.md) — the IMAP/SMTP credentials the poll and
  send paths use, resolved per channel rather than per agent.
- ~~[Input Tasks](../input_tasks/input_tasks.md)~~ — the email-originated
  task flow previously documented there is gone; see
  [What is gone](#what-is-gone).
- ~~[Agent Activities](../agent_activities/agent_activities.md)~~ — the
  `email_task_incoming` / `email_task_reply_pending` activities that used to
  fire for email-originated tasks no longer fire, since task mode itself is
  gone.
