---
feature: flow_message_ingress
domain: flows
one_liner: "Follows one inbound message from any entry point — web chat, server channels, A2A, App MCP, webhooks or triggers — into a turn in an agent session and the reply back out."
primary_label: flow
---
# Message Ingress

## Purpose

Something arrives with text in it, and an agent has to answer. The "something" is a person typing in
the web chat, a Google Chat message, an email in a polled mailbox, an A2A JSON-RPC call from another
agent, a Cinna Desktop request, an `send_message` tool call on the App MCP server, an HTTP webhook, a
cron-fired schedule, or a task the user pressed *Execute* on. This flow follows that message from the
moment it hits the process to the moment the agent's reply is standing in the surface it came from —
across a dozen features that each own one slice of the path.

The path has a wide mouth and a narrow throat. Every door does its own transport authentication and
its own admission policy, and then almost all of them converge on `ChannelIngestionService`
(`backend/app/services/sessions/channel_ingestion_service.py`), which turns "who is talking" into a
`Session` row, and on `SessionService.send_session_message` /
`SessionService.initiate_stream`, which turn a message into a stream against the agent's container.
Outbound is deliberately *not* unified: the reply leaves through Socket.IO, an SSE frame, an A2A
`Task`, an MCP tool result, a Google Chat message patch, or an SMTP queue row, and each of those
lives in its own module. Agent selection is treated here as one stage — its internals are in
[Routing & identity chain](routing_identity_chain.md).

## Participants

| Feature | Role in this flow | Module |
|---|---|---|
| [chat_windows](../application/chat_interface/chat_windows.md) | Web-UI door: sends the message, subscribes to the stream room | `frontend/src/hooks/useSessionStreaming.ts` |
| [agent_sessions](../application/agent_sessions/agent_sessions.md) | Session + message primitives, environment readiness, streaming | `backend/app/services/sessions/session_service.py` |
| [channel ingestion](../application/agent_sessions/channel_ingestion.md) | The narrow throat: access gate, resolve-or-create, stamping | `backend/app/services/sessions/channel_ingestion_service.py` |
| [server_channels](../application/server_channels/server_channels.md) | Webhook / polled / authenticated transports and their policy | `backend/app/services/server_channels/channel_inbound_service.py` |
| [email_integration](../application/email_integration/email_integration.md), [mail_servers](../application/email_integration/mail_servers.md) | Polled IMAP transport, mailbox credentials | `backend/app/services/email/polling_service.py` |
| [email_sessions](../application/email_integration/email_sessions.md) | Threading key and the durable outbound queue | `backend/app/services/email/sending_service.py` |
| [a2a_protocol](../application/a2a_integration/a2a_protocol/a2a_protocol.md) | JSON-RPC door, token scopes, event→A2A mapping | `backend/app/services/a2a/a2a_request_handler.py` |
| [external_agent_access](../application/external_agent_access/external_agent_access.md), [desktop_auth](../application/desktop_auth/desktop_auth.md) | Native-client door on a user JWT, `agent` vs `identity` targets | `backend/app/services/external/external_a2a_request_handler.py` |
| [app_mcp_server](../application/app_mcp_server/app_mcp_server.md) | One `send_message` tool per user, streamed as MCP notifications | `backend/app/services/app_mcp/app_mcp_request_handler.py` |
| [identity_routing](../application/identity_routing/identity_routing.md) | The one case where the session is created in someone else's space | `backend/app/services/identity/identity_service.py` |
| [agent_webhooks](../agents/agent_webhooks/agent_webhooks.md) | Unauthenticated-by-JWT HTTP trigger, session or script | `backend/app/services/agents/agent_webhook_service.py` |
| [task_triggers](../application/input_tasks/task_triggers.md), [input_tasks](../application/input_tasks/input_tasks.md) | Cron / date / webhook triggers and human task execution | `backend/app/services/tasks/input_task_service.py` |
| [agent_schedulers](../agents/agent_schedulers/agent_schedulers.md) | Cron-fired prompt sessions and script triggers | `backend/app/services/agents/agent_schedule_scheduler.py` |
| [agent_handover](../agents/agent_handover/agent_handover.md) | Agent-initiated delegation, via the task path | `backend/app/services/agents/agent_service.py` |
| [guest_sharing](../agents/guest_sharing/guest_sharing.md) | Anonymous visitor door | `backend/app/api/routes/sessions.py` |
| [agent_environment_core](../agents/agent_environment_core/agent_environment_core.md) | The container that actually answers (`POST /chat/stream`) | `backend/app/services/environments/agent_env_connector.py` |
| [agent_message_attachments](../agents/agent_file_management/agent_message_attachments.md) | Files the agent attaches to its reply | `backend/app/services/sessions/message_service.py` |
| [realtime_events](../application/realtime_events/event_bus_system.md) | Socket.IO fan-out and the backend event bus the exit stage rides | `backend/app/services/events/event_service.py` |

