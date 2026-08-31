# Email Integration

## Email is a server channel

Since Phase 4 of the channels & identity unification refactor, **email is one
of the transports behind [Server Channels](../server_channels/server_channels.md)**,
alongside Google Chat. There is no more per-agent email integration: an agent
can no longer own an inbound mailbox from its own Integrations tab. An admin
configures one email channel server-wide (Admin → Server Configuration →
Channels), pointing it at a shared mailbox, and inbound mail is routed to
senders' own agents exactly the way an inbound Google Chat message is —
sender-first against the sender's own installed agents, then against the
server's auto-install catalog.

This document covers what is **specific to the email transport**: the polled
(rather than pushed) inbound model, the `From:`-header trust tier, recipient
validation on a shared mailbox, and outbound delivery through a durable queue.
Everything else — whitelisting, auto-registration, two-pass routing, thread
bindings, identity routing, admin availability policy — is the shared Server
Channels model and is documented there; read
[Server Channels](../server_channels/server_channels.md) first if you have
not already.

## Core Concepts

- **Email channel** — a `ServerChannel` row with `channel_type="email"`. Its
  `config` names an incoming IMAP mail server, an outgoing SMTP mail server, a
  mailbox address to poll, and a from-address for replies. It has no webhook
  URL and no webhook token — see [Polled, not pushed](#polled-not-pushed).
- **Mail Server** — an admin-owned IMAP or SMTP connection (superuser-only,
  server-scoped). See [Mail Servers](mail_servers.md).
- **Polled inbound** — a scheduler fetches unread mail from the configured
  mailbox once a minute; there is nothing pushing to the platform. See
  [Polled, not pushed](#polled-not-pushed).
- **Email Message** — every arrival is stored durably as soon as it is
  fetched, before whitelist, policy, or routing sees it. See
  [Every arrival is recorded, before anything decides](#every-arrival-is-recorded-before-anything-decides).
- **Outgoing Email Queue** — the durable, retried queue email replies go
  through. See [Email Sessions](email_sessions.md).
- **Channel Thread Binding** — same shared concept as every other channel:
  one binding pins one email thread to one platform `(user, agent, session)`.
  The email transport's addition is what the binding's `thread_key` actually
  *is* — see [Threading](#threading-the-thread-key-is-the-root-message-id).

## User Stories / Flows

### 1. Admin sets up an email channel

1. Superuser adds an IMAP server and an SMTP server under **Admin → Server
   Configuration → Mail Servers**, and tests both connections there.
2. Superuser opens **Admin → Server Configuration → Channels**, adds a new
   channel, picks **Email**, and selects the two mail servers plus the
   mailbox address to poll and the from-address replies should use.
3. Sets the email whitelist and decides whether unknown senders should be
   auto-registered — the same fields, the same fail-closed semantics, as a
   Google Chat channel.
4. There is no webhook URL to paste anywhere: the setup panel says so
   explicitly, and explains that the first reply can take up to one poll
   interval.
5. The panel also states, unconditionally, that the sender address on this
   channel comes from the `From:` header and can be forged by anyone who can
   deliver mail to the mailbox — see
   [Trust chain: `From:` is spoofable](#trust-chain-from-is-spoofable).

### 2. Team member's first email

1. An employee emails the shared mailbox (e.g. `support@corp.com`).
2. The next poll tick fetches it, checks it is actually addressed to this
   channel's mailbox, and records it durably.
3. The sender is whitelist-checked and resolved to (or auto-registered as) a
   platform user — the same rule Google Chat uses.
4. Since this is a new thread, routing runs: first against the sender's own
   installed agents, then against the server's auto-install catalog if
   nothing matches.
5. If an auto-install bundle matches, it is installed behind the scenes and
   the reply — once the environment is ready — lands in the same thread as a
   normal email reply, correctly threaded.
6. If nothing matches at all, **nothing is sent back.** Unlike Google Chat,
   this transport has no way to answer synchronously, and a decline is never
   mailed to the sender — see
   [Declines are silent](#declines-are-silent-and-that-is-deliberate).

### 3. Continuing a thread

1. The employee replies to the agent's email.
2. The reply's `In-Reply-To` / `References` headers resolve to the same
   thread root as the original message, so the existing binding is found and
   the message is fed straight into the same session.
3. The agent's reply is queued and delivered with proper `In-Reply-To` /
   `References` headers so it threads correctly in the recipient's mail
   client.

### 4. A denied or unroutable message

1. A message arrives from an address the whitelist does not cover, or from a
   sender the channel policy has turned away, or that fails to route to any
   agent.
2. The message is still recorded in `email_message` (see below) — this is
   the row's whole reason for existing on this transport — but nothing is
   sent back to the sender.
3. A superuser can see the denial in the channel's debug feed; the sender
   sees nothing at all.

## Business Rules

### Trust chain: `From:` is spoofable

Google Chat's sender identity comes out of a Google-signed JWT. Email's comes
out of the `From:` header, and **the `From:` header is spoofable** — anyone
who can get a message into the polled mailbox can claim any address in it.
Nothing downstream re-checks it: the whitelist, user resolution,
auto-registration, and identity routing all treat that address as the
sender's real identity, exactly as they do for a verified Google Chat sender.
An email channel's whitelist is therefore only as strong as the receiving
mail server's own SPF/DKIM/DMARC enforcement — this is stated in the admin
setup panel, not just in this doc, and it is the reason email channels suit
internal team mailboxes rather than an open public inbox (see
[Capabilities removed](#capabilities-removed-in-this-refactor)).

### Polled, not pushed

Unlike Google Chat, an email channel has no webhook: `ServerChannel.webhook_token`
is `NULL` for it, and the setup panel never shows a URL. A scheduler polls the
mailbox once a minute (`POLL_INTERVAL_SECONDS = 60`) for every enabled
channel whose transport is polled, and each fetched message enters the
pipeline at the same post-verification step a webhook request would reach
after `verify_inbound` succeeds. Authentication for this transport therefore
happens inside the poll itself, not in a request handler — there is nothing
else to check the mail against.

### Recipient validation

Because one IMAP mailbox can receive mail addressed to several different
aliases or groups, each channel only accepts mail actually addressed (To/CC)
to its own configured mailbox. Mail for a different recipient in the same
inbox is left alone (not even marked read) so another channel's poll — or a
future one — can still find it.

### Every arrival is recorded, before anything decides

Every fetched message is stored as an `EmailMessage` row **before** the
whitelist, the channel policy, or routing ever sees it — including messages
that are ultimately denied. This is different from the pre-channel behaviour,
where a row only existed for mail that was already routed to an agent.

The reason is that a polled transport has no way to reply to a decline (see
next section), so the operator's only view into "who got turned away and
why" would otherwise be the admin debug feed, which is in-memory and
disappears on restart. On a transport whose senders are external by
definition, that is not an adequate audit trail. So the row exists for every
arrival, and `EmailMessage.agent_id` is `NULL` until (and unless) routing
actually assigns an agent to it — readers must treat `NULL` as "arrived, not
routed," not as missing data. A redelivery of mail that already reached an
agent is dropped rather than re-stored; a redelivery of mail that was never
routed is left for the next attempt to retry.

### Threading: the thread key is the root Message-ID

A thread's binding key is the **root** `Message-ID` of the conversation —
`References[0]` if the sender's client sent a reference chain, else
`In-Reply-To`, else the message's own `Message-ID` (which makes a first
message the root of its own thread) — never the *latest* message. Keying on
the latest instead would open a new binding on every reply, and the visible
symptom would not look like a threading bug: it would look like "the agent
forgot the conversation."

### Outbound goes through a durable queue

Google Chat's outbound delivery is best-effort (a few in-adapter retries,
then a logged failure). Email is the one channel transport with a durable,
retried outbound path: a reply is enqueued into the existing
`OutgoingEmailQueue` and delivered by the pre-existing sending scheduler,
which retries up to three times before giving up. See
[Email Sessions](email_sessions.md) for the mechanics.

### Declines are silent, and that is deliberate

A polled transport has no synchronous reply surface the way a webhook does.
So a sender denied by the whitelist, by channel policy, or by failed user
resolution gets **no reply of any kind** — mailing a decline back would
confirm to a prober which addresses exist on the platform and would turn the
mailbox into a spam amplifier. Every denial is still recorded — in the
channel's admin debug feed, and (uniquely to this transport) durably in
`email_message` as an unrouted row — so an admin can see what happened even
though the sender never will.

## Capabilities removed in this refactor

Per-agent Email Integration is deleted outright, with no compatibility shim.
Four capabilities it offered do not exist any more:

1. **Per-agent email integration.** An agent can no longer own an inbound
   mailbox address from its own Integrations tab. Email is now a single,
   admin-configured server channel, the same as Google Chat.
2. **Clone-per-sender isolation** (`max_clones`, `clone_share_mode`,
   `agent_session_mode`). Every sender used to get their own cloned agent
   with an isolated environment; that is replaced by the shared
   auto-registration + auto-install mechanism every channel now uses — a
   sender gets their *own account*, not a clone of somebody else's agent.
3. **Task mode** (`process_as = new_task`) and the "Send Answer" AI-generated
   email reply. Incoming email can no longer create an `InputTask` for
   manual review before responding — see
   [Input Tasks](../input_tasks/input_tasks.md).
4. **The external-customer inbox pattern.** A stranger with no platform
   account writing to `support@` and having one specific agent answer them no
   longer exists. Under sender-routing, an unknown sender is auto-registered
   (if the channel allows it) as an ordinary platform account and routed over
   **their own** (initially empty) agent set, then the server's auto-install
   catalog — exactly like a first-time Google Chat sender. This is a
   deliberate trade: email channels, like Google Chat channels, are internal
   team surfaces now, not a public support-inbox mechanism.

## Security Model

### Credential separation (unchanged)

Mail server credentials are stored encrypted, backend-only, and are never
shared with an agent. Decryption happens only when the platform connects to
IMAP or SMTP. See [Mail Servers](mail_servers.md).

### Sender identity is the weakest tier the platform trusts

See [Trust chain: `From:` is spoofable](#trust-chain-from-is-spoofable) above.
This was always true of the feature and remains true — it is now surfaced
next to Google Chat's much stronger, signed-JWT trust tier, in the same admin
UI, so an admin configuring a whitelist is not left to guess which tier they
are working with.

### Recipient validation (unchanged)

See [Recipient validation](#recipient-validation) above — this defends a
shared IMAP mailbox against one channel processing another channel's mail.

### Single-process poller (known limitation)

The channel poll scheduler assumes a single backend process: there is no
leader election, the same limitation `channel_pending_scheduler` already
carries. Two backend processes would both poll the same mailbox; the IMAP
`\Seen` flag plus the pipeline's redelivery dedup on `Message-ID` is what
keeps that from double-answering in practice, but it is a race, not a
guarantee. This is a documented limitation, not a bug — see
[Server Channels — Known Limitations](../server_channels/server_channels.md#known-limitations)
for the sibling case, and do not "fix" it by copying the advisory-lock leader
pattern from the model-discovery scheduler: that pattern leaks connections on
pooled connections.

## Integration Points

- [Server Channels](../server_channels/server_channels.md) — the parent
  feature. Whitelisting, auto-registration, two-pass sender routing, thread
  bindings, identity routing, and admin availability policy are all the
  shared channel model; this document only covers what the email transport
  does differently.
- [Mail Servers](mail_servers.md) — the admin-owned IMAP/SMTP connections an
  email channel references by id.
- [Email Sessions](email_sessions.md) — threading mechanics, the outgoing
  queue, and session context for agent scripts.
- [Agent Bundles & Installs](../../agents/agent_bundles/agent_bundles.md) —
  Pass-2 auto-install for a sender who matches no agent of their own uses the
  same `InstallService.install_bundle` entry point every other channel uses.
- [Agent Sessions / Channel Ingestion](../agent_sessions/channel_ingestion.md) —
  email sessions are `channel_caller`-sourced sessions like any other channel;
  `integration_type` is stamped `channel_email`.
- ~~[Input Tasks](../input_tasks/input_tasks.md)~~ — no longer integrated.
  Incoming email cannot create an `InputTask`; see
  [Capabilities removed](#capabilities-removed-in-this-refactor).
