# Email Sessions — Technical Details

Covers only the threading and outgoing-queue mechanics that survive per-agent
Email Integration's deletion. The routing/binding/ingestion pipeline itself is
documented in
[Server Channels — Technical Reference](../server_channels/server_channels_tech.md);
the transport-specific adapter mechanics are documented in
[Email Integration — Technical Details](email_integration_tech.md).

## File Locations

### Backend — Services
- `backend/app/services/server_channels/adapters/email.py` — thread-key
  composite build/parse (`build_reply_thread_key`, `parse_reply_thread_key`),
  `poll()`, `send_message()`. See
  [Email Integration tech](email_integration_tech.md#the-emailchanneladapter-adaptersemailpy).
- `backend/app/services/server_channels/channel_outbound_service.py` —
  `_binding_thread_key(binding, channel)`: the single seam that derives a
  transport-facing thread key from a `ChannelThreadBinding`, and the caller of
  `build_reply_thread_key` for a polled transport. See
  [Thread-key composite](#the-thread-key-composite-_binding_thread_key) below.
- `backend/app/services/email/sending_service.py` — `EmailSendingService`,
  send half only. See
  [Email Integration tech](email_integration_tech.md#emailsendingservice--send-half-only-sending_servicepy).
- `backend/app/services/email/polling_service.py` — retained IMAP/MIME
  mechanics, `format_email_as_message()`.
- `backend/app/services/sessions/message_service.py` —
  `_build_session_context()`: the `email_subject` enrichment, gated on
  `integration_type == "email"`. See
  [The session-context gap](#the-session-context-gap) below.

### Backend — Models
- `backend/app/models/email/email_message.py` — `EmailMessage` (`agent_id`
  nullable since migration `907124e812c5`).
- `backend/app/models/email/outgoing_email_queue.py` — `OutgoingEmailQueue`,
  `OutgoingEmailQueuePublic`, `OutgoingEmailStatus`. Unchanged.
- `backend/app/models/server_channels/channel_thread_binding.py` —
  `ChannelThreadBinding`, in particular `thread_key` (the stored, bare root
  Message-ID) and `last_external_message_id` (the most recent inbound
  message id on the thread — already maintained by the shared inbound
  pipeline, reused here rather than added to).

### Migrations
- `907124e812c5` — `email_message.agent_id` made nullable.
- (Threading/queue schema itself — `email_message`, `outgoing_email_queue` —
  predates this phase and is unchanged.)

## The thread-key composite (`_binding_thread_key`)

`send_message(channel, thread_key, text)` on the shared `ChannelAdapter`
contract has no parameter for reply-threading context (`In-Reply-To`,
`References`), and there is no room to add one without changing every
adapter's signature. The seam settled decision §2.7 uses instead:
`channel_outbound_service._binding_thread_key(binding, channel)` builds a
**composite** thread key for a polled transport only —
`"<root-message-id>|<last-message-id>"` — via
`adapters.email.build_reply_thread_key`. The **stored**
`binding.thread_key` is never touched; it stays the bare root and remains the
unique lookup key everywhere else in the pipeline.

Two things make this helper correct, and both are load-bearing:

1. **It reads `binding.last_external_message_id` inside the same `try` block
   that reads `binding.thread_key`.** Every path into delivery arrives after
   a `db.commit()`, which expires the SQLAlchemy instance — a subsequent
   attribute read is a lazy reload, and a binding deleted concurrently raises
   `ObjectDeletedError`. Reading the two fields in the same guarded block is
   what keeps the helper **total** (never raises; returns `None` on failure)
   rather than turning an honest declined delivery into a crash on the
   delivery path.
2. **Only a `polled` transport gets the composite.** `_binding_thread_key`
   resolves the channel's transport via `get_transport(channel.channel_type)`
   and only calls `build_reply_thread_key` when
   `transport.inbound_mode == "polled"`. A webhook transport (Google Chat)
   gets the bare thread key back unchanged — its `thread_key` is already a
   complete address, and building a composite for it would be meaningless.

`EmailChannelAdapter.send_message` is the corresponding **parser**:
`parse_reply_thread_key(thread_key)` splits the composite back into
`(root_id, last_id)` — see
[Email Integration tech](email_integration_tech.md#outbound-send_messagechannel-thread_key-text)
for the exact split rule (`">|<"`, not a bare `|`).

## The session-context gap

`message_service._build_session_context` (`backend/app/services/sessions/message_service.py`):

```python
if session_db.integration_type == "email":
    ...
    context["email_subject"] = initiating_email.subject
```

Every channel-routed email session is stamped `integration_type =
"channel_email"` (the `channel_<type>` convention `ChannelOutboundService`
gates outbound delivery on), so this equality check **never matches** for a
channel session. `EmailChannelAdapter.record_routing_outcome` correctly
stamps the initiating `EmailMessage.session_id` once routing succeeds — so
the by-`session_id` lookup this code performs would actually find a row if
it ran — but the `integration_type` gate above it never lets that happen.
`sender_email` and `email_thread_id` are unconditional fields on the same
context dict and are unaffected; only `email_subject` is silently skipped.

This is a genuine gap left by the refactor (the check was written for the
pre-channel `integration_type` value and never updated), not a documented
trade-off. It is called out here, and in
[Email Integration tech](email_integration_tech.md#a-real-non-obvious-gap-email_subject-context),
rather than left to be rediscovered later. `EmailPollingService.format_email_as_message`'s
own docstring has been corrected to stop claiming session context is the
authoritative source for subject/sender metadata on this path — see that
function for the current, accurate statement.

## Integration Points

- [Email Integration — Technical Details](email_integration_tech.md) — the
  transport adapter, poller, and deleted-capability inventory.
- [Server Channels — Technical Reference](../server_channels/server_channels_tech.md) —
  `ChannelThreadBinding`, `ChannelInboundService`, `ChannelRoutingService`,
  `ChannelOutboundService` — the shared pipeline every channel (including
  email) runs through.
- [Mail Servers — Technical Details](mail_servers_tech.md) — the SMTP
  credential resolution `EmailSendingService` performs per queue entry.