## The flow

### 1. Arrival — four kinds of door

* **Authenticated HTTP.** `POST /api/v1/sessions/{session_id}/messages/stream`
  (`backend/app/api/routes/messages.py`, `send_message_stream`) for web chat and guest shares;
  `POST /api/v1/webapp/{token}/chat/sessions/{session_id}/messages/stream`
  (`backend/app/api/routes/webapp_chat.py`) for the webapp widget; `POST /api/v1/a2a/{agent_id}/`
  (`backend/app/api/routes/a2a.py`, `_handle_jsonrpc`) and
  `POST /api/v1/external/a2a/agent/{agent_id}/` / `POST /api/v1/external/a2a/identity/{owner_id}/`
  (`backend/app/api/routes/external_a2a.py`).
* **Unauthenticated-by-JWT webhook.** `POST /api/v1/channels/{webhook_token}/inbound`
  (`backend/app/api/routes/server_channels.py`, `channel_inbound`) — Google Chat;
  `POST /agent-hooks/{webhook_id}` (`backend/app/api/routes/agent_hooks.py`) — agent webhooks;
  `POST /api/v1/hooks/{webhook_id}` (`backend/app/api/routes/webhooks.py`) — task triggers.
  All three cap the body before parsing it; the channel route also rate-limits per token
  (`SERVER_CHANNEL_WEBHOOK_RATE_LIMIT_PER_MIN`) before doing any work at all.
* **Polled transport.** `ChannelPollService.poll_enabled_channels`
  (`backend/app/services/server_channels/channel_poll_service.py`) runs every 60 s from
  `channel_poll_scheduler.py`, selects enabled channels whose type declares
  `inbound_mode="polled"`, and calls `EmailChannelAdapter.poll`
  (`backend/app/services/server_channels/adapters/email.py`), which does IMAP `SEARCH UNSEEN` on
  `INBOX` off the loop via `anyio.to_thread`.
* **Platform-internal originator.** `agent_schedule_scheduler.py` and
  `backend/app/services/tasks/task_trigger_scheduler.py` each run a 1-minute APScheduler job;
  `POST /api/v1/tasks/{id}/execute` is the human's version of the same thing. All of these are
  gated off in tests by `if not settings.TESTING:` in `backend/app/main.py`.

The App MCP server is a fifth shape: an ASGI mount at `/mcp` (`backend/app/mcp/server.py`) exposing
one tool, `send_message` (`backend/app/mcp/app_tools.py`). It has a `ServerChannel` row, but that
row is a *policy front only* — `backend/app/services/server_channels/adapters/app_mcp.py` declares
`inbound_mode="authenticated"` and implements no transport, and the policy is read inside
`backend/app/mcp/app_token_verifier.py` on every token verification.

### 2. Transport authentication — the sender becomes a platform user

Each door proves the sender differently, and the trust tiers are not equal (stated plainly in the
`app_mcp.py` module docstring):

* Google Chat — `GoogleChatAdapter.verify_inbound` verifies a Google-signed JWT against the
  channel's `project_number`; `sender_email` comes out of that JWT. Failure raises
  `ChannelVerificationError`, which the route maps to a detail-free 403 and which writes a throttled
  `SecurityEvent`.
* Email — `sender_email` comes out of the `From:` header, and is spoofable. The mailbox filter is
  `_is_addressed_to_channel`, which fails closed on empty config.
* A2A — `AccessTokenService.verify_a2a_token` (`backend/app/services/a2a/access_token_service.py`)
  decodes an HS256 `AgentAccessToken` JWT, then re-checks the row's `token_hash` and `is_revoked`
  and stamps `last_used_at`. A plain user JWT is the fallback.
