# Server Channels

## Purpose

Let people **outside the platform** — company employees on a chat app or a
shared mailbox — talk to platform agents without ever logging in. A
superuser configures one or more channel instances server-wide; inbound
messages are verified, whitelisted, routed to the right agent (installing
one automatically if needed), bound to a persistent conversation thread, and
answered back through the same surface.

Three channel types are registered today: **Google Chat**, a *pushed* transport
(the outside world calls a webhook); **Email**, a *polled* transport
(the platform pulls a mailbox on a timer); and, as of Phase 5 of the channels
& identity unification, **App MCP**, an *authenticated* transport whose caller
is already a platform user — see
[Transport shapes: webhook, polled, authenticated](#transport-shapes-webhook-polled-authenticated)
and [App MCP as a channel](#app-mcp-as-a-channel).
Everything below "Business Rules → Routing" onward is shared by every
transport; email's transport-specific behaviour (the `From:`-header trust
tier, recipient validation on a shared mailbox, the durable outgoing queue)
is documented in [Email Integration](../email_integration/email_integration.md)
rather than duplicated here, and App MCP's own business logic lives in
[App MCP Server](../app_mcp_server/app_mcp_server.md).

Routing normally chooses among the sender's **own** agents. Since Phase 3 of the channels & identity unification refactor a sender may also opt in, per channel, to addressing a **person** — "hey, ask HR what my time-off status is?" — in which case the message is answered by one of that person's agents, from inside that person's workspace. See [Identity routing](#identity-routing-whose-thread-whose-workspace).

## Core Concepts

- **Server Channel** — one admin-configured instance of a channel type (Google Chat, Email, or App MCP today; App MCP is a singleton — at most one may exist). Owns its own transport config, outbound credential, email whitelist, and auto-registration toggle (all inert/empty for App MCP, which has none of these), plus four admin-owned availability defaults (visibility, default enablement, default agent scope, auto-install permission — see [Availability and per-user settings](#availability-and-per-user-settings)). Superuser-only, managed from the **Channels** tab on `/admin/server-configuration`.
- **Channel Adapter / Channel Transport** — the transport-specific implementation (Google Chat, Email) behind a shared contract: authenticate an inbound message (however this transport receives one), parse it into a normalized message, send a reply, describe its own setup steps. Since Phase 4 of the channels & identity unification, the contract is split into **transport shapes** — `webhook`, `polled`, `authenticated` — because a pull transport like email has no `Request` to verify and a webhook-shaped ABC forced it to fake one. See [Transport shapes](#transport-shapes-webhook-polled-authenticated). Adding a new channel type (Discord, Telegram, …) means writing one adapter module and registering it — no pipeline change, no migration.
- **Webhook Token** — an unguessable, per-channel path segment (`POST /api/v1/channels/{webhook_token}/inbound`) that is a *webhook* channel's only "address." It is not a secret in the cryptographic sense (it doesn't prove who sent the request — the adapter's signature check does that); it is what keeps the endpoint from being guessed. A **polled** channel (email) has no webhook at all: `webhook_token` is `NULL` for it, and the setup panel shows no URL — see [Transport shapes](#transport-shapes-webhook-polled-authenticated).
- **Email Whitelist** — a comma-separated list of glob patterns (`*@example.com, devops.*@support.com`) gating which verified senders may reach agents through this channel. Shared matching logic with the email integration's sender allowlist.
- **Auto-Registration** — when enabled, a whitelisted sender with no platform account gets one created automatically, passwordless and already email-confirmed.
- **Channel Thread Binding** — the record that pins one external conversation thread to one platform `(user, agent, session)` triple. This is the feature's conversation state; there is no user-facing view of it — its lifecycle is only observable through the replies a sender sees and the sessions that appear under their account.
- **Server-Wide Auto-Install List** — a curated list of catalog bundles the platform may install automatically for a sender whose message doesn't match any agent they already have.
- **Two-Pass Routing** — every new thread is routed first against the sender's *own* installed agents, then — only on a miss — against the auto-install list.

## User Stories / Flows

### 1. Admin sets up a channel

1. Superuser opens **Admin → Server Configuration → Channels**.
2. Clicks **Add Channel**, picks a type from the card list (Google Chat or Email), then configures that type's fields — for Google Chat, names the channel and enters the GCP project number (the value Google puts in the webhook JWT's audience claim); for Email, picks an incoming (IMAP) and outgoing (SMTP) mail server from [Mail Servers](../email_integration/mail_servers.md) plus the mailbox to poll and the reply-from address. The rest of this flow describes the Google Chat (webhook) case; see [Email Integration](../email_integration/email_integration.md) for the email-specific setup steps.
3. Pastes the Google Chat service-account JSON key — a write-only field; once saved it can only be replaced, never re-displayed.
4. Sets the email whitelist (patterns, or `*` for "anyone Google verifies") and decides whether unknown senders should be auto-registered.
5. Saves. The **setup instructions** panel opens automatically, showing the webhook URL to paste into the Google Chat app's configuration and a step-by-step checklist.
6. Uses **Test outbound** to confirm the stored credential actually works before rolling it out. The target is picked by *email* from the people this channel has already seen; a raw space/thread id stays available as an escape hatch. An email can only be resolved once the app has received a message from that person — the provider's email alias requires user authentication and the app authenticates as an app — so before anyone has written in, the raw id is the only option.
7. Opens the **debug panel** (bug icon on the channel row) to watch messages arrive live and see what the pipeline decided about each one — see [Channel Debug Monitor](channel_debug_monitor.md).
8. Pastes the webhook URL into the Google Cloud Console Chat app config. The channel is live.

The webhook URL is built from the backend's public origin (`BACKEND_BASE_URL`, falling back to `FRONTEND_HOST`) — not from whatever host the admin reached the admin page on. On a deployment where the SPA and the API live on different hostnames, `BACKEND_BASE_URL` must name the API one, or the URL an admin copies points at the SPA and no event ever arrives. For testing a local backend against a real Google Chat app, `make webhook-tunnel` + `make webhook-set-url URL=...` point it at a public HTTPS tunnel (see server_channels_tech.md → Configuration).

### 2. Employee's first message (no UI — this happens entirely outside the platform)

1. An employee DMs the Google Chat app, or the app is added to a space and mentioned there.
2. The message arrives at the webhook, is verified, whitelist-checked, and the sender is resolved (auto-registered if new and allowed).
3. Since this is a brand-new thread, the platform tries to route it: first against the sender's own installed agents, then — if none match — against the server's auto-install list.
4. If a match is found on the auto-install list, the matching bundle is installed for that sender behind the scenes and a "Setting up **X** for you…" reply appears in the thread while the environment builds.
5. Once ready, the agent's real reply lands in the same thread, and a short "Your assistant is ready" notice precedes it if there was a wait.
6. If nothing matches, the sender gets a polite "couldn't find an agent for that — contact your admin" reply.

### 3. Continuing a conversation

1. The employee replies in the same Google Chat thread.
2. Because the thread already has an active binding, the platform skips routing entirely and feeds the message straight into the same session — the same principle App MCP uses for a caller's already-resolved context.
3. The agent's reply streams back to the thread as it completes.

### 4. Environment build fails during auto-install

1. A Pass-2 auto-install starts building an environment; the sender's message is parked in the meantime.
2. If the build fails outright, the binding is marked failed and the sender gets a "setting up your assistant failed — contact your admin" notice.
3. The *next* message the sender sends into that same thread deletes the failed binding and re-runs routing from scratch — a transient failure never permanently wedges the thread.

### 5. Cross-user thread collision (group spaces)

1. Two different whitelisted people can, in principle, post into the same brand-new thread in a shared space before either message has finished routing.
2. Whoever the system finishes creating a binding for first "owns" that thread's session going forward.
3. The other person is told the conversation belongs to someone else and is asked to start a new thread — their message is never merged into a stranger's session, whichever step of the pipeline the collision is caught at.

## Business Rules

### Transport shapes: webhook, polled, authenticated

Since Phase 4 of the channels & identity unification, `ChannelCapabilities`
declares one of three inbound shapes (`ChannelInboundMode`), and every
dispatch on transport shape — the registry, `ServerChannelService`, the
pollers — reads that declaration rather than guessing from which base class
an adapter subclasses:

- **`webhook`** (Google Chat, and the default every pre-existing adapter kept
  byte-for-byte): the outside world pushes an HTTP request to
  `POST /api/v1/channels/{webhook_token}/inbound`. `verify_inbound` is the
  authentication chokepoint — see below.
- **`polled`** (Email): there is no inbound `Request` at all. A scheduler
  pulls the transport on a timer (`poll(channel)`), and authentication for
  the fetched messages happens *inside that method* — it is required to make
  the same "nothing downstream re-checks this" promise `verify_inbound`
  makes for a webhook, and to state **how strong** that promise is for its
  own transport (email's is markedly weaker — see
  [Email Integration](../email_integration/email_integration.md#trust-chain-from-is-spoofable)).
  A polled channel needs no webhook token (`ServerChannel.webhook_token` is
  `NULL` for it) and its admin setup panel shows no URL.
- **`authenticated`** (App MCP, since Phase 5): the caller is already an
  authenticated platform user — a bearer token minted by this server's own
  OAuth flow, bound to one `user_id` — when the pipeline is entered. There is
  no external sender to whitelist and no request to verify: `needs_webhook_token`
  and `needs_outbound_credentials` are both `False`, and the admin form
  renders no webhook, secret, or whitelist field for it. See
  [App MCP as a channel](#app-mcp-as-a-channel).

A transport also declares `needs_outbound_credentials`: `True` (the default)
means the outbound credential lives in `ServerChannel.encrypted_secrets`;
`False` means it lives elsewhere and the adapter **must** override
`has_outbound_credentials()` to say where — the registry refuses to import
otherwise, since the inherited answer would report a working channel of that
type as having no way to reply. Email declares `False`: its credential is
the referenced SMTP `MailServerConfig` row, never anything in
`encrypted_secrets`, and never anywhere near an agent. App MCP also declares
`False`, for the opposite reason: there is no outbound credential at all — the
reply *is* the synchronous response to the caller's own MCP request — so its
adapter's `has_outbound_credentials()` always answers `True` rather than
`False`, because a permanently-uncleared "no credential" admin badge would
train an admin to ignore the same badge on a channel where it means replies
really are failing to deliver.

Adding a channel type is unchanged by the split: one adapter module plus one
registry entry. A poller for a new `polled` transport needs no new scheduler
code either — `ChannelPollService` enumerates polled channel *types* from the
registry, not a hardcoded list.

### App MCP as a channel

As of Phase 5 of the channels & identity unification, [App MCP Server](../app_mcp_server/app_mcp_server.md)
is a **singleton** `ServerChannel` row (`channel_type="app_mcp"`) — the
platform's one `authenticated` transport, alongside Google Chat's `webhook`
and Email's `polled`. What makes it worth its own channel row despite having
no transport of its own to configure:

- **Singleton, enforced twice.** `ServerChannelService.get_or_create_singleton`
  refuses a second row at the application layer, and a partial unique index
  (`uq_server_channel_singleton_type`, migration `867cacb5a827`) enforces the
  same thing at the database layer — the two must never drift apart, since a
  gap in either would let two rows exist with nothing to say which one wins.
  The row is materialized **lazily**, on first admin read or first App MCP
  use, with `enabled=True` and `visibility="public"` — an existing deployment
  keeps working the moment it upgrades. **It also cannot be deleted**, and the
  lazy materialization is the reason rather than any sentiment about the row:
  `ServerChannelService.delete_channel` raises
  `UnsupportedChannelOperationError` (a 422) for any singleton type, because
  the next list or token verification would mint a fresh one *with the default
  values* — so "delete" would read as "remove this" and act as "silently reset
  the kill switch to on". Disabling is the operation the admin wants, and it is
  one switch away on the same row.
- **No config, no secrets, no whitelist.** The admin form renders none of
  these fields for this row — driven off the adapter's declared
  `ChannelCapabilities`, never off a `channel_type == "app_mcp"` conditional
  in the component. Submitting any non-empty `config` is rejected outright by
  the adapter: a value nothing reads is worse than a refusal, because it looks
  configured.
- **What the admin gains that App MCP never had before this phase:** a
  server-wide kill switch (`enabled`), `visibility` (`public` or `restricted`
  + a grant allowlist), and a default agent scope — resolved by the exact same
  `ChannelPolicyService.resolve` every other channel goes through. There is no
  App MCP-specific policy engine.
- **Availability is checked at token use, not at token issue.**
  `AppMCPTokenVerifier` resolves `ChannelPolicyService.resolve(...).is_available`
  on every MCP request, after token validity passes, and caches the answer per
  user id for `settings.APP_MCP_AVAILABILITY_CACHE_TTL_SECONDS` (**default 45
  seconds**) — **so an admin revoke, a withdrawn grant, or the user's own
  toggle takes up to 45 seconds to actually bite.** The cache fails closed: a
  lookup that raises denies the caller and is never cached in either
  direction, so a transient database blip costs one denial, never a TTL-long
  one. `app_mcp_token` rows themselves are never revoked by any of this —
  the token stays valid; only the channel's willingness to honour it changes.
- **Routing runs on the same candidate providers as every other channel.**
  `AppMCPRoutingService.route_message` composes `ChannelCandidateProvider`
  (the caller's own agents, narrowed to the resolved agent scope) with
  `IdentityCandidateProvider` (always called with the resolved policy; it
  returns `[]` when the caller's `allow_identity_routing` is off) —
  the identical composition, in the identical order, Pass 1 uses below. A
  test asserts the two surfaces produce identical candidate lists for the
  same user, which is the property this whole refactor exists to guarantee.
  **Not shared:** Pass 2 (auto-install catalog) and pinning. App MCP routing
  is two steps — classify, or return nothing — with no catalog fallback and
  no `channel_user_setting.pinned_agent_id` honoured; `allow_auto_install` on
  the App MCP channel row is therefore inert as of this phase.
- **`ChannelIngestionService.assert_access`'s `mcp_caller` arm was tightened
  in the same phase.** It used to trust that the (now-deleted) App MCP routing
  layer had verified the caller has a route to the agent, checking nothing
  else. It now requires the agent be owned by the caller, **or** a
  re-verified identity grant (`IdentityService.verify_identity_access`, the
  same six conditions `channel_caller` uses) — see
  [App MCP Server — Access Control](../app_mcp_server/app_mcp_server.md#access-control-on-the-underlying-session).

### Trust chain and fail-closed defenses

The inbound webhook is **platform-unauthenticated by design** — anyone on the internet can `POST` to it. What actually stands between the open internet and creating an agent session, in the order it is checked:

1. **Rate limiting and a body-size cap** run before anything else is even parsed.
2. **Webhook-token resolution** — an unknown token *and* a disabled channel both answer with an identical, detail-free 404. Disabling a channel must not become an oracle that tells a prober "this token used to exist."
3. **Adapter verification is the single authentication chokepoint and runs before anything else touches the payload.** For Google Chat this means checking a bearer JWT against Google's public keys (issuer, audience, signature) — nothing downstream re-checks it, and the whole request is rejected (403, no detail) if it fails. A **polled** channel has no request to run this check against at all — a `POST` to the webhook route for a polled channel's token is refused outright (`ChannelTransportMisuseError`, a `ChannelVerificationError` subclass, so it gets the identical detail-free 403) rather than silently accepted; its authentication instead happens inside `poll()`, on the timer, before a fetched message ever reaches this pipeline. See [Transport shapes](#transport-shapes-webhook-polled-authenticated).
4. **The email whitelist fails closed.** A null or empty whitelist denies *everyone* — an unconfigured channel is a channel nobody can use, not an open one. `*` is the only pattern that allows all verified senders. The whitelist is a comma-separated list matched by any-token-matches semantics, so `"*, ops@corp.com"` is still a blanket allow, not a scoped list with one extra address — this is the single easiest thing to misread about the feature, and both the admin help text and the admin UI itself surface it as an explicit warning rather than leaving it implicit.
5. Only after all of the above does a sender's email get resolved to (or used to create) a platform account.
6. **Channel policy — the admin kill switch, the access grant, and the sender's own toggle — is resolved once and gates every path below it, not only a brand-new thread.** A sender whose access has since been revoked is declined on an existing, already-bound thread exactly as a first-time sender would be; see [Availability and per-user settings](#availability-and-per-user-settings).

A sender's email is trusted only because the adapter verified it came from a payload the transport itself signed — the same trust tier the email integration extends to a verified IMAP sender.

### Registration

- **The channel's own email whitelist is the sole registration gate.** The platform-wide `AUTH_WHITELIST_USER_DOMAINS` signup allowlist is deliberately **not** re-checked — same precedent as the email integration's auto-user creation, so the two features can't silently disagree about who's allowed to have an account.
- Auto-registered users are ordinary, passwordless, already-email-confirmed `agent-user` accounts — every downstream limit and gate (agent-count limits, credential isolation, catalog visibility) applies to them exactly as it would to anyone else. If the person later logs in with Google OAuth on the same address, the existing by-email account linking picks the account up naturally.
- An inactive account (`is_active=False`) is treated as a denial, indistinguishable from a whitelist miss.
- Registration only happens when `auto_register_users` is on; otherwise an unknown sender is simply denied.

### Routing

- **A pin is answered before anything else runs.** If the sender has pinned one of their own agents to this channel (see [Availability and per-user settings](#availability-and-per-user-settings)), classification is skipped entirely — no candidates are built, no short-circuit is evaluated, and Pass 2 does not run, even if the pin fails to resolve (the agent was deleted, or changed hands). The decision still gets a full trace row (`match_method="pinned"`); everything below this bullet describes the unpinned path.
- **Pass 1 — the sender's own agents, constructed directly, not filtered down from another surface's set.** The candidate list is built from `Agent.owner_id == user.id` — every agent the sender owns — narrowed to the resolved channel **agent scope**: `"all"` admits every owned agent (unchanged from before this phase), `"list"` admits only the sender's saved selection, `"none"` admits nothing. An agent excluded by scope is recorded as a skip (`SKIP_NOT_IN_CHANNEL_SCOPE`) rather than dropped. Within scope, an agent is an eligible candidate when it has a non-blank router trigger prompt **or** non-empty prompt examples; one with neither is recorded as a different skip (`SKIP_NO_TRIGGER_PROMPT`) — a candidate list showing only the finalists cannot explain the failure that actually bites. Nothing in this half of Pass 1 reads `AppAgentRoute`, `AppAgentRouteAssignment`, or `UserAppAgentRoute` — that whole family is deleted as of Phase 5. (`SKIP_IDENTITY_ROUTE` is kept in the trace vocabulary purely so the admin UI can still render decisions captured before the scope split; it has no live producer.) Pass 1's own candidate-building code — `ChannelCandidateProvider` — is now the same code [App MCP Server](../app_mcp_server/app_mcp_server.md) calls, not merely a parallel implementation, but each channel row (Google Chat, App MCP) resolves its **own** enablement state (`ChannelPolicyService.resolve` against that row) — toggling one channel's agent scope has no effect on another's. Pass 1 also shares `AgentClassifier.classify` with every other routing consumer — see [Auto Routing Tuning](../routing_tuning/routing_tuning.md).
- **Pass 1, identity half — the people the sender may address, appended only on their own opt-in.** When the sender's resolved `allow_identity_routing` is on, `IdentityCandidateProvider` appends one candidate per identity **owner** they can currently reach (never one per binding), each with the namespaced `ref_id` `identity:{owner_id}` so a person can never be looked up as an agent. Owned agents are listed first and identities after — that ordering is for the trace and the prompt to read top-down, and nothing turns on it. When the classifier picks a person, the decision hands off to **identity Stage 2**, which picks one of *that person's* agents; see [Identity Routing](../identity_routing/identity_routing.md). Both of Stage 2's answers are terminal: an agent (routed, with a grant re-verified at ingest) or nothing (an ordinary `no_match` — Pass 2 does **not** run, because auto-installing a catalog bundle is not an answer to "ask HR about my time off"). An identity owner with nothing currently reachable is recorded as a `SKIP_IDENTITY_UNAVAILABLE` skip; an identity the sender could have reached with the switch **off** is deliberately recorded not at all — see [Availability and per-user settings](#availability-and-per-user-settings).
- **A conditional `only_one` short-circuit.** When the whole Pass-1 ballot holds exactly one candidate, Pass 1 routes to it without asking a model — but only when Pass 2 has nothing to offer this sender either (an exhausted or unreachable auto-install catalog, **or Pass 2 barred by policy — see below**). The rule is that a short-circuit is sound only when there is genuinely no alternative to choose between, and Pass 2's candidates are part of that choice space: a newly auto-registered sender owns zero agents, and the moment Pass 2 onboards them they own exactly one — an unconditional short-circuit would make that onboarding message the last one that could ever reach the catalog. So the one-candidate branch probes the catalog for availability (not classification) before deciding: zero or unreachable ⇒ route without a model call; anything available ⇒ classify anyway so "none of mine" stays a reachable answer. Two or more eligible candidates always classify, and a failed catalog probe is treated as "something might be there" rather than "nothing is," so an outage costs an LLM call, never a silently different route. **That sole candidate can be an identity** — a sender who owns no agents and can reach exactly one identity owner is not a rare shape, especially right after auto-registration — in which case the short-circuit hands straight off to identity Stage 2 rather than to an agent. The probe still writes the full trace — the scanned-but-unclassified candidates land under the `pass_2` stage with a `stage.reason` note explaining they were an availability check, not a classification.
- **Pass 2 — the server-wide auto-install list**, tried only when Pass 1 finds nothing **and** this sender's resolved channel policy allows it: `allow_auto_install` is on, the resolved agent scope is `"all"` (not `"list"` or `"none"` — see [Availability and per-user settings](#availability-and-per-user-settings) for why that is a product decision and not `allow_auto_install` read twice), and there is no pin. When Pass 2 is barred by policy the trace records why, rather than showing an empty catalog scan. Candidates are the union of bundles on the list that the sender hasn't already installed, filtered through the same catalog visibility check every other install path uses, and that carry a router trigger prompt to classify against. **Membership on the auto-install list is never an implicit permission grant** — a non-public bundle nobody has shared with the sender simply never becomes a candidate for them, no matter how loudly it "matches."
- A bundle already installed by the sender (including the publisher's own working install) is excluded from Pass 2 — it had its chance to match in Pass 1.
- **Every routing decision is now durably recorded — a deliberate, documented exception to this feature's usual no-message-text-at-rest stance.** The [Auto Routing Tuning](../routing_tuning/routing_tuning.md) feature persists a `routing_decision` row per decision (candidates considered, including rejected ones, and the verdict), and that row includes the sender's own message text, clamped and kept for `ROUTING_TRACE_RETENTION_DAYS` (default 14 days) before automatic purge. Only a superuser can read it. Turning `ROUTING_TRACE_STORE_MESSAGE_TEXT=False` withholds the sender's own words wherever they appear in the trace — not just the message field, but anywhere else in the record that could carry a copy or rewrite of them — while still answering "which agents were even considered" and keeping the trace replayable via an always-present `message_sha256`. This hides, it does not erase: the underlying rows keep their data until `ROUTING_TRACE_RETENTION_DAYS` expires them or an admin clears the traces. See [Channel Debug Monitor tech](channel_debug_monitor_tech.md) and [Auto Routing Tuning tech](../routing_tuning/routing_tuning_tech.md).

### Identity routing: whose thread, whose workspace

Since Phase 3 of the channels & identity unification, a channel message can open a session on an agent the **sender does not own**. That makes two ownerships diverge, and each answers a different question:

- **`ChannelThreadBinding.user_id` stays the sender** — *whose thread is this?* Thread ownership is what stops one member of a group chat space from posting into another person's conversation, and it is unchanged: a second person posting into an identity-routed thread is still declined as "belongs to someone else".
- **`session.user_id` becomes the identity owner** — *whose workspace is answering?* The agent is theirs, runs on their credentials and in their space, and the session appears in **their** session list, not the sender's. The sender's `GET /sessions/` does not return it. The owner sees a "Via Identity — {caller}" badge (from `identity_caller_name` in `session_metadata`), because they would otherwise find a conversation they never started containing a stranger's message.

So `ChannelThreadBinding.agent_id` names an agent the binding's own user does not own. That is legitimate only because of the identity grant, and only for as long as the grant keeps verifying.

- **`integration_type` stays `channel_<type>`.** An identity-routed channel session is still a channel session; that is what makes the reply deliverable, since `ChannelOutboundService` resolves the outbound binding by gating on the `channel_` prefix. Stamping such a session `identity_mcp` would route correctly, run correctly, and never deliver a word.
- **Both consents are re-read on every message.** The identity grant is re-verified in full (all six conditions, against the database) on every message — rebuilt from the session row on a resume rather than cached — so an owner who deactivates a binding or an assignment mid-thread is honoured on the next turn. And the *sender's own* `allow_identity_routing` is re-read from that message's single policy resolution, so switching it off (or using "reset to defaults", which drops the row and returns the column to its `false` default) stops the existing identity thread on its next message, not merely the next new one. A consent that could not be withdrawn on the conversation it authorised would be no consent at all.
- **The decline is generic, and the thread is not bricked.** Both refusals produce the same detail-free reply every other failure gets — naming the gate would be an oracle for an external sender. The binding then fails, and a failed binding self-heals: the next message deletes it and re-routes over the sender's **own** agents.
- **Recovery never invents a grant.** If the bound session was deleted, the next message arrives with no grant; on a foreign agent it is refused rather than re-authorised from the binding, because nothing in that call is evidence the identity is still shared.

### Thread binding lifecycle

- A binding starts `pending_install` while an auto-installed environment builds, and messages that arrive in the meantime are parked (up to a cap; beyond it, the newest arrival is refused with an explicit "I've got a lot queued, ask again" reply rather than silently dropped).
- It becomes `active` once the environment reaches `running` and any parked messages have been delivered in order.
- `failed` is not a dead end: the **next** inbound message on that thread deletes the failed binding and re-routes from scratch, so a one-off build failure never wedges a thread permanently.
- **A thread belongs to exactly one person.** If a different whitelisted person posts into a thread already bound to someone else, they're told the conversation belongs to someone else rather than being silently merged into that person's session — this applies both when the binding already existed and when two people's first messages in a brand-new thread race each other (the loser of that race gets the same refusal, whether their message is caught at the immediate-ingest step or while it's being parked behind a slower auto-install).
- Uninstalling the bound agent cascades away the binding (next message re-routes, and the reinstall picks the same App Data back up); deleting the bound session only clears the pointer (next message opens a fresh session on the same agent).

### Two accepted divergences from the original plan

- **A `critical_state` environment does not fail a binding.** `critical_state` means the container is up and running, but a post-start provisioning step (a package install, a credential sync) failed — sessions, chat, and terminals keep working. Since the environment would actually have answered the sender, only a genuine `status == "error"` (or `deprecated`) fails a binding; a degraded-but-running environment just keeps waiting like any other in-progress build.
- **The Google JWKS verification hardening was fixed where the keys are fetched, not where they're used**, so both this feature and the two pre-existing Google OAuth login paths get the fix for free with identical external behavior. A JWKS fetch failure is treated as "cannot verify right now" (denied, but logged and distinguishable from a forged signature) rather than misreported as an invalid token.

### Admin surface rules

- Every admin route is superuser-only — there is no partial, role-based access to channel administration, because a channel holds an outbound credential and decides who can reach agents at all.
- The outbound secret is write-only: it is never echoed back by any read endpoint, and editing any other field of a channel leaves a previously-stored secret untouched.
- Regenerating the webhook token is a deliberate, confirmed action — it immediately invalidates the URL pasted into the external chat app's configuration.

## Availability and per-user settings

Every channel carries four admin-owned defaults — `visibility`, `default_enabled_for_users`, `default_agent_scope`, `allow_auto_install` — and every platform user may override some of them for themselves from Settings → Channels. What a given sender actually gets to do is the *resolution* of those two layers, computed fresh for every message by `ChannelPolicyService` (see [tech](server_channels_tech.md)) and never re-derived anywhere else — not the router, not the API projection the settings UI reads, and not the frontend itself. Two implementations of the inherit rules do not stay equal; they drift into a UI that shows a channel as on while the router treats it as off, which is undiagnosable from either side.

> **Absence of a `channel_user_setting` row means "the channel default applies."** Rows are created lazily, on the user's first edit, and nowhere else.

This is the single most important thing in this phase, and the thing a future reader is most likely to break by "fixing" it. It would be natural to materialize a settings row for every user the moment a channel is configured, or the moment a new account signs up — and it would be wrong for two populations this feature exists to serve, and that will **never** have a row of their own:

- **users the channel auto-registers** — a Google Chat sender who has never opened the platform's UI has no session in which a row could be created;
- **every user created after the channel was configured** — nothing retroactively backfills a row for them either.

A design that requires the row to exist, or that reads a missing row as "off", silently switches the channel off for exactly the people auto-install exists to onboard.

### Why NULL, not a stored boolean

`is_enabled` and `agent_scope` are nullable, with no server default — not `bool NOT NULL DEFAULT true`. The distinction matters because of what happens when an admin later changes their mind. If a user who never expressed an opinion had `is_enabled=true` materialized into their row as a convenience, an admin who later flips `default_enabled_for_users` to `false` would switch the channel off for everyone **except** the people who happen to have a row — the opposite of what "default" is supposed to mean, and invisible in the UI, because a stored `true` looks exactly like a deliberate user choice. `NULL` follows the admin default wherever it goes; a stored value freezes the user against it. Only an explicit user action — saving a change in Settings → Channels — ever writes a non-NULL value.

### Visibility and grants

A channel's `visibility` is `"public"` (every platform user may use it) or `"restricted"` (only users an admin has explicitly granted, from the channel's admin allowlist). A restricted channel with no grant rows is not "nobody's decided yet" — it genuinely admits no one until an admin adds them. Grants are consulted only when visibility is restricted; a public channel is never asked about them at all, so the absence of grant rows means something different on a public channel (the question was never asked) than on a restricted one (the answer is no).

Being granted access is not the same as being switched on: a granted user still resolves their own enablement, below, against the channel default. Both must hold for the channel to be usable by that person.

### Default enablement and the per-user toggle

`default_enabled_for_users` decides whether a user with no settings row starts on or off. A user who opens Settings → Channels and flips the switch writes an explicit `is_enabled` that stops following the admin default — until they use "reset to defaults," the only way back to pure inheritance once a value has been written.

### Agent scope

`default_agent_scope`, and the per-user `agent_scope` that can override it, decide which of a user's own agents are routing candidates on this channel: `"all"` (every owned agent — today's behaviour, and what an existing channel keeps unchanged by this phase), `"list"` (only the agents the user has explicitly added), or `"none"` (nothing routes until the user opts agents in). An agent excluded by scope is never silently dropped from a trace — it is recorded as a skip (`SKIP_NOT_IN_CHANNEL_SCOPE`), because a trace that shows only the finalists cannot answer the question a confused user actually asks: "I own three agents — why did none of them answer?"

### Pinning an agent

A user may pin one of their own agents to a channel. A pin answers the routing question outright: classification is skipped entirely for that user on that channel, and the pinned agent is used directly, with its own trace entry (`match_method="pinned"`) so a pinned decision is never invisible the way a skipped trace would be. Ownership is checked both when the pin is saved and again every time it is resolved, because a foreign key only guarantees the pinned agent still exists, never that this user still owns it — an agent that changed hands un-pins rather than silently routing a stranger's message into its new owner's workspace.

### Identity routing (the one setting that is not about your own agents)

Every other per-user field on this card narrows or widens which of the sender's **own** agents can answer. `allow_identity_routing` is different in kind: it lets a message reach an agent belonging to *somebody else*, in a session that lives in that person's workspace and that they can read.

The Settings → Channels expander carries it as a master switch plus, below it, the list of people who have shared an identity with this user. Three properties are decided, not incidental:

- **Off by default, and no inheritance path back.** Every other field renders an honest "following the admin default (on)" caption; this one deliberately has no such caption, because there is no channel default to follow. An administrator cannot consent on someone's behalf to their conversations being readable by a third person.
- **The switch copy states the consequence, unconditionally and not in a tooltip** — the message and the whole conversation then live in that person's workspace, and switching the setting off later stops future messages without taking back one already sent. This was always true of Identity Routing; a chat app makes it far more visible.
- **One identity toggle, not two.** The per-person switches under the master switch are the existing person-level `IdentityBindingAssignment.is_enabled` (read and written through `/users/me/identity-contacts/`), the same toggle Identity Routing uses — deliberately reused rather than duplicated per channel. A per-channel identity allowlist would be a second source of truth for "may I address this person", and the two would drift. The card says so out loud, since turning someone off here also stops the user addressing them from an MCP client.

The list distinguishes "nobody has shared with you" from "the request failed" — a failed fetch must never render as a claim about other people's sharing decisions — and warns when the master switch is on while every person is off, an otherwise silent "on but inert" state.

### Auto-install permission

`allow_auto_install` decides whether Pass 2 — auto-installing a bundle from the server-wide catalog for a sender who matched nothing they already own — may run at all for this channel. It makes explicit something Google Chat did implicitly before this phase (Pass 2 always ran); the default is `True` so an existing channel's behaviour is unchanged by the migration that adds it.

### Decisions worth stating explicitly

These are decided product semantics, not incidental implementation choices — a future change to any of them should be treated as a deliberate re-decision, not a bug fix.

- **`allow_identity_routing` never inherits.** Unlike every other per-user field on the settings row, it is `NOT NULL DEFAULT false` and has no channel-level default at all — an admin default must not be able to turn on something that routes a message into another person's workspace. That consent belongs only to the person whose message it is; nothing above them can supply it on their behalf. It is read on every message (see [Identity routing](#identity-routing-whose-thread-whose-workspace) and the section below), and it is the **sender's** consent, resolved from the sender's own row — "I accept that a message I send on this channel may be routed into somebody else's workspace, where they can read it." It is emphatically not the receiver's gate; the receiver's controls are `IdentityAgentBinding.is_active` and `IdentityBindingAssignment.is_active`.
  - **What the switch does and does not close.** It governs identity routing on **channels and App MCP** — the two surfaces that resolve a `ResolvedChannelPolicy` and compose `IdentityCandidateProvider`. It does **not** govern **External A2A** identity access: `POST /external/agents/{id}/message` with `target_type="identity"` is authorized by its own mechanism — `ExternalAccessPolicy.require_identity_access(db, user, owner_id)`, which requires at least one active `IdentityAgentBinding` with an active assignment for the caller — and never reads `allow_identity_routing`. So a caller with the channel switch off can still reach an identity over External A2A if the owner has bound them. This is the current design as built, stated here so nobody infers a coverage it does not have; whether A2A should also consult the switch is an open product question tracked separately, not a defect being fixed in this doc.
- **Changing `allow_identity_routing` is audited; changing its neighbours is not.** A transition (and only a transition — a save that leaves the value where it was writes nothing) records a `SERVER_CHANNEL_IDENTITY_ROUTING_CHANGED` `SecurityEvent` at severity `medium`, carrying the channel and the new value and never any message text. `is_enabled`, `agent_scope`, `pinned_agent_id` and the agent list only widen or narrow what the sender reaches among their *own* agents, and cost them nothing they did not already have; this one changes whose workspace a message can end up in, and the settings row holds only the current value, so "when did this become true, and who made it so" cannot be reconstructed from it afterwards. The audit is best-effort — it never fails the request whose change already landed.
- **A revoked sender is declined on existing threads too, not only new ones.** An admin disabling the channel, withdrawing a grant, or the user's own switch all stop an already-bound conversation the next time a message arrives on it — not just the next brand-new thread. `ServerChannel.enabled` is documented as an absolute kill switch, and it would not be one if a thread that opened before it flipped kept quietly answering afterward.
- **The decline is deliberately indistinguishable from a whitelist miss.** Whether a sender is turned away because they were never whitelisted, because a restricted channel never granted them, because an admin disabled the channel, or because the sender switched it off for themselves, they see the same reply and the same status. Telling these apart from the outside would let anyone probe a server's channel configuration one message at a time — "you're not whitelisted" says nothing about the server, but "you're not granted this channel" confirms the channel exists and that access control is the reason it was refused. A superuser can still see exactly which term of the conjunction failed, in the admin debug feed; the sender never can.
- **Pass 2 does not run unless the resolved agent scope is `"all"`.** This is not `allow_auto_install` read a second time — it is a separate, deliberate bar. A sender who has restricted their channel to a specific list of agents, or to none, has said "nothing routes here but these"; installing a bundle whose agent is, by construction, out of scope would create a real side effect — an install, an environment build — on a path that is guaranteed to dead-end, because the newly installed agent can never become a routing candidate under a scope that excludes it. The restriction is read as the instruction it is, rather than performed around on the theory that it might become useful once the sender manually widens their own list.
- **A pinned channel never auto-installs**, and that holds even when the pin fails to resolve — the pinned agent was deleted, or changed hands. The sender's instruction ("everything I send here goes to this one agent") stands whether or not the platform can currently honour it; installing a catalog bundle to route around a dead pin would be the router overruling the person it is routing for.

## Architecture Overview

```
Google Chat ──webhook──▶ POST /api/v1/channels/{webhook_token}/inbound
                              │ 1. rate limit + body-size cap
                              │ 2. resolve channel (404: unknown OR disabled)
                              │ 3. adapter.verify_inbound — Google JWT, fails closed (403)
                              │ 4. redelivery dedup
                              │ 5. whitelist check — fails closed
                              │ 6. resolve / auto-register user
                              │ 7. channel policy — kill switch AND grant AND user toggle;
                              │    declined identically to a whitelist miss, on existing
                              │    threads too, not only new ones
                              ▼
                    binding lookup (channel, thread)
                    ── active ─────▶ continue thread (background) ─▶ ChannelIngestionService
                    ── pending ────▶ park message, "still setting up" reply
                    ── failed ─────▶ delete binding, fall through to routing
                    ── missing ────▶ webhook acks immediately; routing runs as a background task:
                                        Pass 1: sender's own agents (owned-agent candidates: trigger prompt or examples)
                                              + identity candidates (one per person), ONLY if allow_identity_routing
                                          → a person wins? identity Stage 2 picks one of THEIR agents
                                             → session in the OWNER's space, binding stays the SENDER's,
                                               integration_type still channel_<type>; Pass 2 does not run
                                        Pass 2: server auto-install list (AI classification, catalog-gated)
                                          → install bundle, binding(pending_install), park message

channel_pending_scheduler (every 45s, TESTING-gated)
                              │ env running        → deliver parked messages, binding → active
                              │ env error/deprecated → binding → failed, notify sender
                              │ env critical_state  → NOT a failure — still waiting
                              │ still building, too long → binding → failed (bounded wait)

Agent reply (STREAM_COMPLETED) ──▶ ChannelOutboundService ──▶ binding lookup by session_id ──▶ adapter.send_message
Agent error (STREAM_ERROR)     ──▶ same path, generic failure notice
```

This diagram is the **webhook** path (Google Chat). A **polled** channel
(Email) has no webhook and no `POST` at all: `channel_poll_scheduler` calls
`ChannelPollService.poll_enabled_channels` on a timer, which calls
`adapter.poll(channel)` per enabled polled channel and feeds each returned
message into `ChannelInboundService.process_inbound` — the identical
post-verification entry point step 4 onward reaches after `verify_inbound`
succeeds above. Everything from "binding lookup" downward is unchanged
between the two transports. See
[Email Sessions — Architecture Overview](../email_integration/email_sessions.md#architecture-overview)
for the polled version of this diagram.

## Integration Points

- [Agent Sessions / Channel Ingestion](../agent_sessions/channel_ingestion.md) — Server Channels is a caller of the canonical inbound pipeline, adding a `channel_caller` `SessionSender` kind that behaves like `task_executor`: the session is owned by the sender's own resolved user, never the agent's publisher. **One exception, since Phase 3 of the channels & identity unification:** an identity-routed message opens the session in the *identity owner's* space, permitted solely by a `ChannelAccessPolicy.identity_grant` whose six conditions `ChannelIngestionService.assert_access` re-reads from the database on every message. The sender kind does not change — it names the *transport*, and the transport is still a channel.
- [Identity Routing](../identity_routing/identity_routing.md) — identity is a routing-layer concept, and Server Channels is its second consumer after App MCP. `IdentityCandidateProvider` supplies the person candidates; `IdentityRoutingService` is Stage 2, called from inside `ChannelRoutingService.decide`. The person-level contact toggle is shared with App MCP; the per-channel `allow_identity_routing` opt-in is the channel's own and never inherits.
- [App MCP Server](../app_mcp_server/app_mcp_server.md) — as of Phase 5, App MCP **is** a `ServerChannel` row (`channel_type="app_mcp"`, the platform's `authenticated` transport) and shares this feature's admin model, `ChannelPolicyService`, `ChannelCandidateProvider`, and `AgentClassifier.classify` outright — not a parallel implementation reaching the same conclusions, but the same code, called with a different channel row. Each channel row still resolves its **own** enablement state, so toggling Google Chat's agent scope has no effect on App MCP's, and vice versa. A freshly auto-installed agent is immediately reachable on both surfaces for the same reason: bundle install auto-populates `Agent.router_trigger_prompt` from the revision snapshot, and both `ChannelCandidateProvider` callers read that field directly.
- [Agent Bundles & Installs](../../agents/agent_bundles/agent_bundles.md) — Pass-2 auto-install uses the same idempotent `InstallService.install_bundle` entry point every other programmatic install uses, and is gated by `CatalogService.user_can_install` visibility.
- [Email Integration](../email_integration/email_integration.md) — since Phase 4 of the channels & identity unification, email **is** a Server Channels transport (`channel_type="email"`, a `PolledChannelTransport`), not a separate pipeline that merely mirrors this one's precedents. The email-specific behaviour — the `From:`-header trust tier, recipient validation on a shared mailbox, the durable `OutgoingEmailQueue` outbound path, the poll scheduler — is documented there; whitelisting, auto-registration (`UserService.create_external_user`, shared by both transports), two-pass routing, and thread bindings are the shared machinery documented here.
- [Mail Servers](../email_integration/mail_servers.md) — the admin-owned IMAP/SMTP connections an email channel references by id in its `config`, the way a Google Chat channel references its own service account.
- [Google OAuth](../auth/google_oauth.md) — the Google Chat adapter's JWT verification reuses the same generalized, cached JWKS verifier the Google OAuth login path uses, pointed at a different issuer and key set.
- [Server Configuration](../server_configuration/disclaimer.md) — Channels is a new tab on the same `/admin/server-configuration` admin page as the Disclaimer feature, following the same superuser-guard and HashTabs conventions.
- [Realtime Events](../realtime_events/event_bus_system.md) — outbound delivery subscribes to `STREAM_COMPLETED` / `STREAM_ERROR` the same way the email integration's sending service does.
- [AI Functions](../../development/backend/ai_functions_development.md) — Pass-2 classification is a second caller of the same `AgentClassifier.classify` (`backend/app/services/routing/agent_classifier.py`) the App MCP router calls — the classifier every routing consumer shares as of [Auto Routing Tuning](../routing_tuning/routing_tuning.md)'s Phase 5. `AIFunctionsService.route_to_agent` is a thin adapter kept for callers outside routing, not the path either pass calls today.
- [Auto Routing Tuning](../routing_tuning/routing_tuning.md) — Server Channels produces the `server_channel` and `email` routing traces: `ChannelInboundService` is the sole real-path caller of `ChannelRoutingService.decide()`, and it owns the one transport→origin map that decides which value a channel decision carries (`google_chat` → `server_channel`, `email` → `email` as of Phase 6 of the channels & identity unification, an unmapped transport falling back to `server_channel` rather than failing a delivery). App MCP is the third `ServerChannel` and writes its own traces (`origin="app_mcp"`) from `AppMCPRoutingService`, not through this pipeline. The live [Channel Debug Monitor](channel_debug_monitor.md) feed's `detail.trace_id` points at the durable `routing_decision` row that feature writes.

## Known Limitations

- **The admin UI has not been visually verified in a browser.** It passed TypeScript, lint, and build checks, but no interactive/visual QA pass was available during development — treat the first real admin session as the first real look at it.
- **Outbound delivery is best-effort for webhook transports (Google Chat)**: three immediate retries inside the adapter, then the failure is recorded on the binding and logged. There is no persistent outbound queue for this transport — a sender whose reply was lost can only ask again. **Email is the exception**: since Phase 4, its outbound goes through the existing, durable `OutgoingEmailQueue` (retried by the pre-existing sending scheduler) rather than through this best-effort path — see [Email Sessions](../email_integration/email_sessions.md).
- **The pending-binding scheduler assumes a single backend process.** There is no leader election; a multi-worker deployment would need the same advisory-lock leader pattern (and the same connection-pool caveat) used by the model-discovery scheduler, or parked messages could be delivered more than once. The email channel poll scheduler (`channel_poll_scheduler`, added in Phase 4) makes the identical single-process assumption, for the identical reason — see [Email Integration](../email_integration/email_integration.md#single-process-poller-known-limitation). Neither should be "fixed" by copying the model-discovery scheduler's advisory-lock leader pattern: it has a known connection leak on pooled connections.
- **Deferred (known, intentionally not fixed here):** rejection/verification-failure audit events are attributed to the channel's creator (`ServerChannel.created_by`); if that superuser account is later deleted, the foreign key nulls out and denial/verification-failure auditing for that channel silently stops being written as `SecurityEvent` rows (it still reaches the application log).
- **~~Identity routing is not reachable from `POST /admin/routing/simulate`.~~ Closed in Phase 6.** `RoutingSimulateRequest` now carries an optional `channel_id`; naming one resolves that channel's real policy for the target user instead of `ResolvedChannelPolicy.for_no_channel()`, whose `allow_identity_routing` is `False` — permissive on everything else, deliberately not on this, because the absence of a channel is not a person's consent. So a hand-typed simulate can now put an identity candidate on the ballot, and a run naming no channel still cannot, by design. A `channel_id` that names no channel is a 404, refused before the run spends an LLM call.
- **~~A channel decision can carry `SKIP_IDENTITY_UNAVAILABLE` but a channel user cannot read the explanation.~~ Closed in Phase 6.** `?expected_agent_id=` widened from a `uuid.UUID` to a candidate **ref**, so it accepts the `identity:{owner_id}` an identity candidate carries and the skip-explanation branch can name a person. Producible and explainable are facts about different layers; both are true now. What remains is Phase 7's: no admin UI control sends the parameter yet.

---

*Last updated: 2026-08-25*