* External A2A — plain `CurrentUser` JWT plus `CurrentClientClaims` (`backend/app/api/deps.py`); no
  A2A access tokens on this surface at all.
* App MCP — `AppMCPToken` bearer, `token_hash` + `expires_at` + `is_revoked`.
* Guest share / webapp — a short-lived share JWT minted by
  `backend/app/services/sharing/agent_guest_share_service.py`, resolved by `deps.py` into a
  `GuestShareContext` / `WebappChatContext` carrying `agent_id` and `owner_id`.

`ChannelInboundService.process_inbound` states the resulting invariant for both channel transports:
**the caller is the authentication chokepoint** — nothing below re-verifies `inbound.sender_email`.
The result is a `SessionSender` (`backend/app/models/sessions/session_sender.py`): a frozen
`(kind, external_id, display_name, platform_user_id)` built by exactly one constructor per door —
`from_channel`, `from_a2a`, `from_app_mcp`, `from_webui`, `from_guest_share`, `from_task_execution`,
`from_system_trigger`.

### 3. Admission — is this sender allowed to talk to this platform at all

Only the channel pipeline has a full admission stack, and its order is load-bearing
(`channel_inbound_service.py`, `process_inbound`): redelivery dedup (3) → whitelist (4) → user
resolution and optional auto-registration (5) → channel policy (6) → attachments (6.5) → binding
dispatch (7). `ChannelPolicyService.describe`
(`backend/app/services/server_channels/channel_policy_service.py`) evaluates three terms in order —
channel enabled, visibility/grant, the user's own `ChannelUserSetting.is_enabled` (inheriting
`ServerChannel.default_enabled_for_users` when absent). A whitelist miss and a policy denial are
answered with the *verbatim same* text, so the reply is not an oracle.

Note the ordering consequence: policy runs **after** user resolution, so a restricted channel can
auto-register an account and then decline it.

Other doors admit differently. A2A checks `A2AAuthContext.can_access_agent` and, for scoped tokens,
`AccessTokenService.can_use_mode`. External A2A uses `ExternalAccessPolicy.resolve_agent`
(`backend/app/services/external/external_access_policy.py`). Agent webhooks check only
`hmac.compare_digest` on the bearer token. The web-UI route does its own ownership and
building-mode checks inline (`routes/sessions.py`, `routes/messages.py`), and deliberately keeps
them there rather than in the service.

### 4. Inbound attachments become rows before anything else reads them

`ChannelAttachmentService.materialize`
(`backend/app/services/server_channels/channel_attachment_service.py`) sits at step 6.5 — below
verification, dedup, the whitelist, user resolution and the policy gate, because an attachment
handle is attacker-influenced data and fetching one is a network call made on the sender's say-so.
It caps the count *before* any fetch, reuses prior materializations keyed on
`(server_channel_id, thread_key, external_message_id, attachment_index)` stored in
`FileUpload.file_metadata`, validates bytes through
`backend/app/services/files/attachment_limits.py`, and writes `FileUpload` rows with
`status="temporary"` owned by **the sender**, not the session owner. Nothing here raises into the
message; everything that fails becomes a `SkippedAttachment`.

### 5. Agent selection

For a new channel thread, `ChannelInboundService._route_new_thread` calls
`ChannelRoutingService.decide` (`backend/app/services/server_channels/channel_routing_service.py`);
for App MCP, `AppMCPRoutingService.route_message`; for External A2A `identity` targets,
`IdentityRoutingService.route_within_identity`. All of them return an agent id and, when the message
was addressed to a person rather than to one of the caller's own agents, an `IdentityGrant`
(ids only). Every other door already knows its agent. **The decision tree, the traces it writes and
the identity model are [Routing & identity chain](routing_identity_chain.md).** What matters here is
that the grant is a *claim*, re-verified downstream, and that a routed thread is pinned by a
`ChannelThreadBinding` row (`backend/app/models/server_channels/channel_thread_binding.py`) whose
uniqueness constraint on `(server_channel_id, thread_key)` is what resolves concurrent first
messages.

### 6. Session resolve-or-create — the narrow throat

`ChannelIngestionService.ingest_inbound_message` runs three steps and delegates all three:

1. `assert_access(db, agent, sender, policy)` — dispatch by **sender kind**, never by
   `integration_type` (which is metadata). `channel_caller` asserts the three-way invariant
   `agent.owner_id == policy.expected_owner_id == sender.platform_user_id`; the one alternative is
   `ChannelAccessPolicy.identity_grant`, whose three ids are re-read via
   `IdentityService.verify_identity_access` on **every message**, because the routing decision and
   the session creation are separated by a worker hop and the owner may have revoked in between.
   Every refusal raises `ChannelDecline` — a `PermissionError` subclass that exists so
   `_drain_parked` can tell "will never succeed" apart from a transient `OSError`.
2. `resolve_or_create_session(...)` — with a `thread_key` it loads the session and runs
   `_verify_resume_sender`; without one it picks the owner via `_select_session_owner_id` and calls
   `SessionService.create_session`, which binds `Session.environment_id` to
   `agent.active_environment_id` and returns `None` (→ `NoActiveEnvironmentError`) when there isn't
   one. Then `_stamp_new_session` applies the whitelisted post-create columns (`caller_id`,
   `identity_caller_id`, `identity_binding_id`, `identity_binding_assignment_id`) and merges
   `session_metadata_extra`. Any unknown key raises `ValueError`.
3. `SessionService.send_session_message(..., initiate_streaming=True)`.

The `integration_type` written at create is the marker every later stage reads:
`channel_<type>` for server channels, `a2a`, `app_mcp`, `identity_mcp`, `external`, `task`,
`schedule`, `webhook`, or `None` for web-UI, guest-share and webapp sessions.

Three doors deliberately do **not** run all of step 1–3: `message/stream` on A2A uses
`assert_access` + `resolve_or_create_session` + `send_session_message(initiate_streaming=False)`
because `SessionStreamProcessor` owns the stream kick; App MCP does the same and then uses raw
`MessageService.create_message`, because `ingest_inbound_message`'s stream kick conflicts with
`stream_and_collect_response`'s session lock; and `POST /api/v1/sessions/` calls only
`resolve_or_create_session`, because there is no message body yet.

### 7. Message row, environment readiness, commands

`SessionService.send_session_message` re-validates `chat_session.user_id == user_id`, rebinds a
stale environment via `resolve_and_rebind_session_environment`, and then branches:

* **Slash command** (`CommandService.is_command`) — handled locally with no LLM call, waking a
  suspended env first if the handler needs one. Returns `action="command_executed"` (sync) or
  `action="queued"` (`/run:<name>`, which does stream).
* **Files present and env not running** — `ensure_environment_ready_for_streaming` (120 s) runs
  *before* upload, because `prepare_user_message_with_files` needs a live container.
* Otherwise `MessageService.create_message(role="user", sent_to_agent_status="pending")`.

`SessionService.initiate_stream` then decides *when*: it collects pending messages, generates a
title if the session has none, runs the install-readiness gate, and dispatches on environment
status — `running` refreshes expiring OAuth credentials and spawns
`MessageService.process_pending_messages`; `suspended`/`stopped` starts activation in the background
and writes `Session.interaction_status = "pending_stream"`; anything else just marks pending. The
resume is event-driven: `SessionService.handle_environment_activated` re-calls `initiate_stream` for
every session in that environment sitting on `pending_stream`.

### 8. Streaming into the container

`process_pending_messages` holds the shared per-session `asyncio.Lock` around its **entire** body,
composes the handler, and runs the processor:

```
WebSocketEventHandler                     ← always the primary
   └─ maybe_attach_channel_relay(...)     ← channel sessions only
        └─ CompositeStreamEventHandler(primary, [ChannelRelayEventHandler])
SessionStreamProcessor(...).process()
   collect batches → mark sent → POST {env}/chat/stream → fan out events → on_complete
```

`SessionStreamProcessor` (`backend/app/services/sessions/stream_processor.py`) is the single
pipeline for all four consumers. Each supplies a `StreamEventHandler`: `WebSocketEventHandler`
(Socket.IO room `session_{id}_stream`), `MCPEventHandler`, `A2AStreamEventHandler` (SSE), or a
composite. The processor partitions pending messages into LLM batches and command batches, marks
them all sent before the first batch starts, and for each LLM batch calls
`MessageService.stream_message_with_events`, which POSTs to the container's `/chat/stream` via
`agent_env_connector.stream_chat`.

`CompositeStreamEventHandler` is asymmetric on purpose: a passenger's failure is isolated and
logged, a **primary's** failure is held until the fan-out finishes and then re-raised — because
`WebSocketEventHandler.on_complete` is what clears `interaction_status`, and swallowing it would
leave the web client on a stale "streaming".

### 9. Finalize — the turn gets an identity

At the end of a batch, `MessageService._process_attachments` extracts `<cinna_attach>` paths from
the assembled text, materialises those workspace files into platform storage, strips the tags,
splices `attachment` events into the recorded stream and yields them live (so streaming A2A clients
get a `FilePart`). `_finalize_agent_message` then writes the agent `SessionMessage` — and returns
**the id of the row this batch actually wrote**, which is carried on the terminal `STREAM_COMPLETED`
event as turn identity. Outbound consumers deliver *that* message and never "the newest agent
message in the session".

### 10. The reply goes back out the door it came in

| Door | Delivery |
|---|---|
| Web chat, guest share, webapp | Socket.IO `stream_event` into room `session_{id}_stream`; the POST returns only a handshake from `MessageService.build_stream_response` |
| A2A `message/send` | A2A `Task` built by `DatabaseTaskStore` (`backend/app/services/a2a/a2a_task_store.py`), polled until terminal |
| A2A / External A2A `message/stream` | SSE frames, one JSON-RPC-wrapped A2A event each, mapped by `backend/app/services/a2a/a2a_event_mapper.py`; attachments become `FilePart`s with 1-hour signed backend URLs from `AgentWorkspaceTokenService.create_file_download_token` |
| App MCP | a JSON string `{"response", "context_id", "agent_name"}`; increments go out as MCP progress/log notifications, not as a stream of the answer |
| Google Chat | live: `ChannelStreamRelay` patches one message every ~3 s and *seals* a slice when the draft outgrows the transport limit. Final: `ChannelOutboundService.handle_stream_completed` |
| Email | `EmailChannelAdapter.send_message` enqueues an `OutgoingEmailQueue` row; `EmailSendingService.send_pending_emails` drains it every 2 minutes, `MAX_RETRIES = 3`, no backoff |
| Agent webhook | `{"success": true, "webhook_type", "log_id"}` — the agent's answer is never returned |

The channel exit is event-driven: `backend/app/main.py` registers
`ChannelOutboundService.handle_stream_completed` / `handle_stream_error` /
`handle_stream_interrupted` on the bus, and each gates on
`session.integration_type.startswith("channel_")` after a registry lookup. The handler fires **once
per LLM batch**, and it is wrapped by the **turn delivery ledger**
(`ChannelTurnDelivery`, `backend/app/models/server_channels/channel_turn_delivery.py`;
service `backend/app/services/server_channels/channel_turn_delivery_service.py`): one row per
external message the turn wrote, roles `draft | sealed | final`, written only at boundaries — never
on the rolling ~3 s patches. A `final` row for this batch's agent message means the turn is already
answered, which is where a redelivered `STREAM_COMPLETED` stops. Comparing the sealed rows'
`visible_char_end` + `content_sha256` against the finalized canonical text is the divergence check;
it changes no behaviour, it only makes a silent assumption able to fire.

## Decision points

| Where | Decides on | Outcomes |
|---|---|---|
| `routes/server_channels.py` `channel_inbound` | token → channel, adapter signature | 404 unknown/disabled, 403 bad signature, 429, 413 |
| `channel_inbound_service.py` `process_inbound` step 3 | `binding.last_external_message_id`, else in-process TTL cache | redelivery acked, or continue |
| `process_inbound` step 4 | `channel.email_whitelist` | denied (fails closed on NULL) or continue |
| `process_inbound` step 5 `_resolve_user` | email → `User`, `channel.auto_register_users` | resolve, auto-create, or deny |
| `channel_policy_service.py` `describe` | enabled → visibility/grant → per-user toggle | granted policy, or the same generic denial |
| `process_inbound` step 7 | `binding.status` | `active` → continue, `pending_install` → park, `failed` → delete and re-route, none → route |
| `channel_control_commands.py` `match_control_command` | exact `/stop` text, no files, outbound configured | interrupt the stream, or fall through |
| `channel_routing_service.decide` / `AppMCPRoutingService` | message text + policy | agent id (+ optional `IdentityGrant`) — see the routing flow |
| `channel_ingestion_service.py` `assert_access` | `sender.kind` + `ChannelAccessPolicy` | return, or `ChannelDecline` |
| `assert_access` `channel_caller` arm | three-way owner invariant, else the re-read grant | own install, identity session, or refusal |
| `_select_session_owner_id` | `sender.kind`, `session_owner_id` / `identity_owner_id` overrides | which user owns the new `Session` |
| `_verify_resume_sender` | `sender.kind` vs the existing row | resume, or refuse (`system_trigger` and `mcp_caller` can never resume here) |
| `session_service.py` `send_session_message` | `CommandService.is_command`, `has_files`, env status | command result, file wait, or plain pending message |
| `session_service.py` `initiate_stream` | install gate, then `environment.status` | `streaming`, `pending` (+ background activation), or `no_pending_messages` |
| `channel_stream_relay.py` `maybe_attach_channel_relay` | feature flag, channel session, binding, `supports_status_notice` | relay attached, or base handler unchanged (email and App MCP always) |
| `channel_outbound_service.py` `handle_stream_completed` | ledger `turn_already_settled`, then relay tail, then `AGENT_MESSAGE_ID_META_KEY` (uuid / explicit `None` / absent) | deliver increment, deliver that row, settle only, or legacy newest-row |

## Where to change what

| If you want to… | Edit | Re-check |
|---|---|---|
| add a new inbound transport | a `ChannelAdapter` subclass under `backend/app/services/server_channels/adapters/` + the registry | its `ChannelCapabilities` (`supports_status_notice` decides whether it gets a streaming relay); `ChannelOutboundService._deliver_ex` |
| add a new sender kind | `SessionSenderKind` + a constructor in `models/sessions/session_sender.py` | all three kind dispatches — `assert_access`, `_select_session_owner_id`, `_verify_resume_sender` — and `get_session_sender`'s reader table |
| stamp a new column at session create | `resolve_or_create_session`'s create-kwarg tuple or `_STAMPABLE_COLUMNS` | every caller passing `extra_session_kwargs`; unknown keys raise |
| change the access rule for one door | that door's `ChannelAccessPolicy`, not the service | `assert_access`'s arm for that kind; the ≥2-callers contract test `backend/tests/architecture/channel_ingestion_callers_test.py` |
| change what the agent receives | `MessageService.collect_pending_batches` / `_build_llm_batch_content` | recovery-context and webapp-context injection in `stream_processor.py`; title generation reads the raw first message, not the assembled batch |
| add a stream consumer | a `StreamEventHandler` implementation + compose it as a passenger | `CompositeStreamEventHandler`'s primary/passenger asymmetry — a passenger must never break the primary |
| change how a channel reply is rendered | `ChannelOutboundService._deliver_ex` / the adapter's `replace_message` | the relay's seal path, and `ChannelTurnDeliveryLedger.settle_turn` (offsets are batch-scoped) |
| gate anything on the send path | `ingest_inbound_message` | it does **not** cover A2A `message/stream`, App MCP, agent webhooks or webapp chat — see the traps |

## Invariants and traps

**One authentication chokepoint per transport, and nothing below re-verifies.** `process_inbound`
says so explicitly. A caller that reaches it with an `inbound` it did not authenticate has voided
the whole module's promise.

**Access is decided by sender kind; `integration_type` is metadata.** The External A2A surface
stamps `integration_type="app_mcp"`/`"identity_mcp"` while building an `a2a_caller` sender, and the
reader `get_session_sender` maps the row back for display. Nothing in `assert_access` reads it.

**An identity grant is a claim, not a conclusion.** `assert_access` re-reads binding, assignment and
all four linkages on every message, and `_select_session_owner_id` refuses an `identity_owner_id`
override unless the grant object is present on the policy — deliberately structural, because
`resolve_or_create_session` is public and a future caller could otherwise commit a session inside a
stranger's workspace with nothing verified.

**A soft `action="error"` is a real failure.** `ingest_inbound_message` reports "no active
environment", "activation failed", "session vanished" and "file preparation refused" as a return
value, not an exception, and every channel caller above decides "delivered or not" by asking whether
something raised. `ChannelInboundService._ingest` converts it to
`ChannelIngestProducedNoMessage` *before* stamping `binding.session_id`; a caller that forgets loses
the sender's message silently, because the next redelivery dedups against the stamp.

**`integration_type` staying `channel_<type>` is load-bearing.** `ChannelOutboundService._resolve_channel_session`
gates on that prefix. A differently-stamped channel session runs perfectly and never delivers.

**Never deliver by recency.** The three states of `AGENT_MESSAGE_ID_META_KEY` — a uuid, an explicit
`None`, and the key absent — are three different instructions. Falling back to the newest agent row
on an explicit `None` re-delivered the *previous* turn's answer into the thread as if it answered
the new question; that is the bug the whole turn-identity mechanism exists to prevent.

**The paths that skip `ingest_inbound_message` are real and are not equivalent.** A2A
`message/stream` uses the two primitives directly (to avoid a double stream kick); App MCP does the
same plus a raw `MessageService.create_message` (session-lock conflict); web-UI/guest-share
`POST /api/v1/sessions/` calls only `resolve_or_create_session` and never `assert_access`, gating in
the route instead; webapp chat calls `SessionService.create_session` directly with no sender at all;
and agent webhooks (`agent_webhook_service.py`) use no `SessionSender`, no `ChannelAccessPolicy` and
no `assert_access` — they are the last trigger path still on the pre-migration primitives. A gate
added to `ingest_inbound_message` silently does not apply to any of them.

**The session-webhook arm queues without kicking.** `_fire_session` creates the session and calls
`MessageService.create_user_message_and_emit_event`, which emits a Socket.IO-only
`user_message_created` (no backend handler consumes it) and leaves `sent_to_agent_status="pending"`.
It never calls `initiate_stream`, and it does not set `interaction_status="pending_stream"`, so
`handle_environment_activated` will not pick it up either. The log status is literally named
"user message queued".

**`system_trigger` is asserted, not fast-pathed.** `allow_system_trigger_fastpath=True` still
requires `policy.expected_owner_id == agent.owner_id == sender.platform_user_id`, and the kind
cannot resume — a `thread_key` reaching the service with it is a caller bug.

**Handover does not use `system_trigger`, despite the docs.** The only two production callers of
`SessionSender.from_system_trigger` are both in `agent_schedule_scheduler.py` and both pass
`trigger_kind="schedule"`; `trigger_kind="handover"` appears only in
`backend/tests/unit/models/test_session_sender.py`. Runtime handover
(`AgentService.create_agent_task`) creates an `InputTask` with `auto_execute=True` and goes through
`InputTaskService.execute_task`, i.e. **`task_executor`** with `integration_type="task"`.

**`Session.email_thread_id` and `Session.sender_email` are dead write paths.** `create_session`
documents both: no caller passes them, historical rows still carry values, and email thread state
now lives on `ChannelThreadBinding.thread_key`.

**Neither poll loop takes a lock.** `channel_poll_scheduler.py` documents the multi-process race
explicitly (IMAP `\Seen` + `external_message_id` dedup is a race, not a guarantee) and warns
*against* copying the advisory-lock leader pattern from `model_discovery_scheduler`, which leaks
pooled connections. The task-trigger and agent-schedule loops are plain `SELECT` + in-loop `await`
with no `SKIP LOCKED`, and the email outbound queue has no claim column either.

**Assistant text is coalesced at finalize.** `_coalesce_assistant_events` merges runs of consecutive
`assistant` events because some SDK adapters flush on every newline — which, unmerged, splits code
fences.

## See also

Every feature named in **Participants** links to its own business doc. The tech docs:

- [Channel Ingestion tech](../application/agent_sessions/channel_ingestion_tech.md) — stage 6, method by method
- [Agent Sessions tech](../application/agent_sessions/agent_sessions_tech.md) — stages 7–9
- [Server Channels tech](../application/server_channels/server_channels_tech.md) and [debug monitor](../application/server_channels/channel_debug_monitor.md) — stages 1–5 and 10
- [A2A Protocol tech](../application/a2a_integration/a2a_protocol/a2a_protocol_tech.md) and [A2A access tokens](../application/a2a_integration/a2a_access_tokens/a2a_access_tokens.md) — stage 2 and the A2A exits
- [Routing Tuning](../application/routing_tuning/routing_tuning.md) — the traces stage 5 writes
- [Routing & identity chain](routing_identity_chain.md) — the agent-selection stage, in depth
